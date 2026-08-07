"""Operator-of-the-day editorial lane (2026-08-07).

Pins the fix for the empty-slate starvation (media silent since 08-04 because
no lead cleared the newsworthiness bar) AND the operator feature: a dependable
daily lead that carries CAPACITY + NEW PROJECTS and rotates to a FRESH operator
each day so it never repeats.

What is pinned:
  1. A candidate becomes a lead carrying the operator's capacity (buildings/MW)
     and new projects (sites added), positive and number-led.
  2. Rotation: an operator featured within the entity window is SKIPPED and the
     next fresh operator leads — the daily-repeat that starves/annoys the feed
     cannot happen.
  3. No material → no lead (never fabricate).
  4. The kill switch works, and the seed score is a reliable floor (>= the
     repetitive dcpi_build one-liner it replaces).

CI-SAFETY: pick_spotlight, _conn, and recent_lead_ledger are all stubbed; no
DB, no network.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def med(monkeypatch):
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    monkeypatch.delenv("MEDIA_OPERATOR_LANE_DISABLE", raising=False)
    import routes.media_editorial as m
    # Never touch a real DB: hand the lane a sentinel "connection".
    monkeypatch.setattr(m, "_conn", lambda: object())
    return m


def _stub_pick(monkeypatch, med, sequence):
    """Stub operator_spotlight.pick_spotlight to yield candidates in order,
    honoring exclude_keys (skips any candidate whose key is excluded)."""
    import routes.operator_spotlight as osp

    def fake(conn, exclude_keys=None):
        ex = set(exclude_keys or ())
        for cand in sequence:
            if cand and cand["key"] not in ex:
                return cand
        return None
    monkeypatch.setattr(osp, "pick_spotlight", fake)


NLIGHTEN = {"angle": "portfolio_growth", "operator": "nLighten", "key": "nlighten",
            "added": 33, "sites": ["Frankfurt", "Madrid", "Milan"],
            "fleet_n": 41, "fleet_mw": 0}
EQUINIX = {"angle": "portfolio_growth", "operator": "Equinix", "key": "equinix",
           "added": 7, "sites": ["Dallas", "Ashburn"],
           "fleet_n": 766, "fleet_mw": 4200}


def test_lead_carries_capacity_and_new_projects(med, monkeypatch):
    _stub_pick(monkeypatch, med, [EQUINIX])
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [])
    lead = med._operator_spotlight_lead()
    assert lead is not None
    assert lead["kind"] == "operator_spotlight"
    assert lead["entity"] == "Equinix"
    # capacity present (canonical fleet, not one spelling)
    assert "766" in lead["trend"] and "4,200 MW" in lead["trend"]
    # new projects present
    assert "7 new sites" in lead["headline_number"]
    assert "Dallas" in lead["so_what"]
    assert lead["dedup_key"] == "operator_spotlight:equinix"


def test_lead_score_clears_the_newsworthiness_bar(med, monkeypatch):
    """The bug that kept the feed silent: a flat 0.90 score never cleared
    _NEWSWORTHY_MIN=8, so the lead was produced but never selectable. The
    computed score must clear the bar for real material — and stay capped so
    it never dominates a genuine big deal."""
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [])
    for cand in (EQUINIX, NLIGHTEN):
        _stub_pick(monkeypatch, med, [cand])
        lead = med._operator_spotlight_lead()
        assert lead["score"] >= med._NEWSWORTHY_MIN, \
            f"{cand['operator']} score {lead['score']} < bar {med._NEWSWORTHY_MIN}"
        assert lead["score"] <= 45.0, "operator lead must stay capped"


def test_mw_unknown_is_not_rendered_as_zero(med, monkeypatch):
    _stub_pick(monkeypatch, med, [NLIGHTEN])   # fleet_mw = 0 (unknown)
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [])
    lead = med._operator_spotlight_lead()
    assert "41 tracked buildings" in lead["trend"]
    assert "MW" not in lead["trend"], "must not publish a fabricated 0 MW"


def test_rotation_skips_a_recently_featured_operator(med, monkeypatch):
    # nLighten led within the window; the lane must skip to Equinix.
    _stub_pick(monkeypatch, med, [NLIGHTEN, EQUINIX])
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [
        {"kind": "operator_spotlight", "entity": "nLighten", "days_ago": 2.0}])
    lead = med._operator_spotlight_lead()
    assert lead is not None
    assert lead["entity"] == "Equinix", "featured operator was not rotated out"


def test_no_material_yields_no_lead(med, monkeypatch):
    _stub_pick(monkeypatch, med, [])   # pick_spotlight returns None
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [])
    assert med._operator_spotlight_lead() is None


def test_kill_switch(med, monkeypatch):
    _stub_pick(monkeypatch, med, [EQUINIX])
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [])
    monkeypatch.setenv("MEDIA_OPERATOR_LANE_DISABLE", "1")
    assert med._operator_spotlight_lead() is None


def test_a_small_operator_still_clears_the_bar(med, monkeypatch):
    """Even a modest operator (few new sites, small fleet) must clear the bar —
    the point is a DEPENDABLE daily lead, not one that only fires for megacaps."""
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [])
    small = {"angle": "portfolio_growth", "operator": "Teledata", "key": "teledata",
             "added": 2, "sites": ["Manchester"], "fleet_n": 9, "fleet_mw": 0}
    _stub_pick(monkeypatch, med, [small])
    lead = med._operator_spotlight_lead()
    assert lead["score"] >= med._NEWSWORTHY_MIN
