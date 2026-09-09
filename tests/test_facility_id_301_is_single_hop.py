#!/usr/bin/env python3
"""/facility/<id> 301s STRAIGHT to the slug that serves 200 — one hop, not two.

NO NETWORK, NO DB (the tests/test_facility_twin_301.py pattern): both modules
are imported directly and every DB-touching helper is monkeypatched.

MEASURED LIVE 2026-09-09, dchub.cloud, cache-busted, following redirects:

    /facility/19409
      -> 301 /facilities/unknown-spectrum-charlotte-national-data-center-2336914d
      -> 301 /facilities/spectrum-charlotte-national-data-center-00f9c9ee
      -> 200

Both hops carry cf-cache-status: MISS, so this is the origin's own answer and
not a stale edge entry. Hop 1 is seo_pages.facility_page redirecting to
discovered_facilities.canonical_slug (x-dc-page-source: seo-facility-canonical-301);
hop 2 is facility_profile_page resolving that stored slug onward to the twin
keeper. The two tables disagree about which slug is canonical.

Sampled 28 ids across 18600-21400 (every 100): 23 single-hop, 5 double-hop
(18700, 19000, 19600, 20800, 20900) = 18%. A wider 29-id spread over the whole
range put every low id at one hop, so the chain concentrates in newer rows.

WHY IT MATTERS: the 301 at /facility/<id> exists to CONSOLIDATE ~22.6k thin
duplicate pages onto the canonical profile. A chain is what GSC files as
"Redirect error", and each extra hop dilutes the consolidation the redirect was
added to perform.

THE LIMIT: this pins the hop count, which is ours. Whether GSC's "Page with
redirect" bucket shrinks is measured in GSC after deploy, not here — and that
bucket is *expected* to hold these URLs; a 301 consolidation is supposed to
produce redirects. Only the CHAINS are the defect.
"""
import pathlib
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402
import routes.seo_pages as sp  # noqa: E402

STALE = "unknown-spectrum-charlotte-national-data-center-2336914d"
FINAL = "spectrum-charlotte-national-data-center-00f9c9ee"


# ── the resolver ─────────────────────────────────────────────────────────

def _chain(monkeypatch, hops: dict):
    """Wire resolve_final_slug onto an in-memory slug -> next-slug map."""
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", lambda s: {"slug": s})
    monkeypatch.setattr(fpp, "_twin_redirect_target",
                        lambda fac, s: hops.get(s))


def test_terminal_slug_is_returned_unchanged(monkeypatch):
    _chain(monkeypatch, {})
    assert fpp.resolve_final_slug(FINAL) == FINAL


def test_one_hop_resolves_to_the_target(monkeypatch):
    _chain(monkeypatch, {STALE: FINAL})
    assert fpp.resolve_final_slug(STALE) == FINAL


def test_two_hop_chain_collapses_to_the_terminal_slug(monkeypatch):
    _chain(monkeypatch, {"a-00000000": STALE, STALE: FINAL})
    assert fpp.resolve_final_slug("a-00000000") == FINAL


def test_a_cycle_returns_the_original_and_does_not_hang(monkeypatch):
    # A 301 loop satisfies every status-code check, so this is the case a
    # naive resolver turns from "extra hop" into "infinite redirect".
    _chain(monkeypatch, {"a-00000000": "b-11111111", "b-11111111": "a-00000000"})
    assert fpp.resolve_final_slug("a-00000000") == "a-00000000"


def test_chain_longer_than_max_hops_returns_the_original(monkeypatch):
    _chain(monkeypatch, {"a-00000000": "b-11111111", "b-11111111": "c-22222222",
                         "c-22222222": "d-33333333", "d-33333333": FINAL})
    assert fpp.resolve_final_slug("a-00000000", max_hops=2) == "a-00000000"


def test_a_db_failure_returns_the_original_instead_of_raising(monkeypatch):
    def _boom(_s):
        raise RuntimeError("pooler reset")
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", _boom)
    assert fpp.resolve_final_slug(STALE) == STALE


def test_an_unresolvable_slug_falls_through_alias_then_legacy(monkeypatch):
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", lambda s: None)
    monkeypatch.setattr(fpp, "_resolve_legacy_slug",
                        lambda s: FINAL if s == STALE else None)
    import routes.facility_slug_freeze as fsf
    monkeypatch.setattr(fsf, "resolve_alias", lambda s: None)
    assert fpp.resolve_final_slug(STALE) == FINAL


# ── the route: what Googlebot actually receives ──────────────────────────

class _Cur:
    def __init__(self, row): self._row = row
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    def fetchone(self): return self._row
    def fetchall(self): return []


class _Conn:
    def __init__(self, row): self._row = row
    def cursor(self, **k): return _Cur(self._row)
    def close(self): pass


def _app(monkeypatch, row):
    monkeypatch.setattr(sp, "_conn", lambda: _Conn(row))
    app = Flask(__name__)
    app.register_blueprint(sp.seo_pages_bp)
    return app.test_client()


def test_facility_id_301s_past_the_stale_stored_slug(monkeypatch):
    """THE REGRESSION. On the unfixed route this Location is STALE.

    discovered_facilities hands back canonical_slug = STALE, but STALE is not
    what /facilities/<slug> serves — it 301s onward to FINAL. Redirecting to
    the stored value therefore costs a second hop for every crawl of this id.
    """
    monkeypatch.setattr(fpp, "resolve_final_slug",
                        lambda s, **k: FINAL if s == STALE else s)
    row = {"id": 19409, "name": "Charlotte National Data Center",
           "provider": "Spectrum", "city": "Charlotte", "state": "NC",
           "canonical_slug": STALE}
    r = _app(monkeypatch, row).get("/facility/19409")
    assert r.status_code == 301
    loc = r.headers["Location"]
    assert loc.endswith("/facilities/" + FINAL), (
        f"expected a single hop to the terminal slug, got {loc!r} — a request "
        f"for that URL 301s again to {FINAL!r}")
    assert STALE not in loc


def test_a_slug_that_already_terminates_is_left_alone(monkeypatch):
    """The 23-of-28 majority case must not move."""
    monkeypatch.setattr(fpp, "resolve_final_slug", lambda s, **k: s)
    row = {"id": 3885, "name": "Switch Tahoe Reno", "provider": "Switch Ltd",
           "city": "Reno", "state": "NV",
           "canonical_slug": "switch-ltd-switch-tahoe-reno-9309b7c9"}
    r = _app(monkeypatch, row).get("/facility/3885")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(
        "/facilities/switch-ltd-switch-tahoe-reno-9309b7c9")


def test_resolver_failure_still_emits_the_stored_slug(monkeypatch):
    """Fail-safe: a broken resolver must never cost us the redirect itself."""
    def _boom(_s, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(fpp, "resolve_final_slug", _boom)
    row = {"id": 19409, "name": "Charlotte National Data Center",
           "provider": "Spectrum", "city": "Charlotte", "state": "NC",
           "canonical_slug": STALE}
    r = _app(monkeypatch, row).get("/facility/19409")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/facilities/" + STALE)
