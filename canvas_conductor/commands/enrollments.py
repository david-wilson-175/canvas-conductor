"""Enrollment commands: list, summary, add, remove, update, reactivate.

Adding people to a course is the one Canvas write where a teacher-level
token and an admin token behave very differently, so three constraints
shape this module:

1. **Payloads must be nested JSON.** `CanvasClient.post/put` send
   `json=`, not form-encoded `data=`. A Rails-style flat key
   (`{"enrollment[user_id]": …}`) therefore arrives as a meaningless
   top-level JSON field: Canvas returns 200 OK and enrolls nobody. Every
   payload here is built with `prefix_keys`, which nests.

2. **Account-level user search is 403 for an ordinary teacher token.**
   `GET /accounts/:id/users?search_term=…` is not available, so the CLI
   cannot turn a person's *name* into a user id. What does work on the
   course-scoped endpoint with plain teacher permissions is Canvas's
   reference syntax — `sis_login_id:<netid>` — so `--user` accepts that
   (plus `sis_user_id:` and bare numeric ids) and passes it through
   untouched rather than trying to resolve it first.

3. **The POST response cannot be trusted to say who was enrolled.** A
   verified-successful create came back with `user: None` while the
   enrollment was real. So every write in this module re-reads the
   course's enrollment list afterwards and confirms the record's type
   and state before reporting success.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

import typer

from ..client import get_client
from ..config import get_course_id
from ..exceptions import CanvasError, CanvasNotFoundError, CanvasPermissionError
from ..utils.output import format_output
from ._common import (
    confirm_or_abort,
    emit,
    err_console,
    guard_readonly,
    handle_canvas_error,
    prefix_keys,
)

app = typer.Typer(name="enrollments", help="Manage course enrollments")


ENROLL_COLUMNS = [
    ("ID", "id"),
    ("User", "user.name"),
    ("Email", "user.email"),
    ("Login", "user.login_id"),
    ("Type", "type"),
    ("State", "enrollment_state"),
]

# Wider set for read-back after a write: `role` distinguishes a custom
# account role from its base type, and `course_section_id` is the only way
# to tell a section-scoped enrollment from a course-wide one.
VERIFY_COLUMNS = [
    ("Enrollment", "id"),
    ("User", "user.name"),
    ("Login", "user.login_id"),
    ("User ID", "user_id"),
    ("Type", "type"),
    ("Role", "role"),
    ("State", "enrollment_state"),
    ("Section", "course_section_id"),
]


_TYPE_ALIASES = {
    "student": "StudentEnrollment",
    "teacher": "TeacherEnrollment",
    "instructor": "TeacherEnrollment",
    "ta": "TaEnrollment",
    "observer": "ObserverEnrollment",
    "designer": "DesignerEnrollment",
}

_BASE_TYPES = {
    "studentenrollment": "StudentEnrollment",
    "teacherenrollment": "TeacherEnrollment",
    "taenrollment": "TaEnrollment",
    "observerenrollment": "ObserverEnrollment",
    "designerenrollment": "DesignerEnrollment",
}

# States Canvas accepts when *creating* an enrollment. `completed` and the
# rest are outcomes of a task, not things you can ask for up front.
_CREATE_STATES = ("active", "invited", "inactive")

# Every live state, passed to the read-back listing so a just-created
# `inactive` enrollment (which the default listing hides) is still found.
_READBACK_STATES = [
    "active",
    "invited",
    "creation_pending",
    "inactive",
    "completed",
    "rejected",
]

# `DELETE /courses/:id/enrollments/:id?task=…`. These are not variations on
# a theme — `delete` destroys the enrollment and the submissions hanging
# off it; `conclude` keeps everything and just ends access.
_TASKS = {
    "conclude": "end access, preserving the enrollment and its submissions",
    "deactivate": "make inactive; record and grades survive (needs admin rights)",
    "inactivate": "synonym for deactivate (needs admin rights)",
    "delete": "DESTROY the enrollment record and its submissions",
}

_STATE_AFTER_TASK = {
    "conclude": "completed",
    "deactivate": "inactive",
    "inactivate": "inactive",
}


# -- Reference / role parsing -------------------------------------------


def normalize_user_ref(value: str) -> str:
    """Turn a `--user` value into something Canvas's `user_id` field accepts.

    Passes through `sis_login_id:`/`sis_user_id:`/`sis_integration_id:`
    references and bare numeric ids; treats anything else as a bare login
    and prefixes it, which is the common case (`milla23` →
    `sis_login_id:milla23`).

    A name is rejected outright rather than guessed at: resolving one needs
    the account-level user search, which 403s for a teacher token, so a
    name could never work no matter how it was encoded.
    """
    ref = (value or "").strip()
    if not ref:
        raise typer.BadParameter("--user cannot be empty.")

    lowered = ref.lower()
    for prefix in ("sis_login_id:", "sis_user_id:", "sis_integration_id:"):
        if lowered.startswith(prefix):
            if not ref[len(prefix):].strip():
                raise typer.BadParameter(f"--user is missing a value after '{prefix}'.")
            return ref

    if ref.isdigit():
        return ref

    if " " in ref:
        raise typer.BadParameter(
            f"--user got what looks like a name ({ref!r}). Canvas can only be "
            "searched by name through the account-level user endpoint, which "
            "returns 403 for a teacher token. Pass the person's netid instead "
            "— e.g. --user jsmith42 or --user sis_login_id:jsmith42."
        )

    if "@" in ref:
        err_console.print(
            f"[yellow]NOTE:[/yellow] treating {ref!r} as a login id "
            "(sis_login_id). If your institution's logins are netids rather "
            "than email addresses, pass the netid instead."
        )

    return f"sis_login_id:{ref}"


def resolve_role(role: str) -> str:
    """Map a friendly role name to a Canvas base enrollment type."""
    key = (role or "").strip().lower()
    if key in _TYPE_ALIASES:
        return _TYPE_ALIASES[key]
    if key in _BASE_TYPES:
        return _BASE_TYPES[key]
    friendly = ", ".join(sorted(set(_TYPE_ALIASES)))
    raise typer.BadParameter(
        f"Unknown role {role!r}. Use one of: {friendly} — or a full Canvas "
        "type such as TaEnrollment. For an institution-defined role, pass "
        "--role-id instead."
    )


def _warn_about_role(canvas_type: str) -> None:
    """Flag roles whose real privileges surprise people."""
    if canvas_type == "TaEnrollment":
        err_console.print(
            "[yellow]WARNING:[/yellow] TaEnrollment carries gradebook access — "
            "a TA can view and change grades for every student in the course. "
            "If you only want someone who can see and edit course content, "
            "--role designer grants that without any grade visibility."
        )
    elif canvas_type == "ObserverEnrollment":
        err_console.print(
            "[yellow]NOTE:[/yellow] observers are normally tied to a specific "
            "student. Pass --associated-user <ref> to link one; without it the "
            "observer sees the course but no student's work."
        )


# -- Read-back verification ---------------------------------------------


def _list_enrollments(client, cid: int) -> list[dict]:
    return client.get_all(
        f"/courses/{cid}/enrollments", params={"state[]": _READBACK_STATES}
    )


def _find_enrollment(client, cid: int, enrollment_id: Any) -> dict | None:
    """Re-read the course's enrollments and return the one with this id.

    Canvas has no course-scoped `GET /enrollments/:id` — the by-id read
    lives under `/accounts/:id/enrollments/:id`, which 403s for a teacher
    token — so verification means listing and filtering.
    """
    for item in _list_enrollments(client, cid):
        if str(item.get("id")) == str(enrollment_id):
            return item
    return None


def _report_verified(
    client,
    cid: int,
    enrollment_id: Any,
    *,
    expect_type: str | None = None,
    expect_state: str | None = None,
    expect_absent: bool = False,
    output: str = "table",
) -> None:
    """Re-read the enrollment and confirm it matches what we asked for.

    Raises `typer.Exit(10)` on a mismatch. A write that reports success
    while changing nothing is the specific failure this guards against —
    it is how the flat-payload bug stayed invisible.
    """
    found = _find_enrollment(client, cid, enrollment_id)

    if expect_absent:
        if found is None:
            emit(f"Verified: enrollment {enrollment_id} is gone from course {cid}.")
            return
        err_console.print(
            f"[red]ERROR:[/red] Read-back failed. Enrollment {enrollment_id} is "
            f"still present with state {found.get('enrollment_state')!r} after "
            "the delete was accepted."
        )
        raise typer.Exit(code=10)

    if found is None:
        err_console.print(
            f"[red]ERROR:[/red] Read-back failed. Canvas accepted the write but "
            f"enrollment {enrollment_id} is not in course {cid}'s enrollment "
            "list. Nothing may have been written. If you passed --section, "
            "check that the section belongs to this course."
        )
        raise typer.Exit(code=10)

    problems = []
    if expect_type and found.get("type") != expect_type:
        problems.append(f"type is {found.get('type')!r}, expected {expect_type!r}")
    if expect_state and found.get("enrollment_state") != expect_state:
        problems.append(
            f"state is {found.get('enrollment_state')!r}, expected {expect_state!r}"
        )

    emit(format_output([found], VERIFY_COLUMNS, output))

    if problems:
        err_console.print(
            "[red]ERROR:[/red] Read-back mismatch: " + "; ".join(problems) + "."
        )
        raise typer.Exit(code=10)
    emit(f"Verified: enrollment {found.get('id')} read back from Canvas.")


def _preview(method: str, path: str, payload: dict | None = None) -> None:
    """Print exactly what a write would send, and send nothing."""
    emit(f"DRY-RUN: {method} {path}")
    if payload is not None:
        emit("DRY-RUN: payload=" + json.dumps(payload, indent=2, sort_keys=True))
    emit("DRY-RUN: no request was made.")


# -- Error handling ------------------------------------------------------


def handle_enrollment_error(exc: Exception, task: str | None = None) -> typer.Exit:
    """Add enrollment-specific guidance to the generic error handler.

    A 403 here almost always means one of a few concrete things, and the
    fix is a different `--user` form or a different `--task` rather than
    anything the user would guess from "Permission denied".
    """
    if isinstance(exc, CanvasPermissionError):
        if task in ("delete", "deactivate", "inactivate"):
            # Verified 2026-09-04. A teacher token holding manage_students,
            # remove_student_from_course and remove_ta_from_course still gets
            # 403 for all three of these tasks on its *own* enrollment, while
            # `conclude` on the same enrollment succeeds. Canvas gates
            # delete/deactivate behind a check that the enrollment is not
            # yours unless you also hold account-level manage_admin_users
            # (False for a plain teacher); conclude does not share that check.
            err_console.print(
                f"[red]ERROR:[/red] Canvas refused --task {task} (403). "
                f"{exc.message}\n"
                "The usual cause is that this is your own enrollment: Canvas "
                "blocks delete/deactivate/inactivate on yourself unless you "
                "hold account-level manage_admin_users. --task conclude is not "
                "subject to that check.\n"
                "  - On your own enrollment: use --task conclude, or remove "
                "yourself from the course People page in the Canvas UI.\n"
                "  - On someone else's: you also need 'Users - add / remove' "
                "for their role in this course. If you have it and this still "
                "fails, the enrollment is likely SIS-managed, which the API "
                "will not let you unwind."
            )
            return typer.Exit(code=4)

        err_console.print(
            f"[red]ERROR:[/red] Canvas refused this enrollment write (403). "
            f"{exc.message}\n"
            "Likely causes, in order:\n"
            "  1. Your token is teacher-level, so account-level user lookup is "
            "not available. Reference the person directly: "
            "--user sis_login_id:<netid> (not a name, not an email).\n"
            "  2. You lack the 'Users - add / remove' permission in this "
            "course — an enrolled teacher usually has it; a TA usually does not.\n"
            "  3. --role-id names a role your account is not allowed to assign. "
            "Listing account roles also requires admin rights, so the id has to "
            "come from someone who can see it."
        )
        return typer.Exit(code=4)

    if isinstance(exc, CanvasNotFoundError):
        err_console.print(
            f"[red]ERROR:[/red] Canvas could not find the target (404). "
            f"{exc.message}\n"
            "If this was --user, check the netid spelling: an unknown "
            "sis_login_id reads as 'not found' rather than 'no such user'. "
            "If it was --enrollment-id, run "
            "`conductor enrollments list -c <alias>` to get a current id."
        )
        return typer.Exit(code=5)

    return handle_canvas_error(exc)


# -- Shared write helpers ------------------------------------------------


def _build_enrollment_payload(
    *,
    user_ref: str,
    canvas_type: str | None,
    role_id: int | None,
    state: str,
    notify: bool,
    limit_to_section: bool,
    associated_user: str | None,
) -> dict:
    """Assemble the nested `{"enrollment": {…}}` body Canvas expects."""
    inner: dict[str, Any] = {
        "user_id": user_ref,
        "type": canvas_type,
        "enrollment_state": state,
        "notify": notify,
        "role_id": role_id,
        "associated_user_id": associated_user,
    }
    if limit_to_section:
        inner["limit_privileges_to_course_section"] = True
    # prefix_keys drops the None entries and nests the rest under
    # "enrollment" — never `enrollment[user_id]`, which this API silently
    # ignores while still returning 200.
    return prefix_keys("enrollment", inner)


def _create_enrollment(
    client,
    *,
    cid: int,
    section: int | None,
    payload: dict,
) -> dict:
    path = (
        f"/sections/{section}/enrollments"
        if section
        else f"/courses/{cid}/enrollments"
    )
    result = client.post(path, data=payload)
    if not isinstance(result, dict) or "id" not in result:
        raise CanvasError(
            "Canvas accepted the enrollment POST but returned no enrollment id, "
            "so the result cannot be verified.",
            body=result,
        )
    return result


# -- Commands ------------------------------------------------------------


@app.command("list")
def list_enrollments(
    course: str = typer.Option(None, "-c", "--course"),
    type_: str = typer.Option(
        None,
        "--type",
        help="student, teacher, ta, observer, designer (or full Canvas type)",
    ),
    state: str = typer.Option(
        None, "--state", help="active, invited, completed, inactive, rejected"
    ),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List enrollments in a course."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        params: dict = {}
        if type_:
            canvas_type = _TYPE_ALIASES.get(type_.lower(), type_)
            params["type[]"] = canvas_type
        if state:
            params["state[]"] = state
        items = client.get_all(f"/courses/{cid}/enrollments", params=params)
        emit(format_output(items, ENROLL_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("summary")
def summary(
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Show enrollment counts grouped by type and state."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        items = client.get_all(f"/courses/{cid}/enrollments")
        counts: Counter[tuple[str, str]] = Counter()
        for e in items:
            counts[(e.get("type", "?"), e.get("enrollment_state", "?"))] += 1
        rows = [
            {"type": t, "state": s, "count": n}
            for (t, s), n in sorted(counts.items())
        ]
        cols = [("Type", "type"), ("State", "state"), ("Count", "count")]
        emit(format_output(rows, cols, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("add")
def add_enrollment(
    user: str = typer.Option(
        ...,
        "--user",
        help=(
            "Who to enrol: a netid (milla23), sis_login_id:milla23, "
            "sis_user_id:<id>, or a numeric Canvas user id. A bare value is "
            "treated as a login. Names are not supported (see --help)."
        ),
    ),
    role: str = typer.Option(
        None,
        "--role",
        help="student, teacher/instructor, ta, designer, observer (or a full Canvas type)",
    ),
    role_id: int = typer.Option(
        None,
        "--role-id",
        help="Institution-defined role id. May be used alone or with --role.",
    ),
    state: str = typer.Option(
        "invited",
        "--state",
        help=(
            "invited (person must accept; default) or active (appears on their "
            "dashboard immediately). inactive also accepted."
        ),
    ),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Email the user (default: notify)."
    ),
    section: int = typer.Option(
        None, "--section", help="Enrol into this section id rather than course-wide."
    ),
    limit_to_section: bool = typer.Option(
        False,
        "--limit-to-section",
        help="Restrict the person to seeing only their own section.",
    ),
    associated_user: str = typer.Option(
        None, "--associated-user", help="For observers: the student to observe."
    ),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    force: bool = typer.Option(False, "--force", help="Override a readonly course."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Add a person to a course at any access level."""
    try:
        if not role and not role_id:
            raise typer.BadParameter("Pass --role (or --role-id for a custom role).")

        state_key = (state or "").strip().lower()
        if state_key not in _CREATE_STATES:
            raise typer.BadParameter(
                f"--state must be one of: {', '.join(_CREATE_STATES)}. "
                "(completed/concluded are results of `enrollments remove --task`, "
                "not states you can create.)"
            )

        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)

        user_ref = normalize_user_ref(user)
        canvas_type = resolve_role(role) if role else None
        assoc_ref = normalize_user_ref(associated_user) if associated_user else None

        payload = _build_enrollment_payload(
            user_ref=user_ref,
            canvas_type=canvas_type,
            role_id=role_id,
            state=state_key,
            notify=notify,
            limit_to_section=limit_to_section,
            associated_user=assoc_ref,
        )
        path = (
            f"/sections/{section}/enrollments"
            if section
            else f"/courses/{cid}/enrollments"
        )

        if dry_run:
            _preview("POST", path, payload)
            return

        if canvas_type:
            _warn_about_role(canvas_type)
        if state_key == "invited" and not notify:
            err_console.print(
                "[yellow]NOTE:[/yellow] an invited enrollment does nothing until "
                "the person accepts it, and --no-notify means they won't be told. "
                "Use --state active to skip the invitation step."
            )

        # A teacher enrollment hands over full control of the course, so it
        # is gated even though nothing is being destroyed.
        if canvas_type == "TeacherEnrollment":
            confirm_or_abort(
                f"Add {user_ref} to course {cid} as a TEACHER? This grants full "
                "control of the course, including the gradebook, course "
                "settings, and the ability to remove other teachers.",
                yes,
                dry_run=False,
            )

        client = get_client(verbose=verbose)
        result = _create_enrollment(client, cid=cid, section=section, payload=payload)

        emit(
            f"Canvas accepted the enrollment (id {result['id']}). Verifying by "
            "re-reading the course's enrollments…"
        )
        _report_verified(
            client,
            cid,
            result["id"],
            expect_type=canvas_type,
            expect_state=state_key if state_key != "invited" else None,
            output=output,
        )
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_enrollment_error(exc)


@app.command("remove")
def remove_enrollment(
    enrollment_id: int = typer.Option(
        ..., "--enrollment-id", help="From `conductor enrollments list`."
    ),
    task: str = typer.Option(
        ...,
        "--task",
        help=(
            "conclude (end access, keep everything) | deactivate | inactivate | "
            "delete (DESTROYS the enrollment and its submissions). Required — "
            "there is no safe default."
        ),
    ),
    confirm_delete: bool = typer.Option(
        False,
        "--confirm-delete",
        help="Required acknowledgement for --task delete.",
    ),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    force: bool = typer.Option(False, "--force", help="Override a readonly course."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Remove an enrollment. --task decides whether the record survives."""
    try:
        task_key = (task or "").strip().lower()
        if task_key not in _TASKS:
            options = "\n".join(f"  {k:<11} {v}" for k, v in _TASKS.items())
            raise typer.BadParameter(f"--task must be one of:\n{options}")

        if task_key == "delete" and not confirm_delete:
            raise typer.BadParameter(
                "--task delete destroys the enrollment record and the "
                "submissions attached to it. This cannot be undone. Pass "
                "--confirm-delete to acknowledge, or use --task conclude to end "
                "the person's access while preserving their work."
            )

        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)
        path = f"/courses/{cid}/enrollments/{enrollment_id}"

        if dry_run:
            _preview("DELETE", f"{path}?task={task_key}")
            return

        client = get_client(verbose=verbose)

        # Show what is about to be removed — the caller passed an opaque id.
        existing = _find_enrollment(client, cid, enrollment_id)
        if existing is None:
            err_console.print(
                f"[red]ERROR:[/red] No enrollment {enrollment_id} in course {cid}. "
                "Run `conductor enrollments list -c <alias>` for current ids."
            )
            raise typer.Exit(code=5)
        emit(format_output([existing], VERIFY_COLUMNS, "table"))

        confirm_or_abort(
            f"{task_key.capitalize()} this enrollment — {_TASKS[task_key]}?",
            yes,
            dry_run=False,
        )

        client.delete(path, params={"task": task_key})

        emit("Canvas accepted the removal. Verifying by re-reading…")
        _report_verified(
            client,
            cid,
            enrollment_id,
            expect_absent=(task_key == "delete"),
            expect_state=_STATE_AFTER_TASK.get(task_key),
            output=output,
        )
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_enrollment_error(exc, task=task_key)


@app.command("reactivate")
def reactivate_enrollment(
    enrollment_id: int = typer.Option(..., "--enrollment-id"),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    force: bool = typer.Option(False, "--force", help="Override a readonly course."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Restore an inactive enrollment to active."""
    try:
        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)
        path = f"/courses/{cid}/enrollments/{enrollment_id}/reactivate"

        if dry_run:
            # PUT, not POST — see the call site below.
            _preview("PUT", path)
            return

        client = get_client(verbose=verbose)
        existing = _find_enrollment(client, cid, enrollment_id)
        if existing is None:
            err_console.print(
                f"[red]ERROR:[/red] No enrollment {enrollment_id} in course {cid}."
            )
            raise typer.Exit(code=5)

        # Canvas only reactivates from `inactive`; a concluded enrollment
        # returns 400 here, which is worth saying up front.
        current = existing.get("enrollment_state")
        if current != "inactive":
            err_console.print(
                f"[yellow]NOTE:[/yellow] enrollment {enrollment_id} is currently "
                f"{current!r}. Reactivate only applies to 'inactive' "
                "enrollments; a concluded one has to be re-added."
            )
        emit(format_output([existing], VERIFY_COLUMNS, "table"))

        confirm_or_abort(
            f"Reactivate enrollment {enrollment_id} in course {cid}?",
            yes,
            dry_run=False,
        )

        # Reactivate is a PUT. Every other enrollment write in this module is
        # a POST, and POSTing here does not 405 — Canvas has no such route, so
        # it answers 404 with an HTML page, which reads as "no such enrollment"
        # and sends you looking for the wrong problem entirely.
        client.put(path, data={})
        emit("Canvas accepted the reactivation. Verifying by re-reading…")
        _report_verified(
            client, cid, enrollment_id, expect_state="active", output=output
        )
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_enrollment_error(exc)


@app.command("update")
def update_enrollment(
    enrollment_id: int = typer.Option(..., "--enrollment-id"),
    role: str = typer.Option(None, "--role", help="New role for this person."),
    role_id: int = typer.Option(None, "--role-id", help="New institution-defined role."),
    section: int = typer.Option(None, "--section", help="Move to this section id."),
    state: str = typer.Option(
        "active", "--state", help="State for the replacement enrollment."
    ),
    limit_to_section: bool = typer.Option(
        False, "--limit-to-section", help="Restrict to their own section."
    ),
    notify: bool = typer.Option(False, "--notify/--no-notify"),
    task: str = typer.Option(
        ...,
        "--task",
        help=(
            "What to do with the OLD enrollment once the new one exists: "
            "conclude | deactivate | inactivate | delete."
        ),
    ),
    confirm_delete: bool = typer.Option(
        False, "--confirm-delete", help="Required acknowledgement for --task delete."
    ),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    force: bool = typer.Option(False, "--force", help="Override a readonly course."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Change someone's role or section by re-enrolling them.

    Canvas has no PUT for an enrollment — role and section are fixed at
    creation. So this adds the replacement enrollment first, verifies it,
    and only then applies --task to the old one. If the add fails the
    person keeps the access they already had.
    """
    try:
        if not role and not role_id and not section and not limit_to_section:
            raise typer.BadParameter(
                "Nothing to change. Pass at least one of --role, --role-id, "
                "--section, or --limit-to-section."
            )

        task_key = (task or "").strip().lower()
        if task_key not in _TASKS:
            options = "\n".join(f"  {k:<11} {v}" for k, v in _TASKS.items())
            raise typer.BadParameter(f"--task must be one of:\n{options}")
        if task_key == "delete" and not confirm_delete:
            raise typer.BadParameter(
                "--task delete destroys the old enrollment and its submissions. "
                "Pass --confirm-delete, or use --task conclude to keep the record."
            )

        state_key = (state or "").strip().lower()
        if state_key not in _CREATE_STATES:
            raise typer.BadParameter(
                f"--state must be one of: {', '.join(_CREATE_STATES)}."
            )

        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)

        client = get_client(verbose=verbose)
        existing = _find_enrollment(client, cid, enrollment_id)
        if existing is None:
            err_console.print(
                f"[red]ERROR:[/red] No enrollment {enrollment_id} in course {cid}. "
                "Run `conductor enrollments list -c <alias>` for current ids."
            )
            raise typer.Exit(code=5)

        user_ref = str(existing.get("user_id") or "")
        if not user_ref:
            raise CanvasError(
                f"Enrollment {enrollment_id} has no user_id, so the replacement "
                "enrollment cannot be addressed to anyone."
            )

        canvas_type = resolve_role(role) if role else existing.get("type")
        target_section = section or existing.get("course_section_id")

        payload = _build_enrollment_payload(
            user_ref=user_ref,
            canvas_type=canvas_type,
            role_id=role_id,
            state=state_key,
            notify=notify,
            limit_to_section=limit_to_section,
            associated_user=None,
        )
        create_path = (
            f"/sections/{target_section}/enrollments"
            if target_section
            else f"/courses/{cid}/enrollments"
        )
        delete_path = f"/courses/{cid}/enrollments/{enrollment_id}"

        emit("Current enrollment:")
        emit(format_output([existing], VERIFY_COLUMNS, "table"))

        if dry_run:
            emit("")
            emit("DRY-RUN: step 1 of 2 — create the replacement enrollment")
            _preview("POST", create_path, payload)
            emit("")
            emit(f"DRY-RUN: step 2 of 2 — {task_key} the old enrollment")
            _preview("DELETE", f"{delete_path}?task={task_key}")
            return

        if canvas_type:
            _warn_about_role(canvas_type)
        if canvas_type == "TeacherEnrollment":
            err_console.print(
                "[yellow]WARNING:[/yellow] the replacement is a TEACHER "
                "enrollment, granting full control of the course."
            )

        confirm_or_abort(
            f"Re-enrol user {user_ref} in course {cid} as {canvas_type or 'role_id ' + str(role_id)} "
            f"(state {state_key}), then {task_key} enrollment {enrollment_id}?",
            yes,
            dry_run=False,
        )

        created = _create_enrollment(
            client, cid=cid, section=target_section, payload=payload
        )
        emit(f"Created replacement enrollment {created['id']}. Verifying…")
        _report_verified(
            client,
            cid,
            created["id"],
            expect_type=canvas_type,
            expect_state=state_key if state_key != "invited" else None,
            output=output,
        )

        emit(f"\nApplying --task {task_key} to the old enrollment {enrollment_id}…")
        try:
            client.delete(delete_path, params={"task": task_key})
        except CanvasError as exc:
            # The replacement enrollment already exists and is verified, so
            # say so — otherwise this reads as a total failure and the next
            # run creates a second duplicate.
            err_console.print(
                f"[yellow]WARNING:[/yellow] the new enrollment "
                f"{created['id']} was created successfully, but removing the "
                f"old enrollment {enrollment_id} failed. The person now holds "
                "BOTH. Re-run `conductor enrollments remove` for the old id."
            )
            raise handle_enrollment_error(exc, task=task_key)
        _report_verified(
            client,
            cid,
            enrollment_id,
            expect_absent=(task_key == "delete"),
            expect_state=_STATE_AFTER_TASK.get(task_key),
            output=output,
        )
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_enrollment_error(exc)
