from __future__ import annotations

from datetime import timedelta

from mailflow_monitor.notifications import NotificationManager
from mailflow_monitor.state import MonitorState


class RecordingSmtpClient:
    sent: list[dict[str, object]] = []
    fail = False

    def __init__(self, config) -> None:
        self.config = config

    def send_message(self, sender: str, recipients: list[str], message) -> None:
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
    RecordingSmtpClient.sent = []
    RecordingSmtpClient.fail = False


def test_alert_is_sent_for_new_incident(loaded_example_config, fixed_now) -> None:
    state = MonitorState()
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, False, "route failed", fixed_now)

    assert len(RecordingSmtpClient.sent) == 1
    assert "failure" in RecordingSmtpClient.sent[0]["subject"].lower()
    assert state.last_alert_at == fixed_now
    assert state.incident_started_at == fixed_now


def test_repeated_alerts_are_rate_limited(loaded_example_config, fixed_now) -> None:
    state = MonitorState(incident_started_at=fixed_now, last_alert_at=fixed_now)
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, False, "still failing", fixed_now + timedelta(seconds=60))

    assert RecordingSmtpClient.sent == []


def test_recovery_message_is_sent_after_success(loaded_example_config, fixed_now) -> None:
    state = MonitorState(incident_started_at=fixed_now - timedelta(hours=1), last_run_success=True)
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, True, "", fixed_now)

    assert any("recovered" in item["subject"].lower() for item in RecordingSmtpClient.sent)
    assert state.last_recovery_at == fixed_now
    assert state.incident_started_at is None


def test_aliveness_respects_interval(loaded_example_config, fixed_now) -> None:
    state = MonitorState(last_run_success=True)
    manager = NotificationManager(loaded_example_config, RecordingSmtpClient)

    manager.handle_after_run(state, True, "", fixed_now)
    manager.handle_after_run(state, True, "", fixed_now + timedelta(seconds=60))

    aliveness = [
        item for item in RecordingSmtpClient.sent if "aliveness" in item["subject"].lower()
    ]
    assert len(aliveness) == 1
    assert state.last_aliveness_at == fixed_now
