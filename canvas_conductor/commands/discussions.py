"""Discussion topic commands: list, create, update, delete.

Ungraded discussion topics can carry a student To-Do date, same as pages.
Note the field name differs: topics take (and return) `todo_date` directly,
whereas pages are written with `student_todo_at`. A *graded* topic drives
the to-do list from its assignment's due date instead, and Canvas rejects
`todo_date` on one.
"""
from __future__ import annotations

import typer

from ..client import get_client
from ..config import get_course_id
from ..utils.dates import CLEAR, local_day, resolve_timezone, to_canvas_datetime
from ..utils.output import format_output
from ._common import confirm_or_abort, emit, err_console, handle_canvas_error

app = typer.Typer(name="discussions", help="Manage discussion topics")


DISCUSSION_COLUMNS = [
    ("ID", "id"),
    ("Title", "title"),
    ("Published", "published"),
    ("To-Do", "todo_local"),
    ("Pinned", "pinned"),
    ("Type", "discussion_type"),
    ("Posted", "posted_at"),
]


def _with_local_todo(topics: list[dict], tz) -> list[dict]:
    """Attach a human-readable `todo_local` for table/CSV display."""
    for topic in topics:
        if isinstance(topic, dict):  # a 204 from Canvas yields None
            topic["todo_local"] = local_day(topic.get("todo_date"), tz)
    return topics


def _resolve_todo(
    todo: str | None, clear_todo: bool, tz_name: str | None, at_time: str
) -> str | None:
    """Turn `--todo` / `--clear-todo` into a payload value (None = leave alone)."""
    if todo and clear_todo:
        raise typer.BadParameter("Pass either --todo or --clear-todo, not both.")
    if clear_todo:
        return CLEAR
    if todo:
        return to_canvas_datetime(todo, resolve_timezone(tz_name), at_time)
    return None


@app.command("list")
def list_discussions(
    course: str = typer.Option(None, "-c", "--course"),
    search: str = typer.Option(None, "--search"),
    scope: str = typer.Option(None, "--scope", help="locked, unlocked, pinned, unpinned"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List discussion topics."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        params: dict = {}
        if search:
            params["search_term"] = search
        if scope:
            params["scope"] = scope
        items = client.get_all(f"/courses/{cid}/discussion_topics", params=params)
        if output != "json":
            items = _with_local_todo(items, resolve_timezone())
        emit(format_output(items, DISCUSSION_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("create")
def create_discussion(
    title: str = typer.Option(..., "--title"),
    course: str = typer.Option(None, "-c", "--course"),
    message: str = typer.Option(None, "--message"),
    discussion_type: str = typer.Option(
        None, "--type", help="side_comment, threaded"
    ),
    published: bool = typer.Option(False, "--published"),
    pinned: bool = typer.Option(False, "--pinned"),
    todo: str = typer.Option(
        None,
        "--todo",
        help="Add to students' To-Do list on this date (ungraded topics only)",
    ),
    at_time: str = typer.Option(
        "23:59", "--at-time", help="Time of day for a bare --todo date (HH:MM)"
    ),
    tz: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates (default: config or local)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Create a discussion topic."""
    try:
        cid = get_course_id(course)
        payload = {
            "title": title,
            "message": message,
            "discussion_type": discussion_type,
            "published": published,
            "pinned": pinned,
            "todo_date": _resolve_todo(todo, False, tz, at_time),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        if todo and not published:
            err_console.print(
                "[yellow]NOTE:[/yellow] the topic is unpublished, so its to-do "
                "date stays invisible to students until you publish it."
            )
        if dry_run:
            emit(f"DRY-RUN: POST /courses/{cid}/discussion_topics payload={payload}")
            return
        client = get_client(verbose=verbose)
        result = client.post(f"/courses/{cid}/discussion_topics", data=payload)
        emit(
            format_output(
                _with_local_todo([result], resolve_timezone(tz)),
                DISCUSSION_COLUMNS,
                "table",
            )
        )
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("update")
def update_discussion(
    discussion_id: int = typer.Option(..., "--id"),
    course: str = typer.Option(None, "-c", "--course"),
    title: str = typer.Option(None, "--title"),
    message: str = typer.Option(None, "--message"),
    published: bool = typer.Option(None, "--published"),
    pinned: bool = typer.Option(None, "--pinned"),
    todo: str = typer.Option(
        None,
        "--todo",
        help="Add to students' To-Do list on this date (ungraded topics only)",
    ),
    clear_todo: bool = typer.Option(
        False, "--clear-todo", help="Remove the topic from students' To-Do list"
    ),
    at_time: str = typer.Option(
        "23:59", "--at-time", help="Time of day for a bare --todo date (HH:MM)"
    ),
    tz: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates (default: config or local)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Update a discussion topic."""
    try:
        cid = get_course_id(course)
        payload = {
            "title": title,
            "message": message,
            "published": published,
            "pinned": pinned,
            "todo_date": _resolve_todo(todo, clear_todo, tz, at_time),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        if not payload:
            emit("No fields supplied — nothing to update.")
            return
        if dry_run:
            emit(
                f"DRY-RUN: PUT /courses/{cid}/discussion_topics/{discussion_id} "
                f"payload={payload}"
            )
            return
        client = get_client(verbose=verbose)
        result = client.put(
            f"/courses/{cid}/discussion_topics/{discussion_id}", payload
        )
        emit(
            format_output(
                _with_local_todo([result], resolve_timezone(tz)),
                DISCUSSION_COLUMNS,
                "table",
            )
        )
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("delete")
def delete_discussion(
    discussion_id: int = typer.Option(..., "--id"),
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Delete a discussion topic."""
    try:
        cid = get_course_id(course)
        confirm_or_abort(
            f"Delete discussion {discussion_id}?", yes=yes, dry_run=dry_run
        )
        client = get_client(verbose=verbose)
        client.delete(f"/courses/{cid}/discussion_topics/{discussion_id}")
        emit(f"Deleted discussion {discussion_id}.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)
