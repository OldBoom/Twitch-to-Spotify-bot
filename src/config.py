"""Application settings. Fail-fast on missing required credentials.

Split into partial models so each setup step only requires its own credentials:
`spotify.auth` needs Spotify only, `twitch.lookup` needs the Twitch app only,
and the bot itself needs everything.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app_paths import data_dir

_REQUIRED_STR = Field(min_length=1)


def _env_file() -> str:
    return str(data_dir() / ".env")


class _EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> object:
        """Treat an empty env var as absent so the error names the field."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("required credential is empty")
            return stripped
        return value


class SpotifySettings(_EnvSettings):
    spotify_client_id: str = _REQUIRED_STR
    spotify_client_secret: str = _REQUIRED_STR
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_tokens_path: Path = Path(".tokens.json")


class TwitchAppSettings(_EnvSettings):
    twitch_client_id: str = _REQUIRED_STR
    twitch_client_secret: str = _REQUIRED_STR


class Settings(SpotifySettings, TwitchAppSettings):
    twitch_bot_id: str = _REQUIRED_STR
    twitch_owner_id: str = _REQUIRED_STR
    twitch_channel_login: str = _REQUIRED_STR

    # Command / abuse controls
    command_prefix: str = "!"
    sr_cooldown_seconds: int = Field(default=60, ge=0)
    # 0 disables the per-user pending cap (unlimited queue slots).
    sr_max_pending_per_user: int = Field(default=0, ge=0)
    sr_max_track_duration_seconds: int = Field(default=480, ge=1)
    blocklist_path: Path = Path("blocklist.json")
    chat_rate_limit_messages: int = Field(default=20, ge=1)
    chat_rate_limit_window_seconds: int = Field(default=30, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Full settings for running the bot. Raises ValidationError if incomplete."""
    return Settings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_spotify_settings() -> SpotifySettings:
    """Spotify-only settings, so OAuth bootstrap works before Twitch is configured."""
    return SpotifySettings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_twitch_app_settings() -> TwitchAppSettings:
    """Twitch client id/secret only, for the user-ID lookup helper."""
    return TwitchAppSettings()  # type: ignore[call-arg]
