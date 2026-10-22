"""The body model (``uzumaki.body``): validation, capabilities and part claims."""

import pytest

from uzumaki.body import (
    BodyError,
    BodyModel,
    Capability,
    ClaimConflict,
    Effector,
    Part,
    PartClaims,
    Sensor,
)


def dog() -> BodyModel:
    legs = tuple(Part(f"leg_{i}", (f"hip_{i}", f"knee_{i}"), "torso", "leg") for i in range(4))
    return BodyModel(
        "dog",
        parts=(Part("torso"), *legs, Part("neck", ("neck_pitch", "neck_yaw"), "torso", "neck")),
        effectors=(Effector("jaw", "neck", "jaw", ("jaw",), "jaw_site"),),
        sensors=(Sensor("eye", "neck"),),
        capabilities=(
            Capability("locomote", tuple(p.name for p in legs)),
            Capability("reach", ("neck",), effector="jaw"),
            Capability("grasp", ("neck",), effector="jaw"),
            Capability("see", (), sensor="eye"),
        ),
    )


def test_capabilities_answer_what_the_body_can_do() -> None:
    body = dog()
    assert body.effectors_with({"reach", "grasp"}) == ("jaw",)
    assert body.provides("locomote") and body.provides("see", "eye")
    assert not body.provides("reach", "eye")
    assert body.claims("reach", "jaw") == {"neck"}
    assert body.claims("stop") == {"torso", "neck", *(f"leg_{i}" for i in range(4))}
    assert body.check_model_names({"jaw", "neck_pitch"}, {"jaw_site"})[0] == "joint hip_0"
    assert len(body.sha256) == 64 and body.document()["schema"] == "uzumaki.body/1.0"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (dict(parts=(Part("a", ("j",)), Part("a"))), "unique"),
        (dict(parts=(Part("a", ("j",), "b"), Part("b", ("k",), "a"))), "cycle"),
        (dict(parts=(Part("a", ("j",)), Part("b", ("j",)))), "joint 'j'"),
        (dict(parts=(Part("a", ("j",), "missing"),)), "unknown part"),
        (dict(parts=(Part("Arm", ("j",)),)), "must match"),
        (dict(effectors=(Effector("g", "nowhere"),)), "unknown part"),
        (dict(effectors=(Effector("g", "a", "claw"),)), "unknown kind"),
        (dict(capabilities=(Capability("reach", ("a",)),)), "needs an effector"),
        (dict(capabilities=(Capability("locomote", ("b",)),)), "unknown"),
        (dict(capabilities=(Capability("stop", ("a",)),)), "implicit"),
        (dict(effectors=(Effector("a", "a"),)), "distinct names"),
    ],
)
def test_inconsistent_bodies_are_refused(change: dict, message: str) -> None:
    fields = dict(name="robot", parts=(Part("a", ("j",)),))
    fields.update(change)
    with pytest.raises(BodyError, match=message):
        BodyModel(**fields)


def test_claims_admit_only_disjoint_calls_and_stop_preempts() -> None:
    body = dog()
    claims = PartClaims(body)
    claims.acquire("walk", body.claims("locomote"))
    claims.acquire("grab", body.claims("grasp", "jaw"))  # legs and neck do not overlap
    with pytest.raises(ClaimConflict) as conflict:
        claims.acquire("nod", {"neck"})
    assert conflict.value.holders == {"neck": "grab"}
    assert claims.acquire("stop", body.claims("stop"), preempt=True) == ["grab", "walk"]
    assert set(claims.held.values()) == {"stop"}
    claims.release("stop")
    assert claims.held == {}
    with pytest.raises(BodyError):
        claims.acquire("x", {"tail"})
