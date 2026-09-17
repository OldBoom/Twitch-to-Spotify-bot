"""Playback poller and pending-slot accounting tests."""

from __future__ import annotations

from typing import Any

import pytest

from playback import (
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    PlaybackMonitor,
    PlaybackState,
)


class FakeSpotify:
    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        self._payloads = payloads
        self.calls = 0

    async def get_current_playback(self) -> dict[str, Any] | None:
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return payload


class RecordingSink:
    def __init__(self, *, skip: bool = False) -> None:
        self.seen: list[str | None] = []
        self._skip = skip

    def notice_now_playing(self, track_id: str | None) -> bool:
        self.seen.append(track_id)
        return self._skip


class SkippingSpotify(FakeSpotify):
    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        super().__init__(payloads)
        self.skips = 0

    async def skip_to_next(self) -> None:
        self.skips += 1


def _playing(
    track_id: str,
    *,
    duration_ms: int | None = None,
    progress_ms: int | None = None,
    is_playing: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {"id": track_id}
    if duration_ms is not None:
        item["duration_ms"] = duration_ms
    payload: dict[str, Any] = {
        "device": {"id": "dev1"},
        "is_playing": is_playing,
        "item": item,
    }
    if progress_ms is not None:
        payload["progress_ms"] = progress_ms
    return payload


def test_state_expires_after_ttl() -> None:
    state = PlaybackState(ttl=30.0)
    state.update(has_active_device=True, track_id="t1", now=100.0)
    assert state.snapshot(now=120.0) is not None
    assert state.snapshot(now=200.0) is None


def test_state_starts_empty() -> None:
    assert PlaybackState().snapshot(now=1.0) is None


@pytest.mark.asyncio
async def test_poll_detects_no_active_device() -> None:
    state = PlaybackState()
    monitor = PlaybackMonitor(FakeSpotify([None]), RecordingSink(), state)  # type: ignore[arg-type]
    snapshot = await monitor.poll_once(now=10.0)
    assert snapshot.has_active_device is False
    assert snapshot.track_id is None


@pytest.mark.asyncio
async def test_poll_reports_track_change_once() -> None:
    sink = RecordingSink()
    spotify = FakeSpotify([_playing("t1"), _playing("t1"), _playing("t2")])
    monitor = PlaybackMonitor(spotify, sink, PlaybackState())  # type: ignore[arg-type]
    for tick in range(3):
        await monitor.poll_once(now=float(tick))
    assert sink.seen == ["t1", "t2"]


@pytest.mark.asyncio
async def test_poll_is_scheduled_for_the_track_boundary() -> None:
    """Near the end of a track, the next poll lands just after it finishes."""
    spotify = FakeSpotify([_playing("t1", duration_ms=200_000, progress_ms=196_500)])
    monitor = PlaybackMonitor(spotify, RecordingSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert monitor.next_interval == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_long_remaining_time_is_capped() -> None:
    """A manual skip mid-song must not go unnoticed for minutes."""
    spotify = FakeSpotify([_playing("t1", duration_ms=400_000, progress_ms=0)])
    monitor = PlaybackMonitor(spotify, RecordingSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert monitor.next_interval == MAX_POLL_INTERVAL


@pytest.mark.asyncio
async def test_overrun_does_not_busy_loop() -> None:
    """Waking slightly early or late must not poll faster than the floor."""
    spotify = FakeSpotify([_playing("t1", duration_ms=200_000, progress_ms=200_000)])
    monitor = PlaybackMonitor(spotify, RecordingSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert monitor.next_interval == MIN_POLL_INTERVAL


@pytest.mark.asyncio
async def test_paused_playback_uses_the_slow_interval() -> None:
    spotify = FakeSpotify(
        [_playing("t1", duration_ms=200_000, progress_ms=1_000, is_playing=False)]
    )
    monitor = PlaybackMonitor(spotify, RecordingSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert monitor.next_interval == MAX_POLL_INTERVAL


@pytest.mark.asyncio
async def test_missing_duration_falls_back_to_slow_interval() -> None:
    spotify = FakeSpotify([_playing("t1")])
    monitor = PlaybackMonitor(spotify, RecordingSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert monitor.next_interval == MAX_POLL_INTERVAL


@pytest.mark.asyncio
async def test_no_device_uses_the_slow_interval() -> None:
    monitor = PlaybackMonitor(FakeSpotify([None]), RecordingSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert monitor.next_interval == MAX_POLL_INTERVAL


@pytest.mark.asyncio
async def test_after_a_skip_the_next_poll_is_prompt() -> None:
    """The replacement track starts immediately, so re-check without waiting."""
    spotify = SkippingSpotify([_playing("t1", duration_ms=400_000, progress_ms=0)])
    sink = RecordingSink(skip=True)
    monitor = PlaybackMonitor(spotify, sink, PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)
    assert spotify.skips == 1
    assert monitor.next_interval == MIN_POLL_INTERVAL
