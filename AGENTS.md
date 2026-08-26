# AGENTS.md — Canvas Conductor

You are an LLM agent working in this repository. Read this file before
writing code or running commands. It's the fastest on-ramp to working
productively with Canvas Conductor.

If you've been asked to do something the CLI already supports (list pages,
upload a file, grade submissions), prefer running an existing command over
writing new code. If you've been asked to add new functionality, write it
as a Conductor *extension* rather than a separate script.

## What this CLI is (and isn't)

Canvas Conductor wraps the Canvas LMS REST API into composable
terminal commands. It targets one human user (course designer / instructor)
managing one or a handful of courses; it is not a multi-tenant service or a
library for production traffic. The ergonomics are tuned for "I want to do X
to my course" rather than for high-throughput automation.

In scope: read/write of pages, modules, assignments, files, submissions,
enrollments, tabs, discussions, assignment groups. Two-step file uploads.
Pagination, retries, rate-limit awareness. Idempotent create-or-update
patterns. JSON/CSV output for piping into other tools. User-defined
extensions for project-specific workflows.

Out of scope: account-level admin operations, SIS imports, the New Quizzes
API (different surface), real-time webhooks, anything that needs a service
account. The configured token is teacher-level; admin endpoints will 403.

## 30-second mental model

Three things are always true:

1. **Credentials live in `.env`** at the project root. Two variables only:
   `CANVAS_BASE_URL` (no trailing slash) and `CANVAS_TOKEN`. Loaded by
   `python-dotenv` via `config.load_env()`. Never hardcode either.

2. **Courses are referenced by alias**, not by numeric id. Aliases are
   defined in `config.toml` under `[courses.<alias>]` blocks. Every command
   takes `-c/--course <alias>`; if exactly one course is configured, the
   flag is optional. Always resolve with `get_course_id(course)` — never
   parse the TOML yourself.

3. **The CLI is a Typer app.** `cli.py` registers built-in command groups
   from `commands/*.py`, then auto-discovers extensions from
   `extensions/*.py` (files starting with `_` are ignored). Each module
   exposes a module-level `app = typer.Typer(name=..., help=...)` and
   decorates functions with `@app.command(...)`.

## Where to look first

```
canvas-conductor/
├── canvas_conductor/
│   ├── cli.py              # Top-level Typer app, command group registration
│   ├── client.py           # CanvasClient — HTTP, pagination, retries, upload
│   ├── config.py           # .env + config.toml loading, course alias resolution
│   ├── exceptions.py       # CanvasError + typed subclasses (Auth/NotFound/…)
│   ├── models.py           # Pydantic response models (used sparingly)
│   ├── utils/output.py     # format_output (table/json/csv) and friends
│   ├── utils/dates.py      # Bare-date → Canvas UTC conversion, tz resolution, shifts
│   ├── commands/           # One module per command group
│   │   ├── _common.py      # emit, handle_canvas_error, confirm_or_abort, …
│   │   ├── pages.py        # ← Best reference for the standard command pattern
│   │   ├── modules.py
│   │   ├── files.py
│   │   └── …
│   └── extensions/
│       ├── __init__.py     # Auto-discovery loader (don't edit unless fixing it)
│       ├── _example.py     # Minimal extension template
│       └── playbook.py     # ← Best reference for a real, multi-command extension
├── config.toml             # Course aliases + [defaults] + project-specific sections
├── .env                    # Credentials (gitignored)
├── pyproject.toml          # Deps + the `conductor` script entry point
└── tests/                  # pytest suite, uses `responses` library for HTTP mocks
```

When you don't know how to do X: grep `commands/` and `extensions/` for
similar patterns first. If you're about to write your first extension, read
`commands/pages.py` end-to-end (it exercises every common pattern: list,
show, create, update, delete, with bodies-from-files and dry-run).

## Core command group or extension?

Decide this before you write the file. The rule follows the scope statement
above: **core is generic Canvas capability, extensions are project-specific
workflow.**

Put it in `commands/` when it wraps a Canvas API surface that any instructor
would want, and its shape is dictated by the API rather than by your course.
Register it in `cli.py` with an explicit `add_typer(..., name=...)`.

Put it in `extensions/` when it encodes decisions specific to one course,
committee, or content pipeline — hardcoded week structures, a particular
markdown layout, a named media registry. `playbook.py` is the reference case:
it only makes sense for one real Canvas course.

Two practical consequences of getting this wrong:

- `extensions/` is **private by default in git**. Its `.gitignore` ignores
  `*.py` and whitelists tracked files by name, so a generic feature parked
  there has to be explicitly un-ignored just to ship. If you find yourself
  adding a `!whitelist.py` line for something every user would want, it
  belongs in `commands/`.
- Extensions load *after* core groups and silently shadow a core group that
  shares their name. Generic functionality in the extension namespace makes
  that collision more likely.

A course sub-resource does not have to live inside its parent's group.
`tabs`, `sections`, `groups`, `requirements`, and `student-groups` are all
top-level groups despite hanging off a course. Prefer a new top-level group
over nesting; no core group nests a sub-group today.

If a tracked extension turns out to be generic, promote it: `git mv` into
`commands/`, register it in `cli.py` **under the same group name**, and drop
its `.gitignore` whitelist line. Keeping the name is what makes the promotion
invisible to users. `requirements` and `student-groups` were promoted this way
on 2026-08-26.

## The extension pattern

Drop a file into `canvas_conductor/extensions/`. The discovery loader
imports it and registers any module-level `app = typer.Typer(...)` as a
subcommand group. Files starting with `_` are skipped (use them for
templates or shared helpers).

### Extensions that live outside the repo

Set `CONDUCTOR_EXTENSIONS_DIR` to a directory and the loader scans it too,
after the bundled ones. This is the counterpart to `CONDUCTOR_CONFIG`: it
lets a project-specific extension live with the project it serves instead of
inside this general-purpose repo.

```bash
export CONDUCTOR_EXTENSIONS_DIR="$HOME/some-project/extensions"
```

Semantics:

- Unset or empty behaves exactly as before. No warning.
- A path that is missing, or is a file rather than a directory, produces one
  stderr warning; the bundled extensions and the whole core CLI still load.
- Files starting with `_` are skipped there too.
- A module that raises on import warns and is skipped, like a bundled one.

Two clashes, handled differently:

- **Same filename in both directories** is an *override*, not a conflict.
  The external file wins, loads exactly once, and nothing is warned. This is
  how you replace a bundled extension locally.
- **Two different modules claiming the same Typer group name** is a real
  collision, warns on stderr, and the later registration shadows the earlier
  one, exactly as before.

Extensions loaded this way are location-independent: paths inside them should
resolve against the config file (`find_config_file()`) or absolute config
values, never against `__file__`.

Minimum viable extension:

```python
# canvas_conductor/extensions/grades_export.py
"""Export per-student grades to CSV for one assignment."""
from __future__ import annotations

import typer

from canvas_conductor.client import get_client
from canvas_conductor.commands._common import emit, handle_canvas_error
from canvas_conductor.config import get_course_id
from canvas_conductor.utils.output import format_output

app = typer.Typer(name="grades-export", help="Export grades for analysis.")


@app.command("assignment")
def export_assignment(
    assignment_id: int = typer.Option(..., "--id"),
    course: str = typer.Option(None, "-c", "--course"),
    output: str = typer.Option("csv", "-o", "--output"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Export a single assignment's submissions."""
    try:
        client = get_client(verbose=verbose)
        cid = get_course_id(course)
        subs = client.get_all(
            f"/courses/{cid}/assignments/{assignment_id}/submissions",
            params={"include[]": "user"},
        )
        cols = [
            ("User", "user.name"),
            ("Login", "user.login_id"),
            ("Score", "score"),
            ("Grade", "grade"),
            ("State", "workflow_state"),
        ]
        emit(format_output(subs, cols, output))
    except Exception as exc:
        raise handle_canvas_error(exc)
```

Now `uv run conductor grades-export assignment --id 12345 -c is402` works.
No registration step required.

Required pieces:
- Module-level `app = typer.Typer(name="<group-name>", help="…")`. The
  `name` becomes the subcommand. Use kebab-case if you want hyphens.
- Each command function decorated with `@app.command("verb")`.
- Standard flags on every command: `-c/--course`, `-o/--output`,
  `-v/--verbose`, `--dry-run`, `-y/--yes`. Match the existing CLI surface.
- Wrap the body in `try: … except Exception as exc: raise handle_canvas_error(exc)`.
  This produces consistent exit codes per error class.

For a richer example that includes file uploads, complex per-call payloads,
and post-processing of HTML, read `extensions/playbook.py`.

Files dropped into `extensions/` are git-ignored by default (the local
`.gitignore` allowlists tracked examples). If you're writing an extension
that should be part of the public repo, add a `!filename.py` line to
`extensions/.gitignore` after creating the file.

## CanvasClient quick reference

`from canvas_conductor.client import get_client` — returns a configured
`CanvasClient` from the env. Always prefer this over instantiating directly.

| Method | Use it for |
|---|---|
| `client.get(path, params=None)` | Single GET. Returns parsed JSON or `None`. |
| `client.get_all(path, params=None)` | GET with automatic Link-header pagination. Returns a list. Use this any time the endpoint is paginated (most listings are). |
| `client.post(path, data=None, files=None, json=None)` | POST. `data=` is JSON-encoded as the body unless `files=` is set (multipart). Use `data=` for normal Canvas requests. |
| `client.put(path, data=None)` | PUT. `data=` is JSON-encoded. |
| `client.delete(path)` | DELETE. Returns `True` on success. |
| `client.upload_file(course_id, local_path, folder_path=None, content_type=None, on_duplicate="rename")` | Implements Canvas's two-step upload. Returns the final file dict from the second-leg response (with `id`, `display_name`, `size`, `url`, etc.). |

Path conventions:
- Pass paths as `f"/courses/{cid}/pages"` (leading slash). The client
  prepends `https://{base_url}/api/v1`.
- For full URLs (e.g. follow-up requests to S3-style upload endpoints), the
  client passes them through unchanged.

Payload shapes: Canvas accepts both nested JSON and flat bracket-style keys
(`{"wiki_page[title]": "Hi"}`). The codebase uses flat bracket style by
convention. There's a helper for it (`prefix_keys`, below) but inlining the
keys is also common — match the surrounding file's style.

Errors raise typed exceptions:
- `CanvasAuthError` (401), `CanvasPermissionError` (403),
  `CanvasNotFoundError` (404), `CanvasValidationError` (422),
  `CanvasRateLimitError` (429), `CanvasServerError` (5xx),
  generic `CanvasError` for everything else, `ConfigError` for
  configuration problems. All inherit from `Exception`.
- The retry/backoff loop transparently handles 429 and 5xx for up to 3
  attempts before the exception escapes.

## Configuration helpers

`from canvas_conductor.config import …`:

| Function | Use it for |
|---|---|
| `get_course_id(course_key: str \| None) -> int` | The only correct way to resolve `--course`. Auto-uses the single configured course if `course_key` is None and exactly one is defined. Raises `ConfigError` with a helpful message otherwise. |
| `get_config() -> dict` | Whole parsed `config.toml` (lru-cached). Use it to read project-specific sections like `[playbook]`. |
| `find_config_file() -> Path \| None` | Walks upward from cwd to find `config.toml`. Useful for resolving paths that are stored in TOML relative to the config file's location. |
| `require_credentials() -> tuple[str, str]` | Returns `(base_url, token)` or raises `ConfigError`. Usually called for you by `get_client()`; only call directly if you need raw credentials. |
| `redact_token(token) -> str` | Redacts a token for safe logging. Use it any time you might print credentials. |

Don't `os.environ` Canvas vars directly — `config.load_env()` does the
loading and respects the walk-up-to-find-`.env` convention.

## Output and error helpers

`from canvas_conductor.commands._common import …`:

| Helper | What it does |
|---|---|
| `emit(text)` | Print to stdout, ensure trailing newline. Use this instead of `print()` for user-facing output. |
| `handle_canvas_error(exc)` | Maps an exception to a `typer.Exit(code=N)` with a formatted error to stderr. Pattern: `raise handle_canvas_error(exc)` from inside an `except`. |
| `confirm_or_abort(message, yes, dry_run)` | Standard destructive-op gate. Call before any delete/overwrite. Honors `--dry-run` and `-y/--yes`. |
| `parse_kv_list("k=v,k2=v2")` | Parse a comma-separated key=value flag value. |
| `prefix_keys("wiki_page", {"title": "Hi"})` | Wraps each key in `wiki_page[…]` form for Canvas. Returns a dict you can pass to `client.post(data=…)`. |

`from canvas_conductor.utils.dates import …` — anything touching a date:

| Helper | What it does |
|---|---|
| `to_canvas_datetime(value, tz=None, at_time="23:59")` | Bare `YYYY-MM-DD` → UTC ISO anchored at end-of-day *local*. Full ISO datetimes pass through, respecting an explicit offset. Never hand-build a timestamp instead of calling this. |
| `resolve_timezone(explicit=None)` | `--tz` flag → `[defaults] timezone` → system local → UTC. Raises `ConfigError` on an unknown zone rather than silently shifting every date. |
| `local_day(value, tz=None)` | Canvas UTC timestamp → `YYYY-MM-DD HH:MM` local, for display. Round-trips back through `to_canvas_datetime`. |
| `parse_shift("7d")` / `shift_iso(value, delta)` | Duration parsing and None-safe timestamp shifting, shared by `assignments bulk-dates` and `pages bulk-todo`. |
| `CLEAR` | The empty-string sentinel that clears a Canvas date field. `None` means "leave alone" — `prefix_keys` drops it. |

`from canvas_conductor.utils.output import format_output, format_kv`:

`format_output(items, columns, output_format)` — returns a string in
table/json/csv form. `columns` is `[("Display Name", "field.path"), …]` with
dotted paths supported (`"user.login_id"`).

## Idiomatic patterns

**Idempotent create-by-name.** Don't blindly POST — search first, reuse
existing resources. The standard shape:

```python
existing = client.get_all(f"/courses/{cid}/modules")
mod = next((m for m in existing if m.get("name") == module_name), None)
if mod:
    module_id = mod["id"]
else:
    mod = client.post(f"/courses/{cid}/modules", data={"module[name]": module_name})
    module_id = mod["id"]
```

This is what makes `playbook deploy` re-runnable.

**Find a file by name in a course folder.**

```python
folders = client.get_all(f"/courses/{cid}/folders")
folder = next((f for f in folders if f.get("name") == "playbook-media"), None)
files = client.get_all(f"/folders/{folder['id']}/files") if folder else []
file_id_by_name = {f["display_name"]: f["id"] for f in files}
```

**Two-step upload with overwrite.**

```python
result = client.upload_file(
    course_id=cid,
    local_path="/tmp/asset.png",
    folder_path="playbook-media",
    content_type="image/png",
    on_duplicate="overwrite",  # default is "rename"
)
canvas_file_id = result["id"]
```

**Dry-run convention.** Check `dry_run` before any side effect. Print what
*would* happen using `emit()`, then return without calling Canvas. The
`confirm_or_abort` helper bakes this in for delete-style commands.

**Prefer `get_all` over manual pagination.** It follows Link-header `rel=next`
automatically and tracks rate limits. The only time to use raw `get` for a
listing is when you only want page 1 (rare).

## Canvas gotchas

These bit us during real work. Save yourself the rediscovery:

- **The page-body sanitizer strips inline CSS** properties: `font-weight`,
  `box-shadow`, `aspect-ratio`. Also strips `<style>` and `<script>` tags
  and any custom CSS class semantics (no admin CSS). Workarounds: wrap text
  in `<strong>` for bold (browsers default headings to bold anyway), use
  borders instead of shadows, set explicit `width`+`height` on iframes.

- **Canvas auto-stamps `?verifier=…` tokens** on file URLs and on
  `media_attachments_iframe/{id}` references when serving page bodies.
  Don't try to add your own; just use the unsigned form.

- **The native inline media embed** is an `<iframe>` pointing at
  `/media_attachments_iframe/{file_id}` with `data-media-type="video"` (or
  `"audio"`). Image embed is `<img src="/courses/{cid}/files/{fid}/preview">`.

- **Module item types** are exactly: `Page`, `Assignment`, `SubHeader`,
  `ExternalUrl`, `File`, `Discussion`, `Quiz`. New Quizzes is a different
  API surface and is not currently supported by this CLI.

- **Pages are addressed by URL slug**, not by id. Slugs are derived from
  the title at creation; if you rename, the slug doesn't change.

- **Submission types** are arrays: `assignment[submission_types][]` with
  values like `online_upload`, `online_url`, `online_text_entry`. Pass a
  list as the dict value; Canvas accepts the JSON encoding.

- **The page to-do field is asymmetric.** You *write*
  `wiki_page[student_todo_at]` but *read back* `todo_date`. Grepping the
  Canvas docs for one name will never surface the other. Discussion topics,
  confusingly, use `todo_date` for both directions.

- **A to-do date on an unpublished page is inert.** Students never see it.
  Any command that sets one should check `published` and say so rather than
  reporting success — see the pre-flight in `pages bulk-todo`.

- **Canvas date fields are cleared with an empty string, not null.**
  `prefix_keys` drops `None` values (that's how "leave this field alone" is
  expressed), so a None can never mean "clear". Use `utils.dates.CLEAR`.

- **Never build a timestamp by string concatenation.** `f"{date}T23:59:00Z"`
  looks right and is off by the UTC offset — in Mountain Time it makes an
  11:59 PM deadline close at 5:59 PM. Use
  `utils.dates.to_canvas_datetime(date)`, which resolves the zone from
  `[defaults] timezone`.

- **The token is teacher-level.** Admin endpoints (account-level reads,
  user provisioning, SIS) will 403. If you encounter a 403, you've hit one
  of these — don't burn cycles trying to make it work.

## Conventions to follow / things to avoid

Do:
- Use `get_course_id(course)` for every course resolution.
- Wrap command bodies in `try: … except Exception as exc: raise handle_canvas_error(exc)`.
- Search-by-name before creating, so re-runs are idempotent.
- Honor `--dry-run` for any side effect.
- Use `client.get_all` for paginated listings.
- Match the existing flag set (`-c`, `-o`, `-v`, `--dry-run`, `-y`).
- Add a one-line docstring on every command — Typer surfaces it as `--help`.

Don't:
- Hardcode tokens, base URLs, course ids, file ids, page slugs.
- `os.environ.get("CANVAS_TOKEN")` directly. Use `require_credentials()`.
- Import inside command bodies — Typer registers commands at module load,
  so heavy imports should be at module level (and lazy where possible).
- Catch and silently swallow `CanvasError` subclasses. Always route through
  `handle_canvas_error`.
- Use `print()`. Use `emit()` (stdout) or `err_console.print()` (stderr).
- Add an extension that duplicates a built-in command group's name. The
  loader warns on stderr when this happens, but the extension still wins:
  it shadows the built-in group entirely, so every command in that group
  disappears.
- Modify `extensions/__init__.py` (the discovery loader) unless you're
  fixing a bug in it.
- Mock the network in code. Use the `responses` library in tests instead.

## Worked example: a small extension end-to-end

Goal: "add a command that copies all module structure from one course to
another (names only — no items)."

1. Create `canvas_conductor/extensions/copy_modules.py`:

   ```python
   """Copy module skeleton (names + order) from one course to another."""
   from __future__ import annotations

   import typer

   from canvas_conductor.client import get_client
   from canvas_conductor.commands._common import (
       confirm_or_abort,
       emit,
       handle_canvas_error,
   )
   from canvas_conductor.config import get_course_id, get_courses

   app = typer.Typer(name="copy-modules", help="Clone module structure between courses.")


   @app.command("run")
   def run(
       source: str = typer.Option(..., "--from", help="Source course alias"),
       target: str = typer.Option(..., "--to", help="Target course alias"),
       dry_run: bool = typer.Option(False, "--dry-run"),
       yes: bool = typer.Option(False, "-y", "--yes"),
       verbose: bool = typer.Option(False, "-v", "--verbose"),
   ) -> None:
       """Copy module names (in order) from --from course to --to course."""
       try:
           src_id = get_course_id(source)
           tgt_id = get_course_id(target)
           client = get_client(verbose=verbose)

           src_modules = client.get_all(f"/courses/{src_id}/modules")
           emit(f"Source has {len(src_modules)} modules.")

           tgt_existing = client.get_all(f"/courses/{tgt_id}/modules")
           tgt_names = {m["name"] for m in tgt_existing}

           to_create = [m for m in src_modules if m["name"] not in tgt_names]
           if not to_create:
               emit("Target already has all source module names.")
               return

           confirm_or_abort(
               f"Create {len(to_create)} modules in '{target}'?",
               yes=yes, dry_run=dry_run,
           )

           for pos, m in enumerate(to_create, start=len(tgt_existing) + 1):
               client.post(
                   f"/courses/{tgt_id}/modules",
                   data={"module[name]": m["name"], "module[position]": pos},
               )
               emit(f"  + {m['name']}")
           emit(f"\nCreated {len(to_create)} modules.")
       except Exception as exc:
           raise handle_canvas_error(exc)
   ```

2. Verify discovery: `uv run conductor copy-modules --help` should list
   the `run` command. If it doesn't show up, look at stderr for an import
   warning from the discovery loader.

3. Smoke-test with `--dry-run` first: `uv run conductor copy-modules run
   --from is402 --to scratch --dry-run`.

4. Add a test in `tests/test_copy_modules.py` using the `responses`
   library to mock the two GETs and the POSTs. Pattern: see the existing
   `tests/test_modules.py`.

That's the full loop: file → discovery → dry-run → real run → tests.

## Tests

`tests/` uses `pytest` and the `responses` library for HTTP mocking.
Conventions:

- One test file per command module. Mirror the layout: `commands/pages.py`
  → `tests/test_pages.py`.
- Mock at the HTTP boundary, not the client. `responses.add(...)` registers
  a fixture URL and response.
- Use the `cli_runner` fixture (from `typer.testing.CliRunner`) to invoke
  the app in-process: `result = runner.invoke(app, ["modules", "list"])`.

Run them: `uv run pytest`. Add `-v` for verbose output, or
`uv run pytest tests/test_pages.py::test_specific` for one case.

When you add a command, add at least one happy-path test plus one error
case (Canvas 404 or 422) so the error formatting is exercised.

## When stuck

Three places to look, in order:
1. `commands/pages.py` — covers the standard CRUD command pattern with
   bodies-from-files, dry-run, and bracket-key payloads.
2. `extensions/playbook.py` — covers a multi-command extension with file
   uploads, custom HTML rendering, and idempotent reuse-by-name.
3. `client.py` — read it once. The whole file fits on screen and explains
   how pagination, retries, rate limits, and the two-step upload work.

Habits that pay off:
- Run `--help` on any command before you guess at its flags.
- `uv run conductor config validate` is the fastest way to confirm the
  environment + token + at least one configured course are wired up.
- `-v` on any command prints HTTP requests and rate-limit budget remaining
  to stderr — invaluable for debugging unexpected behavior.
- When Canvas returns 422, read the response body. The validation message
  is in `errors[0].message` or `errors.<field>[0].message` — see
  `_extract_message` in `client.py`.
