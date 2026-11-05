# Образ для запуска бота вне GitHub Actions.
# Сборка из корня репозитория:
#   docker build -f deploy/Dockerfile -t newsbot .
# Запуск:
#   docker run -d --name newsbot --restart unless-stopped \
#     --env-file .env -v "$PWD/data:/data" newsbot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow \
    STATE_FILE=/data/state.json

WORKDIR /app

# ca-certificates и tzdata нужны для HTTPS и московского времени в дайджестах.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# argostranslate тянет torch (~800 МБ) и нужен только как офлайн-фолбэк перевода.
# На маленьком сервере он не окупается: остаются google/mymemory из TRANSLATE_CHAIN.
RUN grep -v '^argostranslate' requirements.txt > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt

COPY newsbot ./newsbot
COPY news_bot.py glossary.txt pyproject.toml ./

RUN mkdir -p /data \
    && useradd --create-home --uid 10001 bot \
    && chown -R bot:bot /data /app
USER bot

VOLUME ["/data"]

# Одна итерация — обычный запуск бота, пауза — 10 минут.
CMD ["python", "news_bot.py", "--loop", "600"]
