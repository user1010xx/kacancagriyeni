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
    ws.append(["105", "Ali", "@ali"])
    ws.append(["106", "Veli", "@veli"])
    wb.save(excel_path)

    store = PersonnelStore(tmp_path / "personnels.json")
    count = store.load_from_excel(excel_path)
    assert count == 2
    assert store.count() == 2

    raw = json.loads((tmp_path / "personnels.json").read_text(encoding="utf-8"))
    assert "105" in raw and "106" in raw