"""PBX sağlayıcı seçimi: invekto | toniva.

Bot ve diğer katmanlar bu modülden import eder; provider env ile değişir.
"""

from __future__ import annotations

import os
import hashlib
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import invekto_client
import toniva_client

# Ortak yardımcılar — her iki provider sonrası normalize dict ile çalışır
from invekto_client import (  # noqa: F401
    call_key,
    call_key_variants,
    dedupe_calls_by_key,
    enrich_delivered_rows_with_callback_status,
    format_call_message,
    parse_command_dates,
    parse_command_date_list,
    split_calls_by_time,
    filter_calls_after_time,
    filter_by_department,
)


class PbxError(Exception):
    """Birleşik PBX hata tipi (InvektoError / TonivaError sarmalayıcı)."""


_ROUTING_CACHE_LOCK = threading.Lock()
_ROUTING_DAY_CACHE: OrderedDict[tuple, tuple[float, list[dict[str, Any]]]] = OrderedDict()
_ROUTING_CACHE_MAX_DAYS = 64


def _routing_today() -> date:
    return datetime.now(ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Istanbul"))).date()


def get_provider_name() -> str:
    return os.getenv("PBX_PROVIDER", "toniva").strip().lower() or "toniva"


def is_toniva() -> bool:
    return get_provider_name() == "toniva"


def is_invekto() -> bool:
    return get_provider_name() == "invekto"


def fetch_missed_calls(
    company_code: str,
    start_date: date,
    end_date: date,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    try:
        if is_toniva():
            return toniva_client.fetch_missed_calls(
                company_code, start_date, end_date, **kwargs
            )
        return invekto_client.fetch_missed_calls(
            company_code, start_date, end_date, **kwargs
        )
    except (invekto_client.InvektoError, toniva_client.TonivaError) as exc:
        raise PbxError(str(exc)) from exc


def fetch_conversations(
    company_code: str,
    start_date: date,
    end_date: date,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    try:
        if is_toniva():
            return toniva_client.fetch_conversations(
                company_code, start_date, end_date, **kwargs
            )
        return invekto_client.fetch_conversations(
            company_code, start_date, end_date, **kwargs
        )
    except (invekto_client.InvektoError, toniva_client.TonivaError) as exc:
        raise PbxError(str(exc)) from exc


def fetch_routing_conversations(
    company_code: str, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    kwargs = {"include_zero_duration": True, "force_day_chunk": True} if is_toniva() else {}
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    identity = (
        get_provider_name(), company_code,
        os.getenv("TONIVA_BASE_URL", ""),
        hashlib.sha256(os.getenv("TONIVA_API_KEY", "").encode()).hexdigest(),
        os.getenv("BOT_TIMEZONE", "Europe/Istanbul"),
    )
    result: list[dict[str, Any]] = []
    day = start_date
    while day <= end_date:
        key = (*identity, day)
        with _ROUTING_CACHE_LOCK:
            now = time.monotonic()
            ttl = 15 if day >= _routing_today() else 3600
            cached = _ROUTING_DAY_CACHE.get(key)
            if cached is None or now - cached[0] >= ttl:
                rows = fetch_conversations(company_code, day, day, **kwargs)
                _ROUTING_DAY_CACHE[key] = (time.monotonic(), rows)
            else:
                rows = cached[1]
            _ROUTING_DAY_CACHE.move_to_end(key)
            while len(_ROUTING_DAY_CACHE) > _ROUTING_CACHE_MAX_DAYS:
                _ROUTING_DAY_CACHE.popitem(last=False)
            result.extend(dict(row) for row in rows)
        day += timedelta(days=1)
    return result


def get_available_queues(
    company_code: str,
    start_date: date,
    end_date: date,
    **kwargs: Any,
) -> list[tuple[str, str]]:
    try:
        if is_toniva():
            return toniva_client.get_available_queues(
                company_code, start_date, end_date, **kwargs
            )
        return invekto_client.get_available_queues(
            company_code, start_date, end_date, **kwargs
        )
    except (invekto_client.InvektoError, toniva_client.TonivaError) as exc:
        raise PbxError(str(exc)) from exc


def build_phone_dahili_cache(
    company_code: str,
    days: int = 15,
    timeout: int = 30,
) -> dict[str, str]:
    try:
        if is_toniva():
            return toniva_client.build_phone_dahili_cache(
                company_code, days=days, timeout=timeout
            )
        return invekto_client.build_phone_dahili_cache(
            company_code, days=days, timeout=timeout
        )
    except (invekto_client.InvektoError, toniva_client.TonivaError) as exc:
        raise PbxError(str(exc)) from exc
