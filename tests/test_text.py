from __future__ import annotations

from newsbot.text import (
    detect_categories,
    detect_tags,
    has_negation,
    importance,
    is_breaking,
    is_junk,
    is_opinion,
    is_russian,
    make_lead,
    norm_tokens,
    normalize_title_for_dedup,
    numbers_in,
    same_event,
    similarity,
    smart_cut,
)


def test_norm_tokens_stems_english_and_russian():
    assert "clash" in norm_tokens("clashes")
    assert "clash" in norm_tokens("clash")
    assert "policy" in norm_tokens("policies")
    assert norm_tokens("missiles") == norm_tokens("missile")
    assert norm_tokens("Путин встретился с Лукашенко") & {"путин", "лукашенк"}


def test_is_russian():
    assert is_russian("Путин встретился с Лукашенко")
    assert not is_russian("Putin met Lukashenko")


def test_similarity_and_same_event():
    a = norm_tokens("At least 17 killed in stampede at religious gathering in India")
    b = norm_tokens("Deadly stampede kills 17 at Indian religious festival")
    ok, _, inter = similarity(a, b)
    assert inter >= 2
    assert same_event(a, b, numbers_in("...17..."), numbers_in("...17..."))


def test_numbers_words():
    assert "4" in numbers_in("four people killed")
    assert "17" in numbers_in("17 killed")
    assert "12" in numbers_in("dozens killed")


def test_negation_detected():
    assert has_negation(norm_tokens("Trump declines to endorse Netanyahu"))
    assert not has_negation(norm_tokens("Trump endorses Netanyahu"))


def test_different_events_not_similar():
    a = norm_tokens("Ukraine drone attack hits oil refinery")
    b = norm_tokens("Nintendo announces new Mario game")
    ok, _, _ = similarity(a, b)
    assert not ok


def test_detect_tags():
    assert "#спорт" in detect_tags("BBC Sport", "Arsenal beat Chelsea in Premier League", "")
    assert "#игры" in detect_tags("IGN", "Nintendo announces new Mario game", "")
    assert "#железо" in detect_tags("Tom's Hardware", "Nvidia RTX review", "")
    assert "#конфликты" in detect_tags("BBC World", "Drone attack hits refinery", "")
    assert "#мир" in detect_tags("Unknown", "A quiet day in the village", "")


def test_detect_categories_russian():
    cats = detect_categories("Lenta.ru", "Президент Франции обсудил санкции с премьер-министром", "")
    assert cats[0][0] == "политика"


def test_opinion():
    assert is_opinion("Opinion: why this matters", "https://example.com/opinion/x")
    assert is_opinion("Мнение: почему это важно")
    assert not is_opinion("Wildfire forces evacuations")


def test_breaking():
    assert is_breaking("Breaking: massive earthquake hits Turkey", "")
    assert is_breaking("Землетрясение в Турции", "")
    assert not is_breaking("New Mario game announced", "")


def test_junk_and_lead():
    assert is_junk("short")
    assert is_junk("- bullet point in a list")
    assert is_junk("19.10.2026 12:00 something happened here")
    body = (
        "PARIS (Reuters) - French officials said on Monday that a fire broke out "
        "at a warehouse near the city centre.\n\n"
        "No injuries were reported. An investigation has been opened.\n\n"
        "Read more: https://example.com"
    )
    lead = make_lead(body, "", 400)
    assert "PARIS (Reuters)" not in lead
    assert "fire broke out" in lead
    assert "Read more" not in lead


def test_smart_cut_keeps_entity():
    text = "x" * 50 + "&amp;" + "y" * 20
    cut = smart_cut(text, 55)
    assert "&amp;" not in cut or cut.endswith("&amp;")


def test_importance_breaking_higher():
    kwargs = dict(published_ts=1_000_000, now=1_000_000, source_weight=1.0,
                  category="мир", cluster_size=1, title="Something happened")
    assert importance(breaking=True, **kwargs) > importance(breaking=False, **kwargs)


def test_normalize_title_prefix():
    assert normalize_title_for_dedup("BREAKING: Big news") == "Big news"
    assert normalize_title_for_dedup("Big news - BBC News") == "Big news"
