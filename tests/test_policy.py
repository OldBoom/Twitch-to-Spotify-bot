"""Tests for I/O-free policy gate."""

from requests_service import PolicyConfig, RejectReason, UserState, evaluate_policy
from spotify.client import Track

TRACK = Track(
    id="track1",
    name="Song",
    artists=("Artist",),
    artist_ids=("artist1",),
    duration_ms=200_000,
    uri="spotify:track:track1",
)


def test_allows_first_request() -> None:
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=False,
        track=TRACK,
        now=100.0,
        user_state=UserState(),
        config=PolicyConfig(),
    )
    assert decision.allowed


def test_cooldown_rejects() -> None:
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=False,
        track=TRACK,
        now=130.0,
        user_state=UserState(last_request_at=100.0, pending_count=1),
        config=PolicyConfig(cooldown_seconds=60),
    )
    assert not decision.allowed
    assert decision.reason == RejectReason.COOLDOWN
    assert decision.retry_after == 30.0


def test_privileged_bypasses_cooldown_and_cap() -> None:
    decision = evaluate_policy(
        user_id="mod",
        is_privileged=True,
        track=TRACK,
        now=110.0,
        user_state=UserState(last_request_at=100.0, pending_count=99),
        config=PolicyConfig(cooldown_seconds=60, max_pending_per_user=2),
    )
    assert decision.allowed


def test_pending_cap() -> None:
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=False,
        track=TRACK,
        now=1000.0,
        user_state=UserState(last_request_at=0.0, pending_count=2),
        config=PolicyConfig(max_pending_per_user=2),
    )
    assert not decision.allowed
    assert decision.reason == RejectReason.PENDING_CAP


def test_zero_pending_cap_is_unlimited() -> None:
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=False,
        track=TRACK,
        now=1000.0,
        user_state=UserState(last_request_at=0.0, pending_count=99),
        config=PolicyConfig(max_pending_per_user=0, cooldown_seconds=0),
    )
    assert decision.allowed


def test_too_long() -> None:
    long_track = Track(
        id="long",
        name="Epic",
        artists=("A",),
        artist_ids=("a1",),
        duration_ms=600_000,
        uri="spotify:track:long",
    )
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=False,
        track=long_track,
        now=0.0,
        user_state=UserState(),
        config=PolicyConfig(max_track_duration_seconds=480),
    )
    assert decision.reason == RejectReason.TOO_LONG


def test_blocked_track() -> None:
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=True,
        track=TRACK,
        now=0.0,
        user_state=None,
        config=PolicyConfig(blocked_track_ids=frozenset({"track1"})),
    )
    assert decision.reason == RejectReason.BLOCKED_TRACK


def test_blocked_artist() -> None:
    decision = evaluate_policy(
        user_id="u1",
        is_privileged=True,
        track=TRACK,
        now=0.0,
        user_state=None,
        config=PolicyConfig(blocked_artist_ids=frozenset({"artist1"})),
    )
    assert decision.reason == RejectReason.BLOCKED_ARTIST
