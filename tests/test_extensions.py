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


# ---------------------------------------------------------------------------
# CONDUCTOR_EXTENSIONS_DIR — loading extensions from outside the package
# ---------------------------------------------------------------------------


def _write_external(tmp_dir: Path, stem: str, group_name: str, marker: str = "pong") -> Path:
    """Write a throwaway extension into an arbitrary directory."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{stem}.py"
    path.write_text(
        textwrap.dedent(
            f"""
            import typer
            app = typer.Typer(name="{group_name}", help="external fixture")

            @app.command("ping")
            def ping() -> None:
                typer.echo("{marker}")
            """
        )
    )
    importlib.invalidate_caches()
    return path


def test_external_dir_unset_behaves_exactly_as_before(monkeypatch, capsys):
    """No env var means today's behaviour: bundled only, no warning."""
    monkeypatch.delenv("CONDUCTOR_EXTENSIONS_DIR", raising=False)
    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    assert "playbook" in registered  # the bundled extension still loads
    assert "warning" not in capsys.readouterr().err


def test_external_dir_empty_string_is_ignored(monkeypatch, capsys):
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", "")
    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    assert "playbook" in registered
    assert "warning" not in capsys.readouterr().err


def test_external_dir_loads_an_extension(monkeypatch, tmp_path):
    ext_dir = tmp_path / "ext"
    _write_external(ext_dir, "loader_external", "loader-external")
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(ext_dir))

    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    assert "loader-external" in registered


def test_external_dir_extension_is_actually_invocable(monkeypatch, tmp_path):
    """Registering is not enough; the command must run."""
    ext_dir = tmp_path / "ext"
    _write_external(ext_dir, "loader_invocable", "loader-invocable", marker="external-ok")
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(ext_dir))

    main = typer.Typer()
    ext_pkg.discover_extensions(main)
    result = CliRunner().invoke(main, ["loader-invocable", "ping"])
    assert result.exit_code == 0, result.output
    assert "external-ok" in result.output


def test_external_dir_missing_path_warns_but_cli_survives(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(tmp_path / "does-not-exist"))
    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    err = capsys.readouterr().err
    assert "warning" in err
    assert "does-not-exist" in err
    assert "playbook" in registered  # bundled extensions still loaded


def test_external_dir_pointing_at_a_file_warns(monkeypatch, tmp_path, capsys):
    target = tmp_path / "not-a-dir.txt"
    target.write_text("x")
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(target))
    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    assert "warning" in capsys.readouterr().err
    assert "playbook" in registered


def test_external_underscore_files_are_skipped(monkeypatch, tmp_path):
    ext_dir = tmp_path / "ext"
    _write_external(ext_dir, "_private_helper", "should-not-appear")
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(ext_dir))
    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    assert "should-not-appear" not in registered


def test_external_same_filename_overrides_bundled_without_warning(monkeypatch, tmp_path, capsys):
    """Same module filename in both dirs is an override, not a collision.

    It must load exactly once, from the external directory, and must not warn
    about colliding with itself.
    """
    bundled = _write_extension("loader_override", "loader-override")
    ext_dir = tmp_path / "ext"
    _write_external(ext_dir, "loader_override", "loader-override", marker="external-wins")
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(ext_dir))
    try:
        main = typer.Typer()
        registered = ext_pkg.discover_extensions(main)
        result = CliRunner().invoke(main, ["loader-override", "ping"])
    finally:
        _cleanup(bundled)

    assert registered.count("loader-override") == 1, "must load once, not twice"
    assert "warning" not in capsys.readouterr().err
    assert "external-wins" in result.output, "the external copy must win"


def test_external_group_name_collision_still_warns(monkeypatch, tmp_path, capsys):
    """Two DIFFERENT modules claiming one group name is a real collision."""
    bundled = _write_extension("loader_bundled_name", "loader-dup-name")
    ext_dir = tmp_path / "ext"
    _write_external(ext_dir, "loader_external_name", "loader-dup-name")
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(ext_dir))
    try:
        main = typer.Typer()
        registered = ext_pkg.discover_extensions(main)
    finally:
        _cleanup(bundled)

    assert registered.count("loader-dup-name") == 2
    assert "warning" in capsys.readouterr().err


def test_external_broken_extension_does_not_crash_cli(monkeypatch, tmp_path, capsys):
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "loader_broken.py").write_text("raise RuntimeError('boom')\n")
    importlib.invalidate_caches()
    monkeypatch.setenv("CONDUCTOR_EXTENSIONS_DIR", str(ext_dir))

    main = typer.Typer()
    registered = ext_pkg.discover_extensions(main)
    err = capsys.readouterr().err
    assert "warning" in err
    assert "loader_broken" in err
    assert "playbook" in registered  # everything else still loaded
