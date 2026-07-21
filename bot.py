import asyncio
import logging
import os
import time
import uuid
from datetime import date, datetime as dtm, timedelta, time as dt_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

# .env diğer yerel importlardan / ConfigStore'dan önce yüklenmeli
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config_store import ConfigStore
from delivered_store import DeliveredStore
from excel_export import export_delivered_report_excel, export_missed_calls_excel, sort_calls
from personnel_store import PersonnelStore
from pbx_provider import (
    PbxError,
    build_phone_dahili_cache,
    call_key,
    call_key_variants,
    dedupe_calls_by_key,
    enrich_delivered_rows_with_callback_status,
    fetch_conversations,
    fetch_missed_calls,
    get_available_queues,
    parse_command_date_list,
    parse_command_dates,
    split_calls_by_time,
)
from notifications import (
    NotifyKind,
    build_missed_call_context,
    counts_as_failed_dm,
    deliver_missed_call_notification,
    lookup_dahili_from_cache,
    should_mark_complete,
)
from phone_map_store import PhoneMapStore, normalize_phone_key
from sent_store import SentStore

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"
_LOG_CONFIGURED = False


def _configure_logging() -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)
    _LOG_CONFIGURED = True


_configure_logging()
logger = logging.getLogger(__name__)

config = ConfigStore(DATA_DIR / "config.json")
sent_store = SentStore(DATA_DIR / "sent_calls.json")
personnel_store = PersonnelStore(DATA_DIR / "personnels.json")
delivered_store = DeliveredStore(DATA_DIR / "delivered_calls.json", retention_hours=24)
phone_map_store = PhoneMapStore(DATA_DIR / "phone_map.json")
_MISSED_CALL_PROCESS_LOCK = asyncio.Lock()
_GONDER_RUNNING = False
_GONDER_CANCEL = False
_GONDER_TASK: asyncio.Task | None = None

# /gonder durdur | stop | iptal | cancel
_GONDER_STOP_TOKENS = frozenset(
    {
        "durdur",
        "stop",
        "iptal",
        "cancel",
        "iptal et",
        "dur",
    }
)

# Dahili cache: her poll'da PBX'e 15 günlük görüşme sorgusu atmaktan kaçınmak için
# 5 dakikalık TTL ile önbelleğe alınır. Telefon→dahili eşlemesi nadiren değişir.
_DAHILI_CACHE_TTL_SECONDS = 300
_dahili_cache: dict[str, str] = {}
_dahili_cache_built_at: "dtm | None" = None

REPORT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Istanbul"))
try:
    DAILY_REPORT_HOUR = max(0, min(int(os.getenv("DAILY_REPORT_HOUR", "10")), 23))
except ValueError:
    DAILY_REPORT_HOUR = 10

try:
    DELIVERED_RETENTION_DAYS = max(1, int(os.getenv("DELIVERED_RETENTION_DAYS", "30")))
except ValueError:
    DELIVERED_RETENTION_DAYS = 30

delivered_store.retention_hours = DELIVERED_RETENTION_DAYS * 24


def _report_today() -> date:
    """PBX rapor tarihi ile uyumlu 'bugün' (Railway UTC olsa bile TR takvimi)."""
    return dtm.now(REPORT_TZ).date()

HELP_TEXT = (
    "Merhaba! Bu bot PBX kaçan (cevapsız) çağrıları Telegram'a iletir.\n"
    "Destek: Toniva (varsayılan) ve Invekto.\n\n"
    "── Komutlar ──\n"
    "/ayar - Mevcut ayarları göster\n"
    "/firmakodu <kod> - Invekto firma kodu (yalnızca Invekto)\n"
    "/chatid - Bu grubun ID'sini göster\n"
    "/ping - Bot bağlantı testi\n"
    "/stats - Bot istatistikleri\n"
    "/temizle - Eski dedup kayıtlarını temizle\n"
    "/kuyruklar - PBX kuyruk/departman listesi\n"
    "/kacancagri 15.06.2026, 25.06.2026 - Kaçan çağrı Excel raporu\n"
    "/iletilenkacancagri 28.06.2026 - İletilen çağrı Excel raporu\n"
    "/gonder 20.07.2026,21.07.2026 - Seçili günleri gruba+DM yeniden ilet\n"
    "/gonder durdur - Çalışan /gonder işlemini durdur\n"
    "/eslestir 905352211581 585 - Telefon→dahili kalıcı eşle\n"
    "/debugeslesme 905352211581 - Eşleme teşhisi\n"
    "/personelekle 105 Ahmet @ahmet - Tek personel ekle/güncelle\n"
    "/personelsil 105 - Personel sil\n"
    "/personeller - Kayıtlı personelleri listele\n\n"
    "── Toplu personel ekleme (Excel) ──\n"
    "Bu gruba bir .xlsx dosyası gönderin.\n"
    "Sütun sırası (önemli):\n"
    "  A sütunu → Personel ismi\n"
    "  B sütunu → Dahili adı\n"
    "  C sütunu → Telegram kullanıcı adı\n"
    "Örnek satır: Ahmet Yılmaz | 105 | ahmet_yilmaz\n"
    "İlk satır başlık olabilir (otomatik atlanır).\n"
    "Var olan dahili güncellenir; DM bağlantısı korunur.\n\n"
    "── Özel mesaj (DM) ──\n"
    "Personel, bota özel sohbetten /start yazmalıdır.\n"
    "Böylece kaçan çağrı bildirimi kişiye de iletilir.\n"
)


def _require_company_code() -> str | None:
    """PBX hazır mı? Toniva'da API key, Invekto'da firma kodu gerekir."""
    return config.pbx_ready_token()


def _pbx_not_ready_message() -> str:
    if config.is_toniva:
        return (
            "Toniva API anahtarı tanımlı değil. "
            "TONIVA_API_KEY ortam değişkenini ayarlayın (Railway Variables / .env)."
        )
    return "Önce /firmakodu komutu ile firma kodunu ayarlayın."


def _record_delivered_notification(notify_ctx, group_sent_at: "dtm | None" = None) -> None:
    personnel = notify_ctx.personnel or {}
    personel_adi = personnel.get("personel_adi", notify_ctx.dahili or "")
    # "İletilen çağrı" raporu, çağrının tarihi değil iletim zamanına göre gruplanır.
    call_date = _report_today()
    # En doğru iletilen zaman: mesajın gerçekten Telegram'a gönderildiği an
    # (deliver fonksiyonundan gelen group_sent_at). Bu, Telegram görünümü ile Excel'i birebir aynı yapar.
    notified_at_local = group_sent_at or dtm.now(REPORT_TZ).replace(tzinfo=None)
    delivered_store.add(
        call_key=notify_ctx.key,
        phone=notify_ctx.phone,
        personel_adi=personel_adi,
        call_date=call_date,
        notified_at=notified_at_local,
    )


def _purge_delivered_store_by_retention_window() -> int:
    min_day = _report_today() - timedelta(days=DELIVERED_RETENTION_DAYS - 1)
    return delivered_store.purge_older_than_call_date(min_day)


def _parse_delivered_report_date(value: str) -> date:
    return dtm.strptime(value.strip(), "%d.%m.%Y").date()


def _rows_until_now(rows: list[dict], *, now: dtm | None = None) -> list[dict]:
    """Yalnızca belirtilen ana kadar iletilmiş kayıtları döndürür."""
    now_value = now or dtm.now(REPORT_TZ).replace(tzinfo=None)
    out: list[dict] = []
    for row in rows:
        notified_text = str(row.get("notified_at") or "").strip()
        if not notified_text:
            continue
        try:
            notified_at = dtm.strptime(notified_text, "%d.%m.%Y %H:%M:%S")
        except ValueError:
            continue
        if notified_at <= now_value:
            out.append(row)
    return out


async def _build_delivered_report_rows(target_date: date, rows: list[dict]) -> list[dict]:
    company_code = _require_company_code()
    if not company_code:
        reason = "Toniva API Key Yok" if config.is_toniva else "Firma Kodu Yok"
        return [
            {**r, "callback_status": f"Kontrol Edilemedi ({reason})"}
            for r in rows
        ]

    # Personele iletilen saatten (dakika:saniye dahil) sonraki aramaları 
    # rapor çekildiği ana kadar kontrol ediyoruz.
    # iletilen tarihin 1 gün öncesinden bugüne (+1) kadar conversation çekiyoruz.
    # Zenginleştirme içindeki zaman filtresi sadece iletilen sonrası olanları alır.
    conv_start = target_date - timedelta(days=1)
    conv_end = _report_today() + timedelta(days=1)
    conversations = await asyncio.to_thread(
        fetch_conversations,
        company_code,
        conv_start,
        conv_end,
    )
    return enrich_delivered_rows_with_callback_status(
        rows,
        conversations,
        personnel_store.get_all(),
    )


def _fetch_kwargs() -> dict:
    return {
        "department_names": config.department_names or None,
        "loose_department_match": config.department_loose_match,
    }


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _cutoff_time_for_date(target_date: date) -> str | None:
    """BACKFILL_DATES içindeki günler için saat filtresi (poll + backfill)."""
    raw_dates = os.getenv("BACKFILL_DATES", "").strip()
    if not raw_dates:
        return None

    after_time = os.getenv("BACKFILL_AFTER_TIME", "14:57:00").strip()
    if not after_time:
        return None

    for raw in raw_dates.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            if dtm.strptime(part, "%d.%m.%Y").date() == target_date:
                return after_time
        except ValueError:
            continue
    return None


def _apply_time_cutoff(calls: list, target_date: date, cutoff: str) -> list:
    """Cutoff öncesi çağrıları bildirmeden sent_store'a işler; sonrasını döndürür."""
    before, after = split_calls_by_time(calls, cutoff)
    skipped = 0
    for call in before:
        keys = call_key_variants(call)
        if not sent_store.is_complete_any(keys):
            sent_store.mark_complete_keys(keys, save=False)
            skipped += 1

    if skipped:
        sent_store.flush()
        logger.info(
            "%s saat filtresi <%s: %s çağrı bildirilmeden işlendi",
            target_date.isoformat(),
            cutoff,
            skipped,
        )

    logger.info(
        "%s saat filtresi >=%s: %s çağrı işlenecek",
        target_date.isoformat(),
        cutoff,
        len(after),
    )
    return after


def _allowed_chat_filter() -> filters.MessageFilter:
    class AllowedGroupFilter(filters.MessageFilter):
        def filter(self, message) -> bool:
            if message.chat.type not in ("group", "supergroup"):
                return False
            if message.chat_id != config.target_chat_id:
                logger.warning(
                    "Yetkisiz grup komutu reddedildi. gelen=%s beklenen=%s",
                    message.chat_id,
                    config.target_chat_id,
                )
                return False
            return True

    return AllowedGroupFilter()


def _update_bot_data(context: ContextTypes.DEFAULT_TYPE, **fields) -> None:
    if context.bot_data is None:
        return
    context.bot_data.update(fields)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    text = update.effective_message.text if update.effective_message else "-"
    logger.info(
        "Gelen update: chat_id=%s chat_type=%s text=%s",
        chat.id,
        chat.type,
        text,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def private_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "Telegram kullanıcı adınız (username) tanımlı olmalı. "
            "Ayarlar > Kullanıcı adı bölümünden ekleyin."
        )
        return

    linked = personnel_store.link_chat_id_by_username(user.username, update.effective_chat.id)
    if linked:
        await update.message.reply_text(
            "✅ Bağlantı kuruldu\n"
            "Artık kaçan çağrılar sizlere iletilecektir."
        )
    else:
        await update.message.reply_text(
            f"⚠️ @{user.username} için personel kaydı bulunamadı."
        )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    allowed = chat.id == config.target_chat_id
    await update.message.reply_text(
        f"pong\nchat_id={chat.id}\nbeklenen={config.target_chat_id}\n"
        f"yetkili_grup={'evet' if allowed else 'hayir'}"
    )


async def ayar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(config.as_text())


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    company_code = _require_company_code()
    bot_data = context.bot_data or {}
    today = _report_today()
    yesterday = today - timedelta(days=1)
    delivered_today = len(delivered_store.get_by_call_date(today))
    delivered_yesterday = len(delivered_store.get_by_call_date(yesterday))

    provider_label = "Toniva" if config.is_toniva else "Invekto"
    auth_line = (
        f"🔌 Provider: {provider_label}\n"
        if config.is_toniva
        else f"🏢 Firma Kodu: {company_code or 'Ayarlanmadı'}\n"
    )
    text = (
        "📊 Bot İstatistikleri\n\n"
        f"{auth_line}"
        f"🏷️ Departman: {config.department_name or 'Tümü'}\n"
        f"📦 Tamamlanan (dedup) çağrı: {sent_store.count()}\n"
        f"⏳ DM bekleyen (grup gönderildi): {sent_store.group_notified_count()}\n"
        f"📨 Grup bekleyen (DM gönderildi): {sent_store.private_notified_count()}\n"
        f"📁 İletilen kayıt (bugün): {delivered_today}\n"
        f"📁 İletilen kayıt (dün): {delivered_yesterday}\n"
        f"👥 Kayıtlı personel: {personnel_store.count()}\n"
        f"⏱️ Polling aralığı: {config.polling_interval_seconds} sn\n"
        f"📨 Bildirim filtresi: {'Sadece tamamlanmamış' if config.notify_uncompleted_only else 'Tümü'}\n"
        f"🕒 Son poll (bu oturum): {bot_data.get('last_poll_count', '-')}\n"
        f"🕒 Son poll zamanı: {bot_data.get('last_poll_time', '-')}\n"
        f"⚡ Son API süresi: {bot_data.get('last_api_duration_ms', '-')} ms\n"
        f"❌ Son poll hatası: {bot_data.get('last_poll_error', '-')}\n"
        f"📭 Başarısız DM (bu oturum): {bot_data.get('failed_dm_count', 0)}\n"
    )
    await update.message.reply_text(text)


async def temizle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    removed = sent_store.purge_old()
    await update.message.reply_text(f"✅ {removed} eski dedup kaydı temizlendi.")


async def personelekle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.message.reply_text(
            "Tek personel:\n"
            "/personelekle 105 Ahmet @ahmet_yilmaz\n"
            "veya /personelekle 105 \"Ahmet Yılmaz\" @ahmet_yilmaz\n\n"
            "Toplu ekleme: gruba .xlsx gönderin\n"
            "A=Personel ismi | B=Dahili adı | C=Telegram kullanıcı adı\n"
            "Detay: /help"
        )
        return

    dahili = context.args[0].strip()
    username = context.args[-1].strip()
    ad = " ".join(context.args[1:-1]).strip().strip('"').strip("'")

    if not dahili or not ad:
        await update.message.reply_text("Dahili ve personel adı boş olamaz.")
        return

    if personnel_store.add_or_update(dahili, ad, username):
        await update.message.reply_text(
            f"✅ Personel eklendi/güncellendi: {ad} (Dahili: {dahili})\n"
            "Personel bota DM'den /start yazarak özel mesaj almayı aktifleştirmeli."
        )
    else:
        await update.message.reply_text("Personel eklenemedi.")


async def personelsil_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Kullanım: /personelsil 105")
        return

    dahili = context.args[0].strip()
    if personnel_store.remove(dahili):
        await update.message.reply_text(f"✅ Personel silindi: {dahili}")
    else:
        await update.message.reply_text("Böyle bir personel bulunamadı.")


async def personeller_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = personnel_store.get_all()
    if not items:
        await update.message.reply_text(
            "Kayıtlı personel yok.\n\n"
            "Tek ekle: /personelekle 105 Ahmet @ahmet\n"
            "Toplu ekle: gruba .xlsx gönder\n"
            "  A=Personel ismi | B=Dahili adı | C=Telegram kullanıcı adı\n"
            "Detay: /start veya /help"
        )
        return

    lines = ["📋 Kayıtlı Personeller\n"]
    for p in items:
        dm = "✅ DM hazır" if p["dm_ready"] else "⚠️ /start bekliyor"
        lines.append(
            f"• {p['dahili_ad']} - {p['personel_adi']} - @{p['telegram_username']} ({dm})"
        )
    await update.message.reply_text("\n".join(lines))


async def personel_excel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc or not doc.file_name:
        return

    name_lower = doc.file_name.lower()
    if name_lower.endswith((".xls", ".csv")):
        await update.message.reply_text(
            "Lütfen .xlsx formatında gönderin (Excel → Farklı Kaydet → .xlsx).\n\n"
            "Sütun sırası:\n"
            "• A: Personel ismi\n"
            "• B: Dahili adı\n"
            "• C: Telegram kullanıcı adı"
        )
        return
    if not name_lower.endswith((".xlsx", ".xlsm")):
        # Diğer dosya türlerine karışma
        return

    await update.message.reply_text("Excel işleniyor, lütfen bekleyin...")

    file = await context.bot.get_file(doc.file_id)
    temp_path = DATA_DIR / "temp_personel_upload.xlsx"
    await file.download_to_drive(temp_path)

    try:
        before = personnel_store.count()
        count = personnel_store.load_from_excel(temp_path)
        after = personnel_store.count()
        if count == 0:
            await update.message.reply_text(
                "⚠️ Excel'den personel okunamadı.\n\n"
                "Kontrol edin:\n"
                "• A sütunu: Personel ismi\n"
                "• B sütunu: Dahili adı\n"
                "• C sütunu: Telegram kullanıcı adı\n"
                "• İlk satır başlık olabilir (otomatik atlanır)\n"
                "• Dosya .xlsx olmalı"
            )
            return

        await update.message.reply_text(
            f"✅ Toplu personel Excel işlendi.\n"
            f"• İşlenen satır: {count}\n"
            f"• Önceki toplam: {before}\n"
            f"• Şimdiki toplam: {after}\n\n"
            "Personeller özel mesaj için bota DM'den /start yazmalıdır.\n"
            "Liste: /personeller"
        )
    except Exception as e:
        logger.exception("Personel Excel işlenemedi")
        await update.message.reply_text(f"❌ Excel işlenirken hata oluştu: {e}")
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    allowed = chat.id == config.target_chat_id
    await update.message.reply_text(
        f"Sohbet ID: {chat.id}\n"
        f"Bu ID şu anda TELEGRAM_GROUP_CHAT_ID olarak kullanılıyor.\n"
        f"Durum: {'Bu grup yetkili' if allowed else 'Bu grup yetkili degil'}"
    )


async def firmakodu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.is_toniva:
        await update.message.reply_text(
            "Bu bot Toniva provider ile çalışıyor. Firma kodu gerekmez.\n"
            "Kimlik doğrulama: TONIVA_API_KEY ortam değişkeni.\n"
            "Kuyruk filtresi: TONIVA_QUEUE (örn. 1000)."
        )
        return

    if not context.args:
        await update.message.reply_text("Kullanım: /firmakodu 12345678")
        return

    code = context.args[0].strip()
    if not code.isdigit() or len(code) != 8:
        await update.message.reply_text("Firma kodu 8 haneli sayı olmalıdır.")
        return

    config.company_code = code
    await update.message.reply_text(f"✅ Firma kodu ayarlandı: {code}")


async def kuyruklar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    company_code = _require_company_code()
    if not company_code:
        await update.message.reply_text(_pbx_not_ready_message())
        return

    today = _report_today()
    try:
        queues = await asyncio.to_thread(
            get_available_queues,
            company_code,
            today,
            today,
        )
    except PbxError as exc:
        await update.message.reply_text(f"PBX hatası: {exc}")
        return
    except Exception as exc:
        logger.exception("Kuyruk listesi alınamadı")
        await update.message.reply_text(f"Kuyruk listesi alınamadı: {exc}")
        return

    if not queues:
        await update.message.reply_text("PBX'ten kuyruk listesi alınamadı.")
        return

    lines = ["📋 Kuyruk/Departman Adları\n"]
    for name, number in queues:
        if number:
            lines.append(f"• {name} (no: {number})")
        else:
            lines.append(f"• {name}")

    await update.message.reply_text("\n".join(lines))


async def kacancagri_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    company_code = _require_company_code()
    if not company_code:
        await update.message.reply_text(_pbx_not_ready_message())
        return

    if not context.args:
        await update.message.reply_text("Kullanım: /kacancagri 15.06.2026, 25.06.2026")
        return

    raw_dates = " ".join(context.args)
    try:
        start_date, end_date = parse_command_dates(raw_dates)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await update.message.reply_text("Kaçan çağrılar sorgulanıyor, lütfen bekleyin...")

    try:
        calls = await asyncio.to_thread(
            fetch_missed_calls,
            company_code,
            start_date,
            end_date,
            uncompleted_only=False,
            **_fetch_kwargs(),
        )
    except PbxError as exc:
        await update.message.reply_text(f"PBX hatası: {exc}")
        return
    except Exception as exc:
        logger.exception("Kaçan çağrı sorgusu başarısız")
        await update.message.reply_text(f"Sorgu sırasında hata oluştu: {exc}")
        return

    if not calls:
        message = "Belirtilen aralıkta kaçan çağrı bulunamadı."
        if config.department_name:
            try:
                queues = await asyncio.to_thread(
                    get_available_queues,
                    company_code,
                    start_date,
                    end_date,
                )
                if queues:
                    names = ", ".join(name for name, _ in queues[:8])
                    message += (
                        f"\n\n⚠️ Ayarlı kuyruk/departman: {config.department_name}\n"
                        f"PBX kuyruk adları: {names}\n\n"
                        "Doğru adı görmek için /kuyruklar komutunu kullanın."
                    )
            except Exception:
                pass
        await update.message.reply_text(message)
        return

    calls = sort_calls(calls)
    _uid = uuid.uuid4().hex[:8]
    filename = (
        f"kacancagri_{start_date.strftime('%d.%m.%Y')}_"
        f"{end_date.strftime('%d.%m.%Y')}_{_uid}.xlsx"
    )
    export_path = DATA_DIR / "exports" / filename

    try:
        await asyncio.to_thread(export_missed_calls_excel, calls, export_path)
        await update.message.reply_text(
            f"📋 Kaçan Çağrılar ({start_date.strftime('%d.%m.%Y')} - "
            f"{end_date.strftime('%d.%m.%Y')})\n"
            f"Toplam: {len(calls)}\n"
            "Excel dosyası hazırlanıyor..."
        )
        with export_path.open("rb") as excel_file:
            await update.message.reply_document(
                document=excel_file,
                filename=filename,
                caption=f"Toplam {len(calls)} kaçan çağrı",
            )
    except Exception as exc:
        logger.exception("Excel oluşturulamadı")
        await update.message.reply_text(f"Excel dosyası oluşturulamadı: {exc}")
    finally:
        if export_path.exists():
            export_path.unlink()


def _is_gonder_stop_request(args: list[str] | None) -> bool:
    """ /gonder durdur | stop | iptal | cancel """
    if not args:
        return False
    joined = " ".join(str(a).strip() for a in args).strip().casefold()
    if joined in _GONDER_STOP_TOKENS:
        return True
    # tek token: /gonder durdur
    if len(args) == 1 and str(args[0]).strip().casefold() in _GONDER_STOP_TOKENS:
        return True
    return False


def _request_gonder_cancel() -> str:
    """Çalışan /gonder için iptal iste. Dönüş: kullanıcıya mesaj."""
    global _GONDER_CANCEL
    if not _GONDER_RUNNING:
        return "ℹ️ Şu an çalışan bir /gonder işlemi yok."
    _GONDER_CANCEL = True
    logger.info("/gonder durdur istendi.")
    return "🛑 Durdurma isteği alındı. Mevcut çağrı bittikten sonra /gonder duracak..."


def _gonder_should_stop() -> bool:
    return _GONDER_CANCEL


async def _run_gonder_job(
    bot,
    chat_id: int,
    target_dates: list,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Arka planda /gonder işi — handler hemen döner ki /gonder durdur işlenebilsin."""
    global _GONDER_RUNNING, _GONDER_CANCEL, _dahili_cache, _dahili_cache_built_at

    date_labels = ", ".join(d.strftime("%d.%m.%Y") for d in target_dates)
    cancelled = False
    cleared_dedup = 0
    cleared_delivered = 0
    total_sent = 0
    total_failed_dm = 0
    day_lines: list[str] = []

    try:
        _dahili_cache = {}
        _dahili_cache_built_at = None

        date_set = set(target_dates)
        cleared_dedup = sent_store.unmark_for_dates(date_set)
        cleared_delivered = delivered_store.remove_by_call_key_dates(date_set)
        logger.info(
            "/gonder dedup temizlendi: dates=%s removed_dedup=%s removed_delivered=%s",
            date_labels,
            cleared_dedup,
            cleared_delivered,
        )

        if _gonder_should_stop():
            cancelled = True
            await bot.send_message(
                chat_id=chat_id,
                text="🛑 /gonder durduruldu (dedup temizliği sonrası, iletim başlamadan).",
            )
            return

        try:
            throttle = float(os.getenv("BACKFILL_THROTTLE_SECONDS", "0.15"))
        except ValueError:
            throttle = 0.15
        throttle = max(0.0, throttle)

        for target in target_dates:
            if _gonder_should_stop():
                cancelled = True
                day_lines.append(
                    f"• {target.strftime('%d.%m.%Y')}: atlandı (durduruldu)"
                )
                break

            label = target.strftime("%d.%m.%Y")
            try:
                sent_now, failed_dm = await _process_missed_calls_for_date(
                    bot,
                    target,
                    context=context,
                    throttle_seconds=throttle,
                    should_cancel=_gonder_should_stop,
                )
            except Exception as exc:
                logger.exception("/gonder gün işlenemedi: %s", label)
                day_lines.append(f"• {label}: hata — {exc}")
                continue

            total_sent += sent_now
            total_failed_dm += failed_dm
            if _gonder_should_stop():
                cancelled = True
                day_lines.append(
                    f"• {label}: kısmi tamamlanan={sent_now}, "
                    f"başarısız_dm={failed_dm} (durduruldu)"
                )
                break

            day_lines.append(
                f"• {label}: tamamlanan={sent_now}, başarısız_dm={failed_dm}"
            )
            logger.info(
                "/gonder gün bitti: %s sent=%s failed_dm=%s",
                label,
                sent_now,
                failed_dm,
            )

        if cancelled:
            summary = (
                f"🛑 Yeniden iletim DURDURULDU\n"
                f"Günler: {date_labels}\n"
                f"Temizlenen dedup: {cleared_dedup}\n"
                f"Temizlenen iletilen kayıt: {cleared_delivered}\n"
                f"Durana kadar tamamlanan bildirim: {total_sent}\n"
                f"Başarısız DM: {total_failed_dm}\n\n"
                + ("\n".join(day_lines) if day_lines else "• henüz gün işlenmedi")
            )
        else:
            summary = (
                f"✅ Yeniden iletim bitti\n"
                f"Günler: {date_labels}\n"
                f"Temizlenen dedup: {cleared_dedup}\n"
                f"Temizlenen iletilen kayıt: {cleared_delivered}\n"
                f"Toplam tamamlanan bildirim: {total_sent}\n"
                f"Başarısız DM: {total_failed_dm}\n\n"
                + "\n".join(day_lines)
            )
        await bot.send_message(chat_id=chat_id, text=summary)
    except Exception:
        logger.exception("/gonder arka plan işi çöktü")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="❌ /gonder beklenmeyen hata ile durdu. Logları kontrol edin.",
            )
        except Exception:
            pass
    finally:
        _GONDER_RUNNING = False
        _GONDER_CANCEL = False


async def gonder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Seçili günlerin kaçan çağrılarını dedup temizleyip sırayla yeniden iletir.

    Kullanım:
      /gonder 20.07.2026,21.07.2026
      /gonder durdur

    ÖNEMLİ: İş arka planda çalışır; handler hemen döner.
    Böylece /gonder durdur aynı anda işlenebilir (PTB sıralı update kilidi kırılır).
    """
    global _GONDER_RUNNING, _GONDER_CANCEL, _GONDER_TASK

    # --- Durdur (önce ve hızlı) ---
    if _is_gonder_stop_request(context.args):
        await update.message.reply_text(_request_gonder_cancel())
        return

    company_code = _require_company_code()
    if not company_code:
        await update.message.reply_text(_pbx_not_ready_message())
        return

    if not context.args:
        await update.message.reply_text(
            "Kullanım:\n"
            "• /gonder 20.07.2026,21.07.2026 — seçili günleri yeniden ilet\n"
            "• /gonder durdur — çalışan iletimi durdur\n\n"
            "Belirtilen günlerin kaçan çağrıları gruba ve personele sırayla yeniden iletilir.\n"
            "⚠️ Aynı çağrılar için daha önce giden mesajlar varsa grupta çift bildirim olabilir."
        )
        return

    try:
        target_dates = parse_command_date_list(" ".join(context.args))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    today = _report_today()
    future = [d for d in target_dates if d > today]
    if future:
        await update.message.reply_text(
            "İleri tarih gönderilemez: "
            + ", ".join(d.strftime("%d.%m.%Y") for d in future)
        )
        return

    if _GONDER_RUNNING:
        await update.message.reply_text(
            "⏳ /gonder zaten çalışıyor.\n"
            "Durdurmak için: /gonder durdur"
        )
        return

    _GONDER_RUNNING = True
    _GONDER_CANCEL = False
    date_labels = ", ".join(d.strftime("%d.%m.%Y") for d in target_dates)
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"📤 Yeniden iletim başlıyor (arka plan)\n"
        f"Günler: {date_labels}\n"
        f"Önce dedup temizlenir, ardından çağrılar sırayla gruba ve personele gönderilir.\n"
        f"⛔ Durdurmak için hemen yazın: /gonder durdur\n"
        f"Bu işlem birkaç dakika sürebilir..."
    )

    # Handler hemen bitsin → /gonder durdur sıraya girmeden işlensin
    _GONDER_TASK = context.application.create_task(
        _run_gonder_job(
            context.bot,
            chat_id,
            target_dates,
            context=context,
        ),
        update=update,
    )


async def eslestir_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manuel telefon → dahili kalıcı eşleme.

    Kullanım: /eslestir 905352211581 585
             /eslestir 905352211581 selen
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım: /eslestir <telefon> <dahili_no_veya_ad>\n"
            "Örnek: /eslestir 905352211581 585\n"
            "Örnek: /eslestir 905352211581 selen\n\n"
            "Bu kayıt kalıcıdır (phone_map.json). UI CDR'da görünen ama "
            "API'den gelmeyen dış aramalar için kullanın."
        )
        return

    phone = context.args[0].strip()
    dahili = " ".join(context.args[1:]).strip()
    if not normalize_phone_key(phone):
        await update.message.reply_text("Geçersiz telefon numarası.")
        return
    if not dahili:
        await update.message.reply_text("Dahili boş olamaz.")
        return

    personnel = personnel_store.find_for_extension(dahili)
    phone_map_store.set(phone, dahili)
    # Bellek cache'i de güncelle
    global _dahili_cache, _dahili_cache_built_at
    pk = normalize_phone_key(phone)
    _dahili_cache[pk] = dahili

    if personnel:
        ad = personnel.get("personel_adi", dahili)
        await update.message.reply_text(
            f"✅ Eşleme kaydedildi\n"
            f"📞 {phone} → {dahili} ({ad})\n"
            f"Personel kaydı bulundu; sonraki kaçan çağrılarda DM+grup gidecek."
        )
    else:
        await update.message.reply_text(
            f"⚠️ Eşleme kaydedildi: {phone} → {dahili}\n"
            f"Ancak personel listesinde '{dahili}' bulunamadı.\n"
            f"/personelekle ile ekleyin veya dahili numarasını kontrol edin."
        )


async def debugeslesme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telefon eşleme teşhisi: cache, kalıcı map, API satır özeti."""
    if not context.args:
        await update.message.reply_text("Kullanım: /debugeslesme 905352211581")
        return

    phone = context.args[0].strip()
    pk = normalize_phone_key(phone)
    mem = lookup_dahili_from_cache(_dahili_cache, phone)
    persistent = phone_map_store.lookup(phone)
    personnel_mem = (
        personnel_store.find_for_extension(mem) if mem else None
    )
    personnel_pers = (
        personnel_store.find_for_extension(persistent) if persistent else None
    )

    lines = [
        "🔎 Eşleme teşhisi\n",
        f"Telefon: {phone}",
        f"Normalize: {pk or '(boş)'}",
        f"Bellek cache: {mem or 'YOK'} (cache boyutu={len(_dahili_cache)})",
        f"Kalıcı map: {persistent or 'YOK'} (map boyutu={phone_map_store.count()})",
        f"Personel(bellek): {personnel_mem.get('personel_adi') if personnel_mem else 'YOK'}",
        f"Personel(kalıcı): {personnel_pers.get('personel_adi') if personnel_pers else 'YOK'}",
    ]

    company_code = _require_company_code()
    if not company_code:
        lines.append(f"\nPBX: {_pbx_not_ready_message()}")
        await update.message.reply_text("\n".join(lines))
        return

    await update.message.reply_text("API sorgulanıyor (son 3 gün)...")
    try:
        end = _report_today()
        start = end - timedelta(days=3)
        rows = await asyncio.to_thread(fetch_conversations, company_code, start, end)
        hits = [
            r
            for r in rows
            if normalize_phone_key(str(r.get("Phone") or r.get("phone") or "")) == pk
        ]
        lines.append(f"\nAPI conversations ({start}…{end}): {len(rows)} satır")
        lines.append(f"Bu telefon eşleşen: {len(hits)}")
        if hits:
            sample = hits[-1]
            lines.append(
                f"Son kayıt: Ext={sample.get('Extension')!r} "
                f"Name={sample.get('ExtensionName')!r} "
                f"Date={sample.get('Date') or sample.get('ChekInDate')!r} "
                f"Time={sample.get('Time') or sample.get('ChekInTime')!r}"
            )
        elif rows:
            sample = rows[0]
            lines.append(f"Örnek satır alanları: {list(sample.keys())[:15]}")
        else:
            lines.append(
                "⚠️ API 0 satır döndü — UI CDR public API'de yok olabilir. "
                "Manuel: /eslestir <tel> <dahili>"
            )
    except Exception as exc:
        lines.append(f"\nAPI hata: {exc}")

    lines.append(
        "\nManuel eşle: /eslestir "
        f"{phone} <dahili>"
    )
    await update.message.reply_text("\n".join(lines))


async def iletilenkacancagri_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Kullanım: /iletilenkacancagri 28.06.2026")
        return

    raw_date = context.args[0].strip()
    try:
        target_date = _parse_delivered_report_date(raw_date)
    except ValueError:
        await update.message.reply_text("Geçersiz tarih. Format: /iletilenkacancagri 28.06.2026")
        return

    today = _report_today()
    if target_date > today:
        await update.message.reply_text(
            "Gelecek tarih için rapor alınamaz."
        )
        return

    _purge_delivered_store_by_retention_window()

    rows = delivered_store.get_by_call_date(target_date)
    now_local = dtm.now(REPORT_TZ).replace(tzinfo=None)
    if target_date == today:
        rows = _rows_until_now(rows, now=now_local)

    if not rows:
        if target_date == today:
            await update.message.reply_text(
                f"📊 {today.strftime('%d.%m.%Y')} {now_local.strftime('%H:%M')} itibarıyla "
                "personele iletilen kaçan çağrı bulunamadı."
            )
        else:
            await update.message.reply_text(
                f"📊 {target_date.strftime('%d.%m.%Y')} tarihinde personele iletilen kaçan çağrı bulunamadı."
            )
        return

    await update.message.reply_text("İletilen çağrı raporu hazırlanıyor, lütfen bekleyin...")

    filename = f"iletilen_kacancagri_{target_date.strftime('%d-%m-%Y')}_{uuid.uuid4().hex[:8]}.xlsx"
    export_path = DATA_DIR / "exports" / filename

    try:
        report_rows = await _build_delivered_report_rows(target_date, rows)
        await asyncio.to_thread(export_delivered_report_excel, report_rows, export_path)

        if target_date == today:
            caption = (
                "📊 Personele İletilen Kaçan Çağrılar\n"
                f"Tarih: {today.strftime('%d.%m.%Y')}\n"
                f"Saat: {now_local.strftime('%H:%M')} itibarıyla\n"
                f"Toplam: {len(rows)}"
            )
        else:
            caption = (
                "📊 Personele İletilen Kaçan Çağrılar\n"
                f"Tarih: {target_date.strftime('%d.%m.%Y')}\n"
                f"Toplam: {len(rows)}"
            )

        with export_path.open("rb") as excel_file:
            await update.message.reply_document(
                document=excel_file,
                filename=filename,
                caption=caption,
            )
    except Exception as exc:
        logger.exception("/iletilenkacancagri raporu oluşturulamadı")
        await update.message.reply_text(f"Rapor oluşturulamadı: {exc}")
    finally:
        if export_path.exists():
            export_path.unlink(missing_ok=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Beklenmeyen hata: %s", context.error)


async def _process_missed_calls_for_date(
    bot,
    target_date: date,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    throttle_seconds: float = 0.0,
    after_time: str | None = None,
    should_cancel=None,
) -> tuple[int, int]:
    """Belirli bir günün kaçan çağrılarını işler. (bildirilen_sayı, başarısız_dm)

    should_cancel: isteğe bağlı callable() -> bool; True olursa döngü kırılır
    (/gonder durdur için).
    """
    async with _MISSED_CALL_PROCESS_LOCK:
        company_code = _require_company_code()
        if not company_code:
            return 0, 0

        if should_cancel and should_cancel():
            return 0, 0

        sent_now = 0
        failed_dm = 0
        api_started = time.monotonic()

        try:
            calls = await asyncio.to_thread(
                fetch_missed_calls,
                company_code,
                target_date,
                target_date,
                uncompleted_only=config.notify_uncompleted_only,
                **_fetch_kwargs(),
            )
            api_ms = int((time.monotonic() - api_started) * 1000)
            if context is not None:
                _update_bot_data(
                    context,
                    last_api_duration_ms=api_ms,
                    last_poll_error="-",
                )
        except Exception as exc:
            logger.warning(
                "Kaçan çağrı kontrolü başarısız (%s): %s",
                target_date.isoformat(),
                exc,
            )
            if context is not None:
                _update_bot_data(context, last_poll_error=str(exc))
            return 0, 0

        if should_cancel and should_cancel():
            return 0, 0

        calls = dedupe_calls_by_key(calls)

        cutoff = after_time or _cutoff_time_for_date(target_date)
        if cutoff:
            calls = _apply_time_cutoff(calls, target_date, cutoff)

        global _dahili_cache, _dahili_cache_built_at
        now_mono = dtm.now()
        if (
            _dahili_cache_built_at is None
            or (now_mono - _dahili_cache_built_at).total_seconds() > _DAHILI_CACHE_TTL_SECONDS
        ):
            if should_cancel and should_cancel():
                return 0, 0
            _dahili_cache = await asyncio.to_thread(build_phone_dahili_cache, company_code, 15)
            _dahili_cache_built_at = now_mono
            # API'den gelen eşlemeleri kalıcı depoya yaz
            try:
                merged = phone_map_store.merge(_dahili_cache)
                if merged:
                    logger.info("phone_map kalıcı depo güncellendi: +%s kayıt", merged)
            except Exception as exc:
                logger.warning("phone_map merge başarısız: %s", exc)
        dahili_cache = _dahili_cache

        for call in calls:
            if should_cancel and should_cancel():
                logger.info(
                    "Kaçan çağrı işleme iptal edildi (%s), şimdiye kadar sent=%s",
                    target_date.isoformat(),
                    sent_now,
                )
                break

            notify_ctx = build_missed_call_context(
                call,
                dahili_cache=dahili_cache,
                personnel_store=personnel_store,
                sent_store=sent_store,
                phone_map_store=phone_map_store,
            )
            if notify_ctx is None:
                continue

            key_variants = call_key_variants(call)

            private_ok, group_ok, group_sent_at = await deliver_missed_call_notification(
                notify_ctx,
                bot=bot,
                target_chat_id=config.target_chat_id,
            )

            if counts_as_failed_dm(notify_ctx, private_ok):
                failed_dm += 1

            if group_ok and not notify_ctx.group_notified_before:
                sent_store.mark_group_notified_keys(key_variants, save=True)
                # Sadece gerçek personel eşleşmesi olan iletimleri "personele iletilen" kaydına al.
                # Böylece Excel raporunda personel adı boş veya "bilinmiyor" satırlar olmaz.
                if notify_ctx.kind == NotifyKind.PERSONNEL:
                    _record_delivered_notification(notify_ctx, group_sent_at=group_sent_at)

            if (
                notify_ctx.kind == NotifyKind.PERSONNEL
                and private_ok
                and not notify_ctx.private_notified_before
            ):
                sent_store.mark_private_notified_keys(key_variants, save=True)

            if should_mark_complete(notify_ctx, private_ok=private_ok, group_ok=group_ok):
                sent_store.mark_complete_keys(key_variants, save=True)
                sent_now += 1

            if throttle_seconds > 0:
                await asyncio.sleep(throttle_seconds)

        return sent_now, failed_dm


async def poll_missed_calls(context: ContextTypes.DEFAULT_TYPE) -> None:
    _purge_delivered_store_by_retention_window()

    sent_now, failed_dm = await _process_missed_calls_for_date(
        context.bot,
        _report_today(),
        context=context,
    )

    _update_bot_data(
        context,
        last_poll_count=sent_now,
        last_poll_time=dtm.now(REPORT_TZ).strftime("%d.%m.%Y %H:%M:%S"),
        failed_dm_count=context.bot_data.get("failed_dm_count", 0) + failed_dm
        if context.bot_data
        else failed_dm,
    )


async def _seed_today_missed_calls_if_needed() -> int:
    """İlk kurulum / boş dedup deposunda bugünün çağrılarını bildirimsiz işaretle.

    Flood önleme: sent_store tamamen boşsa (veya SEED_TODAY_ON_STARTUP zorlanırsa)
    bugünkü kaçanları complete sayar; Telegram'a göndermez.
    Volume doluysa (count > 0) varsayılan olarak atlanır.
    """
    if not _env_flag("SEED_TODAY_ON_STARTUP", default=True):
        logger.info("SEED_TODAY_ON_STARTUP=false; bugün seed atlandı.")
        return 0

    company_code = _require_company_code()
    if not company_code:
        return 0

    force = _env_flag("SEED_TODAY_FORCE", default=False)
    if sent_store.count() > 0 and not force:
        logger.info(
            "Dedup deposu dolu (%s kayıt); bugün seed atlandı. "
            "Zorlamak için SEED_TODAY_FORCE=true.",
            sent_store.count(),
        )
        return 0

    today = _report_today()
    try:
        calls = await asyncio.to_thread(
            fetch_missed_calls,
            company_code,
            today,
            today,
            uncompleted_only=False,
            **_fetch_kwargs(),
        )
    except Exception as exc:
        logger.warning("Bugün seed sorgusu başarısız: %s", exc)
        return 0

    calls = dedupe_calls_by_key(calls)
    seeded = 0
    for call in calls:
        keys = call_key_variants(call)
        if not sent_store.is_complete_any(keys):
            sent_store.mark_complete_keys(keys, save=False)
            seeded += 1
    if seeded:
        sent_store.flush()
    logger.info(
        "Bugün seed tamamlandı: %s çağrı bildirimsiz işlendi (tarih=%s).",
        seeded,
        today.isoformat(),
    )
    return seeded


async def _backfill_missed_calls(application: Application) -> None:
    """Deploy sonrası yalnızca BACKFILL_DATES ile belirtilen günleri işler."""
    if not _env_flag("BACKFILL_ON_STARTUP", default=True):
        return

    company_code = _require_company_code()
    if not company_code:
        return

    raw_dates = os.getenv("BACKFILL_DATES", "").strip()
    if not raw_dates:
        logger.info("BACKFILL_DATES boş; startup backfill atlandı.")
        return

    after_time = os.getenv("BACKFILL_AFTER_TIME", "14:57:00").strip() or None
    throttle = float(os.getenv("BACKFILL_THROTTLE_SECONDS", "0.15"))
    seen: set[str] = set()

    for raw in raw_dates.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            target = dtm.strptime(part, "%d.%m.%Y").date()
        except ValueError:
            logger.warning("BACKFILL_DATES geçersiz tarih atlandı: %s", part)
            continue

        job_key = config.backfill_job_key(target, after_time)
        if job_key in seen or config.is_backfilled(target, after_time):
            continue
        seen.add(job_key)

        logger.info(
            "%s için backfill başlıyor (saat >= %s)...",
            target.isoformat(),
            after_time or "00:00:00",
        )
        sent_now, failed_dm = await _process_missed_calls_for_date(
            application.bot,
            target,
            throttle_seconds=throttle,
            after_time=after_time,
        )
        config.mark_backfilled(target, after_time)
        logger.info(
            "Backfill tamamlandı: %s | bildirim=%s | başarısız_dm=%s",
            job_key,
            sent_now,
            failed_dm,
        )


async def purge_old_sent_calls(context: ContextTypes.DEFAULT_TYPE) -> None:
    removed = sent_store.purge_old()
    if removed:
        logger.info("Periyodik temizlik: %s eski dedup kaydı silindi.", removed)


async def send_daily_delivered_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Her sabah 10:00'da bir önceki gün personele iletilen kaçan çağrıları Excel olarak gönderir."""
    if not config.target_chat_id:
        return

    _purge_delivered_store_by_retention_window()
    today = _report_today()
    target_date = today - timedelta(days=1)
    rows = delivered_store.get_by_call_date(target_date)

    if not rows:
        await context.bot.send_message(
            chat_id=config.target_chat_id,
            text=(
                f"📊 {target_date.strftime('%d.%m.%Y')} tarihinde personele iletilen kaçan çağrı bulunamadı."
            ),
        )
        return

    filename = f"iletilen_kacancagri_{target_date.strftime('%d-%m-%Y')}_{uuid.uuid4().hex[:8]}.xlsx"
    export_path = DATA_DIR / "exports" / filename

    try:
        report_rows = await _build_delivered_report_rows(target_date, rows)

        await asyncio.to_thread(export_delivered_report_excel, report_rows, export_path)
        caption = (
            f"📊 Personele İletilen Kaçan Çağrılar (Önceki Gün)\n"
            f"Tarih: {target_date.strftime('%d.%m.%Y')}\n"
            f"Toplam: {len(rows)}"
        )
        with export_path.open("rb") as excel_file:
            await context.bot.send_document(
                chat_id=config.target_chat_id,
                document=excel_file,
                filename=filename,
                caption=caption,
            )
        logger.info(
            "Günlük iletilen rapor gönderildi: %s (%s kayıt).",
            target_date.isoformat(),
            len(rows),
        )
    except Exception as exc:
        logger.exception("Günlük iletilen raporu gönderilemedi")
        await context.bot.send_message(
            chat_id=config.target_chat_id,
            text=f"Günlük iletilen çağrı raporu oluşturulamadı: {exc}",
        )
    finally:
        if export_path.exists():
            export_path.unlink(missing_ok=True)


async def post_init(application: Application) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    await application.bot.delete_webhook(drop_pending_updates=True)
    me = await application.bot.get_me()
    logger.info("Bot aktif: @%s", me.username)
    logger.info("PBX provider: %s", config.pbx_provider)
    logger.info("Yetkili grup ID: %s", config.target_chat_id)
    logger.info("İzlenen kuyruk/departmanlar: %s", config.department_name or "Tümü")
    logger.info("DATA_DIR: %s", DATA_DIR)

    try:
        await _seed_today_missed_calls_if_needed()
    except Exception as exc:
        logger.warning("Bugün seed işlemi başarısız: %s", exc)

    try:
        await _backfill_missed_calls(application)
    except Exception as exc:
        logger.warning("Backfill işlemi başarısız: %s", exc)


def main() -> None:
    missing = config.validate()
    if missing:
        raise SystemExit(f"Eksik veya hatalı ortam değişkenleri: {', '.join(missing)}")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed = _allowed_chat_filter()
    group_only = filters.ChatType.GROUPS
    private_only = filters.ChatType.PRIVATE

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        # /gonder çalışırken /gonder durdur'un işlenebilmesi için zorunlu
        .concurrent_updates(True)
        .build()
    )

    application.bot_data.setdefault("failed_dm_count", 0)

    application.add_handler(TypeHandler(Update, log_update), group=-1)
    application.add_handler(CommandHandler("start", private_start_command, filters=private_only))
    application.add_handler(CommandHandler("ping", ping_command, filters=group_only))
    application.add_handler(CommandHandler("chatid", chatid_command, filters=group_only))
    application.add_handler(CommandHandler("start", start_command, filters=allowed))
    application.add_handler(CommandHandler("help", start_command, filters=allowed))
    application.add_handler(CommandHandler("ayar", ayar_command, filters=allowed))
    application.add_handler(CommandHandler("stats", stats_command, filters=allowed))
    application.add_handler(CommandHandler("temizle", temizle_command, filters=allowed))
    application.add_handler(CommandHandler("firmakodu", firmakodu_command, filters=allowed))
    application.add_handler(CommandHandler("kuyruklar", kuyruklar_command, filters=allowed))
    application.add_handler(CommandHandler("kacancagri", kacancagri_command, filters=allowed))
    application.add_handler(CommandHandler("iletilenkacancagri", iletilenkacancagri_command, filters=allowed))
    application.add_handler(CommandHandler("gonder", gonder_command, filters=allowed))
    application.add_handler(CommandHandler("eslestir", eslestir_command, filters=allowed))
    application.add_handler(CommandHandler("debugeslesme", debugeslesme_command, filters=allowed))
    application.add_handler(CommandHandler("personelekle", personelekle_command, filters=allowed))
    application.add_handler(CommandHandler("personelsil", personelsil_command, filters=allowed))
    application.add_handler(CommandHandler("personeller", personeller_command, filters=allowed))
    application.add_handler(MessageHandler(filters.Document.ALL & allowed, personel_excel_handler))

    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(
        poll_missed_calls,
        interval=config.polling_interval_seconds,
        first=5,
        name="missed-call-poller",
    )
    application.job_queue.run_daily(
        purge_old_sent_calls,
        time=dt_time(hour=3, minute=0, tzinfo=REPORT_TZ),
        name="sent-store-purge",
    )
    application.job_queue.run_daily(
        send_daily_delivered_report,
        time=dt_time(hour=DAILY_REPORT_HOUR, minute=0, tzinfo=REPORT_TZ),
        name="daily-delivered-report",
    )

    logger.info("Polling başlıyor...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()