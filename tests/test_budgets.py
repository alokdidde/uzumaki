from typing import Any

import pytest

from fakes import budgets
from uzumaki.budgets import BudgetError, Ledger, Usage, parse_usage


@pytest.mark.parametrize(
    "override",
    [
        {"wall_ms": 0},
        {"wall_ms": -1},
        {"model_calls": -1},
        {"steps": 1.5},
        {"retries": True},
        {"max_output_bytes": 0},
        {"tokens": 0},
        {"cost_micro_usd": "10"},
        {"attempts": 0},
        {"unknown_usage": "assume_zero"},
    ],
)
def test_invalid_budgets_are_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(BudgetError):
        budgets(**override)


def test_zero_call_budgets_are_allowed() -> None:
    assert budgets(model_calls=0, steps=0, retries=0).model_calls == 0


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({}, Usage(None, None)),
        ({"usage": {"input_tokens": 3, "output_tokens": 4}}, Usage(7, None)),
        ({"usage": {"input_tokens": 3}}, Usage(None, None)),  # partial is not a total
        ({"usage": {"cost_micro_usd": 0}}, Usage(None, 0)),  # reported zero is known zero
    ],
)
def test_usage_is_known_only_when_reported(output: dict[str, Any], expected: Usage) -> None:
    assert parse_usage(output) == expected


@pytest.mark.parametrize(
    "usage",
    [[], {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": True, "output_tokens": 1}],
)
def test_malformed_usage_is_rejected(usage: Any) -> None:
    with pytest.raises(BudgetError):
        parse_usage({"usage": usage})


def test_unknown_usage_is_never_reported_as_zero() -> None:
    ledger = Ledger(budgets(unknown_usage="record_unknown", tokens=100))
    assert ledger.add_usage(Usage(10, 5)) is None
    assert ledger.add_usage(Usage(None, None)) is None
    snap = ledger.snapshot()
    assert snap["tokens"] == {"known": 10, "unknown_calls": 1, "total": None, "limit": 100}
    assert snap["cost_micro_usd"]["total"] is None and snap["cost_micro_usd"]["known"] == 5


def test_fail_closed_stops_on_unknown_usage_only_for_limited_dimensions() -> None:
    assert Ledger(budgets(tokens=100)).add_usage(Usage(None, 1)) == "tokens_unknown"
    assert Ledger(budgets(cost_micro_usd=100)).add_usage(Usage(1, None)) == "cost_unknown"
    assert Ledger(budgets()).add_usage(Usage(None, None)) is None  # no limit: recorded only


def test_known_usage_over_the_limit_is_exhaustion() -> None:
    ledger = Ledger(budgets(tokens=10, cost_micro_usd=100))
    assert ledger.add_usage(Usage(10, 100)) is None
    assert ledger.before_model_call() == "tokens"
    assert Ledger(budgets(tokens=10)).add_usage(Usage(11, None)) == "tokens"
