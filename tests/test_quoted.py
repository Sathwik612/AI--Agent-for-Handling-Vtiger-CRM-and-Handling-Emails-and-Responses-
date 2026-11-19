from __future__ import annotations

from emailagent.mail.parser import parse_email
from emailagent.mail.quoted import split_reply


def test_gmail_attribution_splits_top_posted_reply() -> None:
    body = ("Hi Kristy, I'm working on it. I have the PR only.\nThank you! Van\n\n"
            "On Wed, Nov 18, 2026 at 11:36 PM Kristen K <k@lumoslearning.com> wrote:\n"
            "> Hi Van, any update on the quote?\n> Thanks")
    r = split_reply(body)
    assert r.new_text == "Hi Kristy, I'm working on it. I have the PR only.\nThank you! Van"
    assert r.quoted_text.startswith("On Wed, Nov 18") and "any update" in r.quoted_text
    assert r.marker == "attribution"


def test_attribution_wrapped_over_two_lines() -> None:
    r = split_reply("Sounds good.\n\nOn Thu, Nov 19, 2026 at 9:01 AM Some Long Name <\nname@school.org> wrote:\n> old")
    assert r.new_text == "Sounds good." and r.marker == "attribution"


def test_outlook_header_block_and_separator() -> None:
    body = ("Please revise.\n\n________________________________\nFrom: Kaitlyn <k@lumos.com>\n"
            "Sent: Wednesday, November 18, 2026 3:00 PM\nTo: Lana\nSubject: Quote\n\nOld text")
    r = split_reply(body)
    assert r.new_text == "Please revise." and r.quoted_text.startswith("____") and r.marker == "outlook-header"


def test_original_message_marker() -> None:
    r = split_reply("Thanks!\n\n-----Original Message-----\nFrom: x\nSent: y\nOld")
    assert r.new_text == "Thanks!" and r.marker == "original-message"


def test_plain_quote_prefix_without_attribution() -> None:
    r = split_reply("Yes please.\n> earlier line\n> another")
    assert r.new_text == "Yes please." and r.marker == "quote-prefix"


def test_bottom_posting_falls_back_to_unquoted_lines() -> None:
    body = "On Wed, Nov 18, 2026 at 3:00 PM Kaitlyn <k@lumos.com> wrote:\n> Can you confirm 700 students?\nYes, 700.\n> And ELA?\nYes, ELA too."
    r = split_reply(body)
    assert r.new_text == "Yes, 700.\nYes, ELA too."
    assert "Can you confirm" in r.quoted_text  # nothing is lost


def test_no_quote_returns_everything_as_new() -> None:
    r = split_reply("Just a fresh email.\nSecond line.")
    assert r.new_text == "Just a fresh email.\nSecond line." and r.quoted_text == "" and r.marker is None


def test_ordinary_sentences_are_not_mistaken_for_attributions() -> None:
    body = "On Monday he said he wrote: a letter.\nOn the other hand we agree."
    assert split_reply(body).marker is None


def test_empty_body() -> None:
    r = split_reply("")
    assert r.new_text == "" and r.quoted_text == ""


def test_parse_email_exposes_new_and_quoted_text() -> None:
    raw = (b"From: v@fmsd.org\r\nTo: k@lumos.com\r\nSubject: Re: quote\r\nMessage-ID: <r@x>\r\n\r\n"
           b"Working on it.\r\n\r\nOn Wed, Nov 18, 2026 at 11:36 PM K <k@lumos.com> wrote:\r\n> where is it\r\n")
    p = parse_email(raw)
    assert p.new_text == "Working on it." and "where is it" in p.quoted_text
    assert "where is it" in p.body_text  # the full body is still kept
