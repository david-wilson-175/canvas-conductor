"""Discussion topic commands (list, create, update, delete) and the
`entries` sub-group of discussion-entry primitives.

Ungraded discussion topics can carry a student To-Do date, same as pages.
Note the field name differs: topics take (and return) `todo_date` directly,
whereas pages are written with `student_todo_at`. A *graded* topic drives
the to-do list from its assignment's due date instead, and Canvas rejects
`todo_date` on one.

## Entries

`discussions entries` reads and writes individual posts. It is deliberately
a set of primitives — a faithful projection of Canvas's entry API with
stateless filters — not a workflow. Nothing here remembers a previous run,
composes prose, or infers whether an assignment was completed; those belong
in a caller's script (or an extension), built on the JSON below.

The stable record `entries list`/`show` emit under `-o json`, one per
entry, flattened out of the nested thread:

| Field | Type | Notes |
|---|---|---|
| `id` | int | Canvas entry id. |
| `parent_id` | int \\| null | null for a top-level post. |
| `topic_id` | int | The topic the entry belongs to. |
| `user_id` | int \\| null | null on a deleted entry — Canvas drops the author. |
| `display_name` | str \\| null | Joined from the view's `participants` map. |
| `role` | str \\| null | Canvas enrollment type, highest-privilege first (`TeacherEnrollment`, `TaEnrollment`, …). null if the author has no enrollment in the course, or `--no-roles` was passed. |
| `roles` | list[str] \\| null | Every enrollment type the author holds. `[]` when they hold none; null when `--no-roles` skipped the join. |
| `created_at` / `updated_at` | str \\| null | Canvas UTC ISO-8601. |
| `message_html` | str \\| null | Exactly as Canvas returns it. null on a deleted entry. |
| `message_text` | str | `message_html` rendered to plain text. `""` on a deleted entry. |
| `word_count` | int | Words in `message_text`. |
| `depth` | int \\| null | 0 for a top-level post, +1 per level of nesting. |
| `deleted` | bool | True for a tombstone: no author, no message, replies intact. |
| `unread` | bool | Unread *by the calling token's user*, from the view's `unread_entries`. |
| `rating_count` / `rating_sum` | int \\| null | Canvas ratings ("like" counts), when the topic allows them. |
| `editor_id` | int \\| null | Last user to edit or delete the entry. |
| `attachments` | list[dict] | Files attached to the entry; `[]` when none. |

Canvas facts this module is built around, all verified against a sandbox
course on 2026-09-04:

- **`GET …/topics/:id/view` is the only complete read.** The `entries`
  endpoint returns top-level posts only (4 of 8 in the verified thread).
  The view nests `replies` recursively and can exceed two levels.
- **The view is a materialized cache and lags writes by 1–2 seconds**, and
  is empty on a topic whose view has never been built. Entries missing from
  it may show up in the sibling `new_entries` list; both are merged here,
  and read-back after a write retries before giving up.
- **Entries in the view carry only `user_id`** — no name — so the
  `participants` join is mandatory, and roles need a second join against
  the course's enrollments.
- **Reading never marks anything read.** `GET …/view` and `GET …/entries`
  both leave `unread_entries` untouched (verified by forcing an entry
  unread and re-reading). Only `entries mark-read` changes it.
- **Institutions inject `<link>`/`<script>` tags into every message body**,
  which is why `message_text` goes through a tag-aware renderer.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from ..client import get_client
from ..config import get_course_id
from ..exceptions import CanvasError, CanvasNotFoundError, CanvasPermissionError
from ..utils.dates import CLEAR, local_day, resolve_timezone, to_canvas_datetime
from ..utils.html_text import html_to_text, word_count
from ..utils.output import format_output, format_kv
from ._common import (
    confirm_or_abort,
    emit,
    err_console,
    guard_readonly,
    handle_canvas_error,
    preview_write,
)

app = typer.Typer(name="discussions", help="Manage discussion topics")
entries_app = typer.Typer(
    name="entries",
    help="Read and write individual discussion posts and replies.",
)
app.add_typer(entries_app, name="entries")


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


# =======================================================================
# Discussion entries
# =======================================================================

# Columns for table/CSV output. Message bodies are deliberately absent:
# these are student work, and a summary listing is not a request to dump
# them. `-o tree` reads the thread; `-o json` carries every field.
ENTRY_COLUMNS = [
    ("ID", "id"),
    ("Parent", "parent_id"),
    ("Depth", "depth"),
    ("Author", "display_name"),
    ("Role", "role_short"),
    ("Posted", "created_local"),
    ("Words", "word_count"),
    ("Unread", "unread"),
    ("Deleted", "deleted"),
]

ENTRY_OUTPUTS = ("table", "json", "csv", "tree")
SHOW_OUTPUTS = ("text", "json", "table", "csv")

# Enrollment states included in the role join. A student who has been
# concluded still wrote the post, so their role has to resolve.
_ROLE_ENROLLMENT_STATES = [
    "active",
    "invited",
    "creation_pending",
    "inactive",
    "completed",
]

# Highest privilege first: a user with two enrollments gets the first match
# as their `role`, and `roles` preserves this order.
_ROLE_PRECEDENCE = [
    "TeacherEnrollment",
    "TaEnrollment",
    "DesignerEnrollment",
    "ObserverEnrollment",
    "StudentEnrollment",
    "StudentViewEnrollment",
]

_ROLE_FILTER_ALIASES = {
    "teacher": "TeacherEnrollment",
    "instructor": "TeacherEnrollment",
    "ta": "TaEnrollment",
    "tas": "TaEnrollment",
    "designer": "DesignerEnrollment",
    "observer": "ObserverEnrollment",
    "student": "StudentEnrollment",
    "students": "StudentEnrollment",
    "studentview": "StudentViewEnrollment",
    "teststudent": "StudentViewEnrollment",
}

# Canvas's materialized view lags a write by a second or two, so a
# read-back immediately after POST can miss an entry that really exists.
_VERIFY_ATTEMPTS = 4
_VERIFY_SLEEP = 1.0


# -- Fetching and flattening --------------------------------------------


def _view_path(cid: int, topic_id: int) -> str:
    return f"/courses/{cid}/discussion_topics/{topic_id}/view"


def _entries_path(cid: int, topic_id: int, parent_id: Any = None) -> str:
    base = f"/courses/{cid}/discussion_topics/{topic_id}/entries"
    return f"{base}/{parent_id}/replies" if parent_id else base


def _collect_nodes(payload: dict) -> dict[Any, dict]:
    """Index every entry in a `/view` payload by id, tree and all.

    Walks the nested `replies` and then merges `new_entries` — entries
    Canvas has accepted but not yet folded into the materialized view.
    Without that merge a post made seconds ago is simply missing.
    """
    nodes: dict[Any, dict] = {}

    def add(item: Any, parent_id: Any) -> Any:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        eid = item["id"]
        if eid not in nodes:
            node = {k: v for k, v in item.items() if k != "replies"}
            if node.get("parent_id") is None and parent_id is not None:
                node["parent_id"] = parent_id
            nodes[eid] = node
        return eid

    def walk(items: Any, parent_id: Any) -> None:
        for item in items or []:
            eid = add(item, parent_id)
            if eid is not None and isinstance(item, dict):
                walk(item.get("replies"), eid)

    walk(payload.get("view"), None)
    for item in payload.get("new_entries") or []:
        add(item, item.get("parent_id") if isinstance(item, dict) else None)
    return nodes


def _sort_key(node: dict) -> tuple:
    return (str(node.get("created_at") or ""), str(node.get("id")))


def _thread_order(nodes: dict[Any, dict]) -> list[tuple[Any, int]]:
    """Return `(entry_id, depth)` in thread order: depth-first, oldest first.

    An entry whose parent is missing from the payload is treated as a root
    rather than dropped — a partially-materialized view must never make a
    real post vanish.
    """
    children: dict[Any, list[Any]] = defaultdict(list)
    for eid, node in nodes.items():
        parent = node.get("parent_id")
        if parent not in nodes or parent == eid:
            parent = None
        children[parent].append(eid)

    ordered: list[tuple[Any, int]] = []
    seen: set[Any] = set()

    def emit_level(parent: Any, depth: int) -> None:
        for eid in sorted(children.get(parent, []), key=lambda i: _sort_key(nodes[i])):
            if eid in seen:  # pragma: no cover - cycle guard
                continue
            seen.add(eid)
            ordered.append((eid, depth))
            emit_level(eid, depth + 1)

    emit_level(None, 0)
    # Anything left over (only reachable through a cycle) still gets out.
    for eid in sorted(nodes, key=lambda i: _sort_key(nodes[i])):  # pragma: no cover
        if eid not in seen:
            seen.add(eid)
            ordered.append((eid, 0))
    return ordered


def _participant_names(payload: dict) -> dict[Any, str]:
    names: dict[Any, str] = {}
    for person in payload.get("participants") or []:
        if isinstance(person, dict) and person.get("id") is not None:
            display = person.get("display_name") or person.get("name")
            if display:
                names[person["id"]] = display
    return names


def _role_map(client, cid: int) -> dict[Any, list[str]]:
    """Join course enrollments so every entry can carry its author's role.

    Costs one paginated call. `entries list --no-roles` skips it.
    """
    enrollments = client.get_all(
        f"/courses/{cid}/enrollments", params={"state[]": _ROLE_ENROLLMENT_STATES}
    )
    by_user: dict[Any, set[str]] = defaultdict(set)
    for item in enrollments:
        if not isinstance(item, dict):
            continue
        user_id = item.get("user_id")
        etype = item.get("type")
        if user_id is not None and etype:
            by_user[user_id].add(etype)

    def rank(etype: str) -> tuple[int, str]:
        try:
            return (_ROLE_PRECEDENCE.index(etype), etype)
        except ValueError:
            return (len(_ROLE_PRECEDENCE), etype)

    return {uid: sorted(types, key=rank) for uid, types in by_user.items()}


def _fetch_roles(client, cid: int, enabled: bool) -> dict[Any, list[str]] | None:
    """Role map, or None when the join was skipped or refused."""
    if not enabled:
        return None
    try:
        return _role_map(client, cid)
    except CanvasPermissionError as exc:
        err_console.print(
            f"[yellow]NOTE:[/yellow] could not read this course's enrollments "
            f"(403: {exc.message}), so `role` is null on every entry. "
            "Pass --no-roles to skip the join and silence this."
        )
        return None


def _entry_record(
    node: dict,
    *,
    topic_id: int,
    depth: int | None,
    names: dict[Any, str],
    roles: dict[Any, list[str]] | None,
    unread_ids: set | None,
) -> dict:
    """Build one stable output record from a raw Canvas entry.

    Handles both shapes Canvas returns: the `/view` node (bare `user_id`,
    unread implied by the topic's `unread_entries`) and the `/entries`
    node (`user`, `user_name` and `read_state` inline).
    """
    entry_id = node.get("id")
    user_id = node.get("user_id")
    user_obj = node.get("user") if isinstance(node.get("user"), dict) else {}
    display = (
        names.get(user_id)
        or user_obj.get("display_name")
        or node.get("user_name")
        or None
    )

    user_roles = roles.get(user_id, []) if roles is not None else None
    message_html = node.get("message")
    message_text = html_to_text(message_html)

    if unread_ids is not None:
        unread = entry_id in unread_ids
    else:
        unread = node.get("read_state") == "unread"

    attachments = node.get("attachments")
    if not isinstance(attachments, list):
        single = node.get("attachment")
        attachments = [single] if isinstance(single, dict) else []

    return {
        "id": entry_id,
        "parent_id": node.get("parent_id"),
        "topic_id": topic_id,
        "user_id": user_id,
        "display_name": display,
        "role": user_roles[0] if user_roles else None,
        "roles": user_roles,
        "created_at": node.get("created_at"),
        "updated_at": node.get("updated_at"),
        "message_html": message_html,
        "message_text": message_text,
        "word_count": word_count(message_text),
        "depth": depth,
        "deleted": bool(node.get("deleted")),
        "unread": bool(unread),
        "rating_count": node.get("rating_count"),
        "rating_sum": node.get("rating_sum"),
        "editor_id": node.get("editor_id"),
        "attachments": attachments,
    }


def _thread_records(
    client, cid: int, topic_id: int, roles: dict[Any, list[str]] | None
) -> list[dict]:
    """Read a topic's whole thread and flatten it into output records."""
    payload = client.get(_view_path(cid, topic_id)) or {}
    if not isinstance(payload, dict):  # pragma: no cover - defensive
        payload = {}

    nodes = _collect_nodes(payload)
    names = _participant_names(payload)
    unread_ids = set(payload.get("unread_entries") or [])

    return [
        _entry_record(
            nodes[eid],
            topic_id=topic_id,
            depth=depth,
            names=names,
            roles=roles,
            unread_ids=unread_ids,
        )
        for eid, depth in _thread_order(nodes)
    ]


# -- Filters (all stateless) --------------------------------------------


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_role_filter(value: str) -> str:
    """Accept `teacher`, `TeacherEnrollment`, `ta`, … as a role filter."""
    key = (value or "").strip().lower().replace("-", "").replace("_", "")
    if key in _ROLE_FILTER_ALIASES:
        return _ROLE_FILTER_ALIASES[key]
    for known in _ROLE_PRECEDENCE:
        if key == known.lower():
            return known
    friendly = ", ".join(sorted(set(_ROLE_FILTER_ALIASES)))
    raise typer.BadParameter(
        f"Unknown --role {value!r}. Use one of: {friendly} — or a full Canvas "
        "type such as TaEnrollment."
    )


def _matches_user(record: dict, needle: str) -> bool:
    """Match a `--user` value against an entry's author.

    A numeric value is an exact `user_id`; anything else is a
    case-insensitive substring of the display name. Canvas cannot be
    searched by name (the account-level endpoint 403s for a teacher
    token), but the names are already in hand from `participants`, so
    matching them locally is free.
    """
    needle = needle.strip()
    if needle.isdigit():
        return str(record.get("user_id")) == needle
    name = record.get("display_name") or ""
    return needle.lower() in name.lower()


def _filter_records(
    records: list[dict],
    *,
    since: str | None = None,
    until: str | None = None,
    unread_only: bool = False,
    user: str | None = None,
    role_type: str | None = None,
    top_level_only: bool = False,
    replies_only: bool = False,
    min_words: int | None = None,
    max_words: int | None = None,
    include_deleted: bool = True,
) -> list[dict]:
    """Apply every `entries list` filter. Filters compose (AND).

    Filtering is per entry, not per subtree: excluding a parent does not
    exclude its replies. The `depth` field still reports the entry's real
    place in the thread, so a caller can rebuild structure from
    `parent_id` regardless of what was filtered out.
    """
    since_dt = _as_datetime(since)
    until_dt = _as_datetime(until)

    out = []
    for record in records:
        if not include_deleted and record["deleted"]:
            continue
        if unread_only and not record["unread"]:
            continue
        if top_level_only and record["parent_id"] is not None:
            continue
        if replies_only and record["parent_id"] is None:
            continue
        if min_words is not None and record["word_count"] < min_words:
            continue
        if max_words is not None and record["word_count"] > max_words:
            continue
        if user and not _matches_user(record, user):
            continue
        if role_type and role_type not in (record.get("roles") or []):
            continue
        if since_dt or until_dt:
            created = _as_datetime(record.get("created_at"))
            if created is None:
                continue
            if since_dt and created < since_dt:
                continue
            if until_dt and created > until_dt:
                continue
        out.append(record)
    return out


# -- Rendering ----------------------------------------------------------


def _short_role(role: str | None) -> str:
    if not role:
        return ""
    return role[: -len("Enrollment")] if role.endswith("Enrollment") else role


def _display_rows(records: list[dict], tz) -> list[dict]:
    """Add table/CSV-only display columns without touching the JSON schema."""
    return [
        {
            **record,
            "role_short": _short_role(record.get("role")),
            "created_local": local_day(record.get("created_at"), tz),
        }
        for record in records
    ]


def _author_label(record: dict) -> str:
    if record.get("display_name"):
        return str(record["display_name"])
    if record.get("user_id") is not None:
        return f"user {record['user_id']}"
    return "unknown"


def _entry_headline(record: dict, tz) -> str:
    bits = [f"[{record['id']}]", _author_label(record)]
    role = _short_role(record.get("role"))
    if role:
        bits.append(f"({role})")
    bits.append(f"· {local_day(record.get('created_at'), tz) or 'no date'}")
    bits.append(f"· {record['word_count']} words")
    if record["deleted"]:
        bits.append("· DELETED")
    if record["unread"]:
        bits.append("· unread")
    return " ".join(bits)


def _render_tree(records: list[dict], tz) -> str:
    """Indented thread view. `*` marks an entry unread by the current user."""
    if not records:
        return "(no entries)"
    lines: list[str] = []
    for record in records:
        pad = "  " * (record.get("depth") or 0)
        marker = "*" if record["unread"] else " "
        lines.append(f"{pad}{marker} {_entry_headline(record, tz)}")
        body = record["message_text"] or ("(deleted)" if record["deleted"] else "")
        for line in body.split("\n"):
            lines.append(f"{pad}    {line}" if line else "")
        lines.append("")
    return "\n".join(lines).rstrip()


def _emit_entries(records: list[dict], output: str, tz) -> None:
    if output == "tree":
        emit(_render_tree(records, tz))
    elif output == "json":
        emit(format_output(records, ENTRY_COLUMNS, "json"))
    else:
        emit(format_output(_display_rows(records, tz), ENTRY_COLUMNS, output))


def _validate_output(value: str, allowed: tuple[str, ...]) -> str:
    chosen = (value or "").lower()
    if chosen not in allowed:
        raise typer.BadParameter(
            f"Unknown output format {value!r}. Use one of: {', '.join(allowed)}."
        )
    return chosen


# -- Write helpers ------------------------------------------------------


def _resolve_message(message: str | None, message_file: str | None) -> str:
    """Get the post body from `--message` or `--message-file`.

    The file is read verbatim as UTF-8 — no markdown conversion, no
    reflowing, no template expansion. Canvas renders the body as HTML, so
    what you write is what gets posted.
    """
    if bool(message) == bool(message_file):
        raise typer.BadParameter(
            "Pass exactly one of --message or --message-file."
        )

    if message_file:
        path = Path(message_file).expanduser()
        if not path.is_file():
            raise typer.BadParameter(f"--message-file not found: {message_file}")
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise typer.BadParameter(
                f"--message-file {message_file} is not valid UTF-8 text ({exc})."
            )
    else:
        body = message or ""

    if not body.strip():
        raise typer.BadParameter("The message body is empty.")
    return body


def _find_direct(client, cid: int, topic_id: int, entry_id: Any, parent_id: Any):
    """Look an entry up through the non-materialized endpoints.

    `GET …/entries` and `GET …/entries/:id/replies` reflect a write
    immediately, where the threaded view can lag it by a second or two.
    """
    for item in client.get_all(_entries_path(cid, topic_id, parent_id)):
        if isinstance(item, dict) and str(item.get("id")) == str(entry_id):
            return item
    return None


def _verify_entry(
    client,
    cid: int,
    topic_id: int,
    entry_id: Any,
    parent_id: Any,
    output: str,
    tz,
) -> None:
    """Re-read a just-written entry from Canvas and print what came back.

    A Canvas write can return 200 OK and change nothing, so a write is not
    reported as successful until it has been read back. Exits 10 if the
    entry cannot be found by either route.
    """
    roles = _fetch_roles(client, cid, True)

    for attempt in range(_VERIFY_ATTEMPTS):
        records = _thread_records(client, cid, topic_id, roles)
        found = next((r for r in records if str(r["id"]) == str(entry_id)), None)
        if found is not None:
            _emit_entries([found], output, tz)
            emit(f"Verified: entry {entry_id} read back from the topic's thread.")
            return
        if attempt < _VERIFY_ATTEMPTS - 1:
            time.sleep(_VERIFY_SLEEP)

    # The threaded view is a cache that Canvas rebuilds asynchronously.
    # The entry listing is not, so it settles the question.
    raw = _find_direct(client, cid, topic_id, entry_id, parent_id)
    if raw is not None:
        record = _entry_record(
            raw,
            topic_id=topic_id,
            depth=0 if parent_id is None else None,
            names={},
            roles=roles,
            unread_ids=None,
        )
        _emit_entries([record], output, tz)
        err_console.print(
            f"[yellow]NOTE:[/yellow] entry {entry_id} exists and was read back "
            "from the entries endpoint, but the topic's threaded view has not "
            "rebuilt yet, so `depth` is unknown. Re-run `entries list` in a "
            "moment to see it in place."
        )
        emit(f"Verified: entry {entry_id} read back from Canvas.")
        return

    err_console.print(
        f"[red]ERROR:[/red] Read-back failed. Canvas accepted the write and "
        f"reported entry {entry_id}, but it is in neither the thread nor the "
        f"entry listing for topic {topic_id}. The post may not have been "
        "saved — check the topic in Canvas before posting again."
    )
    raise typer.Exit(code=10)


# -- Errors -------------------------------------------------------------


def handle_entry_error(exc: Exception, cid: int, topic_id: int) -> typer.Exit:
    """Add entry-specific guidance to the generic error handler."""
    if isinstance(exc, CanvasNotFoundError):
        err_console.print(
            f"[red]ERROR:[/red] No discussion topic {topic_id} in course {cid} "
            f"(404). {exc.message}\n"
            "Run `conductor discussions list` to see this course's topic ids. "
            "If the topic is a group discussion, students post in the per-group "
            "child topics, which live under /groups/:id, not the course topic."
        )
        return typer.Exit(code=5)

    if isinstance(exc, CanvasPermissionError):
        err_console.print(
            f"[red]ERROR:[/red] Canvas refused this discussion request (403). "
            f"{exc.message}\n"
            "Likely causes, in order:\n"
            "  1. The topic is locked or its availability window has closed — "
            "Canvas blocks new entries then, even for a teacher.\n"
            "  2. The topic belongs to a different course than -c resolves to.\n"
            "  3. The topic is a group discussion; entries live in the group "
            "child topics rather than on the course topic."
        )
        return typer.Exit(code=4)

    return handle_canvas_error(exc)


def _explain_empty(client, cid: int, topic_id: int) -> None:
    """Say why a thread came back empty, if Canvas can tell us."""
    try:
        topic = client.get(f"/courses/{cid}/discussion_topics/{topic_id}") or {}
    except CanvasError:  # pragma: no cover - best-effort explanation only
        return
    if not isinstance(topic, dict):  # pragma: no cover - defensive
        return

    if topic.get("group_category_id"):
        children = topic.get("group_topic_children") or []
        ids = ", ".join(
            f"group {c.get('group_id')} topic {c.get('id')}"
            for c in children
            if isinstance(c, dict)
        )
        err_console.print(
            "[yellow]NOTE:[/yellow] this is a group discussion, so the course "
            "topic itself holds no entries — each group has its own child "
            "topic." + (f" Children: {ids}." if ids else "")
        )
    elif not topic.get("published"):
        err_console.print(
            "[yellow]NOTE:[/yellow] this topic is unpublished, so no student "
            "can have posted to it yet."
        )
    elif topic.get("discussion_subentry_count"):
        # The topic knows it has entries but the view returned none: Canvas
        # builds that view lazily, and the first read of a cold one comes
        # back completely empty rather than blocking.
        err_console.print(
            "[yellow]NOTE:[/yellow] Canvas reports "
            f"{topic['discussion_subentry_count']} entries on this topic but "
            "returned an empty thread. Its threaded view is built lazily and "
            "has not been materialized yet — re-run in a few seconds."
        )


# -- Commands -----------------------------------------------------------


@entries_app.command("list")
def list_entries(
    topic: int = typer.Option(..., "--topic", "-t", help="Discussion topic id"),
    course: str = typer.Option(None, "-c", "--course"),
    since: str = typer.Option(
        None,
        "--since",
        help="Only entries posted at or after this date/time (bare dates start at 00:00 local)",
    ),
    until: str = typer.Option(
        None,
        "--until",
        help="Only entries posted at or before this date/time (bare dates end at 23:59 local)",
    ),
    unread: bool = typer.Option(
        False, "--unread", help="Only entries unread by you"
    ),
    user: str = typer.Option(
        None, "--user", help="Author: a numeric user id, or part of a display name"
    ),
    role: str = typer.Option(
        None, "--role", help="Author's enrollment: teacher, ta, student, designer, observer"
    ),
    top_level_only: bool = typer.Option(
        False, "--top-level-only", help="Only posts with no parent"
    ),
    replies_only: bool = typer.Option(
        False, "--replies-only", help="Only entries that reply to another entry"
    ),
    min_words: int = typer.Option(None, "--min-words"),
    max_words: int = typer.Option(None, "--max-words"),
    include_deleted: bool = typer.Option(
        True,
        "--include-deleted/--no-deleted",
        help="Include deleted entries (shown as flagged tombstones)",
    ),
    no_roles: bool = typer.Option(
        False, "--no-roles", help="Skip the enrollment join that fills in `role`"
    ),
    tz: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates and display"
    ),
    output: str = typer.Option(
        "table", "-o", "--output", help="table, json, csv, or tree"
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List every entry in a topic, replies included, flattened.

    Reads the whole thread in one call and resolves author names and roles.
    Filters are stateless and compose; nothing is remembered between runs,
    so "what's new" is `--since <a timestamp you kept>`.

    Reading never marks anything read in Canvas — use `entries mark-read`.
    """
    cid = None
    try:
        output = _validate_output(output, ENTRY_OUTPUTS)
        if role and no_roles:
            raise typer.BadParameter("--role needs the enrollment join; drop --no-roles.")
        if top_level_only and replies_only:
            raise typer.BadParameter(
                "--top-level-only and --replies-only exclude each other; every "
                "entry is one or the other."
            )
        # Resolved up front: a typo'd role should fail before two round-trips.
        role_type = _resolve_role_filter(role) if role else None

        zone = resolve_timezone(tz)
        since_iso = to_canvas_datetime(since, zone, "00:00") if since else None
        until_iso = to_canvas_datetime(until, zone, "23:59") if until else None

        cid = get_course_id(course)
        client = get_client(verbose=verbose)
        roles = _fetch_roles(client, cid, not no_roles)
        records = _thread_records(client, cid, topic, roles)

        if not records:
            _explain_empty(client, cid, topic)

        filtered = _filter_records(
            records,
            since=since_iso,
            until=until_iso,
            unread_only=unread,
            user=user,
            role_type=role_type,
            top_level_only=top_level_only,
            replies_only=replies_only,
            min_words=min_words,
            max_words=max_words,
            include_deleted=include_deleted,
        )
        _emit_entries(filtered, output, zone)
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_entry_error(exc, cid, topic) if cid else handle_canvas_error(exc)


@entries_app.command("show")
def show_entry(
    topic: int = typer.Option(..., "--topic", "-t", help="Discussion topic id"),
    entry: int = typer.Option(..., "--entry", "-e", help="Discussion entry id"),
    course: str = typer.Option(None, "-c", "--course"),
    no_roles: bool = typer.Option(
        False, "--no-roles", help="Skip the enrollment join that fills in `role`"
    ),
    tz: str = typer.Option(None, "--tz", help="IANA timezone for display"),
    output: str = typer.Option(
        "text", "-o", "--output", help="text, json, table, or csv"
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Show one entry, with its author, role, and message body.

    Canvas has no single-entry read endpoint, so this reads the thread and
    picks the entry out of it. Reading does not mark it read.
    """
    cid = None
    try:
        output = _validate_output(output, SHOW_OUTPUTS)
        zone = resolve_timezone(tz)
        cid = get_course_id(course)
        client = get_client(verbose=verbose)
        roles = _fetch_roles(client, cid, not no_roles)
        records = _thread_records(client, cid, topic, roles)

        record = next((r for r in records if str(r["id"]) == str(entry)), None)
        if record is None:
            err_console.print(
                f"[red]ERROR:[/red] No entry {entry} in topic {topic} (404). "
                "Run `conductor discussions entries list --topic "
                f"{topic}` to see the ids this topic holds."
            )
            raise typer.Exit(code=5)

        if output == "text":
            emit(_entry_headline(record, zone))
            meta = {
                "Entry": record["id"],
                "Parent": record["parent_id"] if record["parent_id"] else "(top level)",
                "Depth": record["depth"],
                "Author": _author_label(record),
                "User ID": record["user_id"],
                "Role": record["role"] or "(none)",
                "Posted": local_day(record["created_at"], zone),
                "Edited": local_day(record["updated_at"], zone),
                "Words": record["word_count"],
                "Unread": "yes" if record["unread"] else "no",
                "Deleted": "yes" if record["deleted"] else "no",
            }
            emit(format_kv(meta))
            emit("")
            emit(record["message_text"] or "(no message)")
        else:
            _emit_entries([record], output, zone)
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_entry_error(exc, cid, topic) if cid else handle_canvas_error(exc)


@entries_app.command("create")
def create_entry(
    topic: int = typer.Option(..., "--topic", "-t", help="Discussion topic id"),
    course: str = typer.Option(None, "-c", "--course"),
    message: str = typer.Option(None, "--message", help="Post body (HTML or text)"),
    message_file: str = typer.Option(
        None, "--message-file", help="Read the post body from a UTF-8 file, verbatim"
    ),
    tz: str = typer.Option(None, "--tz", help="IANA timezone for display"),
    output: str = typer.Option("table", "-o", "--output"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force", help="Write to a readonly course"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Post a new top-level entry to a discussion topic.

    The body is sent exactly as given. Canvas renders it as HTML, so plain
    text with blank lines arrives as one paragraph — pass HTML (or
    `--message-file` containing HTML) if you want structure.
    """
    cid = None
    try:
        output = _validate_output(output, ENTRY_OUTPUTS)
        body = _resolve_message(message, message_file)
        zone = resolve_timezone(tz)
        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)

        path = _entries_path(cid, topic)
        payload = {"message": body}
        if dry_run:
            preview_write("POST", path, payload)
            return

        client = get_client(verbose=verbose)
        result = client.post(path, data=payload)
        entry_id = result.get("id") if isinstance(result, dict) else None
        if entry_id is None:
            err_console.print(
                "[red]ERROR:[/red] Canvas accepted the POST but returned no "
                "entry id, so there is nothing to verify. Check the topic in "
                "Canvas before posting again."
            )
            raise typer.Exit(code=10)

        _verify_entry(client, cid, topic, entry_id, None, output, zone)
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_entry_error(exc, cid, topic) if cid else handle_canvas_error(exc)


@entries_app.command("reply")
def reply_to_entry(
    topic: int = typer.Option(..., "--topic", "-t", help="Discussion topic id"),
    entry: int = typer.Option(..., "--entry", "-e", help="Entry id to reply to"),
    course: str = typer.Option(None, "-c", "--course"),
    message: str = typer.Option(None, "--message", help="Reply body (HTML or text)"),
    message_file: str = typer.Option(
        None, "--message-file", help="Read the reply body from a UTF-8 file, verbatim"
    ),
    tz: str = typer.Option(None, "--tz", help="IANA timezone for display"),
    output: str = typer.Option("table", "-o", "--output"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force", help="Write to a readonly course"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Reply to an existing entry.

    Replies nest arbitrarily: replying to a reply is accepted on a
    `side_comment` topic as readily as on a `threaded` one (verified), and
    the resulting depth shows up in the thread. `discussion_type` governs
    what Canvas's own UI offers, not what the API accepts.
    """
    cid = None
    try:
        output = _validate_output(output, ENTRY_OUTPUTS)
        body = _resolve_message(message, message_file)
        zone = resolve_timezone(tz)
        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)

        path = _entries_path(cid, topic, entry)
        payload = {"message": body}
        if dry_run:
            preview_write("POST", path, payload)
            return

        client = get_client(verbose=verbose)
        result = client.post(path, data=payload)
        entry_id = result.get("id") if isinstance(result, dict) else None
        if entry_id is None:
            err_console.print(
                "[red]ERROR:[/red] Canvas accepted the POST but returned no "
                "entry id, so there is nothing to verify. Check the topic in "
                "Canvas before posting again."
            )
            raise typer.Exit(code=10)

        _verify_entry(client, cid, topic, entry_id, entry, output, zone)
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_entry_error(exc, cid, topic) if cid else handle_canvas_error(exc)


@entries_app.command("mark-read")
def mark_entries_read(
    topic: int = typer.Option(..., "--topic", "-t", help="Discussion topic id"),
    course: str = typer.Option(None, "-c", "--course"),
    entry: int = typer.Option(None, "--entry", "-e", help="Mark one entry read"),
    all_entries: bool = typer.Option(
        False, "--all", help="Mark every entry in the topic read"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force", help="Write to a readonly course"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Mark one entry, or the whole topic, read.

    Read state is never changed implicitly: listing and showing entries
    leave your unread badges alone, and only this command clears them.
    It affects your own read state, not anyone else's.
    """
    cid = None
    try:
        if bool(entry) == bool(all_entries):
            raise typer.BadParameter("Pass exactly one of --entry or --all.")

        cid = get_course_id(course)
        guard_readonly(course, force, dry_run)

        base = f"/courses/{cid}/discussion_topics/{topic}"
        path = f"{base}/read_all" if all_entries else f"{base}/entries/{entry}/read"
        if dry_run:
            preview_write("PUT", path)
            return

        client = get_client(verbose=verbose)
        client.put(path)

        # Canvas answers 204 with no body, so confirm by re-reading the
        # topic's unread list rather than trusting the status code.
        payload = client.get(_view_path(cid, topic)) or {}
        unread_ids = set(payload.get("unread_entries") or [])
        if all_entries:
            if unread_ids:
                count = len(unread_ids)
                err_console.print(
                    f"[red]ERROR:[/red] Read-back failed. {count} "
                    f"{'entry is' if count == 1 else 'entries are'} still "
                    f"unread in topic {topic} after Canvas accepted read_all."
                )
                raise typer.Exit(code=10)
            emit(f"Verified: every entry in topic {topic} is now read.")
        else:
            if entry in unread_ids:
                err_console.print(
                    f"[red]ERROR:[/red] Read-back failed. Entry {entry} is "
                    "still unread after Canvas accepted the write."
                )
                raise typer.Exit(code=10)
            emit(f"Verified: entry {entry} is now read.")
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_entry_error(exc, cid, topic) if cid else handle_canvas_error(exc)
