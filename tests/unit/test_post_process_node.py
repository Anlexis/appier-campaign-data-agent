# CMN-C2-289 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Convention: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); this backbone formatter declares ANONYMOUS,
# so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is the
# MODULE-LEVEL _security_gate_output() helper (fleet-green pattern - the
# framework gate methods are @final and the real SDK auto-wraps _extra_ hooks),
# so the helper is also unit-tested directly as a plain function.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "1001",
        "record_ref": "appier://campaigns/1001",
        "campaign_name": "Campaign 1001",
        "intent": "lookup_campaign",
        "confirmation": "Retrieved campaign settings 'Campaign 1001' - ref=appier://campaigns/1001 - id=1001",
        "appier_payload": to_json({"campaign_id": "1001"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "1001"
        assert out["record_ref"] == "appier://campaigns/1001"
        assert out["intent"] == "lookup_campaign"
        assert out["confirmation"].startswith("Retrieved campaign settings")
        # JSON round-trip: the appier_payload string surfaces parsed.
        assert out["appier_payload"] == {"campaign_id": "1001"}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallAppierApiNode: Appier API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Appier API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record_id/record_ref" in entry for entry in result["error_log"])

    def test_a_blocked_output_clears_every_output_bearing_field(self):
        """Blocking is containment, not a relabelled success.

        The invoke envelope falls back to `result` whenever `formatted_output`
        is empty - including on an error status - so a gate that only flipped
        the status would still ship the un-gated inner answer.
        """
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {}
        for field in (
            "result",
            "confirmation",
            "appier_payload",
            "pending_action",
            "record_id",
            "record_ref",
            "campaign_name",
        ):
            assert result[field] == "", field
        assert result["approval_required"] is False

    def test_approval_required_success_passes_without_record_evidence(self):
        """Pre-action approval gate: an explicit approval-required response has
        no record evidence BY DESIGN (the write was refused) and must pass."""
        state = _state(
            record_id="",
            record_ref="",
            intent="set_status",
            approval_required=True,
            pending_action=to_json({"intent": "set_status", "campaign_id": "1001", "target_status": "paused"}),
            confirmation="Approval required - no change has been made. ...",
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["approval_required"] is True
        assert out["pending_action"]["target_status"] == "paused"
        assert out["record_id"] == "" and out["record_ref"] == ""


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "1001", "record_ref": "appier://campaigns/1001", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "1001", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []

    def test_approval_required_not_required_to_carry_evidence(self):
        violations = _security_gate_output(
            {
                "record_id": "",
                "record_ref": "",
                "approval_required": True,
                "confirmation": "Approval required - no change has been made.",
            },
            is_success=True,
        )
        assert violations == []

    def test_approval_required_with_record_evidence_is_inconsistent(self):
        """Pending and completed are mutually exclusive - the gate blocks a
        response claiming both."""
        violations = _security_gate_output(
            {"record_id": "1001", "record_ref": "", "approval_required": True, "confirmation": "Approval required"},
            is_success=True,
        )
        assert any("mutually exclusive" in v for v in violations)

    def test_approval_required_without_confirmation_blocked(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "approval_required": True, "confirmation": ""},
            is_success=True,
        )
        assert any("request-for-approval" in v for v in violations)
