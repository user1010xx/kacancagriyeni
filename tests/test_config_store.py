import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import pytest

from config_store import ConfigStore


def test_department_names_strip_quotes(tmp_path: Path):
    path = tmp_path / "config.json"
    with patch.dict(
        os.environ,
        {
            "INVEKTO_DEPARTMENT_NAME": '"Gelen Arama,MESAI DIŞI"',
            "TELEGRAM_GROUP_CHAT_ID": "-1001",
        },
        clear=False,
    ):
        cfg = ConfigStore(path)
    assert cfg.department_names == ["Gelen Arama", "MESAI DIŞI"]


def test_invalid_runtime_json_fails_without_erasing_data(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{invalid json", encoding="utf-8")

    with patch.dict(os.environ, {"TELEGRAM_GROUP_CHAT_ID": "-1001"}, clear=False):
        with pytest.raises(ValueError):
            ConfigStore(path)
    assert path.read_text(encoding="utf-8") == "{invalid json"


def test_invalid_runtime_json_recovers_backup(tmp_path):
    path = tmp_path / "config.json"
    config = ConfigStore(path)
    config.company_code = "12345678"
    path.write_text("{invalid", encoding="utf-8")
    assert ConfigStore(path).company_code == "12345678"
    assert list(tmp_path.glob("*.corrupt"))


def test_live_delivery_start_time_persists_across_restarts(tmp_path):
    path = tmp_path / "config.json"
    with patch.dict(os.environ, {"TELEGRAM_GROUP_CHAT_ID": "-1001"}, clear=False):
        config = ConfigStore(path)
        started = datetime(2026, 10, 9, 17, 54, 35, 598000)
        config.live_delivery_since = started

        restored = ConfigStore(path)

    assert restored.live_delivery_since == started