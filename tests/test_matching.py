"""Search re-ranking: Spotify's top hit is not always the right track."""

from matching import MIN_SCORE, artist_title_query, best_match, score, transliterate
from spotify.client import Track


def track(name: str, artists: tuple[str, ...], *, id: str = "t1") -> Track:
    return Track(
        id=id,
        name=name,
        artists=artists,
        artist_ids=("a1",),
        duration_ms=200_000,
        uri=f"spotify:track:{id}",
    )


# Spotify stores this band romanized, so the Cyrillic request only lines up
# after transliteration.
WANTED = track("Дурак и молния", ("Korol i Shut",), id="wanted")
# A different band's song that happens to be titled with the requested band's
# name; it matched just as well before transliteration was added.
DECOY = track("Король и Шут", ("Leningrad",), id="decoy")
TRIBUTE = track("Памяти М.Ю. Горшенева", ("Спектакль Джо",), id="tribute")

QUERY = "король и шут дурак и морния"


def test_transliterates_cyrillic_to_latin() -> None:
    assert transliterate("король и шут") == "korol i shut"


def test_typo_and_romanized_artist_still_match() -> None:
    """"морния" for "молния", plus a romanized artist name."""
    assert score(QUERY, WANTED) == 1.0


def test_decoy_titled_after_the_band_scores_lower() -> None:
    assert score(QUERY, DECOY) < score(QUERY, WANTED)


def test_unrelated_tribute_scores_low() -> None:
    assert score(QUERY, TRIBUTE) < MIN_SCORE


def test_best_match_prefers_intended_track() -> None:
    chosen = best_match(QUERY, [TRIBUTE, DECOY, WANTED])
    assert chosen is not None
    assert chosen.id == "wanted"


def test_no_plausible_candidate_returns_none() -> None:
    assert best_match("zzzz totally unrelated query", [TRIBUTE, WANTED]) is None


def test_ties_keep_spotify_ordering() -> None:
    """Equal scores must not promote a cover over Spotify's top hit."""
    original = track("Never Gonna Give You Up", ("Rick Astley",), id="original")
    cover = track("Never Gonna Give You Up", ("Kapena",), id="cover")
    chosen = best_match("never gonna give you up", [original, cover])
    assert chosen is not None
    assert chosen.id == "original"


def test_artist_only_query_matches_any_of_their_tracks() -> None:
    assert score("король и шут", WANTED) == 1.0


def test_extra_words_still_clear_the_threshold() -> None:
    assert score("please play never gonna give you up", WANTED) < MIN_SCORE
    rick = track("Never Gonna Give You Up", ("Rick Astley",))
    assert score("please play never gonna give you up", rick) >= MIN_SCORE


def test_duplicate_candidates_are_ignored() -> None:
    assert best_match("король и шут", [WANTED, WANTED]) is WANTED


def test_artist_title_query_builds_field_filters() -> None:
    assert artist_title_query("Rick Astley - Never Gonna Give You Up") == (
        'artist:"Rick Astley" track:"Never Gonna Give You Up"'
    )


def test_by_separator_reverses_artist_and_title() -> None:
    assert artist_title_query("Never Gonna Give You Up by Rick Astley") == (
        'artist:"Rick Astley" track:"Never Gonna Give You Up"'
    )


def test_artist_title_query_without_separator() -> None:
    assert artist_title_query("король и шут дурак и молния") is None
    assert artist_title_query("just-one-hyphenated-thing") is None


def test_quotes_do_not_break_the_field_query() -> None:
    assert artist_title_query('"Rick Astley" - "Never Gonna Give You Up"') == (
        'artist:"Rick Astley" track:"Never Gonna Give You Up"'
    )
