"""/api/v1/cf/purge/market-pages must evict EVERY published market page.

2026-09-17. be#4694 put the live ladder (Pro $99/mo, $10 once) on every
/markets/<slug> painter, replacing a stale "19,000+ / $49/mo" block. The origin
was correct in minutes; /markets/ashburn served the DEAD offer for two hours
after the merge — cf-cache-status HIT, age 7276, three consecutive reads with
age climbing and no revalidation. Market HTML carries
stale-while-revalidate=86400, so that copy had ~22h left to run.

Nothing could purge it. cf-purge-changed.mjs declares backend-served surfaces
out of scope; the backend deploy has no CF step; and purge/markets-fix covers
the HUB (/markets, /market-intelligence), not the detail pages, despite its
name. These tests are the fence on the route that closes that.

The chunking assertions exist because of the note already in cf_purge.py: with
only ~24 derived URLs a route never produces a second batch, so asserting on
the route alone "cannot tell 30 from 300". Here the slug source is STUBBED long
enough to actually chunk.
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


def _app():
    import routes.cf_purge as m
    app = Flask(__name__)
    app.register_blueprint(m.cf_purge_bp)
    return app, m


class _Recorder:
    """Stands in for _purge_urls. One call == one CF request == one batch."""

    def __init__(self):
        self.calls = []

    def __call__(self, urls):
        self.calls.append(list(urls))
        return {"ok": True, "purged": list(urls)}

    @property
    def flat(self):
        return [u for c in self.calls for u in c]


def _drive(slugs, path="/api/v1/cf/purge/market-pages"):
    """Call the route with sitemapped_market_slugs() stubbed to `slugs`."""
    import routes.market_deep_dive as mdd
    app, m = _app()
    rec = _Recorder()
    o_purge, o_slugs = m._purge_urls, mdd.sitemapped_market_slugs
    m._purge_urls = rec
    mdd.sitemapped_market_slugs = lambda *a, **k: list(slugs)
    try:
        with app.test_client() as c:
            r = c.get(path)
    finally:
        m._purge_urls = o_purge
        mdd.sitemapped_market_slugs = o_slugs
    return r, rec


def test_market_pages_takes_no_caller_supplied_urls():
    """PUBLIC route. If it purges what it is handed, anyone can evict any path
    on the zone — the constraint the sibling one-shots document."""
    fn = _fn("purge_market_pages")
    reads = [
        n.attr for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr in
        ("args", "json", "form", "values", "data", "get_json")
    ]
    assert not reads, (
        f"purge_market_pages reads caller input {sorted(set(reads))}; it is "
        f"PUBLIC, so a caller-supplied URL is a zone-eviction primitive"
    )


def test_every_sitemapped_slug_is_purged_exactly_once():
    """A fixed slice or an off-by-one drops the TAIL of the slug list, and the
    dropped pages are the ones that keep serving. 70 slugs > 2 batches."""
    slugs = [f"m{i:03d}" for i in range(70)]
    r, rec = _drive(slugs)
    assert r.status_code == 200
    got = rec.flat
    want = [f"https://dchub.cloud/markets/{s}" for s in slugs]
    assert got == want, (
        f"purged {len(got)} URLs, expected {len(want)}; "
        f"missing={sorted(set(want) - set(got))[:5]} "
        f"unexpected={sorted(set(got) - set(want))[:5]}"
    )
    assert r.get_json()["url_count"] == 70


def test_no_batch_exceeds_the_cf_cap():
    """CF rejects the whole request at 31 files, leaving every URL in it
    serving. This is the assertion that tells 30 from 300."""
    import routes.cf_purge as m
    slugs = [f"m{i:03d}" for i in range(70)]
    r, rec = _drive(slugs)
    sizes = [len(c) for c in rec.calls]
    assert sizes, "route made no CF request at all"
    assert max(sizes) <= m._CF_PURGE_MAX_FILES, (
        f"a batch carried {max(sizes)} files; CF caps at "
        f"{m._CF_PURGE_MAX_FILES} and rejects the whole call above it "
        f"(batch sizes {sizes})"
    )
    assert len(sizes) == 3, f"70 slugs at a cap of 30 is 3 batches, got {sizes}"
    assert r.get_json()["batches"] == 3


def test_ashburn_the_page_measured_stale_is_covered():
    """The page that was actually serving the dead $49/mo offer. If it is not
    in the set the route is decorative."""
    r, rec = _drive(["ashburn", "dallas"])
    assert "https://dchub.cloud/markets/ashburn" in rec.flat, (
        "the page measured STALE on 2026-09-17 is not purged"
    )


def test_empty_derivation_does_not_report_a_clean_purge():
    """all([]) is True. Without an explicit guard, deriving zero slugs reports
    ok:true having purged nothing — a green light for a broken derivation."""
    r, rec = _drive([])
    body = r.get_json()
    assert rec.calls == [], "purged something from an empty slug list"
    assert body["ok"] is False, (
        f"empty derivation reported ok={body['ok']}; purging nothing must "
        f"never read as success"
    )
    assert body["url_count"] == 0


# ── the admin route's >30 list ───────────────────────────────────────────────

def _drive_admin(urls, monkeypatch):
    import internal_auth
    import routes.cf_purge as m
    app, m = _app()
    rec = _Recorder()
    o = m._purge_urls
    m._purge_urls = rec
    monkeypatch.setattr(m, "require_internal_or_admin", lambda *a, **k: True)
    try:
        with app.test_client() as c:
            r = c.post("/api/v1/cf/purge", json={"urls": urls})
    finally:
        m._purge_urls = o
    return r, rec


def test_admin_purge_batches_above_the_cf_cap(monkeypatch):
    """253 market URLs in one call is what a hand purge actually needs. Handed
    to CF unchunked it is rejected outright and every URL keeps serving."""
    import routes.cf_purge as m
    urls = [f"https://dchub.cloud/markets/m{i:03d}" for i in range(31)]
    r, rec = _drive_admin(urls, monkeypatch)
    assert r.status_code == 200
    sizes = [len(c) for c in rec.calls]
    assert max(sizes) <= m._CF_PURGE_MAX_FILES, (
        f"admin route sent {max(sizes)} files in one CF call; CF rejects >"
        f"{m._CF_PURGE_MAX_FILES}"
    )
    assert rec.flat == urls, "admin route dropped or reordered caller URLs"
    assert r.get_json()["batches"] == 2


def test_admin_purge_small_list_keeps_the_original_shape(monkeypatch):
    """Existing callers read the single _purge_urls dict. A <=30 list must
    still return exactly that, not the batched envelope."""
    urls = ["https://dchub.cloud/markets/ashburn"]
    r, rec = _drive_admin(urls, monkeypatch)
    body = r.get_json()
    assert "batches" not in body, (
        f"a {len(urls)}-URL purge returned the batched envelope {sorted(body)}; "
        f"that moves the response shape for every existing caller"
    )
    assert body["purged"] == urls
