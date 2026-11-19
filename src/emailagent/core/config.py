"""Application configuration.

Secrets (passwords, API keys) are read from environment variables / ``.env`` only.
They are wrapped in ``SecretStr`` so they never appear in logs or ``repr`` output.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SendMode(StrEnum):
    """Global safety switch. ``draft_only`` means nothing is ever sent."""

    DRAFT_ONLY = "draft_only"
    APPROVED_ONLY = "approved_only"  # sends only items the operator approved


class MailAccountConfig(BaseModel):
    """Connection settings for one mailbox. The password is never stored in the database."""

    key: str
    address: str
    password: SecretStr
    market: str
    timezone: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str
    smtp_port: int = 465
    sent_folder: str | None = None  # auto-detected when not set
    display_name: str | None = None  # the name customers see, e.g. "Kaitlyn Kristy"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {v!r}") from exc
        return v

    @property
    def password_is_set(self) -> bool:
        return bool(self.password.get_secret_value())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    app_secret_key: SecretStr = SecretStr("change-me")
    database_url: str = "postgresql+psycopg://emailagent:change-me@localhost:5432/emailagent"

    mail_accounts: str = ""
    send_mode: SendMode = SendMode.DRAFT_ONLY
    operator_timezone: str = "Asia/Kolkata"

    # --- What the agent is allowed to work on (all editable; empty = nothing is in scope) ---
    agent_allowed_states: str = ""  # e.g. "OH,TX"
    agent_allowed_bdas: str = ""  # exact names as written in Vtiger's BDA field, e.g. "Rupa"
    # Off-hours window in operator_timezone: the agent covers mail that arrives between
    # window_start and window_end (next day). Mail arriving in daytime hours is left to the BDAs.
    window_start: str = "18:30"
    window_end: str = "10:00"
    # Days the BDAs work (in operator_timezone). Other days count as fully off-hours.
    work_days: str = "MON,TUE,WED,THU,FRI"
    # Senders whose mail is notification noise (comma-separated domains)
    automated_sender_domains: str = "calendly.com"

    ai_provider: str = "anthropic"
    anthropic_api_key: SecretStr = SecretStr("")

    vtiger_base_url: str = ""
    vtiger_username: str = ""
    vtiger_access_key: SecretStr = SecretStr("")

    accounts: dict[str, MailAccountConfig] = Field(default_factory=dict, exclude=True)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def load_mail_accounts(account_keys: str, environ: Mapping[str, str]) -> dict[str, MailAccountConfig]:
    """Build account configs from ``MAIL_<KEY>_*`` variables.

    Raises ``ValueError`` listing every problem at once, so a misconfigured .env is
    reported in one go instead of one error at a time.
    """
    accounts: dict[str, MailAccountConfig] = {}
    problems: list[str] = []

    for raw in (k.strip().lower() for k in account_keys.split(",")):
        if not raw:
            continue
        if not _KEY_RE.match(raw):
            problems.append(f"Invalid account key {raw!r} (use lowercase letters/digits/_)")
            continue
        prefix = f"MAIL_{raw.upper()}_"

        def get(name: str, default: str = "", _p: str = prefix) -> str:
            return environ.get(_p + name, default)

        required = ["ADDRESS", "MARKET", "TIMEZONE", "IMAP_HOST", "SMTP_HOST"]
        missing = [f"{prefix}{n}" for n in required if not get(n)]
        if missing:
            problems.extend(f"Missing {m}" for m in missing)
            continue
        try:
            accounts[raw] = MailAccountConfig(
                key=raw,
                address=get("ADDRESS"),
                password=SecretStr(get("PASSWORD")),
                market=get("MARKET"),
                timezone=get("TIMEZONE"),
                imap_host=get("IMAP_HOST"),
                imap_port=int(get("IMAP_PORT", "993")),
                smtp_host=get("SMTP_HOST"),
                smtp_port=int(get("SMTP_PORT", "465")),
                sent_folder=get("SENT_FOLDER") or None,
                display_name=get("DISPLAY_NAME") or None,
            )
        except ValueError as exc:
            problems.append(f"{prefix}*: {exc}")

    if problems:
        raise ValueError("Invalid mail account configuration:\n  - " + "\n  - ".join(problems))
    return accounts


def _read_dotenv(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # pydantic-settings reads .env into the Settings fields, but the dynamic
    # MAIL_<KEY>_* variables need both sources merged (real environment wins).
    merged: dict[str, str] = {}
    env_file = settings.model_config.get("env_file")
    if isinstance(env_file, str) and os.path.exists(env_file):
        merged.update(_read_dotenv(env_file))
    merged.update(os.environ)
    settings.accounts = load_mail_accounts(settings.mail_accounts, merged)
    return settings
