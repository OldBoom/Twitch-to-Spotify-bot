"""Tests for Spotify URL/URI parsing."""

from parsing import extract_track_id, track_uri

TRACK_ID = "4iV5W9uYEdYUVa79Axb7Rh"


def test_bare_id() -> None:
    assert extract_track_id(TRACK_ID) == TRACK_ID


def test_spotify_uri() -> None:
    assert extract_track_id(f"spotify:track:{TRACK_ID}") == TRACK_ID


def test_open_spotify_url_strips_si() -> None:
    url = f"https://open.spotify.com/track/{TRACK_ID}?si=abc123"
    assert extract_track_id(url) == TRACK_ID


def test_intl_variant_url() -> None:
    url = f"https://open.spotify.com/intl-de/track/{TRACK_ID}?si=xyz"
    assert extract_track_id(url) == TRACK_ID


def test_free_text_returns_none() -> None:
    assert extract_track_id("never gonna give you up") is None


def test_empty_returns_none() -> None:
    assert extract_track_id("   ") is None


def test_track_uri() -> None:
    assert track_uri(TRACK_ID) == f"spotify:track:{TRACK_ID}"
