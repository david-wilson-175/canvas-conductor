"""Assignment command smoke tests, including bulk-dates date math."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from canvas_conductor.cli import app
from canvas_conductor.commands.assignments import _parse_shift, _shift_iso


runner = CliRunner()


def _config(write_config):
    write_config(
        """
[courses.t]
id = 99
"""
    )


def test_parse_shift_units():
    assert _parse_shift("7d") == timedelta(days=7)
    assert _parse_shift("-1d") == timedelta(days=-1)
    assert _parse_shift("12h") == timedelta(hours=12)
    assert _parse_shift("2w") == timedelta(weeks=2)
    assert _parse_shift("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        _parse_shift("nonsense")


def test_shift_iso_round_trips():
    out = _shift_iso("2026-07-01T23:59:00Z", timedelta(days=1))
    assert out == "2026-07-02T23:59:00Z"


def test_assignments_list(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments",
        json=[
            {"id": 1, "name": "HW1", "points_possible": 10, "due_at": None, "published": True}
        ],
    )
    result = runner.invoke(app, ["assignments", "list"])
    assert result.exit_code == 0, result.output
    assert "HW1" in result.output


def _update_payload(mock_responses) -> dict:
    """Return the assignment body of the last PUT the CLI made."""
    body = json.loads(mock_responses.calls[-1].request.body)
    return body["assignment"]


def test_update_individual_grading_on(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/assignments/7", json={"id": 7, "name": "Proj"})
    result = runner.invoke(
        app, ["assignments", "update", "--id", "7", "--individual-grading"]
    )
    assert result.exit_code == 0, result.output
    assert _update_payload(mock_responses)["grade_group_students_individually"] is True


def test_update_group_grading_sends_false(write_config, mock_responses, api):
    """False must survive prefix_keys, which drops None but not False."""
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/assignments/7", json={"id": 7, "name": "Proj"})
    result = runner.invoke(
        app, ["assignments", "update", "--id", "7", "--group-grading"]
    )
    assert result.exit_code == 0, result.output
    assert _update_payload(mock_responses)["grade_group_students_individually"] is False


def test_update_without_flag_leaves_setting_alone(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/assignments/7", json={"id": 7, "name": "Proj"})
    result = runner.invoke(
        app, ["assignments", "update", "--id", "7", "--name", "Renamed"]
    )
    assert result.exit_code == 0, result.output
    assert "grade_group_students_individually" not in _update_payload(mock_responses)


def test_update_404_is_formatted(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/assignments/404", json={"errors": []}, status=404)
    result = runner.invoke(
        app, ["assignments", "update", "--id", "404", "--individual-grading"]
    )
    assert result.exit_code == 5
    assert "not found" in result.output.lower() + result.stderr.lower()


def test_bulk_dates_dry_run(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments",
        json=[
            {"id": 1, "name": "HW1", "due_at": "2026-07-01T23:59:00Z"},
            {"id": 2, "name": "HW2", "due_at": None},  # skipped — no dates
        ],
    )
    result = runner.invoke(
        app, ["assignments", "bulk-dates", "--shift", "7d"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "HW1" in result.output
    assert "2026-07-08T23:59:00Z" in result.output


# --- group_category_id (Laura's addition on top of Dave's individual-grading toggle) ---


def _create_payload(mock_responses) -> dict:
    """Return the assignment body of the last POST the CLI made."""
    body = json.loads(mock_responses.calls[-1].request.body)
    return body["assignment"]


def test_update_sets_group_category_id(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/assignments/7", json={"id": 7, "name": "Proj"})
    result = runner.invoke(
        app,
        ["assignments", "update", "--id", "7", "--group-category-id", "20410"],
    )
    assert result.exit_code == 0, result.output
    assert _update_payload(mock_responses)["group_category_id"] == 20410


def test_update_group_category_id_and_individual_grading_together(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/assignments/7", json={"id": 7, "name": "Proj"})
    result = runner.invoke(
        app,
        [
            "assignments", "update", "--id", "7",
            "--group-category-id", "20410", "--individual-grading",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = _update_payload(mock_responses)
    assert payload["group_category_id"] == 20410
    assert payload["grade_group_students_individually"] is True


def test_create_with_group_category_id(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/assignments", json={"id": 9, "name": "Sprint"})
    result = runner.invoke(
        app,
        [
            "assignments", "create", "--name", "Sprint",
            "--group-category-id", "20410", "--individual-grading",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = _create_payload(mock_responses)
    assert payload["group_category_id"] == 20410
    assert payload["grade_group_students_individually"] is True


def test_create_without_individual_grading_omits_the_field(write_config, mock_responses, api):
    """No grading flag means "don't care" — let Canvas apply its own default
    rather than asserting one. Same tri-state contract as update."""
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/assignments", json={"id": 9, "name": "Sprint"})
    result = runner.invoke(
        app, ["assignments", "create", "--name", "Sprint", "--group-category-id", "20410"]
    )
    assert result.exit_code == 0, result.output
    payload = _create_payload(mock_responses)
    assert payload["group_category_id"] == 20410
    assert "grade_group_students_individually" not in payload


def test_create_group_grading_sends_false(write_config, mock_responses, api):
    """--group-grading is an explicit choice, not silence, so it has to reach
    Canvas as False. Canvas happens to default this field to false on create,
    which is what let the flag sit inert here without any test noticing.
    """
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/assignments", json={"id": 9, "name": "Sprint"})
    result = runner.invoke(
        app,
        [
            "assignments", "create", "--name", "Sprint",
            "--group-category-id", "20410", "--group-grading",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = _create_payload(mock_responses)
    assert payload["group_category_id"] == 20410
    assert payload["grade_group_students_individually"] is False
