"""Guard: a PROVISIONAL canon refresh must not evict a known-good body.

2026-09-12. /api/v1/canon/phrases oscillated between its live value and its
pinned floor. Measured from outside, minutes apart, same URL shape:

    cold=False  facilities 21,600+  source "resolve_public_floors (live)"
    cold=True   facilities 21,500+  source "resolve_public_floors (cold: PINNED floors)"

Downstream that is not cosmetic. This endpoint is the ONE source the nightly
frontend heal, llms.txt, the registry manifests and the CF zone worker read, and
the zone stores /api/v1/* with override_origin — measured MISS then HIT on the
same URL. So a single cold answer is snapshotted at the edge and every surface
rendered during that window bakes in the floor: on 2026-09-12 /llms.txt,
/AGENTS.md, /openapi.json and the MCP server card all served 21,500+ while this
origin answered 21,600+, and the server card carried BOTH in one document.

_cached_body() already refuses to lose a good body to a blip — but only when the
builder RAISES. When resolve_public_floors_cached() is merely cold it RETURNS,
successfully, a body full of pinned floors, and that return overwrote the good
one. Same intent, one branch short.

These tests pin the fix:
  1. a provisional refresh does NOT replace a known-good body;
  2. it does not do so indefinitely either — past the grace window the
     provisional truth is served, so a real outage cannot hide behind an old
     number;
  3. with no prior good body (a genuinely cold process) the provisional body is
     served, because something floored beats 503;
  4. a good body always replaces a provisional one at once;
  5. the covering path is observable, not silent.

No network: the builder is a stub and the clock is injected.
"""
import importlib
import sys

import pytest

sys.path.insert(0, ".")
cp = importlib.import_module("routes.canon_phrases")


GOOD = {
    "ok": True, "cold": False, "degraded": [],
    "facilities": "21,600+", "deals": "2,100+", "markets": "300+", "countries": "170+",
    "tools": 90,
    "value_source": {"facilities": "live", "deals": "live", "markets": "live", "countries": "live"},
    "source": "resolve_public_floors (live)",
}
PROVISIONAL = {
    "ok": True, "cold": True, "degraded": [],
    "facilities": "21,500+", "deals": "2,100+", "markets": "300+", "countries": "170+",
    "tools": 90,
    "value_source": {"facilities": "pinned", "deals": "pinned", "markets": "pinned", "countries": "pinned"},
    "source": "resolve_public_floors (cold: PINNED floors)",
}


# routes/canon_phrases.py serves a pinned floor in THREE shapes, and `cold` is
# set on only one of them — the distinction dchub-frontend#1424 was written for.
# Covering only the cold one would leave the two that bit hardest uncovered.
PINNED_FALLBACK = dict(PROVISIONAL, cold=False,
                       source="PINNED (fallback)")
DEGRADED = dict(GOOD, degraded=["facilities"],
                facilities="21,500+",
                value_source={"facilities": "pinned", "deals": "live",
                              "markets": "live", "countries": "live"},
                source="resolve_public_floors (DEGRADED: facilities)")


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts from an empty memo and restores the module's clock."""
    saved = dict(cp._cache)
    real_time = cp.time.time
    cp._cache.update({"at": 0.0, "body": None, "ttl": cp._CACHE_TTL_S})
    cp._cache.pop("good_at", None)
    yield
    cp.time.time = real_time
    cp._cache.clear()
    cp._cache.update(saved)


def _clock(t):
    cp.time.time = lambda: t


def test_the_fixtures_are_what_this_guard_thinks_they_are():
    """If the discriminator stops seeing these as good/provisional, every
    assertion below is vacuous — so it is checked, not assumed."""
    assert cp._is_provisional(GOOD) is False, "the GOOD fixture does not read as live"
    assert cp._is_provisional(PROVISIONAL) is True, "the PROVISIONAL fixture does not read as pinned"


def test_a_provisional_refresh_does_not_evict_a_good_body():
    _clock(1000.0)
    body, _ = cp._cached_body(lambda: GOOD)
    assert body["facilities"] == "21,600+"

    # TTL expires; the refresh comes back COLD, as it does on a fresh process.
    _clock(1000.0 + cp._CACHE_TTL_S + 1)
    body, _ = cp._cached_body(lambda: PROVISIONAL)
    assert body["facilities"] == "21,600+", (
        "a cold refresh replaced the known-good canon with its pinned floor; every "
        "surface rendered in that window publishes the lower number"
    )
    assert cp._is_provisional(body) is False


def test_the_cover_is_bounded_so_an_outage_cannot_hide():
    _clock(2000.0)
    cp._cached_body(lambda: GOOD)
    grace = cp._GOOD_BODY_GRACE_S
    # Just inside the window: still covered.
    _clock(2000.0 + grace - 1)
    assert cp._cached_body(lambda: PROVISIONAL)[0]["facilities"] == "21,600+"
    # Past it: the provisional truth is served rather than an indefinitely old one.
    # ★ The step must clear the PROVISIONAL TTL too. Covering re-arms the memo for
    #  _PROVISIONAL_TTL_S, so a clock nudge of a second or two never reaches the
    #  builder at all and the assertion reads the still-cached good body — the
    #  test passes or fails on the memo, not on the grace bound it is named for.
    _clock(2000.0 + grace + cp._PROVISIONAL_TTL_S + 2)
    body, _ = cp._cached_body(lambda: PROVISIONAL)
    assert body["facilities"] == "21,500+", (
        f"a good body older than the {grace}s grace still covered a provisional "
        "refresh — a real canon outage would stay invisible"
    )


def test_a_cold_process_with_no_good_body_still_answers():
    _clock(3000.0)
    body, _ = cp._cached_body(lambda: PROVISIONAL)
    assert body is not None and body["facilities"] == "21,500+", (
        "with nothing better to serve, a floored answer must still be served — "
        "503 is worse than a floor"
    )


def test_a_good_body_replaces_a_provisional_one_immediately():
    _clock(4000.0)
    cp._cached_body(lambda: PROVISIONAL)
    _clock(4000.0 + cp._PROVISIONAL_TTL_S + 1)
    body, _ = cp._cached_body(lambda: GOOD)
    assert body["facilities"] == "21,600+"
    assert cp._cache["ttl"] == cp._CACHE_TTL_S, "a good body must get the full TTL back"


def test_covering_is_observable():
    """A number that silently differs from what the builder just returned is
    exactly the kind of thing that costs hours to diagnose."""
    _clock(5000.0)
    cp._cached_body(lambda: GOOD)
    _clock(5000.0 + cp._CACHE_TTL_S + 1)
    cp._cached_body(lambda: PROVISIONAL)
    assert cp._cache.get("covering") is True, (
        "the memo does not record that it is serving a good body over a provisional "
        "refresh, so the route cannot surface it and nobody can see it happening"
    )


@pytest.mark.parametrize("shape,name", [
    ("PROVISIONAL", "cold:true"),
    ("PINNED_FALLBACK", "PINNED (fallback) — cold is FALSE here"),
    ("DEGRADED", "one rejected key, live siblings"),
])
def test_every_provisional_shape_is_covered(shape, name):
    """`cold` is set on only one of the three provisional exits. A fix that
    reads it alone leaves the other two evicting good bodies exactly as before."""
    body_in = globals()[shape]
    assert cp._is_provisional(body_in) is True, (
        f"{name}: this shape does not read as provisional, so nothing below is tested"
    )
    _clock(6000.0)
    cp._cached_body(lambda: GOOD)
    _clock(6000.0 + cp._CACHE_TTL_S + 1)
    served, _ = cp._cached_body(lambda: body_in)
    assert served["facilities"] == "21,600+", (
        f"{name}: this shape replaced the known-good canon with its floor"
    )

