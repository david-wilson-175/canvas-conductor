"""Smoke tests for the `requirements` extension (module item completion
requirements: must-view, must-submit, must-mark-done, min-score/percentage).
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


def _item(item_id, title, item_type="Page", published=True, req=None):
    return {
        "id": item_id,
        "title": title,
        "type": item_type,
        "published": published,
        "completion_requirement": req,
    }


def _module(mod_id, name, items):
    return {"id": mod_id, "name": name, "items": items}


def _puts(mock_responses):
    return [c for c in mock_responses.calls if c.request.method == "PUT"]


def _put_bodies(mock_responses):
    return [json.loads(c.request.body) for c in _puts(mock_responses)]


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def test_requirements_list_shows_current_requirement(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[
            _module(
                1,
                "Module 05: Reading",
                [_item(10, "Reading 5", req={"type": "must_view"})],
            )
        ],
    )
    result = runner.invoke(app, ["requirements", "list"])
    assert result.exit_code == 0, result.output
    assert "Reading 5" in result.output
    assert "must_view" in result.output


def test_requirements_list_missing_only_filters(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[
            _module(
                1,
                "Module 07",
                [
                    _item(10, "Has one", req={"type": "must_view"}),
                    _item(11, "No req yet", req=None),
                ],
            )
        ],
    )
    result = runner.invoke(app, ["requirements", "list", "--missing-only"])
    assert result.exit_code == 0, result.output
    assert "No req yet" in result.output
    assert "Has one" not in result.output


# --------------------------------------------------------------------------
# set (single item)
# --------------------------------------------------------------------------


def test_requirements_set_dry_run_makes_no_request(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app,
        ["requirements", "set", "--module-id", "1", "--item-id", "10", "--type", "must_mark_done"],
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 0


def test_requirements_set_commit_sends_completion_requirement(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/modules/1/items/10",
        json=_item(10, "Exam 1", req={"type": "must_mark_done"}),
    )
    result = runner.invoke(
        app,
        [
            "requirements",
            "set",
            "--module-id",
            "1",
            "--item-id",
            "10",
            "--type",
            "must_mark_done",
            "--commit",
            "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    body = _put_bodies(mock_responses)[0]
    assert body == {"module_item": {"completion_requirement": {"type": "must_mark_done"}}}


def test_requirements_set_min_score_requires_value(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app,
        [
            "requirements",
            "set",
            "--module-id",
            "1",
            "--item-id",
            "10",
            "--type",
            "min_score",
            "--commit",
            "-y",
        ],
    )
    assert result.exit_code != 0
    assert len(_puts(mock_responses)) == 0


def test_requirements_set_rejects_type_and_clear_together(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app,
        [
            "requirements",
            "set",
            "--module-id",
            "1",
            "--item-id",
            "10",
            "--type",
            "must_view",
            "--clear",
        ],
    )
    assert result.exit_code != 0


# --------------------------------------------------------------------------
# bulk-set
# --------------------------------------------------------------------------


def test_bulk_set_by_module_commits_to_every_matching_item(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[
            _module(
                1,
                "Module 03: Reading",
                [_item(10, "Reading 3", "Page")],
            ),
            _module(2, "Module 04: Reading", [_item(11, "Reading 4", "Page")]),
            _module(3, "Module 99: Misc", [_item(12, "Unrelated", "Page")]),
        ],
    )
    mock_responses.put(f"{api}/courses/99/modules/1/items/10", json=_item(10, "Reading 3"))
    mock_responses.put(f"{api}/courses/99/modules/2/items/11", json=_item(11, "Reading 4"))

    result = runner.invoke(
        app,
        [
            "requirements",
            "bulk-set",
            "--module",
            "Reading",
            "--type",
            "must_mark_done",
            "--commit",
            "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 2
    for body in _put_bodies(mock_responses):
        assert body == {"module_item": {"completion_requirement": {"type": "must_mark_done"}}}


def test_bulk_set_skips_subheaders(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[
            _module(
                1,
                "Module 06",
                [
                    _item(10, "Section header", "SubHeader"),
                    _item(11, "Assignment 1", "Assignment"),
                ],
            )
        ],
    )
    mock_responses.put(f"{api}/courses/99/modules/1/items/11", json=_item(11, "Assignment 1"))

    result = runner.invoke(
        app,
        ["requirements", "bulk-set", "--module", "Module 06", "--type", "must_submit", "--commit", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 1
    assert "SubHeader" not in "".join(str(b) for b in _put_bodies(mock_responses))


def test_bulk_set_dry_run_makes_no_requests(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[_module(1, "Module 01", [_item(10, "Assignment 1", "Assignment")])],
    )
    result = runner.invoke(
        app, ["requirements", "bulk-set", "--module", "Module 01", "--type", "must_submit"]
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 0


def test_bulk_set_requires_exactly_one_action(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[_module(1, "Module 01", [_item(10, "Assignment 1", "Assignment")])],
    )
    result = runner.invoke(app, ["requirements", "bulk-set", "--module", "Module 01"])
    assert result.exit_code != 0


def test_bulk_set_from_file(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    csv_path = tmp_path / "reqs.csv"
    csv_path.write_text("module_id,item_id,type\n1,10,must_mark_done\n1,11,\n")
    mock_responses.put(f"{api}/courses/99/modules/1/items/10", json=_item(10, "Reading 1"))

    result = runner.invoke(
        app,
        ["requirements", "bulk-set", "--file", str(csv_path), "--commit", "-y"],
    )
    assert result.exit_code == 0, result.output
    # Row with a blank Type column is skipped (only one PUT, for item 10).
    assert len(_puts(mock_responses)) == 1
