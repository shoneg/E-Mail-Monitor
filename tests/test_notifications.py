from __future__ import annotations

from datetime import timedelta

from mailflow_monitor.notifications import NotificationManager
from mailflow_monitor.state import MonitorState


class RecordingSmtpClient:
    sent: list[dict[str, object]] = []
    fail = False

    def __init__(self, config) -> None:
        """Store account settings for the in-memory client double.

        Args:
            config: Connection settings supplied by the monitor; no network connection is opened.

        Returns:
            None.
        """
        self.config = config

    def send_message(self, sender: str, recipients: list[str], message) -> None:
        """Capture notification metadata or simulate an SMTP failure.

        Args:
            sender: Envelope sender address to record.
            recipients: Envelope recipient addresses to record.
            message: EmailMessage provided by the code under test.

        Returns:
            None.

        Raises:
            RuntimeError: The shared fail switch is enabled.
        """
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(
            {
                "sender": sender,
                "recipients": recipients,
                "subject": message["Subject"],
                "body": message.get_content(),
            }
        )


def setup_function() -> None:
    """Reset shared client-double recordings and failure switches before each test.

    Returns:
        None.
    """
    RecordingSmtpClient.sent = []
    RecordingSmtpClient.fail = False


def test_alert_is_sent_for_new_incident(loaded_example_config, fixed_now) -> None:
    """Verify the first failing run opens an incident and records a successful alert.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    state = MonitorState()
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, False, "route failed", fixed_now)

    assert len(RecordingSmtpClient.sent) == 1
    assert "failure" in RecordingSmtpClient.sent[0]["subject"].lower()
    assert state.last_alert_at == fixed_now
    assert state.incident_started_at == fixed_now


def test_repeated_alerts_are_rate_limited(loaded_example_config, fixed_now) -> None:
    """Verify an ongoing incident does not send alerts inside the repeat interval.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    state = MonitorState(incident_started_at=fixed_now, last_alert_at=fixed_now)
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, False, "still failing", fixed_now + timedelta(seconds=60))

    assert RecordingSmtpClient.sent == []


def test_recovery_message_is_sent_after_success(loaded_example_config, fixed_now) -> None:
    """Verify a healthy run sends recovery and clears the previous incident.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    state = MonitorState(incident_started_at=fixed_now - timedelta(hours=1), last_run_success=True)
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, True, "", fixed_now)

    assert any("recovered" in item["subject"].lower() for item in RecordingSmtpClient.sent)
    assert state.last_recovery_at == fixed_now
    assert state.incident_started_at is None


def test_aliveness_respects_interval(loaded_example_config, fixed_now) -> None:
    """Verify successive healthy runs send only one aliveness message within its interval.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    state = MonitorState(last_run_success=True)
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, True, "", fixed_now)
    manager.handle_after_run(state, True, "", fixed_now + timedelta(seconds=60))

    aliveness = [
        item for item in RecordingSmtpClient.sent if "aliveness" in item["subject"].lower()
    ]
    assert len(aliveness) == 1
    assert state.last_aliveness_at == fixed_now


def test_cleanup_warning_threshold_per_account_and_independent_cooldown(
    loaded_example_config,
    fixed_now,
) -> None:
    """Verify cleanup thresholds are per account and use a separate warning cooldown.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    from dataclasses import replace

    config = replace(
        loaded_example_config,
        monitor=replace(loaded_example_config.monitor, cleanup_received_test_messages=True),
    )
    state = MonitorState(
        last_alert_at=fixed_now,
        cleanup_history={"stalwart_recipient": [False] * 9, "external_recipient": [False] * 9},
    )
    manager = NotificationManager(config, RecordingSmtpClient)
    manager.maybe_send_cleanup_warning(state, fixed_now)
    assert RecordingSmtpClient.sent == []
    state.cleanup_history["stalwart_recipient"].append(False)
    manager.maybe_send_cleanup_warning(state, fixed_now)
    assert len(RecordingSmtpClient.sent) == 1
    assert "Cleanup warning" in RecordingSmtpClient.sent[0]["subject"]
    assert "10 of 10" in RecordingSmtpClient.sent[0]["body"]
    assert state.incident_started_at is None
    assert state.last_alert_at == fixed_now
    manager.maybe_send_cleanup_warning(state, fixed_now + timedelta(seconds=60))
    assert len(RecordingSmtpClient.sent) == 1
    state.cleanup_history["stalwart_recipient"] = [False] * 9 + [True] * 6
    manager.maybe_send_cleanup_warning(state, fixed_now + timedelta(days=1))
    assert len(RecordingSmtpClient.sent) == 1


def test_failed_cleanup_warning_can_be_retried(loaded_example_config, fixed_now) -> None:
    """Verify a failed warning leaves its send timestamp unset so it can be retried.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    from dataclasses import replace

    import pytest

    from mailflow_monitor.models import NotificationError

    config = replace(
        loaded_example_config,
        monitor=replace(loaded_example_config.monitor, cleanup_received_test_messages=True),
    )
    state = MonitorState(cleanup_history={"stalwart_recipient": [False] * 10})
    manager = NotificationManager(config, RecordingSmtpClient)
    RecordingSmtpClient.fail = True
    with pytest.raises(NotificationError):
        manager.maybe_send_cleanup_warning(state, fixed_now)
    assert state.last_cleanup_alert_at is None
    RecordingSmtpClient.fail = False
    manager.maybe_send_cleanup_warning(state, fixed_now)
    assert state.last_cleanup_alert_at == fixed_now
