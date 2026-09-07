import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

import bot as app
from config_store import ConfigStore
from delivered_store import DeliveredStore
from gonder_control import GonderControl
from notifications import send_with_retry
from personnel_store import PersonnelStore
from phone_map_store import PhoneMapStore
from sent_store import SentStore


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    today = app._report_today()
    call = {"ID": "101", "Phone": "905551112233", "ChekInDate": today.isoformat(),
            "ChekInTime": "12:00:00", "Queue": "1000", "Status": "2"}
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app, "sent_store", SentStore(tmp_path / "sent.json"))
    monkeypatch.setattr(app, "delivered_store", DeliveredStore(tmp_path / "delivered.json"))
    monkeypatch.setattr(app, "personnel_store", PersonnelStore(tmp_path / "people.json"))
    monkeypatch.setattr(app, "phone_map_store", PhoneMapStore(tmp_path / "map.json"))
    monkeypatch.setattr(app, "gonder_control", GonderControl(tmp_path / "control.json"))
    monkeypatch.setattr(app, "config", ConfigStore(tmp_path / "config.json"))
    monkeypatch.setattr(app, "_MISSED_CALL_PROCESS_LOCK", asyncio.Lock())
    monkeypatch.setattr(app, "_GONDER_COMMAND_LOCK", asyncio.Lock())
    monkeypatch.setattr(app, "_require_company_code", lambda: "test")
    monkeypatch.setattr(app, "_fetch_kwargs", lambda: {})
    monkeypatch.setattr(app, "_cutoff_time_for_date", lambda target: None)
    monkeypatch.setattr(app, "fetch_missed_calls", lambda *args, **kwargs: [call])
    monkeypatch.setattr(app, "build_phone_dahili_cache", lambda *args: {})
    monkeypatch.setattr(app, "_dahili_cache", {})
    monkeypatch.setattr(app, "_dahili_cache_built_at", datetime.now())
    monkeypatch.delenv("BACKFILL_DATES", raising=False)
    return today, call, SimpleNamespace(send_message=AsyncMock())


def test_unmatched_call_retries_after_mapping_and_reload(delivery, monkeypatch):
    today, call, telegram = delivery
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    assert len(app.sent_store.pending_calls(today)) == 1
    assert app.delivered_store.get_by_call_date(today) == []
    assert telegram.send_message.await_count == 1
    app.phone_map_store.set(call["Phone"], "105")
    app.personnel_store.add_or_update("105", "Ali", "ali", telegram_chat_id="123")
    monkeypatch.setattr(app, "sent_store", SentStore(app.sent_store.path))
    monkeypatch.setattr(app, "fetch_missed_calls", lambda *args, **kwargs: [])
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    assert telegram.send_message.await_count == 2
    assert telegram.send_message.call_args.kwargs["chat_id"] == 123
    assert len(app.delivered_store.get_by_call_date(today)) == 1
    assert app.sent_store.pending_calls(today) == []


def test_dm_is_recorded_before_group_failure_without_duplicate(delivery):
    today, call, telegram = delivery
    app.phone_map_store.set(call["Phone"], "105")
    app.personnel_store.add_or_update("105", "Ali", "ali", telegram_chat_id="123")
    telegram.send_message.side_effect = [None, RuntimeError("group unavailable"), None]
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    assert len(app.delivered_store.get_by_call_date(today)) == 1
    assert app.sent_store.is_private_notified_any(app.call_key_variants(call))
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    assert telegram.send_message.await_count == 3
    assert len(app.delivered_store.get_by_call_date(today)) == 1
    assert app.sent_store.is_complete_any(app.call_key_variants(call))


def test_failed_dm_not_reported(delivery):
    today, call, telegram = delivery
    app.phone_map_store.set(call["Phone"], "105")
    app.personnel_store.add_or_update("105", "Ali", "ali", telegram_chat_id="123")
    telegram.send_message.side_effect = [RuntimeError("blocked"), None]
    assert asyncio.run(app._process_missed_calls_for_date(telegram, today)) == (0, 1)
    assert app.delivered_store.get_by_call_date(today) == []
    assert app.sent_store.pending_calls(today)


def test_poll_retains_error_when_later_day_succeeds(delivery, monkeypatch):
    today, call, telegram = delivery
    app.config.last_poll_date = today - timedelta(days=2)
    process = AsyncMock(side_effect=[app.PbxError("failure"), (0, 0), (0, 0)])
    monkeypatch.setattr(app, "_process_missed_calls_for_date", process)
    context = SimpleNamespace(bot=telegram, bot_data={})
    asyncio.run(app.poll_missed_calls(context))
    assert context.bot_data["last_poll_error"] == "failure"
    assert app.config.last_poll_date == today - timedelta(days=2)
    assert process.await_count == 3


def test_replay_preserves_existing_delivery_history(delivery, monkeypatch):
    today, call, telegram = delivery
    app.phone_map_store.set(call["Phone"], "105")
    app.personnel_store.add_or_update("105", "Ali", "ali", telegram_chat_id="123")
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    monkeypatch.setenv("BACKFILL_THROTTLE_SECONDS", "0")
    app.gonder_control.begin([today])
    asyncio.run(app._run_gonder_job(telegram, -100, [today]))
    assert len(app.delivered_store.get_by_call_date(today)) == 2
    assert app.sent_store.is_complete_any(app.call_key_variants(call))
    assert not app.gonder_control.is_running()


@pytest.mark.parametrize("status,allowed", [("member", False), ("administrator", True), ("creator", True)])
def test_management_requires_admin(delivery, status, allowed):
    callback = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123),
                             effective_chat=SimpleNamespace(id=app.config.target_chat_id),
                             effective_message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status))))
    asyncio.run(app.admin_only(callback)(update, context))
    assert bool(callback.await_count) is allowed


def test_retry_after_is_bounded(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    send = AsyncMock(side_effect=[RetryAfter(1), None])
    asyncio.run(send_with_retry(send, chat_id=1, text="test"))
    assert send.await_count == 2
    sleep.assert_awaited_once()
    send = AsyncMock(side_effect=RetryAfter(1))
    with pytest.raises(RetryAfter):
        asyncio.run(send_with_retry(send, chat_id=1, text="test"))
    assert send.await_count == 3


def test_backfill_failure_does_not_mark_complete(delivery, monkeypatch):
    today, call, telegram = delivery
    monkeypatch.setenv("BACKFILL_DATES", today.strftime("%d.%m.%Y"))
    monkeypatch.setenv("BACKFILL_ON_STARTUP", "true")
    monkeypatch.setattr(app, "_process_missed_calls_for_date", AsyncMock(side_effect=app.PbxError("offline")))
    with pytest.raises(app.PbxError):
        asyncio.run(app._backfill_missed_calls(SimpleNamespace(bot=telegram)))
    assert not app.config.is_backfilled(today, "14:57:00")


def test_backfill_pending_does_not_mark_complete(delivery, monkeypatch):
    today, call, telegram = delivery
    monkeypatch.setenv("BACKFILL_DATES", today.strftime("%d.%m.%Y"))
    monkeypatch.setenv("BACKFILL_AFTER_TIME", "00:00:00")
    monkeypatch.setenv("BACKFILL_ON_STARTUP", "true")
    monkeypatch.setenv("BACKFILL_THROTTLE_SECONDS", "0")
    asyncio.run(app._backfill_missed_calls(SimpleNamespace(bot=telegram)))
    assert not app.config.is_backfilled(today, "00:00:00")
    assert app.sent_store.pending_calls(today)


def test_pbx_report_failure_is_unknown_not_uncalled(delivery, monkeypatch):
    today, call, telegram = delivery
    def offline(*args):
        raise app.PbxError("offline")
    monkeypatch.setattr(app, "fetch_conversations", offline)
    rows = [{"phone": call["Phone"], "personel_adi": "Ali"}]
    actual = asyncio.run(app._build_delivered_report_rows(today, rows))
    assert actual[0]["callback_status"] == "Kontrol Edilemedi (PBX Hatası)"


def test_admin_lookup_failure_is_denied(delivery):
    callback = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123),
                             effective_chat=SimpleNamespace(id=app.config.target_chat_id),
                             effective_message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock(side_effect=RuntimeError("offline"))))
    asyncio.run(app.admin_only(callback)(update, context))
    callback.assert_not_awaited()


def test_seed_covers_poll_lookback(delivery, monkeypatch):
    today, call, telegram = delivery
    from unittest.mock import Mock
    fetch = Mock(return_value=[])
    monkeypatch.setattr(app, "fetch_missed_calls", fetch)
    monkeypatch.setenv("SEED_TODAY_ON_STARTUP", "true")
    asyncio.run(app._seed_today_missed_calls_if_needed())
    assert fetch.call_args.args[1:3] == (today - timedelta(days=1), today)
    assert app.config.last_poll_date == today


def test_replay_cutoff_does_not_mutate_main_store(delivery):
    today, call, telegram = delivery
    replay = SentStore(app.DATA_DIR / "replay.json")
    app._apply_time_cutoff([call], today, "13:00:00", delivery_state=replay)
    assert replay.is_complete_any(app.call_key_variants(call))
    assert not app.sent_store.is_complete_any(app.call_key_variants(call))


def test_pending_delivery_flags_survive_retention_and_restart(delivery):
    today, call, telegram = delivery
    call["ChekInDate"] = (today - timedelta(days=60)).isoformat()
    keys = app.call_key_variants(call)
    app.sent_store.remember_calls([call])
    app.sent_store.mark_private_notified_keys(keys)
    app.sent_store.purge_old()
    restored = SentStore(app.sent_store.path)
    assert restored.is_private_notified_any(keys)
    assert restored.pending_calls(today - timedelta(days=60))


def test_stop_remains_available_during_serialized_management(delivery, monkeypatch):
    calls = []
    async def handler(update, context):
        calls.append(context.args[0])
    monkeypatch.setattr(app, "_gonder_command", handler)

    async def scenario():
        async with app._GONDER_COMMAND_LOCK:
            await app.gonder_command(None, SimpleNamespace(args=["durdur"]))

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))
    assert calls == ["durdur"]