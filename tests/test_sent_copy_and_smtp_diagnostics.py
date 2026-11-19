from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from imapclient import SEEN
from pydantic import SecretStr

from emailagent.core.config import MailAccountConfig
from emailagent.mail.diagnostics import run_smtp_test
from emailagent.mail.imap_client import ImapError
from emailagent.mail.message_builder import build_message
from emailagent.mail.sent_copy import SentCopyWriter
from emailagent.mail.smtp_client import SmtpOutcome, SmtpResult

PASSWORD = "S3cret!pw"


def _account(sent_folder: str | None = None, password: str = PASSWORD) -> MailAccountConfig:
    return MailAccountConfig(
        key="patricia", address="patricia@example.com", password=SecretStr(password), market="Texas STAAR",
        timezone="America/Chicago", imap_host="mail.example.com", smtp_host="mail.example.com",
        sent_folder=sent_folder,
    )


def _msg():
    return build_message(from_addr="patricia@example.com", from_name=None, to_addrs=["a@b.com"],
                         subject="Re: x", body="Hi", message_id="<o1@example.com>")


class FakeImap:
    def __init__(self, folders=None, fail_append: Exception | None = None):
        self.folders = folders if folders is not None else [
            ((b"\\HasNoChildren",), b".", "INBOX"), ((), b".", "INBOX.Sent"), ((), b".", "INBOX.Drafts")]
        self.fail_append, self.appended, self.logged_out, self.logins = fail_append, [], False, []

    def login(self, user, password):
        self.logins.append(user)

    def list_folders(self):
        return self.folders

    def append(self, folder, msg, flags=(), msg_time=None):
        if self.fail_append:
            raise self.fail_append
        self.appended.append((folder, msg, tuple(flags), msg_time))

    def logout(self):
        self.logged_out = True


def _writer(fake: FakeImap, account=None) -> SentCopyWriter:
    return SentCopyWriter(account or _account(), client_factory=lambda *a, **k: fake)


# ------------------------------------------------------------------ Sent copy
def test_copy_goes_to_detected_sent_folder_marked_read() -> None:
    fake = FakeImap()
    assert _writer(fake).save_copy(_msg()) == "INBOX.Sent"
    folder, raw, flags, when = fake.appended[0]
    assert folder == "INBOX.Sent" and flags == (SEEN,) and b"Message-ID: <o1@example.com>" in raw
    assert when.tzinfo is not None and fake.logged_out


def test_sent_flag_wins_over_name_guessing() -> None:
    fake = FakeImap(folders=[((b"\\Sent",), b"/", "Outbox-Archive"), ((), b"/", "Sent")])
    assert _writer(fake).save_copy(_msg()) == "Outbox-Archive"


def test_explicit_folder_override_is_respected() -> None:
    fake = FakeImap()
    assert _writer(fake, _account(sent_folder="INBOX.Archive")).save_copy(_msg()) == "INBOX.Archive"


def test_missing_sent_folder_is_a_clear_error_and_nothing_is_appended() -> None:
    fake = FakeImap(folders=[((), b".", "INBOX")])
    with pytest.raises(ImapError, match="MAIL_PATRICIA_SENT_FOLDER"):
        _writer(fake).save_copy(_msg())
    assert fake.appended == []


def test_append_failure_is_friendly_and_leaks_nothing() -> None:
    fake = FakeImap(fail_append=RuntimeError("boom " + PASSWORD))
    with pytest.raises(ImapError) as err:
        _writer(fake).save_copy(_msg())
    assert PASSWORD not in str(err.value) and fake.logged_out


def test_missing_password_never_connects() -> None:
    called: list[Any] = []
    w = SentCopyWriter(_account(password=""), client_factory=lambda *a, **k: called.append(1))
    with pytest.raises(ImapError):
        w.save_copy(_msg())
    assert called == []


# ------------------------------------------------------------------ smtp-test command
class StubSender:
    def __init__(self, result: SmtpResult):
        self.result, self.sent = result, []

    def send(self, msg):
        self.sent.append(msg)
        return self.result


class StubWriter:
    def __init__(self, error: str | None = None):
        self.error, self.saved = error, []

    def save_copy(self, msg):
        if self.error:
            raise ImapError(self.error)
        self.saved.append(msg)
        return "INBOX.Sent"


OK = SmtpResult(SmtpOutcome.SENT, "2.0.0 queued", 250)
NOW = datetime(2026, 11, 20, 4, 30, tzinfo=UTC)


def _run(to="me@example.com", *, answer=True, result=OK, writer=None, save_copy=True):
    sender, writer = StubSender(result), writer or StubWriter()
    lines: list[str] = []
    prompts: list[str] = []
    code = run_smtp_test(
        _account(), to, confirm=lambda p: (prompts.append(p), answer)[1], save_copy=save_copy,
        out=lines.append, sender_factory=lambda a: sender, writer_factory=lambda a: writer, now=NOW,
    )
    return code, "\n".join(lines), sender, writer, prompts


def test_smtp_test_success_sends_once_and_saves_copy() -> None:
    code, out, sender, writer, _ = _run()
    assert code == 0 and "RESULT: PASS (email sent and copy saved)" in out
    assert len(sender.sent) == 1 and len(writer.saved) == 1 and writer.saved[0] is sender.sent[0]
    msg = sender.sent[0]
    assert msg["To"] == "me@example.com" and "Email Agent SMTP test" in msg["Subject"]
    assert PASSWORD not in out and PASSWORD not in msg.as_string()


def test_declining_the_prompt_sends_nothing() -> None:
    code, out, sender, writer, prompts = _run(answer=False)
    assert code == 1 and sender.sent == [] and writer.saved == [] and "Nothing was sent" in out
    assert "YES" in prompts[0]


@pytest.mark.parametrize("bad", ["", "nope", "a@b.com,c@d.com", "a@b.com c@d.com", "a@b.com\nBcc: x@y.com"])
def test_invalid_or_multiple_recipients_are_refused_before_anything_happens(bad) -> None:
    code, _out, sender, _, prompts = _run(bad)
    assert code == 2 and sender.sent == [] and prompts == []


@pytest.mark.parametrize(("outcome", "advice"), [
    (SmtpOutcome.TRANSIENT, "Safe to try again"), (SmtpOutcome.REJECTED, "Retrying will not help"),
    (SmtpOutcome.AUTH_FAILED, "Do not retry repeatedly"), (SmtpOutcome.UNKNOWN, "UNCERTAIN"),
])
def test_failures_give_advice_and_never_save_a_copy(outcome, advice) -> None:
    code, out, _, writer, _ = _run(result=SmtpResult(outcome, "detail"))
    assert code == 1 and advice in out and "RESULT: FAIL" in out and writer.saved == []


def test_sent_but_copy_failed_is_reported_as_delivered_with_warning() -> None:
    code, out, sender, _, _ = _run(writer=StubWriter(error="Sent folder not found"))
    assert code == 3 and "WAS delivered" in out and len(sender.sent) == 1  # sent exactly once


def test_no_sent_copy_flag() -> None:
    code, out, _, writer, _ = _run(save_copy=False)
    assert code == 0 and writer.saved == [] and "skipped" in out


def test_display_name_is_used_in_the_from_header() -> None:
    sender, writer, lines = StubSender(OK), StubWriter(), []
    acct = _account().model_copy(update={"display_name": "Patricia Williams"})
    run_smtp_test(acct, "me@example.com", confirm=lambda p: True, out=lines.append,
                  sender_factory=lambda a: sender, writer_factory=lambda a: writer, now=NOW)
    assert sender.sent[0]["From"] == "Patricia Williams <patricia@example.com>"
    assert "Patricia Williams <patricia@example.com>" in "\n".join(lines)


def test_without_display_name_the_bare_address_is_used() -> None:
    code, _out, sender, _, _ = _run()
    assert code == 0 and sender.sent[0]["From"] == "patricia@example.com"
