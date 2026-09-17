"""Tests for error-to-chat-reply mapping."""

from requests_service import RejectReason, map_error_to_message


def test_premium_required() -> None:
    assert (
        map_error_to_message(RejectReason.PREMIUM_REQUIRED)
        == "Spotify account is not Premium; requests disabled."
    )


def test_no_active_device() -> None:
    assert (
        map_error_to_message(RejectReason.NO_ACTIVE_DEVICE)
        == "Spotify has no active device; ask the streamer to press play once."
    )


def test_auth_failed() -> None:
    assert "authentication failed" in map_error_to_message(RejectReason.AUTH_FAILED).lower()


def test_rate_limited() -> None:
    assert map_error_to_message(RejectReason.RATE_LIMITED, retry_after=5.2) == (
        "rate limited, try again in 5s"
    )


def test_not_found() -> None:
    assert map_error_to_message(RejectReason.NOT_FOUND, query="abc") == (
        "No track found for `abc`."
    )


def test_empty_query() -> None:
    assert "Usage" in map_error_to_message(RejectReason.EMPTY_QUERY)
