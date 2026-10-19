"""Сборка постов и подписей с гарантией лимитов Telegram."""

from __future__ import annotations

import html as html_lib
import re
from typing import Iterable

from .config import Config
from .dedup import Deduper
from .util import esc, smart_cut

CATEGORY_ICONS = {
    "мир": "🌍",
    "политика": "🏛",
    "конфликты": "🚨",
    "происшествия": "🚨",
    "экономика": "💰",
    "технологии": "💻",
    "наука": "🔬",
    "спорт": "⚽",
    "культура": "🎬",
    "игры": "🎮",
    "железо": "🖥",
    "мнение": "💬",
}


def plural_sources(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} источник"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} источника"
    return f"{n} источников"


def category_icon(category: str) -> str:
    return CATEGORY_ICONS.get(category or "мир", "🌍")


def source_line(source: str, category: str) -> str:
    icon = category_icon(category)
    return f"{icon} <b>{esc(source)}</b>"


def labels_for(cfg: Config, cluster: dict, translated: bool = False) -> list[str]:
    labels: list[str] = []
    if cfg.confirmation_label:
        sources = Deduper.distinct_sources(cluster)
        if sources >= 2:
            labels.append(f"✅ подтверждено: {plural_sources(sources)}")
        else:
            labels.append("⚠️ один источник")
    if cfg.opinion_label and cluster.get("opinion"):
        labels.append("💬 мнение")
    if cfg.translation_note and translated:
        labels.append("🤖 автоматический перевод")
    if cluster.get("translation_failed"):
        labels.append("🌐 перевод недоступен")
    return labels


def build_caption(cfg: Config, cluster: dict, *, lead: str | None = None,
                  update: bool = False, translated: bool | None = None) -> str:
    """Подпись к фото/видео или текст поста, всегда <= CAPTION_LIMIT."""
    links = cluster.get("links") or []
    url = links[0].get("url", "") if links else ""
    title = cluster.get("title_ru") or cluster.get("title") or ""
    source = cluster.get("source", "")
    category = cluster.get("category", "мир")
    lead = (lead if lead is not None else cluster.get("lead_ru", "")) or ""
    tags = cluster.get("tags", "") or ""
    if translated is None:
        translated = bool(cluster.get("translated"))

    header = source_line(source, category)
    if update:
        header = "🔄 <b>Обновлено</b> · " + header

    link_html = (
        f'🔗 <a href="{esc(url, quote=True)}">Читать в оригинале</a>' if url else ""
    )
    labels = labels_for(cfg, cluster, translated=translated)
    others = Deduper.also_reported(cluster, 2)
    also = f"Также сообщали: {esc(', '.join(others))}" if others else ""
    footer_parts = []
    if tags:
        footer_parts.append(esc(tags))
    if labels:
        footer_parts.append(" · ".join(esc(x) for x in labels))
    if also:
        footer_parts.append(esc(also))
    footer = "\n".join(footer_parts)
    tail = f"\n\n{footer}" if footer else ""
    tail += f"\n\n{link_html}" if link_html else ""

    lead_esc = esc(lead).strip()
    title_plain = title
    for attempt in range(4):
        if attempt == 0:
            title_esc = esc(smart_cut(title_plain, 240, mark=True)) if len(esc(title_plain)) > 240 else esc(title_plain)
        elif attempt == 1:
            title_esc = esc(smart_cut(title_plain, 160, mark=True)) if len(esc(title_plain)) > 160 else esc(title_plain)
        else:
            title_esc = esc(smart_cut(title_plain, 100, mark=True)) if len(esc(title_plain)) > 100 else esc(title_plain)
        header_block = f"{header}\n\n<b>{title_esc}</b>"
        budget = cfg.caption_limit - len(header_block) - len(tail) - 4
        current_lead = lead_esc
        if budget > 10 and current_lead:
            current_lead = smart_cut(current_lead, budget)
        elif budget <= 10:
            current_lead = ""
        text = f"{header_block}\n\n{current_lead}{tail}" if current_lead else f"{header_block}{tail}"
        if len(text) <= cfg.caption_limit:
            return text
        # Не влезло: уменьшаем лид и пробуем снова, не обрезая ссылку.
        if lead_esc:
            excess = len(text) - cfg.caption_limit
            new_limit = max(0, len(lead_esc) - excess - 8)
            lead_esc = smart_cut(lead_esc, new_limit) if new_limit > 20 else ""
    # Последний резерв: заголовок и ссылка без лида.
    title_esc = esc(smart_cut(title_plain, 120, mark=True))
    header_block = f"{header}\n\n<b>{title_esc}</b>"
    return f"{header_block}{tail}"[: cfg.caption_limit]


def build_full_text(cfg: Config, cluster: dict, *, lead: str | None = None,
                    update: bool = False, translated: bool | None = None) -> str:
    """Текст без медиа: лимит 4096, не 1024."""
    cfg2 = cfg.with_overrides(caption_limit=cfg.tg_limit)
    return build_caption(cfg2, cluster, lead=lead, update=update, translated=translated)


def format_digest_item(index: int, entry: dict, cluster: dict | None) -> str:
    title = esc(entry.get("title", ""))
    source = esc(entry.get("source", ""))
    url = esc(entry.get("url", ""), quote=True)
    suffix = ""
    if cluster:
        others = Deduper.also_reported(cluster, 2)
        if others:
            suffix = " · " + esc(", ".join(others))
    return f"{index}. <b>{title}</b> — <a href=\"{url}\">{source}</a>{suffix}"


def escape_join(values: Iterable[str], sep: str = " · ") -> str:
    return sep.join(esc(v) for v in values if v)


def strip_html(value: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", value or ""))
