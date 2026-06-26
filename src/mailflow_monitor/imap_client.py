"""IMAP search client for exact mailflow monitor token matching."""

from __future__ import annotations

import email
import imaplib
import logging
import ssl
from email import policy
from email.parser import BytesParser

from .models import ImapConfig, ImapError, TlsMode
from .smtp_client import create_tls_context

LOGGER = logging.getLogger(__name__)
TOKEN_HEADER = "X-Mailflow-Monitor-Token"
ROUTE_HEADER = "X-Mailflow-Monitor-Route"


class ImapClient:
    """Search configured mailboxes for a message with an exact current token."""

    def __init__(self, config: ImapConfig) -> None:
        self.config = config

    def find_token(self, token: str, route_id: str, cleanup: bool = False) -> bool:
        """Return ``True`` when any configured mailbox contains the exact token."""

        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        try:
            connection = self._connect()
            connection.login(self.config.username, self.config.password)
            for mailbox in self.config.mailboxes:
                if self._find_in_mailbox(connection, mailbox, token, route_id, cleanup):
                    return True
            return False
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise ImapError(
                f"IMAP: search failed for host={self.config.host} port={self.config.port}: "
                f"{exc.__class__.__name__}"
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.logout()
                except (imaplib.IMAP4.error, OSError):
                    LOGGER.debug("IMAP logout failed for host=%s", self.config.host)

    def _connect(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        context = create_tls_context(self.config.ca_file)
        if self.config.tls_mode is TlsMode.SSL:
            return imaplib.IMAP4_SSL(
                self.config.host,
                self.config.port,
                ssl_context=context,
                timeout=30,
            )
        connection = imaplib.IMAP4(self.config.host, self.config.port, timeout=30)
        if self.config.tls_mode is TlsMode.STARTTLS:
            connection.starttls(ssl_context=context)
        elif self.config.tls_mode is TlsMode.PLAIN and not self.config.allow_insecure_plaintext:
            raise ImapError("IMAP: plaintext mode is not allowed by configuration")
        return connection

    def _find_in_mailbox(
        self,
        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL,
        mailbox: str,
        token: str,
        route_id: str,
        cleanup: bool,
    ) -> bool:
        status, _ = connection.select(mailbox, readonly=not cleanup)
        if status != "OK":
            raise ImapError(f"IMAP: cannot select mailbox '{mailbox}' on host={self.config.host}")

        candidate_uids = self._search_candidates(connection, token)
        for uid in candidate_uids:
            if self._message_has_exact_token(connection, uid, token, route_id):
                if cleanup:
                    self._delete_message(connection, uid)
                return True
        return False

    def _search_candidates(
        self,
        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL,
        token: str,
    ) -> list[bytes]:
        uids: list[bytes] = []
        for criteria in (
            ("HEADER", TOKEN_HEADER, token),
            ("SUBJECT", token),
        ):
            status, data = connection.uid("SEARCH", None, *criteria)
            if status == "OK" and data:
                uids.extend(_split_uid_response(data[0]))
        return list(dict.fromkeys(uids))

    def _message_has_exact_token(
        self,
        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL,
        uid: bytes,
        token: str,
        route_id: str,
    ) -> bool:
        fetch_query = (
            f"(BODY.PEEK[HEADER.FIELDS ({TOKEN_HEADER} {ROUTE_HEADER} SUBJECT MESSAGE-ID)])"
        )
        status, data = connection.uid("FETCH", uid, fetch_query)
        if status != "OK":
            raise ImapError(f"IMAP: cannot fetch headers for uid={uid.decode(errors='ignore')}")
        raw_headers = _extract_fetch_payload(data)
        if raw_headers is None:
            return False
        message = BytesParser(policy=policy.default).parsebytes(raw_headers)
        header_token = message.get(TOKEN_HEADER)
        header_route = message.get(ROUTE_HEADER)
        subject = message.get("Subject", "")
        if header_token == token and (header_route in (None, route_id)):
            return True
        decoded_subject = str(email.header.make_header(email.header.decode_header(subject)))
        return token in decoded_subject and header_token == token

    def _delete_message(
        self,
        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL,
        uid: bytes,
    ) -> None:
        status, _ = connection.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
        if status != "OK":
            raise ImapError(f"IMAP: cannot mark uid={uid.decode(errors='ignore')} as deleted")
        connection.expunge()


def _split_uid_response(value: bytes | str) -> list[bytes]:
    if isinstance(value, str):
        value = value.encode()
    return [item for item in value.split() if item]


def _extract_fetch_payload(
    data: list[bytes | tuple[bytes, bytes]] | tuple[object, ...],
) -> bytes | None:
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None
