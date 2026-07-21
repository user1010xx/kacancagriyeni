import logging
from dataclasses import dataclass
from datetime import datetime as dtm
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from invekto_client import _call_datetime, _normalize_phone, call_key, call_key_variants


def lookup_dahili_from_cache(dahili_cache: dict, phone: str) -> str | None:
    """Cache'te telefon için dahili ara (çoklu anahtar varyantı)."""
    if not dahili_cache or not phone:
        return None
    keys: list[str] = []
    core = _normalize_phone(phone)
    if core:
        keys.append(core)
    import re

    digits = re.sub(r"\D", "", str(phone))
    for candidate in (
        digits,
        digits[-10:] if len(digits) >= 10 else "",
        digits[-9:] if len(digits) >= 9 else "",
    ):
        if candidate and candidate not in keys:
            keys.append(candidate)
    # ham anahtar da (normalize edilmeden yazılmış cache'ler)
    raw = str(phone).strip()
    if raw and raw not in keys:
        keys.append(raw)
    for key in keys:
        val = dahili_cache.get(key)
        if val:
            return str(val).strip() or None
    return None

_REPORT_TZ = ZoneInfo("Europe/Istanbul")
logger = logging.getLogger(__name__)


class NotifyKind(str, Enum):
    NO_DAHILI = "no_dahili"
    NO_PERSONNEL = "no_personnel"
    PERSONNEL = "personnel"


@dataclass
class MissedCallContext:
    key: str
    phone: str
    call_time_str: str
    dahili: str | None
    personnel: dict[str, str] | None
    kind: NotifyKind
    group_notified_before: bool
    private_notified_before: bool


def build_call_time_str(call: dict[str, Any]) -> str:
    call_date, call_time = _call_datetime(call)
    return f"{call_date} {call_time}".strip() or "Bilinmiyor"


def build_missed_call_context(
    call: dict[str, Any],
    *,
    dahili_cache: dict[str, str],
    personnel_store,
    sent_store,
    phone_map_store=None,
) -> MissedCallContext | None:
    key = call_key(call)
    key_variants = call_key_variants(call)
    if sent_store.is_complete_any(key_variants):
        return None

    group_notified_before = sent_store.is_group_notified_any(key_variants)
    private_notified_before = getattr(
        sent_store,
        "is_private_notified_any",
        lambda keys: False,
    )(key_variants)
    if group_notified_before and private_notified_before:
        return None

    phone = str(call.get("Phone") or "")
    call_time_str = build_call_time_str(call)

    # 1) bellek cache  2) kalıcı phone_map  3) yoksa NO_DAHILI
    dahili = lookup_dahili_from_cache(dahili_cache, phone)
    if not dahili and phone_map_store is not None:
        try:
            dahili = phone_map_store.lookup(phone)
        except Exception:
            dahili = None

    if not dahili:
        return MissedCallContext(
            key=key,
            phone=phone,
            call_time_str=call_time_str,
            dahili=None,
            personnel=None,
            kind=NotifyKind.NO_DAHILI,
            group_notified_before=group_notified_before,
            private_notified_before=False,
        )

    personnel = personnel_store.find_for_extension(dahili)
    if not personnel:
        return MissedCallContext(
            key=key,
            phone=phone,
            call_time_str=call_time_str,
            dahili=dahili,
            personnel=None,
            kind=NotifyKind.NO_PERSONNEL,
            group_notified_before=group_notified_before,
            private_notified_before=False,
        )

    return MissedCallContext(
        key=key,
        phone=phone,
        call_time_str=call_time_str,
        dahili=dahili,
        personnel=personnel,
        kind=NotifyKind.PERSONNEL,
        group_notified_before=group_notified_before,
        private_notified_before=private_notified_before,
    )


def private_chat_id(personnel: dict[str, str]) -> int | None:
    raw = str(personnel.get("telegram_chat_id", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _format_personel_name(name: str) -> str:
    text = str(name).strip()
    if not text:
        return "Personel"

    def _cap(part: str) -> str:
        return part[:1].upper() + part[1:].lower() if part else part

    formatted: list[str] = []
    for token in text.split():
        if "-" in token:
            formatted.append("-".join(_cap(p) for p in token.split("-")))
        else:
            formatted.append(_cap(token))
    return " ".join(formatted)


def build_private_text(personel_adi: str, phone: str, call_time_str: str) -> str:
    display_name = _format_personel_name(personel_adi)
    return (
        "🔴 Kaçan Çağrı\n\n"
        f"👤 Personel: {display_name}\n"
        f"📞 Telefon: {phone}\n"
        f"🕐 Arama: {call_time_str}\n\n"
        "Üye adayımızı arar mısınız?"
    )


def build_group_text(ctx: MissedCallContext, *, private_ok: bool) -> str:
    if ctx.kind == NotifyKind.NO_DAHILI:
        return (
            "🔴 Kaçan Çağrı\n\n"
            f"📞 Telefon: {ctx.phone}\n"
            f"🕐 Arama Saati: {ctx.call_time_str}\n"
            "ℹ️ Son 15 günde eşleşen personel bulunamadı."
        )

    if ctx.kind == NotifyKind.NO_PERSONNEL:
        return (
            "🔴 Kaçan Çağrı\n\n"
            f"📞 Telefon: {ctx.phone}\n"
            f"🕐 Arama Saati: {ctx.call_time_str}\n"
            f"⚠️ Dahili {ctx.dahili} için personel kaydı bulunamadı."
        )

    personnel = ctx.personnel or {}
    personel_adi = personnel.get("personel_adi", ctx.dahili or "")
    tg_username = personnel.get("telegram_username", "")
    chat_id = private_chat_id(personnel)

    if not chat_id:
        info = f"@{tg_username} bota /start demedi (DM gönderilemedi)"
    elif private_ok:
        info = f"@{tg_username} e iletildi."
    else:
        info = f"@{tg_username} e iletilemedi!"

    return (
        "Kaçan çağrı\n"
        f"- Personel : {personel_adi}\n"
        f"- Numara : {ctx.phone}\n"
        f"- Arama saati : {ctx.call_time_str}\n"
        f"- İnfo : {info}"
    )


def should_mark_complete(ctx: MissedCallContext, *, private_ok: bool, group_ok: bool) -> bool:
    if not group_ok:
        return False
    if ctx.kind == NotifyKind.PERSONNEL:
        return private_ok
    return True


def counts_as_failed_dm(ctx: MissedCallContext, private_ok: bool) -> bool:
    return (
        ctx.kind == NotifyKind.PERSONNEL
        and not private_ok
        and not ctx.private_notified_before
    )


async def deliver_missed_call_notification(
    ctx: MissedCallContext,
    *,
    bot,
    target_chat_id: int,
) -> tuple[bool, bool, "dtm | None"]:
    """Özel ve grup bildirimini gönderir.

    Dönüş: (private_ok, group_ok, group_sent_at)
    - group_sent_at: Tam olarak grub a mesajın başarıyla gönderildiği an.
      Bu zaman hem Excel "İletilen Saat" hem de geri arama tespiti için kullanılır.
      Böylece Telegram'daki görünen iletilen zaman ile Excel birebir uyumlu olur.
    """
    private_ok = getattr(ctx, "private_notified_before", False)
    group_ok = getattr(ctx, "group_notified_before", False)
    group_sent_at: dtm | None = None
    should_send_private = ctx.kind == NotifyKind.PERSONNEL and not private_ok
    should_send_group = not group_ok

    if should_send_private:
        personnel = ctx.personnel or {}
        personel_adi = personnel.get("personel_adi", ctx.dahili or "")
        private_text = build_private_text(personel_adi, ctx.phone, ctx.call_time_str)
        chat_id = private_chat_id(personnel)
        if chat_id:
            try:
                await bot.send_message(chat_id=chat_id, text=private_text)
                private_ok = True
            except Exception as exc:
                chat_id_display = chat_id
                bot_name = getattr(bot, "username", None) or bot.__class__.__name__
                logger.warning(
                    "Telegram DM gonderimi basarisiz: bot=%s chat_id=%s phone=%s error=%s",
                    bot_name,
                    chat_id_display,
                    ctx.phone,
                    exc,
                )
                private_ok = False

    if should_send_group:
        group_text = build_group_text(ctx, private_ok=private_ok)
        try:
            await bot.send_message(chat_id=target_chat_id, text=group_text)
            group_ok = True
            group_sent_at = dtm.now(_REPORT_TZ).replace(tzinfo=None)
        except Exception as exc:
            bot_name = getattr(bot, "username", None) or bot.__class__.__name__
            logger.warning(
                "Telegram grup gonderimi basarisiz: bot=%s chat_id=%s phone=%s error=%s",
                bot_name,
                target_chat_id,
                ctx.phone,
                exc,
            )
            group_ok = False

    return private_ok, group_ok, group_sent_at