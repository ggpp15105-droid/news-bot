"""Загрузка статьи и медиа."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

try:  # trafilatura тяжёлый; без него используем og:description
    import trafilatura
except Exception:  # pragma: no cover
    trafilatura = None

from .config import Config
from .text import page_meta
from .util import clean_html, collapse_ws, download_limited, safe_get

log = logging.getLogger("news-bot.article")

_MAX_PAGE_BYTES = 8 * 1024 * 1024


@dataclass
class Article:
    body: str = ""
    image: str = ""
    video: str = ""
    title: str = ""
    description: str = ""
    status: int = 0
    error: str = ""
    final_url: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.body or self.image or self.video) and not self.error


def _decode(response: requests.Response) -> str:
    try:
        if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = response.apparent_encoding or "utf-8"
    except Exception:
        pass
    try:
        return response.text
    except Exception:
        return (response.content or b"").decode("utf-8", "replace")


def fetch_article(cfg: Config, session: requests.Session, url: str) -> Article:
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }
    response = safe_get(session, url, timeout=cfg.fetch_timeout, headers=headers)
    if response is None:
        return Article(error="URL недоступен или не публичный")
    try:
        if not response.ok:
            return Article(status=response.status_code, error=f"HTTP {response.status_code}")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > _MAX_PAGE_BYTES:
            return Article(error="слишком большая страница")
        content = response.content or b""
        if len(content) > _MAX_PAGE_BYTES:
            return Article(error="слишком большая страница")
        page = _decode(response)
        final_url = response.url or url
    finally:
        try:
            response.close()
        except Exception:
            pass

    meta = page_meta(page, final_url)
    body = ""
    if trafilatura is not None:
        try:
            body = trafilatura.extract(
                page,
                include_comments=False,
                include_tables=False,
                include_links=False,
            ) or ""
        except Exception as exc:
            log.warning("trafilatura: %s", exc)
    body = collapse_ws(body) if "\n" not in (body or "") else body
    body = (body or "").strip()[:25000]
    description = clean_html(meta.get("description", ""))[:2000]
    if not body:
        body = description
    return Article(
        body=body,
        image=meta.get("image", ""),
        video=meta.get("video", ""),
        title=collapse_ws(meta.get("title", "")),
        description=description,
        status=200,
        final_url=final_url,
    )


def _ext_from_type(ctype: str, default: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }
    return mapping.get((ctype or "").lower(), default)


def download_image(cfg: Config, session: requests.Session, url: str
                   ) -> tuple[bytes, str, str] | None:
    if not url:
        return None
    result = download_limited(
        session, url,
        max_bytes=cfg.media_photo_max_mb * 1024 * 1024,
        timeout=cfg.fetch_timeout,
        allowed_prefix="image/",
        headers={"User-Agent": cfg.user_agent},
    )
    if not result:
        return None
    data, ctype = result
    if len(data) < 512:
        return None
    ext = _ext_from_type(ctype, ".jpg")
    host = (urlsplit(url).hostname or "image").replace(".", "_")
    return data, f"{host}{ext}", ctype


def download_video(cfg: Config, session: requests.Session, url: str
                   ) -> tuple[bytes, str, str] | None:
    if not url:
        return None
    lower = url.lower().split("?")[0]
    if lower.endswith((".m3u8", ".mpd")):
        return None
    result = download_limited(
        session, url,
        max_bytes=cfg.media_video_max_mb * 1024 * 1024,
        timeout=(10, 60),
        allowed_prefix="video/",
        headers={"User-Agent": cfg.user_agent},
    )
    if not result:
        return None
    data, ctype = result
    if len(data) < 20_000:
        return None
    ext = _ext_from_type(ctype, ".mp4")
    host = (urlsplit(url).hostname or "video").replace(".", "_")
    return data, f"{host}{ext}", ctype
