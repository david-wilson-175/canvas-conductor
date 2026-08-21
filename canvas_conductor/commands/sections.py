"""Section commands: list, crosslist, uncrosslist.

Cross-listing is how Canvas "merges" course sections: a section is moved
out of its current course shell and into a destination shell, carrying its
enrollments with it. Students in every cross-listed section then appear in
one combined course while remaining separable by section for grading, due
dates, and announcements. Nothing is copied or deleted — the emptied source
shells stay behind, and `uncrosslist` puts a section back.
"""
from __future__ import annotations

import typer

from ..client import get_client
from ..config import get_course_id
from ..exceptions import CanvasError
from ..utils.output import format_output
from ._common import confirm_or_abort, emit, err_console, handle_canvas_error

app = typer.Typer(name="sections", help="Manage course sections (list, cross-list)")


SECTION_COLUMNS = [
    ("ID", "id"),
    ("Name", "name"),
    ("SIS ID", "sis_section_id"),
    ("Students", "total_students"),
    ("Course", "course_id"),
]

PLAN_COLUMNS = [
    ("Section", "id"),
    ("Name", "name"),
    ("From", "course_id"),
    ("Students", "total_students"),
    ("Graded", "graded"),
]


def _fetch_section(client, section_id: int) -> dict:
    """Fetch one section with its student count."""
    return client.get(
        f"/sections/{section_id}", params={"include[]": "total_students"}
    )


def _count_graded(client, section_id: int) -> str:
    """Count graded submissions in a section's *current* course.

    Returns a display string — a count, or "?" when the check could not run.
    A teacher token can read this for their own sections, but the endpoint
    403s for sections you don't teach; a failed safety check must not block
    the operation, so the failure is reported rather than raised.
    """
    try:
        submissions = client.get_all(
            f"/sections/{section_id}/students/submissions",
            params={"student_ids[]": "all", "workflow_state": "graded"},
        )
    except CanvasError:
        return "?"
    return str(len(submissions))


@app.command("list")
def list_sections(
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List sections in a course, with enrollment counts."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        items = client.get_all(
            f"/courses/{cid}/sections", params={"include[]": "total_students"}
        )
        emit(format_output(items, SECTION_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("crosslist")
def crosslist_sections(
    ids: str = typer.Option(
        None, "--ids", help="Comma-separated section IDs to cross-list"
    ),
    from_course: str = typer.Option(
        None,
        "--from",
        help="Absorb every section from this configured course instead of --ids",
    ),
    course: str = typer.Option(
        None, "-c", "--course", help="Destination course (the combined shell)"
    ),
    skip_checks: bool = typer.Option(
        False, "--skip-checks", help="Skip the graded-submission pre-flight check"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Cross-list sections into one course so all their students share a shell."""
    try:
        if bool(ids) == bool(from_course):
            raise typer.BadParameter("Pass exactly one of --ids or --from.")

        client = get_client(verbose=verbose)
        dest_cid = get_course_id(course)

        if from_course:
            src_cid = get_course_id(from_course)
            if src_cid == dest_cid:
                raise typer.BadParameter(
                    f"--from and --course both resolve to course {dest_cid}."
                )
            sections = client.get_all(
                f"/courses/{src_cid}/sections", params={"include[]": "total_students"}
            )
        else:
            section_ids = [int(x) for x in ids.split(",") if x.strip()]
            sections = [_fetch_section(client, sid) for sid in section_ids]

        # Sections already in the destination need no work; silently skipping
        # them keeps `--from` re-runnable after a partial failure.
        plan = [s for s in sections if s.get("course_id") != dest_cid]
        skipped = len(sections) - len(plan)
        if skipped:
            err_console.print(
                f"[yellow]NOTE:[/yellow] {skipped} section(s) already in the "
                "destination course; skipping."
            )
        if not plan:
            emit("Nothing to cross-list.")
            return

        for section in plan:
            section["graded"] = (
                "skipped" if skip_checks else _count_graded(client, section["id"])
            )

        emit(format_output(plan, PLAN_COLUMNS, "table"))
        at_risk = [s for s in plan if s["graded"] not in ("0", "skipped", "?")]
        if at_risk:
            err_console.print(
                "[yellow]WARNING:[/yellow] "
                f"{len(at_risk)} section(s) carry graded submissions. Grades tied "
                "to assignments that exist only in the source course will be "
                "orphaned by the move."
            )

        confirm_or_abort(
            f"Cross-list {len(plan)} section(s) into course {dest_cid}?",
            yes,
            dry_run,
        )

        for section in plan:
            client.post(f"/sections/{section['id']}/crosslist/{dest_cid}", data={})
        emit(f"Cross-listed {len(plan)} section(s) into course {dest_cid}.")
    except typer.Exit:
        raise
    except typer.BadParameter:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("uncrosslist")
def uncrosslist_section(
    section_id: int = typer.Option(..., "--id", help="Section ID to restore"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Return a cross-listed section to its original course."""
    try:
        client = get_client(verbose=verbose)
        confirm_or_abort(
            f"De-cross-list section {section_id} back to its original course?",
            yes,
            dry_run,
        )
        client.delete(f"/sections/{section_id}/crosslist")
        emit(f"De-cross-listed section {section_id}.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)
