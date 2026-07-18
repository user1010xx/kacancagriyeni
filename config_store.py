import json
import os
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "company_code": "",
    "backfilled_dates": [],
}


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class ConfigStore:
    def __init__(self, runtime_path: Path) -> None:
        self.runtime_path = runtime_path
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        self._runtime = self._load_runtime()
        self._load_env()

    def _load_env(self) -> None:
        # Varsayılan provider: toniva (bu proje hedefi). Invekto için PBX_PROVIDER=invekto.
        self.pbx_provider = (
            os.getenv("PBX_PROVIDER", "toniva").strip().lower() or "toniva"
        )
        self.toniva_api_key = os.getenv("TONIVA_API_KEY", "").strip()
        self.toniva_base_url = os.getenv(
            "TONIVA_BASE_URL",
            "https://crm.toniva.net/api/public/v1",
        ).strip().rstrip("/")
        self.toniva_missed_status = os.getenv(
            "TONIVA_MISSED_STATUS",
            "Cevapsız",
        ).strip() or "Cevapsız"

        # Kuyruk / departman filtresi:
        # Toniva: TONIVA_QUEUE öncelikli, yoksa INVEKTO_DEPARTMENT_NAME
        # Invekto: INVEKTO_DEPARTMENT_NAME
        if self.pbx_provider == "toniva":
            raw_departments = os.getenv("TONIVA_QUEUE", "").strip()
            if not raw_departments:
                raw_departments = os.getenv("INVEKTO_DEPARTMENT_NAME", "").strip()
        else:
            raw_departments = os.getenv(
                "INVEKTO_DEPARTMENT_NAME",
                "Gelen Arama,MESAI DIŞI",
            ).strip()

        self.department_names = [
            part.strip().strip('"').strip("'")
            for part in raw_departments.split(",")
            if part.strip().strip('"').strip("'")
        ]
        self.department_name = ", ".join(self.department_names)
        self.target_chat_id, self._chat_id_error = self._read_chat_id()
        try:
            self.polling_interval_seconds = max(
                int(os.getenv("POLLING_INTERVAL_SECONDS", "30")),
                15,
            )
        except ValueError:
            self.polling_interval_seconds = 30
        self.notify_uncompleted_only = _env_flag(
            "NOTIFY_UNCOMPLETED_ONLY",
            default=True,
        )
        # Toniva kuyruk adları "1000" / "1000 (1000)" gelebiliyor.
        # Alias filtresi toniva_client içinde zaten var; loose ek güvenlik ağı.
        # Explicit env yoksa: toniva→true, invekto→false
        if "INVEKTO_DEPARTMENT_LOOSE_MATCH" in os.environ:
            self.department_loose_match = _env_flag(
                "INVEKTO_DEPARTMENT_LOOSE_MATCH",
                default=False,
            )
        else:
            self.department_loose_match = self.pbx_provider == "toniva"

    def _load_runtime(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return DEFAULT_RUNTIME_CONFIG.copy()

        try:
            with self.runtime_path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return DEFAULT_RUNTIME_CONFIG.copy()

        merged = DEFAULT_RUNTIME_CONFIG.copy()
        if isinstance(loaded, dict):
            merged.update(loaded)
        return merged

    def _save_runtime(self) -> None:
        temp_path = self.runtime_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(self._runtime, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.runtime_path)

    @staticmethod
    def _read_chat_id() -> tuple[int, str | None]:
        raw = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip().strip("\"'")
        if not raw:
            return 0, None
        try:
            return int(raw), None
        except ValueError:
            return 0, "geçersiz sayı"

    @property
    def company_code(self) -> str:
        return str(self._runtime.get("company_code", "")).strip()

    @company_code.setter
    def company_code(self, value: str) -> None:
        self._runtime["company_code"] = value.strip()
        self._save_runtime()

    @property
    def is_toniva(self) -> bool:
        return self.pbx_provider == "toniva"

    @property
    def is_invekto(self) -> bool:
        return self.pbx_provider == "invekto"

    def pbx_ready_token(self) -> str | None:
        """Bot poll/komutları için 'hazır' işareti.

        Toniva: API key varsa 'toniva' döner (company_code yerine).
        Invekto: firma kodu.
        """
        if self.is_toniva:
            return "toniva" if self.toniva_api_key else None
        return self.company_code or None

    @staticmethod
    def backfill_job_key(target: date, after_time: str | None = None) -> str:
        if after_time:
            return f"{target.isoformat()}|{after_time}"
        return target.isoformat()

    def is_backfilled(self, target: date, after_time: str | None = None) -> bool:
        key = self.backfill_job_key(target, after_time)
        stored = self._runtime.get("backfilled_dates", [])
        return isinstance(stored, list) and key in stored

    def mark_backfilled(self, target: date, after_time: str | None = None) -> None:
        stored = list(self._runtime.get("backfilled_dates", []))
        key = self.backfill_job_key(target, after_time)
        if key not in stored:
            stored.append(key)
            self._runtime["backfilled_dates"] = stored
            self._save_runtime()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            errors.append("TELEGRAM_BOT_TOKEN")
        if self._chat_id_error:
            errors.append("TELEGRAM_GROUP_CHAT_ID (geçersiz)")
        elif not self.target_chat_id:
            errors.append("TELEGRAM_GROUP_CHAT_ID")
        if self.is_toniva and not self.toniva_api_key:
            errors.append("TONIVA_API_KEY")
        if self.pbx_provider not in {"toniva", "invekto"}:
            errors.append("PBX_PROVIDER (toniva|invekto)")
        return errors

    def as_text(self) -> str:
        department = self.department_name or "Tümü (filtre yok)"
        notify_mode = (
            "Sadece tamamlanmamış" if self.notify_uncompleted_only else "Tümü"
        )
        dept_match = (
            "Gevşek (substring)" if self.department_loose_match else "Tam eşleşme"
        )
        provider_label = "Toniva" if self.is_toniva else "Invekto"

        if self.is_toniva:
            key_preview = (
                f"{self.toniva_api_key[:8]}…"
                if len(self.toniva_api_key) > 8
                else ("ayarlı" if self.toniva_api_key else "yok")
            )
            auth_line = f"🔑 API Key: {key_preview}\n"
            extra = (
                f"📡 Base URL: {self.toniva_base_url}\n"
                f"🔴 Missed etiket: {self.toniva_missed_status}\n"
            )
            company_line = ""
        else:
            company = self.company_code or "Ayarlanmadı (/firmakodu)"
            auth_line = ""
            extra = ""
            company_line = f"🏢 Firma Kodu: {company}\n"

        return (
            "⚙️ Bot Ayarları\n\n"
            f"🔌 PBX Provider: {provider_label}\n"
            f"{company_line}"
            f"{auth_line}"
            f"{extra}"
            f"🏷️ Kuyruk/Departman: {department}\n"
            f"🔎 Departman eşleştirme: {dept_match}\n"
            f"📨 Bildirim filtresi: {notify_mode}\n"
            f"💬 Bildirim Grubu: {self.target_chat_id or 'Tanımlı değil'}\n"
            f"⏱️ Kontrol Aralığı: {self.polling_interval_seconds} sn"
        )
