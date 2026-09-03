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

import ast
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_surface_canon                                       # noqa: E402
from ai_surface_canon import (                                # noqa: E402
    PINNED,
    resolve_public_floors,
    resolve_public_floors_cached,
)
from routes.agents_md_fallback import _render_agents_md       # noqa: E402

_HANDLER = Path(__file__).resolve().parent.parent / "routes" / "agents_md_fallback.py"


@pytest.fixture(autouse=True)
def _isolate_floors_cache():
    """★ The floors cache is PROCESS-GLOBAL, and without this the file is
    order-dependent. Proved 2026-09-02: test_agents_md_renders_the_risen_floor
    primes the cache with "19,300+" via a background refresh, and
    test_agents_md_never_publishes_the_degraded_floor then read THAT instead of
    the pin — it passed run alone and failed run in file order.

    Suppressing the refresh flag also stops any test spawning a real network
    thread, so a refresh can never land mid-assertion in a later test."""
    def _reset():
        ai_surface_canon._public_floors_cache.update({"at": 0.0, "val": None})
        ai_surface_canon._public_floors_refreshing = True   # never spawn
    _reset()
    yield
    _reset()
    ai_surface_canon._public_floors_refreshing = False


def _prime():
    """Fill the floors cache synchronously, exactly as the background refresh
    would, so a test can exercise the WARM path deterministically."""
    ai_surface_canon._public_floors_cache["val"] = resolve_public_floors()
    ai_surface_canon._public_floors_cache["at"] = time.time()


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
    """The actual #1872-class symptom: the page, not just the helper.

    ★2026-09-02: the page now reads the floors from cache, so a live value
    reaches it once a refresh has landed rather than on the same request.
    _prime() is that refresh. The property under test is unchanged — a risen
    floor MUST reach the page — only the moment it arrives moved."""
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="19,300+"))
    _prime()
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
    _prime()   # the degrade must be rejected THROUGH the cache, not merely
               # missed because the cache happened to be cold
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


# ── the request path must stay OFF the uncached resolver ─────────────
#
# ★2026-09-02. routes/agents_md_fallback.py called resolve_public_floors() —
# the UNCACHED variant, which makes live HTTP calls per request — directly on
# the /AGENTS.md request path. Measured on that handler, outside the fleet:
# cold 15,400.5ms / 8,003.5 / 8,160.9, against an edge ROUTE_TIMEOUTS DEFAULT
# of 15s. The cold render was already OVER the limit.
#
# Nothing would have caught the regression coming back. ai_surface_sentinel
# audits /AGENTS.md as kind "text" (see its _SURFACES), so the JSON checks
# never run on it, and it asserts no latency budget at all — this surface is
# half-watched. These two guards are the watch: one structural, one behavioural.


def _calls_within(func_name: str) -> set[str]:
    """Every function name called inside `func_name`, via AST.

    ★ AST, not a substring scan of the file. A `"resolve_public_floors(" in
    src` check reports the same thing whether the call sits on the request
    path or in a docstring, a comment, or a helper that never runs — and this
    file's module docstring NAMES resolve_public_floors() three times."""
    tree = ast.parse(_HANDLER.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name), None)
    assert fn is not None, (
        f"{func_name}() no longer exists in {_HANDLER.name}. This guard "
        f"anchors to it; a renamed handler must re-point the guard, not "
        f"silently leave it inspecting nothing.")
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    assert out, f"{func_name}() parsed but calls nothing — guard is vacuous"
    return out


def test_the_request_path_does_not_call_an_uncached_resolver():
    """STRUCTURAL. _render_agents_md() must reach the floors through the
    cached wrapper, never the probing one."""
    calls = _calls_within("_render_agents_md")

    # POSITIVE ANCHOR FIRST. Without this the test passes just as happily if
    # the floors read is deleted outright, which is not the state we want.
    assert "resolve_public_floors_cached" in calls, (
        "_render_agents_md() no longer reads the floors through "
        "resolve_public_floors_cached(). If the floors moved somewhere else, "
        "this guard must move with them.")

    banned = {"resolve_public_floors", "resolve_canon"} & calls
    assert not banned, (
        f"/AGENTS.md is back on an UNCACHED resolver: {sorted(banned)}.\n\n"
        f"These probe live HTTP per request (/api/v1/stats plus a tools/list "
        f"through Cloudflare). Measured on this handler: cold 15,400ms "
        f"against a 15s edge ROUTE_TIMEOUTS DEFAULT — the cold render was "
        f"already over the limit.\n\n"
        f"/AGENTS.md is the PRIMARY agent-discovery surface and it times out "
        f"SILENTLY: a 503 tells a registry scraper nothing, and the sentinel "
        f"audits this surface as kind 'text', so nothing pages.\n\n"
        f"Use resolve_public_floors_cached(). It is the same overlay — same "
        f"keys, same raise-only rule — answered from memory.")


def test_rendering_the_page_does_not_probe_when_the_cache_is_warm(monkeypatch):
    """BEHAVIOURAL, and the one that cannot be satisfied by renaming things.

    A warm cache means the page owes ZERO live probes. Reverting to the
    uncached resolver makes resolve_canon() fire on the request path, which is
    exactly the 15s cold render, and this counts it."""
    probes = []

    def _counting_resolve_canon():
        probes.append(1)
        return _pub(facilities="19,300+")

    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        _counting_resolve_canon)
    _prime()                       # the one legitimate probe, OFF the request path
    assert probes, "_prime() did not probe — the counter is not wired to the resolver"
    baseline = len(probes)

    page = _render_agents_md()

    assert len(probes) == baseline, (
        f"rendering /AGENTS.md made {len(probes) - baseline} live probe(s) "
        f"with a WARM cache — the request path is back on an uncached "
        f"resolver (measured cold cost: 15.4s vs a 15s edge timeout)")
    assert "19,300+ facilities" in page, \
        "warm cache did not actually reach the page — probe count is meaningless"


def test_the_cached_wrapper_is_the_same_overlay_once_warm(monkeypatch):
    """The SEMANTIC-EQUIVALENCE claim the swap rests on, asserted rather than
    assumed: warm cached output == uncached output, key for key."""
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="19,300+"))
    direct = resolve_public_floors()
    _prime()
    assert resolve_public_floors_cached() == direct


def test_the_cold_cache_still_supplies_every_key_the_page_reads():
    """The cold path is a DIFFERENT branch — it returns PINNED rather than the
    overlay — so the keys the handler indexes must exist on it too. A KeyError
    here would be a 500 on the primary discovery surface."""
    cold = resolve_public_floors_cached()          # fixture left the cache empty
    assert cold.get("_cold") is True, "expected the cold branch"
    for key in ("facilities", "deals", "countries"):
        assert cold[key] == PINNED["public"][key]

    page = _render_agents_md()
    assert f"{PINNED['public']['facilities']} facilities" in page
    assert "None" not in page
