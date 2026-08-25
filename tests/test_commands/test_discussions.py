"""Discussion command smoke tests, including To-Do dates on ungraded topics."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from canvas_conductor.cli import app


runner = CliRunner()


def _config(write_config):
    write_config(
        """
[defaults]
timezone = "America/Denver"

[courses.t]
id = 99
"""
    )


def test_discussions_list(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics",
        json=[{"id": 1, "title": "Week 1", "published": True}],
    )
    result = runner.invoke(app, ["discussions", "list"])
    assert result.exit_code == 0, result.output
    assert "Week 1" in result.output


def test_discussions_list_shows_todo_in_local_time(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics",
        json=[
            {
                "id": 1,
                "title": "Week 1",
                "published": True,
                "todo_date": "2026-09-16T05:59:00Z",
            }
        ],
    )
    result = runner.invoke(app, ["discussions", "list"])
    assert result.exit_code == 0, result.output
    assert "2026-09-15 23:59" in result.output


def test_discussions_create_with_todo(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics",
        json={"id": 7, "title": "Reading", "todo_date": "2026-09-16T05:59:00Z"},
    )
    result = runner.invoke(
        app,
        [
            "discussions", "create", "--title", "Reading",
            "--todo", "2026-09-15", "--published",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(mock_responses.calls[0].request.body)
    # Topics take `todo_date` directly — no `student_todo_at` asymmetry here.
    assert body["todo_date"] == "2026-09-16T05:59:00Z"


def test_discussions_update_clear_todo(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/discussion_topics/7", json={"id": 7})
    result = runner.invoke(
        app, ["discussions", "update", "--id", "7", "--clear-todo"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(mock_responses.calls[0].request.body)
    assert body == {"todo_date": ""}


def test_discussions_update_rejects_todo_and_clear_together(write_config):
    _config(write_config)
    result = runner.invoke(
        app,
        ["discussions", "update", "--id", "7", "--todo", "2026-09-15", "--clear-todo"],
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_discussions_create_warns_when_unpublished(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app,
        ["discussions", "create", "--title", "R", "--todo", "2026-09-15", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "unpublished" in result.output
