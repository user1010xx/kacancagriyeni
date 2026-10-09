"""PBX sağlayıcı seçimi: invekto | toniva.

Bot ve diğer katmanlar bu modülden import eder; provider env ile değişir.
"""

from __future__ import annotations

import os
import hashlib
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from collections import OrderedDict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import invekto_client
import toniva_client

logger = logging.getLogger(__name__)


class RoutingConversationRows(list[dict[str, Any]]):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        incomplete_dates: set[date] | None = None,
    ) -> None:
        super().__init__(rows)
        self.incomplete_dates = frozenset(incomplete_dates or ())


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
_ROUTING_DAY_CACHE: OrderedDict[
    tuple, tuple[float, list[dict[str, Any]], date]
] = OrderedDict()
_ROUTING_DAY_INFLIGHT: dict[tuple, Future[list[dict[str, Any]]]] = {}
_ROUTING_CACHE_MAX_DAYS = 64
_ROUTING_FETCH_WORKERS = 2


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
    company_code: str,
    start_date: date,
    end_date: date,
    *,
    allow_partial: bool = False,
) -> list[dict[str, Any]]:
    """Fetch routing CDRs; partial mode retains successful days and marks gaps."""
    kwargs = {"include_zero_duration": True, "force_day_chunk": True} if is_toniva() else {}
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    identity = (
        get_provider_name(), company_code,
        os.getenv("TONIVA_BASE_URL", ""),
        hashlib.sha256(os.getenv("TONIVA_API_KEY", "").encode()).hexdigest(),
        os.getenv("BOT_TIMEZONE", "Europe/Istanbul"),
    )
    days = [
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    ]
    incomplete_dates: set[date] = set()
    incomplete_dates_lock = threading.Lock()

    def fetch_day(day: date) -> list[dict[str, Any]]:
        key = (*identity, day)
        with _ROUTING_CACHE_LOCK:
            now = time.monotonic()
            today = _routing_today()
            ttl = 15 if day >= today else 3600
            cached = _ROUTING_DAY_CACHE.get(key)
            crossed_midnight = cached is not None and cached[2] == day and today > day
            if cached is not None and not crossed_midnight and now - cached[0] < ttl:
                _ROUTING_DAY_CACHE.move_to_end(key)
                return [dict(row) for row in cached[1]]

            pending = _ROUTING_DAY_INFLIGHT.get(key)
            if pending is None:
                pending = Future()
                _ROUTING_DAY_INFLIGHT[key] = pending
                owns_fetch = True
            else:
                owns_fetch = False

        if not owns_fetch:
            return [dict(row) for row in pending.result()]

        try:
            rows = fetch_conversations(company_code, day, day, **kwargs)
        except BaseException as exc:
            with _ROUTING_CACHE_LOCK:
                if _ROUTING_DAY_INFLIGHT.get(key) is pending:
                    _ROUTING_DAY_INFLIGHT.pop(key)
                pending.set_exception(exc)
            raise

        with _ROUTING_CACHE_LOCK:
            _ROUTING_DAY_CACHE[key] = (time.monotonic(), rows, today)
            _ROUTING_DAY_CACHE.move_to_end(key)
            while len(_ROUTING_DAY_CACHE) > _ROUTING_CACHE_MAX_DAYS:
                _ROUTING_DAY_CACHE.popitem(last=False)
            if _ROUTING_DAY_INFLIGHT.get(key) is pending:
                _ROUTING_DAY_INFLIGHT.pop(key)
            pending.set_result(rows)
        return [dict(row) for row in rows]

    def fetch_day_with_fallback(day: date) -> list[dict[str, Any]]:
        try:
            return fetch_day(day)
        except Exception as exc:
            if not allow_partial:
                raise
            with incomplete_dates_lock:
                incomplete_dates.add(day)
            logger.warning(
                "Yönlendirme geçmişi günü atlandı (%s): %s",
                day.isoformat(),
                exc,
            )
            return []

    if len(days) == 1:
        rows = fetch_day_with_fallback(days[0])
        return RoutingConversationRows(rows, incomplete_dates=incomplete_dates)

    with ThreadPoolExecutor(max_workers=min(_ROUTING_FETCH_WORKERS, len(days))) as pool:
        rows = [
            row
            for day_rows in pool.map(fetch_day_with_fallback, days)
            for row in day_rows
        ]
    return RoutingConversationRows(rows, incomplete_dates=incomplete_dates)


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
