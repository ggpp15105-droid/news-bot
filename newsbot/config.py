"""Настройки бота.

Все параметры читаются из переменных окружения. Значения по умолчанию
рассчитаны на спокойный новостной канал: 2 поста за запуск, тихие часы,
жёсткие квоты по источникам.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

MSK_OFFSET_HOURS = 3

# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    category: str = "мир"
    weight: float = 1.0


DEFAULT_FEEDS: tuple[Feed, ...] = (
    # --- мир ---
    Feed("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml", "мир", 1.15),
    Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "мир", 0.95),
    Feed("DW", "https://rss.dw.com/rdf/rss-en-all", "мир", 1.0),
    Feed("France 24", "https://www.france24.com/en/rss", "мир", 0.95),
    Feed("The Guardian", "https://www.theguardian.com/world/rss", "мир", 1.1),
    Feed("Sky News", "https://feeds.skynews.com/feeds/rss/world.xml", "мир", 0.95),
    Feed("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", "экономика", 1.0),
    Feed("Euronews", "https://www.euronews.com/rss", "мир", 0.95),
    Feed("Japan Times", "https://www.japantimes.co.jp/feed/", "мир", 0.9),
    Feed("Anadolu", "https://www.aa.com.tr/en/rss/default?cat=world", "мир", 0.85),
    # --- игры и железо ---
    Feed("IGN", "https://feeds.ign.com/ign/games-all", "игры", 0.9),
    Feed("GameSpot", "https://www.gamespot.com/feeds/news/", "игры", 0.85),
    Feed("Eurogamer", "https://www.eurogamer.net/feed", "игры", 0.85),
    Feed("Rock Paper Shotgun", "https://www.rockpapershotgun.com/feed", "игры", 0.8),
    Feed("Tom's Hardware", "https://www.tomshardware.com/feeds/all", "железо", 0.9),
    # --- русские ---
    Feed("Lenta.ru", "https://lenta.ru/rss/news", "мир", 0.95),
    Feed("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml", "мир", 1.05),
    Feed("ТАСС", "https://tass.ru/rss/v2.xml", "мир", 0.9),
    Feed("Ведомости", "https://www.vedomosti.ru/rss/news", "экономика", 1.0),
    Feed("Игромания", "https://www.igromania.ru/rss/news.xml", "игры", 0.85),
    Feed("3DNews", "https://www.3dnews.ru/news/rss/", "железо", 0.85),
    # --- спорт ---
    Feed("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml", "спорт", 0.9),
    Feed("Sky Sports", "https://www.skysports.com/rss/12040", "спорт", 0.85),
    Feed("CBS Sports", "https://www.cbssports.com/rss/headlines/", "спорт", 0.8),
    Feed("Guardian Sport", "https://www.theguardian.com/uk/sport/rss", "спорт", 0.85),
)

GAME_SOURCES = {"IGN", "GameSpot", "VG247", "Eurogamer", "Rock Paper Shotgun", "Игромания"}
SPORT_SOURCES = {"BBC Sport", "Sky Sports", "CBS Sports", "Guardian Sport"}

# Вес категории при оценке важности новости.
CATEGORY_WEIGHTS: dict[str, float] = {
    "конфликты": 1.20,
    "политика": 1.10,
    "экономика": 1.10,
    "происшествия": 1.05,
    "наука": 1.00,
    "технологии": 1.00,
    "мир": 0.95,
    "спорт": 0.90,
    "культура": 0.85,
    "игры": 0.85,
    "железо": 0.85,
}

# ---------------------------------------------------------------------------
# Теги. Ключ — категория, значение — список терминов.
# Термин с пробелом или бренд получает больший вес (см. text.py).
# ---------------------------------------------------------------------------

CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "конфликты": (
        "war", "wars", "military", "attack", "attacks", "missile", "missiles",
        "drone", "drones", "airstrike", "airstrikes", "troops", "ceasefire",
        "killed", "clashes", "clash", "terror", "terrorist", "explosion",
        "bomb", "bombing", "army", "militant", "militants", "shelling",
        "hostage", "hostages", "weapon", "weapons", "invasion", "soldier",
        "soldiers", "ukraine", "gaza", "rebel", "rebels", "insurgent",
        "offensive", "casualties", "evacuation", "mobilization", "nuclear",
        "война", "войны", "военный", "военные", "армия", "обстрел", "обстрелы",
        "ракета", "ракеты", "дрон", "дроны", "беспилотник", "теракт", "взрыв",
        "перемирие", "заложник", "заложники", "наступление", "бои", "погибли",
        "погиб", "ранены", "жертвы", "эвакуация", "мобилизация",
    ),
    "политика": (
        "election", "elections", "president", "minister", "government",
        "parliament", "senate", "vote", "votes", "sanctions", "summit",
        "diplomat", "diplomacy", "court", "judge", "protest", "protests",
        "referendum", "coup", "opposition", "treaty", "policy", "immigration",
        "border", "asylum", "embassy", "kremlin", "white house", "north korea",
        "prime minister", "presidential", "mayor", "governor", "cabinet",
        "выборы", "президент", "министр", "правительство", "парламент",
        "санкции", "саммит", "дипломат", "суд", "протест", "протесты",
        "госдума", "кремль", "законопроект", "оппозиция", "граница",
        "посольство", "премьер-министр", "мэр", "губернатор",
    ),
    "экономика": (
        "economy", "inflation", "gdp", "market", "markets", "stocks", "shares",
        "oil", "tariff", "tariffs", "trade", "export", "exports", "bank",
        "banks", "central bank", "currency", "dollar", "euro", "recession",
        "unemployment", "jobs", "budget", "tax", "taxes", "investment",
        "investor", "crypto", "bitcoin", "opec", "imf", "world bank",
        "interest rate", "stock market", "prices", "salary", "salaries",
        "экономика", "инфляция", "рынок", "рынки", "акции", "нефть", "рубль",
        "доллар", "евро", "банк", "банки", "тариф", "тарифы", "торговля",
        "бюджет", "налог", "налоги", "инвестиции", "безработица", "цены",
        "зарплата", "зарплаты", "бирже", "биржа",
    ),
    "технологии": (
        "ai", "artificial intelligence", "tech", "technology", "software",
        "startup", "startups", "google", "apple", "microsoft", "amazon",
        "openai", "chatgpt", "chip", "chips", "semiconductor", "semiconductors",
        "robot", "robots", "cyber", "hacker", "hackers", "hacking", "internet",
        "spacex", "nasa", "satellite", "rocket", "lunar", "mars", "quantum",
        "smartphone", "tiktok", "youtube", "facebook", "instagram",
        "elon musk", "tesla", "app", "apps", "android", "ios", "iphone",
        "искусственный интеллект", "нейросеть", "нейросети", "технологии",
        "смартфон", "интернет", "хакер", "хакеры", "взлом", "спутник",
        "робот", "роботы", "приложение", "телефон", "айфон",
    ),
    "спорт": (
        "football", "soccer", "match", "matches", "tournament", "cup",
        "league", "olympic", "olympics", "championship", "player", "players",
        "coach", "goal", "goals", "final", "semifinal", "cricket", "tennis",
        "basketball", "nba", "fifa", "uefa", "stadium", "striker",
        "formula 1", "world cup", "champions league", "grand slam", "medal",
        "fixture", "hockey", "volleyball", "boxing", "transfer", "injury",
        "футбол", "матч", "матчи", "турнир", "кубок", "лига", "хоккей",
        "олимпиада", "чемпионат", "игрок", "игроки", "тренер", "гол", "финал",
        "теннис", "бокс", "плей-офф", "сезон", "сборная", "трансфер",
        "вратарь", "форвард", "травма",
    ),
    "наука": (
        "study", "research", "scientists", "discovery", "climate", "emissions",
        "warming", "energy", "solar", "physics", "biology", "genetics",
        "fossil", "dinosaur", "brain", "asteroid", "vaccine", "virus",
        "health", "disease", "outbreak", "cancer", "space telescope", "ebola",
        "исследование", "ученые", "учёные", "климат", "энергия", "космос",
        "вакцина", "вирус", "здоровье", "болезнь", "эбола", "рак", "ученые",
    ),
    "культура": (
        "film", "movie", "movies", "cinema", "music", "album", "song",
        "concert", "festival", "art", "museum", "exhibition", "book", "novel",
        "celebrity", "actor", "actress", "singer", "oscar", "grammy",
        "cannes", "netflix", "fashion", "theatre", "series", "show",
        "фильм", "кино", "музыка", "альбом", "концерт", "фестиваль",
        "выставка", "книга", "сериал", "актёр", "актер", "мода", "премьера",
        "певец", "певица", "спектакль",
    ),
    "игры": (
        "video game", "videogame", "videogames", "gaming", "gamescom",
        "playstation", "xbox", "nintendo", "switch 2", "steam deck",
        "epic games", "steam", "gameplay", "dlc", "patch notes",
        "early access", "open world", "esports", "gta", "elden ring",
        "call of duty", "fortnite", "minecraft", "valorant", "counter-strike",
        "dota", "cyberpunk", "witcher", "baldur", "zelda", "mario",
        "release date", "sequel", "remake", "remaster", "game developer",
        "игра", "игры", "игру", "игровой", "геймер", "геймеры", "шутер",
        "рпг", "консоль", "консоли", "патч", "дополнение", "релиз",
        "геймплей", "киберспорт", "ранний доступ",
    ),
    "железо": (
        "gpu", "graphics card", "cpu", "processor", "rtx", "radeon",
        "geforce", "nvidia", "amd", "intel", "ryzen", "monitor", "keyboard",
        "mouse", "headset", "ssd", "motherboard", "gaming laptop",
        "gaming pc", "razer", "logitech", "hyperx", "steelseries",
        "overclock", "vram", "ddr5", "laptop", "chipmaker",
        "видеокарта", "процессор", "материнская плата", "монитор",
        "клавиатура", "мышь", "наушники", "накопитель", "оперативная память",
        "разгон", "игровой ноутбук", "игровой компьютер", "жёсткий диск",
        "жесткий диск", "ноутбук",
    ),
    "происшествия": (
        "fire", "fires", "crash", "crashes", "accident", "earthquake",
        "flood", "floods", "storm", "hurricane", "typhoon", "wildfire",
        "shooting", "stabbing", "murder", "collapse", "rescue", "missing",
        "пожар", "пожары", "авария", "крушение", "землетрясение", "наводнение",
        "ураган", "тайфун", "стрельба", "убийство", "обрушение", "спасатели",
        "пропали", "пропал",
    ),
}

# Бренды и названия, которые нельзя переводить буквально.
PROTECT_TERMS: tuple[str, ...] = (
    "Naughty Dog", "Rockstar Games", "CD Projekt Red", "FromSoftware",
    "Insomniac Games", "Santa Monica Studio", "Guerrilla Games",
    "Kojima Productions", "Larian Studios", "Respawn Entertainment",
    "Riot Games", "Epic Games", "Team Cherry", "PlatinumGames",
    "Square Enix", "Bandai Namco", "id Software", "Infinity Ward",
    "Rockstar", "Bethesda", "Ubisoft", "Blizzard", "Valve", "Bungie",
    "Remedy", "Capcom", "BioWare", "Arkane", "Crytek", "Techland",
    "Mojang", "Gearbox", "Atlus", "Sega", "Nintendo", "Activision",
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
    "PlayStation", "Nintendo Switch", "Steam Deck", "Xbox Series",
    "Take-Two", "Warner Bros", "OpenAI", "ChatGPT", "SpaceX",
    "Telegram", "YouTube", "Instagram", "Facebook", "TikTok",
    "Netflix", "Spotify", "WhatsApp", "Discord", "Twitch", "Reddit",
    "Nvidia", "GeForce", "Radeon", "Snapdragon", "Ryzen", "Huawei",
    "Xiaomi", "OnePlus", "iPhone", "iPad", "MacBook", "Samsung",
    "Google", "Microsoft", "Apple", "Amazon", "Tesla", "Sony",
)

# Эти термины защищаем только с заглавной буквы: иначе «halo»/«doom»/«hades»
# в обычном тексте ломают перевод.
AMBIGUOUS_TERMS = {
    "Doom", "Halo", "Hades", "Persona", "Valve", "Sonic", "Minecraft",
    "Remedy", "Steam", "Apple", "Amazon", "Sega", "Atlus", "Capcom",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_social",
    "traffic_source", "maca", "at_medium", "at_campaign", "at_source",
    "fbclid", "gclid", "yclid", "dclid", "msclkid", "igshid", "mc_cid",
    "mc_eid", "ref", "ref_src", "ref_url", "source", "campaign", "partnerid",
    "spm", "share", "sh", "_ga", "_gl", "cmpid", "ocid", "smid", "sr_share",
    "ito", "output", "amp",
}

DEFAULT_SKIP_PATTERNS: tuple[str, ...] = (
    r"\bhow to watch\b",
    r"\bwhere to (watch|stream)\b",
    r"\blive stream\b",
    r"\bhoroscope\b",
    r"\bcrossword\b",
    r"\bwordle\b",
    r"\bconnections hints?\b",
    r"\bbest deals?\b",
    r"\bdeal of the day\b",
    r"\bpromo code\b",
    r"\bdiscount code\b",
    r"\bgift guide\b",
    r"\bbuying guide\b",
    r"\bpodcast\b",
)

DEFAULT_BREAKING_KEYWORDS: tuple[str, ...] = (
    "breaking", "earthquake", "tsunami", "explosion", "blast", "airstrike",
    "assassination", "coup", "invasion", "nuclear", "missile strike",
    "state of emergency", "evacuated", "hostage", "plane crash", "derailment",
    "wildfire", "mass shooting", "killed", "dead", "deaths",
    "землетрясение", "цунами", "взрыв", "авиаудар", "покушение", "переворот",
    "вторжение", "ядерный", "ракетный удар", "чрезвычайное положение",
    "эвакуация", "заложники", "крушение", "лесной пожар", "стрельба",
    "погибли", "погиб", "жертвы", "срочно",
)

DEFAULT_OPINION_PATTERNS: tuple[str, ...] = (
    r"^opinion\b", r"^analysis\b", r"^editorial\b", r"^comment\b",
    r"^column\b", r"^взгляд\b", r"^мнение\b", r"^анализ\b", r"^колонка\b",
)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    def __init__(self, issues: Iterable[str]):
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: str
    admin_id: str = ""
    mm_email: str = ""

    feeds: tuple[Feed, ...] = DEFAULT_FEEDS
    source_weights: Mapping[str, float] = field(default_factory=dict)

    digest_hours: tuple[int, ...] = (8, 20)
    quiet_hours: tuple[int, int] | None = (23, 8)
    weekly_recap: bool = False
    weekly_recap_day: int = 6          # 0=понедельник ... 6=воскресенье
    weekly_recap_hour: int = 12
    weekly_recap_items: int = 10

    max_posts_per_run: int = 2
    max_per_source_per_run: int = 1
    max_per_category_per_run: int = 2
    max_posts_per_day: int = 60
    max_posts_per_hour: int = 6
    max_per_source_per_day: int = 12
    max_per_source_fetch: int = 8
    freshness_hours: int = 30
    min_score: float = 0.0
    post_delay: float = 3.0

    keep_days: int = 7
    max_state_posts: int = 5000
    title_window_hours: int = 48
    dedup_containment: float = 0.62
    dedup_jaccard: float = 0.55
    title_translate_limit: int = 30
    llm_title_budget: int = 8
    embedding_min_similarity: float = 0.86

    lead_limit: int = 650
    caption_limit: int = 1024
    tg_limit: int = 4096
    digest_items: int = 8
    digest_max_per_source: int = 2
    digest_window_hours: int = 24

    edit_updates: bool = True
    max_updates_per_cluster: int = 3
    update_min_interval_min: int = 10
    confirmation_label: bool = True
    translation_note: bool = True
    feedback_buttons: bool = False
    feedback_weight: float = 0.10
    source_buttons: bool = True
    opinion_label: bool = True

    breaking_at_night: bool = False
    breaking_keywords: tuple[str, ...] = DEFAULT_BREAKING_KEYWORDS
    skip_patterns: tuple[str, ...] = DEFAULT_SKIP_PATTERNS
    opinion_patterns: tuple[str, ...] = DEFAULT_OPINION_PATTERNS

    translate_chain: tuple[str, ...] = (
        "llm", "deepl", "yandex", "google", "mymemory", "argos",
    )
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    translate_model: str = "gpt-4o-mini"
    summary_model: str = "gpt-4o-mini"
    embedding_model: str = ""
    llm_summary: bool = True
    llm_timeout: int = 45
    deepl_api_key: str = ""
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    glossary_file: str = "glossary.txt"
    translate_cache_size: int = 1200
    embedding_cache_size: int = 600

    state_file: str = "state.json"
    state_redis_url: str = ""
    state_redis_token: str = ""
    state_redis_key: str = "newsbot:state"
    legacy_posted_file: str = "posted_news.txt"
    legacy_digest_file: str = "digest.txt"

    admin_commands: bool = True
    admin_poll_timeout: int = 0
    heartbeat_url: str = ""
    alert_cooldown_hours: int = 6
    channel_routes: Mapping[str, str] = field(default_factory=dict)

    feed_disable_after_fails: int = 5
    feed_disable_hours: int = 6
    feed_empty_alert_streak: int = 8
    fetch_timeout: tuple[int, int] = (10, 25)
    telegram_timeout: int = 30
    media_photo_max_mb: int = 10
    media_video_max_mb: int = 45

    dry_run: bool = False
    log_level: str = "INFO"
    warnings: tuple[str, ...] = ()
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    @property
    def source_weight_map(self) -> dict[str, float]:
        out = {f.name: f.weight for f in self.feeds}
        out.update({k: float(v) for k, v in self.source_weights.items()})
        return out

    def chat_for_category(self, category: str) -> str:
        return str(self.channel_routes.get(category) or self.channel_id)

    def feed_by_name(self, name: str) -> Feed | None:
        for f in self.feeds:
            if f.name.lower() == name.lower():
                return f
        return None

    def with_overrides(self, **kw: Any) -> "Config":
        return replace(self, **kw)


# ---------------------------------------------------------------------------
# Парсинг окружения
# ---------------------------------------------------------------------------


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or default).strip()


def _as_int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int,
            issues: list[str]) -> int:
    raw = _env(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        issues.append(f"{name}={raw!r} — не целое число, использую {default}")
        return default
    if not lo <= value <= hi:
        issues.append(f"{name}={value} вне диапазона {lo}..{hi}, использую {default}")
        return default
    return value


def _as_float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float,
              issues: list[str]) -> float:
    raw = _env(env, name)
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        issues.append(f"{name}={raw!r} — не число, использую {default}")
        return default
    if not lo <= value <= hi:
        issues.append(f"{name}={value} вне диапазона {lo}..{hi}, использую {default}")
        return default
    return value


def _as_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _env(env, name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да", "вкл"}


def _as_hours(env: Mapping[str, str], name: str, default: tuple[int, ...],
              issues: list[str]) -> tuple[int, ...]:
    raw = _env(env, name)
    if not raw:
        return default
    if raw.lower() in {"off", "none", "нет", "0", "выкл"}:
        return ()
    hours: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 0 <= int(part) <= 23:
            issues.append(f"{name}: {part!r} — не час 0..23")
            continue
        hours.append(int(part))
    return tuple(sorted(set(hours))) if hours else default


def _as_quiet(env: Mapping[str, str], name: str, default: tuple[int, int] | None,
              issues: list[str]) -> tuple[int, int] | None:
    raw = _env(env, name)
    if not raw:
        return default
    if raw.lower() in {"off", "none", "нет", "0"}:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*", raw)
    if not m:
        issues.append(f"{name}={raw!r} — ожидается формат 23-8 или off")
        return default
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23):
        issues.append(f"{name}={raw!r} — часы должны быть 0..23")
        return default
    if start == end:
        return None
    return (start, end)


def _as_json(env: Mapping[str, str], name: str, default: Any, issues: list[str]) -> Any:
    raw = _env(env, name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError as exc:
        issues.append(f"{name}: некорректный JSON ({exc})")
        return default


def _as_list(env: Mapping[str, str], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(env, name)
    if not raw:
        return default
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return tuple(str(x) for x in data if str(x).strip())
        except ValueError:
            pass
    sep = "||" if "||" in raw else ","
    return tuple(x.strip() for x in raw.split(sep) if x.strip())


def _build_feeds(env: Mapping[str, str], issues: list[str]) -> tuple[Feed, ...]:
    feeds = list(DEFAULT_FEEDS)
    disabled = {x.lower() for x in _as_list(env, "DISABLED_FEEDS", ())}
    feeds = [f for f in feeds if f.name.lower() not in disabled]

    extra = _as_json(env, "EXTRA_FEEDS", {}, issues)
    if isinstance(extra, dict):
        for name, spec in extra.items():
            name = str(name).strip()
            if not name:
                continue
            if isinstance(spec, str):
                feeds.append(Feed(name, spec))
            elif isinstance(spec, dict) and spec.get("url"):
                feeds.append(Feed(
                    name,
                    str(spec["url"]),
                    str(spec.get("category", "мир")),
                    float(spec.get("weight", 1.0)),
                ))
            else:
                issues.append(f"EXTRA_FEEDS[{name}]: нужен url или объект с url")
    return tuple(feeds)


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    critical: list[str] = []
    warns: list[str] = []

    token = _env(env, "BOT_TOKEN")
    if not token:
        critical.append("BOT_TOKEN не задан")
    elif ":" not in token:
        critical.append("BOT_TOKEN выглядит некорректно (нет ':')")

    channel = _env(env, "CHANNEL_ID")
    if not channel:
        critical.append("CHANNEL_ID не задан (например @my_channel или -1001234567890)")
    elif not (channel.startswith("@") or re.fullmatch(r"-?\d{5,}", channel)):
        critical.append("CHANNEL_ID должен начинаться с @ или быть числовым id канала")

    admin = _env(env, "ADMIN_ID")
    if admin and not re.fullmatch(r"-?\d{5,}", admin):
        warns.append("ADMIN_ID должен быть числовым id пользователя (команды недоступны)")

    base_url = _env(env, "OPENAI_BASE_URL") or _env(env, "LLM_BASE_URL") or "https://api.openai.com/v1"
    api_key = _env(env, "OPENAI_API_KEY") or _env(env, "LLM_API_KEY")
    translate_model = _env(env, "TRANSLATE_MODEL") or _env(env, "LLM_MODEL") or "gpt-4o-mini"
    summary_model = _env(env, "SUMMARY_MODEL") or translate_model

    chain = _as_list(env, "TRANSLATE_CHAIN", Config.translate_chain)
    known = {"llm", "deepl", "yandex", "google", "mymemory", "argos"}
    unknown = [c for c in chain if c.lower() not in known]
    if unknown:
        warns.append(f"TRANSLATE_CHAIN: неизвестные провайдеры {unknown}")
    chain = tuple(c.lower() for c in chain if c.lower() in known) or ("google", "mymemory", "argos")

    routes = _as_json(env, "CHANNEL_ROUTES", {}, warns)
    if not isinstance(routes, dict):
        warns.append("CHANNEL_ROUTES должен быть объектом {категория: chat_id}")
        routes = {}

    source_weights = _as_json(env, "SOURCE_WEIGHTS", {}, warns)
    if not isinstance(source_weights, dict):
        warns.append("SOURCE_WEIGHTS должен быть объектом {источник: вес}")
        source_weights = {}

    cfg = Config(
        bot_token=token,
        channel_id=channel,
        admin_id=admin,
        mm_email=_env(env, "MM_EMAIL"),
        feeds=_build_feeds(env, warns),
        source_weights={str(k): float(v) for k, v in source_weights.items()
                        if isinstance(v, (int, float, str)) and str(v).replace(".", "", 1).replace("-", "", 1).isdigit()},

        digest_hours=_as_hours(env, "DIGEST_HOURS", (8, 20), warns),
        quiet_hours=_as_quiet(env, "QUIET_HOURS", (23, 8), warns),
        weekly_recap=_as_bool(env, "WEEKLY_RECAP", False),
        weekly_recap_day=_as_int(env, "WEEKLY_RECAP_DAY", 6, 0, 6, warns),
        weekly_recap_hour=_as_int(env, "WEEKLY_RECAP_HOUR", 12, 0, 23, warns),
        weekly_recap_items=_as_int(env, "WEEKLY_RECAP_ITEMS", 10, 3, 20, warns),

        max_posts_per_run=_as_int(env, "MAX_POSTS_PER_RUN", 2, 0, 10, warns),
        max_per_source_per_run=_as_int(env, "MAX_PER_SOURCE_PER_RUN", 1, 1, 5, warns),
        max_per_category_per_run=_as_int(env, "MAX_PER_CATEGORY_PER_RUN", 2, 1, 5, warns),
        max_posts_per_day=_as_int(env, "MAX_POSTS_PER_DAY", 60, 0, 500, warns),
        max_posts_per_hour=_as_int(env, "MAX_POSTS_PER_HOUR", 6, 0, 100, warns),
        max_per_source_per_day=_as_int(env, "MAX_PER_SOURCE_PER_DAY", 12, 0, 100, warns),
        max_per_source_fetch=_as_int(env, "MAX_PER_SOURCE_FETCH", 8, 1, 50, warns),
        freshness_hours=_as_int(env, "FRESHNESS_HOURS", 30, 1, 168, warns),
        min_score=_as_float(env, "MIN_SCORE", 0.0, 0.0, 100.0, warns),
        post_delay=_as_float(env, "POST_DELAY", 3.0, 0.0, 60.0, warns),

        keep_days=_as_int(env, "KEEP_DAYS", 7, 1, 90, warns),
        max_state_posts=_as_int(env, "MAX_STATE_POSTS", 5000, 100, 50000, warns),
        title_window_hours=_as_int(env, "TITLE_WINDOW_H", 48, 1, 168, warns),
        dedup_containment=_as_float(env, "DEDUP_CONTAINMENT", 0.62, 0.3, 0.95, warns),
        dedup_jaccard=_as_float(env, "DEDUP_JACCARD", 0.55, 0.3, 0.95, warns),
        title_translate_limit=_as_int(env, "TITLE_TRANSLATE_LIMIT", 30, 0, 100, warns),
        llm_title_budget=_as_int(env, "LLM_TITLE_BUDGET", 8, 0, 50, warns),
        embedding_min_similarity=_as_float(env, "EMBEDDING_MIN_SIMILARITY", 0.86, 0.5, 0.99, warns),

        lead_limit=_as_int(env, "LEAD_LIMIT", 650, 100, 3000, warns),
        caption_limit=_as_int(env, "CAPTION_LIMIT", 1024, 200, 1024, warns),
        tg_limit=_as_int(env, "TG_LIMIT", 4096, 1000, 4096, warns),
        digest_items=_as_int(env, "DIGEST_ITEMS", 8, 1, 20, warns),
        digest_max_per_source=_as_int(env, "DIGEST_MAX_PER_SOURCE", 2, 1, 10, warns),
        digest_window_hours=_as_int(env, "DIGEST_WINDOW_HOURS", 24, 1, 72, warns),

        edit_updates=_as_bool(env, "EDIT_UPDATES", True),
        max_updates_per_cluster=_as_int(env, "MAX_UPDATES_PER_CLUSTER", 3, 0, 20, warns),
        update_min_interval_min=_as_int(env, "UPDATE_MIN_INTERVAL_MIN", 10, 0, 1440, warns),
        confirmation_label=_as_bool(env, "CONFIRMATION_LABEL", True),
        translation_note=_as_bool(env, "TRANSLATION_NOTE", True),
        feedback_buttons=_as_bool(env, "FEEDBACK_BUTTONS", False),
        source_buttons=_as_bool(env, "SOURCE_BUTTONS", True),
        feedback_weight=_as_float(env, "FEEDBACK_WEIGHT", 0.10, 0.0, 1.0, warns),
        opinion_label=_as_bool(env, "OPINION_LABEL", True),

        breaking_at_night=_as_bool(env, "BREAKING_AT_NIGHT", False),
        breaking_keywords=_as_list(env, "BREAKING_KEYWORDS", DEFAULT_BREAKING_KEYWORDS),
        skip_patterns=_as_list(env, "SKIP_PATTERNS", DEFAULT_SKIP_PATTERNS),
        opinion_patterns=_as_list(env, "OPINION_PATTERNS", DEFAULT_OPINION_PATTERNS),

        translate_chain=chain,
        openai_api_key=api_key,
        openai_base_url=base_url.rstrip("/"),
        translate_model=translate_model,
        summary_model=summary_model,
        embedding_model=_env(env, "EMBEDDING_MODEL"),
        llm_summary=_as_bool(env, "LLM_SUMMARY", True),
        llm_timeout=_as_int(env, "LLM_TIMEOUT", 45, 5, 180, warns),
        deepl_api_key=_env(env, "DEEPL_API_KEY"),
        yandex_api_key=_env(env, "YANDEX_API_KEY"),
        yandex_folder_id=_env(env, "YANDEX_FOLDER_ID"),
        glossary_file=_env(env, "GLOSSARY_FILE") or "",
        translate_cache_size=_as_int(env, "TRANSLATE_CACHE_SIZE", 1200, 100, 10000, warns),
        embedding_cache_size=_as_int(env, "EMBEDDING_CACHE_SIZE", 600, 50, 5000, warns),

        state_file=_env(env, "STATE_FILE") or "state.json",
        state_redis_url=_env(env, "STATE_REDIS_URL") or _env(env, "UPSTASH_REDIS_REST_URL"),
        state_redis_token=_env(env, "STATE_REDIS_TOKEN") or _env(env, "UPSTASH_REDIS_REST_TOKEN"),
        state_redis_key=_env(env, "STATE_REDIS_KEY") or "newsbot:state",
        legacy_posted_file=_env(env, "LEGACY_POSTED_FILE") or "posted_news.txt",
        legacy_digest_file=_env(env, "LEGACY_DIGEST_FILE") or "digest.txt",

        admin_commands=_as_bool(env, "ADMIN_COMMANDS", True),
        admin_poll_timeout=_as_int(env, "ADMIN_POLL_TIMEOUT", 0, 0, 50, warns),
        heartbeat_url=_env(env, "HEARTBEAT_URL"),
        alert_cooldown_hours=_as_int(env, "ALERT_COOLDOWN_H", 6, 1, 168, warns),
        channel_routes={str(k): str(v) for k, v in routes.items()},

        feed_disable_after_fails=_as_int(env, "FEED_DISABLE_AFTER_FAILS", 5, 1, 50, warns),
        feed_disable_hours=_as_int(env, "FEED_DISABLE_HOURS", 6, 1, 168, warns),
        feed_empty_alert_streak=_as_int(env, "FEED_EMPTY_ALERT_STREAK", 8, 1, 100, warns),
        fetch_timeout=(10, _as_int(env, "FETCH_TIMEOUT", 25, 5, 120, warns)),
        telegram_timeout=_as_int(env, "TELEGRAM_TIMEOUT", 30, 5, 120, warns),
        media_photo_max_mb=_as_int(env, "MEDIA_PHOTO_MAX_MB", 10, 1, 50, warns),
        media_video_max_mb=_as_int(env, "MEDIA_VIDEO_MAX_MB", 45, 1, 50, warns),

        dry_run=_as_bool(env, "DRY_RUN", False),
        log_level=(_env(env, "LOG_LEVEL") or "INFO").upper(),
        user_agent=_env(env, "USER_AGENT") or Config.user_agent,
        warnings=(),
    )

    # glossary_file="" means "glossary.txt if it exists" — resolve later.
    if critical:
        raise ConfigError(critical)
    cfg = cfg.with_overrides(warnings=tuple(warns))
    return cfg
