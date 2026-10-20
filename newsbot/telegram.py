"""Telegram Bot API: отправка, редактирование, медиа, админ-команды."""

from __future__ import annotations

import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Config
from .util import mask_secret, strip_tags

log = logging.getLogger("news-bot.telegram")

_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")


def form_value(value: Any) -> Any:
    """Значения для multipart/form-data: словари и списки — в JSON.

    Без этого requests сериализует ``reply_markup`` питоновским repr
    с одинарными кавычками, и Telegram отвечает «can't parse reply
    keyboard markup». Из-за этого фото с кнопками не отправлялись.
    """
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
_OPEN_TAGS = {"b", "strong", "i", "em", "u", "s", "code", "pre", "blockquote", "a"}


@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    chat_id: str = ""
    error: str = ""
    parts: int = 0


@dataclass
class TgResponse:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    status: int = 0
    retry_after: int = 0


def _tag_name(tag: str) -> str:
    return tag.strip("<>").split()[0].lstrip("/").lower()


def split_html(text: str, limit: int) -> list[str]:
    """Делит HTML-текст на части <= limit, не разрывая теги.

    Открытые теги закрываются в конце части и открываются заново в
    следующей. Этого достаточно для нашего подмножества (b/i/a/code/pre).
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    tokens = _TAG_SPLIT_RE.split(text)
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    stack: list[str] = []

    def opening(tag: str) -> str:
        return tag

    def closing(name: str) -> str:
        return f"</{name}>"

    def flush() -> None:
        nonlocal current, length
        if not current:
            return
        tail = "".join(closing(_tag_name(t)) for t in reversed(stack))
        chunks.append("".join(current) + tail)
        current = [opening(t) for t in stack]
        length = sum(len(x) for x in current)

    for token in tokens:
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            name = _tag_name(token)
            if token.startswith("</"):
                if stack and _tag_name(stack[-1]) == name:
                    stack.pop()
                elif any(_tag_name(t) == name for t in stack):
                    while stack and _tag_name(stack.pop()) != name:
                        pass
            elif name in _OPEN_TAGS and not token.endswith("/>"):
                stack.append(token)
            current.append(token)
            length += len(token)
            continue

        remaining = token
        while remaining:
            space = limit - length
            if space <= 1:
                flush()
                space = limit - length
            if len(remaining) <= space:
                current.append(remaining)
                length += len(remaining)
                break
            cut = remaining[:space]
            newline = cut.rfind("\n")
            if newline > space * 0.4:
                cut = cut[: newline + 1]
            else:
                word = cut.rfind(" ")
                if word > space * 0.5:
                    cut = cut[: word + 1]
            current.append(cut)
            length += len(cut)
            remaining = remaining[len(cut):]
            if remaining:
                flush()
    if current:
        tail = "".join(closing(_tag_name(t)) for t in reversed(stack))
        chunks.append("".join(current) + tail)
    return [c for c in chunks if strip_tags(c).strip()]


class TelegramClient:
    def __init__(self, cfg: Config, session: requests.Session,
                 dry_run: bool | None = None):
        self.cfg = cfg
        self.session = session
        self.dry_run = cfg.dry_run if dry_run is None else dry_run
        self.api_url = f"https://api.telegram.org/bot{cfg.bot_token}"
        self._dry_counter = itertools.count(1)
        self._me: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Низкий уровень
    # ------------------------------------------------------------------

    def _api(self, method: str, payload: dict[str, Any] | None = None,
             timeout: int | None = None, files: dict | None = None,
             retries: int = 3) -> TgResponse:
        if self.dry_run:
            log.info("[DRY RUN] %s %s", method, str(payload)[:300])
            return TgResponse(True, {"ok": True, "result": {"message_id": -next(self._dry_counter)}})
        timeout = timeout or self.cfg.telegram_timeout
        url = f"{self.api_url}/{method}"
        last = TgResponse(False, error="неизвестная ошибка")
        for attempt in range(retries):
            try:
                if files:
                    form = {key: form_value(value) for key, value in (payload or {}).items()}
                    r = self.session.post(url, data=form, files=files, timeout=timeout)
                else:
                    r = self.session.post(url, json=payload or {}, timeout=timeout)
            except Exception as exc:
                last = TgResponse(False, error=mask_secret(
                    f"сеть: {exc.__class__.__name__}: {exc}", self.cfg.bot_token))
                time.sleep(1.2 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {}
            description = str(data.get("description") or r.text[:200])
            description = mask_secret(description, self.cfg.bot_token)
            if r.status_code == 200 and data.get("ok"):
                return TgResponse(True, data, status=200)
            retry_after = int(((data.get("parameters") or {}).get("retry_after")) or 0)
            last = TgResponse(False, error=f"HTTP {r.status_code}: {description}",
                              status=r.status_code, retry_after=retry_after)
            if r.status_code == 429:
                wait = min(90, max(1, retry_after or 5)) + attempt
                log.warning("Telegram 429, жду %s с", wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        return last

    def api(self, method: str, payload: dict[str, Any] | None = None,
            timeout: int | None = None) -> TgResponse:
        return self._api(method, payload, timeout)

    # ------------------------------------------------------------------
    # Тексты
    # ------------------------------------------------------------------

    def _send_text_once(self, chat_id: str, text: str, *, preview: bool,
                        buttons: list[list[dict]] | None,
                        silent: bool) -> SendResult:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
            "disable_notification": silent,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        resp = self._api("sendMessage", payload)
        if not resp.ok and resp.status in {400} and (
            "can't parse entities" in resp.error.lower()
            or "unsupported start tag" in resp.error.lower()
            or "unmatched end tag" in resp.error.lower()
        ):
            # Резерв: отправляем без HTML, чтобы новость не потерялась.
            plain = strip_tags(text)
            resp = self._api("sendMessage", {
                "chat_id": chat_id,
                "text": plain[: self.cfg.tg_limit],
                "disable_web_page_preview": not preview,
                "disable_notification": silent,
            })
        if not resp.ok:
            return SendResult(False, error=resp.error, chat_id=chat_id)
        result = resp.data.get("result") or {}
        return SendResult(True, int(result.get("message_id") or 0) or None,
                          chat_id, "", 1)

    def send_text(self, chat_id: str, text: str, *, preview: bool = False,
                  buttons: list[list[dict]] | None = None,
                  silent: bool = False) -> SendResult:
        text = (text or "").strip()
        if not text:
            return SendResult(False, error="пустой текст", chat_id=chat_id)
        parts = split_html(text, self.cfg.tg_limit)
        first_id: int | None = None
        for index, part in enumerate(parts):
            result = self._send_text_once(
                chat_id, part,
                preview=preview and index == 0,
                buttons=buttons if index == 0 else None,
                silent=silent,
            )
            if not result.ok:
                return SendResult(False, first_id, chat_id, result.error, index)
            first_id = first_id or result.message_id
            if index < len(parts) - 1:
                time.sleep(1.0)
        return SendResult(True, first_id, chat_id, "", len(parts))

    def edit_text(self, chat_id: str, message_id: int, text: str, *,
                  preview: bool = False,
                  buttons: list[list[dict]] | None = None) -> SendResult:
        if not message_id or message_id < 0:
            return SendResult(False, error="нет message_id", chat_id=chat_id)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        resp = self._api("editMessageText", payload, retries=2)
        if not resp.ok and "message is not modified" in resp.error.lower():
            return SendResult(True, message_id, chat_id)
        if not resp.ok and resp.status == 400 and "can't parse entities" in resp.error.lower():
            resp = self._api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": strip_tags(text)[: self.cfg.tg_limit],
                "disable_web_page_preview": not preview,
            }, retries=1)
        if not resp.ok:
            return SendResult(False, message_id, chat_id, resp.error)
        return SendResult(True, message_id, chat_id)

    def edit_caption(self, chat_id: str, message_id: int, caption: str, *,
                     buttons: list[list[dict]] | None = None) -> SendResult:
        if not message_id or message_id < 0:
            return SendResult(False, error="нет message_id", chat_id=chat_id)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption[: self.cfg.caption_limit],
            "parse_mode": "HTML",
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        resp = self._api("editMessageCaption", payload, retries=2)
        if not resp.ok and "message is not modified" in resp.error.lower():
            return SendResult(True, message_id, chat_id)
        if not resp.ok:
            return SendResult(False, message_id, chat_id, resp.error)
        return SendResult(True, message_id, chat_id)

    # ------------------------------------------------------------------
    # Медиа
    # ------------------------------------------------------------------

    def _send_media_once(self, method: str, chat_id: str, field: str,
                         *, data: bytes | None, url: str, filename: str,
                         mime: str, caption: str,
                         buttons: list[list[dict]] | None,
                         timeout: int, silent: bool) -> SendResult:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption[: self.cfg.caption_limit],
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        if method == "sendVideo":
            payload["supports_streaming"] = True
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        if data:
            payload[field] = "attach://upload"
            files = {"upload": (filename, data, mime)}
            resp = self._api(method, payload, timeout=timeout, files=files, retries=2)
        else:
            payload[field] = url
            resp = self._api(method, payload, timeout=timeout, retries=2)
        if not resp.ok:
            return SendResult(False, error=resp.error, chat_id=chat_id)
        result = resp.data.get("result") or {}
        return SendResult(True, int(result.get("message_id") or 0) or None, chat_id)

    def _send_media(self, method: str, chat_id: str, field: str,
                    data: bytes | None, url: str, filename: str,
                    mime: str, caption: str, buttons: list[list[dict]] | None,
                    timeout: int, silent: bool = False) -> SendResult:
        """Отправляет медиа с несколькими резервами.

        Порядок попыток:
          1. прямая ссылка — Telegram скачивает сам (так работала
             исходная версия бота и это самый быстрый путь);
          2. загрузка файла, если он уже скачан;
          3. то же, но с подписью без HTML (на случай «can't parse
             entities»).
        """
        caption = (caption or "").strip()
        attempts: list[tuple[bool, str, str]] = []      # (upload, url, caption)
        seen: set[tuple[bool, str, str]] = set()

        def add(upload: bool, link: str, cap: str) -> None:
            key = (upload, link, cap)
            if key in seen:
                return
            if upload and not data:
                return
            if not upload and not link:
                return
            seen.add(key)
            attempts.append(key)

        for cap in (caption, strip_tags(caption) if "<" in caption else ""):
            if not cap:
                continue
            if url:
                add(False, url, cap)
            if data:
                add(True, "", cap)

        last = SendResult(False, error="нет медиа для отправки", chat_id=chat_id)
        for upload, link, cap in attempts:
            result = self._send_media_once(
                method, chat_id, field,
                data=data if upload else None,
                url="" if upload else link,
                filename=filename, mime=mime, caption=cap,
                buttons=buttons, timeout=timeout, silent=silent,
            )
            if result.ok:
                return result
            last = result
            log.warning("%s: %s (%s)", method, result.error, "upload" if upload else "url")
            # 403/404 по ссылке — сразу переходим к следующей попытке,
            # 429/5xx уже отработаны ретраями внутри _api.
        return last

    def send_photo(self, chat_id: str, caption: str, *, data: bytes | None = None,
                   url: str = "", filename: str = "photo.jpg", mime: str = "image/jpeg",
                   buttons: list[list[dict]] | None = None,
                   silent: bool = False) -> SendResult:
        return self._send_media(
            "sendPhoto", chat_id, "photo", data, url, filename, mime or "image/jpeg",
            caption, buttons, timeout=max(60, self.cfg.telegram_timeout),
            silent=silent,
        )

    def send_video(self, chat_id: str, caption: str, *, data: bytes | None = None,
                   url: str = "", filename: str = "video.mp4", mime: str = "video/mp4",
                   buttons: list[list[dict]] | None = None,
                   silent: bool = False) -> SendResult:
        return self._send_media(
            "sendVideo", chat_id, "video", data, url, filename, mime or "video/mp4",
            caption, buttons, timeout=max(90, self.cfg.telegram_timeout),
            silent=silent,
        )

    # ------------------------------------------------------------------
    # Сервисное
    # ------------------------------------------------------------------

    def get_me(self) -> dict[str, Any] | None:
        if self._me is not None:
            return self._me
        resp = self._api("getMe", retries=2)
        if resp.ok:
            self._me = resp.data.get("result") or {}
            return self._me
        log.warning("getMe: %s", resp.error)
        return None

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        resp = self._api("getChat", {"chat_id": chat_id}, retries=1)
        return (resp.data.get("result") or None) if resp.ok else None

    def get_chat_member(self, chat_id: str, user_id: int | str) -> dict[str, Any] | None:
        resp = self._api("getChatMember", {
            "chat_id": chat_id, "user_id": user_id,
        }, retries=1)
        return (resp.data.get("result") or None) if resp.ok else None

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        resp = self._api("getUpdates", {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=max(10, timeout + 10), retries=1)
        if not resp.ok:
            return []
        return list(resp.data.get("result") or [])

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        return self._api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:200],
            "show_alert": False,
        }, retries=1).ok

    def send_alert(self, text: str) -> bool:
        if not self.cfg.admin_id:
            log.warning("ADMIN_ID не задан — алерт не отправлен")
            return False
        return self.send_text(str(self.cfg.admin_id), text, silent=False).ok

    def delete_message(self, chat_id: str, message_id: int) -> bool:
        return self._api("deleteMessage", {
            "chat_id": chat_id, "message_id": message_id,
        }, retries=1).ok

    def send_chat_action(self, chat_id: str, action: str = "typing") -> bool:
        return self._api("sendChatAction", {
            "chat_id": chat_id, "action": action,
        }, retries=0).ok
