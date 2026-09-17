"""Chat message sanitization regression tests.

Twitch rejects messages over 500 characters (TwitchIO raises ValueError) and
treats a leading `/` or `.` as a chat command run as the bot account.
"""

from __future__ import annotations

import chat_format
from requests_service import RejectReason, map_error_to_message


def test_long_query_reply_stays_within_twitch_limit() -> None:
    reply = map_error_to_message(RejectReason.NOT_FOUND, query="A" * 480)
    assert len(chat_format.sanitize(reply)) <= chat_format.MAX_CHAT_MESSAGE


def test_long_spotify_detail_stays_within_twitch_limit() -> None:
    reply = map_error_to_message(RejectReason.SPOTIFY_ERROR, detail="X" * 900)
    assert len(chat_format.sanitize(reply)) <= chat_format.MAX_CHAT_MESSAGE


def test_spotify_detail_cannot_lead_the_message() -> None:
    reply = map_error_to_message(RejectReason.SPOTIFY_ERROR, detail="/ban someviewer")
    assert reply.startswith("Spotify rejected the request:")
    assert not chat_format.sanitize(reply).startswith("/")


def test_sanitize_defuses_command_prefixes() -> None:
    for raw in ("/ban viewer", ".timeout viewer"):
        assert not chat_format.sanitize(raw).startswith(raw[0])


def test_sanitize_collapses_newlines() -> None:
    assert chat_format.sanitize("line one\r\nline two\ttab") == "line one line two tab"


def test_sanitize_truncates_with_ellipsis() -> None:
    out = chat_format.sanitize("B" * 900)
    assert len(out) == chat_format.MAX_CHAT_MESSAGE
    assert out.endswith("\u2026")


def test_sanitize_empty_falls_back() -> None:
    assert chat_format.sanitize("   \n  ") == "Request failed."


def test_echo_shortens_user_text() -> None:
    assert len(chat_format.echo("C" * 500)) == chat_format.MAX_ECHO_LENGTH
