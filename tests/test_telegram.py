from __future__ import annotations

from newsbot.telegram import TelegramClient, TgResponse, split_html


def test_split_html_balances_tags():
    text = "<b>" + ("очень длинный текст " * 200) + "</b>\n\n<a href=\"https://example.com\">ссылка</a>"
    parts = split_html(text, 200)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 220
        assert part.count("<b>") == part.count("</b>")
        assert part.count("<a ") == part.count("</a>")


def test_split_html_short():
    assert split_html("hello", 100) == ["hello"]


def test_dry_run_send(cfg):
    client = TelegramClient(cfg, session=None, dry_run=True)
    result = client.send_text("@test", "<b>hello</b>")
    assert result.ok
    assert result.message_id is not None


def test_send_text_splits(cfg, monkeypatch):
    client = TelegramClient(cfg, session=None, dry_run=True)
    calls = []

    def fake_api(method, payload=None, timeout=None, files=None, retries=3):
        calls.append((method, payload))
        return TgResponse(True, {"ok": True, "result": {"message_id": len(calls)}})

    monkeypatch.setattr(client, "_api", fake_api)
    monkeypatch.setattr("newsbot.telegram.time.sleep", lambda *_: None)
    text = "<b>" + ("x" * (cfg.tg_limit + 100)) + "</b>"
    result = client.send_text("@test", text)
    assert result.ok
    assert result.parts >= 2
    assert len(calls) >= 2
    assert calls[0][1]["parse_mode"] == "HTML"


def test_html_parse_error_falls_back(cfg, monkeypatch):
    client = TelegramClient(cfg, session=None, dry_run=True)
    calls = []

    def fake_api(method, payload=None, timeout=None, files=None, retries=3):
        calls.append(payload)
        if len(calls) == 1:
            return TgResponse(False, error="HTTP 400: can't parse entities", status=400)
        return TgResponse(True, {"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(client, "_api", fake_api)
    result = client.send_text("@test", "<b>broken")
    assert result.ok
    assert len(calls) == 2
    assert "parse_mode" not in calls[1]


def test_edit_not_modified_is_ok(cfg, monkeypatch):
    client = TelegramClient(cfg, session=None, dry_run=True)
    monkeypatch.setattr(client, "_api", lambda *a, **k: TgResponse(
        False, error="HTTP 400: Bad Request: message is not modified", status=400))
    result = client.edit_text("@test", 10, "text")
    assert result.ok


def test_media_payload(cfg, monkeypatch):
    client = TelegramClient(cfg, session=None, dry_run=True)
    seen = {}

    def fake_api(method, payload=None, timeout=None, files=None, retries=3):
        seen["method"] = method
        seen["payload"] = payload
        seen["files"] = files
        return TgResponse(True, {"ok": True, "result": {"message_id": 5}})

    monkeypatch.setattr(client, "_api", fake_api)
    result = client.send_photo("@test", "caption", data=b"1234", filename="x.png",
                               mime="image/png", silent=True)
    assert result.ok
    assert seen["method"] == "sendPhoto"
    assert seen["payload"]["photo"] == "attach://upload"
    assert seen["payload"]["disable_notification"] is True
    assert seen["files"]["upload"][2] == "image/png"


def test_form_value_encodes_json(cfg):
    from newsbot.telegram import form_value

    buttons = [[{"text": "🔗 DW", "url": "https://example.com"}]]
    encoded = form_value({"inline_keyboard": buttons})
    assert isinstance(encoded, str)
    assert '"inline_keyboard"' in encoded and "'" not in encoded
    assert form_value(True) == "true"


def test_media_upload_sends_json_reply_markup(cfg):
    """Фото с кнопками раньше падало: reply_markup уходил питоновским repr."""
    client = TelegramClient(cfg, session=None, dry_run=False)
    captured = {}

    class Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"ok": True, "result": {"message_id": 5}}

    class Session:
        def post(self, url, data=None, files=None, timeout=None, json=None):
            captured["data"] = data
            captured["files"] = files
            return Resp()

    client.session = Session()
    buttons = [[{"text": "🔗 DW", "url": "https://example.com"}]]
    result = client.send_photo("@test", "<b>Заголовок</b>", data=b"x" * 1000,
                               mime="image/jpeg", buttons=buttons)
    assert result.ok
    assert captured["files"], "файл должен уходить multipart"
    markup = captured["data"]["reply_markup"]
    assert isinstance(markup, str)
    assert markup.startswith("{") and '"inline_keyboard"' in markup


def test_media_url_then_upload_fallback(cfg):
    """Если Telegram не смог скачать картинку по ссылке, грузим файлом."""
    client = TelegramClient(cfg, session=None, dry_run=False)
    calls = []

    def fake_api(method, payload=None, timeout=None, files=None, retries=3):
        calls.append("upload" if files else "url")
        if files:
            return TgResponse(True, {"ok": True, "result": {"message_id": 7}})
        return TgResponse(False, {"ok": False, "description": "failed to get HTTP URL content"},
                          status=400)

    client._api = fake_api
    result = client.send_photo(
        "@test", "подпись", url="https://example.com/broken.jpg",
        data=b"x" * 1000, mime="image/jpeg",
    )
    assert result.ok and result.message_id == 7
    assert calls == ["url", "upload"]


def test_media_plain_caption_fallback(cfg):
    client = TelegramClient(cfg, session=None, dry_run=False)
    captions = []

    def fake_api(method, payload=None, timeout=None, files=None, retries=3):
        captions.append(payload["caption"])
        if "can't parse entities" in payload["caption"] or "<" in payload["caption"]:
            return TgResponse(False, {"ok": False,
                                      "description": "Bad Request: can't parse entities"},
                              status=400)
        return TgResponse(True, {"ok": True, "result": {"message_id": 9}})

    client._api = fake_api
    result = client.send_photo("@test", "<b>заголовок</b>", url="https://example.com/i.jpg")
    assert result.ok
    assert any("<" not in cap for cap in captions)
