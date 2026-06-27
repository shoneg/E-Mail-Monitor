"""Logging helpers that reduce the risk of leaking secrets."""

from __future__ import annotations

import logging
import re

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

SECRET_PATTERNS = (
    re.compile(r"(password\s*=\s*)[^,\s)]+", re.IGNORECASE),
    re.compile(r"(token\s*=\s*)[a-zA-Z0-9._:-]+", re.IGNORECASE),
)


def normalize_log_level(level: str) -> str:
    """Return a validated uppercase logging level."""

    normalized = level.strip().upper()
    if normalized not in VALID_LOG_LEVELS:
        expected = ", ".join(VALID_LOG_LEVELS)
        raise ValueError(f"unsupported log level '{level}'; expected one of: {expected}")
    return normalized


class SecretRedactingFilter(logging.Filter):
    """Redact common secret-looking values in log records.

    The application avoids logging configuration objects. This filter is an
    additional guard for third-party or standard-library exception strings.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in SECRET_PATTERNS:
            message = pattern.sub(r"\1<redacted>", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    """Configure stderr logging for CLI runs."""

    normalized = normalize_log_level(level)
    numeric_level = getattr(logging, normalized)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for handler in root_logger.handlers:
        _ensure_redacting_filter(handler)


def _ensure_redacting_filter(handler: logging.Handler) -> None:
    if any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
        return
    handler.addFilter(SecretRedactingFilter())
