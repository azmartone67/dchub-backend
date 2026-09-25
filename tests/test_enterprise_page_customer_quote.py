"""/enterprise renders the approved customer quote, attributed to a person.

The public /enterprise page is THIS template (routes/enterprise.py), not the
frontend repo's enterprise.html, which the worker never serves for that path.
A quote added to the frontend file alone ships nowhere, so the page itself is
the thing to check.

★2026-09-24: the quote is no longer hard-coded here. It is rendered from
util/customer_testimonials, which reads the one shared source
(https://dchub.cloud/testimonials.json). These tests stub that loader — the
suite never reaches the network — and pin the three behaviours that matter:
the quote renders with the same markup, an empty source omits the figure
without breaking the page, and every field is HTML-escaped.
"""
import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

from routes.enterprise import enterprise_bp  # noqa: E402
from util import customer_testimonials as ct  # noqa: E402

RICH = {
    "name": "Rich Bray",
    "title": "Development Manager",
    "company": "LPI Group",
    "quote": (
        "DC Hub is now integral to how we evaluate every site. For so long, "
        "we’ve had to piecemeal information from multiple sources. DC Hub "
        "has it all in one place and provides the clearest picture of "
        "infrastructure that we’ve found."
    ),
    "featured": True,
}


def _get(monkeypatch, rows):
    monkeypatch.setattr(ct, "get_customer_testimonials", lambda: [dict(r) for r in rows])
    app = Flask(__name__)
    app.register_blueprint(enterprise_bp)
    resp = app.test_client().get("/enterprise")
    return resp.status_code, resp.get_data(as_text=True)


def test_enterprise_page_shows_named_customer_quote(monkeypatch):
    status, body = _get(monkeypatch, [RICH])
    assert status == 200
    assert 'class="cq"' in body
    assert "DC Hub is now integral to how we evaluate every site." in body
    assert '<span class="cq-name">Rich Bray</span>' in body
    assert '<span class="cq-role">Development Manager, LPI Group</span>' in body
    assert '<span class="cq-avatar" aria-hidden="true">RB</span>' in body
    assert 'class="cq-more" href="/testimonials"' in body
    # The template placeholders are still filled after the insert.
    assert "{{" not in body


def test_featured_entry_wins_over_first(monkeypatch):
    other = dict(RICH, name="Someone Else", quote="Another quote.", featured=False)
    status, body = _get(monkeypatch, [other, RICH])
    assert status == 200
    assert "Rich Bray" in body and "Someone Else" not in body


def test_empty_source_omits_the_figure_and_page_still_renders(monkeypatch):
    status, body = _get(monkeypatch, [])
    assert status == 200
    assert 'class="cq"' not in body
    assert "{{" not in body
    # The rest of the page is intact.
    assert "Request enterprise access" in body


def test_every_field_is_html_escaped(monkeypatch):
    evil = dict(
        RICH,
        name="<b>Eve</b>",
        title="Dev & Ops",
        company='"Acme"',
        quote="<script>alert(1)</script>",
    )
    status, body = _get(monkeypatch, [evil])
    assert status == 200
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<b>Eve</b>" not in body
    assert "&lt;b&gt;Eve&lt;/b&gt;" in body
    assert "Dev &amp; Ops, &quot;Acme&quot;" in body
