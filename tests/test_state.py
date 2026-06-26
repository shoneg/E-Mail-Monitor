from __future__ import annotations

from pathlib import Path

import pytest

from mailflow_monitor.models import ConfigError
from mailflow_monitor.state import MonitorState, RouteState, StateStore, utc_now


def test_state_is_written_atomically_and_loaded(tmp_path: Path) -> None:
    path = tmp_path / "var" / "state.json"
    store = StateStore(str(path))
    state = MonitorState(last_run_at=utc_now(), last_run_success=True)
    state.routes["route"] = RouteState(last_success=True, last_checked_at=utc_now())

    store.save(state)
    loaded = store.load()

    assert loaded.last_run_success is True
    assert loaded.routes["route"].last_success is True
    assert not (path.parent / ".state.json.tmp").exists()


def test_corrupted_state_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid JSON"):
        StateStore(str(path)).load()

