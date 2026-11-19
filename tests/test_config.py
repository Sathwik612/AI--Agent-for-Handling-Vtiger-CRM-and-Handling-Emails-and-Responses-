from __future__ import annotations

import pytest

from emailagent.core.config import load_mail_accounts

FULL = {
    "MAIL_KAITLYN_ADDRESS": "kaitlyn@example.com",
    "MAIL_KAITLYN_PASSWORD": "s3cret-pass",
    "MAIL_KAITLYN_MARKET": "Ohio OST",
    "MAIL_KAITLYN_TIMEZONE": "America/New_York",
    "MAIL_KAITLYN_IMAP_HOST": "mail.example.com",
    "MAIL_KAITLYN_SMTP_HOST": "mail.example.com",
    "MAIL_PATRICIA_ADDRESS": "patricia@example.com",
    "MAIL_PATRICIA_PASSWORD": "",
    "MAIL_PATRICIA_MARKET": "Texas STAAR",
    "MAIL_PATRICIA_TIMEZONE": "America/Chicago",
    "MAIL_PATRICIA_IMAP_HOST": "mail.example.com",
    "MAIL_PATRICIA_SMTP_HOST": "mail.example.com",
    "MAIL_PATRICIA_SMTP_PORT": "465",
}


def test_loads_both_accounts_with_defaults() -> None:
    accounts = load_mail_accounts("kaitlyn, patricia", FULL)
    assert set(accounts) == {"kaitlyn", "patricia"}
    k = accounts["kaitlyn"]
    assert (k.imap_port, k.smtp_port) == (993, 465)  # defaults
    assert k.timezone == "America/New_York"
    assert accounts["patricia"].market == "Texas STAAR"


def test_password_never_appears_in_repr() -> None:
    acc = load_mail_accounts("kaitlyn", FULL)["kaitlyn"]
    assert acc.password_is_set
    assert "s3cret-pass" not in repr(acc)
    assert "s3cret-pass" not in str(acc.model_dump())


def test_missing_password_is_detected_but_not_fatal() -> None:
    assert not load_mail_accounts("patricia", FULL)["patricia"].password_is_set


def test_reports_all_missing_settings_at_once() -> None:
    with pytest.raises(ValueError) as err:
        load_mail_accounts("kaitlyn", {"MAIL_KAITLYN_ADDRESS": "x@example.com"})
    msg = str(err.value)
    assert "MAIL_KAITLYN_MARKET" in msg and "MAIL_KAITLYN_IMAP_HOST" in msg


def test_rejects_unknown_timezone() -> None:
    env = {**FULL, "MAIL_KAITLYN_TIMEZONE": "EST5EDT_not_real"}
    with pytest.raises(ValueError, match="Unknown timezone"):
        load_mail_accounts("kaitlyn", env)


def test_rejects_bad_account_key() -> None:
    with pytest.raises(ValueError, match="Invalid account key"):
        load_mail_accounts("Bad Key!", FULL)


def test_display_name_is_optional_and_loaded() -> None:
    env = {**FULL, "MAIL_KAITLYN_DISPLAY_NAME": "Kaitlyn Kristy"}
    accounts = load_mail_accounts("kaitlyn,patricia", env)
    assert accounts["kaitlyn"].display_name == "Kaitlyn Kristy" and accounts["patricia"].display_name is None
