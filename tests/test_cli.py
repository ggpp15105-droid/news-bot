from __future__ import annotations

import pytest

from newsbot import cli

BASE_ENV = {
    "BOT_TOKEN": "123456:TEST",
    "CHANNEL_ID": "@test_channel",
    "ADMIN_ID": "123456789",
    "TRANSLATE_CHAIN": "google",
    "GLOSSARY_FILE": "/tmp/none-glossary.txt",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    values = dict(BASE_ENV)
    values["STATE_FILE"] = str(tmp_path / "state.json")
    values["LEGACY_POSTED_FILE"] = str(tmp_path / "none1")
    values["LEGACY_DIGEST_FILE"] = str(tmp_path / "none2")
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_parse_args_defaults():
    args = cli.parse_args([])
    assert not args.doctor and not args.dry_run and not args.digest
    assert args.post == ""


def test_parse_args_combined():
    args = cli.parse_args(["--dry-run", "--post", "https://example.com/a"])
    assert args.dry_run and args.post == "https://example.com/a"


def test_version_prints(capsys):
    assert cli.main(["--version"]) == 0
    assert "news-bot" in capsys.readouterr().out


def test_missing_config_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHANNEL_ID", raising=False)
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err and "CHANNEL_ID" in err


def test_dry_run_flag_overrides_config(env, monkeypatch):
    called = {}

    def fake_run_cli(cfg):
        called["dry_run"] = cfg.dry_run
        return 0

    monkeypatch.setattr("newsbot.doctor.run_cli", fake_run_cli)
    assert cli.main(["--doctor", "--dry-run"]) == 0
    assert called["dry_run"] is True


def test_doctor_exit_code(env, monkeypatch):
    monkeypatch.setattr("newsbot.doctor.run_cli", lambda cfg: 1)
    assert cli.main(["--doctor"]) == 1


def test_post_dry_run_prints_draft(env, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "1")

    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg

        def preview_post(self, url):
            return f"ЧЕРНОВИК {url}"

    monkeypatch.setattr("newsbot.bot.NewsBot", FakeBot)
    assert cli.main(["--post", "https://example.com/a"]) == 0
    assert "ЧЕРНОВИК https://example.com/a" in capsys.readouterr().out


def test_post_live_calls_bot(env, monkeypatch, capsys):
    calls = {}

    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg

        def post_url(self, url):
            calls["url"] = url
            return "✅ Опубликовано"

    monkeypatch.setattr("newsbot.bot.NewsBot", FakeBot)
    assert cli.main(["--post", "https://example.com/b"]) == 0
    assert calls["url"] == "https://example.com/b"
    assert "Опубликовано" in capsys.readouterr().out


def test_digest_dry_run_prints_text(env, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "1")

    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg

        def preview_digest(self, weekly=False):
            assert weekly is False
            return "<b>Дайджест</b>", 3

    monkeypatch.setattr("newsbot.bot.NewsBot", FakeBot)
    assert cli.main(["--digest"]) == 0
    out = capsys.readouterr().out
    assert "Дайджест" in out and "3 новостей" in out


def test_digest_empty(env, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "1")

    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg

        def preview_digest(self, weekly=False):
            return "", 0

    monkeypatch.setattr("newsbot.bot.NewsBot", FakeBot)
    assert cli.main(["--digest"]) == 0
    assert "Нет новостей" in capsys.readouterr().out


def test_recap_live(env, monkeypatch, capsys):
    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg

        def force_recap(self):
            return "✅ Недельный обзор отправлен"

    monkeypatch.setattr("newsbot.bot.NewsBot", FakeBot)
    assert cli.main(["--recap"]) == 0
    assert "обзор" in capsys.readouterr().out.lower()


def test_default_runs_bot(env, monkeypatch):
    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg

        def run(self):
            return 0

    monkeypatch.setattr("newsbot.bot.NewsBot", FakeBot)
    assert cli.main([]) == 0
