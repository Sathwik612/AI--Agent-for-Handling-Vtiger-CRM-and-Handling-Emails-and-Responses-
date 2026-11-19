"""Database schema.

Design notes
------------
* **No secrets here.** Mailbox passwords/API keys live only in the environment.
* **Duplicate prevention is enforced by the database**, not just by code:
  - ``emails``: unique (account_id, message_id) -> the same message is never ingested twice.
  - ``scheduled_emails``: unique ``send_key`` and a partial unique index allowing at most one
    *live* send per inbound email.
  - ``scheduled_emails.outbound_message_id`` is fixed *before* sending, so an SMTP retry reuses
    the same Message-ID and an ambiguous result can be reconciled against the Sent folder.
* Out-of-scope / junk emails keep a minimal row (no body) so they are not re-examined.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from emailagent.db.base import Base, TimestampMixin, str_enum
from emailagent.db.enums import (
    Direction,
    DraftStatus,
    EmailStatus,
    ProposalStatus,
    RuleCategory,
    RunKind,
    RunStatus,
    ScheduleStatus,
    ScopeStatus,
)


class User(Base, TimestampMixin):
    """Dashboard login. Only a password *hash* is stored."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailAccount(Base, TimestampMixin):
    """One mailbox. ``password_env_key`` names the env var holding the password."""

    __tablename__ = "email_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    address: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    market: Mapped[str] = mapped_column(String(64))  # e.g. "Ohio OST", "Texas STAAR"
    timezone: Mapped[str] = mapped_column(String(64))  # IANA name, e.g. America/Chicago
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    password_env_key: Mapped[str] = mapped_column(String(128))
    signature_text: Mapped[str | None] = mapped_column(Text)
    style_guide: Mapped[dict[str, Any] | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    # IMAP sync state (per folder): lets polling resume without re-reading everything.
    inbox_uidvalidity: Mapped[int | None] = mapped_column(Integer)
    inbox_last_uid: Mapped[int | None] = mapped_column(Integer)
    sent_uidvalidity: Mapped[int | None] = mapped_column(Integer)
    sent_last_uid: Mapped[int | None] = mapped_column(Integer)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Contact(Base, TimestampMixin):
    """Local cache of the matching Vtiger lead. Vtiger remains the source of truth."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    crm_record_id: Mapped[str | None] = mapped_column(String(64), unique=True)  # e.g. "10x4521"
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    designation: Mapped[str | None] = mapped_column(String(128))
    company: Mapped[str | None] = mapped_column(String(255))
    school_district: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str | None] = mapped_column(String(64))
    # Fields the agent reads/updates (names confirmed against Vtiger at Milestone 9)
    quote_number: Mapped[str | None] = mapped_column(String(64))
    reachout_reason: Mapped[str | None] = mapped_column(String(64))  # e.g. "QL"
    widget: Mapped[str | None] = mapped_column(String(64))
    sales_rep_call: Mapped[str | None] = mapped_column(String(64))
    crm_snapshot: Mapped[dict[str, Any] | None] = mapped_column()
    crm_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailThread(Base, TimestampMixin):
    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint("account_id", "thread_key", name="uq_email_threads_account_thread_key"),
        Index("ix_email_threads_account_last_message", "account_id", "last_message_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id"))
    # Message-ID of the thread's root message (or a normalised-subject fallback).
    thread_key: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str | None] = mapped_column(String(998))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    scope_status: Mapped[ScopeStatus] = mapped_column(
        str_enum(ScopeStatus, "scope_status"), default=ScopeStatus.UNKNOWN
    )
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    emails: Mapped[list[Email]] = relationship(back_populates="thread")


class Email(Base, TimestampMixin):
    """Every message we see (inbound and outbound), identified by Message-ID."""

    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("account_id", "message_id", name="uq_emails_account_message_id"),
        UniqueConstraint(
            "account_id", "folder", "uidvalidity", "imap_uid", name="uq_emails_imap_position"
        ),
        Index("ix_emails_status", "account_id", "status"),
        Index("ix_emails_thread", "thread_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id"))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("email_threads.id"))
    direction: Mapped[Direction] = mapped_column(str_enum(Direction, "direction"))
    status: Mapped[EmailStatus] = mapped_column(
        str_enum(EmailStatus, "email_status"), default=EmailStatus.NEW
    )
    skip_reason: Mapped[str | None] = mapped_column(String(128))

    # IMAP position (informational; Message-ID is the real identity)
    folder: Mapped[str] = mapped_column(String(128), default="INBOX")
    uidvalidity: Mapped[int | None] = mapped_column(Integer)
    imap_uid: Mapped[int | None] = mapped_column(Integer)

    # Threading headers
    message_id: Mapped[str] = mapped_column(String(512))
    in_reply_to: Mapped[str | None] = mapped_column(String(512))
    references: Mapped[list[Any] | None] = mapped_column()

    from_addr: Mapped[str | None] = mapped_column(String(320), index=True)
    from_name: Mapped[str | None] = mapped_column(String(255))
    to_addrs: Mapped[list[Any] | None] = mapped_column()
    cc_addrs: Mapped[list[Any] | None] = mapped_column()
    subject: Mapped[str | None] = mapped_column(String(998))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL for skipped emails (we keep no body for junk / out-of-scope)
    body_text: Mapped[str | None] = mapped_column(Text)

    ai_intent: Mapped[str | None] = mapped_column(String(64))
    ai_priority: Mapped[str | None] = mapped_column(String(16))

    thread: Mapped[EmailThread | None] = relationship(back_populates="emails")


class AIGeneration(Base):
    """Audit record of each AI call: what was asked (by version), what came back."""

    __tablename__ = "ai_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    output: Mapped[dict[str, Any] | None] = mapped_column()  # validated structured output
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_error: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    requires_human_approval: Mapped[bool | None] = mapped_column(Boolean)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Draft(Base, TimestampMixin):
    """An editable reply draft awaiting (or past) the operator's decision."""

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)  # email replied to
    generation_id: Mapped[int | None] = mapped_column(ForeignKey("ai_generations.id"))
    status: Mapped[DraftStatus] = mapped_column(
        str_enum(DraftStatus, "draft_status"), default=DraftStatus.PENDING_REVIEW
    )
    subject: Mapped[str] = mapped_column(String(998))
    body_text: Mapped[str] = mapped_column(Text)
    edited_by_operator: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledEmail(Base, TimestampMixin):
    """The persistent send queue. Survives restarts: it is just rows in PostgreSQL."""

    __tablename__ = "scheduled_emails"
    __table_args__ = (
        # At most ONE non-cancelled send per inbound email (retry/held/unknown still count).
        Index(
            "uq_scheduled_one_live_per_email",
            "reply_to_email_id",
            unique=True,
            postgresql_where=text("status NOT IN ('cancelled')"),
            sqlite_where=text("status NOT IN ('cancelled')"),
        ),
        Index("ix_scheduled_due", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id"))
    reply_to_email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    # Idempotency key: sha256(account | reply_to message-id | draft id)
    send_key: Mapped[str] = mapped_column(String(64), unique=True)
    # Fixed BEFORE sending; reused on every retry; used to reconcile with the Sent folder.
    outbound_message_id: Mapped[str] = mapped_column(String(512), unique=True)
    to_addrs: Mapped[list[Any]] = mapped_column()
    cc_addrs: Mapped[list[Any] | None] = mapped_column()
    subject: Mapped[str] = mapped_column(String(998))
    body_text: Mapped[str] = mapped_column(Text)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64))
    # If a send session starts after this moment, hold for review instead of sending stale mail.
    max_delay_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ScheduleStatus] = mapped_column(
        str_enum(ScheduleStatus, "schedule_status"), default=ScheduleStatus.PENDING
    )
    hold_reason: Mapped[str | None] = mapped_column(String(255))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SentEmail(Base):
    """Immutable record of every email actually handed to the SMTP server."""

    __tablename__ = "sent_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduled_email_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_emails.id"), unique=True
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(512))
    to_addrs: Mapped[list[Any]] = mapped_column()
    subject: Mapped[str] = mapped_column(String(998))
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=True)
    edited_by_operator: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_response: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrmProposal(Base, TimestampMixin):
    """A proposed Vtiger field change. Nothing is written until the operator approves."""

    __tablename__ = "crm_proposals"
    __table_args__ = (Index("ix_crm_proposals_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    crm_record_id: Mapped[str | None] = mapped_column(String(64))
    field_name: Mapped[str] = mapped_column(String(64))  # e.g. "widget", "sales_rep_call"
    old_value: Mapped[str | None] = mapped_column(String(255))
    new_value: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[ProposalStatus] = mapped_column(
        str_enum(ProposalStatus, "proposal_status"), default=ProposalStatus.PROPOSED
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class CrmComment(Base):
    """Append-only comment log. One comment per email (unique) prevents double-posting."""

    __tablename__ = "crm_comments"
    __table_args__ = (UniqueConstraint("email_id", name="uq_crm_comments_email_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    direction: Mapped[Direction] = mapped_column(str_enum(Direction, "comment_direction"))
    summary: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CallCycle(Base, TimestampMixin):
    """Tracks the 'PR 1 every 5 days' follow-up cycle per contact."""

    __tablename__ = "call_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), unique=True)
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    last_pr1_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rep_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_reason: Mapped[str | None] = mapped_column(String(64))  # replied / not_interested / max_cycles
    flagged_for_operator: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentRule(Base, TimestampMixin):
    """Configurable business rules (auto-send / approval-required / never-send)."""

    __tablename__ = "agent_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[RuleCategory] = mapped_column(str_enum(RuleCategory, "rule_category"))
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any] | None] = mapped_column()
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class AgentRun(Base):
    """One morning or send session; feeds the 'show me everything it did' report."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[RunKind] = mapped_column(str_enum(RunKind, "run_kind"))
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, "run_status"), default=RunStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Append-only trail of who/what did what. Details must never contain secrets."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))  # "operator", "agent", "system"
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any] | None] = mapped_column()
