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
    assert row["ChekInDate"] == "18.07.2026"
    assert row["ChekInTime"] == "09:15:00"


def test_normalize_conversation_row_turkish_cdr_headers():
    """UI/CDR tarzı Türkçe alanlar phone→dahili cache için kritik."""
    row = normalize_conversation_row(
        {
            "YÖN": "Dış Arama",
            "DAHİLİ ADI": "selcuk",
            "DAHİLİ NUMARASI": "608",
            "TELEFON": "905319598246",
            "TARİH": "Salı 21 Temmuz 2026",
            "SAAT": "11:54:34",
            "GÖRÜŞME SÜRESİ": "00:00:00",
        }
    )
    assert row["Phone"] == "905319598246"
    assert row["Extension"] == "608"
    assert row["ExtensionName"] == "selcuk"
    assert row["ChekInDate"] == "21.07.2026"
    assert row["ChekInTime"] == "11:54:34"


def test_build_phone_dahili_cache_from_turkish_conversation_rows():
    from invekto_client import _normalize_phone
    from toniva_client import build_phone_dahili_cache

    api_rows = [
        {
            "DAHİLİ ADI": "selcuk",
            "DAHİLİ NUMARASI": "608",
            "TELEFON": "905319598246",
            "TARİH": "Salı 21 Temmuz 2026",
            "SAAT": "11:54:34",
        },
        {
            "phoneNumber": "905551112233",
            "extension": "105",
            "extensionName": "Ahmet",
            "date": "2026-07-20",
            "time": "10:00:00",
        },
    ]

    with patch(
        "toniva_client.fetch_report",
        return_value=(api_rows, {}),
    ):
        cache = build_phone_dahili_cache("toniva", days=2)

    phone_key = _normalize_phone("905319598246")
    assert phone_key in cache
    assert cache[phone_key] == "608"
    assert cache[_normalize_phone("905551112233")] == "105"


def test_normalize_outbound_zero_talk_cdr_seda():
    """Kullanıcı vakası: Dış Arama + 00:00:00 görüşme + seda/622."""
    row = normalize_conversation_row(
        {
            "YÖN": "Dış Arama",
            "DAHİLİ ADI": "seda",
            "DAHİLİ NUMARASI": "622",
            "TELEFON": "905412084627",
            "TARİH": "Salı 21 Temmuz 2026",
            "SAAT": "16:43:54",
            "ÇALDIRMA SÜRESİ": "00:00:21",
            "GÖRÜŞME SÜRESİ": "00:00:00",
        }
    )
    assert row["Phone"] == "905412084627"
    assert row["Extension"] == "622"
    assert row["ExtensionName"] == "seda"


def test_build_phone_dahili_cache_merges_queue_detail_and_conversations():
    """conversations boş olsa bile queue-detail/CDR satırından eşleme kurulmalı."""
    from invekto_client import _normalize_phone
    from toniva_client import build_phone_dahili_cache

    def fake_fetch(slug, start_date, end_date, **kwargs):
        if slug == "conversations":
            return [], {}
        if slug == "queue-detail":
            return (
                [
                    {
                        "YÖN": "Dış Arama",
                        "DAHİLİ ADI": "seda",
                        "DAHİLİ NUMARASI": "622",
                        "TELEFON": "905412084627",
                        "TARİH": "Salı 21 Temmuz 2026",
                        "SAAT": "16:43:54",
                        "GÖRÜŞME SÜRESİ": "00:00:00",
                    }
                ],
                {},
            )
        return [], {}

    with patch("toniva_client.fetch_report", side_effect=fake_fetch):
        cache = build_phone_dahili_cache("toniva", days=1)

    assert cache.get(_normalize_phone("905412084627")) == "622"


def test_build_phone_dahili_cache_uses_forced_pagination():
    """Conversations ZORUNLU page+pageSize ile çekilmeli (varsayılan 30 bug'ı)."""
    from toniva_client import build_phone_dahili_cache

    calls: list[dict] = []

    def fake_fetch(slug, start_date, end_date, **kwargs):
        calls.append({"slug": slug, **kwargs})
        return ([], {})

    with patch("toniva_client.fetch_report", side_effect=fake_fetch):
        build_phone_dahili_cache("toniva", days=1)

    conv_calls = [c for c in calls if c["slug"] == "conversations"]
    assert conv_calls
    # En az bir istekte explicit pagination
    assert any(c.get("page") == 1 and c.get("page_size") for c in conv_calls)
    # zero-duration için minCallDuration=0
    assert any(c.get("min_call_duration") == 0 for c in conv_calls)
    # queue-detail minDuration ile bozulmamalı
    qd = [c for c in calls if c["slug"] == "queue-detail"]
    assert qd
    assert all(c.get("min_call_duration") is None for c in qd)


def test_fetch_conversations_paginates_beyond_default_30():
    """pageSize dolu sayfalar bitene kadar devam — 30 satırda takılmaz."""
    from datetime import date as d
    from toniva_client import fetch_conversations

    pages: list[int] = []

    def fake_fetch(slug, start_date, end_date, **kwargs):
        assert slug == "conversations"
        page = kwargs.get("page") or 1
        page_size = kwargs.get("page_size") or 5000
        pages.append(page)
        if page == 1:
            rows = [
                {
                    "Phone": f"905550000{i:03d}",
                    "Extension": "608",
                    "ExtensionName": "selcuk",
                    "Date": "2026-07-21",
                    "Time": f"10:{i:02d}:00",
                }
                for i in range(page_size)
            ]
            return rows, {"total_count": page_size + 10}
        if page == 2:
            rows = [
                {
                    "Phone": f"905551000{i:03d}",
                    "Extension": "622",
                    "ExtensionName": "seda",
                    "Date": "2026-07-21",
                    "Time": f"11:{i:02d}:00",
                }
                for i in range(10)
            ]
            return rows, {"total_count": page_size + 10}
        return [], {"total_count": page_size + 10}

    with patch("toniva_client.fetch_report", side_effect=fake_fetch):
        rows = fetch_conversations(
            "toniva",
            d(2026, 7, 21),
            d(2026, 7, 21),
            include_zero_duration=True,
            force_day_chunk=False,
        )

    assert 1 in pages and 2 in pages
    assert len(rows) == 5000 + 10


def test_extract_rows_prefers_longest_list():
    """Özet 30 + asıl 100 satır varsa 100 seçilir."""
    from toniva_client import _extract_rows

    body = {
        "summary": [{"id": i} for i in range(30)],
        "data": {
            "calls": [
                {
                    "Phone": f"90555{i:07d}",
                    "Extension": "585",
                    "Date": "2026-07-21",
                    "Time": "12:00:00",
                }
                for i in range(100)
            ]
        },
        "meta": {"total_count": 100},
    }
    rows = _extract_rows(body)
    assert len(rows) == 100


def test_excel_export_style_headers_map_to_cache():
    """Excel gorusme export sütunları (Title Case TR) → cache."""
    from invekto_client import _normalize_phone
    from toniva_client import build_phone_dahili_cache

    excel_row = {
        "Yön": "Dış Arama",
        "Dahili Adı": "selen",
        "Dahili Numarası": "585",
        "Telefon": "905352198619",
        "Tarih": "2026-07-21",
        "Saat": "11:12:29",
        "Çaldırma Süresi": 5,
        "Görüşme Süresi": 0,
        "Hat": "903129950469",
    }

    def fake_fetch(slug, start_date, end_date, **kwargs):
        if slug == "conversations":
            return [excel_row], {"total_count": 1}
        return [], {}

    with patch("toniva_client.fetch_report", side_effect=fake_fetch):
        cache = build_phone_dahili_cache("toniva", days=0)

    assert cache.get(_normalize_phone("905352198619")) == "585"


def test_extract_rows_columns_matrix_format():
    """CDR tablo formatı: columns + list-of-lists."""
    from toniva_client import _extract_rows

    body = {
        "columns": [
            "YÖN",
            "DAHİLİ ADI",
            "DAHİLİ NUMARASI",
            "TELEFON",
            "TARİH",
            "SAAT",
        ],
        "rows": [
            [
                "Dış Arama",
                "seda",
                "622",
                "905304605429",
                "Pazartesi 20 Temmuz 2026",
                "18:59:26",
            ],
            [
                "Dış Arama",
                "seda",
                "622",
                "905304605429",
                "Pazartesi 20 Temmuz 2026",
                "18:53:05",
            ],
        ],
    }
    rows = _extract_rows(body)
    assert len(rows) == 2
    assert rows[0]["TELEFON"] == "905304605429"
    assert rows[0]["DAHİLİ NUMARASI"] == "622"
    assert rows[0]["DAHİLİ ADI"] == "seda"


def test_build_cache_from_columns_matrix_cdr():
    """Kullanıcı vakası: tablo formatı CDR → 905304605429 → 622."""
    from invekto_client import _normalize_phone
    from toniva_client import build_phone_dahili_cache

    matrix_body_rows = [
        {
            "YÖN": "Dış Arama",
            "DAHİLİ ADI": "seda",
            "DAHİLİ NUMARASI": "622",
            "TELEFON": "905304605429",
            "TARİH": "Pazartesi 20 Temmuz 2026",
            "SAAT": "18:59:26",
            "GÖRÜŞME SÜRESİ": "00:00:00",
        }
    ]

    def fake_fetch(slug, start_date, end_date, **kwargs):
        if slug == "conversations":
            # conversations boş — sadece queue-detail/ham kaynaktan gelsin
            return [], {}
        if slug == "queue-detail":
            return matrix_body_rows, {}
        return [], {}

    with patch("toniva_client.fetch_report", side_effect=fake_fetch):
        cache = build_phone_dahili_cache("toniva", days=1)

    assert cache.get(_normalize_phone("905304605429")) == "622"


def test_heuristic_ingest_unknown_field_names():
    from toniva_client import _ingest_raw_record_for_cache

    phone_best: dict = {}
    _ingest_raw_record_for_cache(
        phone_best,
        {
            "direction": "outbound",
            "agent_name": "seda",
            "agent_ext": "622",
            "remote_number": "905304605429",
            "started_at": "2026-07-20T18:59:26",
        },
    )
    from invekto_client import _normalize_phone

    assert phone_best.get(_normalize_phone("905304605429"))[1] == "622"
