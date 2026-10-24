from __future__ import annotations

import json
import time

from newsbot.state import State
from newsbot.util import canonical_url


def test_canonical_url_removes_tracking():
    url = "https://www.Example.com/news/1/?utm_source=rss&traffic_source=x&b=2&a=1#frag"
    assert canonical_url(url) == "https://example.com/news/1?a=1&b=2"


def test_save_load_roundtrip(cfg):
    state = State.load(cfg)
    state.posted[canonical_url("https://example.com/a")] = {
        "ts": int(time.time()), "url": "https://example.com/a", "source": "BBC World",
    }
    state.save()
    loaded = State.load(cfg)
    assert canonical_url("https://example.com/a") in loaded.posted


def test_counters_and_caps(cfg, now):
    state = State.load(cfg, now)
    record = {
        "ts": int(now),
        "url": "https://example.com/a",
        "canonical": "https://example.com/a",
        "source": "BBC World",
        "category": "мир",
    }
    state.add_posted(record, now)
    assert state.posts_today(now) == 1
    assert state.posts_today_by_source(now)["BBC World"] == 1
    assert state.source_cap_reached("BBC World", now) is False
    for i in range(cfg.max_per_source_per_day):
        rec = dict(record)
        rec["canonical"] = f"https://example.com/{i}"
        state.add_posted(rec, now)
    assert state.source_cap_reached("BBC World", now) is True


def test_feedback_replaces_vote(cfg, state, now):
    state.clusters["c1"] = {"id": "c1", "source": "BBC World", "updated": now}
    assert state.record_feedback("c1", "42", "up", now) == "ok"
    assert state.record_feedback("c1", "42", "down", now) == "ok"
    fb = state.data["feedback"]["c1"]
    assert fb["up"] == 0
    assert fb["down"] == 1
    assert state.record_feedback("c1", "42", "down", now) == "same"


def test_alert_cooldown(cfg, state, now):
    assert state.alert_allowed("feed-x", now, cooldown_hours=6)
    assert not state.alert_allowed("feed-x", now + 60, cooldown_hours=6)
    assert state.alert_allowed("feed-x", now + 7 * 3600, cooldown_hours=6)


def test_chat_id_not_persisted(cfg, state, now):
    """Идентификатор канала не должен утекать в публичный state.json."""
    record = {
        "ts": int(now),
        "url": "https://example.com/secret",
        "canonical": "https://example.com/secret",
        "source": "BBC World",
        "category": "мир",
        "chat_id": "-1001234567890",
    }
    state.add_posted(record, now)
    assert "chat_id" not in record

    state.posted["https://example.com/legacy"] = {
        "ts": int(now), "url": "https://example.com/legacy", "chat_id": "-1001234567890",
    }
    state.save(now)
    saved = json.load(open(cfg.state_file, encoding="utf-8"))
    assert all("chat_id" not in rec for rec in saved["posted"].values())


def test_prune_removes_old(cfg, state, now):
    old = now - (cfg.keep_days + 1) * 86400
    state.posted["https://example.com/old"] = {"ts": int(old), "source": "X"}
    state.clusters["old"] = {"id": "old", "updated": old}
    state.digest.append({"ts": int(old), "title": "old"})
    state.prune(now)
    assert "https://example.com/old" not in state.posted
    assert "old" not in state.clusters
    assert not state.digest


def test_legacy_migration(tmp_path, cfg, now):
    cfg = cfg.with_overrides(
        legacy_posted_file=str(tmp_path / "posted_news.txt"),
        legacy_digest_file=str(tmp_path / "digest.txt"),
        state_file=str(tmp_path / "state.json"),
    )
    with open(cfg.legacy_posted_file, "w", encoding="utf-8") as f:
        f.write(f"{int(now) - 100}|https://www.bbc.co.uk/news/1?at_medium=RSS|Big story\n")
    with open(cfg.legacy_digest_file, "w", encoding="utf-8") as f:
        f.write(f"#last=2026-10-19-8|{int(now) - 3600}\n")
        f.write(f"{int(now) - 2000}|BBC World|Большая новость|https://www.bbc.co.uk/news/1\n")
    state = State.load(cfg, now)
    assert canonical_url("https://www.bbc.co.uk/news/1") in state.posted
    assert state.digest_meta["slot"] == "2026-10-19T08"
    assert any(d["title"] == "Большая новость" for d in state.digest)


def test_atomic_json_is_valid(cfg, state):
    state.save()
    with open(cfg.state_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] >= 2
