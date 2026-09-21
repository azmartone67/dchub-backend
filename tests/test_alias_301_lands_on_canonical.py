"""A legacy-alias 301 lands on the page's DECLARED canonical, in one hop.

Measured live 2026-09-21 (Copilot cited the first URL 8 times, Bing AI
Performance 2026-03-19..09-16):

  /facilities/equinix-inc-equinix-am11-amsterdam-lemelerbergweg-5e2e2b38.html
    301 -> /facilities/equinix-inc-equinix-am11-amsterdam-lemelerbergweg-e78fce36
    200, <link rel=canonical> = .../equinix-equinix-am11-amsterdam-lemelerbergweg-e7d19e28

One hop, onto a page that names another URL as canonical. The route is driven
for real here; only the DB lookups are stubbed.
"""
import ast
import os

import routes.facility_profile_page as fpp
import routes.facility_slug_freeze as fsf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = "equinix-inc-equinix-am11-amsterdam-lemelerbergweg-5e2e2b38"
ALIAS = "equinix-inc-equinix-am11-amsterdam-lemelerbergweg-e78fce36"
KEEPER = "equinix-equinix-am11-amsterdam-lemelerbergweg-e7d19e28"
LEGACY_ROW = {"id": "legacy-1", "_src_table": "facilities", "canonical_slug": ALIAS}


def _wire(monkeypatch, facs, drained=None, pointer=None, dup_twin=None, hop=None,
          fetch_raises=False):
    def fetch(s):
        if fetch_raises:
            raise RuntimeError("db down")
        return facs.get(s)
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", fetch)
    monkeypatch.setattr(fsf, "resolve_alias",
                        lambda s: {OLD: ALIAS}.get(s[:-5] if s.endswith(".html") else s))
    monkeypatch.setattr(fpp, "_drained_twin_url", lambda i: drained)
    monkeypatch.setattr(fpp, "_twin_pointer_url", lambda i: pointer)
    monkeypatch.setattr(fpp, "_canonical_twin_url", lambda i: dup_twin)
    monkeypatch.setattr(fpp, "_twin_redirect_target", lambda f, s, keeper_row=None: hop)


def _location(resp):
    assert resp.status_code == 301, resp.status_code
    return resp.headers["Location"]


def test_am11_shape_lands_on_the_declared_canonical(monkeypatch):
    _wire(monkeypatch, {ALIAS: dict(LEGACY_ROW)},
          drained="https://dchub.cloud/facilities/" + KEEPER)
    assert _location(fpp.render_facility_profile(OLD + ".html")) == "/facilities/" + KEEPER


def test_a_self_canonical_alias_target_is_unchanged(monkeypatch):
    _wire(monkeypatch, {ALIAS: dict(LEGACY_ROW)})
    assert _location(fpp.render_facility_profile(OLD + ".html")) == "/facilities/" + ALIAS


def test_a_twin_301_target_is_followed_so_there_is_no_second_hop(monkeypatch):
    _wire(monkeypatch, {ALIAS: dict(LEGACY_ROW)}, hop=KEEPER,
          drained="https://dchub.cloud/facilities/should-not-win-00000000")
    assert _location(fpp.render_facility_profile(OLD)) == "/facilities/" + KEEPER


def test_duplicate_row_lands_on_its_keeper(monkeypatch):
    row = {"id": 7, "duplicate_of_id": 9, "canonical_slug": ALIAS}
    _wire(monkeypatch, {ALIAS: row}, dup_twin="https://dchub.cloud/facilities/" + KEEPER)
    assert _location(fpp.render_facility_profile(OLD)) == "/facilities/" + KEEPER


def test_fail_soft_to_the_stored_alias(monkeypatch):
    _wire(monkeypatch, {}, drained="https://dchub.cloud/facilities/" + KEEPER)
    assert _location(fpp.render_facility_profile(OLD)) == "/facilities/" + ALIAS
    _wire(monkeypatch, {ALIAS: dict(LEGACY_ROW)}, fetch_raises=True)
    # the requested slug misses too (fetch raises for it first) -> same fallback path
    assert fpp._alias_landing_slug(ALIAS, OLD) == ALIAS


def test_never_lands_on_the_requested_slug(monkeypatch):
    _wire(monkeypatch, {ALIAS: dict(LEGACY_ROW)},
          drained="https://dchub.cloud/facilities/" + OLD)
    assert fpp._alias_landing_slug(ALIAS, OLD) == ALIAS


def test_twin_canonical_arms_keep_their_order(monkeypatch):
    """Moved verbatim out of _render_profile: dup pointer first; for a
    legacy-table row the drain link beats the inferred twin pointer."""
    _wire(monkeypatch, {}, drained="D", pointer="P", dup_twin="K")
    assert fpp._twin_canonical_url({"duplicate_of_id": 1, "_src_table": "facilities"}) == "K"
    assert fpp._twin_canonical_url({"id": "x", "_src_table": "facilities"}) == "D"
    _wire(monkeypatch, {}, drained=None, pointer="P")
    assert fpp._twin_canonical_url({"id": "x", "_src_table": "facilities"}) == "P"
    assert fpp._twin_canonical_url({"id": 3, "_src_table": "discovered_facilities"}) is None


def test_the_page_canonical_reads_the_same_helper():
    src = open(os.path.join(ROOT, "routes", "facility_profile_page.py")).read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_render_profile")
    called = {getattr(n.func, "id", None) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "_twin_canonical_url" in called, "_render_profile no longer reads _twin_canonical_url"
    assert "_drained_twin_url" not in called, "a second copy of the canonical arms is back in _render_profile"
