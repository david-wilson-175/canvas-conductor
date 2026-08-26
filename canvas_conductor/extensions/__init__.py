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
import importlib.util
import os
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


EXTENSIONS_DIR_ENV_VAR = "CONDUCTOR_EXTENSIONS_DIR"


def _external_dir() -> Path | None:
    """Resolve `CONDUCTOR_EXTENSIONS_DIR`, warning on a bad value.

    Returns None when unset, empty, or unusable. A bad path is a warning
    rather than an error: the bundled extensions and the whole core CLI must
    keep working regardless.
    """
    raw = (os.environ.get(EXTENSIONS_DIR_ENV_VAR) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        print(
            f"warning: {EXTENSIONS_DIR_ENV_VAR} is set to {raw!r} but that is "
            "not a directory; no external extensions were loaded.",
            file=sys.stderr,
        )
        return None
    return path


def _load_external(stem: str, path: Path):
    """Import a module from an arbitrary file path.

    Registered in `sys.modules` under the package namespace so it behaves
    exactly like a bundled extension (same import semantics, same name in
    tracebacks). Any stale entry is dropped first so a changed path wins.
    """
    full_name = f"{__name__}.{stem}"
    sys.modules.pop(full_name, None)
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(full_name, None)
        raise
    return mod


def discover_extensions(main_app: typer.Typer) -> list[str]:
    """Scan for modules that expose `app: typer.Typer` and register them.

    Two directories are scanned, in order:

    1. This package (`canvas_conductor/extensions/`), the bundled extensions.
    2. The directory named by `CONDUCTOR_EXTENSIONS_DIR`, if set. This lets
       project-specific extensions live with the project they serve rather
       than inside a general-purpose repo, mirroring what `CONDUCTOR_CONFIG`
       does for `config.toml`.

    Each found app is registered onto `main_app` via `add_typer`. Returns the
    list of registered extension names. Errors are printed to stderr but do
    not crash the CLI.

    Two distinct kinds of clash, handled differently:

    * **Same module filename in both directories.** Treated as an override,
      not a conflict: the external file wins, loads exactly once, and nothing
      is warned. This is how you replace a bundled extension locally.
    * **Two different modules claiming the same Typer group name.** A real
      collision. Extensions load after the core groups, and Typer resolves a
      duplicate name in favor of the last registration — so an extension
      claiming an existing name silently replaces that group in full, not
      just the overlapping commands. That shadowing is preserved here
      (removing it would break anyone relying on it), but it gets a stderr
      warning so the cause isn't invisible when a documented built-in appears
      to vanish.
    """
    registered: list[str] = []
    claimed = _claimed_names(main_app)
    pkg_path = Path(__file__).parent

    # stem -> external Path, or None meaning "bundled". `order` keeps bundled
    # first so a same-name external module shadows a core group predictably.
    sources: dict[str, Path | None] = {}
    order: list[str] = []
    for info in pkgutil.iter_modules([str(pkg_path)]):
        if info.name.startswith("_"):
            continue
        sources[info.name] = None
        order.append(info.name)

    ext_dir = _external_dir()
    if ext_dir is not None:
        for info in pkgutil.iter_modules([str(ext_dir)]):
            if info.name.startswith("_"):
                continue
            if info.name not in sources:
                order.append(info.name)
            # External always wins for a given filename; keeps its position
            # if it is overriding a bundled module of the same name.
            sources[info.name] = ext_dir / f"{info.name}.py"

    for stem in order:
        source = sources[stem]
        try:
            if source is None:
                mod = importlib.import_module(f"{__name__}.{stem}")
            else:
                mod = _load_external(stem, source)
        except Exception as exc:  # pragma: no cover - error path
            where = f" from {source}" if source is not None else ""
            print(
                f"warning: failed to load extension '{stem}'{where}: {exc}",
                file=sys.stderr,
            )
            if "--verbose" in sys.argv or "-v" in sys.argv:
                traceback.print_exc()
            continue

        ext_app = getattr(mod, "app", None)
        if not isinstance(ext_app, typer.Typer):
            continue

        name = ext_app.info.name or stem
        if name in claimed:
            print(
                f"warning: extension '{stem}' claims the command group "
                f"'{name}', which is already registered; the extension will "
                f"shadow it. Rename the `typer.Typer(name=...)` in "
                f"{stem}.py to keep both.",
                file=sys.stderr,
            )
        claimed.add(name)
        main_app.add_typer(ext_app, name=name)
        registered.append(name)

    return registered
