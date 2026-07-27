"""MCP standing page (2026-07-27) — pins "a link that says verify must verify".

The page is our public answer to "where does DC Hub's MCP live and how does it
rank". It carried two rows labelled '✅ listed' whose links went to pages that do
not contain the string "dchub" at all: the official registry's own SOURCE REPO,
and the bare github.com/mcp index. The listings were real, but a reader who
clicked to check found nothing — which is the same failure as reporting an
unreadable listing healthy, pointed at the public instead of at the board.

These tests pin: the known-bad URLs cannot come back, a checkmark is only shown
with a date behind it, and the public page never makes an outbound request.

CI-SAFETY: no network, no DB.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def ms():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import mcp_standing as m
    return m


# ── the bug: "verify →" pointing at a page that never mentions us ────

def test_no_unverifiable_registry_links(ms):
    """Both of these resolve 200 but contain no DC Hub identity, so they cannot
    substantiate the 'listed' claim sitting next to them."""
    bad = {
        "https://github.com/modelcontextprotocol/registry",  # the registry's own source
        "https://github.com/mcp",                            # the unfiltered index
    }
    for r in ms.CONFIRMED_REGISTRIES:
        assert r["url"] not in bad, r["registry"]


def test_registry_links_are_deep_enough_to_check(ms):
    """A listing URL must identify DC Hub — by slug or by an explicit query —
    otherwise the reader lands on a generic index and cannot confirm anything."""
    for r in ms.CONFIRMED_REGISTRIES:
        u = r["url"].lower()
        assert "dchub" in u or "dc-hub" in u, r["registry"]


def test_the_registries_the_user_asked_for_are_present(ms):
    names = {r["registry"] for r in ms.CONFIRMED_REGISTRIES}
    for expected in ("Smithery", "Glama", "Official MCP Registry", "Source (GitHub)"):
        assert expected in names, expected


# ── a checkmark must have a date behind it ───────────────────────────

def test_checkmark_requires_a_verification_date(ms):
    src = _read(os.path.join("routes", "mcp_standing.py"))
    i = src.index("reg_rows = ")
    cell = src[i:i + 400]
    # the status cell must be conditioned on verified_at, not on the hardcoded
    # `listed` flag that made every row read '✅ listed' unconditionally.
    assert "verified_at" in cell
    assert "'✅ listed'" not in cell


def test_only_verified_verdicts_earn_a_date(ms):
    """broken / unverified / NULL must fall through to a plain 'listed' — we
    state what we checked and stay silent about what we could not."""
    src = _read(os.path.join("routes", "mcp_standing.py"))
    i = src.index("def _verified_map")
    body = src[i:src.index("def _registries_live")]
    assert 'startswith("verified")' in body


def test_unreachable_db_degrades_to_listed_not_to_a_crash(ms):
    """A public page must not 500 because the crawler table moved."""
    out = ms._registries_live()
    assert len(out) == len(ms.CONFIRMED_REGISTRIES)
    for r in out:
        assert r["listed"] is True
        assert "verified_at" in r          # present, may be None
        assert "tools" in r


# ── the public page must not make outbound requests ──────────────────

def test_verified_map_is_pure_db(ms):
    """No self-request from a public page — the 2026-07-06 flywheel outage."""
    src = _read(os.path.join("routes", "mcp_standing.py"))
    i = src.index("def _verified_map")
    body = src[i:src.index("def _registries_live")]
    for banned in ("urllib", "requests.", "urlopen", "http://", "https://"):
        assert banned not in body, banned


def test_schema_is_introspected_before_use(ms):
    """registry_truth ALTERs this table, and live schema has diverged from the
    repo DDL before — so the columns are checked, not assumed."""
    src = _read(os.path.join("routes", "mcp_standing.py"))
    i = src.index("def _verified_map")
    body = src[i:src.index("def _registries_live")]
    assert "information_schema.columns" in body


# ── the page answers "where does it live" ────────────────────────────

def test_page_states_the_endpoint_and_source(ms):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ms.mcp_standing_bp)
    html = app.test_client().get("/mcp-standing").get_data(as_text=True)
    assert "Where the MCP server lives" in html
    assert "dchub.cloud/mcp" in html
    assert "github.com/azmartone67/dchub-mcp-server" in html
    assert "/connect#start" in html


def test_page_and_json_agree_on_the_registry_set(ms):
    from flask import Flask
    import json as J
    app = Flask(__name__)
    app.register_blueprint(ms.mcp_standing_bp)
    c = app.test_client()
    d = J.loads(c.get("/api/v1/mcp/standing").get_data(as_text=True))
    html = c.get("/mcp-standing").get_data(as_text=True)
    assert d["registries_count"] == len(ms.CONFIRMED_REGISTRIES)
    for r in d["registries"]:
        assert r["url"] in html, r["registry"]


# ── the root cause: a corrected seed URL must reach an existing row ──

def test_seed_refreshes_a_corrected_listing_url():
    """Was ON CONFLICT DO NOTHING, so fixing a listing_url in the seed changed
    nothing live — the reason the Glama URL stayed stale through repeated
    'fixes' while the seed already carried the right one."""
    src = _read(os.path.join("routes", "mcp_presence_crawler.py"))
    i = src.index("INSERT INTO mcp_presence_listings")
    stmt = src[i:i + 700]
    assert "ON CONFLICT (registry_name) DO UPDATE" in stmt
    assert "listing_url = EXCLUDED.listing_url" in stmt


def test_glama_seed_points_at_a_real_listing_not_the_search_redirect():
    """https://glama.ai/mcp/servers/dchub 302s to /mcp/servers?query=author%3A…
    — a SEARCH page. Reading a tool count off it is how a non-listing became a
    'real' measurement."""
    src = _read(os.path.join("routes", "mcp_presence_crawler.py"))
    i = src.index('"registry_name": "glama"')
    block = src[i:i + 500]
    url = re.search(r'"listing_url":\s*"([^"]+)"', block).group(1)
    assert url.rstrip("/") != "https://glama.ai/mcp/servers/dchub"
    assert "dchub" in url.lower() or "dc-hub" in url.lower()


def test_tool_count_is_gated_on_the_same_evidence_as_the_checkmark(ms):
    """Live briefly showed 'Official MCP Registry — 30 tools'. That 30 is a parse
    artifact off a JSON API response, not a count that registry publishes about
    us. A number a reader reads as fact must clear the bar of the claim beside
    it, so an unverified row shows neither a date nor a count."""
    src = _read(os.path.join("routes", "mcp_standing.py"))
    i = src.index("def _verified_map")
    body = src[i:src.index("def _registries_live")]
    assert '"tools": int(tools) if (tools and verified) else None' in body


def test_a_lagging_listing_still_shows_when_it_was_checked(ms):
    """verified_drift leaves truth_ok_at NULL but WAS verified — dating it from
    the check keeps lag distinguishable from 'never looked'."""
    src = _read(os.path.join("routes", "mcp_standing.py"))
    i = src.index("def _verified_map")
    body = src[i:src.index("def _registries_live")]
    assert "COALESCE(truth_ok_at, truth_checked_at)" in body
