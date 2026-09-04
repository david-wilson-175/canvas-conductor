"""Shared helpers for command modules.

These keep each command short and consistent. The CLI surface lives in
each `commands/<group>.py` file; helpers here only handle plumbing
(error formatting, confirmation prompts, etc.).
"""
from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.console import Console

from ..config import is_course_readonly
from ..exceptions import (
    CanvasAuthError,
    CanvasError,
    CanvasNotFoundError,
    CanvasPermissionError,
    CanvasRateLimitError,
    CanvasValidationError,
    ConfigError,
)


err_console = Console(stderr=True)


def emit(text: str) -> None:
    """Print user-facing output to stdout (no markup interpretation)."""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def handle_canvas_error(exc: Exception) -> "typer.Exit":
    """Format a Canvas/Config error for the user and return an Exit code."""
    if isinstance(exc, ConfigError):
        err_console.print(f"[red]ERROR:[/red] {exc}")
        return typer.Exit(code=2)

    if isinstance(exc, CanvasAuthError):
        err_console.print(
            "[red]ERROR:[/red] Authentication failed (401).\n"
            "Your Canvas token may have expired. Regenerate it under "
            "Account > Settings > Approved Integrations, then update your "
            ".env file."
        )
        return typer.Exit(code=3)

    if isinstance(exc, CanvasPermissionError):
        err_console.print(
            f"[red]ERROR:[/red] Permission denied (403). {exc.message}"
        )
        return typer.Exit(code=4)

    if isinstance(exc, CanvasNotFoundError):
        err_console.print(
            f"[red]ERROR:[/red] Resource not found (404). {exc.message}"
        )
        return typer.Exit(code=5)

    if isinstance(exc, CanvasValidationError):
        err_console.print(
            f"[red]ERROR:[/red] Canvas rejected the request (422): {exc.message}"
        )
        return typer.Exit(code=6)

    if isinstance(exc, CanvasRateLimitError):
        err_console.print(
            "[red]ERROR:[/red] Rate limit exceeded (429). Wait a few minutes "
            "and try again."
        )
        return typer.Exit(code=7)

    if isinstance(exc, CanvasError):
        err_console.print(f"[red]ERROR:[/red] {exc}")
        return typer.Exit(code=8)

    raise exc


def confirm_or_abort(message: str, yes: bool, dry_run: bool) -> None:
    """Standard confirmation flow for destructive ops."""
    if dry_run:
        err_console.print(f"[yellow]DRY-RUN:[/yellow] {message}")
        raise typer.Exit(code=0)
    if yes:
        return
    if not typer.confirm(message, default=False):
        err_console.print("Aborted.")
        raise typer.Exit(code=1)


def guard_readonly(course_key: str | None, force: bool, dry_run: bool = False) -> None:
    """Refuse a write against a course marked `readonly = true` in config.toml.

    The flag pre-dates any enforcement: it lived in config.toml purely as a
    note to humans, and the CLI would happily write to a course carrying it.
    This turns it into an actual interlock. `--force` overrides it (loudly),
    and `--dry-run` is allowed through since it touches nothing.
    """
    if not is_course_readonly(course_key):
        return

    label = course_key or "the default course"
    reason = (
        f"Course '{label}' is marked [bold]readonly = true[/bold] in config.toml"
    )

    if force:
        err_console.print(f"[yellow]WARNING:[/yellow] {reason}. --force given; proceeding.")
        return
    if dry_run:
        err_console.print(
            f"[yellow]NOTE:[/yellow] {reason}. --dry-run writes nothing, but this "
            "command would be refused without --force."
        )
        return

    err_console.print(
        f"[red]ERROR:[/red] {reason}, so write commands are blocked against it.\n"
        "Re-run with --force if you really mean to write, or drop the readonly "
        "flag from that course's config block."
    )
    raise typer.Exit(code=9)


def preview_write(method: str, path: str, payload: Any | None = None) -> None:
    """Print exactly what a write would send, and send nothing.

    The standard body of a `--dry-run` branch: the target and the literal
    payload, so the user can compare it against Canvas's docs before
    letting it go out.
    """
    emit(f"DRY-RUN: {method} {path}")
    if payload is not None:
        emit("DRY-RUN: payload=" + json.dumps(payload, indent=2, sort_keys=True))
    emit("DRY-RUN: no request was made.")


def parse_kv_list(value: str | None) -> dict[str, str]:
    """Parse `key=value,key2=value2` into a dict. Returns {} on empty input."""
    if not value:
        return {}
    out: dict[str, str] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        key, _, val = part.partition("=")
        out[key.strip()] = val.strip()
    return out


def prefix_keys(prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload under a single top-level resource key for Canvas.

    Drops None values, returns an empty dict if everything was None (so
    callers can short-circuit with `if not payload`).

    >>> prefix_keys("wiki_page", {"title": "Hi", "published": None})
    {'wiki_page': {'title': 'Hi'}}

    Canvas's POST/PUT endpoints want the resource nested under its type
    key (`{"wiki_page": {"title": ...}}`). The older `prefix[key]`
    bracket-style is a form-encoded convention; when sent as JSON,
    several endpoints (notably POST /modules, /pages, /assignments)
    reject it with 400. Sticking to nested form keeps every endpoint
    happy.
    """
    inner = {k: v for k, v in payload.items() if v is not None}
    if not inner:
        return {}
    return {prefix: inner}
