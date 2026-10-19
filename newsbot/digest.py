"""Дайджесты и недельный обзор.

Главное отличие от старой версии: слот выбирается один раз — последний
наступивший. Повторная отправка того же слота невозможна, а очередь
дайджестов чистится по возрасту.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .config import Config
from .dedup import Deduper
from .state import State
from .text import norm_tokens, normalize_title_for_dedup
from .util import MSK, esc, msk_now, slot_dt, stable_hash

log = logging.getLogger("news-bot.digest")

Sender = Callable[[str, str], bool]


@dataclass
class DigestResult:
    sent: bool = False
    reason: str = ""
    slot: str = ""
    items: int = 0


def due_slot(cfg: Config, now: datetime) -> str | None:
    """Последний наступивший слот, который ещё не отправлен."""
    local = now.astimezone(MSK)
    due = [h for h in cfg.digest_hours if local.hour >= h]
    if not due:
        return None
    hour = max(due)
    slot = f"{local:%Y-%m-%d}T{hour:02d}"
    return slot


def _plural_sources(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} источник"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} источника"
    return f"{n} источников"


def _rank(entry: dict, cluster: dict | None, now: float) -> float:
    ts = float((cluster or {}).get("published_ts") or entry.get("ts", now))
    age_h = max(0.0, (now - ts) / 3600.0)
    base = float((cluster or {}).get("score", entry.get("score", 0.0)) or 0.0)
    return base + 1.4 * math.exp(-age_h / 20.0)


class DigestService:
    def __init__(self, cfg: Config, state: State, now: float | None = None):
        self.cfg = cfg
        self.state = state
        self.now = time.time() if now is None else now

    # ------------------------------------------------------------------
    # Очередь
    # ------------------------------------------------------------------

    def add_entry(self, cluster: dict, now: float | None = None) -> None:
        entry = {
            "ts": int(self.now if now is None else now),
            "source": cluster.get("source", ""),
            "title": cluster.get("title_ru") or cluster.get("title", ""),
            "url": "",
            "cluster": cluster.get("id", ""),
            "category": cluster.get("category", ""),
            "score": float(cluster.get("score", 0.0) or 0.0),
        }
        links = cluster.get("links") or []
        if links:
            entry["url"] = links[0].get("url", "")
        self.state.add_digest_entry(entry)

    def _candidate_entries(self, cutoff: float) -> list[dict]:
        latest: dict[str, dict] = {}
        for entry in self.state.digest:
            if int(entry.get("ts", 0)) < cutoff:
                continue
            cluster_id = str(entry.get("cluster") or "")
            key = cluster_id or stable_hash(
                "title", normalize_title_for_dedup(entry.get("title", "")), length=16
            )
            current = latest.get(key)
            if current is None or int(entry.get("ts", 0)) > int(current.get("ts", 0)):
                latest[key] = entry
        return list(latest.values())

    def select(self, cutoff: float, limit: int | None = None,
               max_per_source: int | None = None) -> list[dict]:
        limit = self.cfg.digest_items if limit is None else limit
        max_per_source = self.cfg.digest_max_per_source if max_per_source is None else max_per_source
        candidates = self._candidate_entries(cutoff)
        scored: list[tuple[float, dict, dict | None]] = []
        for entry in candidates:
            cluster = self.state.clusters.get(str(entry.get("cluster") or ""))
            if cluster and cluster.get("title_ru"):
                entry = dict(entry)
                entry["title"] = cluster["title_ru"]
                entry["url"] = entry.get("url") or (
                    (cluster.get("links") or [{}])[0].get("url", "")
                )
                entry["source"] = cluster.get("source", entry.get("source", ""))
            if not entry.get("title") or not entry.get("url"):
                continue
            scored.append((_rank(entry, cluster, self.now), entry, cluster))
        scored.sort(key=lambda x: (-x[0], -int(x[1].get("ts", 0))))

        picked: list[dict] = []
        per_source: dict[str, int] = {}
        seen_tokens: list[frozenset[str]] = []
        seen_clusters: set[str] = set()
        for _, entry, cluster in scored:
            source = str(entry.get("source", ""))
            if per_source.get(source, 0) >= max_per_source:
                continue
            cid = str(entry.get("cluster") or "")
            if cid and cid in seen_clusters:
                continue
            tokens = norm_tokens(entry.get("title", ""))
            if tokens and any(
                len(tokens & other) / max(1, min(len(tokens), len(other))) >= 0.8
                for other in seen_tokens
            ):
                continue
            picked.append({"entry": entry, "cluster": cluster})
            per_source[source] = per_source.get(source, 0) + 1
            if cid:
                seen_clusters.add(cid)
            if tokens:
                seen_tokens.append(tokens)
            if len(picked) >= limit:
                break
        return picked

    # ------------------------------------------------------------------
    # Рендер
    # ------------------------------------------------------------------

    def render(self, picked: list[dict], slot: str, weekly: bool = False) -> str:
        slot_time = slot_dt(slot) or msk_now()
        hour = slot_time.hour
        if weekly:
            header = "🗓 <b>Неделя в новостях: главное за 7 дней</b>"
        elif hour < 12:
            header = "☕ <b>Доброе утро! Главное за сутки:</b>"
        else:
            header = "🌆 <b>Добрый вечер! Главное за день:</b>"
        lines = [header, ""]
        for index, item in enumerate(picked, 1):
            entry = item["entry"]
            cluster = item.get("cluster") or {}
            title = esc(entry.get("title", ""))
            source = esc(entry.get("source", ""))
            url = esc(entry.get("url", ""), quote=True)
            extra = ""
            if cluster:
                others = Deduper.also_reported(cluster, 2)
                if others:
                    extra = " · " + esc(", ".join(others))
            lines.append(f"{index}. <b>{title}</b> — <a href=\"{url}\">{source}</a>{extra}")
        return "\n".join(lines)

    @staticmethod
    def _fit(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        # Убираем пункты с конца, чтобы не разорвать HTML.
        lines = text.split("\n")
        while lines and len("\n".join(lines)) > limit:
            index = None
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip() and lines[i].strip()[0].isdigit():
                    index = i
                    break
            if index is None:
                break
            lines.pop(index)
            if index < len(lines) and lines[index - 1].strip() == "":
                lines.pop(index - 1)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Отправка
    # ------------------------------------------------------------------

    def maybe_send(self, sender: Sender) -> DigestResult:
        cfg = self.cfg
        now_dt = datetime.fromtimestamp(self.now, MSK)
        slot = due_slot(cfg, now_dt)
        if not slot:
            return DigestResult(False, "нет наступившего слота")
        meta = self.state.digest_meta
        if meta.get("slot") == slot:
            return DigestResult(False, "слот уже отправлен", slot)
        slot_time = slot_dt(slot)
        if not slot_time:
            return DigestResult(False, "некорректный слот", slot)
        if meta.get("slot") and slot_time <= (slot_dt(str(meta["slot"])) or slot_time - timedelta(days=1)):
            return DigestResult(False, "слот раньше последнего", slot)

        # Утренний дайджест — за сутки, вечерний — с прошлого дайджеста.
        if slot_time.hour < 12:
            cutoff = self.now - 24 * 3600
        else:
            cutoff = float(meta.get("last_ts") or (self.now - cfg.digest_window_hours * 3600))
            cutoff = max(cutoff, self.now - 36 * 3600)

        picked = self.select(cutoff)
        if not picked:
            meta["slot"] = slot
            meta["last_ts"] = int(self.now)
            log.info("Дайджест %s: нет новостей, слот закрыт", slot)
            return DigestResult(False, "нет новостей", slot)

        text = self._fit(self.render(picked, slot), cfg.tg_limit)
        if sender(cfg.channel_id, text):
            meta["slot"] = slot
            meta["last_ts"] = int(self.now)
            meta["last_sent_ts"] = int(self.now)
            self.state.stats["last_digest_ts"] = int(self.now)
            log.info("Дайджест %s отправлен: %d новостей", slot, len(picked))
            return DigestResult(True, "", slot, len(picked))
        return DigestResult(False, "ошибка отправки", slot)

    def maybe_send_weekly(self, sender: Sender) -> DigestResult:
        cfg = self.cfg
        if not cfg.weekly_recap:
            return DigestResult(False, "weekly recap выключен")
        local = datetime.fromtimestamp(self.now, MSK)
        if local.weekday() != cfg.weekly_recap_day or local.hour < cfg.weekly_recap_hour:
            return DigestResult(False, "не время")
        iso = f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
        if self.state.data.get("recap", {}).get("last_week") == iso:
            return DigestResult(False, "уже отправлен")
        cutoff = self.now - 7 * 86400
        picked = self.select(cutoff, limit=cfg.weekly_recap_items,
                             max_per_source=max(1, cfg.digest_max_per_source))
        if not picked:
            self.state.data.setdefault("recap", {})["last_week"] = iso
            return DigestResult(False, "нет новостей")
        text = self._fit(self.render(picked, f"{local:%Y-%m-%d}T{cfg.weekly_recap_hour:02d}", weekly=True),
                         cfg.tg_limit)
        if sender(cfg.channel_id, text):
            self.state.data.setdefault("recap", {})["last_week"] = iso
            self.state.data.setdefault("recap", {})["last_ts"] = int(self.now)
            log.info("Недельный обзор отправлен: %d новостей", len(picked))
            return DigestResult(True, "", iso, len(picked))
        return DigestResult(False, "ошибка отправки", iso, len(picked))
