"""Админ-команды в Telegram и обратная связь под постами."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Callable

from .config import Config
from .state import State
from .telegram import TelegramClient
from .text import norm_tokens
from .util import (
    MSK,
    esc,
    host_of,
    human_age,
)

log = logging.getLogger("news-bot.admin")

PostUrlFn = Callable[[str], str]
ForceDigestFn = Callable[[], str]
ExplainFn = Callable[[str], str]
PreviewFn = Callable[[int], str]
PreviewPostFn = Callable[[str], str]
DoctorFn = Callable[[], str]


HELP = """<b>Команды</b>
/status — состояние бота
/stats [дней] — статистика
/sources — здоровье лент
/enable <лента> — включить ленту
/disable <лента> — выключить ленту
/pause [30m|2h|1d] — пауза публикаций
/resume — снять паузу
/post &lt;url&gt; — опубликовать вручную
/digest — отправить дайджест сейчас
/recap — отправить недельный обзор
/why &lt;url|id&gt; — почему новость не вышла
/preview [N] — очередь публикации
/test &lt;url&gt; — черновик поста в личку
/doctor — проверка окружения
/cache [clear] — кэш переводов
/config — эффективные настройки
/version — версия
/help — эта справка"""


class AdminService:
    def __init__(self, cfg: Config, state: State, tg: TelegramClient, now: float | None = None,
                 post_url: PostUrlFn | None = None, force_digest: ForceDigestFn | None = None,
                 force_recap: ForceDigestFn | None = None,
                 explain: ExplainFn | None = None,
                 preview: PreviewFn | None = None,
                 preview_post: PreviewPostFn | None = None,
                 doctor: DoctorFn | None = None):
        self.cfg = cfg
        self.state = state
        self.tg = tg
        self.now = time.time() if now is None else now
        self.post_url = post_url
        self.force_digest = force_digest
        self.force_recap = force_recap
        self.explain = explain
        self.preview = preview
        self.preview_post = preview_post
        self.doctor = doctor

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def poll(self) -> int:
        if not (self.cfg.admin_commands and self.cfg.admin_id):
            return 0
        offset = int(self.state.admin.get("offset", 0))
        updates = self.tg.get_updates(offset, timeout=0)
        handled = 0
        for update in updates:
            update_id = int(update.get("update_id", 0))
            if update_id >= offset:
                offset = update_id + 1
            try:
                if "callback_query" in update:
                    if self._handle_callback(update["callback_query"]):
                        handled += 1
                elif "message" in update:
                    if self._handle_message(update["message"]):
                        handled += 1
            except Exception as exc:
                log.warning("Ошибка обработки update: %s", exc)
        self.state.admin["offset"] = offset
        return handled

    def _is_admin(self, user_id: int) -> bool:
        try:
            return int(self.cfg.admin_id) == int(user_id)
        except (TypeError, ValueError):
            return False

    def _reply(self, chat_id: int | str, text: str) -> None:
        self.tg.send_text(str(chat_id), text, preview=False)

    # ------------------------------------------------------------------
    # Callback-кнопки
    # ------------------------------------------------------------------

    def _handle_callback(self, callback: dict) -> bool:
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        user = callback.get("from") or {}
        user_id = int(user.get("id") or 0)
        if data.startswith("fb:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[2] not in {"up", "down"}:
                return False
            cluster_id, vote = parts[1], parts[2]
            cluster = self.state.clusters.get(cluster_id)
            source = str((cluster or {}).get("source", ""))
            status = self.state.record_feedback(cluster_id, str(user_id), vote, self.now, source)
            if self.tg.answer_callback(callback_id, "Спасибо!" if status != "same" else "Уже учтено"):
                return True
            return False
        if not self._is_admin(user_id):
            return False
        if data.startswith("digest:"):
            # Слот уже отмечен в состоянии; кнопка информационная.
            self.tg.answer_callback(callback_id, "Дайджест отправлен")
            return True
        return False

    # ------------------------------------------------------------------
    # Сообщения
    # ------------------------------------------------------------------

    def _handle_message(self, message: dict) -> bool:
        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = message.get("from") or {}
        user_id = int(user.get("id") or 0)
        if not text or chat_id is None:
            return False
        if not self._is_admin(user_id):
            return False
        if not text.startswith("/"):
            return False
        command, _, args = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        args = args.strip()
        handler = {
            "/start": self._cmd_help,
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/stats": self._cmd_stats,
            "/sources": self._cmd_sources,
            "/enable": self._cmd_enable,
            "/disable": self._cmd_disable,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/post": self._cmd_post,
            "/digest": self._cmd_digest,
            "/recap": self._cmd_recap,
            "/why": self._cmd_why,
            "/preview": self._cmd_preview,
            "/queue": self._cmd_preview,
            "/test": self._cmd_test,
            "/doctor": self._cmd_doctor,
            "/cache": self._cmd_cache,
            "/config": self._cmd_config,
            "/version": self._cmd_version,
        }.get(command)
        if handler is None:
            self._reply(chat_id, f"Неизвестная команда: <code>{esc(command)}</code>\n\n{HELP}")
            return True
        handler(chat_id, args)
        return True

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    def _cmd_help(self, chat_id, args: str) -> None:
        self._reply(chat_id, HELP)

    def _cmd_status(self, chat_id, args: str) -> None:
        stats = self.state.stats
        paused_until = int(self.state.admin.get("paused_until", 0))
        pause = (
            f"до {datetime.fromtimestamp(paused_until, MSK):%d.%m %H:%M} МСК"
            if paused_until > self.now else "нет"
        )
        last_post = int(stats.get("last_post_ts", 0))
        last_run = int(stats.get("last_run_ts", 0))
        lines = [
            "<b>📊 Состояние бота</b>",
            f"Пауза: {esc(pause)}",
            f"Постов сегодня: {self.state.posts_today(self.now)} / {self.cfg.max_posts_per_day}",
            f"Последний пост: {esc(human_age(self.now - last_post)) if last_post else '—'} назад",
            f"Последний запуск: {esc(human_age(self.now - last_run)) if last_run else '—'} назад",
            f"Кластеров в памяти: {len(self.state.clusters)}",
            f"Дайджест: слот {esc(self.state.digest_meta.get('slot') or '—')}",
            f"Переводчики: {esc(self.translator_report())}",
            f"Кэш переводов: {len(self.state.data.get('translations', {}))}",
        ]
        disabled = self.state.disabled_feeds()
        if disabled:
            lines.append(f"Выключены вручную: {esc(', '.join(sorted(disabled)))}")
        self._reply(chat_id, "\n".join(lines))

    def translator_report(self) -> str:
        providers = self.state.stats.get("translation_providers", {})
        if not providers:
            return "нет данных"
        parts = []
        for name, data in sorted(providers.items()):
            ok = int(data.get("ok", 0))
            fail = int(data.get("fail", 0))
            parts.append(f"{name} {ok}/{ok + fail}")
        return ", ".join(parts)

    def _cmd_stats(self, chat_id, args: str) -> None:
        days = 7
        if args.isdigit():
            days = max(1, min(90, int(args)))
        cutoff = self.now - days * 86400
        posted = [
            r for r in self.state.posted.values()
            if int(r.get("ts", 0)) >= cutoff and not r.get("alias")
        ]
        by_source: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for record in posted:
            by_source[record.get("source", "?")] = by_source.get(record.get("source", "?"), 0) + 1
            cat = record.get("category", "")
            if cat:
                by_category[cat] = by_category.get(cat, 0) + 1
        top_sources = sorted(by_source.items(), key=lambda x: -x[1])[:8]
        top_categories = sorted(by_category.items(), key=lambda x: -x[1])[:8]
        feedback = self.state.feedback_by_source()
        lines = [
            f"<b>📈 Статистика за {days} дн.</b>",
            f"Постов: {len(posted)}",
            "",
            "<b>Источники:</b>",
        ]
        lines += [f"• {esc(name)} — {count}" for name, count in top_sources] or ["• —"]
        lines.append("")
        lines.append("<b>Категории:</b>")
        lines += [f"• #{esc(name)} — {count}" for name, count in top_categories] or ["• —"]
        if feedback:
            lines.append("")
            lines.append("<b>Обратная связь:</b>")
            for name, score in sorted(feedback.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"• {esc(name)}: {score:+.2f}")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_sources(self, chat_id, args: str) -> None:
        lines = ["<b>📡 Ленты</b>", ""]
        for feed in self.cfg.feeds:
            status = self.state.feeds.get(feed.name, {})
            last_ok = int(status.get("last_ok", 0))
            errors = int(status.get("errors", 0))
            disabled_until = int(status.get("disabled_until", 0))
            manual = feed.name.lower() in self.state.disabled_feeds()
            flags = []
            if manual:
                flags.append("выкл")
            if disabled_until > self.now:
                flags.append(f"пауза {human_age(disabled_until - self.now)}")
            if errors:
                flags.append(f"ошибок {errors}")
            if last_ok:
                age = human_age(self.now - last_ok)
                mark = "✅" if age.endswith("мин") or age.endswith("с") else "🟡"
                lines.append(f"{mark} {esc(feed.name)} — {age} назад {('(' + ', '.join(flags) + ')') if flags else ''}")
            else:
                lines.append(f"⚪ {esc(feed.name)} — ещё не было успеха {('(' + ', '.join(flags) + ')') if flags else ''}")
        self._reply(chat_id, "\n".join(lines))

    def _find_feed(self, query: str):
        if not query:
            return None
        q = query.lower()
        for feed in self.cfg.feeds:
            if feed.name.lower() == q:
                return feed
        for feed in self.cfg.feeds:
            if q in feed.name.lower():
                return feed
        return None

    def _cmd_enable(self, chat_id, args: str) -> None:
        feed = self._find_feed(args)
        if not feed:
            self._reply(chat_id, "Лента не найдена. Список: /sources")
            return
        self.state.set_feed_enabled(feed.name, True)
        status = self.state.feeds.setdefault(feed.name, {})
        status["disabled_until"] = 0
        status["errors"] = 0
        self._reply(chat_id, f"✅ Лента «{esc(feed.name)}» включена")

    def _cmd_disable(self, chat_id, args: str) -> None:
        feed = self._find_feed(args)
        if not feed:
            self._reply(chat_id, "Лента не найдена. Список: /sources")
            return
        self.state.set_feed_enabled(feed.name, False)
        self._reply(chat_id, f"⏸ Лента «{esc(feed.name)}» выключена")

    @staticmethod
    def _parse_duration(args: str) -> int:
        raw = (args or "").strip().lower()
        if not raw:
            return 2 * 3600
        m = re.fullmatch(r"(\d+)\s*([mhd]?)", raw)
        if not m:
            return 2 * 3600
        number = int(m.group(1))
        unit = m.group(2)
        if unit == "m":
            return max(60, number * 60)
        if unit == "h":
            return max(60, number * 3600)
        if unit == "d":
            return max(3600, number * 86400)
        # Без единицы считаем минуты: /pause 30 = 30 минут.
        return max(60, number * 60)

    def _cmd_pause(self, chat_id, args: str) -> None:
        seconds = self._parse_duration(args)
        self.state.pause(self.now + seconds)
        until = datetime.fromtimestamp(self.now + seconds, MSK)
        self._reply(chat_id, f"⏸ Публикации приостановлены до {until:%d.%m %H:%M} МСК")

    def _cmd_resume(self, chat_id, args: str) -> None:
        self.state.resume()
        self._reply(chat_id, "▶️ Публикации возобновлены")

    def _cmd_post(self, chat_id, args: str) -> None:
        if not args.startswith(("http://", "https://")):
            self._reply(chat_id, "Использование: <code>/post https://...</code>")
            return
        if not self.post_url:
            self._reply(chat_id, "Ручная публикация недоступна")
            return
        self._reply(chat_id, "⏳ Обрабатываю…")
        result = self.post_url(args.strip())
        self._reply(chat_id, result)

    def _cmd_digest(self, chat_id, args: str) -> None:
        if not self.force_digest:
            self._reply(chat_id, "Дайджест недоступен")
            return
        self._reply(chat_id, self.force_digest())

    def _cmd_recap(self, chat_id, args: str) -> None:
        if not self.force_recap:
            self._reply(chat_id, "Недельный обзор недоступен")
            return
        self._reply(chat_id, self.force_recap())

    def _cmd_why(self, chat_id, args: str) -> None:
        if not args:
            self._reply(chat_id, "Использование: <code>/why https://…</code> или <code>/why id-кластера</code>")
            return
        if not self.explain:
            self._reply(chat_id, "Команда недоступна")
            return
        self._reply(chat_id, self.explain(args))

    def _cmd_preview(self, chat_id, args: str) -> None:
        if not self.preview:
            self._reply(chat_id, "Команда недоступна")
            return
        limit = 8
        if args.isdigit():
            limit = max(1, min(20, int(args)))
        self._reply(chat_id, self.preview(limit))

    def _cmd_test(self, chat_id, args: str) -> None:
        if not args.startswith(("http://", "https://")):
            self._reply(chat_id, "Использование: <code>/test https://…</code>")
            return
        if not self.preview_post:
            self._reply(chat_id, "Команда недоступна")
            return
        self._reply(chat_id, "⏳ Готовлю черновик…")
        draft = self.preview_post(args.strip())
        self._reply(chat_id, draft)

    def _cmd_doctor(self, chat_id, args: str) -> None:
        if not self.doctor:
            self._reply(chat_id, "Команда недоступна")
            return
        self._reply(chat_id, "⏳ Проверяю окружение…")
        self._reply(chat_id, self.doctor())

    def _cmd_cache(self, chat_id, args: str) -> None:
        translations = self.state.data.setdefault("translations", {})
        embeddings = self.state.data.setdefault("embeddings", {})
        if args.lower().startswith("clear"):
            translations.clear()
            embeddings.clear()
            self._reply(chat_id, "🧹 Кэш переводов и эмбеддингов очищен")
            return
        self._reply(chat_id, (
            f"Кэш переводов: {len(translations)}\n"
            f"Кэш эмбеддингов: {len(embeddings)}"
        ))

    def _cmd_config(self, chat_id, args: str) -> None:
        cfg = self.cfg
        lines = [
            "<b>⚙️ Настройки</b>",
            f"Канал: <code>{esc(cfg.channel_id)}</code>",
            f"Постов за запуск: {cfg.max_posts_per_run}",
            f"Лимит в сутки: {cfg.max_posts_per_day}",
            f"С источника за запуск: {cfg.max_per_source_per_run}",
            f"Тихие часы: {esc('-'.join(map(str, cfg.quiet_hours)) if cfg.quiet_hours else 'выкл')} МСК",
            f"Дайджесты: {esc(', '.join(map(str, cfg.digest_hours)) + ' МСК') if cfg.digest_hours else 'выкл'}",
            f"Свежесть: {cfg.freshness_hours} ч",
            f"Дедуп: containment {cfg.dedup_containment}, jaccard {cfg.dedup_jaccard}",
            f"LLM-саммари: {'да' if cfg.llm_summary and cfg.openai_api_key else 'нет'}",
            f"Эмбеддинги: {'да' if cfg.embedding_model and cfg.openai_api_key else 'нет'}",
            f"Обновления постов: {'да' if cfg.edit_updates else 'нет'}",
            f"Кнопки 👍/👎: {'да' if cfg.feedback_buttons else 'нет'}",
            f"Кнопки источников: {'да' if cfg.source_buttons else 'нет'}",
            f"Недельный обзор: {'да' if cfg.weekly_recap else 'нет'}",
            f"Состояние: {'Redis' if cfg.state_redis_url else cfg.state_file}",
            f"Лент: {len(cfg.feeds)}",
        ]
        self._reply(chat_id, "\n".join(lines))

    def _cmd_version(self, chat_id, args: str) -> None:
        from . import __version__
        self._reply(chat_id, f"news-bot <b>{esc(__version__)}</b>")


def source_buttons(cluster: dict, limit: int = 4) -> list[list[dict]]:
    """URL-кнопки на остальные источники кластера (кроме основного).

    Читателю не нужно искать подтверждение в тексте: под постом сразу
    видны кнопки «🔗 DW», «🔗 Lenta.ru» и т.д.
    """
    links = cluster.get("links") or []
    if len(links) < 2:
        return []
    rows: list[list[dict]] = []
    primary = str(links[0].get("source") or "")
    seen = {primary.casefold()} if primary else set()
    for link in links[1:]:
        url = str(link.get("url") or "")
        source = str(link.get("source") or host_of(url) or "")
        if not url or not source:
            continue
        key = source.casefold()
        if key in seen:
            continue
        seen.add(key)
        label = f"🔗 {source}"
        if len(label) > 64:
            label = label[:63] + "…"
        rows.append([{"text": label, "url": url}])
        if len(rows) >= limit:
            break
    return rows


def feedback_keyboard(cluster_id: str) -> list[list[dict]]:
    return [[
        {"text": "👍", "callback_data": f"fb:{cluster_id}:up"},
        {"text": "👎", "callback_data": f"fb:{cluster_id}:down"},
    ]]


def same_title(a: str, b: str) -> bool:
    ta = norm_tokens(a)
    tb = norm_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.8
