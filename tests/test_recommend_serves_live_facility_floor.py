"""/api/agents/recommend must serve the LIVE facility floor — without putting a
live probe on the request path.

★2026-08-29. get_dchub_recommendation is the #5 paid-demand tool (244 calls / 95
distinct free users in 30d). Its blurb read `ai_surface_canon.PINNED['public']`
directly, so it served "18,500+ facilities" while /llms.txt, /AGENTS.md and
/api/v1/ai-agents.json all served the live "19,300+" — the same defect #3304
fixed on /AGENTS.md, on a tool agents actually call.

The old comment justified the pin as "agent hot path, and resolve_canon() probes
live HTTP per call". ★That half was RIGHT and this fix preserves it. MEASURED
2026-08-29 from outside the fleet, resolve_public_floors() took 7.59s / 7.78s /
15.46s (mean 10.3s), against a 15s edge ROUTE_TIMEOUTS DEFAULT. Wiring it
straight onto the handler would have traded a stale number for an intermittent
503 — the trade the standing-rank probe was explicitly warned off making.

So three properties are load-bearing here, and each is asserted below:

  RAISES ONLY   a live value is taken only when it RAISES the floor (inherited
                from resolve_public_floors; re-asserted THROUGH the cache,
                because a cache is exactly where that guarantee could be lost)
  NEVER BLOCKS  the request path answers from cache and refreshes in a daemon
                thread; a cold process serves the PIN, which is a floor, so the
                cold answer is under-stated and never wrong-direction
  ACTUALLY WIRED the route reads the cached resolver, not PINNED['public']

★The third matters most. The bug this whole sweep is about is a fix landing in
a copy nothing reads, so a test that only exercises the helper would pass while
the route kept serving the pin.
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_surface_canon                                              # noqa: E402
from ai_surface_canon import resolve_public_floors_cached            # noqa: E402


# ★2026-09-02 canon walk (facilities 18,500+ -> 20,100+, deals 1,900+ -> 2,000+).
# Every number this module used to type was a COPY of the pin of the day, so the
# walk broke four tests at once and turned the "risen live floor" scenario into a
# DEGRADED one (19,300+ now sits BELOW the pin, where the overlay is right to
# refuse it — the test would have proved the opposite of what it claims). The
# three helpers below derive from PINNED['public'] instead, so the relationships
# each test asserts — live-above-pin RAISES, live-below-pin is REFUSED, the cold
# path serves the pin — hold by construction at the next walk too.
def _pub(**over):
    """A resolve_canon() payload that, un-overridden, matches the pin exactly."""
    base = {k: ai_surface_canon.PINNED["public"][k]
            for k in ("facilities", "deals", "markets", "countries")}
    base.update(over)
    return {"public": base}


def _pin(key: str = "facilities") -> str:
    """The pinned floor — what every fallback path below has to serve."""
    return ai_surface_canon.PINNED["public"][key]


def _risen(key: str = "facilities") -> str:
    """A live value unambiguously ABOVE the pinned floor."""
    return f"{ai_surface_canon._floor_int(_pin(key)) + 1000:,}+"


def _reset_cache():
    with ai_surface_canon._public_floors_lock:
        ai_surface_canon._public_floors_cache["val"] = None
        ai_surface_canon._public_floors_cache["at"] = 0.0
        ai_surface_canon._public_floors_refreshing = False


# ── the overlay still only RAISES, through the cache ──────────────────

def test_a_risen_live_floor_survives_the_cache(monkeypatch):
    _reset_cache()
    risen = _risen()                                   # ★2026-09-02: was the literal "19,300+"
    assert ai_surface_canon._floor_int(risen) > ai_surface_canon._floor_int(_pin()), (
        "the risen scenario no longer rises — it is testing the refusal path instead")
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities=risen))
    ai_surface_canon._refresh_public_floors()          # synchronous, no thread
    assert resolve_public_floors_cached()["facilities"] == risen


def test_a_degraded_live_floor_is_still_refused_through_the_cache(monkeypatch):
    """The 46x under-claim. A cache must not become a way to smuggle it in."""
    _reset_cache()
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: _pub(facilities="400+"))
    ai_surface_canon._refresh_public_floors()
    out = resolve_public_floors_cached()
    assert out["facilities"] == _pin()
    assert any(r.startswith("facilities=") for r in out["_rejected"])


def test_a_raising_resolver_that_explodes_leaves_the_pin_standing(monkeypatch):
    _reset_cache()
    def _boom():
        raise RuntimeError("stats down")
    monkeypatch.setattr(ai_surface_canon, "resolve_canon", _boom)
    ai_surface_canon._refresh_public_floors()
    assert resolve_public_floors_cached()["facilities"] == _pin()


# ── it never blocks the request path ──────────────────────────────────

def test_the_cold_call_answers_immediately_with_the_pin(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(ai_surface_canon, "resolve_canon",
                        lambda: (time.sleep(30), _pub())[1])   # pathologically slow
    t0 = time.time()
    out = resolve_public_floors_cached()
    elapsed = time.time() - t0
    assert elapsed < 0.5, f"cold call BLOCKED for {elapsed:.2f}s — the 503 trade is back"
    assert out["facilities"] == _pin()
    assert out.get("_cold") is True


def test_the_cold_answer_is_never_empty():
    """An empty string here renders 'DC Hub aggregates intelligence from
    facilities' — worse than a stale number, and it is what `_pub = {}` did."""
    _reset_cache()
    out = resolve_public_floors_cached()
    for key in ("facilities", "countries", "deals", "markets"):
        assert str(out.get(key, "")).strip(), f"{key} came back empty"


# ── and the ROUTE actually reads it ───────────────────────────────────

def _recommend_fn():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "api_agents_recommend"), None)
    assert fn is not None, "api_agents_recommend not found in main.py"
    return fn, src


def _recommend_source() -> str:
    fn, src = _recommend_fn()
    return ast.get_source_segment(src, fn) or ""


def test_the_route_reads_the_cached_resolver():
    """★Asserted on the ASSIGNMENT, not on the name appearing somewhere.

    A substring check passes vacuously while the import line still carries the
    name — reverting only `_pub = ...` left it green in mutation testing. The
    call has to actually feed the blurb."""
    fn, _ = _recommend_fn()
    wired = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_pub" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "resolve_public_floors_cached"
        for node in ast.walk(fn)
    )
    assert wired, (
        "/api/agents/recommend does not assign _pub from resolve_public_floors_cached() "
        "— the blurb is back on a frozen pin")


def test_the_route_does_not_build_the_blurb_from_pinned_public():
    src = _recommend_source()
    assert "_ai_canon.get('public')" not in src and '_ai_canon.get("public")' not in src, (
        "the blurb is reading PINNED['public'] again — that is the 18,500+ bug")
