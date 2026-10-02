"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_campaign / update_campaign
/ set_status using a deterministic keyword heuristic, so the template is
testable and runnable without a live LLM (v1 Implementation Note - LLM
synthesis, docs/02_design.md). Low-confidence / unknown falls back to the
read-only "lookup_campaign" default with a note - never a write (a budget,
schedule, or status change spends real ad money).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_campaign", "update_campaign", "set_status")

# Deterministic keyword signals (checked in priority order, writes first so a
# "pause the campaign then show it" style request classifies as the write;
# status keywords before generic update keywords so "pause" wins over "change").
_KEYWORDS = (
    (
        "set_status",
        (
            "pause",
            "paused",
            "suspend",
            "stop the",
            "halt",
            "disable",
            "resume",
            "reactivate",
            "unpause",
            "activate",
            "enable",
            "turn off",
            "turn on",
            "停止",
            "一時停止",
            "再開",
            "有効化",
            "無効化",
        ),
    ),
    (
        "update_campaign",
        (
            "update",
            "change",
            "edit",
            "modify",
            "adjust",
            "revise",
            "increase",
            "decrease",
            "raise",
            "lower",
            "set the",
            "reschedule",
            "extend",
            "rename",
            "budget to",
            "更新",
            "変更",
            "修正",
            "調整",
            "延長",
        ),
    ),
    (
        "lookup_campaign",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "how much",
            "summarize",
            "check",
            "status of",
            "settings for",
            "settings of",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into an Appier campaign operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_campaign (read-only)"]
            intent = "lookup_campaign"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: dict[str, Any] = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
