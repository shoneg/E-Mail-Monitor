"""Data models for configuration, runtime state, and check results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MailflowError(Exception):
    """Base class for expected mailflow monitor errors."""


class ConfigError(MailflowError):
    """The configuration is invalid or incomplete."""


class SmtpError(MailflowError):
    """SMTP delivery failed."""


class ImapError(MailflowError):
    """IMAP access or message search failed."""


class DeliveryTimeoutError(MailflowError):
    """An expected test message was not found before the timeout expired."""


class NotificationError(MailflowError):
    """An alert, recovery, or aliveness message could not be sent."""


class TlsMode(StrEnum):
    """Supported transport security modes for SMTP and IMAP."""

    SSL = "ssl"
    STARTTLS = "starttls"
    PLAIN = "plain"


@dataclass(frozen=True)
class SmtpConfig:
    """SMTP connection settings for an address."""

    host: str
    port: int
    tls_mode: TlsMode
    username: str
    password: str
    allow_insecure_plaintext: bool = False
    ca_file: str | None = None


@dataclass(frozen=True)
class ImapConfig:
    """IMAP connection settings for an address."""

    host: str
    port: int
    tls_mode: TlsMode
    username: str
    password: str
    mailboxes: tuple[str, ...] = ("INBOX",)
    allow_insecure_plaintext: bool = False
    ca_file: str | None = None


@dataclass(frozen=True)
class AddressConfig:
    """Named email address with optional SMTP and IMAP capabilities."""

    id: str
    address: str
    smtp: SmtpConfig | None = None
    imap: ImapConfig | None = None


@dataclass(frozen=True)
class DeliveryConfig:
    """Actual delivery target and expected verification mailboxes."""

    to: str
    expect_at: tuple[str, ...]


@dataclass(frozen=True)
class RouteConfig:
    """A mail path that can be checked."""

    id: str
    description: str
    from_id: str
    deliveries: tuple[DeliveryConfig, ...]
    timeout_seconds: int
    poll_interval_seconds: int
    send_interval_seconds: int | None = None


@dataclass(frozen=True)
class MonitorConfig:
    """Global monitor settings."""

    state_file: str
    lock_file: str
    log_level: str
    default_timeout_seconds: int
    default_poll_interval_seconds: int
    cleanup_received_test_messages: bool = False
    default_send_interval_seconds: int | None = None


@dataclass(frozen=True)
class AlertsConfig:
    """Rules for incident and recovery notifications."""

    enabled: bool
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    repeat_after_seconds: int = 21600
    send_recovery_message: bool = True


@dataclass(frozen=True)
class AlivenessConfig:
    """Rules for periodic aliveness messages."""

    enabled: bool
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    interval_seconds: int = 604800
    only_when_healthy: bool = True


@dataclass(frozen=True)
class NotificationsConfig:
    """Notification settings for the monitor."""

    alerts: AlertsConfig = field(default_factory=lambda: AlertsConfig(enabled=False))
    aliveness: AlivenessConfig = field(default_factory=lambda: AlivenessConfig(enabled=False))


@dataclass(frozen=True)
class AppConfig:
    """Fully validated application configuration."""

    config_path: str
    monitor: MonitorConfig
    addresses: dict[str, AddressConfig]
    routes: tuple[RouteConfig, ...]
    notifications: NotificationsConfig


@dataclass(frozen=True)
class ExpectedMailbox:
    """A concrete IMAP account that must contain a route token."""

    address_id: str
    account: AddressConfig


@dataclass(frozen=True)
class RouteRunResult:
    """Result of one route check."""

    route_id: str
    success: bool
    token: str
    started_at: datetime
    finished_at: datetime
    message: str
    error_class: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """Overall result of a monitor run."""

    success: bool
    route_results: tuple[RouteRunResult, ...]
    notification_failed: bool = False
    skipped_route_ids: tuple[str, ...] = ()
