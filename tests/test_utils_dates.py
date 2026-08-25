"""Unit tests for the shared date helpers.

The interesting cases are all timezone edges: a bare calendar date has to
land at end-of-day *local*, not end-of-day UTC, or Mountain Time deadlines
silently move to 5:59 PM.
"""
from __future__ import annotations

from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest

from canvas_conductor.exceptions import ConfigError
from canvas_conductor.utils.dates import (
    CLEAR,
    local_day,
    parse_shift,
    parse_time_of_day,
    resolve_timezone,
    shift_iso,
    to_canvas_datetime,
)


DENVER = ZoneInfo("America/Denver")


def test_bare_date_anchors_at_end_of_local_day():
    # 23:59 MDT (UTC-6) is 05:59 the next morning in UTC.
    assert to_canvas_datetime("2026-09-15", DENVER) == "2026-09-16T05:59:00Z"


def test_bare_date_respects_standard_time_offset():
    # January is MST (UTC-7), so the same wall clock is an hour later in UTC.
    assert to_canvas_datetime("2026-01-15", DENVER) == "2026-01-16T06:59:00Z"


def test_at_time_override():
    assert to_canvas_datetime("2026-09-15", DENVER, "08:00") == "2026-09-15T14:00:00Z"


def test_explicit_offset_is_respected_not_reinterpreted():
    assert (
        to_canvas_datetime("2026-09-15T09:00:00-06:00", DENVER) == "2026-09-15T15:00:00Z"
    )


def test_utc_z_input_round_trips():
    assert to_canvas_datetime("2026-08-03T23:59:00Z", DENVER) == "2026-08-03T23:59:00Z"


def test_naive_datetime_is_read_as_local():
    assert to_canvas_datetime("2026-09-15 23:59", DENVER) == "2026-09-16T05:59:00Z"


def test_invalid_date_raises_value_error():
    with pytest.raises(ValueError, match="Invalid date"):
        to_canvas_datetime("next tuesday", DENVER)
    with pytest.raises(ValueError, match="Empty date"):
        to_canvas_datetime("   ", DENVER)


def test_parse_time_of_day():
    assert parse_time_of_day("08:05") == time(8, 5)
    assert parse_time_of_day("23:59") == time(23, 59)
    for bad in ("8:5", "24:00", "12:60", "noon", ""):
        with pytest.raises(ValueError, match="Invalid time"):
            parse_time_of_day(bad)


def test_parse_shift_units():
    assert parse_shift("7d") == timedelta(days=7)
    assert parse_shift("-1d") == timedelta(days=-1)
    assert parse_shift("12h") == timedelta(hours=12)
    assert parse_shift("2w") == timedelta(weeks=2)
    assert parse_shift("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError, match="Invalid shift"):
        parse_shift("nonsense")


def test_shift_iso_is_none_safe():
    assert shift_iso(None, timedelta(days=1)) is None
    assert shift_iso("", timedelta(days=1)) is None
    assert shift_iso("2026-07-01T23:59:00Z", timedelta(days=1)) == "2026-07-02T23:59:00Z"


def test_local_day_renders_in_zone():
    assert local_day("2026-09-16T05:59:00Z", DENVER) == "2026-09-15 23:59"
    assert local_day(None, DENVER) == ""


def test_local_day_round_trips_through_to_canvas_datetime():
    canvas = to_canvas_datetime("2026-09-15", DENVER)
    assert to_canvas_datetime(local_day(canvas, DENVER), DENVER) == canvas


def test_resolve_timezone_prefers_explicit_over_config(write_config):
    write_config('[defaults]\ntimezone = "America/New_York"\n')
    assert resolve_timezone("America/Denver") == DENVER
    assert resolve_timezone() == ZoneInfo("America/New_York")


def test_resolve_timezone_rejects_unknown_zone(write_config):
    write_config("[defaults]\n")
    with pytest.raises(ConfigError, match="Unknown timezone"):
        resolve_timezone("Mars/Olympus_Mons")


def test_resolve_timezone_falls_back_without_config(write_config):
    """No `timezone` key: fall back to the machine's zone, never crash."""
    write_config("[defaults]\n")
    assert resolve_timezone() is not None


def test_clear_sentinel_survives_prefix_keys():
    """`prefix_keys` drops None, so CLEAR must not be None."""
    from canvas_conductor.commands._common import prefix_keys

    assert CLEAR is not None
    assert prefix_keys("wiki_page", {"student_todo_at": CLEAR}) == {
        "wiki_page": {"student_todo_at": ""}
    }
