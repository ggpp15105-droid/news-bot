# Развёртывание вне GitHub Actions

Бот не требует GitHub: это обычный периодический процесс на Python. Здесь —
бесплатные площадки, которые реально подходят, и как переехать за 15 минут.

## Что нужно боту от площадки

| Требование | Значение |
|---|---|
| Процесс | запуск раз в 10 минут, длительность 2–3 мин |
| Python | 3.11+ и зависимости из `requirements.txt` (`lxml`, `trafilatura`) |
| Память | ~300–400 МБ на запуск без `argostranslate` (с ним — 1,5 ГБ+) |
| Состояние | файл 6–8 МБ, читается и пишется на каждом запуске |
| Сеть | исходящие HTTPS: RSS-ленты, Telegram, переводчики |

## Бесплатные варианты: что подходит

| Площадка | Что даёт | Подходит? |
|---|---|---|
| **GitHub Actions + внешний пингер** | те же 2000 мин/мес (в публичном репо — без лимита) | ✅ **да, самый простой путь** |
| **Oracle Cloud Always Free** | ARM 2 OCPU / 12 ГБ, 200 ГБ диска, всегда включён | ✅ да, полноценный сервер |
| **Google Cloud e2-micro** | 1 общий vCPU / 1 ГБ, always free в 3 регионах | ⚠️ тесно без `argostranslate`, но работает |
| **Домашний ПК / Raspberry Pi** | ваш интернет и электричество | ✅ да, если готовы держать включённым |
| Vercel / Netlify cron | Hobby — **раз в сутки**, не чаще | ❌ нет |
| Render free | cron только платный, веб-сервис спит через 15 мин | ❌ нет |
| Cloudflare Workers cron | Python-воркеры не тянут `lxml`/`trafilatura` | ❌ без переписывания |
| Railway / Fly.io | бесплатных тарифов больше нет | ❌ нет |
| Hugging Face Spaces | засыпает без трафика | ⚠️ ненадёжно |

**Порядок выбора:**

1. Не хотите переезжать — поставьте пингер на `workflow_dispatch` (о нём —
   раздел «GitHub режет расписание schedule» в основном README). 5 минут, ноль риска.
2. Хотите уйти с GitHub совсем — берите Oracle Cloud Always Free и Docker.

## Шаг 0. Остановите GitHub-расписание

**Важно:** два работающих экземпляра публикуют дубли, потому что состояние у них
разное. Перед переездом отключите workflow: GitHub → **Actions** → «News Bot» →
**⋯ → Disable workflow**. Либо удалите блок `schedule` из
`.github/workflows/news.yml`.

## Шаг 1. Сервер

### Oracle Cloud (рекомендую)

1. Регистрация на <https://cloud.oracle.com/free> — нужна карта (для проверки,
   списаний на Always Free нет). Часть регистраций отклоняет антифрод: помогает
   обычный браузер без VPN, реальный адрес и карта Visa/Mastercard.
2. Создайте инстанс: **Shape** — `VM.Standard.A1.Flex` (Ampere ARM),
   1–2 OCPU и 6–12 ГБ памяти, **Image** — Ubuntu 24.04.
   С июня 2026 бесплатный лимит ARM — 2 OCPU / 12 ГБ (было 4/24).
3. Скачайте SSH-ключ, подключитесь: `ssh -i ключ.key ubuntu@<IP>`.
4. В Security List откройте только 22/TCP — боту входящие порты не нужны.

> Свободные ARM-инстансы в популярных регионах часто «out of capacity».
> Пробуйте другой Availability Domain или регион.

### Google Cloud (если Oracle не даёт инстанс)

`e2-micro` в `us-central1`, `us-west1` или `us-east1` — always free, но 1 ГБ
памяти. Работает с образом без `argostranslate`.

## Шаг 2. Docker (проще всего)

```bash
# 1. Docker на чистую Ubuntu
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker

# 2. Код и настройки
sudo mkdir -p /opt/newsbot && sudo chown "$USER" /opt/newsbot
git clone https://github.com/ggpp15105-droid/news-bot.git /opt/newsbot
cd /opt/newsbot

cp .env.example .env
nano .env      # BOT_TOKEN, CHANNEL_ID, ADMIN_ID — минимум
chmod 600 .env

# 3. Состояние: перенесите текущее, иначе бот заново разберёт свежие новости
mkdir -p deploy/data
curl -fsSL https://raw.githubusercontent.com/ggpp15105-droid/news-bot/main/state.json \
  -o deploy/data/state.json

# 4. Запуск
cd deploy
docker compose up -d --build
docker compose logs -f --tail=50
```

Проверка, что всё живо:

```bash
docker compose ps                     # STATUS → Up
docker compose logs --tail=20         # «Итог: получено …, постов …»
docker compose exec newsbot python news_bot.py --doctor
```

Обновление после изменений в репозитории:

```bash
cd /opt/newsbot && git pull && cd deploy && docker compose up -d --build
```

## Шаг 3. Вариант без Docker (systemd)

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -r -m -d /opt/newsbot -s /bin/bash newsbot
sudo -u newsbot git clone https://github.com/ggpp15105-droid/news-bot.git /opt/newsbot
cd /opt/newsbot

sudo -u newsbot python3 -m venv .venv
sudo -u newsbot .venv/bin/pip install -r requirements.txt   # без argos — быстрее

sudo mkdir -p /opt/newsbot/data && sudo chown -R newsbot: /opt/newsbot/data
sudo curl -fsSL https://raw.githubusercontent.com/ggpp15105-droid/news-bot/main/state.json \
  -o /opt/newsbot/data/state.json && sudo chown newsbot: /opt/newsbot/data/state.json

# секреты — в отдельный файл, не в репозиторий
sudo cp .env.example /etc/newsbot.env && sudo chmod 600 /etc/newsbot.env
sudo nano /etc/newsbot.env

sudo cp deploy/newsbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot
systemctl status newsbot --no-pager
journalctl -u newsbot -f
```

Остановка и перезапуск: `sudo systemctl stop newsbot`, `sudo systemctl restart newsbot`.
Секреты читаются из `/etc/newsbot.env`, в git они не попадают.

## Что меняется по сравнению с GitHub Actions

| Было | Стало |
|---|---|
| Состояние коммитится в `state.json` | лежит в `deploy/data/` на сервере, репозиторий не растёт |
| `schedule` раз в 10 мин (по факту 3–4 раза в сутки) | `--loop 600`, стабильные 10 минут |
| Дайджест 08:00 и 20:00 ± несколько часов | ±10 минут |
| Нет доступа к серверу | SSH, логи, `--doctor` в любой момент |

Адаптивный темп (`ADAPTIVE_POSTING`) при частых запусках не срабатывает: 10 минут
простоя → ровно `MAX_POSTS_PER_RUN` постов, как раньше.

## Мониторинг

Бот уже умеет пинговать внешний URL после каждого запуска — переменная
`HEARTBEAT_URL`. Бесплатный <https://healthchecks.io>: создайте чек с периодом
10 минут и grace 15 минут, скопируйте URL в `.env`. Если бот упал или сервер
уснул — придёт письмо.

Быстрая проверка состояния без лишних слов: `/status` в Telegram у админа.

## Бэкап состояния

`state.json` — это память бота: без него он заново разберёт свежие новости и
может опубликовать повторы. Раз в сутки складывайте копию:

```bash
# crontab -e
0 4 * * * cp /opt/newsbot/deploy/data/state.json /opt/newsbot/deploy/data/state-$(date +\%F).json
```

Либо перенесите состояние в Upstash Redis (бесплатно 10 000 команд в сутки):
переменные `STATE_REDIS_URL` и `STATE_REDIS_TOKEN` уже поддержаны, тогда
локальный файл перестаёт быть источником правды.

## Откат на GitHub Actions

Ничего не потеряется: удалите `deploy/data/state.json` из-под сервера (или
закоммитьте его в репозиторий), включите workflow обратно и остановите контейнер
`docker compose down`. Формат состояния один и тот же.
