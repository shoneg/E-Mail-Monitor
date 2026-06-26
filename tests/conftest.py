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
    for key, value in ENV_VARS.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def loaded_example_config(tmp_path: Path, example_env: None):
    config_path = tmp_path / "config.toml"
    example_text = Path("config.example.toml").read_text(encoding="utf-8")
    config_path.write_text(example_text, encoding="utf-8")
    return load_config(config_path)


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
