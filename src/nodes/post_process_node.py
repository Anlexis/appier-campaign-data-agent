"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Appier workflow
graph has run. The subgraph node merges the inner result into the outer state;
this node shapes the caller-facing `formatted_output` and is the last place the
response can be stopped.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT one of
the framework gate hooks: the framework gate methods are final on FunctionNode
and the runtime auto-wraps the extension hooks, which breaks the call chain, so
domain checks live in a module-level helper invoked inline.

Containment, not just detection: when the gate finds a violation the node
returns an ERROR **and clears every output-bearing state field**. The framework
envelope falls back to `state["result"]` whenever `formatted_output` is empty,
including on an error status - so a gate that only flipped the status, or that
raised, would still ship the un-gated inner answer inside the error envelope.
Clearing the fields is what makes the refusal real.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework's own output scan in FunctionNode also runs on every result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Every state field that can carry released text to the caller. On a violation
# all of them are cleared, so the error envelope has nothing to fall back to.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "confirmation",
    "appier_payload",
    "pending_action",
    "record_id",
    "record_ref",
    "campaign_name",
)


def _security_gate_output(formatted_output: dict[str, Any], is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (record_id/record_ref), which
        would misrepresent the Appier action outcome to the caller. The ONE
        exception is an explicit approval-required response
        (`approval_required=True`): the pre-action approval gate refused the
        write, so no record exists BY DESIGN - such a response must instead
        carry the request-for-approval confirmation and must NOT carry record
        evidence (a response cannot claim to be both pending and completed);
      - any credential-shaped string in the caller-facing output, at any depth -
        the payload and the pending-action preview are nested structures, and a
        scan of the top level only would not see inside them.
    """
    problems: list[str] = []
    approval_required = formatted_output.get("approval_required") is True
    if is_success and approval_required:
        if formatted_output.get("record_id") or formatted_output.get("record_ref"):
            problems.append(
                "PostProcess output gate: approval-required output carries record "
                "evidence (pending and completed are mutually exclusive)"
            )
        if not formatted_output.get("confirmation"):
            problems.append(
                "PostProcess output gate: approval-required output missing the " "request-for-approval confirmation"
            )
    elif is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for key, value in formatted_output.items():
        if _contains_credential(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{key}']")
    return problems


def _contains_credential(value: object) -> bool:
    """Depth-first credential scan of a JSON-like value, keys included."""
    if isinstance(value, str):
        return bool(_CREDENTIAL_LIKE_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_credential(key) or _contains_credential(nested) for key, nested in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_credential(item) for item in value)
    return False


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        errored = state.get("status") == AgentStatus.ERROR.value

        # If the inner workflow errored, preserve the error status (do not mask
        # it) and release nothing: the error entries are this template's own
        # messages, and every field that could carry inner text is cleared.
        if errored:
            # error_log already carries the inner entries; the state reducer
            # appends, so re-emitting them here would duplicate every line.
            return self._contain()

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "campaign_name": state.get("campaign_name", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "appier_payload": from_json(state.get("appier_payload"), {}),
            # Pre-action approval gate: an unapproved write surfaces as an
            # explicit approval-required response (no write happened) with the
            # whitelisted pending-action preview.
            "approval_required": state.get("approval_required") is True,
            "pending_action": from_json(state.get("pending_action"), {}),
        }

        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # Containment: ERROR *and* clear the output-bearing fields, so the
            # envelope cannot fall back to the un-gated inner answer.
            emit_trace_event(
                "post_process_output_blocked",
                {"violations": len(violations)},
                state,
            )
            return self._contain(violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
                "approval_required": state.get("approval_required") is True,
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- containment ----------------------------------------------------------

    def _contain(self, new_errors: "list[str] | None" = None) -> dict[str, Any]:
        """Return an ERROR result carrying no released text.

        Every output-bearing field is written back as empty rather than left
        untouched: the envelope reads `formatted_output` first and `result`
        second, so leaving either populated would ship the very content the
        gate refused. Only NEW error entries are returned - the state reducer
        for error_log appends, so echoing the existing ones would duplicate
        them.
        """
        contained: dict[str, Any] = {field: "" for field in _OUTPUT_BEARING_FIELDS}
        contained["formatted_output"] = {}
        contained["approval_required"] = False
        contained["status"] = AgentStatus.ERROR.value
        if new_errors:
            contained["error_log"] = list(new_errors)
        return contained
