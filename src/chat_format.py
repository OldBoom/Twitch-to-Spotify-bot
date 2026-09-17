"""Outbound chat message safety.

Twitch rejects messages over 500 characters and interprets a leading `/` or `.`
as a chat command, so every outbound string MUST pass through `sanitize`.
"""

from __future__ import annotations

import re

MAX_CHAT_MESSAGE = 500
MAX_ECHO_LENGTH = 80

_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COMMAND_PREFIXES = ("/", ".")


def sanitize(text: str, *, limit: int = MAX_CHAT_MESSAGE) -> str:
    """Collapse whitespace, drop control chars, defuse command prefixes, truncate."""
    cleaned = _CONTROL_RE.sub(" ", text)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return "Request failed."
    if cleaned.startswith(_COMMAND_PREFIXES):
        cleaned = f"\u2060{cleaned}"
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "\u2026"
    return cleaned


def echo(text: str, *, limit: int = MAX_ECHO_LENGTH) -> str:
    """Shorten user-supplied text before embedding it in a reply."""
    cleaned = _WHITESPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "\u2026"
    return cleaned
