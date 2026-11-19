"""Build outgoing emails.

* The Message-ID is generated *before* sending and stays the same on every retry, so an
  ambiguous send can later be reconciled against the Sent folder.
* Replies carry In-Reply-To / References so they land in the customer's existing thread.
* Header values containing CR/LF are rejected (header-injection protection).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, parseaddr

MAX_REFERENCES = 20
_RE_PREFIX = re.compile(r"^\s*(re|aw|sv)\s*:\s*", re.IGNORECASE)


class MessageBuildError(ValueError):
    """The message could not be built safely (bad address, header injection, ...)."""


def generate_message_id(from_addr: str) -> str:
    domain = from_addr.rsplit("@", 1)[-1].strip().lower()
    if not domain or " " in domain:
        raise MessageBuildError(f"Cannot derive a Message-ID domain from {from_addr!r}")
    return f"<{uuid.uuid4().hex}@{domain}>"


def reply_subject(original: str | None) -> str:
    """'Quote request' -> 'Re: Quote request'. Already-prefixed subjects are left alone."""
    base = (original or "").strip()
    if _RE_PREFIX.match(base):
        return base
    return f"Re: {base}" if base else "Re:"


def _clean_address(value: str) -> str:
    _name, addr = parseaddr(value)
    if not addr or "@" not in addr or any(c in value for c in "\r\n,;"):
        raise MessageBuildError(f"Invalid email address: {value!r}")
    return addr


def _threading(in_reply_to: str | None, references: list[str] | None) -> tuple[str | None, str | None]:
    refs = list(references or [])
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)
    if len(refs) > MAX_REFERENCES:  # keep the thread root plus the most recent entries
        refs = [refs[0], *refs[-(MAX_REFERENCES - 1):]]
    return in_reply_to, (" ".join(refs) if refs else None)


def build_message(
    *,
    from_addr: str,
    from_name: str | None,
    to_addrs: list[str],
    subject: str,
    body: str,
    message_id: str,
    cc_addrs: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    date: datetime | None = None,
) -> EmailMessage:
    if not to_addrs:
        raise MessageBuildError("At least one recipient is required")
    msg = EmailMessage(policy=policy.SMTP)
    try:
        sender = _clean_address(from_addr)
        msg["From"] = formataddr((from_name, sender)) if from_name else sender
        msg["To"] = ", ".join(_clean_address(a) for a in to_addrs)
        if cc_addrs:
            msg["Cc"] = ", ".join(_clean_address(a) for a in cc_addrs)
        msg["Subject"] = subject
        msg["Date"] = format_datetime(date or datetime.now(UTC))
        msg["Message-ID"] = message_id
        irt, refs = _threading(in_reply_to, references)
        if irt:
            msg["In-Reply-To"] = irt
        if refs:
            msg["References"] = refs
    except ValueError as exc:  # includes header values with CR/LF
        raise MessageBuildError(f"Unsafe or invalid header: {exc}") from None
    msg.set_content(body)
    return msg


def envelope_recipients(msg: EmailMessage) -> list[str]:
    """All real recipients (To + Cc). There is deliberately no Bcc support."""
    out: list[str] = []
    for header in ("To", "Cc"):
        for value in msg.get_all(header, []):
            for part in str(value).split(","):
                addr = parseaddr(part)[1]
                if addr and addr not in out:
                    out.append(addr)
    return out
