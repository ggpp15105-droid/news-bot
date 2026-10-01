import os
import re
import time
import html
import logging

import requests
import feedparser
from deep_translator import GoogleTranslator

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")                        # секрет GitHub
CHANNEL_ID = os.getenv("CHANNEL_ID", "@world_1news_bot")  # секрет GitHub
MM_EMAIL = os.getenv("MM_EMAIL", "")  # почта для MyMemory: лимит 50 000 симв/день вместо 5 000

RSS_FEEDS = {
    "BBC World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "DW":           "https://rss.dw.com/rdf/rss-en-all",
    "France 24":    "https://www.france24.com/en/rss",
    "The Guardian": "https://www.theguardian.com/world/rss",
}

MAX_PER_SOURCE = 5       # последних новостей с каждого источника
MAX_POSTS_PER_RUN = 5    # максимум постов за один запуск
POST_DELAY = 3           # пауза между постами (сек)
POSTED_FILE = "posted_news.txt"

# ========== ЛОГИКА ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("news-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан! Проверь секреты GitHub")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
google = GoogleTranslator(source="auto", target="ru")
google_off = False


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


# ---------- ПЕРЕВОД ----------
# №1 MyMemory — без ключей (основной), №2 Google — запасной, №3 оригинал

def translate_mymemory(text: str):
    try:
        params = {"q": text[:480], "langpair": "en|ru"}  # все источники на английском
        if MM_EMAIL:
            params["de"] = MM_EMAIL
        r = requests.get("https://api.mymemory.translated.net/get",
                         params=params, timeout=20)
        data = r.json()
        if str(data.get("responseStatus")) == "200":
            t = (data.get("responseData") or {}).get("translatedText", "")
            if t and "MYMEMORY WARNING" not in t:
                return t
    except Exception as e:
        log.warning(f"MyMemory: {e}")
    return None


def translate_google(text: str):
    global google_off
    if google_off:
        return None
    for attempt in range(1, 3):
        try:
            result = google.translate(text[:4000])
            if result:
                return result
        except Exception as e:
            log.warning(f"Google, попытка {attempt}/2: {e}")
            time.sleep(4)
    google_off = True
    log.warning("Google отключён до конца запуска — лимит IP")
    return None


def translate_text(text: str) -> str:
    if not text:
        return text
    return translate_mymemory(text) or translate_google(text) or text


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
    try:
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text[:4000], "parse_mode": "HTML"},
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"Telegram ответил {r.status_code}: {r.text[:300]}")
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
