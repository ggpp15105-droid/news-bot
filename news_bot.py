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
    # --- мир ---
    "BBC World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "DW":           "https://rss.dw.com/rdf/rss-en-all",
    "France 24":    "https://www.france24.com/en/rss",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "Sky News":     "https://feeds.skynews.com/feeds/rss/world.xml",
    "CNBC":         "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "Euronews":     "https://www.euronews.com/rss",
    "Japan Times":  "https://www.japantimes.co.jp/feed/",
    "Anadolu":      "https://www.aa.com.tr/en/rss/default?cat=world",
    # --- игры и железо ---
    "IGN":          "https://feeds.ign.com/ign/games-all",
    "GameSpot":     "https://www.gamespot.com/feeds/news/",
    "Eurogamer":    "https://www.eurogamer.net/feed",
    "Rock Paper Shotgun": "https://www.rockpapershotgun.com/feed",
    "Tom's Hardware": "https://www.tomshardware.com/feeds/all",
    # --- русские ---
    "Lenta.ru":     "https://lenta.ru/rss/news",
    "Коммерсантъ":  "https://www.kommersant.ru/RSS/news.xml",
    "ТАСС":         "https://tass.ru/rss/v2.xml",
    "Ведомости":    "https://www.vedomosti.ru/rss/news",
    "Игромания":    "https://www.igromania.ru/rss/news.xml",
    "3DNews":       "https://www.3dnews.ru/news/rss/",
    # --- спорт ---
    "BBC Sport":      "http://feeds.bbci.co.uk/sport/rss.xml",
    "Sky Sports":     "https://www.skysports.com/rss/12040",
    "CBS Sports":     "https://www.cbssports.com/rss/headlines/",
    "Guardian Sport": "https://www.theguardian.com/uk/sport/rss",
}

MAX_PER_SOURCE = 5
MAX_POSTS_PER_RUN = 5
POST_DELAY = 3
LEAD_SRC_CHARS = 2500   # сколько символов статьи забираем для перевода (лид)
LEAD_LIMIT = 650        # максимум знаков на «главную мысль»
CAPTION_LIMIT = 1024    # лимит подписи к фото/видео в Telegram
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


def _digest_hours() -> list:
    """Часы дайджестов по МСК. По умолчанию: утро 8 и вечер 20.
    Можно переопределить переменной репозитория DIGEST_HOURS, например: 9,14,21"""
    raw = os.getenv("DIGEST_HOURS", "") or "8,20"
    hours = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.append(int(part))
    return sorted(set(hours)) if hours else [8, 20]


DIGEST_HOURS_MSK = _digest_hours()

# ========== ЛОГИКА ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger().setLevel(logging.WARNING)   # глушим отладку Argos (root logger)
log = logging.getLogger("news-bot")
log.setLevel(logging.INFO)

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

# ---------- ЗАЩИТА НАЗВАНИЙ ОТ ПЕРЕВОДА ----------
PROTECT_TERMS = sorted([
    # игровые студии
    "Naughty Dog", "Rockstar Games", "CD Projekt Red", "FromSoftware",
    "Insomniac Games", "Santa Monica Studio", "Guerrilla Games",
    "Kojima Productions", "Larian Studios", "Respawn Entertainment",
    "Riot Games", "Epic Games", "Team Cherry", "PlatinumGames",
    "Square Enix", "Bandai Namco", "id Software", "Infinity Ward",
    "Rockstar", "Bethesda", "Ubisoft", "Blizzard", "Valve", "Bungie",
    "Remedy", "Capcom", "BioWare", "Arkane", "Crytek", "Techland",
    "Mojang", "Gearbox", "Atlus", "Sega", "Nintendo", "Activision",
    # игры и франшизы
    "Intergalactic: The Heretic Prophet", "The Last of Us",
    "Red Dead Redemption", "Grand Theft Auto", "Baldur's Gate",
    "The Legend of Zelda", "The Elder Scrolls", "Assassin's Creed",
    "Ghost of Tsushima", "God of War", "Horizon Forbidden West",
    "Horizon Zero Dawn", "Metal Gear Solid", "Cyberpunk 2077",
    "Counter-Strike", "League of Legends", "Monster Hunter",
    "Call of Duty", "Animal Crossing", "Silent Hill", "Hollow Knight",
    "Super Mario", "Street Fighter", "Mortal Kombat", "Final Fantasy",
    "Resident Evil", "Death Stranding", "The Witcher", "Elden Ring",
    "Dark Souls", "Gears of War", "Gran Turismo", "Watch Dogs",
    "Stardew Valley", "Apex Legends", "Rainbow Six", "Dragon Quest",
    "Star Wars", "Indiana Jones", "Starfield", "Silksong", "Fortnite",
    "Minecraft", "Terraria", "Pokémon", "Persona", "Sekiro", "Halo",
    "Doom", "Fallout", "Uncharted", "Sonic", "Zelda", "Hades",
    # компании, платформы, бренды
    "PlayStation", "Nintendo Switch", "Steam Deck", "Xbox Series",
    "Take-Two", "Warner Bros", "OpenAI", "ChatGPT", "SpaceX",
    "Telegram", "YouTube", "Instagram", "Facebook", "TikTok",
    "Netflix", "Spotify", "WhatsApp", "Discord", "Twitch", "Reddit",
    "Nvidia", "GeForce", "Radeon", "Snapdragon", "Ryzen", "Huawei",
    "Xiaomi", "OnePlus", "iPhone", "iPad", "MacBook", "Samsung",
    "Google", "Microsoft", "Apple", "Amazon", "Tesla", "Sony", "EA",
], key=len, reverse=True)   # длинные названия проверяем первыми

_PROTECT_PATTERNS = [
    (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term in PROTECT_TERMS
]


def protect_terms(text: str):
    """Заменяем защищённые названия на метки XQ..ZX перед переводом."""
    holders = {}
    for i, (term, pat) in enumerate(_PROTECT_PATTERNS, 1):
        m = pat.search(text)
        if m:
            key = f"XQ{i}ZX"
            text = pat.sub(key, text)
            holders[key] = m.group(0)   # как написано в оригинале
    return text, holders


def restore_terms(text: str, holders: dict) -> str:
    """Возвращаем настоящие названия на место меток."""
    for key, name in holders.items():
        text = re.sub(r" *" + re.escape(key) + r" *", f" {name} ", text)
    return re.sub(r" +", " ", text)


# ---------- ПЕРЕВОД ----------
def is_russian(text: str) -> bool:
    if not text:
        return False
    cyr = len(re.findall(r"[а-яё]", text.lower()))
    lat = len(re.findall(r"[a-z]", text.lower()))
    return cyr > lat


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
    if not text or is_russian(text):
        return text
    protected, holders = protect_terms(text)
    t = translate_argos(protected)
    if t:
        return restore_terms(t, holders)
    if len(protected) <= 480:
        t = translate_mymemory(protected)
        if t:
            return restore_terms(t, holders)
    t = translate_google(protected)
    if t:
        return restore_terms(t, holders)
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
        "invasion", "soldier", "ukraine", "gaza", "rebel", "insurgent",
        "война", "военный", "армия", "обстрел", "ракета", "дрон", "беспилотник",
        "теракт", "взрыв", "перемирие", "заложник", "наступление", "бои"],
    "политика": ["election", "president", "minister", "government", "parliament",
        "senate", "vote", "sanctions", "summit", "diplomat", "diplomacy", "court",
        "judge", "protest", "referendum", "coup", "opposition", "treaty", "policy",
        "immigration", "border", "asylum", "embassy", "kremlin", "white house",
        "north korea", "prime minister", "presidential", "mayor", "governor",
        "выборы", "президент", "министр", "правительство", "парламент", "санкции",
        "саммит", "дипломат", "суд", "протест", "госдума", "кремль", "законопроект"],
    "экономика": ["economy", "inflation", "gdp", "market", "markets", "stocks",
        "shares", "oil", "tariff", "tariffs", "trade", "export", "bank", "banks",
        "central bank", "currency", "dollar", "euro", "recession", "unemployment",
        "jobs", "budget", "tax", "taxes", "investment", "investor", "crypto",
        "bitcoin", "opec", "imf", "world bank", "interest rate", "stock market",
        "prices", "permanent residency", "visa",
        "экономика", "инфляция", "рынок", "акции", "нефть", "рубль", "доллар",
        "банк", "тариф", "торговля", "бюджет", "налог", "инвестиции", "безработица"],
    "технологии": ["ai", "artificial intelligence", "tech", "technology",
        "software", "startup", "google", "apple", "microsoft", "amazon", "openai",
        "chatgpt", "chip", "chips", "semiconductor", "robot", "cyber", "hacker",
        "hacking", "internet", "spacex", "nasa", "satellite", "rocket", "lunar",
        "mars", "quantum", "smartphone", "tiktok", "youtube", "facebook",
        "instagram", "elon musk", "tesla", "app",
        "искусственный интеллект", "нейросеть", "технологии", "смартфон",
        "интернет", "хакер", "взлом", "спутник", "робот", "приложение"],
    "спорт": ["football", "soccer", "match", "tournament", "cup", "league",
        "olympic", "olympics", "championship", "player", "coach", "goal", "final",
        "semifinal", "cricket", "tennis", "basketball", "nba", "fifa", "uefa",
        "stadium", "striker", "formula 1", "world cup", "champions league",
        "grand slam", "medal", "fixture", "hockey", "volleyball", "boxing",
        "футбол", "матч", "турнир", "кубок", "лига", "хоккей", "олимпиада",
        "чемпионат", "игрок", "тренер", "гол", "финал", "теннис", "бокс",
        "плей-офф", "сезон", "сборная", "трансфер", "вратарь", "форвард"],
    "наука": ["study", "research", "scientists", "discovery", "climate",
        "emissions", "warming", "energy", "solar", "physics", "biology",
        "genetics", "fossil", "dinosaur", "brain", "asteroid", "vaccine", "virus",
        "health", "disease", "outbreak", "cancer", "space telescope",
        "исследование", "ученые", "учёные", "климат", "энергия", "космос",
        "вакцина", "вирус", "здоровье", "болезнь"],
    "культура": ["film", "movie", "cinema", "music", "album", "song", "concert",
        "festival", "art", "museum", "exhibition", "book", "novel", "celebrity",
        "actor", "actress", "singer", "oscar", "grammy", "cannes", "netflix",
        "fashion", "theatre",
        "фильм", "кино", "музыка", "альбом", "концерт", "фестиваль", "выставка",
        "книга", "сериал", "актёр", "актер", "мода", "премьера"],
    "игры": ["video game", "videogame", "gaming", "gamescom", "playstation",
        "xbox", "nintendo", "switch 2", "steam deck", "epic games", "steam",
        "gameplay", "dlc", "patch notes", "early access", "open world", "esports",
        "gta", "elden ring", "call of duty", "fortnite", "minecraft", "valorant",
        "counter-strike", "dota", "cyberpunk", "witcher", "baldur", "zelda",
        "mario", "release date", "sequel", "remake", "remaster", "game developer",
        "игра", "игры", "игру", "игровой", "геймер", "шутер", "рпг", "консоль",
        "патч", "дополнение", "релиз", "геймплей", "киберспорт", "ранний доступ"],
    "железо": ["gpu", "graphics card", "cpu", "processor", "rtx", "radeon",
        "geforce", "nvidia", "amd", "intel", "ryzen", "monitor", "keyboard",
        "mouse", "headset", "ssd", "motherboard", "gaming laptop", "gaming pc",
        "razer", "logitech", "hyperx", "steelseries", "overclock", "vram", "ddr5",
        "видеокарта", "процессор", "материнская плата", "монитор", "клавиатура",
        "мышь", "наушники", "накопитель", "оперативная память", "разгон",
        "игровой ноутбук", "игровой компьютер", "жёсткий диск", "жесткий диск"],
}

GAME_SOURCES = {"IGN", "GameSpot", "VG247", "Eurogamer", "Rock Paper Shotgun",
                "Игромания"}

SPORT_SOURCES = {"BBC Sport", "Sky Sports", "CBS Sports", "Guardian Sport"}


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
    if source in SPORT_SOURCES:
        scores["спорт"] = scores.get("спорт", 0) + 3
    if not scores:
        return "#мир"
    top = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:2]
    tags = [c for c, s in top if s >= 2]
    return " ".join("#" + t for t in tags) if tags else "#мир"
    # ---------- СТАТЬЯ И МЕДИА ----------
def clean_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^<]+?>", "", raw or ""))


def get_article(link: str):
    """Скачиваем страницу: текст статьи (начало) и картинку-превью."""
    body, image = "", None
    try:
        r = requests.get(link, headers=HEADERS, timeout=15)
        if r.ok and r.text:
            text = trafilatura.extract(r.text, include_comments=False,
                                       include_tables=False) or ""
            body = re.sub(r"\n{3,}", "\n\n", text).strip()
            m = (re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', r.text)
                 or re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', r.text))
            if m:
                image = html.unescape(m.group(1))
    except Exception as e:
        log.warning(f"Статья не скачалась: {e}")
    return body[:LEAD_SRC_CHARS], image


def image_from_entry(entry) -> str:
    """Картинка из самой RSS-ленты (если есть)."""
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in entry.get("links", []) or []:
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", "") or "")
    return html.unescape(m.group(1)) if m else None


def video_from_entry(entry) -> str:
    """Прямая ссылка на видеофайл из RSS (если есть)."""
    for m in entry.get("media_content", []) or []:
        url = m.get("url") or ""
        if url and (str(m.get("type", "")).startswith("video") or ".mp4" in url.lower()):
            return url
    for l in entry.get("links", []) or []:
        url = l.get("href") or ""
        if str(l.get("type", "")).startswith("video") or url.lower().endswith(".mp4"):
            return url
    return None


def is_junk(p: str) -> bool:
    """Мусорные обрывки: даты, имена авторов, буллеты, '- Published', 'Update: ...'"""
    if len(p.split()) < 5:                       # слишком короткий абзац
        return True
    if p.startswith(("-", "–", "—", "•")):       # буллеты-списки (Anadolu и др.)
        return True
    if "Update:" in p:                           # метки обновления
        return True
    digits = sum(ch.isdigit() for ch in p)
    if digits > len(p) * 0.3:                    # больше 30% цифр — не текст
        return True
    return False


def make_lead(body: str, summary: str, limit: int) -> str:
    """Главная мысль: первые 1-3 абзаца статьи (или описание из ленты)."""
    src = body or summary or ""
    paras = [p.strip() for p in src.split("\n\n") if p.strip()]
    out, total = [], 0
        for p in paras:
        if is_junk(p):                           # мусор пропускаем ВСЕГДА, включая первый
            continue
        if out and total + len(p) + 2 > limit:
            break
        out.append(p)
        total += len(p) + 2
        if total >= limit * 0.85:
            break
    text = "\n\n".join(out) if out else src[:limit]
    if len(text) > limit:
        text = smart_cut(text, limit, mark=False)
    return text


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


def build_caption(source: str, title: str, lead: str, link: str, tags: str) -> str:
    """Весь пост одной подписью: заголовок + главная мысль + теги + ссылка."""
    link_html = f"🔗 <a href=\"{html.escape(link, quote=True)}\">📰 Читать в оригинале</a>"
    header = f"🌍 <b>{html.escape(source)}</b>\n\n<b>{html.escape(title)}</b>"
    tags_html = f"\n\n{html.escape(tags)}" if tags else ""
    budget = CAPTION_LIMIT - len(header) - len(link_html) - len(tags_html) - 30
    lead_esc = html.escape((lead or "").strip())
    if len(lead_esc) > budget:
        lead_esc = smart_cut(lead_esc, budget)
    if lead_esc:
        return f"{header}\n\n{lead_esc}{tags_html}\n\n{link_html}"
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
                    "image": image_from_entry(entry),
                    "video": video_from_entry(entry),
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
    """Дайджесты по расписанию DIGEST_HOURS_MSK (по умолчанию 8:00 и 20:00 МСК).
    Каждый дайджест — новости с момента предыдущего дайджеста."""
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    try:
        with open(DIGEST_FILE, encoding="utf-8") as f:
            raw_lines = [l.rstrip("\n") for l in f if l.strip()]
    except FileNotFoundError:
        raw_lines = []

    marker, data_lines = "", []
    for line in raw_lines:
        if line.startswith("#last="):
            marker = line[6:]
        else:
            data_lines.append(line)

    # новости собираем с момента прошлого дайджеста (или за последние 24 часа)
    cutoff = time.time() - 24 * 3600
    if "|" in marker:
        try:
            cutoff = float(marker.split("|", 1)[1])
        except ValueError:
            pass

    entries = []
    for line in data_lines:
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

    # идём от позднего слота к раннему — отправляем максимум один за запуск
    for hour in sorted(DIGEST_HOURS_MSK, reverse=True):
        slot = f"{today}-{hour}"
        if now.hour < hour or marker.split("|", 1)[0] == slot:
            continue
        if picked:
            greeting = ("☕ <b>Доброе утро! Главное за сутки:</b>" if hour < 12
                        else "🌆 <b>Добрый вечер! Главное за день:</b>")
            items = "\n".join(
                f"• {html.escape(t)} — <a href=\"{html.escape(u, quote=True)}\">{html.escape(s)}</a>"
                for s, t, u in picked)
            if not send_message(f"{greeting}\n\n{items}", preview=False):
                return   # не отправилось — повторим в следующий запуск
            log.info(f"Дайджест ({slot}) отправлен: {len(picked)} новостей")
        else:
            log.info(f"Дайджест ({slot}): новых новостей не было")
        data_lines.insert(0, f"#last={slot}|{int(time.time())}")
        with open(DIGEST_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(data_lines) + ("\n" if data_lines else ""))
        return


# ---------- ОТПРАВКА ----------
def send_message(text: str, preview: bool = True) -> bool:
    try:
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text[:TG_LIMIT], "parse_mode": "HTML",
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


def send_photo(photo_url: str, caption: str) -> bool:
    try:
        r = requests.post(
            f"{API_URL}/sendPhoto",
            json={"chat_id": CHANNEL_ID, "photo": photo_url,
                  "caption": caption[:CAPTION_LIMIT], "parse_mode": "HTML"},
            timeout=40,
        )
        if r.status_code != 200:
            log.warning(f"sendPhoto {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        log.warning(f"sendPhoto: {e}")
        return False


def send_video(video_url: str, caption: str) -> bool:
    try:
        r = requests.post(
            f"{API_URL}/sendVideo",
            json={"chat_id": CHANNEL_ID, "video": video_url,
                  "caption": caption[:CAPTION_LIMIT], "parse_mode": "HTML"},
            timeout=60,
        )
        if r.status_code != 200:
            log.warning(f"sendVideo {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        log.warning(f"sendVideo: {e}")
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
    body, page_image = get_article(item["link"])
    image = page_image or item.get("image")
    video = item.get("video")

    title_ru = translate_text(item["title"])
    lead_ru = translate_text(make_lead(body, item["summary"], LEAD_LIMIT))
    tags = detect_tags(item["source"], item["title"], item["summary"])
    caption = build_caption(item["source"], title_ru, lead_ru, item["link"], tags)

    if video and send_video(video, caption):
        return True
    if image and send_photo(image, caption):
        return True
    return send_message(caption, preview=False)


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
    log.info(f"Дайджесты по МСК в часы: {DIGEST_HOURS_MSK}")
    try:
        main()
    except Exception as e:
        ALERTS.append(f"Критическая ошибка: {e!r}")
        send_alerts()
        raise
