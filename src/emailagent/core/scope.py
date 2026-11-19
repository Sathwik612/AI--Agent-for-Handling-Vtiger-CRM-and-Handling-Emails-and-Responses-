"""What the agent is allowed to work on. Everything here is configuration, not code:
add a state or a BDA by editing settings, never by changing logic.

Fail-closed: if the allowed lists are empty, NOTHING is in scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_NAME_TO_ABBR = {name.lower(): abbr for abbr, name in US_STATES.items()}
_PAREN_ABBR = re.compile(r"\(([A-Za-z]{2})\)")


def normalize_state(value: str | None) -> str | None:
    """'Ohio', 'OH', 'oh.', 'Ohio (OH)' -> 'OH'. Unrecognised values -> None (never guessed)."""
    if not value or not value.strip():
        return None
    text = value.strip()
    paren = _PAREN_ABBR.search(text)
    if paren and paren.group(1).upper() in US_STATES:
        return paren.group(1).upper()
    cleaned = re.sub(r"[.\s]+$", "", text).strip()
    if cleaned.upper() in US_STATES:
        return cleaned.upper()
    return _NAME_TO_ABBR.get(re.sub(r"\s+", " ", cleaned).lower())


def normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().casefold()


class ScopeVerdict(StrEnum):
    IN_SCOPE = "in_scope"
    NO_LEAD = "no_lead"  # not in the CRM: the AI must read the thread to decide
    OUT_OF_SCOPE_STATE = "out_of_scope_state"
    OUT_OF_SCOPE_BDA = "out_of_scope_bda"
    STATE_UNRECOGNISED = "state_unrecognised"  # needs the operator's eyes; never guessed
    NOT_CONFIGURED = "not_configured"  # allowed lists are empty -> nothing is in scope


@dataclass(frozen=True)
class ScopeDecision:
    verdict: ScopeVerdict
    reason: str


@dataclass(frozen=True)
class ScopeRules:
    allowed_states: frozenset[str]
    allowed_bdas: frozenset[str]  # normalised with normalize_name()

    @classmethod
    def from_settings(cls, states_csv: str, bdas_csv: str) -> ScopeRules:
        states: set[str] = set()
        unknown: list[str] = []
        for token in (t.strip() for t in states_csv.split(",")):
            if not token:
                continue
            abbr = normalize_state(token)
            if abbr:
                states.add(abbr)
            else:
                unknown.append(token)
        if unknown:
            raise ValueError(f"Unknown state(s) in AGENT_ALLOWED_STATES: {', '.join(unknown)}")
        bdas = {normalize_name(t) for t in bdas_csv.split(",") if t.strip()}
        return cls(frozenset(states), frozenset(bdas))

    @property
    def is_configured(self) -> bool:
        return bool(self.allowed_states) and bool(self.allowed_bdas)


def decide_scope(
    *, lead_found: bool, lead_state: str | None, lead_bda: str | None, rules: ScopeRules
) -> ScopeDecision:
    if not rules.is_configured:
        return ScopeDecision(
            ScopeVerdict.NOT_CONFIGURED, "AGENT_ALLOWED_STATES / AGENT_ALLOWED_BDAS are not both set"
        )
    if not lead_found:
        return ScopeDecision(ScopeVerdict.NO_LEAD, "sender does not match a CRM lead")
    state = normalize_state(lead_state)
    if state is None:
        what = "has no state" if not (lead_state or "").strip() else f"has an unrecognised state {lead_state!r}"
        return ScopeDecision(ScopeVerdict.STATE_UNRECOGNISED, f"lead {what}")
    if state not in rules.allowed_states:
        return ScopeDecision(ScopeVerdict.OUT_OF_SCOPE_STATE, f"lead state {state} is not enabled")
    if normalize_name(lead_bda) not in rules.allowed_bdas:
        return ScopeDecision(ScopeVerdict.OUT_OF_SCOPE_BDA, "lead belongs to a BDA that is not enabled")
    return ScopeDecision(ScopeVerdict.IN_SCOPE, f"state {state}, BDA enabled")
