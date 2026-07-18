import os
from pathlib import Path
from unittest.mock import patch

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


def test_invalid_runtime_json_falls_back_to_default(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{invalid json", encoding="utf-8")

    with patch.dict(os.environ, {"TELEGRAM_GROUP_CHAT_ID": "-1001"}, clear=False):
        cfg = ConfigStore(path)

    assert cfg.company_code == ""