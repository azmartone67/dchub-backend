"""/agent must follow the resolver, not the pinned literal.

MEASURED THE HOUR #3196 DEPLOYED. The derivation landed and four surfaces
self-healed 18,500+ -> 18,800+ on their own:

    /llms.txt                       18,800+
    /connect                        18,800+
    /api/v1/ai-agents.json          18,800+
    /.well-known/mcp.json           18,800+   (.description; the 18,500+ that
                                               contradicted its own tool
                                               descriptions was gone)
    /agent                          18,500+   <- alone, still stale

cf-cache-status: DYNAMIC, cache-control: no-store — not a cache. agent_concierge
re-implemented the {canon_*} substitution as its own .replace() chain off PINNED
instead of calling canon_text(), so the derivation could not reach it. One
module opting out of the shared resolver is all it took.

The module's own note gave a reason: resolve_canon() probes live HTTP per call
and /agent is a hot path. That reason is gone — canon_nums() derives from
canonical_stats' cache via live_public_floors(), which is PEEK-ONLY and never
triggers a query.
"""
import re

import pytest

import ai_surface_canon as asc
import canonical_stats as cs
import routes.agent_concierge as ac


@pytest.fixture
def stats_state():
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _warm(**metrics):
    snap = dict(cs._FALLBACK)
    snap.update(metrics)
    cs._cache = snap
    cs._cache_ts = 1e18
    cs._live_keys.update(metrics.keys())


def _body():
    return ac.agent_landing().get_data(as_text=True)


# Far below any plausible pin (which only grows past 18k), so "the derived value
# won" stays assertable without pinning the literal.
_SYNTH, _SYNTH_PHRASE = 12_345, "12,300+"


def test_the_landing_serves_the_resolver_not_the_pin(stats_state):
    """THE guard for what was measured live. If agent_landing ever goes back to
    substituting off PINNED, this is the test that fails."""
    _warm(facilities_verified=_SYNTH)
    body = _body()
    assert _SYNTH_PHRASE in body
    assert asc.PINNED["public"]["facilities"] not in body, (
        "/agent is serving the pinned literal while the resolver says otherwise "
        "— the exact divergence measured in production on 2026-08-25")


def test_a_cold_cache_still_renders_the_pin_not_the_seed(stats_state):
    """Fail-open unchanged: with nothing measured, the pinned floor renders.
    Never canonical_stats' citation seed (400), which would publish '400+' on a
    public page."""
    cs._cache, cs._cache_ts = None, 0.0
    cs._live_keys.clear()
    body = _body()
    assert asc.PINNED["public"]["facilities"] in body
    seed = cs._floor_phrase(cs._FALLBACK["facilities_verified"], step=100)
    assert seed != asc.PINNED["public"]["facilities"], "guard-the-guard: seed == pin"
    # ★2026-09-09: ANCHORED, was `seed not in body`. The seed floors to "400+",
    # and the pin walked to "21,400+" — which CONTAINS "400+" as a suffix, so a
    # plain substring test failed on a page that was in fact rendering the pin
    # correctly. The guard was right about what it wanted and wrong about how it
    # looked: it must match the seed as a STANDALONE figure, not as the tail of a
    # larger one. Rejecting a digit-or-comma immediately before it does that, and
    # keeps working for whatever the pin walks to next — this would have
    # mis-fired on any future value ending in 400+.
    assert not re.search(rf"(?<![\d,]){re.escape(seed)}", body), (
        f"the citation seed {seed!r} is published on a cold cache — the pin "
        f"{asc.PINNED['public']['facilities']!r} must be what renders")


@pytest.mark.parametrize("mode", ["cold", "warm"])
def test_no_placeholder_ever_escapes_to_an_agent(stats_state, mode):
    """The failure canon_text() calls worse than a stale number: shipping the
    literal '{canon_facilities}' to an agent. Both branches must substitute."""
    if mode == "cold":
        cs._cache, cs._cache_ts = None, 0.0
        cs._live_keys.clear()
    else:
        _warm(facilities_verified=_SYNTH, countries_verified=178,
              markets=306, deals=1_931)
    body = _body()
    assert "{canon_" not in body
    assert "{cookbook_html}" not in body


def test_the_derivation_costs_no_query_on_this_hot_path(stats_state, monkeypatch):
    """/agent is a public hot path. Rendering it must never reach the DB — that
    was the module's stated reason for opting out of the shared resolver, and it
    has to stay untrue."""
    calls = []
    monkeypatch.setattr(cs, "_query_live", lambda: calls.append(1) or dict(cs._FALLBACK))
    cs._cache, cs._cache_ts = None, 0.0
    cs._live_keys.clear()
    _body()
    assert calls == [], "rendering /agent triggered a canonical_stats query"


def test_the_anchored_ban_is_not_vacuous():
    """Guard-the-guard for the ★2026-09-09 anchoring above.

    Anchoring a substring ban is how you stop it false-firing on a legitimate
    larger number — and also how you accidentally make it match nothing at all.
    These four cases pin both directions, so the next person to widen or narrow
    the lookbehind finds out here instead of in production.
    """
    def banned(bad, text):
        return re.search(rf"(?<![\d,]){re.escape(bad)}", text) is not None
    # the collision that prompted the change: a legitimate figure ENDING in the
    # banned digits must pass
    assert not banned("400+", "covering 21,400+ distinct facilities")
    assert not banned("1,400+", "covering 21,400+ distinct facilities")
    # the thing the ban exists for must still fail
    assert banned("400+", "covering 400+ distinct facilities")
    assert banned("1,400+", "1,400+ tracked deals")
