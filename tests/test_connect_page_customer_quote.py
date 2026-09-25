"""/connect shows the featured customer quote, from the one source.

The quote comes from https://dchub.cloud/testimonials.json through
util.customer_testimonials (stubbed here; no network). connect_page() inserts it
into the <!-- CUSTOMER_QUOTE --> slot AFTER canon rendering, so a figure inside
a customer's words is never rewritten. `import main` needs a database, so the
function is lifted out of main.py and run against the real static file and the
real canon, the same harness tests/test_doors_live_vs_stale.py uses.
"""
import ast
import logging
import os
import pathlib
import types

import pytest

flask = pytest.importorskip("flask")

import ai_surface_canon  # noqa: E402
from util import customer_testimonials as ct  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

RICH = {
    "name": "Rich Bray", "title": "Development Manager", "company": "LPI Group",
    "quote": "DC Hub is now integral to how we evaluate every site.", "featured": True,
}


def _connect(monkeypatch, rows):
    monkeypatch.setattr(ct, "get_customer_testimonials", lambda: [dict(r) for r in rows])
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "connect_page")
    fn.decorator_list = []
    ns = {"os": os, "Response": flask.Response, "logger": logging.getLogger("t"),
          "send_from_directory": flask.send_from_directory,
          "_canon_text": ai_surface_canon.canon_text,
          "app": types.SimpleNamespace(static_folder=str(ROOT / "static"))}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    with flask.Flask("connect-quote").test_request_context("/connect"):
        return ns["connect_page"]().get_data(as_text=True)


def test_connect_shows_the_featured_customer_quote(monkeypatch):
    body = _connect(monkeypatch, [RICH])
    assert '<figure class="cq"' in body
    assert "DC Hub is now integral to how we evaluate every site." in body
    assert '<span class="cq-name">Rich Bray</span>' in body
    assert "Development Manager, LPI Group" in body
    assert 'href="/testimonials"' in body
    assert "<!-- CUSTOMER_QUOTE -->" not in body


def test_quote_sits_before_the_live_vs_stale_policy(monkeypatch):
    body = _connect(monkeypatch, [RICH])
    assert body.index('<figure class="cq"') < body.index('id="live-vs-stale"')


def test_empty_source_drops_the_slot_and_the_page_still_renders(monkeypatch):
    body = _connect(monkeypatch, [])
    assert '<figure class="cq"' not in body
    assert "<!-- CUSTOMER_QUOTE -->" not in body
    assert 'id="live-vs-stale"' in body


def test_every_field_is_html_escaped(monkeypatch):
    evil = dict(RICH, name='<b>X</b>', quote='<script>alert(1)</script> & "q"')
    body = _connect(monkeypatch, [evil])
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;" in body
    assert "&lt;b&gt;X&lt;/b&gt;" in body


def test_quote_text_is_never_run_through_canon(monkeypatch):
    # The slot is filled AFTER canon rendering, so a customer's words are never
    # interpreted: a literal canon placeholder in a quote must come through
    # verbatim, not as the canon value. (Canon only substitutes {canon_*}
    # tokens, so a plain number would not prove the ordering; this does.)
    words = dict(RICH, quote="It lists {canon_tools} tools, as they say.")
    body = _connect(monkeypatch, [words])
    assert "It lists {canon_tools} tools, as they say." in body
