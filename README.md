# Email Agent

AI email + CRM assistant for  mailboxes.
Draft-first: **nothing is sent without your approval.**

## Status

| Milestone | State |
|---|---|
| 1. Project setup (config, database schema, migrations, Docker) | **done** |
| 2. IMAP reader (read-only) | **done** |
| 3. SMTP sending + Sent-folder copy, scope/window rules | **done** |
| 4-12 | planned (threading, AI drafts, dashboard, scheduling, approvals, Vtiger, rules, deployment, hardening) |

## Run it on Windows (PowerShell)

Prerequisites: Docker Desktop running, Python 3.12+.

```powershell
# 1. Create your private settings file
Copy-Item .env.example .env

# 2. Generate three secrets and paste them into .env
python scripts/generate_secret.py   # -> APP_SECRET_KEY
python scripts/generate_secret.py   # -> POSTGRES_PASSWORD
# Then edit .env: fill MAIL_KAITLYN_PASSWORD and MAIL_PATRICIA_PASSWORD.
# Leave ANTHROPIC_API_KEY and VTIGER_* empty for now.

# 3. Start the database and the app
docker compose up -d --build

# 4. Check it
Invoke-RestMethod http://localhost:8000/health
docker compose exec app python -m emailagent.cli check-config
docker compose exec app python -m emailagent.cli seed
```

Expected: `/health` returns `status ok / database True`; `check-config` lists both mailboxes with
`password: set` and ends with `Config OK`; `seed` prints both mailboxes and the default rules.

Read-only mailbox check (Milestone 2). Nothing is marked read, moved or deleted:

```powershell
docker compose exec app python -m emailagent.cli imap-test --account kaitlyn --limit 3 --preview
docker compose exec app python -m emailagent.cli imap-test --account patricia --limit 3 --preview
```

Send ONE labelled test email (asks you to type YES first; saves a copy in the Sent folder):

```powershell
docker compose exec app python -m emailagent.cli smtp-test --account patricia --to you@your-own-address.com
```

Useful commands:

```powershell
docker compose logs -f app        # watch logs (secrets are redacted)
docker compose down               # stop (data is kept)
docker compose down -v            # stop AND delete the database - careful
```

For the 10-11 AM / 6:30 PM workflow: Docker Desktop > Settings > General >
"Start Docker Desktop when you sign in". The containers use `restart: unless-stopped`, so they
come back by themselves.

## Running the tests (optional, outside Docker)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## Security notes

* `.env` is git-ignored and docker-ignored. Never commit it or paste its contents in chat.
* Mailbox passwords exist only in `.env`; the database stores just the *name* of the variable.
* The dashboard port is bound to `127.0.0.1`; the database port is not published at all.
* Logs redact passwords/keys and never include full email bodies.

## Project layout

```
src/emailagent/
  core/      config (multi-mailbox, SecretStr) and redacting JSON logging
  db/        SQLAlchemy models, status enums, session handling
  api/       FastAPI app (health endpoint for now)
  core/      config, logging, scope rules (states/BDAs), off-hours window
  mail/      read-only IMAP reader, parser, quoted-text splitter, SMTP sender, Sent-copy writer
  cli.py     check-config, seed, imap-test, smtp-test
migrations/  Alembic migrations (0001 = initial schema)
tests/       pytest suite
```

## Duplicate-send protection (built into the schema)

* `emails`: unique (mailbox, Message-ID) and unique IMAP position.
* `scheduled_emails`: unique `send_key`, unique pre-assigned `outbound_message_id`, and a partial
  unique index allowing only one non-cancelled send per inbound email.
* Ambiguous SMTP results go to `unknown_outcome`, which is never retried automatically.

## What the agent may work on (configuration, not code)

Edit `.env`: `AGENT_ALLOWED_STATES` (e.g. `OH,TX`), `AGENT_ALLOWED_BDAS` (names exactly as in Vtiger),
`WINDOW_START` / `WINDOW_END` (off-hours window in `OPERATOR_TIMEZONE`), `AUTOMATED_SENDER_DOMAINS`.
If the state or BDA list is empty, nothing is in scope (fail-closed). `check-config` validates all of it.

## Send outcomes (duplicate protection)

`SENT` / `TRANSIENT` (safe to retry) / `REJECTED` / `AUTH_FAILED` / `UNKNOWN`. `UNKNOWN` (connection lost
after the message body was transmitted) is **never** retried automatically. If SMTP succeeds but saving
the Sent copy fails, the email is NOT re-sent.
