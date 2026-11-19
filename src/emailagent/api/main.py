"""FastAPI application. Milestone 1 exposes only a health endpoint; the dashboard comes later."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from sqlalchemy import text

from emailagent.core.config import get_settings
from emailagent.core.logging import configure_logging
from emailagent.db.session import get_engine

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Email Agent", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, object]:
        """Liveness + DB connectivity. Reveals no secrets or mailbox details."""
        db_ok = True
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            log.exception("health check: database unreachable")
            db_ok = False
        return {"status": "ok" if db_ok else "degraded", "database": db_ok}

    log.info("app started", extra={"ctx": {"env": settings.app_env, "accounts": list(settings.accounts)}})
    return app


app = create_app()
