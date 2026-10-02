# CMN-C2-289 - Unit tests: CallAppierApiNode (inner Step 4, tool side-effect)
#
# Convention: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate is unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's AppierClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).
#
# Pre-action approval gate: the write intents (update_campaign / set_status)
# execute ONLY with state["approval_granted"] is True. The gate tests use a
# spy client to PROVE the client write method is never called without
# approval, and that it is called exactly once with it.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_appier_api_node import CallAppierApiNode
from src.services.appier_client import AppierApiError
from src.schemas.state import from_json, to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_appier_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "appier_payload": to_json({"campaign_id": "1001"}),
        "intent": "lookup_campaign",
        "campaign_id": "1001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-appier-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for AppierClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_campaign(self, campaign_id, api_key):
        raise AppierApiError(403, "forbidden by integration permissions")


class _FakeLiveClient:
    """Stands in for AppierClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_campaign(self, campaign_id, api_key):
        _FakeLiveClient.captured = {"campaign_id": campaign_id, "api_key": api_key}
        return {"campaign": {"id": campaign_id, "name": f"Campaign {campaign_id}"}}


class _SpyWriteClient:
    """Stands in for AppierClient: records write-method calls (approval-gate spy)."""

    update_calls: list = []
    status_calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    @classmethod
    def reset(cls):
        cls.update_calls = []
        cls.status_calls = []

    def get_campaign(self, campaign_id, api_key):
        return {"campaign": {"id": campaign_id, "name": f"Campaign {campaign_id}"}}

    def update_campaign(self, campaign_id, payload, api_key):
        _SpyWriteClient.update_calls.append(campaign_id)
        return {"campaign_id": campaign_id}

    def set_campaign_status(self, campaign_id, target_status, api_key):
        _SpyWriteClient.status_calls.append((campaign_id, target_status))
        return {"campaign_id": campaign_id, "status": target_status}


class TestCallAppierApiNode:
    def setup_method(self):
        self.node = CallAppierApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert result["record_ref"] == "appier://campaigns/1001"
        assert result["campaign_id"] == "1001"
        assert result["campaign_name"] == "Campaign 1001"

    def test_update_success_via_default_v1_stub(self):
        # Writes require the explicit approval flag (pre-action approval gate).
        state = _state(
            intent="update_campaign",
            approval_granted=True,
            appier_payload=to_json({"campaign_id": "1001", "campaign": {"daily_budget": "3000"}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert result["record_ref"] == "appier://campaigns/1001"

    def test_set_status_success_via_default_v1_stub(self):
        # Writes require the explicit approval flag (pre-action approval gate).
        state = _state(
            intent="set_status",
            approval_granted=True,
            appier_payload=to_json({"campaign_id": "1001", "status": "paused"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_appier_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `appier:` section as the JSON
        # appier_config state field; the stub transport still serves the call.
        state = _state(appier_config=to_json({"base_url": "https://appier.example.test/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "appier://campaigns/1001"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"appier": {"base_url": "https://appier.example.test/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_missing_payload_errors(self):
        result = self.node(_state(appier_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_unresolved_campaign_id_errors(self):
        state = _state(campaign_id="", appier_payload=to_json({"campaign_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved campaign id" in entry for entry in result["error_log"])

    def test_update_without_recognizable_settings_errors(self):
        state = _state(
            intent="update_campaign",
            appier_payload=to_json({"campaign_id": "1001", "campaign": {}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no recognizable settings" in entry for entry in result["error_log"])

    def test_set_status_without_target_errors(self):
        state = _state(
            intent="set_status",
            appier_payload=to_json({"campaign_id": "1001", "status": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved target status" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_campaign"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # Credentials: with a LIVE transport a missing APPIER_API_KEY is a hard
        # error - a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_key_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"APPIER_API_KEY": "mock-key-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_key"] == "mock-key-for-testing"
        assert _FakeLiveClient.captured["campaign_id"] == "1001"

    # -- pre-action approval gate (writes require explicit approval) ----------

    def test_unapproved_update_does_not_call_client_write(self, monkeypatch):
        """No approval -> the client's update_campaign is NEVER called; the node
        returns the approval_required marker + a whitelisted pending preview."""
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _SpyWriteClient)
        _SpyWriteClient.reset()
        state = _state(
            intent="update_campaign",
            appier_payload=to_json({"campaign_id": "1001", "campaign": {"daily_budget": "3000", "name": "Summer"}}),
        )
        result = self.node(state)
        assert _SpyWriteClient.update_calls == []
        assert _SpyWriteClient.status_calls == []
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["approval_required"] is True
        pending = from_json(result["pending_action"], {})
        assert pending == {
            "intent": "update_campaign",
            "campaign_id": "1001",
            "fields": ["daily_budget", "name"],
        }
        # No write happened -> no record evidence is fabricated.
        assert "record_id" not in result
        assert "record_ref" not in result

    def test_unapproved_set_status_does_not_call_client_write(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _SpyWriteClient)
        _SpyWriteClient.reset()
        state = _state(
            intent="set_status",
            appier_payload=to_json({"campaign_id": "1001", "status": "paused"}),
        )
        result = self.node(state)
        assert _SpyWriteClient.status_calls == []
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["approval_required"] is True
        pending = from_json(result["pending_action"], {})
        assert pending == {
            "intent": "set_status",
            "campaign_id": "1001",
            "target_status": "paused",
        }
        assert "record_id" not in result

    def test_approval_granted_false_still_gates_write(self, monkeypatch):
        """The gate honours ONLY the exact boolean True (is-True contract)."""
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _SpyWriteClient)
        _SpyWriteClient.reset()
        state = _state(
            intent="set_status",
            approval_granted=False,
            appier_payload=to_json({"campaign_id": "1001", "status": "paused"}),
        )
        result = self.node(state)
        assert _SpyWriteClient.status_calls == []
        assert result["approval_required"] is True

    def test_approved_update_calls_client_write(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _SpyWriteClient)
        _SpyWriteClient.reset()
        state = _state(
            intent="update_campaign",
            approval_granted=True,
            appier_payload=to_json({"campaign_id": "1001", "campaign": {"daily_budget": "3000"}}),
        )
        result = self.node(state)
        assert _SpyWriteClient.update_calls == ["1001"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert "approval_required" not in result

    def test_approved_set_status_calls_client_write(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _SpyWriteClient)
        _SpyWriteClient.reset()
        state = _state(
            intent="set_status",
            approval_granted=True,
            appier_payload=to_json({"campaign_id": "1001", "status": "paused"}),
        )
        result = self.node(state)
        assert _SpyWriteClient.status_calls == [("1001", "paused")]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_lookup_needs_no_approval(self, monkeypatch):
        """Read-only lookup is unaffected by the approval gate."""
        monkeypatch.setattr("src.nodes.call_appier_api_node.AppierClient", _SpyWriteClient)
        _SpyWriteClient.reset()
        result = self.node(_state(approval_granted=False))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert "approval_required" not in result

    def test_unapproved_write_emits_approval_required_event(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_appier_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        state = _state(
            intent="update_campaign",
            appier_payload=to_json({"campaign_id": "1001", "campaign": {"daily_budget": "3000"}}),
        )
        self.node(state)
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        assert payloads["call_appier_api_approval_required"] == {
            "intent": "update_campaign",
            "has_campaign_id": True,
        }
        assert "call_appier_api_complete" not in payloads

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_appier_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_appier_api_complete"]
        assert payload["intent"] == "lookup_campaign"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
