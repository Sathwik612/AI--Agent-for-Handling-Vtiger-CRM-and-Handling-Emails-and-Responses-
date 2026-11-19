from __future__ import annotations

import json
import logging

from emailagent.core.logging import JsonFormatter, redact


def test_redacts_passwords_and_api_keys() -> None:
    assert "hunter2" not in redact("login password=hunter2 failed")
    assert "abc123def456" not in redact('{"access_key": "abc123def456"}')
    assert "sk-ant-api03-ABCDEFGHIJK" not in redact("key sk-ant-api03-ABCDEFGHIJK used")


def test_json_formatter_redacts_message_and_context() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "connecting password=topsecret", None, None)
    record.ctx = {"api_key": "sk-live-1234567890", "email_id": 7}
    out = json.loads(JsonFormatter().format(record))
    blob = json.dumps(out)
    assert "topsecret" not in blob and "sk-live-1234567890" not in blob
    assert out["ctx"]["email_id"] == 7  # non-secret context is preserved
