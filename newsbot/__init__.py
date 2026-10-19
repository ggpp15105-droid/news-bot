"""News Bot — RSS → перевод → Telegram.

Пакет разбит на небольшие модули:
    config      настройки и валидация окружения
    util        общие утилиты (URL, атомарная запись, HTTP)
    state       состояние бота (JSON-файл или Redis REST)
    feeds       загрузка RSS и health-check лент
    text        текст, теги, скоринг, обрезка
    translate   цепочка переводчиков, LLM-саммари, кэш
    dedup       кластеризация сюжетов и дедупликация
    digest      дайджесты и недельный обзор
    telegram    Telegram Bot API
    admin       админ-команды через getUpdates
    bot         оркестрация запуска
"""

__version__ = "2.0.0"
