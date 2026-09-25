from __future__ import annotations

from email.message import EmailMessage

import pytest

from mailflow_monitor.models import SmtpConfig, SmtpError, TlsMode
from mailflow_monitor.smtp_client import SmtpClient


class PartiallyRefusingSmtp:
    def __init__(self, *args, **kwargs) -> None:
        """Initialize an open fake SMTP connection.

        Args:
            *args: Unused positional connection arguments.
            **kwargs: Unused keyword connection arguments.

        Returns:
            None.
        """
        self.closed = False

    def login(self, username: str, password: str) -> None:
        """Accept credentials without performing authentication.

        Args:
            username: Unused SMTP username.
            password: Unused SMTP password.

        Returns:
            None.
        """
        return None

    def send_message(self, message, from_addr: str, to_addrs: list[str]):
        """Simulate a send where one recipient is refused.

        Args:
            message: Unused email payload.
            from_addr: Unused envelope sender.
            to_addrs: Unused envelope recipient list.

        Returns:
            Refusal mapping for refused@example.net with SMTP status 550.
        """
        return {"refused@example.net": (550, b"recipient rejected")}

    def quit(self) -> None:
        """Mark the fake SMTP connection closed.

        Returns:
            None.
        """
        self.closed = True

    def close(self) -> None:
        """Mark the fake SMTP connection closed.

        Returns:
            None.
        """
        self.closed = True


def test_partial_recipient_refusal_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify partial SMTP acceptance raises a delivery error and closes the connection.

    Args:
        monkeypatch: Pytest fixture for temporary environment or dependency replacements.

    Returns:
        None; assertions verify the expected behavior.
    """
    smtp = PartiallyRefusingSmtp()
    monkeypatch.setattr(
        "mailflow_monitor.smtp_client.smtplib.SMTP_SSL",
        lambda *args, **kwargs: smtp,
    )
    config = SmtpConfig(
        host="smtp.example.net",
        port=465,
        tls_mode=TlsMode.SSL,
        username="user",
        password="secret",
    )
    message = EmailMessage()
    message["From"] = "sender@example.net"
    message["To"] = "accepted@example.net, refused@example.net"
    message["Subject"] = "test"
    message.set_content("test")

    with pytest.raises(SmtpError, match=r"1 recipient\(s\) refused"):
        SmtpClient(config).send_message(
            "sender@example.net",
            ["accepted@example.net", "refused@example.net"],
            message,
        )

    assert smtp.closed is True
