"""assignment-overrides: per-group (or per-section/student) due-date overrides.

Canvas lets one assignment carry multiple "Assign to" cards, each with its
own due/unlock/lock date and its own audience (a course section, one or
more students, or -- for a group assignment -- one of the groups in its
group set). This module manages those cards ("assignment overrides") from
the CLI, with a CSV-driven bulk command for courses with many student
groups (e.g. a rotating-start assignment where each of 40+ groups needs
its own due date).

Sample CSV (`due_dates.csv`) -- column order is irrelevant, headers are
case-insensitive:

    group,due_at
    Team Alpha,2026-09-15
    Team Beta,2026-09-16
    12345,2026-09-17T23:59:00-06:00

`group` accepts either a group name (matched against the groups in the
assignment's group set) or a numeric Canvas group id. `due_at` accepts a
bare `YYYY-MM-DD` (anchored at `--at-time` in `--tz`) or a full ISO-8601
datetime. Optional columns: `unlock_at`, `lock_at`, `title` (defaults to
the group's name).

Then:

    conductor overrides bulk --id 12345 -c mycourse -f due_dates.csv --commit

Re-running `bulk` is safe: a group that already has an override on this
assignment gets that override *updated* rather than duplicated.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import typer

from ..client import get_client
from ..config import get_course_id
from ..utils.dates import DEFAULT_TIME_OF_DAY, local_day, resolve_timezone, to_canvas_datetime
from ..utils.output import format_output
from ._common import confirm_or_abort, emit, handle_canvas_error


app = typer.Typer(
    name="overrides",
    help="Manage per-group/section/student assignment due-date overrides.",
    no_args_is_help=True,
)


OVERRIDE_COLUMNS = [
    ("ID", "id"),
    ("Title", "title"),
    ("Group ID", "group_id"),
    ("Section ID", "course_section_id"),
    ("Students", "student_ids"),
    ("Due At", "due_at"),
    ("Unlock At", "unlock_at"),
    ("Lock At", "lock_at"),
]

PLAN_COLUMNS = [
    ("Group", "group_name"),
    ("Group ID", "group_id"),
    ("Action", "action"),
    ("Due (local)", "due_local"),
]


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _fetch_groups_for_assignment(client, cid: int, assignment_id: int) -> list[dict]:
    """Return the groups belonging to this assignment's group set.

    Raises ValueError if the assignment isn't a group assignment.
    """
    assignment = client.get(f"/courses/{cid}/assignments/{assignment_id}")
    category_id = assignment.get("group_category_id")
    if not category_id:
        raise ValueError(
            f"Assignment {assignment_id} has no group_category_id -- it isn't a "
            "group assignment. Set one first with `assignments update --id "
            f"{assignment_id} --group-category-id <id>`."
        )
    return client.get_all(f"/group_categories/{category_id}/groups")


def _resolve_group(ref: str, groups: list[dict]) -> dict:
    """Resolve a CSV `group` cell (numeric id or name) to a group dict."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("Empty group reference.")
    if ref.isdigit():
        match = next((g for g in groups if str(g.get("id")) == ref), None)
        if not match:
            raise ValueError(f"No group with id {ref} in this assignment's group set.")
        return match
    matches = [g for g in groups if (g.get("name") or "").strip().lower() == ref.lower()]
    if not matches:
        names = ", ".join(repr(g.get("name")) for g in groups) or "(none)"
        raise ValueError(f"No group named {ref!r}. Groups in this set: {names}")
    if len(matches) > 1:
        raise ValueError(f"Group name {ref!r} is ambiguous -- matches multiple groups.")
    return matches[0]


def _cell(row: dict, lower: dict[str, str], name: str) -> str:
    col = lower.get(name)
    if not col:
        return ""
    return (row.get(col) or "").strip()


def _parse_bulk_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise typer.BadParameter(f"CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise typer.BadParameter("CSV has no header row.")
        lower = {f.lower(): f for f in reader.fieldnames}
        if "group" not in lower:
            raise typer.BadParameter("CSV is missing required column: group")
        if "due_at" not in lower:
            raise typer.BadParameter("CSV is missing required column: due_at")

        rows: list[dict[str, str]] = []
        for row_num, row in enumerate(reader, start=2):  # row 1 is the header
            group_ref = _cell(row, lower, "group")
            due_raw = _cell(row, lower, "due_at")
            if not group_ref:
                continue
            if not due_raw:
                raise typer.BadParameter(
                    f"Row {row_num}: missing due_at for group {group_ref!r}."
                )
            rows.append(
                {
                    "group": group_ref,
                    "due_at": due_raw,
                    "unlock_at": _cell(row, lower, "unlock_at"),
                    "lock_at": _cell(row, lower, "lock_at"),
                    "title": _cell(row, lower, "title"),
                    "_row": row_num,
                }
            )
        return rows


# ----------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------

@app.command("list")
def list_overrides(
    assignment_id: int = typer.Option(..., "--id", help="Canvas assignment id."),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List the 'Assign to' overrides on an assignment."""
    try:
        cid = get_course_id(course)
        client = get_client(verbose=verbose)
        items = client.get_all(f"/courses/{cid}/assignments/{assignment_id}/overrides")
        emit(format_output(items, OVERRIDE_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("create")
def create_override(
    assignment_id: int = typer.Option(..., "--id", help="Canvas assignment id."),
    group: str = typer.Option(
        None, "--group", help="Group name or id (assignment must have a group_category_id)."
    ),
    section: int = typer.Option(None, "--section-id", help="Course section id."),
    student_ids: str = typer.Option(
        None, "--student-ids", help="Comma-separated Canvas user ids."
    ),
    title: str = typer.Option(None, "--title"),
    due_at: str = typer.Option(None, "--due-at", help="YYYY-MM-DD or full ISO-8601."),
    unlock_at: str = typer.Option(None, "--unlock-at"),
    lock_at: str = typer.Option(None, "--lock-at"),
    tz_name: str = typer.Option(None, "--tz"),
    at_time: str = typer.Option(DEFAULT_TIME_OF_DAY, "--at-time"),
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Create a single override (one 'Assign to' card) on an assignment."""
    try:
        targets = [
            name
            for name, value in (
                ("--group", group),
                ("--section-id", section),
                ("--student-ids", student_ids),
            )
            if value
        ]
        if len(targets) != 1:
            raise typer.BadParameter(
                "Pass exactly one of --group, --section-id, --student-ids "
                f"(got: {', '.join(targets) or 'none'})."
            )

        cid = get_course_id(course)
        client = get_client(verbose=verbose)
        zone = resolve_timezone(tz_name)

        payload: dict[str, Any] = {}
        if group:
            groups = _fetch_groups_for_assignment(client, cid, assignment_id)
            g = _resolve_group(group, groups)
            payload["group_id"] = g["id"]
            payload["title"] = title or g.get("name")
        elif section:
            payload["course_section_id"] = section
            if title:
                payload["title"] = title
        else:
            payload["student_ids"] = [
                int(s.strip()) for s in student_ids.split(",") if s.strip()
            ]
            if title:
                payload["title"] = title

        if due_at:
            payload["due_at"] = to_canvas_datetime(due_at, zone, at_time)
        if unlock_at:
            payload["unlock_at"] = to_canvas_datetime(unlock_at, zone, at_time)
        if lock_at:
            payload["lock_at"] = to_canvas_datetime(lock_at, zone, at_time)

        if dry_run:
            emit(
                f"DRY-RUN: POST /courses/{cid}/assignments/{assignment_id}/overrides "
                f"assignment_override={payload}"
            )
            return

        confirm_or_abort(
            f"Create override on assignment {assignment_id}?", yes=yes, dry_run=False
        )
        result = client.post(
            f"/courses/{cid}/assignments/{assignment_id}/overrides",
            data={"assignment_override": payload},
        )
        emit(format_output(result, [], "table"))
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        emit(f"ERROR: {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("bulk")
def bulk_create(
    assignment_id: int = typer.Option(..., "--id", help="Canvas assignment id."),
    file: str = typer.Option(
        ..., "--file", "-f", help="CSV: group,due_at[,unlock_at,lock_at,title]"
    ),
    course: str = typer.Option(None, "-c", "--course"),
    tz_name: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates (default: config/local)."
    ),
    at_time: str = typer.Option(
        DEFAULT_TIME_OF_DAY, "--at-time", help="Time of day for bare dates (HH:MM)."
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Create or update one due-date override per group from a CSV.

    Idempotent: a group that already has an override on this assignment gets
    that override updated (PUT) instead of duplicated (POST). Safe to re-run
    -- fix a typo'd date in the CSV and run it again.
    """
    try:
        cid = get_course_id(course)
        rows = _parse_bulk_csv(Path(file))
        if not rows:
            emit("CSV had no data rows -- nothing to do.")
            return

        client = get_client(verbose=verbose)
        zone = resolve_timezone(tz_name)
        groups = _fetch_groups_for_assignment(client, cid, assignment_id)
        existing = client.get_all(f"/courses/{cid}/assignments/{assignment_id}/overrides")
        existing_by_group = {o.get("group_id"): o for o in existing if o.get("group_id")}

        plan: list[dict] = []
        errors: list[str] = []
        for row in rows:
            try:
                g = _resolve_group(row["group"], groups)
            except ValueError as exc:
                errors.append(f"row {row['_row']}: {exc}")
                continue
            try:
                due_iso = to_canvas_datetime(row["due_at"], zone, at_time)
                unlock_iso = (
                    to_canvas_datetime(row["unlock_at"], zone, at_time)
                    if row["unlock_at"]
                    else None
                )
                lock_iso = (
                    to_canvas_datetime(row["lock_at"], zone, at_time)
                    if row["lock_at"]
                    else None
                )
            except ValueError as exc:
                errors.append(f"row {row['_row']} ({g.get('name')}): {exc}")
                continue

            current = existing_by_group.get(g["id"])
            payload: dict[str, Any] = {
                "title": row["title"] or g.get("name"),
                "group_id": g["id"],
                "due_at": due_iso,
            }
            if unlock_iso:
                payload["unlock_at"] = unlock_iso
            if lock_iso:
                payload["lock_at"] = lock_iso

            plan.append(
                {
                    "group_name": g.get("name"),
                    "group_id": g["id"],
                    "override_id": current.get("id") if current else None,
                    "action": "update" if current else "create",
                    "due_local": local_day(due_iso, zone),
                    "payload": payload,
                }
            )

        if errors:
            emit(
                f"{len(errors)} row(s) failed to validate -- fix these and re-run "
                "(nothing was sent to Canvas):"
            )
            for e in errors[:15]:
                emit(f"  - {e}")
            if len(errors) > 15:
                emit(f"  ... and {len(errors) - 15} more")
            raise typer.Exit(code=1)

        emit(format_output(plan, PLAN_COLUMNS, "table"))
        n_create = sum(1 for p in plan if p["action"] == "create")
        n_update = sum(1 for p in plan if p["action"] == "update")
        emit(
            f"\n{len(plan)} override(s): {n_create} new, {n_update} updating existing. "
            f"Timezone: {zone} -- bare dates anchored at {at_time}."
        )

        if dry_run:
            emit("\nDry-run. Re-run with --commit to apply.")
            return

        confirm_or_abort(
            f"Write {len(plan)} override(s) to assignment {assignment_id}?",
            yes=yes,
            dry_run=False,
        )

        created = updated = 0
        failures: list[str] = []
        for item in plan:
            try:
                if item["override_id"]:
                    client.put(
                        f"/courses/{cid}/assignments/{assignment_id}/overrides/"
                        f"{item['override_id']}",
                        data={"assignment_override": item["payload"]},
                    )
                    updated += 1
                else:
                    client.post(
                        f"/courses/{cid}/assignments/{assignment_id}/overrides",
                        data={"assignment_override": item["payload"]},
                    )
                    created += 1
            except Exception as exc:
                failures.append(f"{item['group_name']}: {exc}")

        emit("\nDone.")
        emit(f"  created: {created}")
        emit(f"  updated: {updated}")
        if failures:
            emit(f"  failures: {len(failures)}")
            for f in failures[:10]:
                emit(f"    - {f}")
            if len(failures) > 10:
                emit(f"    ... and {len(failures) - 10} more")
            raise typer.Exit(code=1)
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        emit(f"ERROR: {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("delete")
def delete_override(
    assignment_id: int = typer.Option(..., "--id", help="Canvas assignment id."),
    override_id: int = typer.Option(..., "--override-id"),
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Delete a single override."""
    try:
        cid = get_course_id(course)
        confirm_or_abort(
            f"Delete override {override_id} on assignment {assignment_id}?",
            yes=yes,
            dry_run=dry_run,
        )
        client = get_client(verbose=verbose)
        client.delete(
            f"/courses/{cid}/assignments/{assignment_id}/overrides/{override_id}"
        )
        emit(f"Deleted override {override_id}.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)
