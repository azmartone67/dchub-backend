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
    """A resolve_canon() payload whose baseline is 'live AGREES with the pin'.

    ★2026-09-02: this base used to be a hand-copy of PINNED['public'] as it
    stood on 2026-08-28 ({"facilities": "18,500+", "deals": "1,900+", ...}).
    The canon walk to facilities 20,100+ / deals 2,000+ left the copy BELOW the
    floor, so every scenario that did not override a key silently changed
    meaning from "live agrees" to "live is degraded" — a baseline that drifts
    into the reject path is worse than no baseline. Derived from the canon now,
    so the next walk carries it along instead of stranding it.
    """
    base = dict(PINNED["public"])
    base.update(over)
    return {"public": base}


def _as_int(phrase) -> int:
    """Parse a floor phrase WITHOUT ai_surface_canon._floor_int, deliberately:
    these tests must not build and check their inputs with the same parser
    they are exercising, or a broken parser would make them pass vacuously."""
    return int(str(phrase).replace(",", "").replace("+", "").strip())


# A live facility count that has RISEN ABOVE the pin — the input to the two
# "overlay raises" tests. Kept a LITERAL on purpose: it is the independent half
# of the floor comparison, and deriving it from PINNED would have the raise
# path testing the canon against itself.
# ★2026-09-02: 19,300+ -> 20,900+, because PINNED['public']['facilities'] was
# walked 18,500+ -> 20,100+ (live /api/v1/stats facilities = 20,198). 19,300+
# now sits BELOW the floor, so those two tests would have exercised the REJECT
# path while claiming to prove the overlay RAISES — the exact inversion this
# module exists to prevent. Re-based above the new pin; the guard test below
# fails loudly the next time a walk overtakes it.
RISEN_FACILITIES = "20,900+"


# ── the overlay raises ───────────────────────────────────────────────

def test_a_risen_live_floor_is_taken(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities=RISEN_FACILITIES))
    assert resolve_public_floors()["facilities"] == RISEN_FACILITIES


def test_agents_md_renders_the_risen_floor(monkeypatch):
    """The actual #1872-class symptom: the page, not just the helper."""
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities=RISEN_FACILITIES))
    page = _render_agents_md()
    assert f"{RISEN_FACILITIES} facilities" in page
    # ★2026-09-02: was the hardcoded old pin "18,500+ facilities". The claim is
    # "the page stopped serving THE PIN once live rose above it", so it reads
    # the pin from canon and cannot be left asserting a retired value.
    assert f"{PINNED['public']['facilities']} facilities" not in page


def test_the_risen_scenario_still_rises_above_the_pin():
    """★2026-09-02 GUARDS THE TWO TESTS ABOVE. They only mean "the overlay
    RAISES" while RISEN_FACILITIES sits above PINNED['public']['facilities'].
    The pin has been hand-walked seven times and will be again; when a walk
    overtakes this literal, fail HERE with an instruction, instead of letting
    those two tests quietly become reject-path tests that assert the opposite
    of their names."""
    assert _as_int(RISEN_FACILITIES) > _as_int(PINNED["public"]["facilities"]), (
        f"RISEN_FACILITIES {RISEN_FACILITIES} no longer rises above the pinned "
        f"floor {PINNED['public']['facilities']} — RE-BASE it above the pin. "
        "Do not delete this assertion and do not lower the pin to suit it."
    )


# ── the overlay never lowers ─────────────────────────────────────────

def test_the_degraded_resolver_cannot_lower_a_floor(monkeypatch):
    """THE one that matters. 400 is a positive integer and canon_is_live()
    reads True for it — only the floor comparison rejects it."""
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="400+", deals="1,400+"))
    out = resolve_public_floors()
    assert out["facilities"] == PINNED["public"]["facilities"]
    assert out["deals"] == PINNED["public"]["deals"]
    # ★2026-09-02: was the literal "facilities=400<18500". 400 stays a literal
    # (it is the measured DB-down degrade), but the floor half is read from
    # canon — the pin walked to 20,100+ and a hand-typed pin here only re-breaks
    # at the next walk.
    assert f"facilities=400<{_as_int(PINNED['public']['facilities'])}" in out["_rejected"]


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
