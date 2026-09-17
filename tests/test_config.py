"""Fail-fast configuration tests."""

import pytest
from pydantic import ValidationError

from config import Settings, SpotifySettings, TwitchAppSettings

ALL_KEYS = [
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "TWITCH_BOT_ID",
    "TWITCH_OWNER_ID",
    "TWITCH_CHANNEL_LOGIN",
]


def test_missing_credentials_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_blank_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "sid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "   ")
    with pytest.raises(ValidationError, match="empty"):
        SpotifySettings(_env_file=None)  # type: ignore[call-arg]


def test_spotify_settings_ignore_twitch_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth bootstrap must work before Twitch credentials exist."""
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "sid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "ssecret")

    settings = SpotifySettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.spotify_client_id == "sid"
    assert settings.spotify_redirect_uri == "http://127.0.0.1:8888/callback"


def test_twitch_app_settings_need_only_client_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "tid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "tsecret")

    settings = TwitchAppSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.twitch_client_id == "tid"


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "sid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "ssecret")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "tid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "tsecret")
    monkeypatch.setenv("TWITCH_BOT_ID", "111")
    monkeypatch.setenv("TWITCH_OWNER_ID", "222")
    monkeypatch.setenv("TWITCH_CHANNEL_LOGIN", "streamer")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.spotify_client_id == "sid"
    assert settings.twitch_channel_login == "streamer"
    assert settings.sr_cooldown_seconds == 60
