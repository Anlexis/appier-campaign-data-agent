"""AgentCore Platform v1.0 - CMN-C2-289 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in AppierWorkflowGraphNode (`main`
slot), which wraps the inner AppierWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import stash_caller_campaign
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State

if TYPE_CHECKING:  # import cycle at runtime — the subgraph imports this module
    from src.graph.domain_workflow_graph import AppierWorkflowGraph

# Runtime parameters: src/graph/graph.py -> parents[2] = repo root.
# config/agent.yaml is the static registry manifest (identity, entry point,
# declared secrets); the values the pipeline READS at runtime — the integration
# section and the runtime knobs — live in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Runtime keys forwarded to the inner graph. max_retry is additionally read by
# the framework's own backbone routing from the graph config.
_RUNTIME_KEYS = ("max_retry", "timeout_s")


def load_runtime_config() -> "dict[str, Any]":
    """Read config/config.yaml.

    A missing or unreadable file degrades to the documented defaults rather
    than failing construction; every consumer validates what it reads.
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class AppierWorkflowGraphNode(GraphNode):
    """Wraps the inner Appier workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config().
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "AppierWorkflowGraph":
        from src.graph.domain_workflow_graph import AppierWorkflowGraph

        return AppierWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        #
        # The subgraph call carries a single string and nothing else, so the
        # validated caller campaign data is handed over through the state
        # bridge instead of being packed into that string — where the
        # framework's input mask would rewrite it (see context_bridge.py).
        stash_caller_campaign(str(state.get("caller_campaign") or ""))
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "campaign_id": sub_result.get("campaign_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "campaign_name": sub_result.get("campaign_name", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "approval_required": sub_result.get("approval_required") is True,
            "pending_action": sub_result.get("pending_action", ""),
            "appier_payload": sub_result.get("appier_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime parameters to the inner graph under config["configurable"].

        Reads config/config.yaml — the integration section (`appier`), the
        `llm` section when declared, and the runtime knobs. Nodes take no
        constructor arguments, so the file is the node's only route to the
        deployed values; forwarding {} is the failure this method exists to
        avoid, because every declared setting would then be silently dead.
        """
        runtime = load_runtime_config()
        configurable: dict[str, Any] = {}
        for key in ("appier", "llm"):
            section = runtime.get(key)
            if isinstance(section, dict):
                configurable[key] = section
        for key in _RUNTIME_KEYS:
            if key in runtime:
                configurable[key] = runtime[key]
        return {"configurable": configurable}


class AppierCampaignAgent(AgentBaseGraph):
    """CMN-C2-289 outer graph - Appier Campaign Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in AppierWorkflowGraphNode (`main` slot); Appier
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_289"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = AppierWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Structured product: extend the framework envelope with the campaign
        keys ONLY on SUCCESS - an errored or gate-blocked run surfaces no
        structured payload."""
        output: dict[str, Any] = super().get_output(state)
        if state.get("status") != AgentStatus.SUCCESS.value:
            return output
        output.update(
            {
                "intent": state.get("intent", ""),
                "campaign_id": state.get("campaign_id", ""),
                "record_id": state.get("record_id", ""),
                "record_ref": state.get("record_ref", ""),
                "campaign_name": state.get("campaign_name", ""),
                "confirmation": state.get("confirmation", ""),
                # Pre-action approval gate: lets callers programmatically
                # distinguish a pending (approval-required, no write happened)
                # SUCCESS from a completed one.
                "approval_required": state.get("approval_required") is True,
            }
        )
        return output
