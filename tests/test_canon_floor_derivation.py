"""Guards for the PINNED -> resolver derivation (ai_surface_canon.canon_nums).

The defect these exist to prevent is specific and has recurred SIX times:
`PINNED['public']` is a hand-typed floor that every surface which never calls
resolve_canon() serves DIRECTLY — /llms.txt, /agent, /connect,
/api/v1/ai-agents.json, /.well-known/mcp.json, agent_concierge — so it trails
the live resolver by one manual PR, forever. 15,700 -> 17,000 -> 18,000 ->
18,300 -> 18,400 -> 18,500, each one a hand-walk. canon_nums() now prefers
canonical_stats.live_public_floors().

★ WHY THESE TESTS DRIVE MODULE STATE BY HAND. CI has no database, so
live_public_floors() returns {} here and canon_nums() takes the PIN branch on
every call. A guard written the obvious way would therefore assert the pin,
pass, and never once execute the derived branch it exists to protect — green
because the feature is unreachable, indistinguishable from green because it
works. Every test below that cares about the derived path warms the cache
itself.
"""
import pytest

import ai_surface_canon as asc
import canonical_stats as cs


@pytest.fixture
def stats_state():
    """Save/restore the canonical_stats cache + liveness set.

    These are module globals; a test that leaves them warm would make every
    later test in the session read as 'measured'."""
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _warm(**metrics):
    """Populate the cache as a real query would, and mark those metrics live."""
    snap = dict(cs._FALLBACK)
    snap.update(metrics)
    cs._cache = snap
    cs._cache_ts = 1e18          # far future: never lapses into a refresh
    cs._live_keys.update(metrics.keys())


def _cold():
    cs._cache = None
    cs._cache_ts = 0.0
    cs._live_keys.clear()


# A measured value deliberately far BELOW any plausible pin. The pin has only
# ever grown (18,500+ and climbing), so "12,300+" can never coincide with it —
# which keeps 'the derived value won' assertable without pinning the literal.
_SYNTH = 12_345
_SYNTH_PHRASE = "12,300+"


def test_a_measured_floor_beats_the_pin(stats_state):
    _warm(facilities_verified=_SYNTH)
    assert asc.canon_nums()["{canon_facilities}"] == _SYNTH_PHRASE


def test_a_cold_cache_serves_the_pin_not_the_citation_seed(stats_state):
    """The whole reason the pin survives: canonical_stats' seed is 400.

    Publishing it would put '400+ facilities' on every manifest — a ~47x
    under-claim. The seed is citation-safe, not publication-safe."""
    seed = cs._floor_phrase(cs._FALLBACK["facilities_verified"], step=100)
    pin = asc.PINNED["public"]["facilities"]
    assert seed != pin, (
        "guard-the-guard: the seed has been raised to the pin, so this test can "
        "no longer tell them apart — re-pick the fixture, do not delete the test")
    _cold()
    assert asc.canon_nums()["{canon_facilities}"] == pin


def test_a_warm_but_unmeasured_metric_serves_the_pin(stats_state):
    """A cache full of seeds is not a measurement. stat_is_live() fails closed."""
    cs._cache = dict(cs._FALLBACK)
    cs._cache_ts = 1e18
    cs._live_keys.clear()
    assert asc.canon_nums()["{canon_facilities}"] == asc.PINNED["public"]["facilities"]


@pytest.mark.parametrize("pub_key,phrase_fn", [
    ("facilities", "facilities_verified_phrase"),
    ("countries",  "countries_verified_phrase"),
    ("markets",    "markets_phrase"),
    ("deals",      "deals_phrase"),
])
def test_the_derivation_rounds_exactly_like_the_resolver(stats_state, pub_key, phrase_fn):
    """A second rounding of the same number would be a NEW drift class, inside
    the module that exists to kill drift. The floors must be one implementation."""
    _warm(facilities_verified=18_842, countries_verified=178, markets=306, deals=1_931)
    assert cs.live_public_floors()[pub_key] == getattr(cs, phrase_fn)()


def test_a_measured_floor_that_shrinks_republishes_lower(stats_state):
    """Heals in BOTH directions. max()-against-the-pin would not, and a floor
    stuck above reality is the exact defect that re-floored this metric three
    times in June 2026."""
    _warm(facilities_verified=18_842)
    assert asc.canon_nums()["{canon_facilities}"] == "18,800+"
    _warm(facilities_verified=_SYNTH)
    assert asc.canon_nums()["{canon_facilities}"] == _SYNTH_PHRASE


def test_the_query_except_path_keeps_the_last_known_good(stats_state, monkeypatch):
    """get_canonical_stats() used to revert a measured cache to the static seed
    when _query_live() raised. Harmless while the seed was only a fallback;
    a ~47x under-claim once stat_is_live() lets a publisher serve the cache,
    because _live_keys would still read True over a dict reset to _FALLBACK."""
    _warm(facilities_verified=18_842)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cs, "_query_live", _boom)
    assert cs.get_canonical_stats(force=True)["facilities_verified"] == 18_842
    assert asc.canon_nums()["{canon_facilities}"] == "18,800+"


def test_a_raising_resolver_falls_back_to_the_pin(stats_state, monkeypatch):
    """Fail-soft: canon_text() renders inside routes, so this may never raise."""
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "live_public_floors", _boom)
    assert asc.canon_nums()["{canon_facilities}"] == asc.PINNED["public"]["facilities"]


def test_the_derivation_never_triggers_a_query(stats_state, monkeypatch):
    """canon_text() runs on every agent-facing render. A lapsed TTL must not be
    able to turn one slow query into a slow page on every surface at once."""
    calls = []
    monkeypatch.setattr(cs, "_query_live", lambda: calls.append(1) or dict(cs._FALLBACK))
    _cold()
    asc.canon_nums()
    assert calls == [], "the derivation must peek, never query"


@pytest.mark.parametrize("mode", ["cold", "warm"])
def test_every_public_placeholder_stays_non_empty(stats_state, mode):
    """The failure canon_text() fears most is serving the literal
    '{canon_facilities}' to an agent. Neither branch may resolve to ''."""
    if mode == "cold":
        _cold()
    else:
        _warm(facilities_verified=_SYNTH, countries_verified=178,
              markets=306, deals=1_931)
    nums = asc.canon_nums()
    for ph in ("{canon_facilities}", "{canon_deals}",
               "{canon_markets}", "{canon_countries}"):
        assert nums[ph].strip(), f"{ph} resolved empty in {mode} mode"
