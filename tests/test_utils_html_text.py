"""Tests for the Canvas HTML → plain text renderer."""
from __future__ import annotations

from canvas_conductor.utils.html_text import html_to_text, word_count


# The exact wrapper BYU injects into every discussion entry body.
INJECTED = (
    '<link rel="stylesheet" href="https://example.s3.amazonaws.com/dp_app.css">'
    "{body}"
    '<script src="https://example.s3.amazonaws.com/dp_app.js"></script>'
)


def test_paragraphs_are_separated_by_a_blank_line():
    assert html_to_text("<p>Hello there</p><p>Second</p>") == "Hello there\n\nSecond"


def test_inline_tags_do_not_split_words():
    assert html_to_text("<p>Say <em>some</em>thing</p>") == "Say something"


def test_injected_link_and_script_are_dropped_entirely():
    # A `<link>` has no end tag: a naive skip-depth counter would swallow
    # the rest of the document, and a naive tag strip would leave the URL
    # in the text and inflate the word count.
    html = INJECTED.format(body="<p>Top level one.</p>")
    assert html_to_text(html) == "Top level one."
    assert word_count(html_to_text(html)) == 3


def test_script_body_is_not_treated_as_prose():
    html = "<p>Hi</p><script>var x = 'not prose at all';</script>"
    assert html_to_text(html) == "Hi"


def test_entities_and_nbsp_are_unescaped():
    assert html_to_text("<p>Tom&#39;s &amp; Jerry&nbsp;show</p>") == "Tom's & Jerry show"


def test_breaks_and_list_items():
    # `<br>` and `<li>` are worth one newline; the paragraph/list
    # boundary between them is worth a blank line.
    html = "<p>One<br>Two</p><ul><li>a</li><li>b</li></ul>"
    assert html_to_text(html) == "One\nTwo\n\na\nb"


def test_nested_block_boundaries_collapse_to_one_blank_line():
    # </p></div> then <div><p> is four boundaries meeting at one point.
    html = "<div><p>A</p></div>\n\n\n<div><p>B</p></div>"
    assert html_to_text(html) == "A\n\nB"


def test_plain_text_body_survives_unchanged():
    body = "Top level two, plain text.\n\nSecond paragraph with 'quotes'."
    assert html_to_text(INJECTED.format(body=body)) == body


def test_empty_and_none():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""
    assert word_count(None) == 0


def test_unclosed_tags_do_not_lose_text():
    assert html_to_text("<p>Start <b>bold <p>next") == "Start bold\n\nnext"


def test_self_closing_break_and_link():
    # XHTML-style tags arrive through handle_startendtag, not starttag.
    assert html_to_text('<link href="x"/><p>One<br/>Two</p>') == "One\nTwo"


def test_end_tag_of_a_skipped_container_reopens_text():
    # If `</style>` failed to close the skip region, "after" would vanish.
    assert html_to_text("<style>.a{}</style><p>after</p>") == "after"


def test_headings_and_blockquotes_break_blocks():
    html = "<h2>Title</h2><blockquote>Quoted</blockquote><p>Body</p>"
    assert html_to_text(html) == "Title\n\nQuoted\n\nBody"


def test_table_cells_are_one_line_each():
    html = "<table><tr><td>a</td><td>b</td></tr></table>"
    assert html_to_text(html) == "a\nb"


def test_leading_block_does_not_start_with_a_blank_line():
    assert html_to_text("<p>First</p>") == "First"
