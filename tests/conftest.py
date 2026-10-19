from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from newsbot.config import load_config  # noqa: E402
from newsbot.state import State  # noqa: E402
from newsbot.util import MSK  # noqa: E402


@pytest.fixture
def now() -> float:
    return datetime(2026, 10, 19, 12, 0, tzinfo=MSK).timestamp()


@pytest.fixture
def cfg(tmp_path, now):
    env = {
        "BOT_TOKEN": "123456:TEST",
        "CHANNEL_ID": "@test_channel",
        "ADMIN_ID": "123456789",
        "DRY_RUN": "1",
        "STATE_FILE": str(tmp_path / "state.json"),
        "LEGACY_POSTED_FILE": str(tmp_path / "missing_posted.txt"),
        "LEGACY_DIGEST_FILE": str(tmp_path / "missing_digest.txt"),
        "QUIET_HOURS": "off",
        "WEEKLY_RECAP": "0",
        "DIGEST_HOURS": "8,20",
        "MAX_POSTS_PER_RUN": "2",
        "MAX_PER_SOURCE_PER_RUN": "1",
        "MAX_PER_CATEGORY_PER_RUN": "2",
        "MAX_POSTS_PER_DAY": "60",
        "MAX_POSTS_PER_HOUR": "6",
        "MAX_PER_SOURCE_PER_DAY": "12",
        "FRESHNESS_HOURS": "30",
        "POST_DELAY": "0",
        "OPENAI_API_KEY": "",
        "TRANSLATE_CHAIN": "google,mymemory,argos",
        "TITLE_TRANSLATE_LIMIT": "10",
        "LLM_TITLE_BUDGET": "0",
        "GLOSSARY_FILE": str(tmp_path / "missing_glossary.txt"),
    }
    return load_config(env)


@pytest.fixture
def state(cfg, now):
    return State.load(cfg, now)
