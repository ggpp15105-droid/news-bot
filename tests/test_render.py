from __future__ import annotations

from newsbot.render import build_caption, build_full_text, plural_sources


def cluster(title: str, lead: str = "", translated: bool = True, failed: bool = False):
    return {
        "id": "c1",
        "title": title,
        "title_ru": title,
        "source": "France 24",
        "category": "мир",
        "links": [
            {"url": "https://example.com/news/1?utm_source=rss&a=1", "source": "France 24"},
            {"url": "https://example.com/news/2", "source": "BBC World"},
            {"url": "https://example.com/news/3", "source": "DW"},
        ],
        "tags": "#мир #политика",
        "translated": translated,
        "translation_failed": failed,
        "size": 3,
        "opinion": False,
    }


def test_caption_limit_and_link(cfg):
    for size in (10, 40, 100):
        cap = build_caption(cfg, cluster("Очень длинный заголовок " * size), lead="Лид " * 500)
        assert len(cap) <= cfg.caption_limit
        assert cap.count("<a") == cap.count("</a>") == 1
        assert "Читать в оригинале" in cap


def test_full_text_limit(cfg):
    text = build_full_text(cfg, cluster("Заголовок " * 100), lead="Лид " * 1000)
    assert len(text) <= cfg.tg_limit
    assert text.count("<a") == text.count("</a>") == 1


def test_translation_labels(cfg):
    cap = build_caption(cfg, cluster("Title", translated=True))
    assert "автоматический перевод" in cap
    cap = build_caption(cfg, cluster("Title", translated=False, failed=True))
    assert "перевод недоступен" in cap


def test_confirmation_label(cfg):
    cfg2 = cfg.with_overrides(confirmation_label=True)
    cap = build_caption(cfg2, cluster("Title"))
    assert "✅" in cap and "источника" in cap


def test_plural_sources():
    assert plural_sources(1) == "1 источник"
    assert plural_sources(2) == "2 источника"
    assert plural_sources(5) == "5 источников"
    assert plural_sources(11) == "11 источников"
