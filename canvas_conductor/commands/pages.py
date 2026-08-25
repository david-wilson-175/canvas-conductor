"""Page (wiki) commands: list, show, create, update, delete, set-front, bulk-todo.

To-do dates deserve a note. Canvas Pages have no due date, so the only way
to get a reading onto a student's To-Do list (and onto their calendar) is
the `student_todo_at` field. Two asymmetries make it easy to get wrong:

- You **write** `wiki_page[student_todo_at]` but **read back** `todo_date`.
- A to-do date on an *unpublished* page is inert — students never see it.

`bulk-todo` handles both: it reports the read-side field under its own name
and pre-flights the publish state before committing anything.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import typer

from ..client import get_client
from ..config import get_course_id
from ..utils.dates import (
    CLEAR,
    local_day,
    parse_shift,
    resolve_timezone,
    shift_iso,
    to_canvas_datetime,
)
from ..utils.output import format_output
from ._common import confirm_or_abort, emit, err_console, handle_canvas_error, prefix_keys

app = typer.Typer(name="pages", help="Manage course wiki pages")


PAGE_COLUMNS = [
    ("URL", "url"),
    ("Title", "title"),
    ("Published", "published"),
    ("To-Do", "todo_local"),
    ("Front Page", "front_page"),
    ("Updated", "updated_at"),
]

PLAN_COLUMNS = [
    ("URL", "url"),
    ("Title", "title"),
    ("Published", "published"),
    ("Current To-Do", "current_local"),
    ("New To-Do", "new_local"),
]

# Column headers accepted by `bulk-todo --file`, matched case-insensitively
# so a `pages list -o csv` export can be edited and fed straight back in.
_URL_HEADERS = {"url", "page_url", "slug", "page"}
_DATE_HEADERS = {"to-do", "todo", "todo_date", "todo_at", "student_todo_at", "date"}


def _with_local_todo(pages: list[dict], tz) -> list[dict]:
    """Attach a human-readable `todo_local` to each page for table/CSV display."""
    for page in pages:
        if isinstance(page, dict):  # a 204 from Canvas yields None
            page["todo_local"] = local_day(page.get("todo_date"), tz)
    return pages


@app.command("list")
def list_pages(
    course: str = typer.Option(None, "-c", "--course"),
    sort: str = typer.Option(None, "--sort", help="title, created_at, updated_at"),
    search: str = typer.Option(None, "--search"),
    published: bool = typer.Option(None, "--published"),
    has_todo: bool = typer.Option(
        None,
        "--has-todo/--no-todo",
        help="Only pages that do (or do not) carry a student to-do date",
    ),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List pages (summary only — bodies fetched via `show`)."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        params: dict = {}
        if sort:
            params["sort"] = sort
        if search:
            params["search_term"] = search
        if published is not None:
            params["published"] = published
        pages = client.get_all(f"/courses/{cid}/pages", params=params)
        # Canvas has no server-side filter for to-do state, so filter here.
        if has_todo is not None:
            pages = [p for p in pages if bool(p.get("todo_date")) is has_todo]
        if output != "json":
            pages = _with_local_todo(pages, resolve_timezone())
        emit(format_output(pages, PAGE_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("show")
def show_page(
    url: str = typer.Option(..., "--url", help="Page URL slug or ID"),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Show a single page (with body)."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        data = client.get(f"/courses/{cid}/pages/{url}")
        if output == "table":
            from ..utils.output import format_kv

            emit(format_kv(data))
        else:
            emit(format_output(data, [], output))
    except Exception as exc:
        raise handle_canvas_error(exc)


def _read_body(body: str | None, file: str | None) -> str | None:
    if body is not None:
        return body
    if file:
        return Path(file).read_text()
    return None


def _resolve_todo(
    todo: str | None,
    clear_todo: bool,
    tz_name: str | None,
    at_time: str,
) -> str | None:
    """Turn the `--todo` / `--clear-todo` flag pair into a payload value.

    Returns None to mean "don't touch the field" (so `prefix_keys` drops it),
    `CLEAR` to blank it, or a UTC ISO timestamp to set it.
    """
    if todo and clear_todo:
        raise typer.BadParameter("Pass either --todo or --clear-todo, not both.")
    if clear_todo:
        return CLEAR
    if todo:
        return to_canvas_datetime(todo, resolve_timezone(tz_name), at_time)
    return None


@app.command("create")
def create_page(
    title: str = typer.Option(..., "--title"),
    course: str = typer.Option(None, "-c", "--course"),
    body: str = typer.Option(None, "--body", help="HTML body"),
    file: str = typer.Option(None, "--file", help="Read body from a file (HTML)"),
    published: bool = typer.Option(False, "--published"),
    front_page: bool = typer.Option(False, "--front-page"),
    todo: str = typer.Option(
        None,
        "--todo",
        help="Add to students' To-Do list on this date (YYYY-MM-DD or ISO datetime)",
    ),
    at_time: str = typer.Option(
        "23:59", "--at-time", help="Time of day for a bare --todo date (HH:MM)"
    ),
    tz: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates (default: config or local)"
    ),
    editing_roles: str = typer.Option(
        None, "--editing-roles", help="teachers, students, members, public"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Create a new wiki page."""
    try:
        cid = get_course_id(course)
        payload = prefix_keys(
            "wiki_page",
            {
                "title": title,
                "body": _read_body(body, file),
                "published": published or None,
                "front_page": front_page or None,
                "editing_roles": editing_roles,
                "student_todo_at": _resolve_todo(todo, False, tz, at_time),
            },
        )
        if todo and not published:
            err_console.print(
                "[yellow]NOTE:[/yellow] the page is unpublished, so its to-do "
                "date stays invisible to students until you publish it."
            )
        if dry_run:
            emit(f"DRY-RUN: POST /courses/{cid}/pages payload={payload}")
            return
        client = get_client(verbose=verbose)
        result = client.post(f"/courses/{cid}/pages", data=payload)
        emit(format_output(_with_local_todo([result], resolve_timezone(tz)), PAGE_COLUMNS, "table"))
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("update")
def update_page(
    url: str = typer.Option(..., "--url"),
    course: str = typer.Option(None, "-c", "--course"),
    title: str = typer.Option(None, "--title"),
    body: str = typer.Option(None, "--body"),
    file: str = typer.Option(None, "--file"),
    published: bool = typer.Option(None, "--published"),
    todo: str = typer.Option(
        None,
        "--todo",
        help="Add to students' To-Do list on this date (YYYY-MM-DD or ISO datetime)",
    ),
    clear_todo: bool = typer.Option(
        False, "--clear-todo", help="Remove the page from students' To-Do list"
    ),
    at_time: str = typer.Option(
        "23:59", "--at-time", help="Time of day for a bare --todo date (HH:MM)"
    ),
    tz: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates (default: config or local)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Update an existing wiki page."""
    try:
        cid = get_course_id(course)
        payload = prefix_keys(
            "wiki_page",
            {
                "title": title,
                "body": _read_body(body, file),
                "published": published,
                "student_todo_at": _resolve_todo(todo, clear_todo, tz, at_time),
            },
        )
        if not payload:
            emit("No fields supplied — nothing to update.")
            return
        if dry_run:
            emit(f"DRY-RUN: PUT /courses/{cid}/pages/{url} payload={payload}")
            return
        client = get_client(verbose=verbose)
        result = client.put(f"/courses/{cid}/pages/{url}", payload)
        emit(format_output(_with_local_todo([result], resolve_timezone(tz)), PAGE_COLUMNS, "table"))
    except (typer.Exit, typer.BadParameter):
        raise
    except ValueError as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("delete")
def delete_page(
    url: str = typer.Option(..., "--url"),
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Delete a wiki page."""
    try:
        cid = get_course_id(course)
        confirm_or_abort(f"Delete page '{url}'?", yes=yes, dry_run=dry_run)
        client = get_client(verbose=verbose)
        client.delete(f"/courses/{cid}/pages/{url}")
        emit(f"Deleted page {url}.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("set-front")
def set_front_page(
    url: str = typer.Option(..., "--url"),
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Mark a page as the course's front page."""
    try:
        cid = get_course_id(course)
        if dry_run:
            emit(f"DRY-RUN: would set page '{url}' as front page in course {cid}")
            return
        client = get_client(verbose=verbose)
        client.put(
            f"/courses/{cid}/pages/{url}",
            {"wiki_page": {"front_page": True, "published": True}},
        )
        emit(f"Set page '{url}' as front page.")
    except Exception as exc:
        raise handle_canvas_error(exc)


# ---------------------------------------------------------------------------
# bulk-todo
# ---------------------------------------------------------------------------


def _pick(row: dict, candidates: set[str]) -> str | None:
    """Return the first value in `row` whose header matches `candidates`."""
    for key, value in row.items():
        if key and key.strip().lower() in candidates:
            return (value or "").strip() if isinstance(value, str) else value
    return None


def _load_schedule_file(path: str) -> list[tuple[str, str]]:
    """Read `(url, raw_date)` pairs from a CSV or JSON schedule file.

    CSV is the round-trip format: `pages list -o csv` writes a `URL` and a
    `To-Do` column, and this reads them back. JSON accepts the shape emitted
    by `pages list -o json`.
    """
    raw = Path(path).read_text()
    suffix = Path(path).suffix.lower()

    rows: list[dict]
    if suffix == ".json":
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError(f"{path}: expected a JSON array of page objects.")
        rows = parsed
    else:
        rows = list(csv.DictReader(io.StringIO(raw)))

    pairs: list[tuple[str, str]] = []
    for index, row in enumerate(rows, start=2):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {index} is not an object.")
        url = _pick(row, _URL_HEADERS)
        if not url:
            raise ValueError(
                f"{path}: row {index} has no URL column. Expected one of: "
                f"{', '.join(sorted(_URL_HEADERS))}."
            )
        pairs.append((str(url), str(_pick(row, _DATE_HEADERS) or "")))
    return pairs


def _pages_in_module(client, cid: int, needle: str) -> list[str]:
    """Return page slugs belonging to modules whose name contains `needle`."""
    modules = client.get_all(f"/courses/{cid}/modules", params={"include[]": "items"})
    matched = [m for m in modules if needle.lower() in (m.get("name") or "").lower()]
    if not matched:
        names = ", ".join(repr(m.get("name")) for m in modules) or "(none)"
        raise ValueError(f"No module matches {needle!r}. Modules: {names}")
    slugs: list[str] = []
    for module in matched:
        for item in module.get("items") or []:
            if item.get("type") == "Page" and item.get("page_url"):
                slugs.append(item["page_url"])
    return slugs


@app.command("bulk-todo")
def bulk_todo(
    course: str = typer.Option(None, "-c", "--course"),
    # --- selection -------------------------------------------------------
    all_pages: bool = typer.Option(False, "--all", help="Select every page"),
    urls: str = typer.Option(None, "--url", help="Comma-separated page slugs"),
    module: str = typer.Option(None, "--module", help="Pages inside a matching module"),
    search: str = typer.Option(None, "--search", help="Pages whose title matches"),
    file: str = typer.Option(
        None, "--file", help="CSV/JSON of url + to-do date (round-trips `list -o csv`)"
    ),
    # --- action ----------------------------------------------------------
    at: str = typer.Option(None, "--at", help="Set this date on every selected page"),
    start: str = typer.Option(None, "--start", help="First date of a cadence"),
    every: str = typer.Option(
        None, "--every", help="Cadence step, e.g. 7d — used with --start"
    ),
    shift: str = typer.Option(
        None, "--shift", help="Move existing to-do dates by a duration, e.g. 7d"
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove to-do dates"),
    # --- modifiers -------------------------------------------------------
    at_time: str = typer.Option(
        "23:59", "--at-time", help="Time of day for bare dates (HH:MM)"
    ),
    tz: str = typer.Option(
        None, "--tz", help="IANA timezone for bare dates (default: config or local)"
    ),
    clear_blanks: bool = typer.Option(
        False,
        "--clear-blanks",
        help="With --file, treat an empty date cell as 'clear' instead of 'skip'",
    ),
    publish: bool = typer.Option(
        False, "--publish", help="Also publish selected pages (to-dos need it)"
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Set, shift, or clear student To-Do dates across many pages at once."""
    try:
        cid = get_course_id(course)
        zone = resolve_timezone(tz)

        selectors = [
            ("--all", all_pages),
            ("--url", bool(urls)),
            ("--module", bool(module)),
            ("--search", bool(search)),
            ("--file", bool(file)),
        ]
        chosen = [name for name, active in selectors if active]
        if len(chosen) != 1:
            raise typer.BadParameter(
                "Pass exactly one of --all, --url, --module, --search, --file "
                f"(got: {', '.join(chosen) or 'none'})."
            )

        actions = [
            ("--at", bool(at)),
            ("--start/--every", bool(start) or bool(every)),
            ("--shift", bool(shift)),
            ("--clear", clear),
            ("--file", bool(file)),
        ]
        chosen_actions = [name for name, active in actions if active]
        if len(chosen_actions) != 1:
            raise typer.BadParameter(
                "Pass exactly one action: --at, --start with --every, --shift, "
                f"--clear, or --file (got: {', '.join(chosen_actions) or 'none'})."
            )
        if bool(start) != bool(every):
            raise typer.BadParameter("--start and --every must be used together.")

        client = get_client(verbose=verbose)

        # -- resolve the target pages ------------------------------------
        file_dates: dict[str, str] = {}
        if file:
            pairs = _load_schedule_file(file)
            file_dates = dict(pairs)
            wanted = [url for url, _ in pairs]
        elif urls:
            wanted = [u.strip() for u in urls.split(",") if u.strip()]
        elif module:
            wanted = _pages_in_module(client, cid, module)
        else:
            wanted = []

        params = {"search_term": search} if search else {}
        catalog = client.get_all(f"/courses/{cid}/pages", params=params)
        by_url = {p["url"]: p for p in catalog}

        if wanted:
            missing = [u for u in wanted if u not in by_url]
            if missing:
                raise ValueError(
                    f"No page with slug(s): {', '.join(missing)}. "
                    "Slugs come from the URL column of `pages list`."
                )
            targets = [by_url[u] for u in wanted]
        else:
            targets = catalog

        if not targets:
            emit("No pages matched the selection.")
            return

        # -- compute the new value for each ------------------------------
        step = parse_shift(every) if every else None
        delta = parse_shift(shift) if shift else None
        cursor = to_canvas_datetime(start, zone, at_time) if start else None
        fixed = to_canvas_datetime(at, zone, at_time) if at else None

        plan: list[dict] = []
        for page in targets:
            current = page.get("todo_date")
            if clear:
                new_value: str | None = CLEAR
            elif fixed:
                new_value = fixed
            elif cursor:
                new_value = cursor
                cursor = shift_iso(cursor, step)
            elif delta:
                # Nothing to shift on a page that has no date yet.
                new_value = shift_iso(current, delta) if current else None
            else:  # --file
                raw = file_dates.get(page["url"], "")
                if raw:
                    new_value = to_canvas_datetime(raw, zone, at_time)
                elif clear_blanks:
                    new_value = CLEAR
                else:
                    new_value = None

            if new_value is None or new_value == (current or CLEAR):
                continue
            plan.append(
                {
                    "url": page["url"],
                    "title": page.get("title", ""),
                    "published": page.get("published"),
                    "current_local": local_day(current, zone) or "—",
                    "new_local": local_day(new_value, zone) or "(cleared)",
                    "value": new_value,
                }
            )

        if not plan:
            emit(f"No changes needed — all {len(targets)} selected page(s) already match.")
            return

        emit(format_output(plan, PLAN_COLUMNS, "table"))
        emit(f"\nTimezone: {zone} — bare dates anchored at {at_time}.")

        # -- pre-flight warnings (report, never block) -------------------
        unpublished = [p for p in plan if not p["published"] and p["value"] != CLEAR]
        if unpublished and not publish:
            err_console.print(
                f"[yellow]WARNING:[/yellow] {len(unpublished)} of {len(plan)} "
                "page(s) are unpublished — their to-do dates will not reach "
                "students until published. Re-run with --publish to publish "
                "them as part of this change."
            )

        confirm_or_abort(
            f"Update to-do dates on {len(plan)} page(s) in course {cid}?",
            yes,
            dry_run,
        )

        for entry in plan:
            inner: dict = {"student_todo_at": entry["value"]}
            if publish and not entry["published"]:
                inner["published"] = True
            client.put(f"/courses/{cid}/pages/{entry['url']}", {"wiki_page": inner})
        emit(f"Updated to-do dates on {len(plan)} page(s).")
    except (typer.Exit, typer.BadParameter):
        raise
    except (ValueError, OSError) as exc:  # JSONDecodeError subclasses ValueError
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)
