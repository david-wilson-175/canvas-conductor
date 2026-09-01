"""Tests for the `overrides` command group (per-group/section/student
assignment due-date overrides).

Covers CSV parsing/group-name resolution for `bulk`, the create-vs-update
(idempotent) branch, `list`/`create`/`delete` smoke coverage, and the
mutually-exclusive-target validation on `create`.
"""
from __future__ import annotations

import json

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


def _csv(tmp_path, text, name="due_dates.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _mock_assignment_and_groups(mock_responses, api, category_id=700):
    mock_responses.get(
        f"{api}/courses/99/assignments/500",
        json={"id": 500, "group_category_id": category_id},
    )
    mock_responses.get(
        f"{api}/group_categories/{category_id}/groups",
        json=[
            {"id": 1, "name": "Team Alpha"},
            {"id": 2, "name": "Team Beta"},
        ],
    )


def _last_body(mock_responses, key="assignment_override") -> dict:
    body = json.loads(mock_responses.calls[-1].request.body)
    return body[key]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_overrides(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/assignments/500/overrides",
        json=[{"id": 9, "title": "Team Alpha", "group_id": 1, "due_at": "2026-09-16T05:59:00Z"}],
    )
    result = runner.invoke(app, ["overrides", "list", "--id", "500"])
    assert result.exit_code == 0, result.output
    assert "Team Alpha" in result.output


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_override_by_group_name(write_config, mock_responses, api):
    _config(write_config)
    _mock_assignment_and_groups(mock_responses, api)
    mock_responses.post(
        f"{api}/courses/99/assignments/500/overrides",
        json={"id": 9, "group_id": 1, "title": "Team Alpha"},
    )
    result = runner.invoke(
        app,
        [
            "overrides", "create", "--id", "500",
            "--group", "Team Alpha", "--due-at", "2026-09-15", "--tz", "America/Denver",
            "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = _last_body(mock_responses)
    assert payload["group_id"] == 1
    assert payload["title"] == "Team Alpha"
    assert payload["due_at"] == "2026-09-16T05:59:00Z"


def test_create_override_requires_exactly_one_target(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(app, ["overrides", "create", "--id", "500", "-y"])
    assert result.exit_code != 0
    assert "exactly one" in result.output
    assert len(mock_responses.calls) == 0


def test_create_override_rejects_two_targets(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app,
        [
            "overrides", "create", "--id", "500",
            "--group", "Team Alpha", "--section-id", "12", "-y",
        ],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output
    assert len(mock_responses.calls) == 0


def test_create_override_unknown_group_name_errors(write_config, mock_responses, api):
    _config(write_config)
    _mock_assignment_and_groups(mock_responses, api)
    result = runner.invoke(
        app,
        ["overrides", "create", "--id", "500", "--group", "Team Zzz", "--due-at", "2026-09-15", "-y"],
    )
    assert result.exit_code != 0
    assert "No group named" in result.output


# ---------------------------------------------------------------------------
# bulk
# ---------------------------------------------------------------------------

def test_bulk_dry_run_previews_without_writing(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    _mock_assignment_and_groups(mock_responses, api)
    mock_responses.get(f"{api}/courses/99/assignments/500/overrides", json=[])
    csv_path = _csv(
        tmp_path,
        "group,due_at\nTeam Alpha,2026-09-15\nTeam Beta,2026-09-16\n",
    )
    result = runner.invoke(
        app, ["overrides", "bulk", "--id", "500", "-f", csv_path, "--tz", "America/Denver"]
    )
    assert result.exit_code == 0, result.output
    assert "Dry-run" in result.output
    assert "2 override(s): 2 new, 0 updating existing" in result.output
    posts = [c for c in mock_responses.calls if c.request.method in ("POST", "PUT")]
    assert posts == []


def test_bulk_commit_creates_and_updates(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    _mock_assignment_and_groups(mock_responses, api)
    # Team Alpha (id 1) already has an override; Team Beta (id 2) does not.
    mock_responses.get(
        f"{api}/courses/99/assignments/500/overrides",
        json=[{"id": 9, "group_id": 1, "title": "Team Alpha", "due_at": "2026-09-01T05:59:00Z"}],
    )
    mock_responses.put(
        f"{api}/courses/99/assignments/500/overrides/9",
        json={"id": 9, "group_id": 1, "due_at": "2026-09-16T05:59:00Z"},
    )
    mock_responses.post(
        f"{api}/courses/99/assignments/500/overrides",
        json={"id": 10, "group_id": 2, "due_at": "2026-09-17T05:59:00Z"},
    )
    csv_path = _csv(
        tmp_path,
        "group,due_at\nTeam Alpha,2026-09-15\nTeam Beta,2026-09-16\n",
    )
    result = runner.invoke(
        app,
        [
            "overrides", "bulk", "--id", "500", "-f", csv_path,
            "--tz", "America/Denver", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "created: 1" in result.output
    assert "updated: 1" in result.output

    put_call = next(c for c in mock_responses.calls if c.request.method == "PUT")
    post_call = next(c for c in mock_responses.calls if c.request.method == "POST"
                      and c.request.url.endswith("/overrides"))
    put_payload = json.loads(put_call.request.body)["assignment_override"]
    post_payload = json.loads(post_call.request.body)["assignment_override"]
    assert put_payload["group_id"] == 1
    assert put_payload["due_at"] == "2026-09-16T05:59:00Z"
    assert post_payload["group_id"] == 2
    assert post_payload["due_at"] == "2026-09-17T05:59:00Z"


def test_bulk_missing_due_at_column_errors(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    csv_path = _csv(tmp_path, "group\nTeam Alpha\n")
    result = runner.invoke(app, ["overrides", "bulk", "--id", "500", "-f", csv_path])
    assert result.exit_code != 0
    assert "due_at" in result.output


def test_bulk_unresolvable_group_reports_row_and_sends_nothing(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    _mock_assignment_and_groups(mock_responses, api)
    mock_responses.get(f"{api}/courses/99/assignments/500/overrides", json=[])
    csv_path = _csv(tmp_path, "group,due_at\nTeam Zzz,2026-09-15\n")
    result = runner.invoke(app, ["overrides", "bulk", "--id", "500", "-f", csv_path])
    assert result.exit_code != 0
    assert "No group named" in result.output
    writes = [c for c in mock_responses.calls if c.request.method in ("POST", "PUT")]
    assert writes == []


def test_bulk_assignment_without_group_category_errors(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/assignments/500", json={"id": 500, "group_category_id": None})
    csv_path = _csv(tmp_path, "group,due_at\nTeam Alpha,2026-09-15\n")
    result = runner.invoke(app, ["overrides", "bulk", "--id", "500", "-f", csv_path])
    assert result.exit_code != 0
    assert "group_category_id" in result.output


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_override(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.delete(f"{api}/courses/99/assignments/500/overrides/9", json={})
    result = runner.invoke(app, ["overrides", "delete", "--id", "500", "--override-id", "9", "--commit", "-y"])
    assert result.exit_code == 0, result.output
    assert "Deleted override 9" in result.output
