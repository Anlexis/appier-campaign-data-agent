"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Two modes (pre-action approval gate):

* Completed-action confirmation - formats the looked-up / updated /
  status-changed Appier campaign (id + reference + name) into a human-readable
  confirmation message, surfacing the affected campaign for human review.
* Request-for-approval - when CallAppierApiNode refused an unapproved write
  (state["approval_required"] is True, NO client write happened), renders the
  deterministic pending-action preview (intent, campaign id, field names /
  target status - never raw request-body values) plus the exact instructions
  for approving: resend with an `approve: yes` line / `[approved]` token, or
  `approved: true` in the request context.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

_VERBS = {
    "lookup_campaign": "Retrieved campaign settings",
    "update_campaign": "Updated campaign settings",
    "set_status": "Changed campaign status",
}

_PENDING_VERBS = {
    "update_campaign": "update campaign settings",
    "set_status": "change campaign status",
}

_HOW_TO_APPROVE = (
    "resend the request with an 'approve: yes' line (or the [approved] token, "
    "or approved=true in the request context) to execute it"
)


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation / request-for-approval."""

    # Inner domain node, read-only formatting of already-fetched data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        intent = state.get("intent", "lookup_campaign")
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        campaign_name = state.get("campaign_name", "")
        approval_required = state.get("approval_required") is True

        if approval_required:
            # Request-for-approval mode: NO write happened, so there is no
            # record evidence to demand - render the pending action instead.
            pending = from_json(state.get("pending_action"), {}) or {}
            campaign_id = str(pending.get("campaign_id", "") or state.get("campaign_id", ""))
            verb = _PENDING_VERBS.get(intent, "perform this campaign change")
            parts = [
                f"Approval required - no change has been made. Pending action: {verb} "
                f"for '{campaign_name or campaign_id}'"
            ]
            if campaign_id:
                parts.append(f"id={campaign_id}")
            fields = pending.get("fields") or []
            if fields:
                parts.append("fields: " + ", ".join(str(f) for f in fields))
            target_status = str(pending.get("target_status", "") or "")
            if target_status:
                parts.append(f"target status: {target_status}")
            parts.append(f"To approve, {_HOW_TO_APPROVE}.")
            confirmation = " - ".join(parts)
            # Flat whitelisted scalars only (msgpack-safe state): intent, campaign
            # id, field NAMES, target status - never raw request-body values.
            result: dict[str, Any] = {
                "approval_required": True,
                "intent": intent,
                "campaign_id": campaign_id,
                "pending_fields": [str(f) for f in fields],
                "target_status": target_status,
                "confirmation": confirmation,
            }
        else:
            if not record_id and not record_ref:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
                }
            verb = _VERBS.get(intent, "Processed campaign")
            parts = [f"{verb} '{campaign_name or record_id}'"]
            if record_ref:
                parts.append(f"ref={record_ref}")
            if record_id:
                parts.append(f"id={record_id}")
            confirmation = " - ".join(parts)
            result = {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            }

        # Audit the confirmed / approval-pending action - intent +
        # reference presence + gate mode only (no content).
        emit_trace_event(
            "confirm_complete",
            {
                "intent": intent,
                "has_record_ref": bool(record_ref),
                "approval_required": approval_required,
            },
            state,
        )

        return {
            "confirmation": confirmation,
            "result": result,
            "status": AgentStatus.SUCCESS.value,
        }
