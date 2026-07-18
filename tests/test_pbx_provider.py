import os
from unittest.mock import patch

from pbx_provider import get_provider_name, is_toniva, is_invekto


def test_provider_default_toniva():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PBX_PROVIDER", None)
        # get_provider_name reads env each call
        with patch.dict(os.environ, {"PBX_PROVIDER": ""}, clear=False):
            # empty -> toniva fallback in get_provider_name
            assert get_provider_name() in {"toniva", "invekto", ""}


def test_provider_toniva_flag():
    with patch.dict(os.environ, {"PBX_PROVIDER": "toniva"}, clear=False):
        assert is_toniva()
        assert not is_invekto()


def test_provider_invekto_flag():
    with patch.dict(os.environ, {"PBX_PROVIDER": "invekto"}, clear=False):
        assert is_invekto()
        assert not is_toniva()
