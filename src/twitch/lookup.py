"""Resolve Twitch login names to numeric user IDs.

Usage:
    python -m twitch.lookup <login> [<login> ...]

Needs only TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env. Prints the values
to paste into TWITCH_BOT_ID and TWITCH_OWNER_ID.
"""

from __future__ import annotations

import logging
import sys

import httpx
from pydantic import ValidationError

from config import TwitchAppSettings, get_twitch_app_settings

LOGGER = logging.getLogger("twitch.lookup")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
USERS_URL = "https://api.twitch.tv/helix/users"
REQUEST_TIMEOUT = 10.0


def fetch_app_token(settings: TwitchAppSettings) -> str:
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.twitch_client_id,
            "client_secret": settings.twitch_client_secret,
            "grant_type": "client_credentials",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Twitch token request failed: {response.status_code} {response.text}"
        )
    return response.json()["access_token"]


def fetch_user_ids(settings: TwitchAppSettings, logins: list[str]) -> dict[str, str]:
    token = fetch_app_token(settings)
    response = httpx.get(
        USERS_URL,
        params=[("login", login) for login in logins],
        headers={
            "Client-Id": settings.twitch_client_id,
            "Authorization": f"Bearer {token}",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Twitch users request failed: {response.status_code} {response.text}")
    return {entry["login"]: entry["id"] for entry in response.json().get("data", [])}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    logins = [arg.strip().lower() for arg in sys.argv[1:] if arg.strip()]
    if not logins:
        print("Usage: python -m twitch.lookup <login> [<login> ...]")
        sys.exit(2)

    try:
        settings = get_twitch_app_settings()
    except ValidationError as exc:
        LOGGER.error("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env first:\n%s", exc)
        sys.exit(1)

    try:
        found = fetch_user_ids(settings, logins)
    except (RuntimeError, httpx.HTTPError) as exc:
        LOGGER.error("%s", exc)
        sys.exit(1)

    for login in logins:
        user_id = found.get(login)
        if user_id:
            print(f"{login:<25} {user_id}")
        else:
            print(f"{login:<25} NOT FOUND")

    missing = [login for login in logins if login not in found]
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
