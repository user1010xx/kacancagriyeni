import pytest
from datetime import date
from unittest.mock import patch

from invekto_client import (
    REPORT_TYPE_MISS_CALL,
    fetch_missed_calls,
    fetch_conversations,
    enrich_delivered_rows_with_callback_status,
    parse_command_dates,
    call_key,
    format_call_message,
    filter_by_department,
    filter_calls_after_time,
    split_calls_by_time,
    parse_call_datetime,
    _is_missed_call,
    _is_uncompleted,
    _match_department,
    _normalize_phone,
    _parse_conversation_datetime,
)


def test_parse_command_dates_valid():
    start, end = parse_command_dates("15.06.2026, 25.06.2026")
    assert start == date(2026, 6, 15)
    assert end == date(2026, 6, 25)


def test_parse_command_dates_invalid():
    with pytest.raises(ValueError):
        parse_command_dates("15.06.2026")


def test_parse_command_dates_reverse():
    with pytest.raises(ValueError):
        parse_command_dates("25.06.2026, 15.06.2026")


def test_call_key_and_format():
    sample = {
        "ID": "12345",
        "Phone": "905551112233",
        "ChekInDate": "2026-06-25",
        "ChekInTime": "14:22:11",
        "Queue": "Gelen Arama",
        "Status": "2",
    }
    key = call_key(sample)
    assert key == "905551112233|25.06.2026|14:22:11|Gelen Arama"

    msg = format_call_message(sample)
    assert "Kaçan Çağrı" in msg
    assert "905551112233" in msg


def test_filter_by_department_exact():
    calls = [
        {"Queue": "Gelen Arama", "Phone": "1"},
        {"QueueName": "Satış", "Phone": "2"},
        {"Queue": "gelen arama ekibi", "Phone": "3"},
    ]
    filtered = filter_by_department(calls, "Gelen Arama")
    assert len(filtered) == 1
    assert filtered[0]["Phone"] == "1"


def test_filter_by_department_loose():
    calls = [
        {"Queue": "Gelen Arama", "Phone": "1"},
        {"Queue": "gelen arama ekibi", "Phone": "3"},
    ]
    filtered = filter_by_department(calls, "Gelen Arama", loose=True)
    assert len(filtered) == 2


def test_filter_by_multiple_departments():
    calls = [
        {"Queue": "Gelen Arama", "Phone": "1"},
        {"Queue": "MESAI DIŞI", "Phone": "2"},
        {"Queue": "ANA MENU", "Phone": "3"},
    ]
    filtered = filter_by_department(calls, ["Gelen Arama", "MESAI DIŞI"])
    assert len(filtered) == 2
    assert {c["Phone"] for c in filtered} == {"1", "2"}


def test_filter_by_department_csv_string():
    calls = [
        {"Queue": "Gelen Arama", "Phone": "1"},
        {"Queue": "MESAI DIŞI", "Phone": "2"},
    ]
    filtered = filter_by_department(calls, "Gelen Arama,MESAI DIŞI")
    assert len(filtered) == 2


def test_filter_calls_after_time_inclusive():
    calls = [
        {"CreateTime": "14:56:59", "Phone": "1"},
        {"CreateTime": "14:57:00", "Phone": "2"},
        {"CreateTime": "15:01:00", "Phone": "3"},
    ]
    filtered = filter_calls_after_time(calls, "14:57:00")
    assert [c["Phone"] for c in filtered] == ["2", "3"]


def test_split_calls_by_time():
    calls = [
        {"CreateTime": "10:00:00", "Phone": "1"},
        {"CreateTime": "14:57:05", "Phone": "2"},
    ]
    before, after = split_calls_by_time(calls, "14:57:00")
    assert [c["Phone"] for c in before] == ["1"]
    assert [c["Phone"] for c in after] == ["2"]


def test_call_key_without_id_uses_phone():
    sample = {
        "Phone": "905442231772",
        "CreateDate": "2026-06-27T00:00:00",
        "CreateTime": "14:57:05",
        "Queue": "Gelen Arama",
    }
    key = call_key(sample)
    assert key == "905442231772|27.06.2026|14:57:05|Gelen Arama"


def test_dedupe_calls_by_key():
    from invekto_client import dedupe_calls_by_key

    calls = [
        {"Phone": "905551112233", "CreateDate": "2026-06-27", "CreateTime": "10:00:00", "Queue": "Gelen Arama"},
        {"Phone": "905551112233", "CreateDate": "2026-06-27", "CreateTime": "10:00:00", "Queue": "Gelen Arama"},
        {"Phone": "905551112244", "CreateDate": "2026-06-27", "CreateTime": "11:00:00", "Queue": "Gelen Arama"},
    ]
    assert len(dedupe_calls_by_key(calls)) == 2


def test_call_key_variants_include_legacy():
    from invekto_client import call_key_variants

    sample = {
        "ID": "12345",
        "Phone": "905551112233",
        "CreateDate": "2026-06-27",
        "CreateTime": "10:00:00",
        "Queue": "Gelen Arama",
    }
    variants = call_key_variants(sample)
    assert "905551112233|27.06.2026|10:00:00|Gelen Arama" in variants
    assert "12345|905551112233|27.06.2026|10:00:00|Gelen Arama" in variants


def test_match_department_modes():
    assert _match_department("Gelen Arama", "Gelen Arama")
    assert not _match_department("gelen arama ekibi", "Gelen Arama")
    assert _match_department("gelen arama ekibi", "Gelen Arama", loose=True)


def test_normalize_phone():
    assert _normalize_phone("905551112233") == "5551112233"
    assert _normalize_phone("05551112233") == "5551112233"


def test_parse_call_datetime_variants():
    call1 = {"ChekInDate": "2026-06-25", "ChekInTime": "09:15:00"}
    call2 = {"CreateDate": "25.06.2026", "CreateTime": "10:05"}
    call3 = {"Date": "2026-06-25T11:30:00", "Time": "11:30"}

    d1 = parse_call_datetime(call1)
    d2 = parse_call_datetime(call2)
    d3 = parse_call_datetime(call3)

    assert d1 is not None
    assert d2 is not None
    assert d3 is not None
    assert d1.day == 25


def test_is_missed_and_uncompleted():
    assert _is_missed_call({"Status": "2"})
    assert not _is_missed_call({"Status": "1"})

    assert _is_uncompleted({"IsCompleted": False})
    assert _is_uncompleted({"IsCompleted": "0"})
    assert not _is_uncompleted({"IsCompleted": True})


def test_fetch_missed_calls_uses_miss_call_report_only():
    today = date(2026, 6, 27)
    api_rows = [
        {"ID": "1", "Phone": "905551112233", "Queue": "Gelen Arama", "Status": "2"},
        {"ID": "2", "Phone": "905551112244", "Queue": "Satış", "Status": "2"},
    ]

    with patch("invekto_client._request_report", return_value=api_rows) as mock_request:
        calls = fetch_missed_calls(
            "12345678",
            today,
            today,
            department_name="Gelen Arama",
        )

    mock_request.assert_called_once()
    assert mock_request.call_args[0][3] == REPORT_TYPE_MISS_CALL
    assert len(calls) == 1
    assert calls[0]["Phone"] == "905551112233"


def test_fetch_conversations_uses_report_type_5():
    today = date(2026, 6, 28)
    api_rows = [{"Phone": "905551112233", "EventType": "1"}]
    with patch("invekto_client._request_report", return_value=api_rows) as mock_request:
        rows = fetch_conversations("12345678", today, today)
    assert rows == api_rows
    mock_request.assert_called_once()
    assert mock_request.call_args[0][3] == 5


def test_enrich_delivered_rows_with_callback_status_after_notification_and_name_match():
    rows = [
        {
            "phone": "905012600688",
            "personel_adi": "elcin",
            "notified_at": "28.06.2026 07:58:34",
        }
    ]
    conversations = [
        {
            "EventType": "1",
            "Phone": "905012600688",
            "Date": "2026-06-28",
            "Time": "07:40:00",
            "Extension": "105",
            "ExtensionName": "Elcin-k",
        },
        {
            "EventType": "1",
            "Phone": "905012600688",
            "Date": "2026-06-28",
            "Time": "08:10:12",
            "Extension": "105",
            "ExtensionName": "elci",
        },
    ]
    personnel_rows = [
        {
            "dahili_ad": "105",
            "personel_adi": "Elcin",
            "telegram_username": "elcin",
        }
    ]

    out = enrich_delivered_rows_with_callback_status(rows, conversations, personnel_rows)
    assert out[0]["callback_status"] == "Aradı - 28.06.2026 08:10:12"


def test_enrich_delivered_rows_with_callback_status_ignores_other_person():
    rows = [
        {
            "phone": "905012600688",
            "personel_adi": "elcin",
            "notified_at": "28.06.2026 07:58:34",
        }
    ]
    conversations = [
        {
            "EventType": "1",
            "Phone": "905012600688",
            "Date": "2026-06-28",
            "Time": "08:10:12",
            "Extension": "106",
            "ExtensionName": "asya",
        }
    ]
    personnel_rows = [
        {
            "dahili_ad": "105",
            "personel_adi": "Elcin",
            "telegram_username": "elcin",
        }
    ]

    out = enrich_delivered_rows_with_callback_status(rows, conversations, personnel_rows)
    assert out[0]["callback_status"] == "Aramadı"


def test_enrich_delivered_rows_ignores_calls_before_notification():
    rows = [
        {
            "phone": "905015322108",
            "personel_adi": "doga",
            "notified_at": "03.07.2026 13:22:42",
        }
    ]
    conversations = [
        {
            "Phone": "905015322108",
            "Date": "2026-07-03",
            "Time": "13:21:18",
            "Extension": "101",
            "ExtensionName": "doga",
        },
        {
            "Phone": "905015322108",
            "Date": "2026-07-03",
            "Time": "13:24:05",
            "Extension": "101",
            "ExtensionName": "doga",
        },
    ]
    personnel_rows = [
        {
            "dahili_ad": "101",
            "personel_adi": "doga",
            "telegram_username": "doga",
        }
    ]

    out = enrich_delivered_rows_with_callback_status(rows, conversations, personnel_rows)
    assert out[0]["callback_status"] == "Aradı - 03.07.2026 13:24:05"


def test_call_key_variants_no_duplicates():
    """call_key_variants yinelenen varyant döndürmemeli (duplicate kopya-yapıştır fix)."""
    from invekto_client import call_key_variants

    sample = {
        "ID": "12345",
        "Phone": "905551112233",
        "CreateDate": "2026-06-27",
        "CreateTime": "10:00:00",
        "Queue": "Gelen Arama",
    }
    variants = call_key_variants(sample)
    assert len(variants) == len(set(variants)), "Varyantlar benzersiz olmalı"


def test_call_key_variants_no_id_no_duplicates():
    """ID olmadığında da varyantlar benzersiz olmalı."""
    from invekto_client import call_key_variants

    sample = {
        "Phone": "905551112233",
        "CreateDate": "2026-06-27",
        "CreateTime": "10:00:00",
        "Queue": "Gelen Arama",
    }
    variants = call_key_variants(sample)
    assert len(variants) == len(set(variants)), "Varyantlar benzersiz olmalı"


def test_parse_conversation_datetime_iso8601_T_separator():
    """ISO 8601 'T' içeren tarihler doğru ayrıştırılmalı (geri arama tespiti hatası)."""
    from datetime import datetime

    # Invekto bazen "2026-07-02T00:00:00" formatında tarih döner.
    # Bu format _normalize_date'te işleniyor ancak _parse_conversation_datetime'da
    # eksikti — tüm conversation kayıtları datetime.min döndürüyor, "Aramadı" çıkıyordu.
    result = _parse_conversation_datetime("2026-07-02T00:00:00", "11:37:55")
    assert result == datetime(2026, 7, 2, 11, 37, 55), (
        f"ISO 8601 T-ayracı işlenemedi, dönen: {result}"
    )


def test_parse_conversation_datetime_iso8601_full_datetime():
    """Tam ISO datetime (tarih+saat bir arada) da doğru çalışmalı."""
    from datetime import datetime

    result = _parse_conversation_datetime("2026-07-02T11:37:55", "11:37:55")
    assert result == datetime(2026, 7, 2, 11, 37, 55)


def test_enrich_detects_callback_when_date_is_iso8601():
    """ChekInDate ISO 8601 formatındayken geri arama tespiti çalışmalı (üretim hatası)."""
    rows = [
        {
            "phone": "905051157407",
            "personel_adi": "sergen",
            "notified_at": "02.07.2026 11:37:28",
        }
    ]
    # Conversations API'den gelen kayıt: ChekInDate ISO 8601 formatında
    conversations = [
        {
            "Phone": "905051157407",
            "Date": "2026-07-02T00:00:00",   # ← ISO 8601, "T" içeriyor
            "Time": "11:37:55",               # callback: 27s after notification
            "Extension": "105",
            "ExtensionName": "sergen -O",
        }
    ]
    personnel_rows = [
        {"dahili_ad": "105", "personel_adi": "sergen", "telegram_username": "sergen"}
    ]

    out = enrich_delivered_rows_with_callback_status(rows, conversations, personnel_rows)
    assert out[0]["callback_status"].startswith("Aradı"), (
        f"Geri arama tespit edilemedi: {out[0]['callback_status']}"
    )
