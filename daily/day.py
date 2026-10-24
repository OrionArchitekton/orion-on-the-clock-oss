"""The calendar day every source is cut to, in the operator's own time zone (settings.toml `timezone`).

run.py calls `use_timezone` once per run, from the validated settings, before any stage reads a source. Until then the
zone is UTC, so a library caller that never configures one still gets a well-defined (if foreign) day.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
_ZONE: dt.tzinfo = UTC
_ZONE_NAME = "UTC"


def use_timezone(name: str) -> None:
    """Cut every later day to the IANA zone `name` ("America/Chicago"). An unknown name raises ValueError, so the
    caller fails closed instead of guessing a zone."""
    global _ZONE, _ZONE_NAME
    if not isinstance(name, str) or not name.strip():
        raise ValueError("bad_timezone")
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError("bad_timezone") from e
    _ZONE, _ZONE_NAME = zone, name


def zone() -> dt.tzinfo:
    return _ZONE


def zone_name() -> str:
    return _ZONE_NAME


def day_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time(0), tzinfo=_ZONE)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=_ZONE)
    return start.astimezone(UTC), end.astimezone(UTC)


def to_utc(ts) -> dt.datetime | None:
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        try:
            if not math.isfinite(ts):
                return None
            return dt.datetime.fromtimestamp(ts, UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(ts, str) or not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    try:
        return t.astimezone(UTC)
    except OverflowError:
        return None


def in_day(ts, day: dt.date) -> bool:
    t = to_utc(ts)
    if t is None:
        return False
    start, end = day_window(day)
    return start <= t < end


RUN_DUE = dt.time(5, 30)   # local time the example systemd timer fires (systemd/ OnCalendar); keep the two in step


def due_day(now: dt.datetime) -> dt.date:
    """The day whose run was due at the most recent RUN_DUE local time: yesterday once it has passed, the day before
    yesterday before it (a Persistent catch-up after an early wake still recaps the MISSED day). Wall-clock compare in
    the configured zone, no timedelta arithmetic, so a DST transition cannot shift it."""
    local = now.astimezone(_ZONE)
    return local.date() - dt.timedelta(days=1 if local.time() >= RUN_DUE else 2)


def parse_day(s: str, today: dt.date, now: dt.datetime | None = None) -> dt.date:
    if s == "yesterday":
        return today - dt.timedelta(days=1)
    if s == "due":
        return due_day(now or dt.datetime.now(_ZONE))
    return dt.date.fromisoformat(s)


def gh_range(day: dt.date) -> str:
    """`gh search --merged-at` range for the local day (gh reads bare dates as UTC days)."""
    start = dt.datetime.combine(day, dt.time(0), tzinfo=_ZONE)
    last = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=_ZONE)
    return f"{start.isoformat()}..{last.isoformat()}"
