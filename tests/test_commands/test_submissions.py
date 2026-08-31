"""Submission command tests, focused on the group-grading guard on bulk-grade."""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from canvas_conductor.cli import app


runner = CliRunner()


def _config(write_config):
    write_config(
        """
[courses.t]
id = 99
"""
    )


def _grades_csv(tmp_path: Path) -> str:
    """Two members of the same group on different grades — the case that breaks."""
    path = tmp_path / "grades.csv"
    path.write_text("user_id,grade\n101,90\n102,88\n")
    return str(path)


def _assignment(**overrides) -> dict:
    base = {
        "id": 7,
        "name": "Team Project",
        "group_category_id": 5,
        "grade_group_students_individually": True,
    }
    base.update(overrides)
    return base


def test_bulk_grade_blocks_pooled_group_assignment(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments/7",
        json=_assignment(grade_group_students_individually=False),
    )
    result = runner.invoke(
        app,
        ["submissions", "bulk-grade", "--assignment", "7", "--file", _grades_csv(tmp_path)],
    )
    assert result.exit_code == 2, result.output
    assert "Team Project" in result.output
    assert "--individual-grading" in result.output


def test_bulk_grade_blocks_before_committing(write_config, mock_responses, api, tmp_path):
    """The guard must fire on --commit too, not just dry-run."""
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments/7",
        json=_assignment(grade_group_students_individually=False),
    )
    result = runner.invoke(
        app,
        [
            "submissions", "bulk-grade", "--assignment", "7",
            "--file", _grades_csv(tmp_path), "--commit", "-y",
        ],
    )
    assert result.exit_code == 2, result.output
    # Nothing was posted — the only call was the pre-flight GET.
    assert all(c.request.method == "GET" for c in mock_responses.calls)


def test_bulk_grade_allows_individually_graded_group(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/assignments/7", json=_assignment())
    result = runner.invoke(
        app,
        ["submissions", "bulk-grade", "--assignment", "7", "--file", _grades_csv(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output


def test_bulk_grade_allows_non_group_assignment(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments/7",
        json=_assignment(group_category_id=None, grade_group_students_individually=False),
    )
    result = runner.invoke(
        app,
        ["submissions", "bulk-grade", "--assignment", "7", "--file", _grades_csv(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output


def test_bulk_grade_override_flag_bypasses_guard(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments/7",
        json=_assignment(grade_group_students_individually=False),
    )
    result = runner.invoke(
        app,
        [
            "submissions", "bulk-grade", "--assignment", "7",
            "--file", _grades_csv(tmp_path), "--allow-group-propagation",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output


def test_bulk_grade_commits_when_individually_graded(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/assignments/7", json=_assignment())
    mock_responses.post(
        f"{api}/courses/99/assignments/7/submissions/update_grades",
        json={"id": 42, "url": "https://test.instructure.com/api/v1/progress/42"},
    )
    result = runner.invoke(
        app,
        [
            "submissions", "bulk-grade", "--assignment", "7",
            "--file", _grades_csv(tmp_path), "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "progress/42" in result.output
    posted = [c for c in mock_responses.calls if c.request.method == "POST"]
    assert len(posted) == 1


def test_bulk_grade_missing_file(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app, ["submissions", "bulk-grade", "--assignment", "7", "--file", "nope.csv"]
    )
    assert result.exit_code == 2
    assert "file not found" in result.output


def test_bulk_grade_404_is_formatted(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments/404", json={"errors": []}, status=404
    )
    result = runner.invoke(
        app,
        ["submissions", "bulk-grade", "--assignment", "404", "--file", _grades_csv(tmp_path)],
    )
    assert result.exit_code == 5
