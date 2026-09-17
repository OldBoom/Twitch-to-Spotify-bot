"""Spotify client and song-request service HTTP tests with respx mocks."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from requests_service import PolicyConfig, RejectReason, SongRequestService
from spotify.auth import TokenFileError, load_tokens
from spotify.client import (
    API_BASE,
    MAX_RETRY_AFTER,
    TOKEN_URL,
    SpotifyClient,
    SpotifyError,
    SpotifyNoActiveDevice,
    SpotifyPremiumRequired,
    SpotifyRateLimited,
)

TRACK_ID = "4iV5W9uYEdYUVa79Axb7Rh"
TRACK_JSON = {
    "id": TRACK_ID,
    "name": "Never Gonna Give You Up",
    "artists": [{"id": "artist1", "name": "Rick Astley"}],
    "duration_ms": 213000,
    "uri": f"spotify:track:{TRACK_ID}",
}


@pytest.fixture()
def tokens_path(tmp_path: Path) -> Path:
    path = tmp_path / ".tokens.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "access-old",
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "expires_in": 3600,
                # Far-future expiry so tests exercise the reactive 401 path,
                # not the proactive refresh.
                "expires_at": time.time() + 3600,
                "scope": "user-modify-playback-state user-read-playback-state",
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
async def spotify(tokens_path: Path) -> AsyncIterator[SpotifyClient]:
    client = SpotifyClient(
        client_id="cid",
        client_secret="csecret",
        tokens_path=tokens_path,
    )
    yield client
    await client.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_search_tracks_empty(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": []}})
    )
    assert await spotify.search_tracks("nothing") == []


@respx.mock
@pytest.mark.asyncio
async def test_search_tracks_hit(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [TRACK_JSON]}})
    )
    tracks = await spotify.search_tracks("never")
    assert [t.id for t in tracks] == [TRACK_ID]


@respx.mock
@pytest.mark.asyncio
async def test_search_tracks_skips_null_items(spotify: SpotifyClient) -> None:
    """Spotify occasionally pads results with nulls."""
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [None, TRACK_JSON]}})
    )
    tracks = await spotify.search_tracks("never")
    assert [t.id for t in tracks] == [TRACK_ID]


@respx.mock
@pytest.mark.asyncio
async def test_add_to_queue_success(spotify: SpotifyClient) -> None:
    route = respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(204)
    )
    await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_add_to_queue_accepts_200_with_body(spotify: SpotifyClient) -> None:
    """Spotify documents 204 but really answers 200 plus an opaque body."""
    route = respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(200, text="JoLlNbhbPv1hWeA-mDbByNhVU7U")
    )
    await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_opaque_error_body_is_not_echoed(spotify: SpotifyClient) -> None:
    """Unparseable error bodies must not leak into the chat-facing message."""
    # Exhaust transient retries so the final 500 surfaces.
    respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(500, text="<html>secret internals</html>")
    )
    with pytest.raises(SpotifyError) as exc:
        await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert str(exc.value) == "HTTP 500"
    assert "secret" not in str(exc.value)


@respx.mock
@pytest.mark.asyncio
async def test_transient_5xx_is_retried(spotify: SpotifyClient) -> None:
    """Spotify's brief outages should not force the viewer to retype !sr."""
    route = respx.post(f"{API_BASE}/me/player/queue").mock(
        side_effect=[
            httpx.Response(
                503,
                json={
                    "error": {
                        "status": 503,
                        "message": "An unexpected error occurred. Please try again later.",
                    }
                },
            ),
            httpx.Response(204),
        ]
    )
    await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_persistent_5xx_still_fails(spotify: SpotifyClient) -> None:
    route = respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(
            503,
            json={
                "error": {
                    "status": 503,
                    "message": "An unexpected error occurred. Please try again later.",
                }
            },
        )
    )
    with pytest.raises(SpotifyError) as exc:
        await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert "unexpected error" in str(exc.value).casefold()
    assert route.call_count == 1 + 2  # initial + MAX_TRANSIENT_RETRIES


@respx.mock
@pytest.mark.asyncio
async def test_service_queues_when_spotify_returns_200(spotify: SpotifyClient) -> None:
    """The 200 response used to surface as a false 'Spotify rejected' reply."""
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [TRACK_JSON]}})
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(200, text="JoLlNbhbPv1hWeA-mDbByNhVU7U")
    )
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query="never gonna give you up",
        user_id="viewer1",
        is_privileged=False,
        now=100.0,
    )
    assert result.ok
    assert "Queued:" in result.message


@respx.mock
@pytest.mark.asyncio
async def test_add_to_queue_premium_required(spotify: SpotifyClient) -> None:
    respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "status": 403,
                    "message": "Premium required",
                    "reason": "PREMIUM_REQUIRED",
                }
            },
        )
    )
    with pytest.raises(SpotifyPremiumRequired):
        await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")


@respx.mock
@pytest.mark.asyncio
async def test_add_to_queue_no_active_device(spotify: SpotifyClient) -> None:
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
    with pytest.raises(SpotifyNoActiveDevice):
        await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")


@respx.mock
@pytest.mark.asyncio
async def test_401_refreshes_and_retries(spotify: SpotifyClient, tokens_path: Path) -> None:
    queue = respx.post(f"{API_BASE}/me/player/queue")
    queue.side_effect = [
        httpx.Response(401, json={"error": {"status": 401, "message": "expired"}}),
        httpx.Response(204),
    ]
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-new",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    )
    await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert queue.call_count == 2
    saved = json.loads(tokens_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "access-new"
    assert saved["refresh_token"] == "refresh-2"


@respx.mock
@pytest.mark.asyncio
async def test_429_raises_after_retry(spotify: SpotifyClient) -> None:
    respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
    )
    with pytest.raises(SpotifyRateLimited) as exc:
        await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert exc.value.retry_after == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_429_does_not_sleep_beyond_cap(spotify: SpotifyClient) -> None:
    """A huge Retry-After must fail fast instead of hanging the command."""
    huge = MAX_RETRY_AFTER + 3600
    route = respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(429, headers={"Retry-After": str(huge)}, text="slow")
    )
    started = time.monotonic()
    with pytest.raises(SpotifyRateLimited) as exc:
        await spotify.add_to_queue(f"spotify:track:{TRACK_ID}")
    assert exc.value.retry_after == huge
    assert time.monotonic() - started < 1.0
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_expired_token_refreshes_proactively(tmp_path: Path) -> None:
    """An already-expired stored token must refresh before the first call, not 401."""
    path = tmp_path / ".tokens.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": "refresh-1",
                "expires_at": time.time() - 10,
            }
        ),
        encoding="utf-8",
    )
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fresh", "expires_in": 3600, "token_type": "Bearer"},
        )
    )
    queue_route = respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(204)
    )
    client = SpotifyClient(client_id="cid", client_secret="csecret", tokens_path=path)
    try:
        await client.add_to_queue(f"spotify:track:{TRACK_ID}")
    finally:
        await client.aclose()

    assert token_route.call_count == 1
    assert queue_route.call_count == 1
    assert queue_route.calls[0].request.headers["authorization"] == "Bearer fresh"


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_401s_refresh_once(spotify: SpotifyClient, tokens_path: Path) -> None:
    """Two parallel 401s must not race into two refreshes or corrupt the token file.

    A barrier holds both requests in flight until each has received a 401, so
    the refresh path is genuinely contended rather than incidentally serialized.
    """
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    )

    barrier = asyncio.Barrier(2)

    async def queue_responder(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer access-old":
            await barrier.wait()
            return httpx.Response(401, json={"error": {"status": 401, "message": "expired"}})
        return httpx.Response(204)

    queue_route = respx.post(f"{API_BASE}/me/player/queue").mock(side_effect=queue_responder)

    await asyncio.gather(
        spotify.add_to_queue(f"spotify:track:{TRACK_ID}"),
        spotify.add_to_queue(f"spotify:track:{TRACK_ID}"),
    )

    # 4 = two contended 401s plus two successful retries.
    assert queue_route.call_count == 4
    assert token_route.call_count == 1
    saved = load_tokens(tokens_path)
    assert saved["access_token"] == "fresh"
    assert saved["refresh_token"] == "refresh-2"


def test_malformed_token_file_raises_friendly_error(tmp_path: Path) -> None:
    path = tmp_path / ".tokens.json"
    path.write_text('{"access_token": ""}', encoding="utf-8")
    with pytest.raises(TokenFileError, match="refresh_token"):
        SpotifyClient(client_id="cid", client_secret="csecret", tokens_path=path)


def test_missing_token_file_raises_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(TokenFileError, match="spotify.auth"):
        load_tokens(tmp_path / "absent.json")


@respx.mock
@pytest.mark.asyncio
async def test_service_queues_by_name(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [TRACK_JSON]}})
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(204))
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query="never gonna",
        user_id="viewer1",
        is_privileged=False,
        now=100.0,
    )
    assert result.ok
    assert "Queued:" in result.message


@respx.mock
@pytest.mark.asyncio
async def test_service_maps_premium_error(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/tracks/{TRACK_ID}").mock(
        return_value=httpx.Response(200, json=TRACK_JSON)
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "status": 403,
                    "message": "Premium required",
                    "reason": "PREMIUM_REQUIRED",
                }
            },
        )
    )
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query=f"spotify:track:{TRACK_ID}",
        user_id="viewer1",
        is_privileged=True,
        now=1.0,
    )
    assert not result.ok
    assert result.reason == RejectReason.PREMIUM_REQUIRED
    assert "Premium" in result.message


@respx.mock
@pytest.mark.asyncio
async def test_service_maps_no_device(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/tracks/{TRACK_ID}").mock(
        return_value=httpx.Response(200, json=TRACK_JSON)
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
        query=f"https://open.spotify.com/track/{TRACK_ID}?si=1",
        user_id="viewer1",
        is_privileged=True,
        now=1.0,
    )
    assert result.reason == RejectReason.NO_ACTIVE_DEVICE


@respx.mock
@pytest.mark.asyncio
async def test_service_not_found(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": []}})
    )
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query="zzzzunknownsong",
        user_id="viewer1",
        is_privileged=False,
        now=1.0,
    )
    assert result.reason == RejectReason.NOT_FOUND
    assert "No track found" in result.message


@respx.mock
@pytest.mark.asyncio
async def test_service_maps_rate_limit(spotify: SpotifyClient) -> None:
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [TRACK_JSON]}})
    )
    respx.post(f"{API_BASE}/me/player/queue").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
    )
    service = SongRequestService(spotify, PolicyConfig())
    result = await service.handle_request(
        query="never",
        user_id="viewer1",
        is_privileged=True,
        now=1.0,
    )
    assert result.reason == RejectReason.RATE_LIMITED
    assert "rate limited" in result.message
