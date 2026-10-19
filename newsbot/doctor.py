"""Preflight-проверка окружения: ``python news_bot.py --doctor``.

Проверяет то, что чаще всего ломает запуск: токен и права бота, доступ к
каналу, живые ли ленты, какие переводчики реально готовы, пишется ли
состояние и что с расписанием. Возвращает код 0, если критических проблем
нет, и 1, если есть.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import feedparser

from .config import Config
from .state import State
from .telegram import TelegramClient
from .util import (
    MSK,
    in_quiet_hours,
    make_session,
    quiet_end,
    safe_get,
    slot_dt,
)

log = logging.getLogger("news-bot.doctor")

OK = "ok"
WARN = "warn"
FAIL = "fail"

ICONS = {OK: "✅", WARN: "⚠️", FAIL: "❌"}

# Провайдеры перевода, которым нужен ключ.
KEYED_PROVIDERS = {"llm": "OPENAI_API_KEY", "deepl": "DEEPL_API_KEY"}


@dataclass
class Check:
    """Одна строка отчёта."""

    name: str
    status: str = OK
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        text = f"{ICONS.get(self.status, '•')} {self.name}"
        if self.detail:
            text += f": {self.detail}"
        if self.hint and self.status != OK:
            text += f"\n   → {self.hint}"
        return text


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


def check_config(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    if cfg.warnings:
        checks.append(Check(
            "Конфигурация",
            WARN,
            f"{len(cfg.warnings)} замечаний",
            "; ".join(cfg.warnings[:4]),
        ))
    else:
        checks.append(Check("Конфигурация", OK, "обязательные переменные на месте"))
    if not cfg.admin_id:
        checks.append(Check(
            "ADMIN_ID",
            WARN,
            "не задан",
            "не будут работать админ-команды и алерты",
        ))
    return checks


def check_telegram(cfg: Config, tg: TelegramClient) -> list[Check]:
    checks: list[Check] = []
    me = tg.get_me()
    if not me:
        checks.append(Check(
            "Telegram",
            FAIL,
            "getMe не отвечает",
            "проверь BOT_TOKEN и доступ к api.telegram.org",
        ))
        return checks
    username = me.get("username") or me.get("first_name") or "?"
    checks.append(Check("Telegram", OK, f"бот @{username} (id {me.get('id')})"))
    return checks


def check_channel(cfg: Config, tg: TelegramClient, bot_id: int | None) -> list[Check]:
    checks: list[Check] = []
    chat = tg.get_chat(cfg.channel_id)
    if not chat:
        checks.append(Check(
            "Канал",
            FAIL,
            f"{cfg.channel_id} недоступен",
            "добавь бота в канал и проверь CHANNEL_ID",
        ))
        return checks
    title = chat.get("title") or chat.get("username") or cfg.channel_id
    checks.append(Check("Канал", OK, f"«{title}» найден"))

    if not bot_id:
        return checks
    member = tg.get_chat_member(cfg.channel_id, bot_id)
    if not member:
        checks.append(Check(
            "Права бота",
            WARN,
            "не удалось получить статус участника",
            "проверь, что бот добавлен в канал",
        ))
        return checks
    status = str(member.get("status") or "")
    if status in {"administrator", "creator"}:
        can_post = member.get("can_post_messages", True)
        if can_post:
            checks.append(Check("Права бота", OK, "администратор, может публиковать"))
        else:
            checks.append(Check(
                "Права бота",
                FAIL,
                "администратор без права публикации",
                "включи в настройках канала право «Публикация сообщений»",
            ))
    else:
        checks.append(Check(
            "Права бота",
            FAIL,
            f"статус «{status}», не администратор",
            "выдай боту права администратора канала",
        ))
    return checks


def _fetch_feed(session, url: str) -> tuple[bool, int, str]:
    try:
        response = safe_get(session, url, timeout=(4, 8), max_redirects=2)
        if response is None:
            return False, 0, "нет ответа"
        if response.status_code >= 400:
            return False, 0, f"HTTP {response.status_code}"
        parsed = feedparser.parse(response.content)
        entries = len(getattr(parsed, "entries", []) or [])
        if entries:
            return True, entries, ""
        if getattr(parsed, "bozo", False):
            return False, 0, "не разобрался как RSS/Atom"
        return True, 0, "пустая лента"
    except Exception as exc:  # pragma: no cover - сеть
        return False, 0, f"{exc.__class__.__name__}: {exc}"[:120]


def check_feeds(cfg: Config, session, state: State | None = None,
                now: float | None = None,
                max_workers: int = 8) -> list[Check]:
    now = time.time() if now is None else now
    feeds = list(cfg.feeds)
    if not feeds:
        return [Check("Ленты", FAIL, "список пуст", "добавь EXTRA_FEEDS")]

    results: dict[str, tuple[bool, int, str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_feed, session, feed.url): feed for feed in feeds}
        for future in as_completed(futures):
            feed = futures[future]
            try:
                results[feed.name] = future.result()
            except Exception as exc:  # pragma: no cover - защита от зависаний
                results[feed.name] = (False, 0, str(exc)[:120])

    ok_feeds = [name for name, (ok, _, _) in results.items() if ok]
    empty = [name for name, (ok, count, _) in results.items() if ok and not count]
    failed = [(name, err) for name, (ok, _, err) in results.items() if not ok]

    detail = f"{len(ok_feeds)} из {len(feeds)} отвечают"
    if empty:
        detail += f", пустых: {len(empty)}"
    status = OK
    if failed:
        status = FAIL if len(failed) * 2 >= len(feeds) else WARN
        detail += f", ошибок: {len(failed)}"
    hint = ""
    if failed:
        hint = "; ".join(f"{name}: {err}" for name, err in failed[:5])
    checks = [Check("Ленты", status, detail, hint)]

    disabled: list[str] = []
    if state is not None:
        for feed in feeds:
            info = state.feeds.get(feed.name) or {}
            if int(info.get("disabled_until", 0)) > now or feed.name.lower() in state.disabled_feeds():
                disabled.append(feed.name)
    if disabled:
        checks.append(Check(
            "Отключённые ленты",
            WARN,
            f"{len(disabled)}: {', '.join(disabled[:5])}",
            "включатся автоматически или командой /enable",
        ))
    return checks


def check_providers(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    ready: list[str] = []
    missing: list[str] = []
    for provider in cfg.translate_chain:
        if provider in KEYED_PROVIDERS:
            has_key = bool(cfg.openai_api_key if provider == "llm" else cfg.deepl_api_key)
            (ready if has_key else missing).append(provider)
        elif provider == "yandex":
            has_key = bool(cfg.yandex_api_key and cfg.yandex_folder_id)
            (ready if has_key else missing).append(provider)
        else:
            ready.append(provider)

    detail = ", ".join(ready) or "нет"
    if missing:
        detail += f" (без ключа: {', '.join(missing)})"
    if not ready:
        checks.append(Check(
            "Переводчики",
            FAIL,
            "ни один провайдер не готов",
            "задай OPENAI_API_KEY/DEEPL_API_KEY или добавь google,mymemory,argos в TRANSLATE_CHAIN",
        ))
    elif not any(p in ready for p in ("llm", "deepl", "yandex")):
        checks.append(Check(
            "Переводчики",
            WARN,
            detail,
            "работают только публичные API без ключа — возможны лимиты и ошибки",
        ))
    else:
        checks.append(Check("Переводчики", OK, detail))

    try:
        import argostranslate  # noqa: F401
        argos = True
    except Exception:
        argos = False
    if "argos" in cfg.translate_chain:
        checks.append(Check(
            "Argos (офлайн-перевод)",
            OK if argos else WARN,
            "установлен" if argos else "пакет не установлен",
            "" if argos else "pip install argostranslate, иначе офлайн-фолбэка не будет",
        ))
    return checks


def check_glossary(cfg: Config) -> list[Check]:
    path = cfg.glossary_file or "glossary.txt"
    if not os.path.exists(path):
        return [Check("Глоссарий", WARN, f"{path} не найден",
                      "перевод будет без защиты терминов; создай файл или задай GLOSSARY_FILE")]
    try:
        with open(path, encoding="utf-8") as handle:
            terms = sum(1 for line in handle if line.strip() and not line.startswith("#"))
    except OSError as exc:
        return [Check("Глоссарий", WARN, f"не читается: {exc}")]
    return [Check("Глоссарий", OK, f"{terms} терминов в {path}")]


def check_state(cfg: Config, state: State, now: float | None = None) -> list[Check]:
    now = time.time() if now is None else now
    checks: list[Check] = []
    if cfg.state_redis_url:
        ok, detail = state.redis.ping()
        checks.append(Check(
            "Состояние (Redis)",
            OK if ok else FAIL,
            detail,
            "" if ok else "проверь STATE_REDIS_URL/STATE_REDIS_TOKEN",
        ))
    else:
        path = cfg.state_file
        folder = os.path.dirname(os.path.abspath(path)) or "."
        writable = os.access(folder, os.W_OK)
        if os.path.exists(path):
            size = os.path.getsize(path) / 1024
            detail = f"{path}, {size:.0f} КБ"
        else:
            detail = f"{path} ещё не создан"
        checks.append(Check(
            "Состояние (файл)",
            OK if writable else FAIL,
            detail,
            "" if writable else f"нет прав на запись в {folder}",
        ))

    paused_until = int(state.admin.get("paused_until", 0))
    if paused_until > now:
        checks.append(Check(
            "Пауза",
            WARN,
            f"публикации остановлены до {datetime.fromtimestamp(paused_until, MSK):%d.%m %H:%M} МСК",
            "снять командой /resume",
        ))
    checks.append(Check(
        "История",
        OK,
        f"{len(state.posted)} записей о постах, {len(state.clusters)} кластеров",
    ))
    return checks


def check_schedule(cfg: Config, now: float | None = None) -> list[Check]:
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, MSK)
    checks: list[Check] = []

    if cfg.quiet_hours:
        quiet = in_quiet_hours(local, cfg.quiet_hours)
        if quiet:
            end = quiet_end(local, cfg.quiet_hours)
            checks.append(Check(
                "Тихие часы",
                WARN,
                f"сейчас тихо до {end:%H:%M} МСК" if end else "сейчас тихие часы",
                "breaking-новости выйдут без звука, остальные — после окончания",
            ))
        else:
            checks.append(Check(
                "Тихие часы",
                OK,
                f"{cfg.quiet_hours[0]:02d}–{cfg.quiet_hours[1]:02d} МСК",
            ))
    else:
        checks.append(Check("Тихие часы", OK, "выключены"))

    if not cfg.digest_hours:
        checks.append(Check("Дайджесты", WARN, "выключены", "задай DIGEST_HOURS, например 8,20"))
    else:
        next_hour = None
        today = local.strftime("%Y-%m-%d")
        candidates = []
        for hour in cfg.digest_hours:
            slot = slot_dt(f"{today}T{hour:02d}")
            if slot and slot > local:
                candidates.append(slot)
        if candidates:
            next_hour = min(candidates)
        checks.append(Check(
            "Дайджесты",
            OK,
            ", ".join(f"{h:02d}:00" for h in cfg.digest_hours) + " МСК"
            + (f" (следующий в {next_hour:%H:%M})" if next_hour else " (сегодня уже прошли)"),
        ))

    if cfg.weekly_recap:
        days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        checks.append(Check(
            "Недельный обзор",
            OK,
            f"{days[cfg.weekly_recap_day]}, {cfg.weekly_recap_hour:02d}:00 МСК",
        ))
    return checks


# ---------------------------------------------------------------------------
# Оркестрация
# ---------------------------------------------------------------------------


def run(cfg: Config, *, session=None, tg: TelegramClient | None = None,
        state: State | None = None, now: float | None = None,
        with_feeds: bool = True) -> list[Check]:
    """Выполняет все проверки. Сеть используется, но ошибки не пробрасываются."""
    now = time.time() if now is None else now
    session = session or make_session()
    tg = tg or TelegramClient(cfg, session, dry_run=False)
    state = state or State.load(cfg, now)

    checks: list[Check] = []
    steps = [
        ("config", lambda: check_config(cfg)),
        ("telegram", lambda: check_telegram(cfg, tg)),
        ("channel", lambda: check_channel(cfg, tg, int((tg.get_me() or {}).get("id") or 0) or None)),
        ("providers", lambda: check_providers(cfg)),
        ("glossary", lambda: check_glossary(cfg)),
        ("state", lambda: check_state(cfg, state, now)),
        ("schedule", lambda: check_schedule(cfg, now)),
    ]
    if with_feeds:
        steps.append(("feeds", lambda: check_feeds(cfg, session, state, now)))
    for name, step in steps:
        try:
            checks.extend(step())
        except Exception as exc:  # pragma: no cover - защита от неожиданностей
            log.debug("Проверка %s упала", name, exc_info=True)
            checks.append(Check(f"Проверка {name}", FAIL, f"сбой: {exc.__class__.__name__}"))
    return checks


def summarize(checks: Iterable[Check]) -> dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return counts


def render(checks: Iterable[Check], now: float | None = None) -> str:
    checks = list(checks)
    now = time.time() if now is None else now
    counts = summarize(checks)
    lines = [
        "🧪 Проверка news-bot",
        datetime.fromtimestamp(now, MSK).strftime("Время: %d.%m.%Y %H:%M МСК"),
        f"Итог: {ICONS[OK]} {counts[OK]} · {ICONS[WARN]} {counts[WARN]} · {ICONS[FAIL]} {counts[FAIL]}",
        "",
    ]
    lines += [check.line() for check in checks]

    fails = [check for check in checks if check.status == FAIL]
    if fails:
        lines += ["", "Критические проблемы:"]
        lines += [f"• {check.name}: {check.detail or check.hint}" for check in fails]
        lines += ["", "Бот не запустится, пока это не исправлено."]
    elif counts[WARN]:
        lines += ["", "Критических проблем нет. Замечания выше можно поправить позже."]
    else:
        lines += ["", "Всё готово к запуску."]
    return "\n".join(lines)


def run_cli(cfg: Config) -> int:
    checks = run(cfg)
    report = render(checks)
    print(report)
    return 1 if any(check.status == FAIL for check in checks) else 0


def report_for_admin(cfg: Config, *, session=None, tg: TelegramClient | None = None,
                     state: State | None = None, now: float | None = None) -> str:
    """Отчёт для Telegram: без сети не падает, текст экранируется вызывающим."""
    return render(run(cfg, session=session, tg=tg, state=state, now=now))
