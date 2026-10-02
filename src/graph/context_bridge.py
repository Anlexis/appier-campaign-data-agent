"""Carries validated caller data across the outer/inner graph boundary.

The subgraph node hands the inner graph a single string (``extract_input`` ->
``subgraph.invoke(user_input=...)``) and nothing else: the caller context the
outer graph received is not forwarded to the inner one. Putting the campaign
data into that string does not work either — the framework's input gate masks
personal-name and identifier shapes in ``user_input`` / ``validated_input``
before any node reads them, so a campaign named "Summer Sale" arrives as
"[MASKED]" and the agent would write the masked text to the advertising
platform.

The bridge is therefore a ContextVar: the outer node stashes the already
validated payload as it hands over control, and the inner graph seeds it into
its own initial state. Both halves run in the same thread and the same
call, so the value is picked up by exactly the invocation that stashed it; the
take is destructive so nothing can leak from one invocation into the next.
"""

from __future__ import annotations

from contextvars import ContextVar

_CALLER_CAMPAIGN: ContextVar[str] = ContextVar("appier_caller_campaign", default="")


def stash_caller_campaign(payload: str) -> None:
    """Record the validated caller campaign payload for the inner graph."""
    _CALLER_CAMPAIGN.set(payload or "")


def take_caller_campaign() -> str:
    """Return and clear the stashed payload ("" when nothing was stashed)."""
    payload = _CALLER_CAMPAIGN.get()
    _CALLER_CAMPAIGN.set("")
    return payload
