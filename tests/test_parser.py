from __future__ import annotations

from datetime import UTC
from email.message import EmailMessage

from emailagent.mail.parser import html_to_text, parse_email


def _msg(**headers: str) -> EmailMessage:
    m = EmailMessage()
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    return m


def test_plain_email_basic_fields() -> None:
    m = _msg(From="Jamison T <JTrue@School.org>", To="kaitlyn@lumos.com", Subject="Quote request",
             Message_ID="<abc123@school.org>", Date="Wed, 18 Nov 2026 09:30:00 -0500")
    m.set_content("Hello,\nCan you send a quote for 700 students?\n")
    p = parse_email(m.as_bytes())
    assert p.message_id == "<abc123@school.org>" and not p.message_id_is_synthetic
    assert p.from_addr == "jtrue@school.org" and p.from_name == "Jamison T"
    assert p.to_addrs == ["kaitlyn@lumos.com"]
    assert p.subject == "Quote request"
    assert p.body_source == "plain" and "700 students" in p.body_text
    assert p.date is not None and p.date.tzinfo == UTC and p.date.hour == 14  # 09:30 EST -> 14:30 UTC
    assert not p.is_automated


def test_threading_headers_are_captured() -> None:
    # Raw bytes on purpose: real References headers are folded across lines.
    raw = (b"From: a@b.com\r\nTo: c@d.com\r\nSubject: Re: Quote\r\nMessage-ID: <m3@b.com>\r\n"
           b"In-Reply-To: <m2@d.com>\r\nReferences: <m1@b.com>\r\n <m2@d.com>\r\n\r\nok")
    p = parse_email(raw)
    assert p.in_reply_to == "<m2@d.com>"
    assert p.references == ["<m1@b.com>", "<m2@d.com>"]


def test_html_only_email_is_converted_to_text() -> None:
    m = _msg(From="a@b.com", To="c@d.com", Subject="x", Message_ID="<h@b.com>")
    m.set_content("<html><head><style>p{color:red}</style></head><body><p>Hi there</p>"
                  "<p>Need pricing for <b>OST</b>&nbsp;prep.</p><script>evil()</script></body></html>",
                  subtype="html")
    p = parse_email(m.as_bytes())
    assert p.body_source == "html"
    assert "Hi there" in p.body_text and "OST prep" in p.body_text
    assert "evil" not in p.body_text and "color:red" not in p.body_text


def test_multipart_prefers_plain_and_lists_attachments() -> None:
    m = _msg(From="a@b.com", To="c@d.com", Subject="x", Message_ID="<mp@b.com>")
    m.set_content("plain version")
    m.add_alternative("<p>html version</p>", subtype="html")
    m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="quote.pdf")
    p = parse_email(m.as_bytes())
    assert p.body_text == "plain version" and p.body_source == "plain"
    assert p.attachment_names == ["quote.pdf"]


def test_encoded_subject_and_non_ascii_body() -> None:
    raw = (b"From: =?UTF-8?B?Sm9zw6k=?= <jose@x.com>\r\nTo: a@b.com\r\n"
           b"Subject: =?UTF-8?B?UHJlc3N1cG9zdG8gcGFyYSBhIGVzY29sYQ==?=\r\nMessage-ID: <e@x.com>\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
           + "Gracias, adi\u00f3s \u2013 ok".encode())
    p = parse_email(raw)
    assert p.from_name == "Jos\u00e9"
    assert p.subject == "Pressuposto para a escola"
    assert "adi\u00f3s \u2013 ok" in p.body_text


def test_unknown_charset_does_not_crash() -> None:
    raw = (b"From: a@b.com\r\nTo: c@d.com\r\nSubject: x\r\nMessage-ID: <c@x.com>\r\n"
           b"Content-Type: text/plain; charset=x-not-a-real-charset\r\n\r\nHello caf\xe9")
    p = parse_email(raw)
    assert "Hello caf" in p.body_text


def test_missing_message_id_gets_stable_synthetic_id() -> None:
    raw = b"From: a@b.com\r\nTo: c@d.com\r\nSubject: no id\r\n\r\nbody"
    p1, p2 = parse_email(raw), parse_email(raw)
    assert p1.message_id_is_synthetic and p1.message_id == p2.message_id  # stable => dedupes
    assert parse_email(raw + b"!").message_id != p1.message_id


def test_garbage_message_id_is_replaced() -> None:
    raw = b"From: a@b.com\r\nTo: c@d.com\r\nMessage-ID: not-an-id\r\n\r\nx"
    assert parse_email(raw).message_id_is_synthetic


def test_malformed_headers_do_not_crash() -> None:
    p = parse_email(b"From: <<<>>>\r\nTo: ,,,\r\nDate: not a date\r\n\r\nstill readable")
    assert p.from_addr is None and p.to_addrs == [] and p.date is None
    assert p.body_text == "still readable"


def test_cc_and_duplicates() -> None:
    m = _msg(From="a@b.com", To="X <c@d.com>, c@d.com", Cc="e@f.com", Message_ID="<cc@x>")
    m.set_content("x")
    p = parse_email(m.as_bytes())
    assert p.to_addrs == ["c@d.com"] and p.cc_addrs == ["e@f.com"]


def test_automated_detection() -> None:
    cases = {
        "Auto-Submitted": ("auto-replied", "auto-submitted"),
        "Precedence": ("bulk", "precedence"),
        "List-Unsubscribe": ("<mailto:u@x.com>", "mailing-list"),
    }
    for header, (value, reason) in cases.items():
        m = _msg(From="a@b.com", To="c@d.com", Subject="hello", Message_ID="<a@x>")
        m[header] = value
        m.set_content("x")
        p = parse_email(m.as_bytes())
        assert p.is_automated and p.automated_reason.startswith(reason), header


def test_bounce_and_out_of_office_detected() -> None:
    bounce = parse_email(b"From: MAILER-DAEMON@x.com\r\nTo: a@b.com\r\nSubject: Delivery Status\r\n\r\nfailed")
    assert bounce.automated_reason == "mailer-daemon"
    ooo = parse_email(b"From: a@b.com\r\nTo: c@d.com\r\nSubject: Automatic reply: Quote\r\n\r\naway")
    assert ooo.automated_reason == "auto-subject"


def test_normal_human_email_is_not_flagged() -> None:
    m = _msg(From="a@b.com", To="c@d.com", Subject="Re: pricing", Message_ID="<n@x>")
    m.set_content("Thanks!")
    assert not parse_email(m.as_bytes()).is_automated


def test_html_to_text_handles_broken_html() -> None:
    assert "Hello" in html_to_text("<p>Hello<div>world")


def test_calendly_notification_is_flagged_by_domain() -> None:
    m = _msg(From="Calendly <notifications@calendly.com>", To="k@lumos.com", Subject="New Event: x", Message_ID="<c@x>")
    m.set_content("A new event has been scheduled")
    p = parse_email(m.as_bytes())
    assert p.is_automated and p.automated_reason == "known-notification-domain"


def test_subdomain_of_a_listed_domain_is_flagged() -> None:
    p = parse_email(b"From: a@mail.calendly.com\r\nTo: k@x.com\r\n\r\nhi")
    assert p.automated_reason == "known-notification-domain"


def test_custom_automated_domain_list() -> None:
    raw = b"From: a@vendor.example\r\nTo: k@x.com\r\n\r\nhi"
    assert not parse_email(raw).is_automated
    assert parse_email(raw, automated_domains=frozenset({"vendor.example"})).is_automated


def test_no_reply_style_senders_are_flagged() -> None:
    for sender in ("noreply@x.com", "no-reply@x.com", "donotreply@x.com", "do-not-reply@x.com",
                   "notifications@school.org", "noreply-abc123@x.com", "bounces+tag@x.com"):
        p = parse_email(f"From: {sender}\r\nTo: k@x.com\r\n\r\nhi".encode())
        assert p.automated_reason == "no-reply-or-notification-sender", sender


def test_real_people_with_similar_names_are_not_flagged() -> None:
    for sender in ("noreen@school.org", "replyguy@school.org", "notify.me@school.org", "lana.smith@knott.kyschools.us"):
        assert not parse_email(f"From: {sender}\r\nTo: k@x.com\r\n\r\nhi".encode()).is_automated, sender
