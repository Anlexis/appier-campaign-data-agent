"""Caller-input validation contract for the Appier campaign agent.

Pure, stateless helpers (no framework imports). Every value that a caller can
influence — identifiers, campaign names, budget amounts, schedule dates,
setting entries — passes through one of these before it is assembled into an
Appier request body or rendered back to the caller.

Two rules the whole module exists to enforce:

* **Numbers are finite and bounded.** ``float("NaN")`` and ``float("inf")``
  parse cleanly and JSON accepts the bare literals ``NaN`` / ``Infinity``, but
  every comparison against NaN is False — so an unchecked amount silently
  passes any range check that was meant to stop it. Parsing therefore rejects
  bools, non-numerics, non-finite values and out-of-range magnitudes, and it
  fails CLOSED.
* **Caller strings that render are restricted to an explicit alphabet.** A
  campaign name and a setting value are written to an external advertising
  platform and echoed back in the confirmation, so they are output the caller
  controls. The alphabet below admits ordinary campaign wording (including
  Japanese) and structurally excludes the characters that markup, chat-template
  control tokens and mail addresses need.

Rejections name the FIELD and never repeat the value: an error message is a
log line and a caller-visible string, and echoing a rejected value back into
either is how a rejected payload gets a second delivery route.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

# An Appier campaign id: short identifier, no spaces. Also the alphabet the
# caller-supplied target hint must fit.
INERT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")

# The alphabet caller text is allowed to render in: word characters (Unicode —
# Japanese campaign names are ordinary here), spaces, and a small punctuation
# set. Excluded by construction: < > | [ ] { } @ : " \ and every control
# character, which is what markup, control tokens such as <|im_start|> /
# [INST] / <<SYS>>, and mail addresses are built from.
RENDER_TEXT_RE = re.compile(r"^[\w \-./&()'’,+#]+$", re.UNICODE)

# A setting name is a label, not prose.
SETTING_NAME_RE = re.compile(r"^[\w][\w \-_.]{0,40}$", re.UNICODE)

MAX_NAME_LEN = 100
MAX_SETTING_VALUE_LEN = 200
MAX_SETTINGS = 20
MAX_DATE_LEN = 10

# A daily budget is money on an advertising platform: strictly positive and
# capped well below the point where a typo becomes an incident.
MIN_BUDGET = 0.01
MAX_BUDGET = 1_000_000_000.0

# Runtime knobs from config/config.yaml. Bounded for the same reason: a typo in
# a deployed config must not produce an unbounded call deadline.
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 600


class CallerInputError(ValueError):
    """A caller-supplied value failed validation.

    Carries the offending field NAME so the pipeline can report which input was
    refused. The value itself is never captured.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field} - {reason}" if field else reason)


def _reject_control_characters(value: str, field: str) -> None:
    for char in value:
        if unicodedata.category(char)[0] == "C":
            raise CallerInputError(field, "contains a control character")


def inert_identifier(value: object, field: str) -> str:
    """Return *value* as an identifier, or raise.

    Empty is allowed and returns "" — an absent target is resolved elsewhere
    (or refused by the executor), which is a different failure from a malformed
    one.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        return ""
    if not INERT_ID_RE.match(candidate):
        raise CallerInputError(field, "must be 1-20 characters of letters, digits, '-' or '_'")
    return candidate


def render_text(value: object, field: str, max_length: int) -> str:
    """Return *value* as caller-rendered text, or raise."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        return ""
    _reject_control_characters(candidate, field)
    if len(candidate) > max_length:
        raise CallerInputError(field, f"must be at most {max_length} characters")
    if not RENDER_TEXT_RE.match(candidate):
        raise CallerInputError(
            field,
            "may contain only letters, digits, spaces and the punctuation " "- . / & ( ) ' , + #",
        )
    return candidate


def setting_name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        raise CallerInputError(field, "must not be empty")
    _reject_control_characters(candidate, field)
    if not SETTING_NAME_RE.match(candidate):
        raise CallerInputError(field, "must be a 1-41 character label of letters, digits, spaces, '-', '_' or '.'")
    return candidate


def finite_in_range(value: object, field: str, minimum: float, maximum: float) -> float:
    """Parse *value* as a finite number within [minimum, maximum], or raise.

    Rejects bools (``isinstance(True, int)`` is True in Python), values that do
    not parse as a number at all, NaN and +/-Infinity in both their float and
    their string spellings, and magnitudes outside the range.
    """
    if isinstance(value, bool):
        raise CallerInputError(field, "must be a number, not a boolean")
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if not candidate:
            raise CallerInputError(field, "must not be empty")
    elif isinstance(value, (int, float)):
        candidate = repr(value)
    else:
        raise CallerInputError(field, "must be a number")
    try:
        number = float(candidate)
    except (TypeError, ValueError):
        raise CallerInputError(field, "must be a number") from None
    # NaN and the infinities parse fine; every comparison against NaN is False,
    # so the range check below cannot be relied on to catch them.
    if number != number or number in (float("inf"), float("-inf")):
        raise CallerInputError(field, "must be a finite number")
    if not (minimum <= number <= maximum):
        raise CallerInputError(field, f"must be between {minimum} and {maximum}")
    return number


def budget_amount(value: object, field: str) -> str:
    """Validate a daily budget and return it in the platform's string form."""
    number = finite_in_range(value, field, MIN_BUDGET, MAX_BUDGET)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


def iso_date(value: object, field: str) -> str:
    """Validate an ISO ``YYYY-MM-DD`` date and return it unchanged."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        return ""
    if len(candidate) != MAX_DATE_LEN:
        raise CallerInputError(field, "must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(candidate)
    except ValueError:
        raise CallerInputError(field, "must be an ISO date (YYYY-MM-DD)") from None
    return candidate


TARGET_STATUSES = ("active", "paused")


def inert_target_status(value: object, field: str) -> str:
    """Validate a campaign target status against the closed set, or raise."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip().lower()
    if not candidate:
        return ""
    if candidate not in TARGET_STATUSES:
        raise CallerInputError(field, f"must be one of {', '.join(TARGET_STATUSES)}")
    return candidate


def bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    """Parse a deployment-supplied integer knob within bounds, or raise."""
    number = finite_in_range(value, field, float(minimum), float(maximum))
    if not float(number).is_integer():
        raise CallerInputError(field, "must be a whole number")
    return int(number)
