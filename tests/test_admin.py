from __future__ import annotations

from newsbot.admin import HELP, AdminService, feedback_keyboard, source_buttons
from newsbot.telegram import SendResult

ADMIN_ID = 123456789


class FakeTg:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id, text, **kwargs):
        self.sent.append((str(chat_id), text))
        return SendResult(True, len(self.sent), str(chat_id))

    def answer_callback(self, callback_id, text=""):
        return True


def make_service(cfg, state, now=None, **hooks):
    tg = FakeTg()
    svc = AdminService(cfg, state, tg, now=now, **hooks)
    return svc, tg


def message(text: str, user_id: int = ADMIN_ID) -> dict:
    return {"text": text, "chat": {"id": user_id}, "from": {"id": user_id}}


def cluster_with_sources(*sources: str) -> dict:
    return {
        "id": "abc123",
        "links": [
            {"url": f"https://example.com/{i}", "source": source}
            for i, source in enumerate(sources)
        ],
    }


def test_source_buttons_skip_primary_and_dedup():
    cluster = cluster_with_sources("BBC World", "DW", "DW", "Lenta.ru")
    buttons = source_buttons(cluster)
    labels = [row[0]["text"] for row in buttons]
    assert labels == ["🔗 DW", "🔗 Lenta.ru"]
    assert buttons[0][0]["url"] == "https://example.com/1"


def test_source_buttons_single_source_empty():
    assert source_buttons(cluster_with_sources("BBC World")) == []


def test_source_buttons_respect_limit():
    cluster = cluster_with_sources("A", "B", "C", "D", "E", "F")
    assert len(source_buttons(cluster, limit=2)) == 2


def test_source_buttons_long_label_trimmed():
    cluster = cluster_with_sources("A", "Очень длинное название источника " * 3)
    label = source_buttons(cluster)[0][0]["text"]
    assert len(label) <= 64


def test_help_lists_new_commands():
    for command in ("/why", "/preview", "/test", "/doctor"):
        assert command in HELP


def test_why_dispatches_to_explain(cfg, state):
    seen = {}

    def explain(query):
        seen["query"] = query
        return "ОТВЕТ"

    svc, tg = make_service(cfg, state, explain=explain)
    assert svc._handle_message(message("/why https://example.com/a")) is True
    assert seen["query"] == "https://example.com/a"
    assert any(text == "ОТВЕТ" for _, text in tg.sent)


def test_why_without_args_shows_usage(cfg, state):
    svc, tg = make_service(cfg, state, explain=lambda q: "x")
    svc._handle_message(message("/why"))
    assert "Использование" in tg.sent[-1][1]


def test_preview_passes_limit(cfg, state):
    seen = {}

    def preview(limit):
        seen["limit"] = limit
        return f"ПРЕВЬЮ {limit}"

    svc, tg = make_service(cfg, state, preview=preview)
    svc._handle_message(message("/preview 3"))
    assert seen["limit"] == 3
    assert tg.sent[-1][1] == "ПРЕВЬЮ 3"


def test_preview_defaults_and_clamps(cfg, state):
    seen = []

    def preview(limit):
        seen.append(limit)
        return "ok"

    svc, _ = make_service(cfg, state, preview=preview)
    svc._handle_message(message("/preview"))
    svc._handle_message(message("/preview 999"))
    assert seen == [8, 20]


def test_test_command_builds_draft(cfg, state):
    seen = {}

    def preview_post(url):
        seen["url"] = url
        return "ЧЕРНОВИК"

    svc, tg = make_service(cfg, state, preview_post=preview_post)
    svc._handle_message(message("/test https://example.com/story"))
    assert seen["url"] == "https://example.com/story"
    texts = [text for _, text in tg.sent]
    assert any("Готовлю черновик" in text for text in texts)
    assert texts[-1] == "ЧЕРНОВИК"


def test_test_command_requires_url(cfg, state):
    svc, tg = make_service(cfg, state, preview_post=lambda url: "x")
    svc._handle_message(message("/test просто текст"))
    assert "Использование" in tg.sent[-1][1]


def test_doctor_command(cfg, state):
    svc, tg = make_service(cfg, state, doctor=lambda: "ОТЧЁТ")
    svc._handle_message(message("/doctor"))
    assert tg.sent[-1][1] == "ОТЧЁТ"


def test_commands_ignore_non_admin(cfg, state):
    svc, tg = make_service(cfg, state, explain=lambda q: "x")
    assert svc._handle_message(message("/why abc", user_id=999)) is False
    assert tg.sent == []


def test_unknown_command_returns_help(cfg, state):
    svc, tg = make_service(cfg, state)
    svc._handle_message(message("/nonsense"))
    assert "Неизвестная команда" in tg.sent[-1][1]


def test_config_mentions_source_buttons(cfg, state):
    svc, tg = make_service(cfg, state)
    svc._handle_message(message("/config"))
    assert "Кнопки источников" in tg.sent[-1][1]


def test_feedback_callback_records_vote(cfg, state):
    svc, _ = make_service(cfg, state)
    state.clusters["cid1"] = {"id": "cid1", "source": "BBC World", "links": []}
    handled = svc._handle_callback({
        "id": "cb1", "data": "fb:cid1:up", "from": {"id": ADMIN_ID},
    })
    assert handled is True
    assert state.data["feedback"]["cid1"]["up"] == 1


def test_feedback_keyboard_shape():
    rows = feedback_keyboard("abc")
    assert rows[0][0]["callback_data"] == "fb:abc:up"
    assert rows[0][1]["callback_data"] == "fb:abc:down"
