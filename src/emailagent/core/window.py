"""The agent's off-hours window.

The agent covers mail that arrives while the BDAs are offline: from ``start`` (e.g. 18:30) until
``end`` (e.g. 10:00) the next morning, in the operator's timezone. Mail arriving during the
BDAs' day is left alone.

Work days: on days the BDAs do not work (e.g. Saturday and Sunday) the whole day is off-hours,
so the Monday window runs from Friday 18:30 straight through to Monday 10:00.

Catch-up: if the operator skips a day (holiday), the next morning session processes every window
that ended since the last processed one, so no off-hours mail is lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MAX_CATCHUP_WINDOWS = 14
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

Window = tuple[datetime, datetime]

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
ALL_DAYS = frozenset(range(7))


def parse_work_days(value: str) -> frozenset[int]:
    """'MON,TUE,Wednesday' -> {0, 1, 2}. Unknown names are an error, never ignored."""
    days: set[int] = set()
    unknown: list[str] = []
    for token in (t.strip() for t in value.split(",")):
        if not token:
            continue
        key = token.upper()[:3]
        if key in WEEKDAYS:
            days.add(WEEKDAYS[key])
        else:
            unknown.append(token)
    if unknown:
        raise ValueError(f"Unknown day(s) in WORK_DAYS: {', '.join(unknown)}")
    if not days:
        raise ValueError("WORK_DAYS must list at least one day")
    return frozenset(days)


def _parse_hhmm(value: str, name: str) -> time:
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"{name} must be HH:MM (24-hour), got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


@dataclass(frozen=True)
class ProcessingWindow:
    start: time
    end: time
    tz: ZoneInfo
    work_days: frozenset[int] = ALL_DAYS  # weekdays (Mon=0) the BDAs work

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    @classmethod
    def from_settings(
        cls, start: str, end: str, tz_name: str, work_days: str = "MON,TUE,WED,THU,FRI,SAT,SUN"
    ) -> ProcessingWindow:
        s, e = _parse_hhmm(start, "WINDOW_START"), _parse_hhmm(end, "WINDOW_END")
        if s == e:
            raise ValueError("WINDOW_START and WINDOW_END must differ")
        days = parse_work_days(work_days)
        if days != ALL_DAYS and e > s:
            raise ValueError(
                "WORK_DAYS with days off needs an overnight window (WINDOW_START later than WINDOW_END)"
            )
        return cls(s, e, ZoneInfo(tz_name), days)

    def _previous_work_day(self, day: date) -> date:
        d = day - timedelta(days=1)
        while d.weekday() not in self.work_days:
            d -= timedelta(days=1)
        return d

    def window_ending_on(self, day: date) -> Window | None:
        """The window that ends on ``day`` (None if the BDAs do not work that day)."""
        if day.weekday() not in self.work_days:
            return None
        end = datetime.combine(day, self.end, tzinfo=self.tz)
        if self.overnight:  # it opened on the previous WORKING day (Mon <- Fri)
            start = datetime.combine(self._previous_work_day(day), self.start, tzinfo=self.tz)
        else:
            start = datetime.combine(day, self.start, tzinfo=self.tz)
        return start, end

    def latest_window(self, now: datetime) -> Window:
        local = now.astimezone(self.tz)
        for back in range(8):
            w = self.window_ending_on(local.date() - timedelta(days=back))
            if w is not None and w[1] <= local:
                return w
        raise AssertionError("unreachable: some work day always has a window")

    def windows_between(self, after: datetime, upto: datetime) -> list[Window]:
        """Every window whose end is in (after, upto], oldest first."""
        first = after.astimezone(self.tz).date() - timedelta(days=1)
        last = upto.astimezone(self.tz).date() + timedelta(days=1)
        out: list[Window] = []
        day = first
        while day <= last:
            w = self.window_ending_on(day)
            if w is not None and after < w[1] <= upto:
                out.append(w)
            day += timedelta(days=1)
        return out[-MAX_CATCHUP_WINDOWS:]

    def is_off_hours(self, ts: datetime) -> bool:
        local = ts.astimezone(self.tz)
        if local.weekday() not in self.work_days:  # a day the BDAs are off: all day counts
            return True
        t = local.time()
        if self.start > self.end:
            return t >= self.start or t < self.end
        return self.start <= t < self.end

    @staticmethod
    def contains(ts: datetime, windows: list[Window]) -> bool:
        return any(start <= ts < end for start, end in windows)
