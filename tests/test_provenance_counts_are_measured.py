"""verification_counts must be MEASURED, never canonical_stats' seed floors.

★2026-09-01. routes/provenance.facility_verification_counts() published
canonical_stats' static `_FALLBACK` seeds as if they were counts. With no
database it returned {'verified': 400, 'tracked': 21000} — in the response,
indistinguishable from a real measurement.

These are published numbers: the MCP server instructs agents to cite them as
"N analyst-verified of M tracked facilities", and dchub-mcp-server
lib/result-shaping.mjs renders them as "400/21,000 verified". A transient DB
outage would therefore put a ~48x under-claim on `verified` into an agent's
answer as a hard fact.

The seeds are citation-SAFE for the *_phrase() helpers, which round DOWN and
print "400+" — a floor that announces itself as a floor. A bare integer in
`verification_counts` announces itself as a count.

★★ The subtle half, and why this file also guards canonical_stats: of the five
metrics `_query_live()` measures, `facilities` was the ONLY one that set its
value without calling `_live_keys.add()`. So `stat_is_live("facilities")` read
False even immediately after a successful COUNT. Gating on liveness without
fixing that would have suppressed a correct, measured number on three live
surfaces FOREVER — trading a wrong count for a permanently missing one, which
is not the trade this fix is making. Test 6 pins that marker.

House rules: no import of main.py. canonical_stats and routes/provenance are
stdlib-only at import time, so both are imported directly. Nothing at module
scope. Module globals are saved/restored — a test that left the cache warm
would make every later test in the session read as 'measured'.
"""
import pytest

import canonical_stats as cs
import routes.provenance as prov
from routes.provenance import facility_verification_counts

# Measured values deliberately far from any seed, so 'the measurement won'
# is assertable without pinning a literal that drifts with the data.
_MEASURED_TRACKED = 27_924
_MEASURED_VERIFIED = 20_157


@pytest.fixture
def stats_state():
    """Save/restore the canonical_stats cache + liveness set (module globals).
    Mirrors tests/test_canon_floor_derivation.py::stats_state."""
    prev_cache, prev_ts = cs._cache, cs._cache_ts
    prev_live = set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _warm(**metrics):
    """Populate the cache as a real query would, and mark those metrics live."""
    snap = dict(cs._FALLBACK)
    snap.update(metrics)
    cs._cache, cs._cache_ts = snap, 1e18      # far future: never lapses
    cs._live_keys.update(metrics.keys())


def _cold():
    cs._cache, cs._cache_ts = None, 0.0
    cs._live_keys.clear()


# ── guard the guard ─────────────────────────────────────────────────────────

def test_the_seeds_differ_from_the_measured_fixtures():
    """If a seed were ever raised to the fixture value, every test below would
    stop being able to tell a measurement from a floor — and would pass
    vacuously. Re-pick the fixture; do not delete the test."""
    assert cs._FALLBACK["facilities"] != _MEASURED_TRACKED
    assert cs._FALLBACK["facilities_verified"] != _MEASURED_VERIFIED


# ── 1. the defect: seeds must never be published ────────────────────────────

def test_a_cold_cache_publishes_nothing(stats_state):
    """No measurement has ever been taken -> omit the field entirely."""
    _cold()
    assert facility_verification_counts() is None


def test_a_cache_full_of_seeds_publishes_nothing(stats_state):
    """★ The dangerous branch. The cache is WARM and complete, so a naive
    `if v and t` reads it as data — but every value in it is a hand-typed
    seed. This is exactly the shape the no-DB path produced."""
    cs._cache, cs._cache_ts = dict(cs._FALLBACK), 1e18
    cs._live_keys.clear()

    out = facility_verification_counts()

    assert out is None, (
        f"published {out} — those are canonical_stats._FALLBACK seeds, not "
        "counts. An agent would cite 400 analyst-verified as fact.")


def test_the_seed_pair_is_never_published_while_unmeasured(stats_state):
    """Belt and braces over both unmeasured shapes: never taken, and taken
    but never marked. Catches a 'helpful' floor re-added downstream."""
    seeds = {"verified": int(cs._FALLBACK["facilities_verified"]),
             "tracked": int(cs._FALLBACK["facilities"])}
    for setup in (_cold, lambda: _warm()):
        setup()
        assert facility_verification_counts() != seeds


def test_a_measurement_that_equals_the_seed_still_publishes(stats_state):
    """★ The gate keys on PROVENANCE (was this measured?), never on the value.

    The tempting cheap fix is to blocklist the seed numbers. That is wrong in
    both directions: it suppresses a genuine count that happens to land on
    21,000, and it silently stops working the moment someone re-floors the
    seed. A measured 21,000 is a fact and must publish; an unmeasured 21,000
    is a guess and must not. Only _live_keys can tell them apart."""
    _warm(facilities=cs._FALLBACK["facilities"],
          facilities_verified=cs._FALLBACK["facilities_verified"])
    assert facility_verification_counts() == {
        "verified": int(cs._FALLBACK["facilities_verified"]),
        "tracked": int(cs._FALLBACK["facilities"])}


# ── 2. a real measurement still publishes (this is not a silent drop) ───────

def test_a_measured_pair_publishes(stats_state):
    """The fix must not become 'omit always' — that trades a wrong number for
    a missing one on three live surfaces."""
    _warm(facilities=_MEASURED_TRACKED, facilities_verified=_MEASURED_VERIFIED)
    assert facility_verification_counts() == {
        "verified": _MEASURED_VERIFIED, "tracked": _MEASURED_TRACKED}


# ── 3. partial measurement fails CLOSED ─────────────────────────────────────

@pytest.mark.parametrize("live_key,other_key", [
    ("facilities_verified", "facilities"),
    ("facilities", "facilities_verified"),
])
def test_one_measured_one_seeded_publishes_nothing(stats_state, live_key,
                                                   other_key):
    """`tracked` and `verified` are separate queries with separate except
    paths, so one can be live while the other is still the seed. Publishing
    that pair would be a RATIO between a measurement and a constant."""
    _warm(**{live_key: 99_999})
    cs._live_keys.discard(other_key)     # the other stays seeded + unmeasured
    assert facility_verification_counts() is None


# ── 4. the liveness marker this fix depends on ──────────────────────────────

def test_query_live_marks_the_tracked_count_as_live(stats_state, monkeypatch):
    """★ Without `_live_keys.add("facilities")` the gate above suppresses a
    correct number forever. Exercised through the REAL _query_live() against a
    stub connection — an AST check would not prove the marker actually runs."""
    _cold()
    monkeypatch.setattr(cs, "_conn", lambda: _StubConn())

    cs._query_live()

    assert cs.stat_is_live("facilities"), (
        "the tracked COUNT succeeded but never marked itself live — "
        "facility_verification_counts() will now omit the field on every "
        "response, forever. Restore _live_keys.add('facilities').")
    assert cs.stat_is_live("facilities_verified")


def test_the_marker_reaches_the_publisher(stats_state, monkeypatch):
    """End to end: a live query makes the counts publishable again."""
    _cold()
    monkeypatch.setattr(cs, "_conn", lambda: _StubConn())
    cs._cache, cs._cache_ts = cs._query_live(), 1e18

    out = facility_verification_counts()

    assert out == {"verified": 4_242, "tracked": 4_242}, out


# ── stub connection (class definitions only; nothing executes at import) ────

class _StubCursor:
    """Every metric in _query_live() is individually try/except-wrapped, so a
    cursor that answers every query with the same scalar is safe."""

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return [4_242]

    def fetchall(self):
        return [[4_242]]

    def close(self):
        return None


class _StubConn:
    def cursor(self):
        return _StubCursor()

    def close(self):
        return None
