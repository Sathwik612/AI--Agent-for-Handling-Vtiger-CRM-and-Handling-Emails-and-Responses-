"""Command-line tools.

    python -m emailagent.cli check-config   # validate .env (no network, no secrets printed)
    python -m emailagent.cli seed           # create/update mailbox rows + default rules
    python -m emailagent.cli imap-test --account kaitlyn   # read-only mailbox check
    python -m emailagent.cli smtp-test --account kaitlyn --to me@example.com   # ONE test email
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from emailagent.core.config import get_settings
from emailagent.db.enums import RuleCategory
from emailagent.db.models import AgentRule, EmailAccount
from emailagent.db.session import session_scope

DEFAULT_RULES: list[tuple[RuleCategory, str, str]] = [
    (RuleCategory.APPROVAL_REQUIRED, "pricing", "Pricing, quotes, quote revisions, discounts, offers."),
    (RuleCategory.APPROVAL_REQUIRED, "contract", "Contract or terms questions."),
    (RuleCategory.APPROVAL_REQUIRED, "product_recommendation", "Product recommendations."),
    (RuleCategory.APPROVAL_REQUIRED, "not_interested", "Polite send-off that keeps the door open."),
    (RuleCategory.NEVER_SEND, "complaint", "Complaints and angry customers."),
    (RuleCategory.NEVER_SEND, "legal_or_billing_dispute", "Legal, refund or billing disputes."),
    (RuleCategory.NEVER_SEND, "low_confidence", "AI confidence below threshold."),
    (RuleCategory.NEVER_SEND, "unauthorised_commitment", "Anything committing the business."),
    (RuleCategory.AUTO_SEND_ALLOWED, "simple_acknowledgement", "Disabled at launch (draft-only)."),
]


def check_config() -> int:
    settings = get_settings()
    print(f"Environment : {settings.app_env}")
    print(f"Send mode   : {settings.send_mode.value}")
    print(f"Operator TZ : {settings.operator_timezone}")
    print(f"Mailboxes   : {len(settings.accounts)}")
    problems = 0
    for acc in settings.accounts.values():
        pw = "set" if acc.password_is_set else "MISSING"
        if not acc.password_is_set:
            problems += 1
        print(
            f"  - {acc.key:<9} {acc.address}  [{acc.market}, {acc.timezone}]  "
            f"IMAP {acc.imap_host}:{acc.imap_port}  SMTP {acc.smtp_host}:{acc.smtp_port}  password: {pw}"
        )
    from emailagent.core.scope import ScopeRules
    from emailagent.core.window import ProcessingWindow

    try:
        rules = ScopeRules.from_settings(settings.agent_allowed_states, settings.agent_allowed_bdas)
        print(f"Allowed states : {', '.join(sorted(rules.allowed_states)) or '(none)'}")
        print(f"Allowed BDAs   : {settings.agent_allowed_bdas or '(none)'}")
        if not rules.is_configured:
            print("WARNING: AGENT_ALLOWED_STATES and AGENT_ALLOWED_BDAS must both be set, or nothing is in scope.")
            problems += 1
    except ValueError as exc:
        print(f"WARNING: {exc}")
        problems += 1
    try:
        ProcessingWindow.from_settings(
            settings.window_start, settings.window_end, settings.operator_timezone, settings.work_days
        )
        print(f"Off-hours window: {settings.window_start} to {settings.window_end} ({settings.operator_timezone})")
        print(f"BDA work days  : {settings.work_days} (all other days are fully off-hours)")
    except ValueError as exc:
        print(f"WARNING: {exc}")
        problems += 1
    if settings.app_secret_key.get_secret_value() in ("", "change-me"):
        print("WARNING: APP_SECRET_KEY is still the default; set a real value.")
        problems += 1
    print("Config OK" if problems == 0 else f"Config loaded with {problems} warning(s)")
    return 0 if problems == 0 else 1


def seed() -> int:
    settings = get_settings()
    with session_scope() as db:
        for acc in settings.accounts.values():
            row = db.scalar(select(EmailAccount).where(EmailAccount.key == acc.key))
            if row is None:
                row = EmailAccount(key=acc.key)
                db.add(row)
            row.address = acc.address
            row.market = acc.market
            row.timezone = acc.timezone
            row.imap_host, row.imap_port = acc.imap_host, acc.imap_port
            row.smtp_host, row.smtp_port = acc.smtp_host, acc.smtp_port
            row.password_env_key = f"MAIL_{acc.key.upper()}_PASSWORD"  # a reference, not the secret
            print(f"mailbox ready: {acc.address}")
        for category, name, description in DEFAULT_RULES:
            if db.scalar(select(AgentRule).where(AgentRule.name == name)) is None:
                db.add(
                    AgentRule(
                        category=category,
                        name=name,
                        description=description,
                        enabled=category != RuleCategory.AUTO_SEND_ALLOWED,
                    )
                )
                print(f"rule added: {name} ({category.value})")
    return 0


def _account(account_key: str):  # type: ignore[no-untyped-def]
    settings = get_settings()
    account = settings.accounts.get(account_key.lower())
    if account is None:
        known = ", ".join(settings.accounts) or "none configured"
        print(f"Unknown account '{account_key}'. Configured accounts: {known}", file=sys.stderr)
    return account


def imap_test(account_key: str, limit: int, preview: bool) -> int:
    from emailagent.core.window import ProcessingWindow
    from emailagent.mail.diagnostics import run_imap_test

    account = _account(account_key)
    if account is None:
        return 2
    settings = get_settings()
    window = ProcessingWindow.from_settings(
        settings.window_start, settings.window_end, settings.operator_timezone, settings.work_days
    )
    domains = frozenset(d.strip().lower() for d in settings.automated_sender_domains.split(",") if d.strip())
    return run_imap_test(account, limit=limit, preview=preview, window=window, automated_domains=domains)


def smtp_test(account_key: str, to_addr: str, assume_yes: bool, save_copy: bool) -> int:
    from emailagent.mail.diagnostics import run_smtp_test

    account = _account(account_key)
    if account is None:
        return 2

    def confirm(prompt: str) -> bool:
        if assume_yes:
            return True
        try:
            return input(prompt).strip() == "YES"
        except EOFError:  # no interactive terminal: refuse rather than guess
            print("\nNo interactive terminal. Re-run with --yes to confirm.")
            return False

    return run_smtp_test(account, to_addr, confirm=confirm, save_copy=save_copy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emailagent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="validate .env (no network)")
    sub.add_parser("seed", help="create/update mailbox rows and default rules")
    imap = sub.add_parser("imap-test", help="read-only check of one mailbox's IMAP connection")
    imap.add_argument("--account", required=True, help="account key, e.g. kaitlyn or patricia")
    imap.add_argument("--limit", type=int, default=5, help="how many latest messages to show")
    imap.add_argument("--preview", action="store_true", help="also show a short body preview")
    smtp = sub.add_parser("smtp-test", help="send ONE labelled test email from a mailbox")
    smtp.add_argument("--account", required=True, help="account key, e.g. kaitlyn or patricia")
    smtp.add_argument("--to", required=True, help="the single address to send the test to")
    smtp.add_argument("--yes", action="store_true", help="skip the YES confirmation prompt")
    smtp.add_argument("--no-sent-copy", action="store_true", help="do not save a copy in the Sent folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-config":
            return check_config()
        if args.command == "seed":
            return seed()
        if args.command == "smtp-test":
            return smtp_test(args.account, args.to, args.yes, not args.no_sent_copy)
        return imap_test(args.account, max(1, min(args.limit, 25)), args.preview)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
