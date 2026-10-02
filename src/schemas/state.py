"""AgentCore Platform v1.0 - CMN-C2-289 Appier Campaign Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects (and nested dict/list
# containers) are not msgpack-safe. Extend AgentState with agent-specific
# fields only, and declare every domain field NotRequired[...] (fields are
# absent until their producer node writes them). appier_payload /
# appier_config / caller_campaign / redaction_flags are dicts/lists at the
# point of use but are stored in State as JSON strings via to_json/from_json
# below. Do NOT add credentials, secrets, or Pydantic models. The Appier API
# key is NEVER stored here - it is read via ctx.secrets in CallAppierApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact, msgpack-safe JSON string.

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Appier Campaign agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Appier-workflow fields are added below, all NotRequired. All values are
    JSON/msgpack-serializable primitives - the Appier API key is NEVER stored
    here (accessed via ctx.secrets).
    """

    # Caller-supplied target hint (campaign id or code from the request
    # context). Never inferred; resolution to an Appier campaign id is
    # explicit-only (v1: pass-through when the hint or request text already
    # carries an id).
    campaign_hint: NotRequired[str]
    campaign_id: NotRequired[str]  # resolved Appier campaign id

    # JSON - the validated caller campaign-change block (name / daily_budget /
    # schedule / status / settings) as accepted by the pre_process caller
    # contract. Carried across the graph boundary by the state bridge, because
    # the framework's input mask rewrites this data if it travels in the text
    # channel (src/graph/context_bridge.py).
    caller_campaign: NotRequired[Optional[str]]

    # ValidateInput (deterministic input scan)
    # JSON list[str] of patterns redacted from the text before logging
    # ((de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # ValidateInput (pre-action approval gate). True ONLY when the caller
    # explicitly approved the write: an `approve: yes` line / `[approved]`
    # token in the request text, or `approved: true` in the request context
    # (forwarded by pre_process as the `approved` envelope flag). Never
    # inferred.
    approval_granted: NotRequired[bool]

    # InferAppierFields
    campaign_name: NotRequired[str]  # campaign display name / record label
    # JSON - assembled Appier campaign-API request body (stored as a JSON
    # string, not a native dict; (de)serialize via to_json/from_json).
    appier_payload: NotRequired[Optional[str]]

    # Runtime settings forwarded by _parent_config() and injected by the inner
    # graph's _extra_initial_state() (JSON strings): the integration section
    # (base_url) and the runtime knobs (max_retry, timeout_s) from
    # config/config.yaml.
    appier_config: NotRequired[Optional[str]]
    runtime_config: NotRequired[Optional[str]]

    # CallAppierApi
    record_id: NotRequired[str]  # campaign id / record id returned by Appier
    record_ref: NotRequired[str]  # human-readable reference (appier://campaigns/<id>)

    # CallAppierApi (pre-action approval gate): a write intent
    # (update_campaign / set_status) WITHOUT approval_granted performs NO
    # client write - it returns approval_required=True plus a deterministic
    # whitelisted preview of the pending action (JSON string: intent /
    # campaign_id / field NAMES or target status - never the raw request body)
    # so ConfirmNode can render the request-for-approval.
    approval_required: NotRequired[bool]
    pending_action: NotRequired[Optional[str]]

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
