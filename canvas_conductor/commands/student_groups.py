"""student-groups: build course-level student groups from a CSV roster.

Reads a CSV with one row per (student, group) assignment and idempotently
creates the matching group categories, groups, and memberships in a Canvas
course. Useful for any course that wants to pre-assign students to project
teams, lab pairs, peer-review pods, etc.

Sample CSV (`projects.csv`) — column order is irrelevant, headers can be in
any case:

    sis_user_id,group_name,category
    123456789,Team Alpha,Project Teams
    234567890,Team Alpha,Project Teams
    345678901,Team Beta,Project Teams
    456789012,Team Beta,Project Teams
    567890123,Team Gamma,Project Teams
    678901234,Team Gamma,Project Teams
    123456789,Pair 1,Lab Pairs
    234567890,Pair 1,Lab Pairs
    345678901,Pair 2,Lab Pairs
    456789012,Pair 2,Lab Pairs

Then:

    conductor student-groups apply -c mycourse -f projects.csv

Identifier column — any one of (first match wins):

    user_id        Canvas internal user id (no prefix)
    sis_user_id    SIS user id (sent as `sis_user_id:VALUE`)
    login_id       Canvas login id (sent as `sis_login_id:VALUE`)
    sis_login_id   Same as login_id
    email          Same as login_id (BYU/many schools use email as login)

Required columns: an identifier column (above) and `group_name`.
Optional: a `category` column. If absent, --category supplies the default.

Notes
-----
- The same user can appear in groups across different *categories* (each
  category is independent), but Canvas typically enforces at most one
  group per user *within* a category. Conflicting rows will surface as
  Canvas validation errors and are reported, not raised.
- Memberships are deduplicated against existing course state so re-runs
  are safe.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

import typer

from canvas_conductor.client import get_client
from canvas_conductor.commands._common import (
    confirm_or_abort,
    emit,
    handle_canvas_error,
)
from canvas_conductor.config import get_course_id
from canvas_conductor.utils.output import format_output


app = typer.Typer(
    name="student-groups",
    help="Manage course-level student groups (project teams, lab pairs, peer-review pods, etc.).",
    no_args_is_help=True,
)


# Identifier columns and the prefix Canvas's `user_id` parameter wants.
# Listed in priority order — first match in the CSV header wins.
_ID_COLUMN_FORMATS: list[tuple[str, str]] = [
    ("user_id",      ""),                 # Canvas internal user id
    ("sis_user_id",  "sis_user_id:"),     # SIS user id
    ("login_id",     "sis_login_id:"),    # Canvas login id
    ("sis_login_id", "sis_login_id:"),
    ("email",        "sis_login_id:"),    # convention at many schools
]


# ----------------------------------------------------------------------------
# CSV parsing
# ----------------------------------------------------------------------------

def _detect_id_column(fieldnames: list[str]) -> tuple[str, str]:
    """Return (column_name, canvas_prefix) for the first recognised ID column."""
    by_lower = {f.lower(): f for f in fieldnames}
    for name, prefix in _ID_COLUMN_FORMATS:
        if name in by_lower:
            return by_lower[name], prefix
    raise typer.BadParameter(
        "CSV is missing an identifier column. Add one of: "
        + ", ".join(c for c, _ in _ID_COLUMN_FORMATS)
    )


def _parse_csv(
    path: Path, default_category: str | None,
) -> dict[str, dict[str, list[str]]]:
    """Parse the CSV into {category: {group_name: [prefixed_user_id, ...]}}.

    Skips rows missing required fields. Header matching is case-insensitive.
    """
    if not path.is_file():
        raise typer.BadParameter(f"CSV not found: {path}")

    plan: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    # utf-8-sig strips a BOM that Excel-exported CSVs often carry.
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise typer.BadParameter("CSV has no header row.")
        lower_headers = {f.lower(): f for f in reader.fieldnames}

        if "group_name" not in lower_headers:
            raise typer.BadParameter("CSV is missing required column: group_name")
        id_col, id_prefix = _detect_id_column(reader.fieldnames)
        group_col = lower_headers["group_name"]
        category_col = lower_headers.get("category")

        for row_num, row in enumerate(reader, start=2):  # row 1 is header
            uid = (row.get(id_col) or "").strip()
            group_name = (row.get(group_col) or "").strip()
            if not uid or not group_name:
                continue
            if category_col:
                category = (row.get(category_col) or "").strip() or default_category
            else:
                category = default_category
            if not category:
                raise typer.BadParameter(
                    f"Row {row_num}: no category column and no --category default."
                )
            plan[category][group_name].append(f"{id_prefix}{uid}")

    return dict(plan)


# ----------------------------------------------------------------------------
# Canvas helpers (find-or-create + dedupe lookups)
# ----------------------------------------------------------------------------

def _find_or_create_category(
    client,
    course_id: int,
    name: str,
    self_signup: bool,
    group_limit: int | None,
) -> tuple[dict, bool]:
    """Return (category, was_created)."""
    cats = client.get_all(f"/courses/{course_id}/group_categories")
    existing = next((c for c in cats if c.get("name") == name), None)
    if existing:
        return existing, False
    payload: dict[str, Any] = {"name": name}
    if self_signup:
        payload["self_signup"] = "enabled"
    if group_limit is not None:
        payload["group_limit"] = group_limit
    new = client.post(f"/courses/{course_id}/group_categories", data=payload)
    return new, True


def _find_or_create_group(
    client, category_id: int, name: str,
) -> tuple[dict, bool]:
    """Return (group, was_created)."""
    groups = client.get_all(f"/group_categories/{category_id}/groups")
    existing = next((g for g in groups if g.get("name") == name), None)
    if existing:
        return existing, False
    new = client.post(f"/group_categories/{category_id}/groups", data={"name": name})
    return new, True


def _existing_member_keys(client, group_id: int) -> set[str]:
    """Return a set of identity strings for everyone already in the group.

    Each user can match a CSV reference under any of three representations
    (canvas id, sis_user_id, login_id) so callers can dedupe regardless of
    which column the CSV used.
    """
    users = client.get_all(f"/groups/{group_id}/users")
    keys: set[str] = set()
    for u in users:
        if u.get("id") is not None:
            keys.add(str(u["id"]))
        if u.get("sis_user_id"):
            keys.add(f"sis_user_id:{u['sis_user_id']}")
        if u.get("login_id"):
            keys.add(f"sis_login_id:{u['login_id']}")
    return keys


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

@app.command("apply")
def apply(
    file: str = typer.Option(..., "--file", "-f", help="Path to the roster CSV."),
    course: str = typer.Option(None, "-c", "--course"),
    category: str = typer.Option(
        None, "--category",
        help="Default category name when the CSV has no `category` column.",
    ),
    self_signup: bool = typer.Option(
        False, "--self-signup",
        help="Allow student self-signup when creating new categories.",
    ),
    group_limit: int = typer.Option(
        None, "--group-limit",
        help="Maximum members per group (applied to newly created categories).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run",
                                  help="Preview the plan without touching Canvas."),
    yes: bool = typer.Option(False, "-y", "--yes",
                              help="Skip the confirmation prompt."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Apply a CSV-driven group roster to a course.

    Idempotent: existing categories, groups, and memberships are reused; only
    missing pieces are created. Re-runs are safe.
    """
    try:
        cid = get_course_id(course)
        plan = _parse_csv(Path(file), default_category=category)

        # Plan summary
        n_cats = len(plan)
        n_groups = sum(len(gs) for gs in plan.values())
        n_rows = sum(len(m) for gs in plan.values() for m in gs.values())
        emit(
            f"Plan: {n_rows} membership row(s) across {n_groups} group(s) "
            f"in {n_cats} categor{'ies' if n_cats != 1 else 'y'}."
        )
        for cat_name, groups in sorted(plan.items()):
            emit(f"  [{cat_name}]")
            for g_name, members in sorted(groups.items()):
                emit(f"    {g_name} ({len(members)} member"
                     f"{'s' if len(members) != 1 else ''})")

        if dry_run:
            emit("\nDry-run. Re-run without --dry-run to apply.")
            return

        confirm_or_abort(
            f"Apply roster to course {cid}?",
            yes=yes, dry_run=False,
        )

        client = get_client(verbose=verbose)

        counts = {
            "categories_created": 0,
            "groups_created":     0,
            "memberships_created": 0,
            "memberships_skipped": 0,
        }
        failures: list[str] = []

        for cat_name, groups in plan.items():
            cat, cat_was_new = _find_or_create_category(
                client, cid, cat_name, self_signup, group_limit,
            )
            if cat_was_new:
                counts["categories_created"] += 1
                emit(f"  + category created: {cat_name}")
            cat_id = cat["id"]

            for g_name, members in groups.items():
                grp, grp_was_new = _find_or_create_group(client, cat_id, g_name)
                if grp_was_new:
                    counts["groups_created"] += 1
                    emit(f"    + group created: {g_name}")
                gid = grp["id"]

                existing = _existing_member_keys(client, gid)
                for user_ref in members:
                    if user_ref in existing:
                        counts["memberships_skipped"] += 1
                        continue
                    try:
                        client.post(
                            f"/groups/{gid}/memberships",
                            data={"user_id": user_ref},
                        )
                        counts["memberships_created"] += 1
                        existing.add(user_ref)
                    except Exception as exc:
                        failures.append(
                            f"{cat_name} / {g_name} / {user_ref}: {exc}"
                        )

        emit("\nDone.")
        emit(f"  categories created:   {counts['categories_created']}")
        emit(f"  groups created:       {counts['groups_created']}")
        emit(f"  memberships created:  {counts['memberships_created']}")
        emit(f"  memberships skipped:  {counts['memberships_skipped']} (already present)")
        if failures:
            emit(f"  failures:             {len(failures)}")
            for f in failures[:10]:
                emit(f"    - {f}")
            if len(failures) > 10:
                emit(f"    ... and {len(failures) - 10} more")
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("categories")
def list_categories(
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """List group categories on a course."""
    try:
        cid = get_course_id(course)
        client = get_client(verbose=verbose)
        cats = client.get_all(f"/courses/{cid}/group_categories")
        cols = [
            ("ID", "id"),
            ("Name", "name"),
            ("Self Signup", "self_signup"),
            ("Group Limit", "group_limit"),
            ("Groups", "group_count"),
        ]
        emit(format_output(cats, cols, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("list")
def list_groups(
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """List all groups on a course."""
    try:
        cid = get_course_id(course)
        client = get_client(verbose=verbose)
        groups = client.get_all(f"/courses/{cid}/groups")
        cols = [
            ("ID", "id"),
            ("Name", "name"),
            ("Category ID", "group_category_id"),
            ("Members", "members_count"),
        ]
        emit(format_output(groups, cols, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("members")
def list_members(
    group_id: int = typer.Option(..., "--group", help="Canvas group id."),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """List members of a group."""
    try:
        client = get_client(verbose=verbose)
        users = client.get_all(f"/groups/{group_id}/users")
        cols = [
            ("ID", "id"),
            ("Name", "name"),
            ("SIS User ID", "sis_user_id"),
            ("Login ID", "login_id"),
        ]
        emit(format_output(users, cols, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("auto-assign")
def auto_assign(
    category_id: int = typer.Option(..., "--category-id",
                                     help="Group category id (find via `categories`)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Have Canvas randomly distribute unassigned students across the
    category's groups. Returns a Progress object; Canvas runs the work
    asynchronously."""
    try:
        if dry_run:
            emit(f"DRY-RUN: would auto-assign unassigned students for category {category_id}")
            return
        confirm_or_abort(
            f"Auto-assign all unassigned students in category {category_id}?",
            yes=yes, dry_run=False,
        )
        client = get_client(verbose=verbose)
        result = client.post(
            f"/group_categories/{category_id}/assign_unassigned_members",
            data={},
        )
        if isinstance(result, dict):
            emit(f"Submitted. Progress url: {result.get('url') or result.get('id')}")
        else:
            emit("Submitted.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("delete-category")
def delete_category(
    category_id: int = typer.Option(..., "--id"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Delete a group category along with all its groups and memberships."""
    try:
        confirm_or_abort(
            f"Delete category {category_id} and all its groups?",
            yes=yes, dry_run=dry_run,
        )
        client = get_client(verbose=verbose)
        client.delete(f"/group_categories/{category_id}")
        emit(f"Deleted category {category_id}.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)
