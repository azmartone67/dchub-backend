"""The tier2-export purge must target the leaking URL and nothing a caller names.

2026-09-06. #4038 gated export_facility_csv at the origin (401 anon), but the
eyeball cache kept serving the pre-gate body: 10/10 un-cache-busted probes
returned HTTP 200 / cf-cache-status: HIT / 1,003,630 bytes with age past 1,900s.
The worker fix (dchub-frontend#1409) stops RE-caching but cannot evict a HIT,
because nothing in the worker runs on one. This route is the eviction.

It is public on purpose — requiring a secret to close a live data leak is what
kept the leak open for the 40 minutes it took to find the key. That is only safe
while the URL list stays DERIVED. These tests are the fence on that.
"""
import ast
import os
import sys

import pytest
from flask import Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

SRC = open(os.path.join(REPO, "routes/cf_purge.py")).read()
TREE = ast.parse(SRC)


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in routes/cf_purge.py")


def test_purge_route_takes_no_caller_supplied_urls():
    """A public endpoint that purges whatever it is handed lets anyone evict any
    path on the zone — cheap origin-load amplification. Same constraint the
    og-cards purge documents."""
    fn = _fn("purge_tier2_export")
    reads = [
        n.attr for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr in
        ("args", "json", "form", "values", "data", "get_json")
    ]
    assert not reads, (
        f"purge_tier2_export reads caller input {sorted(set(reads))}; it is "
        f"PUBLIC (no admin key), so a caller-supplied URL would let anyone "
        f"purge any path on the zone"
    )


def test_purge_covers_the_url_measured_leaking():
    """?limit=10000 on dchub.cloud is the exact URL that served 1,003,630 bytes
    to an anonymous caller after the gate shipped. If it is not in the list the
    route is decorative."""
    import routes.cf_purge as m
    app = Flask(__name__)
    app.register_blueprint(m.cf_purge_bp)
    sent = {}

    def _fake(urls):
        sent.setdefault("urls", []).extend(urls)
        return {"ok": True, "purged": urls}

    m_orig = m._purge_urls
    m._purge_urls = _fake
    try:
        with app.test_client() as c:
            r = c.get("/api/v1/cf/purge/tier2-export")
        assert r.status_code == 200
    finally:
        m._purge_urls = m_orig

    urls = sent.get("urls", [])
    assert ("https://dchub.cloud/api/v1/mcp/tools/export_facility_csv?limit=10000"
            in urls), "the measured-leaking URL is not purged"
    assert ("https://dchub.cloud/api/v1/mcp/tools/create_site_report"
            in urls), "the sibling tool is not purged"
    # api.dchub.cloud is a separate edge cache — it answered 401 on 09-06, but
    # download_url in create_site_report points there, so it can be primed.
    assert any(u.startswith("https://api.dchub.cloud/") for u in urls), (
        "api.dchub.cloud is a distinct cache and is not covered"
    )


def test_purge_batches_within_the_cf_thirty_file_limit(monkeypatch):
    """CF purge-by-file takes 30 files per request; a 31st is rejected and that
    URL keeps serving.

    Driven with 100 synthetic URLs, NOT through the route. The route derives
    ~24 URLs, so it never produces a second batch and an assertion on it passes
    identically whether the chunk size is 30 or 300 — mutation-tested, and the
    route-level version of this test did not catch the 300 mutant.
    """
    import routes.cf_purge as m
    assert m._CF_PURGE_MAX_FILES <= 30, (
        f"chunk size {m._CF_PURGE_MAX_FILES} exceeds CF's per-request file limit"
    )
    seen = []
    monkeypatch.setattr(m, "_purge_urls",
                        lambda urls: seen.append(list(urls)) or {"ok": True})
    fake = [f"https://dchub.cloud/x/{i}" for i in range(100)]
    m._purge_in_batches(fake)
    assert seen, "no purge issued"
    assert max(len(b) for b in seen) <= 30, (
        f"a batch exceeded CF's 30-file limit: {[len(b) for b in seen]}"
    )
    assert sum(len(b) for b in seen) == 100, "batching dropped or duplicated URLs"


def test_admin_gated_purge_still_requires_the_key():
    """Adding a public sibling must not have relaxed the arbitrary-URL route."""
    fn = _fn("purge_endpoint")
    src = ast.get_source_segment(SRC, fn) or ""
    assert "X-Admin-Key" in src, (
        "the caller-supplied-URL purge no longer checks X-Admin-Key"
    )
