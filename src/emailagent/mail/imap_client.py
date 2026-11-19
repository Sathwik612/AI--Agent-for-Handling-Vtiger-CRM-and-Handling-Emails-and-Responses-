"""Read-only IMAP access.

Safety guarantees (covered by tests):
* Folders are opened with ``readonly=True`` (IMAP EXAMINE): the server refuses any change.
* Messages are fetched with ``BODY.PEEK[]``, which never sets the \\Seen flag.
* TLS certificates are always verified. There is no "ignore certificate errors" switch.
* Passwords are read from the account config and are never logged or included in errors.
"""

from __future__ import annotations

import logging
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from emailagent.core.config import MailAccountConfig

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30
MAX_MESSAGE_BYTES = 10_000_000
FETCH_BATCH = 20

ClientFactory = Callable[..., Any]


class ImapError(Exception):
    """A connection/auth/fetch problem, with a plain-English explanation (no secrets)."""


@dataclass(frozen=True)
class FolderInfo:
    name: str
    flags: tuple[str, ...]
    is_sent: bool


@dataclass(frozen=True)
class FolderState:
    name: str
    uidvalidity: int
    exists: int


@dataclass(frozen=True)
class RawMessage:
    uid: int
    size: int
    raw: bytes | None  # None when skipped (too large)
    flags: tuple[str, ...]
    skipped_reason: str | None = None
    received_at: datetime | None = None  # server's INTERNALDATE, UTC (not the sender's Date header)


def describe_error(exc: BaseException, account: MailAccountConfig) -> str:
    """Translate low-level errors into something actionable. Never includes the password."""
    host, port = account.imap_host, account.imap_port
    if isinstance(exc, ssl.SSLCertVerificationError):
        detail = getattr(exc, "verify_message", None) or "verification failed"
        return (
            f"The server's TLS certificate did not match the hostname '{host}' "
            f"({detail}). Do not bypass this. Check the exact incoming-server "
            "hostname shown in your hosting panel's 'Configure Mail Client' page."
        )
    if isinstance(exc, ssl.SSLError):
        return (
            f"TLS handshake with {host}:{port} failed. Port 993 expects an SSL/TLS connection; "
            "verify the port and security type in your hosting panel."
        )
    if isinstance(exc, socket.gaierror):
        return f"Could not resolve the hostname '{host}'. Check the spelling and your internet connection."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return (
            f"Timed out connecting to {host}:{port}. A firewall, VPN or ISP may be blocking "
            "outbound port 993."
        )
    if isinstance(exc, ConnectionRefusedError):
        return f"{host}:{port} refused the connection. Check the port number."
    if isinstance(exc, LoginError):
        return (
            f"Login rejected for {account.address}. Check the password in .env (no stray spaces; "
            "use single quotes if it contains $ or #) and that the username is the full email "
            "address. Avoid repeated retries, since some hosts temporarily lock accounts."
        )
    if isinstance(exc, IMAPClientError):
        return f"The IMAP server reported an error: {type(exc).__name__}."
    return f"Unexpected error: {type(exc).__name__}."


_SENT_NAMES = {"sent", "sent items", "sent messages", "sent mail"}


def looks_like_sent(flags: tuple[str, ...], name: str) -> bool:
    """True for a folder flagged \\Sent, or whose last path component is a usual 'Sent' name."""
    last = name.lower().rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    return "\\Sent" in flags or last in _SENT_NAMES


def pick_sent_folder(folders: list[FolderInfo], override: str | None = None) -> str | None:
    if override:
        return override
    flagged = [f.name for f in folders if "\\Sent" in f.flags]
    return (flagged or [f.name for f in folders if f.is_sent] or [None])[0]


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _text(value: bytes | str) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


class ImapReader:
    """Context manager: connects and logs in on enter, logs out on exit."""

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
        self._client: Any = None

    # -- connection -------------------------------------------------------------------------
    def __enter__(self) -> Self:
        acc = self._account
        if not acc.password_is_set:
            raise ImapError(f"No password set for {acc.address}. Fill MAIL_{acc.key.upper()}_PASSWORD in .env.")
        try:
            self._client = self._factory(acc.imap_host, port=acc.imap_port, ssl=True, timeout=self._timeout)
            self._client.login(acc.address, acc.password.get_secret_value())
            self._client.normalise_times = False  # keep timezone-aware INTERNALDATE values
        except Exception as exc:  # noqa: BLE001 - re-raised as a friendly error
            self._safe_logout()
            raise ImapError(describe_error(exc, acc)) from None  # 'from None': keep secrets out of chains
        log.info("imap connected", extra={"ctx": {"account": acc.key}})
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._safe_logout()

    def _safe_logout(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:  # noqa: BLE001 - best effort on the way out
                log.debug("imap logout failed (ignored)")
            self._client = None

    def _wrap(self, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except ImapError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImapError(describe_error(exc, self._account)) from None

    # -- discovery --------------------------------------------------------------------------
    def capabilities(self) -> set[str]:
        return {_text(c).upper() for c in self._wrap(self._client.capabilities)}

    def supports_idle(self) -> bool:
        return "IDLE" in self.capabilities()

    def list_folders(self) -> list[FolderInfo]:
        folders: list[FolderInfo] = []
        for flags, _delim, name in self._wrap(self._client.list_folders):
            flag_names = tuple(_text(f) for f in flags)
            folders.append(FolderInfo(_text(name), flag_names, looks_like_sent(flag_names, _text(name))))
        return folders

    # -- reading ----------------------------------------------------------------------------
    def open_folder(self, name: str = "INBOX") -> FolderState:
        info = self._wrap(lambda: self._client.select_folder(name, readonly=True))
        return FolderState(name, int(info[b"UIDVALIDITY"]), int(info[b"EXISTS"]))

    def latest_uids(self, limit: int) -> list[int]:
        uids = sorted(int(u) for u in self._wrap(lambda: self._client.search(["ALL"])))
        return uids[-limit:] if limit > 0 else []

    def uids_after(self, last_uid: int) -> list[int]:
        """UIDs strictly greater than ``last_uid``.

        IMAP quirk: ``N:*`` always includes the highest UID even when it is below N,
        so the result must be filtered.
        """
        found = self._wrap(lambda: self._client.search(["UID", f"{last_uid + 1}:*"]))
        return sorted(int(u) for u in found if int(u) > last_uid)

    def fetch_raw(self, uids: list[int], max_bytes: int = MAX_MESSAGE_BYTES) -> list[RawMessage]:
        """Fetch full messages without marking them read. Oversized ones are skipped."""
        if not uids:
            return []
        sizes = self._wrap(lambda: self._client.fetch(uids, ["RFC822.SIZE"]))
        wanted = [u for u in uids if int(sizes.get(u, {}).get(b"RFC822.SIZE", 0)) <= max_bytes]
        results: dict[int, RawMessage] = {}
        for uid in uids:
            if uid not in wanted:
                size = int(sizes.get(uid, {}).get(b"RFC822.SIZE", 0))
                results[uid] = RawMessage(uid, size, None, (), f"larger than {max_bytes} bytes")
        for i in range(0, len(wanted), FETCH_BATCH):
            batch = wanted[i : i + FETCH_BATCH]
            # BODY.PEEK[] is the crucial part: it does not set the \Seen flag.
            data = self._wrap(lambda b=batch: self._client.fetch(b, ["BODY.PEEK[]", "FLAGS", "RFC822.SIZE", "INTERNALDATE"]))
            for uid in batch:
                item = data.get(uid)
                if not item or b"BODY[]" not in item:
                    results[uid] = RawMessage(uid, 0, None, (), "server returned no body")
                    continue
                results[uid] = RawMessage(
                    uid,
                    int(item.get(b"RFC822.SIZE", len(item[b"BODY[]"]))),
                    item[b"BODY[]"],
                    tuple(_text(f) for f in item.get(b"FLAGS", ())),
                    received_at=_as_utc(item.get(b"INTERNALDATE")),
                )
        return [results[u] for u in uids]
