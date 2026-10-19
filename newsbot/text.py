"""Текстовая обработка: токены, похожесть, лид, теги, важность."""

from __future__ import annotations

import html as html_lib
import math
import re
import unicodedata
from functools import lru_cache
from typing import Iterable
from urllib.parse import urljoin

from .config import (
    AMBIGUOUS_TERMS,
    CATEGORY_TERMS,
    CATEGORY_WEIGHTS,
    GAME_SOURCES,
    PROTECT_TERMS,
    SPORT_SOURCES,
)
from .util import clean_html, collapse_ws, smart_cut

_CYRILLIC_RE = re.compile(r"[а-яё]", re.I)
_LATIN_RE = re.compile(r"[a-z]", re.I)
_WORD_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")

EN_STOPWORDS = set(
    """
    the a an of in on for to and as at by with from after over is are was were
    be been am that this it its his her their our your not no but or so up out
    about into than then will would could can may might do does did has have had
    who what when where why how more most new news says say said report reports
    amid during year years day days week weeks month months two three four five
    first second back against before between under among across per via off down
    again further once here there all any both each few other some such only own
    same too very now s t don i me my we you he she they them if while just get
    got set put tells told going goes went according official officials update
    updates live latest breaking exclusive analysis opinion video watch photos
    photo read full story top today yesterday tomorrow world global international
    newsroom editor editors journalist journalists people person one two three
    four five six seven eight nine ten hundreds thousands millions billion
    """.split()
)

RU_STOPWORDS = set(
    """
    и в во не что он на я с со как а то все всё она так его но да ты к у же вы за
    бы по только ее её мне было вот от меня еще ещё нет о об из ему теперь когда
    даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам
    ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их
    чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот
    того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
    сейчас были куда зачем сказать всех никогда сегодня завтра вчера который
    которая которые которых этим эта эти это после перед через между также
    сообщает сообщил сообщила по данным источник источники новости новость
    главное видео фото срочно обновление обновлено эксклюзив мнение анализ
    """.split()
)

_STOPWORDS = EN_STOPWORDS | RU_STOPWORDS

# ---------------------------------------------------------------------------
# Нормализация и токены
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return text.replace("ё", "е")


def _stem_en(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith(("ches", "shes", "xes", "zes", "ses")) and len(word) > 4:
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        word = word[:-1]
    for suffix in (
        "ational", "ization", "fulness", "ousness", "iveness", "ization",
        "ance", "ence", "ian",
        "tional", "biliti", "lessli", "entli", "ation", "alism", "aliti",
        "ousli", "iviti", "fulli", "enci", "anci", "abli", "izer", "ator",
        "alli", "ing", "edly", "ed", "ly",
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


RU_ENDINGS = (
    "иями", "ями", "ами", "ов", "ев", "ий", "ый", "ая", "ое", "ые", "ой",
    "ей", "ую", "юю", "ах", "ях", "ам", "ям", "ом", "ем", "ою", "ею", "ия",
    "ье", "а", "я", "ы", "и", "е", "о", "у", "ю", "ь",
)


def _stem_ru(word: str) -> str:
    if len(word) <= 4:
        return word
    for ending in RU_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def stem(word: str) -> str:
    word = normalize_text(word)
    if _CYRILLIC_RE.search(word):
        return _stem_ru(word)
    return _stem_en(word)


def norm_tokens(text: str) -> frozenset[str]:
    """Набор стеммированных значимых слов."""
    text = _EMAIL_RE.sub(" ", _URL_RE.sub(" ", text or ""))
    text = normalize_text(text)
    out: set[str] = set()
    for raw in _WORD_RE.findall(text):
        if raw.isdigit() or len(raw) < 3 or raw in _STOPWORDS:
            continue
        token = stem(raw)
        if len(token) >= 3 and token not in _STOPWORDS:
            out.add(token)
    return frozenset(out)


def token_list(text: str) -> list[str]:
    return sorted(norm_tokens(text))


EVENT_TERMS = frozenset({
    "kill", "dead", "death", "die", "injur", "wound", "fire", "burn",
    "shoot", "stab", "attack", "strike", "blast", "explos", "bomb",
    "crash", "collaps", "earthquak", "flood", "hurrican", "typhoon",
    "wildfir", "evacuat", "rescu", "hostag", "ceasefir", "war", "invad",
    "protest", "riot", "election", "vote", "summit", "trial", "verdict",
    "guilt", "resign", "sentenc", "arrest", "detain", "sanction",
    "surger", "operat", "hospital", "cancer", "diagnos", "treatment",
    "vaccin", "outbreak", "ebola", "appear", "cut", "production", "price",
    "market", "inflation", "tariff", "trade", "deal",
    "погиб", "умер", "жертв", "ранен", "пожар", "стрельб", "взрыв",
    "атак", "удар", "крушен", "обрушен", "землетрясен", "наводнен",
    "ураган", "тайфун", "эваку", "спаса", "заложник", "перемир", "войн",
    "вторжен", "протест", "бунт", "выбор", "голосован", "саммит", "суд",
    "приговор", "винов", "отставк", "арест", "задерж", "санкц",
})


NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "hundred": "100", "dozens": "12", "dozen": "12",
    "один": "1", "одна": "1", "двое": "2", "два": "2", "две": "2",
    "три": "3", "четыре": "4", "пять": "5", "шесть": "6", "семь": "7",
    "восемь": "8", "девять": "9", "десять": "10", "двадцать": "20",
    "тридцать": "30", "сотня": "100", "сотни": "100", "десятки": "10",
}


def numbers_in(text: str) -> frozenset[str]:
    found = set(re.findall(r"\b\d{1,4}\b", text or ""))
    for word in re.findall(r"[^\W\d_]+", (text or "").lower().replace("ё", "е")):
        mapped = NUMBER_WORDS.get(word)
        if mapped:
            found.add(mapped)
    return frozenset(found)


def similarity(a: frozenset[str], b: frozenset[str], containment: float = 0.62,
               jaccard: float = 0.55) -> tuple[bool, float, int]:
    """Похожесть двух наборов токенов.

    Containment важнее Jaccard: короткий заголовок часто является
    подмножеством длинного. Возвращает (похожи, score, общих токенов).
    """
    if not a or not b:
        return False, 0.0, 0
    inter = len(a & b)
    if inter < 3:
        return False, 0.0, inter
    cont = inter / min(len(a), len(b))
    jac = inter / len(a | b)
    score = max(cont, jac)
    ok = (
        cont >= containment
        or jac >= jaccard
        or (inter >= 6 and cont >= 0.45)
        or (inter >= 4 and cont >= 0.5)
    )
    return ok, score, inter


NEGATION_TERMS = frozenset({
    "declin", "deni", "refus", "reject", "cancel", "halt", "suspend",
    "violat", "oppos", "fail", "lose", "defeat", "acquit", "dismiss",
    "отказ", "опроверг", "отмен", "приостанов", "наруш", "проигр", "оправд",
})


def has_negation(tokens: frozenset[str]) -> bool:
    return bool(tokens & NEGATION_TERMS)


def same_event(a: frozenset[str], b: frozenset[str], a_numbers: frozenset[str],
               b_numbers: frozenset[str]) -> bool:
    """Мягкое правило для пересказов: общий событийный термин + общие числа."""
    if has_negation(a) != has_negation(b):
        return False
    shared = a & b
    if len(shared) < 2:
        return False
    cont = len(shared) / max(1, min(len(a), len(b)))
    if a_numbers and b_numbers and (a_numbers & b_numbers) and (shared & EVENT_TERMS):
        return True
    if len(shared) >= 4 and cont >= 0.55:
        return True
    if len(shared) >= 3 and cont >= 0.5 and (shared & EVENT_TERMS):
        return True
    return False


def is_russian(text: str) -> bool:
    if not text:
        return False
    cyr = len(_CYRILLIC_RE.findall(text))
    lat = len(_LATIN_RE.findall(text))
    return cyr > lat


def cyrillic_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _CYRILLIC_RE.match(c)) / len(letters)


# ---------------------------------------------------------------------------
# Заголовки
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(
    r"^\s*(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\s*)*"
    r"(?:(?:breaking|live|live updates?|update|updated|watch|video|photos?|"
    r"in pictures|explained|exclusive|just in|developing|срочно|прямой эфир|"
    r"обновлено|обновление|видео|фото|эксклюзив)\s*[:\-–—]\s*)+",
    re.I,
)
_SUFFIX_RE = re.compile(
    r"\s+[-|–—]\s+(?:BBC(?: News| Sport)?|CNN|Reuters|AP|AFP|DW|Euronews|"
    r"Sky News|The Guardian|Al Jazeera|CNBC|IGN|GameSpot|Eurogamer)\s*$",
    re.I,
)


def normalize_title_for_dedup(title: str) -> str:
    title = collapse_ws(title or "")
    title = _PREFIX_RE.sub("", title)
    title = _SUFFIX_RE.sub("", title)
    # Live-блоги часто добавляют время в заголовок.
    title = re.sub(r"^\d{1,2}:\d{2}\s*", "", title)
    return title.strip(" -–—:|")


# ---------------------------------------------------------------------------
# Чистка summary и лида
# ---------------------------------------------------------------------------

_SUMMARY_BOILERPLATE = re.compile(
    r"(?:the post .{0,120} appeared first on .{0,120}"
    r"|continue reading.{0,120}"
    r"|read more\b.{0,160}"
    r"|subscribe.{0,120}newsletter"
    r"|sign up.{0,120}"
    r"|follow us on .{0,120}"
    r"|download (?:our|the) app.{0,120}"
    r"|all rights reserved.{0,80}"
    r"|this article was (?:originally )?published on .{0,120}"
    r"|image source[,:].{0,80}"
    r"|\(image: .{0,120}\)"
    r"|advertisement"
    r"|©.{0,120}"
    r")",
    re.I,
)

_JUNK_PHRASES = re.compile(
    r"\b(?:read more|read also|follow us|sign up|subscribe|advertisement|"
    r"all rights reserved|image source|getty images|reuters/|photograph:|"
    r"click here|download the app|share this|most read|related articles|"
    r"newsletter|cookie policy|privacy policy|terms of service|contact us|"
    r"support us|donate|become a member|already a subscriber|log in|"
    r"register now|listen to this article|watch the video|comments?)\b",
    re.I,
)

_DATELINE_RE = re.compile(
    r"^\s*(?:[A-Z][A-Z ,.'/-]{2,40}|[A-Z][A-Za-z .'-]{2,40})\s*(?:\(Reuters\))?\s*[-–—]\s*"
)
_DATE_START_RE = re.compile(r"^\s*\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")


def clean_summary(raw: str) -> str:
    text = clean_html(raw)
    text = _SUMMARY_BOILERPLATE.sub(" ", text)
    text = collapse_ws(text)
    return text.strip(" -–—:|")


def _strip_dateline(text: str) -> str:
    return _DATELINE_RE.sub("", text, count=1).strip()


def is_junk(paragraph: str) -> bool:
    p = (paragraph or "").strip()
    if not p:
        return True
    if len(p.split()) < 5:
        return True
    if p.startswith(("-", "–", "—", "•", "*", "·", "▪")):
        return True
    if p.lower().startswith("update:"):
        return True
    digits = sum(ch.isdigit() for ch in p)
    if digits > len(p) * 0.3:
        return True
    if _DATE_START_RE.match(p):
        return True
    if _JUNK_PHRASES.search(p) and len(p) < 400:
        return True
    if len(p) < 80 and p.endswith((":", "|")):
        return True
    return False


def make_lead(body: str, summary: str, limit: int) -> str:
    """Первые 1–3 абзаца статьи; если статьи нет — summary из ленты."""
    summary = clean_summary(summary)
    body = (body or "").strip()
    src = body if len(body) >= 200 else (summary or body)
    if not src:
        return ""
    paras = [p.strip() for p in re.split(r"\n{2,}", src) if p.strip()]
    if len(paras) == 1 and len(paras[0]) > limit * 2:
        # trafilatura иногда отдаёт всё одниим блоком.
        paras = [p.strip() for p in re.split(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ])", src)]

    out: list[str] = []
    total = 0
    for para in paras:
        if is_junk(para):
            continue
        para = _strip_dateline(para)
        if is_junk(para):
            continue
        if out and total + len(para) + 2 > limit:
            break
        out.append(para)
        total += len(para) + 2
        if total >= limit * 0.85:
            break
    if not out:
        return ""
    text = "\n\n".join(out)
    if len(text) > limit:
        text = smart_cut(text, limit, mark=False)
    return text.strip()


# ---------------------------------------------------------------------------
# Метаданные страницы
# ---------------------------------------------------------------------------

_META_PROPERTIES = (
    "og:title", "twitter:title", "og:description", "twitter:description",
    "og:image", "twitter:image", "og:image:secure_url", "og:video",
    "og:video:secure_url", "twitter:player:stream",
)


def _meta_patterns(name: str) -> tuple[re.Pattern, re.Pattern]:
    n = re.escape(name)
    return (
        re.compile(rf'<meta[^>]+(?:property|name)=["\']{n}["\'][^>]*content=["\']([^"\']*)["\']', re.I),
        re.compile(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{n}["\']', re.I),
    )


def meta_content(page: str, name: str) -> str:
    for pattern in _meta_patterns(name):
        m = pattern.search(page or "")
        if m and m.group(1).strip():
            return html_lib.unescape(m.group(1).strip())
    return ""


def page_title(page: str) -> str:
    for name in ("og:title", "twitter:title"):
        value = meta_content(page, name)
        if value:
            return collapse_ws(value)
    m = re.search(r"<title[^>]*>(.*?)</title>", page or "", re.I | re.S)
    return collapse_ws(clean_html(m.group(1))) if m else ""


def page_meta(page: str, base_url: str = "") -> dict[str, str]:
    image = meta_content(page, "og:image") or meta_content(page, "twitter:image") \
        or meta_content(page, "og:image:secure_url")
    video = meta_content(page, "og:video") or meta_content(page, "og:video:secure_url") \
        or meta_content(page, "twitter:player:stream")
    if base_url:
        image = urljoin(base_url, image) if image else ""
        video = urljoin(base_url, video) if video else ""
    return {
        "title": page_title(page),
        "description": meta_content(page, "og:description") or meta_content(page, "twitter:description"),
        "image": image,
        "video": video,
    }


# ---------------------------------------------------------------------------
# Теги
# ---------------------------------------------------------------------------


def _term_regex(term: str, case_sensitive: bool = False) -> re.Pattern:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    flags = 0 if case_sensitive else re.I
    if _CYRILLIC_RE.search(term) and len(term) >= 5:
        base = escaped
        for ending in ("а", "я", "ы", "и", "е", "о", "у", "ю", "ь"):
            if base.endswith(ending):
                base = base[: -len(ending)]
                break
        body = rf"(?<![а-яё]){base}[а-яё]{{0,4}}(?![а-яё])"
    elif term.isascii() and term.isalpha() and len(term) >= 3:
        body = rf"(?<![a-z]){escaped}(?:s|es|ed|ing)?(?![a-z])"
    else:
        body = rf"(?<![a-zа-яё]){escaped}(?![a-zа-яё])"
    return re.compile(body, flags)


@lru_cache(maxsize=1)
def _tag_rules() -> tuple[tuple[str, float, re.Pattern], ...]:
    rules: list[tuple[str, float, re.Pattern]] = []
    seen: set[tuple[str, str]] = set()
    order = list(CATEGORY_TERMS.keys())
    for category in order:
        for term in CATEGORY_TERMS[category]:
            low = term.lower()
            if (category, low) in seen:
                continue
            seen.add((category, low))
            weight = 1.0
            if " " in term:
                weight = 2.5
            if term in PROTECT_TERMS or term.lower() in {
                "google", "apple", "microsoft", "amazon", "openai", "chatgpt",
                "nvidia", "intel", "amd", "playstation", "xbox", "nintendo",
                "netflix", "tesla", "spacex", "iphone", "android", "ebola",
            }:
                weight = 2.5
            case_sensitive = term in AMBIGUOUS_TERMS
            rules.append((category, weight, _term_regex(term, case_sensitive)))
    return tuple(rules)


@lru_cache(maxsize=1)
def _skip_rules() -> tuple[re.Pattern, ...]:
    from .config import DEFAULT_SKIP_PATTERNS
    return tuple(re.compile(p, re.I | re.U) for p in DEFAULT_SKIP_PATTERNS)


def _compile_patterns(patterns: Iterable[str]) -> tuple[re.Pattern, ...]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.I | re.U))
        except re.error:
            continue
    return tuple(out)


def is_low_value(title: str, patterns: tuple[str, ...] = ()) -> bool:
    rules = _compile_patterns(patterns) if patterns else _skip_rules()
    return any(r.search(title or "") for r in rules)


def is_opinion(title: str, link: str = "", patterns: tuple[str, ...] = ()) -> bool:
    rules = _compile_patterns(patterns) if patterns else _compile_patterns(
        ("^opinion\\b", "^analysis\\b", "^editorial\\b", "^comment\\b",
         "^column\\b", "^взгляд\\b", "^мнение\\b", "^анализ\\b", "^колонка\\b")
    )
    text = title or ""
    if any(r.search(text) for r in rules):
        return True
    return bool(re.search(r"/(?:opinion|analysis|editorial|comment)(?:/|$)", link or "", re.I))


def detect_categories(source: str, title: str, summary: str,
                      source_hint: str = "") -> list[tuple[str, float]]:
    text = f"{title} {title} {summary}"
    scores: dict[str, float] = {}
    for category, weight, pattern in _tag_rules():
        matches = len(pattern.findall(text))
        if matches:
            scores[category] = scores.get(category, 0.0) + weight * min(matches, 3)
    if source_hint:
        scores[source_hint] = scores.get(source_hint, 0.0) + 3.0
    if source in GAME_SOURCES:
        scores["игры"] = scores.get("игры", 0.0) + 2.0
    if source in SPORT_SOURCES:
        scores["спорт"] = scores.get("спорт", 0.0) + 2.0
    if not scores:
        return [("мир", 0.0)]
    order = list(CATEGORY_TERMS.keys()) + ["происшествия"]
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], order.index(kv[0]) if kv[0] in order else 99))
    result: list[tuple[str, float]] = []
    for cat, score in ranked:
        if score < 2.0 and result:
            continue
        if score < 1.0:
            continue
        result.append((cat, score))
        if len(result) == 2:
            break
    return result or [("мир", 0.0)]


def detect_tags(source: str, title: str, summary: str, source_hint: str = "",
                opinion: bool = False) -> str:
    cats = [c for c, _ in detect_categories(source, title, summary, source_hint)]
    if opponent := ("мнение" if opinion else ""):
        if opponent not in cats:
            cats = cats[:1] + [opponent]
    return " ".join("#" + c for c in cats)


# ---------------------------------------------------------------------------
# Breaking / скоринг
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _breaking_rules() -> tuple[re.Pattern, ...]:
    from .config import DEFAULT_BREAKING_KEYWORDS
    return tuple(_term_regex(t) for t in DEFAULT_BREAKING_KEYWORDS)


def _breaking_rules_for(keywords: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    if not keywords:
        return _breaking_rules()
    return tuple(_term_regex(t) for t in keywords)


def is_breaking(title: str, summary: str, keywords: tuple[str, ...] = ()) -> bool:
    text = f"{title} {summary}"
    return any(r.search(text) for r in _breaking_rules_for(keywords))


_CLICKBAIT_RE = re.compile(
    r"\b(?:you won'?t believe|shocking|this is why|what happened next|"
    r"watch:|must see|goes viral|breaks the internet)\b",
    re.I,
)


def importance(*, published_ts: float, now: float, source_weight: float,
               category: str, breaking: bool, cluster_size: int,
               feedback: float = 0.0, opinion: bool = False,
               title: str = "", feedback_weight: float = 0.1) -> float:
    age_h = max(0.0, (now - published_ts) / 3600.0)
    recency = math.exp(-age_h / 18.0)
    score = 1.8 * recency
    score += 0.8 * float(source_weight or 1.0)
    score += 0.7 * float(CATEGORY_WEIGHTS.get(category, 0.95))
    if breaking:
        score += 1.0
    if cluster_size > 1:
        score += 0.5 * math.log2(cluster_size)
    if opinion:
        score -= 0.15
    if _CLICKBAIT_RE.search(title or ""):
        score -= 0.4
    if age_h > 24:
        score -= 0.5
    score += max(-0.25, min(0.25, feedback)) * feedback_weight * 10
    return round(score, 3)


def sort_categories(cats: list[tuple[str, float]]) -> list[str]:
    return [c for c, _ in cats]
