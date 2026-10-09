import pytest

from notifications import (
    NotifyKind,
    build_group_text,
    build_missed_call_context,
    build_private_text,
    deliver_missed_call_notification,
    private_chat_id,
    should_mark_complete,
)
from personnel_store import PersonnelStore


class _FakeSentStore:
    def __init__(self):
        self.completed = set()
        self.group_notified = set()
        self.private_notified = set()

    def is_complete(self, key: str) -> bool:
        return key in self.completed

    def is_complete_any(self, keys: list[str]) -> bool:
        return any(key in self.completed for key in keys)

    def is_group_notified(self, key: str) -> bool:
        return key in self.group_notified

    def is_group_notified_any(self, keys: list[str]) -> bool:
        return any(key in self.group_notified for key in keys)

    def is_private_notified_any(self, keys: list[str]) -> bool:
        return any(key in self.private_notified for key in keys)


def test_routing_uses_outbound_before_missed_call_not_later_callback(tmp_path):
    from notifications import build_outbound_history
    from phone_map_store import PhoneMapStore

    personnel = PersonnelStore(tmp_path / "people.json")
    personnel.add_or_update("657", "Mahmut", "mahmut")
    personnel.add_or_update("646", "Elisa", "elisa")
    mapping = PhoneMapStore(tmp_path / "map.json")
    mapping.set("905309644795", "646")
    history = build_outbound_history([
        {"Phone": "905309644795", "Extension": "646", "Direction": "outbound",
         "Date": "12.09.2026", "Time": "14:05:00"},
        {"Phone": "905309644795", "Extension": "657", "Direction": "outbound",
         "Date": "12.09.2026", "Time": "13:47:07", "Duration": 0},
        {"Phone": "905309644795", "Extension": "646", "Direction": "inbound",
         "Date": "12.09.2026", "Time": "13:50:00"},
    ])
    call = {"Phone": "905309644795", "ChekInDate": "2026-09-12",
            "ChekInTime": "13:53:42", "Queue": "1000"}
    ctx = build_missed_call_context(
        call, dahili_cache={"5309644795": "646"}, personnel_store=personnel,
        sent_store=_FakeSentStore(), phone_map_store=mapping, outbound_history=history,
    )
    assert ctx.dahili == "657"
    assert ctx.personnel["personel_adi"] == "Mahmut"


def test_routing_without_prior_outbound_does_not_use_stale_mapping(tmp_path):
    from notifications import build_outbound_history
    from phone_map_store import PhoneMapStore

    personnel = PersonnelStore(tmp_path / "people.json")
    personnel.add_or_update("646", "Elisa", "elisa")
    mapping = PhoneMapStore(tmp_path / "map.json")
    mapping.set("905309644795", "646")
    call = {"Phone": "905309644795", "ChekInDate": "2026-09-12",
            "ChekInTime": "13:53:42", "Queue": "1000"}
    ctx = build_missed_call_context(
        call, dahili_cache={"5309644795": "646"}, personnel_store=personnel,
        sent_store=_FakeSentStore(), phone_map_store=mapping,
        outbound_history=build_outbound_history([]),
    )
    assert ctx.kind == NotifyKind.NO_DAHILI
    assert ctx.personnel is None


@pytest.mark.parametrize("changes", [
    {"Time": "13:53:42"},
    {"Time": "14:00:00"},
    {"Date": "27.08.2026"},
    {"Date": "invalid"},
    {"Direction": "inbound"},
    {"Direction": ""},
    {"Phone": "905309644794"},
    {"Extension": "905309644795"},
])
def test_routing_rejects_unverified_or_out_of_window_records(changes):
    from notifications import build_outbound_history, lookup_outbound_before_call

    row = {"Phone": "905309644795", "Extension": "657", "Direction": "outbound",
           "Date": "12.09.2026", "Time": "13:47:07"}
    row.update(changes)
    call = {"Phone": "905309644795", "ChekInDate": "2026-09-12", "ChekInTime": "13:53:42"}
    assert lookup_outbound_before_call(call, build_outbound_history([row])) is None


def test_routing_conflicting_extensions_at_same_time_are_ambiguous():
    from notifications import build_outbound_history, lookup_outbound_before_call

    row = {"Phone": "905309644795", "Extension": "657", "Direction": "outbound",
           "Date": "12.09.2026", "Time": "13:47:07"}
    call = {"Phone": row["Phone"], "ChekInDate": row["Date"], "ChekInTime": "13:53:42"}
    assert lookup_outbound_before_call(call, build_outbound_history([row, row])) == "657"
    assert lookup_outbound_before_call(call, build_outbound_history([
        row, {**row, "Extension": "646"},
    ])) is None


def test_routing_each_missed_call_uses_its_own_time():
    from notifications import build_outbound_history, lookup_outbound_before_call

    row = {"Phone": "905309644795", "Extension": "657", "Direction": "outbound",
           "Date": "12.09.2026", "Time": "13:47:07"}
    history = build_outbound_history([row, {**row, "Extension": "646", "Time": "14:05:00"}])
    call = {"Phone": row["Phone"], "ChekInDate": row["Date"], "ChekInTime": "13:53:42"}
    assert lookup_outbound_before_call(call, history) == "657"
    assert lookup_outbound_before_call({**call, "ChekInTime": "14:10:00"}, history) == "646"
    assert lookup_outbound_before_call({**call, "ChekInTime": "invalid"}, history) is None


def test_partial_history_trusts_rows_after_missing_days_only():
    from datetime import date
    from notifications import build_outbound_history, lookup_outbound_before_call
    from pbx_provider import RoutingConversationRows

    cdr = {
        "Phone": "905307642914",
        "Extension": "665",
        "Direction": "outbound",
        "Date": "09.10.2026",
        "Time": "16:25:25",
    }
    call = {
        "Phone": "5307642914",
        "ChekInDate": "09.10.2026",
        "ChekInTime": "16:25:59",
    }
    complete_after_gap = RoutingConversationRows(
        [cdr],
        incomplete_dates={date(2026, 10, 6)},
    )
    incomplete_on_match_day = RoutingConversationRows(
        [cdr],
        incomplete_dates={date(2026, 10, 9)},
    )

    assert lookup_outbound_before_call(
        call,
        build_outbound_history(complete_after_gap),
    ) == "665"
    assert lookup_outbound_before_call(
        call,
        build_outbound_history(incomplete_on_match_day),
    ) is None


def test_incomplete_routing_history_is_not_reported_as_no_personnel_match(tmp_path):
    from datetime import date
    from notifications import build_outbound_history
    from pbx_provider import RoutingConversationRows

    call = {
        "Phone": "5307642914",
        "ChekInDate": "09.10.2026",
        "ChekInTime": "16:25:59",
    }
    history = build_outbound_history(
        RoutingConversationRows([], incomplete_dates={date(2026, 10, 9)})
    )
    context = build_missed_call_context(
        call,
        dahili_cache={},
        personnel_store=PersonnelStore(tmp_path / "people.json"),
        sent_store=_FakeSentStore(),
        outbound_history=history,
    )

    assert context is not None
    assert context.routing_history_incomplete
    message = build_group_text(context, private_ok=False)
    assert "eksik CDR günü" in message
    assert "Son 15 günde eşleşen personel bulunamadı" not in message


def test_build_private_text_format():
    msg = build_private_text("seda", "905301718596", "27.06.2026 11:02:13")
    assert "🔴 Kaçan Çağrı" in msg
    assert "👤 Personel: Seda" in msg
    assert "📞 Telefon: 905301718596" in msg
    assert "🕐 Arama: 27.06.2026 11:02:13" in msg
    assert "Üye adayımızı arar mısınız?" in msg
    assert "aram mısın" not in msg


def test_build_context_matches_personnel_by_name(tmp_path):
    sent = _FakeSentStore()
    personnel = PersonnelStore(tmp_path / "p.json")
    personnel.add_or_update("105", "selen-K", "selen_test")

    call = {
        "ID": "57519",
        "Phone": "905425889653",
        "ChekInDate": "2026-06-27",
        "ChekInTime": "09:58:32",
        "Queue": "Gelen Arama",
        "Status": "2",
    }
    ctx = build_missed_call_context(
        call,
        dahili_cache={"5425889653": "selen"},
        personnel_store=personnel,
        sent_store=sent,
    )
    assert ctx is not None
    assert ctx.kind == NotifyKind.PERSONNEL
    assert ctx.personnel is not None


def test_build_context_no_dahili(tmp_path):
    sent = _FakeSentStore()
    personnel = PersonnelStore(tmp_path / "p.json")
    call = {
        "ID": "1",
        "Phone": "905551112233",
        "ChekInDate": "2026-06-26",
        "ChekInTime": "18:35:00",
        "Queue": "Gelen Arama",
        "Status": "2",
    }
    ctx = build_missed_call_context(
        call,
        dahili_cache={},
        personnel_store=personnel,
        sent_store=sent,
    )
    assert ctx is not None
    assert ctx.kind == NotifyKind.NO_DAHILI


def test_build_context_uses_phone_map_store(tmp_path):
    from phone_map_store import PhoneMapStore

    sent = _FakeSentStore()
    personnel = PersonnelStore(tmp_path / "p.json")
    personnel.add_or_update("585", "Selen", "selen_tg")
    pmap = PhoneMapStore(tmp_path / "phone_map.json")
    pmap.set("905352211581", "585")
    call = {
        "ID": "2",
        "Phone": "905352211581",
        "ChekInDate": "2026-07-20",
        "ChekInTime": "18:58:17",
        "Queue": "1000",
        "Status": "Cevapsız",
    }
    ctx = build_missed_call_context(
        call,
        dahili_cache={},  # bellek boş
        personnel_store=personnel,
        sent_store=sent,
        phone_map_store=pmap,
    )
    assert ctx is not None
    assert ctx.kind == NotifyKind.PERSONNEL
    assert ctx.dahili == "585"
    assert ctx.personnel is not None
    assert ctx.personnel.get("personel_adi") == "Selen"


def test_should_mark_complete_rules():
    ctx_personnel = type("C", (), {"kind": NotifyKind.PERSONNEL})()
    ctx_other = type("C", (), {"kind": NotifyKind.NO_DAHILI})()

    assert should_mark_complete(ctx_personnel, private_ok=True, group_ok=True)
    assert not should_mark_complete(ctx_personnel, private_ok=False, group_ok=True)
    assert not should_mark_complete(ctx_other, private_ok=False, group_ok=True)


def test_private_chat_id():
    assert private_chat_id({"telegram_chat_id": "123"}) == 123
    assert private_chat_id({"telegram_chat_id": ""}) is None


def test_group_text_dm_not_ready():
    ctx = type(
        "C",
        (),
        {
            "kind": NotifyKind.PERSONNEL,
            "phone": "905551112233",
            "call_time_str": "26.06.2026 18:35",
            "dahili": "105",
            "personnel": {
                "personel_adi": "Ali",
                "telegram_username": "ali",
                "telegram_chat_id": "",
            },
        },
    )()
    text = build_group_text(ctx, private_ok=False)
    assert "/start" in text


import asyncio


def test_deliver_missed_call_notification_returns_three_values():
    """Kritik regression: fonksiyon (private_ok, group_ok, group_sent_at) döndürmeli."""

    class _FakeBot:
        async def send_message(self, **kwargs):
            pass

    ctx = type(
        "C",
        (),
        {
            "kind": NotifyKind.NO_DAHILI,
            "phone": "905551112233",
            "call_time_str": "02.07.2026 10:00:00",
            "dahili": None,
            "personnel": None,
            "group_notified_before": False,
            "private_notified_before": False,
        },
    )()

    async def _run():
        return await deliver_missed_call_notification(ctx, bot=_FakeBot(), target_chat_id=123)

    result = asyncio.run(_run())
    assert len(result) == 3, "deliver_missed_call_notification 3 değer döndürmeli: (private_ok, group_ok, group_sent_at)"
    private_ok, group_ok, group_sent_at = result
    assert isinstance(private_ok, bool)
    assert isinstance(group_ok, bool)


def test_deliver_group_only_when_private_already_sent():
    class _FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)

    bot = _FakeBot()
    ctx = type(
        "C",
        (),
        {
            "kind": NotifyKind.PERSONNEL,
            "phone": "905551112233",
            "call_time_str": "02.07.2026 10:00:00",
            "dahili": "105",
            "personnel": {
                "personel_adi": "Ali",
                "telegram_username": "ali",
                "telegram_chat_id": "123",
            },
            "group_notified_before": False,
            "private_notified_before": True,
        },
    )()

    private_ok, group_ok, _ = asyncio.run(
        deliver_missed_call_notification(ctx, bot=bot, target_chat_id=999)
    )

    assert private_ok is True
    assert group_ok is True
    assert len(bot.calls) == 1
    assert bot.calls[0]["chat_id"] == 999