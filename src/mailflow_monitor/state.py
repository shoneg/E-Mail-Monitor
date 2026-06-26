"""Persistent JSON state with atomic writes and an advisory lock file."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ConfigError


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware ``datetime``."""

    return datetime.now(UTC)


def format_dt(value: datetime | None) -> str | None:
    """Serialize timestamps consistently for JSON and CLI output."""

    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    """Parse ISO timestamps from the state file."""

    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass
class RouteState:
    """Persisted state for one route."""

    last_success: bool | None = None
    last_checked_at: datetime | None = None
    last_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "last_success": self.last_success,
            "last_checked_at": format_dt(self.last_checked_at),
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RouteState:
        return cls(
            last_success=data.get("last_success"),
            last_checked_at=parse_dt(data.get("last_checked_at")),
            last_error=data.get("last_error"),
        )


@dataclass
class MonitorState:
    """Persisted monitor state without credentials."""

    last_run_at: datetime | None = None
    last_run_success: bool | None = None
    incident_started_at: datetime | None = None
    incident_details: str | None = None
    last_alert_at: datetime | None = None
    last_aliveness_at: datetime | None = None
    last_recovery_at: datetime | None = None
    routes: dict[str, RouteState] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "last_run_at": format_dt(self.last_run_at),
            "last_run_success": self.last_run_success,
            "incident_started_at": format_dt(self.incident_started_at),
            "incident_details": self.incident_details,
            "last_alert_at": format_dt(self.last_alert_at),
            "last_aliveness_at": format_dt(self.last_aliveness_at),
            "last_recovery_at": format_dt(self.last_recovery_at),
            "routes": {key: value.to_json() for key, value in self.routes.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MonitorState:
        routes_raw = data.get("routes", {})
        if not isinstance(routes_raw, dict):
            raise ConfigError("state.routes: must be an object")
        routes: dict[str, RouteState] = {}
        for route_id, route_data in routes_raw.items():
            if not isinstance(route_data, dict):
                raise ConfigError(f"state.routes.{route_id}: must be an object")
            routes[route_id] = RouteState.from_json(route_data)
        return cls(
            last_run_at=parse_dt(data.get("last_run_at")),
            last_run_success=data.get("last_run_success"),
            incident_started_at=parse_dt(data.get("incident_started_at")),
            incident_details=data.get("incident_details"),
            last_alert_at=parse_dt(data.get("last_alert_at")),
            last_aliveness_at=parse_dt(data.get("last_aliveness_at")),
            last_recovery_at=parse_dt(data.get("last_recovery_at")),
            routes=routes,
        )


class StateStore:
    """Load and save the local JSON state file."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> MonitorState:
        """Load the state, or return an empty state when the file does not exist."""

        if not self.path.exists():
            return MonitorState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"state: file is not valid JSON: {self.path}") from exc
        except OSError as exc:
            raise ConfigError(f"state: cannot read file: {self.path}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("state: top-level value must be an object")
        return MonitorState.from_json(raw)

    def save(self, state: MonitorState) -> None:
        """Write the state atomically by fsyncing a temporary file and replacing."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.tmp")
        payload = json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n"
        try:
            with tmp_path.open("w", encoding="utf-8") as tmp_file:
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


class FileLock(AbstractContextManager["FileLock"]):
    """Advisory lock for single-run execution.

    The lock is separate from the state file so a missing state file does not
    disable mutual exclusion.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._handle: Any = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            raise ConfigError(
                f"lock_file: another monitor run is already active: {self.path}"
            ) from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
        return False
