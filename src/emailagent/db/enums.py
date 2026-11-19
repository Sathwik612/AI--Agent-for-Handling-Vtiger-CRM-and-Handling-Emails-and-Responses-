"""Status vocabularies. Kept in one place so the workflow is easy to audit."""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class EmailStatus(StrEnum):
    """Where an inbound email is in the pipeline."""

    NEW = "new"
    SKIPPED_JUNK = "skipped_junk"  # newsletter / auto-reply / bounce: no AI call
    SKIPPED_OUT_OF_SCOPE = "skipped_out_of_scope"  # not OST/STAAR: hidden, no AI call
    CLASSIFIED = "classified"
    DRAFTED = "drafted"
    NEEDS_REVIEW = "needs_review"  # could not be handled safely: waits for the operator
    DONE = "done"
    ARCHIVED = "archived"
    ERROR = "error"


class ScopeStatus(StrEnum):
    UNKNOWN = "unknown"
    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    WRONG_MAILBOX = "wrong_mailbox"  # e.g. a Texas district writing to the Ohio mailbox


class DraftStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # replaced by a regenerated draft
    SENT = "sent"


class ScheduleStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"  # claimed by a worker
    SENT = "sent"
    RETRY = "retry"  # failed in a way that is safe to retry
    HELD = "held"  # e.g. customer wrote again; needs operator review
    CANCELLED = "cancelled"
    UNKNOWN_OUTCOME = "unknown_outcome"  # ambiguous SMTP result: NEVER auto-retried


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class RuleCategory(StrEnum):
    AUTO_SEND_ALLOWED = "auto_send_allowed"
    APPROVAL_REQUIRED = "approval_required"
    NEVER_SEND = "never_send"


class RunKind(StrEnum):
    MORNING = "morning"
    SEND = "send"
    MANUAL = "manual"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
