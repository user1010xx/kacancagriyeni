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
    monkeypatch.setattr(app, "fetch_routing_conversations", lambda *args: [
        {"Phone": call["Phone"], "Extension": "105", "Direction": "outbound",
         "Date": call["ChekInDate"], "Time": "11:00:00"},
    ])
    monkeypatch.setattr(app, "build_phone_dahili_cache", lambda *args: {})
    monkeypatch.setattr(app, "_dahili_cache", {})
    monkeypatch.setattr(app, "_dahili_cache_built_at", datetime.now())
    monkeypatch.delenv("BACKFILL_DATES", raising=False)
    return today, call, SimpleNamespace(send_message=AsyncMock())


def test_unmatched_call_retries_after_personnel_and_reload(delivery, monkeypatch):
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


def test_wrong_recipient_incident_routes_to_mahmut(delivery, monkeypatch):
    today, call, telegram = delivery
    call.update(Phone="905309644795", ChekInTime="13:53:42")
    app.phone_map_store.set(call["Phone"], "646")
    app.personnel_store.add_or_update("657", "Mahmut", "mahmut", telegram_chat_id="65700")
    app.personnel_store.add_or_update("646", "Elisa", "elisa", telegram_chat_id="64600")
    monkeypatch.setattr(app, "_dahili_cache", {"5309644795": "646"})
    rows = [
        {"Phone": call["Phone"], "Extension": extension, "Direction": direction,
         "Date": today.isoformat(), "Time": when, "Duration": 0}
        for extension, direction, when in [
            ("646", "outbound", "14:05:00"),
            ("646", "inbound", "13:53:42"),
            ("657", "outbound", "13:47:07"),
        ]
    ]
    from unittest.mock import Mock
    fetch = Mock(return_value=rows)
    monkeypatch.setattr(app, "fetch_routing_conversations", fetch)
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    assert fetch.call_args.args == ("test", today - timedelta(days=15), today)
    assert telegram.send_message.call_args_list[0].kwargs["chat_id"] == 65700
    assert all(item.kwargs["chat_id"] != 64600 for item in telegram.send_message.call_args_list)
    assert app.delivered_store.get_by_call_date(today)[0]["dahili"] == "657"
    asyncio.run(app._process_missed_calls_for_date(telegram, today))
    assert telegram.send_message.await_count == 2
    assert fetch.call_count == 1


def test_routing_api_failure_keeps_pending_without_sending(delivery, monkeypatch):
    today, call, telegram = delivery
    app.phone_map_store.set(call["Phone"], "105")
    app.personnel_store.add_or_update("105", "Ali", "ali", telegram_chat_id="123")
    from unittest.mock import Mock
    monkeypatch.setattr(app, "fetch_routing_conversations", Mock(side_effect=app.PbxError("offline")))
    with pytest.raises(app.PbxError):
        asyncio.run(app._process_missed_calls_for_date(telegram, today))
    telegram.send_message.assert_not_awaited()
    assert app.sent_store.pending_calls(today)
    assert app.delivered_store.get_by_call_date(today) == []


def test_routing_history_retries_transient_failure(delivery, monkeypatch):
    today, call, telegram = delivery
    app.phone_map_store.set(call["Phone"], "105")
    app.personnel_store.add_or_update("105", "Ali", "ali", telegram_chat_id="123")
    from unittest.mock import Mock

    fetch = Mock(side_effect=[
        app.PbxError("temporary"),
        [{
            "Phone": call["Phone"],
            "Extension": "105",
            "Direction": "outbound",
            "Date": call["ChekInDate"],
            "Time": "11:00:00",
        }],
    ])
    monkeypatch.setattr(app, "fetch_routing_conversations", fetch)
    monkeypatch.setattr(app.asyncio, "sleep", AsyncMock())

    asyncio.run(app._process_missed_calls_for_date(telegram, today))

    assert fetch.call_count == 2
    assert app.sent_store.is_complete_any(app.call_key_variants(call))


def test_saved_pending_call_is_processed_when_missed_call_api_is_unavailable(
    delivery, monkeypatch
):
    today, call, telegram = delivery
    app.sent_store.remember_calls([call])

    def fetch_unavailable(*args, **kwargs):
        raise app.PbxError("Toniva HTTP 503")

    monkeypatch.setattr(app, "fetch_missed_calls", fetch_unavailable)

    asyncio.run(app._process_missed_calls_for_date(telegram, today))

    telegram.send_message.assert_awaited()
    assert app.sent_store.pending_calls(today)


def test_today_scan_summary_is_exposed_in_stats_data(delivery):
    today, _, telegram = delivery
    context = SimpleNamespace(bot=telegram, bot_data={})

    asyncio.run(
        app._process_missed_calls_for_date(telegram, today, context=context)
    )

    assert "API=1" in context.bot_data["last_today_scan"]
    assert "uygun=1" in context.bot_data["last_today_scan"]


def test_debug_routing_uses_requested_call_time_without_mutation(delivery, monkeypatch):
    today, call, telegram = delivery
    app.personnel_store.add_or_update("657", "Mahmut", "mahmut", telegram_chat_id="65700")
    app.phone_map_store.set("905309644795", "646")
    row = {"Phone": "905309644795", "Extension": "657", "Direction": "outbound",
           "Date": "12.09.2026", "Time": "13:47:07"}
    from unittest.mock import Mock
    fetch = Mock(return_value=[row, {**row, "Extension": "646", "Time": "14:05:00"}])
    monkeypatch.setattr(app, "fetch_routing_conversations", fetch)
    reply = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    context = SimpleNamespace(args=[row["Phone"], "12.09.2026", "13:53:42"])
    asyncio.run(app.debugeslesme_command(update, context))
    result = reply.call_args.args[0]
    assert "Mahmut" in result
    assert "657" in result
    assert "13:47:07" in result
    assert "14:05:00" not in result
    assert fetch.call_args.args[1:] == (datetime(2026, 8, 28).date(), datetime(2026, 9, 12).date())
    assert app.phone_map_store.lookup(row["Phone"]) == "646"
    assert app.sent_store.pending_calls(today) == []
    telegram.send_message.assert_not_awaited()


def test_poll_retains_error_when_later_day_succeeds(delivery, monkeypatch):
    today, call, telegram = delivery
    app.config.last_poll_date = today - timedelta(days=2)
    monkeypatch.setattr(app.asyncio, "sleep", AsyncMock())
    failure = app.PbxError("Yönlendirme için dış arama geçmişi alınamadı")
    failure.__cause__ = RuntimeError("Toniva HTTP 429")
    process = AsyncMock(side_effect=[failure, (0, 0), (0, 0)])
    monkeypatch.setattr(app, "_process_missed_calls_for_date", process)
    context = SimpleNamespace(bot=telegram, bot_data={})
    asyncio.run(app.poll_missed_calls(context))
    assert context.bot_data["last_poll_error"] == str(failure)
    assert "RuntimeError: Toniva HTTP 429" in context.bot_data["last_poll_failure"]
    assert context.bot_data["last_poll_failure_time"]
    assert app.config.last_poll_date == today - timedelta(days=2)
    assert process.await_count == 3


def test_toniva_poll_waits_for_queue_detail_before_scanning(delivery, monkeypatch):
    today, _, telegram = delivery
    events = []
    monkeypatch.setattr(
        app.ConfigStore,
        "is_toniva",
        property(lambda self: True),
    )

    async def sleep(seconds):
        events.append(("sleep", seconds))

    async def process(*args, **kwargs):
        events.append(("scan", args[1]))
        return 0, 0

    monkeypatch.setattr(app.asyncio, "sleep", sleep)
    monkeypatch.setattr(app, "_process_missed_calls_for_date", process)
    context = SimpleNamespace(bot=telegram, bot_data={})

    asyncio.run(app.poll_missed_calls(context))

    assert events[0] == ("sleep", app.TONIVA_QUEUE_DETAIL_SETTLE_SECONDS)
    assert all(event[0] == "scan" for event in events[1:])
    assert events[1][1] == today


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