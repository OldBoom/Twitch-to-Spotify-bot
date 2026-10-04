"""Rank Spotify search candidates against a free-text chat query.

Spotify's ranking is a good prior but drifts on loose or misspelled queries,
returning tribute albums, covers, or unrelated tracks. Candidates are scored on
how much of the request they actually account for, so an unconvincing best
match is rejected instead of queued.

Cyrillic requests are transliterated before comparison because Spotify stores
many artists romanized ("Korol i Shut" for "Король и Шут").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

from spotify.client import Track

_DASH = re.compile(r"\s+[-–—]\s+")
_BY = re.compile(r"\s+by\s+", re.IGNORECASE)
_NON_WORD = re.compile(r"\W+", re.UNICODE)

# A query word counts as present when it is at least this similar to a
# candidate word, which absorbs typos like "морния" for "молния".
TOKEN_MATCH_RATIO = 0.8
# Share of the request that a candidate must account for to be queued.
MIN_SCORE = 0.5
# Below this length a query word is too generic to match on substrings alone,
# so "en" or "up" cannot latch onto every candidate.
MIN_SUBSTRING_LENGTH = 4

# Ranking penalties. These only reorder candidates that already clear
# MIN_SCORE, so a stricter preference can never turn into "No track found".
#
# Every title word the request does not account for costs this much, which is
# what separates "Birdbrain" from "BIRDBRAIN en Español Teto SV2". Artist words
# are exempt: charging for them would favour covers by short-named artists over
# the original recording.
EXTRA_TITLE_WORD_PENALTY = 0.04
# Re-recordings chat did not ask for. Asking for them explicitly ("sr x live")
# cancels the penalty, because the marker is then part of the request.
VARIANT_PENALTY = 0.25
_VARIANT_MARKERS = frozenset(
    {
        "live",
        "remix",
        "cover",
        "karaoke",
        "instrumental",
        "acoustic",
        "acapella",
        "sped",
        "slowed",
        "nightcore",
        "reverb",
        "demo",
    }
)

_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Ukrainian / Belarusian letters that show up in track titles.
    "і": "i", "ї": "i", "є": "ie", "ґ": "g", "ў": "u",
}


def transliterate(text: str) -> str:
    return "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in text)


def _tokens(text: str) -> tuple[str, ...]:
    folded = transliterate(text.casefold())
    return tuple(token for token in _NON_WORD.split(folded) if token)


def _similar(left: str, right: str) -> bool:
    if left == right:
        return True
    # Spotify glues some names together ("DEMONDICE"), so a request typed as
    # separate words still has to line up with the single stored word.
    if len(left) >= MIN_SUBSTRING_LENGTH and left in right:
        return True
    if len(right) >= MIN_SUBSTRING_LENGTH and right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= TOKEN_MATCH_RATIO


def _artist_tokens(track: Track) -> tuple[str, ...]:
    return tuple(token for artist in track.artists for token in _tokens(artist))


def _candidate_tokens(track: Track) -> tuple[str, ...]:
    return _tokens(track.name) + _artist_tokens(track)


def _coverage(wanted: tuple[str, ...], candidate: tuple[str, ...]) -> float:
    """Share of `wanted` words present in `candidate`, in 0.0..1.0.

    Deliberately one-sided: extra words in a candidate are not penalised, since
    doing so favours covers by artists with short names over the original.
    """
    if not wanted or not candidate:
        return 0.0
    covered = sum(
        1 for word in wanted if any(_similar(word, other) for other in candidate)
    )
    return covered / len(wanted)


def score(query: str, track: Track) -> float:
    """Share of the query's words that `track` accounts for, in 0.0..1.0."""
    return _coverage(_tokens(query), _candidate_tokens(track))


def score_text(query: str, text: str) -> float:
    """Same scoring against an arbitrary string, e.g. a queued entry's label."""
    return _coverage(_tokens(query), _tokens(text))


def rank(query: str, track: Track) -> float:
    """Ordering score: coverage, minus how much of the track went unasked for.

    Coverage alone ties an exact hit with a padded variation of it, because
    both account for the whole request. The penalties break that tie towards
    the leaner, non-re-recorded candidate.
    """
    wanted = _tokens(query)
    title_tokens = _tokens(track.name)
    candidate = title_tokens + _artist_tokens(track)

    extra_title_words = sum(
        1 for word in title_tokens if not any(_similar(other, word) for other in wanted)
    )
    unrequested_variants = {
        word
        for word in title_tokens
        if word in _VARIANT_MARKERS and not any(_similar(other, word) for other in wanted)
    }

    return (
        _coverage(wanted, candidate)
        - EXTRA_TITLE_WORD_PENALTY * extra_title_words
        - VARIANT_PENALTY * len(unrequested_variants)
    )


def best_match(
    query: str,
    candidates: Iterable[Track],
    *,
    min_score: float = MIN_SCORE,
) -> Track | None:
    """Best candidate, or None when nothing accounts for enough of the query.

    Admission is on coverage so a plausible track is never dropped, while the
    choice among admitted tracks is on `rank`. Ties keep Spotify's own
    ordering, which is a strong signal for which recording is the original.
    """
    best: Track | None = None
    best_rank = 0.0
    seen: set[str] = set()

    for track in candidates:
        if track.id in seen:
            continue
        seen.add(track.id)
        if score(query, track) < min_score:
            continue
        current = rank(query, track)
        if best is None or current > best_rank:
            best_rank = current
            best = track
    return best


def _clean_part(text: str) -> str:
    return text.strip().strip('"').strip()


def artist_title_query(query: str) -> str | None:
    """Field-filtered Spotify query for `artist - title` requests.

    Returns None when the query has no artist/title separator.
    """
    stripped = query.strip()

    by_parts = _BY.split(stripped, maxsplit=1)
    if len(by_parts) == 2:
        title, artist = by_parts
    else:
        dash_parts = _DASH.split(stripped, maxsplit=1)
        if len(dash_parts) != 2:
            return None
        artist, title = dash_parts

    artist, title = _clean_part(artist), _clean_part(title)
    if not artist or not title:
        return None
    return f'artist:"{artist}" track:"{title}"'
