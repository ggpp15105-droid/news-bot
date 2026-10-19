from __future__ import annotations

import pytest

from newsbot.config import DEFAULT_FEEDS, ConfigError, load_config


def base_env(tmp_path):
    return {
        "BOT_TOKEN": "123456:TEST",
        "CHANNEL_ID": "@channel",
        "STATE_FILE": str(tmp_path / "state.json"),
        "LEGACY_POSTED_FILE": str(tmp_path / "none"),
        "LEGACY_DIGEST_FILE": str(tmp_path / "none"),
    }


def test_defaults(tmp_path):
    cfg = load_config(base_env(tmp_path))
    assert len(cfg.feeds) == len(DEFAULT_FEEDS)
    assert cfg.digest_hours == (8, 20)
    assert cfg.quiet_hours == (23, 8)
    assert cfg.max_posts_per_run == 2
    assert cfg.max_per_source_per_run == 1
    assert cfg.max_posts_per_day == 60


def test_missing_token_is_fatal(tmp_path):
    env = base_env(tmp_path)
    del env["BOT_TOKEN"]
    with pytest.raises(ConfigError):
        load_config(env)


def test_bad_channel(tmp_path):
    env = base_env(tmp_path)
    env["CHANNEL_ID"] = "world_1news_bot"
    with pytest.raises(ConfigError):
        load_config(env)


def test_quiet_hours_and_digest_hours(tmp_path):
    env = base_env(tmp_path)
    env["QUIET_HOURS"] = "22-7"
    env["DIGEST_HOURS"] = "9, 14, 21, 25, x"
    cfg = load_config(env)
    assert cfg.quiet_hours == (22, 7)
    assert cfg.digest_hours == (9, 14, 21)


def test_quiet_off(tmp_path):
    env = base_env(tmp_path)
    env["QUIET_HOURS"] = "off"
    assert load_config(env).quiet_hours is None


def test_extra_feeds(tmp_path):
    env = base_env(tmp_path)
    env["EXTRA_FEEDS"] = '{"My Feed": {"url": "https://example.com/rss", "category": "наука", "weight": 1.4}}'
    cfg = load_config(env)
    feed = cfg.feed_by_name("My Feed")
    assert feed is not None
    assert feed.category == "наука"
    assert feed.weight == 1.4


def test_disabled_feeds(tmp_path):
    env = base_env(tmp_path)
    env["DISABLED_FEEDS"] = "BBC Sport, Ведомости"
    cfg = load_config(env)
    assert cfg.feed_by_name("BBC Sport") is None
    assert cfg.feed_by_name("Ведомости") is None


def test_channel_routes(tmp_path):
    env = base_env(tmp_path)
    env["CHANNEL_ROUTES"] = '{"игры": "@games", "спорт": "-1001234567890"}'
    cfg = load_config(env)
    assert cfg.chat_for_category("игры") == "@games"
    assert cfg.chat_for_category("спорт") == "-1001234567890"
    assert cfg.chat_for_category("мир") == "@channel"


def test_bad_optional_number_defaults(tmp_path):
    env = base_env(tmp_path)
    env["MAX_POSTS_PER_RUN"] = "много"
    cfg = load_config(env)
    assert cfg.max_posts_per_run == 2
    assert any("MAX_POSTS_PER_RUN" in w for w in cfg.warnings)


def test_translate_chain_filters_unknown(tmp_path):
    env = base_env(tmp_path)
    env["TRANSLATE_CHAIN"] = "llm,unknown,google"
    cfg = load_config(env)
    assert cfg.translate_chain == ("llm", "google")
    assert any("TRANSLATE_CHAIN" in w for w in cfg.warnings)
