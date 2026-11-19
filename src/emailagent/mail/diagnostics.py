"""Operator-facing checks, printed to the operator's own terminal (never to log files).

* ``imap-test``: read-only health check of one mailbox.
* ``smtp-test``: sends ONE clearly-labelled test email to an address the operator chooses.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from emailagent.core.config import MailAccountConfig
from emailagent.core.window import ProcessingWindow
from emailagent.mail.imap_client import ImapError, ImapReader
from emailagent.mail.message_builder import MessageBuildError, build_message, generate_message_id
from emailagent.mail.parser import DEFAULT_AUTOMATED_DOMAINS, parse_email
from emailagent.mail.sent_copy import SentCopyWriter
from emailagent.mail.smtp_client import SmtpOutcome, SmtpSender

PREVIEW_CHARS = 120
Printer = Callable[[str], None]


def run_imap_test(
    account: MailAccountConfig,
    *,
    limit: int = 5,
    preview: bool = False,
    out: Printer = print,
    reader_factory: Callable[[MailAccountConfig], Any] = ImapReader,
    window: ProcessingWindow | None = None,
    automated_domains: frozenset[str] = DEFAULT_AUTOMATED_DOMAINS,
) -> int:
    out(f"Mailbox : {account.address}  [{account.market}]")
    out(f"Server  : {account.imap_host}:{account.imap_port} (SSL/TLS)")
    problems = 0
    try:
        with reader_factory(account) as reader:
            out("Connect + login ........ OK")
            out(f"IMAP IDLE support ...... {'yes' if reader.supports_idle() else 'no'}")

            folders = reader.list_folders()
            sent = [f.name for f in folders if f.is_sent]
            out(f"Folders ({len(folders)}) ......... " + ", ".join(f.name for f in folders[:12])
                + (" ..." if len(folders) > 12 else ""))
            out(f"Sent folder detected ... {', '.join(sent) if sent else 'NOT FOUND (tell me the name)'}")

            state = reader.open_folder("INBOX")
            out(f"INBOX .................. {state.exists} messages, UIDVALIDITY {state.uidvalidity}")
            out("Mode ................... read-only (nothing is marked as read, moved or deleted)")

            uids = reader.latest_uids(limit)
            out(f"\nLatest {len(uids)} message(s):")
            for raw in reader.fetch_raw(uids):
                out(f"\n  UID {raw.uid}")
                if raw.raw is None:
                    out(f"    skipped: {raw.skipped_reason}")
                    continue
                try:
                    msg = parse_email(raw.raw, automated_domains=automated_domains)
                except Exception as exc:  # noqa: BLE001 - a malformed email must not stop the test
                    problems += 1
                    out(f"    could not parse this message ({type(exc).__name__})")
                    continue
                when = msg.date.strftime("%Y-%m-%d %H:%M UTC") if msg.date else "no date"
                sender = f"{msg.from_name} <{msg.from_addr}>" if msg.from_name else (msg.from_addr or "unknown")
                out(f"    Date       : {when}")
                if raw.received_at is not None:
                    line = f"    Received   : {raw.received_at.strftime('%Y-%m-%d %H:%M UTC')}"
                    if window is not None:
                        local = raw.received_at.astimezone(window.tz)
                        inside = "INSIDE" if window.is_off_hours(raw.received_at) else "outside"
                        line += f"  = {local.strftime('%H:%M')} {window.tz.key}, {inside} the off-hours window"
                    out(line)
                out(f"    From       : {sender}")
                out(f"    Subject    : {msg.subject or '(none)'}")
                out(f"    Message-ID : {msg.message_id}{'  (generated: header was missing)' if msg.message_id_is_synthetic else ''}")
                if msg.in_reply_to:
                    out(f"    In-Reply-To: {msg.in_reply_to}  ({len(msg.references)} reference(s))")
                body_line = f"    Body       : {len(msg.body_text)} characters ({msg.body_source})"
                if msg.quoted_text:
                    body_line += f": {len(msg.new_text)} new + {len(msg.quoted_text)} quoted history"
                out(body_line)
                if msg.attachment_names:
                    out(f"    Attachments: {len(msg.attachment_names)}")
                if msg.is_automated:
                    out(f"    Automated  : yes ({msg.automated_reason}), would be skipped before any AI call")
                if preview and msg.new_text:
                    out(f"    New text   : {' '.join(msg.new_text.split())[:PREVIEW_CHARS]}")
    except ImapError as exc:
        out(f"\nFAILED: {exc}")
        return 1

    out("\nRESULT: PASS" if problems == 0 else f"\nRESULT: PASS with {problems} unparseable message(s)")
    return 0


def run_smtp_test(
    account: MailAccountConfig,
    to_addr: str,
    *,
    confirm: Callable[[str], bool],
    save_copy: bool = True,
    out: Printer = print,
    sender_factory: Callable[[MailAccountConfig], Any] = SmtpSender,
    writer_factory: Callable[[MailAccountConfig], Any] = SentCopyWriter,
    now: datetime | None = None,
) -> int:
    """Exit codes: 0 sent (and copy saved or not requested); 1 not sent; 2 bad input; 3 sent but
    the Sent-folder copy could not be saved (the email itself WAS delivered)."""
    recipient = parseaddr(to_addr)[1]
    if not recipient or "@" not in recipient or any(c in to_addr for c in ",; \r\n"):
        out(f"FAILED: '{to_addr}' is not a single valid email address.")
        return 2

    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    message_id = generate_message_id(account.address)
    try:
        msg = build_message(
            from_addr=account.address, from_name=account.display_name, to_addrs=[recipient],
            subject=f"Email Agent SMTP test ({stamp})",
            body=(
                "This is a test message from your Email Agent project (Milestone 3).\n\n"
                f"If you can read this, outgoing mail from {account.address} through "
                f"{account.smtp_host} works.\n\nMessage-ID: {message_id}\n\nNo action is needed."
            ),
            message_id=message_id, date=now,
        )
    except MessageBuildError as exc:
        out(f"FAILED: {exc}")
        return 2

    out("About to send ONE real email:")
    shown = f"{account.display_name} <{account.address}>" if account.display_name else account.address
    out(f"  From    : {shown}  via {account.smtp_host}:{account.smtp_port} (SSL/TLS)")
    out(f"  To      : {recipient}")
    out(f"  Subject : {msg['Subject']}")
    if not confirm("Type YES (capitals) to send, anything else cancels: "):
        out("Cancelled. Nothing was sent.")
        return 1

    result = sender_factory(account).send(msg)
    out(f"\nSMTP result: {result.outcome.value.upper()}  {result.detail}")
    if result.outcome != SmtpOutcome.SENT:
        advice = {
            SmtpOutcome.TRANSIENT: "Nothing was sent. Safe to try again.",
            SmtpOutcome.REJECTED: "Nothing was sent. Retrying will not help; fix the cause above.",
            SmtpOutcome.AUTH_FAILED: "Nothing was sent. Check the password in .env. Do not retry repeatedly.",
            SmtpOutcome.UNKNOWN: "UNCERTAIN: check the recipient's inbox and the Sent folder before retrying.",
        }[result.outcome]
        out(advice)
        out("\nRESULT: FAIL")
        return 1

    if not save_copy:
        out("Sent copy: skipped (--no-sent-copy)")
        out("\nRESULT: PASS (email sent)")
        return 0
    try:
        folder = writer_factory(account).save_copy(msg)
        out(f"Sent copy: saved in folder '{folder}'")
    except ImapError as exc:
        out(f"Sent copy: NOT saved ({exc})")
        out("The email WAS delivered; only the webmail Sent copy is missing.")
        out("\nRESULT: PASS with warning")
        return 3
    out("\nRESULT: PASS (email sent and copy saved)")
    return 0
