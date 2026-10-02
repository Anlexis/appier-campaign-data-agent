"""AgentCore Platform v1.0 - outer pre_process node.

Owns the caller-data contract. Two channels arrive here and they are treated
differently:

* the request TEXT (``user_input``) — free natural language. It is screened for
  prompt-injection markers, stripped of markup, length-capped, and serialized
  into ``validated_input`` for the inner workflow graph.
* the request CONTEXT (``input_context``) — structured campaign data. Every
  field is checked against an explicit allowlist and explicit bounds here;
  anything unrecognised, malformed, non-finite or outside the render alphabet
  is refused, and the refusal names the field without repeating the value.

The validated campaign data does NOT travel inside the text channel: the
framework's input gate rewrites personal-name and identifier shapes in
``user_input`` / ``validated_input``, which would corrupt a campaign name on
its way to the advertising platform. It crosses the graph boundary through the
state bridge instead (``src/graph/context_bridge.py``).

Business validation of the request text (empty guard, redaction) happens inside
the inner graph; this node does the caller-contract work the entry point cannot
do and the inner graph would do too late.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.graph.context_bridge import stash_caller_campaign
from src.services.security import sanitize_query, screen_raw_and_sanitized, screen_structure
from src.services.validation import (
    MAX_NAME_LEN,
    MAX_SETTING_VALUE_LEN,
    MAX_SETTINGS,
    CallerInputError,
    budget_amount,
    inert_identifier,
    iso_date,
    inert_target_status,
    render_text,
    setting_name,
)

# Caller context: exactly these keys, nothing else.
_TARGET_KEYS = ("campaign_id", "campaign_hint", "campaign_code")
_CONTEXT_KEYS = (*_TARGET_KEYS, "approved", "campaign")
_CAMPAIGN_KEYS = ("name", "daily_budget", "start_date", "end_date", "status", "settings")

# Platform-seeded, not caller-supplied: the AgentCore 1.0.3 Marketplace runner
# always adds conversation_history to input_context, predating this whitelist.
# Recognised-and-ignored so every invocation isn't refused before the caller's
# request is read; it carries no campaign data.
_PLATFORM_SEEDED_KEYS = ("conversation_history",)


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's single external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Appier call runs under that same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted caller is denied before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not user_input or not user_input.strip():
            return self._refuse("PreProcessNode: user_input is empty or missing")

        raw_text = user_input.strip()
        sanitized_input = sanitize_query(raw_text)

        markers = screen_raw_and_sanitized(raw_text, sanitized_input)
        if markers:
            # Screened on the raw text AND on the sanitized text: the markup
            # strip deletes control tokens, so the raw pass is the only one that
            # can see them, and it re-assembles spliced directives, so the
            # sanitized pass is the only one that can see those.
            return self._refuse("PreProcessNode: request text refused - injection markers " f"({', '.join(markers)})")

        try:
            context = self._validate_context(input_context)
        except CallerInputError as exc:
            return self._refuse(f"PreProcessNode: rejected input_context ({exc})")

        campaign_hint = context["campaign_hint"]
        validated_input = json.dumps(
            {
                "text": sanitized_input,
                "campaign_hint": campaign_hint,
                "approved": context["approved"],
            }
        )

        # Hand the validated campaign data to the inner graph out of band — see
        # the module docstring.
        caller_campaign = json.dumps(context["campaign"], ensure_ascii=False)
        stash_caller_campaign(caller_campaign)

        # Audit the shaped request - presence signals only, never the text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_campaign_hint": bool(campaign_hint),
                "has_caller_campaign": bool(context["campaign"]),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "campaign_hint": campaign_hint,
            "caller_campaign": caller_campaign,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller contract ------------------------------------------------------

    def _refuse(self, message: str) -> dict[str, Any]:
        """Fail closed. The message names the field; it never carries the value."""
        return {"status": AgentStatus.ERROR.value, "error_log": [message]}

    def _validate_context(self, input_context: object) -> dict[str, Any]:
        """Validate the caller context against the allowlist and the bounds.

        Returns ``{"campaign_hint": str, "approved": bool, "campaign": dict}``.
        Raises CallerInputError naming the offending field.
        """
        if input_context in (None, {}):
            return {"campaign_hint": "", "approved": False, "campaign": {}}
        if not isinstance(input_context, dict):
            raise CallerInputError("", "must be an object")

        # Screen the parsed structure depth-first, keys included, before any
        # field is read: a hostile field NAME is as much an injection vector as
        # a hostile value, and JSON escapes are already decoded by this point.
        markers = screen_structure(input_context)
        if markers:
            raise CallerInputError("", f"injection markers ({'; '.join(markers)})")

        unknown = [key for key in input_context if key not in _CONTEXT_KEYS and key not in _PLATFORM_SEEDED_KEYS]
        if unknown:
            # Report the count and position, never the caller's key names.
            raise CallerInputError("", f"{len(unknown)} unrecognised field(s); accepted: {', '.join(_CONTEXT_KEYS)}")

        approved = input_context.get("approved")
        if approved is not None and not isinstance(approved, bool):
            raise CallerInputError("approved", "must be a JSON boolean")

        # Target campaign: caller-supplied, never inferred. Priority
        # campaign_id > campaign_hint > campaign_code; every candidate is
        # validated so a malformed one is refused rather than skipped over.
        hint = ""
        for key in _TARGET_KEYS:
            candidate = inert_identifier(input_context.get(key), key)
            if candidate and not hint:
                hint = candidate

        return {
            "campaign_hint": hint,
            "approved": approved is True,
            "campaign": self._validate_campaign(input_context.get("campaign")),
        }

    def _validate_campaign(self, campaign: object) -> dict[str, Any]:
        """Validate the structured campaign-change block."""
        if campaign in (None, {}):
            return {}
        if not isinstance(campaign, dict):
            raise CallerInputError("campaign", "must be an object")

        unknown = [key for key in campaign if key not in _CAMPAIGN_KEYS]
        if unknown:
            raise CallerInputError(
                "campaign", f"{len(unknown)} unrecognised field(s); accepted: {', '.join(_CAMPAIGN_KEYS)}"
            )

        validated: dict[str, Any] = {}
        name = render_text(campaign.get("name"), "campaign.name", MAX_NAME_LEN)
        if name:
            validated["name"] = name
        if campaign.get("daily_budget") is not None:
            validated["daily_budget"] = budget_amount(campaign["daily_budget"], "campaign.daily_budget")
        start_date = iso_date(campaign.get("start_date"), "campaign.start_date")
        if start_date:
            validated["start_date"] = start_date
        end_date = iso_date(campaign.get("end_date"), "campaign.end_date")
        if end_date:
            validated["end_date"] = end_date
        if start_date and end_date and end_date < start_date:
            raise CallerInputError("campaign.end_date", "must not precede campaign.start_date")
        status = inert_target_status(campaign.get("status"), "campaign.status")
        if status:
            validated["status"] = status

        settings = self._validate_settings(campaign.get("settings"))
        if settings:
            validated["settings"] = settings
        return validated

    def _validate_settings(self, settings: object) -> "list[dict[str, str]]":
        if settings in (None, []):
            return []
        if not isinstance(settings, list):
            raise CallerInputError("campaign.settings", "must be a list")
        if len(settings) > MAX_SETTINGS:
            raise CallerInputError("campaign.settings", f"must contain at most {MAX_SETTINGS} entries")
        validated: list[dict[str, str]] = []
        for index, entry in enumerate(settings):
            field = f"campaign.settings[{index}]"
            if not isinstance(entry, dict):
                raise CallerInputError(field, "must be an object with 'name' and 'value'")
            if set(entry) - {"name", "value"}:
                raise CallerInputError(field, "accepts only 'name' and 'value'")
            validated.append(
                {
                    "name": setting_name(entry.get("name"), f"{field}.name"),
                    "value": render_text(entry.get("value"), f"{field}.value", MAX_SETTING_VALUE_LEN),
                }
            )
        return validated
