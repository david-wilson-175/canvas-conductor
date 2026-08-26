"""Module item completion requirements: must-view, must-submit, must-mark-done,
min-score, and min-percentage.

The `modules` command group can publish/unpublish modules and list their
items, but nothing in `commands/modules.py` can set a *completion
requirement* on an individual module item — the thing that gives a reading,
quiz, or assignment a checkbox on the Modules page and makes it count toward
"Student Progress." This group fills that gap.

Kept as its own top-level group rather than folded into `modules`, matching
how `tabs`, `sections`, and `groups` are separate groups despite being course
sub-resources. Originally contributed as an extension and promoted to the core
surface on 2026-08-26; the CLI surface did not change in the move.

Canvas API background
----------------------
Completion requirements live on the *module item*, not the module or the
underlying content object, via:

    PUT /courses/:course_id/modules/:module_id/items/:id
        module_item[completion_requirement][type]           one of:
            must_view | must_contribute | must_submit |
            must_mark_done | min_score | min_percentage
        module_item[completion_requirement][min_score]        (min_score only)
        module_item[completion_requirement][min_percentage]   (min_percentage only)

A few gotchas worth knowing before you script this at scale:

- `must_mark_done` works on *any* item type regardless of whether the
  underlying content supports submissions — it just adds a manual
  "Mark as done" button the student clicks. This is the right choice for
  content with no submission mechanism at all (e.g. an assignment whose
  `submission_types` is `["none"]`, used for an in-person exam or a
  Testing-Center-only grade): `must_submit` would be permanently
  unsatisfiable there since Canvas never records a submission for it.
- `must_submit` requires the item to actually accept a submission
  (an Assignment, Quiz, or graded Discussion with a real submission type).
  Setting it on a `SubHeader` or a no-submission assignment will fail
  (422) — these commands skip `SubHeader` items automatically and report
  any other rejection per-item rather than aborting the whole batch.
- A requirement on an item inside an *unpublished* module, or on an
  unpublished item, is inert: students see no checkbox and no progress is
  tracked until both the module and the item are published. `bulk-set`
  warns (never blocks) when this is about to be the case, mirroring how
  `pages bulk-todo` warns about unpublished to-do dates.
- Group assignments: Canvas records a submission at the group level, so
  one member submitting satisfies `must_submit` for every teammate.

Commands
--------
  conductor requirements list       # see items + their current requirement
  conductor requirements set        # set/clear on exactly one item
  conductor requirements bulk-set   # set the same requirement across many
                                     # items selected by module/title/type
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import typer

from canvas_conductor.client import get_client
from canvas_conductor.commands._common import (
    confirm_or_abort,
    emit,
    err_console,
    handle_canvas_error,
)
from canvas_conductor.config import get_course_id
from canvas_conductor.utils.output import format_output

app = typer.Typer(
    name="requirements",
    help="Manage module item completion requirements (mark-as-done, must-submit, etc.)",
    no_args_is_help=True,
)


VALID_TYPES = {
    "must_view",
    "must_contribute",
    "must_submit",
    "must_mark_done",
    "min_score",
    "min_percentage",
}

# Canvas rejects a completion_requirement on a SubHeader (it's a label, not
# content) — auto-skip these rather than surfacing a confusing 422 per item.
NO_REQUIREMENT_TYPES = {"SubHeader"}

LIST_COLUMNS = [
    ("Module", "module_name"),
    ("Item", "title"),
    ("Type", "type"),
    ("Requirement", "req_summary"),
    ("Published", "published"),
    ("Module ID", "module_id"),
    ("Item ID", "id"),
]

PLAN_COLUMNS = [
    ("Module", "module_name"),
    ("Item", "title"),
    ("Item Type", "type"),
    ("Published", "published"),
    ("New Requirement", "req_summary"),
]


def _req_summary(item: dict) -> str:
    req = item.get("completion_requirement")
    if not req:
        return "—"
    kind = req.get("type", "")
    if kind == "min_score" and req.get("min_score") is not None:
        return f"min_score ({req['min_score']})"
    if kind == "min_percentage" and req.get("min_percentage") is not None:
        return f"min_percentage ({req['min_percentage']}%)"
    return kind


def _fetch_modules_with_items(client, cid: int) -> list[dict]:
    return client.get_all(f"/courses/{cid}/modules", params={"include[]": "items"})


def _flatten_items(modules: list[dict]) -> list[dict]:
    """Return one row per module item, each tagged with its module's name/id."""
    rows: list[dict] = []
    for module in modules:
        for item in module.get("items") or []:
            row = dict(item)
            row["module_name"] = module.get("name", "")
            row["module_id"] = module.get("id")
            row["req_summary"] = _req_summary(item)
            rows.append(row)
    return rows


def _matches_any(haystack: str, needles: list[str]) -> bool:
    haystack = haystack.lower()
    return any(n.lower() in haystack for n in needles)


@app.command("list")
def list_requirements(
    course: str = typer.Option(None, "-c", "--course"),
    module: str = typer.Option(
        None, "--module", help="Comma-separated module-name substrings to filter to"
    ),
    item_type: str = typer.Option(
        None,
        "--item-type",
        help="Comma-separated item types to filter to, e.g. Page,Assignment",
    ),
    missing_only: bool = typer.Option(
        False, "--missing-only", help="Only items with no completion requirement set"
    ),
    output: str = typer.Option("table", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """List module items across the course with their current requirement."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        modules = _fetch_modules_with_items(client, cid)

        if module:
            needles = [m.strip() for m in module.split(",") if m.strip()]
            modules = [m for m in modules if _matches_any(m.get("name", ""), needles)]

        rows = _flatten_items(modules)

        if item_type:
            types = {t.strip() for t in item_type.split(",") if t.strip()}
            rows = [r for r in rows if r.get("type") in types]

        if missing_only:
            rows = [r for r in rows if not r.get("completion_requirement")]

        emit(format_output(rows, LIST_COLUMNS, output))
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("set")
def set_requirement(
    module_id: int = typer.Option(..., "--module-id", help="Module ID (see `modules list`)"),
    item_id: int = typer.Option(..., "--item-id", help="Module item ID (see `requirements list`)"),
    course: str = typer.Option(None, "-c", "--course"),
    type: str = typer.Option(
        None,
        "--type",
        help=f"One of: {', '.join(sorted(VALID_TYPES))}",
    ),
    min_score: float = typer.Option(None, "--min-score", help="Required with --type min_score"),
    min_percentage: float = typer.Option(
        None, "--min-percentage", help="Required with --type min_percentage"
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the completion requirement"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Set or clear the completion requirement on exactly one module item."""
    try:
        if bool(type) == clear:
            raise typer.BadParameter("Pass exactly one of --type or --clear.")
        if type and type not in VALID_TYPES:
            raise typer.BadParameter(f"--type must be one of: {', '.join(sorted(VALID_TYPES))}")
        if type == "min_score" and min_score is None:
            raise typer.BadParameter("--type min_score requires --min-score")
        if type == "min_percentage" and min_percentage is None:
            raise typer.BadParameter("--type min_percentage requires --min-percentage")

        cid = get_course_id(course)
        client = get_client(verbose=verbose)

        if clear:
            # Canvas has no dedicated "remove requirement" call; sending an
            # empty completion_requirement object clears it.
            inner: dict[str, Any] = {"completion_requirement": {}}
            action_desc = "Clear the completion requirement"
        else:
            req: dict[str, Any] = {"type": type}
            if type == "min_score":
                req["min_score"] = min_score
            if type == "min_percentage":
                req["min_percentage"] = min_percentage
            inner = {"completion_requirement": req}
            action_desc = f"Set requirement '{type}'"

        confirm_or_abort(
            f"{action_desc} on module {module_id} item {item_id} (course {cid})?",
            yes,
            dry_run,
        )

        result = client.put(
            f"/courses/{cid}/modules/{module_id}/items/{item_id}",
            {"module_item": inner},
        )
        row = dict(result or {})
        row["module_name"] = ""
        row["module_id"] = module_id
        row["req_summary"] = _req_summary(row)
        emit(format_output([row], LIST_COLUMNS, "table"))
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)


def _load_file_rows(path: str) -> list[dict]:
    """Read override rows from a CSV/JSON file for `bulk-set --file`.

    Expected columns/keys (case-insensitive): module_id, item_id or id,
    title (optional, for readability only), type, min_score/min_percentage
    (optional). Round-trips `requirements list -o csv` once you've filled
    in a Type column by hand.
    """
    raw = Path(path).read_text()
    suffix = Path(path).suffix.lower()
    rows: list[dict]
    if suffix == ".json":
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError(f"{path}: expected a JSON array of rows.")
        rows = parsed
    else:
        rows = list(csv.DictReader(io.StringIO(raw)))

    out: list[dict] = []
    for index, row in enumerate(rows, start=2):
        lower = {(k or "").strip().lower(): v for k, v in row.items()}
        module_id = lower.get("module_id") or lower.get("module id")
        item_id = lower.get("item_id") or lower.get("item id") or lower.get("id")
        req_type = (lower.get("type") or lower.get("requirement") or "").strip()
        if not module_id or not item_id:
            raise ValueError(f"{path}: row {index} is missing module_id or item_id.")
        if not req_type:
            # Blank Type cell means "leave this one alone" — same convention
            # as pages bulk-todo's blank-date-means-skip default.
            continue
        if req_type not in VALID_TYPES:
            raise ValueError(
                f"{path}: row {index} has type={req_type!r}, expected one of "
                f"{', '.join(sorted(VALID_TYPES))}."
            )
        out.append(
            {
                "module_id": int(module_id),
                "item_id": int(item_id),
                "type": req_type,
                "min_score": lower.get("min_score") or None,
                "min_percentage": lower.get("min_percentage") or None,
            }
        )
    return out


@app.command("bulk-set")
def bulk_set(
    course: str = typer.Option(None, "-c", "--course"),
    # --- selection ---------------------------------------------------
    module: str = typer.Option(
        None, "--module", help="Comma-separated module-name substrings to select from"
    ),
    search: str = typer.Option(
        None, "--search", help="Only items whose title contains this substring"
    ),
    item_type: str = typer.Option(
        None,
        "--item-type",
        help="Comma-separated item types to include, e.g. Page,Assignment,Quiz",
    ),
    item_ids: str = typer.Option(
        None, "--item-ids", help="Comma-separated explicit module item IDs"
    ),
    exclude: str = typer.Option(
        None, "--exclude", help="Comma-separated title substrings or item IDs to skip"
    ),
    file: str = typer.Option(
        None,
        "--file",
        help="CSV/JSON of module_id,item_id,type rows (overrides other selectors)",
    ),
    # --- action --------------------------------------------------------
    type: str = typer.Option(
        None,
        "--type",
        help=f"One of: {', '.join(sorted(VALID_TYPES))} (ignored with --file)",
    ),
    min_score: float = typer.Option(None, "--min-score", help="Required with --type min_score"),
    min_percentage: float = typer.Option(
        None, "--min-percentage", help="Required with --type min_percentage"
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove requirements instead of setting"),
    dry_run: bool = typer.Option(True, "--dry-run/--commit"),
    yes: bool = typer.Option(False, "-y", "--yes"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Set (or clear) the same completion requirement across many module items."""
    try:
        if not file:
            if bool(type) == clear:
                raise typer.BadParameter("Pass exactly one of --type or --clear (or use --file).")
            if type and type not in VALID_TYPES:
                raise typer.BadParameter(
                    f"--type must be one of: {', '.join(sorted(VALID_TYPES))}"
                )
            if type == "min_score" and min_score is None:
                raise typer.BadParameter("--type min_score requires --min-score")
            if type == "min_percentage" and min_percentage is None:
                raise typer.BadParameter("--type min_percentage requires --min-percentage")

        cid = get_course_id(course)
        client = get_client(verbose=verbose)

        plan: list[dict] = []

        if file:
            for row in _load_file_rows(file):
                plan.append(
                    {
                        "module_id": row["module_id"],
                        "id": row["item_id"],
                        "title": "",
                        "module_name": "",
                        "type": "",
                        "published": None,
                        "new_type": row["type"],
                        "new_min_score": row["min_score"],
                        "new_min_percentage": row["min_percentage"],
                    }
                )
        else:
            modules = _fetch_modules_with_items(client, cid)
            if module:
                needles = [m.strip() for m in module.split(",") if m.strip()]
                modules = [m for m in modules if _matches_any(m.get("name", ""), needles)]

            rows = _flatten_items(modules)

            if item_ids:
                wanted_ids = {int(i.strip()) for i in item_ids.split(",") if i.strip()}
                rows = [r for r in rows if r.get("id") in wanted_ids]
            if item_type:
                types_wanted = {t.strip() for t in item_type.split(",") if t.strip()}
                rows = [r for r in rows if r.get("type") in types_wanted]
            if search:
                rows = [r for r in rows if search.lower() in (r.get("title") or "").lower()]
            if exclude:
                excl = [e.strip() for e in exclude.split(",") if e.strip()]
                excl_ids = {e for e in excl if e.isdigit()}
                excl_text = [e for e in excl if not e.isdigit()]
                rows = [
                    r
                    for r in rows
                    if str(r.get("id")) not in excl_ids
                    and not _matches_any(r.get("title") or "", excl_text)
                ]

            skipped_subheaders = [r for r in rows if r.get("type") in NO_REQUIREMENT_TYPES]
            rows = [r for r in rows if r.get("type") not in NO_REQUIREMENT_TYPES]
            if skipped_subheaders:
                names = ", ".join(r.get("title", "") for r in skipped_subheaders)
                err_console.print(
                    f"[yellow]NOTE:[/yellow] skipping {len(skipped_subheaders)} "
                    f"SubHeader item(s) — headers can't carry a requirement: {names}"
                )

            for r in rows:
                plan.append(
                    {
                        "module_id": r["module_id"],
                        "id": r["id"],
                        "title": r.get("title", ""),
                        "module_name": r.get("module_name", ""),
                        "type": r.get("type", ""),
                        "published": r.get("published"),
                        "new_type": None if clear else type,
                        "new_min_score": min_score,
                        "new_min_percentage": min_percentage,
                    }
                )

        if not plan:
            emit("No module items matched the selection.")
            return

        for entry in plan:
            if clear or entry.get("new_type") is None and file:
                entry["req_summary"] = "(cleared)" if clear else "—"
            else:
                fake = {"completion_requirement": {"type": entry["new_type"]}}
                if entry["new_type"] == "min_score":
                    fake["completion_requirement"]["min_score"] = entry["new_min_score"]
                if entry["new_type"] == "min_percentage":
                    fake["completion_requirement"]["min_percentage"] = entry["new_min_percentage"]
                entry["req_summary"] = _req_summary(fake)

        emit(format_output(plan, PLAN_COLUMNS, "table"))

        unpublished = [p for p in plan if p.get("published") is False]
        if unpublished:
            err_console.print(
                f"[yellow]WARNING:[/yellow] {len(unpublished)} of {len(plan)} "
                "selected item(s) are unpublished — their requirement won't be "
                "visible or trackable until the item (and its module) is published."
            )

        verb = "Clear" if clear else "Set"
        confirm_or_abort(
            f"{verb} completion requirements on {len(plan)} module item(s) in course {cid}?",
            yes,
            dry_run,
        )

        succeeded = 0
        for entry in plan:
            if clear:
                inner: dict[str, Any] = {"completion_requirement": {}}
            else:
                req: dict[str, Any] = {"type": entry["new_type"]}
                if entry["new_type"] == "min_score":
                    req["min_score"] = entry["new_min_score"]
                if entry["new_type"] == "min_percentage":
                    req["min_percentage"] = entry["new_min_percentage"]
                inner = {"completion_requirement": req}
            try:
                client.put(
                    f"/courses/{cid}/modules/{entry['module_id']}/items/{entry['id']}",
                    {"module_item": inner},
                )
                succeeded += 1
            except Exception as exc:  # keep going; report per-item failures
                err_console.print(
                    f"[red]FAILED:[/red] module {entry['module_id']} item "
                    f"{entry['id']} ({entry.get('title', '')}): {exc}"
                )

        emit(f"Updated {succeeded}/{len(plan)} module item(s).")
    except (typer.Exit, typer.BadParameter):
        raise
    except (ValueError, OSError) as exc:
        err_console.print(f"[red]ERROR:[/red] {exc}")
        raise typer.Exit(code=2)
    except Exception as exc:
        raise handle_canvas_error(exc)
