"""Перевод, LLM-саммари, глоссарий и эмбеддинги.

Цепочка провайдеров настраивается через ``TRANSLATE_CHAIN``. По умолчанию:
LLM → DeepL → Yandex → Google → MyMemory → Argos. Провайдер без ключа
просто пропускается. Все удачные переводы кэшируются в состоянии.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests

from .config import AMBIGUOUS_TERMS, PROTECT_TERMS, Config
from .state import State
from .text import cyrillic_ratio, is_russian
from .util import chunk_text, collapse_ws, stable_hash

log = logging.getLogger("news-bot.translate")

_PLACEHOLDER_RE = re.compile(r"XQ\s*(\d+)\s*ZX", re.I)
_BAD_TRANSLATION_RE = re.compile(
    r"(MYMEMORY WARNING|QUERY LENGTH LIMIT|INVALID (?:LANGUAGE|PAIR)|"
    r"NO QUERY SPECIFIED|PLEASE SELECT TWO DISTINCT LANGUAGES)",
    re.I,
)


@dataclass
class TranslationResult:
    text: str
    provider: str = ""
    ok: bool = True
    error: str = ""

    @property
    def used_fallback(self) -> bool:
        return not self.ok


@dataclass
class SummaryResult:
    title: str
    summary: str
    provider: str = "llm"
    ok: bool = True
    error: str = ""


def _clean_model_text(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json|text)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    value = value.strip().strip('"').strip("«»").strip()
    return collapse_ws(value) if "\n" not in value else value.strip()


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


class Translator:
    PROVIDER_NAMES = ("llm", "deepl", "yandex", "google", "mymemory", "argos")

    def __init__(self, cfg: Config, state: State, session: requests.Session,
                 now: float | None = None):
        self.cfg = cfg
        self.state = state
        self.session = session
        self.now = time.time() if now is None else now
        self.argos_ready = False
        self.argos_module = None
        self._deep_translator_cls = None
        self._embedding_disabled = False
        self._warned: set[str] = set()
        self.provider_failures: dict[str, int] = {}
        self.glossary = self._load_glossary()
        self.protect_terms = self._build_protect_terms()
        self._argos_init()
        self.provider_version = stable_hash(
            ",".join(cfg.translate_chain),
            cfg.translate_model,
            cfg.summary_model,
            json.dumps(self.glossary, ensure_ascii=False),
            length=20,
        )

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def _argos_init(self) -> None:
        try:
            from argostranslate import package as argo_package
            from argostranslate import translate as argo_translate

            installed = any(
                p.from_code == "en" and p.to_code == "ru"
                for p in argo_package.get_installed_packages()
            )
            if installed:
                self.argos_module = argo_translate
                self.argos_ready = True
        except Exception:
            self.argos_module = None

    def _load_glossary(self) -> dict[str, str]:
        path = self.cfg.glossary_file or "glossary.txt"
        out: dict[str, str] = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=>" in line:
                        term, translated = line.split("=>", 1)
                    elif "=" in line:
                        term, translated = line.split("=", 1)
                    else:
                        continue
                    term, translated = term.strip(), translated.strip()
                    if not term:
                        continue
                    out[term] = translated or term
                    if len(out) >= 300:
                        break
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Глоссарий %s: %s", path, exc)
        return out

    def _build_protect_terms(self) -> list[tuple[str, str, bool]]:
        """[(term, preferred_or_original, case_sensitive)]"""
        items: dict[str, tuple[str, bool]] = {}
        for term in PROTECT_TERMS:
            items[term.lower()] = (term, term in AMBIGUOUS_TERMS)
        for term, translated in self.glossary.items():
            items[term.lower()] = (translated, term in AMBIGUOUS_TERMS)
        result = [(term, repl, case) for term, (repl, case) in items.items()]
        result.sort(key=lambda x: len(x[0]), reverse=True)
        return result

    def _provider_available(self, name: str) -> bool:
        if name == "llm":
            return bool(self.cfg.openai_api_key)
        if name == "deepl":
            return bool(self.cfg.deepl_api_key)
        if name == "yandex":
            return bool(self.cfg.yandex_api_key and self.cfg.yandex_folder_id)
        if name == "argos":
            return self.argos_ready
        return name in {"google", "mymemory"}

    @property
    def available(self) -> list[str]:
        return [p for p in self.cfg.translate_chain if self._provider_available(p)]

    @property
    def has_llm(self) -> bool:
        return bool(self.cfg.openai_api_key)

    @property
    def has_embeddings(self) -> bool:
        return bool(self.cfg.openai_api_key and self.cfg.embedding_model
                    and not self._embedding_disabled)

    # ------------------------------------------------------------------
    # Защита терминов
    # ------------------------------------------------------------------

    def _protect(self, text: str) -> tuple[str, dict[str, str]]:
        holders: dict[str, str] = {}
        for index, (term, replacement, case_sensitive) in enumerate(self.protect_terms, 1):
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                pattern = re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", flags)
            except re.error:
                continue
            if not pattern.search(text):
                continue
            key = f"XQ{index}ZX"
            text = pattern.sub(key, text)
            holders[key] = replacement
        return text, holders

    def _restore(self, text: str, holders: dict[str, str]) -> str:
        if not holders:
            return text
        for key, replacement in holders.items():
            text = re.sub(r"\s*" + re.escape(key) + r"\s*", f" {replacement} ", text,
                          flags=re.IGNORECASE)
        # Некоторые переводчики вставляют пробел внутрь метки.
        text = _PLACEHOLDER_RE.sub(" ", text)
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def translate(self, text: str, mode: str = "text",
                  allow_llm: bool = True) -> TranslationResult:
        source = (text or "").strip()
        if not source:
            return TranslationResult("", "none", False, "empty")
        if is_russian(source):
            return TranslationResult(source, "source", True)

        mode_key = f"{mode}|{'full' if allow_llm else 'fast'}"
        key = State.cache_key(source, mode_key, self.provider_version)
        cached = self.state.translation_get(key, self.now)
        if cached:
            return TranslationResult(cached, "cache", True)

        providers = list(self.available)
        if not allow_llm:
            providers = [p for p in providers if p != "llm"]
            if not providers:
                # Быстрый переводчик недоступен — не тратим LLM на дедуп.
                return TranslationResult(source, "none", False, "нет быстрого переводчика")
        if not providers:
            return TranslationResult(source, "none", False, "нет доступных переводчиков")

        protected, holders = self._protect(source)
        errors: list[str] = []
        for provider in providers:
            if self.provider_failures.get(provider, 0) >= 2:
                continue
            try:
                raw = self._call_provider(provider, protected, mode)
                if not raw:
                    errors.append(f"{provider}: пусто")
                    continue
                restored = self._restore(raw, holders)
                ok, reason = self._validate(restored, source, mode)
                if not ok:
                    errors.append(f"{provider}: {reason}")
                    self.state.record_provider(provider, False)
                    continue
                self.state.record_provider(provider, True)
                self.state.translation_set(key, restored, self.now, provider)
                return TranslationResult(restored, provider, True)
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                self.state.record_provider(provider, False)
                self.provider_failures[provider] = self.provider_failures.get(provider, 0) + 1
                if self.provider_failures[provider] <= 1:
                    log.warning("Перевод %s не удался: %s", provider, exc)
        # Не кэшируем неудачу: следующий запуск попробует снова.
        return TranslationResult(source, "none", False, "; ".join(errors[:3]))

    def summarize(self, title: str, body: str, source: str = "") -> SummaryResult | None:
        """LLM-саммари на русском. None — если LLM недоступен/не удалось."""
        if not (self.has_llm and self.cfg.llm_summary):
            return None
        body = (body or "").strip()
        if len(body) < 80:
            return None
        prompt_title = collapse_ws(title)[:300]
        prompt_body = body[:6000]
        cache_text = f"{source}\n{prompt_title}\n{prompt_body}"
        key = State.cache_key(cache_text, "summary-v2", self.provider_version)
        cached = self.state.translation_get(key, self.now)
        if cached:
            data = _parse_json_object(cached)
            if data and data.get("title") and data.get("summary"):
                return SummaryResult(
                    _clean_model_text(str(data["title"])),
                    _clean_model_text(str(data["summary"])),
                    "cache",
                    True,
                )

        glossary_lines = [
            f"{term} → {translated}"
            for term, translated in list(self.glossary.items())[:40]
            if translated and translated != term
        ]
        system = (
            "Ты — редактор русскоязычного новостного канала. Переводи и кратко "
            "пересказывай новости на русском языке. Пиши нейтрально, только факты "
            "из текста, без домыслов и кликбейта. Сохраняй имена, топонимы, "
            "названия компаний, игр и брендов. Не добавляй своих оценок."
        )
        user = (
            "Сделай заголовок (до 120 символов) и краткое содержание (2–3 предложения, "
            "до 600 символов) на русском.\n"
            "Верни строго JSON без пояснений:\n"
            '{"title": "...", "summary": "..."}\n\n'
            f"Источник: {source}\n"
            f"Оригинальный заголовок: {prompt_title}\n"
            f"Текст: {prompt_body}"
        )
        if glossary_lines:
            user += "\n\nПредпочтительные переводы терминов:\n" + "\n".join(glossary_lines)

        try:
            raw = self._llm_chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                model=self.cfg.summary_model,
                max_tokens=700,
                temperature=0.2,
                json_mode=True,
            )
        except Exception as exc:
            log.warning("LLM-саммари не удалось: %s", exc)
            self.state.record_provider("llm-summary", False)
            return None

        data = _parse_json_object(raw)
        if not data:
            self.state.record_provider("llm-summary", False)
            return None
        out_title = _clean_model_text(str(data.get("title", "")))
        out_summary = _clean_model_text(str(data.get("summary", "")))
        if not out_title or not out_summary:
            self.state.record_provider("llm-summary", False)
            return None
        if cyrillic_ratio(out_title) < 0.3 or cyrillic_ratio(out_summary) < 0.3:
            self.state.record_provider("llm-summary", False)
            return None
        if len(out_title) > 220 or len(out_summary) > 900:
            out_title = out_title[:220].rstrip()
            out_summary = out_summary[:900].rstrip()
        self.state.record_provider("llm-summary", True)
        self.state.translation_set(
            key, json.dumps({"title": out_title, "summary": out_summary},
                            ensure_ascii=False),
            self.now, "llm-summary",
        )
        return SummaryResult(out_title, out_summary, "llm", True)

    # ------------------------------------------------------------------
    # Валидация
    # ------------------------------------------------------------------

    def _validate(self, text: str, source: str, mode: str) -> tuple[bool, str]:
        value = (text or "").strip()
        if not value:
            return False, "пустой ответ"
        if _BAD_TRANSLATION_RE.search(value):
            return False, "ответ содержит предупреждение сервиса"
        if _PLACEHOLDER_RE.search(value):
            return False, "не восстановлены названия"
        if cyrillic_ratio(value) < 0.35:
            return False, "мало кириллицы"
        ratio = len(value) / max(1, len(source))
        if ratio < 0.15 or ratio > 4.5:
            return False, f"подозрительная длина ({ratio:.2f})"
        if mode == "title" and "\n" in value:
            return False, "заголовок в несколько строк"
        if len(source) > 200 and len(value) < 40:
            return False, "слишком короткий перевод"
        return True, ""

    # ------------------------------------------------------------------
    # Провайдеры
    # ------------------------------------------------------------------

    def _call_provider(self, provider: str, text: str, mode: str) -> str:
        if provider == "llm":
            return self._llm_translate(text, mode)
        if provider == "deepl":
            return self._deepl_translate(text)
        if provider == "yandex":
            return self._yandex_translate(text)
        if provider == "google":
            return self._google_translate(text)
        if provider == "mymemory":
            return self._mymemory_translate(text)
        if provider == "argos":
            return self._argos_translate(text)
        raise ValueError(f"неизвестный провайдер {provider}")

    def _llm_translate(self, text: str, mode: str) -> str:
        instruction = (
            "Переведи на русский язык. Верни только перевод, без пояснений. "
            "Сохраняй имена, топонимы, названия компаний, брендов и игр. "
            "Стиль — нейтральный новостной."
        )
        if mode == "title":
            instruction += " Это заголовок: одна строка, без кавычек."
        raw = self._llm_chat(
            [{"role": "system", "content": instruction},
             {"role": "user", "content": text}],
            model=self.cfg.translate_model,
            max_tokens=1200 if mode != "title" else 200,
            temperature=0.1,
        )
        return _clean_model_text(raw)

    def _llm_chat(self, messages: list[dict], model: str, max_tokens: int,
                  temperature: float, json_mode: bool = False) -> str:
        if not self.cfg.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY не задан")
        url = f"{self.cfg.openai_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.cfg.openai_api_key}",
            "Content-Type": "application/json",
        }
        last_error = ""
        for attempt in range(2):
            try:
                r = self.session.post(url, json=payload, headers=headers,
                                      timeout=self.cfg.llm_timeout)
                if r.status_code == 429:
                    time.sleep(2 + attempt * 3)
                    last_error = "429"
                    continue
                if r.status_code >= 500:
                    time.sleep(1 + attempt * 2)
                    last_error = f"HTTP {r.status_code}"
                    continue
                if r.status_code == 400 and json_mode:
                    # Некоторые OpenAI-совместимые API не поддерживают
                    # response_format. Повторяем без него.
                    payload.pop("response_format", None)
                    json_mode = False
                    continue
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError("нет choices в ответе")
                content = (choices[0].get("message") or {}).get("content") or ""
                if not content.strip():
                    raise RuntimeError("пустой content")
                return content
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1 + attempt * 2)
        raise RuntimeError(last_error or "LLM недоступен")

    def _deepl_translate(self, text: str) -> str:
        key = self.cfg.deepl_api_key
        endpoint = (
            "https://api-free.deepl.com/v2/translate"
            if key.endswith(":fx") else "https://api.deepl.com/v2/translate"
        )
        parts = chunk_text(text, 4000)
        out: list[str] = []
        for part in parts:
            r = self.session.post(
                endpoint,
                data={"auth_key": key, "text": part, "target_lang": "RU"},
                timeout=40,
            )
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            data = r.json()
            translations = data.get("translations") or []
            if not translations:
                raise RuntimeError("пустой ответ DeepL")
            out.append(translations[0].get("text", ""))
        return "\n".join(out)

    def _yandex_translate(self, text: str) -> str:
        parts = chunk_text(text, 4000)
        out: list[str] = []
        for part in parts:
            r = self.session.post(
                "https://translate.api.cloud.yandex.net/translate/v2/translate",
                headers={
                    "Authorization": f"Api-Key {self.cfg.yandex_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "folderId": self.cfg.yandex_folder_id,
                    "targetLanguageCode": "ru",
                    "texts": [part],
                },
                timeout=40,
            )
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            translations = (r.json() or {}).get("translations") or []
            if not translations:
                raise RuntimeError("пустой ответ Yandex")
            out.append(translations[0].get("text", ""))
        return "\n".join(out)

    def _google_translate(self, text: str) -> str:
        if self._deep_translator_cls is None:
            from deep_translator import GoogleTranslator
            self._deep_translator_cls = GoogleTranslator
        translator = self._deep_translator_cls(source="auto", target="ru")
        parts = chunk_text(text, 4000)
        out = []
        for part in parts:
            translated = translator.translate(part)
            if not translated:
                raise RuntimeError("пустой ответ Google")
            out.append(translated)
        return "\n".join(out)

    def _mymemory_translate(self, text: str) -> str:
        parts = chunk_text(text, 450)
        out: list[str] = []
        for part in parts:
            params = {"q": part, "langpair": "en|ru"}
            if self.cfg.mm_email:
                params["de"] = self.cfg.mm_email
            r = self.session.get(
                "https://api.mymemory.translated.net/get",
                params=params,
                timeout=30,
            )
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
            if str(data.get("responseStatus")) != "200":
                raise RuntimeError(str(data.get("responseDetails"))[:100])
            value = (data.get("responseData") or {}).get("translatedText", "")
            if not value or _BAD_TRANSLATION_RE.search(value):
                raise RuntimeError("MyMemory вернул предупреждение")
            out.append(html_lib.unescape(value))
        return "\n".join(out)

    def _argos_translate(self, text: str) -> str:
        if not (self.argos_ready and self.argos_module):
            raise RuntimeError("Argos не установлен")
        parts = chunk_text(text, 1800)
        out = [self.argos_module.translate(part, "en", "ru") for part in parts]
        return "\n".join(x for x in out if x)

    # ------------------------------------------------------------------
    # Эмбеддинги
    # ------------------------------------------------------------------

    def embed(self, texts: Iterable[str]) -> list[list[float]] | None:
        if not self.has_embeddings:
            return None
        items = [collapse_ws(t)[:1000] for t in texts]
        if not items:
            return []
        result: list[list[float] | None] = [None] * len(items)
        missing: list[tuple[int, str, str]] = []
        for i, text in enumerate(items):
            key = stable_hash("embed", self.cfg.embedding_model, text, length=32)
            cached = self.state.embedding_get(key, self.now)
            if cached:
                result[i] = cached
            else:
                missing.append((i, key, text))
        if missing:
            try:
                vectors = self._embedding_request([text for _, _, text in missing])
            except Exception as exc:
                log.warning("Эмбеддинги недоступны: %s", exc)
                self._embedding_disabled = True
                return None
            for (index, key, _), vector in zip(missing, vectors):
                result[index] = vector
                self.state.embedding_set(key, vector, self.now)
        return [vec if vec is not None else [] for vec in result]

    def _embedding_request(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.cfg.openai_base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.cfg.openai_api_key}",
            "Content-Type": "application/json",
        }
        out: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = texts[start:start + 32]
            r = self.session.post(
                url,
                headers=headers,
                json={"model": self.cfg.embedding_model, "input": batch},
                timeout=self.cfg.llm_timeout,
            )
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            data = r.json()
            items = sorted(data.get("data") or [], key=lambda x: int(x.get("index", 0)))
            if len(items) != len(batch):
                raise RuntimeError("неполный ответ embeddings")
            out.extend([list(map(float, item.get("embedding") or [])) for item in items])
        return out

    # ------------------------------------------------------------------
    # Диагностика
    # ------------------------------------------------------------------

    def diagnostics(self) -> str:
        available = self.available
        if not available:
            return "нет доступных переводчиков"
        return ", ".join(available)
