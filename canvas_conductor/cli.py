"""Top-level Typer application.

Each command module under ``canvas_conductor.commands`` exposes an
``app: typer.Typer`` that gets attached here as a subcommand group.
Extensions in ``canvas_conductor.extensions`` are auto-discovered after the
core groups are registered.
"""
from __future__ import annotations

import typer

from . import __version__
from .commands import (
    assignments,
    config_cmds,
    courses,
    discussions,
    enrollments,
    files,
    groups,
    modules,
    pages,
    sections,
    submissions,
    tabs,
)
from .extensions import discover_extensions


app = typer.Typer(
    name="conductor",
    help=(
        "Canvas Conductor — manage Canvas LMS courses from the terminal. "
        "Use --help on any subcommand for details."
    ),
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"canvas-conductor {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
):
    """Canvas Conductor CLI."""
    # Top-level callback — no setup needed; per-command flags handle the rest.
    return


# Core command groups.
app.add_typer(courses.app, name="courses")
app.add_typer(modules.app, name="modules")
app.add_typer(pages.app, name="pages")
app.add_typer(assignments.app, name="assignments")
app.add_typer(files.app, name="files")
app.add_typer(submissions.app, name="submissions")
app.add_typer(enrollments.app, name="enrollments")
app.add_typer(sections.app, name="sections")
app.add_typer(tabs.app, name="tabs")
app.add_typer(discussions.app, name="discussions")
app.add_typer(groups.app, name="groups")
app.add_typer(config_cmds.app, name="config")

# User-defined extensions (drop *.py files in canvas_conductor/extensions/).
discover_extensions(app)


if __name__ == "__main__":  # pragma: no cover
    app()
