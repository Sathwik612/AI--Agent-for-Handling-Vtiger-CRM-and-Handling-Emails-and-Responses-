from __future__ import annotations

import pytest

from emailagent.mail.message_builder import (
    MAX_REFERENCES,
    MessageBuildError,
    build_message,
    envelope_recipients,
    generate_message_id,
    reply_subject,
)
from emailagent.mail.parser import parse_email


def _build(**over):
    kw = {"from_addr": "kaitlyn@lumos.com", "from_name": "Kaitlyn K", "to_addrs": ["a@school.org"],
          "subject": "Re: Quote", "body": "Hello", "message_id": "<out1@lumos.com>"}
    kw.update(over)
    return build_message(**kw)


@pytest.mark.parametrize(("orig", "expected"), [
    ("Quote request", "Re: Quote request"), ("Re: Quote request", "Re: Quote request"),
    ("RE: x", "RE: x"), ("  re:x", "re:x"), (None, "Re:"), ("", "Re:"),
])
def test_reply_subject(orig, expected) -> None:
    assert reply_subject(orig) == expected


def test_message_id_uses_sender_domain_and_is_unique() -> None:
    a, b = generate_message_id("k@lumoslearning.com"), generate_message_id("k@lumoslearning.com")
    assert a != b and a.startswith("<") and a.endswith("@lumoslearning.com>")


def test_threading_headers_roundtrip_through_our_own_parser() -> None:
    msg = _build(in_reply_to="<m2@x>", references=["<m1@x>", "<m2@x>"], cc_addrs=["c@school.org"])
    p = parse_email(msg.as_bytes())
    assert p.message_id == "<out1@lumos.com>"
    assert p.in_reply_to == "<m2@x>" and p.references == ["<m1@x>", "<m2@x>"]  # no duplicate of m2
    assert p.to_addrs == ["a@school.org"] and p.cc_addrs == ["c@school.org"]
    assert p.body_text == "Hello"


def test_in_reply_to_is_added_to_references_when_missing() -> None:
    p = parse_email(_build(in_reply_to="<m2@x>", references=["<m1@x>"]).as_bytes())
    assert p.references == ["<m1@x>", "<m2@x>"]


def test_references_are_capped_but_keep_root() -> None:
    refs = [f"<m{i}@x>" for i in range(60)]
    p = parse_email(_build(in_reply_to="<m59@x>", references=refs).as_bytes())
    assert len(p.references) == MAX_REFERENCES
    assert p.references[0] == "<m0@x>" and p.references[-1] == "<m59@x>"


@pytest.mark.parametrize("field", ["subject", "to", "from", "cc"])
def test_header_injection_is_rejected(field) -> None:
    evil = "x\r\nBcc: attacker@evil.com"
    over = {"subject": {"subject": evil}, "to": {"to_addrs": [f"a@b.com{evil}"]},
            "from": {"from_addr": f"a@b.com{evil}"}, "cc": {"cc_addrs": [f"a@b.com{evil}"]}}[field]
    with pytest.raises(MessageBuildError):
        _build(**over)


@pytest.mark.parametrize("bad", ["not-an-address", "a@b.com, c@d.com", "", "a b@c.com\n"])
def test_invalid_recipients_rejected(bad) -> None:
    with pytest.raises(MessageBuildError):
        _build(to_addrs=[bad])


def test_requires_a_recipient() -> None:
    with pytest.raises(MessageBuildError):
        _build(to_addrs=[])


def test_no_bcc_header_and_envelope_includes_cc() -> None:
    msg = _build(cc_addrs=["c@school.org"])
    assert msg["Bcc"] is None
    assert envelope_recipients(msg) == ["a@school.org", "c@school.org"]


def test_non_ascii_content_survives() -> None:
    p = parse_email(_build(subject="Re: Presupuesto \u00e1\u00f1o", body="Gracias \u2013 adi\u00f3s").as_bytes())
    assert p.subject == "Re: Presupuesto \u00e1\u00f1o" and "adi\u00f3s" in p.body_text
