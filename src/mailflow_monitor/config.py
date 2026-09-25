"""Load and validate the TOML configuration file."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .logging_utils import normalize_log_level
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

LOG_LEVEL_ENV_VAR = "MAILFLOW_MONITOR_LOG_LEVEL"
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_config(path: str | Path = "config.toml") -> AppConfig:
    """Load TOML, expand environment values, and validate the configuration.

    Loads .env beside the TOML file without modifying the process environment.

    Args:
        path: TOML file path; defaults to config.toml in the current directory.

    Returns:
        Validated application settings with resolved filesystem paths.

    Raises:
        ConfigError: A file cannot be read, parsing fails, or configuration validation fails.
    """

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
    # Expand after TOML parsing so quotes/newlines in secrets stay values, not syntax.
    # Merge in this order so process variables override local .env assignments.
    environment = {**_load_dotenv(config_path.with_name(".env")), **os.environ}
    expanded_data = _expand_environment_values(data, environment)
    return _parse_app_config(expanded_data, config_path, environment)


def _load_dotenv(path: Path) -> dict[str, str]:
    """Read the supported .env assignment syntax without executing shell code.

    Blank lines, comments, an optional export prefix, and quoted values are supported. Later
    assignments replace earlier values; variable references are not expanded here.

    Args:
        path: Environment file to read as UTF-8.

    Returns:
        Variable names mapped to values, or an empty dict if the file is absent.

    Raises:
        ConfigError: The file cannot be read or an assignment is malformed.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"config: cannot read environment file: {path}") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export") and stripped[6:7].isspace():
            stripped = stripped[6:].lstrip()

        name, separator, raw_value = stripped.partition("=")
        name = name.strip()
        if not separator or ENV_NAME_PATTERN.fullmatch(name) is None:
            raise ConfigError(f"config: invalid .env entry at {path}:{line_number}")

        values[name] = _parse_dotenv_value(raw_value, path, line_number)
    return values


def _parse_dotenv_value(raw_value: str, path: Path, line_number: int) -> str:
    """Decode one .env value and remove permitted trailing comments.

    Args:
        raw_value: Text after the assignment separator, including optional quotes.
        path: Environment file path included in error messages.
        line_number: One-based source line number included in error messages.

    Returns:
        Decoded value; escapes are interpreted only inside double quotes.

    Raises:
        ConfigError: A quoted value is unterminated or has invalid trailing text.
    """
    value = raw_value.strip()
    if not value:
        return ""
    if value[0] not in {'"', "'"}:
        # Only whitespace-prefixed # starts an unquoted comment; embedded # is data.
        comment = re.search(r"\s+#", value)
        return value[: comment.start()].rstrip() if comment else value

    quote = value[0]
    parsed: list[str] = []
    escaped = False
    # Single quotes remain literal; unknown double-quoted escapes retain the backslash.
    escape_sequences = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    for index, character in enumerate(value[1:], start=1):
        if quote == '"' and escaped:
            parsed.append(escape_sequences.get(character, f"\\{character}"))
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character == quote:
            trailing = value[index + 1 :].strip()
            if trailing and not trailing.startswith("#"):
                break
            return "".join(parsed)
        parsed.append(character)

    raise ConfigError(f"config: invalid quoted .env value at {path}:{line_number}")


def _expand_environment_values(value: Any, environment: Mapping[str, str]) -> Any:
    """Recursively substitute environment references in configuration values.

    Args:
        value: Parsed TOML value, list, or table; table keys are left unchanged.
        environment: Environment values with process variables taking precedence over .env.

    Returns:
        Expanded structure, preserving non-string scalar values.

    Raises:
        ConfigError: A referenced environment variable is unset.
    """
    if isinstance(value, str):
        return _expand_environment_string(value, environment)
    if isinstance(value, list):
        return [_expand_environment_values(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment_values(item, environment) for key, item in value.items()}
    return value


def _expand_environment_string(text: str, environment: Mapping[str, str]) -> str:
    """Replace ${NAME} references in one string in a single pass.

    Args:
        text: Configuration string containing optional environment references.
        environment: Environment values with process variables taking precedence over .env.

    Returns:
        String with references replaced verbatim by their values.

    Raises:
        ConfigError: A referenced environment variable is unset.
    """
    def replace(match: re.Match[str]) -> str:
        """Resolve one regular-expression environment match.

        Args:
            match: Placeholder match whose first group contains the variable name.

        Returns:
            Environment value to insert without further expansion.

        Raises:
            ConfigError: The matched variable is unset.
        """
        name = match.group(1)
        try:
            return environment[name]
        except KeyError as exc:
            raise ConfigError(
                f"config: referenced environment variable ${name} is not set"
            ) from exc

    return ENV_VAR_PATTERN.sub(replace, text)


def _parse_app_config(
    data: Mapping[str, Any],
    config_path: Path,
    environment: Mapping[str, str],
) -> AppConfig:
    """Assemble validated sections and check route references.

    Args:
        data: Parsed configuration table to read.
        config_path: Absolute path of the source TOML file.
        environment: Environment values with process variables taking precedence over .env.

    Returns:
        Complete application configuration.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    base_dir = config_path.parent
    monitor = _parse_monitor(_require_mapping(data, "monitor"), base_dir, environment)
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


def _parse_monitor(
    data: Mapping[str, Any],
    base_dir: Path,
    environment: Mapping[str, str],
) -> MonitorConfig:
    """Parse global timing, cleanup, logging, and state-file settings.

    Args:
        data: Parsed configuration table to read.
        base_dir: Directory containing the configuration file; base for relative paths.
        environment: Environment values with process variables taking precedence over .env.

    Returns:
        Monitor settings with defaults and the environment log-level override applied.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    default_send_interval = _get_optional_positive_int(
        data,
        "default_send_interval_seconds",
        "monitor.default_send_interval_seconds",
    )
    if default_poll > default_timeout:
        raise ConfigError(
            "monitor.default_poll_interval_seconds: must not be greater than "
            "monitor.default_timeout_seconds"
        )
    configured_log_level = _get_str(data, "log_level", "monitor.log_level", default="INFO")
    log_level = environment.get(LOG_LEVEL_ENV_VAR, configured_log_level)
    try:
        log_level = normalize_log_level(log_level)
    except ValueError as exc:
        source = "monitor.log_level"
        if LOG_LEVEL_ENV_VAR in environment:
            source = LOG_LEVEL_ENV_VAR
        raise ConfigError(f"{source}: {exc}") from exc
    cleanup_settings = {
        key: _get_positive_int(data, key, f"monitor.{key}", default)
        for key, default in (
            ("cleanup_retry_count", 10),
            ("cleanup_retry_interval_seconds", 60),
            ("cleanup_failure_threshold", 10),
            ("cleanup_failure_window", 15),
        )
    }
    if cleanup_settings["cleanup_failure_threshold"] > cleanup_settings["cleanup_failure_window"]:
        raise ConfigError(
            "monitor.cleanup_failure_threshold: must not exceed cleanup_failure_window"
        )
    return MonitorConfig(
        cleanup_retry_count=cleanup_settings["cleanup_retry_count"],
        cleanup_retry_interval_seconds=cleanup_settings["cleanup_retry_interval_seconds"],
        cleanup_failure_threshold=cleanup_settings["cleanup_failure_threshold"],
        cleanup_failure_window=cleanup_settings["cleanup_failure_window"],
        state_file=str(state_file),
        lock_file=str(lock_file),
        log_level=log_level,
        default_timeout_seconds=default_timeout,
        default_poll_interval_seconds=default_poll,
        default_send_interval_seconds=default_send_interval,
        cleanup_received_test_messages=_get_bool(
            data,
            "cleanup_received_test_messages",
            "monitor.cleanup_received_test_messages",
            default=False,
        ),
    )


def _parse_addresses(data: Mapping[str, Any], base_dir: Path) -> dict[str, AddressConfig]:
    """Build account definitions with optional SMTP and IMAP capabilities.

    Args:
        data: Address tables indexed by account ID.
        base_dir: Directory containing the configuration file; base for relative paths.

    Returns:
        Non-empty mapping of account IDs to connection and address settings.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Parse and validate one account's SMTP connection settings.

    Args:
        data: Parsed configuration table to read.
        path: Configuration field path used in validation errors.
        base_dir: Directory containing the configuration file; base for relative paths.

    Returns:
        SMTP settings with any custom CA path resolved.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Parse and validate one account's IMAP connection settings.

    Args:
        data: Parsed configuration table to read.
        path: Configuration field path used in validation errors.
        base_dir: Directory containing the configuration file; base for relative paths.

    Returns:
        IMAP settings with any custom CA path resolved.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Validate the configured transport security mode.

    Args:
        data: Parsed configuration table to read.
        path: Parent configuration path for the tls_mode field.

    Returns:
        The matching ssl, starttls, or plain enum member.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    value = _get_str(data, "tls_mode", f"{path}.tls_mode")
    try:
        return TlsMode(value)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in TlsMode)
        raise ConfigError(f"{path}.tls_mode: invalid value '{value}', allowed: {allowed}") from exc


def _validate_tls_mode(tls_mode: TlsMode, allow_plain: bool, path: str) -> None:
    """Require explicit opt-in before allowing plaintext connections.

    Args:
        tls_mode: Selected transport security mode.
        allow_plain: Whether insecure plaintext is explicitly permitted.
        path: Configuration field path used in validation errors.

    Returns:
        None.

    Raises:
        ConfigError: Plaintext mode was selected without opt-in.
    """
    if tls_mode is TlsMode.PLAIN and not allow_plain:
        raise ConfigError(
            f"{path}.tls_mode: 'plain' is only allowed with allow_insecure_plaintext = true"
        )


def _parse_routes(raw_routes: Any, monitor: MonitorConfig) -> list[RouteConfig]:
    """Parse route definitions, apply timing defaults, and enforce unique IDs.

    Args:
        raw_routes: Unvalidated TOML routes array.
        monitor: Global defaults for route timing.

    Returns:
        Non-empty route list in configuration order; account references are checked separately.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
        send_interval = _get_optional_positive_int(
            table,
            "send_interval_seconds",
            f"{path}.send_interval_seconds",
            default=monitor.default_send_interval_seconds,
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
                send_interval_seconds=send_interval,
            )
        )
    return routes


def _parse_deliveries(raw: Any, route_path: str) -> list[DeliveryConfig]:
    """Parse route targets and optional verification account references.

    Args:
        raw: Unvalidated deliveries array.
        route_path: Parent route path for validation errors.

    Returns:
        Non-empty delivery list; an empty expect_at tuple enables send-only delivery.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{route_path}.deliveries: at least one delivery is required")
    deliveries: list[DeliveryConfig] = []
    for index, raw_delivery in enumerate(raw):
        path = f"{route_path}.deliveries[{index}]"
        table = _ensure_mapping(raw_delivery, path)
        expect_raw = table.get("expect_at", [])
        if not isinstance(expect_raw, list):
            raise ConfigError(f"{path}.expect_at: must be a list")
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
    """Check route account references and required SMTP/IMAP capabilities.

    Args:
        routes: Parsed routes to validate.
        addresses: Configured accounts indexed by address ID.

    Returns:
        None.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Parse incident and aliveness notification policies.

    Args:
        data: Parsed configuration table to read.
        addresses: Configured accounts indexed by address ID.

    Returns:
        Notification settings with absent sections disabled.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    alerts = _parse_alerts(_optional_mapping(data, "alerts"), addresses)
    aliveness = _parse_aliveness(_optional_mapping(data, "aliveness"), addresses)
    return NotificationsConfig(alerts=alerts, aliveness=aliveness)


def _parse_alerts(data: Mapping[str, Any], addresses: Mapping[str, AddressConfig]) -> AlertsConfig:
    """Parse incident alerts and validate enabled notification requirements.

    Args:
        data: Parsed configuration table to read.
        addresses: Configured accounts indexed by address ID.

    Returns:
        Alert, repeat-interval, and recovery settings.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Parse aliveness settings and validate enabled notification requirements.

    Args:
        data: Parsed configuration table to read.
        addresses: Configured accounts indexed by address ID.

    Returns:
        Aliveness interval and health-policy settings.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Validate literal email recipients and account:<id> references.

    Args:
        raw: Unvalidated recipient list; an empty list is allowed.
        path: Configuration field path used in validation errors.
        addresses: Configured accounts indexed by address ID.

    Returns:
        Validated recipient strings in input order, with references still unresolved.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Require an existing SMTP-capable sender for enabled notifications.

    Args:
        enabled: Whether this notification policy is enabled.
        sender: Sender account ID, or None if omitted.
        addresses: Configured accounts indexed by address ID.
        path: Configuration field path used in validation errors.

    Returns:
        None.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Resolve account:<id> recipients to their configured email addresses.

    Args:
        recipients: Literal email addresses or validated account references.
        addresses: Configured accounts indexed by address ID.

    Returns:
        Email addresses in input order, retaining any duplicates.

    Raises:
        KeyError: An account reference is absent from addresses.
    """

    resolved = []
    for recipient in recipients:
        if recipient.startswith("account:"):
            resolved.append(addresses[recipient.removeprefix("account:")].address)
        else:
            resolved.append(recipient)
    return tuple(resolved)


def _resolve_relative_path(value: str, base_dir: Path) -> Path:
    """Expand a home-directory prefix and resolve a configuration path.

    Args:
        value: Absolute or relative filesystem path.
        base_dir: Directory containing the configuration file; base for relative paths.

    Returns:
        Absolute resolved path; the target need not exist.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _resolve_optional_path(value: str | None, base_dir: Path) -> str | None:
    """Resolve a filesystem path only when a value is present.

    Args:
        value: Configured filesystem path, or None.
        base_dir: Directory containing the configuration file; base for relative paths.

    Returns:
        Absolute path string, or None if no path was supplied.
    """
    if value is None:
        return None
    return str(_resolve_relative_path(value, base_dir))


def _require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Read a configuration subtable and validate its shape.

    Args:
        data: Parsed configuration table to read.
        key: Subtable name to look up and include in error messages.

    Returns:
        The required table.

    Raises:
        ConfigError: The table is missing or has an invalid type.
    """
    if key not in data:
        raise ConfigError(f"{key}: table is required")
    return _ensure_mapping(data[key], key)


def _optional_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Read a configuration subtable and validate its shape.

    Args:
        data: Parsed configuration table to read.
        key: Subtable name to look up and include in error messages.

    Returns:
        The existing table, or an empty dict if absent.

    Raises:
        ConfigError: The value is not a table.
    """
    if key not in data:
        return {}
    return _ensure_mapping(data[key], key)


def _ensure_mapping(value: Any, path: str) -> Mapping[str, Any]:
    """Require a TOML table at the given configuration path.

    Args:
        value: Unvalidated parsed value.
        path: Configuration field path used in validation errors.

    Returns:
        The original dictionary.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: must be a TOML table")
    return value


def _get_str(
    data: Mapping[str, Any],
    key: str,
    path: str,
    default: str | None = None,
) -> str:
    """Read a required non-empty string or its supplied default.

    Args:
        data: Parsed configuration table to read.
        key: Field name to look up.
        path: Configuration field path used in validation errors.
        default: Fallback for a missing key; None makes the key required.

    Returns:
        Validated string or the supplied default.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    if key not in data:
        if default is not None:
            return default
        raise ConfigError(f"{path}: is required")
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: must be a non-empty string")
    return value


def _get_optional_str(data: Mapping[str, Any], key: str, path: str) -> str | None:
    """Read an optional non-empty string.

    Args:
        data: Parsed configuration table to read.
        key: Field name to look up.
        path: Configuration field path used in validation errors.

    Returns:
        Validated string, or None for an absent or None value.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Read a strict boolean without coercing other value types.

    Args:
        data: Parsed configuration table to read.
        key: Field name to look up.
        path: Configuration field path used in validation errors.
        default: Fallback for an absent key; defaults to False.

    Returns:
        Configured boolean, or the default for an absent key.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
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
    """Read a positive integer, explicitly rejecting boolean values.

    Args:
        data: Parsed configuration table to read.
        key: Field name to look up.
        path: Configuration field path used in validation errors.
        default: Fallback for a missing key; None makes the key required.

    Returns:
        Positive integer or the supplied default.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    if key not in data:
        if default is not None:
            return default
        raise ConfigError(f"{path}: is required")
    value = data[key]
    # bool subclasses int in Python, but TOML booleans are not valid numeric settings.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path}: must be a positive integer")
    return value


def _get_optional_positive_int(
    data: Mapping[str, Any],
    key: str,
    path: str,
    default: int | None = None,
) -> int | None:
    """Read an optional positive integer, explicitly rejecting booleans.

    Args:
        data: Parsed configuration table to read.
        key: Field name to look up.
        path: Configuration field path used in validation errors.
        default: Fallback for an absent key; defaults to None.

    Returns:
        Positive integer, or the default (possibly None) for an absent key.

    Raises:
        ConfigError: A required field, value, or referenced account is invalid.
    """
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{path}: must be a positive integer")
    return value
