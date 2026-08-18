"""Guard: the white-glove lane must propagate LIVE canon, not the pinned fallback.

★ WHY THIS EXISTS. white_glove_propagation.load_canon() read ai_surface_canon
.PINNED directly. PINNED["public"] is the DB-DOWN fallback — ai_surface_canon
says so in its own comments, and warns that "every downstream consumer
(registry submitters, description builders, white-glove propagation) kept
pasting it" the last time it froze.

It froze again. Measured 2026-08-18:

    live  GET /api/v1/canon/phrases      -> facilities: "18,300+"
    lane  white_glove_runs.payload.canon -> facilities_floor: 18000

So the lane whose entire job is scrubbing stale numbers off our registry
listings was writing a 300-stale number ONTO them, and every listing it
"corrected" was corrected to the wrong value. These tests pin the direction of
that dependency: resolve_canon() wins, PINNED is only the floor of last resort.
"""
import sys
import types

import pytest


@pytest.fixture
def load_canon(monkeypatch):
    """Import load_canon with a stub ai_surface_canon so the test never needs
    a DB or the live HTTP probe resolve_canon() normally performs."""
    from routes.white_glove_propagation import load_canon as _lc
    return _lc


def _stub_canon(monkeypatch, pinned_public, resolved_public, resolve_raises=False):
    """Install a fake ai_surface_canon module for the duration of a test."""
    mod = types.ModuleType("ai_surface_canon")
    mod.PINNED = {
        "tools_advertised": 82,
        "version": "2.12.0",
        "mcp_endpoint": "https://dchub.cloud/mcp",
        "public": dict(pinned_public),
        "stale_markers": [],
    }

    def _resolve():
        if resolve_raises:
            raise RuntimeError("stats probe down")
        return {"public": dict(resolved_public)}

    mod.resolve_canon = _resolve
    monkeypatch.setitem(sys.modules, "ai_surface_canon", mod)


PINNED_PUBLIC = {"facilities": "18,000+", "deals": "1,800+", "markets": "300+"}
LIVE_PUBLIC = {"facilities": "18,300+", "deals": "1,800+", "markets": "300+"}


def test_live_canon_beats_pinned_fallback(monkeypatch, load_canon):
    """The exact 2026-08-18 production drift: pinned says 18,000, live says
    18,300. The lane must publish 18,300."""
    _stub_canon(monkeypatch, PINNED_PUBLIC, LIVE_PUBLIC)
    out = load_canon()
    assert out["facilities_floor"] == 18300, (
        "load_canon published the PINNED fallback over the live-resolved value "
        "— this is the bug that pasted 18,000+ onto every registry listing"
    )
    assert out["canon_source"].startswith("resolve_canon:")
    assert "facilities" in out["canon_source"]


def test_falls_back_to_pinned_when_resolver_raises(monkeypatch, load_canon):
    """Fail-soft: a dead resolver must leave the pinned floor standing, not
    None and not 0. Publishing "0+ facilities" is worse than publishing a
    stale floor."""
    _stub_canon(monkeypatch, PINNED_PUBLIC, LIVE_PUBLIC, resolve_raises=True)
    out = load_canon()
    assert out["facilities_floor"] == 18000
    assert out["canon_source"] == "pinned"


def test_unresolvable_field_does_not_drag_the_others_back(monkeypatch, load_canon):
    """Per-field overlay. One missing field must not discard the two that DID
    resolve — an all-or-nothing overlay would republish stale deals/markets
    every time the facilities probe hiccuped."""
    partial = {"facilities": "18,300+", "deals": None, "markets": "310+"}
    _stub_canon(monkeypatch, PINNED_PUBLIC, partial)
    out = load_canon()
    assert out["facilities_floor"] == 18300
    assert out["markets_floor"] == 310
    assert out["deals_floor"] == 1800, "unresolved field should keep its pinned floor"


def test_zero_and_negative_live_values_are_rejected(monkeypatch, load_canon):
    """A resolver that returns 0 is a broken resolver, not a fleet of zero
    facilities. ★ prior art: canonical_stats shipped "0+" to the public
    surfaces exactly this way."""
    _stub_canon(monkeypatch, PINNED_PUBLIC, {"facilities": "0+", "deals": "1,800+",
                                             "markets": "300+"})
    out = load_canon()
    assert out["facilities_floor"] == 18000, "0 must never overwrite a real floor"


def test_degraded_resolver_below_the_floor_is_rejected(monkeypatch, load_canon):
    """★ THE ONE THAT ALMOST SHIPPED. resolve_canon() does not raise when the
    DB is down — it DEGRADES. Measured 2026-08-18 with no DATABASE_URL it
    returned, without error, facilities=400 / deals=1400 against pinned floors
    of 18,000 / 1,800.

    A `> 0` check does not catch this: 400 is a positive integer. Taking it
    would paste "400+ facilities" onto every registry listing — a 45x
    UNDER-claim, and strictly worse than the stale 18,000 this change fixes.
    The overlay may only ever RAISE a floor."""
    degraded = {"facilities": "400+", "deals": "1,400+", "markets": "300+"}
    _stub_canon(monkeypatch, PINNED_PUBLIC, degraded)
    out = load_canon()
    assert out["facilities_floor"] == 18000, (
        "a DB-down resolver lowered the published facility floor to 400 — "
        "the lane would have under-claimed by 45x on every listing"
    )
    assert out["deals_floor"] == 1800
    assert out["canon_below_floor"], "below-floor rejection must be recorded"


def test_growth_still_raises_the_floor(monkeypatch, load_canon):
    """The max() guard must not freeze the number. Real fleet growth — the
    normal case — still has to propagate, or this fix just reintroduces the
    original staleness with extra steps."""
    grown = {"facilities": "19,500+", "deals": "1,900+", "markets": "320+"}
    _stub_canon(monkeypatch, PINNED_PUBLIC, grown)
    out = load_canon()
    assert out["facilities_floor"] == 19500
    assert out["deals_floor"] == 1900
    assert out["markets_floor"] == 320
    assert not out.get("canon_below_floor")


def test_canon_source_is_reported(monkeypatch, load_canon):
    """The run payload must record WHICH source supplied the numbers, so a
    future audit can tell a live run from a fallback run without guessing."""
    _stub_canon(monkeypatch, PINNED_PUBLIC, LIVE_PUBLIC)
    assert load_canon()["canon_source"] != "none"
