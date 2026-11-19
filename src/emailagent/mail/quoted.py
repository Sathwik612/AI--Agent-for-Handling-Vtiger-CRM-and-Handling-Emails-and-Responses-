"""Separate what the person *just wrote* from the quoted thread history underneath.

A heuristic on purpose: the full original body is always kept alongside, so a wrong split
loses nothing. The AI sees ``new_text`` (cheap, focused) plus the structured thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ORIGINAL_MSG = re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
_OUTLOOK_FROM = re.compile(r"^\s*From:\s+\S")
_OUTLOOK_NEXT = re.compile(r"^\s*(Sent|Date):\s+\S")
_UNDERSCORES = re.compile(r"^\s*_{5,}\s*$")
_ON_START = re.compile(r"^\s*On\s", re.IGNORECASE)
_WROTE_END = re.compile(r"wrote:\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SplitResult:
    new_text: str
    quoted_text: str
    marker: str | None  # which pattern ended the new text (None = nothing quoted found)


def _is_attribution(lines: list[str], i: int) -> bool:
    """'On <date>, <name> <email> wrote:' - may be wrapped over up to 3 lines."""
    if not _ON_START.match(lines[i]):
        return False
    for k in (1, 2, 3):
        joined = " ".join(s.strip() for s in lines[i : i + k])
        if _WROTE_END.search(joined) and re.search(r"\d", joined):  # a date/time must be present
            return True
    return False


def _is_outlook_header(lines: list[str], i: int) -> bool:
    return bool(_OUTLOOK_FROM.match(lines[i])) and any(
        _OUTLOOK_NEXT.match(s) for s in lines[i + 1 : i + 4]
    )


def split_reply(body: str) -> SplitResult:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    marker_idx: int | None = None
    marker_kind: str | None = None

    for i, line in enumerate(lines):
        if _ORIGINAL_MSG.match(line):
            marker_idx, marker_kind = i, "original-message"
        elif _is_attribution(lines, i):
            marker_idx, marker_kind = i, "attribution"
        elif _is_outlook_header(lines, i):
            # include a preceding '_____' separator line in the quoted part
            marker_idx = i - 1 if i > 0 and _UNDERSCORES.match(lines[i - 1]) else i
            marker_kind = "outlook-header"
        elif line.lstrip().startswith(">"):
            marker_idx, marker_kind = i, "quote-prefix"
        if marker_idx is not None:
            break

    if marker_idx is None:
        return SplitResult(body.strip(), "", None)

    new_text = "\n".join(lines[:marker_idx]).strip()
    quoted = "\n".join(lines[marker_idx:]).strip()

    if not new_text and marker_kind in {"attribution", "quote-prefix"}:
        # Bottom/inline posting: the person's words are the unquoted lines under the quote.
        own = [ln for ln in lines[marker_idx:] if ln.strip() and not ln.lstrip().startswith(">")]
        own = [ln for ln in own if not _is_attribution([ln], 0) and not _WROTE_END.search(ln)]
        if own:
            return SplitResult("\n".join(own).strip(), quoted, marker_kind)
    return SplitResult(new_text, quoted, marker_kind)
