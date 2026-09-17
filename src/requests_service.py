"""Song-request orchestration and I/O-free policy gate."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import chat_format
from matching import MIN_SCORE, artist_title_query, best_match, score_text
from parsing import extract_track_id, track_uri
from spotify.client import (
    NowPlaying,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyError,
    SpotifyNoActiveDevice,
    SpotifyPremiumRequired,
    SpotifyRateLimited,
    Track,
)

LOGGER = logging.getLogger("requests_service")

# Serve repeated !song calls from cache so viewers cannot spam the Spotify API.
NOW_PLAYING_CACHE_SECONDS = 5.0

# Entries listed by !queue before the reply is summarised, chosen to stay well
# inside Twitch's 500-character limit.
QUEUE_PREVIEW = 5


class RejectReason(StrEnum):
    EMPTY_QUERY = "empty_query"
    COOLDOWN = "cooldown"
    PENDING_CAP = "pending_cap"
    TOO_LONG = "too_long"
    BLOCKED_TRACK = "blocked_track"
    BLOCKED_ARTIST = "blocked_artist"
    NOT_FOUND = "not_found"
    PREMIUM_REQUIRED = "premium_required"
    NO_ACTIVE_DEVICE = "no_active_device"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    SPOTIFY_ERROR = "spotify_error"


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """A queued viewer request that has not started playing yet."""

    track_id: str
    display: str
    user_id: str
    requester: str


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    cooldown_seconds: int = 60
    # 0 = unlimited pending requests per user.
    max_pending_per_user: int = 0
    max_track_duration_seconds: int = 480
    blocked_track_ids: frozenset[str] = field(default_factory=frozenset)
    blocked_artist_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: RejectReason | None = None
    retry_after: float | None = None


@dataclass
class UserState:
    last_request_at: float = 0.0
    pending_count: int = 0


@dataclass(frozen=True, slots=True)
class RequestResult:
    ok: bool
    message: str
    track: Track | None = None
    reason: RejectReason | None = None


def load_blocklist(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    if not path.exists():
        return frozenset(), frozenset()
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    tracks = frozenset(str(x) for x in (data.get("track_ids") or []))
    artists = frozenset(str(x) for x in (data.get("artist_ids") or []))
    return tracks, artists


def evaluate_policy(
    *,
    user_id: str,
    is_privileged: bool,
    track: Track,
    now: float,
    user_state: UserState | None,
    config: PolicyConfig,
) -> GateDecision:
    """I/O-free policy gate. Privileged users (broadcaster/mods) bypass cooldown and caps."""
    if track.id in config.blocked_track_ids:
        return GateDecision(False, RejectReason.BLOCKED_TRACK)
    if any(aid in config.blocked_artist_ids for aid in track.artist_ids):
        return GateDecision(False, RejectReason.BLOCKED_ARTIST)
    if track.duration_seconds > config.max_track_duration_seconds:
        return GateDecision(False, RejectReason.TOO_LONG)

    if is_privileged:
        return GateDecision(True)

    state = user_state or UserState()
    if (
        config.max_pending_per_user > 0
        and state.pending_count >= config.max_pending_per_user
    ):
        return GateDecision(False, RejectReason.PENDING_CAP)

    elapsed = now - state.last_request_at
    if state.last_request_at > 0 and elapsed < config.cooldown_seconds:
        return GateDecision(
            False,
            RejectReason.COOLDOWN,
            retry_after=config.cooldown_seconds - elapsed,
        )

    return GateDecision(True)


def map_error_to_message(
    reason: RejectReason,
    *,
    query: str = "",
    track: Track | None = None,
    retry_after: float | None = None,
    detail: str | None = None,
) -> str:
    if reason == RejectReason.EMPTY_QUERY:
        return "Usage: !sr <song name | Spotify link>"
    if reason == RejectReason.NOT_FOUND:
        return f"No track found for `{chat_format.echo(query)}`."
    if reason == RejectReason.COOLDOWN:
        seconds = max(1, int(retry_after or 1))
        return f"Cooldown active, try again in {seconds}s."
    if reason == RejectReason.PENDING_CAP:
        return "You already have too many songs pending in the queue."
    if reason == RejectReason.TOO_LONG:
        return "That track is too long for song requests."
    if reason == RejectReason.BLOCKED_TRACK:
        return "That track is blocked."
    if reason == RejectReason.BLOCKED_ARTIST:
        return "That artist is blocked."
    if reason == RejectReason.PREMIUM_REQUIRED:
        return "Spotify account is not Premium; requests disabled."
    if reason == RejectReason.NO_ACTIVE_DEVICE:
        return "Spotify has no active device; ask the streamer to press play once."
    if reason == RejectReason.AUTH_FAILED:
        return "Spotify authentication failed; streamer needs to re-authorize."
    if reason == RejectReason.RATE_LIMITED:
        seconds = max(1, int(retry_after or 1))
        return f"rate limited, try again in {seconds}s"
    if reason == RejectReason.SPOTIFY_ERROR:
        # Static prefix: `detail` is remote text and MUST NOT lead the message.
        # 5xx bodies are Spotify's generic "try again later"; keep chat short.
        if detail and (
            detail.startswith("HTTP 5")
            or "unexpected error" in detail.casefold()
        ):
            return "Spotify had a temporary glitch, try !sr again."
        if detail:
            return f"Spotify rejected the request: {chat_format.echo(detail, limit=200)}"
        return "Spotify rejected the request."
    return "Request failed."


def _format_position(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds) // 1000
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def format_now_playing(now_playing: NowPlaying | None) -> str:
    if now_playing is None:
        return "Nothing is playing right now."

    who = ", ".join(now_playing.performers) if now_playing.performers else "Unknown"
    prefix = "Now playing" if now_playing.is_playing else "Paused"
    position = _format_position(now_playing.progress_ms)
    if now_playing.duration_ms > 0:
        position = f"{position} / {_format_position(now_playing.duration_ms)}"

    message = f"{prefix}: {now_playing.name} — {who} [{position}]"
    if now_playing.url:
        message = f"{message} {now_playing.url}"
    return message


class SongRequestService:
    def __init__(
        self,
        spotify: SpotifyClient,
        config: PolicyConfig,
    ) -> None:
        self._spotify = spotify
        self._config = config
        self._users: dict[str, UserState] = {}
        self._pending: deque[PendingRequest] = deque()
        self._now_playing_cache: tuple[float, NowPlaying | None] | None = None
        # Removed after queueing: Spotify cannot un-queue, so these are skipped
        # once playback reaches them.
        self._cancelled: set[str] = set()

    def _get_user(self, user_id: str) -> UserState:
        if user_id not in self._users:
            self._users[user_id] = UserState()
        return self._users[user_id]

    def _release(self, user_id: str) -> None:
        state = self._users.get(user_id)
        if state and state.pending_count > 0:
            state.pending_count -= 1

    def notice_now_playing(self, track_id: str | None) -> bool:
        """Release pending slots once a requested track reaches the decks.

        Called by `PlaybackMonitor` on every track change. Entries ahead of the
        match were skipped, so they are released too. Returns True when the
        track was removed via `remove_request` and should be skipped.
        """
        if track_id is None:
            return False

        index = next(
            (i for i, entry in enumerate(self._pending) if entry.track_id == track_id),
            None,
        )
        if index is not None:
            for _ in range(index + 1):
                entry = self._pending.popleft()
                self._release(entry.user_id)

        if track_id in self._cancelled:
            # One-shot: a later legitimate request for the same track must play.
            self._cancelled.discard(track_id)
            return True
        return False

    async def handle_request(
        self,
        *,
        query: str,
        user_id: str,
        is_privileged: bool,
        now: float | None = None,
        requester: str = "",
    ) -> RequestResult:
        q = query.strip()
        if not q:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.EMPTY_QUERY),
                reason=RejectReason.EMPTY_QUERY,
            )

        clock = time.monotonic() if now is None else now
        user_state = self._get_user(user_id)

        # No local device pre-check: `GET /me/player` reports 204 even while
        # music is playing (private session, reporting lag), so guessing here
        # rejected valid requests. Spotify decides, and paused-but-active
        # devices accept queueing normally.
        try:
            track = await self._resolve_track(q)
        except SpotifyAuthError:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.AUTH_FAILED),
                reason=RejectReason.AUTH_FAILED,
            )
        except SpotifyRateLimited as exc:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.RATE_LIMITED, retry_after=exc.retry_after),
                reason=RejectReason.RATE_LIMITED,
            )
        except SpotifyError as exc:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.SPOTIFY_ERROR, detail=str(exc)),
                reason=RejectReason.SPOTIFY_ERROR,
            )

        if track is None:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.NOT_FOUND, query=q),
                reason=RejectReason.NOT_FOUND,
            )

        decision = evaluate_policy(
            user_id=user_id,
            is_privileged=is_privileged,
            track=track,
            now=clock,
            user_state=user_state,
            config=self._config,
        )
        if not decision.allowed:
            assert decision.reason is not None
            return RequestResult(
                False,
                map_error_to_message(
                    decision.reason,
                    track=track,
                    retry_after=decision.retry_after,
                ),
                track=track,
                reason=decision.reason,
            )

        try:
            await self._spotify.add_to_queue(track_uri(track.id))
        except SpotifyPremiumRequired:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.PREMIUM_REQUIRED),
                track=track,
                reason=RejectReason.PREMIUM_REQUIRED,
            )
        except SpotifyNoActiveDevice:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.NO_ACTIVE_DEVICE),
                track=track,
                reason=RejectReason.NO_ACTIVE_DEVICE,
            )
        except SpotifyAuthError:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.AUTH_FAILED),
                track=track,
                reason=RejectReason.AUTH_FAILED,
            )
        except SpotifyRateLimited as exc:
            return RequestResult(
                False,
                map_error_to_message(RejectReason.RATE_LIMITED, retry_after=exc.retry_after),
                track=track,
                reason=RejectReason.RATE_LIMITED,
            )
        except SpotifyError as exc:
            LOGGER.exception("Spotify queue failed")
            return RequestResult(
                False,
                map_error_to_message(RejectReason.SPOTIFY_ERROR, detail=str(exc)),
                track=track,
                reason=RejectReason.SPOTIFY_ERROR,
            )

        user_state.last_request_at = clock
        user_state.pending_count += 1
        self._pending.append(
            PendingRequest(
                track_id=track.id,
                display=track.display,
                user_id=user_id,
                requester=requester or user_id,
            )
        )
        return RequestResult(
            True,
            f"Queued: {track.display}",
            track=track,
        )

    def _find_pending(self, selector: str) -> tuple[int, PendingRequest] | None:
        entries = list(self._pending)
        if not entries:
            return None

        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(entries):
                return index, entries[index]
            return None

        best: tuple[int, PendingRequest] | None = None
        best_score = MIN_SCORE
        for index, entry in enumerate(entries):
            current = score_text(selector, entry.display)
            if current >= best_score and (best is None or current > best_score):
                best_score = current
                best = (index, entry)
        return best

    def remove_request(
        self,
        selector: str,
        *,
        user_id: str,
        is_privileged: bool,
    ) -> ActionResult:
        """Drop a queued request by position or name.

        Spotify has no un-queue endpoint, so the track is also recorded for
        `PlaybackMonitor` to skip if playback reaches it.
        """
        wanted = selector.strip()
        if not wanted:
            return ActionResult(False, "Usage: !srremove <number|name>")
        if not self._pending:
            return ActionResult(False, "No song requests queued.")

        found = self._find_pending(wanted)
        if found is None:
            return ActionResult(
                False, f"No queued request matches `{chat_format.echo(wanted)}`."
            )

        index, entry = found
        if not is_privileged and entry.user_id != user_id:
            return ActionResult(False, "You can only remove your own requests.")

        del self._pending[index]
        self._release(entry.user_id)
        self._cancelled.add(entry.track_id)
        return ActionResult(True, f"Removed: {entry.display} (skipped if it comes up)")

    def clear_all_requests(self) -> ActionResult:
        """Drop every pending request, freeing each requester's slot.

        Cleared tracks are skipped on arrival, same as `remove_request`, since
        Spotify keeps them in its own queue.
        """
        if not self._pending:
            return ActionResult(False, "No song requests queued.")

        count = len(self._pending)
        for entry in self._pending:
            self._release(entry.user_id)
            self._cancelled.add(entry.track_id)
        self._pending.clear()
        return ActionResult(
            True, f"Cleared {count} request(s); they will be skipped if they come up."
        )

    def invalidate_now_playing(self) -> None:
        """Drop the !np cache after the bot itself changed playback."""
        self._now_playing_cache = None

    async def skip_current(self) -> ActionResult:
        """Advance playback on behalf of `!srskip`."""
        try:
            await self._spotify.skip_to_next()
        except SpotifyPremiumRequired:
            return ActionResult(False, map_error_to_message(RejectReason.PREMIUM_REQUIRED))
        except SpotifyNoActiveDevice:
            return ActionResult(False, map_error_to_message(RejectReason.NO_ACTIVE_DEVICE))
        except SpotifyAuthError:
            return ActionResult(False, map_error_to_message(RejectReason.AUTH_FAILED))
        except SpotifyRateLimited as exc:
            return ActionResult(
                False,
                map_error_to_message(RejectReason.RATE_LIMITED, retry_after=exc.retry_after),
            )
        except SpotifyError as exc:
            LOGGER.exception("Spotify skip failed")
            return ActionResult(
                False, map_error_to_message(RejectReason.SPOTIFY_ERROR, detail=str(exc))
            )

        self.invalidate_now_playing()
        return ActionResult(True, "Skipped.")

    def queue_message(self) -> str:
        """Numbered list of requests that have not started playing yet."""
        if not self._pending:
            return "No song requests queued."

        total = len(self._pending)
        shown = [
            f"{i}) {entry.display} ({entry.requester})"
            for i, entry in enumerate(list(self._pending)[:QUEUE_PREVIEW], start=1)
        ]
        message = f"Requests ({total}): " + " | ".join(shown)
        if total > QUEUE_PREVIEW:
            message = f"{message} | +{total - QUEUE_PREVIEW} more"
        return message

    async def _resolve_track(self, query: str) -> Track | None:
        track_id = extract_track_id(query)
        if track_id:
            return await self._spotify.get_track(track_id)

        candidates = await self._spotify.search_tracks(query)
        # An explicit `artist - title` request also gets a field-filtered pass,
        # which Spotify answers far more precisely than free text.
        fielded = artist_title_query(query)
        if fielded:
            candidates += await self._spotify.search_tracks(fielded)
        return best_match(query, candidates)

    async def now_playing_message(self, *, now: float | None = None) -> str:
        clock = time.monotonic() if now is None else now

        cached = self._now_playing_cache
        if cached is not None and clock - cached[0] < NOW_PLAYING_CACHE_SECONDS:
            return format_now_playing(cached[1])

        try:
            current = await self._spotify.get_now_playing()
        except SpotifyAuthError:
            return map_error_to_message(RejectReason.AUTH_FAILED)
        except SpotifyRateLimited as exc:
            return map_error_to_message(RejectReason.RATE_LIMITED, retry_after=exc.retry_after)
        except SpotifyError as exc:
            return map_error_to_message(RejectReason.SPOTIFY_ERROR, detail=str(exc))

        self._now_playing_cache = (clock, current)
        return format_now_playing(current)
