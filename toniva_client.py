"""Toniva Public API client — kaçan çağrılar queue-detail + Cevapsız filtresi ile."""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from invekto_client import (
    _department_name,
    _extract_dahili_from_record,
    _normalize_date,
    _normalize_phone,
    _parse_conversation_datetime,
    _parse_department_names,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://crm.toniva.net/api/public/v1"
DEFAULT_MISSED_STATUS = "Cevapsız"
# Bilinen kod / İngilizce etiketler (env hedeflerine ek)
_BUILTIN_MISSED_STATUSES = frozenset(
    {
        "2",
        "missed",
        "unanswered",
        "no-answer",
        "no_answer",
        "noanswer",
        "cevapsiz",
        "cevapsız",
    }
)


class TonivaError(Exception):
    pass


def _report_tz() -> ZoneInfo:
    name = os.getenv("BOT_TIMEZONE", "Europe/Istanbul").strip() or "Europe/Istanbul"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Istanbul")


def _report_today() -> date:
    """Railway UTC olsa bile bot takvimi (varsayılan Europe/Istanbul)."""
    return datetime.now(_report_tz()).date()


def _base_url() -> str:
    return os.getenv("TONIVA_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")


def _api_key() -> str:
    return os.getenv("TONIVA_API_KEY", "").strip()


def _timeout_seconds(default: int = 30) -> int:
    try:
        return max(5, int(os.getenv("TONIVA_TIMEOUT_SECONDS", str(default))))
    except ValueError:
        return default


def _keep_raw() -> bool:
    raw = os.getenv("TONIVA_KEEP_RAW", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _missed_status_targets() -> set[str]:
    raw = os.getenv("TONIVA_MISSED_STATUS", DEFAULT_MISSED_STATUS).strip()
    if not raw:
        raw = DEFAULT_MISSED_STATUS
    # Virgülle birden fazla etiket: Cevapsız,Missed,Unanswered
    targets = {part.strip().casefold() for part in raw.split(",") if part.strip()}
    # TR 'ı' / ASCII 'i' varyasyonları
    expanded: set[str] = set()
    for t in targets:
        expanded.add(t)
        expanded.add(t.replace("ı", "i").replace("İ", "i"))
        expanded.add(_ascii_fold(t))
    return expanded


def _ascii_fold(value: str) -> str:
    text = str(value or "").strip().casefold().replace("\u0307", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.translate(
        str.maketrans(
            {
                "ç": "c",
                "ğ": "g",
                "ı": "i",
                "ö": "o",
                "ş": "s",
                "ü": "u",
            }
        )
    )


def _auth_headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise TonivaError("TONIVA_API_KEY tanımlı değil.")
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


def _key_slug(value: str) -> str:
    """Alan adı karşılaştırması: boşluk/underscore yok, TR harfler sade."""
    return re.sub(r"[\s_\-]+", "", _ascii_fold(value))


def _field(record: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return str(record[key]).strip()
    # case-insensitive + TR-slug fallback (KUYRUK ADI, TARİH, DURUM, ...)
    lower_map = {str(k).casefold(): v for k, v in record.items()}
    slug_map = {_key_slug(k): v for k, v in record.items()}
    for key in keys:
        val = lower_map.get(key.casefold())
        if val not in (None, ""):
            return str(val).strip()
        val = slug_map.get(_key_slug(key))
        if val not in (None, ""):
            return str(val).strip()
    return default


def _extract_rows(body: Any) -> list[dict[str, Any]]:
    """Toniva yanıtından satır listesini çıkarır (rows / data / Data / list)."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]

    if not isinstance(body, dict):
        raise TonivaError("Toniva API beklenmeyen yanıt tipi döndürdü.")

    # Öncelik: bilinen anahtarlar (yanlış liste seçimini azaltır)
    for key in ("rows", "data", "Data", "items", "result", "records"):
        value = body.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            for inner in ("rows", "data", "items", "records"):
                nested = value.get(inner)
                if isinstance(nested, list):
                    return [r for r in nested if isinstance(r, dict)]

    # Son çare: meta dışındaki ilk dict-listesi
    skip_keys = {"meta", "Meta", "error", "errors", "message", "Message"}
    for key, value in body.items():
        if key in skip_keys:
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            logger.debug("Toniva satırlar '%s' alanından okundu (fallback).", key)
            return [r for r in value if isinstance(r, dict)]

    return []


def _extract_meta(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        meta = body.get("meta") or body.get("Meta") or {}
        if isinstance(meta, dict):
            return meta
    return {}


def _request_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> Any:
    url = f"{_base_url()}{path}"
    timeout = timeout if timeout is not None else _timeout_seconds()
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            response = requests.request(
                method,
                url,
                headers=_auth_headers(),
                params=params,
                timeout=timeout,
            )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 1.5 * (attempt + 1)
                logger.warning("Toniva rate limit (429), %.1fs bekleniyor...", wait)
                time.sleep(max(wait, 0.5))
                continue

            if response.status_code >= 400:
                message = response.text[:300]
                try:
                    err_body = response.json()
                    if isinstance(err_body, dict):
                        message = (
                            err_body.get("message")
                            or err_body.get("Message")
                            or err_body.get("code")
                            or message
                        )
                except Exception:
                    pass
                raise TonivaError(f"Toniva HTTP {response.status_code}: {message}")

            if not response.content:
                return {}
            return response.json()
        except TonivaError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(1.5 * (attempt + 1))

    raise TonivaError(f"Toniva API isteği başarısız: {last_error}")


def fetch_report(
    slug: str,
    start_date: date,
    end_date: date,
    *,
    queue: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    timeout: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params: dict[str, Any] = {
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d"),
    }
    if queue:
        params["queue"] = queue
    if page_size is not None:
        params["pageSize"] = page_size
    if page is not None:
        params["page"] = page

    body = _request_json(
        "GET",
        f"/reports/{slug}",
        params=params,
        timeout=timeout,
    )
    return _extract_rows(body), _extract_meta(body)


def _parse_turkish_long_date(value: str) -> str:
    """'Cumartesi 18 Temmuz 2026' → dd.mm.yyyy; başarısızsa boş."""
    months = {
        "ocak": 1,
        "şubat": 2,
        "subat": 2,
        "mart": 3,
        "nisan": 4,
        "mayıs": 5,
        "mayis": 5,
        "haziran": 6,
        "temmuz": 7,
        "ağustos": 8,
        "agustos": 8,
        "eylül": 9,
        "eylul": 9,
        "ekim": 10,
        "kasım": 11,
        "kasim": 11,
        "aralık": 12,
        "aralik": 12,
    }
    parts = value.strip().split()
    # [weekday] day month year  veya  day month year
    if len(parts) >= 3:
        try:
            if parts[0].isdigit():
                day_s, month_s, year_s = parts[0], parts[1], parts[2]
            else:
                day_s, month_s, year_s = parts[1], parts[2], parts[3]
            day = int(day_s)
            year = int(year_s)
            month = months.get(month_s.casefold())
            if month:
                return date(year, month, day).strftime("%d.%m.%Y")
        except (ValueError, IndexError):
            pass
    return ""


def _normalize_call_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    # ISO datetime: yalnızca tarih kısmını al (T ayırıcı). "Temmuz" içindeki T'ye dokunma.
    if "T" in text and len(text) >= 10 and text[4] == "-" and text[7] == "-":
        text = text.split("T", 1)[0]

    # Standart formatlar
    for candidate in (text, text[:10]):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(candidate.replace("/", "."), fmt.replace("/", ".")).strftime(
                    "%d.%m.%Y"
                )
            except ValueError:
                continue

    # Türkçe uzun tarih (UI: "Cumartesi 18 Temmuz 2026")
    long_form = _parse_turkish_long_date(text)
    if long_form:
        return long_form

    # Son çare: Invekto normalizer (yalnızca net tarih stringleri için güvenli)
    try:
        return _normalize_date(text)
    except Exception:
        return text


def _normalize_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text and " " not in text:
        # datetime ISO içinde saat
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.strftime("%H:%M:%S")
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return text


def normalize_queue_detail_row(record: dict[str, Any]) -> dict[str, Any]:
    """Toniva kuyruk detay satırını botun beklediği Invekto-benzeri forma çevirir."""
    phone = _field(
        record,
        "Phone",
        "phone",
        "Caller",
        "caller",
        "CallerNumber",
        "phoneNumber",
        "Telefon",
        "telefon",
    )
    queue = _field(
        record,
        "Queue",
        "QueueName",
        "queue",
        "queueName",
        "queue_name",
        "Kuyruk",
        "Kuyruk Adi",
        "Kuyruk Adı",
        "kuyrukAdi",
        "kuyruk_adi",
    )
    call_date_raw = _field(
        record,
        "ChekInDate",
        "CreateDate",
        "Date",
        "date",
        "callDate",
        "Tarih",
        "tarih",
        "TARİH",
    )
    call_time_raw = _field(
        record,
        "ChekInTime",
        "CreateTime",
        "Time",
        "time",
        "callTime",
        "Saat",
        "saat",
        "SAAT",
    )
    # Tek datetime alanında birleşik gelebilir
    if not call_time_raw and call_date_raw and "T" in call_date_raw:
        call_time_raw = call_date_raw

    status = _field(
        record,
        "Status",
        "status",
        "Durum",
        "durum",
        "callStatus",
        "state",
    )
    extension = _field(
        record,
        "Extension",
        "extension",
        "Dahili",
        "dahili",
        "CompletedExtension",
    )
    extension_name = _field(
        record,
        "ExtensionName",
        "extensionName",
        "DahiliAdi",
        "dahiliAdi",
        "dahili_adi",
        "Agent",
        "agent",
        "CompletedExtensionName",
    )
    trunk = _field(record, "Trunk", "trunk", "Hat", "hat", "Line", "line")
    call_duration = _field(
        record,
        "CallTime",
        "callTime",
        "CallDuration",
        "talkTime",
        "GorusmeSuresi",
        "görüşme süresi",
    )
    ring_duration = _field(
        record,
        "RingTime",
        "ringTime",
        "RingDuration",
        "CaldırmaSuresi",
        "çaldırma süresi",
    )
    call_id = _field(record, "ID", "Id", "id", "CallID", "callId", "call_id")

    call_date = _normalize_call_date(call_date_raw)
    call_time = _normalize_time(call_time_raw)
    # "1000 (1000)" → kanonik kuyruk adı tercihen parantez dışı / numara
    queue_canonical = _canonical_queue_label(queue)

    normalized = {
        "ID": call_id,
        "Phone": phone,
        "Queue": queue_canonical or queue,
        "QueueName": queue_canonical or queue,
        "ChekInDate": call_date,
        "CreateDate": call_date,
        "ChekInTime": call_time,
        "CreateTime": call_time,
        "Status": status,
        "Extension": extension,
        "ExtensionName": extension_name,
        "Trunk": trunk,
        "CallTime": call_duration,
        "RingTime": ring_duration,
        "IsCompleted": record.get("IsCompleted", record.get("isCompleted")),
        "_source": "toniva",
        "_queue_raw": queue,
    }
    if _keep_raw():
        normalized["_raw"] = record
    return normalized


def normalize_conversation_row(record: dict[str, Any]) -> dict[str, Any]:
    """Görüşme/CDR satırını botun beklediği forma çevirir.

    queue-detail ile aynı Türkçe UI alanlarını da destekler
    (TELEFON, DAHİLİ ADI, DAHİLİ NUMARASI, TARİH, SAAT, …).
    Aksi halde telefon→dahili cache boş kalır ve tüm kaçan çağrılar
    'personel bulunamadı' diye düşer.
    """
    phone = _field(
        record,
        "Phone",
        "phone",
        "Caller",
        "caller",
        "CallerNumber",
        "phoneNumber",
        "CalledNumber",
        "calledNumber",
        "Destination",
        "destination",
        "Telefon",
        "telefon",
        "TELEFON",
    )
    call_date_raw = _field(
        record,
        "Date",
        "date",
        "ChekInDate",
        "CreateDate",
        "callDate",
        "Tarih",
        "tarih",
        "TARİH",
    )
    call_time_raw = _field(
        record,
        "Time",
        "time",
        "ChekInTime",
        "CreateTime",
        "callTime",
        "Saat",
        "saat",
        "SAAT",
    )
    # Tek datetime alanında birleşik gelebilir
    if not call_time_raw and call_date_raw and "T" in str(call_date_raw):
        call_time_raw = call_date_raw

    # Dahili numarası (608) — UI: DAHİLİ NUMARASI
    extension = _field(
        record,
        "Extension",
        "extension",
        "CompletedExtension",
        "Dahili",
        "dahili",
        "DahiliNumarasi",
        "Dahili Numarası",
        "DAHİLİ NUMARASI",
        "extensionNumber",
        "ExtensionNumber",
    )
    # Dahili adı (selcuk) — UI: DAHİLİ ADI
    extension_name = _field(
        record,
        "ExtensionName",
        "extensionName",
        "CompletedExtensionName",
        "Agent",
        "agent",
        "Name",
        "DahiliAdi",
        "dahiliAdi",
        "dahili_adi",
        "Dahili Adı",
        "DAHİLİ ADI",
        "User",
        "DisplayName",
    )

    call_date = _normalize_call_date(call_date_raw)
    call_time = _normalize_time(call_time_raw)

    return {
        "Phone": phone,
        "phone": phone,
        "Date": call_date or call_date_raw,
        "Time": call_time or call_time_raw,
        "ChekInDate": call_date or call_date_raw,
        "CreateDate": call_date or call_date_raw,
        "ChekInTime": call_time or call_time_raw,
        "CreateTime": call_time or call_time_raw,
        "Extension": extension,
        "ExtensionName": extension_name,
        "CompletedExtension": extension,
        "CompletedExtensionName": extension_name,
        "_source": "toniva",
    }


def _canonical_queue_label(value: str) -> str:
    """'1000 (1000)' → '1000'; düz ad olduğu gibi."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", text)
    if match:
        outer = match.group(1).strip()
        inner = match.group(2).strip()
        # UI kalıbı: numara (numara) veya ad (numara)
        if outer == inner:
            return outer
        # Tercih: kısa numara parçası (digit ağırlıklı)
        if inner.isdigit() and not outer.isdigit():
            return inner
        return outer
    return text


def _queue_aliases(value: str) -> set[str]:
    """Kuyruk adı karşılaştırma seti: '1000', '1000 (1000)' uyumu."""
    text = str(value or "").strip()
    if not text:
        return set()
    aliases = {text.casefold(), _ascii_fold(text), _key_slug(text)}
    canonical = _canonical_queue_label(text)
    if canonical:
        aliases.add(canonical.casefold())
        aliases.add(_ascii_fold(canonical))
        aliases.add(_key_slug(canonical))
    match = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", text)
    if match:
        for part in (match.group(1).strip(), match.group(2).strip()):
            if part:
                aliases.add(part.casefold())
                aliases.add(_ascii_fold(part))
                aliases.add(_key_slug(part))
    return {a for a in aliases if a}


def queues_match(left: str, right: str, *, loose: bool = False) -> bool:
    left_aliases = _queue_aliases(left)
    right_aliases = _queue_aliases(right)
    if not left_aliases or not right_aliases:
        return False
    if left_aliases & right_aliases:
        return True
    if not loose:
        return False
    # Gevşek: bir alias diğerinin alt dizesi (min 2 karakter)
    for a in left_aliases:
        for b in right_aliases:
            if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
                return True
    return False


def filter_by_queues(
    calls: list[dict[str, Any]],
    department_names: list[str] | None,
    *,
    loose: bool = False,
) -> list[dict[str, Any]]:
    """Toniva kuyruk filtresi — '1000' ile '1000 (1000)' eşleşir."""
    names = [n.strip() for n in (department_names or []) if str(n).strip()]
    if not names:
        return calls
    out: list[dict[str, Any]] = []
    for call in calls:
        queue = _department_name(call) or str(call.get("_queue_raw") or "")
        if any(queues_match(queue, name, loose=loose) for name in names):
            out.append(call)
    return out


def is_missed_status(record: dict[str, Any]) -> bool:
    """Normalize edilmiş veya ham satırda cevapsız mı?

    Önce tam eşleşme; kısa substring ile yanlış pozitif (ör. 'Cevap') engellenir.
    """
    status_raw = _field(record, "Status", "status", "Durum", "durum", "callStatus")
    if not status_raw:
        return False

    status_cf = status_raw.casefold().strip()
    status_ascii = _ascii_fold(status_raw)
    targets = _missed_status_targets()

    if status_cf in targets or status_ascii in targets:
        return True
    if status_cf in _BUILTIN_MISSED_STATUSES or status_ascii in _BUILTIN_MISSED_STATUSES:
        return True

    # Kontrollü: yalnızca uzun hedef, status metninin içinde geçsin
    # (status ⊂ target yapmıyoruz — "Cevap" ⊂ "Cevapsız" yanlış pozitif olur)
    for target in targets:
        if len(target) < 5:
            continue
        if target in status_cf or target in status_ascii:
            return True
    return False


def _configured_queues() -> list[str] | None:
    """TONIVA_QUEUE env ile kuyruk filtresi."""
    raw = os.getenv("TONIVA_QUEUE", "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return None


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
    """Kaçan çağrıları Toniva queue-detail raporundan çeker.

    company_code imza uyumu için alınır; Toniva'da kullanılmaz.
    uncompleted_only: Toniva UI'da IsCompleted genelde yok; yoksa yok sayılır.
    """
    del company_code  # imza uyumu

    names = department_names or _parse_department_names(department_name)
    if not names:
        names = _configured_queues()

    # API queue param: tek kuyruk varsa query'ye ver (daha az veri)
    # "1000 (1000)" ise API'ye kanonik "1000" gönder
    queue_param: str | None = None
    if names and len(names) == 1:
        queue_param = _canonical_queue_label(names[0]) or names[0]

    rows, _meta = fetch_report(
        "queue-detail",
        start_date,
        end_date,
        queue=queue_param,
        timeout=timeout,
    )

    if not rows:
        logger.info(
            "Toniva queue-detail boş döndü (%s … %s, queue=%s).",
            start_date,
            end_date,
            queue_param or "*",
        )

    missed: list[dict[str, Any]] = []
    skipped_no_status = 0
    skipped_not_missed = 0
    skipped_no_phone = 0
    skipped_completed = 0
    weak_datetime = 0

    for row in rows:
        if not _field(row, "Status", "status", "Durum", "durum", "callStatus"):
            skipped_no_status += 1
            # Status yoksa kaçan sayma (yanlış pozitif riski)
            continue
        if not is_missed_status(row):
            skipped_not_missed += 1
            continue
        normalized = normalize_queue_detail_row(row)
        if not normalized.get("Phone"):
            skipped_no_phone += 1
            continue
        if not normalized.get("ChekInDate") or not normalized.get("ChekInTime"):
            weak_datetime += 1
            logger.warning(
                "Toniva zayıf tarih/saat (dedup riski): phone=%s date=%s time=%s raw_keys=%s",
                normalized.get("Phone"),
                normalized.get("ChekInDate"),
                normalized.get("ChekInTime"),
                list(row.keys())[:12] if isinstance(row, dict) else [],
            )
        if uncompleted_only:
            completed = normalized.get("IsCompleted")
            if isinstance(completed, bool) and completed:
                skipped_completed += 1
                continue
            if str(completed).strip().lower() in {"true", "1", "yes"}:
                skipped_completed += 1
                continue
        missed.append(normalized)

    before_queue = len(missed)
    if names:
        # Toniva: her zaman alias-aware filtre; loose ek gevşeklik sağlar
        missed = filter_by_queues(
            missed,
            names,
            loose=loose_department_match,
        )

    logger.info(
        "Toniva missed: raw=%s missed=%s after_queue=%s "
        "(no_status=%s not_missed=%s no_phone=%s completed=%s weak_dt=%s queue=%s)",
        len(rows),
        before_queue,
        len(missed),
        skipped_no_status,
        skipped_not_missed,
        skipped_no_phone,
        skipped_completed,
        weak_datetime,
        names or "*",
    )
    if rows and before_queue == 0 and skipped_no_status == len(rows):
        sample_keys = list(rows[0].keys()) if isinstance(rows[0], dict) else []
        logger.warning(
            "Toniva: tüm satırlarda Status/Durum yok — alan map kontrol edin. keys=%s",
            sample_keys[:20],
        )
    if before_queue and names and not missed:
        sample_queue = "?"
        for r in rows:
            if is_missed_status(r):
                sample_queue = _department_name(normalize_queue_detail_row(r)) or "?"
                break
        logger.warning(
            "Toniva: cevapsız var ama kuyruk filtresi sıfırladı. "
            "TONIVA_QUEUE=%s örnek_kuyruk=%s",
            names,
            sample_queue,
        )

    return missed


def fetch_conversations(
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    del company_code
    all_rows: list[dict[str, Any]] = []

    # 1) OpenAPI önerisi: pageSize olmadan tüm pencere (max 5000)
    rows, meta = fetch_report(
        "conversations",
        start_date,
        end_date,
        timeout=timeout,
    )
    all_rows.extend(rows)
    truncated = bool(meta.get("truncated"))

    # 2) truncated veya tam sayfa şüphesi → sayfalı devam
    if truncated or len(rows) >= 5000:
        page = 2
        page_size = 5000
        # İlk sayfayı pageSize ile yeniden çekme; devam sayfalarından ekle
        # truncated true ise ilk batch zaten cap'li olabilir — page=1 pageSize ile yenile
        if truncated and len(rows) < 5000:
            rows_p1, meta = fetch_report(
                "conversations",
                start_date,
                end_date,
                page=1,
                page_size=page_size,
                timeout=timeout,
            )
            all_rows = list(rows_p1)
            page = 2

        while page <= 50:
            rows_page, meta = fetch_report(
                "conversations",
                start_date,
                end_date,
                page=page,
                page_size=page_size,
                timeout=timeout,
            )
            if not rows_page:
                break
            all_rows.extend(rows_page)
            total = meta.get("total_count") or meta.get("totalCount")
            if total is not None:
                try:
                    if len(all_rows) >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            if len(rows_page) < page_size:
                break
            if meta.get("truncated") and page >= 50:
                break
            page += 1
        else:
            logger.warning("Toniva conversations 50 sayfa sınırına ulaştı.")

        if meta.get("truncated") or truncated:
            logger.warning(
                "Toniva conversations truncated (start=%s end=%s, fetched=%s)",
                start_date,
                end_date,
                len(all_rows),
            )

    logger.info(
        "Toniva conversations: %s satır (%s … %s)",
        len(all_rows),
        start_date,
        end_date,
    )
    return [normalize_conversation_row(r) for r in all_rows]


def get_available_queues(
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    timeout: int = 30,
) -> list[tuple[str, str]]:
    del company_code
    queues: dict[str, str] = {}

    try:
        rows, _ = fetch_report(
            "queue-summary",
            start_date,
            end_date,
            timeout=timeout,
        )
        for row in rows:
            name = _field(
                row,
                "QueueName",
                "Queue",
                "queueName",
                "queue",
                "name",
                "Name",
            )
            number = _field(row, "QUEUE", "queueId", "queue_id", "id", "number")
            if name:
                queues[name] = number or name
    except TonivaError as exc:
        logger.warning("queue-summary alınamadı: %s", exc)

    # Fallback: queue-detail'den unique kuyruklar
    if not queues:
        try:
            rows, _ = fetch_report(
                "queue-detail",
                start_date,
                end_date,
                timeout=timeout,
            )
            for row in rows:
                norm = normalize_queue_detail_row(row)
                name = _department_name(norm)
                if name:
                    queues[name] = name
        except TonivaError as exc:
            logger.warning("queue-detail kuyruk listesi alınamadı: %s", exc)

    return sorted((name, number) for name, number in queues.items())


def build_phone_dahili_cache(
    company_code: str,
    days: int = 15,
    timeout: int = 30,
) -> dict[str, str]:
    """Son N günlük görüşmelerden telefon → son dahili eşlemesi."""
    end_date = _report_today()
    start_date = end_date - timedelta(days=days)
    try:
        records = fetch_conversations(
            company_code,
            start_date,
            end_date,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Toniva dahili cache oluşturulamadı: %s", exc)
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
