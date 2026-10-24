"""Оркестрация запуска: сбор, дедупликация, публикация, обновления."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime

from .admin import AdminService, feedback_keyboard, source_buttons
from .article import Article, download_image, download_video, fetch_article
from .config import Config, ConfigError, load_config
from .dedup import Candidate, Deduper
from .digest import DigestService
from .feeds import FeedFetcher, RawItem
from .render import build_caption, build_full_text, plural_sources
from .state import State
from .telegram import TelegramClient
from .text import (
    cyrillic_ratio,
    detect_categories,
    detect_tags,
    importance,
    is_breaking,
    is_low_value,
    is_opinion,
    is_russian,
    make_lead,
    normalize_title_for_dedup,
)
from .translate import Translator
from .util import (
    MSK,
    canonical_url,
    clean_html,
    esc,
    format_list,
    host_of,
    in_quiet_hours,
    is_http_url,
    make_session,
    quiet_end,
    setup_logging,
    smart_cut,
    stable_hash,
)

log = logging.getLogger("news-bot")

MAX_ATTEMPTS_PER_CLUSTER = 3


@dataclass
class Composed:
    """Результат подготовки поста: всё, что нужно для отправки."""

    url: str
    source: str
    category: str
    chat_id: str
    caption: str
    article: Article
    image_candidates: tuple[str, ...] = ()


@dataclass
class RunStats:
    fetched: int = 0
    fresh: int = 0
    stale: int = 0
    low_value: int = 0
    already_posted: int = 0
    duplicates: int = 0
    clusters: int = 0
    posted: int = 0
    updated: int = 0
    errors: int = 0


class NewsBot:
    def __init__(self, cfg: Config, now: float | None = None):
        self.cfg = cfg
        self.now = time.time() if now is None else now
        self.session = make_session()
        self.state = State.load(cfg, self.now)
        self.translator = Translator(cfg, self.state, self.session, self.now)
        self.fetcher = FeedFetcher(cfg, self.state, self.session, self.now)
        self.deduper = Deduper(
            cfg, self.state, self.now,
            embedder=self.translator.embed if self.translator.has_embeddings else None,
        )
        self.tg = TelegramClient(cfg, self.session)
        self.digest = DigestService(cfg, self.state, self.now)
        self.admin = AdminService(
            cfg, self.state, self.tg, self.now,
            post_url=self.post_url,
            force_digest=self.force_digest,
            force_recap=self.force_recap,
            explain=self.explain,
            preview=self.preview_candidates,
            preview_post=self.preview_post,
            doctor=self.doctor_report,
        )
        self.alerts: list[str] = []
        self.stats = RunStats()
        self._feedback: dict[str, float] = self.state.feedback_by_source()
        self._run_ok = True

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------

    def run(self) -> int:
        start = time.time()
        self.now = start
        self.state.stats["runs"] = int(self.state.stats.get("runs", 0)) + 1
        self.state.stats["last_run_ts"] = int(start)
        log.info("Запуск news-bot %s", self.cfg.state_file)

        if not self.cfg.dry_run:
            me = self.tg.get_me()
            if not me:
                self._run_ok = False
                alert = "Telegram API недоступен: проверь BOT_TOKEN и доступ к api.telegram.org"
                log.error(alert)
                self._send_critical(alert)
                self._save()
                return 1
            username = (me or {}).get("username", "")
            if username:
                log.info("Telegram: @%s", username)

        self.admin.poll()

        paused = self.state.paused(self.now)
        if paused:
            log.info("Публикации на паузе до %s",
                     datetime.fromtimestamp(int(self.state.admin.get("paused_until", 0)), MSK))
        else:
            report = self.fetcher.fetch_all()
            self._collect_feed_alerts(report)
            self.process_items(report.items)

        # Дайджест отправляем после сбора: если бот был недоступен ночью,
        # утренний дайджест всё равно увидит свежие сюжеты.
        self.digest.now = self.now
        digest_result = self.digest.maybe_send(self._send_digest)
        if digest_result.sent:
            log.info("Дайджест: %s", digest_result.slot)
        weekly = self.digest.maybe_send_weekly(self._send_digest)
        if weekly.sent:
            log.info("Недельный обзор: %s", weekly.slot)

        if not paused:
            self.post_pending()
            self.apply_updates()

        self.stats.errors = len(self.alerts)
        self.state.stats["last_run_ok"] = self._run_ok
        self.state.stats["last_success_ts"] = int(time.time())
        self._flush_alerts()
        self._save()
        self._heartbeat()
        log.info(
            "Итог: получено %d, свежих %d, дублей %d, постов %d, обновлений %d, ошибок %d",
            self.stats.fetched, self.stats.fresh, self.stats.duplicates,
            self.stats.posted, self.stats.updated, self.stats.errors,
        )
        return 0

    # ------------------------------------------------------------------
    # Сбор и дедупликация
    # ------------------------------------------------------------------

    def process_items(self, items: list[RawItem]) -> None:
        now = self.now
        self.digest.now = now
        self.stats.fetched = len(items)
        candidates: list[Candidate] = []
        seen: set[str] = set()

        for item in items:
            canonical = canonical_url(item.link)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            if canonical in self.state.posted:
                self.stats.already_posted += 1
                self.state.record_rejection(item.link, "already-posted", now)
                continue
            if self.state.source_cap_reached(item.source, now):
                self.state.record_rejection(item.link, "source-cap", now)
                continue
            title = normalize_title_for_dedup(clean_html(item.title))
            if not title:
                self.state.record_rejection(item.link, "empty-title", now)
                continue
            if is_low_value(title, self.cfg.skip_patterns):
                self.stats.low_value += 1
                self.state.record_rejection(item.link, "low-value", now)
                continue
            age = now - item.published_ts
            if age > self.cfg.freshness_hours * 3600:
                self.stats.stale += 1
                self.state.record_rejection(item.link, "stale", now)
                continue
            hint = item.feed_category if item.feed_category != "мир" else ""
            cats = detect_categories(item.source, title, item.summary, hint)
            category = cats[0][0] if cats else "мир"
            breaking = is_breaking(title, item.summary, self.cfg.breaking_keywords)
            opinion = is_opinion(title, item.link, self.cfg.opinion_patterns)
            weight = self.cfg.source_weight_map.get(item.source, item.feed_weight or 1.0)
            score = importance(
                published_ts=item.published_ts,
                now=now,
                source_weight=weight,
                category=category,
                breaking=breaking,
                cluster_size=1,
                feedback=self._feedback.get(item.source, 0.0),
                opinion=opinion,
                title=title,
                feedback_weight=self.cfg.feedback_weight,
            )
            candidate = Candidate(
                url=item.link,
                canonical=canonical,
                source=item.source,
                title=title,
                title_ru="",
                summary=item.summary,
                published_ts=item.published_ts,
                category=category,
                cats=cats,
                image=item.image,
                video=item.video,
                breaking=breaking,
                opinion=opinion,
                score=score,
            )
            candidates.append(candidate)

        candidates.sort(key=lambda c: -c.score)
        pool = max(16, self.cfg.title_translate_limit * 3, self.cfg.max_posts_per_run * 10)
        candidates = candidates[:pool]
        self.stats.fresh = len(candidates)

        self._translate_titles_for_dedup(candidates, now)

        if self.translator.has_embeddings and candidates:
            vectors = self.translator.embed([c.title_ru or c.title for c in candidates])
            if vectors:
                for candidate, vector in zip(candidates, vectors):
                    candidate.embedding = vector or None

        translate_budget = max(12, self.cfg.title_translate_limit * 2)
        llm_title_budget = self.cfg.llm_title_budget if self.translator.has_llm else 0
        for candidate in candidates:
            candidate.prepare()
            match = self.deduper.find(candidate)
            if match:
                cluster = self.state.clusters.get(match.cluster_id)
                if not cluster:
                    continue
                added = self.deduper.attach(cluster, candidate)
                if added:
                    self.stats.duplicates += 1
                    self.digest.add_entry(cluster, now)
                    if cluster.get("posted"):
                        cluster["pending_update"] = True
                else:
                    self.stats.duplicates += 1
                if translate_budget > 0:
                    provider = self._ensure_cluster_title(
                        cluster, now, allow_llm=llm_title_budget > 0
                    )
                    translate_budget -= 1
                    if provider == "llm":
                        llm_title_budget -= 1
            else:
                cluster = self.deduper.create(candidate)
                self.stats.clusters += 1
                if translate_budget > 0:
                    provider = self._ensure_cluster_title(
                        cluster, now, allow_llm=llm_title_budget > 0
                    )
                    translate_budget -= 1
                    if provider == "llm":
                        llm_title_budget -= 1
                self.digest.add_entry(cluster, now)
            self._refresh_cluster(cluster, candidate)

        log.info(
            "Собрано %d, свежих %d (устаревших %d, мусора %d, уже было %d), "
            "дублей %d, новых сюжетов %d",
            self.stats.fetched, self.stats.fresh, self.stats.stale,
            self.stats.low_value, self.stats.already_posted,
            self.stats.duplicates, self.stats.clusters,
        )

    def _translate_titles_for_dedup(self, candidates: list[Candidate], now: float) -> None:
        budget = self.cfg.title_translate_limit
        for candidate in candidates:
            if is_russian(candidate.title):
                candidate.title_ru = candidate.title
                continue
            if budget <= 0:
                candidate.title_ru = ""
                continue
            result = self.translator.translate(candidate.title, mode="title", allow_llm=False)
            budget -= 1
            if result.ok and result.provider != "source":
                candidate.title_ru = result.text
            else:
                candidate.title_ru = ""

    def _ensure_cluster_title(self, cluster: dict, now: float,
                              allow_llm: bool = False) -> str:
        title_ru = str(cluster.get("title_ru") or "")
        if title_ru and cyrillic_ratio(title_ru) >= 0.3:
            cluster["translated"] = bool(cluster.get("translated")) or not is_russian(cluster.get("title", ""))
            return "cache"
        original = str(cluster.get("title") or "")
        if not original:
            return ""
        if is_russian(original):
            cluster["title_ru"] = original
            cluster["translated"] = False
            return "source"
        result = self.translator.translate(original, mode="title", allow_llm=allow_llm)
        if result.ok and result.provider != "source":
            cluster["title_ru"] = result.text
            cluster["translated"] = True
            return result.provider
        cluster.setdefault("title_ru", "")
        cluster["translation_failed"] = True
        return ""

    def _refresh_cluster(self, cluster: dict, candidate: Candidate) -> None:
        cluster["score"] = max(float(cluster.get("score", 0)), float(candidate.score))
        cluster["breaking"] = bool(cluster.get("breaking")) or candidate.breaking
        cluster["opinion"] = bool(cluster.get("opinion")) or candidate.opinion
        if not cluster.get("category"):
            cluster["category"] = candidate.category
        if not cluster.get("cats"):
            cluster["cats"] = [[c, float(s)] for c, s in candidate.cats]
        media = cluster.setdefault("media", {})
        if candidate.image and not media.get("image"):
            media["image"] = candidate.image
        if candidate.video and not media.get("video"):
            media["video"] = candidate.video
        if not cluster.get("summary") and candidate.summary:
            cluster["summary"] = candidate.summary

    # ------------------------------------------------------------------
    # Публикация
    # ------------------------------------------------------------------

    def _cluster_rank(self, cluster: dict, now: float) -> float:
        published = float(cluster.get("published_ts") or cluster.get("first_seen") or now)
        age_h = max(0.0, (now - published) / 3600.0)
        size = max(1, int(cluster.get("size", len(cluster.get("links") or [])) or 1))
        rank = float(cluster.get("score", 0.0)) + 1.25 * math.exp(-age_h / 18.0)
        rank += 0.25 * math.log2(size)
        if cluster.get("breaking"):
            rank += 0.35
        return rank

    def _hourly_posts(self, now: float) -> int:
        cutoff = now - 3600
        clusters = {
            r.get("cluster")
            for r in self.state.posted.values()
            if int(r.get("ts", 0)) >= cutoff
        }
        return len(clusters)

    def _global_block(self, now: float) -> str:
        """Общая причина, по которой сейчас нельзя публиковать ('' — можно)."""
        counters = self.state.counter_today(now)
        if int(counters.get("posts", 0)) >= self.cfg.max_posts_per_day:
            return f"дневной лимит постов ({self.cfg.max_posts_per_day})"
        if self._hourly_posts(now) >= self.cfg.max_posts_per_hour:
            return f"часовой лимит постов ({self.cfg.max_posts_per_hour})"
        if self.state.paused(now):
            until = int(self.state.admin.get("paused_until", 0))
            if until:
                return f"пауза до {datetime.fromtimestamp(until, MSK):%d.%m %H:%M} МСК"
            return "публикации на паузе"
        local = datetime.fromtimestamp(now, MSK)
        quiet = in_quiet_hours(local, self.cfg.quiet_hours)
        if quiet and not self.cfg.breaking_at_night:
            end = quiet_end(local, self.cfg.quiet_hours)
            return f"тихие часы до {end:%H:%M} МСК" if end else "тихие часы"
        return ""

    def _is_quiet(self, now: float) -> bool:
        return in_quiet_hours(datetime.fromtimestamp(now, MSK), self.cfg.quiet_hours)

    def _eligible_pool(self, now: float) -> list[dict]:
        """Свежие неопубликованные сюжеты, отсортированные по приоритету."""
        freshness = self.cfg.freshness_hours * 3600
        quiet = self._is_quiet(now)
        eligible: list[dict] = []
        for cluster in self.state.clusters.values():
            if cluster.get("posted") or cluster.get("skipped"):
                continue
            published = float(cluster.get("published_ts") or cluster.get("first_seen") or 0)
            if published and now - published > freshness:
                cluster["skipped"] = "stale"
                continue
            if float(cluster.get("score", 0)) < self.cfg.min_score:
                continue
            if quiet and not cluster.get("breaking"):
                continue
            eligible.append(cluster)
        eligible.sort(key=lambda c: -self._cluster_rank(c, now))
        return eligible

    def _pick(self, pool: list[dict], now: float) -> tuple[list[dict], dict[str, str]]:
        """Отбирает посты по квотам. Возвращает выбранные и причины пропуска."""
        selected: list[dict] = []
        reasons: dict[str, str] = {}
        per_source: dict[str, int] = {}
        per_category: dict[str, int] = {}
        for cluster in pool:
            cid = str(cluster.get("id", ""))
            source = str(cluster.get("source", ""))
            category = str(cluster.get("category", "мир"))
            if len(selected) >= self.cfg.max_posts_per_run:
                reasons[cid] = f"лимит {self.cfg.max_posts_per_run} постов за запуск"
                continue
            if per_source.get(source, 0) >= self.cfg.max_per_source_per_run:
                reasons[cid] = (
                    f"лимит {self.cfg.max_per_source_per_run} постов с одного источника за запуск"
                )
                continue
            if per_category.get(category, 0) >= self.cfg.max_per_category_per_run:
                reasons[cid] = (
                    f"лимит {self.cfg.max_per_category_per_run} постов на категорию за запуск"
                )
                continue
            if self.state.source_cap_reached(source, now):
                reasons[cid] = f"суточный лимит источника ({self.cfg.max_per_source_per_day})"
                continue
            selected.append(cluster)
            per_source[source] = per_source.get(source, 0) + 1
            per_category[category] = per_category.get(category, 0) + 1
        return selected, reasons

    def post_pending(self) -> None:
        now = self.now
        block = self._global_block(now)
        if block:
            log.info("Публикации отложены: %s", block)
            return

        eligible = self._eligible_pool(now)
        selected, _ = self._pick(eligible, now)
        quiet = self._is_quiet(now)

        if not selected:
            log.info("Нет подходящих сюжетов для публикации (кандидатов %d)", len(eligible))
            return

        for cluster in selected:
            ok, reason = self.post_cluster(cluster, now, silent=quiet)
            if ok:
                self.stats.posted += 1
                self.state.save(now)
            else:
                cluster["attempts"] = int(cluster.get("attempts", 0)) + 1
                if int(cluster["attempts"]) >= MAX_ATTEMPTS_PER_CLUSTER:
                    cluster["skipped"] = "error"
                    self.add_alert(
                        f"Сюжет «{cluster.get('title', '')[:80]}» пропущен после "
                        f"{MAX_ATTEMPTS_PER_CLUSTER} ошибок: {reason[:160]}"
                    )
                log.warning("Не удалось опубликовать «%s»: %s", cluster.get("title", "")[:60], reason)
            time.sleep(self.cfg.post_delay)

    def _compose(self, cluster: dict, now: float,
                 article: Article | None = None) -> "Composed | None":
        """Готовит заголовок, лид, теги, медиа и подпись. Ничего не отправляет."""
        links = cluster.get("links") or []
        if not links:
            return None
        primary = links[0]
        url = primary.get("url", "")
        source = str(cluster.get("source") or primary.get("source") or host_of(url))
        category = str(cluster.get("category") or "мир")
        chat_id = self.cfg.chat_for_category(category)

        if article is None:
            article = fetch_article(self.cfg, self.session, url) if url else Article()
        summary = str(primary.get("summary") or cluster.get("summary") or "")
        body = article.body or summary
        feed = self.cfg.feed_by_name(source)
        hint = feed.category if feed and feed.category != "мир" else ""

        translated = bool(cluster.get("translated"))
        translation_failed = bool(cluster.get("translation_failed"))
        title_ru = str(cluster.get("title_ru") or "")
        original_title = str(cluster.get("title") or "")

        if not title_ru or cyrillic_ratio(title_ru) < 0.3:
            if is_russian(original_title):
                title_ru = original_title
                translated = False
            else:
                result = self.translator.translate(original_title, mode="title", allow_llm=True)
                if result.ok and result.provider != "source":
                    title_ru = result.text
                    translated = True
                    translation_failed = False
                else:
                    title_ru = original_title
                    translation_failed = True
                    log.warning("Заголовок не переведён: %s", result.error)

        lead_ru = ""
        summary_result = None
        if self.translator.has_llm and self.cfg.llm_summary and body:
            if not is_russian(body[:600]) or not is_russian(original_title):
                summary_result = self.translator.summarize(original_title, body, source)
        if summary_result:
            title_ru = summary_result.title
            lead_ru = summary_result.summary
            translated = True
            translation_failed = False
        else:
            lead_src = make_lead(article.body, summary, self.cfg.lead_limit)
            if lead_src:
                if is_russian(lead_src):
                    lead_ru = lead_src
                else:
                    result = self.translator.translate(lead_src, mode="text", allow_llm=True)
                    if result.ok and result.provider != "source":
                        lead_ru = result.text
                        translated = True
                    else:
                        lead_ru = ""
                        translation_failed = True
                        log.warning("Лид не переведён: %s", result.error)

        if lead_ru and len(lead_ru) > self.cfg.lead_limit:
            lead_ru = smart_cut(lead_ru, self.cfg.lead_limit)

        tags = detect_tags(
            source,
            original_title,
            summary,
            source_hint=hint,
            opinion=bool(cluster.get("opinion")),
        )
        cluster["title_ru"] = title_ru or original_title
        cluster["lead_ru"] = lead_ru
        cluster["tags"] = tags
        cluster["translated"] = translated
        cluster["translation_failed"] = translation_failed
        cluster["category"] = category
        media = cluster.setdefault("media", {})
        if article.image:
            media["image"] = article.image
        if article.video:
            media["video"] = article.video
        # Все известные картинки: RSS, og:image из статьи, <img> со страницы.
        # При публикации пробуем их по очереди — если первая не отдаётся
        # Telegram, пост всё равно выйдет с картинкой.
        candidates: list[str] = []
        for candidate_image in (
            article.image,
            media.get("image"),
            *article.images,
            media.get("secondary_image"),
        ):
            value = str(candidate_image or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        if candidates:
            media["image"] = candidates[0]
            media["image_candidates"] = candidates[:4]

        caption = build_caption(self.cfg, cluster, lead=lead_ru, translated=translated)
        return Composed(
            url=url,
            source=source,
            category=category,
            chat_id=chat_id,
            caption=caption,
            article=article,
            image_candidates=tuple(media.get("image_candidates") or ()),
        )

    def _buttons(self, cluster: dict) -> list[list[dict]] | None:
        """Кнопки под постом: дополнительные источники и (опционально) 👍/👎."""
        rows: list[list[dict]] = []
        if self.cfg.source_buttons:
            rows.extend(source_buttons(cluster))
        if self.cfg.feedback_buttons:
            rows.extend(feedback_keyboard(str(cluster.get("id", ""))))
        return rows or None

    def post_cluster(self, cluster: dict, now: float, silent: bool = False,
                     article: Article | None = None) -> tuple[bool, str]:
        composed = self._compose(cluster, now, article)
        if composed is None:
            return False, "нет ссылок"
        links = cluster.get("links") or []
        primary = links[0]
        url, source, category = composed.url, composed.source, composed.category
        chat_id, caption = composed.chat_id, composed.caption
        media = cluster.get("media") or {}
        buttons = self._buttons(cluster)
        translated = bool(cluster.get("translated"))
        lead_ru = str(cluster.get("lead_ru") or "")
        original_title = str(cluster.get("title") or "")

        sent_kind = ""
        message_id: int | None = None
        errors: list[str] = []

        video_url = str(media.get("video") or "")
        if video_url:
            # Сначала отдаём ссылку Telegram (так работала исходная версия),
            # при ошибке — скачиваем и загружаем файлом.
            result = self.tg.send_video(chat_id, caption, url=video_url, buttons=buttons,
                                        silent=silent)
            if not result.ok:
                downloaded = download_video(self.cfg, self.session, video_url)
                if downloaded:
                    data, filename, ctype = downloaded
                    result = self.tg.send_video(chat_id, caption, data=data, url=video_url,
                                                filename=filename, mime=ctype,
                                                buttons=buttons, silent=silent)
            if result.ok:
                sent_kind, message_id = "video", result.message_id
            else:
                errors.append(f"video: {result.error}")
                log.warning("Видео не отправлено (%s): %s", video_url[:80], result.error)

        if not sent_kind:
            for image_url in composed.image_candidates:
                result = self.tg.send_photo(chat_id, caption, url=image_url,
                                            buttons=buttons, silent=silent)
                if not result.ok:
                    downloaded = download_image(self.cfg, self.session, image_url)
                    if downloaded:
                        data, filename, ctype = downloaded
                        result = self.tg.send_photo(chat_id, caption, data=data,
                                                    url=image_url, filename=filename,
                                                    mime=ctype, buttons=buttons,
                                                    silent=silent)
                if result.ok:
                    sent_kind, message_id = "photo", result.message_id
                    media["image"] = image_url
                    break
                errors.append(f"photo: {result.error}")
                log.warning("Фото не отправлено (%s): %s", image_url[:80], result.error)

        if not sent_kind:
            text = build_full_text(self.cfg, cluster, lead=lead_ru, translated=translated)
            result = self.tg.send_text(chat_id, text, preview=False, buttons=buttons, silent=silent)
            if result.ok:
                sent_kind, message_id = "text", result.message_id
            else:
                errors.append(f"text: {result.error}")

        if not sent_kind:
            return False, "; ".join(errors)[:400]

        cluster["posted"] = True
        cluster["posted_ts"] = now
        cluster["messages"] = cluster.get("messages") or {}
        cluster["messages"][chat_id] = {"id": message_id, "kind": sent_kind}
        cluster["updates"] = 0
        cluster["last_update_ts"] = now
        cluster["pending_update"] = False

        record = {
            "ts": int(now),
            "url": url,
            "canonical": primary.get("canonical") or canonical_url(url),
            "title": original_title,
            "title_ru": cluster.get("title_ru", ""),
            "source": source,
            "category": category,
            "cluster": cluster.get("id", ""),
            "message_id": message_id,
            "kind": sent_kind,
            "score": float(cluster.get("score", 0)),
            "translated": translated,
        }
        self.state.add_posted(record, now)
        for link in links[1:]:
            alias = dict(record)
            alias["canonical"] = link.get("canonical") or canonical_url(link.get("url", ""))
            alias["url"] = link.get("url", "")
            alias["alias"] = True
            if alias["canonical"]:
                self.state.posted[alias["canonical"]] = alias
        self.digest.add_entry(cluster, now)
        self.state.stats["last_post_ts"] = int(now)
        log.info(
            "Опубликовано [%s] %s — %s (%s)",
            sent_kind, source, original_title[:70], cluster.get("id", "")[:8],
        )
        return True, ""

    # ------------------------------------------------------------------
    # Обновления
    # ------------------------------------------------------------------

    def apply_updates(self) -> None:
        if not self.cfg.edit_updates:
            return
        now = self.now
        interval = self.cfg.update_min_interval_min * 60
        for cluster in self.state.clusters.values():
            if not cluster.get("pending_update"):
                continue
            if not cluster.get("posted"):
                cluster["pending_update"] = False
                continue
            if int(cluster.get("updates", 0)) >= self.cfg.max_updates_per_cluster:
                cluster["pending_update"] = False
                continue
            last_update = float(cluster.get("last_update_ts", 0))
            if last_update and now - last_update < interval:
                continue
            messages = cluster.get("messages") or {}
            if not messages:
                cluster["pending_update"] = False
                continue
            caption = build_caption(
                self.cfg, cluster,
                lead=cluster.get("lead_ru", ""),
                update=True,
                translated=bool(cluster.get("translated")),
            )
            ok_any = False
            for chat_id, info in messages.items():
                kind = info.get("kind", "text")
                message_id = int(info.get("id") or 0)
                if kind in {"photo", "video"}:
                    result = self.tg.edit_caption(chat_id, message_id, caption)
                else:
                    text = build_full_text(
                        self.cfg, cluster,
                        lead=cluster.get("lead_ru", ""),
                        update=True,
                        translated=bool(cluster.get("translated")),
                    )
                    result = self.tg.edit_text(chat_id, message_id, text)
                if result.ok:
                    ok_any = True
                else:
                    log.warning("Не удалось обновить пост %s: %s", message_id, result.error)
            cluster["pending_update"] = False
            if ok_any:
                cluster["updates"] = int(cluster.get("updates", 0)) + 1
                cluster["last_update_ts"] = now
                self.stats.updated += 1
                self.digest.add_entry(cluster, now)
                log.info("Обновлён пост «%s» (%d источников)",
                         cluster.get("title", "")[:60], int(cluster.get("size", 1)))

    # ------------------------------------------------------------------
    # Дайджесты
    # ------------------------------------------------------------------

    def _send_digest(self, chat_id: str, text: str) -> bool:
        return self.tg.send_text(chat_id, text, preview=False).ok

    def _digest_text(self, weekly: bool = False) -> tuple[str, int]:
        """Текст дайджеста без отправки: (html, сколько новостей)."""
        now = time.time()
        self.digest.now = now
        local = datetime.fromtimestamp(now, MSK)
        slot = f"{local:%Y-%m-%d}T{local.hour:02d}"
        if weekly:
            picked = self.digest.select(
                now - 7 * 86400,
                limit=self.cfg.weekly_recap_items,
                max_per_source=max(1, self.cfg.digest_max_per_source),
            )
            if not picked:
                return "", 0
            return self.digest.render(picked, slot, weekly=True), len(picked)
        picked = self.digest.select(now - 24 * 3600)
        if not picked:
            return "", 0
        return self.digest.render(picked, slot), len(picked)

    def preview_digest(self, weekly: bool = False) -> tuple[str, int]:
        """Дайджест для --dry-run: ничего не отправляет и не меняет слот."""
        return self._digest_text(weekly)

    def force_digest(self) -> str:
        text, count = self._digest_text(False)
        if not count:
            return "Нет новостей для дайджеста"
        ok = self.tg.send_text(self.cfg.channel_id, text, preview=False).ok
        if ok:
            self.digest.state.stats["last_digest_ts"] = int(time.time())
            return f"✅ Дайджест отправлен ({count} новостей)"
        return "❌ Не удалось отправить дайджест"

    def force_recap(self) -> str:
        text, count = self._digest_text(True)
        if not count:
            return "Нет новостей для недельного обзора"
        ok = self.tg.send_text(self.cfg.channel_id, text, preview=False).ok
        if ok:
            return f"✅ Недельный обзор отправлен ({count} новостей)"
        return "❌ Не удалось отправить обзор"

    # ------------------------------------------------------------------
    # Ручные операции и диагностика: /post, /test, /why, /preview, /doctor
    # ------------------------------------------------------------------

    _REJECTION_LABELS = {
        "already-posted": "уже была опубликована — ссылка есть в истории постов",
        "source-cap": "исчерпан суточный лимит источника",
        "low-value": "заголовок похож на служебный или кликбейт (SKIP_PATTERNS)",
        "stale": "слишком старая новость (FRESHNESS_HOURS)",
        "empty-title": "пустой заголовок после очистки",
    }

    def _manual_cluster(self, url: str, now: float,
                        register: bool = True) -> "tuple[dict, Article] | str":
        """Загружает статью и собирает кластер. При ошибке возвращает текст."""
        url = (url or "").strip()
        article = fetch_article(self.cfg, self.session, url)
        title = article.title or host_of(url) or url
        title = normalize_title_for_dedup(title) or title
        source = host_of(url) or "manual"
        summary = article.description or ""
        if not article.body and not summary and not article.image:
            return f"❌ Не удалось загрузить статью: {article.error or 'нет текста'}"
        cats = detect_categories(source, title, summary)
        category = cats[0][0] if cats else "мир"
        breaking = is_breaking(title, summary, self.cfg.breaking_keywords)
        candidate = Candidate(
            url=url,
            canonical=canonical_url(url) or url,
            source=source,
            title=title,
            title_ru="",
            summary=summary,
            published_ts=now,
            category=category,
            cats=cats,
            image=article.image,
            video=article.video,
            breaking=breaking,
            score=10.0,
        ).prepare()
        cluster = self.deduper.create(
            candidate,
            force_id=stable_hash("manual", url, now, length=20),
            register=register,
        )
        self._ensure_cluster_title(cluster, now)
        self._refresh_cluster(cluster, candidate)
        return cluster, article

    def post_url(self, url: str) -> str:
        now = time.time()
        self.now = now
        prepared = self._manual_cluster(url, now)
        if isinstance(prepared, str):
            return prepared
        cluster, article = prepared
        ok, reason = self.post_cluster(cluster, now, article=article)
        if ok:
            self.state.save(now)
            return f"✅ Опубликовано: {cluster.get('title_ru') or cluster.get('title')}"
        return f"❌ Не удалось опубликовать: {reason}"

    def preview_post(self, url: str) -> str:
        """Черновик поста по ссылке: ничего не публикует и не пишет в state."""
        now = time.time()
        self.now = now
        prepared = self._manual_cluster(url, now, register=False)
        if isinstance(prepared, str):
            return prepared
        cluster, article = prepared
        composed = self._compose(cluster, now, article)
        if composed is None:
            return "❌ Не удалось собрать пост: нет ссылок"
        buttons = self._buttons(cluster) or []
        lines = [
            "🧪 <b>Черновик поста</b> — в канал не отправлен",
            "",
            composed.caption,
            "",
            "— — —",
            f"Категория: #{esc(composed.category)} → <code>{esc(composed.chat_id)}</code>",
            f"Скор: {float(cluster.get('score', 0)):.1f}",
        ]
        if buttons:
            labels = ", ".join(row[0].get("text", "") for row in buttons)
            lines.append(f"Кнопки: {esc(labels)}")
        if cluster.get("translation_failed"):
            lines.append("⚠️ Перевод не удался — часть текста осталась на языке оригинала.")
        return "\n".join(lines)

    def _find_cluster(self, query: str, canon: str = "") -> dict | None:
        query = (query or "").strip()
        if not query:
            return None
        lowered = query.casefold()
        clusters = list(self.state.clusters.values())
        if canon:
            for cluster in clusters:
                for link in cluster.get("links") or []:
                    if link.get("canonical") == canon:
                        return cluster
        if len(lowered) >= 4:
            for cluster in clusters:
                if str(cluster.get("id", "")).startswith(lowered):
                    return cluster
        if len(lowered) >= 4:
            for cluster in clusters:
                text = (
                    str(cluster.get("title", "")) + " " + str(cluster.get("title_ru", ""))
                ).casefold()
                if lowered in text:
                    return cluster
        return None

    def _queue_reasons(self, cluster: dict, now: float) -> list[str]:
        """Почему сюжет ещё не опубликован (без побочных эффектов)."""
        reasons: list[str] = []
        block = self._global_block(now)
        if block:
            reasons.append(block)
        score = float(cluster.get("score", 0))
        if score < self.cfg.min_score:
            reasons.append(f"скор {score:.1f} ниже MIN_SCORE={self.cfg.min_score}")
        published = float(cluster.get("published_ts") or cluster.get("first_seen") or 0)
        age_h = (now - published) / 3600 if published else 0.0
        if age_h > self.cfg.freshness_hours:
            reasons.append(f"возраст {age_h:.0f} ч больше FRESHNESS_HOURS={self.cfg.freshness_hours}")
        source = str(cluster.get("source", ""))
        if self.state.source_cap_reached(source, now):
            reasons.append(f"исчерпан суточный лимит источника ({self.cfg.max_per_source_per_day})")
        if self._is_quiet(now) and not cluster.get("breaking"):
            reasons.append("тихие часы: сюжет не breaking")

        freshness = self.cfg.freshness_hours * 3600
        pool = [
            c for c in self.state.clusters.values()
            if not c.get("posted") and not c.get("skipped")
            and float(c.get("score", 0)) >= self.cfg.min_score
            and (
                not float(c.get("published_ts") or 0)
                or now - float(c.get("published_ts") or 0) <= freshness
            )
        ]
        pool.sort(key=lambda c: -self._cluster_rank(c, now))
        ids = [str(c.get("id", "")) for c in pool]
        cid = str(cluster.get("id", ""))
        if cid in ids:
            position = ids.index(cid) + 1
            if position > self.cfg.max_posts_per_run:
                reasons.append(
                    f"в очереди {position}-е место, за запуск выходит "
                    f"{self.cfg.max_posts_per_run}"
                )
        return reasons

    def _explain_posted(self, record: dict) -> str:
        ts = int(record.get("ts", 0))
        when = datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M") if ts else "—"
        lines = [
            "✅ <b>Новость уже опубликована</b>",
            f"<b>{esc(str(record.get('title_ru') or record.get('title') or '—'))}</b>",
            f"{esc(str(record.get('source') or '—'))} · #{esc(str(record.get('category') or 'мир'))}",
            f"Время: {when} МСК",
        ]
        if record.get("message_id"):
            lines.append(
                f"Сообщение: #{record.get('message_id')} в {esc(str(record.get('chat_id') or ''))}"
            )
        if record.get("alias"):
            lines.append("Пометка: это дубль другого поста (тот же сюжет)")
        return "\n".join(lines)

    def _explain_cluster(self, cluster: dict, now: float) -> str:
        title = cluster.get("title_ru") or cluster.get("title") or "—"
        lines = [
            "🔎 <b>Сюжет найден</b>",
            f"<b>{esc(str(title))}</b>",
            f"{esc(str(cluster.get('source') or '—'))} · #{esc(str(cluster.get('category') or 'мир'))}"
            f" · скор {float(cluster.get('score', 0)):.1f}"
            f" · {plural_sources(max(1, Deduper.distinct_sources(cluster)))}",
        ]
        first_seen = float(cluster.get("first_seen") or 0)
        if first_seen:
            lines.append(f"Появился: {datetime.fromtimestamp(first_seen, MSK):%d.%m %H:%M} МСК")
        if cluster.get("posted"):
            posted_ts = float(cluster.get("posted_ts") or 0)
            when = (
                datetime.fromtimestamp(posted_ts, MSK).strftime("%d.%m %H:%M")
                if posted_ts else "—"
            )
            lines.append(f"✅ Опубликован {when} МСК")
            for chat_id, info in (cluster.get("messages") or {}).items():
                lines.append(f"Сообщение: #{info.get('id')} в {esc(str(chat_id))}")
                break
            if cluster.get("pending_update"):
                lines.append("🔄 Ждёт обновления: пришёл новый источник")
            return "\n".join(lines)
        skipped = str(cluster.get("skipped") or "")
        if skipped == "stale":
            lines.append("⏳ Снят с очереди: новость старше FRESHNESS_HOURS")
            return "\n".join(lines)
        if skipped == "error":
            lines.append(
                f"❌ Снят после {MAX_ATTEMPTS_PER_CLUSTER} неудачных попыток публикации"
            )
            return "\n".join(lines)
        lines.append("🕓 <b>В очереди</b>")
        reasons = self._queue_reasons(cluster, now)
        if reasons:
            lines += [f"• {esc(reason)}" for reason in reasons]
        else:
            lines.append("• ограничений нет — выйдет при следующем запуске")
        return "\n".join(lines)

    def _explain_rejection(self, item: dict) -> str:
        reason = str(item.get("reason") or "")
        label = self._REJECTION_LABELS.get(reason, reason or "неизвестно")
        ts = int(item.get("ts", 0))
        when = datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M") if ts else "—"
        lines = [
            "🚫 <b>Новость отфильтрована</b>",
            f"Причина: {esc(label)}",
            f"Когда: {when} МСК",
            f"Ссылка: {esc(str(item.get('url') or ''))}",
        ]
        if reason == "low-value":
            lines.append("Правила задаются переменной SKIP_PATTERNS.")
        elif reason == "stale":
            lines.append(f"Порог свежести: FRESHNESS_HOURS={self.cfg.freshness_hours}.")
        return "\n".join(lines)

    def explain(self, query: str) -> str:
        """Объясняет судьбу новости по URL, id кластера или части заголовка."""
        query = (query or "").strip()
        if not query:
            return "Использование: <code>/why https://…</code> или <code>/why id-кластера</code>"
        canon = canonical_url(query) if is_http_url(query) else ""
        record = self.state.posted.get(canon) if canon else None
        if record:
            return self._explain_posted(record)
        cluster = self._find_cluster(query, canon)
        if cluster:
            return self._explain_cluster(cluster, self.now)
        rejection = self.state.find_rejection(query)
        if rejection:
            return self._explain_rejection(rejection)
        lines = [
            "🤷 <b>Ничего не нашёл</b>",
            f"Запрос: <code>{esc(smart_cut(query, 200))}</code>",
            "",
            "Возможные причины:",
            "• URL отличается от того, что пришёл в ленте — проверь ссылку без utm-меток;",
            "• новость старше KEEP_DAYS и выпала из истории;",
            "• новость ещё не приходила в ленты.",
            "",
            "Посмотреть очередь: /preview",
        ]
        return "\n".join(lines)

    def preview_candidates(self, limit: int = 8) -> str:
        """Топ очереди с причинами, почему сюжет ещё не вышел."""
        now = self.now
        limit = max(1, min(20, int(limit or 8)))
        block = self._global_block(now)
        pool = self._eligible_pool(now)
        selected, reasons = self._pick(pool, now)
        selected_ids = {str(c.get("id", "")) for c in selected}

        lines = ["<b>👀 Очередь публикации</b>", ""]
        if block:
            lines.append(f"⛔ Сейчас публикации заблокированы: {esc(block)}")
        else:
            lines.append(
                f"Сейчас выйдет {len(selected)} из {len(pool)} "
                f"(лимит {self.cfg.max_posts_per_run} за запуск)"
            )
        lines.append("")
        if not pool:
            lines.append("Очередь пуста: подходящих свежих сюжетов нет.")
            return "\n".join(lines)

        for index, cluster in enumerate(pool[:limit], 1):
            title = str(cluster.get("title_ru") or cluster.get("title") or "—")
            cid = str(cluster.get("id", ""))
            source = str(cluster.get("source") or "—")
            category = str(cluster.get("category") or "мир")
            size = max(1, int(cluster.get("size", len(cluster.get("links") or [])) or 1))
            score = float(cluster.get("score", 0))
            mark = "▶️" if cid in selected_ids else "•"
            line = f"{mark} {index}. <b>{esc(smart_cut(title, 140))}</b>"
            line += (
                f"\n    {esc(source)} · #{esc(category)} · скор {score:.1f}"
                f" · {plural_sources(size)}"
            )
            flags = []
            if cluster.get("breaking"):
                flags.append("breaking")
            if cluster.get("opinion"):
                flags.append("мнение")
            if flags:
                line += " · " + esc(", ".join(flags))
            reason = reasons.get(cid, "")
            if reason and cid not in selected_ids:
                line += f"\n    ⏸ {esc(reason)}"
            lines.append(line)
        if len(pool) > limit:
            lines.append(f"\n…и ещё {len(pool) - limit}. <code>/preview {min(20, len(pool))}</code>")
        return "\n".join(lines)

    def doctor_report(self) -> str:
        """Preflight-отчёт для Telegram: реальные проверки, без dry-run."""
        from .doctor import render, run

        real_tg = TelegramClient(self.cfg, self.session, dry_run=False)
        checks = run(
            self.cfg,
            session=self.session,
            tg=real_tg,
            state=self.state,
            now=self.now,
        )
        return esc(render(checks, self.now))

    # ------------------------------------------------------------------
    # Алерты и служебное
    # ------------------------------------------------------------------

    def add_alert(self, message: str) -> None:
        message = (message or "").strip()
        if message and message not in self.alerts:
            self.alerts.append(message)

    def _collect_feed_alerts(self, report) -> None:
        if report.failures:
            details = format_list(report.errors, 5)
            self.add_alert(
                f"RSS: {report.failures} лент с ошибками. {details}"
            )
        for alert in report.alerts:
            self.add_alert(alert)

    def _flush_alerts(self) -> None:
        if not self.alerts:
            return
        now = time.time()
        lines: list[str] = []
        for message in self.alerts[:10]:
            key = stable_hash("alert", message, length=16)
            if self.state.alert_allowed(key, now):
                lines.append(f"• {esc(message[:300])}")
        self.alerts.clear()
        if not lines:
            return
        text = "🚨 <b>News Bot — проблемы:</b>\n\n" + "\n".join(lines)
        if not self.tg.send_alert(text):
            log.warning("Не удалось отправить алерт админу")

    def _send_critical(self, message: str) -> None:
        self.tg.send_alert(f"🚨 <b>News Bot:</b>\n{esc(message)}")

    def _save(self) -> None:
        try:
            self.state.save(time.time())
        except Exception as exc:
            log.error("Не удалось сохранить состояние: %s", exc)
            self._run_ok = False

    def _heartbeat(self) -> None:
        if not self.cfg.heartbeat_url:
            return
        try:
            self.session.get(self.cfg.heartbeat_url, timeout=10)
        except Exception as exc:
            log.warning("Heartbeat: %s", exc)


def main(env: dict[str, str] | None = None) -> int:
    try:
        cfg = load_config(env)
    except ConfigError as exc:
        log = setup_logging()
        for issue in exc.issues:
            log.error("Конфигурация: %s", issue)
        return 2
    log = setup_logging(cfg.log_level)
    for warning in cfg.warnings:
        log.warning("Конфигурация: %s", warning)
    bot = NewsBot(cfg)
    try:
        return bot.run()
    except Exception as exc:
        log.exception("Критическая ошибка")
        try:
            bot.add_alert(f"Критическая ошибка: {exc!r}")
            bot._flush_alerts()
            bot.state.save(time.time())
        except Exception:
            pass
        return 1


def entrypoint() -> None:
    raise SystemExit(main())
