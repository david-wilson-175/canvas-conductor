"""Student progress through a course's module sequence — per student and aggregated.

Canvas ships a per-student progress view (Modules > Student Progress, backed by
``GET /courses/:id/users/:uid/progress``) but no aggregate one: there is no
built-in way to ask "how far has the class as a whole gotten?". This group
answers that question, and the per-student one, from the terminal.

Two data sources
----------------
``--source native`` uses Canvas's own module progression:

    GET /courses/:id/modules?student_id=:uid&include[]=items

This is the data behind the Student Progress page. **It only exists when the
course's module items carry completion requirements** (see
``conductor requirements``). With no requirements anywhere in the course,
Canvas reports every module as ``state: "completed"`` for every student —
including students who have never logged in — and ``/users/:uid/progress``
returns a 400 reading "no progress available because this course is not module
based". That vacuous "completed" is very easy to mistake for real data, so the
native source refuses to run against a course with zero requirements rather
than cheerfully reporting 100% completion.

``--source submissions`` derives progress from graded work instead:

    GET /courses/:id/modules?include[]=items       (the sequence)
    GET /courses/:id/assignments                   (due dates, item -> assignment)
    GET /courses/:id/students/submissions?student_ids[]=all

Any module item that resolves to an assignment (an ``Assignment``, a graded
``Quiz``, or a graded ``Discussion``) is trackable, and a student has done it
once they have submitted, been graded, or been excused. Pages and other
view-only items are invisible to this source: without a completion requirement
Canvas records no per-student signal for them at all.

``--source auto`` (the default) picks native when the course has any completion
requirement and submissions otherwise. Every command prints which source it
used, because the two answer subtly different questions.

Vocabulary
----------
step        A module holding at least one trackable item. Modules that are pure
            front matter (only pages, no requirements) are listed but kept out
            of every denominator — otherwise a course's "Getting Started"
            module would permanently cap completion below 100%.
reached     The highest-numbered step where the student has done at least one
            trackable item. "How far into the sequence did they get."
completed   A step where the student has done *all* of its trackable items.
expected    How many steps were due as of ``--as-of`` (default: now). The pace
            baseline. Steps with no due date never count as expected.
pace        completed / expected. 1.0 is on schedule, below 1.0 is behind.

Commands
--------
  conductor progress student --user "Jane Doe"   # one student's walk
  conductor progress course                      # the aggregate view
  conductor progress export -o csv               # one row per student
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

import typer

from ..client import get_client
from ..config import get_course_id
from ..utils.dates import local_day, resolve_timezone, to_canvas_datetime
from ..utils.output import format_output
from ._common import emit, err_console, handle_canvas_error

app = typer.Typer(
    name="progress",
    help="Track student progress through the module sequence (per student and aggregate)",
    no_args_is_help=True,
)


VALID_SOURCES = {"auto", "native", "submissions"}

# Module item types whose `content_id` can resolve to an assignment, and
# therefore to a per-student submission. Page / SubHeader / ExternalUrl / File
# carry no per-student signal at all unless they have a completion requirement.
SUBMITTABLE_ITEM_TYPES = ("Assignment", "Quiz", "Discussion")

STEP_COLUMNS = [
    ("Step", "step_label"),
    ("Module", "name"),
    ("Due", "due_local"),
    ("Done", "done_label"),
    ("Status", "status"),
]

COURSE_STEP_COLUMNS = [
    ("Step", "step_label"),
    ("Module", "name"),
    ("Due", "due_local"),
    ("Complete", "n_complete"),
    ("Started", "n_started"),
    ("%", "pct_label"),
]


# --------------------------------------------------------------------------
# Fetching and shaping the course sequence
# --------------------------------------------------------------------------


def _fetch_modules(client, cid: int) -> list[dict]:
    return client.get_all(f"/courses/{cid}/modules", params={"include[]": "items"})


def _count_requirements(modules: list[dict]) -> int:
    return sum(
        1
        for m in modules
        for item in (m.get("items") or [])
        if item.get("completion_requirement")
    )


def _assignment_lookup(client, cid: int) -> dict[str, dict[Any, dict]]:
    """Build `item_type -> content_id -> assignment` maps.

    A module item's `content_id` means a different thing per type: for an
    `Assignment` it is the assignment id, for a `Quiz` the quiz id, for a
    `Discussion` the discussion topic id. The assignment payload carries both
    back-references (`quiz_id`, `discussion_topic.id`), so a single listing
    builds all three maps. New Quizzes items arrive typed as `Assignment`
    already, so they need no special handling.
    """
    assignments = client.get_all(f"/courses/{cid}/assignments")
    maps: dict[str, dict[Any, dict]] = {t: {} for t in SUBMITTABLE_ITEM_TYPES}
    for asg in assignments:
        maps["Assignment"][asg["id"]] = asg
        if asg.get("quiz_id"):
            maps["Quiz"][asg["quiz_id"]] = asg
        topic = asg.get("discussion_topic")
        if isinstance(topic, dict) and topic.get("id"):
            maps["Discussion"][topic["id"]] = asg
    return maps


def _build_steps(
    modules: list[dict],
    assignment_maps: dict[str, dict[Any, dict]] | None,
    use_requirements: bool,
) -> list[dict]:
    """Return the module sequence in course order with trackable items resolved.

    `use_requirements` selects which notion of "trackable" applies: the native
    source tracks items carrying a completion requirement, the submissions
    source tracks items backed by an assignment.
    """
    steps: list[dict] = []
    ordered = sorted(modules, key=lambda m: (m.get("position") or 0, m.get("id") or 0))

    for module in ordered:
        tracked: list[dict] = []
        for item in module.get("items") or []:
            if use_requirements:
                if not item.get("completion_requirement"):
                    continue
                tracked.append(
                    {
                        "item_id": item["id"],
                        "title": item.get("title", ""),
                        "type": item.get("type"),
                        "assignment_id": None,
                        "due_at": None,
                    }
                )
                continue

            asg = (assignment_maps or {}).get(item.get("type"), {}).get(
                item.get("content_id")
            )
            if not asg:
                continue
            tracked.append(
                {
                    "item_id": item["id"],
                    "title": item.get("title", ""),
                    "type": item.get("type"),
                    "assignment_id": asg["id"],
                    "due_at": asg.get("due_at"),
                }
            )

        dues = [t["due_at"] for t in tracked if t.get("due_at")]
        steps.append(
            {
                "module_id": module["id"],
                "position": module.get("position"),
                "name": module.get("name", ""),
                "published": module.get("published"),
                "items": tracked,
                # A module is "due" when its last piece of tracked work is due.
                "due_at": max(dues) if dues else None,
            }
        )

    # Number only the modules that actually carry trackable work, so that
    # front-matter modules don't shift every step number by one.
    counter = 0
    for step in steps:
        if step["items"]:
            counter += 1
            step["step"] = counter
        else:
            step["step"] = None
    return steps


def _total_steps(steps: list[dict]) -> int:
    return sum(1 for s in steps if s["step"] is not None)


# --------------------------------------------------------------------------
# Students
# --------------------------------------------------------------------------


def _fetch_students(
    client, cid: int, states: list[str], section: str | None = None
) -> list[dict]:
    """Active student enrollments, deduped by user (multi-section students)."""
    enrollments = client.get_all(
        f"/courses/{cid}/enrollments",
        params={"type[]": "StudentEnrollment", "state[]": states},
    )
    if section:
        sections = client.get_all(f"/courses/{cid}/sections")
        wanted = {
            s["id"]
            for s in sections
            if str(s.get("id")) == section
            or section.lower() in (s.get("name") or "").lower()
        }
        if not wanted:
            raise ValueError(f"No section matched {section!r} in course {cid}.")
        enrollments = [e for e in enrollments if e.get("course_section_id") in wanted]

    by_user: dict[int, dict] = {}
    for enr in enrollments:
        uid = enr.get("user_id")
        if uid is None:
            continue
        prior = by_user.get(uid)
        # Keep the most recent activity across a student's section enrollments.
        if prior and (prior.get("last_activity_at") or "") >= (
            enr.get("last_activity_at") or ""
        ):
            continue
        by_user[uid] = enr
    return sorted(
        by_user.values(), key=lambda e: (e.get("user") or {}).get("sortable_name") or ""
    )


def _resolve_student(students: list[dict], needle: str) -> dict:
    """Match one student by Canvas id, SIS id, login, or name substring."""
    needle = needle.strip()
    lowered = needle.lower()

    for enr in students:
        user = enr.get("user") or {}
        if needle in {
            str(enr.get("user_id")),
            str(user.get("id")),
            str(user.get("sis_user_id") or ""),
            str(user.get("login_id") or ""),
        }:
            return enr

    matches = [
        e
        for e in students
        if lowered in ((e.get("user") or {}).get("name") or "").lower()
        or lowered in ((e.get("user") or {}).get("sortable_name") or "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"No student matched {needle!r}. Try a Canvas user id, login, or "
            "part of the name as it appears in `conductor enrollments list`."
        )
    names = ", ".join((m.get("user") or {}).get("name", "?") for m in matches[:8])
    raise ValueError(
        f"{len(matches)} students matched {needle!r}: {names}"
        f"{' …' if len(matches) > 8 else ''}. Narrow it down or pass a user id."
    )


# --------------------------------------------------------------------------
# The two progress sources. Both reduce to "which module item ids are done".
# --------------------------------------------------------------------------


def _submission_is_done(sub: dict | None) -> bool:
    """Has the student discharged this piece of work?

    Submitting is the common case. A teacher-entered grade with no submission
    (offline work — an in-person interview, a Testing Center exam) also counts,
    as does an excusal: in both the student is no longer carrying the item.
    """
    if not sub:
        return False
    if sub.get("excused"):
        return True
    if sub.get("submitted_at"):
        return True
    return sub.get("workflow_state") in {"submitted", "graded", "pending_review"}


def _submissions_by_user(
    client, cid: int, student_ids: list[int] | None = None
) -> dict[int, dict[int, dict]]:
    """`user_id -> assignment_id -> submission`, in one paginated sweep."""
    params: dict[str, Any] = {
        "student_ids[]": [str(i) for i in student_ids] if student_ids else "all"
    }
    rows = client.get_all(f"/courses/{cid}/students/submissions", params=params)
    out: dict[int, dict[int, dict]] = {}
    for row in rows:
        uid, aid = row.get("user_id"), row.get("assignment_id")
        if uid is None or aid is None:
            continue
        out.setdefault(uid, {})[aid] = row
    return out


def _done_ids_from_submissions(steps: list[dict], subs: dict[int, dict]) -> set[int]:
    return {
        item["item_id"]
        for step in steps
        for item in step["items"]
        if _submission_is_done(subs.get(item["assignment_id"]))
    }


def _done_ids_native(client, cid: int, user_id: int) -> set[int]:
    """Ask Canvas directly which requirements this student has met."""
    modules = client.get_all(
        f"/courses/{cid}/modules",
        params={"student_id": user_id, "include[]": "items"},
    )
    return {
        item["id"]
        for module in modules
        for item in (module.get("items") or [])
        if (item.get("completion_requirement") or {}).get("completed")
    }


def _resolve_source(requested: str, requirement_count: int) -> str:
    if requested not in VALID_SOURCES:
        raise typer.BadParameter(
            f"--source must be one of: {', '.join(sorted(VALID_SOURCES))}"
        )
    if requested == "native" and requirement_count == 0:
        raise ValueError(
            "This course has no module completion requirements, so Canvas's "
            "native progress data does not exist for it — every module would "
            "report as 'completed' for every student, including students who "
            "have never opened the course. Use --source submissions, or set "
            "requirements first with `conductor requirements bulk-set`."
        )
    if requested != "auto":
        return requested
    return "native" if requirement_count else "submissions"


def _source_note(source: str, requirement_count: int) -> str:
    if source == "native":
        return (
            f"Source: native module progression "
            f"({requirement_count} completion requirement(s) in the course)"
        )
    return (
        "Source: submissions (no module completion requirements are set, so "
        "Canvas has no native progress data for this course)"
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_of_instant(as_of: str | None, tz) -> datetime:
    if not as_of:
        return datetime.now(timezone.utc)
    parsed = _parse_iso(to_canvas_datetime(as_of, tz))
    if parsed is None:  # pragma: no cover — to_canvas_datetime already validates
        raise ValueError(f"Could not parse --as-of {as_of!r}.")
    return parsed


def _expected_steps(steps: list[dict], at: datetime) -> int:
    """How many steps were due by `at`. Steps with no due date never count."""
    count = 0
    for step in steps:
        if step["step"] is None:
            continue
        due = _parse_iso(step["due_at"])
        if due and due <= at:
            count += 1
    return count


def _evaluate(steps: list[dict], done_ids: set[int], expected: int) -> dict:
    """Roll a set of completed item ids up into per-step rows and summary stats."""
    rows: list[dict] = []
    reached = 0
    completed = 0
    items_done = 0
    items_total = 0

    for step in steps:
        n_total = len(step["items"])
        n_done = sum(1 for item in step["items"] if item["item_id"] in done_ids)
        items_total += n_total
        items_done += n_done

        if step["step"] is None:
            status = "—"
        elif n_done == 0:
            status = "not started"
        elif n_done == n_total:
            status = "complete"
            completed += 1
        else:
            status = "partial"

        if step["step"] is not None and n_done:
            reached = step["step"]

        rows.append(
            {
                **step,
                "step_label": str(step["step"]) if step["step"] else "—",
                "done": n_done,
                "total": n_total,
                "done_label": f"{n_done}/{n_total}" if n_total else "—",
                "status": status,
            }
        )

    total = _total_steps(steps)
    return {
        "rows": rows,
        "reached": reached,
        "completed": completed,
        "steps_total": total,
        "items_done": items_done,
        "items_total": items_total,
        "pct": (100.0 * completed / total) if total else 0.0,
        "expected": expected,
        # No steps due yet means nobody can be behind: pace is undefined, not 0.
        "pace": (completed / expected) if expected else None,
        "on_pace": completed >= expected,
    }


def _decorate_dates(rows: list[dict], tz) -> None:
    for row in rows:
        row["due_local"] = local_day(row.get("due_at"), tz) if row.get("due_at") else ""


def _bar(value: int, largest: int, width: int = 30) -> str:
    if largest <= 0 or value <= 0:
        return ""
    return "█" * max(1, round(width * value / largest))


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.0f}%" if whole else "—"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


@app.command("student")
def student_progress(
    user: str = typer.Option(
        ..., "--user", help="Canvas user id, SIS id, login, or part of the name"
    ),
    course: str = typer.Option(None, "-c", "--course"),
    source: str = typer.Option(
        "auto", "--source", help="auto | native | submissions"
    ),
    as_of: str = typer.Option(
        None, "--as-of", help="Pace baseline date (YYYY-MM-DD). Defaults to now."
    ),
    state: str = typer.Option(
        "active", "--state", help="Comma-separated enrollment states to search"
    ),
    tz: str = typer.Option(None, "--tz", help="Timezone for displayed dates"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Show one student's progress through the module sequence."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        zone = resolve_timezone(tz)

        modules = _fetch_modules(client, cid)
        if not modules:
            emit("This course has no modules — nothing to track progress against.")
            return

        requirement_count = _count_requirements(modules)
        resolved = _resolve_source(source, requirement_count)

        states = [s.strip() for s in state.split(",") if s.strip()]
        students = _fetch_students(client, cid, states)
        enrollment = _resolve_student(students, user)
        uid = enrollment["user_id"]
        person = enrollment.get("user") or {}

        maps = None if resolved == "native" else _assignment_lookup(client, cid)
        steps = _build_steps(modules, maps, use_requirements=(resolved == "native"))
        if not _total_steps(steps):
            emit(
                "No trackable items found in this course's modules.\n"
                + _source_note(resolved, requirement_count)
            )
            return

        if resolved == "native":
            done_ids = _done_ids_native(client, cid, uid)
        else:
            subs = _submissions_by_user(client, cid, [uid]).get(uid, {})
            done_ids = _done_ids_from_submissions(steps, subs)

        at = _as_of_instant(as_of, zone)
        result = _evaluate(steps, done_ids, _expected_steps(steps, at))
        _decorate_dates(result["rows"], zone)

        payload = {
            "course_id": cid,
            "user_id": uid,
            "name": person.get("name"),
            "login_id": person.get("login_id"),
            "sis_user_id": person.get("sis_user_id"),
            "source": resolved,
            "as_of": at.isoformat(),
            "last_activity_at": enrollment.get("last_activity_at"),
            "reached": result["reached"],
            "completed": result["completed"],
            "steps_total": result["steps_total"],
            "expected": result["expected"],
            "pace": result["pace"],
            "steps": [
                {
                    k: row[k]
                    for k in (
                        "step",
                        "module_id",
                        "name",
                        "due_at",
                        "done",
                        "total",
                        "status",
                    )
                }
                for row in result["rows"]
            ],
        }

        if output == "json":
            emit(format_output(payload, [], "json"))
            return
        if output == "csv":
            emit(format_output(result["rows"], STEP_COLUMNS, "csv"))
            return

        emit(f"{person.get('name', uid)} — {person.get('login_id') or uid}")
        emit(_source_note(resolved, requirement_count))
        emit("")
        emit(format_output(result["rows"], STEP_COLUMNS, "table"))
        emit("")
        pace_text = (
            "n/a (nothing due yet)"
            if result["pace"] is None
            else f"{result['pace']:.2f}  "
            + ("(on pace)" if result["on_pace"] else "(behind)")
        )
        for label, value in (
            ("Reached", f"step {result['reached']} of {result['steps_total']}"),
            (
                "Completed",
                f"{result['completed']} of {result['steps_total']} steps "
                f"({result['pct']:.0f}%)",
            ),
            ("Steps due as of --as-of", f"{result['expected']}"),
            ("Pace", pace_text),
            (
                "Last activity",
                local_day(enrollment.get("last_activity_at"), zone) or "never",
            ),
        ):
            emit(f"  {label:<24} {value}")
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("course")
def course_progress(
    course: str = typer.Option(None, "-c", "--course"),
    source: str = typer.Option(
        "auto", "--source", help="auto | native | submissions"
    ),
    as_of: str = typer.Option(
        None, "--as-of", help="Pace baseline date (YYYY-MM-DD). Defaults to now."
    ),
    section: str = typer.Option(
        None, "--section", help="Limit to one section (id or name substring)"
    ),
    state: str = typer.Option(
        "active", "--state", help="Comma-separated enrollment states to include"
    ),
    tz: str = typer.Option(None, "--tz", help="Timezone for displayed dates"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Aggregate progress across every student — the view Canvas doesn't ship."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        zone = resolve_timezone(tz)

        modules = _fetch_modules(client, cid)
        if not modules:
            emit("This course has no modules — nothing to track progress against.")
            return

        requirement_count = _count_requirements(modules)
        resolved = _resolve_source(source, requirement_count)

        states = [s.strip() for s in state.split(",") if s.strip()]
        students = _fetch_students(client, cid, states, section)
        if not students:
            emit("No student enrollments matched.")
            return

        maps = None if resolved == "native" else _assignment_lookup(client, cid)
        steps = _build_steps(modules, maps, use_requirements=(resolved == "native"))
        total_steps = _total_steps(steps)
        if not total_steps:
            emit(
                "No trackable items found in this course's modules.\n"
                + _source_note(resolved, requirement_count)
            )
            return

        at = _as_of_instant(as_of, zone)
        expected = _expected_steps(steps, at)

        if resolved == "native":
            err_console.print(
                f"[yellow]NOTE:[/yellow] the native source costs one request per "
                f"student ({len(students)} here). --source submissions needs ~10 "
                "for the whole course."
            )
            per_student = {
                e["user_id"]: _done_ids_native(client, cid, e["user_id"])
                for e in students
            }
        else:
            subs = _submissions_by_user(client, cid)
            per_student = {
                e["user_id"]: _done_ids_from_submissions(
                    steps, subs.get(e["user_id"], {})
                )
                for e in students
            }

        results = {
            e["user_id"]: _evaluate(steps, per_student[e["user_id"]], expected)
            for e in students
        }

        # Per-step rollup.
        step_rows: list[dict] = []
        for index, step in enumerate(steps):
            n_complete = n_started = 0
            for res in results.values():
                row = res["rows"][index]
                if row["total"] and row["done"] == row["total"]:
                    n_complete += 1
                elif row["done"]:
                    n_started += 1
            step_rows.append(
                {
                    **step,
                    "step_label": str(step["step"]) if step["step"] else "—",
                    "n_complete": n_complete if step["step"] else "—",
                    "n_started": n_started if step["step"] else "—",
                    "pct": (100.0 * n_complete / len(students)) if step["step"] else None,
                    "pct_label": _pct(n_complete, len(students)) if step["step"] else "—",
                }
            )
        _decorate_dates(step_rows, zone)

        completed_counts = [r["completed"] for r in results.values()]
        reached_counts = [r["reached"] for r in results.values()]
        distribution = {
            n: sum(1 for c in reached_counts if c == n) for n in range(total_steps + 1)
        }
        never_active = sum(1 for e in students if not e.get("last_activity_at"))
        not_started = sum(1 for c in reached_counts if c == 0)
        on_pace = sum(1 for r in results.values() if r["on_pace"])
        finished = sum(1 for c in completed_counts if c == total_steps)

        summary = {
            "course_id": cid,
            "students": len(students),
            "source": resolved,
            "as_of": at.isoformat(),
            "steps_total": total_steps,
            "expected_steps": expected,
            "mean_completed": round(mean(completed_counts), 2),
            "median_completed": median(completed_counts),
            "mean_reached": round(mean(reached_counts), 2),
            "mean_completion_pct": round(
                100.0 * mean(completed_counts) / total_steps, 1
            ),
            "pace_index": round(mean(completed_counts) / expected, 2)
            if expected
            else None,
            "on_pace_students": on_pace,
            "behind_students": len(students) - on_pace,
            "not_started_students": not_started,
            "finished_students": finished,
            "never_active_students": never_active,
            "reached_distribution": distribution,
        }

        if output == "json":
            emit(
                format_output(
                    {
                        **summary,
                        "steps": [
                            {
                                "step": r["step"],
                                "module_id": r["module_id"],
                                "name": r["name"],
                                "due_at": r["due_at"],
                                "complete": r["n_complete"],
                                "started": r["n_started"],
                                "pct": r["pct"],
                            }
                            for r in step_rows
                        ],
                    },
                    [],
                    "json",
                )
            )
            return
        if output == "csv":
            emit(format_output(step_rows, COURSE_STEP_COLUMNS, "csv"))
            return

        emit(f"Course {cid} — progress across {len(students)} student(s)")
        emit(_source_note(resolved, requirement_count))
        emit(f"As of {local_day(at.isoformat(), zone)}")
        emit("")
        emit("Per-step completion")
        emit(format_output(step_rows, COURSE_STEP_COLUMNS, "table"))
        emit("")
        emit("Furthest step reached")
        largest = max(distribution.values()) if distribution else 0
        for n, count in distribution.items():
            label = "0 (none)" if n == 0 else str(n)
            emit(
                f"  {label:>9}  {count:>4}  {_pct(count, len(students)):>4}  "
                f"{_bar(count, largest)}"
            )
        emit("")
        emit("Momentum")
        pace_text = (
            "n/a (nothing due yet)"
            if summary["pace_index"] is None
            else f"{summary['pace_index']}  "
            + ("(class on pace)" if summary["pace_index"] >= 1 else "(class behind)")
        )
        for label, value in (
            (
                "Mean steps completed",
                f"{summary['mean_completed']} of {total_steps}  "
                f"({summary['mean_completion_pct']}%)",
            ),
            ("Median steps completed", f"{summary['median_completed']:g}"),
            ("Mean furthest reached", f"{summary['mean_reached']}"),
            ("Steps due as of --as-of", f"{expected}"),
            ("Pace index", pace_text),
            ("On pace or ahead", f"{on_pace}  ({_pct(on_pace, len(students))})"),
            (
                "Behind",
                f"{len(students) - on_pace}  "
                f"({_pct(len(students) - on_pace, len(students))})",
            ),
            (
                "Never started",
                f"{not_started}  ({_pct(not_started, len(students))})",
            ),
            (
                f"Finished all {total_steps} steps",
                f"{finished}  ({_pct(finished, len(students))})",
            ),
            ("No Canvas activity ever", f"{never_active}"),
        ):
            emit(f"  {label:<24} {value}")
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("export")
def export_progress(
    course: str = typer.Option(None, "-c", "--course"),
    source: str = typer.Option(
        "auto", "--source", help="auto | native | submissions"
    ),
    as_of: str = typer.Option(
        None, "--as-of", help="Pace baseline date (YYYY-MM-DD). Defaults to now."
    ),
    section: str = typer.Option(
        None, "--section", help="Limit to one section (id or name substring)"
    ),
    state: str = typer.Option(
        "active", "--state", help="Comma-separated enrollment states to include"
    ),
    behind_only: bool = typer.Option(
        False, "--behind-only", help="Only students below the pace baseline"
    ),
    tz: str = typer.Option(None, "--tz", help="Timezone for displayed dates"),
    output: str = typer.Option("csv", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """One row per student, one column per step — the dashboard feed."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        zone = resolve_timezone(tz)

        modules = _fetch_modules(client, cid)
        if not modules:
            emit("This course has no modules — nothing to track progress against.")
            return

        requirement_count = _count_requirements(modules)
        resolved = _resolve_source(source, requirement_count)

        states = [s.strip() for s in state.split(",") if s.strip()]
        students = _fetch_students(client, cid, states, section)
        if not students:
            emit("No student enrollments matched.")
            return

        maps = None if resolved == "native" else _assignment_lookup(client, cid)
        steps = _build_steps(modules, maps, use_requirements=(resolved == "native"))
        total_steps = _total_steps(steps)
        if not total_steps:
            emit(
                "No trackable items found in this course's modules.\n"
                + _source_note(resolved, requirement_count)
            )
            return

        at = _as_of_instant(as_of, zone)
        expected = _expected_steps(steps, at)

        if resolved == "submissions":
            subs = _submissions_by_user(client, cid)

        rows: list[dict] = []
        for enrollment in students:
            uid = enrollment["user_id"]
            person = enrollment.get("user") or {}
            if resolved == "native":
                done_ids = _done_ids_native(client, cid, uid)
            else:
                done_ids = _done_ids_from_submissions(steps, subs.get(uid, {}))
            result = _evaluate(steps, done_ids, expected)
            if behind_only and result["on_pace"]:
                continue

            row = {
                "user_id": uid,
                "name": person.get("name"),
                "sortable_name": person.get("sortable_name"),
                "login_id": person.get("login_id"),
                "sis_user_id": person.get("sis_user_id"),
                "reached": result["reached"],
                "completed": result["completed"],
                "steps_total": total_steps,
                "pct": round(result["pct"], 1),
                "expected": expected,
                "pace": result["pace"],
                "on_pace": result["on_pace"],
                "last_activity_at": enrollment.get("last_activity_at"),
                "last_activity_local": local_day(
                    enrollment.get("last_activity_at"), zone
                ),
                "total_activity_seconds": enrollment.get("total_activity_time"),
            }
            for step_row in result["rows"]:
                if step_row["step"] is None:
                    continue
                row[f"step_{step_row['step']}"] = step_row["status"]
            rows.append(row)

        columns = [
            ("User ID", "user_id"),
            ("Name", "sortable_name"),
            ("Login", "login_id"),
            ("SIS ID", "sis_user_id"),
            ("Reached", "reached"),
            ("Completed", "completed"),
            ("Of", "steps_total"),
            ("%", "pct"),
            ("Expected", "expected"),
            ("On Pace", "on_pace"),
            ("Last Activity", "last_activity_local"),
        ] + [(f"Step {n}", f"step_{n}") for n in range(1, total_steps + 1)]

        if output != "json":
            err_console.print(f"[dim]{_source_note(resolved, requirement_count)}[/dim]")
        emit(format_output(rows, columns, output))
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)
