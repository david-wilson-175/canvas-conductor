"""Tests for the `progress` command group (per-student and aggregate progress).

The fixture course below is deliberately shaped like a real one: a front-matter
module with no trackable work, three numbered steps with one assignment each,
and three students at different points in the sequence. `--as-of 2026-09-01`
pins the pace baseline so the expectations don't drift with the wall clock.
"""
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


# --- fixture course -------------------------------------------------------

MODULES = [
    {
        "id": 1,
        "position": 1,
        "name": "Getting Started",
        "published": True,
        "items": [{"id": 100, "type": "Page", "title": "Welcome", "page_url": "welcome"}],
    },
    {
        "id": 2,
        "position": 2,
        "name": "Step 1: Resume",
        "published": True,
        "items": [
            {"id": 101, "type": "Page", "title": "Step 1", "page_url": "step-1"},
            {"id": 102, "type": "Assignment", "title": "Resume", "content_id": 5001},
        ],
    },
    {
        "id": 3,
        "position": 3,
        "name": "Step 2: LinkedIn",
        "published": True,
        "items": [{"id": 103, "type": "Assignment", "title": "LinkedIn", "content_id": 5002}],
    },
    {
        "id": 4,
        "position": 4,
        "name": "Step 3: Portfolio",
        "published": True,
        "items": [{"id": 104, "type": "Assignment", "title": "Portfolio", "content_id": 5003}],
    },
]

ASSIGNMENTS = [
    {"id": 5001, "name": "Resume", "due_at": "2026-08-16T05:59:00Z"},
    {"id": 5002, "name": "LinkedIn", "due_at": "2026-08-23T05:59:00Z"},
    {"id": 5003, "name": "Portfolio", "due_at": "2026-09-30T05:59:00Z"},
]


def _student(uid, name, sortable, login, last_activity="2026-09-01T12:00:00Z"):
    return {
        "id": uid * 10,
        "user_id": uid,
        "course_section_id": 7,
        "type": "StudentEnrollment",
        "enrollment_state": "active",
        "last_activity_at": last_activity,
        "total_activity_time": 120,
        "user": {
            "id": uid,
            "name": name,
            "sortable_name": sortable,
            "login_id": login,
            "sis_user_id": f"sis-{uid}",
        },
    }


ENROLLMENTS = [
    _student(11, "Ada Finished", "Finished, Ada", "ada"),
    _student(12, "Bo Partial", "Partial, Bo", "bo"),
    _student(13, "Cy Absent", "Absent, Cy", "cy", last_activity=None),
]


def _sub(uid, aid, state="unsubmitted", submitted_at=None, **extra):
    return {
        "user_id": uid,
        "assignment_id": aid,
        "workflow_state": state,
        "submitted_at": submitted_at,
        **extra,
    }


# Ada: all three done. Bo: skipped step 1, did step 2 only. Cy: nothing.
SUBMISSIONS = [
    _sub(11, 5001, "submitted", "2026-08-10T00:00:00Z"),
    _sub(11, 5002, "submitted", "2026-08-20T00:00:00Z"),
    _sub(11, 5003, "graded"),  # graded with no submission (offline work)
    _sub(12, 5001),
    _sub(12, 5002, "submitted", "2026-08-22T00:00:00Z"),
    _sub(12, 5003),
    _sub(13, 5001),
    _sub(13, 5002),
    _sub(13, 5003),
]


def _mock_course(mock_responses, api, modules=None, submissions=None):
    mock_responses.get(f"{api}/courses/99/modules", json=modules or MODULES)
    mock_responses.get(f"{api}/courses/99/assignments", json=ASSIGNMENTS)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/students/submissions",
        json=SUBMISSIONS if submissions is None else submissions,
    )


AS_OF = ["--as-of", "2026-09-01"]


# --------------------------------------------------------------------------
# student
# --------------------------------------------------------------------------


def test_student_reports_step_by_step_progress(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "student", "--user", "ada", *AS_OF])
    assert result.exit_code == 0, result.output
    assert "Ada Finished" in result.output
    assert "step 3 of 3" in result.output
    assert "3 of 3 steps (100%)" in result.output


def test_student_json_separates_reached_from_completed(write_config, mock_responses, api):
    """Bo did step 2 but not step 1: reached 2, completed 1."""
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "student", "--user", "bo", "-o", "json", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reached"] == 2
    assert payload["completed"] == 1
    assert payload["steps_total"] == 3
    # Steps 1 and 2 were due by 2026-09-01; step 3 is due Sept 30.
    assert payload["expected"] == 2
    assert payload["pace"] == 0.5
    statuses = {s["step"]: s["status"] for s in payload["steps"]}
    assert statuses == {None: "—", 1: "not started", 2: "complete", 3: "not started"}


def test_student_front_matter_module_is_excluded_from_the_denominator(
    write_config, mock_responses, api
):
    """'Getting Started' has no trackable item, so the course is 3 steps not 4."""
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "json", *AS_OF]
    )
    payload = json.loads(result.output)
    assert payload["steps_total"] == 3
    front = [s for s in payload["steps"] if s["name"] == "Getting Started"][0]
    assert front["step"] is None
    assert front["total"] == 0


def test_student_counts_a_grade_without_a_submission_as_done(
    write_config, mock_responses, api
):
    """Ada's step 3 was graded offline — no submitted_at, but she's not carrying it."""
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "json", *AS_OF]
    )
    statuses = {s["step"]: s["status"] for s in json.loads(result.output)["steps"]}
    assert statuses[3] == "complete"


def test_student_ambiguous_name_lists_candidates(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "student", "--user", "a", *AS_OF])
    assert result.exit_code == 2
    assert "matched" in result.output


def test_student_unknown_user_is_a_clean_error(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "student", "--user", "nobody", *AS_OF])
    assert result.exit_code == 2
    assert "No student matched" in result.output


# --------------------------------------------------------------------------
# course
# --------------------------------------------------------------------------


def test_course_aggregates_across_students(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", "-o", "json", *AS_OF])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["students"] == 3
    assert payload["steps_total"] == 3
    assert payload["expected_steps"] == 2
    # Ada 3, Bo 1, Cy 0.
    assert payload["mean_completed"] == 1.33
    assert payload["median_completed"] == 1
    # Reached: Ada 3, Bo 2, Cy 0.
    assert payload["mean_reached"] == 1.67
    assert payload["not_started_students"] == 1
    assert payload["finished_students"] == 1
    assert payload["never_active_students"] == 1
    assert payload["reached_distribution"] == {"0": 1, "1": 0, "2": 1, "3": 1}


def test_course_pace_flags_who_is_behind(write_config, mock_responses, api):
    """Two steps due: Ada (3) is ahead, Bo (1) and Cy (0) are behind."""
    _config(write_config)
    _mock_course(mock_responses, api)
    payload = json.loads(
        runner.invoke(app, ["progress", "course", "-o", "json", *AS_OF]).output
    )
    assert payload["on_pace_students"] == 1
    assert payload["behind_students"] == 2
    assert payload["pace_index"] == 0.67


def test_course_table_output_renders_the_report(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", *AS_OF])
    assert result.exit_code == 0, result.output
    assert "Per-step completion" in result.output
    assert "Furthest step reached" in result.output
    assert "Momentum" in result.output
    assert "Step 1: Resume" in result.output


def test_course_with_no_modules_says_so(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/modules", json=[])
    result = runner.invoke(app, ["progress", "course"])
    assert result.exit_code == 0, result.output
    assert "no modules" in result.output


def test_course_with_no_trackable_items_says_so(write_config, mock_responses, api):
    """A course of nothing but pages has no per-student signal to report."""
    _config(write_config)
    pages_only = [dict(MODULES[0])]
    _mock_course(mock_responses, api, modules=pages_only)
    result = runner.invoke(app, ["progress", "course", *AS_OF])
    assert result.exit_code == 0, result.output
    assert "No trackable items" in result.output


# --------------------------------------------------------------------------
# source selection
# --------------------------------------------------------------------------


def test_native_source_refuses_a_course_with_no_requirements(
    write_config, mock_responses, api
):
    """Canvas reports every module 'completed' when no requirements exist —
    reporting that as 100% progress would be worse than failing."""
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", "--source", "native", *AS_OF])
    assert result.exit_code == 2
    assert "no module completion requirements" in result.output


def test_auto_source_picks_native_when_requirements_exist(
    write_config, mock_responses, api
):
    _config(write_config)
    with_reqs = json.loads(json.dumps(MODULES))
    with_reqs[1]["items"][1]["completion_requirement"] = {
        "type": "must_submit",
        "completed": True,
    }
    with_reqs[2]["items"][0]["completion_requirement"] = {
        "type": "must_submit",
        "completed": False,
    }
    mock_responses.get(f"{api}/courses/99/modules", json=with_reqs)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)

    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "json", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source"] == "native"
    # Only the two items carrying a requirement are steps; Canvas says one is done.
    assert payload["steps_total"] == 2
    assert payload["completed"] == 1
    assert payload["reached"] == 1


def test_auto_source_falls_back_to_submissions_and_says_so(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "student", "--user", "ada", *AS_OF])
    assert result.exit_code == 0, result.output
    assert "Source: submissions" in result.output


def test_invalid_source_is_rejected(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", "--source", "vibes", *AS_OF])
    assert result.exit_code != 0


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def test_export_emits_one_row_per_student_with_a_column_per_step(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "export", "-o", "csv", *AS_OF])
    assert result.exit_code == 0, result.output
    # CliRunner folds stderr into output; the source note is written there
    # precisely so that piping the CSV somewhere gives clean data.
    lines = result.output.strip().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("User ID,"))
    header, body = lines[start], lines[start + 1 :]
    assert "Step 1" in header and "Step 3" in header
    assert "Step 4" not in header  # only 3 real steps
    assert len(body) == 3
    assert body[0].startswith("13,")  # sorted by sortable_name: Absent, Cy


def test_export_behind_only_filters_to_students_off_pace(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "export", "--behind-only", "-o", "json", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert {r["login_id"] for r in rows} == {"bo", "cy"}


def test_export_json_carries_pace_and_activity_fields(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    rows = json.loads(
        runner.invoke(app, ["progress", "export", "-o", "json", *AS_OF]).output
    )
    ada = [r for r in rows if r["login_id"] == "ada"][0]
    assert ada["reached"] == 3
    assert ada["completed"] == 3
    assert ada["on_pace"] is True
    assert ada["step_1"] == "complete"
    cy = [r for r in rows if r["login_id"] == "cy"][0]
    assert cy["last_activity_at"] is None
    assert cy["on_pace"] is False


# --------------------------------------------------------------------------
# resolving a module item to its assignment
# --------------------------------------------------------------------------


def test_quiz_and_discussion_items_resolve_through_their_back_references(
    write_config, mock_responses, api
):
    """A module item's `content_id` is type-dependent: assignment id for an
    Assignment, quiz id for a Quiz, topic id for a Discussion. The assignment
    listing carries `quiz_id` and `discussion_topic.id` so one call maps all
    three. Get this wrong and quizzes/discussions vanish from the denominator.
    """
    _config(write_config)
    mixed_modules = [
        {
            "id": 20,
            "position": 1,
            "name": "Step 1: Quiz",
            "items": [{"id": 200, "type": "Quiz", "title": "Q", "content_id": 9001}],
        },
        {
            "id": 21,
            "position": 2,
            "name": "Step 2: Discussion",
            "items": [{"id": 201, "type": "Discussion", "title": "D", "content_id": 9002}],
        },
        {
            "id": 22,
            "position": 3,
            "name": "Step 3: Ungraded survey",
            # A practice quiz with no backing assignment is correctly untrackable.
            "items": [{"id": 202, "type": "Quiz", "title": "S", "content_id": 9003}],
        },
    ]
    mixed_assignments = [
        {"id": 6001, "name": "Q", "due_at": "2026-08-16T05:59:00Z", "quiz_id": 9001},
        {
            "id": 6002,
            "name": "D",
            "due_at": "2026-08-23T05:59:00Z",
            "discussion_topic": {"id": 9002},
        },
    ]
    mock_responses.get(f"{api}/courses/99/modules", json=mixed_modules)
    mock_responses.get(f"{api}/courses/99/assignments", json=mixed_assignments)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/students/submissions",
        json=[
            _sub(11, 6001, "submitted", "2026-08-10T00:00:00Z"),
            _sub(11, 6002, "submitted", "2026-08-20T00:00:00Z"),
        ],
    )

    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "json", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # The quiz and the discussion count; the ungraded survey does not.
    assert payload["steps_total"] == 2
    assert payload["completed"] == 2
    statuses = {s["name"]: s["status"] for s in payload["steps"]}
    assert statuses["Step 1: Quiz"] == "complete"
    assert statuses["Step 2: Discussion"] == "complete"
    assert statuses["Step 3: Ungraded survey"] == "—"


def test_a_step_with_several_items_reports_partial(write_config, mock_responses, api):
    _config(write_config)
    two_item = [
        {
            "id": 30,
            "position": 1,
            "name": "Step 1: Two deliverables",
            "items": [
                {"id": 300, "type": "Assignment", "title": "A", "content_id": 5001},
                {"id": 301, "type": "Assignment", "title": "B", "content_id": 5002},
            ],
        }
    ]
    mock_responses.get(f"{api}/courses/99/modules", json=two_item)
    mock_responses.get(f"{api}/courses/99/assignments", json=ASSIGNMENTS)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/students/submissions",
        json=[_sub(11, 5001, "submitted", "2026-08-10T00:00:00Z"), _sub(11, 5002)],
    )
    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "json", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    step = payload["steps"][0]
    assert (step["done"], step["total"], step["status"]) == (1, 2, "partial")
    # Half-done is not done: reached the step, did not complete it.
    assert payload["reached"] == 1
    assert payload["completed"] == 0


def test_an_excused_student_is_not_still_carrying_the_item(
    write_config, mock_responses, api
):
    _config(write_config)
    _mock_course(
        mock_responses,
        api,
        submissions=[_sub(11, 5001, "unsubmitted", None, excused=True)],
    )
    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "json", *AS_OF]
    )
    statuses = {s["step"]: s["status"] for s in json.loads(result.output)["steps"]}
    assert statuses[1] == "complete"


def test_student_resolves_on_a_unique_name_substring(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "student", "--user", "finished", "-o", "json", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["user_id"] == 11


def test_student_csv_output_emits_the_step_rows(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "student", "--user", "ada", "-o", "csv", *AS_OF]
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Step,"))
    assert lines[start].startswith("Step,Module,Due,Done,Status")
    assert len(lines[start + 1 :]) == 4  # front matter + 3 steps


# --------------------------------------------------------------------------
# section filtering
# --------------------------------------------------------------------------

SECTIONS = [
    {"id": 7, "name": "Section A", "course_id": 99},
    {"id": 8, "name": "Section B", "course_id": 99},
]


def _sectioned(mock_responses, api):
    """Cy sits in Section B; Ada and Bo are in Section A."""
    enrollments = json.loads(json.dumps(ENROLLMENTS))
    enrollments[2]["course_section_id"] = 8
    mock_responses.get(f"{api}/courses/99/modules", json=MODULES)
    mock_responses.get(f"{api}/courses/99/assignments", json=ASSIGNMENTS)
    mock_responses.get(f"{api}/courses/99/enrollments", json=enrollments)
    mock_responses.get(f"{api}/courses/99/sections", json=SECTIONS)
    mock_responses.get(f"{api}/courses/99/students/submissions", json=SUBMISSIONS)


def test_section_filter_narrows_the_cohort_by_name(write_config, mock_responses, api):
    _config(write_config)
    _sectioned(mock_responses, api)
    payload = json.loads(
        runner.invoke(
            app, ["progress", "course", "--section", "Section B", "-o", "json", *AS_OF]
        ).output
    )
    assert payload["students"] == 1
    assert payload["not_started_students"] == 1


def test_section_filter_accepts_an_id(write_config, mock_responses, api):
    _config(write_config)
    _sectioned(mock_responses, api)
    payload = json.loads(
        runner.invoke(
            app, ["progress", "course", "--section", "7", "-o", "json", *AS_OF]
        ).output
    )
    assert payload["students"] == 2


def test_unknown_section_is_a_clean_error(write_config, mock_responses, api):
    _config(write_config)
    _sectioned(mock_responses, api)
    result = runner.invoke(
        app, ["progress", "course", "--section", "Section Z", *AS_OF]
    )
    assert result.exit_code == 2
    assert "No section matched" in result.output


# --------------------------------------------------------------------------
# native source, course-wide
# --------------------------------------------------------------------------


def _native_course(mock_responses, api):
    """Serve per-student module progression keyed off the `student_id` param.

    Ada has met both requirements, Bo the second only, Cy neither.
    """
    with_reqs = json.loads(json.dumps(MODULES))
    with_reqs[1]["items"][1]["completion_requirement"] = {"type": "must_submit"}
    with_reqs[2]["items"][0]["completion_requirement"] = {"type": "must_submit"}
    done_by_user = {"11": {102, 103}, "12": {103}, "13": set()}

    def callback(request):
        from urllib.parse import parse_qs, urlparse

        uid = parse_qs(urlparse(request.url).query).get("student_id", [None])[0]
        body = json.loads(json.dumps(with_reqs))
        if uid is not None:
            done = done_by_user[uid]
            for module in body:
                for item in module["items"]:
                    if item.get("completion_requirement"):
                        item["completion_requirement"]["completed"] = (
                            item["id"] in done
                        )
        return (200, {}, json.dumps(body))

    mock_responses.add_callback(
        "GET", f"{api}/courses/99/modules", callback=callback, content_type="application/json"
    )
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)


def test_course_native_source_aggregates_per_student_progression(
    write_config, mock_responses, api
):
    _config(write_config)
    _native_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", "-o", "json", *AS_OF])
    assert result.exit_code == 0, result.output
    # The native source prints a request-cost note to stderr first; CliRunner
    # folds stderr into output, so slice to the JSON body.
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["source"] == "native"
    assert payload["steps_total"] == 2
    # Ada 2 steps, Bo 1, Cy 0.
    assert payload["mean_completed"] == 1.0
    assert payload["finished_students"] == 1
    assert payload["not_started_students"] == 1
    assert payload["reached_distribution"] == {"0": 1, "1": 0, "2": 2}


def test_course_native_source_warns_about_its_request_cost(
    write_config, mock_responses, api
):
    _config(write_config)
    _native_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", *AS_OF])
    assert result.exit_code == 0, result.output
    assert "one request per student" in result.output


def test_export_native_source_produces_per_student_rows(
    write_config, mock_responses, api
):
    _config(write_config)
    _native_course(mock_responses, api)
    out = runner.invoke(app, ["progress", "export", "-o", "json", *AS_OF]).output
    rows = json.loads(out[out.index("[") :])
    by_login = {r["login_id"]: r for r in rows}
    assert by_login["ada"]["completed"] == 2
    assert by_login["bo"]["completed"] == 1
    assert by_login["cy"]["completed"] == 0


def test_a_multi_section_student_is_counted_once(write_config, mock_responses, api):
    """Canvas returns one enrollment per section. Counting both would inflate
    the denominator and double-weight that student in every average."""
    _config(write_config)
    def ada_in(section_id, enrollment_id, last_activity):
        dup = json.loads(json.dumps(ENROLLMENTS[0]))
        dup.update(
            id=enrollment_id,
            course_section_id=section_id,
            last_activity_at=last_activity,
        )
        return dup

    # Ada appears three times. The newest activity must win regardless of the
    # order Canvas returns them in, so bracket the newest with an older one
    # on each side.
    enrollments = (
        [ada_in(8, 998, "2026-08-20T12:00:00Z")]
        + json.loads(json.dumps(ENROLLMENTS))
        + [ada_in(9, 999, "2026-09-02T12:00:00Z"), ada_in(10, 1000, "2026-08-01T00:00:00Z")]
    )
    mock_responses.get(f"{api}/courses/99/modules", json=MODULES)
    mock_responses.get(f"{api}/courses/99/assignments", json=ASSIGNMENTS)
    mock_responses.get(f"{api}/courses/99/enrollments", json=enrollments)
    mock_responses.get(f"{api}/courses/99/students/submissions", json=SUBMISSIONS)

    payload = json.loads(
        runner.invoke(app, ["progress", "course", "-o", "json", *AS_OF]).output
    )
    assert payload["students"] == 3
    # The surviving row keeps the most recent activity across her sections.
    rows = json.loads(
        runner.invoke(app, ["progress", "export", "-o", "json", *AS_OF]).output
    )
    ada = [r for r in rows if r["login_id"] == "ada"]
    assert len(ada) == 1
    assert ada[0]["last_activity_at"] == "2026-09-02T12:00:00Z"


def test_course_csv_output_emits_the_step_rows(write_config, mock_responses, api):
    _config(write_config)
    _mock_course(mock_responses, api)
    result = runner.invoke(app, ["progress", "course", "-o", "csv", *AS_OF])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Step,"))
    assert lines[start].startswith("Step,Module,Due,Complete,Started")
    assert len(lines[start + 1 :]) == 4  # front matter + 3 steps


def test_course_counts_partially_started_students_separately(
    write_config, mock_responses, api
):
    """A step with some but not all items done is 'started', not 'complete'."""
    _config(write_config)
    two_item = [
        {
            "id": 30,
            "position": 1,
            "name": "Step 1: Two deliverables",
            "items": [
                {"id": 300, "type": "Assignment", "title": "A", "content_id": 5001},
                {"id": 301, "type": "Assignment", "title": "B", "content_id": 5002},
            ],
        }
    ]
    mock_responses.get(f"{api}/courses/99/modules", json=two_item)
    mock_responses.get(f"{api}/courses/99/assignments", json=ASSIGNMENTS)
    mock_responses.get(f"{api}/courses/99/enrollments", json=ENROLLMENTS)
    mock_responses.get(
        f"{api}/courses/99/students/submissions",
        json=[
            # Ada finished both, Bo only one, Cy neither.
            _sub(11, 5001, "submitted", "2026-08-10T00:00:00Z"),
            _sub(11, 5002, "submitted", "2026-08-11T00:00:00Z"),
            _sub(12, 5001, "submitted", "2026-08-12T00:00:00Z"),
        ],
    )
    payload = json.loads(
        runner.invoke(app, ["progress", "course", "-o", "json", *AS_OF]).output
    )
    step = payload["steps"][0]
    assert step["complete"] == 1
    assert step["started"] == 1


def test_course_reports_when_no_students_match(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/modules", json=MODULES)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[])
    result = runner.invoke(app, ["progress", "course", *AS_OF])
    assert result.exit_code == 0, result.output
    assert "No student enrollments matched" in result.output


# --------------------------------------------------------------------------
# error propagation
# --------------------------------------------------------------------------


def test_canvas_404_is_reported_with_the_standard_exit_code(
    write_config, mock_responses, api
):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/modules",
        json={"errors": [{"message": "The specified resource does not exist."}]},
        status=404,
    )
    result = runner.invoke(app, ["progress", "course"])
    assert result.exit_code == 5
    assert "not found" in result.output.lower()
