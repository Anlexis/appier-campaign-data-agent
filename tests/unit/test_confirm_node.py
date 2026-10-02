# CMN-C2-289 - Unit tests: ConfirmNode (inner Step 5)
#
# Convention: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "1001",
        "record_ref": "appier://campaigns/1001",
        "campaign_name": "Campaign 1001",
        "intent": "lookup_campaign",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved campaign settings" in result["confirmation"]
        assert "Campaign 1001" in result["confirmation"]
        assert "ref=appier://campaigns/1001" in result["confirmation"]
        assert "id=1001" in result["confirmation"]
        assert result["result"]["record_id"] == "1001"
        assert result["result"]["record_ref"] == "appier://campaigns/1001"

    def test_update_verb(self):
        result = self.node(_state(intent="update_campaign"))
        assert "Updated campaign settings" in result["confirmation"]

    def test_set_status_verb(self):
        result = self.node(_state(intent="set_status"))
        assert "Changed campaign status" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed campaign" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=1001" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_name_missing(self):
        result = self.node(_state(campaign_name=""))
        assert "'1001'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    # -- request-for-approval mode (pre-action approval gate) -----------------

    def test_approval_request_for_update(self):
        """Approval-required mode: renders the pending action + how to approve;
        record evidence is NOT required (no write happened)."""
        state = _state(
            intent="update_campaign",
            record_id="",
            record_ref="",
            campaign_name="Summer",
            approval_required=True,
            pending_action=to_json(
                {"intent": "update_campaign", "campaign_id": "1001", "fields": ["daily_budget", "schedule"]}
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        confirmation = result["confirmation"]
        assert "Approval required" in confirmation
        assert "no change has been made" in confirmation
        assert "update campaign settings" in confirmation
        assert "id=1001" in confirmation
        assert "fields: daily_budget, schedule" in confirmation
        assert "approve: yes" in confirmation
        # Completed-action wording must NOT appear.
        assert "Updated campaign settings" not in confirmation
        assert result["result"]["approval_required"] is True
        assert result["result"]["pending_fields"] == ["daily_budget", "schedule"]
        assert result["result"]["campaign_id"] == "1001"

    def test_approval_request_for_set_status(self):
        state = _state(
            intent="set_status",
            record_id="",
            record_ref="",
            campaign_name="",
            approval_required=True,
            pending_action=to_json({"intent": "set_status", "campaign_id": "1001", "target_status": "paused"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        confirmation = result["confirmation"]
        assert "Approval required" in confirmation
        assert "change campaign status" in confirmation
        assert "target status: paused" in confirmation
        assert "Changed campaign status" not in confirmation
        assert result["result"]["target_status"] == "paused"

    def test_approval_request_s4_signal(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(
            _state(
                intent="set_status",
                record_id="",
                record_ref="",
                approval_required=True,
                pending_action=to_json({"intent": "set_status", "campaign_id": "1001", "target_status": "paused"}),
            )
        )
        payloads = {args[0]: args[1] for args in events}
        assert payloads["confirm_complete"] == {
            "intent": "set_status",
            "has_record_ref": False,
            "approval_required": True,
        }

    def test_audit_emits_reference_presence_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        assert payloads["confirm_complete"] == {
            "intent": "lookup_campaign",
            "has_record_ref": True,
            "approval_required": False,
        }
