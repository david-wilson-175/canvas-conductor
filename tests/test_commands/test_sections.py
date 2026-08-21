"""Smoke tests for the `sections` command group via the Typer CLI runner."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from canvas_conductor.cli import app


runner = CliRunner()


def _config(write_config):
    write_config(
        """
[courses.combined]
id = 99
name = "Combined"

[courses.other]
id = 77
name = "Other"
"""
    )


def test_sections_list_table(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/sections",
        json=[
            {"id": 1, "name": "Sec 1", "course_id": 99, "total_students": 30},
        ],
    )
    result = runner.invoke(app, ["sections", "list", "-c", "combined"])
    assert result.exit_code == 0, result.output
    assert "Sec 1" in result.output


def test_sections_list_json(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/sections", json=[{"id": 5, "name": "S5", "course_id": 99}]
    )
    result = runner.invoke(app, ["sections", "list", "-c", "combined", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["id"] == 5


def test_crosslist_posts_for_each_section(write_config, mock_responses, api):
    _config(write_config)
    for sid in (11, 12):
        mock_responses.get(
            f"{api}/sections/{sid}",
            json={"id": sid, "name": f"S{sid}", "course_id": 77, "total_students": 10},
        )
        mock_responses.get(f"{api}/sections/{sid}/students/submissions", json=[])
        mock_responses.post(f"{api}/sections/{sid}/crosslist/99", json={"id": sid})

    result = runner.invoke(
        app, ["sections", "crosslist", "--ids", "11,12", "-c", "combined", "-y"]
    )
    assert result.exit_code == 0, result.output
    posts = [c for c in mock_responses.calls if c.request.method == "POST"]
    assert len(posts) == 2
    assert posts[0].request.url.endswith("/sections/11/crosslist/99")


def test_crosslist_from_course_absorbs_all_sections(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/77/sections",
        json=[{"id": 21, "name": "S21", "course_id": 77, "total_students": 4}],
    )
    mock_responses.get(f"{api}/sections/21/students/submissions", json=[])
    mock_responses.post(f"{api}/sections/21/crosslist/99", json={"id": 21})

    result = runner.invoke(
        app, ["sections", "crosslist", "--from", "other", "-c", "combined", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert "Cross-listed 1 section(s)" in result.output


def test_crosslist_dry_run_makes_no_post(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/sections/31",
        json={"id": 31, "name": "S31", "course_id": 77, "total_students": 12},
    )
    mock_responses.get(f"{api}/sections/31/students/submissions", json=[])

    result = runner.invoke(
        app, ["sections", "crosslist", "--ids", "31", "-c", "combined", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert not [c for c in mock_responses.calls if c.request.method == "POST"]


def test_crosslist_warns_on_graded_submissions(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/sections/41",
        json={"id": 41, "name": "S41", "course_id": 77, "total_students": 12},
    )
    mock_responses.get(
        f"{api}/sections/41/students/submissions",
        json=[{"id": 1, "score": 10}, {"id": 2, "score": 8}],
    )

    result = runner.invoke(
        app, ["sections", "crosslist", "--ids", "41", "-c", "combined", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output


def test_crosslist_skips_sections_already_in_destination(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.get(
        f"{api}/sections/51",
        json={"id": 51, "name": "S51", "course_id": 99, "total_students": 12},
    )
    result = runner.invoke(
        app, ["sections", "crosslist", "--ids", "51", "-c", "combined", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert "Nothing to cross-list." in result.output
    assert not [c for c in mock_responses.calls if c.request.method == "POST"]


def test_crosslist_requires_exactly_one_source(write_config):
    _config(write_config)
    both = runner.invoke(
        app,
        ["sections", "crosslist", "--ids", "1", "--from", "other", "-c", "combined"],
    )
    assert both.exit_code != 0
    neither = runner.invoke(app, ["sections", "crosslist", "-c", "combined"])
    assert neither.exit_code != 0


def test_crosslist_permission_error_exits_4(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/sections/61",
        json={"id": 61, "name": "S61", "course_id": 77, "total_students": 3},
    )
    mock_responses.get(f"{api}/sections/61/students/submissions", json=[])
    mock_responses.post(
        f"{api}/sections/61/crosslist/99",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = runner.invoke(
        app, ["sections", "crosslist", "--ids", "61", "-c", "combined", "-y"]
    )
    assert result.exit_code == 4, result.output


def test_uncrosslist_deletes(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.delete(f"{api}/sections/71/crosslist", json={"id": 71})
    result = runner.invoke(app, ["sections", "uncrosslist", "--id", "71", "-y"])
    assert result.exit_code == 0, result.output
    assert "De-cross-listed section 71." in result.output
