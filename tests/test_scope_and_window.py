from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from emailagent.core.scope import ScopeRules, ScopeVerdict, decide_scope, normalize_state
from emailagent.core.window import ProcessingWindow

IST = ZoneInfo("Asia/Kolkata")


def dt(day: int, h: int, m: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, h, m, tzinfo=IST)


# ------------------------------------------------------------------ scope
@pytest.mark.parametrize(("raw", "expected"), [
    ("OH", "OH"), ("oh", "OH"), ("OH.", "OH"), (" Ohio ", "OH"), ("ohio", "OH"), ("Ohio (OH)", "OH"),
    ("TX", "TX"), ("Texas", "TX"), ("new  york", "NY"), ("District of Columbia", "DC"),
    ("", None), (None, None), ("Narnia", None), ("TZ", None), ("O", None),
])
def test_normalize_state(raw, expected) -> None:
    assert normalize_state(raw) == expected


def _rules(states="OH,TX", bdas="Rupa") -> ScopeRules:
    return ScopeRules.from_settings(states, bdas)


def test_rules_accept_names_or_codes_and_add_any_state_without_code_changes() -> None:
    assert _rules("Ohio, texas").allowed_states == {"OH", "TX"}
    assert _rules("OH,TX,KY,LA").allowed_states == {"OH", "TX", "KY", "LA"}


def test_typo_in_allowed_states_is_an_error_not_silently_ignored() -> None:
    with pytest.raises(ValueError, match="TZ"):
        _rules("OH,TZ")


def test_empty_rules_are_fail_closed() -> None:
    for rules in (_rules("", ""), _rules("OH,TX", ""), _rules("", "Rupa")):
        d = decide_scope(lead_found=True, lead_state="OH", lead_bda="Rupa", rules=rules)
        assert d.verdict == ScopeVerdict.NOT_CONFIGURED


@pytest.mark.parametrize(("found", "state", "bda", "verdict"), [
    (True, "OH", "Rupa", ScopeVerdict.IN_SCOPE),
    (True, "Texas", "rupa ", ScopeVerdict.IN_SCOPE),
    (True, "OH.", "RUPA", ScopeVerdict.IN_SCOPE),
    (True, "KY", "Rupa", ScopeVerdict.OUT_OF_SCOPE_STATE),
    (True, "OH", "Someone Else", ScopeVerdict.OUT_OF_SCOPE_BDA),
    (True, "OH", "", ScopeVerdict.OUT_OF_SCOPE_BDA),
    (True, "OH", None, ScopeVerdict.OUT_OF_SCOPE_BDA),
    (True, "", "Rupa", ScopeVerdict.STATE_UNRECOGNISED),
    (True, "Atlantis", "Rupa", ScopeVerdict.STATE_UNRECOGNISED),
    (False, None, None, ScopeVerdict.NO_LEAD),
])
def test_decide_scope(found, state, bda, verdict) -> None:
    assert decide_scope(lead_found=found, lead_state=state, lead_bda=bda, rules=_rules()).verdict == verdict


# ------------------------------------------------------------------ window
W = ProcessingWindow.from_settings("18:30", "10:00", "Asia/Kolkata")


def test_latest_window_after_the_morning_cutoff() -> None:
    start, end = W.latest_window(dt(20, 10, 30))  # Fri 10:30 IST
    assert (start, end) == (dt(19, 18, 30), dt(20, 10, 0))


def test_latest_window_before_the_cutoff_uses_yesterdays() -> None:
    assert W.latest_window(dt(20, 9, 0)) == (dt(18, 18, 30), dt(19, 10, 0))


def test_overnight_membership_and_exclusive_end() -> None:
    win = [W.latest_window(dt(20, 10, 30))]
    assert W.contains(dt(19, 18, 30), win)          # opening instant is inside
    assert W.contains(dt(19, 23, 59), win)
    assert W.contains(dt(20, 9, 59), win)
    assert not W.contains(dt(20, 10, 0), win)       # end is exclusive
    assert not W.contains(dt(19, 18, 29), win)
    assert not W.contains(dt(19, 15, 0), win)       # BDA working hours: left to the BDAs


def test_is_off_hours_time_of_day() -> None:
    assert W.is_off_hours(dt(19, 18, 30)) and W.is_off_hours(dt(19, 3, 0))
    assert not W.is_off_hours(dt(19, 10, 0)) and not W.is_off_hours(dt(19, 14, 0))


def test_us_timestamps_are_converted_correctly() -> None:
    # 7:00 AM Eastern (UTC-5) on 19 Nov = 12:00 UTC = 17:30 IST -> BDA hours, outside the window
    est = datetime(2026, 11, 19, 7, 0, tzinfo=ZoneInfo("America/New_York"))
    assert not W.is_off_hours(est)
    # 9:00 AM Eastern = 19:30 IST -> inside
    assert W.is_off_hours(datetime(2026, 11, 19, 9, 0, tzinfo=ZoneInfo("America/New_York")))


def test_catch_up_after_a_weekend_returns_every_missed_window() -> None:
    last_processed_end = dt(20, 10, 0)           # Fri 10:00 was the last processed cutoff
    windows = W.windows_between(last_processed_end, dt(23, 10, 30))  # Mon 10:30
    assert [w[1] for w in windows] == [dt(21, 10), dt(22, 10), dt(23, 10)]
    assert windows[0][0] == dt(20, 18, 30)       # Fri evening (US Friday business day) is covered
    assert W.contains(dt(21, 2, 0), windows) and not W.contains(dt(21, 14, 0), windows)


def test_no_windows_when_nothing_new() -> None:
    assert W.windows_between(dt(20, 10, 0), dt(20, 10, 30)) == []


def test_catch_up_is_capped() -> None:
    assert len(W.windows_between(dt(1, 10, 0), dt(28, 10, 30))) == 14


def test_daytime_window_config_also_works() -> None:
    day = ProcessingWindow.from_settings("09:00", "17:00", "Asia/Kolkata")
    assert day.window_ending_on(dt(19, 0).date()) == (dt(19, 9), dt(19, 17))
    assert day.is_off_hours(dt(19, 12)) and not day.is_off_hours(dt(19, 18))


@pytest.mark.parametrize("start", ["25:00", "6pm", "18:75", ""])
def test_bad_times_are_rejected(start) -> None:
    with pytest.raises(ValueError):
        ProcessingWindow.from_settings(start, "10:00", "Asia/Kolkata")


def test_equal_start_and_end_rejected() -> None:
    with pytest.raises(ValueError):
        ProcessingWindow.from_settings("10:00", "10:00", "Asia/Kolkata")


def test_duration_is_15_5_hours() -> None:
    s, e = W.latest_window(dt(20, 12))
    assert e - s == timedelta(hours=15, minutes=30)


# ------------------------------------------------------------------ weekends (BDAs work Mon-Fri)
from emailagent.core.window import parse_work_days

WK = ProcessingWindow.from_settings("18:30", "10:00", "Asia/Kolkata", "MON,TUE,WED,THU,FRI")


def test_calendar_assumptions() -> None:
    assert dt(20, 0).strftime("%a") == "Fri" and dt(21, 0).strftime("%a") == "Sat"
    assert dt(22, 0).strftime("%a") == "Sun" and dt(23, 0).strftime("%a") == "Mon"


def test_monday_window_reaches_back_to_friday_evening() -> None:
    assert WK.latest_window(dt(23, 10, 30)) == (dt(20, 18, 30), dt(23, 10, 0))


def test_weekday_windows_are_unchanged() -> None:
    assert WK.latest_window(dt(24, 10, 30)) == (dt(23, 18, 30), dt(24, 10, 0))  # Tue session


def test_whole_weekend_counts_as_off_hours() -> None:
    for ts in (dt(20, 18, 30), dt(20, 23, 0), dt(21, 14, 0), dt(22, 3, 0), dt(22, 15, 0), dt(23, 9, 59)):
        assert WK.is_off_hours(ts), ts
    for ts in (dt(20, 10, 0), dt(20, 17, 0), dt(23, 10, 0), dt(23, 14, 0)):
        assert not WK.is_off_hours(ts), ts


def test_monday_catch_up_is_one_window_covering_the_weekend() -> None:
    windows = WK.windows_between(dt(20, 10, 0), dt(23, 10, 30))  # last processed Fri 10:00
    assert windows == [(dt(20, 18, 30), dt(23, 10, 0))]
    for ts in (dt(21, 14, 0), dt(22, 15, 0), dt(20, 20, 0), dt(23, 3, 0)):
        assert WK.contains(ts, windows), ts
    assert not WK.contains(dt(20, 17, 0), windows)  # Friday BDA hours


def test_us_friday_business_day_is_not_lost() -> None:
    # Friday 9 AM Eastern = Friday 19:30 IST: BDAs are already offline -> belongs to Monday's run
    fri_et = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    windows = WK.windows_between(dt(20, 10, 0), dt(23, 10, 30))
    assert WK.contains(fri_et, windows) and WK.is_off_hours(fri_et)


def test_running_on_a_weekend_finds_nothing_new() -> None:
    assert WK.windows_between(dt(20, 10, 0), dt(21, 11, 0)) == []  # Saturday session: no window ends
    assert WK.latest_window(dt(21, 11, 0)) == (dt(19, 18, 30), dt(20, 10, 0))  # still Friday's


def test_skipped_tuesday_is_caught_up_on_wednesday() -> None:
    windows = WK.windows_between(dt(23, 10, 0), dt(25, 10, 30))
    assert [w[1] for w in windows] == [dt(24, 10), dt(25, 10)]


def test_windows_and_off_hours_agree_everywhere() -> None:
    """Every instant over two weeks: contains() over all windows == is_off_hours()."""
    start, end = dt(16, 10, 0), dt(30, 10, 0)
    windows = WK.windows_between(start, end)
    ts = start
    while ts < end:
        assert WK.contains(ts, windows) == WK.is_off_hours(ts), ts
        ts += timedelta(minutes=30)


@pytest.mark.parametrize(("raw", "expected"), [
    ("MON,TUE,WED,THU,FRI", {0, 1, 2, 3, 4}), ("mon, Tuesday ,fri", {0, 1, 4}), ("SAT", {5}),
])
def test_parse_work_days(raw, expected) -> None:
    assert parse_work_days(raw) == expected


@pytest.mark.parametrize("bad", ["", "MON,FUNDAY", "xyz"])
def test_bad_work_days_rejected(bad) -> None:
    with pytest.raises(ValueError):
        parse_work_days(bad)


def test_days_off_require_an_overnight_window() -> None:
    with pytest.raises(ValueError, match="overnight"):
        ProcessingWindow.from_settings("09:00", "17:00", "Asia/Kolkata", "MON,TUE,WED,THU,FRI")


def test_seven_day_config_keeps_old_behaviour() -> None:
    all7 = ProcessingWindow.from_settings("18:30", "10:00", "Asia/Kolkata", "MON,TUE,WED,THU,FRI,SAT,SUN")
    assert [w[1] for w in all7.windows_between(dt(20, 10, 0), dt(23, 10, 30))] == [dt(21, 10), dt(22, 10), dt(23, 10)]
