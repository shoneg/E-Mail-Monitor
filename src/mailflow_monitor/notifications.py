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
        self.config = config
        self.smtp_client_factory = smtp_client_factory

    def handle_after_run(
        self,
        state: MonitorState,
        run_success: bool,
        failure_details: str,
        now: datetime,
    ) -> None:
        """Send due notifications and update notification timestamps in state."""

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

    def _maybe_send_alert(self, state: MonitorState, details: str, now: datetime) -> None:
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

