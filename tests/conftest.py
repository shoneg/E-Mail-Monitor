from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mailflow_monitor.config import load_config

ENV_VARS = {
    "STALWART_SMTP_PASSWORD": "stalwart-smtp",
    "STALWART_IMAP_PASSWORD": "stalwart-imap",
    "EXTERNAL_SMTP_PASSWORD": "external-smtp",
    "EXTERNAL_IMAP_PASSWORD": "external-imap",
}


@pytest.fixture
def example_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install deterministic example credentials in the test process environment.

    Args:
        monkeypatch: Pytest fixture for temporary environment or dependency replacements.

    Returns:
        None.
    """
    for key, value in ENV_VARS.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def loaded_example_config(tmp_path: Path, example_env: None):
    """Copy and load the example with paths isolated in a temporary directory.

    Args:
        tmp_path: Pytest-provided temporary directory for isolated configuration/state files.
        example_env: Fixture dependency installing the example environment secrets; yields None.

    Returns:
        Validated AppConfig using the fixture credentials and temporary state/lock paths.
    """
    config_path = tmp_path / "config.toml"
    example_text = Path("config.example.toml").read_text(encoding="utf-8")
    config_path.write_text(example_text, encoding="utf-8")
    return load_config(config_path)


@pytest.fixture
def fixed_now() -> datetime:
    """Provide a stable clock value for timing and notification tests.

    Returns:
        Timezone-aware UTC datetime for 2026-01-02 at 03:04:05.
    """
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
