from __future__ import annotations

from datetime import UTC, datetime

from mailflow_monitor.imap_client import ImapClient
from mailflow_monitor.models import ImapConfig, TlsMode
from mailflow_monitor.monitor import build_test_message


class FakeFetchConnection:
    def __init__(self, raw_headers: bytes) -> None:
        self.raw_headers = raw_headers

    def uid(self, command: str, uid: str, query: str):
        assert command == "FETCH"
        assert uid == "1"
        assert "HEADER.FIELDS" in query
        return "OK", [(b"1 FETCH", self.raw_headers)]


def test_test_message_contains_exact_token_and_route_headers() -> None:
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


def test_imap_header_matching_requires_exact_current_token() -> None:
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
