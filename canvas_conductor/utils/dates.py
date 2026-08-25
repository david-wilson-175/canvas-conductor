"""Date and time helpers shared across command groups.

Canvas stores every timestamp in UTC and returns it ISO-8601 with a `Z`
suffix. Humans scheduling a course think in local calendar days ("the
reading opens Sept 15"). This module is the single place that bridges the
two, so that "Sept 15" means 11:59 PM *in the course's timezone* rather
than 11:59 PM UTC — which, in Mountain Time, would land at 5:59 PM the
same afternoon.

Timezone resolution order (see `resolve_timezone`):
    1. an explicit `--tz` flag
    2. `[defaults] timezone` in config.toml
    3. the machine's local timezone
    4. UTC
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import get_defaults
from ..exceptions import ConfigError


# Sentinel meaning "send an explicit empty value to Canvas to clear this
# field". `prefix_keys` drops None, so None can't express "clear" — it means
# "leave alone". Canvas clears a date field when it receives an empty string.
CLEAR = ""

DEFAULT_TIME_OF_DAY = "23:59"

_DURATION_RE = re.compile(r"^(?P<sign>-?)(?P<n>\d+)(?P<unit>[dhwm])$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")


def parse_shift(value: str) -> timedelta:
    """Parse `7d`, `-3d`, `12h`, `2w`, `30m` into a `timedelta`."""
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid shift '{value}'. Examples: 7d, -1d, 12h, 2w, 30m")
    sign = -1 if m["sign"] else 1
    n = int(m["n"]) * sign
    unit = m["unit"]
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "w":
        return timedelta(weeks=n)
    if unit == "m":
        return timedelta(minutes=n)
    raise ValueError(f"Unknown unit: {unit}")  # pragma: no cover


def parse_time_of_day(value: str) -> time:
    """Parse `HH:MM` (24-hour) into a `time`."""
    m = _TIME_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"Invalid time '{value}'. Use 24-hour HH:MM, e.g. 23:59.")
    hour, minute = int(m["h"]), int(m["m"])
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time '{value}'. Use 24-hour HH:MM, e.g. 23:59.")
    return time(hour=hour, minute=minute)


def resolve_timezone(explicit: str | None = None) -> ZoneInfo:
    """Return the timezone bare calendar dates should be interpreted in.

    Explicit flag wins, then `[defaults] timezone` in config.toml, then the
    machine's local zone, then UTC. An unknown zone name is a config error
    rather than a silent fallback — a typo'd zone would shift every date.
    """
    name = explicit or (get_defaults().get("timezone") or None)
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"Unknown timezone {name!r}. Use an IANA name such as "
                "'America/Denver'."
            ) from exc

    local = datetime.now().astimezone().tzinfo
    if isinstance(local, ZoneInfo):
        return local
    # `astimezone()` on some platforms yields a fixed-offset tzinfo with no
    # IANA identity. Try to recover the name from it; fall back to UTC.
    try:
        return ZoneInfo(str(local))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_canvas_datetime(
    value: str,
    tz: ZoneInfo | None = None,
    at_time: str | time = DEFAULT_TIME_OF_DAY,
) -> str:
    """Normalize a user-supplied date into Canvas's UTC ISO-8601 form.

    Accepts either a bare calendar date (`2026-09-15`), which is anchored at
    `at_time` in `tz`, or any ISO-8601 datetime. A datetime carrying its own
    offset is respected as given; a naive one is read as local to `tz`.

        >>> to_canvas_datetime("2026-09-15", ZoneInfo("America/Denver"))
        '2026-09-16T05:59:00Z'
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Empty date value.")

    tz = tz or resolve_timezone()
    tod = parse_time_of_day(at_time) if isinstance(at_time, str) else at_time

    if _DATE_ONLY_RE.match(raw):
        parsed = datetime.combine(
            datetime.strptime(raw, "%Y-%m-%d").date(), tod, tzinfo=tz
        )
    else:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Invalid date {value!r}. Use YYYY-MM-DD or a full ISO-8601 "
                "datetime such as 2026-09-15T23:59:00-06:00."
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)

    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift_iso(value: str | None, delta: timedelta) -> str | None:
    """Shift a Canvas ISO timestamp by `delta`, returning UTC ISO. None-safe.

    The shift is applied to the absolute instant, so a `7d` shift across a
    DST boundary keeps the wall-clock time in the originating zone only if
    that zone's offset is unchanged. For whole-day course scheduling this is
    what you want: `bulk-todo --shift 7d` moves Tuesday to Tuesday.
    """
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    shifted = dt + delta
    if shifted.tzinfo is None:
        shifted = shifted.replace(tzinfo=timezone.utc)
    return shifted.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(value: str | None, tz: ZoneInfo | None = None) -> str:
    """Render a Canvas UTC timestamp as a local `YYYY-MM-DD HH:MM` string.

    Used for display so a to-do date reads back the way it was typed rather
    than as the UTC instant it is stored as.
    """
    if not value:
        return ""
    tz = tz or resolve_timezone()
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
