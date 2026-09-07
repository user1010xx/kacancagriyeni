from datetime import date, datetime
from unittest.mock import patch

import pytest
from openpyxl import load_workbook

import toniva_client
from excel_export import export_delivered_report_excel, sort_calls
from invekto_client import enrich_delivered_rows_with_callback_status


@pytest.mark.parametrize("phone,direction,expected", [
    ("905550000001", "outbound", "Aradı"),
    ("905550000001", "inbound", "Aramadı"),
    ("", "outbound", "Aramadı"),
    ("905550000002", "outbound", "Aramadı"),
    ("905550000001", "", "Aramadı"),
])
def test_callback_requires_phone_and_direction(phone, direction, expected):
    rows = [{"phone": "905550000001", "personel_adi": "Deniz Kaya", "dahili": "101", "notified_at": "07.09.2026 10:00:00"}]
    conversations = [{"Phone": phone, "Direction": direction, "Extension": "101", "Date": "07.09.2026", "Time": "10:10:00"}]
    result = enrich_delivered_rows_with_callback_status(rows, conversations, now=datetime(2026, 9, 7, 12))
    assert result[0]["callback_status"].startswith(expected)


def test_conversation_failure_is_not_empty_success():
    with patch.object(toniva_client, "_fetch_conversations_pages", side_effect=toniva_client.TonivaError("offline")):
        with pytest.raises(toniva_client.TonivaError):
            toniva_client.fetch_conversations("fake", date(2026, 9, 6), date(2026, 9, 7))


def test_conversation_aliases_do_not_collapse():
    rows = [
        {"CallerNumber": "905550000001", "ChekInTime": "10:00:00", "extensionNumber": "101"},
        {"CallerNumber": "905550000002", "ChekInTime": "11:00:00", "extensionNumber": "102"},
    ]
    with patch.object(toniva_client, "fetch_report", return_value=(rows, {"total_count": 2})):
        actual = toniva_client._fetch_conversations_pages(date(2026, 9, 7), date(2026, 9, 7), min_call_duration=None, min_ring_duration=None, timeout=5)
    assert actual == rows


def test_excel_invalid_date_and_literal_formula(tmp_path):
    valid = {"Date": "07.09.2026", "Time": "10:00:00"}
    invalid = {"Date": "invalid"}
    assert sort_calls([invalid, valid]) == [valid, invalid]
    output = tmp_path / "report.xlsx"
    export_delivered_report_excel([{"personel_adi": "=1+1"}], output)
    workbook = load_workbook(output)
    assert workbook.active["A2"].data_type == "s"
    workbook.close()


@pytest.mark.parametrize("responses", [
    [([{"ID": "1"}], {"total_count": 2})],
    [([], {"truncated": True})],
    [([{"ID": "1"}], {"truncated": True}), ([{"ID": "1"}], {"truncated": True})],
])
def test_incomplete_pages_raise(responses):
    with patch.object(toniva_client, "fetch_report", side_effect=responses):
        with pytest.raises(toniva_client.TonivaError):
            toniva_client._fetch_conversations_pages(date(2026, 9, 7), date(2026, 9, 7),
                                                    min_call_duration=None, min_ring_duration=None, timeout=5)