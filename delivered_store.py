import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_STORE_TZ = ZoneInfo("Europe/Istanbul")


class DeliveredStore:
    """Başarıyla personele (gerçek personel eşleşmesi olan) iletilen kaçan çağrı kayıtları.
    Sadece NotifyKind.PERSONNEL olan iletimler buraya kaydedilir.
    """

    def __init__(self, path: Path, *, retention_hours: int = 24) -> None:
        self.path = path
        self.retention_hours = retention_hours
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: list[dict[str, str]] = self._load()

    def _load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    @staticmethod
    def _parse_entry_call_date(value: Any) -> date | None:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except Exception:
            return None

    def _purge_expired(self) -> None:
        if self.retention_hours <= 0:
            return
        cutoff = datetime.now(_STORE_TZ).replace(tzinfo=None) - timedelta(hours=self.retention_hours)
        kept: list[dict[str, str]] = []
        for entry in self._entries:
            try:
                notified = datetime.strptime(entry["notified_at"], "%d.%m.%Y %H:%M:%S")
            except Exception:
                kept.append(entry)
                continue
            if notified >= cutoff:
                kept.append(entry)
        if len(kept) != len(self._entries):
            self._entries = kept
            self._save()

    def add(
        self,
        *,
        call_key: str,
        phone: str,
        personel_adi: str,
        call_date: date,
        notified_at: datetime | None = None,
    ) -> None:
        when = notified_at or datetime.now()
        entry = {
            "call_key": call_key,
            "phone": str(phone).strip(),
            "personel_adi": str(personel_adi).strip(),
            "call_date": call_date.strftime("%Y-%m-%d"),
            "notified_at": when.strftime("%d.%m.%Y %H:%M:%S"),
        }
        with self._lock:
            if any(e.get("call_key") == call_key for e in self._entries):
                return
            self._entries.append(entry)
            self._save()

    def get_by_call_date(self, target: date) -> list[dict[str, Any]]:
        key = target.strftime("%Y-%m-%d")
        rows = [e for e in self._entries if e.get("call_date") == key]
        return sorted(rows, key=lambda r: r.get("notified_at", ""))

    def purge_call_date(self, target: date) -> int:
        key = target.strftime("%Y-%m-%d")
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("call_date") != key]
            if len(self._entries) != before:
                self._save()
            return before - len(self._entries)

    def purge_expired(self) -> int:
        with self._lock:
            before = len(self._entries)
            self._purge_expired()
            return before - len(self._entries)

    def purge_older_than_call_date(self, min_call_date: date) -> int:
        """Belirtilen tarihten eski çağrı günlerini temizler (min_call_date dahil kalır)."""
        with self._lock:
            before = len(self._entries)
            kept: list[dict[str, str]] = []
            for entry in self._entries:
                entry_call_date = self._parse_entry_call_date(entry.get("call_date"))
                if entry_call_date is None or entry_call_date >= min_call_date:
                    kept.append(entry)
            self._entries = kept
            if len(self._entries) != before:
                self._save()
            return before - len(self._entries)

    def count(self) -> int:
        return len(self._entries)