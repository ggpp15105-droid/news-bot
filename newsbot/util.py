"""Общие утилиты: время, URL, атомарная запись, HTTP, обрезка текста."""

from __future__ import annotations

import hashlib
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import MSK_OFFSET_HOURS, TRACKING_PARAMS

MSK = timezone(timedelta(hours=MSK_OFFSET_HOURS))

_TAG_RE = re.compile(r"<[^>]+>", re.S)
_WS_RE = re.compile(r"[ \t\u00a0]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


def setup_logging(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    # Argos бывает очень болтливым.
    logging.getLogger().setLevel(logging.WARNING)
    log = logging.getLogger("news-bot")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    return log


def msk_now() -> datetime:
    return datetime.now(MSK)


def ts(dt: datetime | None = None) -> int:
    return int((dt or datetime.now(timezone.utc)).timestamp())


def dt_from_ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def msk_date(value: datetime | None = None) -> str:
    return (value or msk_now()).astimezone(MSK).strftime("%Y-%m-%d")


def age_hours(timestamp: float, now: float | None = None) -> float:
    return max(0.0, ((now if now is not None else time.time()) - timestamp) / 3600.0)


def human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return f"{seconds // 86400} д {seconds % 86400 // 3600} ч"


# ---------------------------------------------------------------------------
# Текст / HTML
# ---------------------------------------------------------------------------


def clean_html(raw: str) -> str:
    """Убирает теги, раскрывает entities и нормализует пробелы."""
    if not raw:
        return ""
    text = html_lib.unescape(str(raw))
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def strip_tags(value: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", value or ""))


def esc(value: Any, quote: bool = False) -> str:
    return html_lib.escape(str(value or ""), quote=quote)


def collapse_ws(value: str) -> str:
    return " ".join((value or "").split())


def smart_cut(text: str, limit: int, mark: bool = True) -> str:
    """Обрезка по границе предложения, без разрыва HTML-entity.

    ``mark=False`` — обрезка перед переводом, без многоточия.
    """
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:] and limit - amp < 12:
        cut = cut[:amp]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", cut)]
    if ends and ends[-1] > len(cut) * 0.45:
        cut = cut[: ends[-1]]
    cut = cut.rstrip(" ,;:—-")
    return cut + (" …" if mark else "")


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(text: str, limit: int) -> list[str]:
    """Делит текст на куски не длиннее limit, по предложениям/словам."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        while len(sentence) > limit:
            if current:
                chunks.append(current)
                current = ""
            cut = sentence[:limit]
            space = cut.rfind(" ")
            if space > limit // 2:
                cut = cut[:space]
            chunks.append(cut.strip())
            sentence = sentence[len(cut):].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def origin_from_html(page: str, fallback: str = "") -> str:
    m = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        page or "",
        re.I,
    )
    if not m:
        m = re.search(
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
            page or "",
            re.I,
        )
    return html_lib.unescape(m.group(1)) if m else fallback


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------


def canonical_url(url: str) -> str:
    """Канонический URL для дедупликации.

    Убирает fragment, www, tracking-параметры, сортирует query и
    нормализует хвостовой слэш. Для показа/скачивания используется
    исходная ссылка.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low in TRACKING_PARAMS or low.startswith("utm_"):
            continue
        if low.startswith("__") or low.startswith("_hs"):
            continue
        query.append((key, value))
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query, doseq=True), ""))


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def is_http_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return p.scheme in {"http", "https"} and bool(p.hostname)
    except ValueError:
        return False


def _public_host(host: str) -> bool:
    """Проверяет, что все A/AAAA записи хоста публичные.

    Защита от SSRF: статьи и картинки приходят из RSS и не должны
    позволять боту ходить на localhost/внутреннюю сеть.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def is_safe_public_url(url: str) -> bool:
    if not is_http_url(url):
        return False
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return _public_host(host)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_session(retries: int = 2):
    """requests.Session с аккуратными retry (без агрессии к чужим сайтам)."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_get(session, url: str, *, timeout=(10, 30), stream: bool = False,
             headers: dict | None = None, max_redirects: int = 3):
    """GET с ручными редиректами и проверкой, что каждый хост публичный.

    Защита от SSRF: статья или картинка из RSS не должна уводить бота
    на localhost или во внутреннюю сеть даже через redirect.
    """
    from urllib.parse import urljoin

    current = url
    for _ in range(max_redirects + 1):
        if not is_safe_public_url(current):
            return None
        try:
            response = session.get(
                current,
                timeout=timeout,
                stream=stream,
                allow_redirects=False,
                headers=headers or {},
            )
        except Exception:
            return None
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            try:
                response.close()
            except Exception:
                pass
            if not location:
                return None
            current = urljoin(current, location)
            continue
        return response
    return None


def download_limited(session, url: str, max_bytes: int, timeout=(10, 30),
                     allowed_prefix: str = "", headers: dict | None = None) -> tuple[bytes, str] | None:
    """Скачивает не более max_bytes. Возвращает (bytes, content-type)."""
    if max_bytes <= 0:
        return None
    response = safe_get(session, url, timeout=timeout, stream=True, headers=headers)
    if response is None:
        return None
    try:
        if not response.ok:
            return None
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if allowed_prefix and not ctype.startswith(allowed_prefix):
            return None
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            return None
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > max_bytes:
                return None
    except Exception:
        return None
    finally:
        try:
            response.close()
        except Exception:
            pass
    return bytes(data), ctype


# ---------------------------------------------------------------------------
# Файлы / JSON
# ---------------------------------------------------------------------------


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, data: Any, compact: bool = True) -> None:
    text = json.dumps(
        data,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
        sort_keys=True,
    )
    atomic_write_text(path, text + "\n")


def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def stable_hash(*parts: Any, length: int = 16) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:length]


# ---------------------------------------------------------------------------
# Тихие часы
# ---------------------------------------------------------------------------


def in_quiet_hours(now: datetime, quiet: tuple[int, int] | None) -> bool:
    if not quiet:
        return False
    local = now.astimezone(MSK)
    start, end = quiet
    hour = local.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def quiet_end(now: datetime, quiet: tuple[int, int] | None) -> datetime | None:
    """Когда закончатся тихие часы (в MSK)."""
    if not quiet or not in_quiet_hours(now, quiet):
        return None
    local = now.astimezone(MSK)
    _, end = quiet
    candidate = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def normalize_slot(now: datetime, hour: int) -> str:
    local = now.astimezone(MSK)
    return f"{local:%Y-%m-%d}T{hour:02d}"


def slot_dt(slot: str) -> datetime | None:
    try:
        return datetime.strptime(slot, "%Y-%m-%dT%H").replace(tzinfo=MSK)
    except (ValueError, TypeError):
        return None


def mask_secret(text: str, secret: str) -> str:
    if secret:
        return (text or "").replace(secret, "***")
    return text or ""


def bool_icon(value: bool) -> str:
    return "✅" if value else "❌"


def format_list(items: Iterable[str], limit: int = 10) -> str:
    items = [str(x) for x in items]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" и ещё {len(items) - limit}"
