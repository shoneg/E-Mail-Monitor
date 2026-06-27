from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock

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


def test_route_is_skipped_until_send_interval_has_elapsed(
    loaded_example_config,
    fixed_now,
) -> None:
    route = next(
        route for route in loaded_example_config.routes if route.id == "external-to-stalwart"
    )
    config = replace(loaded_example_config, routes=(replace(route, send_interval_seconds=300),))
    FakeImapClient.found_usernames = {"monitor-in@stalwart.example"}
    first_monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
        now_factory=lambda: fixed_now,
    )

    first_monitor.check()
    second_result = first_monitor.check()

    assert len(_sent_test_messages()) == 1
    assert second_result.route_results == ()
    assert second_result.skipped_route_ids == ("external-to-stalwart",)
    assert second_result.success is True

    due_monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
        now_factory=lambda: fixed_now + timedelta(seconds=300),
    )
    due_monitor.check()

    assert len(_sent_test_messages()) == 2


def test_force_bypasses_send_interval(loaded_example_config, fixed_now) -> None:
    route = next(
        route for route in loaded_example_config.routes if route.id == "external-to-stalwart"
    )
    config = replace(loaded_example_config, routes=(replace(route, send_interval_seconds=300),))
    FakeImapClient.found_usernames = {"monitor-in@stalwart.example"}
    monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
        now_factory=lambda: fixed_now,
    )

    monitor.check()
    result = monitor.check(force=True)

    assert len(_sent_test_messages()) == 2
    assert len(result.route_results) == 1
    assert result.skipped_route_ids == ()


def test_send_only_route_succeeds_without_imap_check(loaded_example_config) -> None:
    route = next(
        route for route in loaded_example_config.routes if route.id == "external-to-stalwart"
    )
    delivery = replace(route.deliveries[0], expect_at=())
    config = replace(loaded_example_config, routes=(replace(route, deliveries=(delivery,)),))
    monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check()

    assert result.success is True
    assert FakeImapClient.calls == []
    assert "verification disabled" in result.route_results[0].message


def test_due_routes_run_concurrently_and_results_keep_config_order(
    loaded_example_config,
) -> None:
    first, second = loaded_example_config.routes[:2]
    routes = tuple(
        replace(route, deliveries=(replace(route.deliveries[0], expect_at=()),))
        for route in (first, second)
    )
    config = replace(loaded_example_config, routes=routes)
    all_routes_started = Event()
    start_lock = Lock()
    started_route_count = 0

    class ConcurrentSmtpClient(FakeSmtpClient):
        def send_message(self, sender: str, recipients: list[str], message) -> None:
            nonlocal started_route_count
            if message.get("X-Mailflow-Monitor-Token"):
                with start_lock:
                    started_route_count += 1
                    if started_route_count == len(routes):
                        all_routes_started.set()
                if not all_routes_started.wait(timeout=1):
                    raise SmtpError("routes did not run concurrently")
            super().send_message(sender, recipients, message)

    monitor = MailflowMonitor(
        config,
        smtp_client_factory=ConcurrentSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check()

    assert result.success is True
    assert started_route_count == 2
    assert tuple(route.route_id for route in result.route_results) == (
        "external-to-stalwart",
        "stalwart-to-external",
    )


def _sequence(*values: float):
    iterator = iter(values)
    last = values[-1]

    def next_value() -> float:
        nonlocal last
        with suppress(StopIteration):
            last = next(iterator)
        return last

    return next_value


def _sent_test_messages() -> list[dict[str, object]]:
    return [item for item in FakeSmtpClient.sent if item["message"].get("X-Mailflow-Monitor-Token")]
