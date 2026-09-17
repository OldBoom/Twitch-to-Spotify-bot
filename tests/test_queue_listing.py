"""!queue lists the bot's tracked requests, not Spotify's context tracks."""

import httpx
import pytest
import respx

from requests_service import QUEUE_PREVIEW, PolicyConfig, SongRequestService
from spotify.client import API_BASE, SpotifyClient


def _track_json(track_id: str, name: str) -> dict[str, object]:
    return {
        "id": track_id,
        "name": name,
        "artists": [{"id": "artist1", "name": "Artist"}],
        "duration_ms": 200_000,
        "uri": f"spotify:track:{track_id}",
    }


@pytest.fixture
def spotify(tmp_path) -> SpotifyClient:
    path = tmp_path / ".tokens.json"
    path.write_text(
        '{"access_token": "a", "refresh_token": "r", "expires_at": 9999999999}',
        encoding="utf-8",
    )
    return SpotifyClient(client_id="cid", client_secret="csecret", tokens_path=path)


async def _queue(service: SongRequestService, track_id: str, name: str, who: str) -> None:
    respx.get(f"{API_BASE}/tracks/{track_id}").mock(
        return_value=httpx.Response(200, json=_track_json(track_id, name))
    )
    result = await service.handle_request(
        query=f"spotify:track:{track_id}",
        user_id=who,
        is_privileged=True,
        requester=who,
    )
    assert result.ok, result.message


def test_empty_queue_message(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    assert service.queue_message() == "No song requests queued."


@respx.mock
@pytest.mark.asyncio
async def test_queue_is_numbered_with_requester(spotify: SpotifyClient) -> None:
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    service = SongRequestService(spotify, PolicyConfig())

    await _queue(service, "aaaaaaaaaaaaaaaaaaaaaa", "First", "alice")
    await _queue(service, "bbbbbbbbbbbbbbbbbbbbbb", "Second", "bob")

    message = service.queue_message()
    assert message == (
        "Requests (2): 1) First — Artist (alice) | 2) Second — Artist (bob)"
    )
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_long_queue_is_summarised(spotify: SpotifyClient) -> None:
    """Replies must stay inside Twitch's 500-character limit."""
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    service = SongRequestService(spotify, PolicyConfig())

    total = QUEUE_PREVIEW + 3
    for index in range(total):
        await _queue(service, f"{index:022d}", f"Song {index}", f"user{index}")

    message = service.queue_message()
    assert message.startswith(f"Requests ({total}): 1) Song 0")
    assert message.endswith(f"+{total - QUEUE_PREVIEW} more")
    assert f"{QUEUE_PREVIEW + 1}) Song {QUEUE_PREVIEW}" not in message
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_played_requests_leave_the_queue(spotify: SpotifyClient) -> None:
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    service = SongRequestService(spotify, PolicyConfig())

    await _queue(service, "aaaaaaaaaaaaaaaaaaaaaa", "First", "alice")
    await _queue(service, "bbbbbbbbbbbbbbbbbbbbbb", "Second", "bob")

    service.notice_now_playing("aaaaaaaaaaaaaaaaaaaaaa")
    assert service.queue_message() == "Requests (1): 1) Second — Artist (bob)"
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_requester_falls_back_to_user_id(spotify: SpotifyClient) -> None:
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    respx.get(f"{API_BASE}/tracks/aaaaaaaaaaaaaaaaaaaaaa").mock(
        return_value=httpx.Response(200, json=_track_json("aaaaaaaaaaaaaaaaaaaaaa", "First"))
    )
    service = SongRequestService(spotify, PolicyConfig())
    await service.handle_request(
        query="spotify:track:aaaaaaaaaaaaaaaaaaaaaa",
        user_id="12345",
        is_privileged=True,
    )
    assert "(12345)" in service.queue_message()
    await spotify.aclose()
