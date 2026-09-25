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
    """Normalize and validate a configured logging level.

    Args:
        level: Level name; surrounding whitespace and case are ignored.

    Returns:
        One of DEBUG, INFO, WARNING, ERROR, or CRITICAL.

    Raises:
        ValueError: The normalized name is unsupported.
    """

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
        """Redact password/token assignments in the formatted log message in place.

        Only patterns in SECRET_PATTERNS are masked; this is not a general secret scanner.

        Args:
            record: Log record whose message and interpolation arguments are updated.

        Returns:
            True, allowing the redacted record to be emitted.
        """
        message = record.getMessage()
        for pattern in SECRET_PATTERNS:
            message = pattern.sub(r"\1<redacted>", message)
        record.msg = message
        # getMessage() already applied interpolation; clear arguments so handlers
        # do not format the sanitized message again with the original secret values.
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    """Set the root log level and attach redaction to every current handler.

    Installs the default stderr handler only if logging has no handlers yet.

    Args:
        level: Supported logging level, compared case-insensitively.

    Returns:
        None.

    Raises:
        ValueError: The log level is unsupported.
    """

    normalized = normalize_log_level(level)
    numeric_level = getattr(logging, normalized)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Propagated child records bypass root logger filters but still pass through
    # handler filters, so redaction belongs on every output handler.
    for handler in root_logger.handlers:
        _ensure_redacting_filter(handler)


def _ensure_redacting_filter(handler: logging.Handler) -> None:
    """Attach a secret filter unless this handler already has one.

    Args:
        handler: Output handler to inspect and, if necessary, modify.

    Returns:
        None.
    """
    if any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
        return
    handler.addFilter(SecretRedactingFilter())
