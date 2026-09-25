from __future__ import annotations

from datetime import UTC, datetime

from mailflow_monitor.imap_client import ImapClient
from mailflow_monitor.models import ImapConfig, TlsMode
from mailflow_monitor.monitor import build_test_message


class FakeFetchConnection:
    def __init__(self, raw_headers: bytes) -> None:
        """Store the raw header payload to return from FETCH.

        Args:
            raw_headers: Encoded message headers returned by the fake IMAP server.

        Returns:
            None.
        """
        self.raw_headers = raw_headers

    def uid(self, command: str, uid: str, query: str):
        """Validate a header-only FETCH and return the stored header payload.

        Args:
            command: IMAP command; must be FETCH.
            uid: Requested UID; must be 1.
            query: Fetch expression containing HEADER.FIELDS.

        Returns:
            OK status and a single IMAP literal tuple containing the stored headers.
        """
        assert command == "FETCH"
        assert uid == "1"
        assert "HEADER.FIELDS" in query
        return "OK", [(b"1 FETCH", self.raw_headers)]


class FakeSearchConnection:
    def __init__(self) -> None:
        """Initialize the list of recorded IMAP search criteria.

        Returns:
            None.
        """
        self.criteria: list[tuple[str, ...]] = []

    def uid(self, command: str, charset: None, *criteria: str):
        """Record SEARCH criteria and make only the TEXT search find a UID.

        Args:
            command: IMAP command; must be SEARCH.
            charset: Must be None to omit CHARSET from SEARCH.
            *criteria: Search criterion and arguments to record.

        Returns:
            OK response containing UID 42 for TEXT, or an empty result otherwise.
        """
        assert command == "SEARCH"
        assert charset is None
        self.criteria.append(criteria)
        if criteria[0] == "TEXT":
            return "OK", [b"42"]
        return "OK", [b""]


class FakeDeleteConnection:
    def __init__(self, *capabilities: bytes) -> None:
        """Initialize a fake server with explicit capabilities and command recording.

        Args:
            *capabilities: Advertised IMAP capability byte strings.

        Returns:
            None.
        """
        self.capabilities = capabilities
        self.commands: list[tuple[str, ...]] = []

    def uid(self, command: str, *arguments: str):
        """Record a UID command and simulate successful completion.

        Args:
            command: UID operation name such as STORE or EXPUNGE.
            *arguments: Operation arguments to retain in the recording.

        Returns:
            OK status with an empty response payload.
        """
        self.commands.append((command, *arguments))
        return "OK", [b""]

    def expunge(self):
        """Fail immediately if cleanup attempts a mailbox-wide expunge.

        Returns:
            Never returns normally.

        Raises:
            AssertionError: Always raised because global expunge is forbidden.
        """
        raise AssertionError("global EXPUNGE must never be used")


def test_test_message_contains_exact_token_and_route_headers() -> None:
    """Verify test messages carry route/token headers and redundant token identifiers.

    Returns:
        None; assertions verify the expected behavior.
    """
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    message = build_test_message(
        sender="sender@example.net",
        recipients=["recipient@example.net"],
        route_id="route-a",
        token="abc123",
        created_at=created_at,
    )

    assert message["X-Mailflow-Monitor-Token"] == "abc123"
    assert message["X-Mailflow-Monitor-Route"] == "route-a"
    assert "abc123" in message["Subject"]
    assert "abc123" in message["Message-ID"]


def test_imap_header_matching_requires_exact_current_token() -> None:
    """Verify custom-header matching rejects a token from another delivery.

    Returns:
        None; assertions verify the expected behavior.
    """
    client = ImapClient(
        ImapConfig(
            host="imap.example.net",
            port=993,
            tls_mode=TlsMode.SSL,
            username="user",
            password="secret",
        )
    )
    raw_headers = (
        b"X-Mailflow-Monitor-Token: exact-token\r\n"
        b"X-Mailflow-Monitor-Route: route-a\r\n"
        b"Subject: [mailflow-monitor] route=route-a token=exact-token\r\n\r\n"
    )

    assert client._message_has_exact_token(
        FakeFetchConnection(raw_headers),
        b"1",
        "exact-token",
        "route-a",
    )
    assert not client._message_has_exact_token(
        FakeFetchConnection(raw_headers),
        b"1",
        "other-token",
        "route-a",
    )


def test_imap_subject_fallback_matches_when_forwarder_removes_custom_headers() -> None:
    """Verify an exact subject can identify forwarded mail without custom headers.

    Returns:
        None; assertions verify the expected behavior.
    """
    client = ImapClient(
        ImapConfig(
            host="imap.example.net",
            port=993,
            tls_mode=TlsMode.SSL,
            username="user",
            password="secret",
        )
    )
    raw_headers = (
        b"Subject: [mailflow-monitor] route=stalwart-via-anonaddy "
        b"token=e1cb642e364a41c3813f92cad9ab8467\r\n\r\n"
    )

    assert client._message_has_exact_token(
        FakeFetchConnection(raw_headers),
        b"1",
        "e1cb642e364a41c3813f92cad9ab8467",
        "stalwart-via-anonaddy",
    )


def test_imap_subject_fallback_rejects_wrong_route_and_non_exact_subject() -> None:
    """Verify subject fallback rejects mismatched routes and added forwarding prefixes.

    Returns:
        None; assertions verify the expected behavior.
    """
    client = ImapClient(
        ImapConfig(
            host="imap.example.net",
            port=993,
            tls_mode=TlsMode.SSL,
            username="user",
            password="secret",
        )
    )
    wrong_route = b"Subject: [mailflow-monitor] route=other-route token=exact-token\r\n\r\n"
    modified_subject = b"Subject: Fwd: [mailflow-monitor] route=route-a token=exact-token\r\n\r\n"

    assert not client._message_has_exact_token(
        FakeFetchConnection(wrong_route),
        b"1",
        "exact-token",
        "route-a",
    )
    assert not client._message_has_exact_token(
        FakeFetchConnection(modified_subject),
        b"1",
        "exact-token",
        "route-a",
    )


def test_imap_candidate_search_includes_standard_headers_and_message_body() -> None:
    """Verify candidate discovery includes Message-ID and full-text search results.

    Returns:
        None; assertions verify the expected behavior.
    """
    client = ImapClient(
        ImapConfig(
            host="imap.example.net",
            port=993,
            tls_mode=TlsMode.SSL,
            username="user",
            password="secret",
        )
    )
    connection = FakeSearchConnection()

    assert client._search_candidates(connection, "exact-token", "INBOX") == [b"42"]
    assert ("HEADER", "Message-ID", "exact-token") in connection.criteria
    assert ("TEXT", "exact-token") in connection.criteria


def test_imap_cleanup_uses_uid_expunge_when_supported() -> None:
    """Verify capable servers receive only UID-scoped deletion and expunge commands.

    Returns:
        None; assertions verify the expected behavior.
    """
    client = ImapClient(
        ImapConfig(
            host="imap.example.net",
            port=993,
            tls_mode=TlsMode.SSL,
            username="user",
            password="secret",
        )
    )
    connection = FakeDeleteConnection(b"IMAP4rev1", b"UIDPLUS")

    client._delete_message(connection, b"42")

    assert connection.commands == [
        ("STORE", "42", "+FLAGS.SILENT", r"(\Deleted)"),
        ("EXPUNGE", "42"),
    ]


def test_imap_cleanup_never_globally_expunges_without_uidplus() -> None:
    """Verify older servers only receive a deleted flag, preserving unrelated messages.

    Returns:
        None; assertions verify the expected behavior.
    """
    client = ImapClient(
        ImapConfig(
            host="imap.example.net",
            port=993,
            tls_mode=TlsMode.SSL,
            username="user",
            password="secret",
        )
    )
    connection = FakeDeleteConnection(b"IMAP4rev1")

    client._delete_message(connection, b"42")

    assert connection.commands == [
        ("STORE", "42", "+FLAGS.SILENT", r"(\Deleted)"),
    ]


def test_failed_search_is_not_mistaken_for_already_deleted_message():
    """Verify a rejected SEARCH raises an error instead of reporting an absent message.

    Returns:
        None; assertions verify the expected behavior.
    """
    import pytest

    from mailflow_monitor.models import ImapError

    class BrokenSearch:
        def uid(self, *args):
            """Simulate a server-side rejection of an IMAP SEARCH request.

            Args:
                *args: UID command arguments accepted for compatibility; unused.

            Returns:
                NO status and a diagnostic response payload.
            """
            return "NO", [b"search temporarily unavailable"]

    client = ImapClient(ImapConfig("host", 993, TlsMode.SSL, "user", "password"))
    with pytest.raises(ImapError, match="candidate search failed"):
        client._search_candidates(BrokenSearch(), "token", "INBOX")


def test_socket_timeouts_are_classified_as_retryable(monkeypatch):
    """Verify connection timeouts are translated into transient IMAP errors.

    Args:
        monkeypatch: Pytest fixture for temporary environment or dependency replacements.

    Returns:
        None; assertions verify the expected behavior.
    """
    import pytest

    from mailflow_monitor.models import TransientImapError

    client = ImapClient(ImapConfig("host", 993, TlsMode.SSL, "user", "password"))

    def timeout():
        """Simulate a connection attempt ending in a socket timeout.

        Returns:
            Never returns normally.

        Raises:
            TimeoutError: Always raised by this connection stub.
        """
        raise TimeoutError("timeout")

    monkeypatch.setattr(client, "_connect", timeout)
    with pytest.raises(TransientImapError):
        client.find_token("token", "route")
