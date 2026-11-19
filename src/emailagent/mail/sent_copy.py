"""Save a copy of a sent email into the mailbox's Sent folder.

This is the ONLY place the agent writes to a mailbox (one IMAP APPEND). It is a separate class
from ``ImapReader`` so that the reader stays provably read-only.

Why it exists: SMTP delivers the message but does not file it in webmail's Sent folder, and the
BDAs (and the agent's own duplicate-reply check) rely on Sent to see what was already said.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from typing import Any

from imapclient import SEEN, IMAPClient

from emailagent.core.config import MailAccountConfig
from emailagent.mail.imap_client import (
    DEFAULT_TIMEOUT_S,
    ClientFactory,
    FolderInfo,
    ImapError,
    describe_error,
    looks_like_sent,
    pick_sent_folder,
)

log = logging.getLogger(__name__)


class SentCopyWriter:
    def __init__(
        self,
        account: MailAccountConfig,
        *,
        client_factory: ClientFactory | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._account = account
        self._factory = client_factory or IMAPClient
        self._timeout = timeout

    def save_copy(self, msg: EmailMessage) -> str:
        """Append ``msg`` to the Sent folder (marked as read). Returns the folder name."""
        acc = self._account
        if not acc.password_is_set:
            raise ImapError(f"No password set for {acc.address}.")
        raw = msg.as_bytes(policy=policy.SMTP)
        client: Any = None
        try:
            client = self._factory(acc.imap_host, port=acc.imap_port, ssl=True, timeout=self._timeout)
            client.login(acc.address, acc.password.get_secret_value())
            folders = [
                FolderInfo(
                    name if isinstance(name, str) else name.decode("utf-8", "replace"),
                    tuple(f.decode() if isinstance(f, bytes) else str(f) for f in flags),
                    False,
                )
                for flags, _delim, name in client.list_folders()
            ]
            folders = [FolderInfo(f.name, f.flags, looks_like_sent(f.flags, f.name)) for f in folders]
            target = pick_sent_folder(folders, acc.sent_folder)
            if target is None:
                raise ImapError(
                    "Could not find the Sent folder. Set MAIL_"
                    f"{acc.key.upper()}_SENT_FOLDER in .env to its exact name."
                )
            client.append(target, raw, flags=(SEEN,), msg_time=datetime.now(UTC))
            log.info("sent copy saved", extra={"ctx": {"account": acc.key, "folder": target}})
            return target
        except ImapError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImapError(describe_error(exc, acc)) from None
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001
                    log.debug("imap logout failed (ignored)")
