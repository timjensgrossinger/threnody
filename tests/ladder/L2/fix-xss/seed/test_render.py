"""Exploit-style grader: feeds real XSS payloads and asserts they stay inert.

The check is deliberately structural rather than substring-based. After a correct
fix the payload survives as escaped TEXT, so the literal word "onerror" is still
present in the output and is harmless -- asserting on substrings would reject a
correct solution. What must be true is that no attacker-controlled ELEMENT or
ATTRIBUTE exists, so the output is parsed and its tag/attribute set compared with
the template's own.

Also asserts ordinary text still renders, so a "fix" that strips or rejects input,
or deletes the feature, fails just as hard as no fix at all.
"""
from html.parser import HTMLParser

import pytest

from render import render_comment, render_feed

PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    '"><script>alert(1)</script>',
    "<iframe src=javascript:alert(1)></iframe>",
    "</p></li><script>alert(1)</script>",
    "<a href='javascript:alert(1)'>x</a>",
    "<body onpageshow=alert(1)>",
]

# Exactly the elements the template itself emits.
COMMENT_TAGS = {"li", "span", "p"}
FEED_TAGS = COMMENT_TAGS | {"ul"}


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(name.lower() for name, _ in attrs)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


def parse(markup):
    p = _Collect()
    p.feed(markup)
    p.close()
    return p


@pytest.mark.parametrize("payload", PAYLOADS)
def test_payload_creates_no_element_in_body(payload):
    parsed = parse(render_comment("alice", payload))
    injected = set(parsed.tags) - COMMENT_TAGS
    assert not injected, f"payload created live element(s): {sorted(injected)}"


@pytest.mark.parametrize("payload", PAYLOADS)
def test_payload_creates_no_element_in_author(payload):
    parsed = parse(render_comment(payload, "hello"))
    injected = set(parsed.tags) - COMMENT_TAGS
    assert not injected, f"payload created live element(s): {sorted(injected)}"


@pytest.mark.parametrize("payload", PAYLOADS)
def test_payload_creates_no_event_handler_attribute(payload):
    for markup in (render_comment("alice", payload), render_comment(payload, "hi")):
        parsed = parse(markup)
        handlers = [a for a in parsed.attrs if a.startswith("on")]
        assert not handlers, f"payload created handler attribute(s): {handlers}"


def test_attribute_break_out_creates_no_attribute():
    parsed = parse(render_comment('a" onmouseover="alert(1)', "body"))
    assert "onmouseover" not in parsed.attrs
    assert set(parsed.tags) <= COMMENT_TAGS


def test_payload_text_is_preserved_as_text():
    # Escaping, not stripping: the payload must still be readable to a human.
    parsed = parse(render_comment("alice", "<script>alert(1)</script>"))
    joined = "".join(parsed.text)
    assert "alert(1)" in joined, "payload was stripped rather than escaped"


def test_structure_is_preserved():
    out = render_comment("alice", "hello")
    assert '<li class="comment">' in out
    assert '<span class="author">' in out
    assert '<p class="body">' in out
    assert out.endswith("</li>")


def test_ordinary_text_still_renders():
    out = render_comment("Ana Lopez", "It's a café - great coffee & pastries!")
    parsed = parse(out)
    joined = "".join(parsed.text)
    assert "Ana Lopez" in joined
    assert "café" in joined
    assert "great coffee & pastries!" in joined
    assert set(parsed.tags) <= COMMENT_TAGS


def test_feed_wraps_items():
    out = render_feed([("alice", "one"), ("bob", "two")])
    assert out.startswith('<ul class="feed">')
    assert out.endswith("</ul>")
    assert out.count('<li class="comment">') == 2
    joined = "".join(parse(out).text)
    assert "one" in joined and "two" in joined


def test_feed_escapes_payloads():
    parsed = parse(render_feed([("alice", "<script>alert(1)</script>")]))
    assert set(parsed.tags) <= FEED_TAGS
