"""Turn raw RFC 822 bytes into a clean, safe ``ParsedEmail``.

Uses Python's standard ``email`` package (policy.default) for MIME, charset and header decoding.
The only regex is for the References header, which is a plain list of ``<id>`` tokens.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from emailagent.mail.quoted import split_reply

_MSGID_RE = re.compile(r"<[^<>\s]+>")
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
DEFAULT_AUTOMATED_DOMAINS: frozenset[str] = frozenset({"calendly.com"})
_AUTOMATED_LOCALPART = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|bounces?|newsletter)([-_+.].*)?$"
)
_AUTO_SUBJECT_PREFIXES = ("automatic reply", "auto-reply", "autoreply", "out of office", "undeliverable")


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    message_id_is_synthetic: bool
    in_reply_to: str | None
    references: list[str]
    from_addr: str | None
    from_name: str | None
    to_addrs: list[str]
    cc_addrs: list[str]
    reply_to_addrs: list[str]
    subject: str | None
    date: datetime | None  # timezone-aware, UTC
    body_text: str
    body_source: str  # "plain" | "html" | "none"
    new_text: str = ""  # what the sender just wrote (quoted history removed)
    quoted_text: str = ""  # the quoted thread history underneath
    attachment_names: list[str] = field(default_factory=list)
    is_automated: bool = False
    automated_reason: str | None = None


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return _tidy(parser.text())


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _header(msg: Message, name: str) -> str | None:
    value = msg.get(name)
    if value is None:
        return None
    # Header values can contain folding newlines; collapse them.
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned or None


def _addresses(msg: Message, name: str) -> list[str]:
    values = [str(v) for v in msg.get_all(name, [])]
    out: list[str] = []
    for _display, addr in getaddresses(values):
        addr = addr.strip().lower()
        if addr and "@" in addr and addr not in out:
            out.append(addr)
    return out


def _parse_date(msg: Message) -> datetime | None:
    raw = _header(msg, "Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _part_text(part: Message) -> str:
    """Decode a text part; never raise on a bad/unknown charset."""
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        return content if isinstance(content, str) else ""
    except (LookupError, UnicodeError, AssertionError):
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return ""


def _extract_body(msg: EmailMessage) -> tuple[str, str]:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = _tidy(_part_text(plain))
        if text:
            return text, "plain"
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        text = html_to_text(_part_text(html))
        if text:
            return text, "html"
    return "", "none"


def _attachment_names(msg: EmailMessage) -> list[str]:
    names: list[str] = []
    for part in msg.iter_attachments():
        names.append(part.get_filename() or f"({part.get_content_type()})")
    return names


def _detect_automated(
    msg: Message, from_addr: str | None, subject: str | None, domains: frozenset[str]
) -> str | None:
    auto = (_header(msg, "Auto-Submitted") or "").lower()
    if auto and auto != "no":
        return f"auto-submitted:{auto}"
    if (_header(msg, "Precedence") or "").lower() in {"bulk", "junk", "list", "auto_reply"}:
        return "precedence"
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return "mailing-list"
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Auto-Response-Suppress"):
        return "auto-response-header"
    if msg.get_content_type() == "multipart/report":
        return "delivery-report"
    local, _, domain = (from_addr or "").partition("@")
    if local in {"mailer-daemon", "postmaster"}:
        return "mailer-daemon"
    if any(domain == d or domain.endswith("." + d) for d in domains):
        return "known-notification-domain"
    if _AUTOMATED_LOCALPART.match(local):
        return "no-reply-or-notification-sender"
    if subject and subject.lower().startswith(_AUTO_SUBJECT_PREFIXES):
        return "auto-subject"
    return None


def parse_email(
    raw: bytes, *, automated_domains: frozenset[str] = DEFAULT_AUTOMATED_DOMAINS
) -> ParsedEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)

    subject = _header(msg, "Subject")
    from_list = getaddresses([str(v) for v in msg.get_all("From", [])])
    from_name, from_addr = (from_list[0] if from_list else ("", ""))
    from_addr = from_addr.strip().lower() or None

    message_id = _header(msg, "Message-ID")
    synthetic = False
    if not message_id or not _MSGID_RE.fullmatch(message_id):
        # No usable Message-ID: derive a stable one from the content so re-reads dedupe.
        digest = hashlib.sha256(raw).hexdigest()[:40]
        message_id, synthetic = f"<synthetic-{digest}@email-agent.local>", True

    in_reply_to_ids = _MSGID_RE.findall(_header(msg, "In-Reply-To") or "")
    body, source = _extract_body(msg)
    reason = _detect_automated(msg, from_addr, subject, automated_domains)
    split = split_reply(body) if body else None

    return ParsedEmail(
        message_id=message_id,
        message_id_is_synthetic=synthetic,
        in_reply_to=in_reply_to_ids[0] if in_reply_to_ids else None,
        references=_MSGID_RE.findall(_header(msg, "References") or ""),
        from_addr=from_addr,
        from_name=(from_name or None),
        to_addrs=_addresses(msg, "To"),
        cc_addrs=_addresses(msg, "Cc"),
        reply_to_addrs=_addresses(msg, "Reply-To"),
        subject=subject,
        date=_parse_date(msg),
        body_text=body,
        body_source=source,
        new_text=split.new_text if split else "",
        quoted_text=split.quoted_text if split else "",
        attachment_names=_attachment_names(msg),
        is_automated=reason is not None,
        automated_reason=reason,
    )
