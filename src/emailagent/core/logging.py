"""Structured (JSON) logging with secret redaction.

Rules: never log passwords/API keys, and never log full email bodies. Log IDs and
statuses instead.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_REDACTIONS = [
    (
        re.compile(
            r"(?i)(password|passwd|secret|access[_-]?key|api[_-]?key|token|authorization)"
            r"(\"?\s*[:=]\s*\"?)([^\s\",;]+)"
        ),
        r"\1\2***REDACTED***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-***REDACTED***"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload["ctx"] = json.loads(redact(json.dumps(ctx, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
