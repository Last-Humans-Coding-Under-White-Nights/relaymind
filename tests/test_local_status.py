"""The class of the local model (#126; RM26, spec 0.31 sections 7.4 and 8)."""

import pytest

from gateway_helpers import build_gateway
from relaymind.orchestrator.local_status import ABSENT_REASON, LocalModelClass, LocalStatus
from relaymind.orchestrator.route_decision import ExecutionMode

UNAVAILABLE = LocalStatus(LocalModelClass.UNAVAILABLE, "no answer within 30 s")
UNSUITABLE = LocalStatus(LocalModelClass.UNSUITABLE, "the routing warm-up took 5200 ms")


@pytest.mark.parametrize(
    "status, forbidden",
    [
        (LocalStatus.ready(), set()),
        (UNSUITABLE, {ExecutionMode.PARALLEL_DEMO}),
        (UNAVAILABLE, {ExecutionMode.LOCAL_ONLY, ExecutionMode.PARALLEL_DEMO}),
        (LocalStatus.absent(), {ExecutionMode.LOCAL_ONLY, ExecutionMode.PARALLEL_DEMO}),
    ],
)
def test_each_class_forbids_the_modes_section_7_4_names(status, forbidden):
    assert {mode for mode in ExecutionMode if status.forbids(mode)} == forbidden
    for mode in ExecutionMode:
        refusal = status.refusal(mode)
        assert (refusal is not None) == (mode in forbidden)
        if refusal is not None:
            assert mode.value in refusal and status.status.value in refusal


def test_only_a_ready_model_is_usable():
    assert LocalStatus.ready().usable
    assert not any(s.usable for s in (UNAVAILABLE, UNSUITABLE, LocalStatus.absent()))


@pytest.mark.parametrize("reason", [None, "", "  ", 5])
def test_a_class_other_than_ready_needs_a_cause(reason):
    with pytest.raises(ValueError):
        LocalStatus(LocalModelClass.UNAVAILABLE, reason)


def test_an_unknown_class_is_refused():
    with pytest.raises(ValueError):
        LocalStatus("slow", "x")


def test_the_run_accepted_fields():
    """Section 8: local_model is null only when absent."""
    assert LocalStatus.absent().to_json_object(None) == {
        "local_model": None,
        "local_model_status": "absent",
        "local_model_reason": ABSENT_REASON,
    }
    assert UNSUITABLE.to_json_object("m")["local_model"] == "m"


def test_the_launcher_line_names_the_class_and_the_cause():
    assert UNAVAILABLE.line() == (
        "Local model: unavailable (no answer within 30 s); auto runs as managed external execution."
    )


def test_the_orchestrator_refuses_a_class_that_contradicts_its_model(tmp_path):
    """RM26: absent exactly when no local model is configured."""
    configured = build_gateway(tmp_path / "configured", [])
    absent = build_gateway(tmp_path / "absent", [], local_status=LocalStatus.absent())
    try:
        with pytest.raises(ValueError, match="cannot be absent"):
            configured.orchestrator.set_local_status(LocalStatus.absent())
        with pytest.raises(ValueError, match="no local model is configured"):
            absent.orchestrator.set_local_status(LocalStatus.ready())
    finally:
        configured.store.close()
        absent.store.close()
