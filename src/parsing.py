"""Spotify track URI / URL parsing."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# open.spotify.com/track/<id>, open.spotify.com/intl-xx/track/<id>, spotify:track:<id>
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_URI_RE = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")
_PATH_TRACK_RE = re.compile(r"(?:^|/)(?:intl-[a-z]{2}/)?track/([A-Za-z0-9]{22})(?:/|$)")


def extract_track_id(text: str) -> str | None:
    """Return a Spotify track ID if `text` is a URL or URI; otherwise None.

    Free-text queries return None so the caller can search by name.
    Strips query params such as ?si=.
    """
    raw = text.strip()
    if not raw:
        return None

    # Bare track ID
    if _TRACK_ID_RE.fullmatch(raw):
        return raw

    uri_match = _URI_RE.fullmatch(raw)
    if uri_match:
        return uri_match.group(1)

    # Allow pasting URL with surrounding whitespace / angle brackets
    cleaned = raw.strip("<>")
    parsed = urlparse(cleaned)
    if parsed.scheme in {"http", "https"} and "spotify.com" in (parsed.netloc or ""):
        path_match = _PATH_TRACK_RE.search(parsed.path)
        if path_match:
            return path_match.group(1)

    # Fallback: URI embedded inside a longer string
    embedded = re.search(r"spotify:track:([A-Za-z0-9]{22})", raw)
    if embedded:
        return embedded.group(1)

    return None


def track_uri(track_id: str) -> str:
    return f"spotify:track:{track_id}"
