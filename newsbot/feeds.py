"""Загрузка RSS, health-check лент и извлечение медиа."""

from __future__ import annotations

import calendar
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests

from .config import Config, Feed
from .state import State
from .text import clean_summary
from .util import clean_html, collapse_ws, is_http_url

log = logging.getLogger("news-bot.feeds")

_MAX_FEED_BYTES = 5 * 1024 * 1024
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


@dataclass
class RawItem:
    source: str
    link: str
    title: str
    summary: str
    published_ts: float
    image: str = ""
    video: str = ""
    feed_category: str = "мир"
    feed_weight: float = 1.0

    @property
    def key(self) -> str:
        return self.link


@dataclass
class FeedReport:
    items: list[RawItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    successes: int = 0
    failures: int = 0
    skipped: int = 0
    empty: int = 0


def _parse_time(entry: Any) -> float:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return float(calendar.timegm(value))
            except (TypeError, ValueError, OverflowError):
                continue
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            from datetime import timezone
            from email.utils import parsedate_to_datetime
            try:
                dt = parsedate_to_datetime(str(value))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                continue
    return time.time()


def _media_url(base: str, url: str) -> str:
    url = html_lib.unescape((url or "").strip())
    if not url:
        return ""
    resolved = urljoin(base, url) if base else url
    return resolved if is_http_url(resolved) else ""


def image_from_entry(entry: Any, base_url: str = "") -> str:
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if url:
                resolved = _media_url(base_url, url)
                if resolved:
                    return resolved
    for link in entry.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            resolved = _media_url(base_url, link.get("href", ""))
            if resolved:
                return resolved
    for key in ("summary", "description", "content"):
        value = entry.get(key)
        if isinstance(value, list) and value:
            value = value[0].get("value", "") if isinstance(value[0], dict) else ""
        m = _IMG_RE.search(str(value or ""))
        if m:
            resolved = _media_url(base_url, m.group(1))
            if resolved:
                return resolved
    return ""


def video_from_entry(entry: Any, base_url: str = "") -> str:
    for item in entry.get("media_content", []) or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        ctype = str(item.get("type", ""))
        if url and (ctype.startswith("video") or url.lower().endswith((".mp4", ".m3u8"))):
            resolved = _media_url(base_url, url)
            if resolved:
                return resolved
    for link in entry.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        url = link.get("href") or ""
        ctype = str(link.get("type", ""))
        if url and (ctype.startswith("video") or url.lower().endswith(".mp4")):
            resolved = _media_url(base_url, url)
            if resolved:
                return resolved
    return ""


def _entry_summary(entry: Any) -> str:
    value = entry.get("summary") or entry.get("description") or ""
    if not value:
        content = entry.get("content")
        if isinstance(content, list) and content:
            value = content[0].get("value", "") if isinstance(content[0], dict) else ""
    return clean_summary(str(value or ""))[:1000]


def fetch_feed(feed: Feed, session: requests.Session, cfg: Config,
               now: float | None = None) -> tuple[list[RawItem], str | None]:
    """Возвращает (items, error). error=None при успехе."""
    now = time.time() if now is None else now
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }
    try:
        response = session.get(feed.url, headers=headers, timeout=cfg.fetch_timeout)
    except Exception as exc:
        return [], f"сеть: {exc.__class__.__name__}: {exc}"[:240]
    if response.status_code != 200:
        return [], f"HTTP {response.status_code}"
    content = response.content
    if not content:
        return [], "пустой ответ"
    if len(content) > _MAX_FEED_BYTES:
        return [], "слишком большой ответ"

    parsed = feedparser.parse(content)
    entries = parsed.entries or []
    if not entries:
        if getattr(parsed, "bozo", 0):
            reason = f"ошибка разбора: {getattr(parsed, 'bozo_exception', '')}"[:180]
            return [], reason
        return [], None

    items: list[RawItem] = []
    seen: set[str] = set()
    for entry in entries[: cfg.max_per_source_fetch]:
        link = str(entry.get("link") or "").strip()
        title = clean_html(entry.get("title", ""))
        if not link or not title or not is_http_url(link):
            continue
        canonical = link
        if canonical in seen:
            continue
        seen.add(canonical)
        published = _parse_time(entry)
        if published > now + 86400:
            published = now
        items.append(RawItem(
            source=feed.name,
            link=link,
            title=collapse_ws(title),
            summary=_entry_summary(entry),
            published_ts=published,
            image=image_from_entry(entry, link),
            video=video_from_entry(entry, link),
            feed_category=feed.category,
            feed_weight=feed.weight,
        ))
    if not items:
        return [], "нет валидных записей"
    return items, None


class FeedFetcher:
    def __init__(self, cfg: Config, state: State, session: requests.Session,
                 now: float | None = None):
        self.cfg = cfg
        self.state = state
        self.session = session
        self.now = time.time() if now is None else now

    def _status(self, name: str) -> dict:
        return self.state.feeds.setdefault(name, {
            "last_ok": 0,
            "last_ts": 0,
            "errors": 0,
            "empty_streak": 0,
            "disabled_until": 0,
            "last_count": 0,
            "total_ok": 0,
            "total_fail": 0,
            "last_error": "",
        })

    def _is_disabled(self, feed: Feed, status: dict) -> bool:
        if feed.name.lower() in self.state.disabled_feeds():
            return True
        return float(status.get("disabled_until", 0)) > self.now

    def _record_failure(self, feed: Feed, error: str) -> str | None:
        status = self._status(feed.name)
        status["errors"] = int(status.get("errors", 0)) + 1
        status["total_fail"] = int(status.get("total_fail", 0)) + 1
        status["last_error"] = error[:240]
        status["last_ts"] = int(self.now)
        alert = None
        if int(status["errors"]) >= self.cfg.feed_disable_after_fails:
            status["disabled_until"] = int(self.now + self.cfg.feed_disable_hours * 3600)
            status["errors"] = 0
            alert = (
                f"Лента «{feed.name}» отключена на {self.cfg.feed_disable_hours} ч "
                f"после {self.cfg.feed_disable_after_fails} ошибок: {error[:120]}"
            )
            log.warning(alert)
        return alert

    def _record_success(self, feed: Feed, count: int) -> str | None:
        status = self._status(feed.name)
        if int(status.get("errors", 0)) > 0:
            log.info("Лента «%s» снова работает", feed.name)
        status["errors"] = 0
        status["last_ok"] = int(self.now)
        status["last_ts"] = int(self.now)
        status["last_count"] = int(count)
        status["total_ok"] = int(status.get("total_ok", 0)) + 1
        status["disabled_until"] = 0
        alert = None
        if count == 0:
            status["empty_streak"] = int(status.get("empty_streak", 0)) + 1
            if int(status["empty_streak"]) == self.cfg.feed_empty_alert_streak:
                alert = f"Лента «{feed.name}» {status['empty_streak']} запусков подряд пустая"
        else:
            status["empty_streak"] = 0
        return alert

    def fetch_all(self) -> FeedReport:
        report = FeedReport()
        for feed in self.cfg.feeds:
            status = self._status(feed.name)
            if self._is_disabled(feed, status):
                report.skipped += 1
                continue
            items, error = fetch_feed(feed, self.session, self.cfg, self.now)
            if error:
                report.failures += 1
                report.errors.append(f"{feed.name}: {error}")
                alert = self._record_failure(feed, error)
                if alert:
                    report.alerts.append(alert)
                continue
            report.successes += 1
            alert = self._record_success(feed, len(items))
            if alert:
                report.alerts.append(alert)
            if not items:
                report.empty += 1
                continue
            report.items.extend(items)
        log.info(
            "RSS: %d лент OK, %d ошибок, %d отключено, %d новостей",
            report.successes, report.failures, report.skipped, len(report.items),
        )
        return report

    def health_rows(self) -> list[tuple[str, dict]]:
        return [(feed.name, self._status(feed.name)) for feed in self.cfg.feeds]

    def disabled_now(self) -> list[str]:
        out = []
        for feed in self.cfg.feeds:
            status = self._status(feed.name)
            if self._is_disabled(feed, status):
                out.append(feed.name)
        return out
