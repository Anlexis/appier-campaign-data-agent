# CMN-C2-289 - Unit tests: InferAppierFieldsNode (inner Step 3)
#
# Convention: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework input mask rewrites Title-Case
# bigrams in validated_input (even across newlines), so quoted display names
# are single-word and "Key: value" setting keys/values stay single-word /
# lower-case.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_appier_fields_node import InferAppierFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_appier_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_campaign", campaign_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "campaign_hint": campaign_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferAppierFieldsNode:
    def setup_method(self):
        self.node = InferAppierFieldsNode()

    def test_lookup_extracts_id_from_text(self):
        result = self.node(
            _state("Look up the campaign settings for campaign id 1001 and summarize the current budget and schedule.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == "1001"
        # appier_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["appier_payload"], str)
        assert from_json(result["appier_payload"], {}) == {"campaign_id": "1001"}

    def test_id_shaped_hint_used_when_text_has_no_id(self):
        result = self.node(_state("Show the current settings summary", campaign_hint="C-42"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == "C-42"
        assert from_json(result["appier_payload"], {}) == {"campaign_id": "C-42"}

    def test_update_builds_campaign_payload(self):
        # Keys/values stay single-word or lower-case: the framework name
        # mask rewrites Title-Case word pairs even ACROSS newlines
        # ("Frequency Cap" would be masked before execute() sees the text).
        text = (
            'Update campaign id 1001 named "Summer"\n'
            "budget to 3,000\n"
            "from 2026-08-01 to 2026-08-31\n"
            "Objective: awareness"
        )
        result = self.node(_state(text, intent="update_campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == "1001"
        assert result["campaign_name"] == "Summer"
        payload = from_json(result["appier_payload"], {})
        assert payload["campaign_id"] == "1001"
        campaign = payload["campaign"]
        assert campaign["name"] == "Summer"
        assert campaign["daily_budget"] == "3000"
        assert campaign["schedule"] == {"start_date": "2026-08-01", "end_date": "2026-08-31"}
        assert {"name": "Objective", "value": "awareness"} in campaign["settings"]

    def test_approval_line_is_not_a_campaign_setting(self):
        """The approval-gate control line (`approve: yes`) is a workflow signal
        consumed by ValidateInputNode - never written to Appier as a setting."""
        text = "Update campaign id 1001\n" "budget to 3,000\n" "Objective: awareness\n" "approve: yes"
        result = self.node(_state(text, intent="update_campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        campaign = from_json(result["appier_payload"], {})["campaign"]
        setting_names = [s["name"].lower() for s in campaign.get("settings", [])]
        assert "approve" not in setting_names
        assert {"name": "Objective", "value": "awareness"} in campaign["settings"]

    def test_set_status_pause_payload(self):
        result = self.node(_state("Pause campaign id 1001", intent="set_status"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["appier_payload"], {}) == {"campaign_id": "1001", "status": "paused"}

    def test_set_status_resume_wins_over_pause_substring(self):
        # Activation words are checked FIRST so "unpause" is not swallowed by
        # the "pause" substring.
        result = self.node(_state("Unpause campaign id 1001", intent="set_status"))
        assert from_json(result["appier_payload"], {}) == {"campaign_id": "1001", "status": "active"}

    def test_unresolved_id_left_empty_never_invented(self):
        result = self.node(_state("Show the current settings for the flagged campaign"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["campaign_id"] == ""
        assert from_json(result["appier_payload"], {}) == {"campaign_id": ""}

    def test_malformed_hint_is_refused_rather_than_silently_dropped(self):
        """A caller who supplied a target expects it to be used or refused.

        Ignoring a malformed hint and continuing would leave the request
        pointing at whatever the text happened to mention - or at nothing -
        without telling the caller their target was discarded.
        """
        result = self.node(_state("Show the current settings summary", campaign_hint="not a valid id!"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("campaign_hint" in entry for entry in result["error_log"])
        # The rejected value is named by field, never echoed.
        assert not any("not a valid id!" in entry for entry in result["error_log"])

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_field_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_appier_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the campaign settings for campaign id 1001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - signals only, never content.
        payload = payloads["infer_appier_fields_complete"]
        assert payload["intent"] == "lookup_campaign"
        assert payload["has_campaign_id"] is True
        assert payload["n_fields"] == 0


class TestCallerBlockWinsAndIsBounded:
    """The bridged caller block is authoritative, and it is still checked here.

    Every value that reaches the outbound request body is validated at this
    node whatever route it arrived by, so a direct call with a hostile block is
    refused even though the entry contract would have caught it first.
    """

    def setup_method(self):
        self.node = InferAppierFieldsNode()

    def _update_state(self, campaign, text="Update campaign id 1001 budget to 3000"):
        return _state(text, intent="update_campaign", caller_campaign=json.dumps(campaign))

    def test_caller_values_override_the_text(self):
        result = self.node.execute(self._update_state({"name": "Autumn Push", "daily_budget": 999}))
        assert result["status"] == AgentStatus.SUCCESS.value
        campaign = from_json(result["appier_payload"], {})["campaign"]
        assert campaign["name"] == "Autumn Push"
        assert campaign["daily_budget"] == "999"

    def test_text_extraction_still_applies_where_the_caller_supplied_nothing(self):
        result = self.node.execute(self._update_state({}))
        campaign = from_json(result["appier_payload"], {})["campaign"]
        assert campaign["daily_budget"] == "3000"

    @pytest.mark.parametrize(
        "campaign",
        [
            {"daily_budget": "NaN"},
            {"daily_budget": "Infinity"},
            {"daily_budget": 0},
            {"daily_budget": 1e12},
            {"daily_budget": True},
            {"name": "a@b.example"},
            {"start_date": "2026-13-45"},
            {"start_date": "2026-08-10", "end_date": "2026-08-01"},
            {"settings": [{"name": "n", "value": "v"}] * 21},
            {"settings": "not-a-list"},
            {"settings": ["not-an-object"]},
        ],
    )
    def test_a_hostile_caller_block_is_refused_here_too(self, campaign):
        result = self.node.execute(self._update_state(campaign))
        assert result["status"] == AgentStatus.ERROR.value
        assert "appier_payload" not in result

    def test_a_status_outside_the_closed_set_is_refused_on_the_status_path(self):
        state = _state(
            "Pause campaign id 1001",
            intent="set_status",
            caller_campaign=json.dumps({"status": "archived"}),
        )
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("campaign.status" in entry for entry in result["error_log"])

    def test_a_masked_text_value_is_refused_rather_than_written(self):
        """Writing the mask marker to a live campaign is worse than refusing.

        The framework's input gate rewrites a Title Case name in the request
        text before this node sees it; the refusal names the field and points
        at the channel that carries the value intact.
        """
        result = self.node.execute(
            _state(
                'Update campaign id 1001 named "[MASKED] 2026"',
                intent="update_campaign",
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("campaign name" in entry for entry in result["error_log"])
        assert any("request context" in entry for entry in result["error_log"])
