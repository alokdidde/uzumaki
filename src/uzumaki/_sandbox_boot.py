"""Restricted-worker bootstrap. Runs as a script in a fresh ``python -I -S -B`` child.

Do not import this module from the harness. ``uzumaki.sandbox`` starts it by file path in a new
session with a scrubbed environment and only the broker pipes open. Before it reads any candidate
code it applies, in order: resource limits, ``no_new_privs``, a Landlock domain (filesystem
allowlist, TCP denied, signals and abstract UNIX sockets scoped to the domain) and a seccomp
filter (no sockets, no new processes or exec, no namespace, ptrace, rlimit or mount changes). It
then checks that the restrictions hold and reports ``ready``. Only after that does it load and
run the candidate. Everything here is stdlib-only and self-contained, because the child cannot
read the harness package once restricted.

The restrictions are enforced by the kernel. Nothing here relies on Python-level checks: the
candidate runs in this same process and can use ``ctypes``, so it can do anything this process
is still allowed to do, and nothing more.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import resource
import socket
import struct
import sys
from typing import Any

CONTAINMENT_EXIT = 70

# Linux x86_64 only (the verified platform). Syscall numbers from arch/x86/entry/syscalls.
_SYS = {
    "socket": 41,
    "socketpair": 53,
    "clone": 56,
    "fork": 57,
    "vfork": 58,
    "execve": 59,
    "ptrace": 101,
    "setpgid": 109,
    "setsid": 112,
    "chroot": 161,
    "pivot_root": 155,
    "setrlimit": 160,
    "mount": 165,
    "umount2": 166,
    "reboot": 169,
    "init_module": 175,
    "delete_module": 176,
    "kexec_load": 246,
    "add_key": 248,
    "request_key": 249,
    "keyctl": 250,
    "unshare": 272,
    "perf_event_open": 298,
    "prlimit64": 302,
    "name_to_handle_at": 303,
    "open_by_handle_at": 304,
    "setns": 308,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "finit_module": 313,
    "kexec_file_load": 320,
    "bpf": 321,
    "execveat": 322,
    "userfaultfd": 323,
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "open_tree": 428,
    "move_mount": 429,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "fspick": 433,
    "clone3": 435,
    "pidfd_getfd": 438,
    "mount_setattr": 442,
}
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_DENIED = tuple(n for n in _SYS if n not in ("clone", "clone3"))

_PR_GET_SECCOMP = 21
_PR_SET_SECCOMP = 22
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_SECCOMP_MODE_FILTER = 2

_AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_EPERM = 1
_ENOSYS = 38
_CLONE_THREAD = 0x00010000
_CLONE_NAMESPACES = (
    0x00020000  # NEWNS
    | 0x02000000  # NEWCGROUP
    | 0x04000000  # NEWUTS
    | 0x08000000  # NEWIPC
    | 0x10000000  # NEWUSER
    | 0x20000000  # NEWPID
    | 0x40000000  # NEWNET
)

# Landlock access rights (include/uapi/linux/landlock.h).
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_TRUNCATE = 1 << 14
_FS_ALL = (1 << 16) - 1  # every filesystem right up to IOCTL_DEV (ABI 5 and later)
_FILE_ONLY = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | (1 << 15)
_NET_ALL = (1 << 0) | (1 << 1)  # BIND_TCP | CONNECT_TCP
_SCOPE_ALL = (1 << 0) | (1 << 1)  # ABSTRACT_UNIX_SOCKET | SIGNAL
_RULE_PATH_BENEATH = 1
_CREATE_RULESET_VERSION = 1 << 0
MIN_LANDLOCK_ABI = 6

_READ = _FS_READ_FILE | _FS_READ_DIR
_SCRATCH = (
    _FS_READ_FILE
    | _FS_READ_DIR
    | _FS_WRITE_FILE
    | _FS_TRUNCATE
    | _FS_REMOVE_DIR
    | _FS_REMOVE_FILE
    | _FS_MAKE_DIR
    | _FS_MAKE_REG
)


class ContainmentError(Exception):
    """A restriction could not be applied or verified. Carries a fixed code."""


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.restype = ctypes.c_int


def _check(result: int, code: str) -> int:
    if result < 0:
        raise ContainmentError(code)
    return result


def _set_limits(limits: dict[str, int]) -> None:
    cpu = limits["cpu_seconds"]
    pairs = (
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_CPU, cpu, cpu + 1),
        (resource.RLIMIT_AS, limits["memory_bytes"], limits["memory_bytes"]),
        (resource.RLIMIT_FSIZE, limits["max_file_bytes"], limits["max_file_bytes"]),
        (resource.RLIMIT_NOFILE, limits["max_open_files"], limits["max_open_files"]),
    )
    try:
        for which, soft, hard in pairs:
            resource.setrlimit(which, (soft, hard))
    except (OSError, ValueError):
        raise ContainmentError("rlimit_failed") from None


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _landlock_abi() -> int:
    abi = int(_libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION))
    return abi if abi > 0 else 0


def _apply_landlock(read_paths: list[str], write_paths: list[str]) -> int:
    abi = _landlock_abi()
    if abi < MIN_LANDLOCK_ABI:
        raise ContainmentError("landlock_unavailable")
    attr = _RulesetAttr(_FS_ALL, _NET_ALL, _SCOPE_ALL)
    size = ctypes.sizeof(attr)
    created = int(_libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), size, 0))
    ruleset = _check(created, "landlock_ruleset_failed")
    try:
        for paths, access in ((read_paths, _READ), (write_paths, _SCRATCH)):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except FileNotFoundError:
                    continue
                try:
                    allowed = access if os.path.isdir(path) else access & _FILE_ONLY
                    rule = _PathBeneathAttr(allowed, fd)
                    _check(
                        int(
                            _libc.syscall(
                                _SYS_LANDLOCK_ADD_RULE,
                                ruleset,
                                _RULE_PATH_BENEATH,
                                ctypes.byref(rule),
                                0,
                            )
                        ),
                        "landlock_rule_failed",
                    )
                finally:
                    os.close(fd)
        _check(int(_libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)), "landlock_failed")
    finally:
        os.close(ruleset)
    return abi


def _seccomp_program() -> bytes:
    """Assemble a classic-BPF seccomp filter (deny-list on the verified x86_64 ABI)."""
    ld, jeq, jge, jset, ret = 0x20, 0x15, 0x35, 0x45, 0x06
    code: list[tuple[str, int, str | int, str | int]] = [
        ("ld", 4, 0, 0),  # seccomp_data.arch
        ("jeq", _AUDIT_ARCH_X86_64, 1, 0),
        ("ret", _RET_KILL_PROCESS, 0, 0),
        ("ld", 0, 0, 0),  # seccomp_data.nr
        ("jge", _X32_SYSCALL_BIT, "deny", 0),
        ("jeq", _SYS["clone3"], "nosys", 0),
        ("jeq", _SYS["clone"], "clone", 0),
    ]
    code += [("jeq", _SYS[name], "deny", 0) for name in _DENIED]
    code.append(("ret", _RET_ALLOW, 0, 0))
    labels = {"deny": len(code)}
    code.append(("ret", _RET_ERRNO | _EPERM, 0, 0))
    labels["nosys"] = len(code)
    code.append(("ret", _RET_ERRNO | _ENOSYS, 0, 0))
    labels["clone"] = len(code)
    # Threads only: CLONE_THREAD set and no namespace flags. Low 32 bits of args[0] (flags).
    code += [
        ("ld", 16, 0, 0),
        ("jset", _CLONE_THREAD, 0, "deny_thread"),
        ("jset", _CLONE_NAMESPACES, "deny_thread", 0),
        ("ret", _RET_ALLOW, 0, 0),
    ]
    labels["deny_thread"] = len(code)
    code.append(("ret", _RET_ERRNO | _EPERM, 0, 0))
    ops = {"ld": ld, "jeq": jeq, "jge": jge, "jset": jset, "ret": ret}
    out = bytearray()
    for index, (op, k, jt, jf) in enumerate(code):

        def target(t: str | int, at: int = index) -> int:
            offset = t if isinstance(t, int) else labels[t] - at - 1
            if not 0 <= offset <= 255:
                raise ContainmentError("seccomp_assembly_failed")
            return offset

        out += struct.pack("<HBBI", ops[op], target(jt), target(jf), k)
    return bytes(out)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _apply_seccomp() -> None:
    program = _seccomp_program()
    buffer = ctypes.create_string_buffer(program, len(program))
    fprog = _SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    _check(
        _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0),
        "seccomp_failed",
    )


def _verify(probes: list[str]) -> None:
    if _libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise ContainmentError("no_new_privs_not_set")
    if _libc.prctl(_PR_GET_SECCOMP, 0, 0, 0, 0) != _SECCOMP_MODE_FILTER:
        raise ContainmentError("seccomp_not_active")
    for path in probes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except PermissionError:
            continue
        except FileNotFoundError:
            continue
        os.close(fd)
        raise ContainmentError("filesystem_not_restricted")
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pass
    else:
        probe.close()
        raise ContainmentError("network_not_restricted")


def contain(config: dict[str, Any]) -> int:
    """Apply and verify every restriction; return the Landlock ABI in force."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ContainmentError("unverified_platform")
    keep = {0, 1, 2, config["request_fd"], config["response_fd"]}
    for fd in range(3, max(keep) + 1):
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass
    os.closerange(max(keep) + 1, 65536)
    _set_limits(config["limits"])
    if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise ContainmentError("no_new_privs_failed")
    abi = _apply_landlock(config["read_paths"], [config["scratch"]])
    _apply_seccomp()
    _verify(config["probe_paths"])
    return abi


class _Channel:
    def __init__(self, request_fd: int, response_fd: int, max_bytes: int) -> None:
        self._out = request_fd
        self._in = response_fd
        self._max = max_bytes
        self._buffer = b""

    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        view = memoryview(data)
        while view:
            view = view[os.write(self._out, view) :]

    def receive(self) -> dict[str, Any]:
        while b"\n" not in self._buffer:
            if len(self._buffer) > self._max:
                raise ConnectionError("broker_message_too_large")
            chunk = os.read(self._in, 65536)
            if not chunk:
                raise ConnectionError("broker_closed")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ConnectionError("broker_malformed")
        return message


class Tools:
    """Candidate-side client for the host tool broker. Enforcement happens in the host."""

    def __init__(self, channel: _Channel, versions: dict[str, str]) -> None:
        self._channel = channel
        self._next = 0
        self.versions = dict(versions)

    def call(
        self, tool: str, args: dict[str, Any] | None = None, *, version: str | None = None
    ) -> dict[str, Any]:
        self._next += 1
        self._channel.send(
            {
                "type": "tool",
                "id": self._next,
                "tool": tool,
                "version": version if version is not None else self.versions.get(tool, ""),
                "args": {} if args is None else args,
            }
        )
        return self._channel.receive()


def main(argv: list[str]) -> int:
    config = json.loads(argv[1])
    channel = _Channel(config["request_fd"], config["response_fd"], config["max_message_bytes"])
    try:
        abi = contain(config)
    except ContainmentError as error:
        channel.send({"type": "containment_failed", "reason": str(error)})
        return CONTAINMENT_EXIT
    channel.send({"type": "ready", "protocol": config["protocol"], "landlock_abi": abi})
    with open(config["candidate"], encoding="utf-8") as handle:
        source = handle.read()
    with open(config["payload"], encoding="utf-8") as handle:
        payload = json.load(handle)
    namespace: dict[str, Any] = {"__name__": "candidate", "__builtins__": __builtins__}
    try:
        exec(compile(source, "candidate.py", "exec"), namespace)
        run = namespace.get("run")
        if not callable(run):
            raise TypeError("candidate defines no run(tools, payload)")
        value = run(Tools(channel, config["tools"]), payload)
    except BaseException as error:  # the candidate is untrusted; report only the type name
        name = type(error).__name__
        sys.stdout.flush()
        channel.send({"type": "error", "error": name[:64]})
        return 1
    sys.stdout.flush()
    try:
        channel.send({"type": "result", "value": value})
    except (TypeError, ValueError, RecursionError):
        channel.send({"type": "error", "error": "unserializable_result"})
        return 1
    return 0


if __name__ == "__main__":
    code = main(sys.argv)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)
