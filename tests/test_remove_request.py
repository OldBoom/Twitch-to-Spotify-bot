"""!srremove: bookkeeping plus the auto-skip that stands in for un-queueing."""

import httpx
import pytest
import respx

from playback import PlaybackMonitor, PlaybackState
from requests_service import PolicyConfig, SongRequestService
from spotify.client import API_BASE, SpotifyClient

TRACK_A = "aaaaaaaaaaaaaaaaaaaaaa"
TRACK_B = "bbbbbbbbbbbbbbbbbbbbbb"


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


async def _seed(service: SongRequestService) -> None:
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    for track_id, name, who in (
        (TRACK_A, "Дурак и молния", "alice"),
        (TRACK_B, "Never Gonna Give You Up", "bob"),
    ):
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


@respx.mock
@pytest.mark.asyncio
async def test_remove_by_number(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    result = service.remove_request("1", user_id="alice", is_privileged=True)
    assert result.ok
    assert "Дурак и молния" in result.message
    assert "Never Gonna" in service.queue_message()
    assert "Дурак" not in service.queue_message()
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_remove_by_name_tolerates_typo(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    result = service.remove_request("дурак и морния", user_id="alice", is_privileged=True)
    assert result.ok
    assert "Дурак" not in service.queue_message()
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_out_of_range_number_is_rejected(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    result = service.remove_request("9", user_id="alice", is_privileged=True)
    assert not result.ok
    assert "No queued request matches" in result.message
    assert "Requests (2)" in service.queue_message()
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_viewer_cannot_remove_someone_elses_request(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    result = service.remove_request("1", user_id="bob", is_privileged=False)
    assert not result.ok
    assert "only remove your own" in result.message
    assert "Requests (2)" in service.queue_message()
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_viewer_can_remove_own_request(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    result = service.remove_request("2", user_id="bob", is_privileged=False)
    assert result.ok
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_removal_frees_the_requester_slot(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig(max_pending_per_user=1))
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    respx.get(f"{API_BASE}/tracks/{TRACK_A}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_A, "First"))
    )
    await service.handle_request(
        query=f"spotify:track:{TRACK_A}",
        user_id="alice",
        is_privileged=False,
        requester="alice",
        now=1.0,
    )
    assert service.remove_request("1", user_id="alice", is_privileged=False).ok

    respx.get(f"{API_BASE}/tracks/{TRACK_B}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_B, "Second"))
    )
    again = await service.handle_request(
        query=f"spotify:track:{TRACK_B}",
        user_id="alice",
        is_privileged=False,
        requester="alice",
        now=1000.0,
    )
    assert again.ok, again.message
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_removed_track_is_skipped_when_it_starts(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)
    assert service.remove_request("1", user_id="alice", is_privileged=True).ok

    respx.get(f"{API_BASE}/me/player").mock(
        return_value=httpx.Response(
            200, json={"device": {"id": "d1"}, "item": {"id": TRACK_A}}
        )
    )
    skip = respx.post(f"{API_BASE}/me/player/next").mock(
        return_value=httpx.Response(200)
    )

    state = PlaybackState()
    monitor = PlaybackMonitor(spotify, service, state)
    await monitor.poll_once(now=10.0)

    assert skip.call_count == 1
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_kept_track_is_not_skipped(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    respx.get(f"{API_BASE}/me/player").mock(
        return_value=httpx.Response(
            200, json={"device": {"id": "d1"}, "item": {"id": TRACK_A}}
        )
    )
    skip = respx.post(f"{API_BASE}/me/player/next")

    monitor = PlaybackMonitor(spotify, service, PlaybackState())
    await monitor.poll_once(now=10.0)

    assert not skip.called
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_skip_applies_once_only(spotify: SpotifyClient) -> None:
    """A later legitimate request for the same track must still play."""
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)
    assert service.remove_request("1", user_id="alice", is_privileged=True).ok

    assert service.notice_now_playing(TRACK_A) is True
    assert service.notice_now_playing(TRACK_A) is False
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_clear_all_empties_the_queue(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)

    result = service.clear_all_requests()
    assert result.ok
    assert "Cleared 2 request(s)" in result.message
    assert service.queue_message() == "No song requests queued."
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_clear_all_marks_tracks_for_skipping(spotify: SpotifyClient) -> None:
    """Spotify keeps them queued, so a clean slate means skipping them."""
    service = SongRequestService(spotify, PolicyConfig())
    await _seed(service)
    service.clear_all_requests()

    assert service.notice_now_playing(TRACK_A) is True
    assert service.notice_now_playing(TRACK_B) is True
    await spotify.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_clear_all_frees_every_slot(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig(max_pending_per_user=1))
    respx.post(f"{API_BASE}/me/player/queue").mock(return_value=httpx.Response(200))
    respx.get(f"{API_BASE}/tracks/{TRACK_A}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_A, "First"))
    )
    await service.handle_request(
        query=f"spotify:track:{TRACK_A}",
        user_id="alice",
        is_privileged=False,
        requester="alice",
        now=1.0,
    )
    service.clear_all_requests()

    respx.get(f"{API_BASE}/tracks/{TRACK_B}").mock(
        return_value=httpx.Response(200, json=_track_json(TRACK_B, "Second"))
    )
    again = await service.handle_request(
        query=f"spotify:track:{TRACK_B}",
        user_id="alice",
        is_privileged=False,
        requester="alice",
        now=1000.0,
    )
    assert again.ok, again.message
    await spotify.aclose()


def test_clear_all_on_empty_queue(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    result = service.clear_all_requests()
    assert not result.ok
    assert result.message == "No song requests queued."


def test_usage_and_empty_queue_messages(spotify: SpotifyClient) -> None:
    service = SongRequestService(spotify, PolicyConfig())
    assert "Usage:" in service.remove_request("", user_id="a", is_privileged=True).message
    assert (
        service.remove_request("1", user_id="a", is_privileged=True).message
        == "No song requests queued."
    )
