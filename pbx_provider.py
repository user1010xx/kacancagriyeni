"""PBX sağlayıcı seçimi: invekto | toniva.

Bot ve diğer katmanlar bu modülden import eder; provider env ile değişir.
"""

from __future__ import annotations

import os
from datetime import date
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
    split_calls_by_time,
    filter_calls_after_time,
    filter_by_department,
)


class PbxError(Exception):
    """Birleşik PBX hata tipi (InvektoError / TonivaError sarmalayıcı)."""


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
