"""Pending-slot release driven by playback, plus device-availability handling."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from requests_service import PolicyConfig, RejectReason, SongRequestService
from spotify.client import API_BASE, SpotifyClient

TRACK_A = "4iV5W9uYEdYUVa79Axb7Rh"
TRACK_B = "1301WleyT98MSxVHPZCA6M"


def _track_json(track_id: str, name: str) -> dict[str, object]:
    return {
        "id": track_id,
        "name": name,
        "artists": [{"id": "artist1", "name": "Artist"}],
        "duration_ms": 200_000,
        "uri": f"spotify:track:{track_id}",
    }


@pytest.fixture()
def spotify(tmp_path: Path):
    path = tmp_path / ".tokens.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": 9_999_999_999,
            }
        ),
        encoding="utf-8",
    )
    return SpotifyClient(client_id="cid", client_secret="csecret", tokens_path=path)


@respx.mock
@pytest.mark.asyncio
async def test_pending_cap_then_released_when_track_plays(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/tracks/{TRACK_A}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_A, "A"))
    )
    respx.get(f"{API_BASE}/tracks/{TRACK_B}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_B, "B"))
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(204))

    service = SongRequestService(spotify, PolicyConfig(cooldown_seconds=0, max_pending_per_user=1))

    first = await service.handle_request(
        query=f"spotify:track:{TRACK_A}", user_id="v1", is_privileged=False, now=0.0
    )
    assert first.ok

    blocked = await service.handle_request(
        query=f"spotify:track:{TRACK_B}", user_id="v1", is_privileged=False, now=1.0
    )
    assert blocked.reason == RejectReason.PENDING_CAP

    # The requested track reaches the decks; the slot frees immediately.
    service.notice_now_playing(TRACK_A)

    allowed = await service.handle_request(
        query=f"spotify:track:{TRACK_B}", user_id="v1", is_privileged=False, now=2.0
    )
    assert allowed.ok
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_skipped_track_also_releases_slot(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/tracks/{TRACK_A}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_A, "A"))
    )
    respx.get(f"{API_BASE}/tracks/{TRACK_B}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_B, "B"))
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(204))

    service = SongRequestService(spotify, PolicyConfig(cooldown_seconds=0, max_pending_per_user=2))
    await service.handle_request(
        query=f"spotify:track:{TRACK_A}", user_id="v1", is_privileged=False, now=0.0
    )
    await service.handle_request(
        query=f"spotify:track:{TRACK_B}", user_id="v1", is_privileged=False, now=1.0
    )

    # A skipped straight to B: both slots must release.
    service.notice_now_playing(TRACK_B)
    assert service._users["v1"].pending_count == 0
    await spotify.aclose()


def test_unknown_now_playing_is_ignored(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    service.notice_now_playing("someUnrelatedTrack")
    service.notice_now_playing(None)


@respx.mock
@pytest.mark.asyncio
async def test_idle_player_report_does_not_block_requests(spotify: SpotifyClient) -> None:
    """`GET /me/player` says 204 even while music plays, so it must not gate !sr.

    Regression: viewers were told "no active device" while playback was running.
    """
    player = respx.get(f"{API_BASE}/me/player").mock(return_value=httpx.Response(204))
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(
            200, json={"tracks": {"items": [_track_json(TRACK_A, "Some Song")]}}
        )
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(204))

    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query="some song", user_id="v1", is_privileged=False, now=105.0
    )
    assert result.ok, result.message
    assert not player.called
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_paused_playback_still_accepts_requests(spotify: SpotifyClient) -> None:
    """Queueing onto a paused-but-active device is allowed by Spotify."""
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(
            200, json={"tracks": {"items": [_track_json(TRACK_A, "Some Song")]}}
        )
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(204))

    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query="some song", user_id="v1", is_privileged=False, now=1.0
    )
    assert result.ok, result.message
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_spotify_is_the_authority_on_no_device(spotify: SpotifyClient) -> None:
    """A genuine refusal from Spotify is still reported to chat."""
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(
            200, json={"tracks": {"items": [_track_json(TRACK_A, "Some Song")]}}
        )
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(
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
    result = await service.handle_request(
        query="some song", user_id="v1", is_privileged=False, now=1.0
    )
    assert result.reason == RejectReason.NO_ACTIVE_DEVICE
    assert "press play once" in result.message
    await spotify.aclose()
