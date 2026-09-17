"""Playback state cache and poller.

Spotify emits no track-end event, so a poller drives both the active-device
pre-check and pending-request accounting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from spotify.client import SpotifyError

if TYPE_CHECKING:
    from spotify.client import SpotifyClient

LOGGER = logging.getLogger("playback")

# Spotify publishes no playback events, so the only way to notice a track
# change is to ask. The current track's own remaining time tells us when the
# next change is due, so polls are scheduled to land on that boundary instead
# of running at a fixed rate.
MIN_POLL_INTERVAL = 1.0
MAX_POLL_INTERVAL = 15.0
# Wake just after the track should have ended, absorbing request latency.
TRACK_END_MARGIN = 0.5
# Give up on a bot-initiated change appearing after this long.
AWAIT_CHANGE_TIMEOUT = 10.0

DEFAULT_STATE_TTL = 30.0


class TrackChangeSink(Protocol):
    def notice_now_playing(self, track_id: str | None) -> bool:
        """Returns True when the track was removed and should be skipped."""
        ...


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    has_active_device: bool
    track_id: str | None
    updated_at: float


class PlaybackState:
    """Last known playback state. Reads return None once the data is stale."""

    def __init__(self, *, ttl: float = DEFAULT_STATE_TTL) -> None:
        self._ttl = ttl
        self._snapshot: PlaybackSnapshot | None = None

    def update(
        self,
        *,
        has_active_device: bool,
        track_id: str | None,
        now: float | None = None,
    ) -> PlaybackSnapshot:
        clock = time.monotonic() if now is None else now
        self._snapshot = PlaybackSnapshot(
            has_active_device=has_active_device,
            track_id=track_id,
            updated_at=clock,
        )
        return self._snapshot

    def snapshot(self, now: float | None = None) -> PlaybackSnapshot | None:
        if self._snapshot is None:
            return None
        clock = time.monotonic() if now is None else now
        if clock - self._snapshot.updated_at > self._ttl:
            return None
        return self._snapshot

    def invalidate(self) -> None:
        self._snapshot = None


class PlaybackMonitor:
    def __init__(
        self,
        spotify: SpotifyClient,
        sink: TrackChangeSink,
        state: PlaybackState,
        *,
        min_interval: float = MIN_POLL_INTERVAL,
        max_interval: float = MAX_POLL_INTERVAL,
    ) -> None:
        self._spotify = spotify
        self._sink = sink
        self._state = state
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._next_interval = max_interval
        self._last_track_id: str | None = None
        self._wake = asyncio.Event()
        # Deadline for expecting a bot-initiated change to show up.
        self._awaiting_until = 0.0

    @property
    def next_interval(self) -> float:
        """Seconds to wait before the next poll, set by the last poll."""
        return self._next_interval

    def wake(self, *, now: float | None = None) -> None:
        """Poll again now, because the bot just changed playback itself.

        Spotify may not report the new track instantly, so polls stay at the
        minimum interval until the change appears or the deadline passes.
        """
        clock = time.monotonic() if now is None else now
        self._awaiting_until = clock + AWAIT_CHANGE_TIMEOUT
        self._wake.set()

    async def poll_once(self, *, now: float | None = None) -> PlaybackSnapshot:
        data: dict[str, Any] | None = await self._spotify.get_current_playback()
        if data is None:
            self._last_track_id = None
            self._next_interval = self._max_interval
            return self._state.update(has_active_device=False, track_id=None, now=now)

        has_device = bool(data.get("device"))
        item = data.get("item") or {}
        track_id = item.get("id") if isinstance(item, dict) else None

        self._next_interval = self._interval_for(data)

        if track_id != self._last_track_id:
            self._last_track_id = track_id
            self._awaiting_until = 0.0
            if self._sink.notice_now_playing(track_id):
                await self._skip_removed(track_id)
                # The replacement track starts right away; look again promptly.
                self._next_interval = self._min_interval
        elif self._awaiting_change(now):
            self._next_interval = self._min_interval

        return self._state.update(has_active_device=has_device, track_id=track_id, now=now)

    def _awaiting_change(self, now: float | None = None) -> bool:
        clock = time.monotonic() if now is None else now
        return clock < self._awaiting_until

    def _interval_for(self, data: dict[str, Any]) -> float:
        """Time until the current track is due to end, clamped to the bounds.

        The upper bound keeps manual skips and device changes from going
        unnoticed for a whole song; the lower bound stops tight polling loops.
        """
        if not data.get("is_playing"):
            return self._max_interval

        item = data.get("item") or {}
        duration = item.get("duration_ms") if isinstance(item, dict) else None
        progress = data.get("progress_ms")
        if not isinstance(duration, int) or not isinstance(progress, int):
            return self._max_interval
        if duration <= 0:
            return self._max_interval

        remaining = (duration - progress) / 1000.0 + TRACK_END_MARGIN
        return max(self._min_interval, min(self._max_interval, remaining))

    async def _skip_removed(self, track_id: str | None) -> None:
        """Spotify cannot un-queue, so a removed request is skipped on arrival."""
        LOGGER.info("Skipping removed request %s", track_id)
        try:
            await self._spotify.skip_to_next()
        except SpotifyError as exc:
            LOGGER.warning("Could not skip removed request: %s", exc)

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except SpotifyError as exc:
                LOGGER.warning("Playback poll failed: %s", exc)
                self._state.invalidate()
                self._next_interval = self._max_interval
            except Exception:
                LOGGER.exception("Unexpected playback poll failure")
                self._state.invalidate()
                self._next_interval = self._max_interval
            await self._sleep_or_wake(self._next_interval)

    async def _sleep_or_wake(self, timeout: float) -> None:
        """Wait out the interval, returning early if `wake` is called."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            return
        self._wake.clear()
