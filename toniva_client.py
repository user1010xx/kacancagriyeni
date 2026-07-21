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


def _rows_from_columns_matrix(
    columns: Any,
    matrix: Any,
) -> list[dict[str, Any]]:
    """columns + list-of-lists → dict satırlar (CDR tablo formatı)."""
    if not isinstance(columns, list) or not columns:
        return []
    if not isinstance(matrix, list) or not matrix:
        return []
    col_names = [str(c).strip() for c in columns]
    out: list[dict[str, Any]] = []
    for row in matrix:
        if not isinstance(row, (list, tuple)):
            continue
        item: dict[str, Any] = {}
        for idx, name in enumerate(col_names):
            if not name:
                continue
            item[name] = row[idx] if idx < len(row) else None
        if item:
            out.append(item)
    return out


def _extract_rows(body: Any) -> list[dict[str, Any]]:
    """Toniva yanıtından satır listesini çıkarır.

    Desteklenen şekiller:
    - [ {...}, {...} ]
    - { rows|data|items|records|result: [ {...} ] }
    - { columns: [...], rows|data: [ [...], ... ] }  ← tablo/CDR
    - iç içe payload / result sarmalayıcıları
    """
    if isinstance(body, list):
        if not body:
            return []
        if isinstance(body[0], dict):
            return [r for r in body if isinstance(r, dict)]
        return []

    if not isinstance(body, dict):
        raise TonivaError("Toniva API beklenmeyen yanıt tipi döndürdü.")

    # Tablo formatı: columns + matrix
    for col_key in ("columns", "Columns", "fields", "Fields", "headers", "Headers"):
        cols = body.get(col_key)
        if not cols:
            continue
        for row_key in ("rows", "data", "Data", "items", "records", "values", "result"):
            matrix = body.get(row_key)
            table = _rows_from_columns_matrix(cols, matrix)
            if table:
                logger.info(
                    "Toniva satırlar tablo formatından okundu (columns=%s, rows=%s)",
                    len(cols) if isinstance(cols, list) else "?",
                    len(table),
                )
                return table

    # Bilinen anahtarlar (dict satırlar)
    for key in (
        "rows",
        "data",
        "Data",
        "items",
        "result",
        "records",
        "calls",
        "list",
        "payload",
        "content",
    ):
        value = body.get(key)
        if isinstance(value, list):
            if value and isinstance(value[0], dict):
                return [r for r in value if isinstance(r, dict)]
            # list of lists without columns at this level — skip
        if isinstance(value, dict):
            nested = _extract_rows(value)
            if nested:
                return nested

    # Son çare: meta dışındaki en büyük dict-listesi (derinlik 3)
    skip_keys = {
        "meta",
        "Meta",
        "error",
        "errors",
        "message",
        "Message",
        "code",
        "status",
        "Status",
        "columns",
        "Columns",
    }

    def _find_dict_lists(obj: Any, depth: int = 0) -> list[list[dict[str, Any]]]:
        if depth > 3:
            return []
        found: list[list[dict[str, Any]]] = []
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            found.append([r for r in obj if isinstance(r, dict)])
            return found
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in skip_keys:
                    continue
                found.extend(_find_dict_lists(v, depth + 1))
        return found

    candidates = _find_dict_lists(body)
    if candidates:
        best = max(candidates, key=len)
        logger.debug("Toniva satırlar recursive fallback ile okundu (%s satır).", len(best))
        return best

    # Boş yanıtta anahtarları logla — üretim debug
    if body:
        logger.warning(
            "Toniva yanıtında satır listesi bulunamadı. top_keys=%s",
            list(body.keys())[:20],
        )
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
    min_call_duration: int | None = None,
    min_ring_duration: int | None = None,
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
    # Cevapsız dış aramalar (görüşme 00:00:00) personel eşlemesi için gerekli.
    # Varsayılan API eşiği bunları elemiş olabilir.
    if min_call_duration is not None:
        params["minCallDuration"] = min_call_duration
    if min_ring_duration is not None:
        params["minRingDuration"] = min_ring_duration

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
        "DahiliNumarasi",
        "Dahili Numarası",
        "DAHİLİ NUMARASI",
        "extensionNumber",
        "ExtensionNumber",
        "CompletedExtension",
    )
    extension_name = _field(
        record,
        "ExtensionName",
        "extensionName",
        "DahiliAdi",
        "dahiliAdi",
        "dahili_adi",
        "Dahili Adı",
        "DAHİLİ ADI",
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


def _field_slug_contains(record: dict[str, Any], *needles: str) -> str:
    """Alan adında (slug) needle geçen ilk dolu değeri döndürür."""
    needle_set = {_key_slug(n) for n in needles if n}
    for key, value in record.items():
        if value in (None, ""):
            continue
        slug = _key_slug(str(key))
        if any(n and n in slug for n in needle_set):
            text = str(value).strip()
            # Süre / boş tire alanlarını telefon sanma
            if not text or text in {"-", "—", "–"}:
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
                continue
            return text
    return ""


def _looks_like_external_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))
    # TR harici/mobil genelde 10+ hane (90 + 10 veya 0 + 10)
    return len(digits) >= 10


def _looks_like_extension(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text in {"-", "—", "–"}:
        return False
    digits = re.sub(r"\D", "", text)
    # Dahili: kısa numara (2-6 hane) veya isim (seda, selcuk)
    if 2 <= len(digits) <= 6 and digits == re.sub(r"\D", "", text):
        return True
    if not digits and 2 <= len(text) <= 40:
        return True
    # "seda", "selcuk -O"
    if re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", text) and len(text) <= 40:
        return True
    return False


def _best_external_phone(*candidates: Any) -> str:
    best = ""
    best_digits = ""
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        digits = re.sub(r"\D", "", text)
        if len(digits) < 10:
            continue
        # Daha uzun / 90'lı formatı tercih et
        if len(digits) > len(best_digits) or (
            len(digits) == len(best_digits) and digits.startswith("90")
        ):
            best = text
            best_digits = digits
    return best


def normalize_conversation_row(record: dict[str, Any]) -> dict[str, Any]:
    """Görüşme/CDR satırını botun beklediği forma çevirir.

    queue-detail ile aynı Türkçe UI alanlarını da destekler
    (TELEFON, DAHİLİ ADI, DAHİLİ NUMARASI, TARİH, SAAT, …).
    Ayrıca bilinmeyen alan adlarında slug taraması yapar — aksi halde
    telefon→dahili cache boş kalır ve tüm kaçanlar 'personel yok' düşer.
    """
    phone_known = _field(
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
        "Callee",
        "callee",
        "RemoteNumber",
        "remoteNumber",
        "number",
        "Number",
        "Telefon",
        "telefon",
        "TELEFON",
    )
    phone_slug = _field_slug_contains(
        record,
        "telefon",
        "phone",
        "caller",
        "called",
        "destination",
        "remote",
        "number",
    )
    phone = _best_external_phone(phone_known, phone_slug) or phone_known or phone_slug

    call_date_raw = _field(
        record,
        "Date",
        "date",
        "ChekInDate",
        "CreateDate",
        "callDate",
        "startDate",
        "StartDate",
        "start_time",
        "startTime",
        "StartTime",
        "datetime",
        "DateTime",
        "Tarih",
        "tarih",
        "TARİH",
    )
    if not call_date_raw:
        call_date_raw = _field_slug_contains(record, "tarih", "date", "start")

    call_time_raw = _field(
        record,
        "Time",
        "time",
        "ChekInTime",
        "CreateTime",
        "callTime",
        "startTime",
        "StartTime",
        "Saat",
        "saat",
        "SAAT",
    )
    if not call_time_raw:
        call_time_raw = _field_slug_contains(record, "saat", "time")

    # Tek datetime alanında birleşik gelebilir
    if not call_time_raw and call_date_raw and ("T" in str(call_date_raw) or " " in str(call_date_raw)):
        call_time_raw = call_date_raw

    # Dahili numarası (622) — UI: DAHİLİ NUMARASI
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
        "agentExtension",
        "AgentExtension",
        "src",
        "Src",
    )
    if not extension or _looks_like_external_phone(extension):
        ext_slug = _field_slug_contains(
            record,
            "dahilinumara",
            "extensionnumber",
            "extension",
            "dahili",
            "agentext",
        )
        if ext_slug and _looks_like_extension(ext_slug) and not _looks_like_external_phone(ext_slug):
            extension = ext_slug

    # Dahili adı (seda) — UI: DAHİLİ ADI
    extension_name = _field(
        record,
        "ExtensionName",
        "extensionName",
        "CompletedExtensionName",
        "Agent",
        "agent",
        "agentName",
        "AgentName",
        "Name",
        "DahiliAdi",
        "dahiliAdi",
        "dahili_adi",
        "Dahili Adı",
        "DAHİLİ ADI",
        "User",
        "userName",
        "DisplayName",
    )
    if not extension_name:
        extension_name = _field_slug_contains(
            record,
            "dahiliadi",
            "extensionname",
            "agentname",
            "agent",
            "displayname",
            "username",
        )

    # Dahili numara yanlışlıkla isim alanına yazıldıysa ayır
    if extension and not re.search(r"\d", extension) and not extension_name:
        extension_name = extension
        extension = ""
    if extension_name and re.fullmatch(r"\d{2,6}", extension_name) and not extension:
        extension = extension_name
        extension_name = ""

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
    include_zero_duration: bool = True,
) -> list[dict[str, Any]]:
    del company_code
    all_rows: list[dict[str, Any]] = []
    min_call = 0 if include_zero_duration else None
    min_ring = 0 if include_zero_duration else None

    # 1) OpenAPI önerisi: pageSize olmadan tüm pencere (max 5000)
    rows, meta = fetch_report(
        "conversations",
        start_date,
        end_date,
        min_call_duration=min_call,
        min_ring_duration=min_ring,
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
                min_call_duration=min_call,
                min_ring_duration=min_ring,
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
                min_call_duration=min_call,
                min_ring_duration=min_ring,
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


def _dahili_rank(value: str) -> int:
    """Aynı anda hem isim hem numara gelirse numarayı tercih et."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{2,6}", text):
        return 2
    return 1


def _ingest_phone_dahili_hit(
    phone_best: dict[str, tuple[datetime, str]],
    rec: dict[str, Any],
    *,
    when: datetime | None = None,
) -> None:
    """Normalize edilmiş kayıttan cache'e telefon→dahili işler."""
    phone_key = _normalize_phone(rec.get("Phone") or rec.get("phone") or "")
    # TR harici numara: normalize sonrası en az 10 hane
    if not phone_key or len(re.sub(r"\D", "", phone_key)) < 10:
        return

    dahili = _extract_dahili_from_record(rec)
    if not dahili:
        return
    # Harici numara dahili sanılmasın
    if _looks_like_external_phone(dahili):
        return

    if when is None:
        when = _parse_conversation_datetime(
            rec.get("Date") or rec.get("ChekInDate") or rec.get("CreateDate") or "",
            rec.get("Time") or rec.get("ChekInTime") or rec.get("CreateTime") or "",
        )

    prev = phone_best.get(phone_key)
    if (
        prev is None
        or when > prev[0]
        or (when == prev[0] and _dahili_rank(dahili) > _dahili_rank(prev[1]))
    ):
        phone_best[phone_key] = (when, dahili)


def _guess_when_from_raw(raw: dict[str, Any]) -> datetime:
    """Ham satırdan en iyi tarih-saat tahminini çıkarır."""
    date_cands: list[str] = []
    time_cands: list[str] = []
    for key, value in raw.items():
        if value in (None, ""):
            continue
        text = str(value).strip()
        slug = _key_slug(str(key))
        if any(n in slug for n in ("tarih", "date", "start", "datetime", "created")):
            date_cands.append(text)
        if any(n in slug for n in ("saat", "time")) and "date" not in slug:
            time_cands.append(text)
        # ISO tek alan
        if "T" in text and re.match(r"\d{4}-\d{2}-\d{2}T", text):
            date_cands.append(text)
            time_cands.append(text)

    for d in date_cands or [""]:
        d_norm = _normalize_call_date(d) or d
        t_norm = ""
        for t in time_cands or [""]:
            t_norm = _normalize_time(t) or t
            when = _parse_conversation_datetime(d_norm, t_norm)
            if when != datetime.min:
                return when
        when = _parse_conversation_datetime(d_norm, t_norm or "")
        if when != datetime.min:
            return when
    return datetime.min


def _ingest_raw_record_for_cache(
    phone_best: dict[str, tuple[datetime, str]],
    raw: dict[str, Any],
) -> None:
    """Şema-bağımsız ham satır işleme.

    1) Bilinen normalizer'lar (queue-detail + conversation)
    2) Tüm alanları tarayıp harici telefon + dahili (no/ad) heuristic eşlemesi

    UI CDR'daki 'Dış Arama / seda / 622 / 9053…' satırları API'de farklı
    alan adlarıyla gelse bile yakalanır.
    """
    if not isinstance(raw, dict) or not raw:
        return

    # 1) Standart normalizer'lar
    for norm in (
        normalize_conversation_row(raw),
        normalize_queue_detail_row(raw),
    ):
        _ingest_phone_dahili_hit(phone_best, norm)

    # 2) Heuristic: alan adından bağımsız telefon / dahili topla
    phones: list[str] = []
    ext_nums: list[str] = []
    ext_names: list[str] = []

    for key, value in raw.items():
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list, tuple, bool)):
            continue
        text = str(value).strip()
        if not text or text in {"-", "—", "–"}:
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
            continue

        slug = _key_slug(str(key))
        digits = re.sub(r"\D", "", text)

        if len(digits) >= 10:
            # Yön/trunk/hat gibi alanlardaki santral numarasını ele
            if any(n in slug for n in ("trunk", "hat", "line", "did")):
                continue
            phones.append(text)
            continue

        # Kısa numara = dahili adayı (yalnızca alan adı ipucu ile; Queue/ID yanlış pozitif olmasın)
        if re.fullmatch(r"\d{2,6}", text):
            if any(
                n in slug
                for n in (
                    "ext",
                    "dahili",
                    "agent",
                    "user",
                    "src",
                )
            ) or slug in {"number", "numara", "no"}:
                ext_nums.append(text)
            continue

        # İsim adayı
        if re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", text) and len(text) <= 40:
            if any(
                n in slug
                for n in (
                    "ext",
                    "dahili",
                    "agent",
                    "user",
                    "name",
                    "person",
                    "display",
                )
            ):
                ext_names.append(text)

    if not phones:
        return

    dahili = ext_nums[0] if ext_nums else (ext_names[0] if ext_names else "")
    if not dahili or _looks_like_external_phone(dahili):
        return

    when = _guess_when_from_raw(raw)
    for phone in phones:
        _ingest_phone_dahili_hit(
            phone_best,
            {
                "Phone": phone,
                "Extension": ext_nums[0] if ext_nums else "",
                "ExtensionName": ext_names[0] if ext_names else "",
            },
            when=when,
        )


def _safe_fetch_report_rows(
    slug: str,
    start_date: date,
    end_date: date,
    *,
    timeout: int,
    try_zero_duration: bool = False,
) -> list[dict[str, Any]]:
    """Rapor satırlarını güvenli çeker.

    minCallDuration=0 bazı raporlarda 400 verebiliyor; önce paramsız dene,
    gerekirse sıfır süreli ikinci deneme yap.
    """
    rows: list[dict[str, Any]] = []
    try:
        rows, _meta = fetch_report(
            slug,
            start_date,
            end_date,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning(
            "Toniva %s paramsız çekilemedi (%s…%s): %s",
            slug,
            start_date,
            end_date,
            exc,
        )
        rows = []

    if try_zero_duration:
        try:
            rows0, _ = fetch_report(
                slug,
                start_date,
                end_date,
                min_call_duration=0,
                min_ring_duration=0,
                timeout=timeout,
            )
            if rows0:
                # birleştir (ham dict; id yoksa tümünü ekle)
                rows = list(rows) + list(rows0)
        except Exception as exc:
            logger.info(
                "Toniva %s minDuration=0 desteklenmiyor/hata: %s",
                slug,
                exc,
            )
    return rows


def build_phone_dahili_cache(
    company_code: str,
    days: int = 15,
    timeout: int = 30,
) -> dict[str, str]:
    """Son N günden telefon → son dahili eşlemesi.

    Gerçek kök neden analizi:
    - UI 'Detaylı Arama Logları (CDR)' ≠ her zaman /reports/conversations içeriği
    - Cevapsız dış arama (görüşme 00:00:00) conversations'ta yok sayılabilir
    - minCallDuration=0 queue-detail'i bozup cache'i tamamen boşaltabiliyordu
    - Yanıt tablo (columns+rows) veya farklı alan adlarıyla gelebilir

    Strateji:
    1) conversations + queue-detail (paramsız, güvenli)
    2) conversations için ek minDuration=0 denemesi
    3) Truncation/boşlukta gün gün conversations
    4) Her ham satırda şema-bağımsız heuristic + normalizer
    """
    del company_code  # imza uyumu; Toniva API key ile çalışır
    end_date = _report_today()
    days = max(1, int(days))
    start_date = end_date - timedelta(days=days)
    phone_best: dict[str, tuple[datetime, str]] = {}
    sample_raw_keys: list[str] = []
    raw_total = 0

    def _consume_raw(raw_rows: list[dict[str, Any]]) -> None:
        nonlocal raw_total, sample_raw_keys
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            raw_total += 1
            if not sample_raw_keys:
                sample_raw_keys = list(raw.keys())[:30]
            _ingest_raw_record_for_cache(phone_best, raw)

    # --- 1) conversations tam pencere ---
    conv_n = 0
    conv0_n = 0
    try:
        conv = fetch_conversations(
            "toniva",
            start_date,
            end_date,
            timeout=timeout,
            include_zero_duration=False,
        )
        conv_n = len(conv)
        for rec in conv:
            _ingest_phone_dahili_hit(phone_best, rec)
    except Exception as exc:
        logger.warning("Toniva conversations (paramsız) cache başarısız: %s", exc)

    # Sıfır süreli (CDR dış arama) için ayrı geçiş — 400 olursa base bozulmasın
    try:
        conv0 = fetch_conversations(
            "toniva",
            start_date,
            end_date,
            timeout=timeout,
            include_zero_duration=True,
        )
        conv0_n = len(conv0)
        for rec in conv0:
            _ingest_phone_dahili_hit(phone_best, rec)
    except Exception as exc:
        logger.info("Toniva conversations minDuration=0 atlandı: %s", exc)

    logger.info(
        "Toniva cache conversations: base=%s zero_dur=%s",
        conv_n,
        conv0_n,
    )

    # --- 2) queue-detail (minDuration YOK — 400 riski) ---
    try:
        qd_rows = _safe_fetch_report_rows(
            "queue-detail",
            start_date,
            end_date,
            timeout=timeout,
            try_zero_duration=False,
        )
        _consume_raw(qd_rows)
        logger.info("Toniva cache queue-detail raw=%s", len(qd_rows))
    except Exception as exc:
        logger.warning("Toniva queue-detail cache başarısız: %s", exc)

    # --- 3) Hâlâ zayıfsa: gün gün conversations (5000 cap kaçırma) ---
    if len(phone_best) < 3:
        logger.info(
            "Toniva cache zayıf (%s numara); gün-gün conversations deneniyor.",
            len(phone_best),
        )
        for offset in range(days + 1):
            day = end_date - timedelta(days=offset)
            try:
                day_rows = fetch_conversations(
                    "toniva",
                    day,
                    day,
                    timeout=timeout,
                    include_zero_duration=True,
                )
                for rec in day_rows:
                    _ingest_phone_dahili_hit(phone_best, rec)
            except Exception as exc:
                logger.warning(
                    "Toniva günlük conversations başarısız (%s): %s",
                    day.isoformat(),
                    exc,
                )
            try:
                raw_day = _safe_fetch_report_rows(
                    "queue-detail",
                    day,
                    day,
                    timeout=timeout,
                    try_zero_duration=False,
                )
                _consume_raw(raw_day)
            except Exception:
                pass

    cache = {phone: dahili for phone, (_, dahili) in phone_best.items()}
    logger.info(
        "Toniva dahili cache: %s numara | ham_satir=%s keys_ornek=%s aralik=%s…%s",
        len(cache),
        raw_total,
        sample_raw_keys or "(yok)",
        start_date,
        end_date,
    )
    if not cache:
        logger.warning(
            "Toniva dahili cache BOŞ — personel eşlemesi yapılamaz. "
            "Örnek ham alan adları: %s. "
            "UI CDR satırları API conversations/queue-detail içinde yoksa "
            "eşleme imkânsızdır (public API'de ayrı CDR endpoint yok).",
            sample_raw_keys or "(yok)",
        )
    return cache
