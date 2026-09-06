"""The admin notification for /api/v1/enterprise/contact must not be injectable.

Every field it renders is PUBLIC form input. Until 2026-09-05 all six were
interpolated raw, so a stranger could put markup — or an event handler — into
the owner's inbox. The `email` field was the sharpest: it sat inside a
SINGLE-QUOTED href, so one apostrophe escaped the attribute.

These payloads are the ones that actually worked against the old code.
"""
import json
import re

import pytest

import routes.enterprise_inquiry as ei


HOSTILE = {
    "firm": "<script>alert(1)</script>Acme",
    "tier_requested": "<img src=x onerror=alert(2)>",
    "name": "</h2><b>injected</b>",
    # the attribute break: an apostrophe closes href='...' and adds a handler
    "email": "a' onmouseover='alert(3)",
    "use_case": "<svg/onload=alert(4)>",
    "notes": "line1\n<iframe src=javascript:alert(5)></iframe>",
}


def _capture(monkeypatch):
    """Run _notify_admin and return the JSON body it would have POSTed."""
    sent = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=None):
        sent["payload"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("ADMIN_INBOX_EMAIL", "owner@example.com")
    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", fake_urlopen)
    ei._notify_admin(dict(HOSTILE))
    assert "payload" in sent, "_notify_admin sent nothing — the test proves nothing"
    return sent["payload"]


def _parse(html):
    """Return (tag names present, list of (tag, attrs)) by ACTUALLY parsing.

    Substring checks are the wrong tool here and I got them wrong first:
    "onerror=" appears in the SAFE output too, inside `&lt;img src=x
    onerror=...&gt;` — escaped text, inert. What matters is whether a TAG
    exists that the template did not write. So parse, don't grep.
    """
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags, self.pairs = [], []
        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            self.pairs.append((tag, dict(attrs)))
        handle_startendtag = handle_starttag

    p = P(); p.feed(html)
    return p.tags, p.pairs


# The only tags this template writes itself. Anything else came from input.
TEMPLATE_TAGS = {"h2", "p", "b", "br", "a"}


def test_no_tag_the_template_did_not_write_survives(monkeypatch):
    html = _capture(monkeypatch)["html"]
    tags, _ = _parse(html)
    intruders = sorted(set(tags) - TEMPLATE_TAGS)
    assert not intruders, (
        f"attacker-controlled tag(s) rendered in the admin email: {intruders}")


def test_no_element_carries_an_event_handler(monkeypatch):
    html = _capture(monkeypatch)["html"]
    _, pairs = _parse(html)
    bad = [(t, k) for t, attrs in pairs for k in attrs if k.lower().startswith("on")]
    assert not bad, f"event handler(s) rendered as real attributes: {bad}"


def test_the_payload_is_escaped_not_dropped(monkeypatch):
    """Escaping must not silently eat the report — the owner still needs to read it."""
    html = _capture(monkeypatch)["html"]
    assert "&lt;script&gt;" in html and "Acme" in html


def test_the_mailto_attribute_cannot_be_broken_out_of(monkeypatch):
    """The original bug: href='mailto:{email}' in a SINGLE-quoted attribute,
    so one apostrophe in the email escaped into attribute context."""
    html = _capture(monkeypatch)["html"]
    _, pairs = _parse(html)
    anchors = [a for t, a in pairs if t == "a"]
    assert anchors, f"the mailto anchor vanished: {html[:300]}"
    href = anchors[0].get("href", "")
    assert href.startswith("mailto:"), f"unexpected href: {href!r}"
    # The parser resolved the attribute — so the quote did NOT break out.
    # What remains must be percent-encoded, never a live quote or '='.
    assert "'" not in href and '"' not in href, f"raw quote in href: {href!r}"
    assert "=" not in href, f"raw '=' in href — a handler could form: {href!r}"
    assert len(anchors) == 1, f"input created an extra anchor: {anchors}"
    # ★ The assertion that makes THIS test catch the original bug on its own.
    #   Against the vulnerable code the parser happily resolved href='mailto:a'
    #   and split the rest off as a SEPARATE onmouseover attribute — so every
    #   check above still passed while the anchor was fully compromised. The
    #   anchor must carry exactly one attribute, and it must be href.
    assert list(anchors[0].keys()) == ["href"], (
        f"input added attribute(s) to the anchor: {anchors[0]}")


def test_headers_cannot_be_injected_via_newlines(monkeypatch):
    payload = _capture(monkeypatch)
    for field in ("subject", "reply_to"):
        v = payload[field]
        assert "\n" not in v and "\r" not in v, f"CR/LF survived into {field}: {v!r}"


def test_newlines_in_notes_still_render_as_line_breaks(monkeypatch):
    """Escaping must not cost the feature: notes keep their <br> formatting."""
    html = _capture(monkeypatch)["html"]
    assert "line1<br>" in html, "notes lost their line-break rendering"


def test_a_missing_api_key_sends_nothing(monkeypatch):
    """Guard the guard: with no key the function must return before sending."""
    calls = []
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", lambda *a, **k: calls.append(1))
    ei._notify_admin(dict(HOSTILE))
    assert not calls
