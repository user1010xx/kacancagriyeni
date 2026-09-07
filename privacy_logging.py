import logging
import os
import re


def redact(text: str) -> str:
    for name in ("TELEGRAM_BOT_TOKEN", "TONIVA_API_KEY"):
        secret = os.getenv(name, "").strip()
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", text)
    text = re.sub(r"tva_[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)", "[NUMBER]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))