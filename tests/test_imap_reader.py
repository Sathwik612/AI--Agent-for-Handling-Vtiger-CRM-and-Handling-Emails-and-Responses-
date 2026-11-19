from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from imapclient.exceptions import LoginError
from pydantic import SecretStr

from emailagent.core.config import MailAccountConfig
from emailagent.mail.diagnostics import run_imap_test
from emailagent.mail.imap_client import ImapError, ImapReader, describe_error

PASSWORD = "S3cret!pw"


def _account(password: str = PASSWORD) -> MailAccountConfig:
    return MailAccountConfig(
        key="kaitlyn", address="kaitlyn@example.com", password=SecretStr(password),
        market="Ohio OST", timezone="America/New_York",
        imap_host="mail.example.com", imap_port=993, smtp_host="mail.example.com",
    )


def _rfc822(n: int, extra: str = "") -> bytes:
    return (f"From: Parent <p{n}@school.org>\r\nTo: kaitlyn@example.com\r\nSubject: Question {n}\r\n"
            f"Message-ID: <q{n}@school.org>\r\nDate: Wed, 18 Nov 2026 09:30:00 -0500\r\n{extra}\r\n"
            f"Body of message {n}").encode()


class FakeClient:
    """Mimics IMAPClient and records every call, so tests can assert on what was requested."""

    def __init__(self, host: str, port: int, ssl: bool, timeout: int, *, messages: dict[int, bytes] | None = None):
        self.init_args = (host, port, ssl, timeout)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.messages = messages if messages is not None else {1: _rfc822(1), 2: _rfc822(2), 7: _rfc822(7)}
        self.logged_out = False
        self.fail_login = False

    normalise_times = True

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user,), {}))  # password deliberately not recorded
        if self.fail_login:
            raise LoginError("[AUTHENTICATIONFAILED] bad credentials " + password)

    def capabilities(self):
        return (b"IMAP4REV1", b"IDLE", b"UIDPLUS")

    def list_folders(self):
        return [((b"\\HasNoChildren",), b".", "INBOX"), ((b"\\Sent",), b".", "INBOX.Sent"),
                ((), b".", "INBOX.Drafts")]

    def select_folder(self, name, readonly=False):
        self.calls.append(("select_folder", (name,), {"readonly": readonly}))
        return {b"UIDVALIDITY": 1700000000, b"EXISTS": len(self.messages)}

    def search(self, criteria):
        self.calls.append(("search", (criteria,), {}))
        if criteria[0] == "UID":
            start = int(criteria[1].split(":")[0])
            top = max(self.messages)
            return [u for u in self.messages if u >= start] or [top]  # IMAP N:* quirk
        return list(self.messages)

    def fetch(self, uids, data):
        self.calls.append(("fetch", (list(uids), list(data)), {}))
        out: dict[int, dict[bytes, Any]] = {}
        for u in uids:
            raw = self.messages[u]
            item: dict[bytes, Any] = {b"RFC822.SIZE": len(raw)}
            if "BODY.PEEK[]" in data:
                item[b"BODY[]"] = raw  # real servers answer PEEK with the BODY[] key
                item[b"FLAGS"] = ()
                item[b"INTERNALDATE"] = datetime(2026, 11, 18, 16, 9, tzinfo=timezone(timedelta(hours=-5)))
            out[u] = item
        return out

    def logout(self) -> None:
        self.logged_out = True


def _factory(holder: list[FakeClient], **kw: Any):
    def make(host: str, port: int, ssl: bool, timeout: int) -> FakeClient:
        c = FakeClient(host, port, ssl, timeout, **kw)
        holder.append(c)
        return c
    return make


def test_connects_with_ssl_and_logs_in_with_full_address() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)):
        pass
    c = made[0]
    assert c.init_args == ("mail.example.com", 993, True, 30)
    assert c.calls[0] == ("login", ("kaitlyn@example.com",), {})
    assert c.logged_out


def test_folder_is_opened_read_only() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        state = r.open_folder("INBOX")
    assert state.uidvalidity == 1700000000 and state.exists == 3
    assert ("select_folder", ("INBOX",), {"readonly": True}) in made[0].calls


def test_fetch_uses_peek_so_messages_are_not_marked_read() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        r.open_folder()
        got = r.fetch_raw([1, 2])
    fetches = [c for c in made[0].calls if c[0] == "fetch"]
    body_requests = [c[1][1] for c in fetches if any("BODY" in d for d in c[1][1])]
    assert body_requests and all("BODY.PEEK[]" in req for req in body_requests)
    assert not any(d in ("RFC822", "BODY[]") for _, (_, data), _ in fetches for d in data)  # would set \Seen
    assert [m.uid for m in got] == [1, 2] and got[0].raw is not None


def test_no_write_operations_are_ever_called() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        r.open_folder()
        r.fetch_raw(r.latest_uids(5))
        r.uids_after(1)
        r.list_folders()
        r.supports_idle()
    used = {name for name, _, _ in made[0].calls}
    assert used <= {"login", "select_folder", "search", "fetch"}  # no store/copy/move/delete/append


def test_latest_uids_and_uids_after_quirk() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        r.open_folder()
        assert r.latest_uids(2) == [2, 7]
        assert r.uids_after(2) == [7]
        assert r.uids_after(7) == []  # server returns [7] for "8:*"; must be filtered out


def test_idle_and_sent_folder_detection() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        assert r.supports_idle()
        assert [f.name for f in r.list_folders() if f.is_sent] == ["INBOX.Sent"]


def test_oversized_message_is_skipped_not_downloaded() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        r.open_folder()
        got = r.fetch_raw([1], max_bytes=10)
    assert got[0].raw is None and "larger than" in (got[0].skipped_reason or "")
    assert not any("BODY.PEEK[]" in c[1][1] for c in made[0].calls if c[0] == "fetch")


def test_login_failure_is_friendly_and_never_leaks_password() -> None:
    def make(host, port, ssl, timeout):
        c = FakeClient(host, port, ssl, timeout)
        c.fail_login = True
        return c

    with pytest.raises(ImapError) as err:
        ImapReader(_account(), client_factory=make).__enter__()
    msg = str(err.value)
    assert "Login rejected" in msg and PASSWORD not in msg
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_missing_password_is_reported_before_any_network_call() -> None:
    made: list[FakeClient] = []
    with pytest.raises(ImapError, match="MAIL_KAITLYN_PASSWORD"):
        ImapReader(_account(password=""), client_factory=_factory(made)).__enter__()
    assert made == []


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(-2, "Name or service not known"), "resolve the hostname"),
        (TimeoutError(), "Timed out"),
        (ConnectionRefusedError(), "refused"),
        (ssl.SSLCertVerificationError(1, "hostname mismatch"), "certificate"),
        (ssl.SSLError("handshake"), "TLS handshake"),
    ],
)
def test_error_descriptions(exc: Exception, expected: str) -> None:
    assert expected in describe_error(exc, _account())


def test_diagnostics_pass_output_has_required_fields_and_no_secrets() -> None:
    lines: list[str] = []
    made: list[FakeClient] = []
    code = run_imap_test(_account(), limit=2, preview=True, out=lines.append,
                         reader_factory=lambda a: ImapReader(a, client_factory=_factory(made)))
    text = "\n".join(lines)
    assert code == 0 and "RESULT: PASS" in text
    for expected in ("From       : Parent <p7@school.org>", "Subject    : Question 7",
                     "Message-ID : <q7@school.org>", "Body       :", "IMAP IDLE support ...... yes",
                     "INBOX.Sent", "read-only"):
        assert expected in text, expected
    assert PASSWORD not in text


def test_diagnostics_hide_body_text_unless_preview_requested() -> None:
    made: list[FakeClient] = []
    lines: list[str] = []
    run_imap_test(_account(), limit=1, preview=False, out=lines.append,
                  reader_factory=lambda a: ImapReader(a, client_factory=_factory(made)))
    assert "Body of message" not in "\n".join(lines)


def test_diagnostics_survive_a_malformed_message() -> None:
    made: list[FakeClient] = []
    lines: list[str] = []
    msgs = {1: _rfc822(1), 2: b"\x00\xff garbage with no headers at all"}
    code = run_imap_test(_account(), limit=2, out=lines.append,
                         reader_factory=lambda a: ImapReader(a, client_factory=_factory(made, messages=msgs)))
    assert code == 0 and "UID 1" in "\n".join(lines) and "UID 2" in "\n".join(lines)


def test_diagnostics_report_failure_with_exit_code_1() -> None:
    def make(host, port, ssl, timeout):
        c = FakeClient(host, port, ssl, timeout)
        c.fail_login = True
        return c

    lines: list[str] = []
    code = run_imap_test(_account(), out=lines.append, reader_factory=lambda a: ImapReader(a, client_factory=make))
    assert code == 1 and "FAILED: Login rejected" in "\n".join(lines) and PASSWORD not in "\n".join(lines)


def test_received_time_is_the_servers_internaldate_in_utc() -> None:
    made: list[FakeClient] = []
    with ImapReader(_account(), client_factory=_factory(made)) as r:
        r.open_folder()
        got = r.fetch_raw([1])
    assert got[0].received_at == datetime(2026, 11, 18, 21, 9, tzinfo=UTC)  # 16:09 at UTC-5
    assert got[0].received_at.utcoffset() == timedelta(0)
    assert made[0].normalise_times is False  # asks imapclient for timezone-aware values


def test_imap_test_output_shows_window_and_quote_split() -> None:
    from emailagent.core.window import ProcessingWindow

    made: list[FakeClient] = []
    lines: list[str] = []
    body = ("Thanks, will do.\r\n\r\nOn Wed, Nov 18, 2026 at 3:00 PM Kaitlyn <k@example.com> wrote:\r\n> old text here")
    msgs = {5: (f"From: P <p@school.org>\r\nTo: k@example.com\r\nSubject: Re: q\r\nMessage-ID: <q@x>\r\n\r\n{body}").encode()}
    win = ProcessingWindow.from_settings("18:30", "10:00", "Asia/Kolkata")
    run_imap_test(_account(), limit=1, out=lines.append, window=win,
                  reader_factory=lambda a: ImapReader(a, client_factory=_factory(made, messages=msgs)))
    text = "\n".join(lines)
    assert "Received   : 2026-11-18 21:09 UTC  = 02:39 Asia/Kolkata, INSIDE the off-hours window" in text
    assert "new + " in text and "quoted history" in text
