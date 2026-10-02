# CMN-C2-289 - Unit tests: ClassifyIntentNode (inner Step 2)
#
# Intents: lookup_campaign / update_campaign / set_status (deterministic
# keyword heuristic, v1 - no LLM; unknown falls back to the read-only lookup).
#
# Convention: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_campaign(self):
        result = self.node(_state("Look up the campaign settings for campaign id 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_campaign"

    def test_keyword_update_campaign(self):
        result = self.node(_state("Update the daily budget for campaign id 1001"))
        assert result["intent"] == "update_campaign"

    def test_keyword_set_status_pause(self):
        result = self.node(_state("Pause campaign id 1001 immediately"))
        assert result["intent"] == "set_status"

    def test_keyword_set_status_resume(self):
        result = self.node(_state("Resume campaign id 1001"))
        assert result["intent"] == "set_status"

    def test_status_keyword_wins_over_update(self):
        # Priority order is status-first: "pause ... then change ..." style
        # requests classify as the status write, never the generic update.
        result = self.node(_state("Pause campaign id 1001 and change the note later"))
        assert result["intent"] == "set_status"

    def test_write_keyword_wins_over_lookup(self):
        # Writes-first: an "update ... then show it" style request classifies
        # as the write, never the read.
        result = self.node(_state("Update the budget for campaign id 1001 and show the settings"))
        assert result["intent"] == "update_campaign"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_campaign"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_campaign" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the campaign settings for campaign id 1001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_campaign"
        assert payloads["classify_intent_complete"]["defaulted"] is False
