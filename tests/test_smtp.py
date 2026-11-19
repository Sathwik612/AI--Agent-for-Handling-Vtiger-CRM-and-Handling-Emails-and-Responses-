"""SMTP sender: unit tests with a scripted fake, plus integration tests against a REAL
SMTP-over-TLS server (aiosmtpd) so the protocol handling is exercised, not just mocked."""

from __future__ import annotations

import datetime as dt
import ipaddress
import smtplib
import socket
import ssl
from pathlib import Path
from typing import ClassVar

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from emailagent.core.config import MailAccountConfig
from emailagent.mail.message_builder import build_message
from emailagent.mail.parser import parse_email
from emailagent.mail.smtp_client import SmtpOutcome, SmtpSender

PASSWORD = "S3cret!pw"


def _account(port: int = 465, host: str = "mail.example.com", password: str = PASSWORD) -> MailAccountConfig:
    return MailAccountConfig(
        key="kaitlyn", address="kaitlyn@example.com", password=SecretStr(password), market="Ohio OST",
        timezone="America/New_York", imap_host=host, smtp_host=host, smtp_port=port,
    )


def _msg(to="parent@school.org", **kw):
    return build_message(
        from_addr="kaitlyn@example.com", from_name="Kaitlyn K", to_addrs=[to], subject="Re: Quote",
        body="Hello there", message_id="<fixed-id-1@example.com>", in_reply_to="<orig@school.org>",
        references=["<root@school.org>"], **kw,
    )


# ---------------------------------------------------------------- scripted fake (stage coverage)
class FakeSMTP:
    script: ClassVar[dict[str, object]] = {}
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.args = (host, port, timeout, context)
        self.calls: list[str] = []
        self.data_bytes: bytes | None = None
        self.quit_called = False
        FakeSMTP.instances.append(self)
        self._maybe("connect")

    def _maybe(self, stage: str):
        action = FakeSMTP.script.get(stage)
        if isinstance(action, BaseException):
            raise action
        return action

    def login(self, user, password):
        self.calls.append("login")
        self._maybe("login")

    def mail(self, sender):
        self.calls.append("mail")
        return self._maybe("mail") or (250, b"ok")

    def rcpt(self, addr):
        self.calls.append("rcpt")
        return self._maybe("rcpt") or (250, b"ok")

    def data(self, raw):
        self.calls.append("data")
        self.data_bytes = raw
        return self._maybe("data") or (250, b"2.0.0 queued as ABC123")

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSMTP.script, FakeSMTP.instances = {}, []


def _send(account=None, msg=None):
    return SmtpSender(account or _account(), smtp_factory=FakeSMTP).send(msg or _msg())


def test_success_sends_crlf_message_and_quits() -> None:
    r = _send()
    assert r.outcome == SmtpOutcome.SENT and r.code == 250 and not r.safe_to_retry
    f = FakeSMTP.instances[0]
    assert f.calls == ["login", "mail", "rcpt", "data"] and f.quit_called
    assert b"\r\n" in f.data_bytes and b"Message-ID: <fixed-id-1@example.com>" in f.data_bytes
    assert f.args[:2] == ("mail.example.com", 465) and isinstance(f.args[3], ssl.SSLContext)


def test_certificate_verification_is_on_by_default() -> None:
    ctx = FakeSMTP.instances or (_send(), FakeSMTP.instances)[1]
    ssl_ctx = ctx[0].args[3]
    assert ssl_ctx.verify_mode == ssl.CERT_REQUIRED and ssl_ctx.check_hostname


def test_auth_failure_is_not_retried_and_never_leaks_password() -> None:
    FakeSMTP.script["login"] = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials " + PASSWORD.encode())
    r = _send()
    assert r.outcome == SmtpOutcome.AUTH_FAILED and not r.safe_to_retry
    assert PASSWORD not in r.detail
    assert "data" not in FakeSMTP.instances[0].calls


def test_missing_password_never_connects() -> None:
    r = _send(_account(password=""))
    assert r.outcome == SmtpOutcome.AUTH_FAILED and FakeSMTP.instances == []


@pytest.mark.parametrize(("stage", "code", "outcome"), [
    ("mail", 550, SmtpOutcome.REJECTED), ("mail", 451, SmtpOutcome.TRANSIENT),
    ("rcpt", 550, SmtpOutcome.REJECTED), ("rcpt", 450, SmtpOutcome.TRANSIENT),
    ("data", 554, SmtpOutcome.REJECTED), ("data", 451, SmtpOutcome.TRANSIENT),
])
def test_error_codes_are_classified_and_nothing_is_delivered(stage, code, outcome) -> None:
    FakeSMTP.script[stage] = (code, b"nope")
    r = _send()
    assert r.outcome == outcome and r.code == code
    assert r.safe_to_retry == (outcome == SmtpOutcome.TRANSIENT)


def test_recipient_refusal_stops_before_data() -> None:
    FakeSMTP.script["rcpt"] = (550, b"no such user")
    _send()
    assert "data" not in FakeSMTP.instances[0].calls


@pytest.mark.parametrize(("exc", "outcome"), [
    (socket.gaierror(-2, "x"), SmtpOutcome.TRANSIENT),
    (ConnectionRefusedError(), SmtpOutcome.TRANSIENT),
    (TimeoutError(), SmtpOutcome.TRANSIENT),
    (ssl.SSLError("handshake"), SmtpOutcome.TRANSIENT),
    (ssl.SSLCertVerificationError(1, "mismatch"), SmtpOutcome.REJECTED),
])
def test_connect_failures_are_safe_to_classify(exc, outcome) -> None:
    FakeSMTP.script["connect"] = exc
    r = _send()
    assert r.outcome == outcome
    assert r.safe_to_retry == (outcome == SmtpOutcome.TRANSIENT)


@pytest.mark.parametrize("exc", [smtplib.SMTPServerDisconnected("gone"), TimeoutError(), ConnectionResetError()])
def test_failure_after_data_is_unknown_and_never_auto_retried(exc) -> None:
    FakeSMTP.script["data"] = exc
    r = _send()
    assert r.outcome == SmtpOutcome.UNKNOWN and not r.safe_to_retry
    assert "may or may not" in r.detail


def test_failure_before_data_is_not_unknown() -> None:
    FakeSMTP.script["rcpt"] = smtplib.SMTPServerDisconnected("gone")
    assert _send().outcome == SmtpOutcome.TRANSIENT


# ---------------------------------------------------------------- real TLS server
@pytest.fixture(scope="module")
def tls_files(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return d / "cert.pem", d / "key.pem"


class _Handler:
    def __init__(self, mode: str = "ok"):
        self.mode, self.received = mode, []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.mode == "reject_rcpt":
            return "550 5.1.1 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        if self.mode == "drop_after_data":
            server.transport.abort()  # connection dies after the body was transmitted
            return "250 never delivered"
        if self.mode == "reject_data":
            return "554 5.7.1 spam suspected"
        self.received.append(envelope)
        return "250 OK queued"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def start_server(tls_files):
    controllers: list[Controller] = []

    def start(mode: str = "ok"):
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(*map(str, tls_files))

        def authenticator(server, session, envelope, mechanism, auth_data):
            ok = isinstance(auth_data, LoginPassword) and auth_data.password == PASSWORD.encode()
            return AuthResult(success=ok, handled=False)

        handler, port = _Handler(mode), _free_port()
        c = Controller(handler, hostname="127.0.0.1", port=port, ssl_context=server_ctx,
                       authenticator=authenticator, auth_required=True, auth_require_tls=False,
                       ready_timeout=10)
        c.start()
        controllers.append(c)
        return handler, port

    yield start
    for c in controllers:
        c.stop()


def _trusting_ctx(tls_files) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(tls_files[0]))


def test_real_server_end_to_end_delivery_preserves_threading(start_server, tls_files) -> None:
    handler, port = start_server("ok")
    r = SmtpSender(_account(port, "127.0.0.1"), ssl_context=_trusting_ctx(tls_files)).send(_msg())
    assert r.outcome == SmtpOutcome.SENT, r.detail
    env = handler.received[0]
    assert env.mail_from == "kaitlyn@example.com" and env.rcpt_tos == ["parent@school.org"]
    got = parse_email(env.original_content)
    assert got.message_id == "<fixed-id-1@example.com>"           # fixed ID survives the wire
    assert got.in_reply_to == "<orig@school.org>" and got.references == ["<root@school.org>", "<orig@school.org>"]
    assert got.subject == "Re: Quote" and got.body_text == "Hello there"


def test_real_server_wrong_password(start_server, tls_files) -> None:
    _, port = start_server("ok")
    r = SmtpSender(_account(port, "127.0.0.1", password="wrong"), ssl_context=_trusting_ctx(tls_files)).send(_msg())
    assert r.outcome == SmtpOutcome.AUTH_FAILED and "wrong" not in r.detail


def test_real_server_rejects_recipient(start_server, tls_files) -> None:
    handler, port = start_server("reject_rcpt")
    r = SmtpSender(_account(port, "127.0.0.1"), ssl_context=_trusting_ctx(tls_files)).send(_msg())
    assert r.outcome == SmtpOutcome.REJECTED and r.code == 550 and handler.received == []


def test_real_server_rejects_message_after_data(start_server, tls_files) -> None:
    _, port = start_server("reject_data")
    r = SmtpSender(_account(port, "127.0.0.1"), ssl_context=_trusting_ctx(tls_files)).send(_msg())
    assert r.outcome == SmtpOutcome.REJECTED and r.code == 554


def test_real_server_untrusted_certificate_is_refused_not_bypassed(start_server) -> None:
    _, port = start_server("ok")
    r = SmtpSender(_account(port, "127.0.0.1")).send(_msg())  # default context: self-signed cert is untrusted
    assert r.outcome == SmtpOutcome.REJECTED and "certificate" in r.detail.lower()


def test_real_server_connection_lost_after_data_is_unknown(start_server, tls_files) -> None:
    _, port = start_server("drop_after_data")
    r = SmtpSender(_account(port, "127.0.0.1"), ssl_context=_trusting_ctx(tls_files)).send(_msg())
    assert r.outcome == SmtpOutcome.UNKNOWN and not r.safe_to_retry


def test_real_server_nothing_listening_is_transient() -> None:
    r = SmtpSender(_account(_free_port(), "127.0.0.1"), timeout=3).send(_msg())
    assert r.outcome == SmtpOutcome.TRANSIENT and r.safe_to_retry
