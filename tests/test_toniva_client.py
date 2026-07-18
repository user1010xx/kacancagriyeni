from datetime import date
from unittest.mock import patch

from toniva_client import (
    _canonical_queue_label,
    _extract_rows,
    _key_slug,
    filter_by_queues,
    is_missed_status,
    normalize_conversation_row,
    normalize_queue_detail_row,
    queues_match,
    fetch_missed_calls,
)


def test_key_slug_turkish_headers():
    assert _key_slug("KUYRUK ADI") == "kuyrukadi"
    assert _key_slug("Kuyruk Adı") == "kuyrukadi"
    assert _key_slug("TARİH") == "tarih" or _key_slug("TARİH").startswith("tari")
    assert _key_slug("DURUM") == "durum"


def test_is_missed_status_cevapsiz():
    assert is_missed_status({"Status": "Cevapsız"})
    assert is_missed_status({"durum": "cevapsız"})
    assert is_missed_status({"Status": "2"})
    assert not is_missed_status({"Status": "Cevaplandı"})
    assert not is_missed_status({"Status": ""})
    # Kısa hedef yanlış pozitif üretmemeli (env varsayılanı Cevapsız)
    assert not is_missed_status({"Status": "Cevap"})


def test_queue_aliases_match_ui_format():
    assert queues_match("1000", "1000 (1000)")
    assert queues_match("1000 (1000)", "1000")
    assert _canonical_queue_label("1000 (1000)") == "1000"
    assert not queues_match("1000", "2000")

    calls = [
        {"Queue": "1000 (1000)", "Phone": "1"},
        {"Queue": "2000", "Phone": "2"},
    ]
    filtered = filter_by_queues(calls, ["1000"])
    assert len(filtered) == 1
    assert filtered[0]["Phone"] == "1"


def test_normalize_queue_detail_ui_like_row():
    raw = {
        "KUYRUK ADI": "1000",
        "TELEFON": "905362907812",
        "TARİH": "2026-07-18",
        "SAAT": "14:58:38",
        "GÖRÜŞME SÜRESİ": "00:00:00",
        "DURUM": "Cevapsız",
        "ÇALDIRMA SÜRESİ": "00:00:00",
        "DAHİLİ": "",
        "DAHİLİ ADI": "",
        "HAT": "903129950469",
    }
    norm = normalize_queue_detail_row(raw)
    assert norm["Phone"] == "905362907812"
    assert norm["Queue"] == "1000"
    assert norm["ChekInDate"] == "18.07.2026"
    assert norm["ChekInTime"] == "14:58:38"
    assert norm["Status"] == "Cevapsız"
    assert norm["Trunk"] == "903129950469"


def test_normalize_turkish_long_date():
    raw = {
        "Phone": "905551112233",
        "Queue": "1000",
        "Date": "Cumartesi 18 Temmuz 2026",
        "Time": "13:54:16",
        "Status": "Cevapsız",
    }
    norm = normalize_queue_detail_row(raw)
    assert norm["ChekInDate"] == "18.07.2026"
    assert norm["ChekInTime"] == "13:54:16"


def test_extract_rows_variants():
    assert len(_extract_rows([{"a": 1}])) == 1
    assert len(_extract_rows({"rows": [{"a": 1}, {"b": 2}]})) == 2
    assert len(_extract_rows({"data": [{"a": 1}]})) == 1
    assert len(_extract_rows({"meta": {}, "items": []})) == 0


def test_fetch_missed_calls_filters_cevapsiz_and_queue():
    api_rows = [
        {
            "Phone": "905551112233",
            "Queue": "1000",
            "Date": "2026-07-18",
            "Time": "10:00:00",
            "Status": "Cevapsız",
        },
        {
            "Phone": "905551112244",
            "Queue": "1000",
            "Date": "2026-07-18",
            "Time": "11:00:00",
            "Status": "Cevaplandı",
        },
        {
            "Phone": "905551112255",
            "Queue": "2000",
            "Date": "2026-07-18",
            "Time": "12:00:00",
            "Status": "Cevapsız",
        },
    ]

    with patch(
        "toniva_client.fetch_report",
        return_value=(api_rows, {}),
    ) as mock_report:
        calls = fetch_missed_calls(
            "toniva",
            date(2026, 7, 18),
            date(2026, 7, 18),
            department_names=["1000"],
        )

    mock_report.assert_called_once()
    assert mock_report.call_args[0][0] == "queue-detail"
    assert len(calls) == 1
    assert calls[0]["Phone"] == "905551112233"
    assert calls[0]["Queue"] == "1000"


def test_fetch_missed_calls_matches_queue_display_format():
    api_rows = [
        {
            "Phone": "905551112233",
            "Queue": "1000 (1000)",
            "Date": "2026-07-18",
            "Time": "10:00:00",
            "Status": "Cevapsız",
        },
    ]
    with patch("toniva_client.fetch_report", return_value=(api_rows, {})):
        calls = fetch_missed_calls(
            "toniva",
            date(2026, 7, 18),
            date(2026, 7, 18),
            department_names=["1000"],
        )
    assert len(calls) == 1
    assert calls[0]["Phone"] == "905551112233"


def test_fetch_missed_skips_rows_without_status():
    api_rows = [
        {"Phone": "905551112233", "Queue": "1000", "Date": "2026-07-18", "Time": "10:00:00"},
    ]
    with patch("toniva_client.fetch_report", return_value=(api_rows, {})):
        calls = fetch_missed_calls(
            "toniva",
            date(2026, 7, 18),
            date(2026, 7, 18),
            department_names=["1000"],
        )
    assert calls == []


def test_normalize_conversation_row():
    row = normalize_conversation_row(
        {
            "phoneNumber": "905551112233",
            "date": "2026-07-18",
            "time": "09:15:00",
            "extension": "105",
            "extensionName": "Ahmet",
        }
    )
    assert row["Phone"] == "905551112233"
    assert row["Extension"] == "105"
    assert row["ExtensionName"] == "Ahmet"
