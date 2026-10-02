"""AgentCore Platform v1.0 - inner Appier workflow graph (Cat 2 domain workflow).

Instantiated by AppierWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_appier_fields
          -> call_appier_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    appier     - integration section (base_url, ...) from config/config.yaml;
                 injected into State as the JSON `appier_config` field via
                 _extra_initial_state() so the no-arg nodes can read it
    llm        - reserved for the documented synthesis follow-up; unused in
                 deterministic v1
    max_retry  - backbone retry ceiling, also validated and used by the
                 framework's own routing on the outer graph
    timeout_s  - deadline handed to the Appier client for each call

The validated caller campaign data arrives through the state bridge rather
than through config or the input string - see src/graph/context_bridge.py.

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import take_caller_campaign
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_appier_fields_node import InferAppierFieldsNode
from src.nodes.call_appier_api_node import CallAppierApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json

_RUNTIME_KEYS = ("max_retry", "timeout_s")


class AppierWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "appier_campaign_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the appier section is optional (the client
        # falls back to the documented default base_url + the network-free v1
        # stub transport), and a missing/unusable setting is handled at
        # CallAppierApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_appier_fields"] = InferAppierFieldsNode()
        self._nodes["call_appier_api"] = CallAppierApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_appier_fields")
        self._sg.add_edge("infer_appier_fields", "call_appier_api")
        self._sg.add_edge("call_appier_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        """Required by the BaseGraph contract; the topology above is linear.

        Annotated with this graph's OWN State: a path callable's annotation is
        read as its input schema and fields outside it are projected away, so
        an annotation naming a wider base type would hide the very fields a
        route decides on. Kept correct here even though no conditional edge
        references it today, so that adding one cannot introduce that failure.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state: forwarded settings + the bridged caller data.

        Dict/list values are stored as JSON strings so the state stays
        msgpack-safe for checkpointing, and the no-arg nodes read them back
        with from_json().
        """
        configurable = self.config.get("configurable") or {}
        extra: dict[str, Any] = {}
        appier = configurable.get("appier") or {}
        if appier:
            extra["appier_config"] = to_json(appier)
        runtime = {k: configurable[k] for k in _RUNTIME_KEYS if k in configurable}
        if runtime:
            extra["runtime_config"] = to_json(runtime)
        caller_campaign = take_caller_campaign()
        if caller_campaign:
            extra["caller_campaign"] = caller_campaign
        return extra

    def get_output(self, state: State) -> dict[str, Any]:
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "campaign_id": state.get("campaign_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "campaign_name": state.get("campaign_name", ""),
            "confirmation": state.get("confirmation", ""),
            "approval_required": state.get("approval_required") is True,
            "pending_action": state.get("pending_action", ""),
            "appier_payload": state.get("appier_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
