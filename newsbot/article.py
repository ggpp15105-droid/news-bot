"""Загрузка статьи и медиа."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

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

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_BAD_IMAGE_PARTS = ("logo", "icon", "sprite", "avatar", "placeholder", "pixel",
                    "spacer", "blank.", "favicon", "/emoji/", "badge", "counter",
                    "subscribe", "newsletter", "banner", "/ads/", "advert", "tracking")

_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_LINK_IMAGE_RE = re.compile(
    r'<link\b[^>]*rel=["\'](?:image_src|apple-touch-icon)["\'][^>]*>', re.I
)


def _attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        out.setdefault(name, html_lib.unescape(value.strip()))
    return out


def _looks_like_content_image(url: str, attrs: dict[str, str]) -> bool:
    if not url or url.startswith("data:"):
        return False
    lower = url.lower()
    if lower.endswith(".svg") or ".svg?" in lower:
        return False
    if any(part in lower for part in _BAD_IMAGE_PARTS):
        return False
    for key in ("width", "height"):
        value = attrs.get(key, "")
        if value.isdigit() and int(value) <= 120:
            return False
    return True


def jsonld_images(page: str, base_url: str = "", limit: int = 4) -> list[str]:
    """Картинки из schema.org разметки (JSON-LD).

    Многие новостные сайты (в том числе российские) не отдают og:image,
    но кладут обложку в ``"image"`` JSON-LD.
    """
    found: list[str] = []

    def push(value: Any) -> None:
        if isinstance(value, str) and value:
            url = urljoin(base_url, value) if base_url else value
            if url.startswith(("http://", "https://")) and url not in found:
                found.append(url)
        elif isinstance(value, dict):
            push(value.get("url") or value.get("contentUrl"))
        elif isinstance(value, list):
            for entry in value:
                push(entry)

    for block in _LD_JSON_RE.findall(page or ""):
        raw = html_lib.unescape(block).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack: list[Any] = [data]
        while stack and len(found) < limit:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "image" in item:
                    push(item["image"])
                for key, value in item.items():
                    if key != "image" and isinstance(value, (dict, list)):
                        stack.append(value)
    return found[:limit]


def link_image(page: str, base_url: str = "") -> str:
    """``<link rel="image_src">`` — ещё один старый способ отдать обложку."""
    for tag in _LINK_IMAGE_RE.finditer(page or ""):
        attrs = _attrs(tag.group(0))
        href = attrs.get("href", "")
        if href.startswith(("http://", "https://")):
            return href
        if href and base_url:
            return urljoin(base_url, href)
    return ""


def page_images(page: str, base_url: str = "", limit: int = 4) -> list[str]:
    """Картинки со страницы: og:image уже приходит отдельно, здесь — <img>.

    Нужен как резерв для сайтов без og:image (например, ТАСС): если
    основная картинка не отдаётся Telegram, пробуем следующую.
    """
    found: list[str] = []
    for tag in _IMG_TAG_RE.finditer(page or ""):
        attrs = _attrs(tag.group(0))
        raw = attrs.get("src") or attrs.get("data-src") or attrs.get("data-original") or ""
        if not raw and attrs.get("srcset"):
            raw = attrs["srcset"].split(",")[0].strip().split(" ")[0]
        url = urljoin(base_url, raw) if base_url and raw else raw
        if not url.startswith(("http://", "https://")):
            continue
        if not _looks_like_content_image(url, attrs):
            continue
        if url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


@dataclass
class Article:
    body: str = ""
    image: str = ""
    video: str = ""
    images: tuple[str, ...] = ()
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
    primary_image = meta.get("image", "") or link_image(page, final_url) \
        or next(iter(jsonld_images(page, final_url, limit=1)), "")
    extra_images: list[str] = []
    for url in (*jsonld_images(page, final_url), *page_images(page, final_url)):
        if url != primary_image and url not in extra_images:
            extra_images.append(url)
    return Article(
        body=body,
        image=primary_image or (extra_images[0] if extra_images else ""),
        video=meta.get("video", ""),
        images=tuple(extra_images[:3]),
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
