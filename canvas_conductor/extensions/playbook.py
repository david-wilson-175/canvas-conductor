"""IS Career Playbook deployment extension.

This file ships in the repository as a *worked example* of how Canvas
Conductor was used to populate a real Canvas course from a local `content/`
folder of markdown files plus a registry of media assets. It is also the
production tool the BYU IS career-prep committee uses to maintain the
Playbook on Canvas — but its primary value to other readers is as a
reference implementation. Copy patterns from it; don't be afraid to delete
this file if you fork the project for a different course.

What the IS Career Playbook is
------------------------------
A 5-week, completion-graded supplement that lives inside an Information
Systems course at BYU. Each week pairs a markdown content page with one assignment and
a set of NotebookLM-generated media (audio companion, deep-dive videos,
infographics):

    Step 1  Optimize Your Resume
    Step 2  Build Your Professional Presence (LinkedIn, Handshake)
    Step 3  Demonstrate Your Technical Skills (GitHub, Portfolio)
    Step 4  Interview with Confidence
    Step 5  Launch Your Career

Source-of-truth lives outside this extension: markdown in `../content/`,
media registry in `../canvas-integration/media-urls.toml`. This file
contains the deployment glue — placement of media inside each page, the
inline-CSS styling layer, and the per-week assignment shape.

What this extension demonstrates for future authors
---------------------------------------------------
- The auto-discovery extension pattern (drop file in extensions/, get
  `conductor playbook ...`).
- Idempotent create-or-update against Canvas (search by name, reuse).
- The two-step file upload (`client.upload_file`).
- Folder-scoped file lookup (`_course_files`).
- Markdown → embed-aware HTML → Canvas-sanitizer-aware inline styling.
- Shelling out to a sibling CLI (`nlm`) for upstream artifact fetching.
- Reading project-specific config under a per-course
  `[courses.<alias>.playbook]` section in the shared `config.toml`.

Commands
--------

  conductor playbook fetch-media   # download NotebookLM artifacts (via `nlm`)
  conductor playbook upload-media  # upload local files to Canvas folder
  conductor playbook deploy        # create modules/pages/assignments/items
  conductor playbook sync-pages    # re-render markdown pages with embeds

See `canvas-integration/README.md` at the project root for the full
workflow, the placement map, and source-of-truth conventions.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from canvas_conductor.client import get_client
from canvas_conductor.commands._common import (
    confirm_or_abort,
    emit,
    handle_canvas_error,
)
from canvas_conductor.config import (
    find_config_file,
    get_config,
    get_course_id,
    get_courses,
)
from canvas_conductor.exceptions import ConfigError
from canvas_conductor.utils.dates import to_canvas_datetime

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


app = typer.Typer(
    name="playbook",
    help="IS Career Playbook deployment commands (course content + media).",
    no_args_is_help=True,
)


# ============================================================================
# Per-week structure (markdown filenames + assignment shape)
# ============================================================================

# Course-level pages and modules that frame the per-week sequence.
# - Course Home Header: a standalone wiki page Canvas auto-renders at the top
#   of the course homepage (above the module list). Title is magic.
# - Getting Started: a "module 0" with orientation content for students,
#   shown before the five weekly Step modules.
COURSE_HOME_HEADER_TITLE = "Course Home Header"
GETTING_STARTED_MODULE = "Getting Started"
GETTING_STARTED_PAGE_TITLE = "Getting Started"
GETTING_STARTED_CONTENT_FILE = "getting-started.md"

# (week_key, module_name, content_filename)
WEEKS: list[tuple[str, str, str]] = [
    ("week_1", "Step 1: Optimize Your Resume",                                    "week-1-resumes.md"),
    ("week_2", "Step 2: Build Your Professional Presence (LinkedIn, Handshake)",  "week-2-linkedin-networking.md"),
    ("week_3", "Step 3: Demonstrate Your Technical Skills (GitHub, Portfolio)",    "week-3-work-experience-portfolios.md"),
    ("week_4", "Step 4: Interview with Confidence",                               "week-4-interview-prep.md"),
    ("week_5", "Step 5: Launch Your Career",                                      "week-5-recruiting-plan.md"),
]

# Maps each week_key to the [courses.<alias>.playbook.schedule] config-key prefix used to
# look up to-do dates (e.g. "step_1_page", "step_1_due"). See Change 3.
WEEK_KEY_TO_SCHEDULE_PREFIX: dict[str, str] = {
    "week_1": "step_1",
    "week_2": "step_2",
    "week_3": "step_3",
    "week_4": "step_4",
    "week_5": "step_5",
}

ASSIGNMENTS: dict[str, dict[str, Any]] = {
    "week_1": {
        "name": "Resume (PDF)",
        "submission_types": ["online_upload"],
        "allowed_extensions": ["pdf"],
    },
    "week_2": {
        "name": "LinkedIn Profile URL",
        "submission_types": ["online_url"],
    },
    "week_3": {
        "name": "GitHub Profile + Experience Plan",
        "submission_types": ["online_text_entry", "online_url"],
    },
    "week_4": {
        "name": "Quincia Mock Interview Completion",
        "submission_types": ["online_text_entry", "online_upload"],
    },
    "week_5": {
        "name": "MSB Student Database Update + Recruiting Plan",
        "submission_types": ["online_text_entry"],
    },
}

# Media placements drive inline embedding inside each week's page.
# Each entry: (anchor, canvas_filename, embed_title, kind)
#   anchor "TOP" injects the embed before the first H2 of the page.
#   any other anchor is matched as a substring against `<!-- MEDIA: ... -->`
#   placeholder comments in the source markdown.
MEDIA_PLACEMENTS: dict[str, list[tuple[str, str, str, str]]] = {
    "week_1": [
        ("TOP", "w1-podcast.mp4",
         "Listen on the go: Quantify your impact to beat algorithms (optional audio companion)", "audio"),
        ("Explainer video (NotebookLM, 2-3 min)", "w1-ats-deep-dive.mp4",
         "Watch: SEO For Your Resume (ATS Deep Dive)", "video"),
        ("Before/After Tear-Down (static visual) — Pre-program experience",
         "w1-reframing-experience.mp4",
         "Watch: The Experience Makeover", "video"),
        ("Before/After Tear-Down (static visual) — Pre-program experience",
         "w1-before-after-teardown.png",
         "Resume Impact Formula Guide", "image"),
        ("Good/Better/Best (static visual)", "w1-good-better-best.png",
         "Resume Bullet Writing Framework", "image"),
    ],
    "week_2": [
        ("TOP", "w2-podcast.mp4",
         "Listen on the go: Turn Your LinkedIn Into a Billboard (optional audio companion)", "audio"),
        ("Before/After Tear-Down (static visual) — Default/empty LinkedIn",
         "w2-linkedin-before-after.png",
         "LinkedIn Profile Optimization Strategy Comparison", "image"),
        ("Outreach Template Cards", "w2-outreach-templates.png",
         "Student Professional Networking Toolkit", "image"),
    ],
    "week_3": [
        ("TOP", "w3-podcast.mp4",
         "Listen on the go: Landing tech jobs with a proof layer (optional audio companion)", "audio"),
        ("Work Experience Snapshot", "w3-experience-snapshot.png",
         "The Experience Gap Comparison", "image"),
    ],
    "week_4": [
        ("TOP", "w4-podcast.mp4",
         "Listen on the go: Nail Your Interview with PBF and PAR (optional audio companion)", "audio"),
        ("Cinematic Overview (NotebookLM, 2-4 min)", "w4-par-deep-dive.mp4",
         "Watch: Mastering the PAR Framework", "video"),
    ],
    "week_5": [
        ("TOP", "w5-podcast.mp4",
         "Listen on the go: Beat the automated job rejection machine (optional audio companion)", "audio"),
        ("Career Fair Cheat Card", "w5-career-fair-cheat-card.png",
         "Career Fair Preparation Cheat Card", "image"),
        ("Audio Overview (NotebookLM podcast", "w5-four-step-job-search.mp4",
         "Watch: Bypassing Auto-Rejection (Four-Step Job Search)", "video"),
    ],
}


# ============================================================================
# Path resolution
#
# Reads [courses.<alias>.playbook] from canvas-conductor's config.toml, e.g.:
#
#   [courses.is-career-playbook]
#   id = 35416
#   name = "IS Career Playbook"
#   term = "Spring Semester 2026"
#
#   [courses.is-career-playbook.playbook]
#   content_dir       = "/Users/dave/code/is-career-playbook/content"
#   local_media_cache = "/tmp/playbook-media"
#   media_urls_file   = "/Users/dave/code/is-career-playbook/canvas-integration/media-urls.toml"
#
# Per-course nesting means multiple courses can have their own playbook
# config without colliding. The course alias is whatever you pass to `-c`,
# or the single configured course if there's only one.
# ============================================================================

def _resolve_playbook_alias(course: str | None) -> str:
    """Return the alias whose [courses.<alias>.playbook] block we'll read.

    If `course` is given, use it. Otherwise, if exactly one configured course
    has a `playbook` sub-block, use that one. Otherwise, raise ConfigError.
    """
    courses = get_courses()
    if course is not None:
        if course not in courses:
            keys = ", ".join(sorted(courses)) or "(none)"
            raise ConfigError(
                f"Course '{course}' not found in config.toml. Available: {keys}"
            )
        return course

    candidates = [k for k, v in courses.items() if isinstance(v.get("playbook"), dict)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        raise ConfigError(
            "No course has a [courses.<alias>.playbook] block. Add one to "
            "config.toml or pass --course/-c."
        )
    keys = ", ".join(sorted(candidates))
    raise ConfigError(
        "Multiple courses have a [playbook] sub-block — pass --course/-c "
        f"with one of: {keys}"
    )


def _playbook_section(course: str | None) -> dict[str, Any]:
    """Return the `[courses.<alias>.playbook]` config section, or {} if absent."""
    alias = _resolve_playbook_alias(course)
    return get_courses().get(alias, {}).get("playbook", {}) or {}


def _playbook_schedule(course: str | None) -> dict[str, str]:
    """Return the `[courses.<alias>.playbook.schedule]` section, or {} if missing."""
    return _playbook_section(course).get("schedule", {}) or {}


def _todo_datetime(date_iso: str | None) -> str | None:
    """Convert a YYYY-MM-DD schedule date into a Canvas-ready UTC datetime.

    End-of-day is resolved in the *course's* timezone (`[defaults] timezone`,
    else the local zone), not UTC. Anchoring at 23:59Z as this used to would
    put a Mountain Time deadline at 5:59 PM — visibly wrong for the
    `*_due` keys, which drive real assignment due dates.
    """
    if not date_iso:
        return None
    return to_canvas_datetime(date_iso)


def _playbook_paths(course: str | None) -> dict[str, Any]:
    cfg = _playbook_section(course)
    cfg_file = find_config_file()
    base = cfg_file.parent if cfg_file else Path.cwd()

    def _resolve(key: str, default: str) -> Path:
        value = Path(cfg.get(key, default)).expanduser()
        if not value.is_absolute():
            value = (base / value).resolve()
        return value

    return {
        "content_dir":         _resolve("content_dir", "../content"),
        "local_media_cache":   _resolve("local_media_cache", "/tmp/playbook-media"),
        "media_urls_file":     _resolve("media_urls_file", "../canvas-integration/media-urls.toml"),
        "canvas_media_folder": cfg.get("canvas_media_folder", "playbook-media"),
    }


# ============================================================================
# Page styling (inline CSS — Canvas's sanitizer strips font-weight/box-shadow/aspect-ratio)
# ============================================================================

WRAPPER_STYLE = (
    "max-width: 760px; margin: 0 auto; padding: 0 1rem; "
    "line-height: 1.65; color: #1f2937; "
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif;"
)
H2_STYLE = ("margin: 2.75rem 0 1rem; padding-bottom: 0.4rem; "
            "border-bottom: 1px solid #e5e7eb; color: #111827;")
H3_STYLE = "margin: 2rem 0 0.75rem; color: #111827;"
HR_STYLE = "border: none; border-top: 1px solid #e5e7eb; margin: 2.5rem 0;"
QUOTE_STYLE = ("margin: 1.5rem 0; padding: 0.85rem 1.1rem; "
               "border-left: 4px solid #94a3b8; background: #f8fafc; "
               "border-radius: 0 0.4rem 0.4rem 0; color: #334155;")
META_QUOTE_STYLE = ("margin: 0 0 2rem; padding: 0.4rem 0.75rem 0.4rem 1rem; "
                    "border-left: 3px solid #d1d5db; background: transparent; "
                    "color: #6b7280; font-size: 0.95em;")
AI_PANEL_STYLE = ("margin: 1.75rem 0; padding: 1rem 1.25rem; "
                  "border-left: 4px solid #8b5cf6; background: #faf5ff; "
                  "border-radius: 0 0.5rem 0.5rem 0;")
AI_LABEL_STYLE = "color: #6d28d9; margin-bottom: 0.5rem; font-size: 0.95em;"
TIP_PANEL_STYLE = ("margin: 1.75rem 0; padding: 1rem 1.25rem; "
                   "border-left: 4px solid #f59e0b; background: #fffbeb; "
                   "border-radius: 0 0.5rem 0.5rem 0;")
TIP_LABEL_STYLE = "color: #b45309; margin-bottom: 0.4rem; font-size: 0.95em;"
ACTIVITY_PANEL_STYLE = ("margin: 2.5rem 0; padding: 1.5rem 1.75rem; "
                       "background: #ecfeff; border: 1px solid #a5f3fc; "
                       "border-radius: 0.75rem;")
ACTIVITY_HEADING_STYLE = ("margin: 0 0 1rem; padding-bottom: 0.5rem; "
                          "border-bottom: 1px solid #a5f3fc; color: #0e7490;")
DELIVERABLE_PANEL_STYLE = ("margin: 2.5rem 0; padding: 1.5rem 1.75rem; "
                          "background: #eff6ff; border: 1px solid #bfdbfe; "
                          "border-radius: 0.75rem;")
DELIVERABLE_HEADING_STYLE = ("margin: 0 0 1rem; padding-bottom: 0.5rem; "
                             "border-bottom: 1px solid #bfdbfe; color: #1d4ed8;")
FIGURE_STYLE = "margin: 2rem auto; text-align: center;"
FIGURE_IMG_STYLE = ("display: block; max-width: 60%; height: auto; "
                    "margin: 0 auto; border: 1px solid #e5e7eb; border-radius: 0.5rem;")
FIGURE_VIDEO_STYLE = ("display: block; width: 100%; max-width: 640px; height: 360px; "
                      "margin: 0 auto; border: 1px solid #e5e7eb; border-radius: 0.5rem;")
FIGURE_AUDIO_STYLE = ("display: block; width: 100%; max-width: 480px; height: 14rem; "
                      "margin: 0 auto; border: 0;")
FIGCAPTION_STYLE = ("margin-top: 0.75rem; font-style: italic; "
                    "color: #6b7280; font-size: 0.9em;")


def _build_embed(course_id: int, file_id: int, title: str, kind: str) -> str:
    """Build a Canvas-ready embed: <figure><media/><figcaption/></figure>.

    Video/audio use Canvas's `media_attachments_iframe` URL — Canvas auto-stamps
    a verifier token at render time. Images use the file `/preview` endpoint.
    """
    if kind == "image":
        media = (
            f'<img src="/courses/{course_id}/files/{file_id}/preview" '
            f'alt="{title}" style="{FIGURE_IMG_STYLE}">'
        )
    else:
        media_type = "video" if kind == "video" else "audio"
        iframe_style = FIGURE_VIDEO_STYLE if kind == "video" else FIGURE_AUDIO_STYLE
        media = (
            f'<iframe style="{iframe_style}" '
            f'title="{media_type.capitalize()} player for {title}" '
            f'data-media-type="{media_type}" '
            f'src="/media_attachments_iframe/{file_id}" '
            f'loading="lazy" '
            f'allowfullscreen="allowfullscreen" '
            f'allow="fullscreen"></iframe>'
        )
    return (
        f'<figure style="{FIGURE_STYLE}">{media}'
        f'<figcaption style="{FIGCAPTION_STYLE}">{title}</figcaption>'
        f'</figure>'
    )


def _style_html(raw_html: str) -> str:
    """Apply modern inline styling: typography, callouts, panels, figures."""
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(raw_html, "html.parser")

    # Strip duplicate H1 — Canvas already shows page title at top of UI
    for h1 in soup.find_all("h1"):
        h1.decompose()

    for h2 in soup.find_all("h2"):
        h2["style"] = H2_STYLE
    for h3 in soup.find_all("h3"):
        h3["style"] = H3_STYLE
    for hr in soup.find_all("hr"):
        hr["style"] = HR_STYLE

    # Blockquote styling: meta vs default
    for bq in soup.find_all("blockquote"):
        text = bq.get_text(strip=True).lower()
        if "estimated time" in text:
            bq["style"] = META_QUOTE_STYLE
            strong = bq.find("strong")
            if strong:
                strong.insert(0, NavigableString("⏱  "))
        else:
            bq["style"] = QUOTE_STYLE

    # AI Playground: <p><strong>AI Playground:</strong></p> + <blockquote>
    for p in list(soup.find_all("p")):
        if not p.parent:
            continue
        strong = p.find("strong")
        if not strong or "AI Playground" not in strong.get_text():
            continue
        nxt = p.next_sibling
        while nxt is not None and not getattr(nxt, "name", None):
            nxt = nxt.next_sibling
        if not nxt or nxt.name != "blockquote":
            continue
        container = soup.new_tag("div", style=AI_PANEL_STYLE)
        label = soup.new_tag("div", style=AI_LABEL_STYLE)
        label_strong = soup.new_tag("strong")
        label_strong.append(NavigableString("✨  AI Playground"))
        label.append(label_strong)
        container.append(label)
        for child in list(nxt.children):
            container.append(child.extract())
        p.insert_before(container)
        p.decompose()
        nxt.decompose()

    # Pro tip: <p><strong>Pro tip:</strong> rest...</p>
    for p in list(soup.find_all("p")):
        if not p.parent:
            continue
        strong = p.find("strong")
        if not strong:
            continue
        if strong.get_text().strip().rstrip(":").lower() != "pro tip":
            continue
        container = soup.new_tag("div", style=TIP_PANEL_STYLE)
        label = soup.new_tag("div", style=TIP_LABEL_STYLE)
        label_strong = soup.new_tag("strong")
        label_strong.append(NavigableString("💡  Pro tip"))
        label.append(label_strong)
        container.append(label)
        new_p = soup.new_tag("p", style="margin: 0;")
        seen_strong: Any = False
        for c in list(p.children):
            if not seen_strong and c is strong:
                seen_strong = True
                continue
            if seen_strong is True and isinstance(c, NavigableString) and c.strip() == "":
                seen_strong = "consumed"
                continue
            new_p.append(c.extract())
        container.append(new_p)
        p.insert_before(container)
        p.decompose()

    # Activity / Deliverable section panels
    for h2 in list(soup.find_all("h2")):
        if not h2.parent:
            continue
        text = h2.get_text(strip=True)
        if text not in ("Activity", "Deliverable"):
            continue
        is_activity = text == "Activity"
        panel_style = ACTIVITY_PANEL_STYLE if is_activity else DELIVERABLE_PANEL_STYLE
        heading_style = ACTIVITY_HEADING_STYLE if is_activity else DELIVERABLE_HEADING_STYLE

        elements = [h2]
        cur = h2.next_sibling
        while cur is not None:
            if getattr(cur, "name", None) in ("h2", "hr"):
                break
            elements.append(cur)
            cur = cur.next_sibling

        prev = h2.previous_sibling
        while prev is not None and not getattr(prev, "name", None):
            prev = prev.previous_sibling
        if prev is not None and prev.name == "hr":
            prev.decompose()

        container = soup.new_tag("div", style=panel_style)
        h2["style"] = heading_style
        h2.insert_before(container)
        for elem in elements:
            container.append(elem.extract())

        nxt = container.next_sibling
        while nxt is not None and not getattr(nxt, "name", None):
            nxt = nxt.next_sibling
        if nxt is not None and nxt.name == "hr":
            nxt.decompose()

    return str(soup)


def _md_to_canvas_html(
    md_path: Path,
    course_id: int,
    placements: list[tuple[str, str, str, str]],
    file_ids: dict[str, int],
) -> str:
    """Render a markdown source file into a styled Canvas page body."""
    import markdown

    content = md_path.read_text()

    # Group placements by anchor (preserve list order within each group)
    by_anchor: dict[str, list[tuple[str, str, str]]] = {}
    for anchor, fname, title, kind in placements:
        by_anchor.setdefault(anchor, []).append((fname, title, kind))

    # Replace each non-TOP MEDIA placeholder with the corresponding embed(s)
    for anchor, items in by_anchor.items():
        if anchor == "TOP":
            continue
        embeds = []
        for fname, title, kind in items:
            fid = file_ids.get(fname)
            if not fid:
                emit(f"  WARN: no Canvas file id for '{fname}' (skipping embed)")
                continue
            embeds.append(_build_embed(course_id, fid, title, kind))
        if not embeds:
            continue
        replacement = "\n\n" + "\n".join(embeds) + "\n\n"
        pattern = re.compile(
            r'<!--\s*MEDIA:[^>]*?' + re.escape(anchor) + r'.*?-->',
            re.DOTALL,
        )
        new_content, n = pattern.subn(replacement, content, count=1)
        if n == 0:
            emit(f"  WARN: anchor not found in markdown: {anchor!r}")
        content = new_content

    # Strip remaining (unmatched) MEDIA placeholders + TODO comments + tracking sections
    content = re.sub(r'<!--\s*MEDIA:.*?-->', '', content, flags=re.DOTALL)
    content = re.sub(r'<!--\s*TODO:.*?-->', '', content, flags=re.DOTALL)
    content = re.sub(r'\n## Media Assets\n.*', '', content, flags=re.DOTALL)
    content = re.sub(r'\n## Open Questions\n.*', '', content, flags=re.DOTALL)

    # Inject TOP-anchored embeds right before the first H2
    top_items = by_anchor.get("TOP", [])
    if top_items:
        top_blocks = []
        for fname, title, kind in top_items:
            fid = file_ids.get(fname)
            if not fid:
                emit(f"  WARN: no Canvas file id for TOP embed '{fname}'")
                continue
            top_blocks.append(_build_embed(course_id, fid, title, kind))
        if top_blocks:
            top_html = "\n\n" + "\n".join(top_blocks) + "\n\n"
            content, n = re.subn(r'(?m)^(## )', top_html + r'\1', content, count=1)
            if n == 0:
                content = top_html + content

    content = re.sub(r'\n{3,}', '\n\n', content)

    html = markdown.markdown(content, extensions=["tables", "fenced_code", "md_in_html"])
    html = _style_html(html)
    return f'<div style="{WRAPPER_STYLE}">\n{html}\n</div>'


def _course_home_header_body(course_id: int, file_ids: dict[str, int]) -> str:
    """Build the standalone wiki-page body that Canvas renders above the
    module list on the course homepage. Uses `playbook-banner.png` from
    the playbook-media folder if present; otherwise renders a styled
    placeholder until a real banner is uploaded.
    """
    banner_fid = file_ids.get("playbook-banner.png")
    if banner_fid:
        banner = (
            f'<img src="/courses/{course_id}/files/{banner_fid}/preview" '
            f'alt="IS Career Playbook banner" '
            f'style="display: block; width: 100%; height: auto; '
            f'border-radius: 0.5rem;">'
        )
    else:
        banner = (
            '<div style="background: #f3f4f6; color: #6b7280; '
            'padding: 3rem 2rem; text-align: center; '
            'border: 1px dashed #d1d5db; border-radius: 0.5rem; '
            'font-style: italic;">'
            'Banner image coming soon'
            '</div>'
        )
    welcome = (
        '<p>Welcome to the <strong>IS Career Playbook</strong>, a 5-step '
        'sequence designed to help you build a professional presence, '
        'sharpen your interview skills, and launch your job search with '
        'confidence. Work through each step at your own pace, complete the '
        'deliverables, and you will be career-ready by the end of the '
        'semester.</p>'
    )
    return (
        f'<div style="{WRAPPER_STYLE}">'
        f'{banner}'
        f'<div style="margin-top: 1.5rem;">{welcome}</div>'
        f'</div>'
    )


def _course_files(client, course_id: int, folder_name: str) -> dict[str, int]:
    """Return {filename: file_id} for files in the named course folder."""
    folders = client.get_all(f"/courses/{course_id}/folders")
    folder = next((f for f in folders if f.get("name") == folder_name), None)
    if folder is None:
        return {}
    files = client.get_all(f"/folders/{folder['id']}/files")
    return {f["display_name"]: f["id"] for f in files}


# ============================================================================
# Commands
# ============================================================================

@app.command("fetch-media")
def fetch_media(
    course: str = typer.Option(None, "-c", "--course",
                               help="Course alias whose [playbook] block names the registry."),
    week: str = typer.Option(None, "--week", help="Limit to one week (week_1..week_5)."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-download files even if present."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Download NotebookLM artifacts to the local cache via the `nlm` CLI.

    Reads media-urls.toml for notebook_id + artifact_id + local_filename, then
    shells out to `nlm download <type> <notebook_id> --id <artifact_id> --output <path>`.
    Skips files that already exist (use --refresh to re-download).
    """
    paths = _playbook_paths(course)
    media_urls_path: Path = paths["media_urls_file"]
    cache_dir: Path = paths["local_media_cache"]

    if not media_urls_path.is_file():
        emit(f"ERROR: media registry not found at {media_urls_path}")
        raise typer.Exit(code=2)

    cache_dir.mkdir(parents=True, exist_ok=True)
    with media_urls_path.open("rb") as fh:
        registry = tomllib.load(fh)

    targets: list[dict[str, Any]] = []
    for wkey, assets in registry.items():
        if not isinstance(assets, dict) or not wkey.startswith("week_"):
            continue
        if week and wkey != week:
            continue
        for asset_key, meta in assets.items():
            if not isinstance(meta, dict):
                continue
            if "local_filename" not in meta:
                emit(f"  WARN: skipping {wkey}.{asset_key} — no local_filename in registry")
                continue
            kind = meta.get("type")
            if kind not in ("infographic", "video", "audio"):
                emit(f"  WARN: skipping {wkey}.{asset_key} — unknown type {kind!r}")
                continue
            targets.append({
                "kind": kind,
                "notebook_id": meta["notebook_id"],
                "artifact_id": meta.get("artifact_id"),
                "local_path": cache_dir / meta["local_filename"],
            })

    if not targets:
        emit("No targets found in media registry.")
        return

    emit(f"Will fetch {len(targets)} artifact(s) into {cache_dir}")
    fetched = 0
    for t in targets:
        if t["local_path"].exists() and not refresh:
            if verbose:
                emit(f"  skip (exists): {t['local_path'].name}")
            continue
        cmd = [
            "nlm", "download", t["kind"],
            t["notebook_id"],
            "--output", str(t["local_path"]),
            "--no-progress",
        ]
        if t["artifact_id"]:
            cmd.extend(["--id", t["artifact_id"]])
        emit(f"  {t['kind']:11s} {t['local_path'].name}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            emit(f"    ERROR: {(result.stderr or result.stdout).strip()[:200]}")
        else:
            fetched += 1
            if verbose:
                emit("    OK")

    emit(f"\nFetched {fetched} new file(s); cache at {cache_dir}")


def _upload_manifest() -> list[tuple[str, str]]:
    """Build [(canvas_filename, content_type), ...] from MEDIA_PLACEMENTS, deduped."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for items in MEDIA_PLACEMENTS.values():
        for _anchor, fname, _title, _kind in items:
            if fname in seen:
                continue
            seen.add(fname)
            ct = "image/png" if fname.endswith(".png") else "video/mp4"
            out.append((fname, ct))
    return out


@app.command("upload-media")
def upload_media(
    course: str = typer.Option(None, "-c", "--course"),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="Re-upload files that already exist on Canvas."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Upload local media files to the Canvas course's playbook-media folder.

    Files must have already been downloaded via `playbook fetch-media`. By
    default, files already present in the Canvas folder (matched by name) are
    skipped; pass --refresh to overwrite them.
    """
    try:
        cid = get_course_id(course)
        paths = _playbook_paths(course)
        client = get_client(verbose=verbose)

        existing = _course_files(client, cid, paths["canvas_media_folder"])
        manifest = _upload_manifest()

        ready: list[tuple[str, str, Path]] = []
        already: list[str] = []
        missing: list[str] = []
        for fname, ct in manifest:
            local = paths["local_media_cache"] / fname
            if not local.is_file():
                missing.append(str(local))
                continue
            if fname in existing and not refresh:
                already.append(fname)
                continue
            ready.append((fname, ct, local))

        emit(
            f"Manifest: {len(manifest)} files | already on Canvas: {len(already)} "
            f"| to upload: {len(ready)} | missing locally: {len(missing)}"
        )
        if missing:
            emit("\n  Missing locally (run `playbook fetch-media` first):")
            for m in missing:
                emit(f"    {m}")

        if dry_run:
            emit("\nDRY-RUN: would upload:")
            for fname, ct, _ in ready:
                emit(f"    {fname} ({ct})")
            return

        if not ready:
            emit("Nothing to upload.")
            return

        for i, (fname, ct, local) in enumerate(ready, 1):
            size_mb = local.stat().st_size / 1_000_000
            emit(f"  [{i}/{len(ready)}] uploading {fname} ({size_mb:.1f} MB)")
            result = client.upload_file(
                course_id=cid,
                local_path=str(local),
                folder_path=paths["canvas_media_folder"],
                content_type=ct,
                on_duplicate="overwrite",
            )
            if verbose:
                emit(f"    -> file id {result.get('id')}")

        emit(
            f"\nUploaded {len(ready)} file(s) to course {cid}/"
            f"{paths['canvas_media_folder']}"
        )
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("deploy")
def deploy(
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Build the full course structure: assignment group, modules, pages, items.

    Each module ends up with: page → "Media" subheader → "Deliverable" subheader
    → assignment. Media is embedded directly in the page body (not as separate
    module items) — the embeds use Canvas-uploaded files in the playbook-media
    folder, so run `upload-media` before `deploy` if files aren't already there.

    Idempotent: existing modules/pages/assignments matched by name are reused
    rather than duplicated.
    """
    try:
        cid = get_course_id(course)
        paths = _playbook_paths(course)

        schedule = _playbook_schedule(course)

        if dry_run:
            client = None
            file_ids: dict[str, int] = {}
            emit(f"[DRY-RUN] Deploying playbook to course {cid}\n")
        else:
            client = get_client(verbose=verbose)
            file_ids = _course_files(client, cid, paths["canvas_media_folder"])
            emit(f"Found {len(file_ids)} files in '{paths['canvas_media_folder']}' folder.")
            emit(f"\nDeploying playbook to course {cid}\n")

        if schedule:
            emit(f"Schedule: {len(schedule)} entries from [courses.{course or '?'}.playbook.schedule].")
        else:
            emit("Schedule: none configured ([courses.<alias>.playbook.schedule] absent or empty).")

        # 1. Set default view to modules
        emit("Ensuring course default_view is 'modules'...")
        if client:
            client.put(f"/courses/{cid}", {"course": {"default_view": "modules"}})

        # 2. Assignment group
        emit("\nEnsuring assignment group 'IS Career Playbook' (5%)...")
        ag_id: int | None = None
        if client:
            existing_ags = client.get_all(f"/courses/{cid}/assignment_groups")
            ag = next((g for g in existing_ags if g.get("name") == "IS Career Playbook"), None)
            if ag:
                ag_id = ag["id"]
                emit(f"  reuse existing assignment group id={ag_id}")
            else:
                ag = client.post(
                    f"/courses/{cid}/assignment_groups",
                    data={"name": "IS Career Playbook", "group_weight": 5.0},
                )
                ag_id = ag["id"]
                emit(f"  created assignment group id={ag_id}")

        # 3. Course Home Header — standalone page Canvas auto-renders at the
        # top of the course homepage. Not added to any module.
        emit(f"\nEnsuring '{COURSE_HOME_HEADER_TITLE}' page...")
        if client:
            header_body = _course_home_header_body(cid, file_ids)
            existing_pages = client.get_all(
                f"/courses/{cid}/pages",
                params={"search_term": COURSE_HOME_HEADER_TITLE},
            )
            header_page = next(
                (p for p in existing_pages if p.get("title") == COURSE_HOME_HEADER_TITLE),
                None,
            )
            if header_page:
                client.put(
                    f"/courses/{cid}/pages/{header_page['url']}",
                    {"wiki_page": {"body": header_body}},
                )
                emit(f"  reuse url='{header_page['url']}', body updated")
            else:
                new_page = client.post(
                    f"/courses/{cid}/pages",
                    data={"wiki_page": {
                        "title": COURSE_HOME_HEADER_TITLE,
                        "body": header_body,
                        "published": False,
                    }},
                )
                emit(f"  created url='{new_page.get('url')}'")
        else:
            emit(f"  would create page '{COURSE_HOME_HEADER_TITLE}'")

        # 4. Getting Started "module 0" — orientation page before the steps
        emit(f"\nEnsuring '{GETTING_STARTED_MODULE}' module + page (position 1)...")
        gs_content_path: Path = paths["content_dir"] / GETTING_STARTED_CONTENT_FILE
        gs_module_id: int | None = None
        gs_page_url: str | None = None
        if client:
            existing_modules = client.get_all(f"/courses/{cid}/modules")
            gs_mod = next(
                (m for m in existing_modules if m.get("name") == GETTING_STARTED_MODULE),
                None,
            )
            if gs_mod:
                gs_module_id = gs_mod["id"]
                emit(f"  module: reuse id={gs_module_id}")
            else:
                gs_mod = client.post(
                    f"/courses/{cid}/modules",
                    data={"module": {"name": GETTING_STARTED_MODULE, "position": 1}},
                )
                gs_module_id = gs_mod["id"]
                emit(f"  module: created id={gs_module_id}")

            if gs_content_path.is_file():
                gs_body = _md_to_canvas_html(gs_content_path, cid, [], file_ids)
                existing_pages = client.get_all(
                    f"/courses/{cid}/pages",
                    params={"search_term": GETTING_STARTED_PAGE_TITLE},
                )
                gs_page = next(
                    (p for p in existing_pages if p.get("title") == GETTING_STARTED_PAGE_TITLE),
                    None,
                )
                if gs_page:
                    gs_page_url = gs_page["url"]
                    client.put(
                        f"/courses/{cid}/pages/{gs_page_url}",
                        {"wiki_page": {"body": gs_body}},
                    )
                    emit(f"  page:   reuse url='{gs_page_url}', body updated")
                else:
                    gs_page = client.post(
                        f"/courses/{cid}/pages",
                        data={"wiki_page": {
                            "title": GETTING_STARTED_PAGE_TITLE,
                            "body": gs_body,
                            "published": False,
                        }},
                    )
                    gs_page_url = gs_page.get("url")
                    emit(f"  page:   created url='{gs_page_url}'")

                # Module item linking the page (idempotent on page_url)
                existing_items = client.get_all(
                    f"/courses/{cid}/modules/{gs_module_id}/items"
                )
                page_urls_present = {
                    it.get("page_url") for it in existing_items
                    if it.get("type") == "Page"
                }
                if gs_page_url and gs_page_url not in page_urls_present:
                    client.post(
                        f"/courses/{cid}/modules/{gs_module_id}/items",
                        data={"module_item": {
                            "type": "Page",
                            "page_url": gs_page_url,
                        }},
                    )
            else:
                emit(f"  page:   SKIP (markdown not found at {gs_content_path})")
        else:
            emit(f"  would create module '{GETTING_STARTED_MODULE}' and its page")

        # 5. Per-week: module + page (with embedded media) + assignment + module items
        # Position 2 onward — Getting Started occupies position 1.
        for position, (week_key, module_name, content_filename) in enumerate(WEEKS, 2):
            emit(f"\n--- {module_name} ---")
            content_path: Path = paths["content_dir"] / content_filename
            sched_prefix = WEEK_KEY_TO_SCHEDULE_PREFIX.get(week_key, "")
            todo_at = _todo_datetime(schedule.get(f"{sched_prefix}_page"))
            due_at = _todo_datetime(schedule.get(f"{sched_prefix}_due"))
            if todo_at or due_at:
                emit(
                    f"  schedule: page todo={todo_at or '—'}, "
                    f"assignment due={due_at or '—'}"
                )

            # Module
            module_id: int | None = None
            if client:
                existing_modules = client.get_all(f"/courses/{cid}/modules")
                mod = next((m for m in existing_modules if m.get("name") == module_name), None)
                if mod:
                    module_id = mod["id"]
                    emit(f"  module: reuse id={module_id}")
                else:
                    mod = client.post(
                        f"/courses/{cid}/modules",
                        data={"module": {"name": module_name, "position": position}},
                    )
                    module_id = mod["id"]
                    emit(f"  module: created id={module_id}")
            else:
                emit(f"  module: would create '{module_name}' (position {position})")

            # Page (markdown → embed-aware HTML → styled)
            page_url: str | None = None
            if content_path.is_file():
                html_body = _md_to_canvas_html(
                    content_path, cid,
                    MEDIA_PLACEMENTS.get(week_key, []), file_ids,
                )
                emit(f"  page: '{module_name}' ({len(html_body)} chars HTML)")
                if client:
                    existing_pages = client.get_all(
                        f"/courses/{cid}/pages",
                        params={"search_term": module_name},
                    )
                    page = next((p for p in existing_pages if p.get("title") == module_name), None)
                    if page:
                        page_url = page["url"]
                        put_payload: dict[str, Any] = {"body": html_body}
                        if todo_at:
                            put_payload["student_todo_at"] = todo_at
                        client.put(
                            f"/courses/{cid}/pages/{page_url}",
                            {"wiki_page": put_payload},
                        )
                        emit(f"    reuse url='{page_url}', body updated")
                    else:
                        create_payload: dict[str, Any] = {
                            "title": module_name,
                            "body": html_body,
                            "published": False,
                        }
                        if todo_at:
                            create_payload["student_todo_at"] = todo_at
                        page = client.post(
                            f"/courses/{cid}/pages",
                            data={"wiki_page": create_payload},
                        )
                        page_url = page.get("url")
                        emit(f"    created url='{page_url}'")
            else:
                emit(f"  page: SKIP (markdown not found at {content_path})")

            # Assignment
            asgn_meta = ASSIGNMENTS.get(week_key, {})
            assignment_id: int | None = None
            if client and asgn_meta:
                existing_asgns = client.get_all(f"/courses/{cid}/assignments")
                asgn = next(
                    (a for a in existing_asgns if a.get("name") == asgn_meta["name"]),
                    None,
                )
                if asgn:
                    assignment_id = asgn["id"]
                    emit(f"  assignment: reuse id={assignment_id} ({asgn_meta['name']})")
                    if due_at:
                        client.put(
                            f"/courses/{cid}/assignments/{assignment_id}",
                            {"assignment": {"due_at": due_at}},
                        )
                else:
                    asgn_payload: dict[str, Any] = {
                        "name": asgn_meta["name"],
                        "points_possible": 10,
                        "grading_type": "pass_fail",
                        "published": False,
                        "description":
                            f"<p>See the {module_name} page for full instructions.</p>",
                        "submission_types":
                            asgn_meta.get("submission_types", ["online_text_entry"]),
                    }
                    if "allowed_extensions" in asgn_meta:
                        asgn_payload["allowed_extensions"] = asgn_meta["allowed_extensions"]
                    if ag_id:
                        asgn_payload["assignment_group_id"] = ag_id
                    if due_at:
                        asgn_payload["due_at"] = due_at
                    asgn = client.post(
                        f"/courses/{cid}/assignments",
                        data={"assignment": asgn_payload},
                    )
                    assignment_id = asgn["id"]
                    emit(f"  assignment: created id={assignment_id} ({asgn_meta['name']})")
            elif asgn_meta:
                emit(f"  assignment: would create '{asgn_meta['name']}'")

            # Module items: just the content page and its deliverable, in
            # that order. No subheaders — keeping the per-module structure
            # minimal so students see "page → assignment" with no clutter.
            # Idempotency anchors on page_url / content_id rather than
            # display title (Canvas auto-renames items to match the page
            # or assignment title, so title-based checks miss duplicates).
            if client and module_id:
                existing_items = client.get_all(f"/courses/{cid}/modules/{module_id}/items")
                page_urls_present = {
                    it.get("page_url") for it in existing_items
                    if it.get("type") == "Page"
                }
                assignment_ids_present = {
                    it.get("content_id") for it in existing_items
                    if it.get("type") == "Assignment"
                }

                if page_url and page_url not in page_urls_present:
                    client.post(
                        f"/courses/{cid}/modules/{module_id}/items",
                        data={"module_item": {
                            "type": "Page",
                            "page_url": page_url,
                        }},
                    )
                if assignment_id and assignment_id not in assignment_ids_present:
                    client.post(
                        f"/courses/{cid}/modules/{module_id}/items",
                        data={"module_item": {
                            "type": "Assignment",
                            "content_id": assignment_id,
                        }},
                    )

        emit("\nDeploy complete.")
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("sync-pages")
def sync_pages(
    course: str = typer.Option(None, "-c", "--course"),
    week: str = typer.Option(None, "--week", help="Limit to one week (week_1..week_5)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Re-render markdown pages with embedded media and update Canvas.

    Use this after editing markdown content, the placement map, or the styling
    layer. Idempotent — only updates page bodies; does not touch modules or
    assignments.
    """
    try:
        cid = get_course_id(course)
        paths = _playbook_paths(course)
        schedule = _playbook_schedule(course)
        client = get_client(verbose=verbose)

        file_ids = _course_files(client, cid, paths["canvas_media_folder"])
        emit(f"Found {len(file_ids)} files in '{paths['canvas_media_folder']}' folder.")

        existing_pages = client.get_all(f"/courses/{cid}/pages")
        page_map = {p["title"]: p["url"] for p in existing_pages}

        for week_key, module_name, content_filename in WEEKS:
            if week and week_key != week:
                continue
            content_path: Path = paths["content_dir"] / content_filename
            if not content_path.is_file():
                emit(f"  SKIP: {content_filename} not found")
                continue
            if module_name not in page_map:
                emit(f"  WARN: no page '{module_name}' on Canvas — run `playbook deploy` first")
                continue
            html_body = _md_to_canvas_html(
                content_path, cid,
                MEDIA_PLACEMENTS.get(week_key, []), file_ids,
            )
            slug = page_map[module_name]
            sched_prefix = WEEK_KEY_TO_SCHEDULE_PREFIX.get(week_key, "")
            todo_at = _todo_datetime(schedule.get(f"{sched_prefix}_page"))
            tag = "[DRY-RUN] " if dry_run else ""
            todo_note = f" + todo:{todo_at}" if todo_at else ""
            emit(f"  {tag}{module_name}: {len(html_body)} chars -> {slug}{todo_note}")
            if not dry_run:
                payload: dict[str, Any] = {"body": html_body}
                if todo_at:
                    payload["student_todo_at"] = todo_at
                client.put(f"/courses/{cid}/pages/{slug}", {"wiki_page": payload})

        emit("\nDone.")
    except Exception as exc:
        raise handle_canvas_error(exc)


@app.command("reset")
def reset(
    course: str = typer.Option(None, "-c", "--course"),
    dry_run: bool = typer.Option(
        True, "--dry-run/--commit",
        help="Default: dry-run. Pass --commit to actually delete.",
    ),
    yes: bool = typer.Option(False, "-y", "--yes",
                             help="Skip the final confirmation prompt."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Delete everything `playbook deploy` would have created in a course.

    Resources targeted (all matched by name, same as `deploy`'s idempotency
    check, so nothing outside the playbook scope is touched):

      - The 5 weekly modules (deletes module items too, but not the linked
        page/assignment objects — those are removed separately below)
      - The 5 weekly pages
      - The 5 weekly assignments
      - The "IS Career Playbook" assignment group
      - All files in the playbook-media folder (folder itself is left in
        place; `upload-media` will reuse it next time)

    Default is dry-run. Pass --commit to actually delete. The cache at
    `/tmp/playbook-media` is untouched — clear it manually if you want a
    truly clean slate before the next `fetch-media`.
    """
    try:
        cid = get_course_id(course)
        paths = _playbook_paths(course)
        client = get_client(verbose=verbose)

        # Discovery: anchor on the most stable signals (assignment group
        # name + assignment names + folder name) rather than module/page
        # names, which can drift when content is renamed in source.
        assignment_names = {meta["name"] for meta in ASSIGNMENTS.values()}
        ag_name = "IS Career Playbook"

        # Assignment group (stable name)
        ag_groups = client.get_all(f"/courses/{cid}/assignment_groups")
        target_ags = [g for g in ag_groups if g.get("name") == ag_name]
        ag_ids = {g["id"] for g in target_ags}

        # Assignments: in the playbook group OR matching the known names
        assignments = client.get_all(f"/courses/{cid}/assignments")
        target_assignments = [
            a for a in assignments
            if a.get("assignment_group_id") in ag_ids
            or a.get("name") in assignment_names
        ]
        target_assignment_ids = {a["id"] for a in target_assignments}

        # Modules: any module that contains a playbook-managed assignment
        # as an item, OR whose name matches the Getting Started orientation
        # module (which has no assignment to anchor on). Walk items to find
        # associated pages too.
        anchored_module_names = {GETTING_STARTED_MODULE}
        all_modules = client.get_all(f"/courses/{cid}/modules")
        target_modules: list[dict] = []
        target_page_urls: set[str] = set()
        for m in all_modules:
            items = client.get_all(f"/courses/{cid}/modules/{m['id']}/items")
            has_playbook_asgn = any(
                it.get("type") == "Assignment"
                and it.get("content_id") in target_assignment_ids
                for it in items
            )
            name_matches = m.get("name") in anchored_module_names
            if not has_playbook_asgn and not name_matches:
                continue
            target_modules.append(m)
            for it in items:
                if it.get("type") == "Page" and it.get("page_url"):
                    target_page_urls.add(it["page_url"])

        # Pages: those referenced by a playbook module's Page items, OR
        # whose title matches a current WEEKS module name, the Getting Started
        # orientation page, or the Course Home Header. The title fallback makes
        # reset still work if modules have already been deleted (e.g. from a
        # previous failed reset run).
        module_titles = {wname for _, wname, _ in WEEKS}
        module_titles.add(GETTING_STARTED_PAGE_TITLE)
        module_titles.add(COURSE_HOME_HEADER_TITLE)
        all_pages = client.get_all(f"/courses/{cid}/pages")
        target_pages = [
            p for p in all_pages
            if p.get("url") in target_page_urls
            or p.get("title") in module_titles
        ]

        # Files in the playbook-media folder (stable folder name)
        folders = client.get_all(f"/courses/{cid}/folders")
        media_folder = next(
            (f for f in folders if f.get("name") == paths["canvas_media_folder"]),
            None,
        )
        target_files = (
            client.get_all(f"/folders/{media_folder['id']}/files")
            if media_folder else []
        )

        # Report
        emit(f"Course {cid}: playbook resources discovered")
        emit(f"  modules        ({len(target_modules)})")
        for m in target_modules:
            emit(f"    - {m['name']}  (id={m['id']})")
        emit(f"  pages          ({len(target_pages)})")
        for p in target_pages:
            emit(f"    - {p['title']}  (url={p['url']})")
        emit(f"  assignments    ({len(target_assignments)})")
        for a in target_assignments:
            emit(f"    - {a['name']}  (id={a['id']})")
        emit(f"  asgn groups    ({len(target_ags)})")
        for g in target_ags:
            emit(f"    - {g['name']}  (id={g['id']})")
        emit(f"  media files    ({len(target_files)}"
             f" in '{paths['canvas_media_folder']}' folder)")
        for f in target_files:
            emit(f"    - {f['display_name']}  (id={f['id']})")

        total = (
            len(target_modules) + len(target_pages) + len(target_assignments)
            + len(target_ags) + len(target_files)
        )

        if total == 0:
            emit("\nNothing to delete.")
            return

        if dry_run:
            emit(f"\nDry-run. {total} resource(s) would be deleted. "
                 "Pass --commit to actually delete.")
            return

        confirm_or_abort(
            f"Permanently delete {total} resource(s) from course {cid}? "
            "This cannot be undone.",
            yes=yes, dry_run=False,
        )

        # Delete in safe order: modules first (drops items), then the
        # underlying content + group + files.
        emit("")
        for m in target_modules:
            client.delete(f"/courses/{cid}/modules/{m['id']}")
            emit(f"  - module        {m['name']}")
        for p in target_pages:
            # Canvas refuses to delete the front page; demote it first.
            if p.get("front_page"):
                client.put(
                    f"/courses/{cid}/pages/{p['url']}",
                    {"wiki_page": {"front_page": False}},
                )
            client.delete(f"/courses/{cid}/pages/{p['url']}")
            emit(f"  - page          {p['title']}")
        for a in target_assignments:
            client.delete(f"/courses/{cid}/assignments/{a['id']}")
            emit(f"  - assignment    {a['name']}")
        for g in target_ags:
            client.delete(f"/courses/{cid}/assignment_groups/{g['id']}")
            emit(f"  - asgn group    {g['name']}")
        for f in target_files:
            client.delete(f"/files/{f['id']}")
            emit(f"  - media file    {f['display_name']}")

        emit(f"\nDeleted {total} resource(s). Run `playbook deploy` to rebuild.")
    except typer.Exit:
        raise
    except Exception as exc:
        raise handle_canvas_error(exc)
