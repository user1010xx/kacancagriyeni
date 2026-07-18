import os
from pathlib import Path
from unittest.mock import patch

from config_store import ConfigStore


def test_toniva_validate_requires_api_key(tmp_path: Path):
    path = tmp_path / "config.json"
    env = {
        "PBX_PROVIDER": "toniva",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_GROUP_CHAT_ID": "-1001",
        "TONIVA_API_KEY": "",
    }
    with patch.dict(os.environ, env, clear=False):
        # clear key if inherited
        os.environ["TONIVA_API_KEY"] = ""
        cfg = ConfigStore(path)
        errors = cfg.validate()
    assert "TONIVA_API_KEY" in errors


def test_toniva_ready_token(tmp_path: Path):
    path = tmp_path / "config.json"
    with patch.dict(
        os.environ,
        {
            "PBX_PROVIDER": "toniva",
            "TELEGRAM_GROUP_CHAT_ID": "-1001",
            "TONIVA_API_KEY": "tva_test_key",
            "TONIVA_QUEUE": "1000",
        },
        clear=False,
    ):
        cfg = ConfigStore(path)
        assert cfg.is_toniva
        assert cfg.pbx_ready_token() == "toniva"
        assert cfg.department_names == ["1000"]


def test_invekto_still_uses_company_code(tmp_path: Path):
    path = tmp_path / "config.json"
    with patch.dict(
        os.environ,
        {
            "PBX_PROVIDER": "invekto",
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_GROUP_CHAT_ID": "-1001",
            "INVEKTO_DEPARTMENT_NAME": "Gelen Arama",
        },
        clear=False,
    ):
        cfg = ConfigStore(path)
        cfg.company_code = "12345678"
        assert cfg.is_invekto
        assert cfg.pbx_ready_token() == "12345678"
        assert "Gelen Arama" in cfg.department_names
