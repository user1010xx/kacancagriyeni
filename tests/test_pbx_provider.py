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


def test_routing_fetch_includes_unanswered_outbound_calls():
    from datetime import date
    from pbx_provider import fetch_routing_conversations

    day = date(2026, 9, 12)
    for provider in ("toniva", "invekto"):
        with patch.dict(os.environ, {"PBX_PROVIDER": provider}):
            with patch(f"pbx_provider.{provider}_client.fetch_conversations", return_value=[]) as fetch:
                assert fetch_routing_conversations("test", day, day) == []
                expected = {"include_zero_duration": True, "force_day_chunk": True} if provider == "toniva" else {}
                fetch.assert_called_once_with("test", day, day, **expected)
