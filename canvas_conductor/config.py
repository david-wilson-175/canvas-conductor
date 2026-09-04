"""Configuration loading: `.env` for secrets, `config.toml` for course definitions.

Lookup walks upward from the current working directory so commands can be run
from anywhere inside the project tree.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .exceptions import ConfigError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib


CONFIG_FILENAME = "config.toml"
ENV_FILENAME = ".env"
CONFIG_ENV_VAR = "CONDUCTOR_CONFIG"


def _walk_up_for(filename: str, start: Path | None = None) -> Path | None:
    """Return the first existing `filename` walking from `start` to filesystem root."""
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        candidate = parent / filename
        if candidate.is_file():
            return candidate
    return None


def find_env_file() -> Path | None:
    return _walk_up_for(ENV_FILENAME)


def find_config_file() -> Path | None:
    """Locate config.toml.

    Resolution order:
        1. If CONDUCTOR_CONFIG is set, use that path (must point to a file).
        2. Otherwise walk up from the current working directory looking for
           `config.toml`.

    Setting CONDUCTOR_CONFIG lets a single master config live outside any
    particular checkout (e.g., in a Dropbox-synced teaching hub) so multiple
    machines and tools can share one course list.
    """
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path
        raise ConfigError(
            f"{CONFIG_ENV_VAR} is set to {override!r} but no file exists at "
            "that path. Unset the variable or point it at a valid config.toml."
        )
    return _walk_up_for(CONFIG_FILENAME)


@lru_cache(maxsize=1)
def load_env() -> dict[str, str]:
    """Load `.env` (if present) into the process environment, return a snapshot."""
    env_path = find_env_file()
    if env_path is not None:
        load_dotenv(env_path, override=False)
    return {
        "CANVAS_BASE_URL": os.environ.get("CANVAS_BASE_URL", ""),
        "CANVAS_TOKEN": os.environ.get("CANVAS_TOKEN", ""),
    }


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Read and parse `config.toml`. Returns `{}` if the file is missing."""
    cfg_path = find_config_file()
    if cfg_path is None:
        return {}
    with cfg_path.open("rb") as fh:
        return tomllib.load(fh)


def get_defaults() -> dict[str, Any]:
    cfg = get_config()
    return cfg.get("defaults", {}) or {}


def get_courses() -> dict[str, dict[str, Any]]:
    cfg = get_config()
    return cfg.get("courses", {}) or {}


def get_course_entry(course_key: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve a course alias to its `(alias, config-block)` pair.

    - If `course_key` is given, look it up in `[courses.*]`.
    - If `course_key` is None and exactly one course is configured, use it.
    - Otherwise raise `ConfigError` with a helpful message.

    Returning the whole block (not just the id) lets callers read
    per-course policy such as `readonly = true`.
    """
    courses = get_courses()
    if not courses:
        raise ConfigError(
            "No courses are configured. Add a [courses.<key>] block to "
            "config.toml. See config.toml in this repo for an example."
        )

    if course_key is None:
        if len(courses) == 1:
            only_key = next(iter(courses))
            return only_key, courses[only_key]
        keys = ", ".join(sorted(courses))
        raise ConfigError(
            "Multiple courses configured — pass --course/-c with one of: "
            f"{keys}"
        )

    if course_key not in courses:
        keys = ", ".join(sorted(courses))
        raise ConfigError(
            f"Course '{course_key}' not found in config.toml. Available: {keys}"
        )

    return course_key, courses[course_key]


def get_course_id(course_key: str | None) -> int:
    """Resolve a course alias to a Canvas course ID."""
    alias, entry = get_course_entry(course_key)
    if "id" not in entry:
        raise ConfigError(f"Course '{alias}' is missing the required 'id' field.")
    return int(entry["id"])


def is_course_readonly(course_key: str | None) -> bool:
    """Whether `[courses.<alias>] readonly = true` is set.

    Until 2026-09-04 this flag was documentation only — the CLI parsed the
    config but never consulted it, so a `readonly` course accepted writes
    exactly like any other. Write commands now gate on it; see
    `commands/_common.guard_readonly`.
    """
    _, entry = get_course_entry(course_key)
    return bool(entry.get("readonly", False))


def require_credentials() -> tuple[str, str]:
    """Return `(base_url, token)` or raise ConfigError with setup instructions."""
    env = load_env()
    base_url = env.get("CANVAS_BASE_URL") or os.environ.get("CANVAS_BASE_URL", "")
    token = env.get("CANVAS_TOKEN") or os.environ.get("CANVAS_TOKEN", "")
    base_url = base_url.strip().rstrip("/")
    token = token.strip()

    missing: list[str] = []
    if not base_url:
        missing.append("CANVAS_BASE_URL")
    if not token:
        missing.append("CANVAS_TOKEN")
    if missing:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing)}.\n"
            "Copy .env.example to .env and fill in your Canvas instance URL "
            "and API token. See README.md for token generation instructions."
        )
    return base_url, token


def reset_caches() -> None:
    """Clear the lru_cache wrappers; mainly useful for tests."""
    load_env.cache_clear()
    get_config.cache_clear()


def redact_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"
