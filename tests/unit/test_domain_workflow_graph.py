# CMN-C2-289 - Unit tests: inner AppierWorkflowGraph (BaseGraph) contract.
# The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import AppierWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return AppierWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "appier_campaign_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_appier_config_as_json():
    g = _graph({"configurable": {"appier": {"base_url": "https://appier.example.test/v1"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict.
    assert isinstance(extra["appier_config"], str)
    assert from_json(extra["appier_config"], {}) == {"base_url": "https://appier.example.test/v1"}


def test_extra_initial_state_empty_without_appier_section():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "1001", "record_ref": "appier://campaigns/1001", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_campaign",
            "campaign_id": "1001",
            "record_id": "1001",
            "record_ref": "appier://campaigns/1001",
            "campaign_name": "Campaign 1001",
            "confirmation": "ok",
            "appier_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_campaign"
    assert out["record_ref"] == "appier://campaigns/1001"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "1001", "record_ref": "appier://campaigns/1001", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm.
    A read-only lookup needs NO approval (the gate covers writes only)."""
    g = _graph({"configurable": {"appier": {"base_url": "https://api.appier.com/v1"}}})
    g.compile()
    result = g.invoke(
        user_input="Look up the campaign settings for campaign id 1001 and summarize the current budget and schedule."
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "1001"
    assert result["record_ref"] == "appier://campaigns/1001"
    assert result["intent"] == "lookup_campaign"
    assert result["confirmation"]
    assert result["approval_required"] is False
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferAppierFieldsNode",
        "CallAppierApiNode",
        "ConfirmNode",
    ]


def test_inner_invoke_unapproved_write_requires_approval():
    """Pre-action approval gate, full inner path: a write request WITHOUT an
    explicit approval performs NO write - the pipeline completes with the
    approval_required marker, the pending-action preview, and a
    request-for-approval confirmation (no record evidence)."""
    g = _graph({"configurable": {"appier": {"base_url": "https://api.appier.com/v1"}}})
    g.compile()
    result = g.invoke(user_input="Pause the campaign with campaign id 1001")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["intent"] == "set_status"
    assert result["approval_required"] is True
    pending = from_json(result["pending_action"], {})
    assert pending == {"intent": "set_status", "campaign_id": "1001", "target_status": "paused"}
    assert result["record_id"] == ""
    assert result["record_ref"] == ""
    assert "Approval required" in result["confirmation"]
    assert "approve: yes" in result["confirmation"]


def test_inner_invoke_approved_write_proceeds():
    """Pre-action approval gate, full inner path: the SAME write request WITH
    the explicit `approve: yes` line executes the write and returns the
    completed-action confirmation with record evidence."""
    g = _graph({"configurable": {"appier": {"base_url": "https://api.appier.com/v1"}}})
    g.compile()
    result = g.invoke(user_input="Pause the campaign with campaign id 1001\napprove: yes")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["intent"] == "set_status"
    assert result["approval_required"] is False
    assert result["record_id"] == "1001"
    assert result["record_ref"] == "appier://campaigns/1001"
    assert "Changed campaign status" in result["confirmation"]
    assert "Approval required" not in result["confirmation"]
