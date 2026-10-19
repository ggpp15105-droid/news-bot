"""Точка входа: ``python news_bot.py``.

Вся логика — в пакете ``newsbot``. Опции командной строки описаны в
``newsbot/cli.py`` (``--doctor``, ``--dry-run``, ``--post``, ``--digest``).
"""

from newsbot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
