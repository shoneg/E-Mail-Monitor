"""Core mailflow monitor orchestration."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from .imap_client import ImapClient
from .models import (
    AppConfig,
    CheckResult,
    ConfigError,
    DeliveryTimeoutError,
    ImapError,
    RouteConfig,
    RouteRunResult,
    SmtpError,
)
from .notifications import NotificationManager
from .smtp_client import SmtpClient
from .state import FileLock, MonitorState, RouteState, StateStore, format_dt, utc_now

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
        self.config = config
        self.smtp_client_factory = smtp_client_factory
        self.imap_client_factory = imap_client_factory
        self.sleep = sleep
        self.monotonic = monotonic
        self.now_factory = now_factory
        self.state_store = StateStore(config.monitor.state_file)

    def check(self, route_ids: Iterable[str] | None = None) -> CheckResult:
        """Run all configured routes or the selected subset."""

        selected_routes = self._select_routes(route_ids)
        with FileLock(self.config.monitor.lock_file):
            state = self.state_store.load()
            results = tuple(self._run_route(route) for route in selected_routes)
            now = self.now_factory()
            run_success = all(result.success for result in results)
            failure_details = _failure_details(results)
            self._update_state_from_results(state, results, run_success, now, failure_details)

            notification_failed = False
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

            self.state_store.save(state)
            return CheckResult(
                success=run_success,
                route_results=results,
                notification_failed=notification_failed,
            )

    def _select_routes(self, route_ids: Iterable[str] | None) -> tuple[RouteConfig, ...]:
        if route_ids is None:
            return self.config.routes
        wanted = tuple(route_ids)
        routes_by_id = {route.id: route for route in self.config.routes}
        missing = [route_id for route_id in wanted if route_id not in routes_by_id]
        if missing:
            raise ConfigError(f"route selection: unknown route ID(s): {', '.join(missing)}")
        return tuple(routes_by_id[route_id] for route_id in wanted)

    def _run_route(self, route: RouteConfig) -> RouteRunResult:
        started_at = self.now_factory()
        token = uuid.uuid4().hex
        direction = self._route_direction(route)
        try:
            self._send_test_message(route, token, started_at)
            self._wait_for_expected_mailboxes(route, token)
            return RouteRunResult(
                route_id=route.id,
                success=True,
                token=token,
                started_at=started_at,
                finished_at=self.now_factory(),
                message=f"route={route.id} direction={direction} succeeded",
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
            )

    def _send_test_message(self, route: RouteConfig, token: str, now: datetime) -> None:
        sender = self.config.addresses[route.from_id]
        if sender.smtp is None:
            raise ConfigError(f"route '{route.id}': sender '{route.from_id}' has no SMTP settings")
        recipients = [self.config.addresses[delivery.to].address for delivery in route.deliveries]
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
                f"route={route.id} account={route.from_id} class=SmtpError: {exc}"
            ) from exc

    def _wait_for_expected_mailboxes(self, route: RouteConfig, token: str) -> None:
        expected_ids = _unique_expected_ids(route)
        found: set[str] = set()
        deadline = self.monotonic() + route.timeout_seconds
        while True:
            for address_id in expected_ids:
                if address_id in found:
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
                        cleanup=self.config.monitor.cleanup_received_test_messages,
                    )
                except ImapError as exc:
                    raise ImapError(
                        f"route={route.id} account={address_id} class=ImapError: {exc}"
                    ) from exc
                if has_token:
                    found.add(address_id)
            if found == set(expected_ids):
                return
            now = self.monotonic()
            if now >= deadline:
                missing = ", ".join(sorted(set(expected_ids) - found))
                raise DeliveryTimeoutError(
                    f"route={route.id} account={missing} class=DeliveryTimeoutError "
                    f"token was not found within {route.timeout_seconds}s"
                )
            self.sleep(min(route.poll_interval_seconds, max(0.0, deadline - now)))

    def _route_direction(self, route: RouteConfig) -> str:
        sender = self.config.addresses[route.from_id].address
        recipients = [self.config.addresses[delivery.to].address for delivery in route.deliveries]
        return f"{sender} -> {', '.join(recipients)}"

    def _update_state_from_results(
        self,
        state: MonitorState,
        results: tuple[RouteRunResult, ...],
        run_success: bool,
        now: datetime,
        failure_details: str,
    ) -> None:
        state.last_run_at = now
        state.last_run_success = run_success
        if not run_success and state.incident_details is None:
            state.incident_details = failure_details
        for result in results:
            state.routes[result.route_id] = RouteState(
                last_success=result.success,
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
    """Build the unique test message sent for one route run."""

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[mailflow-monitor] route={route_id} token={token}"
    message["Date"] = format_datetime(created_at)
    message["Message-ID"] = make_msgid(
        idstring=f"mailflow-{route_id}",
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
    """Serialize a check result without exposing credentials."""

    payload = {
        "success": result.success,
        "notification_failed": result.notification_failed,
        "routes": [
            {
                "route_id": item.route_id,
                "success": item.success,
                "token": item.token,
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
    """Render a human-readable CLI summary."""

    lines = ["mailflow-monitor check summary"]
    for item in result.route_results:
        status = "OK" if item.success else "FAIL"
        lines.append(f"- {status} {item.route_id}: {item.message}")
    if result.notification_failed:
        lines.append("- FAIL notifications: at least one notification could not be sent")
    overall = "OK" if result.success and not result.notification_failed else "FAIL"
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def _unique_expected_ids(route: RouteConfig) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for delivery in route.deliveries:
        for address_id in delivery.expect_at:
            seen[address_id] = None
    return tuple(seen)


def _failure_details(results: tuple[RouteRunResult, ...]) -> str:
    failures = [result for result in results if not result.success]
    if not failures:
        return ""
    return "\n".join(f"- {item.route_id}: {item.error_class}: {item.message}" for item in failures)
