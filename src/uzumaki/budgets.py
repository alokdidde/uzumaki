"""Run budgets and a usage ledger in which unknown usage stays unknown.

Every budget is an explicit integer bound. Wall time, model calls, execution steps (tool calls),
retries, attempts and output bytes are always enforced. Tokens and cost are enforced only from
usage the provider actually reports; missing usage is never counted as zero. When a token or cost
limit is set, ``unknown_usage`` chooses what a response without that usage does:

- ``fail_closed`` (default): the run stops as ``budget_exhausted`` because staying within the
  limit can no longer be shown.
- ``record_unknown``: the run continues; the ledger counts the call as unknown and reports no
  total for that dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

UNKNOWN_USAGE_POLICIES = ("fail_closed", "record_unknown")
_OPTIONAL = frozenset(
    {"attempts", "model_call_timeout_ms", "tool_call_timeout_ms", "tokens", "cost_micro_usd"}
)
_MAY_BE_ZERO = frozenset({"model_calls", "steps", "retries"})


class BudgetError(ValueError):
    """A budget or a reported usage value is invalid."""


@dataclass(frozen=True)
class Budgets:
    """Per-run bounds. Model calls include retries; steps count tool calls."""

    wall_ms: int
    model_calls: int
    steps: int
    retries: int
    max_output_bytes: int
    max_total_output_bytes: int
    attempts: int | None = None
    model_call_timeout_ms: int | None = None
    tool_call_timeout_ms: int | None = None
    tokens: int | None = None
    cost_micro_usd: int | None = None
    unknown_usage: str = "fail_closed"

    def __post_init__(self) -> None:
        for f in fields(self):
            if f.name == "unknown_usage":
                continue
            value = getattr(self, f.name)
            if value is None and f.name in _OPTIONAL:
                continue
            if type(value) is not int:
                raise BudgetError(f"budget {f.name} must be an integer")
            if value < 0 or (value == 0 and f.name not in _MAY_BE_ZERO):
                raise BudgetError(f"budget {f.name} is out of range")
        if self.unknown_usage not in UNKNOWN_USAGE_POLICIES:
            raise BudgetError("unknown_usage policy is not supported")


@dataclass(frozen=True)
class Usage:
    """Usage reported for one model call. ``None`` means the provider did not report it."""

    tokens: int | None
    cost_micro_usd: int | None


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise BudgetError("reported usage is malformed")
    return value


def parse_usage(output: dict[str, Any]) -> Usage:
    """Read ``output["usage"]`` (``input_tokens``, ``output_tokens``, ``cost_micro_usd``).

    Tokens are known only when both input and output tokens are reported. Malformed values raise
    ``BudgetError``; absent values are unknown.
    """
    usage = output.get("usage")
    if usage is None:
        return Usage(None, None)
    if not isinstance(usage, dict):
        raise BudgetError("reported usage is malformed")
    parts = [usage.get("input_tokens"), usage.get("output_tokens")]
    known = [_count(p) for p in parts if p is not None]
    tokens = sum(known) if len(known) == 2 else None
    cost = usage.get("cost_micro_usd")
    return Usage(tokens, None if cost is None else _count(cost))


class Ledger:
    """Consumption against ``Budgets``. Each check returns an exhaustion reason or ``None``."""

    def __init__(self, budgets: Budgets) -> None:
        self.budgets = budgets
        self.model_calls = 0
        self.steps = 0
        self.retries = 0
        self.attempts = 0
        self.output_bytes = 0
        self.tokens_known = 0
        self.tokens_unknown_calls = 0
        self.cost_known = 0
        self.cost_unknown_calls = 0

    def before_model_call(self) -> str | None:
        b = self.budgets
        if self.model_calls >= b.model_calls:
            return "model_calls"
        if b.tokens is not None and self.tokens_known >= b.tokens:
            return "tokens"
        if b.cost_micro_usd is not None and self.cost_known >= b.cost_micro_usd:
            return "cost"
        return None

    def before_step(self) -> str | None:
        return "steps" if self.steps >= self.budgets.steps else None

    def before_retry(self) -> str | None:
        return "retries" if self.retries >= self.budgets.retries else None

    def before_attempt(self) -> str | None:
        limit = self.budgets.attempts
        return "attempts" if limit is not None and self.attempts >= limit else None

    def add_usage(self, usage: Usage) -> str | None:
        b = self.budgets
        reason = None
        if usage.tokens is None:
            self.tokens_unknown_calls += 1
            if b.tokens is not None and b.unknown_usage == "fail_closed":
                reason = "tokens_unknown"
        else:
            self.tokens_known += usage.tokens
            if b.tokens is not None and self.tokens_known > b.tokens:
                reason = "tokens"
        if usage.cost_micro_usd is None:
            self.cost_unknown_calls += 1
            if b.cost_micro_usd is not None and b.unknown_usage == "fail_closed":
                reason = reason or "cost_unknown"
        else:
            self.cost_known += usage.cost_micro_usd
            if b.cost_micro_usd is not None and self.cost_known > b.cost_micro_usd:
                reason = reason or "cost"
        return reason

    def add_output(self, size: int) -> str | None:
        self.output_bytes += size
        if self.output_bytes > self.budgets.max_total_output_bytes:
            return "total_output_bytes"
        return None

    def snapshot(self) -> dict[str, Any]:
        b = self.budgets
        return {
            "model_calls": {"used": self.model_calls, "limit": b.model_calls},
            "steps": {"used": self.steps, "limit": b.steps},
            "retries": {"used": self.retries, "limit": b.retries},
            "attempts": {"used": self.attempts, "limit": b.attempts},
            "output_bytes": {"used": self.output_bytes, "limit": b.max_total_output_bytes},
            "tokens": {
                "known": self.tokens_known,
                "unknown_calls": self.tokens_unknown_calls,
                "total": self.tokens_known if self.tokens_unknown_calls == 0 else None,
                "limit": b.tokens,
            },
            "cost_micro_usd": {
                "known": self.cost_known,
                "unknown_calls": self.cost_unknown_calls,
                "total": self.cost_known if self.cost_unknown_calls == 0 else None,
                "limit": b.cost_micro_usd,
            },
            "unknown_usage": b.unknown_usage,
        }
