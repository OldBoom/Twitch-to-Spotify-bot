"""TwitchIO 3 song-request bot."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.parse import quote

import twitchio
from twitchio import eventsub
from twitchio.exceptions import HTTPException
from twitchio.ext import commands

import chat_format
from rate_limit import ChatRateLimiter
from requests_service import SongRequestService

if TYPE_CHECKING:
    from config import Settings
    from playback import PlaybackMonitor
    from spotify.client import SpotifyClient

LOGGER = logging.getLogger("twitch.bot")

BOT_SCOPES = "user:read:chat user:write:chat user:bot"
CHANNEL_SCOPES = "channel:bot"

# Remember recent outbound message ids to break command/reply loops.
SENT_HISTORY = 50

# Statuses Twitch returns for a chat subscription attempted before user consent.
PENDING_AUTH_STATUSES = frozenset({400, 401, 403})


def _display_name(chatter: object) -> str:
    name = getattr(chatter, "display_name", None) or getattr(chatter, "name", None)
    return str(name or "")


def _is_privileged(chatter: object) -> bool:
    broadcaster = bool(getattr(chatter, "broadcaster", False))
    moderator = bool(getattr(chatter, "moderator", False))
    lead_moderator = bool(getattr(chatter, "lead_moderator", False))
    return broadcaster or moderator or lead_moderator


def _oauth_url(scopes: str) -> str:
    return (
        "http://localhost:4343/oauth"
        f"?scopes={quote(scopes, safe='')}&force_verify=true"
    )


def should_process_own_message(
    *,
    bot_id: str,
    owner_id: str | None,
    message_id: str,
    text: str,
    prefix: str,
    sent_ids: Iterable[str],
) -> bool:
    """Whether a message authored by `bot_id` should still run commands.

    TwitchIO drops every message whose author is the bot. When the broadcaster
    runs the bot under their own account that would discard their own commands,
    so allow them through while skipping the bot's own outbound replies.
    """
    if owner_id is None or bot_id != owner_id:
        return False
    if message_id in sent_ids:
        return False
    return text.startswith(prefix)


class SongRequestBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        spotify: SpotifyClient,
        service: SongRequestService,
        rate_limiter: ChatRateLimiter,
        monitor: PlaybackMonitor,
    ) -> None:
        self.settings = settings
        self.spotify = spotify
        self.service = service
        self.rate_limiter = rate_limiter
        self.monitor = monitor
        self._monitor_task: asyncio.Task[None] | None = None
        self._chat_subscribed = False
        self._sent_ids: deque[str] = deque(maxlen=SENT_HISTORY)
        super().__init__(
            client_id=settings.twitch_client_id,
            client_secret=settings.twitch_client_secret,
            bot_id=settings.twitch_bot_id,
            owner_id=settings.twitch_owner_id,
            prefix=settings.command_prefix,
        )

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        self._monitor_task = asyncio.create_task(self.monitor.run(), name="playback-monitor")
        # event_ready logs the OAuth instructions; the adapter is not listening yet.
        await self._ensure_chat_subscription()

    async def _ensure_chat_subscription(self) -> bool:
        if self._chat_subscribed:
            return True
        payload = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self.owner_id,
            user_id=self.bot_id,
        )
        try:
            await self.subscribe_websocket(payload=payload)
        except HTTPException as exc:
            # 400 "invalid transport and auth combination" is what Twitch returns
            # when no user token is loaded yet and only the app token is available.
            if exc.status in PENDING_AUTH_STATUSES:
                LOGGER.warning(
                    "Chat subscription needs Twitch OAuth first (status %s).",
                    exc.status,
                )
                return False
            raise
        self._chat_subscribed = True
        LOGGER.info(
            "Subscribed to chat messages for owner_id=%s as bot_id=%s",
            self.owner_id,
            self.bot_id,
        )
        return True

    def _log_oauth_instructions(self) -> None:
        if self.settings.twitch_bot_id == self.settings.twitch_owner_id:
            # One account holds one token, so a second consent would replace the
            # first and drop its scopes. Grant everything in a single visit.
            LOGGER.warning(
                "Authorize Twitch while this process is running (single account, one visit):\n"
                "     %s",
                _oauth_url(f"{BOT_SCOPES} {CHANNEL_SCOPES}"),
            )
            return
        LOGGER.warning(
            "Authorize Twitch while this process is running:\n"
            "  1) Bot scopes (as the bot account):\n     %s\n"
            "  2) Channel bot scope (as the broadcaster):\n     %s",
            _oauth_url(BOT_SCOPES),
            _oauth_url(CHANNEL_SCOPES),
        )

    async def close(self, **options: object) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        await super().close(**options)  # type: ignore[arg-type]

    async def event_ready(self) -> None:
        LOGGER.info(
            "Logged in as bot_id=%s channel=%s",
            self.bot_id,
            self.settings.twitch_channel_login,
        )
        if not self._chat_subscribed:
            self._log_oauth_instructions()

    async def event_oauth_authorized(
        self,
        payload: twitchio.authentication.UserTokenPayload,
    ) -> None:
        await self.add_token(payload.access_token, payload.refresh_token)
        # TwitchIO only persists tokens on graceful shutdown; save now so a crash
        # or a killed process does not force the consent flow again.
        await self.save_tokens()
        LOGGER.info("Twitch OAuth authorized for user_id=%s", payload.user_id)
        if await self._ensure_chat_subscription():
            LOGGER.info("Chat subscription active — try !sr or !np in chat.")

    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        if payload.source_broadcaster is not None:
            return
        if payload.chatter.id == self.bot_id and not should_process_own_message(
            bot_id=self.bot_id,
            owner_id=self.settings.twitch_owner_id,
            message_id=payload.id,
            text=payload.text,
            prefix=self.settings.command_prefix,
            sent_ids=self._sent_ids,
        ):
            return
        await self.process_commands(payload)

    def help_message(self) -> str:
        p = self.settings.command_prefix
        return (
            f"Commands: {p}sr <name|link> request a song | "
            f"{p}np what is playing now | "
            f"{p}queue list queued requests | "
            f"{p}srremove <number|name> remove your request | "
            f"{p}srskip skip current track (mods) | "
            f"{p}srclearall drop all requests (mods) | "
            f"{p}srhelp this list"
        )

    async def send_rate_limited(self, ctx: commands.Context, message: str) -> None:
        """Twitch rejects >500 chars and treats a leading `/` as a command."""
        await self.rate_limiter.acquire()
        sent = await ctx.send(chat_format.sanitize(message))
        self._sent_ids.append(sent.id)

    async def event_command_error(
        self,
        payload: commands.CommandErrorPayload,
    ) -> None:
        LOGGER.exception(
            "Command %r failed",
            getattr(payload.context.command, "name", "unknown"),
            exc_info=payload.exception,
        )


class SongRequestComponent(commands.Component):
    def __init__(self, bot: SongRequestBot) -> None:
        self.bot = bot

    @commands.command(name="sr", aliases=["songrequest", "request"])
    async def song_request(self, ctx: commands.Context, *, query: str = "") -> None:
        """Queue a Spotify track: !sr <name | Spotify link>"""
        chatter = ctx.chatter
        user_id = str(getattr(chatter, "id", "") or "")
        privileged = _is_privileged(chatter)
        result = await self.bot.service.handle_request(
            query=query,
            user_id=user_id,
            is_privileged=privileged,
            requester=_display_name(chatter),
        )
        await self.bot.send_rate_limited(ctx, result.message)

    @commands.command(name="np", aliases=["nowplaying", "song", "current"])
    async def now_playing(self, ctx: commands.Context) -> None:
        """Show the currently playing track: !np"""
        message = await self.bot.service.now_playing_message()
        await self.bot.send_rate_limited(ctx, message)

    @commands.command(name="queue", aliases=["q", "srqueue", "songs"])
    async def show_queue(self, ctx: commands.Context) -> None:
        """List queued requests with numbers: !queue"""
        await self.bot.send_rate_limited(ctx, self.bot.service.queue_message())

    @commands.command(name="srremove", aliases=["srdel", "srcancel", "unrequest"])
    async def remove_request(self, ctx: commands.Context, *, selector: str = "") -> None:
        """Remove a queued request: !srremove <number|name>"""
        chatter = ctx.chatter
        result = self.bot.service.remove_request(
            selector,
            user_id=str(getattr(chatter, "id", "") or ""),
            is_privileged=_is_privileged(chatter),
        )
        await self.bot.send_rate_limited(ctx, result.message)

    @commands.command(name="srskip", aliases=["skip"])
    async def skip_song(self, ctx: commands.Context) -> None:
        """Skip the current track (streamer and mods): !srskip"""
        if not _is_privileged(ctx.chatter):
            await self.bot.send_rate_limited(
                ctx, "Only the streamer and mods can skip."
            )
            return
        result = await self.bot.service.skip_current()
        if result.ok:
            # Poll immediately so !np and pending slots reflect the new track.
            self.bot.monitor.wake()
        await self.bot.send_rate_limited(ctx, result.message)

    # Deliberately no aliases: this wipes the whole request queue.
    @commands.command(name="srclearall")
    async def clear_all_requests(self, ctx: commands.Context) -> None:
        """Drop every queued request (streamer and mods): !srclearall"""
        if not _is_privileged(ctx.chatter):
            await self.bot.send_rate_limited(
                ctx, "Only the streamer and mods can clear the queue."
            )
            return
        result = self.bot.service.clear_all_requests()
        await self.bot.send_rate_limited(ctx, result.message)

    @commands.command(name="srhelp", aliases=["commands", "srcommands"])
    async def help_command(self, ctx: commands.Context) -> None:
        """List the available commands: !srhelp"""
        await self.bot.send_rate_limited(ctx, self.bot.help_message())
