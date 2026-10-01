import os
import re
import time
import html
import logging

import requests
import feedparser
from deep_translator import GoogleTranslator

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")                 # секрет из GitHub
CHANNEL_ID = os.getenv("CHANNEL_ID", "@world_1news_bot")

RSS_FEEDS = {
    "BBC World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "DW":           "https://rss.dw.com/rdf/rss-en-all",
    "France 24":    "https://www.france24.com/en/rss",
    "The Guardian": "https://www.theguardian.com/world/rss",
}

MAX_PER_SOURCE = 5       # сколько последних новостей брать с источника
MAX_POSTS_PER_RUN = 5    # максимум постов за один запуск
POST_DELAY = 3           # пауза между постами, сек
POSTED_FILE = "posted_news.txt"

# ========== ЛОГИКА ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("news-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан! Проверь секреты GitHub")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
translator = GoogleTranslator(source="auto", target="ru")
google_blocked = False   # Google упёрся в лимит — дальше не дёргаем


def load_posted() -> set:
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(f.read().splitlines())
    except FileNotFoundError:
        return set()


def save_posted(link: str):
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")


def clean_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^<]+?>", "", raw or ""))


def translate_text(text: str) -> str:
    """Перевод на русский. Если Google упёрся в лимит — публикуем оригинал."""
    global google_blocked
    if not text or google_blocked:
        return text
    for attempt in range(1, 4):
        try:
            return translator.translate(text[:4000]) or text
        except Exception as e:
            log.warning(f"Перевод, попытка {attempt}/3 не удалась: {e}")
            time.sleep(8 * attempt)
    google_blocked = True
    log.error("Google ограничил запросы — остальные посты уйдут без перевода")
    return text


def fetch_news() -> list:
    news = []
    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:MAX_PER_SOURCE]:
                news.append({
                    "id": entry.link,
                    "source": source,
                    "title": clean_html(entry.get("title", "")),
                    "summary": clean_html(entry.get("summary", ""))[:400],
                    "link": entry.link,
                })
            log.info(f"{source}: получено {len(feed.entries)}")
        except Exception as e:
            log.error(f"Ошибка загрузки {source}: {e}")
    return news


def send_message(text: str) -> bool:
    """Отправка в канал. При ошибке показываем ПОЛНЫЙ ответ Telegram."""
    try:
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text[:4000], "parse_mode": "HTML"},
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"Telegram ответил {r.status_code}: {r.text[:500]}")
            return False
        return True
    except Exception as e:
        log.error(f"Сетевая ошибка при отправке: {e}")
        return False


def post_news():
    posted = load_posted()
    news = fetch_news()
    fresh = [n for n in news if n["id"] not in posted]
    log.info(f"Всего новостей: {len(news)}, новых: {len(fresh)}")

    published = 0
    for item in fresh[:MAX_POSTS_PER_RUN]:
        title_ru = translate_text(item["title"])
        summary_ru = translate_text(item["summary"])

        text = (
            f"🌍 <b>{html.escape(item['source'])}</b>\n\n"
            f"<b>{html.escape(title_ru)}</b>\n\n"
            f"{html.escape(summary_ru)}\n\n"
            f"🔗 <a href=\"{html.escape(item['link'])}\">Читать полностью</a>"
        )

        if send_message(text):
            save_posted(item["id"])
            published += 1
            log.info(f"Опубликовано: {title_ru[:60]}")
        time.sleep(POST_DELAY)

    log.info(f"Итог запуска: опубликовано {published}")


if __name__ == "__main__":
    post_news()
