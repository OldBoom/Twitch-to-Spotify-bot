# Spotify Music Bot

Single-channel Twitch bot. Chat command `!sr <song name | Spotify link>` resolves a track and appends it to the end of the owner's Spotify Premium playback queue.

## Requirements

- Python 3.11+
- Spotify Premium account (queue API returns `403 PREMIUM_REQUIRED` on Free)
- Twitch developer application
- An **active** Spotify playback device when requesting songs (`404 NO_ACTIVE_DEVICE` otherwise)

## Install

```powershell
cd C:\Users\shava\MyProjects\SpotifyMusicBot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

Edit `.env` and fill every required field.

## User-only setup (Cursor cannot do these)

### 1. Spotify app

1. Open [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and create an app.
2. Copy **Client ID** and **Client Secret** into `.env` (`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`).
3. Add redirect URI exactly: `http://127.0.0.1:8888/callback` (Spotify rejects bare `localhost`).
4. In Development Mode, allowlist the Premium account that will own the queue (max 25 users).

### 2. Spotify OAuth (one-time)

```powershell
python -m spotify.auth
```

1. Browser opens the Spotify consent screen.
2. Log in as the Premium account and click **Agree**.
3. Tokens are written to `.tokens.json` (gitignored). Re-run if refresh tokens are revoked.

Scopes requested: `user-modify-playback-state`, `user-read-playback-state`.

### 3. Twitch app

1. Open [Twitch Developer Console](https://dev.twitch.tv/console/apps) and create an application.
2. Set OAuth Redirect URL to: `http://localhost:4343/oauth/callback`
3. Copy Client ID / Secret into `.env` (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`).
4. Set `TWITCH_CHANNEL_LOGIN` to the broadcaster login name.
5. Resolve the numeric user IDs with the bundled helper (needs only the client id/secret above):

```powershell
python -m twitch.lookup yourbotaccount yourchannel
```

It prints one `login  id` pair per line. Put the bot account's id in `TWITCH_BOT_ID` and your
own channel's id in `TWITCH_OWNER_ID`.

A separate bot account is optional. Setting both IDs to your own account works, and replies
are then posted as you. TwitchIO normally discards every message authored by `TWITCH_BOT_ID`
to avoid reply loops, so in that setup the bot instead skips only the messages it sent itself
and still answers your own commands.

Each setup step only validates the credentials it needs, so `python -m spotify.auth` works
before Twitch is configured, and `python -m twitch.lookup` works before the user IDs are known.

### 4. Twitch OAuth (one-time)

Start the bot once (see below). TwitchIO serves OAuth on port `4343`, and the startup log
prints the exact URL(s) to open.

**Single account** (`TWITCH_BOT_ID` == `TWITCH_OWNER_ID`) — grant every scope in one visit:

`http://localhost:4343/oauth?scopes=user:read:chat%20user:write:chat%20user:bot%20channel:bot&force_verify=true`

Splitting this into two visits does **not** work with one account: tokens are stored per user
id, so the second consent replaces the first and its scopes are lost, leaving the chat
subscription failing with 403.

**Separate bot account** — two consents, because they are two different users:

1. As the **bot account**:
   `http://localhost:4343/oauth?scopes=user:read:chat%20user:write:chat%20user:bot&force_verify=true`
2. As the **broadcaster**:
   `http://localhost:4343/oauth?scopes=channel:bot&force_verify=true`

Tokens are stored in `.tio.tokens.json` (gitignored) as soon as consent completes, so they
survive restarts and crashes.

### 5. Runtime precondition

Before viewers use `!sr`, start playback on a Spotify device signed into the Premium account (desktop, phone, or Web Player). The bot cannot start a device for you.

You do **not** need to be live. `channel.chat.message` is delivered for offline chat too, so
commands work whenever the bot process is running.

## Run

### Control app (recommended day-to-day)

Start/Stop from a small window. Tokens stay on disk, so you authorize once and
then just press **Start** / **Stop** for each stream:

```powershell
python -m control_app
```

Keep `.env`, `.tokens.json`, and `.tio.tokens.json` in the project folder (or
next to the `.exe` once packaged). Closing the window stops the bot.

### Standalone exe (desktop shortcut)

Build once:

```powershell
python scripts/build_exe.py
```

That writes `release/SpotifyMusicBot.exe` and copies `.env` / token files into
`release/` when they exist. Create a Desktop shortcut that targets that exe
(leave the shortcut's "Start in" folder as `release\`). Do not move the exe
without its sibling config files.

### Command line

```powershell
python -m main
```

In chat:

```text
!sr never gonna give you up
!sr https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh
!sr spotify:track:4iV5W9uYEdYUVa79Axb7Rh
!np
```

## Commands

| Command | Aliases | Purpose |
| --- | --- | --- |
| `!sr <name\|link>` | `!songrequest`, `!request` | Append a track to the Spotify queue |
| `!np` | `!nowplaying`, `!song`, `!current` | Show the currently playing track |
| `!queue` | `!q`, `!srqueue`, `!songs` | Numbered list of requests not yet played |
| `!srremove <number\|name>` | `!srdel`, `!srcancel`, `!unrequest` | Remove a queued request |
| `!srskip` | `!skip` | Skip the current track (streamer/mods) |
| `!srclearall` | none, on purpose | Drop every queued request (streamer/mods) |
| `!srhelp` | `!commands`, `!srcommands` | List the commands |

`!srskip` wakes the playback poller straight away, so `!np` and the pending-slot accounting
reflect the new track within about a second instead of waiting for the next scheduled poll.

`!srclearall` has no aliases so it cannot be fired off by accident. It frees every requester's
slot and, like `!srremove`, marks the tracks to be skipped, because Spotify still holds them in
its own queue.

### Changing playlist mid-stream

Starting a different playlist or album replaces the current track and everything the playlist
would have played next, but it leaves already-queued requests in place, so nothing needs
restoring. A request that was playing at the moment you switch counts as played: its slot was
released when it started, and the bot will not re-queue it.

`!queue` lists the bot's own tracked requests, newest last, with the requester's name. It
deliberately does not mirror `GET /me/player/queue`, which also returns upcoming album or
playlist tracks and would bury the actual requests.

`!srremove` accepts either a position from `!queue` or a track name (typo-tolerant, same
matching as `!sr`). You and your mods can remove anything; a viewer can only remove their own
request. Removing frees the requester's pending slot.

### Removal is a skip, not an un-queue

Spotify's Web API can add to the queue but has **no endpoint to remove or reorder** anything
already in it. So `!srremove` drops the entry from `!queue` and records the track; when
playback reaches it, the bot calls `POST /me/player/next`. Roughly a second of it can be heard
before the skip. The skip applies once, so if somebody legitimately requests the same song
later it plays normally.

### Why there is a poller

Spotify publishes no playback events, webhooks, or sockets, so a track change can only be
noticed by asking. Rather than polling at a fixed rate, each response carries `duration_ms`
and `progress_ms`, so the next poll is scheduled to land just after the current track is due
to end:

- Track changes are seen about a second after they happen, which is what keeps `!srremove`
  responsive and pending slots accurate.
- Long tracks are still checked every 15s at most, so manual skips and device changes do not
  go unnoticed for a whole song.
- Paused, idle, or device-less playback drops to the 15s interval.
- Polls never run closer together than 1s.

`!np` replies like `Now playing: Never Gonna Give You Up — Rick Astley [1:23 / 3:33] <link>`,
or `Paused: ...` when playback is paused. Results are cached for 5s so the command cannot be
spammed against the Spotify API.

## Search accuracy

Name searches fetch 10 candidates and re-rank them locally instead of trusting Spotify's top
hit, which drifts to tribute albums and covers on loose queries. A candidate is scored on how
many of the request's words it accounts for, across both title and artist:

- Typos are tolerated (`дурак и морния` still finds `Дурак и молния`).
- Cyrillic requests are transliterated, so `король и шут` matches the romanized `Korol i Shut`.
- Equal scores keep Spotify's own order, which favours the original over covers.
- `artist - title` and `title by artist` also run a field-filtered search.
- If nothing accounts for at least half the request, the reply is `No track found` rather
  than a wrong track.

Spotify links and URIs bypass search entirely and are always exact.

## Abuse controls (defaults)

| Setting | Default |
| --- | --- |
| Per-user cooldown | 60s |
| Max pending per user | 0 (unlimited; set `SR_MAX_PENDING_PER_USER` to re-enable) |
| Max track duration | 480s |
| Chat send rate | 20 messages / 30s |
| Blocklist | `blocklist.json` |

Broadcaster and moderators bypass cooldown and pending caps. The blocklist and the
max-duration limit still apply to everyone.

A pending slot is released when the requested track actually starts playing. A background
poller checks `GET /me/player` to detect track changes, because Spotify emits no track-end
event. If a track is skipped, any request queued ahead of the new track is released too.

That poll never gates `!sr`. `GET /me/player` answers `204 No Content` in cases where music
is in fact playing — a private session most commonly, or simply state that has not been
reported yet — so treating it as "no device" rejected valid requests. Spotify decides
instead: the search and queue calls are always attempted, and only a real `404` from the
queue endpoint produces the no-device reply. Requests are therefore accepted while playback
is paused, which Spotify allows as long as the device is still active.

## Chat error replies

| Condition | Reply |
| --- | --- |
| `403 PREMIUM_REQUIRED` | Spotify account is not Premium; requests disabled. |
| `404 NO_ACTIVE_DEVICE` | Spotify has no active device; ask the streamer to press play once. |
| Auth failure after refresh | Spotify authentication failed; streamer needs to re-authorize. |
| `429` | rate limited, try again in Ns |
| Empty search | No track found for ``...``. |

Every outbound reply passes through `chat_format.sanitize`, which collapses newlines,
truncates to Twitch's 500-character limit, and defuses a leading `/` or `.` so remote or
viewer-supplied text can never be sent as a chat command by the bot account.

Spotify's `Retry-After` is honoured only up to 10s; longer values fail immediately rather
than stalling the command.

## Tests / lint

```powershell
pytest
ruff check src tests
pyright
```

## Spotify API limits (cannot be coded around)

- Append only: no insert-after-current, no remove, no reorder.
- Queue can be cleared by playlist/album context changes.
- No push event for track end; detecting transitions requires polling.
- Local files and market-unavailable tracks cannot be queued.

## Policy note

Spotify Developer Terms attached to the Player API state that Spotify content may not be broadcasted and streaming apps may not be commercial. Using this bot on a Twitch stream may conflict with those terms. Accept the risk or obtain Spotify written approval.
