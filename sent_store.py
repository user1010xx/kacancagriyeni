import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _calendar_today() -> date:
    """BOT_TIMEZONE (varsayılan Europe/Istanbul) ile bugün — Railway UTC kayması önlenir."""
    name = os.getenv("BOT_TIMEZONE", "Europe/Istanbul").strip() or "Europe/Istanbul"
    try:
        return datetime.now(ZoneInfo(name)).date()
    except Exception:
        return date.today()


class SentStore:
    def __init__(self, path: Path, *, max_age_days: int = 45) -> None:
        self.path = path
        self.max_age_days = max_age_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._completed, self._group_notified, self._private_notified = self._load()
        self._dirty = False

    def _extract_date_from_key(self, key: str) -> date | None:
        for part in key.split("|"):
            try:
                return datetime.strptime(part.strip(), "%d.%m.%Y").date()
            except Exception:
                continue
        return None

    def _cleanup_old(self, keys: set[str]) -> set[str]:
        if self.max_age_days <= 0:
            return keys
        cutoff = _calendar_today() - timedelta(days=self.max_age_days)
        cleaned = set()
        for k in keys:
            kd = self._extract_date_from_key(k)
            if kd is None or kd >= cutoff:
                cleaned.add(k)
        return cleaned

    def _load(self) -> tuple[set[str], set[str], set[str]]:
        if not self.path.exists():
            return set(), set(), set()

        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            raw_completed = set(data)
            raw_group: set[str] = set()
            raw_private: set[str] = set()
            needs_save = True
        elif isinstance(data, dict):
            raw_completed = set(data.get("completed", []))
            raw_group = set(data.get("group_notified", []))
            raw_private = set(data.get("private_notified", []))
            needs_save = "private_notified" not in data
        else:
            return set(), set(), set()

        completed = self._cleanup_old(raw_completed)
        group_notified = self._cleanup_old(raw_group)
        private_notified = self._cleanup_old(raw_private)
        if (
            len(completed) != len(raw_completed)
            or len(group_notified) != len(raw_group)
            or len(private_notified) != len(raw_private)
        ):
            needs_save = True

        if needs_save:
            self._completed = completed
            self._group_notified = group_notified
            self._private_notified = private_notified
            self._dirty = True
            self._save()

        return completed, group_notified, private_notified

    def _save(self) -> None:
        payload = {
            "completed": sorted(self._completed),
            "group_notified": sorted(self._group_notified),
            "private_notified": sorted(self._private_notified),
        }
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)
        self._dirty = False

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._save()

    def is_complete(self, key: str) -> bool:
        return key in self._completed

    def is_complete_any(self, keys: list[str]) -> bool:
        return any(key in self._completed for key in keys)

    def is_group_notified(self, key: str) -> bool:
        return key in self._group_notified

    def is_group_notified_any(self, keys: list[str]) -> bool:
        return any(key in self._group_notified for key in keys)

    def is_private_notified(self, key: str) -> bool:
        return key in self._private_notified

    def is_private_notified_any(self, keys: list[str]) -> bool:
        return any(key in self._private_notified for key in keys)

    def has(self, key: str) -> bool:
        return self.is_complete(key)

    def mark_group_notified(self, key: str, *, save: bool = True) -> None:
        with self._lock:
            self._group_notified.add(key)
            self._dirty = True
            if save:
                self._save()

    def mark_private_notified(self, key: str, *, save: bool = True) -> None:
        with self._lock:
            self._private_notified.add(key)
            self._dirty = True
            if save:
                self._save()

    def mark_complete(self, key: str, *, save: bool = True) -> None:
        with self._lock:
            self._completed.add(key)
            self._group_notified.discard(key)
            self._private_notified.discard(key)
            self._dirty = True
            if save:
                self._save()

    def mark_complete_keys(self, keys: list[str], *, save: bool = True) -> None:
        with self._lock:
            for key in keys:
                self._completed.add(key)
                self._group_notified.discard(key)
                self._private_notified.discard(key)
            self._dirty = True
            if save:
                self._save()

    def mark_group_notified_keys(self, keys: list[str], *, save: bool = True) -> None:
        with self._lock:
            for key in keys:
                self._group_notified.add(key)
            self._dirty = True
            if save:
                self._save()

    def mark_private_notified_keys(self, keys: list[str], *, save: bool = True) -> None:
        with self._lock:
            for key in keys:
                self._private_notified.add(key)
            self._dirty = True
            if save:
                self._save()

    def add(self, key: str) -> None:
        self.mark_complete(key)

    def add_many(self, keys: list[str], *, save: bool = True) -> None:
        with self._lock:
            self._completed.update(keys)
            self._group_notified.difference_update(keys)
            self._private_notified.difference_update(keys)
            self._dirty = True
            if save:
                self._save()

    def count(self) -> int:
        return len(self._completed)

    def group_notified_count(self) -> int:
        return len(self._group_notified)

    def private_notified_count(self) -> int:
        return len(self._private_notified)

    def purge_old(self, days: int | None = None) -> int:
        with self._lock:
            before = len(self._completed) + len(self._group_notified) + len(self._private_notified)
            if days is not None:
                self.max_age_days = days
            self._completed = self._cleanup_old(self._completed)
            self._group_notified = self._cleanup_old(self._group_notified)
            self._private_notified = self._cleanup_old(self._private_notified)
            self._dirty = True
            self._save()
            after = len(self._completed) + len(self._group_notified) + len(self._private_notified)
            return before - after

    def unmark_for_dates(self, target_dates: set[date] | list[date]) -> int:
        """Belirtilen çağrı günlerine ait dedup kayıtlarını siler (yeniden iletim için)."""
        wanted = {d for d in target_dates if isinstance(d, date)}
        if not wanted:
            return 0

        def _matches(key: str) -> bool:
            kd = self._extract_date_from_key(key)
            return kd is not None and kd in wanted

        with self._lock:
            before = (
                len(self._completed)
                + len(self._group_notified)
                + len(self._private_notified)
            )
            self._completed = {k for k in self._completed if not _matches(k)}
            self._group_notified = {k for k in self._group_notified if not _matches(k)}
            self._private_notified = {
                k for k in self._private_notified if not _matches(k)
            }
            after = (
                len(self._completed)
                + len(self._group_notified)
                + len(self._private_notified)
            )
            removed = before - after
            if removed:
                self._dirty = True
                self._save()
            return removed