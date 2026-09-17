"""Now-playing command tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from requests_service import PolicyConfig, SongRequestService, format_now_playing
from spotify.client import API_BASE, NowPlaying, SpotifyClient

TRACK_PAYLOAD = {
    "is_playing": True,
    "progress_ms": 83_000,
    "device": {"id": "dev1"},
    "item": {
        "id": "4iV5W9uYEdYUVa79Axb7Rh",
        "name": "Never Gonna Give You Up",
        "duration_ms": 213_000,
        "artists": [{"id": "a1", "name": "Rick Astley"}],
        "external_urls": {"spotify": "https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh"},
    },
}


@pytest.fixture()
def spotify(tmp_path: Path) -> SpotifyClient:
    path = tmp_path / ".tokens.json"
    path.write_text(
        json.dumps(
            {"access_token": "a", "refresh_token": "r", "expires_at": 9_999_999_999}
        ),
        encoding="utf-8",
    )
    return SpotifyClient(client_id="cid", client_secret="csecret", tokens_path=path)


def test_format_nothing_playing() -> None:
    assert format_now_playing(None) == "Nothing is playing right now."


def test_format_playing_with_position() -> None:
    message = format_now_playing(
        NowPlaying(
            name="Song",
            performers=("A", "B"),
            duration_ms=213_000,
            progress_ms=83_000,
            is_playing=True,
            url=None,
        )
    )
    assert message == "Now playing: Song — A, B [1:23 / 3:33]"


def test_format_paused() -> None:
    message = format_now_playing(
        NowPlaying(
            name="Song",
            performers=("A",),
            duration_ms=60_000,
            progress_ms=0,
            is_playing=False,
            url=None,
        )
    )
    assert message.startswith("Paused: Song — A [0:00 / 1:00]")


def test_podcast_episode_uses_show_name() -> None:
    parsed = NowPlaying.from_api(
        {
            "is_playing": True,
            "progress_ms": 0,
            "item": {
                "id": "ep1",
                "name": "Episode 5",
                "duration_ms": 1_800_000,
                "show": {"name": "Some Podcast"},
            },
        }
    )
    assert parsed is not None
    assert parsed.performers == ("Some Podcast",)


def test_advert_without_item_is_none() -> None:
    assert NowPlaying.from_api({"is_playing": True, "item": None}) is None


@respx.mock
@pytest.mark.asyncio
async def test_now_playing_message_includes_url(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/me/player").mock(return_value=httpx.Response(200, json=TRACK_PAYLOAD))
    service = SongRequestService(spotify, PolicyConfig())
    message = await service.now_playing_message(now=1.0)
    assert "Now playing: Never Gonna Give You Up — Rick Astley [1:23 / 3:33]" in message
    assert message.endswith("https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh")
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_now_playing_reports_idle_device(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/me/player").mock(return_value=httpx.Response(204))
    service = SongRequestService(spotify, PolicyConfig())
    assert await service.now_playing_message(now=1.0) == "Nothing is playing right now."
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_now_playing_is_cached_against_spam(spotify: SpotifyClient) -> None:
    route = respx.get(f"{API_BASE}/me/player").mock(
        return_value=httpx.Response(200, json=TRACK_PAYLOAD)
    )
    service = SongRequestService(spotify, PolicyConfig())

    for tick in (100.0, 101.0, 102.0, 104.9):
        await service.now_playing_message(now=tick)
    assert route.call_count == 1

    # Past the cache window the API is consulted again.
    await service.now_playing_message(now=110.0)
    assert route.call_count == 2
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_now_playing_maps_rate_limit(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/me/player").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
    )
    service = SongRequestService(spotify, PolicyConfig())
    assert "rate limited" in await service.now_playing_message(now=1.0)
    await spotify.aclose()
