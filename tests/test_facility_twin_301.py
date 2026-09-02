#!/usr/bin/env python3
"""A duplicate-twin /facilities/<slug> page 301s to its keeper (seo F5).

NO NETWORK, NO DB. routes.facility_profile_page is imported directly (the
tests/test_frozen_slug_canonical_select.py pattern); the DB-touching helpers
are monkeypatched.

MEASURED (2026-09-02, GSC page grain 14d vs prior 14d): the sitemap page
/facilities/equinix-equinix-fr5-frankfurt-kleyerstrasse-3366f937 fell
249 -> 5 impressions, 5 -> 0 clicks, while its twin
/facilities/equinix-equinix-fr5-ad94b281 (NOT in the sitemap, served 200 with
<link rel=canonical> -> 3366f937) fell 158 -> 3. Live rows: twin address
'Kleyerstrasse', keeper address NULL, coordinates 12 m apart. The AWS pair
(1d2f0b85 -> af54722f) has identical coordinates and no addresses. The Lumen
pair is one row reached by two slugs. A canonical is a hint; a 301 is a hop.

THE LIMIT: a redirect consolidates the pair for Google; it does not make the
keeper rank. That is measured in GSC after deploy, not here.
"""
import pathlib
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402

TWIN = "equinix-equinix-fr5-ad94b281"
KEEPER = "equinix-equinix-fr5-frankfurt-kleyerstrasse-3366f937"
ALIAS = "lumen-technologies-level-3-hayward-dc0928e2"
FROZEN = "level3-hayward-dc0928e2"


def _twin_row(**over):
    row = {"id": 18622, "name": "Equinix FR5", "provider": "Equinix",
           "address": "Kleyerstraße", "city": "Frankfurt am Main",
           "latitude": 50.0988, "longitude": 8.632103,
           "is_duplicate": 0, "duplicate_of_id": 337,
           "canonical_slug": TWIN}
    row.update(over)
    return row


def _keeper(**over):
    k = {"canonical_slug": KEEPER, "address": None,
         "latitude": 50.09885, "longitude": 8.632004,
         "duplicate_of_id": None, "slug_rows": 1}
    k.update(over)
    return k


# ── the decision function ────────────────────────────────────────────────

def test_case_b_twin_with_one_sided_address_and_close_coords_redirects(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row", lambda _id: _keeper())
    assert fpp._twin_redirect_target(_twin_row(), TWIN) == KEEPER


def test_case_b_identical_coordinates_no_addresses_redirects(monkeypatch):
    # the AWS eu-central-1 shape
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(latitude=50.1109, longitude=8.6821))
    fac = _twin_row(address=None, latitude=50.1109, longitude=8.6821)
    assert fpp._twin_redirect_target(fac, TWIN) == KEEPER


def test_case_b_matching_addresses_redirect_even_when_coords_disagree(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(address="kleyerstrasse 90",
                                            latitude=51.0, longitude=9.0))
    fac = _twin_row(address="Kleyerstrasse 90")
    assert fpp._twin_redirect_target(fac, TWIN) == KEEPER


def test_different_street_addresses_keep_200_plus_canonical(monkeypatch):
    """Two buildings the dedup job merely grouped: a hint, not a hop."""
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(address="Hanauer Landstrasse 300"))
    fac = _twin_row(address="Kleyerstrasse 90")   # 12 m apart by coords
    assert fpp._twin_redirect_target(fac, TWIN) is None


def test_far_apart_coordinates_without_addresses_keep_200(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(latitude=50.2, longitude=8.9))
    fac = _twin_row(address=None)
    assert fpp._twin_redirect_target(fac, TWIN) is None


def test_no_coordinates_and_no_addresses_keep_200(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(latitude=None, longitude=None))
    fac = _twin_row(address=None, latitude=None, longitude=None)
    assert fpp._twin_redirect_target(fac, TWIN) is None


def test_loop_guard_keeper_that_points_onward_never_redirects(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(duplicate_of_id=99))
    assert fpp._twin_redirect_target(_twin_row(), TWIN) is None


def test_loop_guard_shared_frozen_slug_never_redirects(monkeypatch):
    """Two rows wearing the keeper's slug: the URL might serve the other one,
    which could carry its own pointer. Refuse the hop."""
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(slug_rows=2))
    assert fpp._twin_redirect_target(_twin_row(), TWIN) is None


def test_keeper_slug_equal_to_request_or_frozen_never_redirects(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(canonical_slug=TWIN))
    assert fpp._twin_redirect_target(_twin_row(), TWIN) is None


def test_unresolvable_keeper_keeps_200(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row", lambda _id: None)
    assert fpp._twin_redirect_target(_twin_row(), TWIN) is None


def test_keeper_itself_does_not_redirect(monkeypatch):
    calls = []
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: calls.append(_id) or _keeper())
    fac = _twin_row(canonical_slug=KEEPER, duplicate_of_id=None, id=337)
    assert fpp._twin_redirect_target(fac, KEEPER) is None
    assert calls == [], "a row with no pointer must not even look up a keeper"


def test_case_a_alias_of_the_same_row_redirects_to_its_frozen_slug(monkeypatch):
    calls = []
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: calls.append(_id) or None)
    fac = _twin_row(canonical_slug=FROZEN, duplicate_of_id=None)
    assert fpp._twin_redirect_target(fac, ALIAS) == FROZEN
    assert calls == []


def test_case_a_refuses_a_malformed_frozen_slug():
    fac = _twin_row(canonical_slug="Not A Slug/at all", duplicate_of_id=None)
    assert fpp._twin_redirect_target(fac, ALIAS) is None


# ── the route ────────────────────────────────────────────────────────────

def _app(monkeypatch, rows, keeper):
    app = Flask("t")
    app.register_blueprint(fpp.facility_profile_bp)
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", lambda s: rows.get(s))
    monkeypatch.setattr(fpp, "_canonical_twin_row", lambda _id: keeper)
    monkeypatch.setattr(fpp, "_nearby_generation_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(fpp, "_render_profile",
                        lambda fac, slug: f"<html>rendered {slug}</html>")
    return app.test_client()


def test_route_twin_301s_to_keeper_and_keeper_serves_200(monkeypatch):
    rows = {TWIN: _twin_row(),
            KEEPER: _twin_row(id=337, canonical_slug=KEEPER,
                              duplicate_of_id=None, address=None)}
    c = _app(monkeypatch, rows, _keeper())
    r = c.get(f"/facilities/{TWIN}")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(f"/facilities/{KEEPER}")
    assert r.headers.get("X-DC-Hub-Source") == "facility-twin-301"
    # follow the hop by hand: the keeper must terminate the chain
    r2 = c.get(f"/facilities/{KEEPER}")
    assert r2.status_code == 200
    assert "Location" not in r2.headers
    assert b"rendered" in r2.data


def test_route_keeps_200_when_addresses_differ(monkeypatch):
    rows = {TWIN: _twin_row(address="Kleyerstrasse 90")}
    c = _app(monkeypatch, rows, _keeper(address="Hanauer Landstrasse 300"))
    r = c.get(f"/facilities/{TWIN}")
    assert r.status_code == 200
    assert b"rendered" in r.data


def test_route_alias_301s_to_frozen_slug(monkeypatch):
    rows = {ALIAS: _twin_row(canonical_slug=FROZEN, duplicate_of_id=None),
            FROZEN: _twin_row(canonical_slug=FROZEN, duplicate_of_id=None)}
    c = _app(monkeypatch, rows, None)
    r = c.get(f"/facilities/{ALIAS}")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(f"/facilities/{FROZEN}")
    assert c.get(f"/facilities/{FROZEN}").status_code == 200


def test_route_decision_failure_is_fail_open(monkeypatch):
    rows = {TWIN: _twin_row()}
    c = _app(monkeypatch, rows, None)
    def _boom(_fac, _slug):
        raise RuntimeError("db gone")
    monkeypatch.setattr(fpp, "_twin_redirect_target", _boom)
    assert c.get(f"/facilities/{TWIN}").status_code == 200


# ── the keeper query's shape ─────────────────────────────────────────────

def test_keeper_query_selects_the_fields_the_guards_read():
    import inspect
    src = inspect.getsource(fpp._canonical_twin_row)
    for needle in ("COALESCE(k.is_duplicate, 0) = 0", "k.duplicate_of_id",
                   "AS slug_rows", "k.address", "k.latitude", "k.longitude"):
        assert needle in src, f"keeper query lost {needle!r}"


def test_canonical_url_wrapper_still_serves_the_rel_canonical_path(monkeypatch):
    monkeypatch.setattr(fpp, "_canonical_twin_row",
                        lambda _id: _keeper(slug_rows=2, duplicate_of_id=5))
    # the canonical HINT is unconditional on the redirect-only guards
    assert fpp._canonical_twin_url(337) == f"https://dchub.cloud/facilities/{KEEPER}"
