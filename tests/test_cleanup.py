from dataclasses import replace
from datetime import timedelta

from mailflow_monitor.cleanup import CleanupManager
from mailflow_monitor.models import ImapError
from mailflow_monitor.state import CleanupTask, MonitorState


class FailingCleanup:
    calls = 0

    def __init__(self, config):
        """Accept factory settings without opening a real connection.

        Args:
            config: IMAP settings accepted for interface compatibility; unused.

        Returns:
            None.
        """
        pass

    def find_token(self, token, route_id, cleanup=False):
        """Count a cleanup attempt and simulate a deletion failure.

        Args:
            token: Delivery token supplied by the monitor.
            route_id: Route ID supplied for matching.
            cleanup: Whether this call requests deletion instead of read-only verification.

        Returns:
            Never returns normally.

        Raises:
            ImapError: Always raised after recording an attempt.
            AssertionError: The caller did not request cleanup.
        """
        assert cleanup
        type(self).calls += 1
        raise ImapError("cannot mark message deleted")


def test_retry_spacing_exhaustion_and_single_history_entry(loaded_example_config, fixed_now):
    """Verify retry timing and that exhaustion records one outcome for eleven attempts.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    FailingCleanup.calls = 0
    config = replace(
        loaded_example_config,
        monitor=replace(loaded_example_config.monitor, cleanup_received_test_messages=True),
    )
    state = MonitorState(cleanup_pending=[CleanupTask("stalwart_recipient", "route", "token")])
    now = fixed_now
    manager = CleanupManager(config, FailingCleanup, lambda: now)
    manager.process(state)
    assert FailingCleanup.calls == 1
    for attempt in range(1, 11):
        # Probe just before and exactly at each due time without sleeping in real time.
        now = fixed_now + timedelta(seconds=60 * attempt - 1)
        manager.process(state)
        assert FailingCleanup.calls == attempt
        now += timedelta(seconds=1)
        manager.process(state)
        assert FailingCleanup.calls == attempt + 1
    assert state.cleanup_pending == []
    assert state.cleanup_history == {"stalwart_recipient": [False]}
    manager.process(state)
    assert FailingCleanup.calls == 11


def test_success_or_already_absent_message_finishes_cleanup(loaded_example_config, fixed_now):
    """Verify both successful deletion and absent messages complete queued cleanup.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    config = replace(
        loaded_example_config,
        monitor=replace(loaded_example_config.monitor, cleanup_received_test_messages=True),
    )

    class SuccessfulCleanup(FailingCleanup):
        def find_token(self, token, route_id, cleanup=False):
            """Simulate successful cleanup of present mail and an already absent message.

            Args:
                token: Delivery token supplied by the monitor.
                route_id: Route ID supplied for matching.
                cleanup: Whether this call requests deletion instead of read-only verification.

            Returns:
                True for the token present, otherwise False; neither result raises an error.
            """
            return token == "present"

    state = MonitorState(
        cleanup_pending=[
            CleanupTask("stalwart_recipient", "route", t) for t in ("present", "gone")
        ],
        cleanup_history={"stalwart_recipient": [False] * 15},
    )
    CleanupManager(config, SuccessfulCleanup, lambda: fixed_now).process(state)
    assert state.cleanup_pending == []
    assert state.cleanup_history["stalwart_recipient"] == [False] * 13 + [True, True]


def test_cleanup_disabled_preserves_queue_without_network_access(loaded_example_config, fixed_now):
    """Verify disabling cleanup pauses pending tasks without touching IMAP.

    Args:
        loaded_example_config: Validated example configuration with state paths inside a temporary
            directory.
        fixed_now: Deterministic timezone-aware UTC timestamp supplied by the fixture.

    Returns:
        None; assertions verify the expected behavior.
    """
    FailingCleanup.calls = 0
    state = MonitorState(cleanup_pending=[CleanupTask("stalwart_recipient", "route", "token")])
    CleanupManager(loaded_example_config, FailingCleanup, lambda: fixed_now).process(state)
    assert FailingCleanup.calls == 0
    assert state.cleanup_pending[0].attempts == 0
