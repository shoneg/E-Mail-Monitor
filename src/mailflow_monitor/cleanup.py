"""Persistent, best-effort cleanup independent of delivery health."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from .imap_client import ImapClient
from .models import AppConfig, ImapError
from .state import MonitorState, utc_now

LOGGER = logging.getLogger(__name__)


class CleanupManager:
    def __init__(
        self,
        config: AppConfig,
        imap_client_factory: type[ImapClient] = ImapClient,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.imap_client_factory = imap_client_factory
        self.now_factory = now_factory

    def process(self, state: MonitorState) -> None:
        settings = self.config.monitor
        if not settings.cleanup_received_test_messages:
            return
        for task in list(state.cleanup_pending):
            if task.next_attempt_at is not None and self.now_factory() < task.next_attempt_at:
                continue
            account = self.config.addresses.get(task.address_id)
            if account is None or account.imap is None:
                # Removed accounts cannot be retried; discard their work without touching mail.
                state.cleanup_pending.remove(task)
                continue
            task.attempts += 1
            try:
                # Re-search and verify the exact token on every attempt. A missing message
                # is already gone and needs no more work; never reuse an old IMAP UID.
                self.imap_client_factory(account.imap).find_token(
                    task.token,
                    task.route_id,
                    cleanup=True,
                )
            except ImapError as exc:
                LOGGER.info(
                    "Cleanup deferred: account=%s route=%s attempt=%s error=%s",
                    task.address_id,
                    task.route_id,
                    task.attempts,
                    exc,
                )
                if task.attempts <= settings.cleanup_retry_count:
                    task.next_attempt_at = self.now_factory() + timedelta(
                        seconds=settings.cleanup_retry_interval_seconds,
                    )
                    continue
                success = False
            else:
                success = True
            state.cleanup_pending.remove(task)
            history = state.cleanup_history.setdefault(task.address_id, [])
            history.append(success)
            del history[: -settings.cleanup_failure_window]
