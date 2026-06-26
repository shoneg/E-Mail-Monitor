"""Logging helpers that reduce the risk of leaking secrets."""

from __future__ import annotations

import logging
import re

SECRET_PATTERNS = (
    re.compile(r"(password\s*=\s*)[^,\s)]+", re.IGNORECASE),
    re.compile(r"(token\s*=\s*)[a-zA-Z0-9._:-]+", re.IGNORECASE),
)


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

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().addFilter(SecretRedactingFilter())

