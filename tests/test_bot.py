from __future__ import annotations

from datetime import datetime

import newsbot.bot as bot_module
from newsbot.article import Article
from newsbot.bot import NewsBot
from newsbot.feeds import RawItem
from newsbot.telegram import SendResult
from newsbot.translate import TranslationResult
from newsbot.util import MSK


def patch_bot(bot, monkeypatch):
    def fake_translate(text, mode="text", allow_llm=True):
        return TranslationResult("ПЕРЕВОД: " + text, "fake", True)

    monkeypatch.setattr(bot.translator, "translate", fake_translate)
    monkeypatch.setattr(bot.translator, "summarize", lambda *a, **k: None)
    monkeypatch.setattr(bot_module, "fetch_article", lambda cfg, session, url: Article(
        body="Тело статьи с подробностями. " * 40,
        image="https://example.com/image.jpg",
        title="Page title",
    ))
    monkeypatch.setattr(bot_module, "download_image", lambda *a, **k: None)
    monkeypatch.setattr(bot_module, "download_video", lambda *a, **k: None)


def make_items(now: float) -> list[RawItem]:
    data = [
        ("BBC World", "Wildfire forces evacuations in northern Spain"),
        ("DW", "Wildfire forces evacuations in northern Spain"),
        ("Lenta.ru", "В Испании эвакуируют жителей из-за лесных пожаров"),
        ("CNBC", "Oil prices jump after OPEC cuts production"),
        ("IGN", "Nintendo announces new Mario game for Switch 2"),
    ]
    return [
        RawItem(
            source=source,
            link=f"https://example.com/{i}?utm_source=rss",
            title=title,
            summary="Summary text",
            published_ts=now - 600,
            image=f"https://example.com/{i}.jpg",
        )
        for i, (source, title) in enumerate(data)
    ]


def patch_senders(bot, monkeypatch):
    sent = []

    def send_photo(chat_id, caption, **kwargs):
        sent.append(("photo", chat_id, caption, kwargs.get("buttons")))
        return SendResult(True, len(sent), chat_id)

    def send_video(chat_id, caption, **kwargs):
        sent.append(("video", chat_id, caption, kwargs.get("buttons")))
        return SendResult(True, len(sent), chat_id)

    def send_text(chat_id, text, **kwargs):
        sent.append(("text", chat_id, text, kwargs.get("buttons")))
        return SendResult(True, len(sent), chat_id)

    def edit_caption(chat_id, message_id, caption, **kwargs):
        sent.append(("edit", chat_id, caption, kwargs.get("buttons")))
        return SendResult(True, message_id, chat_id)

    monkeypatch.setattr(bot.tg, "send_photo", send_photo)
    monkeypatch.setattr(bot.tg, "send_video", send_video)
    monkeypatch.setattr(bot.tg, "send_text", send_text)
    monkeypatch.setattr(bot.tg, "edit_caption", edit_caption)
    return sent


def test_posts_two_and_dedups(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)

    items = make_items(now)
    bot.process_items(items)
    bot.post_pending()

    assert len([s for s in sent if s[0] == "photo"]) == 2
    assert len(bot.state.clusters) == 4
    assert bot.state.posts_today(now) == 2
    sources = {r["source"] for r in bot.state.posted.values() if not r.get("alias")}
    assert len(sources) == 2
    for kind, chat, caption, _buttons in sent:
        assert len(caption) <= cfg.caption_limit
        assert "Читать в оригинале" in caption

    # Повторный запуск не публикует те же новости: в очереди остались
    # только сюжеты, не выбранные в первом запуске.
    bot2 = NewsBot(cfg, now + 60)
    patch_bot(bot2, monkeypatch)
    sent2 = patch_senders(bot2, monkeypatch)
    bot2.process_items(items)
    bot2.post_pending()
    assert len(sent2) == 2
    first_sources = {s[2] for s in sent}
    assert all(s[2] not in first_sources for s in sent2)
    assert not any("Lenta.ru" in s[2] or "IGN" in s[2] for s in sent)
    assert any("Lenta.ru" in s[2] or "IGN" in s[2] for s in sent2)


def test_no_repost_when_all_posted(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(
        state_file=str(tmp_path / "state.json"),
        max_posts_per_run=5,
        max_per_source_per_run=5,
        max_per_category_per_run=5,
    )
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    items = make_items(now)
    bot.process_items(items)
    bot.post_pending()
    assert len([s for s in sent if s[0] == "photo"]) == 4
    bot.state.save(now)

    bot2 = NewsBot(cfg, now + 60)
    patch_bot(bot2, monkeypatch)
    sent2 = patch_senders(bot2, monkeypatch)
    bot2.process_items(items)
    bot2.post_pending()
    assert sent2 == []


def test_second_run_posts_remaining_clusters(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    items = make_items(now)
    bot.process_items(items)
    bot.post_pending()
    assert len([s for s in sent if s[0] == "photo"]) == 2
    bot.state.save(now)

    bot2 = NewsBot(cfg, now + 120)
    patch_bot(bot2, monkeypatch)
    sent2 = patch_senders(bot2, monkeypatch)
    bot2.process_items(items)
    bot2.post_pending()
    assert len(sent2) == 2


def test_quiet_hours_no_posts(cfg, monkeypatch, tmp_path):
    night = datetime(2026, 10, 20, 2, 0, tzinfo=MSK).timestamp()
    cfg = cfg.with_overrides(
        state_file=str(tmp_path / "state.json"),
        quiet_hours=(23, 8),
        breaking_at_night=False,
    )
    bot = NewsBot(cfg, night)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    bot.process_items(make_items(night))
    bot.post_pending()
    assert sent == []


def test_breaking_at_night_posts_silent(cfg, monkeypatch, tmp_path):
    night = datetime(2026, 10, 20, 2, 0, tzinfo=MSK).timestamp()
    cfg = cfg.with_overrides(
        state_file=str(tmp_path / "state.json"),
        quiet_hours=(23, 8),
        breaking_at_night=True,
        max_posts_per_run=1,
    )
    bot = NewsBot(cfg, night)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    items = [
        RawItem(source="BBC World", link="https://example.com/quake",
                title="Breaking: massive earthquake hits Turkey",
                summary="At least 20 killed", published_ts=night - 60,
                image="https://example.com/quake.jpg"),
        RawItem(source="CNBC", link="https://example.com/oil",
                title="Oil prices jump after OPEC cuts production",
                summary="", published_ts=night - 60),
    ]
    bot.process_items(items)
    bot.post_pending()
    assert len(sent) == 1
    assert "earthquake" in sent[0][2] or "ЗЕМЛЕТРЯСЕНИЕ" in sent[0][2].upper()


def test_update_after_new_source(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    items = make_items(now)
    bot.process_items(items)
    bot.post_pending()
    assert len([s for s in sent if s[0] == "photo"]) == 2

    extra = RawItem(
        source="France 24",
        link="https://example.com/extra",
        title="Wildfire forces evacuations in northern Spain",
        summary="", published_ts=now - 60,
    )
    bot.deduper.now = now + 3600
    bot.now = now + 3600
    bot.process_items([extra])
    assert any(c.get("pending_update") for c in bot.state.clusters.values())
    bot.apply_updates()
    assert any(s[0] == "edit" for s in sent)


def test_source_buttons_under_post(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    bot.process_items(make_items(now))
    bot.post_pending()

    photos = [s for s in sent if s[0] == "photo"]
    assert photos, "должен быть хотя бы один пост"
    all_buttons = [row for s in photos if s[3] for row in s[3]]
    labels = [row[0]["text"] for row in all_buttons]
    assert any("DW" in label or "Lenta.ru" in label for label in labels)
    # Кнопки ведут на реальные ссылки, а не на callback.
    assert all("url" in row[0] for row in all_buttons)


def test_source_buttons_can_be_disabled(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"), source_buttons=False)
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    sent = patch_senders(bot, monkeypatch)
    bot.process_items(make_items(now))
    bot.post_pending()
    photos = [s for s in sent if s[0] == "photo"]
    assert photos
    assert all(s[3] is None for s in photos)


def test_preview_post_does_not_register_cluster(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    draft = bot.preview_post("https://example.com/manual")
    assert "Черновик поста" in draft
    assert "в канал не отправлен" in draft
    assert "Читать в оригинале" in draft
    assert bot.state.clusters == {}
    assert bot.state.posted == {}


def test_explain_queued_and_rejection(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    patch_senders(bot, monkeypatch)
    items = make_items(now)
    bot.process_items(items)

    cluster = list(bot.state.clusters.values())[0]
    text = bot.explain(str(cluster["id"]))
    assert "Сюжет найден" in text
    assert "очереди" in text

    url_text = bot.explain("https://example.com/0?utm_source=rss")
    assert "Сюжет найден" in url_text

    # Кликбейт отфильтровывается на входе и объясняется через /why.
    clickbait = RawItem(
        source="IGN", link="https://example.com/how-to-watch",
        title="How to watch the Nintendo Direct live stream",
        summary="", published_ts=now - 60,
    )
    bot.process_items([clickbait])
    filtered = bot.explain("https://example.com/how-to-watch")
    assert "отфильтрована" in filtered
    assert "SKIP_PATTERNS" in filtered

    missing = bot.explain("https://example.com/ничего-нет")
    assert "Ничего не нашёл" in missing


def test_explain_after_posting(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    patch_senders(bot, monkeypatch)
    bot.process_items(make_items(now))
    bot.post_pending()

    posted = [record for record in bot.state.posted.values() if not record.get("alias")]
    assert posted
    text = bot.explain(str(posted[0]["url"]))
    assert "уже опубликована" in text
    assert "Сообщение" in text


def test_preview_candidates_shows_limits(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    patch_bot(bot, monkeypatch)
    patch_senders(bot, monkeypatch)
    bot.process_items(make_items(now))

    text = bot.preview_candidates(5)
    assert "Очередь публикации" in text
    assert "выйдет 2 из" in text
    assert "▶️" in text
    assert "запуск" in text


def test_preview_candidates_during_quiet_hours(cfg, monkeypatch, tmp_path):
    night = datetime(2026, 10, 20, 2, 0, tzinfo=MSK).timestamp()
    cfg = cfg.with_overrides(
        state_file=str(tmp_path / "state.json"),
        quiet_hours=(23, 8),
        breaking_at_night=False,
    )
    bot = NewsBot(cfg, night)
    patch_bot(bot, monkeypatch)
    patch_senders(bot, monkeypatch)
    bot.process_items(make_items(night))
    text = bot.preview_candidates(5)
    assert "заблокированы" in text
    assert "тихие часы" in text


def test_doctor_report_works(cfg, now, monkeypatch, tmp_path):
    cfg = cfg.with_overrides(state_file=str(tmp_path / "state.json"))
    bot = NewsBot(cfg, now)
    import newsbot.doctor as doctor

    monkeypatch.setattr(
        doctor, "run",
        lambda *a, **k: [doctor.Check("X", doctor.OK, "ok")],
    )
    text = bot.doctor_report()
    assert "Проверка news-bot" in text
