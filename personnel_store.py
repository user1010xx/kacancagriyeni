import json
import os
import threading
from pathlib import Path

from openpyxl import load_workbook
from invekto_client import _normalize_person_text
from json_storage import load_json, save_json


class PersonnelStore:
    """Personel yönetimi: dahili_ad -> {personel_adi, telegram_username, telegram_chat_id}"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        self._data = load_json(self.path, {}, dict)
        if any(not isinstance(info, dict) for info in self._data.values()):
            raise ValueError("Geçersiz personel kayıt yapısı")

    def _save(self) -> None:
        save_json(self.path, self._data)

    def add_or_update(
        self,
        dahili_ad: str,
        personel_adi: str,
        telegram_username: str,
        *,
        telegram_chat_id: str | None = None,
        save: bool = True,
    ) -> bool:
        dahili = str(dahili_ad).strip()
        if not dahili:
            return False

        username = str(telegram_username).strip().lstrip("@")
        with self._lock:
            existing = self._data.get(dahili, {})
            same_user = username.casefold() == existing.get("telegram_username", "").casefold()
            chat_id = (
                str(telegram_chat_id).strip()
                if telegram_chat_id is not None
                else existing.get("telegram_chat_id", "") if same_user else ""
            )
            self._data[dahili] = {
                "personel_adi": str(personel_adi).strip(),
                "telegram_username": username,
                "telegram_chat_id": chat_id,
            }
            if save:
                self._save()
        return True

    def link_chat_id_by_username(self, username: str, chat_id: int) -> int:
        if not username:
            return 0

        normalized = str(username).strip().lstrip("@").casefold()
        updated = 0
        with self._lock:
            for dahili, info in self._data.items():
                stored = str(info.get("telegram_username", "")).strip().lstrip("@").casefold()
                linked_id = str(info.get("telegram_chat_id") or "")
                if stored == normalized and (not linked_id or linked_id == str(chat_id)):
                    info["telegram_chat_id"] = str(chat_id)
                    updated += 1
            if updated:
                self._save()
        return updated

    def remove(self, dahili_ad: str) -> bool:
        dahili = str(dahili_ad).strip()
        with self._lock:
            if dahili in self._data:
                del self._data[dahili]
                self._save()
                return True
        return False

    @staticmethod
    def _extension_token(value: str) -> str:
        """Invekto/UI farklarını yumuşatır: 'selen-K' ve 'Selen K' -> 'selen'."""
        text = str(value).strip().casefold()
        if not text:
            return ""
        return text.split("-")[0].split()[0]

    def get(self, dahili_ad: str) -> dict[str, str] | None:
        return self._data.get(str(dahili_ad).strip())

    def find_for_extension(self, extension: str) -> dict[str, str] | None:
        """Invekto ExtensionName ile personel kaydını eşleştirir.

        Sıra: tam dahili anahtarı -> büyük/küçük harf -> personel adı -> @username
        """
        ext = str(extension).strip()
        if not ext:
            return None

        direct = self.get(ext)
        if direct:
            return direct

        ext_cf = ext.casefold()
        ext_token = self._extension_token(ext)

        for dahili, info in self._data.items():
            if str(dahili).strip().casefold() == ext_cf:
                return info

        normalized = _normalize_person_text(ext)
        matches = [
            info for info in self._data.values()
            if normalized in {
                _normalize_person_text(info.get("personel_adi", "")),
                _normalize_person_text(info.get("telegram_username", "")),
            }
        ]
        if matches:
            return matches[0] if len(matches) == 1 else None
        if len(normalized.split()) == 1:
            matches = [
                info for info in self._data.values()
                if self._extension_token(info.get("personel_adi", "")) == ext_token
            ]
            return matches[0] if len(matches) == 1 else None
        return None

    def get_all(self) -> list[dict[str, str]]:
        result = []
        for dahili, info in self._data.items():
            chat_id = str(info.get("telegram_chat_id", "")).strip()
            result.append(
                {
                    "dahili_ad": dahili,
                    "personel_adi": info.get("personel_adi", ""),
                    "telegram_username": info.get("telegram_username", ""),
                    "telegram_chat_id": chat_id,
                    "dm_ready": bool(chat_id),
                }
            )
        return sorted(result, key=lambda x: x["dahili_ad"])

    @staticmethod
    def _cell_str(row: tuple | list, index: int) -> str:
        if index >= len(row) or row[index] is None:
            return ""
        return str(row[index]).strip()

    @staticmethod
    def _is_header_row(ad: str, dahili: str, username: str) -> bool:
        """Başlık satırını atla (Türkçe / İngilizce)."""
        markers = {
            ad.casefold(),
            dahili.casefold(),
            username.casefold().lstrip("@"),
        }
        header_tokens = {
            "personel",
            "personel adı",
            "personel adi",
            "personel ismi",
            "ad",
            "adı",
            "adi",
            "isim",
            "name",
            "dahili",
            "dahili adı",
            "dahili adi",
            "dahili_ad",
            "extension",
            "extensionname",
            "telegram",
            "telegram kullanıcı adı",
            "telegram kullanici adi",
            "username",
            "kullanıcı adı",
            "kullanici adi",
            "@username",
        }
        return bool(markers & header_tokens)

    def load_from_excel(self, excel_path: Path) -> int:
        """Excel'den toplu personel ekle/güncelle.

        Sütun sırası (zorunlu):
          A: Personel ismi
          B: Dahili adı
          C: Telegram kullanıcı adı (@opsizonal)
        """
        if not excel_path.exists():
            return 0

        count = 0
        wb = load_workbook(excel_path, read_only=True, data_only=True)
        try:
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                if not row:
                    continue

                # A=isim, B=dahili, C=telegram username
                ad = self._cell_str(row, 0)
                dahili = self._cell_str(row, 1)
                username = self._cell_str(row, 2)

                if self._is_header_row(ad, dahili, username):
                    continue
                if not dahili or not ad:
                    continue

                if self.add_or_update(dahili, ad, username, save=False):
                    count += 1
        finally:
            wb.close()

        if count:
            with self._lock:
                self._save()
        return count

    def count(self) -> int:
        return len(self._data)