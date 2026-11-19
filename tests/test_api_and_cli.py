from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from emailagent import cli
from emailagent.core import config
from emailagent.db import session as db_session
from emailagent.db.base import Base
from emailagent.db.models import AgentRule, EmailAccount

ENV = {
    "MAIL_ACCOUNTS": "kaitlyn,patricia",
    "MAIL_KAITLYN_ADDRESS": "k@example.com", "MAIL_KAITLYN_PASSWORD": "pw1",
    "MAIL_KAITLYN_MARKET": "Ohio OST", "MAIL_KAITLYN_TIMEZONE": "America/New_York",
    "MAIL_KAITLYN_IMAP_HOST": "h", "MAIL_KAITLYN_SMTP_HOST": "h",
    "MAIL_PATRICIA_ADDRESS": "p@example.com", "MAIL_PATRICIA_PASSWORD": "pw2",
    "MAIL_PATRICIA_MARKET": "Texas STAAR", "MAIL_PATRICIA_TIMEZONE": "America/Chicago",
    "MAIL_PATRICIA_IMAP_HOST": "h", "MAIL_PATRICIA_SMTP_HOST": "h",
    "APP_SECRET_KEY": "x" * 40,
    "AGENT_ALLOWED_STATES": "OH,Texas",
    "AGENT_ALLOWED_BDAS": "Rupa",
}


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    config.get_settings.cache_clear()
    db_session._engine = None
    db_session._factory = None
    Base.metadata.create_all(db_session.get_engine())
    yield
    config.get_settings.cache_clear()
    db_session._engine = None
    db_session._factory = None


def test_health_endpoint(configured) -> None:
    from emailagent.api.main import create_app

    r = TestClient(create_app()).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": True}


def test_health_reports_degraded_when_db_down(configured, monkeypatch) -> None:
    from emailagent.api import main

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "get_engine", boom)
    assert TestClient(main.create_app()).get("/health").json()["status"] == "degraded"


def test_seed_is_idempotent_and_stores_no_password(configured) -> None:
    assert cli.seed() == 0
    assert cli.seed() == 0  # second run must not duplicate
    with Session(db_session.get_engine()) as s:
        accounts = s.scalars(select(EmailAccount)).all()
        assert {a.key for a in accounts} == {"kaitlyn", "patricia"}
        assert all(a.password_env_key.endswith("_PASSWORD") for a in accounts)
        assert "pw1" not in repr([a.__dict__ for a in accounts])
        rules = s.scalars(select(AgentRule)).all()
        assert len(rules) == len({r.name for r in rules}) == 9
        assert not next(r for r in rules if r.name == "simple_acknowledgement").enabled


def test_check_config_output_hides_passwords(configured, capsys) -> None:
    assert cli.check_config() == 0
    out = capsys.readouterr().out
    assert "pw1" not in out and "pw2" not in out
    assert "Ohio OST" in out and "Config OK" in out


def test_check_config_warns_when_scope_is_empty(configured, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_ALLOWED_STATES", "")
    config.get_settings.cache_clear()
    assert cli.check_config() == 1  # fail-closed: nothing in scope is flagged
    assert "nothing is in scope" in capsys.readouterr().out


def test_check_config_reports_scope(configured, capsys) -> None:
    cli.check_config()
    out = capsys.readouterr().out
    assert "Allowed states : OH, TX" in out and "Off-hours window: 18:30 to 10:00" in out


def test_check_config_rejects_typo_state(configured, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_ALLOWED_STATES", "OH,TZ")
    config.get_settings.cache_clear()
    assert cli.check_config() == 1
    assert "TZ" in capsys.readouterr().out
