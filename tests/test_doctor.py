from __future__ import annotations

import time

from newsbot import doctor


def make_cfg(tmp_path, **overrides):
    from newsbot.config import load_config

    env = {
        "BOT_TOKEN": "123456:TEST",
        "CHANNEL_ID": "@test_channel",
        "ADMIN_ID": "123456789",
        "STATE_FILE": str(tmp_path / "state.json"),
        "LEGACY_POSTED_FILE": str(tmp_path / "none1"),
        "LEGACY_DIGEST_FILE": str(tmp_path / "none2"),
        "GLOSSARY_FILE": str(tmp_path / "none3"),
        "TRANSLATE_CHAIN": "google,mymemory",
    }
    env.update(overrides)
    return load_config(env)


class FakeTg:
    def __init__(self, me=None, chat=None, member=None):
        self._me = me
        self._chat = chat
        self._member = member

    def get_me(self):
        return self._me

    def get_chat(self, chat_id):
        return self._chat

    def get_chat_member(self, chat_id, user_id):
        return self._member


def test_config_check_reports_warnings(tmp_path):
    cfg = make_cfg(tmp_path, MAX_POSTS_PER_RUN="много")
    checks = doctor.check_config(cfg)
    assert checks[0].status == doctor.WARN
    assert "MAX_POSTS_PER_RUN" in checks[0].hint


def test_config_check_ok_without_warnings(tmp_path):
    cfg = make_cfg(tmp_path)
    checks = doctor.check_config(cfg)
    assert checks[0].status == doctor.OK


def test_telegram_check_fails_without_network(tmp_path):
    cfg = make_cfg(tmp_path)
    checks = doctor.check_telegram(cfg, FakeTg(me=None))
    assert checks[0].status == doctor.FAIL
    assert "BOT_TOKEN" in checks[0].hint


def test_channel_check_detects_missing_rights(tmp_path):
    cfg = make_cfg(tmp_path)
    tg = FakeTg(me={"id": 42, "username": "bot"}, chat={"title": "Канал"},
                member={"status": "administrator", "can_post_messages": False})
    checks = doctor.check_channel(cfg, tg, 42)
    assert checks[0].status == doctor.OK
    assert checks[1].status == doctor.FAIL
    assert "публикации" in checks[1].detail


def test_channel_check_missing_chat(tmp_path):
    cfg = make_cfg(tmp_path)
    checks = doctor.check_channel(cfg, FakeTg(me={"id": 42}), 42)
    assert checks[0].status == doctor.FAIL


def test_channel_check_ok_for_administrator(tmp_path):
    cfg = make_cfg(tmp_path)
    tg = FakeTg(me={"id": 42}, chat={"title": "Канал"},
                member={"status": "creator"})
    checks = doctor.check_channel(cfg, tg, 42)
    assert all(check.status == doctor.OK for check in checks)


def test_providers_only_public_gives_warning(tmp_path):
    cfg = make_cfg(tmp_path)
    checks = doctor.check_providers(cfg)
    assert checks[0].status == doctor.WARN
    assert "google" in checks[0].detail


def test_providers_no_chain_fails(tmp_path):
    cfg = make_cfg(tmp_path, TRANSLATE_CHAIN="llm")
    checks = doctor.check_providers(cfg)
    with_key = cfg.with_overrides(openai_api_key="sk-test")
    assert doctor.check_providers(with_key)[0].status == doctor.OK
    assert checks[0].status == doctor.FAIL


def test_state_check_writes_file_status(tmp_path):
    from newsbot.state import State

    cfg = make_cfg(tmp_path)
    state = State.load(cfg, time.time())
    checks = doctor.check_state(cfg, state)
    names = [check.name for check in checks]
    assert any("файл" in name for name in names)
    assert all(check.status == doctor.OK for check in checks)


def test_state_check_reports_pause(tmp_path):
    from newsbot.state import State

    cfg = make_cfg(tmp_path)
    now = time.time()
    state = State.load(cfg, now)
    state.pause(now + 3600)
    checks = doctor.check_state(cfg, state, now)
    pause = [check for check in checks if check.name == "Пауза"]
    assert pause and pause[0].status == doctor.WARN


def test_schedule_quiet_hours(tmp_path):
    from datetime import datetime

    from newsbot.util import MSK

    cfg = make_cfg(tmp_path, QUIET_HOURS="23-8")
    night = datetime(2026, 10, 20, 2, 0, tzinfo=MSK).timestamp()
    checks = doctor.check_schedule(cfg, night)
    quiet = [check for check in checks if check.name == "Тихие часы"][0]
    assert quiet.status == doctor.WARN
    assert "08:00" in quiet.detail


def test_schedule_off_and_recap(tmp_path):
    cfg = make_cfg(tmp_path, QUIET_HOURS="off", DIGEST_HOURS="off",
                   WEEKLY_RECAP="1", WEEKLY_RECAP_DAY="6", WEEKLY_RECAP_HOUR="12")
    checks = doctor.check_schedule(cfg, time.time())
    names = {check.name: check for check in checks}
    assert names["Тихие часы"].status == doctor.OK
    assert names["Дайджесты"].status == doctor.WARN
    assert "вс" in names["Недельный обзор"].detail


def test_render_and_summary(tmp_path):
    checks = [
        doctor.Check("A", doctor.OK, "всё хорошо"),
        doctor.Check("B", doctor.WARN, "мелочь", "подсказка"),
        doctor.Check("C", doctor.FAIL, "сломано", "починить"),
    ]
    assert doctor.summarize(checks) == {doctor.OK: 1, doctor.WARN: 1, doctor.FAIL: 1}
    text = doctor.render(checks)
    assert "✅ 1" in text and "⚠️ 1" in text and "❌ 1" in text
    assert "Критические проблемы" in text and "C: сломано" in text
    assert doctor.render(checks[:1]).endswith("Всё готово к запуску.")
    assert "Замечания выше" in doctor.render(checks[:2])


def test_run_does_not_crash_without_network(tmp_path):
    cfg = make_cfg(tmp_path)
    checks = doctor.run(
        cfg,
        tg=FakeTg(me={"id": 1, "username": "bot"}, chat={"title": "Канал"},
                  member={"status": "administrator"}),
        now=time.time(),
        with_feeds=False,
    )
    assert any(check.name == "Состояние (файл)" for check in checks)
    assert not any(check.name == "Ленты" for check in checks)


def test_feeds_check_marks_failures(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)

    class FakeSession:
        pass

    def fake_fetch(session, url):
        if "bad" in url:
            return False, 0, "HTTP 500"
        return True, 3, ""

    monkeypatch.setattr(doctor, "_fetch_feed", fake_fetch)
    feed_cfg = cfg.with_overrides(
        feeds=(
            cfg.feeds[0].__class__("Good", "https://good.example/rss"),
            cfg.feeds[0].__class__("Good2", "https://good2.example/rss"),
            cfg.feeds[0].__class__("Bad", "https://bad.example/rss"),
        )
    )
    checks = doctor.check_feeds(feed_cfg, FakeSession())
    assert checks[0].status == doctor.WARN
    assert "Bad" in checks[0].hint


def test_report_for_admin_returns_text(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(doctor, "run", lambda *a, **k: [doctor.Check("X", doctor.OK, "ok")])
    text = doctor.report_for_admin(cfg)
    assert "Проверка news-bot" in text
