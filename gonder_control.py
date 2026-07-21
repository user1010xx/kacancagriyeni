""" /gonder iş durumu — bellek + dosya (çoklu process / restart dayanıklı).

Sorun: bellek bayrağı False iken arka plan veya poll hâlâ basabiliyordu;
durdur 'iş yok' diyordu. Dosya + task iptali + poll kilidi ile düzeltilir.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any


class GonderControl:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._task: Any = None  # asyncio.Task | None
        self._mem_running = False
        self._mem_cancel = False
        self._dates: list[str] = []

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"running": False, "cancel": False, "dates": []}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"running": False, "cancel": False, "dates": []}

    def _write(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    def is_running(self) -> bool:
        with self._lock:
            if self._mem_running:
                return True
            return bool(self._read().get("running"))

    def should_stop(self) -> bool:
        with self._lock:
            if self._mem_cancel:
                return True
            return bool(self._read().get("cancel"))

    def active_dates(self) -> list[date]:
        with self._lock:
            raw = list(self._dates) or list(self._read().get("dates") or [])
        out: list[date] = []
        for item in raw:
            try:
                out.append(date.fromisoformat(str(item)))
            except ValueError:
                continue
        return out

    def begin(self, dates: list[date], task: Any = None) -> None:
        with self._lock:
            self._mem_running = True
            self._mem_cancel = False
            self._dates = [d.isoformat() for d in dates]
            self._task = task
            self._write(
                {
                    "running": True,
                    "cancel": False,
                    "dates": self._dates,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
            )

    def attach_task(self, task: Any) -> None:
        with self._lock:
            self._task = task

    def request_cancel(self) -> tuple[bool, str]:
        """İptal iste. (iş_vardı_mı, kullanıcı_mesajı)"""
        with self._lock:
            file_state = self._read()
            was_running = self._mem_running or bool(file_state.get("running"))
            self._mem_cancel = True
            dates = list(self._dates) or list(file_state.get("dates") or [])
            self._write(
                {
                    "running": was_running,
                    "cancel": True,
                    "dates": dates,
                    "started_at": file_state.get("started_at"),
                    "cancel_requested_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            task = self._task

        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:
                pass

        if not was_running:
            return (
                False,
                "ℹ️ Kayıtlı çalışan /gonder yok.\n"
                "Yine de flood varsa: /gonder sessiz — kalanları bildirimsiz kapatır.",
            )
        return (
            True,
            "🛑 Durdurma isteği alındı.\n"
            "Arka plan görevi iptal ediliyor; yeni mesaj kesilecek.\n"
            "Gerekirse: /gonder sessiz",
        )

    def finish(self) -> None:
        with self._lock:
            self._mem_running = False
            self._mem_cancel = False
            self._task = None
            # dates son iş için kısa süre saklanabilir; running kapat
            prev = self._read()
            self._write(
                {
                    "running": False,
                    "cancel": False,
                    "dates": prev.get("dates") or self._dates,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self._dates = []
