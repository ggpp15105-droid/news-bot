import os
import re
import html
import time
import logging
from time import mktime
from datetime import datetime, timezone

import requests
import feedparser
from deep_translator import GoogleTranslator

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
POSTED_FILE = "posted_news.txt"
MAX_AGE_HOURS = 6       # не публиковать новости старше 6 часов
MAX_POSTS_PER_RUN = 10  # максимум постов за один запуск
POSTED_LIMIT = 3000     # сколько последних ID хранить

RSS_FEEDS = {
    "BBC World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "DW":           "https://rss.dw.com/rdf/rss-en-all",
    "France 24":    "https://www.france24.com/en/rss",
    "The Guardian": "https://www.theguardian.com/world/rss",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
translator = GoogleTranslator(source="auto", target="ru")


def load_posted() -> set:
    try:
        with open(POSTED_FILE, encoding="utf-8") as f:
            return set(f.read().splitlines())
    except FileNotFoundError:
        return set()


def save_posted(ids: set):
    recent = list(ids)[-POSTED_LIMIT:]
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(recent))


def translate(text: str) -> str:
    if not text:
        return ""
    for attempt in range(3):
        try:
            time.sleep(1)
            return translator.translate(text[:4500])
        except Exception as e:
            logging.warning(f"Перевод (попытка {attempt + 1}): {e}")
            time.sleep(5)
    return text


def clean_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^<]+?>", "", raw))


def send_message(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()


def entry_age_hours(entry) -> float:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return 0.0
    dt = datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def fetch_news():
    news = []
    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                news.append({
                    "id": entry.link,
                    "source": source,
                    "title": entry.get("title", ""),
                    "summary": clean_html(entry.get("summary", ""))[:400],
                    "link": entry.link,
                    "age": entry_age_hours(entry),
                })
            logging.info(f"{source}: получено {len(feed.entries)}")
        except Exception as e:
            logging.error(f"Ошибка загрузки {source}: {e}")
    return news


def main():
    posted = load_posted()
    news = fetch_news()

    # Первый запуск: только запоминаем текущие новости, чтобы не завалить канал
    if not posted:
        save_posted({n["id"] for n in news})
        logging.info("Первый запуск: новости запомнены, публикация начнётся со следующего цикла")
        return

    fresh = [n for n in news if n["id"] not in posted and n["age"] <= MAX_AGE_HOURS]
    fresh.sort(key=lambda n: n["age"])  # от старых к новым
    logging.info(f"Новых для публикации: {len(fresh)}")

    for item in fresh[:MAX_POSTS_PER_RUN]:
        title_ru = translate(item["title"])
        summary_ru = translate(item["summary"])
        text = (
            f"🌍 <b>{html.escape(item['source'])}</b>\n\n"
            f"<b>{html.escape(title_ru)}</b>\n\n"
            f"{html.escape(summary_ru)}\n\n"
            f"🔗 <a href='{item['link']}'>Читать полностью</a>"
        )
        try:
            send_message(text)
            posted.add(item["id"])
            logging.info(f"Опубликовано: {title_ru[:60]}")
            time.sleep(2)
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")

    save_posted(posted)


if __name__ == "__main__":
    main()
