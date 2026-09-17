"""Async Spotify Web API client."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from spotify.auth import TOKEN_EXPIRY_MARGIN, load_tokens, save_tokens

LOGGER = logging.getLogger("spotify.client")

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CONNECT_TIMEOUT = 3.0
REQUEST_TIMEOUT = 10.0

# Never block a chat command longer than this waiting out a 429.
MAX_RETRY_AFTER = 10.0

# Spotify occasionally answers 5xx with "An unexpected error occurred";
# one or two short retries usually succeed (as viewers already see by retyping).
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
MAX_TRANSIENT_RETRIES = 2
TRANSIENT_BACKOFF = 0.6

# Candidates fetched per search so results can be re-ranked locally.
# Spotify rejects search limits above 10 with "Invalid limit".
SEARCH_LIMIT = 10


class SpotifyError(Exception):
    """Base Spotify client error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class SpotifyAuthError(SpotifyError):
    pass


class SpotifyPremiumRequired(SpotifyError):
    pass


class SpotifyNoActiveDevice(SpotifyError):
    pass


class SpotifyRateLimited(SpotifyError):
    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message, status=429, reason="RATE_LIMITED")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Track:
    id: str
    name: str
    artists: tuple[str, ...]
    artist_ids: tuple[str, ...]
    duration_ms: int
    uri: str

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def display(self) -> str:
        artists = ", ".join(self.artists) if self.artists else "Unknown"
        return f"{self.name} — {artists}"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Track:
        artists = data.get("artists") or []
        return cls(
            id=data["id"],
            name=data.get("name") or "Unknown",
            artists=tuple(a.get("name") or "Unknown" for a in artists),
            artist_ids=tuple(a["id"] for a in artists if a.get("id")),
            duration_ms=int(data.get("duration_ms") or 0),
            uri=data.get("uri") or f"spotify:track:{data['id']}",
        )


@dataclass(frozen=True, slots=True)
class NowPlaying:
    name: str
    performers: tuple[str, ...]
    duration_ms: int
    progress_ms: int
    is_playing: bool
    url: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> NowPlaying | None:
        item = data.get("item")
        if not isinstance(item, dict) or not item.get("name"):
            return None

        artists = item.get("artists") or []
        performers = tuple(a["name"] for a in artists if isinstance(a, dict) and a.get("name"))
        if not performers:
            # Podcast episodes carry a `show` instead of `artists`.
            show = item.get("show")
            if isinstance(show, dict) and show.get("name"):
                performers = (show["name"],)

        external = item.get("external_urls")
        url = external.get("spotify") if isinstance(external, dict) else None

        return cls(
            name=str(item["name"]),
            performers=performers,
            duration_ms=int(item.get("duration_ms") or 0),
            progress_ms=int(data.get("progress_ms") or 0),
            is_playing=bool(data.get("is_playing")),
            url=url,
        )


class SpotifyClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        tokens_path: Path,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens_path = tokens_path
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
        )
        tokens = load_tokens(tokens_path)
        self._access_token: str = tokens["access_token"]
        self._refresh_token: str = tokens["refresh_token"]
        self._expires_at: float = float(tokens.get("expires_at") or 0.0)
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> SpotifyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def refresh_access_token(self) -> None:
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        response = await self._http.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
        )
        if response.status_code != 200:
            raise SpotifyAuthError(
                f"Token refresh failed: {response.status_code} {response.text}",
                status=response.status_code,
            )
        payload = response.json()
        self._access_token = payload["access_token"]
        if payload.get("refresh_token"):
            self._refresh_token = payload["refresh_token"]
        expires_in = float(payload.get("expires_in") or 3600)
        self._expires_at = time.time() + expires_in - TOKEN_EXPIRY_MARGIN
        save_tokens(
            self._tokens_path,
            {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "token_type": payload.get("token_type", "Bearer"),
                "expires_in": payload.get("expires_in"),
                "expires_at": self._expires_at,
                "scope": payload.get("scope"),
            },
        )
        LOGGER.info("Spotify access token refreshed")

    async def _ensure_fresh_token(self) -> None:
        """Refresh proactively when the stored token is at or past its expiry."""
        if time.time() < self._expires_at:
            return
        async with self._refresh_lock:
            if time.time() < self._expires_at:
                return
            await self.refresh_access_token()

    async def _refresh_stale_token(self, seen_token: str) -> None:
        """Refresh after a 401, unless a concurrent caller already replaced the token."""
        async with self._refresh_lock:
            if self._access_token != seen_token:
                return
            await self.refresh_access_token()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        _retried: bool = False,
        _rate_retried: bool = False,
        _transient_retries: int = 0,
    ) -> httpx.Response:
        url = f"{API_BASE}{path}"
        await self._ensure_fresh_token()
        sent_token = self._access_token
        response = await self._http.request(
            method,
            url,
            params=params,
            headers=self._auth_header(),
        )

        if response.status_code == 401 and not _retried:
            await self._refresh_stale_token(sent_token)
            return await self._request(
                method,
                path,
                params=params,
                _retried=True,
                _rate_retried=_rate_retried,
                _transient_retries=_transient_retries,
            )

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After") or "1")
            if not _rate_retried and retry_after <= MAX_RETRY_AFTER:
                LOGGER.warning("Spotify rate limited; sleeping %.1fs", retry_after)
                await asyncio.sleep(retry_after)
                return await self._request(
                    method,
                    path,
                    params=params,
                    _retried=_retried,
                    _rate_retried=True,
                    _transient_retries=_transient_retries,
                )
            raise SpotifyRateLimited(
                "Spotify rate limited",
                retry_after=retry_after,
            )

        if (
            response.status_code in TRANSIENT_STATUSES
            and _transient_retries < MAX_TRANSIENT_RETRIES
        ):
            delay = TRANSIENT_BACKOFF * (_transient_retries + 1)
            LOGGER.warning(
                "Spotify transient %s on %s %s; retry %s/%s after %.1fs",
                response.status_code,
                method,
                path,
                _transient_retries + 1,
                MAX_TRANSIENT_RETRIES,
                delay,
            )
            await asyncio.sleep(delay)
            return await self._request(
                method,
                path,
                params=params,
                _retried=_retried,
                _rate_retried=_rate_retried,
                _transient_retries=_transient_retries + 1,
            )

        return response

    @staticmethod
    def _raise_player_error(response: httpx.Response) -> None:
        reason: str | None = None
        message: str | None = None
        try:
            err = response.json().get("error") or {}
            reason = err.get("reason")
            message = err.get("message")
        except Exception:  # noqa: BLE001
            pass

        if not message:
            # Bodies may be HTML or opaque text; log them instead of echoing to chat.
            LOGGER.warning(
                "Unparsed Spotify error body (status %s): %r",
                response.status_code,
                response.text[:200],
            )
            message = f"HTTP {response.status_code}"

        if response.status_code == 403 and reason == "PREMIUM_REQUIRED":
            raise SpotifyPremiumRequired(message, status=403, reason=reason)
        if response.status_code == 404 and reason == "NO_ACTIVE_DEVICE":
            raise SpotifyNoActiveDevice(message, status=404, reason=reason)
        if response.status_code == 401:
            raise SpotifyAuthError(message, status=401, reason=reason)
        raise SpotifyError(message, status=response.status_code, reason=reason)

    async def search_tracks(self, query: str, *, limit: int = SEARCH_LIMIT) -> list[Track]:
        """Candidate tracks, in Spotify's own ranking order."""
        response = await self._request(
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": limit},
        )
        if response.status_code == 401:
            raise SpotifyAuthError(response.text, status=401)
        if response.status_code >= 400:
            self._raise_player_error(response)

        items = ((response.json().get("tracks") or {}).get("items")) or []
        return [Track.from_api(item) for item in items if item and item.get("id")]

    async def get_track(self, track_id: str) -> Track | None:
        response = await self._request("GET", f"/tracks/{track_id}")
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise SpotifyAuthError(response.text, status=401)
        if response.status_code >= 400:
            self._raise_player_error(response)
        return Track.from_api(response.json())

    async def get_current_playback(self) -> dict[str, Any] | None:
        response = await self._request("GET", "/me/player")
        if response.status_code == 204:
            return None
        if response.status_code == 401:
            raise SpotifyAuthError(response.text, status=401)
        if response.status_code >= 400:
            self._raise_player_error(response)
        if not response.content:
            return None
        return response.json()

    async def get_now_playing(self) -> NowPlaying | None:
        """Currently playing track/episode, or None when nothing is active."""
        data = await self.get_current_playback()
        if data is None:
            return None
        return NowPlaying.from_api(data)

    async def skip_to_next(self) -> None:
        """Advance playback, used to drop a request removed after queueing."""
        response = await self._request("POST", "/me/player/next")
        if response.is_success:
            return
        if response.status_code == 401:
            raise SpotifyAuthError(response.text, status=401)
        self._raise_player_error(response)

    async def add_to_queue(self, uri: str, *, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        response = await self._request("POST", "/me/player/queue", params=params)
        # Documented as 204, but Spotify also answers 200 with a body.
        if response.is_success:
            return
        if response.status_code == 401:
            raise SpotifyAuthError(response.text, status=401)
        self._raise_player_error(response)
