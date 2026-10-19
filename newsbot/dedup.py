"""Кластеризация сюжетов.

Одна новость = один кластер. Внутри кластера — ссылки разных источников.
Сравнение идёт по переведённым заголовкам (чтобы русские и английские
источники встречались в одном пространстве), по containment/Jaccard
токенов и, если включено, по эмбеддингам.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .state import State
from .text import (
    has_negation,
    is_opinion,
    norm_tokens,
    numbers_in,
    same_event,
    similarity,
)
from .util import collapse_ws, stable_hash

log = logging.getLogger("news-bot.dedup")

MAX_LINKS_PER_CLUSTER = 8


@dataclass
class Match:
    cluster_id: str
    score: float
    shared: int
    is_update: bool = True


@dataclass
class Candidate:
    url: str
    canonical: str
    source: str
    title: str
    title_ru: str
    summary: str
    published_ts: float
    category: str = ""
    cats: list[tuple[str, float]] = field(default_factory=list)
    image: str = ""
    video: str = ""
    breaking: bool = False
    opinion: bool = False
    score: float = 0.0
    tokens: frozenset[str] = field(default_factory=frozenset)
    embedding: list[float] | None = None

    def prepare(self) -> "Candidate":
        self.title = collapse_ws(self.title)
        self.title_ru = collapse_ws(self.title_ru) or self.title
        base = self.title_ru if self.title_ru and not self.title_ru.isascii() else self.title
        self.tokens = norm_tokens(base)
        if not self.tokens:
            self.tokens = norm_tokens(f"{self.title} {self.title_ru}")
        return self


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class Deduper:
    def __init__(self, cfg: Config, state: State, now: float | None = None,
                 embedder: Callable[[list[str]], list[list[float]] | None] | None = None):
        self.cfg = cfg
        self.state = state
        self.now = time.time() if now is None else now
        self.embedder = embedder
        self._token_cache: dict[str, frozenset[str]] = {}
        self._cluster_tokens: dict[str, list[tuple[frozenset[str], str]]] = {}

    # ------------------------------------------------------------------
    # Токены / эмбеддинги
    # ------------------------------------------------------------------

    def tokens_for(self, text: str) -> frozenset[str]:
        text = text or ""
        cached = self._token_cache.get(text)
        if cached is None:
            cached = norm_tokens(text)
            if len(self._token_cache) > 5000:
                self._token_cache.clear()
            self._token_cache[text] = cached
        return cached

    def _cluster_variants(self, cluster: dict) -> list[tuple[frozenset[str], str]]:
        cid = str(cluster.get("id", ""))
        cached = self._cluster_tokens.get(cid)
        if cached is not None:
            return cached
        variants: list[str] = []
        if cluster.get("title_ru"):
            variants.append(str(cluster["title_ru"]))
        variants.append(str(cluster.get("title", "")))
        for link in (cluster.get("links") or [])[-3:]:
            value = link.get("title_ru") or link.get("title")
            if value:
                variants.append(str(value))
        seen: list[tuple[frozenset[str], str]] = []
        known: set[frozenset[str]] = set()
        for text in variants:
            tokens = self.tokens_for(text)
            if tokens and tokens not in known:
                known.add(tokens)
                seen.append((tokens, text))
        self._cluster_tokens[cid] = seen
        return seen

    def _cluster_embedding(self, cluster: dict) -> list[float] | None:
        if not cluster.get("title_ru"):
            return None
        key = stable_hash("embed", self.cfg.embedding_model, str(cluster["title_ru"]), length=32)
        return self.state.embedding_get(key, self.now)

    # ------------------------------------------------------------------
    # Поиск кластера
    # ------------------------------------------------------------------

    def find(self, candidate: Candidate) -> Match | None:
        if not candidate.tokens and not candidate.embedding:
            return None
        window = self.cfg.title_window_hours * 3600
        best: Match | None = None
        best_rank = -1.0
        candidate_emb = candidate.embedding
        candidate_numbers = numbers_in(candidate.title_ru or candidate.title)

        for cid, cluster in self.state.clusters.items():
            updated = float(cluster.get("updated", 0))
            if updated and updated < self.now - window:
                continue
            title_score = 0.0
            shared = 0
            for tokens, text in self._cluster_variants(cluster):
                ok, score, inter = similarity(
                    candidate.tokens,
                    tokens,
                    containment=self.cfg.dedup_containment,
                    jaccard=self.cfg.dedup_jaccard,
                )
                if not ok and same_event(
                    candidate.tokens, tokens, candidate_numbers, numbers_in(text)
                ):
                    ok = True
                    score = max(score, 0.6)
                if ok and has_negation(candidate.tokens) != has_negation(tokens):
                    ok = False
                if ok and score > title_score:
                    title_score = score
                    shared = inter
            emb_score = 0.0
            if candidate_emb:
                cluster_emb = self._cluster_embedding(cluster)
                if cluster_emb:
                    emb_score = cosine(candidate_emb, cluster_emb)

            matched = title_score > 0 or emb_score >= self.cfg.embedding_min_similarity
            if not matched:
                continue
            # Если совпадение только по эмбеддингам, требуем высокий порог.
            if title_score == 0 and emb_score < self.cfg.embedding_min_similarity:
                continue
            rank = title_score * 2 + emb_score
            # Свежий и близкий по времени кластер предпочтительнее.
            rank += max(0.0, 1.0 - abs(self.now - updated) / max(1.0, window)) * 0.05
            if rank > best_rank:
                best_rank = rank
                best = Match(cid, round(max(title_score, emb_score), 3), shared, True)

        return best

    # ------------------------------------------------------------------
    # Создание / обновление кластера
    # ------------------------------------------------------------------

    def create(self, candidate: Candidate, force_id: str = "", register: bool = True) -> dict:
        cid = force_id or stable_hash("cluster", candidate.canonical or candidate.url, length=20)
        cluster = {
            "id": cid,
            "title": candidate.title,
            "title_ru": candidate.title_ru,
            "source": candidate.source,
            "category": candidate.category,
            "cats": [[c, float(s)] for c, s in candidate.cats],
            "published_ts": float(candidate.published_ts or self.now),
            "first_seen": float(self.now),
            "updated": float(self.now),
            "links": [],
            "message_ids": {},
            "updates": 0,
            "breaking": bool(candidate.breaking),
            "opinion": bool(candidate.opinion),
            "score": float(candidate.score),
            "size": 1,
            "media": {"image": candidate.image, "video": candidate.video},
        }
        self._append_link(cluster, candidate)
        if register:
            self.state.add_cluster(cluster)
        self._cluster_tokens.pop(cid, None)
        return cluster

    def attach(self, cluster: dict, candidate: Candidate) -> bool:
        """Добавляет источник в кластер. True, если это новая ссылка."""
        before = len(cluster.get("links") or [])
        added = self._append_link(cluster, candidate)
        cluster["updated"] = float(self.now)
        cluster["breaking"] = bool(cluster.get("breaking")) or bool(candidate.breaking)
        cluster["score"] = max(float(cluster.get("score", 0)), float(candidate.score))
        cluster["size"] = int(cluster.get("size", before)) + (1 if added else 0)
        if not cluster.get("category") and candidate.category:
            cluster["category"] = candidate.category
        if not cluster.get("title_ru") and candidate.title_ru:
            cluster["title_ru"] = candidate.title_ru
        if candidate.published_ts and (
            not cluster.get("published_ts")
            or candidate.published_ts < float(cluster["published_ts"])
        ):
            cluster["published_ts"] = float(candidate.published_ts)
        if not (cluster.get("media") or {}).get("image") and candidate.image:
            cluster.setdefault("media", {})["image"] = candidate.image
        if not (cluster.get("media") or {}).get("video") and candidate.video:
            cluster.setdefault("media", {})["video"] = candidate.video
        self._cluster_tokens.pop(str(cluster.get("id")), None)
        return added

    def _append_link(self, cluster: dict, candidate: Candidate) -> bool:
        links = cluster.setdefault("links", [])
        for link in links:
            if link.get("canonical") == candidate.canonical:
                return False
        links.append({
            "url": candidate.url,
            "canonical": candidate.canonical,
            "source": candidate.source,
            "title": candidate.title,
            "title_ru": candidate.title_ru,
            "summary": candidate.summary,
            "ts": float(candidate.published_ts or self.now),
        })
        if len(links) > MAX_LINKS_PER_CLUSTER:
            # Оставляем первую ссылку (она — «якорь») и самые свежие.
            cluster["links"] = [links[0]] + links[-(MAX_LINKS_PER_CLUSTER - 1):]
        return True

    # ------------------------------------------------------------------
    # Подтверждение двумя источниками
    # ------------------------------------------------------------------

    @staticmethod
    def distinct_sources(cluster: dict) -> int:
        return len({
            str(link.get("source", ""))
            for link in (cluster.get("links") or [])
            if link.get("source")
        })

    @classmethod
    def confirmed(cls, cluster: dict) -> bool:
        return cls.distinct_sources(cluster) >= 2

    # ------------------------------------------------------------------
    # Рендер
    # ------------------------------------------------------------------

    @classmethod
    def also_reported(cls, cluster: dict, limit: int = 3) -> list[str]:
        seen: list[str] = []
        primary = str(cluster.get("source", ""))
        for link in (cluster.get("links") or [])[1:]:
            source = str(link.get("source", ""))
            if source and source != primary and source not in seen:
                seen.append(source)
        return seen[:limit]

    @staticmethod
    def confirmation_text(cluster: dict) -> str:
        sources = Deduper.distinct_sources(cluster)
        if sources >= 2:
            return f"✅ подтверждено: {sources} источника"
        return "⚠️ один источник"


def dedup_labels(cfg: Config, cluster: dict) -> list[str]:
    labels: list[str] = []
    if cfg.confirmation_label:
        labels.append(Deduper.confirmation_text(cluster))
    if cfg.opinion_label and cluster.get("opinion"):
        labels.append("💬 мнение")
    return labels


def is_opinion_title(title: str, link: str = "", patterns: tuple[str, ...] = ()) -> bool:
    return is_opinion(title, link, patterns)
