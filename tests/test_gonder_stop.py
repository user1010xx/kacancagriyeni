""" /gonder durdur yardımcıları """

from datetime import date

import bot as bot_module
from gonder_control import GonderControl


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


def test_is_gonder_silence_request():
    assert bot_module._is_gonder_silence_request(["sessiz"])
    assert bot_module._is_gonder_silence_request(["silence"])
    assert not bot_module._is_gonder_silence_request(["durdur"])


def test_gonder_control_cancel_when_idle(tmp_path):
    ctrl = GonderControl(tmp_path / "gonder_state.json")
    had, msg = ctrl.request_cancel()
    assert had is False
    assert "yok" in msg.casefold() or "sessiz" in msg.casefold()


def test_gonder_control_cancel_when_running(tmp_path):
    ctrl = GonderControl(tmp_path / "gonder_state.json")
    ctrl.begin([date(2026, 7, 20), date(2026, 7, 21)])
    assert ctrl.is_running() is True
    had, msg = ctrl.request_cancel()
    assert had is True
    assert ctrl.should_stop() is True
    assert "durdur" in msg.casefold() or "🛑" in msg
    ctrl.finish()
    assert ctrl.is_running() is False


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
