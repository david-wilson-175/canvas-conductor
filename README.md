# Canvas Conductor

A command-line tool for managing Canvas LMS courses. Wraps the Canvas REST API into composable commands for instructors, course designers, and administrators who want to automate course management from the terminal.

## Quick Start

### 1. Install uv (if not already installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone the repo

```bash
git clone https://github.com/youruser/canvas-conductor.git ~/code/canvas-conductor
cd ~/code/canvas-conductor
```

### 3. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Canvas URL and API token
```

Generate a Canvas API token:
1. Go to your Canvas instance (e.g., `https://school.instructure.com`)
2. Navigate to Account > Settings
3. Under "Approved Integrations", click "+ New Access Token"
4. Copy the token into your `.env` file

### 4. Install the `conductor` CLI on your PATH

The recommended pattern is `uv tool install --editable`. This drops a
`conductor` binary in `~/.local/bin/` (managed by uv in an isolated venv) but
points it at your source checkout, so any code edits are picked up on the
next invocation without reinstalling.

```bash
uv tool install --editable ~/code/canvas-conductor
```

If `~/.local/bin` isn't on your PATH yet, uv will say so. Fix it with:

```bash
uv tool update-shell
```

After this you can `cd` anywhere and run `conductor ...` directly.

### 5. Validate and try it out

```bash
conductor config validate
conductor courses list
```

### Alternative install patterns

- **Light alias.** If you'd rather not install a managed tool, add an alias
  to your shell rc:

  ```bash
  alias conductor='uv run --project ~/code/canvas-conductor conductor'
  ```

  Trades a small uv-resolver startup cost on each call for not having to
  manage a separate install. `which conductor` will show the alias rather
  than a real binary.

- **Project-local invocation.** From inside the repo, `uv run conductor ...`
  always works without any global setup. Handy for one-off use or while
  developing the CLI itself.

### Updating after pulls or new dependencies

With the `--editable` install, code changes are live. If you `git pull` and
the pyproject grew new dependencies, refresh the tool's venv:

```bash
uv tool upgrade canvas-conductor
```

## Configuration

### Secrets (`.env`)

Your `.env` file stores credentials. Never commit this file.

```
CANVAS_BASE_URL=https://school.instructure.com
CANVAS_TOKEN=7407~your_token_here
```

### Course Configuration (`config.toml`)

By default Conductor walks up from the current working directory to find a
`config.toml`. If you'd rather keep your course list in a single shared
location (for example, a Dropbox-synced teaching folder used by multiple
machines or other tools), point the `CONDUCTOR_CONFIG` environment variable at
that file:

```bash
export CONDUCTOR_CONFIG="$HOME/Dropbox/agent-sync/teaching/teaching-os/canvas-conductor.toml"
```

When `CONDUCTOR_CONFIG` is set it takes precedence over the walk-up search.

Define the courses you manage with short aliases. Aliases can include
hyphens. Each `[courses.<alias>]` block can also carry sub-blocks
consumed by extensions (e.g., `[courses.is-career-playbook.playbook]`):

```toml
[defaults]
output_format = "table"
per_page = 100
confirm_destructive = true

[courses.is566]
id = 33431
name = "IS 566: Data Engineering"
term = "Winter 2027"

[courses.is-career-playbook]
id = 35416
name = "IS Career Playbook (development course)"
term = "Spring Semester 2026"
```

Use the short alias with `--course` (or `-c`):

```bash
conductor modules list --course is402
conductor pages list -c is515
```

If only one course is configured, it is used automatically.

## Command Reference

All commands follow the pattern: `conductor <group> <action> [options]`

### Global Options

Every command supports these flags:

| Flag | Short | Description |
|------|-------|-------------|
| `--course` | `-c` | Course alias from config.toml |
| `--output` | `-o` | Output format: table, json, csv |
| `--verbose` | `-v` | Show HTTP request details |
| `--dry-run` | | Preview changes without executing |
| `--yes` | `-y` | Skip confirmation prompts |

### Courses

```bash
conductor courses list                          # List your courses
conductor courses show -c is402                 # Show course details
conductor courses update -c is402 --name "New Name"  # Update course
conductor courses settings -c is402             # View course settings
```

### Modules

```bash
conductor modules list -c is402                 # List all modules
conductor modules create -c is402 --name "Week 6: Final Review"
conductor modules update -c is402 --id 12345 --published true
conductor modules delete -c is402 --id 12345
conductor modules reorder -c is402 --ids 1,2,3  # Set module order
conductor modules publish -c is402 --id 12345    # Publish a module
conductor modules unpublish -c is402 --id 12345  # Unpublish a module
```

### Pages

```bash
conductor pages list -c is402                   # List all pages
conductor pages show -c is402 --url welcome     # Show page content
conductor pages create -c is402 --title "Welcome" --body "<h1>Hello</h1>"
conductor pages create -c is402 --title "Notes" --file notes.html
conductor pages update -c is402 --url welcome --body "<h1>Updated</h1>"
conductor pages delete -c is402 --url welcome
conductor pages set-front -c is402 --url welcome  # Set as front page
```

### Assignments

```bash
conductor assignments list -c is402             # List all assignments
conductor assignments create -c is402 --name "Resume" --points 10 --type online_upload
conductor assignments update -c is402 --id 12345 --due "2026-07-01T23:59:00Z"
conductor assignments delete -c is402 --id 12345
conductor assignments bulk-dates -c is402 --shift 7d  # Shift all due dates by 7 days
```

### Files

```bash
conductor files list -c is402                   # List course files
conductor files upload -c is402 --file report.pdf --folder "course files/reports"
conductor files delete -c is402 --id 12345
conductor files quota -c is402                  # Check storage usage
```

### Submissions

```bash
conductor submissions list -c is402 --assignment 12345
conductor submissions grade -c is402 --assignment 12345 --user 67890 --grade "pass"
conductor submissions bulk-grade -c is402 --assignment 12345 --file grades.csv
conductor submissions download -c is402 --assignment 12345 --dir ./submissions
```

### Enrollments

```bash
conductor enrollments list -c is402             # List all enrollments
conductor enrollments list -c is402 --type student --state active
conductor enrollments summary -c is402          # Show enrollment counts by type
```

### Sections

Cross-listing is how Canvas combines multiple sections into one course:
each section moves into a destination shell, bringing its students with it.
Run with `--dry-run` first — the pre-flight check reports how many graded
submissions each section would carry out of its current course.

```bash
conductor sections list -c is402                # List sections + student counts
conductor sections crosslist --ids 45231,45232 -c is402 --dry-run
conductor sections crosslist --from is402-b -c is402 -y   # Absorb a whole shell
conductor sections uncrosslist --id 45231       # Restore to original course
```

### Tabs (Navigation)

```bash
conductor tabs list -c is402                    # List navigation tabs
conductor tabs show -c is402 --tab assignments  # Show a specific tab
conductor tabs hide -c is402 --tab discussions  # Hide a tab
```

### Discussions

```bash
conductor discussions list -c is402
conductor discussions create -c is402 --title "Week 1 Discussion" --message "<p>Introduce yourself</p>"
conductor discussions update -c is402 --id 12345 --pinned true
conductor discussions delete -c is402 --id 12345
```

### Assignment Groups

```bash
conductor groups list -c is402                  # List assignment groups
conductor groups create -c is402 --name "Homework" --weight 40
conductor groups update -c is402 --id 12345 --weight 50
conductor groups delete -c is402 --id 12345
```

### Config

```bash
conductor config show                           # Show current config (token redacted)
conductor config validate                       # Test connection and list courses
conductor config courses                        # List configured courses
```

## Output Formats

All list commands support three output formats:

**Table** (default, human-readable):
```
$ conductor modules list -c is402

  #  Name                                    Published  Items
  1  Week 1: Resumes                         Yes        5
  2  Week 2: LinkedIn and Networking          Yes        4
  3  Week 3: Work Experience and Portfolios   No         3
```

**JSON** (machine-readable, pipe to `jq`):
```bash
conductor modules list -c is402 -o json
conductor modules list -c is402 -o json | jq '.[].name'
```

**CSV** (for spreadsheets):
```bash
conductor modules list -c is402 -o csv > modules.csv
```

## Extensions

Canvas Conductor supports user-defined extensions. Drop a Python file into the `extensions/` directory to add custom commands.

### Writing an Extension

Create a file in `canvas_conductor/extensions/` (skip files starting with `_`):

```python
# extensions/deploy_markdown.py
"""Deploy markdown files as Canvas pages."""
import typer
from canvas_conductor.client import get_client
from canvas_conductor.config import get_course_id

app = typer.Typer(name="deploy", help="Deploy content to Canvas")

@app.command()
def markdown(
    source: str = typer.Argument(..., help="Path to markdown file"),
    course: str = typer.Option(None, "-c", "--course"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Convert a markdown file to HTML and push it as a Canvas page."""
    client = get_client(verbose=verbose)
    cid = get_course_id(course)
    # Your logic here...
```

The extension is automatically discovered and available as:
```bash
conductor deploy markdown content/week-1.md -c is402
```

### Extension Guidelines

- Define `app = typer.Typer(name="your-command", help="Description")` at module level
- Use `get_client()` and `get_course_id()` from the core library
- Files starting with `_` are ignored (use `_example.py` as a template)
- Errors in extension loading print a warning but do not crash the CLI

## Common Workflows

### Set up a new course

```bash
# Verify your token works
conductor config validate

# See what courses you have access to
conductor courses list

# Add the course to config.toml, then:
conductor modules list -c mycourse
```

### Copy module structure between courses

```bash
# Export module names from source course
conductor modules list -c source -o json > modules.json

# Create modules in target course (scripted)
cat modules.json | jq -r '.[].name' | while read name; do
  uv run conductor modules create -c target --name "$name" --yes
done
```

### Bulk update assignment dates

```bash
# Shift all due dates forward by one week
conductor assignments bulk-dates -c is402 --shift 7d

# Or export, edit in a spreadsheet, and re-import
conductor assignments list -c is402 -o csv > dates.csv
# Edit dates.csv...
conductor assignments bulk-dates -c is402 --file dates.csv
```

### Export grades

```bash
conductor submissions list -c is402 --assignment 12345 -o csv > grades.csv
```

## Troubleshooting

### "Authentication failed (401)"

Your Canvas token may have expired. Regenerate it:
1. Go to your Canvas instance > Account > Settings
2. Under "Approved Integrations", click "+ New Access Token"
3. Update the `CANVAS_TOKEN` value in your `.env` file

### "Course not found (404)"

The course ID in your `config.toml` may be incorrect. Find the correct ID:
1. Open the course in Canvas
2. The URL contains the ID: `https://school.instructure.com/courses/12345`
3. Update `config.toml` with the correct ID

### "Rate limit exceeded (429)"

Canvas Conductor automatically handles rate limiting with retries and backoff. If you see this error repeatedly, you are making too many requests. Wait a few minutes and try again.

### Commands are slow

Large courses with many items require multiple paginated API calls. Use `--verbose` to see individual request timing. For list commands, consider piping JSON output to a file for repeated analysis rather than re-fetching.

## Development

### Setup

```bash
git clone https://github.com/youruser/canvas-conductor.git
cd canvas-conductor
uv sync --extra dev
```

### Run tests

```bash
uv run pytest
uv run pytest -v              # Verbose output
uv run pytest tests/test_client.py  # Run specific test file
```

### Run the CLI (development)

```bash
uv run conductor --help
uv run conductor courses list
```

### Add a dependency

```bash
uv add <package>              # Runtime dependency
uv add --group dev <package>  # Dev-only dependency
```

### Project structure

```
canvas-conductor/
  canvas_conductor/
    cli.py              # Typer app, command group registration
    client.py           # CanvasClient (HTTP, pagination, rate limits)
    config.py           # Config loader (.env + TOML)
    models.py           # Pydantic response models
    exceptions.py       # Typed exception classes
    commands/           # One module per command group
    extensions/         # User-defined extensions (auto-discovered)
    utils/              # Output formatting, pagination, markdown
  tests/                # pytest suite
  config.toml           # Course configuration
  .env                  # Secrets (not committed)
  pyproject.toml        # Package metadata and dependencies
```

## License

MIT
