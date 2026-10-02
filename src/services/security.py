"""Input sanitizing and prompt-injection screening for the campaign agent.

Pure, stateless domain helpers (not framework gate methods). Two separate jobs
that must not be confused with each other:

* :func:`sanitize_query` STRIPS markup and caps length. Stripping is not
  refusal. It removes ``<...>`` runs, which includes chat-template control
  tokens such as ``<|im_start|>`` — so a directive wrapped in one comes out the
  other side as ordinary text with the marker gone, converting a detectable
  attack into an undetectable one.
* :func:`screen_for_injection` REFUSES. It runs on the raw text (where the
  control tokens are still visible) and again on the sanitized text (where a
  directive spliced with markup — ``ig<b>nore all previous instructions`` —
  has been re-assembled into a matchable phrase). Neither pass alone sees both.

The screen reports marker CLASS names, never the matched text: a caller-visible
error that quotes the payload back is a second delivery route for it.

Patterns are anchored on their own structure so ordinary campaign wording is
unaffected — a request may legitimately say "ignore duplicates", "system
settings" or "override the daily cap", and none of those are refused.
"""

from __future__ import annotations

import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# Chat-template control tokens, screened as a CLASS rather than as a list of
# known strings: any <|...|> delimiter, the Llama-style [INST] / [/INST]
# instruction wrapper, and the <<SYS>> system block.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|>]{0,64}\|>" r"|<</?SYS>>" r"|\[/?INST\]" r"|<\|(?:im_start|im_end|endoftext)\|>",
    re.IGNORECASE,
)

# Directive phrasing that only makes sense as an attempt to re-instruct the
# model. Each alternative requires its full shape, so a bare verb ("ignore",
# "override") in normal campaign wording does not match.
_DIRECTIVE_RE = re.compile(
    r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instructions?|rules?|prompts?|directions?)\b"
    r"|\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions?|rules?|prompts?|directions?)\b"
    r"|\byou\s+are\s+now\s+(?:a|an|the)\b"
    r"|\b(?:reveal|print|show|repeat|output)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+|initial\s+|original\s+)?prompt\b"
    r"|\bsystem\s+prompt\s*[:=]"
    r"|\boverride\s+(?:your|the|all)\s+(?:system\s+)?(?:instructions?|rules?|safety|guardrails?)\b"
    r"|\bact\s+as\s+(?:a|an|the)\s+(?:developer|admin|administrator|root|system)\b",
    re.IGNORECASE,
)

_SCREENS = (
    ("control_token", _CONTROL_TOKEN_RE),
    ("directive_override", _DIRECTIVE_RE),
)


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip markup runs and cap length.

    Screening happens separately — see the module docstring for why stripping
    alone would make a token attack harder to detect rather than easier.
    """
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


def screen_for_injection(text: str) -> list[str]:
    """Return the marker classes found in *text* (empty when it is clean).

    Never returns the matched substring.
    """
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for marker, pattern in _SCREENS:
        if pattern.search(text) and marker not in found:
            found.append(marker)
    return found


def screen_raw_and_sanitized(raw: str, sanitized: str) -> list[str]:
    """Screen both representations of the same caller text.

    The raw pass sees control tokens before the markup strip deletes them; the
    sanitized pass sees directives that were only matchable once the strip
    removed the markup splicing them apart.
    """
    markers = screen_for_injection(raw)
    for marker in screen_for_injection(sanitized):
        if marker not in markers:
            markers.append(marker)
    return markers


def screen_structure(value: object, path: str = "") -> list[str]:
    """Depth-first screen of a parsed caller structure, KEYS included.

    Runs after JSON parsing, so ``\\u``-escaped payloads have already been
    decoded into the characters they encode; a scan of the raw request bytes
    would not see them. Returns ``["<class> at <path>"]`` entries whose path is
    built only from key names that are themselves clean — an unrecognised or
    hostile key name is reported by position, never echoed.
    """
    findings: list[str] = []
    if isinstance(value, str):
        for marker in screen_for_injection(value):
            findings.append(f"{marker} in {path or 'value'}")
        return findings
    if isinstance(value, dict):
        for index, (key, nested) in enumerate(value.items()):
            key_markers = screen_for_injection(key) if isinstance(key, str) else []
            safe_key = f"field #{index + 1}" if key_markers else str(key)
            child = f"{path}.{safe_key}" if path else safe_key
            for marker in key_markers:
                findings.append(f"{marker} in the name of {child}")
            findings.extend(screen_structure(nested, child))
        return findings
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            findings.extend(screen_structure(item, child))
    return findings
