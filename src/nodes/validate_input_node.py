"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input and runs a deterministic (regex, NOT LLM)
scan of the inbound text for email addresses / access-token-like strings,
which are flag-and-redacted before anything is logged. A campaign request
legitimately names a campaign and its settings (the framework PII mask in
BaseNode.__call__ additionally masks emails/phones/names in user_input /
validated_input), so this is flag-and-redact for safe logging, not a hard
reject. The only deterministic auto-reject is the empty / non-request guard.

Pre-action approval gate (write authorization signal): this node also resolves
the deterministic `approval_granted` flag - True ONLY on an explicit
`approve: yes` line / `[approved]` token in the request text, or the exact
boolean `approved: true` envelope flag forwarded by pre_process from
input_context. CallAppierApiNode refuses to execute a write intent
(update_campaign / set_status) without it.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# Deterministic patterns: email addresses and bearer/JWT/API-token-like
# strings that might appear in a pasted request. Flagged + redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3

# Pre-action approval gate: explicit approval phrases only (deterministic,
# never inferred). Either a dedicated `approve: yes` line (ASCII or full-width
# colon) or the literal `[approved]` token anywhere in the request text.
_APPROVE_LINE_RE = re.compile(r"^\s*approve\s*[:：]\s*yes\s*$", re.IGNORECASE | re.MULTILINE)
_APPROVED_TOKEN = "[approved]"


class ValidateInputNode(FunctionNode):
    """Validate and flag-and-redact the inbound campaign request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        campaign_hint = state.get("campaign_hint", "")
        context_approved = False
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                campaign_hint = obj.get("campaign_hint", campaign_hint)
                # Envelope flag forwarded by pre_process from
                # input_context.get("approved") is True - exact boolean only.
                context_approved = obj.get("approved") is True
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Pre-action approval gate: explicit signals only - the caller-context
        # boolean (envelope) or an explicit approval phrase in the text. Never
        # inferred from intent wording.
        approval_granted = bool(context_approved or _APPROVE_LINE_RE.search(text) or _APPROVED_TOKEN in text.lower())

        # Audit the scan outcome - redaction flags + approval signal
        # only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {
                "has_campaign_hint": bool(campaign_hint),
                "redaction_flags": flags,
                "approval_granted": approval_granted,
            },
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "campaign_hint": campaign_hint,
            "redaction_flags": to_json(flags),
            "approval_granted": approval_granted,
            "status": AgentStatus.SUCCESS.value,
        }
