from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from mailflow_monitor.imap_client import ImapError
from mailflow_monitor.models import SmtpError
from mailflow_monitor.monitor import MailflowMonitor


class FakeSmtpClient:
    sent: list[dict[str, object]] = []
    fail_test_delivery = False

    def __init__(self, config) -> None:
        self.config = config

    def send_message(self, sender: str, recipients: list[str], message) -> None:
        if self.fail_test_delivery and message.get("X-Mailflow-Monitor-Token"):
            raise SmtpError("SMTP: forced test failure")
        self.sent.append({"sender": sender, "recipients": recipients, "message": message})


class FakeImapClient:
    found_usernames: set[str] = set()
    fail_usernames: set[str] = set()
    calls: list[dict[str, object]] = []

    def __init__(self, config) -> None:
        self.config = config

    def find_token(self, token: str, route_id: str, cleanup: bool = False) -> bool:
        self.calls.append(
            {
                "username": self.config.username,
                "token": token,
                "route_id": route_id,
                "cleanup": cleanup,
            }
        )
        if self.config.username in self.fail_usernames:
            raise ImapError("IMAP: forced failure")
        return self.config.username in self.found_usernames


def setup_function() -> None:
    FakeSmtpClient.sent = []
    FakeSmtpClient.fail_test_delivery = False
    FakeImapClient.found_usernames = set()
    FakeImapClient.fail_usernames = set()
    FakeImapClient.calls = []


def test_successful_direct_delivery_path(loaded_example_config) -> None:
    FakeImapClient.found_usernames = {"monitor-in@stalwart.example"}
    monitor = MailflowMonitor(
        loaded_example_config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check(route_ids=["external-to-stalwart"])

    assert result.success is True
    assert FakeSmtpClient.sent[0]["recipients"] == ["monitor-in@stalwart.example"]
    assert result.route_results[0].token


def test_successful_alias_forwarding_path_to_differs_from_expect_at(loaded_example_config) -> None:
    FakeImapClient.found_usernames = {"monitor-target@example-external.net"}
    monitor = MailflowMonitor(
        loaded_example_config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check(route_ids=["stalwart-via-anonaddy"])

    assert result.success is True
    assert FakeSmtpClient.sent[0]["recipients"] == ["some-alias@anonaddy.example"]
    assert FakeImapClient.calls[0]["username"] == "monitor-target@example-external.net"


def test_timeout_waiting_for_delivery(loaded_example_config) -> None:
    route = next(
        route for route in loaded_example_config.routes if route.id == "external-to-stalwart"
    )
    short_route = replace(route, timeout_seconds=1, poll_interval_seconds=1)
    config = replace(loaded_example_config, routes=(short_route,))
    monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
        monotonic=_sequence(0, 2),
        sleep=lambda seconds: None,
    )

    result = monitor.check()

    assert result.success is False
    assert result.route_results[0].error_class == "DeliveryTimeoutError"


def test_smtp_error_marks_route_failed(loaded_example_config) -> None:
    FakeSmtpClient.fail_test_delivery = True
    monitor = MailflowMonitor(
        loaded_example_config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check(route_ids=["external-to-stalwart"])

    assert result.success is False
    assert result.route_results[0].error_class == "SmtpError"


def test_imap_error_marks_route_failed(loaded_example_config) -> None:
    FakeImapClient.fail_usernames = {"monitor-in@stalwart.example"}
    monitor = MailflowMonitor(
        loaded_example_config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check(route_ids=["external-to-stalwart"])

    assert result.success is False
    assert result.route_results[0].error_class == "ImapError"


def test_monitor_state_file_is_updated(loaded_example_config) -> None:
    FakeImapClient.found_usernames = {"monitor-in@stalwart.example"}
    monitor = MailflowMonitor(
        loaded_example_config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    monitor.check(route_ids=["external-to-stalwart"])

    assert Path(loaded_example_config.monitor.state_file).exists()


def _sequence(*values: float):
    iterator = iter(values)
    last = values[-1]

    def next_value() -> float:
        nonlocal last
        with suppress(StopIteration):
            last = next(iterator)
        return last

    return next_value
