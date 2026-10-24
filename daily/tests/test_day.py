import datetime as dt

import pytest

from daily import day as D


def test_window_is_the_local_day_in_utc():
    start, end = D.day_window(dt.date(2026, 10, 16))
    assert start.isoformat() == "2026-10-16T07:00:00+00:00"
    assert end.isoformat() == "2026-10-17T07:00:00+00:00"


def test_window_spans_25_hours_when_dst_ends():
    start, end = D.day_window(dt.date(2026, 11, 1))
    assert end - start == dt.timedelta(hours=25)


def test_window_spans_23_hours_when_dst_starts():
    start, end = D.day_window(dt.date(2026, 3, 8))
    assert end - start == dt.timedelta(hours=23)


def test_in_day_accepts_z_offsets_and_epoch():
    d = dt.date(2026, 10, 16)
    assert D.in_day("2026-10-16T07:00:00Z", d)  # start is inclusive
    assert D.in_day("2026-10-16T23:59:59-07:00", d)
    assert D.in_day("2026-10-16T20:00:00.123456+00:00", d)
    assert D.in_day(dt.datetime(2026, 10, 16, 20, tzinfo=dt.timezone.utc).timestamp(), d)


def test_in_day_rejects_edges_naive_and_junk():
    d = dt.date(2026, 10, 16)
    assert not D.in_day("2026-10-16T06:59:59Z", d)  # still Oct 15 in the configured zone
    assert not D.in_day("2026-10-17T07:00:00Z", d)  # end is exclusive
    assert not D.in_day("2026-10-16T12:00:00", d)  # naive: refused
    assert not D.in_day("", d)
    assert not D.in_day(None, d)
    assert not D.in_day("soon", d)


def test_parse_day():
    today = dt.date(2026, 10, 17)
    assert D.parse_day("yesterday", today) == dt.date(2026, 10, 16)
    assert D.parse_day("2026-10-15", today) == dt.date(2026, 10, 15)


def test_gh_range_uses_the_zone_offset_of_that_day():
    assert D.gh_range(dt.date(2026, 10, 16)) == "2026-10-16T00:00:00-07:00..2026-10-16T23:59:59-07:00"
    assert D.gh_range(dt.date(2026, 12, 1)) == "2026-12-01T00:00:00-08:00..2026-12-01T23:59:59-08:00"


def test_in_day_rejects_junk_numbers_without_raising():
    d = dt.date(2026, 10, 16)
    inside = dt.datetime(2026, 10, 16, 20, tzinfo=dt.timezone.utc).timestamp()
    assert D.in_day(inside, d)  # positive control: a valid epoch inside the day
    assert D.in_day(int(inside), d)
    for junk in (float("nan"), float("inf"), float("-inf"), 1.7e12, 1.7e18, True, False):
        assert D.in_day(junk, d) is False, junk
    assert D.to_utc(1.7e12) is None  # milliseconds epoch is not seconds


def test_in_day_accepts_the_real_timestamp_formats():
    d = dt.date(2026, 10, 16)
    assert D.in_day("2026-10-16T20:00:00.003Z", d)  # 3-digit milliseconds with Z
    assert D.in_day("2026-10-16T20:00:00.123456+00:00", d)  # microseconds with a numeric offset


def test_in_day_rejects_values_that_overflow_conversion():
    d = dt.date(2026, 10, 16)
    assert D.in_day("0001-01-01T00:00:00+01:00", d) is False  # astimezone(UTC) underflows
    assert D.in_day("9999-12-31T23:59:59-01:00", d) is False  # astimezone(UTC) overflows
    assert D.in_day(10**400, d) is False  # too large to convert to float
    assert D.to_utc(10**400) is None


# ---- --day due: the day whose run was due at the most recent 05:30 local time ----

def _pt(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=D.zone())


def test_due_before_0530_is_the_day_before_yesterday():
    now = _pt(2026, 10, 19, 4, 0)   # a Persistent catch-up woke early: the 05:30 of 10-18 was missed, it recaps 10-17
    assert D.due_day(now) == dt.date(2026, 10, 17)
    assert D.parse_day("due", now.date(), now) == dt.date(2026, 10, 17)


def test_due_from_0530_is_yesterday():
    assert D.due_day(_pt(2026, 10, 19, 6, 0)) == dt.date(2026, 10, 18)
    assert D.due_day(_pt(2026, 10, 19, 5, 30)) == dt.date(2026, 10, 18)
    assert D.due_day(_pt(2026, 10, 19, 5, 29)) == dt.date(2026, 10, 17)


def test_due_is_dst_safe_on_the_transition_days():
    # US spring forward 2026-03-08 (02:00 -> 03:00), fall back 2026-11-01 (02:00 -> 01:00)
    assert D.due_day(_pt(2026, 3, 8, 4, 0)) == dt.date(2026, 3, 6)
    assert D.due_day(_pt(2026, 3, 8, 6, 0)) == dt.date(2026, 3, 7)
    assert D.due_day(_pt(2026, 3, 9, 4, 0)) == dt.date(2026, 3, 7)
    assert D.due_day(_pt(2026, 11, 1, 4, 0)) == dt.date(2026, 10, 30)
    assert D.due_day(_pt(2026, 11, 1, 6, 0)) == dt.date(2026, 10, 31)
    assert D.due_day(_pt(2026, 11, 2, 4, 0)) == dt.date(2026, 10, 31)


def test_due_uses_the_configured_zone_whatever_zone_the_clock_is_in():
    utc = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)   # 05:00 PDT
    assert D.due_day(utc) == dt.date(2026, 10, 17)
    utc = dt.datetime(2026, 10, 19, 13, 30, tzinfo=dt.timezone.utc)   # 06:30 PDT
    assert D.due_day(utc) == dt.date(2026, 10, 18)


def test_due_matches_the_literal_formula_away_from_dst():
    for h in (0, 3, 5, 6, 12, 23):
        now = _pt(2026, 10, 19, h, 15)
        assert D.due_day(now) == (now - dt.timedelta(hours=5, minutes=30)).date() - dt.timedelta(days=1)


# ---- the zone is the operator's (settings.toml `timezone`) ----

def test_another_zone_moves_the_window():
    D.use_timezone("Europe/Berlin")
    start, end = D.day_window(dt.date(2026, 10, 16))
    assert start.isoformat() == "2026-10-15T22:00:00+00:00" and end - start == dt.timedelta(hours=24)
    assert D.zone_name() == "Europe/Berlin"
    assert D.gh_range(dt.date(2026, 10, 16)) == "2026-10-16T00:00:00+02:00..2026-10-16T23:59:59+02:00"


def test_utc_is_a_zone_too():
    D.use_timezone("UTC")
    start, end = D.day_window(dt.date(2026, 10, 16))
    assert start.isoformat() == "2026-10-16T00:00:00+00:00" and end.isoformat() == "2026-10-17T00:00:00+00:00"


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "  ", None, 5, "../etc/passwd"])
def test_an_unknown_zone_is_refused_and_the_old_one_stays(name):
    before = D.zone_name()
    with pytest.raises(ValueError, match="^bad_timezone$"):
        D.use_timezone(name)
    assert D.zone_name() == before
