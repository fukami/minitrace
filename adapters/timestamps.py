"""Timestamp normalisation to the spec §3.5 form, shared by the preprocessors.

Spec §3.5 and the base schema require UTC, six fractional digits, and a Z
suffix (2026-05-03T10:00:00.123456Z). Harnesses write other ISO 8601 shapes:
milliseconds with Z (codex, cursor), microseconds with a numeric offset (vibe).
to_spec_utc() converts any of those to the required form: the same instant,
zero-padded, never rounded.

It reads a timestamp as the DSL's iso_normalise does (parse_iso_utc, DSL
rc3 §9.1; a fraction longer than six digits is truncated, a leap second
read as 23:59:59.999999). A value it cannot read is null, never passed
through, and counted for the caller's warning line (take_refused; ruled
2026-10-29). None and the empty string return None.

Regex rather than datetime.fromisoformat, which on Python 3.9 rejects Z and
fractions of other than three or six digits.

claude-code reads its record timestamps through to_spec_utc as well (ruled
2026-11-08).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

def spec_ts(d: datetime) -> str:
    """The spec §3.5 form of a datetime the caller already holds in UTC (a
    naive one is written as it is). Every writer in adapters/ uses this, not
    strftime: glibc's %Y writes year 999 as "999", macOS's as "0999",
    and a year below 1000 is reachable from input data."""
    return (f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:"
            f"{d.second:02d}.{d.microsecond:06d}Z")


def source_date_epoch() -> str | None:
    """SOURCE_DATE_EPOCH in spec §3.5 form, the one reading every writer in
    adapters/ uses (ruled 2026-10-22): None when unset or empty; ValueError
    when it is not ASCII digits alone (no sign, space, underscore or
    fraction) or names a time a §3.5 timestamp cannot hold."""
    import os  # noqa: PLC0415
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        return None
    if not (epoch.isascii() and epoch.isdigit()):
        raise ValueError(f"SOURCE_DATE_EPOCH {epoch[:40]!r} is not a whole number of seconds")
    try:
        return spec_ts(datetime.fromtimestamp(int(epoch), timezone.utc))
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"SOURCE_DATE_EPOCH {epoch[:40]!r} is outside the §3.5 range") from None


_ISO_EXTENDED = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})")


def parse_iso_utc(s: Any) -> datetime:
    """The one reading of a timestamp (DSL rc3 §9.1, iso_normalise; ruled
    2026-10-29 for the pre-processors too): an offset-bearing ISO 8601
    extended date-time with uppercase T and Z, a fraction of at least one
    digit truncated (not rounded) to six, a valid proleptic Gregorian date,
    hours 00-23, minutes 00-59, seconds 00-59 or a leap second, an offset
    below 24h with minutes 00-59 (-00:00 is UTC), and a UTC result in the
    years 0001-9999. A leap second is seconds 60 whose value after
    conversion to UTC is 23:59:60 (the offset applied to hours and
    minutes, the seconds carried), read as 23:59:59.999999 of that UTC day
    (minitrace-spec §3.5); any other second 60 is refused. Returns the UTC
    instant; raises TypeError or ValueError for anything else."""
    if not isinstance(s, str):
        raise TypeError(f"a timestamp must be a string, got {type(s).__name__}")
    m = _ISO_EXTENDED.fullmatch(s)
    if m is None:
        raise ValueError(f"not an offset-bearing ISO 8601 extended date-time: {s[:40]!r}")
    y, mo, d, h, mi, sec, frac, off = m.groups()
    if off == "Z":
        tz = timezone.utc
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            raise ValueError(f"offset out of range: {off}")
        tz = timezone((1 if off[0] == "+" else -1) * timedelta(hours=oh, minutes=om))
    leap = sec == "60"
    local = datetime(int(y), int(mo), int(d), int(h), int(mi), 59 if leap else int(sec),
                     0 if leap else int((frac or "")[:6].ljust(6, "0")), tzinfo=tz)   # ValueError if invalid
    try:
        utc = local.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError(f"{s[:40]!r} leaves the years 0001-9999 in UTC") from None
    if leap:
        if (utc.hour, utc.minute) != (23, 59):
            raise ValueError(f"second 60 is a leap second only at 23:59:60 UTC: {s[:40]!r}")
        utc = utc.replace(microsecond=999999)
    return utc


# Values to_spec_utc wrote as null since the last take_refused(), for the
# caller's warning line (ruled 2026-10-29).
_refused = 0


def take_refused() -> int:
    """The number of values to_spec_utc nulled since the last call; resets it."""
    global _refused
    n, _refused = _refused, 0
    return n


def to_spec_utc(ts: Any) -> str | None:
    """ts in the spec §3.5 form, read by parse_iso_utc. Ruled 2026-10-29:
    a value it cannot read (malformed, an invalid date, a second 60 that is
    no leap second, outside the years 0001-9999 in UTC, not a string) is
    never passed through: it is null and counted for take_refused(). None
    and the empty string are null and not counted."""
    global _refused
    if ts is None or ts == "":
        return None
    try:
        return spec_ts(parse_iso_utc(ts))
    except (TypeError, ValueError):
        _refused += 1
        return None


_NAIVE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?\Z"
)


def naive_local_to_utc(ts: Any, zone: str = "UTC") -> Any:
    """A zoneless local time (aider: '2026-07-25 03:11:02') read in `zone`,
    returned in spec §3.5 form.

    The zone is an assumption the caller supplies, not something the source
    states: callers MUST record which zone was applied (e.g. a provenance
    basis field). "UTC" uses timezone.utc and needs no tz database; any other
    value is an IANA name resolved through zoneinfo (ZoneInfoNotFoundError for
    an unknown one). Ambiguous or skipped wall times at a DST transition
    resolve with fold=0 (the earlier of two readings).

    Unrecognised shapes are returned unchanged for the schema gate to report
    (unlike to_spec_utc, which writes them as null; this reader's caller,
    aider, is outside that ruling); None and the empty string return None.
    """
    if ts is None or ts == "":
        return None
    if not isinstance(ts, str):
        return ts
    m = _NAIVE.match(ts)
    if not m:
        return ts
    if zone == "UTC":
        tz = timezone.utc
    else:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone)
    y, mo, d, h, mi, s, frac = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s),
                      int((frac or "").ljust(6, "0")), tzinfo=tz)
        utc = dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):  # 02-30, :60; a year leaving 0001-9999 in UTC
        return ts
    return spec_ts(utc)


def uuid7_time(value: Any) -> str | None:
    """Creation time embedded in a UUIDv7 (RFC 9562 §5.7), in spec §3.5 form.

    The first 48 bits of a UUIDv7 are Unix epoch milliseconds. This is a
    fallback anchor under spec §3.5.1 for sources that record no wall-clock
    time but identify the session with a UUIDv7: callers MUST record that the
    timestamp was derived this way (e.g. a provenance basis field), since it
    is the ID's creation time, not a recorded event time.

    Returns None unless value parses as a UUID whose version is 7.
    """
    if not isinstance(value, str):
        return None
    try:
        u = uuid.UUID(value)
    except ValueError:
        return None
    if u.version != 7:
        return None
    ms = u.int >> 80
    try:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)
    except OverflowError:  # 48 bits of ms reach year 10889; the id is session data
        return None
    return spec_ts(dt)
