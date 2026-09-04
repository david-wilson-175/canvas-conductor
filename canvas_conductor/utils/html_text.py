"""Convert Canvas-authored HTML into readable plain text.

Discussion entries, page bodies and assignment descriptions all come back
from Canvas as HTML. Anything that wants to read, count or grep that prose
needs a text rendering of it, so this is the single place that does the
conversion.

Two Canvas-specific reasons this is more than a regex:

1. **Institutions inject `<link>` and `<script>` tags into entry bodies.**
   A BYU discussion entry posted as `<p>Hi</p>` reads back as
   ``<link rel="stylesheet" href="…/dp_app.css"><p>Hi</p><script
   src="…/dp_app.js"></script>``. Naively stripping tags leaves the script
   URL in the text and inflates every word count.

2. **Block structure carries meaning.** `<p>`/`<br>`/`<li>` boundaries are
   the only paragraph breaks a Canvas body has, so they become newlines
   rather than disappearing.

Standard library only — no bs4 dependency for what is ultimately a
tag-aware text dump.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser


# Elements whose *content* is not prose. HTMLParser hands `<script>` and
# `<style>` bodies to handle_data as raw CDATA, so they have to be skipped
# explicitly or the JavaScript ends up in the text.
_SKIP_CONTAINERS = {"script", "style", "noscript", "head", "title"}

# Void elements that carry no content. They must never open a skip region:
# `<link>` has no end tag, so incrementing a depth counter on it would
# swallow the entire rest of the document.
_VOID_IGNORED = {"link", "meta", "base"}

# Elements that start/end a visual block, and how much vertical space the
# boundary is worth: 2 newlines (a blank line) between paragraph-level
# blocks, 1 between list items and table cells. Boundaries take the
# strongest value of everything that meets at that point rather than
# accumulating, so `</p><p>` is one blank line, not two.
_BLOCK_BREAKS = {
    "address": 2, "article": 2, "aside": 2, "blockquote": 2, "div": 2,
    "dl": 2, "figure": 2, "figcaption": 2, "footer": 2, "form": 2, "h1": 2,
    "h2": 2, "h3": 2, "h4": 2, "h5": 2, "h6": 2, "header": 2, "hr": 2,
    "main": 2, "nav": 2, "ol": 2, "p": 2, "pre": 2, "section": 2,
    "table": 2, "tbody": 2, "tfoot": 2, "thead": 2, "ul": 2,
    "br": 1, "dd": 1, "dt": 1, "li": 1, "td": 1, "th": 1, "tr": 1,
}

_INLINE_SPACES = re.compile(r"[ \t\f\v\r]+")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPTISH_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


class _TextExtractor(HTMLParser):
    """Accumulate visible text, turning block boundaries into newlines."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._pending_break = 0

    # -- tag handling ---------------------------------------------------

    def _boundary(self, tag: str) -> None:
        strength = _BLOCK_BREAKS.get(tag)
        if strength and self.parts:
            self._pending_break = max(self._pending_break, strength)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _VOID_IGNORED:
            return
        if tag in _SKIP_CONTAINERS:
            self._skip_depth += 1
            return
        if not self._skip_depth:
            self._boundary(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # Self-closing form (`<br/>`, `<link/>`): opens and closes at once.
        tag = tag.lower()
        if tag in _SKIP_CONTAINERS or tag in _VOID_IGNORED:
            return
        if not self._skip_depth:
            self._boundary(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_IGNORED:
            return
        if tag in _SKIP_CONTAINERS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if not self._skip_depth:
            self._boundary(tag)

    # -- text handling --------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._pending_break and data.strip():
            self.parts.append("\n" * self._pending_break)
            self._pending_break = 0
        self.parts.append(data)


def _normalize(text: str) -> str:
    """Collapse runs of spaces and blank lines; trim each line."""
    text = text.replace("\xa0", " ").replace("\u200b", "")
    lines = [_INLINE_SPACES.sub(" ", line).strip() for line in text.split("\n")]

    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1]:
            # Keep a single blank line as a paragraph break.
            out.append("")
    return "\n".join(out).strip()


def html_to_text(value: str | None) -> str:
    """Render a Canvas HTML body as plain text.

    Block elements become newlines, `<script>`/`<style>`/`<link>` noise is
    dropped entirely, entities are unescaped, and whitespace is collapsed
    so that word counts mean something.

    >>> html_to_text('<p>Hello <em>there</em></p><p>Bye</p>')
    'Hello there\\nBye'
    """
    if not value:
        return ""

    parser = _TextExtractor()
    try:
        parser.feed(str(value))
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # pragma: no cover - HTMLParser is very tolerant
        # Never let an unparseable body break a read command; fall back to
        # a blunt strip rather than returning nothing.
        text = unescape(_TAG_RE.sub(" ", _SCRIPTISH_RE.sub(" ", str(value))))

    return _normalize(text)


def word_count(text: str | None) -> int:
    """Count whitespace-separated words in already-plain text."""
    if not text:
        return 0
    return len(text.split())
