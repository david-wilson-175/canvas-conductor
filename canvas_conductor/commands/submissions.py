"""Submission commands: list, grade, bulk-grade, download."""
from __future__ import annotations

import csv
from pathlib import Path

import typer

from ..client import get_client
from ..config import get_course_id
from ..utils.output import format_output
from ._common import emit, handle_canvas_error

app = typer.Typer(name="submissions", help="View and grade submissions")


SUBMISSION_COLUMNS = [
    ("User", "user_id"),
    ("State", "workflow_state"),
    ("Score", "score"),
    ("Grade", "grade"),
    ("Submitted", "submitted_at"),
    ("Late", "late"),
    ("Missing", "missing"),
]


@app.command("list")
def list_submissions(
    assignment_id: int = typer.Option(..., "--assignment"),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List submissions for an assignment."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        items = client.get_all(
            f"/courses/{cid}/assignments/{assignment_id}/submissions",
            params={"include[]": "user"},
        )
        emit(format_output(items, SUBMISSION_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("grade")
def grade_submission(
    assignment_id: int = typer.Option(..., "--assignment"),
    user_id: int = typer.Option(..., "--user"),
    grade: str = typer.Option(..., "--grade", help="Grade value (e.g., '10', 'pass', 'A')"),
    comment: str = typer.Option(None, "--comment"),
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Grade a single submission."""
    try:
        cid = get_course_id(course)
        payload: dict = {"submission": {"posted_grade": grade}}
        if comment:
            payload["comment"] = {"text_comment": comment}
        path = f"/courses/{cid}/assignments/{assignment_id}/submissions/{user_id}"
        if dry_run:
            emit(f"DRY-RUN: PUT {path} payload={payload}")
            return
        client = get_client(verbose=verbose)
        result = client.put(path, payload)
        emit(format_output(result, SUBMISSION_COLUMNS, "table"))
    except Exception as exc:
        raise handle_canvas_error(exc)


def _check_group_grading(client, cid: int, assignment_id: int, allow: bool) -> None:
    """Refuse to bulk-grade a group assignment that pools one grade per group.

    On such an assignment Canvas applies every row to the whole group, so
    per-student grades in the CSV are silently overwritten by whichever row
    for that group lands last. The CSV looks right; the grades are wrong.
    """
    assignment = client.get(f"/courses/{cid}/assignments/{assignment_id}")
    is_group = assignment.get("group_category_id") is not None
    individually = assignment.get("grade_group_students_individually")
    if not is_group or individually or allow:
        return
    name = assignment.get("name", assignment_id)
    emit(
        f"ERROR: '{name}' is a group assignment that pools one grade per group.\n"
        "Canvas would apply each CSV row to every member of that student's "
        "group, so per-student grades would be silently overwritten.\n\n"
        "Turn on individual grading first:\n"
        f"  conductor assignments update -c <course> --id {assignment_id} "
        "--individual-grading\n\n"
        "Or pass --allow-group-propagation if one grade per group is what you want."
    )
    raise typer.Exit(code=2)


@app.command("bulk-grade")
def bulk_grade(
    assignment_id: int = typer.Option(..., "--assignment"),
    file: str = typer.Option(..., "--file", help="CSV with columns: user_id,grade[,comment]"),
    course: str = typer.Option(None, "-c", "--course"),
    allow_group_propagation: bool = typer.Option(
        False,
        "--allow-group-propagation",
        help="Permit bulk-grading a group assignment that pools one grade per group.",
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Grade many submissions at once via a CSV.

    Canvas processes this asynchronously and returns a Progress object;
    we surface the progress URL so the user can poll if needed.
    """
    try:
        cid = get_course_id(course)
        path_obj = Path(file)
        if not path_obj.is_file():
            emit(f"ERROR: file not found: {file}")
            raise typer.Exit(code=2)

        # Nested form: {"grade_data": {<user_id>: {"posted_grade": ..., "text_comment": ...}}}
        per_student: dict[str, dict] = {}
        with path_obj.open() as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                uid = row.get("user_id") or row.get("id")
                grade = row.get("grade") or row.get("posted_grade")
                if not uid or grade is None:
                    continue
                entry: dict = {"posted_grade": grade}
                comment = row.get("comment") or row.get("text_comment")
                if comment:
                    entry["text_comment"] = comment
                per_student[str(uid)] = entry

        if not per_student:
            emit("No grades parsed from CSV (expected columns: user_id, grade).")
            raise typer.Exit(code=2)

        n_students = len(per_student)
        grade_data = {"grade_data": per_student}
        url = f"/courses/{cid}/assignments/{assignment_id}/submissions/update_grades"

        # Runs in dry-run too: catching the group-propagation trap is only
        # useful if it fires before you commit.
        client = get_client(verbose=verbose)
        _check_group_grading(client, cid, assignment_id, allow_group_propagation)

        if dry_run:
            emit(f"DRY-RUN: would post {n_students} grades to {url}")
            for uid, entry in list(per_student.items())[:10]:
                emit(f"  {uid}: {entry}")
            if n_students > 10:
                emit(f"  ... and {n_students - 10} more rows")
            return

        if not yes and not typer.confirm(
            f"Post {n_students} grades to assignment {assignment_id}?", default=False
        ):
            emit("Aborted.")
            raise typer.Exit(code=1)

        result = client.post(url, data=grade_data)
        emit("Submitted bulk grade job. Canvas processes this asynchronously.")
        if isinstance(result, dict):
            if "url" in result:
                emit(f"Progress URL: {result['url']}")
            elif "id" in result:
                emit(f"Progress ID: {result['id']} — poll GET /progress/{result['id']}")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("download")
def download_submissions(
    assignment_id: int = typer.Option(..., "--assignment"),
    out_dir: str = typer.Option(..., "--dir", help="Local directory to write into"),
    course: str = typer.Option(None, "-c", "--course"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Download attachment files from each submission to a local directory."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)

        subs = client.get_all(
            f"/courses/{cid}/assignments/{assignment_id}/submissions",
            params={"include[]": "user"},
        )

        downloaded = 0
        skipped = 0
        for sub in subs:
            user = sub.get("user") or {}
            slug = (user.get("name") or f"user-{sub.get('user_id')}").replace("/", "_")
            attachments = sub.get("attachments") or []
            if not attachments:
                skipped += 1
                continue
            for att in attachments:
                url = att.get("url")
                fname = att.get("display_name") or att.get("filename") or "file"
                if not url:
                    continue
                # Attachment URLs are pre-signed and don't take our bearer token.
                response = client.session.get(
                    url, allow_redirects=True, headers={"Authorization": ""}
                )
                response.raise_for_status()
                dest = target / f"{slug}_{fname}"
                dest.write_bytes(response.content)
                downloaded += 1

        emit(
            f"Downloaded {downloaded} files into {target}. "
            f"Skipped {skipped} submissions with no attachments."
        )
    except Exception as exc:
        raise handle_canvas_error(exc)
