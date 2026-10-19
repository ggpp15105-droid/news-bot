from __future__ import annotations

from datetime import datetime, timedelta

from newsbot.digest import DigestService
from newsbot.util import MSK


def add_cluster(state, cid: str, title: str, ts: float, source: str = "BBC World",
                score: float = 5.0) -> None:
    state.clusters[cid] = {
        "id": cid,
        "title": title,
        "title_ru": title,
        "source": source,
        "category": "мир",
        "links": [{"url": f"https://example.com/{cid}"}],
        "published_ts": ts,
        "updated": ts,
        "score": score,
        "size": 1,
    }
    state.add_digest_entry({
        "ts": int(ts),
        "source": source,
        "title": title,
        "url": f"https://example.com/{cid}",
        "cluster": cid,
        "category": "мир",
        "score": score,
    })


def test_exactly_two_digests_per_day(cfg, state):
    base = datetime(2026, 10, 19, 0, 0, tzinfo=MSK)
    titles = [
        "пожар на канадской военной базе",
        "нападение на дипломатов на западном берегу",
        "стрельба в школе в орегоне",
        "ураган в мексике",
        "протесты во франции",
        "выборы в израиле",
        "вспышка эболы в кении",
        "саммит по украине",
        "землетрясение в турции",
        "забастовка в германии",
    ]
    for i in range(30):
        ts = (base + timedelta(minutes=20 * i)).timestamp()
        add_cluster(state, f"c{i}", f"{titles[i % len(titles)]} подробности {i}", ts,
                    ["BBC World", "DW", "Lenta.ru"][i % 3])
    sends = []
    for hour, minute in [(8, 0), (8, 30), (9, 0), (19, 30), (20, 0), (20, 30),
                         (21, 0), (21, 30), (22, 0), (22, 30), (23, 0), (23, 30)]:
        now = datetime(2026, 10, 19, hour, minute, tzinfo=MSK)
        service = DigestService(cfg, state, now.timestamp())
        result = service.maybe_send(lambda chat, text: sends.append(text) or True)
        if result.sent:
            assert result.slot.endswith(("T08", "T20"))
    assert len(sends) == 2, sends
    assert "Доброе утро" in sends[0]
    assert "Добрый вечер" in sends[1]


def test_same_slot_not_resent(cfg, state, now):
    add_cluster(state, "c1", "крупный пожар на складе", now)
    service = DigestService(cfg, state, now)
    assert service.maybe_send(lambda chat, text: True).sent
    second = DigestService(cfg, state, now + 60)
    assert not second.maybe_send(lambda chat, text: True).sent
    assert second.maybe_send(lambda chat, text: True).reason == "слот уже отправлен"


def test_missed_slot_catch_up(cfg, state):
    # Пропустили вечерний слот, утром следующего дня отправляем один утренний.
    add_cluster(state, "c1", "новость про пожар", datetime(2026, 10, 19, 23, 0, tzinfo=MSK).timestamp())
    now = datetime(2026, 10, 20, 9, 0, tzinfo=MSK)
    service = DigestService(cfg, state, now.timestamp())
    sent = []
    result = service.maybe_send(lambda chat, text: sent.append(text) or True)
    assert result.sent
    assert len(sent) == 1
    assert "Доброе утро" in sent[0]


def test_no_news_closes_slot(cfg, state):
    now = datetime(2026, 10, 19, 8, 0, tzinfo=MSK)
    service = DigestService(cfg, state, now.timestamp())
    sent = []
    result = service.maybe_send(lambda chat, text: sent.append(text) or True)
    assert not result.sent
    assert result.reason == "нет новостей"
    assert state.digest_meta["slot"] == "2026-10-19T08"
    assert sent == []


def test_source_cap_in_digest(cfg, state):
    now = datetime(2026, 10, 19, 8, 0, tzinfo=MSK)
    base = now.timestamp()
    words = ["пожар", "нападение", "стрельба", "ураган", "протесты", "выборы"]
    for i in range(6):
        add_cluster(state, f"c{i}", f"{words[i]} подробности {i}",
                    base - 3600 + i, "BBC World")
    service = DigestService(cfg, state, now.timestamp())
    picked = service.select(base - 86400)
    assert len(picked) <= cfg.digest_max_per_source
    assert len(picked) == cfg.digest_max_per_source
