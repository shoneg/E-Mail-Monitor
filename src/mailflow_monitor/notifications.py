"""Notification rules for incidents, recovery, and aliveness."""

from __future__ import annotations

import logging
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from .config import resolve_notification_recipients
from .models import AppConfig, NotificationError
from .smtp_client import SmtpClient
from .state import MonitorState, format_dt

LOGGER = logging.getLogger(__name__)


class NotificationManager:
    """Apply notification policy and send email through the configured sender."""

    def __init__(
        self,
        config: AppConfig,
        smtp_client_factory: type[SmtpClient] = SmtpClient,
    ) -> None:
        """Configure notification policy and the SMTP client constructor.

        Args:
            config: Validated account definitions and notification settings.
            smtp_client_factory: Callable constructing an SMTP client from sender connection
                settings.

        Returns:
            None.
        """
        self.config = config
        self.smtp_client_factory = smtp_client_factory

    def handle_after_run(
        self,
        state: MonitorState,
        run_success: bool,
        failure_details: str,
        now: datetime,
    ) -> None:
        """Apply incident/recovery policy and update notification state in place.

        Healthy runs may send recovery and aliveness mail before clearing the incident. Failed
        runs open/update an incident and may send a rate-limited alert. The caller must decide
        whether a partial run is eligible and persist the changed state.

        Args:
            state: Mutable monitor state containing incident details and successful-send
                timestamps.
            run_success: Whether the checked routes are healthy.
            failure_details: Human-readable route failures for incident alerts.
            now: Timezone-aware current time used for policy checks and message dates.

        Returns:
            None.

        Raises:
            NotificationError: A required sender is unavailable or SMTP delivery fails.
        """

        if run_success:
            self._maybe_send_recovery(state, now)
            self._maybe_send_aliveness(state, now)
            state.incident_started_at = None
            state.incident_details = None
            return

        if state.incident_started_at is None:
            state.incident_started_at = now
            state.incident_details = failure_details
        elif failure_details:
            state.incident_details = failure_details
        self._maybe_send_alert(state, failure_details, now)

    def maybe_send_cleanup_warning(self, state: MonitorState, now: datetime) -> None:
        """Warn when completed cleanup failures reach a per-account threshold.

        Only active IMAP accounts are considered. The warning has its own cooldown and does not
        open a delivery incident; the timestamp advances only after a successful send.

        Args:
            state: Mutable cleanup history and independent cleanup-warning timestamp.
            now: Timezone-aware current time used for policy checks and message dates.

        Returns:
            None.

        Raises:
            NotificationError: A required sender is unavailable or SMTP delivery fails.
        """
        alerts = self.config.notifications.alerts
        settings = self.config.monitor
        if not alerts.enabled or not settings.cleanup_received_test_messages:
            return
        if (
            state.last_cleanup_alert_at is not None
            and (now - state.last_cleanup_alert_at).total_seconds() < alerts.repeat_after_seconds
        ):
            return
        details = []
        for address_id, history in state.cleanup_history.items():
            account = self.config.addresses.get(address_id)
            if account is None or account.imap is None:
                continue
            recent = history[-settings.cleanup_failure_window :]
            failures = recent.count(False)
            if failures >= settings.cleanup_failure_threshold:
                details.append(
                    f"- {address_id}: {failures} of {len(recent)} completed cleanups failed"
                )
        if not details:
            return
        self._send(
            alerts.sender,
            alerts.recipients,
            "[mailflow-monitor] Cleanup warning",
            "Repeated cleanup attempts were exhausted for previously verified test messages.\n"
            "These cleanup failures do not indicate failed delivery.\n"
            "No further deletion attempts will be made for the affected messages; "
            "they may remain in the mailbox or be marked deleted.\n\n" + "\n".join(details),
            now,
        )
        # Failed sends must not consume the cooldown or suppress the next retry.
        state.last_cleanup_alert_at = now

    def _maybe_send_alert(self, state: MonitorState, details: str, now: datetime) -> None:
        """Send an enabled incident alert if its repeat interval has elapsed.

        Updates last_alert_at only after successful SMTP delivery.

        Args:
            state: Mutable monitor state containing incident details and successful-send
                timestamps.
            details: Failure descriptions to include in the alert body.
            now: Timezone-aware current time used for policy checks and message dates.

        Returns:
            None.

        Raises:
            NotificationError: A required sender is unavailable or SMTP delivery fails.
        """
        alerts = self.config.notifications.alerts
        if not alerts.enabled:
            return
        if state.last_alert_at is not None:
            elapsed = (now - state.last_alert_at).total_seconds()
            if elapsed < alerts.repeat_after_seconds:
                LOGGER.info("Alert suppressed by repeat_after_seconds policy")
                return
        subject = "[mailflow-monitor] Delivery path failure"
        body = (
            "The mailflow monitor detected a failing delivery path.\n\n"
            f"Incident started at: {format_dt(state.incident_started_at)}\n"
            f"Current run at: {format_dt(now)}\n\n"
            f"{details}\n"
        )
        self._send(alerts.sender, alerts.recipients, subject, body, now)
        state.last_alert_at = now

    def _maybe_send_recovery(self, state: MonitorState, now: datetime) -> None:
        """Send an enabled recovery message when a previous incident is recorded.

        Updates last_recovery_at after sending; the caller clears the incident.

        Args:
            state: Mutable monitor state containing incident details and successful-send
                timestamps.
            now: Timezone-aware current time used for policy checks and message dates.

        Returns:
            None.

        Raises:
            NotificationError: A required sender is unavailable or SMTP delivery fails.
        """
        alerts = self.config.notifications.alerts
        if not alerts.enabled or not alerts.send_recovery_message:
            return
        if state.incident_started_at is None:
            return
        subject = "[mailflow-monitor] Delivery paths recovered"
        body = (
            "The mailflow monitor completed a successful run after a previous incident.\n\n"
            f"Incident started at: {format_dt(state.incident_started_at)}\n"
            f"Recovered at: {format_dt(now)}\n"
        )
        self._send(alerts.sender, alerts.recipients, subject, body, now)
        state.last_recovery_at = now

    def _maybe_send_aliveness(self, state: MonitorState, now: datetime) -> None:
        """Send aliveness mail when enabled, eligible, and due.

        Checks saved run health when only_when_healthy is enabled and advances last_aliveness_at
        only after a successful send.

        Args:
            state: Mutable monitor state containing incident details and successful-send
                timestamps.
            now: Timezone-aware current time used for policy checks and message dates.

        Returns:
            None.

        Raises:
            NotificationError: A required sender is unavailable or SMTP delivery fails.
        """
        aliveness = self.config.notifications.aliveness
        if not aliveness.enabled:
            return
        if aliveness.only_when_healthy and state.last_run_success is not True:
            return
        if state.last_aliveness_at is not None:
            elapsed = (now - state.last_aliveness_at).total_seconds()
            if elapsed < aliveness.interval_seconds:
                return
        subject = "[mailflow-monitor] Aliveness"
        body = (
            "The mailflow monitor completed its latest full run successfully.\n\n"
            f"Run time: {format_dt(now)}\n"
        )
        self._send(aliveness.sender, aliveness.recipients, subject, body, now)
        state.last_aliveness_at = now

    def _send(
        self,
        sender_id: str | None,
        recipient_refs: tuple[str, ...],
        subject: str,
        body: str,
        now: datetime,
    ) -> None:
        """Resolve notification accounts, construct an email, and send it via SMTP.

        Args:
            sender_id: Configured SMTP sender account ID; None is an error.
            recipient_refs: Validated email addresses or account:<id> references.
            subject: Subject header for the notification.
            body: Plain-text notification body.
            now: Timezone-aware current time used for policy checks and message dates.

        Returns:
            None.

        Raises:
            NotificationError: A required sender is unavailable or SMTP delivery fails.
        """
        if sender_id is None:
            raise NotificationError("notification sender is not configured")
        sender = self.config.addresses[sender_id]
        if sender.smtp is None:
            raise NotificationError(f"notification sender '{sender_id}' has no SMTP settings")
        recipients = list(resolve_notification_recipients(recipient_refs, self.config.addresses))
        message = EmailMessage()
        message["From"] = sender.address
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain="mailflow-monitor.local")
        message.set_content(body)
        try:
            self.smtp_client_factory(sender.smtp).send_message(sender.address, recipients, message)
        except Exception as exc:
            raise NotificationError(
                f"notification delivery failed via sender '{sender_id}': {exc.__class__.__name__}"
            ) from exc
