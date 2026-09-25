"""IMAP search client for exact mailflow monitor token matching."""

from __future__ import annotations

import email
import imaplib
import logging
from email import policy
from email.parser import BytesParser

from .models import ImapConfig, ImapError, TlsMode, TransientImapError
from .smtp_client import create_tls_context

LOGGER = logging.getLogger(__name__)
TOKEN_HEADER = "X-Mailflow-Monitor-Token"
ROUTE_HEADER = "X-Mailflow-Monitor-Route"
MESSAGE_ID_HEADER = "Message-ID"


class ImapClient:
    """Search configured mailboxes for a message with an exact current token."""

    def __init__(self, config: ImapConfig) -> None:
        """Store connection settings and mailboxes for later searches.

        Args:
            config: IMAP host, credentials, TLS mode, and mailbox names.

        Returns:
            None.
        """
        self.config = config

    def find_token(self, token: str, route_id: str, cleanup: bool = False) -> bool:
        """Search configured mailboxes for an exact token and optionally remove its message.

        Opens a new connection for each call and attempts logout even after a failure.

        Args:
            token: Current ASCII delivery token to verify exactly.
            route_id: Expected route ID used to reject unrelated messages.
            cleanup: Whether to delete the first verified match; defaults to read-only search.

        Returns:
            True for the first verified match, or False after all mailboxes were searched.

        Raises:
            TransientImapError: The connection aborts or a network/OS operation fails.
            ImapError: Authentication, mailbox access, search, verification, or deletion fails.
        """

        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        try:
            connection = self._connect()
            connection.login(self.config.username, self.config.password)
            for mailbox in self.config.mailboxes:
                if self._find_in_mailbox(connection, mailbox, token, route_id, cleanup):
                    return True
            return False
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise TransientImapError(
                f"IMAP: connection failed for host={self.config.host} port={self.config.port}: "
                f"{exc.__class__.__name__}"
            ) from exc
        except imaplib.IMAP4.error as exc:
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
        """Open an IMAP connection using the configured transport security.

        Returns:
            Unauthenticated connection with a 30-second socket-operation timeout.

        Raises:
            ImapError: Plaintext mode lacks explicit opt-in.
            imaplib.IMAP4.error: Connection setup or STARTTLS fails.
            OSError: The socket or TLS context cannot be initialized.
        """
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
        """Select one mailbox, verify search candidates, and optionally delete a match.

        Args:
            connection: Authenticated IMAP connection on which to select the mailbox.
            mailbox: Configured mailbox name to select.
            token: Exact delivery token to match.
            route_id: Expected route ID.
            cleanup: Whether to select read-write and delete the first verified message.

        Returns:
            True if a candidate matches, otherwise False.

        Raises:
            ImapError: Mailbox selection, search, header fetch, or cleanup fails.
        """
        status, _ = connection.select(mailbox, readonly=not cleanup)
        if status != "OK":
            raise ImapError(f"IMAP: cannot select mailbox '{mailbox}' on host={self.config.host}")

        candidate_uids = self._search_candidates(connection, token, mailbox)
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
        mailbox: str,
    ) -> list[bytes]:
        """Collect candidate UIDs from token header, Message-ID, subject, and text searches.

        Args:
            connection: Authenticated IMAP connection; the target mailbox must already be
                selected.
            token: ASCII token used as a server-side search term.
            mailbox: Selected mailbox name, included in diagnostics.

        Returns:
            Unique UID byte strings in first-seen order; matches still need exact verification.

        Raises:
            ImapError: Any search returns a non-OK status.
        """
        # SEARCH performs broad substring matching; a candidate is never sufficient
        # proof of delivery until its fetched headers pass exact verification.
        uids: list[bytes] = []
        for criteria in (
            ("HEADER", TOKEN_HEADER, token),
            ("HEADER", MESSAGE_ID_HEADER, token),
            ("SUBJECT", token),
            ("TEXT", token),
        ):
            # None intentionally omits CHARSET; every search value here is ASCII.
            status, data = connection.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
            if status != "OK":
                raise ImapError(
                    f"IMAP: candidate search failed for host={self.config.host} "
                    f"mailbox={mailbox} criterion={_describe_search_criteria(criteria)}"
                )
            if data:
                matches = _split_uid_response(data[0])
                LOGGER.debug(
                    "IMAP candidate search: host=%s mailbox=%s criterion=%s matches=%d",
                    self.config.host,
                    mailbox,
                    _describe_search_criteria(criteria),
                    len(matches),
                )
                uids.extend(matches)
        return list(dict.fromkeys(uids))

    def _message_has_exact_token(
        self,
        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL,
        uid: bytes,
        token: str,
        route_id: str,
    ) -> bool:
        """Fetch headers without marking mail as read and verify the token/route pair.

        Args:
            connection: Authenticated IMAP connection; the target mailbox must already be
                selected.
            uid: ASCII UID bytes identifying a candidate in the selected mailbox.
            token: Delivery token requiring an exact match.
            route_id: Expected route ID.

        Returns:
            True for an exact token header with no conflicting route, or an exact subject when the
            token header is absent; False for a mismatch or absent fetch payload.

        Raises:
            ImapError: Header fetching returns a non-OK status.
        """
        fetch_query = (
            f"(BODY.PEEK[HEADER.FIELDS ({TOKEN_HEADER} {ROUTE_HEADER} SUBJECT MESSAGE-ID)])"
        )
        status, data = connection.uid("FETCH", uid.decode("ascii"), fetch_query)
        if status != "OK":
            raise ImapError(f"IMAP: cannot fetch headers for uid={uid.decode(errors='ignore')}")
        raw_headers = _extract_fetch_payload(data)
        if raw_headers is None:
            return False
        message = BytesParser(policy=policy.default).parsebytes(raw_headers)
        header_token = message.get(TOKEN_HEADER)
        header_route = message.get(ROUTE_HEADER)
        subject = message.get("Subject", "")
        decoded_subject = str(email.header.make_header(email.header.decode_header(subject)))
        # An explicit token header is authoritative: a wrong header must not be
        # rescued by a matching subject. Subject fallback covers stripped headers only.
        if header_token is not None:
            return header_token == token and header_route in (None, route_id)
        if header_route not in (None, route_id):
            return False
        return decoded_subject == f"[mailflow-monitor] route={route_id} token={token}"

    def _delete_message(
        self,
        connection: imaplib.IMAP4 | imaplib.IMAP4_SSL,
        uid: bytes,
    ) -> None:
        """Mark a verified message deleted and selectively expunge it when supported.

        Args:
            connection: Authenticated IMAP connection with the mailbox selected read-write.
            uid: ASCII UID bytes of the already verified message.

        Returns:
            None. Without UIDPLUS/IMAP4rev2, the message only receives the deleted flag.

        Raises:
            ImapError: Marking or selectively expunging the message fails.
        """
        uid_text = uid.decode("ascii")
        status, _ = connection.uid(
            "STORE",
            uid_text,
            "+FLAGS.SILENT",
            r"(\Deleted)",
        )
        if status != "OK":
            raise ImapError(f"IMAP: cannot mark uid={uid.decode(errors='ignore')} as deleted")
        # A mailbox-wide EXPUNGE would also remove unrelated messages already marked
        # deleted. Leave our flag in place if the server cannot expunge a single UID.
        if not _supports_selective_expunge(connection):
            LOGGER.info(
                "IMAP message marked deleted but not expunged because the server "
                "does not support UIDPLUS or IMAP4rev2: host=%s uid=%s",
                self.config.host,
                uid_text,
            )
            return
        status, _ = connection.uid("EXPUNGE", uid_text)
        if status != "OK":
            raise ImapError(f"IMAP: cannot expunge uid={uid.decode(errors='ignore')}")


def _split_uid_response(value: bytes | str) -> list[bytes]:
    """Normalize a whitespace-separated server UID response.

    Args:
        value: SEARCH response payload as bytes or text.

    Returns:
        UID byte strings, omitting empty fields.
    """
    if isinstance(value, str):
        value = value.encode()
    return [item for item in value.split() if item]


def _describe_search_criteria(criteria: tuple[str, ...]) -> str:
    """Describe a search strategy without including its token value.

    Args:
        criteria: Non-empty SEARCH criteria tuple; HEADER includes a header name.

    Returns:
        Criterion name, with the header name for HEADER searches.
    """
    if criteria[0] == "HEADER":
        return f"HEADER {criteria[1]}"
    return criteria[0]


def _supports_selective_expunge(connection: imaplib.IMAP4 | imaplib.IMAP4_SSL) -> bool:
    """Detect whether the server advertises UID-scoped expunging.

    Args:
        connection: IMAP connection exposing byte or string capability names.

    Returns:
        True if UIDPLUS or IMAP4rev2 is advertised, ignoring case.
    """
    capabilities = {
        item.decode("ascii", errors="ignore").upper() if isinstance(item, bytes) else item.upper()
        for item in connection.capabilities
    }
    return bool({"UIDPLUS", "IMAP4REV2"} & capabilities)


def _extract_fetch_payload(
    data: list[bytes | tuple[bytes, bytes]] | tuple[object, ...],
) -> bytes | None:
    """Extract the first literal byte payload from an IMAP FETCH response.

    Args:
        data: Mixed response sequence containing protocol bytes and literal tuples.

    Returns:
        First tuple payload containing bytes, or None when no payload is present.
    """
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None
