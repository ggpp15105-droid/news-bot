"""Состояние бота.

По умолчанию — один атомарно записываемый JSON-файл (``state.json``),
который workflow коммитит в репозиторий. Если заданы
``STATE_REDIS_URL``/``STATE_REDIS_TOKEN`` (Upstash REST), состояние
читается и пишется в Redis, а локальный файл остаётся резервной копией.

Формат намеренно простой: словари и списки, без бинарных файлов.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any

from .config import Config
from .util import (
    atomic_write_json,
    canonical_url,
    host_of,
    load_json,
    msk_date,
    stable_hash,
)

log = logging.getLogger("news-bot.state")

SCHEMA_VERSION = 2
MAX_CLUSTERS = 3000
MAX_DIGEST_ENTRIES = 6000
MAX_ALERTS = 300
MAX_FEEDBACK_USERS = 500


def _empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated": 0,
        "posted": {},
        "clusters": {},
        "digest": [],
        "digest_meta": {"slot": "", "last_ts": 0, "last_sent_ts": 0},
        "recap": {"last_week": ""},
        "feeds": {},
        "counters": {"date": "", "posts": 0, "per_source": {}, "per_category": {}},
        "alerts": {},
        "translations": {},
        "embeddings": {},
        "feedback": {},
        "admin": {"offset": 0, "paused_until": 0, "disabled_feeds": []},
        "stats": {
            "runs": 0,
            "last_run_ts": 0,
            "last_post_ts": 0,
            "last_digest_ts": 0,
            "last_success_ts": 0,
            "translation_providers": {},
        },
        "migrated_from_legacy": False,
    }


class RedisRestBackend:
    """Минимальный клиент Upstash Redis REST (GET/SET)."""

    def __init__(self, url: str, token: str, key: str, timeout: int = 10):
        self.url = url.rstrip("/")
        self.token = token
        self.key = key
        self.timeout = timeout
        self.session = None
        try:
            import requests
            self.session = requests.Session()
        except Exception:  # pragma: no cover - requests есть в зависимостях
            self.session = None

    @property
    def available(self) -> bool:
        return bool(self.url and self.token and self.session)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def load(self) -> dict[str, Any] | None:
        if not self.available:
            return None
        try:
            r = self.session.get(
                f"{self.url}/get/{self.key}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            if not r.ok:
                log.warning("Redis GET: HTTP %s", r.status_code)
                return None
            data = r.json()
            raw = data.get("result")
            if not raw:
                return None
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            log.warning("Redis GET: %s", exc)
            return None

    def save(self, data: dict[str, Any]) -> bool:
        if not self.available:
            return False
        try:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            r = self.session.post(
                f"{self.url}/set/{self.key}",
                headers=self._headers(),
                data=raw.encode("utf-8"),
                timeout=self.timeout,
            )
            if not r.ok:
                log.warning("Redis SET: HTTP %s", r.status_code)
                return False
            return True
        except Exception as exc:
            log.warning("Redis SET: %s", exc)
            return False


class State:
    def __init__(self, cfg: Config, data: dict[str, Any] | None = None):
        self.cfg = cfg
        self.data = copy.deepcopy(data) if data else _empty_state()
        self.redis = RedisRestBackend(
            cfg.state_redis_url, cfg.state_redis_token, cfg.state_redis_key
        )
        self._ensure()

    # ------------------------------------------------------------------
    # Загрузка / сохранение
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, cfg: Config, now: float | None = None) -> "State":
        now = time.time() if now is None else now
        data: dict[str, Any] | None = None
        redis = RedisRestBackend(cfg.state_redis_url, cfg.state_redis_token, cfg.state_redis_key)
        if redis.available:
            data = redis.load()
            if data:
                log.info("Состояние загружено из Redis")
        if not data:
            data = load_json(cfg.state_file, None)
            if data:
                log.info("Состояние загружено из %s", cfg.state_file)
        if not data:
            data = _empty_state()
            if cls._migrate_legacy(data, cfg, now):
                log.info("Импортировано состояние из legacy-файлов")
        state = cls(cfg, data)
        if state.data.get("version", 1) < SCHEMA_VERSION:
            state.data["version"] = SCHEMA_VERSION
        state.prune(now)
        return state

    def save(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.prune(now)
        self.data["updated"] = int(now)
        atomic_write_json(self.cfg.state_file, self.data)
        if self.redis.available and not self.redis.save(self.data):
            log.warning("Состояние сохранено локально, Redis недоступен")

    def _ensure(self) -> None:
        defaults = _empty_state()
        for key, value in defaults.items():
            if key not in self.data or not isinstance(self.data[key], type(value)):
                if isinstance(value, dict) and isinstance(self.data.get(key), dict):
                    continue
                if key not in self.data:
                    self.data[key] = copy.deepcopy(value)
        # Добавляем недостающие поля во вложенные словари.
        for section in ("digest_meta", "recap", "counters", "admin", "stats"):
            base = defaults[section]
            current = self.data.setdefault(section, {})
            if not isinstance(current, dict):
                self.data[section] = copy.deepcopy(base)
                continue
            for key, value in base.items():
                current.setdefault(key, copy.deepcopy(value))

    # ------------------------------------------------------------------
    # Legacy-миграция
    # ------------------------------------------------------------------

    @staticmethod
    def _source_for_url(cfg: Config, url: str) -> str:
        host = host_of(url)
        best = ""
        for feed in cfg.feeds:
            fhost = host_of(feed.url)
            if fhost and (host == fhost or host.endswith("." + fhost)):
                if len(fhost) > len(best):
                    best = fhost
                    source = feed.name
        if best:
            return source
        return host

    @classmethod
    def _migrate_legacy(cls, data: dict[str, Any], cfg: Config, now: float) -> bool:
        changed = False
        cutoff = now - cfg.keep_days * 86400
        posted = data.setdefault("posted", {})
        try:
            with open(cfg.legacy_posted_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("|", 2)
                    if len(parts) == 3 and parts[0].isdigit():
                        stamp, url, title = int(parts[0]), parts[1], parts[2]
                    else:
                        stamp, url, title = int(now), line, ""
                    if stamp < cutoff or not url:
                        continue
                    canon = canonical_url(url)
                    if not canon:
                        continue
                    posted.setdefault(canon, {
                        "ts": stamp,
                        "url": url,
                        "title": title,
                        "title_ru": "",
                        "source": cls._source_for_url(cfg, url),
                        "category": "",
                        "cluster": "",
                        "message_id": None,
                        "chat_id": "",
                        "kind": "",
                        "legacy": True,
                    })
                    changed = True
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Не удалось импортировать %s: %s", cfg.legacy_posted_file, exc)

        digest = data.setdefault("digest", [])
        marker_slot = ""
        marker_ts = 0
        try:
            with open(cfg.legacy_digest_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#last="):
                        raw = line[len("#last="):]
                        if "|" in raw:
                            slot, stamp = raw.split("|", 1)
                            marker_slot = slot
                            try:
                                marker_ts = int(float(stamp))
                            except ValueError:
                                marker_ts = 0
                        continue
                    parts = line.split("|", 3)
                    if len(parts) != 4 or not parts[0].isdigit():
                        continue
                    stamp = int(parts[0])
                    if stamp < cutoff:
                        continue
                    digest.append({
                        "ts": stamp,
                        "source": parts[1],
                        "title": parts[2],
                        "url": parts[3],
                        "cluster": "",
                        "category": "",
                        "score": 0.0,
                        "legacy": True,
                    })
                    changed = True
            if digest:
                digest.sort(key=lambda x: int(x.get("ts", 0)))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Не удалось импортировать %s: %s", cfg.legacy_digest_file, exc)

        if marker_slot:
            # Старый формат "2026-10-19-20" → "2026-10-19T20".
            try:
                date, hour = marker_slot.rsplit("-", 1)
                slot = f"{date}T{int(hour):02d}"
            except ValueError:
                slot = marker_slot
            data["digest_meta"] = {
                "slot": slot,
                "last_ts": marker_ts or int(now),
                "last_sent_ts": marker_ts or int(now),
            }
        data["migrated_from_legacy"] = changed
        return changed

    # ------------------------------------------------------------------
    # Prune
    # ------------------------------------------------------------------

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.cfg.keep_days * 86400

        posted: dict[str, dict] = self.data.get("posted", {})
        kept = {k: v for k, v in posted.items() if int(v.get("ts", 0)) >= cutoff}
        if len(kept) > self.cfg.max_state_posts:
            newest = sorted(kept.items(), key=lambda kv: int(kv[1].get("ts", 0)), reverse=True)
            kept = dict(newest[: self.cfg.max_state_posts])
        self.data["posted"] = kept

        clusters: dict[str, dict] = self.data.get("clusters", {})
        kept_clusters = {
            k: v for k, v in clusters.items() if int(v.get("updated", 0)) >= cutoff
        }
        if len(kept_clusters) > MAX_CLUSTERS:
            newest = sorted(
                kept_clusters.items(), key=lambda kv: int(kv[1].get("updated", 0)), reverse=True
            )
            kept_clusters = dict(newest[:MAX_CLUSTERS])
        self.data["clusters"] = kept_clusters

        digest = [d for d in self.data.get("digest", []) if int(d.get("ts", 0)) >= cutoff]
        if len(digest) > MAX_DIGEST_ENTRIES:
            digest = digest[-MAX_DIGEST_ENTRIES:]
        digest.sort(key=lambda x: int(x.get("ts", 0)))
        self.data["digest"] = digest

        translations: dict[str, dict] = self.data.get("translations", {})
        if len(translations) > self.cfg.translate_cache_size:
            ordered = sorted(
                translations.items(),
                key=lambda kv: int(kv[1].get("used", kv[1].get("ts", 0))),
                reverse=True,
            )
            translations = dict(ordered[: self.cfg.translate_cache_size])
        self.data["translations"] = translations

        embeddings: dict[str, dict] = self.data.get("embeddings", {})
        if len(embeddings) > self.cfg.embedding_cache_size:
            ordered = sorted(
                embeddings.items(),
                key=lambda kv: int(kv[1].get("used", kv[1].get("ts", 0))),
                reverse=True,
            )
            embeddings = dict(ordered[: self.cfg.embedding_cache_size])
        self.data["embeddings"] = embeddings

        alerts: dict[str, int] = self.data.get("alerts", {})
        if len(alerts) > MAX_ALERTS:
            ordered = sorted(alerts.items(), key=lambda kv: int(kv[1]), reverse=True)
            alerts = dict(ordered[:MAX_ALERTS])
        self.data["alerts"] = {
            k: int(v) for k, v in alerts.items() if int(v) >= now - 90 * 86400
        }

        feedback: dict[str, dict] = self.data.get("feedback", {})
        kept_feedback = {}
        for cid, fb in feedback.items():
            if cid not in kept_clusters:
                continue
            if int(fb.get("ts", 0)) < cutoff:
                continue
            users = fb.get("users", {})
            if isinstance(users, dict) and len(users) > MAX_FEEDBACK_USERS:
                fb["users"] = dict(list(users.items())[-MAX_FEEDBACK_USERS:])
            kept_feedback[cid] = fb
        self.data["feedback"] = kept_feedback

    # ------------------------------------------------------------------
    # Доступ к данным
    # ------------------------------------------------------------------

    @property
    def posted(self) -> dict[str, dict]:
        return self.data.setdefault("posted", {})

    @property
    def clusters(self) -> dict[str, dict]:
        return self.data.setdefault("clusters", {})

    @property
    def digest(self) -> list[dict]:
        return self.data.setdefault("digest", [])

    @property
    def digest_meta(self) -> dict:
        return self.data.setdefault("digest_meta", {})

    @property
    def feeds(self) -> dict[str, dict]:
        return self.data.setdefault("feeds", {})

    @property
    def counters(self) -> dict:
        return self.data.setdefault("counters", {})

    @property
    def admin(self) -> dict:
        return self.data.setdefault("admin", {})

    @property
    def stats(self) -> dict:
        return self.data.setdefault("stats", {})

    def counter_today(self, now: float) -> dict:
        today = msk_date(datetime_from_ts(now))
        counters = self.counters
        if counters.get("date") != today:
            counters["date"] = today
            counters["posts"] = 0
            counters["per_source"] = {}
            counters["per_category"] = {}
        return counters

    def posts_today(self, now: float) -> int:
        return int(self.counter_today(now).get("posts", 0))

    def posts_today_by_source(self, now: float) -> dict[str, int]:
        return dict(self.counter_today(now).get("per_source", {}))

    def source_cap_reached(self, source: str, now: float) -> bool:
        if self.cfg.max_per_source_per_day <= 0:
            return False
        return self.posts_today_by_source(now).get(source, 0) >= self.cfg.max_per_source_per_day

    def add_posted(self, record: dict, now: float) -> None:
        canon = record.get("canonical") or canonical_url(record.get("url", ""))
        record["ts"] = int(record.get("ts", now))
        self.posted[canon] = record
        counters = self.counter_today(now)
        counters["posts"] = int(counters.get("posts", 0)) + 1
        per_source = counters.setdefault("per_source", {})
        per_source[record.get("source", "")] = per_source.get(record.get("source", ""), 0) + 1
        per_category = counters.setdefault("per_category", {})
        cat = record.get("category", "")
        if cat:
            per_category[cat] = per_category.get(cat, 0) + 1
        self.stats["last_post_ts"] = int(now)
        self.stats["last_success_ts"] = int(now)

    def add_digest_entry(self, record: dict) -> None:
        self.digest.append(record)

    def add_cluster(self, cluster: dict) -> None:
        self.clusters[cluster["id"]] = cluster

    def recent_posted(self, now: float, window_hours: int) -> list[dict]:
        cutoff = now - window_hours * 3600
        return [r for r in self.posted.values() if int(r.get("ts", 0)) >= cutoff]

    def alert_allowed(self, key: str, now: float, cooldown_hours: int | None = None) -> bool:
        cooldown = self.cfg.alert_cooldown_hours if cooldown_hours is None else cooldown_hours
        last = int(self.data.setdefault("alerts", {}).get(key, 0))
        if last and now - last < cooldown * 3600:
            return False
        self.data["alerts"][key] = int(now)
        return True

    def pause(self, until: float) -> None:
        self.admin["paused_until"] = int(until)

    def resume(self) -> None:
        self.admin["paused_until"] = 0

    def paused(self, now: float) -> bool:
        return int(self.admin.get("paused_until", 0)) > now

    # ------------------------------------------------------------------
    # Кэш переводов и эмбеддингов
    # ------------------------------------------------------------------

    @staticmethod
    def cache_key(text: str, mode: str, provider_version: str) -> str:
        return stable_hash(provider_version, mode, text, length=32)

    def translation_get(self, key: str, now: float) -> str | None:
        entry = self.data.setdefault("translations", {}).get(key)
        if not isinstance(entry, dict):
            return None
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        entry["used"] = int(now)
        return text

    def translation_set(self, key: str, text: str, now: float, provider: str) -> None:
        self.data.setdefault("translations", {})[key] = {
            "text": text,
            "ts": int(now),
            "used": int(now),
            "provider": provider,
        }

    def embedding_get(self, key: str, now: float) -> list[float] | None:
        entry = self.data.setdefault("embeddings", {}).get(key)
        if not isinstance(entry, dict):
            return None
        vec = entry.get("v")
        if not isinstance(vec, list) or not vec:
            return None
        entry["used"] = int(now)
        return [float(x) for x in vec]

    def embedding_set(self, key: str, vec: list[float], now: float) -> None:
        self.data.setdefault("embeddings", {})[key] = {
            "v": [round(float(x), 6) for x in vec],
            "ts": int(now),
            "used": int(now),
        }

    def record_provider(self, provider: str, ok: bool) -> None:
        providers = self.stats.setdefault("translation_providers", {})
        entry = providers.setdefault(provider, {"ok": 0, "fail": 0})
        entry["ok" if ok else "fail"] = int(entry.get("ok" if ok else "fail", 0)) + 1

    # ------------------------------------------------------------------
    # Обратная связь
    # ------------------------------------------------------------------

    def record_feedback(self, cluster_id: str, user_id: str, vote: str, now: float,
                        source: str = "") -> str:
        if vote not in {"up", "down"}:
            return "ignored"
        fb = self.data.setdefault("feedback", {}).setdefault(
            cluster_id, {"up": 0, "down": 0, "users": {}, "ts": int(now), "source": source}
        )
        users = fb.setdefault("users", {})
        old = users.get(str(user_id))
        if old == vote:
            return "same"
        if old in {"up", "down"}:
            fb[old] = max(0, int(fb.get(old, 0)) - 1)
        fb[vote] = int(fb.get(vote, 0)) + 1
        users[str(user_id)] = vote
        fb["ts"] = int(now)
        if source:
            fb["source"] = source
        return "ok"

    def feedback_by_source(self) -> dict[str, float]:
        """Нормализованный рейтинг источников: -1..1."""
        totals: dict[str, list[int]] = {}
        for cid, fb in self.data.get("feedback", {}).items():
            source = fb.get("source") or self.clusters.get(cid, {}).get("source", "")
            if not source:
                continue
            up = int(fb.get("up", 0))
            down = int(fb.get("down", 0))
            agg = totals.setdefault(source, [0, 0])
            agg[0] += up
            agg[1] += down
        out: dict[str, float] = {}
        for source, (up, down) in totals.items():
            out[source] = (up - down) / max(1, up + down)
        return out

    # ------------------------------------------------------------------
    # Админ-состояние
    # ------------------------------------------------------------------

    def disabled_feeds(self) -> set[str]:
        return {str(x) for x in self.admin.get("disabled_feeds", [])}

    def set_feed_enabled(self, name: str, enabled: bool) -> None:
        disabled = self.disabled_feeds()
        if enabled:
            disabled.discard(name.lower())
        else:
            disabled.add(name.lower())
        self.admin["disabled_feeds"] = sorted(disabled)


def datetime_from_ts(value: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, timezone.utc)


def source_for_url(cfg: Config, url: str) -> str:
    return State._source_for_url(cfg, url)
