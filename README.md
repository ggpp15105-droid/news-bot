# news-bot — новостной Telegram-бот для GitHub Actions

Бот собирает новости из 30+ RSS-лент, находит дубли одного и того же события,
переводит заголовки и тексты на русский, оценивает важность и публикует
2–3 поста за запуск в Telegram-канал. Работает без своего сервера: запускается
по расписанию через GitHub Actions (`*/10 * * * *`) и хранит состояние в
`state.json` в репозитории (или в Upstash Redis).

## Что умеет

| Возможность | Как работает |
|---|---|
| **Сбор новостей** | 25 встроенных лент (мир, экономика, игры, железо, спорт, русские СМИ) + свои через `EXTRA_FEEDS` |
| **Дедупликация** | Канонизация URL (87 tracking-параметров), близость заголовков (containment/Jaccard), проверка чисел и отрицаний, эмбеддинги |
| **Перевод** | Цепочка LLM → DeepL → Yandex → Google → MyMemory → Argos, с валидацией, глоссарием и кэшем |
| **Оценка важности** | Свежесть + вес источника + категория + breaking + размер кластера + feedback − кликбейт |
| **Посты** | Картинка/видео или текст, HTML-разметка, подпись до 1024 символов, ссылка на источник |
| **Обновления** | Если к новости пришло подтверждение от другого источника — `editMessageText`, до 3 апдейтов на кластер |
| **Дайджесты** | 2 раза в сутки (08:00 и 20:00 МСК), 8–10 пунктов, token-фильтр против похожих заголовков |
| **Тихие часы** | По умолчанию 23:00–08:00 МСК; только breaking-новости, и те без звука |
| **Маршрутизация** | `CHANNEL_ROUTES` — разные категории в разные каналы |
| **Админка** | Команды в личке: статус, статистика, включение/выключение лент, пауза, ручной пост |
| **Устойчивость** | Circuit breaker на переводчиках, автоотключение упавших лент, алерты админу, heartbeat |

## Быстрый старт

```bash
git clone https://github.com/ggpp15105-droid/news-bot.git
cd news-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # заполнить BOT_TOKEN, CHANNEL_ID, ADMIN_ID
set -a && source .env && set +a
python news_bot.py --doctor    # preflight: токен, канал, ленты, переводчики
DRY_RUN=1 python news_bot.py   # полный цикл без публикации
```

### Командная строка

| Команда | Что делает |
|---|---|
| `python news_bot.py` | обычный запуск (то же, что делает workflow) |
| `python news_bot.py --doctor` | проверка окружения: токен, права в канале, ленты, переводчики, состояние, расписание; код возврата 1 при критических проблемах |
| `python news_bot.py --dry-run` | полный цикл без публикации |
| `python news_bot.py --post URL` | опубликовать конкретную ссылку (с `--dry-run` — показать черновик) |
| `python news_bot.py --digest` | отправить дайджест немедленно |
| `python news_bot.py --recap` | отправить недельный обзор |
| `python news_bot.py --loop [SEC]` | работать демоном: запуск раз в SEC секунд (по умолчанию 600). Нужен на VPS/Docker без GitHub Actions |
| `python news_bot.py --version` | версия |

Флаг `--dry-run` перекрывает `DRY_RUN` из окружения в обе стороны: удобно
проверить боевые ключи, ничего не публикуя.

Для локального запуска удобно использовать `python-dotenv` или просто
экспортировать переменные окружения — сам бот читает только `os.environ`.

### Telegram

1. Создайте бота у [@BotFather](https://t.me/BotFather), получите `BOT_TOKEN`.
2. Добавьте бота в канал **администратором** с правом публикации.
3. `CHANNEL_ID` — `@username` канала или числовой id вида `-1001234567890`.
4. `ADMIN_ID` — ваш личный numeric id (узнать у [@userinfobot](https://t.me/userinfobot));
   на него приходят алерты и там работают админ-команды.

### GitHub Actions

1. Форкните/склонируйте репозиторий, включите Actions.
2. В **Settings → Secrets and variables → Actions** добавьте секреты:
   `BOT_TOKEN`, `CHANNEL_ID`, `ADMIN_ID`, при необходимости `OPENAI_API_KEY`,
   `DEEPL_API_KEY`, `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`,
   `STATE_REDIS_URL`, `STATE_REDIS_TOKEN`, `HEARTBEAT_URL`.
3. Необязательные параметры — в **Variables** (см. таблицу ниже), workflow
   сам передаёт их в переменные окружения.
4. Workflow `News Bot` запускается каждые 10 минут, коммитит `state.json`
   обратно в `main` (только для `refs/heads/main`) и присылает алерт админу
   при падении.

**Ручной запуск:** Actions → **News Bot** → **Run workflow**. Доступны
параметры:

| Параметр | Что делает |
|---|---|
| `dry_run` | собрать и перевести новости, но ничего не публиковать |
| `digest` | отправить дайджест немедленно, не дожидаясь 08:00/20:00 |
| `post_url` | опубликовать конкретную ссылку и выйти |

Это же доступно локально через `--dry-run`, `--digest` и `--post`.

> Важно: для сохранения состояния workflow делает `git pull --rebase` и `push`
> в `main`. Если ветка защищена, разрешите push от GitHub Actions или
> используйте Upstash Redis (`STATE_REDIS_URL` / `STATE_REDIS_TOKEN`) — тогда
> состояние хранится вне репозитория.

## Переменные окружения

### Обязательные

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от BotFather |
| `CHANNEL_ID` | `@channel` или numeric id канала |
| `ADMIN_ID` | Numeric id администратора (без него не работают команды и алерты) |

### Расписание и объём

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DIGEST_HOURS` | `8,20` | Часы дайджестов по МСК, через запятую; `off` — выключить |
| `QUIET_HOURS` | `23-8` | Тихие часы (может переходить через полночь); `off` — выключить |
| `BREAKING_AT_NIGHT` | `false` | Публиковать breaking-новости ночью (без звука) |
| `WEEKLY_RECAP` | `false` | Еженедельный дайджест |
| `WEEKLY_RECAP_DAY` | `6` | День недели recap: 0 — понедельник … 6 — воскресенье |
| `WEEKLY_RECAP_HOUR` | `12` | Час recap по МСК |
| `MAX_POSTS_PER_RUN` | `2` | Постов за один запуск |
| `MAX_PER_SOURCE_PER_RUN` | `1` | Максимум постов с одной ленты за запуск |
| `MAX_PER_CATEGORY_PER_RUN` | `2` | Максимум постов одной категории за запуск |
| `MAX_POSTS_PER_DAY` | `60` | Суточный лимит |
| `MAX_POSTS_PER_HOUR` | `6` | Часовой лимит |
| `MAX_PER_SOURCE_PER_DAY` | `12` | Суточный лимит на источник |
| `MAX_PER_SOURCE_FETCH` | `8` | Сколько свежих записей брать из ленты |
| `ADAPTIVE_POSTING` | `true` | Больше постов за запуск, если прошлый был давно (см. ниже) |
| `POSTS_PER_HOUR_TARGET` | `1.5` | Сколько постов «капает» в час для адаптивного темпа |
| `MAX_POSTS_PER_RUN_HARD` | `8` | Потолок на один запуск в адаптивном режиме |
| `SCHEDULER_STALE_HOURS` | `3` | Простой, после которого бот пишет алерт о срезанном расписании |
| `FRESHNESS_HOURS` | `30` | Максимальный возраст новости |
| `MIN_SCORE` | `0.0` | Минимальный скор для публикации |
| `POST_DELAY` | `3.0` | Пауза между постами, секунды |

### Дедупликация и качество

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DEDUP_CONTAINMENT` | `0.62` | Порог вложенности токенов заголовка |
| `DEDUP_JACCARD` | `0.55` | Порог Jaccard |
| `TITLE_TRANSLATE_LIMIT` | `30` | Сколько заголовков переводить за запуск (0 — выкл.) |
| `LLM_TITLE_BUDGET` | `8` | Сколько заголовков отдать LLM за запуск |
| `EMBEDDING_MIN_SIMILARITY` | `0.86` | Порог косинусной близости для эмбеддингов |
| `EDIT_UPDATES` | `true` | Редактировать пост при подтверждении от другого источника |
| `MAX_UPDATES_PER_CLUSTER` | `3` | Максимум апдейтов на кластер |
| `UPDATE_MIN_INTERVAL_MIN` | `10` | Минимальный интервал между апдейтами, минуты |
| `CONFIRMATION_LABEL` | `true` | Пометка «подтверждено N источниками» |
| `TRANSLATION_NOTE` | `true` | Пометка о машинном переводе |
| `FEEDBACK_BUTTONS` | `false` | Кнопки 👍/👎 под постами |
| `SOURCE_BUTTONS` | `true` | Кнопки «🔗 Источник» на остальные ленты под постом (до 4) |
| `FEEDBACK_WEIGHT` | `0.10` | Влияние feedback на скор |
| `OPINION_LABEL` | `true` | Помечать мнения/аналитику |
| `SKIP_PATTERNS` | встроенные | Регулярки для отбраковки кликбейта (JSON-список) |

### Ленты и маршрутизация

| Переменная | Описание |
|---|---|
| `EXTRA_FEEDS` | JSON: `{"Название": "https://…/rss"}` или `{"Название": {"url": "…", "category": "наука", "weight": 1.1}}` |
| `DISABLED_FEEDS` | Имена отключённых лент через запятую |
| `SOURCE_WEIGHTS` | JSON: `{"BBC World": 1.2}` |
| `CHANNEL_ROUTES` | JSON: `{"игры": "@games_channel"}` |

### Перевод и LLM

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TRANSLATE_CHAIN` | `llm,deepl,yandex,google,mymemory,argos` | Порядок провайдеров |
| `OPENAI_API_KEY` | — | Ключ OpenAI-совместимого API |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL (для локальных/прокси) |
| `TRANSLATE_MODEL` | `gpt-4o-mini` | Модель перевода |
| `SUMMARY_MODEL` | = `TRANSLATE_MODEL` | Модель суммаризации |
| `EMBEDDING_MODEL` | — | Модель эмбеддингов; пусто — отключено |
| `LLM_SUMMARY` | `true` | Суммаризация через LLM |
| `LLM_TIMEOUT` | `45` | Таймаут LLM, секунды |
| `DEEPL_API_KEY` | — | DeepL |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | — | Yandex Translate |
| `MM_EMAIL` | — | E-mail для MyMemory (поднимает лимит) |
| `GLOSSARY_FILE` | `glossary.txt` | Файл глоссария (по строке на термин) |
| `TRANSLATE_CACHE_SIZE` | `1200` | Размер кэша переводов |
| `EMBEDDING_CACHE_SIZE` | `600` | Размер кэша эмбеддингов |

### Состояние и служебное

| Переменная | По умолчанию | Описание |
|---|---|---|
| `STATE_FILE` | `state.json` | Локальный файл состояния |
| `STATE_REDIS_URL` / `STATE_REDIS_TOKEN` | — | Upstash Redis REST (альтернатива файлу) |
| `STATE_REDIS_KEY` | `newsbot:state` | Ключ в Redis |
| `KEEP_DAYS` | `7` | Сколько дней хранить историю |
| `MAX_STATE_POSTS` | `5000` | Максимум записей в истории |
| `ADMIN_COMMANDS` | `true` | Опрос админ-команд |
| `ADMIN_POLL_TIMEOUT` | `0` | Сколько секунд опрашивать команды (0 — без ожидания) |
| `HEARTBEAT_URL` | — | URL для пинга после успешного запуска (healthchecks.io и т.п.) |
| `ALERT_COOLDOWN_H` | `6` | Кулдаун одинаковых алертов, часы |
| `FEED_DISABLE_AFTER_FAILS` | `5` | Отключить ленту после N ошибок подряд |
| `FEED_DISABLE_HOURS` | `6` | На сколько часов отключать |
| `FEED_EMPTY_ALERT_STREAK` | `8` | Алерт, если лента N раз подряд пустая |
| `FETCH_TIMEOUT` | `25` | Таймаут запросов, секунды |
| `TELEGRAM_TIMEOUT` | `30` | Таймаут Telegram API |
| `MEDIA_PHOTO_MAX_MB` | `10` | Максимальный размер фото |
| `MEDIA_VIDEO_MAX_MB` | `45` | Максимальный размер видео |
| `DRY_RUN` | `false` | Ничего не публиковать, только логи |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

Некорректные необязательные значения не роняют бота: используется значение
по умолчанию, а предупреждение пишется в лог. Отсутствие `BOT_TOKEN`,
`CHANNEL_ID` или их явно неверный формат — фатальная ошибка (`exit 2`).

## Админ-команды

Бот опрашивает личные сообщения от `ADMIN_ID` (Telegram `getUpdates`):

| Команда | Действие |
|---|---|
| `/status` | Состояние: пауза, ленты, лимиты, последние посты |
| `/stats` | Статистика публикаций за день/неделю |
| `/sources` | Список лент с состоянием |
| `/enable <лента>` / `/disable <лента>` | Включить/выключить ленту |
| `/pause <часы>` / `/resume` | Пауза публикаций / возобновление |
| `/post <url>` | Опубликовать статью по ссылке |
| `/why <url\|id\|текст>` | Почему новости нет в канале: опубликована / в очереди и что мешает / отфильтрована |
| `/preview [N]` | Топ очереди: что выйдет сейчас, что ждёт и почему |
| `/queue` | То же, что `/preview` |
| `/test <url>` | Черновик поста по ссылке — в личку, без публикации |
| `/doctor` | Preflight-проверка окружения прямо в Telegram |
| `/digest` | Собрать дайджест немедленно |
| `/recap` | Еженедельный дайджест |
| `/cache` | Очистить кэши |
| `/config` | Показать эффективную конфигурацию |
| `/version` | Версия бота |

Реакции 👍/👎 под постами (`FEEDBACK_BUTTONS=true`) учитываются в скоре.
Если сюжет подтвердили несколько лент, под постом появляются кнопки
«🔗 DW», «🔗 Lenta.ru» и т.д. (`SOURCE_BUTTONS`).

Типичный сценарий: «почему этой новости нет?» → скопировать ссылку из ленты →
`/why <ссылка>`. Команда ответит одно из трёх: уже опубликовано (с номером
поста), стоит в очереди (и что именно мешает — лимит, тихие часы, место в
очереди) или отфильтровано (кликбейт, устарело, лимит источника).

## Архитектура

```
news_bot.py            # тонкий entrypoint → newsbot.cli
newsbot/
  cli.py               # --doctor/--dry-run/--post/--digest/--recap
  doctor.py            # preflight-проверки окружения

deploy/                # запуск вне GitHub Actions (Docker, systemd)
  config.py            # Config dataclass, парсинг env, фиды, категории
  util.py              # МСК-время, безопасный HTTP (SSRF-защита), хэши, escape
  state.py             # State: state.json / Upstash Redis, миграция, prune, квоты
  text.py              # нормализация, стеммер, similarity, same_event, числа
  translate.py         # цепочка провайдеров, кэш, глоссарий, circuit breaker
  feeds.py             # загрузка RSS/Atom, фильтры, health лент
  dedup.py             # кластеры, канонизация URL, эмбеддинги
  digest.py            # дайджесты и recap
  telegram.py          # Bot API: sendPhoto/sendVideo/sendMessage/edit, split_html
  admin.py             # админ-команды, кнопки источников
  render.py            # сборка подписи и полного текста, лимиты
  article.py           # извлечение статьи, картинки, видео
  bot.py               # NewsBot: цикл, скоринг, публикация, алерты
  __init__.py
tests/                 # pytest: 130 тестов, без сети
```

Поток одного запуска:

```
fetch_rss → фильтр свежести/паттернов → перевод заголовков → дедуп в кластеры
→ скоринг → отбор (квоты, тихие часы) → загрузка статьи/медиа → перевод и
сокращение → публикация → апдейты кластеров → дайджесты/recap → сохранение
состояния → алерты/heartbeat
```

## Тесты и линтер

```bash
pip install -r requirements-dev.txt
python -m pytest              # 130 тестов, сеть не нужна
ruff check --select E,F,I     # стиль и импорты
```

Тесты покрывают: парсинг конфигурации, стеммер и `same_event`, дедуп и
канонизацию URL, дайджесты и token-фильтр, состояние и квоты, рендеринг
подписей (лимиты 1024/4096, целостность HTML), Telegram API и
`split_html`, полный цикл публикации с заглушками (2 поста за запуск,
отсутствие повторов, тихие часы, breaking ночью, апдейты), а также
`--doctor`/CLI, команды `/why` `/preview` `/test` `/doctor` и кнопки
источников.

CI-workflow `checks` (ruff + pytest) запускается на push/PR и перед
расписанием — так падение тестов не доходит до канала.

## Устройство дедупликации

1. **URL** — отбрасываются tracking-параметры, `http/https`, `www`, якоря.
2. **Текст** — заголовок нормализуется (регистр, пунктуация, числа-словами),
   токены стеммируются (EN/RU).
3. **Похожесть** — containment (`|A∩B| / min(|A|,|B|)`) и Jaccard; мягкие
   правила для коротких заголовков.
4. **Событие** — если оба заголовка содержат общий `EVENT_TERM` (fire, fire,
   пожар…) и совпадающее число/числительное — это одно событие.
5. **Отрицания** — «X declines» и «X endorses» не склеиваются.
6. **Эмбеддинги** — при заданном `EMBEDDING_MODEL` сравниваются векторы
   (порог `EMBEDDING_MIN_SIMILARITY`), если текстовых признаков мало.

## GitHub режет расписание schedule

GitHub не гарантирует частоту `schedule`: при высокой нагрузке запуски
пропускаются. Обычно `*/10` даёт 30–45 запусков в сутки, но бывают периоды,
когда остаётся 3–5 в сутки — тогда посты выходят редко, а дайджесты опаздывают
на несколько часов. Код бота тут ни при чём, лечится снаружи.

**Как проверить:**

```bash
gh run list --workflow news.yml --limit 40 \
  --json createdAt,event --jq '.[] | select(.event=="schedule") | .createdAt'
```

Если между запусками по 5–8 часов, а в YAML стоит `*/10` — это оно.

**Что бот делает сам:**

- `ADAPTIVE_POSTING` — за редкий запуск выходит больше постов:
  `MAX_POSTS_PER_RUN + часы простоя × POSTS_PER_HOUR_TARGET`, но не больше
  `MAX_POSTS_PER_RUN_HARD`, часовой (`MAX_POSTS_PER_HOUR`) и суточной квот.
  С настройками по умолчанию после 8-часовой паузы выйдет до 6 постов вместо
  2 — больше не даст часовой лимит.
- Если простой больше `SCHEDULER_STALE_HOURS`, бот пишет в личку алерт
  «GitHub не запускал бота N ч». То же видно в `/status`.
- Дайджест, приехавший в тихие часы, уходит без звука.

**Как вернуть частые запуски — внешний пингер (5 минут):**

1. Создать fine-grained token: GitHub → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens. Repository access — только
   `news-bot`, Permissions → Actions: **Read and write**.
2. Зарегистрироваться на [cron-job.org](https://cron-job.org) (бесплатно) и
   создать задачу с расписанием «каждые 10 минут»:
   - URL: `https://api.github.com/repos/ggpp15105-droid/news-bot/actions/workflows/news.yml/dispatches`
   - Method: `POST`
   - Headers:
     `Authorization: Bearer <токен>`,
     `Accept: application/vnd.github+json`
   - Body: `{"ref":"main"}`
3. Проверить: в Actions появится запуск с событием `workflow_dispatch`.

Проверка вручную из терминала:

```bash
curl -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/ggpp15105-droid/news-bot/actions/workflows/news.yml/dispatches \
  -d '{"ref":"main"}'
```

Токен хранится только в пингере — в репозиторий его класть не нужно.
`schedule` при этом можно оставить как страховку: лишние запуски бот
спокойно переносит, повторов не будет.

## Работа совсем без GitHub Actions

Если хочется уйти с GitHub на обычный сервер, бот это умеет: `--loop` крутит
обычные запуски с заданным интервалом, состояние живёт в файле на диске и
больше не коммитится в репозиторий.

Готовые файлы — в каталоге [`deploy/`](deploy/README.md):

| Файл | Для чего |
|---|---|
| `deploy/Dockerfile` | образ без `argostranslate` (torch ~800 МБ не нужен на сервере) |
| `deploy/docker-compose.yml` | запуск одной командой, автоперезапуск, состояние в `deploy/data/` |
| `deploy/newsbot.service` | unit для systemd, если Docker не нужен |
| `deploy/README.md` | пошагово: Oracle Cloud Always Free, GCP e2-micro, мониторинг, бэкап |

Короткая версия:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

Бесплатные площадки, которые действительно подходят: **Oracle Cloud Always
Free** (ARM 2 OCPU / 12 ГБ, с июня 2026 — было 4/24), **Google Cloud e2-micro**
(1 ГБ, тесновато без argos), домашний ПК или Raspberry Pi. Vercel/Netlify дают
cron только раз в сутки, Render — только платный cron, Cloudflare Workers не
потянет `lxml`/`trafilatura`.

**Перед переездом отключите GitHub-расписание** (Actions → News Bot →
Disable workflow): два экземпляра с разными состояниями опубликуют дубли.

## Траблшутинг

| Симптом | Что делать |
|---|---|
| `exit 2` при старте | Проверить `BOT_TOKEN` и `CHANNEL_ID` — в логе перечислены все проблемы |
| Бот молчит | Проверить `QUIET_HOURS`, `paused_until` (`/status`), лимиты `MAX_POSTS_*` |
| Посты и дайджесты приходят редко/с опозданием | GitHub срезал `schedule` — см. раздел «GitHub режет расписание schedule» |
| В постах нет картинок | Бот берёт `og:image` / JSON-LD / `image_src` → `<img>` со страницы → картинку из RSS. Ссылку отдаёт Telegram сразу, при ошибке скачивает и грузит файлом, а если картинка не открылась — пробует следующую. Если у источника картинок нет вообще, пост выйдет текстом. Проверить формат: `/test <url>` |
| Дубли в канале | Понизить `DEDUP_CONTAINMENT`/`DEDUP_JACCARD` нельзя — это увеличит склейку; наоборот, **повысить** пороги. Проверить `TRANSLATE_CHAIN` — без перевода EN/RU-дубли не находятся |
| Перевод не работает | Бот сам пропускает упавший провайдер после 2 ошибок; смотреть `WARNING` в логе, проверить ключи, поставить `TRANSLATE_CHAIN=google,mymemory,argos` |
| `state.json` не коммитится | Ветка защищена или нет прав `contents: write`; альтернатива — Upstash Redis |
| Лента «отключена» | Найдётся в `/sources`; `/enable <имя>` или ждать `FEED_DISABLE_HOURS` |

## Лицензия

MIT — см. [LICENSE](LICENSE).
