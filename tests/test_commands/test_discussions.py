"""Discussion command smoke tests, including To-Do dates on ungraded topics."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from canvas_conductor import client as client_module
from canvas_conductor.cli import app
from canvas_conductor.commands import discussions


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


# =======================================================================
# discussions entries
# =======================================================================

# A thread shaped like the ones Canvas actually returns: nesting past two
# levels, a deleted tombstone with no author or message, and the
# institution's injected <link>/<script> wrapper around every body.
WRAP = (
    '<link rel="stylesheet" href="https://example.com/dp_app.css">'
    "{body}"
    '<script src="https://example.com/dp_app.js"></script>'
)


def _view_payload(**overrides):
    payload = {
        "unread_entries": [301],
        "forced_entries": [],
        "entry_ratings": {},
        "participants": [
            {"id": 11, "display_name": "Ada Lovelace"},
            {"id": 22, "display_name": "Grace Hopper"},
        ],
        "view": [
            {
                "id": 100,
                "user_id": 11,
                "parent_id": None,
                "created_at": "2026-09-01T10:00:00Z",
                "updated_at": "2026-09-01T10:00:00Z",
                "message": WRAP.format(body="<p>One two three four five.</p>"),
                "rating_count": None,
                "rating_sum": None,
                "replies": [
                    {
                        "id": 200,
                        "user_id": 22,
                        "parent_id": 100,
                        "created_at": "2026-09-02T10:00:00Z",
                        "updated_at": "2026-09-02T10:00:00Z",
                        "message": WRAP.format(body="<p>Second level.</p>"),
                        "replies": [
                            {
                                "id": 300,
                                "user_id": 11,
                                "parent_id": 200,
                                "created_at": "2026-09-03T10:00:00Z",
                                "updated_at": "2026-09-03T10:00:00Z",
                                "message": WRAP.format(body="<p>Third level here.</p>"),
                                "replies": [
                                    {
                                        "id": 400,
                                        "parent_id": 300,
                                        "created_at": "2026-09-03T11:00:00Z",
                                        "updated_at": "2026-09-03T11:00:00Z",
                                        "editor_id": 11,
                                        "deleted": True,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "id": 301,
                "user_id": 22,
                "parent_id": None,
                "created_at": "2026-09-04T10:00:00Z",
                "updated_at": "2026-09-04T10:00:00Z",
                "message": WRAP.format(body="<p>A second top-level post.</p>"),
                "replies": [],
            },
        ],
        "new_entries": [],
    }
    payload.update(overrides)
    return payload


ENROLLMENTS = [
    {"id": 1, "user_id": 11, "type": "StudentEnrollment"},
    {"id": 2, "user_id": 22, "type": "TeacherEnrollment"},
    {"id": 3, "user_id": 22, "type": "TaEnrollment"},
]


def _mock_thread(mock_responses, api, view=None, enrollments=None):
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/view", json=view or _view_payload()
    )
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=ENROLLMENTS if enrollments is None else enrollments,
    )


def _entries(result):
    return json.loads(result.output)


def _run(args):
    return runner.invoke(app, ["discussions", "entries", *args])


def test_entries_list_flattens_past_two_levels(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    result = _run(["list", "--topic", "5", "-o", "json"])
    assert result.exit_code == 0, result.output

    records = _entries(result)
    # Thread order, depth-first and oldest-first, including the 4th level.
    assert [(r["id"], r["depth"]) for r in records] == [
        (100, 0), (200, 1), (300, 2), (400, 3), (301, 0),
    ]
    assert [r["parent_id"] for r in records] == [None, 100, 200, 300, None]


def test_entries_list_resolves_names_and_roles(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))
    by_id = {r["id"]: r for r in records}

    # Entries carry only user_id; the name comes from `participants` and
    # the role from a second join against the course's enrollments.
    assert by_id[100]["display_name"] == "Ada Lovelace"
    assert by_id[100]["role"] == "StudentEnrollment"
    # Highest privilege wins when someone holds two enrollments.
    assert by_id[200]["display_name"] == "Grace Hopper"
    assert by_id[200]["role"] == "TeacherEnrollment"
    assert by_id[200]["roles"] == ["TeacherEnrollment", "TaEnrollment"]


def test_entries_list_json_carries_the_documented_schema(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))

    expected = {
        "id", "parent_id", "topic_id", "user_id", "display_name", "role",
        "roles", "created_at", "updated_at", "message_html", "message_text",
        "word_count", "depth", "deleted", "unread", "rating_count",
        "rating_sum", "editor_id", "attachments",
    }
    assert all(set(r) == expected for r in records)

    first = records[0]
    assert first["topic_id"] == 5
    assert first["message_text"] == "One two three four five."
    assert first["word_count"] == 5
    # The raw HTML is preserved exactly, injected wrapper and all.
    assert "dp_app.css" in first["message_html"]
    assert "dp_app" not in first["message_text"]


def test_entries_list_flags_deleted_without_crashing(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))
    tombstone = next(r for r in records if r["id"] == 400)

    # Canvas drops the author and the body from a deleted entry, but keeps
    # its place in the tree.
    assert tombstone["deleted"] is True
    assert tombstone["user_id"] is None
    assert tombstone["display_name"] is None
    assert tombstone["message_html"] is None
    assert tombstone["message_text"] == ""
    assert tombstone["word_count"] == 0
    assert tombstone["depth"] == 3


def test_entries_list_merges_new_entries_the_view_has_not_absorbed(
    write_config, mock_responses, api
):
    _config(write_config)
    # Canvas's threaded view is a cache that lags a write by a second or
    # two; entries it hasn't absorbed yet arrive in `new_entries`.
    view = _view_payload(
        new_entries=[
            {
                "id": 500,
                "user_id": 11,
                "parent_id": 301,
                "created_at": "2026-09-05T10:00:00Z",
                "updated_at": "2026-09-05T10:00:00Z",
                "message": "<p>Just posted.</p>",
            }
        ]
    )
    _mock_thread(mock_responses, api, view=view)
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))
    fresh = next(r for r in records if r["id"] == 500)
    assert (fresh["parent_id"], fresh["depth"]) == (301, 1)


def test_entries_list_keeps_orphans_when_a_parent_is_missing(
    write_config, mock_responses, api
):
    _config(write_config)
    view = _view_payload(
        view=[],
        new_entries=[
            {
                "id": 600,
                "user_id": 11,
                "parent_id": 999999,
                "created_at": "2026-09-05T10:00:00Z",
                "message": "<p>Orphan.</p>",
            }
        ],
    )
    _mock_thread(mock_responses, api, view=view)
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))
    # A partially-materialized view must never make a real post vanish.
    assert [r["id"] for r in records] == [600]
    assert records[0]["depth"] == 0


def test_entries_list_reading_never_marks_anything_read(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_thread(mock_responses, api)
    result = _run(["list", "--topic", "5", "-o", "json"])
    assert result.exit_code == 0, result.output
    # No PUT to …/read or …/read_all: unread badges the user relies on in
    # the web UI and on mobile must survive a read.
    assert [c.request.method for c in mock_responses.calls] == ["GET", "GET"]


def test_entries_list_unread_filter(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(_run(["list", "--topic", "5", "--unread", "-o", "json"]))
    assert [r["id"] for r in records] == [301]
    assert records[0]["unread"] is True


def test_entries_list_filters_compose(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    # since + role + replies-only, all at once.
    records = _entries(
        _run([
            "list", "--topic", "5", "-o", "json",
            "--since", "2026-09-02", "--role", "teacher", "--replies-only",
        ])
    )
    assert [r["id"] for r in records] == [200]


def test_entries_list_since_and_until_bracket_the_thread(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(
        _run(["list", "--topic", "5", "-o", "json", "--until", "2026-09-01"])
    )
    # A bare --until date covers that whole local day.
    assert [r["id"] for r in records] == [100]


def test_entries_list_user_filter_by_id_and_by_name(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    by_id = _entries(_run(["list", "--topic", "5", "-o", "json", "--user", "22"]))
    assert [r["id"] for r in by_id] == [200, 301]

    _mock_thread(mock_responses, api)
    by_name = _entries(_run(["list", "--topic", "5", "-o", "json", "--user", "hopper"]))
    assert [r["id"] for r in by_name] == [200, 301]


def test_entries_list_structural_and_length_filters(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    tops = _entries(_run(["list", "--topic", "5", "-o", "json", "--top-level-only"]))
    assert [r["id"] for r in tops] == [100, 301]

    _mock_thread(mock_responses, api)
    wordy = _entries(
        _run(["list", "--topic", "5", "-o", "json", "--min-words", "4", "--max-words", "5"])
    )
    assert [r["id"] for r in wordy] == [100, 301]

    _mock_thread(mock_responses, api)
    alive = _entries(_run(["list", "--topic", "5", "-o", "json", "--no-deleted"]))
    assert 400 not in [r["id"] for r in alive]


def test_entries_list_no_roles_skips_the_enrollment_call(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/discussion_topics/5/view", json=_view_payload())
    records = _entries(_run(["list", "--topic", "5", "-o", "json", "--no-roles"]))
    # One call only: the join is what costs the extra request.
    assert len(mock_responses.calls) == 1
    # null rather than [] so a caller can tell "not queried" from "none".
    assert all(r["role"] is None and r["roles"] is None for r in records)


def test_entries_list_role_filter_needs_the_join(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["list", "--topic", "5", "--role", "teacher", "--no-roles"])
    assert result.exit_code != 0
    assert "drop --no-roles" in result.output


def test_entries_list_rejects_an_unknown_role(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["list", "--topic", "5", "--role", "grader"])
    assert result.exit_code != 0
    assert "Unknown --role" in result.output


def test_entries_list_tree_shows_nesting_and_bodies(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    result = _run(["list", "--topic", "5", "-o", "tree"])
    assert result.exit_code == 0, result.output
    lines = {line.strip(): line for line in result.output.splitlines() if line.strip()}
    third = next(v for k, v in lines.items() if k.startswith("[300]"))
    top = next(v for k, v in lines.items() if k.startswith("[100]"))
    assert len(third) - len(third.lstrip()) > len(top) - len(top.lstrip())
    assert "Third level here." in result.output
    assert "DELETED" in result.output


def test_entries_list_table_omits_message_bodies(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    result = _run(["list", "--topic", "5"])
    assert result.exit_code == 0, result.output
    # Post bodies are student work: a summary listing is not a request for
    # them. `-o tree` reads the thread, `-o json` carries every field.
    assert "One two three four five" not in result.output
    assert "Ada Lovelace" in result.output


def test_entries_list_explains_a_404(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/view",
        json={"errors": [{"message": "The specified resource does not exist."}]},
        status=404,
    )
    result = _run(["list", "--topic", "5"])
    assert result.exit_code == 5
    assert "No discussion topic 5 in course 99" in result.output


def test_entries_show_prints_one_entry(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    result = _run(["show", "--topic", "5", "--entry", "300"])
    assert result.exit_code == 0, result.output
    assert "Third level here." in result.output
    assert "Ada Lovelace" in result.output


def test_entries_show_missing_entry_exits_5(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    result = _run(["show", "--topic", "5", "--entry", "999"])
    assert result.exit_code == 5
    assert "No entry 999 in topic 5" in result.output


def test_entries_create_posts_and_reads_back(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries", json={"id": 301}
    )
    _mock_thread(mock_responses, api)
    result = _run(["create", "--topic", "5", "--message", "<p>Posted.</p>"])
    assert result.exit_code == 0, result.output

    body = json.loads(mock_responses.calls[0].request.body)
    # Nested-or-flat matters: a Rails-style `message[...]` bracket key would
    # arrive as a meaningless top-level JSON field and write nothing.
    assert body == {"message": "<p>Posted.</p>"}
    assert "Verified: entry 301" in result.output


def test_entries_create_dry_run_writes_nothing(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["create", "--topic", "5", "--message", "<p>Posted.</p>", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(mock_responses.calls) == 0
    assert "POST /courses/99/discussion_topics/5/entries" in result.output
    assert "no request was made" in result.output


def test_entries_create_exits_10_when_the_readback_finds_nothing(
    write_config, mock_responses, api, monkeypatch
):
    _config(write_config)
    monkeypatch.setattr(discussions, "_VERIFY_SLEEP", 0)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries", json={"id": 777}
    )
    # Canvas said 200 OK, but neither read route can find the entry.
    _mock_thread(mock_responses, api)
    mock_responses.get(f"{api}/courses/99/discussion_topics/5/entries", json=[])

    result = _run(["create", "--topic", "5", "--message", "hi"])
    assert result.exit_code == 10
    assert "Read-back failed" in result.output


def test_entries_create_survives_a_lagging_view(
    write_config, mock_responses, api, monkeypatch
):
    _config(write_config)
    monkeypatch.setattr(discussions, "_VERIFY_SLEEP", 0)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries", json={"id": 777}
    )
    _mock_thread(mock_responses, api)
    # The materialized view hasn't rebuilt, but the entries endpoint —
    # which is not a cache — has the post.
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/entries",
        json=[{
            "id": 777,
            "user_id": 11,
            "parent_id": None,
            "created_at": "2026-09-05T10:00:00Z",
            "message": "<p>hi</p>",
            "read_state": "read",
        }],
    )
    result = _run(["create", "--topic", "5", "--message", "hi"])
    assert result.exit_code == 0, result.output
    assert "Verified: entry 777" in result.output


def test_entries_reply_targets_the_replies_endpoint(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries/100/replies", json={"id": 200}
    )
    _mock_thread(mock_responses, api)
    result = _run(["reply", "--topic", "5", "--entry", "100", "--message", "Sure."])
    assert result.exit_code == 0, result.output
    assert mock_responses.calls[0].request.url.endswith("/entries/100/replies")
    assert json.loads(mock_responses.calls[0].request.body) == {"message": "Sure."}


def test_entries_message_file_survives_prose(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    prose = (
        "<p>Thanks — I think you're onto something with the \"two selves\" idea.</p>\n"
        "\n"
        "<p>But is it \"obvious\"? O'Brien argues the opposite; don't let him "
        "off easy.</p>\n"
    )
    path = tmp_path / "reply.html"
    path.write_text(prose, encoding="utf-8")

    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries/100/replies", json={"id": 200}
    )
    _mock_thread(mock_responses, api)
    result = _run(
        ["reply", "--topic", "5", "--entry", "100", "--message-file", str(path)]
    )
    assert result.exit_code == 0, result.output
    # Byte-for-byte: no markdown pass, no reflowing, no smart quotes.
    assert json.loads(mock_responses.calls[0].request.body) == {"message": prose}


def test_entries_message_and_message_file_are_exclusive(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    path = tmp_path / "reply.txt"
    path.write_text("hi", encoding="utf-8")

    both = _run([
        "create", "--topic", "5", "--message", "hi", "--message-file", str(path),
    ])
    assert both.exit_code != 0
    assert "exactly one of --message or --message-file" in both.output

    neither = _run(["create", "--topic", "5"])
    assert neither.exit_code != 0


def test_entries_message_file_must_exist(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["create", "--topic", "5", "--message-file", "/nope/missing.txt"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_entries_mark_read_one_entry(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/discussion_topics/5/entries/301/read", body="", status=204
    )
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/view",
        json=_view_payload(unread_entries=[]),
    )
    result = _run(["mark-read", "--topic", "5", "--entry", "301"])
    assert result.exit_code == 0, result.output
    assert mock_responses.calls[0].request.method == "PUT"
    assert "Verified: entry 301 is now read" in result.output


def test_entries_mark_read_all(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/discussion_topics/5/read_all", body="", status=204
    )
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/view",
        json=_view_payload(unread_entries=[]),
    )
    result = _run(["mark-read", "--topic", "5", "--all"])
    assert result.exit_code == 0, result.output
    assert "every entry in topic 5 is now read" in result.output


def test_entries_mark_read_exits_10_if_still_unread(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/discussion_topics/5/entries/301/read", body="", status=204
    )
    # Canvas returned 204 and changed nothing.
    mock_responses.get(f"{api}/courses/99/discussion_topics/5/view", json=_view_payload())
    result = _run(["mark-read", "--topic", "5", "--entry", "301"])
    assert result.exit_code == 10
    assert "still unread" in result.output


def test_entries_mark_read_dry_run_writes_nothing(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["mark-read", "--topic", "5", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(mock_responses.calls) == 0
    assert "PUT /courses/99/discussion_topics/5/read_all" in result.output


def test_entries_mark_read_needs_exactly_one_target(write_config, mock_responses, api):
    _config(write_config)
    both = _run(["mark-read", "--topic", "5", "--entry", "301", "--all"])
    assert both.exit_code != 0
    assert "exactly one of --entry or --all" in both.output
    assert _run(["mark-read", "--topic", "5"]).exit_code != 0


def test_entries_writes_are_blocked_on_a_readonly_course(
    write_config, mock_responses, api
):
    write_config(
        """
[defaults]
timezone = "America/Denver"

[courses.t]
id = 99
readonly = true
"""
    )
    result = _run(["create", "--topic", "5", "--message", "hi"])
    assert result.exit_code == 9
    assert len(mock_responses.calls) == 0


def test_entries_list_rejects_contradictory_structure_filters(
    write_config, mock_responses, api
):
    _config(write_config)
    result = _run(["list", "--topic", "5", "--top-level-only", "--replies-only"])
    assert result.exit_code != 0
    assert "exclude each other" in result.output
    assert len(mock_responses.calls) == 0


def test_entries_list_explains_a_cold_materialized_view(
    write_config, mock_responses, api
):
    _config(write_config)
    # Canvas builds a topic's threaded view lazily: the first read of a cold
    # one comes back entirely empty even though the entries exist.
    empty = _view_payload(view=[], participants=[], unread_entries=[], new_entries=[])
    _mock_thread(mock_responses, api, view=empty)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5",
        json={"id": 5, "published": True, "discussion_subentry_count": 8},
    )
    result = _run(["list", "--topic", "5", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert "has not been materialized yet" in result.output


def test_entries_list_explains_a_group_discussion(write_config, mock_responses, api):
    _config(write_config)
    empty = _view_payload(view=[], participants=[], unread_entries=[], new_entries=[])
    _mock_thread(mock_responses, api, view=empty)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5",
        json={
            "id": 5,
            "published": True,
            "group_category_id": 12,
            "group_topic_children": [{"id": 77, "group_id": 3}],
        },
    )
    result = _run(["list", "--topic", "5", "-o", "json"])
    assert result.exit_code == 0, result.output
    # The course topic of a group discussion holds no entries at all.
    assert "group discussion" in result.output
    assert "group 3 topic 77" in result.output


def test_entries_list_paginates_the_enrollment_join(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/discussion_topics/5/view", json=_view_payload())
    # The role join is a listing, so it has to follow Link: rel="next".
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[{"id": 1, "user_id": 11, "type": "StudentEnrollment"}],
        headers={"Link": f'<{api}/courses/99/enrollments?page=2>; rel="next"'},
    )
    mock_responses.get(
        f"{api}/courses/99/enrollments?page=2",
        json=[{"id": 2, "user_id": 22, "type": "TeacherEnrollment"}],
    )
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))
    by_id = {r["id"]: r for r in records}
    # Page 2 is where the teacher lives; a single-page read would miss them.
    assert by_id[200]["role"] == "TeacherEnrollment"


def test_entries_list_degrades_when_enrollments_are_forbidden(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/discussion_topics/5/view", json=_view_payload())
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = _run(["list", "--topic", "5", "-o", "json"])
    # Losing the role join must not lose the thread.
    assert result.exit_code == 0, result.output
    # (rich wraps the stderr note, so match a fragment that cannot break)
    assert "could not read this course's enrollments" in result.output
    records = json.loads(result.output[result.output.index("[") :])
    assert len(records) == 5
    assert all(r["role"] is None for r in records)


def test_entries_list_explains_a_403(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/view",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = _run(["list", "--topic", "5"])
    assert result.exit_code == 4
    assert "locked" in result.output
    assert "group discussion" in result.output


def test_entries_list_rejects_an_unknown_output_format(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["list", "--topic", "5", "-o", "yaml"])
    assert result.exit_code != 0
    assert "Unknown output format" in result.output
    assert len(mock_responses.calls) == 0


def test_entries_list_accepts_a_full_canvas_role_type(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(
        _run(["list", "--topic", "5", "-o", "json", "--role", "TaEnrollment"])
    )
    # Grace holds both; the filter matches any of her enrollments, not just
    # the highest-privilege one.
    assert [r["id"] for r in records] == [200, 301]


def test_entries_list_tree_on_an_empty_thread(write_config, mock_responses, api):
    _config(write_config)
    empty = _view_payload(view=[], participants=[], unread_entries=[], new_entries=[])
    _mock_thread(mock_responses, api, view=empty)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5",
        json={"id": 5, "published": False, "discussion_subentry_count": 0},
    )
    result = _run(["list", "--topic", "5", "-o", "tree"])
    assert result.exit_code == 0, result.output
    assert "(no entries)" in result.output
    assert "unpublished" in result.output


def test_entries_list_survives_an_entry_with_no_author(write_config, mock_responses, api):
    _config(write_config)
    # A deleted entry has no user_id at all; name lookup and the --user
    # filter both have to cope rather than raise.
    _mock_thread(mock_responses, api)
    result = _run(["list", "--topic", "5", "-o", "tree", "--user", "ada"])
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output  # the tombstone is filtered out, not crashed on


def test_entries_show_json_carries_the_full_record(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(_run(["show", "--topic", "5", "--entry", "400", "-o", "json"]))
    assert len(records) == 1
    assert records[0]["deleted"] is True
    assert records[0]["message_text"] == ""


def test_entries_create_exits_10_when_canvas_returns_no_id(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries", json={"ok": True}
    )
    result = _run(["create", "--topic", "5", "--message", "hi"])
    assert result.exit_code == 10
    assert "returned no entry id" in result.output


def test_entries_reply_falls_back_to_the_replies_endpoint(
    write_config, mock_responses, api, monkeypatch
):
    _config(write_config)
    monkeypatch.setattr(discussions, "_VERIFY_SLEEP", 0)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries/100/replies", json={"id": 888}
    )
    _mock_thread(mock_responses, api)
    # The threaded view lags, so verification asks the parent's replies
    # listing — which is not a cache — instead.
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/entries/100/replies",
        json=[{
            "id": 888,
            "user_id": 22,
            "parent_id": 100,
            "created_at": "2026-09-05T10:00:00Z",
            "user_name": "Grace Hopper",
            "message": "<p>Late.</p>",
            "read_state": "read",
        }],
    )
    result = _run(["reply", "--topic", "5", "--entry", "100", "--message", "Late.", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert "threaded view has not rebuilt yet" in result.output
    record = json.loads(result.output[result.output.index("[") : result.output.rindex("]") + 1])[0]
    # Name falls back to the entry's own `user_name` when the (stale) view
    # has no participant entry for the author.
    assert record["display_name"] == "Grace Hopper"
    assert record["depth"] is None


def test_entries_message_file_rejects_an_empty_body(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    path = tmp_path / "blank.txt"
    path.write_text("   \n\n", encoding="utf-8")
    result = _run(["create", "--topic", "5", "--message-file", str(path)])
    assert result.exit_code != 0
    assert "message body is empty" in result.output


def test_entries_message_file_rejects_non_utf8(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    path = tmp_path / "latin1.txt"
    path.write_bytes("caf\xe9 not utf-8".encode("latin-1"))
    result = _run(["create", "--topic", "5", "--message-file", str(path)])
    assert result.exit_code != 0
    assert "not valid UTF-8" in result.output


def test_entries_mark_read_surfaces_a_403(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/discussion_topics/5/read_all",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = _run(["mark-read", "--topic", "5", "--all"])
    assert result.exit_code == 4
    assert "refused this discussion request" in result.output


def test_entries_list_since_alone_drops_older_entries(write_config, mock_responses, api):
    _config(write_config)
    _mock_thread(mock_responses, api)
    records = _entries(
        _run(["list", "--topic", "5", "-o", "json", "--since", "2026-09-03"])
    )
    # Sept 1 and Sept 2 fall away; the deleted Sept 3 entry does not.
    assert [r["id"] for r in records] == [300, 400, 301]


def test_entries_list_rejects_an_unparseable_since(write_config, mock_responses, api):
    _config(write_config)
    result = _run(["list", "--topic", "5", "--since", "last tuesday"])
    assert result.exit_code == 2
    assert "Invalid date" in result.output
    assert len(mock_responses.calls) == 0


def test_entries_list_labels_an_author_missing_from_participants(
    write_config, mock_responses, api
):
    _config(write_config)
    # Canvas only lists participants who posted *and* are still resolvable;
    # an entry can name a user the map has never heard of.
    view = _view_payload(participants=[])
    _mock_thread(mock_responses, api, view=view)
    result = _run(["list", "--topic", "5", "-o", "tree"])
    assert result.exit_code == 0, result.output
    assert "user 11" in result.output


def test_entries_list_ranks_an_institution_defined_role_last(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_thread(
        mock_responses,
        api,
        enrollments=[
            {"id": 1, "user_id": 11, "type": "PeerMentorEnrollment"},
            {"id": 2, "user_id": 11, "type": "StudentEnrollment"},
        ],
    )
    records = _entries(_run(["list", "--topic", "5", "-o", "json"]))
    ada = next(r for r in records if r["user_id"] == 11)
    # A type outside the known precedence sorts after every known one
    # rather than crashing the sort or winning by accident.
    assert ada["roles"] == ["StudentEnrollment", "PeerMentorEnrollment"]
    assert ada["role"] == "StudentEnrollment"


def test_entries_list_falls_through_to_the_generic_handler(
    write_config, mock_responses, api, monkeypatch
):
    _config(write_config)
    # The client retries 5xx with backoff; don't actually wait for it.
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/discussion_topics/5/view",
        json={"errors": [{"message": "Internal server error"}]},
        status=500,
    )
    result = _run(["list", "--topic", "5"])
    # Not a 404 or 403, so no entry-specific advice — just the standard
    # CanvasError exit code.
    assert result.exit_code == 8


def test_entries_mark_read_all_exits_10_if_any_remain_unread(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.put(
        f"{api}/courses/99/discussion_topics/5/read_all", body="", status=204
    )
    mock_responses.get(f"{api}/courses/99/discussion_topics/5/view", json=_view_payload())
    result = _run(["mark-read", "--topic", "5", "--all"])
    assert result.exit_code == 10
    assert "1 entry is still" in result.output


def test_entries_reply_exits_10_when_canvas_returns_no_id(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/discussion_topics/5/entries/100/replies", json={}
    )
    result = _run(["reply", "--topic", "5", "--entry", "100", "--message", "hi"])
    assert result.exit_code == 10
    assert "returned no entry id" in result.output
