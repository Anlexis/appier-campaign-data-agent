"""AgentCore Platform v1.0 - inner workflow Step 4: CallAppierApi (tool side-effect).

Performs the lookup/update/status call against the Appier campaign-management
endpoints via src/services/appier_client.py.

Pre-action approval gate (confirmation/approval BEFORE externally impactful
writes): the write intents update_campaign / set_status execute ONLY when the
caller explicitly approved them (state["approval_granted"] is True - resolved
by ValidateInputNode from an `approve: yes` line / `[approved]` token in the
request text or the `approved: true` boolean in the request context). Without
approval the node performs NO client write - it returns SUCCESS with
`approval_required=True` plus a deterministic whitelisted preview of the
pending action (`pending_action` JSON: intent, campaign id, field names /
target status), which ConfirmNode renders as a request-for-approval message.
The read-only lookup_campaign intent is unaffected.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. The subgraph node passes the caller's InvocationContext into the
       inner graph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration key is read via
       ctx.secrets.get("APPIER_API_KEY") (InvocationContext.from_state(state)) -
       never os.environ, never stored in state. v1 note: while the
       deterministic NETWORK-FREE stub transport is active a missing key is
       tolerated (a sentinel placeholder is used - it is never sent anywhere
       because no request leaves the process); with a LIVE transport injected,
       a missing key is a hard status=error - a real API is never called
       unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external advertising system; HTTP 4xx/5xx surfaces as
       status=error + error_log (no silent pass).

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). The integration settings (base_url) and the runtime deadline
(timeout_s) arrive as the JSON `appier_config` / `runtime_config` state fields,
injected by the inner graph's _extra_initial_state() from config/config.yaml -
or via the optional `config["configurable"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.appier_client import DEFAULT_TIMEOUT_S, AppierApiError, AppierClient
from src.services.validation import (
    MAX_TIMEOUT_S,
    MIN_TIMEOUT_S,
    CallerInputError,
    bounded_int,
)

_SECRET_KEY = "APPIER_API_KEY"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallAppierApiNode(FunctionNode):
    """Look up / update / pause-resume an Appier campaign via the campaign API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = from_json(state.get("appier_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallAppierApiNode: missing appier_payload"],
            }

        intent = state.get("intent", "lookup_campaign") or "lookup_campaign"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["appier"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("appier_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("appier") or {}
        settings.update(override)

        # Runtime deadline: declared in config/config.yaml and forwarded by the
        # graph. Bounded rather than trusted - an out-of-range deployment value
        # is a configuration error, not a reason to call with no deadline.
        runtime = dict(from_json(state.get("runtime_config"), {}) or {})
        runtime.update(((config or {}).get("configurable") or {}))
        try:
            timeout_s = (
                float(bounded_int(runtime["timeout_s"], "timeout_s", MIN_TIMEOUT_S, MAX_TIMEOUT_S))
                if runtime.get("timeout_s") is not None
                else DEFAULT_TIMEOUT_S
            )
        except CallerInputError as exc:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallAppierApiNode: invalid runtime configuration - {exc}"],
            }

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE v1 stub (documented limitation).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = AppierClient(base_url=base_url, timeout_s=timeout_s) if base_url else AppierClient(timeout_s=timeout_s)

        # Key from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_key = ctx.secrets.get(_SECRET_KEY)
        if api_key is None:
            if client.uses_stub_transport:
                # v1 stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_key = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallAppierApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        campaign_id = state.get("campaign_id", "") or str(payload.get("campaign_id", "") or "")
        if not campaign_id:
            # Explicit-target policy (docs/02): an unresolved campaign id is a
            # hard error for EVERY intent - reads and writes alike - never guessed.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallAppierApiNode: unresolved campaign id - cannot {intent}"],
            }

        try:
            if intent == "lookup_campaign":
                resp = client.get_campaign(campaign_id, api_key) or {}
                record = resp.get("campaign") or {}
                if not record:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [f"CallAppierApiNode: no campaign found for id {campaign_id}"],
                    }
                record_id = str(record.get("id", "")) or campaign_id
                campaign_name = state.get("campaign_name", "") or str(record.get("name", ""))
            elif intent == "update_campaign":
                updates = payload.get("campaign") or {}
                if not updates:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallAppierApiNode: no recognizable settings to update "
                            "(budget/schedule/name/Key: value lines)"
                        ],
                    }
                if state.get("approval_granted") is not True:
                    # Pre-action approval gate: NO client write without an
                    # explicit approval - preview lists field NAMES only.
                    return self._approval_pending(
                        state,
                        intent,
                        campaign_id,
                        {"fields": sorted(str(k) for k in updates)},
                    )
                resp = client.update_campaign(campaign_id, {"campaign": updates}, api_key) or {}
                record_id = str(resp.get("campaign_id", "")) or campaign_id
                campaign_name = state.get("campaign_name", "")
            elif intent == "set_status":
                target_status = str(payload.get("status", "") or "")
                if not target_status:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallAppierApiNode: unresolved target status - " "cannot change campaign status"],
                    }
                if state.get("approval_granted") is not True:
                    # Pre-action approval gate: NO client write without an
                    # explicit approval - preview carries the target status.
                    return self._approval_pending(state, intent, campaign_id, {"target_status": target_status})
                resp = client.set_campaign_status(campaign_id, target_status, api_key) or {}
                record_id = str(resp.get("campaign_id", "")) or campaign_id
                campaign_name = state.get("campaign_name", "")
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallAppierApiNode: unknown intent '{intent}'"],
                }
        except AppierApiError as exc:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallAppierApiNode: {exc}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallAppierApiNode: Appier call failed: {exc}"],
            }

        record_ref = f"appier://campaigns/{record_id}" if record_id else ""

        # Audit the tool side-effect - intent + presence signals only,
        # never campaign content or credentials.
        emit_trace_event(
            "call_appier_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "campaign_id": campaign_id or record_id,
            "campaign_name": campaign_name,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- pre-action approval gate ---------------------------------------------

    def _approval_pending(
        self, state: dict[str, Any], intent: str, campaign_id: str, detail: dict[str, Any]
    ) -> dict[str, Any]:
        """Refuse an unapproved write: no client write method is called.

        Returns SUCCESS with the `approval_required` marker plus a
        deterministic, whitelisted preview of the pending action (intent /
        campaign id / field NAMES or target status - never the raw request
        body values), so ConfirmNode can render the request-for-approval
        message. The write is performed only on a later, explicitly approved
        request (see ValidateInputNode - approval_granted).
        """
        pending: dict[str, Any] = {"intent": intent, "campaign_id": campaign_id}
        pending.update(detail)

        # Audit the refused-until-approved write - signals only.
        emit_trace_event(
            "call_appier_api_approval_required",
            {"intent": intent, "has_campaign_id": bool(campaign_id)},
            state,
        )

        return {
            "approval_required": True,
            "pending_action": to_json(pending),
            "campaign_id": campaign_id,
            "campaign_name": state.get("campaign_name", ""),
            "status": AgentStatus.SUCCESS.value,
        }
