import os
import re
import time
import html
import logging

import requests
import feedparser
import trafilatura
from deep_translator import GoogleTranslator

# Argos — офлайн-переводчик: работает без интернета и без лимитов
try:
    import argostranslate.translate as argo_translate
    from argostranslate import package as argo_package
except Exception:
    argo_translate = None
    argo_package = None

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")                        # секрет GitHub
CHANNEL_ID = os.getenv("CHANNEL_ID", "@world_1news_bot")  # секрет GitHub
MM_EMAIL = os.getenv("MM_EMAIL", "")  # запасной переводчик MyMemory

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
MAX_BODY_CHARS = 6000    # максимум символов полного текста статьи
POSTED_FILE = "posted_news.txt"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ========== ЛОГИКА ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("news-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан! Проверь секреты GitHub")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
google = GoogleTranslator(source="auto", target="ru")
google_off = False


def argos_ready() -> bool:
    """Установлена ли офлайн-модель перевода en→ru."""
    if argo_package is None:
        return False
    try:
        return any(p.from_code == "en" and p.to_code == "ru"
                   for p in argo_package.get_installed_packages())
    except Exception:
        return False


ARGOS_OK = argos_ready()


# ---------- ПЕРЕВОД ----------
# №1 Argos (офлайн, без лимитов) → №2 MyMemory (короткие) → №3 Google → оригинал

def translate_argos(text: str):
    if not (ARGOS_OK and text):
        return None
    try:
        return argo_translate.translate(text, "en", "ru").strip() or None
    except Exception as e:
        log.warning(f"Argos: {e}")
        return None


def translate_mymemory(text: str):
    try:
        params = {"q": text[:480], "langpair": "en|ru"}
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
    if google_off or not text:
        return None
    try:
        return google.translate(text[:4000]) or None
    except Exception as e:
        log.warning(f"Google: {e}")
        google_off = True
        return None


def translate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    t = translate_argos(text)
    if t:
        return t
    if len(text) <= 480:            # MyMemory работает только с короткими кусками
        t = translate_mymemory(text)
        if t:
            return t
    t = translate_google(text)
    if t:
        return t
    return text                      # крайний случай — публикуем оригинал


# ---------- ПОЛУЧЕНИЕ НОВОСТЕЙ ----------
def clean_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^<]+?>", "", raw or ""))


def image_from_entry(entry) -> str:
    """Достаём картинку из самой RSS-ленты."""
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in entry.get("links", []) or []:
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", "") or "")
    return html.unescape(m.group(1)) if m else None


def image_from_html(page: str) -> str:
    """Достаём og:image со страницы статьи (обычно лучшая картинка)."""
    if not page:
        return None
    m = (re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', page)
         or re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', page))
    return html.unescape(m.group(1)) if m else None


def get_article(link: str):
    """Скачиваем статью и вытаскиваем чистый полный текст + картинку."""
    body, image = "", None
    try:
        r = requests.get(link, headers=HEADERS, timeout=15)
        if r.ok and r.text:
            body = trafilatura.extract(r.text, include_comments=False,
                                       include_tables=False) or ""
            body = re.sub(r"\n{3,}", "\n\n", body).strip()[:MAX_BODY_CHARS]
            image = image_from_html(r.text)
    except Exception as e:
        log.warning(f"Статья не скачалась: {e}")
    return body, image


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
                    "summary": clean_html(entry.get("summary", ""))[:1000],
                    "link": entry.link,
                    "rss_image": image_from_entry(entry),
                })
            log.info(f"{source}: получено {len(feed.entries)}")
        except Exception as e:
            log.error(f"Ошибка загрузки {source}: {e}")
    return news


# ---------- ОТПРАВКА ----------
def load_posted() -> set:
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(f.read().splitlines())
    except FileNotFoundError:
        return set()


def save_posted(link: str):
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")


def send_message(text: str) -> bool:
    try:
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"Telegram {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.error(f"Сетевая ошибка: {e}")
        return False


def send_photo(photo_url: str, caption: str) -> bool:
    try:
        r = requests.post(
            f"{API_URL}/sendPhoto",
            json={"chat_id": CHANNEL_ID, "photo": photo_url,
                  "caption": caption[:1024], "parse_mode": "HTML"},
            timeout=40,
        )
        if r.status_code != 200:
            log.warning(f"sendPhoto {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.warning(f"sendPhoto: {e}")
        return False


def split_text(text: str, limit: int = 3500) -> list:
    """Режем длинный текст на части по абзацам (лимит Telegram — 4096)."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 <= limit:
            cur = f"{cur}\n\n{para}".strip()
        else:
            if cur:
                parts.append(cur)
            while len(para) > limit:
                parts.append(para[:limit])
                para = para[limit:]
            cur = para
    if cur:
        parts.append(cur)
    return parts


# ---------- ПУБЛИКАЦИЯ ----------
def post_item(item) -> bool:
    log.info(f"Обрабатываю: {item['title'][:60]}")

    body, img = get_article(item["link"])
    if not body:
        body = item["summary"]        # сайт не отдал текст — берём описание из RSS
    image = img or item.get("rss_image")

    title_ru = translate_text(item["title"])
    body_ru = translate_text(body)
    paras = [p.strip() for p in body_ru.split("\n\n") if p.strip()]
    lead = paras[0] if paras else ""

    caption = f"🌍 <b>{html.escape(item['source'])}</b>\n\n<b>{html.escape(title_ru)}</b>"
    if lead:
        caption += f"\n\n{html.escape(lead)}"
    caption = caption[:1020]

    sent = False
    if image:
        sent = send_photo(image, caption)
    if not sent:
        sent = send_message(caption)

    link_line = f"🔗 <a href=\"{html.escape(item['link'])}\">📰 Читать в оригинале</a>"
    rest = "\n\n".join(paras[1:]).strip()
    if rest:
        chunks = split_text(rest)[:4]
        chunks[-1] += "\n\n" + link_line
        for chunk in chunks:
            if send_message(chunk):
                sent = True
            time.sleep(1)
    else:
        if send_message(link_line):
            sent = True

    return sent


def post_news():
    posted = load_posted()
    news = fetch_news()
    fresh = [n for n in news if n["id"] not in posted]
    log.info(f"Всего новостей: {len(news)}, новых: {len(fresh)}")

    published = 0
    for item in fresh[:MAX_POSTS_PER_RUN]:
        try:
            if post_item(item):
                save_posted(item["id"])
                published += 1
        except Exception as e:
            log.error(f"Ошибка обработки новости: {e}")
        time.sleep(POST_DELAY)

    log.info(f"Итог запуска: опубликовано {published}")


if __name__ == "__main__":
    log.info(f"Переводчик: {'Argos (офлайн, без лимитов)' if ARGOS_OK else 'запасной (MyMemory/Google)'}")
    post_news()
