"""!srskip: advance playback and let the poller catch up immediately."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from playback import AWAIT_CHANGE_TIMEOUT, MIN_POLL_INTERVAL, PlaybackMonitor, PlaybackState
from requests_service import (
    PolicyConfig,
    RejectReason,
    SongRequestService,
    map_error_to_message,
)
from spotify.client import API_BASE, SpotifyClient


@pytest.fixture
def spotify(tmp_path) -> SpotifyClient:
    path = tmp_path / ".tokens.json"
    path.write_text(
        '{"access_token": "a", "refresh_token": "r", "expires_at": 9999999999}',
        encoding="utf-8",
    )
    return SpotifyClient(client_id="cid", client_secret="csecret", tokens_path=path)


@respx.mock
@pytest.mark.asyncio
async def test_skip_calls_spotify(spotify: SpotifyClient) -> None:
    route = respx.post(f"{API_BASE}/me/player/next").mock(
        return_value=httpx.Response(204)
    )
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.skip_current()
    assert result.ok
    assert result.message == "Skipped."
    assert route.call_count == 1
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_skip_without_device_is_reported(spotify: SpotifyClient) -> None:
    respx.post(f"{API_BASE}/me/player/next").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "status": 404,
                    "message": "No active device",
                    "reason": "NO_ACTIVE_DEVICE",
                }
            },
        )
    )
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.skip_current()
    assert not result.ok
    assert result.message == map_error_to_message(RejectReason.NO_ACTIVE_DEVICE)
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_skip_clears_the_now_playing_cache(spotify: SpotifyClient) -> None:
    """A stale !np reply after skipping would show the wrong track."""
    respx.post(f"{API_BASE}/me/player/next").mock(return_value=httpx.Response(204))
    player = respx.get(f"{API_BASE}/me/player")
    player.side_effect = [
        httpx.Response(
            200,
            json={
                "device": {"id": "d1"},
                "is_playing": True,
                "progress_ms": 1000,
                "item": {"id": "t1", "name": "Old", "duration_ms": 200_000, "artists": []},
            },
        ),
        httpx.Response(
            200,
            json={
                "device": {"id": "d1"},
                "is_playing": True,
                "progress_ms": 0,
                "item": {"id": "t2", "name": "New", "duration_ms": 200_000, "artists": []},
            },
        ),
    ]

    service = SongRequestService(spotify, PolicyConfig())
    first = await service.now_playing_message(now=100.0)
    assert "Old" in first

    await service.skip_current()
    # Same clock: without invalidation the 5s cache would still say "Old".
    second = await service.now_playing_message(now=100.0)
    assert "New" in second
    await spotify.aclose()


class FakeSpotify:
    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        self._payloads = payloads
        self.calls = 0

    async def get_current_playback(self) -> dict[str, Any] | None:
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return payload


class QuietSink:
    def notice_now_playing(self, track_id: str | None) -> bool:
        return False


def _playing(track_id: str, *, progress_ms: int = 0) -> dict[str, Any]:
    return {
        "device": {"id": "dev1"},
        "is_playing": True,
        "progress_ms": progress_ms,
        "item": {"id": track_id, "duration_ms": 400_000},
    }


@pytest.mark.asyncio
async def test_wake_keeps_polling_until_the_change_appears() -> None:
    """Spotify can still report the old track right after a skip."""
    spotify = FakeSpotify([_playing("t1")])
    monitor = PlaybackMonitor(spotify, QuietSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)

    monitor.wake(now=0.0)
    await monitor.poll_once(now=0.5)
    assert monitor.next_interval == MIN_POLL_INTERVAL


@pytest.mark.asyncio
async def test_wake_gives_up_after_the_deadline() -> None:
    """A failed skip must not pin the poller at the fast interval forever."""
    spotify = FakeSpotify([_playing("t1")])
    monitor = PlaybackMonitor(spotify, QuietSink(), PlaybackState())  # type: ignore[arg-type]
    await monitor.poll_once(now=0.0)

    monitor.wake(now=0.0)
    await monitor.poll_once(now=AWAIT_CHANGE_TIMEOUT + 1.0)
    assert monitor.next_interval > MIN_POLL_INTERVAL


@pytest.mark.asyncio
async def test_wake_interrupts_the_sleep() -> None:
    spotify = FakeSpotify([_playing("t1")])
    monitor = PlaybackMonitor(spotify, QuietSink(), PlaybackState())  # type: ignore[arg-type]
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.sleep(0.05)
        polls_before = spotify.calls
        monitor.wake()
        await asyncio.sleep(0.05)
        assert spotify.calls > polls_before
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
