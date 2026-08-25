"""Page command smoke tests, including student To-Do dates."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from canvas_conductor.cli import app


runner = CliRunner()


def _config(write_config):
    """Pin a timezone so bare-date math is deterministic across machines."""
    write_config(
        """
[defaults]
timezone = "America/Denver"

[courses.t]
id = 99
"""
    )


def _page(url, title="P", published=True, todo=None):
    return {
        "page_id": abs(hash(url)) % 10000,
        "url": url,
        "title": title,
        "published": published,
        "front_page": False,
        "todo_date": todo,
    }


def _puts(mock_responses):
    return [c for c in mock_responses.calls if c.request.method == "PUT"]


def _put_bodies(mock_responses):
    return [json.loads(c.request.body) for c in _puts(mock_responses)]


# --------------------------------------------------------------------------
# list / show / create / update basics
# --------------------------------------------------------------------------


def test_pages_list(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[{"page_id": 1, "url": "welcome", "title": "Welcome", "published": True}],
    )
    result = runner.invoke(app, ["pages", "list"])
    assert result.exit_code == 0, result.output
    assert "Welcome" in result.output


def test_pages_list_shows_todo_in_local_time(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("reading-1", "Reading 1", todo="2026-09-16T05:59:00Z")],
    )
    result = runner.invoke(app, ["pages", "list"])
    assert result.exit_code == 0, result.output
    # Stored as 05:59Z, shown as the Sept 15 evening the instructor typed.
    assert "2026-09-15 23:59" in result.output


def test_pages_list_json_keeps_raw_api_fields(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("reading-1", todo="2026-09-16T05:59:00Z")],
    )
    result = runner.invoke(app, ["pages", "list", "-o", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["todo_date"] == "2026-09-16T05:59:00Z"
    assert "todo_local" not in payload[0], "json output must stay faithful to the API"


def test_pages_list_has_todo_filter(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[
            _page("with", "Has One", todo="2026-09-16T05:59:00Z"),
            _page("without", "Has None"),
        ],
    )
    result = runner.invoke(app, ["pages", "list", "--has-todo"])
    assert result.exit_code == 0, result.output
    assert "Has One" in result.output
    assert "Has None" not in result.output


def test_pages_list_no_todo_filter(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[
            _page("with", "Has One", todo="2026-09-16T05:59:00Z"),
            _page("without", "Has None"),
        ],
    )
    result = runner.invoke(app, ["pages", "list", "--no-todo"])
    assert result.exit_code == 0, result.output
    assert "Has None" in result.output
    assert "Has One" not in result.output


def test_pages_create_dry_run(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app, ["pages", "create", "--title", "X", "--body", "<p>y</p>", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "'wiki_page'" in result.output
    assert "'title': 'X'" in result.output


def test_pages_create_with_todo_dry_run(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app,
        ["pages", "create", "--title", "R1", "--todo", "2026-09-15", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "'student_todo_at': '2026-09-16T05:59:00Z'" in result.output


def test_pages_create_with_todo_warns_when_unpublished(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app,
        ["pages", "create", "--title", "R1", "--todo", "2026-09-15", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "unpublished" in result.output


def test_pages_set_front(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/pages/welcome",
        json={"url": "welcome", "title": "Welcome", "front_page": True},
    )
    result = runner.invoke(app, ["pages", "set-front", "--url", "welcome"])
    assert result.exit_code == 0, result.output
    assert "Set page 'welcome' as front page" in result.output


# --------------------------------------------------------------------------
# update --todo / --clear-todo
# --------------------------------------------------------------------------


def test_pages_update_todo_sends_student_todo_at(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/pages/reading-1",
        json=_page("reading-1", todo="2026-09-16T05:59:00Z"),
    )
    result = runner.invoke(
        app, ["pages", "update", "--url", "reading-1", "--todo", "2026-09-15"]
    )
    assert result.exit_code == 0, result.output
    assert _put_bodies(mock_responses)[0] == {
        "wiki_page": {"student_todo_at": "2026-09-16T05:59:00Z"}
    }


def test_pages_update_clear_todo_sends_empty_string(write_config, mock_responses, api):
    """`prefix_keys` drops None, so 'clear' has to travel as an empty string."""
    _config(write_config)
    mock_responses.put(f"{api}/courses/99/pages/reading-1", json=_page("reading-1"))
    result = runner.invoke(
        app, ["pages", "update", "--url", "reading-1", "--clear-todo"]
    )
    assert result.exit_code == 0, result.output
    assert _put_bodies(mock_responses)[0] == {"wiki_page": {"student_todo_at": ""}}


def test_pages_update_rejects_todo_and_clear_together(write_config):
    _config(write_config)
    result = runner.invoke(
        app,
        ["pages", "update", "--url", "r", "--todo", "2026-09-15", "--clear-todo"],
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_pages_update_rejects_unparseable_date(write_config):
    _config(write_config)
    result = runner.invoke(
        app, ["pages", "update", "--url", "r", "--todo", "sometime next week"]
    )
    assert result.exit_code == 2, result.output
    assert "Invalid date" in result.output


def test_pages_update_not_found_exits_5(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/pages/nope",
        json={"errors": [{"message": "The specified resource does not exist."}]},
        status=404,
    )
    result = runner.invoke(
        app, ["pages", "update", "--url", "nope", "--todo", "2026-09-15"]
    )
    assert result.exit_code == 5, result.output


# --------------------------------------------------------------------------
# bulk-todo
# --------------------------------------------------------------------------


def test_bulk_todo_at_sets_same_date_on_all(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r1", "R1"), _page("r2", "R2")],
    )
    for slug in ("r1", "r2"):
        mock_responses.put(f"{api}/courses/99/pages/{slug}", json=_page(slug))

    result = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15", "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    bodies = _put_bodies(mock_responses)
    assert len(bodies) == 2
    assert all(
        b["wiki_page"]["student_todo_at"] == "2026-09-16T05:59:00Z" for b in bodies
    )


def test_bulk_todo_defaults_to_dry_run(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    result = runner.invoke(app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert not _puts(mock_responses), "dry-run must not write"


def test_bulk_todo_cadence_walks_forward(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("w1", "Week 1"), _page("w2", "Week 2"), _page("w3", "Week 3")],
    )
    for slug in ("w1", "w2", "w3"):
        mock_responses.put(f"{api}/courses/99/pages/{slug}", json=_page(slug))

    result = runner.invoke(
        app,
        [
            "pages", "bulk-todo", "--all",
            "--start", "2026-09-01", "--every", "7d", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    dates = [b["wiki_page"]["student_todo_at"] for b in _put_bodies(mock_responses)]
    assert dates == [
        "2026-09-02T05:59:00Z",
        "2026-09-09T05:59:00Z",
        "2026-09-16T05:59:00Z",
    ]


def test_bulk_todo_shift_moves_existing_and_skips_undated(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[
            _page("dated", "Dated", todo="2026-09-16T05:59:00Z"),
            _page("undated", "Undated"),
        ],
    )
    mock_responses.put(f"{api}/courses/99/pages/dated", json=_page("dated"))

    result = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--shift", "7d", "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    bodies = _put_bodies(mock_responses)
    assert len(bodies) == 1, "a page with no to-do date has nothing to shift"
    assert bodies[0]["wiki_page"]["student_todo_at"] == "2026-09-23T05:59:00Z"


def test_bulk_todo_clear_only_touches_dated_pages(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[
            _page("dated", "Dated", todo="2026-09-16T05:59:00Z"),
            _page("undated", "Undated"),
        ],
    )
    mock_responses.put(f"{api}/courses/99/pages/dated", json=_page("dated"))

    result = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--clear", "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    bodies = _put_bodies(mock_responses)
    assert len(bodies) == 1
    assert bodies[0]["wiki_page"]["student_todo_at"] == ""


def test_bulk_todo_skips_pages_already_correct(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r1", "R1", todo="2026-09-16T05:59:00Z")],
    )
    result = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15", "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert "No changes needed" in result.output
    assert not _puts(mock_responses)


def test_bulk_todo_url_selection(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r1", "R1"), _page("r2", "R2"), _page("r3", "R3")],
    )
    mock_responses.put(f"{api}/courses/99/pages/r2", json=_page("r2"))

    result = runner.invoke(
        app,
        ["pages", "bulk-todo", "--url", "r2", "--at", "2026-09-15", "--commit", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 1
    assert _puts(mock_responses)[0].request.url.endswith("/pages/r2")


def test_bulk_todo_unknown_slug_exits_2(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    result = runner.invoke(
        app, ["pages", "bulk-todo", "--url", "typo", "--at", "2026-09-15"]
    )
    assert result.exit_code == 2, result.output
    assert "No page with slug(s): typo" in result.output


def test_bulk_todo_module_selection(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json=[
            {
                "id": 1,
                "name": "Week 3: Networking",
                "items": [
                    {"type": "Page", "page_url": "r2"},
                    {"type": "Assignment", "content_id": 5},
                ],
            },
            {"id": 2, "name": "Week 4", "items": [{"type": "Page", "page_url": "r3"}]},
        ],
    )
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r1", "R1"), _page("r2", "R2"), _page("r3", "R3")],
    )
    mock_responses.put(f"{api}/courses/99/pages/r2", json=_page("r2"))

    result = runner.invoke(
        app,
        [
            "pages", "bulk-todo", "--module", "Week 3",
            "--at", "2026-09-15", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 1
    assert _puts(mock_responses)[0].request.url.endswith("/pages/r2")


def test_bulk_todo_unknown_module_exits_2(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules", json=[{"id": 1, "name": "Week 1", "items": []}]
    )
    result = runner.invoke(
        app, ["pages", "bulk-todo", "--module", "Week 9", "--at", "2026-09-15"]
    )
    assert result.exit_code == 2, result.output
    assert "No module matches" in result.output


def test_bulk_todo_from_csv_round_trip(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    # Exactly the shape `pages list -o csv` writes.
    schedule = tmp_path / "schedule.csv"
    schedule.write_text(
        "URL,Title,Published,To-Do,Front Page,Updated\n"
        "r1,R1,Yes,2026-09-15,No,\n"
        "r2,R2,Yes,2026-09-22,No,\n"
    )
    mock_responses.get(
        f"{api}/courses/99/pages", json=[_page("r1", "R1"), _page("r2", "R2")]
    )
    for slug in ("r1", "r2"):
        mock_responses.put(f"{api}/courses/99/pages/{slug}", json=_page(slug))

    result = runner.invoke(
        app, ["pages", "bulk-todo", "--file", str(schedule), "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    dates = [b["wiki_page"]["student_todo_at"] for b in _put_bodies(mock_responses)]
    assert dates == ["2026-09-16T05:59:00Z", "2026-09-23T05:59:00Z"]


def test_bulk_todo_csv_blank_cells_are_skipped_by_default(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    schedule = tmp_path / "schedule.csv"
    schedule.write_text("URL,To-Do\nr1,2026-09-15\nr2,\n")
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r1", "R1"), _page("r2", "R2", todo="2026-10-01T05:59:00Z")],
    )
    mock_responses.put(f"{api}/courses/99/pages/r1", json=_page("r1"))

    result = runner.invoke(
        app, ["pages", "bulk-todo", "--file", str(schedule), "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert len(_puts(mock_responses)) == 1, "a blank cell must not wipe an existing date"


def test_bulk_todo_csv_clear_blanks_opt_in(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    schedule = tmp_path / "schedule.csv"
    schedule.write_text("URL,To-Do\nr2,\n")
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r2", "R2", todo="2026-10-01T05:59:00Z")],
    )
    mock_responses.put(f"{api}/courses/99/pages/r2", json=_page("r2"))

    result = runner.invoke(
        app,
        [
            "pages", "bulk-todo", "--file", str(schedule),
            "--clear-blanks", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _put_bodies(mock_responses)[0]["wiki_page"]["student_todo_at"] == ""


def test_bulk_todo_from_json_file(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps([{"url": "r1", "todo_date": "2026-09-16T05:59:00Z"}])
    )
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    mock_responses.put(f"{api}/courses/99/pages/r1", json=_page("r1"))

    result = runner.invoke(
        app, ["pages", "bulk-todo", "--file", str(schedule), "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    body = _put_bodies(mock_responses)[0]
    assert body["wiki_page"]["student_todo_at"] == "2026-09-16T05:59:00Z"


def test_bulk_todo_csv_without_url_column_exits_2(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    schedule = tmp_path / "bad.csv"
    schedule.write_text("Title,To-Do\nR1,2026-09-15\n")
    result = runner.invoke(app, ["pages", "bulk-todo", "--file", str(schedule)])
    assert result.exit_code == 2, result.output
    assert "no URL column" in result.output


def test_bulk_todo_warns_about_unpublished_pages(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages",
        json=[_page("r1", "R1", published=False)],
    )
    result = runner.invoke(app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15"])
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "unpublished" in result.output


def test_bulk_todo_publish_flag_publishes_alongside(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/pages", json=[_page("r1", "R1", published=False)]
    )
    mock_responses.put(f"{api}/courses/99/pages/r1", json=_page("r1"))

    result = runner.invoke(
        app,
        [
            "pages", "bulk-todo", "--all", "--at", "2026-09-15",
            "--publish", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _put_bodies(mock_responses)[0]["wiki_page"] == {
        "student_todo_at": "2026-09-16T05:59:00Z",
        "published": True,
    }
    assert "WARNING" not in result.output


def test_bulk_todo_requires_exactly_one_selector(write_config):
    _config(write_config)
    neither = runner.invoke(app, ["pages", "bulk-todo", "--at", "2026-09-15"])
    assert neither.exit_code != 0
    assert "exactly one of" in neither.output

    both = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--url", "r1", "--at", "2026-09-15"]
    )
    assert both.exit_code != 0


def test_bulk_todo_requires_exactly_one_action(write_config):
    _config(write_config)
    none = runner.invoke(app, ["pages", "bulk-todo", "--all"])
    assert none.exit_code != 0
    assert "exactly one action" in none.output

    two = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15", "--clear"]
    )
    assert two.exit_code != 0


def test_bulk_todo_start_and_every_must_pair(write_config):
    _config(write_config)
    result = runner.invoke(app, ["pages", "bulk-todo", "--all", "--start", "2026-09-01"])
    assert result.exit_code != 0
    assert "must be used together" in result.output


def test_bulk_todo_reports_resolved_timezone(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    result = runner.invoke(app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15"])
    assert result.exit_code == 0, result.output
    assert "America/Denver" in result.output


def test_bulk_todo_tz_flag_overrides_config(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    mock_responses.put(f"{api}/courses/99/pages/r1", json=_page("r1"))
    result = runner.invoke(
        app,
        [
            "pages", "bulk-todo", "--all", "--at", "2026-09-15",
            "--tz", "UTC", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    body = _put_bodies(mock_responses)[0]
    assert body["wiki_page"]["student_todo_at"] == "2026-09-15T23:59:00Z"


def test_bulk_todo_at_time_override(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    mock_responses.put(f"{api}/courses/99/pages/r1", json=_page("r1"))
    result = runner.invoke(
        app,
        [
            "pages", "bulk-todo", "--all", "--at", "2026-09-15",
            "--at-time", "08:00", "--commit", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    body = _put_bodies(mock_responses)[0]
    assert body["wiki_page"]["student_todo_at"] == "2026-09-15T14:00:00Z"


def test_bulk_todo_permission_error_exits_4(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/pages", json=[_page("r1", "R1")])
    mock_responses.put(
        f"{api}/courses/99/pages/r1",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = runner.invoke(
        app, ["pages", "bulk-todo", "--all", "--at", "2026-09-15", "--commit", "-y"]
    )
    assert result.exit_code == 4, result.output
