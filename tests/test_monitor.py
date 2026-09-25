from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock

from mailflow_monitor.imap_client import ImapError
from mailflow_monitor.models import SmtpError
from mailflow_monitor.monitor import MailflowMonitor
from mailflow_monitor.state import MonitorState


class FakeSmtpClient:
    sent: list[dict[str, object]] = []
    fail_test_delivery = False

    def __init__(self, config) -> None:
        """Store account settings for the in-memory client double.

        Args:
            config: Connection settings supplied by the monitor; no network connection is opened.

        Returns:
            None.
        """
        self.config = config

    def send_message(self, sender: str, recipients: list[str], message) -> None:
        """Record outgoing mail or simulate a configured test-message failure.

        Args:
            sender: Envelope sender address to record.
            recipients: Envelope recipient addresses to record.
            message: EmailMessage provided by the code under test.

        Returns:
            None.

        Raises:
            SmtpError: Test delivery failure is enabled and the message has a token header.
        """
        if self.fail_test_delivery and message.get("X-Mailflow-Monitor-Token"):
            raise SmtpError("SMTP: forced test failure")
        self.sent.append({"sender": sender, "recipients": recipients, "message": message})


class FakeImapClient:
    found_usernames: set[str] = set()
    fail_usernames: set[str] = set()
    calls: list[dict[str, object]] = []

    def __init__(self, config) -> None:
        """Store account settings for the in-memory client double.

        Args:
            config: Connection settings supplied by the monitor; no network connection is opened.

        Returns:
            None.
        """
        self.config = config

    def find_token(self, token: str, route_id: str, cleanup: bool = False) -> bool:
        """Record a lookup and simulate account-specific matching or failure.

        Args:
            token: Delivery token supplied by the monitor.
            route_id: Route ID supplied for matching.
            cleanup: Whether this call requests deletion instead of read-only verification.

        Returns:
            True if the account username belongs to found_usernames, otherwise False.

        Raises:
            ImapError: The account username belongs to fail_usernames.
        """
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
    """Reset shared client-double recordings and failure switches before each test.

    Returns:
        None.
    """
    FakeSmtpClient.sent = []
    FakeSmtpClient.fail_test_delivery = False
    FakeImapClient.found_usernames = set()
    FakeImapClient.fail_usernames = set()
    FakeImapClient.calls = []


def test_successful_direct_delivery_path(loaded_example_config) -> None:
    """Verify direct delivery sends to and checks the configured destination.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify alias delivery sends to the alias but checks the forwarding destination.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify an absent token becomes a delivery timeout when the fake deadline expires.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify SMTP failures become failed route results with the original error class.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify permanent IMAP errors fail the route instead of being retried.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify completing a route check creates the configured persistent state file.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify saved health is reused until the send interval permits a new message.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify a forced run sends again even when its normal interval has not elapsed.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
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
    """Verify SMTP acceptance is sufficient when no verification account is configured.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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


def test_each_delivery_uses_a_distinct_message_and_token(loaded_example_config) -> None:
    """Verify each route delivery gets its own recipient, message, and token.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
    route = next(
        route for route in loaded_example_config.routes if route.id == "external-to-stalwart"
    )
    second_delivery = replace(route.deliveries[0], to="anonaddy_alias")
    route = replace(route, deliveries=(route.deliveries[0], second_delivery))
    config = replace(loaded_example_config, routes=(route,))
    FakeImapClient.found_usernames = {"monitor-in@stalwart.example"}
    monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
    )

    result = monitor.check()

    sent_messages = _sent_test_messages()
    sent_tokens = tuple(str(item["message"]["X-Mailflow-Monitor-Token"]) for item in sent_messages)
    assert result.success is True
    assert [item["recipients"] for item in sent_messages] == [
        ["monitor-in@stalwart.example"],
        ["some-alias@anonaddy.example"],
    ]
    assert len(set(sent_tokens)) == 2
    assert result.route_results[0].delivery_tokens == sent_tokens


def test_one_delivery_cannot_satisfy_another_delivery_check(loaded_example_config) -> None:
    """Verify one arriving message cannot satisfy two deliveries to the same account.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
    route = next(
        route for route in loaded_example_config.routes if route.id == "external-to-stalwart"
    )
    second_delivery = replace(route.deliveries[0], to="anonaddy_alias")
    route = replace(
        route,
        deliveries=(route.deliveries[0], second_delivery),
        timeout_seconds=1,
        poll_interval_seconds=1,
    )
    config = replace(loaded_example_config, routes=(route,))

    class OnlyFirstDeliveryArrives(FakeImapClient):
        def find_token(self, token: str, route_id: str, cleanup: bool = False) -> bool:
            """Record the lookup and recognize only the first sent test message.

            Args:
                token: Delivery token supplied by the monitor.
                route_id: Route ID supplied for matching.
                cleanup: Whether this call requests deletion instead of read-only verification.

            Returns:
                True only if the supplied token matches the first recorded test message.
            """
            self.calls.append(
                {
                    "username": self.config.username,
                    "token": token,
                    "route_id": route_id,
                    "cleanup": cleanup,
                }
            )
            first_message = _sent_test_messages()[0]["message"]
            return token == first_message["X-Mailflow-Monitor-Token"]

    monitor = MailflowMonitor(
        config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=OnlyFirstDeliveryArrives,
        monotonic=_sequence(0, 2),
        sleep=lambda seconds: None,
    )

    result = monitor.check()

    assert result.success is False
    assert result.route_results[0].error_class == "DeliveryTimeoutError"
    assert len({call["token"] for call in FakeImapClient.calls}) == 2


def test_successful_partial_run_does_not_clear_global_incident(
    loaded_example_config,
    fixed_now,
) -> None:
    """Verify a healthy route subset neither clears global failure nor sends recovery.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    incident_started_at = fixed_now - timedelta(hours=1)
    FakeImapClient.found_usernames = {"monitor-in@stalwart.example"}
    monitor = MailflowMonitor(
        loaded_example_config,
        smtp_client_factory=FakeSmtpClient,
        imap_client_factory=FakeImapClient,
        now_factory=lambda: fixed_now,
    )
    monitor.state_store.save(
        MonitorState(
            last_run_success=False,
            incident_started_at=incident_started_at,
        )
    )

    result = monitor.check(route_ids=["external-to-stalwart"])
    state = monitor.state_store.load()

    assert result.success is True
    assert state.last_run_success is False
    assert state.incident_started_at == incident_started_at
    assert state.last_recovery_at is None
    assert state.last_aliveness_at is None
    assert len(FakeSmtpClient.sent) == 1


def test_due_routes_run_concurrently_and_results_keep_config_order(
    loaded_example_config,
) -> None:
    """Verify routes overlap in execution while their results retain configuration order.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
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
            """Require all route sends to start before recording each message.

            Args:
                sender: Envelope sender address to record.
                recipients: Envelope recipient addresses to record.
                message: EmailMessage provided by the code under test.

            Returns:
                None.

            Raises:
                SmtpError: Not all routes reach the synchronization event within one second.
            """
            nonlocal started_route_count
            if message.get("X-Mailflow-Monitor-Token"):
                with start_lock:
                    started_route_count += 1
                    if started_route_count == len(routes):
                        all_routes_started.set()
                # Waiting outside the lock lets the other worker reach the event;
                # serial execution times out here and makes the route fail.
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
    """Build a deterministic clock that repeats its last value after exhaustion.

    Args:
        *values: Non-empty sequence of seconds to return on successive calls.

    Returns:
        Zero-argument callable returning the next value or the last value indefinitely.
    """
    iterator = iter(values)
    last = values[-1]

    def next_value() -> float:
        """Advance the fake clock once, retaining the last value when exhausted.

        Returns:
            Next configured time value, or the previous value after exhaustion.
        """
        nonlocal last
        with suppress(StopIteration):
            last = next(iterator)
        return last

    return next_value


def _sent_test_messages() -> list[dict[str, object]]:
    """Select recorded SMTP messages carrying the monitor token header.

    Returns:
        Recorded test-message dictionaries, excluding incident and aliveness emails.
    """
    return [item for item in FakeSmtpClient.sent if item["message"].get("X-Mailflow-Monitor-Token")]


def test_transient_imap_failure_retries_same_token_without_resending(loaded_example_config) -> None:
    """Verify transient IMAP failures poll the original token after the configured delay.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
    from mailflow_monitor.models import TransientImapError

    class FlakyImap(FakeImapClient):
        tokens: list[str] = []

        def find_token(self, token, route_id, cleanup=False):
            """Record tokens and fail the first lookup to exercise transient retries.

            Args:
                token: Delivery token supplied by the monitor.
                route_id: Route ID supplied for matching.
                cleanup: Whether this call requests deletion instead of read-only verification.

            Returns:
                True on subsequent lookups.

            Raises:
                TransientImapError: This is the first lookup.
            """
            self.tokens.append(token)
            if len(self.tokens) == 1:
                raise TransientImapError("network timeout")
            return True

    sleeps = []
    monitor = MailflowMonitor(
        loaded_example_config,
        FakeSmtpClient,
        FlakyImap,
        monotonic=_sequence(0, 30),
        sleep=sleeps.append,
    )
    result = monitor.check(route_ids=["external-to-stalwart"])
    assert result.success
    assert len(_sent_test_messages()) == 1
    assert len(FlakyImap.tokens) == 2
    assert len(set(FlakyImap.tokens)) == 1
    assert sleeps == [30]


def test_persistent_network_failure_reports_unverified_delivery(loaded_example_config) -> None:
    """Verify network failure at the deadline reports unavailable verification.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
    from mailflow_monitor.models import TransientImapError

    class OfflineImap(FakeImapClient):
        def find_token(self, token, route_id, cleanup=False):
            """Simulate a persistent network outage for every lookup.

            Args:
                token: Delivery token supplied by the monitor.
                route_id: Route ID supplied for matching.
                cleanup: Whether this call requests deletion instead of read-only verification.

            Returns:
                Never returns normally.

            Raises:
                TransientImapError: Always raised to simulate an offline server.
            """
            raise TransientImapError("network timeout")

    monitor = MailflowMonitor(
        loaded_example_config,
        FakeSmtpClient,
        OfflineImap,
        monotonic=_sequence(0, 30, 901),
        sleep=lambda seconds: None,
    )
    result = monitor.check(route_ids=["external-to-stalwart"])
    assert not result.success
    assert result.route_results[0].error_class == "ImapError"
    assert "could not be verified" in result.route_results[0].message


def test_cleanup_failure_keeps_delivery_healthy_and_retries_on_skipped_runs(
    loaded_example_config,
    fixed_now,
) -> None:
    """Verify cleanup survives restart and retries without sending another test message.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    config = replace(
        loaded_example_config,
        routes=(loaded_example_config.routes[0],),
        monitor=replace(loaded_example_config.monitor, cleanup_received_test_messages=True),
    )

    class CleanupFails(FakeImapClient):
        cleanups = 0

        def find_token(self, token, route_id, cleanup=False):
            """Accept delivery verification but count and reject cleanup attempts.

            Args:
                token: Delivery token supplied by the monitor.
                route_id: Route ID supplied for matching.
                cleanup: Whether this call requests deletion instead of read-only verification.

            Returns:
                True for read-only verification.

            Raises:
                ImapError: Cleanup was requested.
            """
            if cleanup:
                type(self).cleanups += 1
                raise ImapError("cannot mark message deleted")
            return True

    monitor = MailflowMonitor(config, FakeSmtpClient, CleanupFails, now_factory=lambda: fixed_now)
    assert monitor.check().success
    state = monitor.state_store.load()
    assert state.incident_started_at is None
    assert state.cleanup_pending[0].attempts == 1
    token = state.cleanup_pending[0].token
    assert not any("failure" in item["message"]["Subject"] for item in FakeSmtpClient.sent)

    # Simulate a fresh process, with no new delivery due yet.
    restarted = MailflowMonitor(
        config,
        FakeSmtpClient,
        CleanupFails,
        now_factory=lambda: fixed_now + timedelta(seconds=60),
    )
    result = restarted.check()
    assert result.success and result.route_results == ()
    assert len(_sent_test_messages()) == 1
    assert CleanupFails.cleanups == 2
    assert restarted.state_store.load().cleanup_pending[0].token == token
    assert restarted.state_store.load().cleanup_pending[0].attempts == 2


def test_verified_message_is_cleaned_even_if_another_delivery_times_out(loaded_example_config):
    """Verify partial route failure preserves cleanup work for the verified delivery.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.

    Returns:
        None; assertions verify the expected behavior.
    """
    route = loaded_example_config.routes[0]
    route = replace(route, deliveries=(route.deliveries[0], route.deliveries[0]))
    config = replace(
        loaded_example_config,
        routes=(route,),
        monitor=replace(loaded_example_config.monitor, cleanup_received_test_messages=True),
    )

    class FirstArrives(FakeImapClient):
        cleaned = []

        def find_token(self, token, route_id, cleanup=False):
            """Record cleanup tokens and recognize only the first delivery.

            Args:
                token: Delivery token supplied by the monitor.
                route_id: Route ID supplied for matching.
                cleanup: Whether this call requests deletion instead of read-only verification.

            Returns:
                True if the supplied token belongs to the first sent test message.
            """
            if cleanup:
                self.cleaned.append(token)
            return token == _sent_test_messages()[0]["message"]["X-Mailflow-Monitor-Token"]

    monitor = MailflowMonitor(
        config,
        FakeSmtpClient,
        FirstArrives,
        monotonic=_sequence(0, 901),
        sleep=lambda seconds: None,
    )
    result = monitor.check()
    assert not result.success
    assert FirstArrives.cleaned == [result.route_results[0].delivery_tokens[0]]
    assert monitor.state_store.load().cleanup_history == {"stalwart_recipient": [True]}
