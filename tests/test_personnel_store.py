import json
from pathlib import Path

from personnel_store import PersonnelStore


def test_add_update_and_link_chat_id(tmp_path: Path):
    store = PersonnelStore(tmp_path / "personnels.json")
    assert store.add_or_update("105", "Ahmet", "@ahmet_yilmaz")
    assert store.get("105")["telegram_username"] == "ahmet_yilmaz"

    linked = store.link_chat_id_by_username("ahmet_yilmaz", 123456789)
    assert linked == 1
    assert store.get("105")["telegram_chat_id"] == "123456789"
    assert store.get_all()[0]["dm_ready"] is True


def test_find_for_extension_by_name_token(tmp_path: Path):
    store = PersonnelStore(tmp_path / "personnels.json")
    store.add_or_update("105", "selen-K", "selen_user")

    found = store.find_for_extension("selen")
    assert found is not None
    assert found["personel_adi"] == "selen-K"
    assert found["telegram_username"] == "selen_user"


def test_find_for_extension_case_insensitive_key(tmp_path: Path):
    store = PersonnelStore(tmp_path / "personnels.json")
    store.add_or_update("Selen", "Selen Yilmaz", "selen_user")

    found = store.find_for_extension("selen")
    assert found is not None
    assert found["personel_adi"] == "Selen Yilmaz"


def test_excel_bulk_save_once(tmp_path: Path):
    from openpyxl import Workbook

    excel_path = tmp_path / "personel.xlsx"
    wb = Workbook()
    ws = wb.active
    # A=personel ismi, B=dahili, C=telegram username
    ws.append(["Personel ismi", "Dahili adı", "Telegram kullanıcı adı"])
    ws.append(["Ali", "105", "@ali"])
    ws.append(["Veli", "106", "@veli"])
    wb.save(excel_path)

    store = PersonnelStore(tmp_path / "personnels.json")
    count = store.load_from_excel(excel_path)
    assert count == 2
    assert store.count() == 2
    assert store.get("105")["personel_adi"] == "Ali"
    assert store.get("105")["telegram_username"] == "ali"
    assert store.get("106")["personel_adi"] == "Veli"

    raw = json.loads((tmp_path / "personnels.json").read_text(encoding="utf-8"))
    assert "105" in raw and "106" in raw


def test_excel_bulk_resets_chat_id_when_username_changes(tmp_path: Path):
    from openpyxl import Workbook

    store = PersonnelStore(tmp_path / "personnels.json")
    store.add_or_update("105", "Ali", "ali", telegram_chat_id="999")

    excel_path = tmp_path / "personel.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Ali Yılmaz", "105", "ali_new"])
    wb.save(excel_path)

    count = store.load_from_excel(excel_path)
    assert count == 1
    row = store.get("105")
    assert row["personel_adi"] == "Ali Yılmaz"
    assert row["telegram_username"] == "ali_new"
    assert row["telegram_chat_id"] == ""


def test_same_username_preserves_link_and_different_user_cannot_claim(tmp_path):
    store = PersonnelStore(tmp_path / "p.json")
    store.add_or_update("101", "Deniz", "deniz", telegram_chat_id="111")
    store.add_or_update("101", "Deniz Kaya", "@DENIZ")
    assert store.get("101")["telegram_chat_id"] == "111"
    assert store.link_chat_id_by_username("deniz", 222) == 0


def test_full_name_priority_and_ambiguous_first_name(tmp_path):
    store = PersonnelStore(tmp_path / "p.json")
    store.add_or_update("101", "Ahmet Yilmaz", "ahmet_y")
    store.add_or_update("102", "Ahmet Demir", "ahmet_d")
    assert store.find_for_extension("Ahmet Demir")["telegram_username"] == "ahmet_d"
    assert store.find_for_extension("Ahmet") is None