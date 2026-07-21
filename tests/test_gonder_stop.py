""" /gonder durdur yardımcıları """

import bot as bot_module


def test_is_gonder_stop_request_variants():
    assert bot_module._is_gonder_stop_request(["durdur"])
    assert bot_module._is_gonder_stop_request(["DURDUR"])
    assert bot_module._is_gonder_stop_request(["stop"])
    assert bot_module._is_gonder_stop_request(["iptal"])
    assert bot_module._is_gonder_stop_request(["cancel"])
    assert not bot_module._is_gonder_stop_request([])
    assert not bot_module._is_gonder_stop_request(None)
    assert not bot_module._is_gonder_stop_request(["20.07.2026"])
    assert not bot_module._is_gonder_stop_request(["20.07.2026", "21.07.2026"])


def test_request_gonder_cancel_when_idle():
    bot_module._GONDER_RUNNING = False
    bot_module._GONDER_CANCEL = False
    msg = bot_module._request_gonder_cancel()
    assert "yok" in msg.casefold() or "çalışan" in msg.casefold()
    assert bot_module._GONDER_CANCEL is False


def test_request_gonder_cancel_when_running():
    bot_module._GONDER_RUNNING = True
    bot_module._GONDER_CANCEL = False
    try:
        msg = bot_module._request_gonder_cancel()
        assert "durdur" in msg.casefold() or "🛑" in msg
        assert bot_module._GONDER_CANCEL is True
        assert bot_module._gonder_should_stop() is True
    finally:
        bot_module._GONDER_RUNNING = False
        bot_module._GONDER_CANCEL = False


def test_lookup_dahili_from_cache_variants():
    from notifications import lookup_dahili_from_cache

    cache = {"5352211581": "585"}
    assert lookup_dahili_from_cache(cache, "905352211581") == "585"
    assert lookup_dahili_from_cache(cache, "05352211581") == "585"
    assert lookup_dahili_from_cache(cache, "999") is None


def test_phone_map_store_roundtrip(tmp_path):
    from phone_map_store import PhoneMapStore

    store = PhoneMapStore(tmp_path / "phone_map.json")
    assert store.set("905352211581", "585")
    assert store.lookup("905352211581") == "585"
    assert store.lookup("5352211581") == "585"
    store2 = PhoneMapStore(tmp_path / "phone_map.json")
    assert store2.lookup("905352211581") == "585"
    assert store2.merge({"905304605429": "622"}) == 1
    assert store2.lookup("905304605429") == "622"
