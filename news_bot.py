import os
import re
import time
import html
import logging

import requests
import feedparser
import trafilatura
from deep_translator import GoogleTranslator

# Argos — офлайн-переводчик: без интернета и лимитов
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
PRE_CUT_CHARS = 4200     # обрезка оригинала перед переводом (скорость)
TG_LIMIT = 4096          # жёсткий лимит Telegram на одно сообщение
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
    if len(text) <= 480:
        t = translate_mymemory(text)
        if t:
            return t
    t = translate_google(text)
    if t:
        return t
    return text


# ---------- СТАТЬЯ ----------
def clean_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^<]+?>", "", raw or ""))


def get_article(link: str) -> str:
    """Скачиваем страницу и вытаскиваем чистый полный текст статьи."""
    try:
        r = requests.get(link, headers=HEADERS, timeout=15)
        if r.ok and r.text:
            body = trafilatura.extract(r.text, include_comments=False,
                                       include_tables=False) or ""
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            return body[:12000]
    except Exception as e:
        log.warning(f"Статья не скачалась: {e}")
    return ""


def smart_cut(s: str, limit: int) -> str:
    """Обрезка по концу предложения, не ломая HTML-сущности."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    amp = cut.rfind("&")                       # не режем &amp; и подобные посередине
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    ends = [m.end() for m in re.finditer(r"[.!?…]", cut)]
    if ends and ends[-1] > len(cut) * 0.5:
        cut = cut[:ends[-1]]                   # режем по последнему предложению
    return cut.rstrip() + " …"


def build_post(source: str, title: str, body: str, link: str) -> str:
    """Всё в одном сообщении: источник, заголовок, текст, ссылка в конце."""
    link_html = f"🔗 <a href=\"{html.escape(link, quote=True)}\">📰 Читать в оригинале</a>"
    header = f"🌍 <b>{html.escape(source)}</b>\n\n<b>{html.escape(title)}</b>"
    if body:
        body_esc = html.escape(body).strip()
        # бюджет: лимит Telegram минус шапка, ссылка и разделители
        budget = TG_LIMIT - len(header) - len(link_html) - 20
        if len(body_esc) > budget:
            body_esc = smart_cut(body_esc, budget)
        return f"{header}\n\n{body_esc}\n\n{link_html}"
    return f"{header}\n\n{link_html}"


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
            json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"Telegram {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.error(f"Сетевая ошибка: {e}")
        return False


# ---------- ПУБЛИКАЦИЯ ----------
def post_item(item) -> bool:
    log.info(f"Обрабатываю: {item['title'][:60]}")
    body = get_article(item["link"])
    if not body:
        body = item["summary"]                 # сайт не отдал текст — берём описание из RSS
    if len(body) > PRE_CUT_CHARS:
        body = smart_cut(body, PRE_CUT_CHARS)  # режем оригинал до перевода (скорость)

    title_ru = translate_text(item["title"])
    body_ru = translate_text(body).strip()
    text = build_post(item["source"], title_ru, body_ru, item["link"])
    return send_message(text)


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
