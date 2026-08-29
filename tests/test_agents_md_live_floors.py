"""/AGENTS.md must serve the LIVE floor, and must never serve a degraded one.

★2026-08-28. Two failures, opposite directions, and fixing one naively causes
the other:

  STALE   routes/agents_md_fallback read ai_surface_canon.PINNED directly, so
          it served "18,500+ facilities" while /llms.txt,
          /api/v1/canon/phrases and /api/v1/ai-agents.json all served the live
          "19,300+". The pin has been hand-chased upward six times, each time
          AFTER it froze.

  WORSE   swapping it to resolve_canon() publishes "400+ facilities" during any
          DB outage. Measured on main with no DATABASE_URL, resolve_canon()
          returns public.facilities "400+" against a pinned "18,500+" WITHOUT
          raising — canonical_stats._FALLBACK["facilities_verified"] is 400 —
          and canon_is_live() reads True for it, because 400 IS a measurement.
          A 46x under-claim on the primary agent-discovery surface.

The load-bearing property is that the overlay only ever RAISES a floor, so
that is what is asserted, in both directions.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_surface_canon                                       # noqa: E402
from ai_surface_canon import PINNED, resolve_public_floors    # noqa: E402
from routes.agents_md_fallback import _render_agents_md       # noqa: E402


def _pub(**over):
    base = {"facilities": "18,500+", "deals": "1,900+",
            "markets": "300+", "countries": "170+"}
    base.update(over)
    return {"public": base}


# ── the overlay raises ───────────────────────────────────────────────

def test_a_risen_live_floor_is_taken(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="19,300+"))
    assert resolve_public_floors()["facilities"] == "19,300+"


def test_agents_md_renders_the_risen_floor(monkeypatch):
    """The actual #1872-class symptom: the page, not just the helper."""
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="19,300+"))
    page = _render_agents_md()
    assert "19,300+ facilities" in page
    assert "18,500+ facilities" not in page


# ── the overlay never lowers ─────────────────────────────────────────

def test_the_degraded_resolver_cannot_lower_a_floor(monkeypatch):
    """THE one that matters. 400 is a positive integer and canon_is_live()
    reads True for it — only the floor comparison rejects it."""
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="400+", deals="1,400+"))
    out = resolve_public_floors()
    assert out["facilities"] == PINNED["public"]["facilities"]
    assert out["deals"] == PINNED["public"]["deals"]
    assert "facilities=400<18500" in out["_rejected"]


def test_agents_md_never_publishes_the_degraded_floor(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="400+"))
    page = _render_agents_md()
    assert "400+ facilities" not in page
    assert f"{PINNED['public']['facilities']} facilities" in page


def test_must_fail_control_canon_is_live_does_not_catch_the_degrade():
    """CONTROL: pin down WHY the existing fail-closed primitive is not enough,
    so nobody 'simplifies' resolve_public_floors down to a canon_is_live gate.
    If this ever fails, canon_is_live grew a sanity check and this helper's
    justification needs rewriting."""
    from ai_surface_canon import canon_is_live
    degraded = {"public": {"facilities": "400+"},
                "facilities_verified_live": "400+"}
    assert canon_is_live(degraded, "public.facilities") is True, \
        "canon_is_live answers 'was it measured', not 'is it sane'"


# ── fail-soft ────────────────────────────────────────────────────────

def test_a_raising_resolver_error_leaves_the_pin_standing(monkeypatch):
    def boom():
        raise RuntimeError("neon down")
    monkeypatch.setattr(ai_surface_canon, "resolve_canon", boom)
    out = resolve_public_floors()
    assert out["facilities"] == PINNED["public"]["facilities"]
    assert out["_source"]["facilities"] == "pinned"


def test_a_non_numeric_live_phrase_is_skipped_not_crashed(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="coming soon"))
    out = resolve_public_floors()
    assert out["facilities"] == PINNED["public"]["facilities"]


def test_agents_md_still_renders_with_no_resolver_at_all(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_canon", lambda: {})
    page = _render_agents_md()
    assert "AGENTS.md — DC Hub" in page
    assert "None" not in page
