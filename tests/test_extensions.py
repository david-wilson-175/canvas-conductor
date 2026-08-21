"""Test the extension auto-discovery loader."""
from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import typer
from typer.testing import CliRunner

from canvas_conductor import extensions as ext_pkg


def test_underscore_files_are_skipped():
    """`_example.py` ships with the package and must NOT register a group."""
    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    assert "example" not in registered  # `_example.py` is filtered


def _write_extension(stem: str, group_name: str) -> Path:
    """Write a throwaway extension into the package dir; caller must unlink."""
    pkg_dir = Path(ext_pkg.__file__).parent
    path = pkg_dir / f"{stem}.py"
    path.write_text(
        textwrap.dedent(
            f"""
            import typer
            app = typer.Typer(name="{group_name}", help="collision fixture")

            @app.command("ping")
            def ping() -> None:
                typer.echo("pong")
            """
        )
    )
    sys.modules.pop(f"canvas_conductor.extensions.{stem}", None)
    importlib.invalidate_caches()
    return path


def _cleanup(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
        sys.modules.pop(f"canvas_conductor.extensions.{path.stem}", None)


def test_collision_with_core_group_warns_and_still_shadows(capsys):
    """An extension taking a built-in's name must warn but keep working."""
    path = _write_extension("loader_collide_core", "loader-collide")
    try:
        main = typer.Typer()
        main.add_typer(typer.Typer(), name="loader-collide")
        registered = ext_pkg.discover_extensions(main)
    finally:
        _cleanup(path)

    assert "loader-collide" in registered  # shadowing behavior is preserved
    err = capsys.readouterr().err
    assert "warning" in err
    assert "loader-collide" in err
    assert "loader_collide_core.py" in err


def test_collision_between_two_extensions_warns(capsys):
    """Two extensions claiming one name: the second gets the warning."""
    first = _write_extension("loader_collide_a", "loader-dupe")
    second = _write_extension("loader_collide_b", "loader-dupe")
    try:
        registered = ext_pkg.discover_extensions(typer.Typer())
    finally:
        _cleanup(first, second)

    assert registered.count("loader-dupe") == 2
    err = capsys.readouterr().err
    assert err.count("warning") == 1


def test_no_warning_without_collision(capsys):
    """The common case must stay silent — no false-positive noise."""
    path = _write_extension("loader_no_collide", "loader-unique")
    try:
        ext_pkg.discover_extensions(typer.Typer())
    finally:
        _cleanup(path)

    assert "warning" not in capsys.readouterr().err


def test_loader_picks_up_real_extension(tmp_path, monkeypatch):
    """Drop a fresh extension into the package dir and confirm it registers."""
    pkg_dir = Path(ext_pkg.__file__).parent
    new_file = pkg_dir / "_loader_smoke_extension.py"

    # Use a name that does NOT start with underscore for the actual test file:
    real_file = pkg_dir / "loader_smoke_extension.py"
    real_file.write_text(
        textwrap.dedent(
            """
            import typer
            app = typer.Typer(name="loader-smoke", help="smoke test extension")

            @app.command("ping")
            def ping() -> None:
                typer.echo("pong")
            """
        )
    )

    try:
        # Drop any cached module so re-import sees the new file.
        sys.modules.pop(
            "canvas_conductor.extensions.loader_smoke_extension", None
        )
        importlib.invalidate_caches()
        main = typer.Typer()
        registered = ext_pkg.discover_extensions(main)
        assert "loader-smoke" in registered
    finally:
        real_file.unlink(missing_ok=True)
        new_file.unlink(missing_ok=True)
        sys.modules.pop(
            "canvas_conductor.extensions.loader_smoke_extension", None
        )
