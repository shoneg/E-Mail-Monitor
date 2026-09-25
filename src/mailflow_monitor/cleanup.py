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
    """Process persisted cleanup tasks without changing delivery health."""

    def __init__(
        self,
        config: AppConfig,
        imap_client_factory: type[ImapClient] = ImapClient,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        """Configure cleanup dependencies without accessing any mailboxes.

        Args:
            config: Validated settings including retry limits and account definitions.
            imap_client_factory: Client constructor accepting one account's IMAP settings.
            now_factory: Callable returning a timezone-aware current time for retry scheduling.

        Returns:
            None.
        """
        self.config = config
        self.imap_client_factory = imap_client_factory
        self.now_factory = now_factory

    def process(self, state: MonitorState) -> None:
        """Attempt each due cleanup task once and update its persistent work state.

        IMAP errors schedule a later retry or record an exhausted cleanup. Disabled cleanup leaves
        the queue untouched. The caller must save the modified state; this method neither sleeps
        between retries nor changes delivery health.

        Args:
            state: Mutable state whose pending tasks and per-account outcome history are updated.

        Returns:
            None.
        """
        settings = self.config.monitor
        if not settings.cleanup_received_test_messages:
            return
        # Iterate a snapshot so removing completed tasks does not skip later entries.
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
                # The counter includes the initial attempt, so N retries allow N+1
                # total attempts. Schedule from failure completion, not attempt start.
                if task.attempts <= settings.cleanup_retry_count:
                    task.next_attempt_at = self.now_factory() + timedelta(
                        seconds=settings.cleanup_retry_interval_seconds,
                    )
                    continue
                success = False
            else:
                success = True
            state.cleanup_pending.remove(task)
            # Count one terminal outcome per message, not each failed retry, and
            # retain only the configured per-account warning window.
            history = state.cleanup_history.setdefault(task.address_id, [])
            history.append(success)
            del history[: -settings.cleanup_failure_window]
