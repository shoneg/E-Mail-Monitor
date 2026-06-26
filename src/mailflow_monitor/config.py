"""Load and validate the TOML configuration file."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import (
    AddressConfig,
    AlertsConfig,
    AlivenessConfig,
    AppConfig,
    ConfigError,
    DeliveryConfig,
    ImapConfig,
    MonitorConfig,
    NotificationsConfig,
    RouteConfig,
    SmtpConfig,
    TlsMode,
)

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_config(path: str | Path = "config.toml") -> AppConfig:
    """Read a TOML file, expand environment variables, and validate references."""

    config_path = Path(path).expanduser().resolve()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"config: cannot read file: {config_path}") from exc

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config: invalid TOML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("config: top-level value must be a TOML table")
    expanded_data = _expand_environment_values(data)
    return _parse_app_config(expanded_data, config_path)


def _expand_environment_values(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_environment_string(value)
    if isinstance(value, list):
        return [_expand_environment_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment_values(item) for key, item in value.items()}
    return value


def _expand_environment_string(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError as exc:
            raise ConfigError(
                f"config: referenced environment variable ${name} is not set"
            ) from exc

    return ENV_VAR_PATTERN.sub(replace, text)


def _parse_app_config(data: Mapping[str, Any], config_path: Path) -> AppConfig:
    base_dir = config_path.parent
    monitor = _parse_monitor(_require_mapping(data, "monitor"), base_dir)
    addresses = _parse_addresses(_require_mapping(data, "addresses"), base_dir)
    routes = _parse_routes(data.get("routes", []), monitor)
    notifications = _parse_notifications(_optional_mapping(data, "notifications"), addresses)
    _validate_routes(routes, addresses)
    return AppConfig(
        config_path=str(config_path),
        monitor=monitor,
        addresses=addresses,
        routes=tuple(routes),
        notifications=notifications,
    )


def _parse_monitor(data: Mapping[str, Any], base_dir: Path) -> MonitorConfig:
    state_file = _resolve_relative_path(
        _get_str(data, "state_file", "monitor.state_file", default="var/state.json"),
        base_dir,
    )
    lock_file = _resolve_relative_path(
        _get_str(data, "lock_file", "monitor.lock_file", default="var/mailflow-monitor.lock"),
        base_dir,
    )
    default_timeout = _get_positive_int(
        data,
        "default_timeout_seconds",
        "monitor.default_timeout_seconds",
        default=900,
    )
    default_poll = _get_positive_int(
        data,
        "default_poll_interval_seconds",
        "monitor.default_poll_interval_seconds",
        default=30,
    )
    if default_poll > default_timeout:
        raise ConfigError(
            "monitor.default_poll_interval_seconds: must not be greater than "
            "monitor.default_timeout_seconds"
        )
    return MonitorConfig(
        state_file=str(state_file),
        lock_file=str(lock_file),
        log_level=_get_str(data, "log_level", "monitor.log_level", default="INFO"),
        default_timeout_seconds=default_timeout,
        default_poll_interval_seconds=default_poll,
        cleanup_received_test_messages=_get_bool(
            data,
            "cleanup_received_test_messages",
            "monitor.cleanup_received_test_messages",
            default=False,
        ),
    )


def _parse_addresses(data: Mapping[str, Any], base_dir: Path) -> dict[str, AddressConfig]:
    addresses: dict[str, AddressConfig] = {}
    for address_id, raw in data.items():
        path = f"addresses.{address_id}"
        table = _ensure_mapping(raw, path)
        email_address = _get_str(table, "address", f"{path}.address")
        smtp = None
        imap = None
        if "smtp" in table:
            smtp = _parse_smtp(
                _ensure_mapping(table["smtp"], f"{path}.smtp"),
                f"{path}.smtp",
                base_dir,
            )
        if "imap" in table:
            imap = _parse_imap(
                _ensure_mapping(table["imap"], f"{path}.imap"),
                f"{path}.imap",
                base_dir,
            )
        addresses[address_id] = AddressConfig(
            id=address_id,
            address=email_address,
            smtp=smtp,
            imap=imap,
        )
    if not addresses:
        raise ConfigError("addresses: at least one address must be configured")
    return addresses


def _parse_smtp(data: Mapping[str, Any], path: str, base_dir: Path) -> SmtpConfig:
    tls_mode = _parse_tls_mode(data, path)
    allow_plain = _get_bool(data, "allow_insecure_plaintext", f"{path}.allow_insecure_plaintext")
    _validate_tls_mode(tls_mode, allow_plain, path)
    ca_file = _resolve_optional_path(
        _get_optional_str(data, "ca_file", f"{path}.ca_file"),
        base_dir,
    )
    return SmtpConfig(
        host=_get_str(data, "host", f"{path}.host"),
        port=_get_positive_int(data, "port", f"{path}.port"),
        tls_mode=tls_mode,
        username=_get_str(data, "username", f"{path}.username"),
        password=_get_str(data, "password", f"{path}.password"),
        allow_insecure_plaintext=allow_plain,
        ca_file=ca_file,
    )


def _parse_imap(data: Mapping[str, Any], path: str, base_dir: Path) -> ImapConfig:
    tls_mode = _parse_tls_mode(data, path)
    allow_plain = _get_bool(data, "allow_insecure_plaintext", f"{path}.allow_insecure_plaintext")
    _validate_tls_mode(tls_mode, allow_plain, path)
    mailboxes_raw = data.get("mailboxes", ["INBOX"])
    if not isinstance(mailboxes_raw, list) or not mailboxes_raw:
        raise ConfigError(f"{path}.mailboxes: must be a non-empty list")
    mailboxes: list[str] = []
    for index, mailbox in enumerate(mailboxes_raw):
        if not isinstance(mailbox, str) or not mailbox:
            raise ConfigError(f"{path}.mailboxes[{index}]: must be a non-empty string")
        mailboxes.append(mailbox)
    ca_file = _resolve_optional_path(
        _get_optional_str(data, "ca_file", f"{path}.ca_file"),
        base_dir,
    )
    return ImapConfig(
        host=_get_str(data, "host", f"{path}.host"),
        port=_get_positive_int(data, "port", f"{path}.port"),
        tls_mode=tls_mode,
        username=_get_str(data, "username", f"{path}.username"),
        password=_get_str(data, "password", f"{path}.password"),
        mailboxes=tuple(mailboxes),
        allow_insecure_plaintext=allow_plain,
        ca_file=ca_file,
    )


def _parse_tls_mode(data: Mapping[str, Any], path: str) -> TlsMode:
    value = _get_str(data, "tls_mode", f"{path}.tls_mode")
    try:
        return TlsMode(value)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in TlsMode)
        raise ConfigError(f"{path}.tls_mode: invalid value '{value}', allowed: {allowed}") from exc


def _validate_tls_mode(tls_mode: TlsMode, allow_plain: bool, path: str) -> None:
    if tls_mode is TlsMode.PLAIN and not allow_plain:
        raise ConfigError(
            f"{path}.tls_mode: 'plain' is only allowed with allow_insecure_plaintext = true"
        )


def _parse_routes(raw_routes: Any, monitor: MonitorConfig) -> list[RouteConfig]:
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ConfigError("routes: at least one route must be configured")
    routes: list[RouteConfig] = []
    seen_ids: set[str] = set()
    for index, raw_route in enumerate(raw_routes):
        path = f"routes[{index}]"
        table = _ensure_mapping(raw_route, path)
        route_id = _get_str(table, "id", f"{path}.id")
        if route_id in seen_ids:
            raise ConfigError(f"{path}.id: route ID '{route_id}' is duplicated")
        seen_ids.add(route_id)
        timeout = _get_positive_int(
            table,
            "timeout_seconds",
            f"{path}.timeout_seconds",
            default=monitor.default_timeout_seconds,
        )
        poll_interval = _get_positive_int(
            table,
            "poll_interval_seconds",
            f"{path}.poll_interval_seconds",
            default=monitor.default_poll_interval_seconds,
        )
        if poll_interval > timeout:
            raise ConfigError(
                f"{path}.poll_interval_seconds: must not be greater than {path}.timeout_seconds"
            )
        deliveries = _parse_deliveries(table.get("deliveries"), path)
        routes.append(
            RouteConfig(
                id=route_id,
                description=_get_str(table, "description", f"{path}.description", default=route_id),
                from_id=_get_str(table, "from", f"{path}.from"),
                deliveries=tuple(deliveries),
                timeout_seconds=timeout,
                poll_interval_seconds=poll_interval,
            )
        )
    return routes


def _parse_deliveries(raw: Any, route_path: str) -> list[DeliveryConfig]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{route_path}.deliveries: at least one delivery is required")
    deliveries: list[DeliveryConfig] = []
    for index, raw_delivery in enumerate(raw):
        path = f"{route_path}.deliveries[{index}]"
        table = _ensure_mapping(raw_delivery, path)
        expect_raw = table.get("expect_at")
        if not isinstance(expect_raw, list) or not expect_raw:
            raise ConfigError(f"{path}.expect_at: must be a non-empty list")
        expect_at = []
        for expect_index, item in enumerate(expect_raw):
            if not isinstance(item, str) or not item:
                raise ConfigError(f"{path}.expect_at[{expect_index}]: must be a string")
            expect_at.append(item)
        deliveries.append(
            DeliveryConfig(
                to=_get_str(table, "to", f"{path}.to"),
                expect_at=tuple(expect_at),
            )
        )
    return deliveries


def _validate_routes(routes: list[RouteConfig], addresses: Mapping[str, AddressConfig]) -> None:
    for route_index, route in enumerate(routes):
        route_path = f"routes[{route_index}]"
        sender = addresses.get(route.from_id)
        if sender is None:
            raise ConfigError(f"{route_path}.from: address '{route.from_id}' does not exist")
        if sender.smtp is None:
            raise ConfigError(f"{route_path}.from: address '{route.from_id}' requires SMTP")
        for delivery_index, delivery in enumerate(route.deliveries):
            delivery_path = f"{route_path}.deliveries[{delivery_index}]"
            if delivery.to not in addresses:
                raise ConfigError(f"{delivery_path}.to: address '{delivery.to}' does not exist")
            for expect_index, expect_id in enumerate(delivery.expect_at):
                expected = addresses.get(expect_id)
                expect_path = f"{delivery_path}.expect_at[{expect_index}]"
                if expected is None:
                    raise ConfigError(f"{expect_path}: address '{expect_id}' does not exist")
                if expected.imap is None:
                    raise ConfigError(f"{expect_path}: address '{expect_id}' requires IMAP")


def _parse_notifications(
    data: Mapping[str, Any],
    addresses: Mapping[str, AddressConfig],
) -> NotificationsConfig:
    alerts = _parse_alerts(_optional_mapping(data, "alerts"), addresses)
    aliveness = _parse_aliveness(_optional_mapping(data, "aliveness"), addresses)
    return NotificationsConfig(alerts=alerts, aliveness=aliveness)


def _parse_alerts(data: Mapping[str, Any], addresses: Mapping[str, AddressConfig]) -> AlertsConfig:
    path = "notifications.alerts"
    enabled = _get_bool(data, "enabled", f"{path}.enabled", default=False)
    sender = _get_optional_str(data, "sender", f"{path}.sender")
    recipients = _parse_recipients(data.get("recipients", []), f"{path}.recipients", addresses)
    repeat = _get_positive_int(data, "repeat_after_seconds", f"{path}.repeat_after_seconds", 21600)
    send_recovery = _get_bool(
        data,
        "send_recovery_message",
        f"{path}.send_recovery_message",
        default=True,
    )
    _validate_notification_sender(enabled, sender, addresses, path)
    if enabled and not recipients:
        raise ConfigError(f"{path}.recipients: at least one recipient is required")
    return AlertsConfig(
        enabled=enabled,
        sender=sender,
        recipients=tuple(recipients),
        repeat_after_seconds=repeat,
        send_recovery_message=send_recovery,
    )


def _parse_aliveness(
    data: Mapping[str, Any],
    addresses: Mapping[str, AddressConfig],
) -> AlivenessConfig:
    path = "notifications.aliveness"
    enabled = _get_bool(data, "enabled", f"{path}.enabled", default=False)
    sender = _get_optional_str(data, "sender", f"{path}.sender")
    recipients = _parse_recipients(data.get("recipients", []), f"{path}.recipients", addresses)
    interval = _get_positive_int(data, "interval_seconds", f"{path}.interval_seconds", 604800)
    only_when_healthy = _get_bool(
        data,
        "only_when_healthy",
        f"{path}.only_when_healthy",
        default=True,
    )
    _validate_notification_sender(enabled, sender, addresses, path)
    if enabled and not recipients:
        raise ConfigError(f"{path}.recipients: at least one recipient is required")
    return AlivenessConfig(
        enabled=enabled,
        sender=sender,
        recipients=tuple(recipients),
        interval_seconds=interval,
        only_when_healthy=only_when_healthy,
    )


def _parse_recipients(
    raw: Any,
    path: str,
    addresses: Mapping[str, AddressConfig],
) -> list[str]:
    if raw == []:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: must be a list")
    recipients: list[str] = []
    for index, value in enumerate(raw):
        item_path = f"{path}[{index}]"
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{item_path}: must be a non-empty string")
        if value.startswith("account:"):
            account_id = value.removeprefix("account:")
            if account_id not in addresses:
                raise ConfigError(f"{item_path}: address '{account_id}' does not exist")
        elif not EMAIL_PATTERN.match(value):
            raise ConfigError(f"{item_path}: must be account:<id> or an email address")
        recipients.append(value)
    return recipients


def _validate_notification_sender(
    enabled: bool,
    sender: str | None,
    addresses: Mapping[str, AddressConfig],
    path: str,
) -> None:
    if not enabled:
        return
    if sender is None:
        raise ConfigError(f"{path}.sender: is required when notification is enabled")
    account = addresses.get(sender)
    if account is None:
        raise ConfigError(f"{path}.sender: address '{sender}' does not exist")
    if account.smtp is None:
        raise ConfigError(f"{path}.sender: address '{sender}' requires SMTP")


def resolve_notification_recipients(
    recipients: tuple[str, ...],
    addresses: Mapping[str, AddressConfig],
) -> tuple[str, ...]:
    """Replace ``account:<id>`` recipients with their configured email address."""

    resolved = []
    for recipient in recipients:
        if recipient.startswith("account:"):
            resolved.append(addresses[recipient.removeprefix("account:")].address)
        else:
            resolved.append(recipient)
    return tuple(resolved)


def _resolve_relative_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _resolve_optional_path(value: str | None, base_dir: Path) -> str | None:
    if value is None:
        return None
    return str(_resolve_relative_path(value, base_dir))


def _require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    if key not in data:
        raise ConfigError(f"{key}: table is required")
    return _ensure_mapping(data[key], key)


def _optional_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    if key not in data:
        return {}
    return _ensure_mapping(data[key], key)


def _ensure_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: must be a TOML table")
    return value


def _get_str(
    data: Mapping[str, Any],
    key: str,
    path: str,
    default: str | None = None,
) -> str:
    if key not in data:
        if default is not None:
            return default
        raise ConfigError(f"{path}: is required")
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: must be a non-empty string")
    return value


def _get_optional_str(data: Mapping[str, Any], key: str, path: str) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: must be a non-empty string")
    return value


def _get_bool(
    data: Mapping[str, Any],
    key: str,
    path: str,
    default: bool = False,
) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: must be true or false")
    return value


def _get_positive_int(
    data: Mapping[str, Any],
    key: str,
    path: str,
    default: int | None = None,
) -> int:
    if key not in data:
        if default is not None:
            return default
        raise ConfigError(f"{path}: is required")
    value = data[key]
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path}: must be a positive integer")
    return value
