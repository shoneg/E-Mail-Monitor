"""Core mailflow monitor orchestration."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from .cleanup import CleanupManager
from .imap_client import ImapClient
from .models import (
    AppConfig,
    CheckResult,
    ConfigError,
    DeliveryConfig,
    DeliveryTimeoutError,
    ImapError,
    RouteConfig,
    RouteRunResult,
    SmtpError,
    TransientImapError,
)
from .notifications import NotificationManager
from .smtp_client import SmtpClient
from .state import CleanupTask, FileLock, MonitorState, RouteState, StateStore, format_dt, utc_now

LOGGER = logging.getLogger(__name__)


class MailflowMonitor:
    """Run configured mailflow checks and update persistent state."""

    def __init__(
        self,
        config: AppConfig,
        smtp_client_factory: type[SmtpClient] = SmtpClient,
        imap_client_factory: type[ImapClient] = ImapClient,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        """Configure monitoring dependencies without starting network or file operations.

        Args:
            config: Validated application settings.
            smtp_client_factory: SMTP client constructor shared by checks and notifications.
            imap_client_factory: IMAP client constructor used for verification and cleanup.
            sleep: Callable accepting seconds to wait between polling passes.
            monotonic: Clock returning monotonic seconds for delivery deadlines.
            now_factory: Clock returning timezone-aware times for persisted state and message
                dates.

        Returns:
            None.
        """
        self.config = config
        self.smtp_client_factory = smtp_client_factory
        self.imap_client_factory = imap_client_factory
        self.sleep = sleep
        self.monotonic = monotonic
        self.now_factory = now_factory
        self.state_store = StateStore(config.monitor.state_file)

    def check(
        self,
        route_ids: Iterable[str] | None = None,
        force: bool = False,
    ) -> CheckResult:
        """Run due routes under a process lock, notify, clean up, and persist state.

        Successful partial runs preserve global incident state. Expected delivery failures become
        route results; notification failures are logged and reported separately.

        Args:
            route_ids: Route IDs to select in the supplied order; None selects all configured
                routes.
            force: Whether to bypass route send intervals; defaults to False.

        Returns:
            Selected-route health (including saved outcomes for skipped routes), executed route
            results, skipped IDs, and a separate notification-failure flag.

        Raises:
            ConfigError: A route ID is unknown, state cannot be loaded, or another run holds the
                lock.
            OSError: Lock-file access or state persistence fails.
        """

        selected_routes = self._select_routes(route_ids)
        selected_route_ids = tuple(route.id for route in selected_routes)
        all_route_ids = tuple(route.id for route in self.config.routes)
        is_full_run = set(selected_route_ids) == set(all_route_ids)
        with FileLock(self.config.monitor.lock_file):
            state = self.state_store.load()
            now = self.now_factory()
            due_routes = tuple(
                route for route in selected_routes if force or self._is_route_due(route, state, now)
            )
            skipped_route_ids = tuple(
                route.id for route in selected_routes if route not in due_routes
            )
            results = self._run_routes(due_routes)
            self._update_state_from_results(state, results)
            for result in results:
                for address_id, token in result.cleanup_requests:
                    state.cleanup_pending.append(CleanupTask(address_id, result.route_id, token))

            # Skipped routes keep their last known health; an empty batch of executed
            # checks must not implicitly turn a previously failing run healthy.
            run_success = all(
                state.routes.get(route_id) is not None
                and state.routes[route_id].last_success is True
                for route_id in selected_route_ids
            )
            # A failed subset can establish an incident, but a healthy subset cannot
            # establish recovery for routes that were not selected.
            state.last_run_at = now
            if is_full_run or not run_success:
                state.last_run_success = run_success

            notification_failed = False
            if is_full_run or not run_success:
                failure_details = _state_failure_details(state, all_route_ids)
                try:
                    NotificationManager(self.config, self.smtp_client_factory).handle_after_run(
                        state,
                        run_success,
                        failure_details,
                        now,
                    )
                except Exception as exc:
                    notification_failed = True
                    LOGGER.error("Notification delivery failed: %s", exc)

            # Persist verified deliveries and pending cleanup before any further network I/O.
            self.state_store.save(state)
            CleanupManager(self.config, self.imap_client_factory, self.now_factory).process(state)
            try:
                notifications = NotificationManager(self.config, self.smtp_client_factory)
                notifications.maybe_send_cleanup_warning(state, self.now_factory())
            except Exception as exc:
                notification_failed = True
                LOGGER.error("Cleanup notification delivery failed: %s", exc)
            self.state_store.save(state)
            return CheckResult(
                success=run_success,
                route_results=results,
                notification_failed=notification_failed,
                skipped_route_ids=skipped_route_ids,
            )

    def _run_routes(self, routes: tuple[RouteConfig, ...]) -> tuple[RouteRunResult, ...]:
        """Run due routes concurrently while preserving their input order.

        Args:
            routes: Due route definitions; may be empty.

        Returns:
            One result per route in input order, regardless of worker completion order.
        """
        if len(routes) < 2:
            return tuple(self._run_route(route) for route in routes)

        with ThreadPoolExecutor(
            max_workers=len(routes),
            thread_name_prefix="mailflow-route",
        ) as executor:
            # map preserves input order; workers return results and the caller alone
            # merges them into shared persistent state after all workers finish.
            return tuple(executor.map(self._run_route, routes))

    @staticmethod
    def _is_route_due(route: RouteConfig, state: MonitorState, now: datetime) -> bool:
        """Check whether a route has no interval restriction or its interval has elapsed.

        Args:
            route: Route whose configured send interval is checked.
            state: Saved route timestamps.
            now: Timezone-aware current time to compare with the last send attempt.

        Returns:
            True if the route has never run, has no interval, or is due again.
        """
        if route.send_interval_seconds is None:
            return True
        route_state = state.routes.get(route.id)
        if route_state is None:
            return True
        # Older state files may only contain last_checked_at.
        last_sent_at = route_state.last_sent_at or route_state.last_checked_at
        if last_sent_at is None:
            return True
        return (now - last_sent_at).total_seconds() >= route.send_interval_seconds

    def _select_routes(self, route_ids: Iterable[str] | None) -> tuple[RouteConfig, ...]:
        """Resolve an optional route selection against configured IDs.

        Args:
            route_ids: IDs in requested order; None selects all routes in configuration order.

        Returns:
            Selected route definitions, retaining the supplied order and any duplicate IDs.

        Raises:
            ConfigError: At least one selected ID is unknown.
        """
        if route_ids is None:
            return self.config.routes
        wanted = tuple(route_ids)
        routes_by_id = {route.id: route for route in self.config.routes}
        missing = [route_id for route_id in wanted if route_id not in routes_by_id]
        if missing:
            raise ConfigError(f"route selection: unknown route ID(s): {', '.join(missing)}")
        return tuple(routes_by_id[route_id] for route_id in wanted)

    def _run_route(self, route: RouteConfig) -> RouteRunResult:
        """Send one message per delivery and verify all configured destination accounts.

        Send-only routes succeed on SMTP acceptance. Delivery and unexpected runtime errors during
        execution are converted to failure results; verified cleanup requests are retained even if
        another delivery fails.

        Args:
            route: Validated route with at least one delivery.

        Returns:
            Success/failure details, per-delivery tokens, and cleanup requests for verified mail.
        """
        started_at = self.now_factory()
        # Separate tokens prevent one arrival from satisfying multiple deliveries
        # that happen to share the same verification account.
        delivery_tokens = tuple(uuid.uuid4().hex for _ in route.deliveries)
        token = delivery_tokens[0]
        delivery_attempts = tuple(zip(route.deliveries, delivery_tokens, strict=True))
        cleanup_requests: list[tuple[str, str]] = []
        direction = self._route_direction(route)
        LOGGER.info("Route started: route=%s direction=%s", route.id, direction)
        try:
            for delivery, delivery_token in delivery_attempts:
                self._send_test_message(route, delivery, delivery_token, started_at)
            if any(delivery.expect_at for delivery in route.deliveries):
                LOGGER.info(
                    "Waiting for delivery: route=%s deliveries=%s timeout=%ss",
                    route.id,
                    len(route.deliveries),
                    route.timeout_seconds,
                )
                self._wait_for_expected_mailboxes(route, delivery_attempts, cleanup_requests)
                outcome = "succeeded"
            else:
                outcome = "sent (delivery verification disabled)"
            LOGGER.info("Route completed: route=%s outcome=%s", route.id, outcome)
            return RouteRunResult(
                route_id=route.id,
                success=True,
                token=token,
                started_at=started_at,
                finished_at=self.now_factory(),
                message=f"route={route.id} direction={direction} {outcome}",
                delivery_tokens=delivery_tokens,
                cleanup_requests=tuple(cleanup_requests),
            )
        except (SmtpError, ImapError, DeliveryTimeoutError) as exc:
            LOGGER.warning("Route failed: route=%s direction=%s error=%s", route.id, direction, exc)
            return RouteRunResult(
                route_id=route.id,
                success=False,
                token=token,
                started_at=started_at,
                finished_at=self.now_factory(),
                message=f"route={route.id} direction={direction} failed: {exc}",
                error_class=exc.__class__.__name__,
                delivery_tokens=delivery_tokens,
                cleanup_requests=tuple(cleanup_requests),
            )
        except Exception as exc:
            LOGGER.exception("Unexpected route failure: route=%s direction=%s", route.id, direction)
            return RouteRunResult(
                route_id=route.id,
                success=False,
                token=token,
                started_at=started_at,
                finished_at=self.now_factory(),
                message=f"route={route.id} direction={direction} failed: {exc.__class__.__name__}",
                error_class=exc.__class__.__name__,
                delivery_tokens=delivery_tokens,
                cleanup_requests=tuple(cleanup_requests),
            )

    def _send_test_message(
        self,
        route: RouteConfig,
        delivery: DeliveryConfig,
        token: str,
        now: datetime,
    ) -> None:
        """Build and send a test message for one concrete delivery.

        Args:
            route: Route defining the SMTP sender and route header.
            delivery: Delivery defining the actual SMTP target account.
            token: Unique token assigned to this delivery attempt.
            now: Timezone-aware creation time used in the message.

        Returns:
            None.

        Raises:
            ConfigError: The sender has no SMTP configuration.
            SmtpError: Sending fails; the error includes route and account context.
        """
        sender = self.config.addresses[route.from_id]
        if sender.smtp is None:
            raise ConfigError(f"route '{route.id}': sender '{route.from_id}' has no SMTP settings")
        recipients = [self.config.addresses[delivery.to].address]
        message = build_test_message(
            sender=sender.address,
            recipients=recipients,
            route_id=route.id,
            token=token,
            created_at=now,
        )
        try:
            self.smtp_client_factory(sender.smtp).send_message(sender.address, recipients, message)
        except SmtpError as exc:
            raise SmtpError(
                f"route={route.id} delivery={delivery.to} account={route.from_id} "
                f"class=SmtpError: {exc}"
            ) from exc

    def _wait_for_expected_mailboxes(
        self,
        route: RouteConfig,
        delivery_attempts: tuple[tuple[DeliveryConfig, str], ...],
        cleanup_requests: list[tuple[str, str]],
    ) -> None:
        """Poll each delivery/account pair until verified or the shared deadline expires.

        The deadline starts after SMTP sends and is checked between polling passes, so blocking
        network operations may overrun it. Transient errors retry the same token without
        resending; already verified pairs are not searched again.

        Args:
            route: Route providing timeout, polling interval, and diagnostic ID.
            delivery_attempts: Delivery definitions paired with their unique current tokens.
            cleanup_requests: Mutable output list of verified (account ID, token) pairs to clean
                up.

        Returns:
            None when every required delivery/account pair has been verified.

        Raises:
            ConfigError: An expected account has no IMAP configuration.
            ImapError: A permanent IMAP failure occurs or transient failures remain at the
                deadline.
            DeliveryTimeoutError: The deadline expires after successful searches without all
                tokens.
        """
        # Deduplicate accounts within each delivery while keeping deliveries distinct.
        expectations = tuple(
            (delivery_index, address_id, token)
            for delivery_index, (delivery, token) in enumerate(delivery_attempts)
            for address_id in dict.fromkeys(delivery.expect_at)
        )
        expected_ids = {
            (delivery_index, address_id) for delivery_index, address_id, _ in expectations
        }
        found: set[tuple[int, str]] = set()
        last_transient_errors: dict[tuple[int, str], str] = {}
        deadline = self.monotonic() + route.timeout_seconds
        while True:
            for delivery_index, address_id, token in expectations:
                expectation_id = (delivery_index, address_id)
                if expectation_id in found:
                    continue
                account = self.config.addresses[address_id]
                if account.imap is None:
                    raise ConfigError(
                        f"route '{route.id}': expected account '{address_id}' has no IMAP"
                    )
                client = self.imap_client_factory(account.imap)
                try:
                    has_token = client.find_token(
                        token,
                        route.id,
                        cleanup=False,
                    )
                except TransientImapError as exc:
                    last_transient_errors[expectation_id] = str(exc)
                    LOGGER.info(
                        "Temporary IMAP failure: route=%s account=%s; retrying: %s",
                        route.id,
                        address_id,
                        exc,
                    )
                    continue
                except ImapError as exc:
                    raise ImapError(
                        f"route={route.id} delivery={delivery_index} account={address_id} "
                        f"class=ImapError: {exc}"
                    ) from exc
                # A successful query supersedes a previous connectivity error, even
                # when the message is still absent, so timeout diagnostics stay accurate.
                last_transient_errors.pop(expectation_id, None)
                if has_token:
                    found.add(expectation_id)
                    if self.config.monitor.cleanup_received_test_messages:
                        cleanup_requests.append((address_id, token))
            if found == expected_ids:
                return
            now = self.monotonic()
            # Check between passes: one blocking IMAP operation can overrun the deadline.
            if now >= deadline:
                missing = ", ".join(
                    f"delivery={delivery_index} account={address_id}"
                    for delivery_index, address_id in sorted(expected_ids - found)
                )
                if last_transient_errors:
                    raise ImapError(
                        f"route={route.id} missing={missing}: delivery could not be verified "
                        f"within {route.timeout_seconds}s; last IMAP errors: "
                        + "; ".join(last_transient_errors.values())
                    )
                raise DeliveryTimeoutError(
                    f"route={route.id} missing={missing} class=DeliveryTimeoutError "
                    f"token was not found within {route.timeout_seconds}s"
                )
            self.sleep(min(route.poll_interval_seconds, max(0.0, deadline - now)))

    def _route_direction(self, route: RouteConfig) -> str:
        """Format the SMTP sender and actual delivery destinations for diagnostics.

        Args:
            route: Route whose account references are resolved.

        Returns:
            A sender -> recipient list string; forwarding verification accounts are omitted.
        """
        sender = self.config.addresses[route.from_id].address
        recipients = [self.config.addresses[delivery.to].address for delivery in route.deliveries]
        return f"{sender} -> {', '.join(recipients)}"

    def _update_state_from_results(
        self,
        state: MonitorState,
        results: tuple[RouteRunResult, ...],
    ) -> None:
        """Replace saved per-route outcomes with the latest executed results.

        The recorded send time is the attempt start, including failed attempts, so repeated
        failures still obey the configured send interval.

        Args:
            state: Mutable monitor state to update in place.
            results: Completed route checks, excluding skipped routes.

        Returns:
            None.
        """
        for result in results:
            state.routes[result.route_id] = RouteState(
                last_success=result.success,
                last_sent_at=result.started_at,
                last_checked_at=result.finished_at,
                last_error=None if result.success else result.message,
            )


def build_test_message(
    sender: str,
    recipients: list[str],
    route_id: str,
    token: str,
    created_at: datetime,
) -> EmailMessage:
    """Build a message identifying one delivery attempt in a route run.

    Args:
        sender: Sender email address for the From header.
        recipients: Email addresses for the To header.
        route_id: Route identifier embedded in headers, subject, and body.
        token: Unique delivery token embedded for subsequent IMAP verification.
        created_at: Timezone-aware creation time for the Date header and body.

    Returns:
        Unsent plain-text email containing route/token headers and a generated Message-ID.
    """

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[mailflow-monitor] route={route_id} token={token}"
    message["Date"] = format_datetime(created_at)
    message["Message-ID"] = make_msgid(
        idstring=f"mailflow-{route_id}-{token}",
        domain="mailflow-monitor.local",
    )
    message["X-Mailflow-Monitor-Token"] = token
    message["X-Mailflow-Monitor-Route"] = route_id
    message.set_content(
        "Mailflow monitor test message.\n\n"
        f"Route: {route_id}\n"
        f"Token: {token}\n"
        f"Created at: {format_dt(created_at)}\n"
    )
    return message


def result_to_json(result: CheckResult) -> str:
    """Serialize check results without including account configuration.

    Args:
        result: Completed run result to serialize.

    Returns:
        Indented JSON with route outcomes, timestamps, delivery tokens, and notification status.
    """

    payload = {
        "success": result.success,
        "notification_failed": result.notification_failed,
        "skipped_routes": list(result.skipped_route_ids),
        "routes": [
            {
                "route_id": item.route_id,
                "success": item.success,
                "token": item.token,
                "delivery_tokens": list(item.delivery_tokens),
                "started_at": format_dt(item.started_at),
                "finished_at": format_dt(item.finished_at),
                "message": item.message,
                "error_class": item.error_class,
            }
            for item in result.route_results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_text_summary(result: CheckResult) -> str:
    """Render CLI outcomes for executed and skipped routes.

    Args:
        result: Completed run result to summarize.

    Returns:
        Multiline summary whose overall status also accounts for notification failures.
    """

    lines = ["mailflow-monitor check summary"]
    for item in result.route_results:
        status = "OK" if item.success else "FAIL"
        lines.append(f"- {status} {item.route_id}: {item.message}")
    for route_id in result.skipped_route_ids:
        lines.append(f"- SKIP {route_id}: send interval has not elapsed")
    if result.notification_failed:
        lines.append("- FAIL notifications: at least one notification could not be sent")
    overall = "OK" if result.success and not result.notification_failed else "FAIL"
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def _state_failure_details(state: MonitorState, route_ids: tuple[str, ...]) -> str:
    """Collect known route failures from persisted state for notifications.

    Args:
        state: Saved route outcomes and error descriptions.
        route_ids: IDs to inspect in output order.

    Returns:
        Newline-separated failure descriptions, or an empty string if none are recorded.
    """
    failures = []
    for route_id in route_ids:
        route_state = state.routes.get(route_id)
        if route_state is not None and route_state.last_success is False:
            failures.append(f"- {route_id}: {route_state.last_error or 'route failed'}")
    return "\n".join(failures)
