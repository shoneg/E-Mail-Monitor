"""SMTP delivery client with strict TLS defaults."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .models import SmtpConfig, SmtpError, TlsMode

LOGGER = logging.getLogger(__name__)


def create_tls_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Create a TLS context with certificate and hostname verification enabled."""

    return ssl.create_default_context(cafile=ca_file)


class SmtpClient:
    """Send email through one configured SMTP account."""

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    def send_message(self, sender: str, recipients: list[str], message: EmailMessage) -> None:
        """Send one message and close the SMTP connection afterwards."""

        if not recipients:
            raise SmtpError("SMTP: no recipients were provided")
        context = create_tls_context(self.config.ca_file)
        smtp: smtplib.SMTP | smtplib.SMTP_SSL | None = None
        try:
            if self.config.tls_mode is TlsMode.SSL:
                smtp = smtplib.SMTP_SSL(
                    self.config.host,
                    self.config.port,
                    context=context,
                    timeout=30,
                )
            else:
                smtp = smtplib.SMTP(self.config.host, self.config.port, timeout=30)
                if self.config.tls_mode is TlsMode.STARTTLS:
                    smtp.starttls(context=context)
                elif (
                    self.config.tls_mode is TlsMode.PLAIN
                    and not self.config.allow_insecure_plaintext
                ):
                    raise SmtpError("SMTP: plaintext mode is not allowed by configuration")
            smtp.login(self.config.username, self.config.password)
            smtp.send_message(message, from_addr=sender, to_addrs=recipients)
            LOGGER.debug(
                "SMTP message sent via host=%s port=%s",
                self.config.host,
                self.config.port,
            )
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise SmtpError(
                f"SMTP: delivery failed for host={self.config.host} port={self.config.port}: "
                f"{exc.__class__.__name__}"
            ) from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except (smtplib.SMTPException, OSError):
                    smtp.close()
