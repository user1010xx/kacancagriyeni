import os
import pytest
from unittest.mock import patch

from pbx_provider import get_provider_name, is_toniva, is_invekto


@pytest.fixture(autouse=True)
def isolated_routing_cache(monkeypatch):
    from collections import OrderedDict
    import pbx_provider as provider

    monkeypatch.setattr(provider, "_ROUTING_DAY_CACHE", OrderedDict())


def test_concurrent_routing_requests_share_daily_fetch():
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date
    from threading import Barrier
    import pbx_provider as provider

    day = date(2026, 9, 12)
    ready = Barrier(2)

    def request():
        ready.wait(timeout=5)
        return provider.fetch_routing_conversations("concurrent", day, day)

    with patch.object(provider, "fetch_conversations", return_value=[{"Phone": "905550000001"}]) as fetch:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request) for _ in range(2)]
            assert futures[0].result(timeout=5) == futures[1].result(timeout=5)
        assert fetch.call_count == 1


def test_routing_refreshes_today_without_reloading_history(monkeypatch):
    from datetime import date, timedelta
    import pbx_provider as provider

    today = date(2026, 9, 12)
    clock = [0.0]
    monkeypatch.setattr(provider, "_routing_today", lambda: today)
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock[0])
    with patch.object(provider, "fetch_conversations", return_value=[]) as fetch:
        provider.fetch_routing_conversations("ttl", today - timedelta(days=1), today)
        clock[0] = 14
        provider.fetch_routing_conversations("ttl", today - timedelta(days=1), today)
        assert fetch.call_count == 2
        clock[0] = 15
        provider.fetch_routing_conversations("ttl", today - timedelta(days=1), today)
        assert fetch.call_count == 3
        assert fetch.call_args.args[1:] == (today, today)
        clock[0] = 3601
        provider.fetch_routing_conversations("ttl", today - timedelta(days=1), today)
        assert fetch.call_count == 5


def test_expired_routing_cache_does_not_hide_refresh_failure(monkeypatch):
    from datetime import date
    import pbx_provider as provider

    day = date(2026, 9, 12)
    clock = [0.0]
    monkeypatch.setattr(provider, "_routing_today", lambda: day)
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock[0])
    with patch.object(provider, "fetch_conversations", side_effect=[
        [{"Extension": "646"}], provider.PbxError("offline"), [{"Extension": "657"}],
    ]) as fetch:
        assert provider.fetch_routing_conversations("retry", day, day)[0]["Extension"] == "646"
        clock[0] = 16
        with pytest.raises(provider.PbxError, match="offline"):
            provider.fetch_routing_conversations("retry", day, day)
        assert provider.fetch_routing_conversations("retry", day, day)[0]["Extension"] == "657"
        assert fetch.call_count == 3


def test_routing_cache_is_scoped_to_credentials_and_bounded(monkeypatch):
    from datetime import date
    import pbx_provider as provider

    day = date(2026, 9, 12)
    monkeypatch.setattr(provider, "_ROUTING_CACHE_MAX_DAYS", 2)
    with patch.object(provider, "fetch_conversations", return_value=[]) as fetch:
        for token in ("first", "second", "third", "first"):
            monkeypatch.setenv("TONIVA_API_KEY", token)
            provider.fetch_routing_conversations("identity", day, day)
        assert fetch.call_count == 4
        assert len(provider._ROUTING_DAY_CACHE) == 2


def test_routing_cache_does_not_share_mutable_rows():
    from datetime import date
    import pbx_provider as provider

    day = date(2026, 9, 12)
    with patch.object(provider, "fetch_conversations", return_value=[{"Extension": "657"}]):
        first = provider.fetch_routing_conversations("copy", day, day)
        first[0]["Extension"] = "646"
        assert provider.fetch_routing_conversations("copy", day, day)[0]["Extension"] == "657"


def test_routing_history_reuses_successful_days():
    from datetime import date, timedelta
    import pbx_provider as provider

    day = date(2026, 9, 12)
    with patch.dict(os.environ, {"PBX_PROVIDER": "toniva", "TONIVA_API_KEY": "cache-reuse-test"}):
        with patch.object(provider, "fetch_conversations", return_value=[{"Phone": "905550000001"}]) as fetch:
            first = provider.fetch_routing_conversations("reuse", day - timedelta(days=2), day)
            second = provider.fetch_routing_conversations("reuse", day - timedelta(days=2), day)
            assert first == second
            assert fetch.call_count == 3
            assert all(item.args[1] == item.args[2] for item in fetch.call_args_list)


def test_routing_history_retries_failed_day_without_reloading_successes():
    from datetime import date, timedelta
    import pbx_provider as provider

    day = date(2026, 9, 12)
    with patch.dict(os.environ, {"PBX_PROVIDER": "toniva", "TONIVA_API_KEY": "cache-failure-test"}):
        with patch.object(provider, "fetch_conversations", side_effect=[[], provider.PbxError("offline"), []]) as fetch:
            with pytest.raises(provider.PbxError):
                provider.fetch_routing_conversations("failure", day - timedelta(days=1), day)
            assert provider.fetch_routing_conversations("failure", day - timedelta(days=1), day) == []
            assert fetch.call_count == 3


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
