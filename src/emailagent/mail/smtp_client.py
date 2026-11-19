"""SMTP sending with an explicit, duplicate-safe outcome.

Outcomes
--------
SENT         the server accepted the message (250 after DATA).
TRANSIENT    nothing was accepted (connect/login/envelope problem or a 4xx refusal).
             Safe to retry automatically.
REJECTED     the server refused for good (5xx, bad recipient, certificate problem).
             Nothing was delivered. Retrying will not help.
AUTH_FAILED  login refused. Not retried automatically (avoids locking the mailbox).
UNKNOWN      the connection broke AFTER the message body was transmitted, so the server may
             or may not have delivered it. NEVER retried automatically: a human (or a
             Sent-folder reconciliation using the fixed Message-ID) must decide.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from enum import StrEnum
from typing import Any

from emailagent.core.config import MailAccountConfig
from emailagent.core.logging import redact
from emailagent.mail.message_builder import envelope_recipients

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30
SmtpFactory = Callable[..., Any]


class SmtpOutcome(StrEnum):
    SENT = "sent"
    TRANSIENT = "transient"
    REJECTED = "rejected"
    AUTH_FAILED = "auth_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SmtpResult:
    outcome: SmtpOutcome
    detail: str
    code: int | None = None

    @property
    def safe_to_retry(self) -> bool:
        return self.outcome == SmtpOutcome.TRANSIENT


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _connection_problem(exc: BaseException, host: str, port: int) -> tuple[SmtpOutcome, str]:
    if isinstance(exc, ssl.SSLCertVerificationError):
        detail = getattr(exc, "verify_message", None) or "verification failed"
        return SmtpOutcome.REJECTED, (
            f"The server's TLS certificate did not match '{host}' ({detail}). Not bypassed. "
            "Check the outgoing-server hostname in your hosting panel."
        )
    if isinstance(exc, ssl.SSLError):
        return SmtpOutcome.TRANSIENT, f"TLS handshake with {host}:{port} failed (port 465 expects SSL/TLS)."
    if isinstance(exc, socket.gaierror):
        return SmtpOutcome.TRANSIENT, f"Could not resolve '{host}'. Check the hostname and your internet connection."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SmtpOutcome.TRANSIENT, f"Timed out talking to {host}:{port} (firewall/VPN may block port 465)."
    if isinstance(exc, ConnectionRefusedError):
        return SmtpOutcome.TRANSIENT, f"{host}:{port} refused the connection. Check the port."
    return SmtpOutcome.TRANSIENT, f"Connection problem ({type(exc).__name__})."


def _by_code(code: int, response: Any, what: str) -> SmtpResult:
    detail = redact(f"{what}: {code} {_text(response)[:200]}")
    outcome = SmtpOutcome.TRANSIENT if 400 <= code < 500 else SmtpOutcome.REJECTED
    return SmtpResult(outcome, detail, code)


class SmtpSender:
    """One connection per message (simple and robust at this volume)."""

    def __init__(
        self,
        account: MailAccountConfig,
        *,
        smtp_factory: SmtpFactory | None = None,
        ssl_context: ssl.SSLContext | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._account = account
        self._factory = smtp_factory or smtplib.SMTP_SSL
        self._ssl_context = ssl_context
        self._timeout = timeout

    def send(self, msg: EmailMessage) -> SmtpResult:
        acc = self._account
        if not acc.password_is_set:
            return SmtpResult(
                SmtpOutcome.AUTH_FAILED, f"No password set (MAIL_{acc.key.upper()}_PASSWORD in .env)."
            )
        sender = parseaddr(str(msg["From"]))[1]
        recipients = envelope_recipients(msg)
        if not sender or not recipients:
            return SmtpResult(SmtpOutcome.REJECTED, "Message has no valid sender or recipient.")
        raw = msg.as_bytes(policy=policy.SMTP)

        stage = "connect"
        server: Any = None
        try:
            ctx = self._ssl_context or ssl.create_default_context()  # always verifies certificates
            server = self._factory(acc.smtp_host, acc.smtp_port, timeout=self._timeout, context=ctx)
            stage = "login"
            server.login(acc.address, acc.password.get_secret_value())
            stage = "envelope"
            code, resp = server.mail(sender)
            if code != 250:
                return _by_code(code, resp, "Sender refused")
            for rcpt in recipients:
                code, resp = server.rcpt(rcpt)
                if code not in (250, 251):
                    return _by_code(code, resp, f"Recipient {rcpt} refused")
            stage = "data"
            code, resp = server.data(raw)
            if code == 250:
                log.info("smtp accepted", extra={"ctx": {"account": acc.key, "recipients": len(recipients)}})
                return SmtpResult(SmtpOutcome.SENT, redact(_text(resp)[:200]), code)
            return _by_code(code, resp, "Message refused after DATA")
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError) as exc:
            return SmtpResult(
                SmtpOutcome.AUTH_FAILED,
                "Login rejected. Check the password in .env and that the username is the full email "
                f"address. Not retried automatically ({type(exc).__name__}).",
                getattr(exc, "smtp_code", None),
            )
        except smtplib.SMTPResponseException as exc:  # server replied with an error code
            return _by_code(exc.smtp_code, exc.smtp_error, f"Server error at {stage}")
        except (OSError, smtplib.SMTPException) as exc:  # connection dropped / timed out
            if stage == "data":
                return SmtpResult(
                    SmtpOutcome.UNKNOWN,
                    "Connection failed AFTER the message was transmitted; it may or may not have "
                    f"been delivered ({type(exc).__name__}). Check the Sent folder before doing anything.",
                )
            outcome, detail = _connection_problem(exc, acc.smtp_host, acc.smtp_port)
            return SmtpResult(outcome, detail)
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001 - best effort
                    log.debug("smtp quit failed (ignored)")
