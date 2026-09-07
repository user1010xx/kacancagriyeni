import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as app
from config_store import ConfigStore
from json_storage import load_json, save_json
from privacy_logging import RedactingFormatter


def test_logs_redact_message_and_exception(monkeypatch):
    secret = "test-secret-value"
    monkeypatch.setenv("TONIVA_API_KEY", secret)
    try:
        raise RuntimeError(f"api={secret} phone=905551112233")
    except RuntimeError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, "", 1,
                                   "https://api.telegram.org/bot12345:abcDEF/sendMessage", (), sys.exc_info())
    rendered = RedactingFormatter().format(record)
    assert secret not in rendered
    assert "905551112233" not in rendered
    assert "abcDEF" not in rendered


def test_settings_never_show_key_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("PBX_PROVIDER", "toniva")
    monkeypatch.setenv("TONIVA_API_KEY", "tva_very_secret_api_key")
    assert "tva_very" not in ConfigStore(tmp_path / "config.json").as_text()


def test_atomic_write_retries_permission_error(monkeypatch, tmp_path):
    import json_storage
    replace = json_storage.os.replace
    calls = []

    def flaky(source, destination):
        calls.append(destination)
        if len(calls) == 1:
            raise PermissionError("temporary lock")
        return replace(source, destination)

    monkeypatch.setattr(json_storage.os, "replace", flaky)
    monkeypatch.setattr(json_storage.time, "sleep", lambda seconds: None)
    path = tmp_path / "store.json"
    save_json(path, {"test": 1})
    assert load_json(path, {}, dict) == {"test": 1}
    path.write_text("corrupted", encoding="utf-8")
    assert load_json(path, {}, dict) == {"test": 1}


def test_corrupt_primary_and_backup_fail_closed(tmp_path):
    path = tmp_path / "store.json"
    save_json(path, {})
    path.write_text("broken", encoding="utf-8")
    path.with_suffix(".json.bak").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        load_json(path, {}, dict)


def test_stalled_poll_alert_is_throttled(monkeypatch):
    monkeypatch.setattr(app, "gonder_control", SimpleNamespace(is_running=lambda: False))
    monkeypatch.setattr(app.time, "monotonic", lambda: 10000)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),
                              bot_data={"last_poll_success_monotonic": 1})
    asyncio.run(app.check_poll_health(context))
    asyncio.run(app.check_poll_health(context))
    assert context.bot.send_message.await_count == 1