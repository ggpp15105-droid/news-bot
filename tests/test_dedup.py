from __future__ import annotations

from newsbot.dedup import Candidate, Deduper


def make_candidate(url: str, title: str, source: str = "BBC World", title_ru: str = "",
                   ts: float = 1_000_000.0) -> Candidate:
    return Candidate(
        url=url,
        canonical=url,
        source=source,
        title=title,
        title_ru=title_ru,
        summary="",
        published_ts=ts,
        category="мир",
        score=5.0,
    ).prepare()


def test_creates_and_matches_cluster(cfg, state):
    d = Deduper(cfg, state, now=1_000_000.0)
    first = make_candidate(
        "https://example.com/a",
        "Ukraine drone attack hits Russian refinery, officials say",
        "BBC World",
    )
    cluster = d.create(first)
    assert cluster["size"] == 1
    second = make_candidate(
        "https://example.com/b",
        "Russia says Ukraine drone attack hit refinery",
        "DW",
    )
    match = d.find(second)
    assert match is not None
    assert match.cluster_id == cluster["id"]
    assert d.attach(cluster, second)
    assert cluster["size"] == 2
    assert d.distinct_sources(cluster) == 2
    assert d.confirmed(cluster)


def test_russian_and_english_titles_match_via_translation(cfg, state):
    d = Deduper(cfg, state, now=1_000_000.0)
    first = make_candidate(
        "https://example.com/a",
        "Wildfire forces evacuations in northern Spain",
        "BBC World",
        title_ru="Лесной пожар заставил эвакуировать жителей на севере Испании",
    )
    cluster = d.create(first)
    second = make_candidate(
        "https://example.com/b",
        "В Испании эвакуируют жителей из-за лесных пожаров",
        "Lenta.ru",
    )
    match = d.find(second)
    assert match is not None
    assert match.cluster_id == cluster["id"]


def test_negation_does_not_merge(cfg, state):
    d = Deduper(cfg, state, now=1_000_000.0)
    first = make_candidate(
        "https://example.com/a",
        "Trump endorses Netanyahu ahead of Israeli elections",
        "BBC World",
    )
    d.create(first)
    second = make_candidate(
        "https://example.com/b",
        "Trump declines to endorse Netanyahu before Israeli vote",
        "DW",
    )
    assert d.find(second) is None


def test_different_events_do_not_merge(cfg, state):
    d = Deduper(cfg, state, now=1_000_000.0)
    d.create(make_candidate(
        "https://example.com/a",
        "Nintendo announces new Mario game for Switch 2",
        "IGN",
    ))
    other = make_candidate(
        "https://example.com/b",
        "Oil prices jump after OPEC cuts production",
        "CNBC",
    )
    assert d.find(other) is None


def test_same_canonical_not_duplicated(cfg, state):
    d = Deduper(cfg, state, now=1_000_000.0)
    first = make_candidate("https://example.com/a", "Large fire destroys warehouse near Paris", "BBC World")
    cluster = d.create(first)
    same = make_candidate("https://example.com/a", "Large fire destroys warehouse near Paris", "DW")
    match = d.find(same)
    assert match is not None
    assert not d.attach(cluster, same)
    assert cluster["size"] == 1


def test_same_event_numbers_and_word_numbers(cfg, state):
    d = Deduper(cfg, state, now=1_000_000.0)
    first = make_candidate(
        "https://example.com/a",
        "Canada military base fire kills 4",
        "BBC World",
    )
    cluster = d.create(first)
    second = make_candidate(
        "https://example.com/b",
        "Deadly fire at Canadian military base leaves four dead",
        "DW",
    )
    match = d.find(second)
    assert match is not None
    assert match.cluster_id == cluster["id"]
