"""One-time Spotify Authorization Code Flow bootstrap.

Binds a loopback HTTP server on 127.0.0.1:8888, prints the consent URL,
exchanges the authorization code for tokens, and persists them to
`.tokens.json` with mode 0600 where the OS supports it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from app_paths import data_dir, ensure_data_cwd
from config import SpotifySettings, get_spotify_settings

LOGGER = logging.getLogger("spotify.auth")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-modify-playback-state user-read-playback-state"

# Refresh this many seconds before the reported expiry.
TOKEN_EXPIRY_MARGIN = 60.0


class TokenFileError(RuntimeError):
    """Token file is absent, unreadable, or missing required fields."""


class _CallbackState:
    code: str | None = None
    error: str | None = None
    expected_state: str = ""


def _make_handler(state: _CallbackState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            params = urllib.parse.parse_qs(parsed.query)
            returned_state = (params.get("state") or [None])[0]
            if returned_state != state.expected_state:
                state.error = "state_mismatch"
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid state. Close this tab.")
                return

            if "error" in params:
                state.error = params["error"][0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization denied. Close this tab.")
                return

            code = (params.get("code") or [None])[0]
            if not code:
                state.error = "missing_code"
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code. Close this tab.")
                return

            state.code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Spotify authorized.</h1>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            LOGGER.debug("callback: " + format, *args)

    return Handler


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict[str, Any]:
    response = httpx.post(
        SPOTIFY_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def save_tokens(path: Path, payload: dict[str, Any]) -> None:
    """Write tokens atomically so a concurrent refresh cannot truncate the file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    expires_at = payload.get("expires_at")
    if expires_at is None and payload.get("expires_in") is not None:
        expires_at = time.time() + float(payload["expires_in"]) - TOKEN_EXPIRY_MARGIN

    data = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "token_type": payload.get("token_type", "Bearer"),
        "expires_in": payload.get("expires_in"),
        "expires_at": expires_at,
        "scope": payload.get("scope"),
    }

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Windows may ignore or reject chmod; tokens still written.
        LOGGER.warning("Could not set 0600 on %s; restrict file access manually", path)
    os.replace(tmp, path)


def load_tokens(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise TokenFileError(f"{path} not found. Run: python -m spotify.auth")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenFileError(
            f"{path} is unreadable ({exc}). Re-run: python -m spotify.auth"
        ) from exc
    if not isinstance(data, dict):
        raise TokenFileError(f"{path} is not a JSON object. Re-run: python -m spotify.auth")
    missing = [key for key in ("access_token", "refresh_token") if not data.get(key)]
    if missing:
        raise TokenFileError(
            f"{path} is missing {', '.join(missing)}. Re-run: python -m spotify.auth"
        )
    return data


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "show_dialog": "true",
        }
    )
    return f"{SPOTIFY_AUTH_URL}?{query}"


def run_oauth_bootstrap(settings: SpotifySettings | None = None) -> Path:
    ensure_data_cwd()
    settings = settings or get_spotify_settings()
    redirect = urllib.parse.urlparse(settings.spotify_redirect_uri)
    host = redirect.hostname or "127.0.0.1"
    port = redirect.port or 8888

    callback_state = _CallbackState()
    callback_state.expected_state = secrets.token_urlsafe(16)

    authorize_url = build_authorize_url(
        client_id=settings.spotify_client_id,
        redirect_uri=settings.spotify_redirect_uri,
        state=callback_state.expected_state,
    )

    print("Open this URL in a browser and authorize with your Spotify Premium account:")
    print(authorize_url)
    try:
        webbrowser.open(authorize_url)
    except Exception:  # noqa: BLE001
        pass

    server = HTTPServer((host, port), _make_handler(callback_state))
    print(f"Listening for callback on {settings.spotify_redirect_uri} ...")
    while callback_state.code is None and callback_state.error is None:
        server.handle_request()
    server.server_close()

    if callback_state.error:
        raise RuntimeError(f"Spotify authorization failed: {callback_state.error}")
    assert callback_state.code is not None

    tokens = exchange_code(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        code=callback_state.code,
    )
    tokens_path = settings.spotify_tokens_path
    if not tokens_path.is_absolute():
        tokens_path = data_dir() / tokens_path
    save_tokens(tokens_path, tokens)
    print(f"Tokens saved to {tokens_path}")
    return tokens_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        run_oauth_bootstrap()
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
