"""Conformance check for an embodiment and a task pack: is this robot supported?

    python -m uzumaki.conformance --embodiment so101 [--task pick-place] [--case ID]

Checks, in order, each reported as PASS or FAIL with a reason:

1. ``body``: the robot description compiles to a valid body model.
2. ``model_names``: every joint and site it names exists in the robot's model file (skipped for
   bodies without a model file).
3. ``task_binding``: the body has an effector with every capability the task pack requires, and
   each of the pack's tools maps to a capability of the body.
4. ``skill_catalog``: the pack's skills load against the pack's tool catalog, so no skill can
   call a tool this robot lacks.
5. ``stop_preempts``: ``stop`` claims every part and preempts a call holding the effector.
6. ``deterministic_reset``: one case run twice from fresh environments gives identical evidence.
7. ``replay``: recorded-decision replay of that evidence reproduces it exactly.
8. ``evaluator_outcome``: the run's outcome is the independent evaluator's verdict.

Exit status 0 only if nothing failed. Passing means the harness can drive this body on this task;
it says nothing about how well the robot performs.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uzumaki.body import BodyModel, PartClaims
from uzumaki.embodiments import REPO, Embodiment, embodiment
from uzumaki.registry import RegistrySnapshot, load_skill_set, seal_skill
from uzumaki.tasks import TaskBinding, TaskPack, task_pack


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "pass", "fail" or "skip"
    detail: str


def load_pack_skills(pack: TaskPack) -> RegistrySnapshot:
    """The pack's skill set, sealed and loaded against the pack's tool catalog."""
    if pack.skill_set is None:
        raise ValueError(f"task pack {pack.name!r} has no skill set")
    document = json.loads((REPO / pack.skill_set).read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    with tempfile.TemporaryDirectory() as directory:
        sealed = Path(directory) / "skills.json"
        sealed.write_text(json.dumps(document), encoding="utf-8")
        return load_skill_set(sealed, tools=pack.tool_catalog).snapshot()


def check(target: Embodiment, pack: TaskPack, case_id: str | None = None) -> list[Check]:
    from uzumaki.baseline import replay_case, run_case

    results: list[Check] = []

    def record(name: str, step: Callable[[], str]) -> bool:
        try:
            detail = step()
        except Exception as error:  # noqa: BLE001 - every failure is reported, none hidden
            results.append(Check(name, "fail", f"{type(error).__name__}: {error}"))
            return False
        status = "skip" if detail.startswith("skipped") else "pass"
        results.append(Check(name, status, detail))
        return True

    state: dict[str, Any] = {}

    def body() -> str:
        state["body"] = target.robot.body()
        model: BodyModel = state["body"]
        return f"{model.configuration or 'custom'}: " + "; ".join(
            line.strip() for line in model.summary().splitlines()[1:]
        )

    if not record("body", body):
        return results
    model: BodyModel = state["body"]
    state["adapter"] = None

    def names() -> str:
        state["adapter"] = target.adapter()
        found = state["adapter"].model_names()
        if found is None:
            return "skipped: no model file"
        missing = model.check_model_names(*found)
        if missing:
            raise ValueError(f"not in {model.model_file}: {', '.join(missing)}")
        return f"{len(model.joint_names)} joints and the sites found in {model.model_file}"

    adapter_ok = record("model_names", names)

    def binding() -> str:
        state["binding"] = pack.bind(model)
        bound: TaskBinding = state["binding"]
        claims = ", ".join(f"{t}: {'+'.join(sorted(p)) or '-'}" for t, p in bound.claims.items())
        if bound.roles:
            roles = ", ".join(f"{role} {effector}" for role, effector in bound.roles.items())
            return f"roles {roles}; claims {claims}"
        return f"effector {bound.effector}; claims {claims}"

    if not record("task_binding", binding):
        return results

    def catalog() -> str:
        state["snapshot"] = load_pack_skills(pack)
        snapshot: RegistrySnapshot = state["snapshot"]
        tools = ", ".join(sorted(pack.tool_catalog))
        return f"{len(snapshot.keys())} skills load against the tools {tools}"

    record("skill_catalog", catalog)

    def stop() -> str:
        bound: TaskBinding = state["binding"]
        actuating = max(bound.claims.values(), key=len)
        claims = PartClaims(model)
        claims.acquire("call-1", actuating)
        preempted = claims.acquire("stop", model.claims("stop"), preempt=True)
        if preempted != ["call-1"] or set(claims.held) != {p.name for p in model.parts}:
            raise AssertionError("stop did not take every part")
        return "stop takes every part and preempts the running call"

    record("stop_preempts", stop)
    if not adapter_ok or not all(r.status != "fail" for r in results):
        return results
    adapter = state["adapter"]
    cases = adapter.cases(pack.name)
    chosen = next((c for c in cases if c.case_id == case_id), None) if case_id else cases[0]
    if chosen is None:
        results.append(Check("deterministic_reset", "fail", f"unknown case {case_id}"))
        return results
    snapshot = state["snapshot"]
    environment = adapter.environment(pack.name)

    def reset() -> str:
        first = run_case(chosen, snapshot, environment, protocol=pack.protocol)
        second = run_case(chosen, snapshot, environment, protocol=pack.protocol)
        a, b = (json.dumps(r.bundle, sort_keys=True) for r in (first, second))
        if a != b:
            raise AssertionError("two fresh runs of the same case produced different evidence")
        state["run"] = first
        return f"case {chosen.case_id} twice: identical evidence"

    if not record("deterministic_reset", reset):
        return results
    run = state["run"]

    def replayed() -> str:
        report = replay_case(run.bundle, snapshot, protocol=pack.protocol)
        if not report.matches:
            raise AssertionError(f"replay differs: {', '.join(report.differences)}")
        return "recorded-decision replay matches"

    record("replay", replayed)

    def outcome() -> str:
        events = [e for e in run.bundle["events"] if e["kind"] == "outcome"]
        bundle_outcome = events[-1]["data"]["primary"] if events else None
        verdict = (run.verdict or {}).get("primary")
        if bundle_outcome != verdict:
            raise AssertionError(f"outcome {bundle_outcome} but evaluator said {verdict}")
        return f"outcome {bundle_outcome}, from the evaluator"

    record("evaluator_outcome", outcome)
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m uzumaki.conformance", description=__doc__)
    parser.add_argument("--embodiment", required=True)
    parser.add_argument("--task", default="pick-place")
    parser.add_argument("--case", help="case to run (default: the first of the set)")
    options = parser.parse_args(argv)
    try:
        target, pack = embodiment(options.embodiment), task_pack(options.task)
    except KeyError as error:
        print(error.args[0], file=sys.stderr)
        return 2
    results = check(target, pack, options.case)
    for result in results:
        print(f"{result.status.upper():4}  {result.name:20} {result.detail}")
    failed = [r for r in results if r.status == "fail"]
    print(f"{options.embodiment} on {options.task}: " + ("FAILED" if failed else "conformant"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
