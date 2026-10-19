"""Командная строка: ``python news_bot.py [опции]``.

Опции нужны для ручных проверок и кнопки Run workflow:

    --doctor          preflight-проверка окружения
    --dry-run         полный цикл без публикации
    --post URL        опубликовать конкретную ссылку
    --digest          отправить дайджест сейчас (в --dry-run — показать)
    --recap           отправить недельный обзор сейчас
    --version         версия бота
    --verbose         подробный лог (DEBUG)

Без опций поведение прежнее: один обычный запуск бота.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence

from . import __version__
from .config import Config, ConfigError, load_config
from .util import mask_secret, setup_logging

log = logging.getLogger("news-bot.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news_bot.py",
        description="Новостной бот: RSS → перевод → Telegram",
    )
    parser.add_argument("--doctor", action="store_true",
                        help="проверить окружение и ничего не публиковать")
    parser.add_argument("--dry-run", action="store_true",
                        help="полный цикл без публикации")
    parser.add_argument("--post", metavar="URL", default="",
                        help="опубликовать статью по ссылке (в --dry-run — показать черновик)")
    parser.add_argument("--digest", action="store_true",
                        help="отправить дайджест немедленно")
    parser.add_argument("--recap", action="store_true",
                        help="отправить недельный обзор немедленно")
    parser.add_argument("--version", action="store_true", help="показать версию")
    parser.add_argument("--verbose", action="store_true", help="лог уровня DEBUG")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(list(argv) if argv is not None else None)


def _load_config(args: argparse.Namespace) -> Config | None:
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        print("Ошибки конфигурации:", file=sys.stderr)
        for issue in exc.issues:
            print(f"  • {issue}", file=sys.stderr)
        print("\nПодсказка: BOT_TOKEN и CHANNEL_ID обязательны. "
              "Значения по умолчанию — в .env.example.", file=sys.stderr)
        return None
    if args.dry_run and not cfg.dry_run:
        cfg = cfg.with_overrides(dry_run=True)
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"news-bot {__version__}")
        return 0

    cfg = _load_config(args)
    if cfg is None:
        return 2

    level = "DEBUG" if args.verbose else cfg.log_level
    setup_logging(level)
    for warning in cfg.warnings:
        log.warning("Конфигурация: %s", warning)

    if args.doctor:
        from .doctor import run_cli
        return run_cli(cfg)

    from .bot import NewsBot

    bot = NewsBot(cfg)
    if args.post:
        try:
            if cfg.dry_run:
                draft = bot.preview_post(args.post)
                print(draft)
            else:
                print(bot.post_url(args.post))
        except Exception as exc:
            log.exception("Ручная публикация не удалась")
            print(f"❌ Ошибка: {mask_secret(str(exc), cfg.bot_token)}", file=sys.stderr)
            return 1
        return 0

    if args.digest or args.recap:
        weekly = bool(args.recap)
        try:
            if cfg.dry_run:
                text, count = bot.preview_digest(weekly=weekly)
                if not count:
                    print("Нет новостей для дайджеста")
                    return 0
                print(text)
                print(f"\n[DRY RUN] {count} новостей, ничего не отправлено")
            else:
                print(bot.force_recap() if weekly else bot.force_digest())
        except Exception as exc:
            log.exception("Дайджест не удался")
            print(f"❌ Ошибка: {mask_secret(str(exc), cfg.bot_token)}", file=sys.stderr)
            return 1
        return 0

    try:
        return bot.run()
    except Exception as exc:
        log.exception("Критическая ошибка")
        try:
            bot.add_alert(f"Критическая ошибка: {exc!r}")
            bot._flush_alerts()
            bot.state.save()
        except Exception:
            pass
        return 1
