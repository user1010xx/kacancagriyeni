import re
import requests
from datetime import date, datetime, timedelta
from typing import Any

API_URL = "https://app.invekto.com/invekto/pbxreport"
REPORT_TYPE_MISS_CALL = 2
REPORT_TYPE_QUEUE = 3
REPORT_TYPE_QUEUE_DETAIL = 4
REPORT_TYPE_CONVERSATION = 5


class InvektoError(Exception):
    pass


def _parse_date(value: str) -> date:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Geçersiz tarih formatı: {value}")


def _match_department(name: str, target: str, *, loose: bool = False) -> bool:
    name_cf = name.strip().casefold()
    target_cf = target.strip().casefold()
    if not name_cf or not target_cf:
        return False
    if name_cf == target_cf:
        return True
    if not loose:
        return False
    return target_cf in name_cf or name_cf in target_cf


def _request_report(
    company_code: str,
    start_date: date,
    end_date: date,
    report_type: int,
    *,
    queue: str | None = None,
    uncompleted_only: bool = False,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "filterType": 0,
        "companyCode": company_code,
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d"),
        "reportType": report_type,
    }

    if queue:
        payload["queue"] = queue

    if report_type in (REPORT_TYPE_MISS_CALL, REPORT_TYPE_QUEUE_DETAIL):
        payload["unCompleted"] = uncompleted_only

    # Basit retry (3 deneme)
    for attempt in range(3):
        try:
            response = requests.post(API_URL, json=payload, timeout=timeout)
            response.raise_for_status()
            break
        except Exception:
            if attempt == 2:
                raise
            import time as _t
            _t.sleep(1.5 * (attempt + 1))

    body = response.json()
    if not body.get("Status"):
        message = body.get("Message") or "Invekto API isteği başarısız."
        raise InvektoError(message)

    data = body.get("Data") or []
    if not isinstance(data, list):
        raise InvektoError("Invekto API beklenmeyen veri döndürdü.")

    return data


def _is_missed_call(record: dict[str, Any]) -> bool:
    return str(record.get("Status", "")).strip() in {"2"}


def _is_uncompleted(record: dict[str, Any]) -> bool:
    value = record.get("IsCompleted")
    if isinstance(value, bool):
        return not value
    if value is None:
        return True
    return str(value).strip().lower() in {"false", "0", ""}


def _department_name(record: dict[str, Any]) -> str:
    return str(record.get("Queue") or record.get("QueueName") or "").strip()


def _normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    if "T" in text:
        text = text.split("T", 1)[0]

    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            continue

    return text


def _call_datetime(record: dict[str, Any]) -> tuple[str, str]:
    raw_date = (
        record.get("ChekInDate")
        or record.get("CreateDate")
        or record.get("Date")
        or ""
    )
    call_time = (
        record.get("ChekInTime")
        or record.get("CreateTime")
        or record.get("Time")
        or ""
    )
    return _normalize_date(raw_date), str(call_time).strip()


def parse_call_datetime(call: dict[str, Any]) -> datetime | None:
    """Parse call record to a real datetime object for reliable sorting and comparison."""
    try:
        date_str, time_str = _call_datetime(call)
        if not date_str:
            return None
        time_str = time_str or "00:00:00"
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.strptime(f"{date_str} {time_str}".strip(), fmt)
            except ValueError:
                continue
        # Last attempt with raw
        raw_date = str(call.get("ChekInDate") or call.get("CreateDate") or call.get("Date") or "").strip()
        raw_time = str(call.get("ChekInTime") or call.get("CreateTime") or call.get("Time") or "").strip()
        if raw_date and "T" in raw_date:
            raw_date = raw_date.split("T", 1)[0]
        if raw_date and raw_time:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(f"{raw_date} {raw_time}", fmt)
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def resolve_queue_number(
    company_code: str,
    start_date: date,
    end_date: date,
    department_name: str,
    *,
    loose: bool = False,
    timeout: int = 30,
) -> str | None:
    queues = _request_report(
        company_code,
        start_date,
        end_date,
        REPORT_TYPE_QUEUE,
        timeout=timeout,
    )

    for queue in queues:
        queue_name = str(queue.get("QueueName") or queue.get("Queue") or "").strip()
        if _match_department(queue_name, department_name, loose=loose):
            for key in ("QUEUE", "Queue", "Queue1", "queue"):
                value = queue.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()

    return None


def _normalize_time_hms(value: str) -> str:
    text = str(value).strip()
    if not text:
        return "00:00:00"
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return text


def _call_time_hms(call: dict[str, Any]) -> str:
    _, call_time = _call_datetime(call)
    return _normalize_time_hms(call_time or "")


def split_calls_by_time(
    calls: list[dict[str, Any]],
    after_time: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Çağrıları cutoff saatine göre (öncesi, itibaren) ikiye ayırır."""
    if not after_time:
        return [], list(calls)

    cutoff = _normalize_time_hms(after_time)
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    for call in calls:
        if _call_time_hms(call) >= cutoff:
            after.append(call)
        else:
            before.append(call)
    return before, after


def filter_calls_after_time(
    calls: list[dict[str, Any]],
    after_time: str | None,
) -> list[dict[str, Any]]:
    """Yalnızca belirtilen saatten itibaren (dahil) olan çağrıları döndürür."""
    _, after = split_calls_by_time(calls, after_time)
    return after


def _parse_department_names(
    department_names: str | list[str] | None,
) -> list[str] | None:
    if department_names is None:
        return None
    if isinstance(department_names, list):
        parsed = [name.strip() for name in department_names if str(name).strip()]
        return parsed or None
    parts = [part.strip() for part in str(department_names).split(",") if part.strip()]
    return parts or None


def filter_by_department(
    calls: list[dict[str, Any]],
    department_names: str | list[str] | None,
    *,
    loose: bool = False,
) -> list[dict[str, Any]]:
    names = _parse_department_names(department_names)
    if not names:
        return calls

    return [
        call
        for call in calls
        if any(
            _match_department(_department_name(call), name, loose=loose)
            for name in names
        )
    ]


def _fetch_from_queue_detail(
    company_code: str,
    start_date: date,
    end_date: date,
    queue_number: str,
    *,
    uncompleted_only: bool = False,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    records = _request_report(
        company_code,
        start_date,
        end_date,
        REPORT_TYPE_QUEUE_DETAIL,
        queue=queue_number,
        uncompleted_only=uncompleted_only,
        timeout=timeout,
    )

    missed_calls = [record for record in records if _is_missed_call(record)]
    if uncompleted_only:
        missed_calls = [record for record in missed_calls if _is_uncompleted(record)]

    return missed_calls


def _fetch_from_miss_call_report(
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    uncompleted_only: bool = False,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    return _request_report(
        company_code,
        start_date,
        end_date,
        REPORT_TYPE_MISS_CALL,
        uncompleted_only=uncompleted_only,
        timeout=timeout,
    )


def fetch_missed_calls(
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    department_name: str | None = None,
    department_names: list[str] | None = None,
    uncompleted_only: bool = False,
    loose_department_match: bool = False,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Kaçan çağrıları Invekto'nun kaçan çağrı raporundan (reportType 2) çeker.

    Invekto artık kaçan çağrıları departman detay raporunda (reportType 4) değil,
    ayrı kaçan çağrı raporunda sunuyor. Departman filtresi client-side uygulanır.
    """
    names = department_names or _parse_department_names(department_name)
    calls = _fetch_from_miss_call_report(
        company_code,
        start_date,
        end_date,
        uncompleted_only=uncompleted_only,
        timeout=timeout,
    )
    return filter_by_department(calls, names, loose=loose_department_match)


def call_key(call: dict[str, Any]) -> str:
    """Dedup anahtarı: telefon + tarih + saat + kuyruk (saat HH:MM:SS normalize)."""
    call_date, call_time = _call_datetime(call)
    phone = str(call.get("Phone") or "").strip()
    return "|".join(
        [
            phone,
            call_date,
            _normalize_time_hms(call_time),
            _department_name(call),
        ]
    )


def call_key_variants(call: dict[str, Any]) -> list[str]:
    """Eski ve yeni kayıt formatlarıyla uyumlu dedup anahtarları."""
    canonical = call_key(call)
    phone = str(call.get("Phone") or "").strip()
    call_date, call_time = _call_datetime(call)
    time_norm = _normalize_time_hms(call_time)
    dept = _department_name(call)

    variants = [canonical]
    legacy_id = str(call.get("ID") or call.get("CallID") or "").strip()
    if legacy_id:
        variants.append("|".join([legacy_id, phone, call_date, time_norm, dept]))
    variants.append("|".join(["", phone, call_date, time_norm, dept]))
    # Eski format: id|phone|date|time|dept (id=phone veya boş)
    if phone:
        variants.append("|".join([phone, phone, call_date, time_norm, dept]))

    seen: set[str] = set()
    ordered: list[str] = []
    for key in variants:
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def dedupe_calls_by_key(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aynı dedup anahtarına sahip kayıtları tekilleştirir."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for call in calls:
        key = call_key(call)
        if key in seen:
            continue
        seen.add(key)
        unique.append(call)
    return unique


def format_call_message(call: dict[str, Any]) -> str:
    phone = call.get("Phone") or "Bilinmiyor"
    call_date, call_time = _call_datetime(call)
    call_datetime = f"{call_date} {call_time}".strip() or "Bilinmiyor"
    department = _department_name(call) or "Bilinmiyor"

    return (
        "🔴 Kaçan Çağrı\n\n"
        f"📞 Telefon: {phone}\n"
        f"🕐 Arama Saati: {call_datetime}\n"
        f"🏷️ Departman: {department}"
    )


def get_available_queues(
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    timeout: int = 30,
) -> list[tuple[str, str]]:
    queues: dict[str, str] = {}

    for report in _request_report(
        company_code, start_date, end_date, REPORT_TYPE_QUEUE, timeout=timeout
    ):
        name = str(report.get("QueueName") or report.get("Queue") or "").strip()
        number = str(report.get("QUEUE") or "").strip()
        if name:
            queues[name] = number

    for report in _request_report(
        company_code,
        start_date,
        end_date,
        REPORT_TYPE_MISS_CALL,
        uncompleted_only=False,
        timeout=timeout,
    ):
        name = _department_name(report)
        if name and name not in queues:
            queues[name] = ""

    return sorted((name, number) for name, number in queues.items())


def parse_command_dates(text: str) -> tuple[date, date]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError("Tarih aralığı virgülle ayrılmış iki tarih olmalı.")

    start = _parse_date(parts[0])
    end = _parse_date(parts[1])
    if start > end:
        raise ValueError("Başlangıç tarihi bitiş tarihinden büyük olamaz.")

    return start, end


# ====================== YENİ: GÖRÜŞME + PERSONEL YÖNLENDİRME ======================

def _normalize_phone(phone: str) -> str:
    """Normalize telefon numarasını son 10 haneli core haline getirir.
    Örnek: 905551112233, 05551112233, 5551112233 → 5551112233
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) > 10:
        if digits.startswith("90"):
            digits = digits[2:]
        elif digits.startswith("0"):
            digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


_TR_MAP = str.maketrans(
    {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "Ç": "c",
        "Ğ": "g",
        "İ": "i",
        "I": "i",
        "Ö": "o",
        "Ş": "s",
        "Ü": "u",
    }
)


def _normalize_person_text(value: Any) -> str:
    text = str(value or "").strip().translate(_TR_MAP).casefold()
    # Invekto'da extension isimleri "selcuk -O", "selen-K" gibi gelebiliyor.
    # Soneki temizle ki "selcuk" ile "selcuk -O" eşleşsin.
    text = re.sub(r"\s*[-_]\s*[a-z]?\s*$", "", text).strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _person_tokens(value: Any) -> list[str]:
    normalized = _normalize_person_text(value)
    if not normalized:
        return []
    tokens = [t for t in normalized.split(" ") if t]
    if not tokens:
        return []
    return tokens


def _person_name_matches(left: Any, right: Any) -> bool:
    left_tokens = _person_tokens(left)
    right_tokens = _person_tokens(right)
    if not left_tokens or not right_tokens:
        return False

    left_join = " ".join(left_tokens)
    right_join = " ".join(right_tokens)
    if left_join == right_join:
        return True

    for a in left_tokens:
        for b in right_tokens:
            if a == b:
                return True
            # "elcin-k" / "elci" gibi kısmi eşleşmeler için kontrollü prefix
            if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
                return True
    return False


def _parse_conversation_datetime(date_value: Any, time_value: Any) -> datetime:
    # Normalize separators: Invekto sometimes uses / instead of .
    # Also strip ISO 8601 'T' separator (e.g. "2026-07-02T00:00:00" → "2026-07-02")
    # which is consistent with how _normalize_date handles missed-call records.
    raw_date = str(date_value or "").strip()
    if "T" in raw_date:
        raw_date = raw_date.split("T", 1)[0]
    date_str = raw_date.replace("/", ".").replace("-", ".")
    time_str = str(time_value or "").strip()
    text = f"{date_str} {time_str}".strip()

    for fmt in (
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",  # fallback for old formats
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            # Take up to 19 chars to handle possible extra millis etc.
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return datetime.min


def fetch_conversations(
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    return _request_report(
        company_code,
        start_date,
        end_date,
        REPORT_TYPE_CONVERSATION,
        timeout=timeout,
    )


def enrich_delivered_rows_with_callback_status(
    rows: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    personnel_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """İletilen çağrı satırlarına geri arama durumunu ekler.

    Kurallar (kullanıcının istediği davranış):
    - Conversation'lar iletilen tarihten raporun çekildiği ana kadar (bugün+1) aranır.
    - **Sadece iletilen notified_at zamanından sonraki** aramalar dikkate alınır.
      iletilen saati öncesi (hatta tam aynı saniye) olanlar yok sayılır.
    - Küçük sistem saat farkı için max 60 saniye tolerans.
    - Eşleşme: telefon + personel (dahili veya ExtensionName fuzzy).
    - Bulunan ilk (en erken) geri arama "Aradı - dd.mm.yyyy HH:MM:SS" olarak gösterilir.
    - Rapor çekildiği ana kadar herhangi bir zamanda arama yapılmışsa "Aradı" yazar.
    """
    personnel_rows = personnel_rows or []

    prepared: list[dict[str, Any]] = []
    for rec in conversations:
        when = _parse_conversation_datetime(
            rec.get("Date") or rec.get("ChekInDate") or rec.get("CreateDate") or "",
            rec.get("Time") or rec.get("ChekInTime") or rec.get("CreateTime") or "",
        )
        if when == datetime.min:
            continue

        # Daha esnek: Artık EventType/ Direction filtresi yapmıyoruz.
        # Çünkü bazı outgoing callback kayıtlarında EventType farklı olabiliyor.
        # Seçimi tamamen telefon + personel (extension / extension_name) + zaman > iletilen ile yapıyoruz.
        # Bu, Invekto'da görülen tüm ilgili kayıtları yakalamayı sağlar.

        phone_val = rec.get("Phone") or rec.get("phone") or rec.get("CalledNumber") or ""
        ext_val = (
            rec.get("Extension")
            or rec.get("CompletedExtension")
            or rec.get("extension")
            or rec.get("ExtensionName")
            or rec.get("dahili")
            or ""
        )
        ext_name_val = (
            rec.get("ExtensionName")
            or rec.get("CompletedExtensionName")
            or rec.get("extensionName")
            or rec.get("Name")
            or rec.get("Agent")
            or rec.get("User")
            or rec.get("Person")
            or rec.get("DisplayName")
            or rec.get("ExtensionDisplayName")
            or ""
        )

        prepared.append(
            {
                "phone": _normalize_phone(phone_val),
                "when": when,
                "extension": str(ext_val).strip(),
                "extension_name": str(ext_name_val).strip(),
                "original": rec,  # for robust name matching on any field
            }
        )

    prepared.sort(key=lambda x: x["when"])

    def _candidate_extensions(target_person: str) -> set[str]:
        candidates: set[str] = set()
        for p in personnel_rows:
            pname = p.get("personel_adi") or ""
            if _person_name_matches(pname, target_person):
                dahili = str(p.get("dahili_ad") or "").strip()
                if dahili:
                    candidates.add(dahili)
        return candidates

    enriched: list[dict[str, Any]] = []
    for row in rows:
        row_copy = dict(row)
        target_phone = _normalize_phone(row.get("phone") or "")
        target_person = str(row.get("personel_adi") or "").strip()

        notified_text = str(row.get("notified_at") or "").strip()
        notified_at: datetime | None = None
        try:
            if notified_text:
                notified_at = datetime.strptime(notified_text, "%d.%m.%Y %H:%M:%S")
        except ValueError:
            notified_at = None

        extensions = _candidate_extensions(target_person)
        first_match: datetime | None = None

        for rec in prepared:
            when = rec["when"]
            if notified_at:
                # Sadece iletilen saatinden kesin olarak sonra yapılan aramalar önemli.
                # Iletimden onceki kayitlar callback sayilmaz.
                if when <= notified_at:
                    continue

            ext = str(rec["extension"] or "").strip()
            ext_name = str(rec["extension_name"] or "").strip()

            by_extension = bool(ext and extensions and ext in extensions)
            by_name = _person_name_matches(ext_name, target_person)
            by_name_on_ext = _person_name_matches(ext, target_person)
            personnel_match = by_extension or by_name or by_name_on_ext

            # Robust: check any string field in original rec for name match (in case name is in unexpected field)
            if not personnel_match and "original" in rec:
                orig = rec["original"]
                for k, v in orig.items():
                    if isinstance(v, (str, int)) and str(v).strip():
                        if _person_name_matches(str(v), target_person):
                            personnel_match = True
                            break

            # Telefon kontrolü: ya tam eşleşme ya da conv kaydında phone yoksa
            rec_phone = rec.get("phone") or ""
            phone_match = (not target_phone) or (not rec_phone) or (rec_phone == target_phone)
            # Extra robustness: if phones normalize to same last 9 digits (in case of prefix difference)
            if not phone_match and target_phone and rec_phone:
                if target_phone[-9:] == rec_phone[-9:]:
                    phone_match = True

            if personnel_match and phone_match:
                first_match = when
                break

        if first_match:
            # Tam tarih + saat göster (farklı güne taşan callback'ler için de net olsun)
            row_copy["callback_status"] = f"Aradı - {first_match.strftime('%d.%m.%Y %H:%M:%S')}"
        else:
            row_copy["callback_status"] = "Aramadı"

        enriched.append(row_copy)

    return enriched


def _extract_dahili_from_record(rec: dict[str, Any]) -> str:
    """Kayıttan personel eşlemesi için dahili bilgisini çıkarır.

    Öncelik: dahili numarası (608) — personel deposu genelde numara anahtarlı.
    Yoksa dahili adı (selcuk). Boş string'ler atlanır.
    """
    candidates = (
        rec.get("Extension"),
        rec.get("extension"),
        rec.get("CompletedExtension"),
        rec.get("ExtensionName"),
        rec.get("extensionName"),
        rec.get("CompletedExtensionName"),
        rec.get("Dahili"),
        rec.get("dahili"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def build_phone_dahili_cache(
    company_code: str,
    days: int = 15,
    timeout: int = 30,
) -> dict[str, str]:
    """Son N günlük görüşme raporundan telefon -> son dahili eşlemesi üretir."""
    # Railway UTC kayması için bot timezone (varsayılan Europe/Istanbul)
    try:
        import os
        from zoneinfo import ZoneInfo

        tz_name = os.getenv("BOT_TIMEZONE", "Europe/Istanbul").strip() or "Europe/Istanbul"
        end_date = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        end_date = date.today()
    start_date = end_date - timedelta(days=days)

    try:
        records = _request_report(
            company_code,
            start_date,
            end_date,
            REPORT_TYPE_CONVERSATION,
            timeout=timeout,
        )
    except Exception:
        return {}

    phone_best: dict[str, tuple[datetime, str]] = {}
    for rec in records:
        phone_key = _normalize_phone(rec.get("Phone") or rec.get("phone") or "")
        dahili = _extract_dahili_from_record(rec)
        if not phone_key or not dahili:
            continue

        when = _parse_conversation_datetime(
            rec.get("Date") or rec.get("ChekInDate") or rec.get("CreateDate") or "",
            rec.get("Time") or rec.get("ChekInTime") or rec.get("CreateTime") or "",
        )
        prev = phone_best.get(phone_key)
        if prev is None or when > prev[0]:
            phone_best[phone_key] = (when, dahili)

    return {phone: dahili for phone, (_, dahili) in phone_best.items()}


def get_last_dahili_for_phone(
    company_code: str,
    phone: str,
    days: int = 15,
    timeout: int = 30,
    *,
    cache: dict[str, str] | None = None,
) -> str | None:
    """Son N günde bu numara ile ilgili en son dahiliyi döndürür."""
    phone_key = _normalize_phone(phone)
    if not phone_key:
        return None

    if cache is not None:
        return cache.get(phone_key)

    single_cache = build_phone_dahili_cache(company_code, days=days, timeout=timeout)
    return single_cache.get(phone_key)