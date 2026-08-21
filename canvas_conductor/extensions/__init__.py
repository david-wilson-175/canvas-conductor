"""Extension auto-discovery.

Drop a `.py` file into this directory that defines a module-level
`app = typer.Typer(name=...)` and it will be registered as a subcommand
group on the main CLI. Files starting with `_` are ignored (use them for
shared helpers or examples like `_example.py`).

By convention, this directory is treated as *private by default* in git
(see the local `.gitignore`). The discovery loader still picks up every
`.py` file at runtime, so a private extension works exactly like a
tracked one; it just won't be committed. `playbook.py` and `_example.py`
are the only extensions tracked as worked examples.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

import typer


def _claimed_names(main_app: typer.Typer) -> set[str]:
    """Return the command-group and command names already bound on `main_app`."""
    names: set[str] = set()
    for group in main_app.registered_groups:
        instance = group.typer_instance
        name = group.name or (instance.info.name if instance else None)
        if name:
            names.add(name)
    for command in main_app.registered_commands:
        name = command.name or (
            command.callback.__name__ if command.callback else None
        )
        if name:
            names.add(name)
    return names


def discover_extensions(main_app: typer.Typer) -> list[str]:
    """Scan this package for modules that expose `app: typer.Typer`.

    Each found app is registered onto `main_app` via `add_typer`. Returns the
    list of registered extension names. Errors are printed to stderr but do
    not crash the CLI.

    Extensions load after the core groups, and Typer resolves a duplicate
    name in favor of the last registration — so an extension claiming an
    existing name silently replaces that group in full, not just the
    overlapping commands. That shadowing is preserved here (removing it
    would break anyone relying on it), but it gets a stderr warning so the
    cause isn't invisible when a documented built-in appears to vanish.
    """
    registered: list[str] = []
    claimed = _claimed_names(main_app)
    pkg_path = Path(__file__).parent

    for info in pkgutil.iter_modules([str(pkg_path)]):
        if info.name.startswith("_"):
            continue
        full_name = f"{__name__}.{info.name}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:  # pragma: no cover - error path
            print(
                f"warning: failed to load extension '{info.name}': {exc}",
                file=sys.stderr,
            )
            if "--verbose" in sys.argv or "-v" in sys.argv:
                traceback.print_exc()
            continue

        ext_app = getattr(mod, "app", None)
        if not isinstance(ext_app, typer.Typer):
            continue

        name = ext_app.info.name or info.name
        if name in claimed:
            print(
                f"warning: extension '{info.name}' claims the command group "
                f"'{name}', which is already registered; the extension will "
                f"shadow it. Rename the `typer.Typer(name=...)` in "
                f"{info.name}.py to keep both.",
                file=sys.stderr,
            )
        claimed.add(name)
        main_app.add_typer(ext_app, name=name)
        registered.append(name)

    return registered
