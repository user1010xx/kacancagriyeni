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
    assert should_mark_complete(ctx_other, private_ok=False, group_ok=True)


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