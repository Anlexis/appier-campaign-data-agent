# CMN-C2-289 - Unit tests: ValidateInputNode (inner Step 1, flag-and-redact)
#
# Convention: nodes are invoked via node(state) - through BaseNode.__call__
# (trust gate -> input gate -> execute -> output gate) - never bare
# node.execute(state), except where a test's whole point is to prove this
# template refuses something without a framework gate in front. This inner domain
# node declares ANONYMOUS, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.
#
# Input layering exercised here:
#   * the FRAMEWORK input mask in __call__ rewrites emails (any '@') in
#     validated_input to "[MASKED]" BEFORE execute() sees the text - the
#     intentional-PII test asserts that [MASKED] path;
#   * the NODE's own deterministic scan handles token-shaped strings the
#     framework mask does not cover (secret_* / sk-* / eyJ*) - flag + [REDACTED].

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "validated_input": "Look up the campaign settings for campaign id 1001.",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "validate-input-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        result = self.node(_state(validated_input="Show the current settings for the flagged campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "Show the current settings for the flagged campaign"
        assert from_json(result["redaction_flags"], None) == []

    def test_success_serialized_json_input(self):
        payload = json.dumps({"text": "summarize the campaign budget on file", "campaign_hint": "1001"})
        result = self.node(_state(validated_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "summarize the campaign budget on file"
        assert result["campaign_hint"] == "1001"

    def test_empty_input_errors(self):
        result = self.node(_state(validated_input="  "))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_short_input_errors(self):
        result = self.node(_state(validated_input="ab"))
        assert result["status"] == AgentStatus.ERROR.value

    def test_framework_s2_masks_email_before_execute(self):
        """Intentional-PII path: the framework input mask in __call__ rewrites the
        email to [MASKED] before execute() runs, so no raw address survives."""
        result = self.node(_state(validated_input="send the report for campaign id 1001 to ads.lead@example.com"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "ads.lead@example.com" not in result["validated_input"]
        assert "[MASKED]" in result["validated_input"]

    def test_node_s2_redacts_token_shaped_string(self):
        """The node's own deterministic scan covers token shapes the framework
        PII mask does not (secret_*): flagged + [REDACTED] before logging."""
        text = "integration key secret_abcdef123456 for campaign id 1001"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "secret_abcdef123456" not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    # -- pre-action approval gate: explicit approval extraction ---------------

    def test_no_approval_signal_defaults_false(self):
        result = self.node(_state(validated_input="pause the campaign with campaign id 1001"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["approval_granted"] is False

    def test_approve_yes_line_grants_approval(self):
        text = "pause the campaign with campaign id 1001\napprove: yes"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["approval_granted"] is True

    def test_approved_token_grants_approval(self):
        text = "pause the campaign with campaign id 1001 [approved]"
        result = self.node(_state(validated_input=text))
        assert result["approval_granted"] is True

    def test_inline_approve_word_is_not_approval(self):
        """Only the exact phrase forms count - a mention of approval inside a
        sentence never grants the write."""
        text = "please approve: yes or no whether campaign id 1001 should pause"
        result = self.node(_state(validated_input=text))
        assert result["approval_granted"] is False

    def test_envelope_approved_true_grants_approval(self):
        payload = json.dumps(
            {"text": "pause the campaign with campaign id 1001", "campaign_hint": "1001", "approved": True}
        )
        result = self.node(_state(validated_input=payload))
        assert result["approval_granted"] is True

    def test_envelope_approved_must_be_exact_boolean(self):
        payload = json.dumps(
            {"text": "pause the campaign with campaign id 1001", "campaign_hint": "1001", "approved": "yes"}
        )
        result = self.node(_state(validated_input=payload))
        assert result["approval_granted"] is False

    def test_audit_emits_scan_outcome_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.validate_input_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(validated_input="summarize the budget for campaign id 1001"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - flags only, never the text.
        assert payloads["validate_input_complete"]["redaction_flags"] == []
        assert payloads["validate_input_complete"]["approval_granted"] is False
        assert "text" not in payloads["validate_input_complete"]
