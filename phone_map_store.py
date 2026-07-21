"""Kalıcı telefon → dahili eşlemesi.

PBX conversations/queue-detail bazen UI CDR'daki cevapsız dış aramaları
döndürmez. Bu depo:
- API'den yakalanan eşlemeleri kalıcı tutar
- Manuel /eslestir kayıtlarını saklar
- Bellek cache boşalsa bile son bilinen eşlemeyi kullanır
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path


def normalize_phone_key(phone: str) -> str:
    """Son 10 haneli çekirdek numara (TR)."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) > 10:
        if digits.startswith("90"):
            digits = digits[2:]
        elif digits.startswith("0"):
            digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


class PhoneMapStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cleaned: dict[str, str] = {}
                for k, v in loaded.items():
                    pk = normalize_phone_key(str(k))
                    dahili = str(v or "").strip()
                    if pk and dahili:
                        cleaned[pk] = dahili
                self._data = cleaned
        except Exception:
            self._data = {}

    def _save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp, self.path)

    def lookup(self, phone: str) -> str | None:
        """Telefon için dahili; birden fazla anahtar varyantı dener."""
        candidates = self._key_variants(phone)
        with self._lock:
            for key in candidates:
                val = self._data.get(key)
                if val:
                    return val
        return None

    def set(self, phone: str, dahili: str, *, save: bool = True) -> bool:
        pk = normalize_phone_key(phone)
        dahili_s = str(dahili or "").strip()
        if not pk or not dahili_s:
            return False
        with self._lock:
            if self._data.get(pk) == dahili_s:
                return True
            self._data[pk] = dahili_s
            if save:
                self._save()
        return True

    def merge(self, mapping: dict[str, str]) -> int:
        """API cache'inden toplu birleştir. Dönüş: yeni/değişen kayıt sayısı."""
        if not mapping:
            return 0
        changed = 0
        with self._lock:
            for phone, dahili in mapping.items():
                pk = normalize_phone_key(phone)
                dahili_s = str(dahili or "").strip()
                if not pk or not dahili_s:
                    continue
                if self._data.get(pk) != dahili_s:
                    self._data[pk] = dahili_s
                    changed += 1
            if changed:
                self._save()
        return changed

    def get_all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)

    def count(self) -> int:
        return len(self._data)

    def remove(self, phone: str) -> bool:
        pk = normalize_phone_key(phone)
        with self._lock:
            if pk in self._data:
                del self._data[pk]
                self._save()
                return True
        return False

    @staticmethod
    def _key_variants(phone: str) -> list[str]:
        raw = str(phone or "").strip()
        digits = re.sub(r"\D", "", raw)
        keys: list[str] = []
        core = normalize_phone_key(raw)
        for candidate in (
            core,
            digits,
            digits[-10:] if len(digits) >= 10 else "",
            digits[-9:] if len(digits) >= 9 else "",
            digits[2:] if digits.startswith("90") and len(digits) > 10 else "",
        ):
            if candidate and candidate not in keys:
                keys.append(candidate)
        return keys
