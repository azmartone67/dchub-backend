"""A PROVISIONAL canon body must not be memoized for the full TTL.

/api/v1/canon/phrases answers ok:true while serving PINNED floors — on a cold
process, on a resolver fallback, and on a DEGRADED resolve. Those are safe to
READ (a floor is under-stated, never wrong-direction) but this endpoint is the
one source the nightly frontend heal, llms.txt, the registry manifests and the
CF zone worker all pull from, so the longer the origin keeps ANSWERING cold, the
more chances the edge has to snapshot a cold body.

MEASURED 2026-09-09: cf-cache-status HIT, age 1741, serving facilities "20,700+"
while this origin was already warm at "21,200+". The floors resolver itself warms
in 7.6-15.5s; the 300s memo was turning that ~10s condition into a 5-minute one.

★ The discriminator reads ONLY the floor keys. substations / fiber_routes /
transmission_lines / assets are "pinned" on a perfectly WARM bundle by design —
they are not in _PUBLIC_FLOOR_KEYS, so the overlay can never mark them live.
Treating them as evidence would make EVERY body look provisional and pin the
short TTL on forever, which is why the warm case below is as load-bearing as the
cold ones.
"""
import routes.canon_phrases as cp


_FLOOR_LIVE = {
    "facilities": "live", "deals": "live", "markets": "live", "countries": "live",
    # pinned BY DESIGN even when warm — not floor keys
    "substations": "pinned", "fiber_routes": "pinned",
    "transmission_lines": "pinned", "assets": "pinned",
}
_ALL_PINNED = {k: "pinned" for k in _FLOOR_LIVE}

WARM = {"ok": True, "cold": False, "degraded": [], "value_source": _FLOOR_LIVE}
COLD = {"ok": True, "cold": True, "degraded": [], "value_source": _ALL_PINNED}
FALLBACK = {"ok": True, "cold": False, "degraded": [], "value_source": _ALL_PINNED}
DEGRADED = {"ok": True, "cold": False, "degraded": ["facilities=400<21200"],
            "value_source": dict(_FLOOR_LIVE, facilities="pinned")}
NO_PROVENANCE = {"ok": True, "cold": False, "degraded": []}


def _reset():
    cp._cache["at"] = 0.0
    cp._cache["body"] = None
    cp._cache["ttl"] = cp._CACHE_TTL_S


def test_warm_body_is_not_provisional():
    assert cp._is_provisional(WARM) is False


def test_every_provisional_shape_is_caught():
    # cold marks only the first of these three
    assert cp._is_provisional(COLD) is True
    assert cp._is_provisional(FALLBACK) is True
    assert cp._is_provisional(DEGRADED) is True
    assert cp._is_provisional(NO_PROVENANCE) is True
    assert cp._is_provisional(None) is True


def test_cache_holds_a_provisional_body_only_briefly():
    _reset()
    body, cached = cp._cached_body(lambda: COLD)
    assert body is COLD and cached is False
    assert cp._cache["ttl"] == cp._PROVISIONAL_TTL_S


def test_cache_holds_a_warm_body_for_the_full_ttl():
    _reset()
    body, cached = cp._cached_body(lambda: WARM)
    assert body is WARM and cached is False
    assert cp._cache["ttl"] == cp._CACHE_TTL_S


def test_a_cold_memo_expires_and_lets_a_warm_body_through():
    """The point of the whole change: the origin stops answering cold quickly."""
    _reset()
    cp._cached_body(lambda: COLD)
    assert cp._cache["ttl"] == cp._PROVISIONAL_TTL_S

    # still inside the short window -> served from the memo, no rebuild
    calls = []

    def _builder():
        calls.append(1)
        return WARM

    cp._cached_body(_builder)
    assert calls == [], "a provisional body inside its TTL should still be memoized"

    # age it past the SHORT ttl but well inside the full one, and it rebuilds
    cp._cache["at"] -= (cp._PROVISIONAL_TTL_S + 1)
    body, cached = cp._cached_body(_builder)
    assert calls == [1], "the short TTL must expire long before _CACHE_TTL_S"
    assert body is WARM and cached is False
    assert cp._cache["ttl"] == cp._CACHE_TTL_S


def test_a_warm_body_is_still_held_past_the_short_ttl():
    """Non-vacuity: the short TTL must apply to provisional bodies ONLY."""
    _reset()
    cp._cached_body(lambda: WARM)
    cp._cache["at"] -= (cp._PROVISIONAL_TTL_S + 1)
    calls = []

    def _builder():
        calls.append(1)
        return COLD

    body, cached = cp._cached_body(_builder)
    assert calls == [], "a warm body must survive well past the provisional TTL"
    assert body is WARM and cached is True
