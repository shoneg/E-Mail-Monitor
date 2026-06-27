from __future__ import annotations

import logging

import pytest

from mailflow_monitor.logging_utils import configure_logging, normalize_log_level


def test_normalize_log_level_is_case_insensitive_and_rejects_unknown_values() -> None:
    assert normalize_log_level(" debug ") == "DEBUG"

    with pytest.raises(ValueError, match="unsupported log level"):
        normalize_log_level("verbose")


def test_configure_logging_redacts_child_logger_records(capsys) -> None:
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_filters = root.filters[:]
    old_level = root.level
    for handler in old_handlers:
        root.removeHandler(handler)
    root.filters[:] = []

    try:
        configure_logging("INFO")
        logging.getLogger("mailflow_monitor.child").error("password=secret token=abc123")

        captured = capsys.readouterr()

        assert "password=<redacted>" in captured.err
        assert "token=<redacted>" in captured.err
        assert "password=secret" not in captured.err
        assert "token=abc123" not in captured.err
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in old_handlers:
            root.addHandler(handler)
        root.filters[:] = old_filters
        root.setLevel(old_level)
