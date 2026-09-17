"""Entrypoint for the Spotify-aware Twitch song-request bot."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import twitchio
from pydantic import ValidationError

from app_paths import data_dir, ensure_data_cwd
from config import Settings, get_settings
from playback import PlaybackMonitor, PlaybackState
from rate_limit import ChatRateLimiter
from requests_service import PolicyConfig, SongRequestService, load_blocklist
from spotify.auth import TokenFileError
from spotify.client import SpotifyClient
from twitch.bot import SongRequestBot

LOGGER = logging.getLogger("main")


def _resolve_under_data(path: Path) -> Path:
    """Relative paths in settings live next to ``.env`` / tokens."""
    return path if path.is_absolute() else data_dir() / path


def _build_policy(settings: Settings) -> PolicyConfig:
    blocked_tracks, blocked_artists = load_blocklist(
        _resolve_under_data(settings.blocklist_path)
    )
    return PolicyConfig(
        cooldown_seconds=settings.sr_cooldown_seconds,
        max_pending_per_user=settings.sr_max_pending_per_user,
        max_track_duration_seconds=settings.sr_max_track_duration_seconds,
        blocked_track_ids=blocked_tracks,
        blocked_artist_ids=blocked_artists,
    )


async def run_bot(settings: Settings) -> None:
    spotify = SpotifyClient(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        tokens_path=_resolve_under_data(Path(settings.spotify_tokens_path)),
    )
    playback = PlaybackState()
    service = SongRequestService(spotify, _build_policy(settings))
    monitor = PlaybackMonitor(spotify, service, playback)
    rate_limiter = ChatRateLimiter(
        max_messages=settings.chat_rate_limit_messages,
        window_seconds=float(settings.chat_rate_limit_window_seconds),
    )

    bot = SongRequestBot(
        settings=settings,
        spotify=spotify,
        service=service,
        rate_limiter=rate_limiter,
        monitor=monitor,
    )
    try:
        async with bot:
            await bot.start()
    finally:
        await spotify.aclose()


def main() -> None:
    # Windowed PyInstaller builds null sys.stdout/stderr; reconnect them so
    # logging reaches the parent control app when launched as ``--bot``.
    if getattr(sys, "frozen", False):
        if sys.stdout is None:
            sys.stdout = open(1, "w", encoding="utf-8", errors="replace", closefd=False)
        if sys.stderr is None:
            sys.stderr = open(2, "w", encoding="utf-8", errors="replace", closefd=False)

    ensure_data_cwd()
    twitchio.utils.setup_logging(level=logging.INFO)
    try:
        settings = get_settings()
    except ValidationError as exc:
        LOGGER.error("Configuration invalid (fail-fast):\n%s", exc)
        sys.exit(1)

    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        LOGGER.warning("Shutting down due to KeyboardInterrupt")
    except TokenFileError as exc:
        LOGGER.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
