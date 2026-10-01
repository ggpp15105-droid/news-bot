import os
import re
import time
import html
import logging
from datetime import datetime, timezone, timedelta

import requests
import feedparser
import trafilatura
from deep_translator import GoogleTranslator

try:
    import argostranslate.translate as argo_translate
    from argostranslate import package as argo_package
except Exception:
    argo_translate = None
    argo_package = None

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@world_1news_bot")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()   # алерты о проблемах
MM_EMAIL = os.getenv("MM_EMAIL", "")

RSS_FEEDS = {
    "BBC World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "DW":           "https://rss.dw.com/rdf/rss-en-all",
    "France 24":    "https://www.france24.com/en/rss",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "Reuters":      "https://www.reuters.com/rssFeed/world",
    "AP News":      "https://apnews.com/index.rss",
    "Euronews":     "https://www.euronews.com/rss",
    "NHK World":    "https://www3.nhk.or.jp/nhkworld/en/news/rss/all.xml",
    "Anadolu":      "https://www.aa.com.tr/en/rss/default?cat=world",
    "IGN":          "https://feeds.ign.com/ign/games-all",
    "GameSpot":     "https://www.gamespot.com/feeds/news/",
    "PC Gamer":     "https://www.pcgamer.com/feed/",
    "Eurogamer":    "https://www.eurogamer.net/feed",
    "Rock Paper Shotgun": "https://www.rockpapershotgun.com/feed",
    "Tom's Hardware": "https://www.tomshardware.com/feeds/all",
}

MAX_PER_SOURCE = 5
MAX_POSTS_PER_RUN = 5
POST_DELAY = 3
PRE_CUT_CHARS = 4200
TG_LIMIT = 4096
POSTED_FILE = "posted_news.txt"
DIGEST_FILE = "digest.txt"
DIGEST_ITEMS = 8
KEEP_DAYS = 7          # сколько дней помнить ссылки
TITLE_WINDOW_H = 48    # окно дедупликации по заголовкам
MAX_LINES = 2000       # потолок памяти
MSK = timezone(timedelta(hours=3))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _int_env(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


DIGEST_HOUR_MSK = _int_env("DIGEST_HOUR", 8)   # час утреннего дайджеста по МСК

# ========== ЛОГИКА ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("news-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан! Проверь секреты GitHub")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALERTS = []   # копим проблемы за запуск, в конце отправим админу

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


# ---------- ДЕДУПЛИКАЦИЯ ----------
STOPWORDS = set("""the a an of in on for to and as at by with from after over
is are was were be been am that this it its his her their our your not no but
or so up out about into than then will would could can may might do does did
has have had who what when where why how more most new news says say said
report reports amid during year years day days week month two three four five
first second back against before between under among across per via off down
again further once here there all any both each few other some such only own
same too very now s t don i me my we you he she they them if while just get
got set put say tells told going goes went""".split())


def norm_tokens(text: str) -> set:
    out = set()
    for w in re.sub(r"[^\w\s]", " ", (text or "").lower()).split():
        if len(w) < 2 or w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def similar(a: set, b: set) -> bool:
    inter = a & b
    if len(inter) < 3:
        return False
    return len(inter) / len(a | b) >= 0.5


def is_dup(tokens: set, titles: list, now: int) -> bool:
    for ts, old in titles:
        if now - ts > TITLE_WINDOW_H * 3600:
            continue
        if similar(tokens, old):
            return True
    return False


# ---------- КАТЕГОРИИ / ХЕШТАГИ ----------
CATEGORIES = {
    "конфликты": ["war", "military", "attack", "missile", "drone", "airstrike",
        "troops", "ceasefire", "killed", "clash", "clashes", "terror", "explosion",
        "bomb", "bombing", "army", "militant", "shelling", "hostage", "weapon",
        "invasion", "soldier", "ukraine", "gaza", "rebel", "insurgent"],
    "политика": ["election", "president", "minister", "government", "parliament",
        "senate", "vote", "sanctions", "summit", "diplomat", "diplomacy", "court",
        "judge", "protest", "referendum", "coup", "opposition", "treaty", "policy",
        "immigration", "border", "asylum", "embassy", "kremlin", "white house",
        "north korea", "prime minister", "presidential", "mayor", "governor"],
    "экономика": ["economy", "inflation", "gdp", "market", "markets", "stocks",
        "shares", "oil", "tariff", "tariffs", "trade", "export", "bank", "banks",
        "central bank", "currency", "dollar", "euro", "recession", "unemployment",
        "jobs", "budget", "tax", "taxes", "investment", "investor", "crypto",
        "bitcoin", "opec", "imf", "world bank", "interest rate", "stock market",
        "prices", "permanent residency", "visa"],
    "технологии": ["ai", "artificial intelligence", "tech", "technology",
        "software", "startup", "google", "apple", "microsoft", "amazon", "openai",
        "chatgpt", "chip", "chips", "semiconductor", "robot", "cyber", "hacker",
        "hacking", "internet", "spacex", "nasa", "satellite", "rocket", "lunar",
        "mars", "quantum", "smartphone", "tiktok", "youtube", "facebook",
        "instagram", "elon musk", "tesla", "app"],
    "спорт": ["football", "soccer", "match", "tournament", "cup", "league",
        "olympic", "olympics", "championship", "player", "coach", "goal", "final",
        "semifinal", "cricket", "tennis", "basketball", "nba", "fifa", "uefa",
        "stadium", "striker", "formula 1", "world cup", "champions league",
        "grand slam", "medal", "fixture"],
    "наука": ["study", "research", "scientists", "discovery", "climate",
        "emissions", "warming", "energy", "solar", "physics", "biology",
        "genetics", "fossil", "dinosaur", "brain", "asteroid", "vaccine", "virus",
        "health", "disease", "outbreak", "cancer", "space telescope"],
    "культура": ["film", "movie", "cinema", "music", "album", "song", "concert",
        "festival", "art", "museum", "exhibition", "book", "novel", "celebrity",
        "actor", "actress", "singer", "oscar", "grammy", "cannes", "netflix",
        "fashion", "theatre"],
    "игры": ["video game", "videogame", "gaming", "gamescom", "playstation",
        "xbox", "nintendo", "switch 2", "steam deck", "epic games", "steam",
        "gameplay", "dlc", "patch notes", "early access", "open world", "esports",
        "gta", "elden ring", "call of duty", "fortnite", "minecraft", "valorant",
        "counter-strike", "dota", "cyberpunk", "witcher", "baldur", "zelda",
        "mario", "release date", "sequel", "remake", "remaster", "game developer"],
    "железо": ["gpu", "graphics card", "cpu", "processor", "rtx", "radeon",
        "geforce", "nvidia", "amd", "intel", "ryzen", "monitor", "keyboard",
        "mouse", "headset", "ssd", "motherboard", "gaming laptop", "gaming pc",
        "razer", "logitech", "hyperx", "steelseries", "overclock", "vram", "ddr5"],
}


GAME_SOURCES = {"IGN", "GameSpot", "PC Gamer", "Eurogamer", "Rock Paper Shotgun"}


def detect_tags(source: str, title: str, summary: str) -> str:
    tl = " " + re.sub(r"[^\w\s]", " ", f"{title} {title} {summary}").lower() + " "
    scores = {}
    for cat, words in CATEGORIES.items():
        s = 0
        for w in words:
            if " " in w:
                if w in tl:
                    s += 2
            elif f" {w} " in tl:
                s += 1
        if s > 0:
            scores[cat] = s
    if source in GAME_SOURCES:
        scores["игры"] = scores.get("игры", 0) + 3
    if not scores:
        return "#мир"
    top = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:2]
    tags = [c for c, s in top if s >= 2]
    return " ".join("#" + t for t in tags) if tags else "#мир"
    # ---------- СТАТЬЯ ----------
def clean_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^<]+?>", "", raw or ""))


def get_article(link: str) -> str:
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


def smart_cut(s: str, limit: int, mark: bool = True) -> str:
    """Обрезка по концу предложения (mark=False — перед переводом)."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    ends = [m.end() for m in re.finditer(r"[.!?…]", cut)]
    if ends and ends[-1] > len(cut) * 0.5:
        cut = cut[:ends[-1]]
    return cut.rstrip() + (" …" if mark else "")


def build_post(source: str, title: str, body: str, link: str, tags: str) -> str:
    link_html = f"🔗 <a href=\"{html.escape(link, quote=True)}\">📰 Читать в оригинале</a>"
    header = f"🌍 <b>{html.escape(source)}</b>\n\n<b>{html.escape(title)}</b>"
    tags_html = f"\n\n{html.escape(tags)}" if tags else ""
    if body:
        body_esc = html.escape(body).strip()
        budget = TG_LIMIT - len(header) - len(link_html) - len(tags_html) - 30
        if len(body_esc) > budget:
            body_esc = smart_cut(body_esc, budget)
        return f"{header}\n\n{body_esc}{tags_html}\n\n{link_html}"
    return f"{header}{tags_html}\n\n{link_html}"


def fetch_news() -> list:
    news, empty_sources = [], []
    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            entries = feed.entries or []
            for entry in entries[:MAX_PER_SOURCE]:
                news.append({
                    "id": entry.link,
                    "source": source,
                    "title": clean_html(entry.get("title", "")),
                    "summary": clean_html(entry.get("summary", ""))[:1000],
                    "link": entry.link,
                })
            log.info(f"{source}: получено {len(entries)}")
            if not entries:
                empty_sources.append(source)
        except Exception as e:
            empty_sources.append(source)
            log.error(f"Ошибка загрузки {source}: {e}")
    if len(empty_sources) >= max(2, len(RSS_FEEDS) // 2):
        ALERTS.append("RSS не отдают новости: " + ", ".join(empty_sources))
    return news


# ---------- ПАМЯТЬ ----------
def ensure_files():
    for p in (POSTED_FILE, DIGEST_FILE):
        open(p, "a", encoding="utf-8").close()


def load_state():
    posted, titles = set(), []
    try:
        with open(POSTED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3 and parts[0].isdigit():
                    posted.add(parts[1])
                    titles.append((int(parts[0]), norm_tokens(parts[2])))
                else:
                    posted.add(line)
    except FileNotFoundError:
        pass
    return posted, titles


def save_posted(url: str, title: str):
    title = " ".join((title or "").split())
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{int(time.time())}|{url}|{title}\n")


def prune_posted():
    now = int(time.time())
    try:
        with open(POSTED_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return
    kept = []
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            if now - int(parts[0]) <= KEEP_DAYS * 86400:
                kept.append(line)
        else:
            kept.append(line)
    if len(kept) > MAX_LINES:
        kept = kept[-MAX_LINES:]
    if len(kept) < len(lines):
        with open(POSTED_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
        log.info(f"Очистка памяти: {len(lines)} → {len(kept)} записей")


# ---------- ДАЙДЖЕСТ ----------
def digest_add(source: str, title_ru: str, url: str):
    title_ru = " ".join(title_ru.split())
    with open(DIGEST_FILE, "a", encoding="utf-8") as f:
        f.write(f"{int(time.time())}|{source}|{title_ru}|{url}\n")


def maybe_send_digest():
    now = datetime.now(MSK)
    if now.hour < DIGEST_HOUR_MSK:
        return
    today = now.strftime("%Y-%m-%d")
    try:
        with open(DIGEST_FILE, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
    except FileNotFoundError:
        return
    last = ""
    if lines and lines[0].startswith("#last="):
        last = lines[0][6:]
    if last == today:
        return

    cutoff = time.time() - 24 * 3600
    entries = []
    for line in lines:
        if line.startswith("#"):
            continue
        parts = line.split("|", 3)
        if len(parts) == 4 and parts[0].isdigit() and int(parts[0]) >= cutoff:
            entries.append(parts)
    entries.sort(key=lambda p: -int(p[0]))

    picked, picked_tokens = [], []
    for ts, source, title, url in entries:
        tk = norm_tokens(title)
        if not tk or any(similar(tk, o) for o in picked_tokens):
            continue
        picked.append((source, title, url))
        picked_tokens.append(tk)
        if len(picked) >= DIGEST_ITEMS:
            break

    ok = True
    if picked:
        items = "\n".join(
            f"• {html.escape(t)} — <a href=\"{html.escape(u, quote=True)}\">{html.escape(s)}</a>"
            for s, t, u in picked)
        ok = send_message(f"☕ <b>Доброе утро! Главное за сутки:</b>\n\n{items}",
                          preview=False)
        if ok:
            log.info("Дайджест отправлен")
    if ok:
        with open(DIGEST_FILE, "w", encoding="utf-8") as f:
            f.write(f"#last={today}\n")


# ---------- ОТПРАВКА ----------
def send_message(text: str, preview: bool = True) -> bool:
    try:
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": not preview},
            timeout=30,
        )
        if r.status_code != 200:
            msg = f"Telegram {r.status_code}: {r.text[:150]}"
            log.error(msg)
            ALERTS.append(msg)
            return False
        return True
    except Exception as e:
        msg = f"Сетевая ошибка: {e}"
        log.error(msg)
        ALERTS.append(msg)
        return False


def send_alerts():
    if not ALERTS:
        return
    if not ADMIN_ID:
        log.warning("Есть проблемы, но ADMIN_ID не задан — алерт не отправлен")
        return
    text = ("🚨 <b>News Bot — проблемы за запуск:</b>\n\n"
            + "\n".join(f"• {html.escape(a[:200])}" for a in ALERTS[:10]))
    try:
        requests.post(f"{API_URL}/sendMessage",
                      json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        log.error(f"Не удалось отправить алерт: {e}")


# ---------- ПУБЛИКАЦИЯ ----------
def post_item(item) -> bool:
    log.info(f"Обрабатываю: {item['title'][:60]}")
    body = get_article(item["link"]) or item["summary"]
    if len(body) > PRE_CUT_CHARS:
        body = smart_cut(body, PRE_CUT_CHARS, mark=False)

    title_ru = translate_text(item["title"])
    body_ru = translate_text(body).strip()
        tags = detect_tags(item["source"], item["title"], item["summary"])
    text = build_post(item["source"], title_ru, body_ru, item["link"], tags)

    if send_message(text):
        digest_add(item["source"], title_ru, item["link"])
        return True
    return False


def post_news():
    now = int(time.time())
    posted, titles = load_state()
    news = fetch_news()

    fresh, seen = [], set()
    for item in news:
        if item["id"] in posted or item["id"] in seen:
            continue
        tk = norm_tokens(item["title"])
        if is_dup(tk, titles, now):
            continue
        fresh.append(item)
        seen.add(item["id"])
        titles.append((now, tk))
    log.info(f"Всего: {len(news)}, новых после дедупликации: {len(fresh)}")

    published = 0
    for item in fresh[:MAX_POSTS_PER_RUN]:
        try:
            if post_item(item):
                save_posted(item["id"], item["title"])
                published += 1
        except Exception as e:
            msg = f"Ошибка обработки «{item['title'][:50]}»: {e}"
            log.error(msg)
            ALERTS.append(msg)
        time.sleep(POST_DELAY)

    prune_posted()
    log.info(f"Итог запуска: опубликовано {published}")


def main():
    ensure_files()
    maybe_send_digest()
    post_news()
    send_alerts()


if __name__ == "__main__":
    log.info(f"Переводчик: {'Argos (офлайн, без лимитов)' if ARGOS_OK else 'запасной'}")
    try:
        main()
    except Exception as e:
        ALERTS.append(f"Критическая ошибка: {e!r}")
        send_alerts()
        raise
