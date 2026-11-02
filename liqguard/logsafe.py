"""Log redaction: secrets must never reach log output, even via exception messages."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Optional

REDACTED = "[REDACTED]"

PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),            # Telegram bot token
    re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{40,}"),                # Fernet ciphertext / wrapped keys
    re.compile(r"\b[A-Za-z0-9]{64}\b"),                        # Binance API keys, secrets, HMAC signatures
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),                    # OpenAI-style API keys
    re.compile(r"(?i)((?:token|signature|secret|api_?key|hash)=)[^&\s\"']+"),
]


def redact(text: str, literals: Iterable[str] = ()) -> str:
    for value in literals:
        if value:
            text = text.replace(value, REDACTED)
    for pattern in PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, literals: Iterable[Optional[str]] = ()):
        super().__init__()
        self._literals = {v for v in literals if v and len(v) >= 8}

    def add_secret(self, value: Optional[str]) -> None:
        if value and len(value) >= 8:
            self._literals.add(value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message, self._literals)
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info), self._literals)
            record.exc_info = None
        if cleaned != message or record.args:
            record.msg, record.args = cleaned, ()
        return True


def install_log_redaction(*literals: Optional[str]) -> RedactingFilter:
    """Attach one redacting filter to every root handler (records from all loggers pass through them)."""
    flt = RedactingFilter(literals)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(flt)
    return flt


AUDIT_LOGGER = "liqguard.protect.audit"


def install_audit_log(path: Optional[Path], redaction: Optional[logging.Filter] = None) -> Optional[logging.Handler]:
    """Append every protective-action audit line to `path` (mode 600), redacted like the console."""
    if path is None:
        return None
    path = Path(path)
    logger = logging.getLogger(AUDIT_LOGGER)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path.resolve():
            return handler
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    if redaction is not None:
        handler.addFilter(redaction)
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    return handler
