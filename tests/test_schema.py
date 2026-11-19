"""The database itself must make duplicate processing/sending impossible."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from emailagent.db.enums import Direction, ScheduleStatus
from emailagent.db.models import Draft, Email, EmailAccount, ScheduledEmail

NOW = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)


def _account(db: Session, key: str = "kaitlyn") -> EmailAccount:
    acc = EmailAccount(
        key=key, address=f"{key}@example.com", market="Ohio OST", timezone="America/New_York",
        imap_host="h", smtp_host="h", password_env_key=f"MAIL_{key.upper()}_PASSWORD",
    )
    db.add(acc)
    db.flush()
    return acc


def _email(db: Session, acc: EmailAccount, message_id: str, uid: int | None = None) -> Email:
    e = Email(account_id=acc.id, direction=Direction.INBOUND, message_id=message_id,
              folder="INBOX", uidvalidity=1, imap_uid=uid)
    db.add(e)
    db.flush()
    return e


def _scheduled(db: Session, acc: EmailAccount, email: Email, key: str, mid: str,
               status: ScheduleStatus = ScheduleStatus.PENDING) -> ScheduledEmail:
    draft = Draft(email_id=email.id, subject="Re: x", body_text="hi")
    db.add(draft)
    db.flush()
    s = ScheduledEmail(
        draft_id=draft.id, account_id=acc.id, reply_to_email_id=email.id, send_key=key,
        outbound_message_id=mid, to_addrs=["a@b.com"], subject="Re: x", body_text="hi",
        scheduled_at=NOW, timezone="America/New_York", status=status,
    )
    db.add(s)
    db.flush()
    return s


def test_same_message_id_cannot_be_ingested_twice(db: Session) -> None:
    acc = _account(db)
    _email(db, acc, "<m1@x>", uid=1)
    with pytest.raises(IntegrityError):
        _email(db, acc, "<m1@x>", uid=2)


def test_same_message_id_allowed_in_different_mailboxes(db: Session) -> None:
    _email(db, _account(db, "kaitlyn"), "<m1@x>", uid=1)
    _email(db, _account(db, "patricia"), "<m1@x>", uid=1)  # e.g. a mail sent to both


def test_same_imap_position_cannot_be_ingested_twice(db: Session) -> None:
    acc = _account(db)
    _email(db, acc, "<a@x>", uid=5)
    with pytest.raises(IntegrityError):
        _email(db, acc, "<b@x>", uid=5)


def test_only_one_live_send_per_inbound_email(db: Session) -> None:
    acc = _account(db)
    email = _email(db, acc, "<m1@x>", uid=1)
    _scheduled(db, acc, email, "k1", "<out1@x>")
    with pytest.raises(IntegrityError):
        _scheduled(db, acc, email, "k2", "<out2@x>")


def test_unknown_outcome_still_blocks_a_second_send(db: Session) -> None:
    acc = _account(db)
    email = _email(db, acc, "<m1@x>", uid=1)
    _scheduled(db, acc, email, "k1", "<out1@x>", ScheduleStatus.UNKNOWN_OUTCOME)
    with pytest.raises(IntegrityError):
        _scheduled(db, acc, email, "k2", "<out2@x>")


def test_cancelled_send_can_be_replaced(db: Session) -> None:
    acc = _account(db)
    email = _email(db, acc, "<m1@x>", uid=1)
    _scheduled(db, acc, email, "k1", "<out1@x>", ScheduleStatus.CANCELLED)
    _scheduled(db, acc, email, "k2", "<out2@x>")  # allowed


def test_send_key_and_outbound_message_id_are_unique(db: Session) -> None:
    acc = _account(db)
    e1, e2 = _email(db, acc, "<m1@x>", 1), _email(db, acc, "<m2@x>", 2)
    _scheduled(db, acc, e1, "k1", "<out1@x>")
    with pytest.raises(IntegrityError):
        _scheduled(db, acc, e2, "k1", "<out2@x>")  # same send_key


def test_database_check_constraint_rejects_invalid_status(db: Session) -> None:
    """Raw SQL bypasses SQLAlchemy's own validation, so this proves the DB-level CHECK works."""
    acc = _account(db)
    email = _email(db, acc, "<m1@x>", uid=1)
    s = _scheduled(db, acc, email, "k1", "<out1@x>")
    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE scheduled_emails SET status = 'bogus' WHERE id = :id"), {"id": s.id}
        )
