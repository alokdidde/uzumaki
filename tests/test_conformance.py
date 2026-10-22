"""Conformance check (``uzumaki.conformance``) on the fixture arm and on broken robots."""

from typing import Any

import pytest

from uzumaki import conformance
from uzumaki.embodiments import Embodiment, FixtureAdapter
from uzumaki.robots import Arm, Camera, Gripper, Legs, Quadruped, SingleArm
from uzumaki.tasks import PICK_PLACE


def statuses(results: list[conformance.Check]) -> dict[str, str]:
    return {r.name: r.status for r in results}


def test_fixture_arm_is_conformant(capsys: pytest.CaptureFixture[str]) -> None:
    assert conformance.main(["--embodiment", "fixture-arm"]) == 0
    out = capsys.readouterr().out
    assert "fixture-arm on pick-place: conformant" in out
    assert "PASS  deterministic_reset" in out and "SKIP  model_names" in out


class NamedAdapter(FixtureAdapter):
    def model_names(self) -> Any:
        return frozenset({"shoulder"}), frozenset()


def test_misnamed_joints_and_missing_capabilities_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import uzumaki.embodiments as module

    monkeypatch.setattr(module, "NamedAdapter", NamedAdapter, raising=False)
    wrong = Embodiment(
        "wrong",
        SingleArm(arm=Arm(("shoulder", "elbow")), gripper=Gripper(("gripper",), site="tip")),
        "fixture",
        "",
        ("pick-place",),
        "uzumaki.embodiments:NamedAdapter",
    )
    found = statuses(conformance.check(wrong, PICK_PLACE))
    assert found["model_names"] == "fail" and "deterministic_reset" not in found
    dog = Embodiment(
        "dog",
        Quadruped(legs=Legs(tuple((f"h{i}",) for i in range(4))), camera=Camera("eye")),
        "fixture",
        "",
        ("pick-place",),
        "uzumaki.embodiments:FixtureAdapter",
    )
    found = statuses(conformance.check(dog, PICK_PLACE))
    assert found == {"body": "pass", "model_names": "skip", "task_binding": "fail"}


def test_unknown_names_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert conformance.main(["--embodiment", "r2d2"]) == 2
    assert "known" in capsys.readouterr().err
