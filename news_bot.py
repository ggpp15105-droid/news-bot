"""Точка входа: ``python news_bot.py``.

Вся логика — в пакете ``newsbot``. Этот файл оставлен для совместимости
с workflow и привычной команды запуска.
"""

from newsbot.bot import entrypoint

if __name__ == "__main__":
    entrypoint()
