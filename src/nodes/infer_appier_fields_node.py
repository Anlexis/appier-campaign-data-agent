"""AgentCore Platform v1.0 - inner workflow Step 3: InferAppierFields.

Assembles the validated Appier campaign-API request body for the classified
intent from two sources, in this order:

1. the caller's structured campaign block, carried across the graph boundary by
   the state bridge after the entry contract validated it;
2. entities extracted from the request text (campaign id, quoted display name,
   budget amount, ISO schedule dates, target status, "Key: value" settings).

The caller block wins wherever both supply a field. Text extraction is a
convenience, and it has a hard limitation the caller needs to know about: the
framework's input gate rewrites personal-name and identifier shapes in the
request text before this node sees it, so a name written in prose can arrive
already replaced by a mask marker. Writing that marker to an advertising
platform would be worse than refusing, so a masked value is refused with a
message pointing at the structured channel.

Every value that reaches the request body - whichever source it came from -
passes the same bounds: identifiers inert, amounts finite and in range, dates
real ISO calendar dates, settings capped in count and length and restricted to
the render alphabet. An unresolved campaign id is left empty rather than
invented; the executor surfaces the miss as status=error.
"""

import re
from typing import Any, Callable

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.validation import (
    MAX_NAME_LEN,
    MAX_SETTING_VALUE_LEN,
    MAX_SETTINGS,
    CallerInputError,
    budget_amount,
    inert_identifier,
    inert_target_status,
    iso_date,
    render_text,
    setting_name,
)

# An Appier campaign id: short alphanumeric identifier (no spaces).
_ID_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit id mention in the request text, EN or JA
# ("campaign id 1001" / "campaign #C-42" / "キャンペーンID 1001").
_ID_IN_TEXT_RE = re.compile(
    r"campaign\s+(?:id|code|number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|campaign\s+#\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:キャンペーンID|キャンペーン番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted display name: named "Foo" / called "Foo". Curly quotes included so a
# pasted rich-text request still matches.
_NAME_QUOTED_RE = re.compile(r'(?:named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# Budget amount: "budget to $500" / "daily budget: 3,000" (currency optional).
_BUDGET_RE = re.compile(
    r"budget\s*(?:to|of|at|=|:)?\s*(?:[¥$]|USD|JPY)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
# Schedule range: "from 2026-08-01 to 2026-08-31" (ISO dates only - never guessed).
_DATE_RANGE_RE = re.compile(
    r"from\s+(\d{4}-\d{2}-\d{2})\s+(?:to|until|through)\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_END_DATE_RE = re.compile(r"(?:until|through)\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
# "Key: value" setting lines (ASCII or full-width colon), incl. CJK keys.
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that map to first-class fields, not custom settings.
_ID_KEYS = ("id", "campaign id", "campaign code", "campaign number")
_NAME_KEYS = ("name", "campaign name")
_BUDGET_KEYS = ("budget", "daily budget", "total budget")
_START_KEYS = ("start", "start date", "schedule start")
_END_KEYS = ("end", "end date", "schedule end")
# Approval-gate control lines ("approve: yes") are a workflow signal consumed
# by ValidateInputNode - never a campaign setting to write to Appier.
_APPROVAL_KEYS = ("approve", "approved", "approval")
# Target-status signals (activation words FIRST so "unpause"/"reactivate" is
# not swallowed by the "pause" substring).
_ACTIVATE_WORDS = ("resume", "reactivate", "unpause", "activate", "enable", "turn on", "再開", "有効化")
_PAUSE_WORDS = ("pause", "suspend", "stop", "halt", "disable", "turn off", "停止", "一時停止", "無効化")

# The marker the framework's input gate leaves behind when it rewrites a value
# in the request text.
_MASK_MARKER = "[MASKED]"
_MASKED_ADVICE = "arrived masked by the input gate - supply it in the request context " "instead of the request text"


class InferAppierFieldsNode(FunctionNode):
    """Extract entities and assemble the Appier campaign-API request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_campaign") or "lookup_campaign"
        campaign_hint = state.get("campaign_hint", "") or ""
        caller = from_json(state.get("caller_campaign"), {}) or {}

        if not text.strip():
            return self._refuse("InferAppierFieldsNode: missing validated_input")
        if not isinstance(caller, dict):
            return self._refuse("InferAppierFieldsNode: caller campaign data is not an object")

        try:
            fields = self._parse_fields(text)
            campaign_id = self._resolve_id(text, campaign_hint, fields)
            campaign_name = self._resolve_name(text, fields, caller)

            if intent == "set_status":
                payload = {
                    "campaign_id": campaign_id,
                    "status": self._resolve_status(text, caller),
                }
            elif intent == "update_campaign":
                payload = self._build_campaign_update(text, campaign_id, campaign_name, fields, caller)
            else:  # lookup_campaign (read-only default)
                payload = {"campaign_id": campaign_id}
        except CallerInputError as exc:
            # Fail closed, naming the field. The rejected value is never echoed.
            return self._refuse(f"InferAppierFieldsNode: rejected {exc}")

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_appier_fields_complete",
            {
                "intent": intent,
                "has_campaign_id": bool(campaign_id),
                "n_fields": len(fields),
                "from_caller_context": bool(caller),
            },
            state,
        )

        return {
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "appier_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- helpers --------------------------------------------------------------

    def _refuse(self, message: str) -> dict[str, Any]:
        return {"status": AgentStatus.ERROR.value, "error_log": [message]}

    def _from_text(self, value: str, field: str, checker: Callable[..., str], *args: Any) -> str:
        """Validate a text-extracted value, mapping a mask residue to advice."""
        if _MASK_MARKER in value:
            raise CallerInputError(field, _MASKED_ADVICE)
        return str(checker(value, field, *args))

    # -- extraction -----------------------------------------------------------

    def _resolve_id(self, text: str, campaign_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit id only: text mention > id-shaped hint > 'Campaign ID:' field. Never invented."""
        m = _ID_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or m.group(3) or ""
        hint = inert_identifier(campaign_hint, "campaign_hint")
        if hint:
            return hint
        for key, value in fields:
            if key.strip().lower() in _ID_KEYS and _ID_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, fields: "list[tuple[str, str]]", caller: dict[str, Any]) -> str:
        if caller.get("name"):
            return render_text(caller["name"], "campaign.name", MAX_NAME_LEN)
        m = _NAME_QUOTED_RE.search(text)
        if m:
            return self._from_text(m.group(1).strip(), "campaign name", render_text, MAX_NAME_LEN)
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS:
                return self._from_text(value.strip(), "campaign name", render_text, MAX_NAME_LEN)
        return ""

    def _resolve_status(self, text: str, caller: dict[str, Any]) -> str:
        """Deterministic target status; unresolved stays "" (executor -> status=error)."""
        if caller.get("status"):
            return inert_target_status(caller["status"], "campaign.status")
        low = text.lower()
        if any(w in low for w in _ACTIVATE_WORDS):
            return "active"
        if any(w in low for w in _PAUSE_WORDS):
            return "paused"
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] setting fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- payload assembly (Appier campaign-API PATCH shape) --------------------

    def _build_campaign_update(
        self, text: str, campaign_id: str, campaign_name: str, fields: "list[tuple[str, str]]", caller: dict[str, Any]
    ) -> dict[str, Any]:
        campaign: dict[str, Any] = {}
        if campaign_name:
            campaign["name"] = campaign_name

        budget = self._resolve_budget(text, fields, caller)
        if budget:
            campaign["daily_budget"] = budget

        start_date, end_date = self._resolve_schedule(text, fields, caller)
        schedule: dict[str, Any] = {}
        if start_date:
            schedule["start_date"] = start_date
        if end_date:
            schedule["end_date"] = end_date
        if schedule:
            campaign["schedule"] = schedule

        settings = self._resolve_settings(fields, caller)
        if settings:
            campaign["settings"] = settings

        return {"campaign_id": campaign_id, "campaign": campaign}

    def _resolve_budget(self, text: str, fields: "list[tuple[str, str]]", caller: dict[str, Any]) -> str:
        if caller.get("daily_budget") is not None:
            return budget_amount(caller["daily_budget"], "campaign.daily_budget")
        for key, value in fields:
            if key.strip().lower() in _BUDGET_KEYS:
                m = _BUDGET_RE.search(f"budget: {value}")
                if m:
                    return self._from_text(m.group(1), "campaign budget", budget_amount)
        m = _BUDGET_RE.search(text)
        if m:
            return self._from_text(m.group(1), "campaign budget", budget_amount)
        return ""

    def _resolve_schedule(
        self, text: str, fields: "list[tuple[str, str]]", caller: dict[str, Any]
    ) -> "tuple[str, str]":
        start_date = iso_date(caller.get("start_date"), "campaign.start_date")
        end_date = iso_date(caller.get("end_date"), "campaign.end_date")
        for key, value in fields:
            low = key.strip().lower()
            if low in _START_KEYS and not start_date:
                start_date = self._from_text(value.strip(), "schedule start date", iso_date)
            elif low in _END_KEYS and not end_date:
                end_date = self._from_text(value.strip(), "schedule end date", iso_date)
        m = _DATE_RANGE_RE.search(text)
        if m:
            start_date = start_date or iso_date(m.group(1), "schedule start date")
            end_date = end_date or iso_date(m.group(2), "schedule end date")
        elif not end_date:
            m = _END_DATE_RE.search(text)
            if m:
                end_date = iso_date(m.group(1), "schedule end date")
        if start_date and end_date and end_date < start_date:
            raise CallerInputError("schedule end date", "must not precede the start date")
        return start_date, end_date

    def _resolve_settings(self, fields: "list[tuple[str, str]]", caller: dict[str, Any]) -> "list[dict[str, str]]":
        caller_settings = caller.get("settings") or []
        if caller_settings:
            if not isinstance(caller_settings, list) or len(caller_settings) > MAX_SETTINGS:
                raise CallerInputError("campaign.settings", f"must be a list of at most {MAX_SETTINGS} entries")
            resolved: list[dict[str, str]] = []
            for entry in caller_settings:
                if not isinstance(entry, dict):
                    raise CallerInputError("campaign.settings", "entries must be objects with 'name' and 'value'")
                resolved.append(
                    {
                        "name": setting_name(entry.get("name"), "campaign.settings.name"),
                        "value": render_text(entry.get("value"), "campaign.settings.value", MAX_SETTING_VALUE_LEN),
                    }
                )
            return resolved
        skip_keys = _ID_KEYS + _NAME_KEYS + _BUDGET_KEYS + _START_KEYS + _END_KEYS + _APPROVAL_KEYS
        settings: list[dict[str, str]] = []
        for key, value in fields:
            if key.strip().lower() in skip_keys:
                continue
            if len(settings) >= MAX_SETTINGS:
                raise CallerInputError("campaign settings", f"must contain at most {MAX_SETTINGS} entries")
            settings.append(
                {
                    "name": self._from_text(key, "setting name", setting_name),
                    "value": self._from_text(value, "setting value", render_text, MAX_SETTING_VALUE_LEN),
                }
            )
        return settings
