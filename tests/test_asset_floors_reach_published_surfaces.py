"""
test_asset_floors_reach_published_surfaces.py — a measured floor that no
publication key names is a number nobody can serve.
(2026-09-19)

MEASURED live that day, cache-busted:

    /api/v1/canon/phrases   assets            "320,000+"  value_source pinned
    /api/v1/infrastructure/stats              330,961     (the owner)
    /api/v1/canon/phrases   transmission_lines "94,000+"  value_source pinned
    /api/v1/infrastructure/stats               95,569     (the owner)

substations and fiber_routes were ALSO pinned and merely CORRECT BY COINCIDENCE
— the hand-typed pin happened to equal the live floor.

★ THE CAUSE WAS NOT A FAILING QUERY. canonical_stats gained floor specs AND
  SELECT COUNT(*) queries for substations (2026-09-02) and fiber_routes +
  transmission_lines (2026-09-07), and both notes say the point was to stop
  surfaces hardcoding. Neither ever published, because
  ai_surface_canon._PUBLIC_FLOOR_KEYS — the tuple resolve_public_floors() loops
  over — still named only the original four. A SPEC, a QUERY and a PUBLICATION
  KEY are three separate gates, and this repo has now missed each of them in
  turn for the same figure (the 09-02 note records its own predecessor missing
  the spec; 09-07 records the query; this file is the third).

  So the load-bearing test here is test_every_publication_key_has_a_spec_and_a_pin
  — it ties the three gates together so the next number cannot pass one and
  silently fail another.
"""
import re

import pytest

import ai_surface_canon
import canonical_stats
from ai_surface_canon import _PUBLIC_FLOOR_KEYS, PINNED, resolve_public_floors


ASSET_KEYS = ("substations", "fiber_routes", "transmission_lines", "assets")


# ── the three gates must agree ──────────────────────────────────────────────
def test_every_publication_key_has_a_spec_and_a_pin():
    """Spec (measured) + pin (cold-start floor) + key (publishable). Missing any
    one of the three is how each of these figures stayed stale in turn."""
    specs = canonical_stats._PUBLIC_FLOOR_SPECS
    pinned_public = PINNED.get("public") or {}
    for key in _PUBLIC_FLOOR_KEYS:
        assert key in specs, f"{key} is publishable but nothing measures it"
        assert key in pinned_public, f"{key} is publishable but has no cold-start pin"


def test_the_asset_layers_are_publishable():
    for key in ASSET_KEYS:
        assert key in _PUBLIC_FLOOR_KEYS, (
            f"{key} is measured and pinned but not in _PUBLIC_FLOOR_KEYS, so "
            f"resolve_public_floors() can never overlay it — the 09-02/09-07 bug")


def test_the_original_four_are_still_publishable():
    for key in ("facilities", "deals", "markets", "countries"):
        assert key in _PUBLIC_FLOOR_KEYS


# ── the overlay's semantics hold for the new keys ───────────────────────────
def _overlay_with(monkeypatch, public):
    monkeypatch.setattr(ai_surface_canon, "resolve_canon", lambda: {"public": public})
    return resolve_public_floors()


def test_a_higher_live_value_replaces_the_pin(monkeypatch):
    """transmission_lines: pin 94,000+, owner measured 95,569 -> 95,000+."""
    out = _overlay_with(monkeypatch, {"transmission_lines": "95,000+"})
    assert out["transmission_lines"] == "95,000+"
    assert out["_source"]["transmission_lines"] == "live"


def test_a_lower_live_value_is_rejected_not_published(monkeypatch):
    """The overlay only ever RAISES. A degraded resolver must not lower a floor
    just because its key is now listed — that is what makes widening the tuple
    cheap."""
    pin = (PINNED.get("public") or {}).get("assets")
    assert pin, "assets lost its cold-start pin"
    out = _overlay_with(monkeypatch, {"assets": "1,000+"})
    assert out["assets"] == pin
    assert out["_source"]["assets"] == "pinned"
    assert any(r.startswith("assets=") for r in out["_rejected"])


def test_an_unmeasured_key_leaves_the_pin(monkeypatch):
    pin = (PINNED.get("public") or {}).get("substations")
    out = _overlay_with(monkeypatch, {})
    assert out["substations"] == pin
    assert out["_source"]["substations"] == "pinned"


# ── the assets figure itself ────────────────────────────────────────────────
def test_assets_floors_to_ten_thousand():
    _stat_key, floor = canonical_stats._PUBLIC_FLOOR_SPECS["assets"]
    assert floor(330961) == "330,000+"
    assert floor(329999) == "320,000+"


def test_assets_beats_the_pin_it_replaces():
    """The measured owner value must clear the pin, or the overlay rejects it
    and this whole change is inert."""
    _stat_key, floor = canonical_stats._PUBLIC_FLOOR_SPECS["assets"]
    pin_i = int(re.sub(r"[^\d]", "", (PINNED.get("public") or {})["assets"]))
    assert int(re.sub(r"[^\d]", "", floor(330961))) >= pin_i


def test_the_asset_member_list_is_imported_not_retyped():
    """routes/infrastructure_data_routes._STATS_MEMBERS decides what an 'asset'
    is. A second copy of those table names here would be two owners again, one
    level down."""
    src = open(canonical_stats.__file__, encoding="utf-8").read()
    assert "_STATS_MEMBERS" in src, "the owner's member list is no longer imported"
    for table in ("subsea_cables", "subsea_landing_points", "power_plants_eia"):
        assert table not in src, (
            f"{table} is retyped in canonical_stats — import the member list instead")


def test_asset_member_tables_excludes_subset_and_facility():
    """The filter under test, not the owner's list: `== 'asset'`, never a
    negation that would sweep in a role added later."""
    tables = canonical_stats.asset_member_tables()
    assert "transmission_lines_eia" not in tables       # the subset
    assert "discovered_facilities" not in tables        # the facility population
    assert "substations" in tables and "fiber_routes" in tables
    assert len(tables) >= 7


def test_subset_and_facility_members_are_never_summed():
    """The owner tags 'subset' (a narrower view of a population already counted)
    and 'facility' (a different population entirely). Summing either inflates
    the asset total."""
    from routes.infrastructure_data_routes import _STATS_MEMBERS
    roles = {role for _k, _t, role in _STATS_MEMBERS}
    assert {"asset", "subset", "facility"} <= roles
    assets = [t for _k, t, role in _STATS_MEMBERS if role == "asset"]
    assert "transmission_lines_eia" not in assets       # the subset
    assert "discovered_facilities" not in assets        # the facility population
    assert len(assets) >= 7


# ── controls: each assertion above must be able to fail ─────────────────────
def test_control_the_spec_check_rejects_a_missing_spec():
    specs = dict(canonical_stats._PUBLIC_FLOOR_SPECS)
    specs.pop("assets", None)
    assert "assets" not in specs


def test_control_the_retype_scan_would_catch_a_pasted_table():
    assert "subsea_cables" in "out['x'] = 'SELECT COUNT(*) FROM subsea_cables'"
