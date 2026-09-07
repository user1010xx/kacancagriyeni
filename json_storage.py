import json
import logging
import os
import time
import uuid
from pathlib import Path


def _replace(source: Path, destination: Path) -> None:
    for attempt in range(4):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


def _atomic_bytes(path: Path, content: bytes) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_bytes(path, payload)
    _atomic_bytes(path.with_suffix(path.suffix + ".bak"), payload)


def load_json(path: Path, default, expected_type):
    backup = path.with_suffix(path.suffix + ".bak")
    if not path.exists() and not backup.exists():
        return default
    for candidate in (path, backup):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(data, expected_type):
                raise ValueError("Unexpected JSON structure")
        except (OSError, ValueError):
            continue
        if candidate == backup:
            if path.exists():
                _replace(path, path.with_name(f"{path.name}.{uuid.uuid4().hex}.corrupt"))
            _atomic_bytes(path, candidate.read_bytes())
            logging.getLogger(__name__).warning("JSON yedekten kurtarıldı: %s", path.name)
        return data
    raise ValueError(f"Kayıt dosyası ve yedeği okunamıyor: {path.name}; veri sıfırlanmadı")