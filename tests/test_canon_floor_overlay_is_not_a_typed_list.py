"""The floor overlay must publish EVERY measured key, not a typed few.

#4878 gave resolve_canon() the asset floors it never had a writer for, as:

    for _akey in ("substations", "fiber_routes", "transmission_lines", "assets"):

That is the same shape as the bug it fixed. `_PUBLIC_FLOOR_KEYS` was a four-name
tuple; `substations` gained a _PUBLIC_FLOOR_SPECS entry on 2026-09-02 and
`fiber_routes`/`transmission_lines` gained one with their queries on 2026-09-07,
and every one of them published its PIN until 2026-09-19 because nobody extended
the tuple. A second tuple re-arms that for whichever key is added next.

`dcpi_countries` is the proof it is already re-armed: it has a
_PUBLIC_FLOOR_SPECS entry, it has no dedicated resolver in resolve_canon(), and
it is absent from #4878's tuple. These tests call resolve_canon() FOR REAL —
stubbing its network doors, not the function — because monkeypatching
resolve_canon is precisely why its own body went unpinned.
"""
import pytest

import ai_surface_canon as canon
import canonical_stats as cstats


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _no_get(path, timeout=15):
        raise AssertionError("unstubbed network: _get(%r)" % path)
    def _no_tools(timeout=20):
        raise AssertionError("unstubbed network: mcp probe")
    monkeypatch.setattr(canon, "_get", _no_get)
    monkeypatch.setattr(canon, "_mcp_tool_count", _no_tools)
    monkeypatch.setattr(canon, "_mcp_tool_names", _no_tools)
    monkeypatch.setattr(canon, "_mcp_server_version", lambda *a, **k: "")
    monkeypatch.setattr(canon, "_adopt_live_version", lambda *a, **k: None)
    import routes.claim_ledger as _cl
    monkeypatch.setattr(_cl, "register_canon_claims", lambda *a, **k: None)


def _pin(key):
    return (canon.PINNED.get("public") or {}).get(key)


def _public(monkeypatch, floors):
    monkeypatch.setattr(canon, "_live_public_floors", lambda: dict(floors))
    return (canon.resolve_canon() or {}).get("public") or {}


# ── the generalisation ───────────────────────────────────────────────────

def test_a_measured_key_outside_the_old_tuple_now_publishes(monkeypatch):
    """★ THE REFACTOR. dcpi_countries is measured, has no resolver, and was not
    in #4878's tuple — so under that code it published its pin forever."""
    key = "dcpi_countries"
    assert key in cstats._PUBLIC_FLOOR_SPECS, "fixture assumes a measured key"
    pinned = _pin(key)
    assert pinned, "%s must still be a pinned public key" % key
    measured = "999+"
    assert measured != pinned, "fixture equals the pin — would pass on a no-op"
    got = _public(monkeypatch, {key: measured})
    assert got[key] == measured, (
        "%s published %r — the overlay is still gated on a typed key list, so "
        "the next _PUBLIC_FLOOR_SPECS entry will publish its pin too"
        % (key, got[key])
    )


def test_every_measured_spec_key_can_reach_public(monkeypatch):
    """Stated over the SPECS rather than over a list typed here, so adding a
    spec cannot leave this test behind either."""
    resolver_owned = {"deals", "facilities", "markets", "countries", "news_sources"}
    probes, expected = {}, {}
    for i, key in enumerate(sorted(cstats._PUBLIC_FLOOR_SPECS)):
        if key in resolver_owned or not _pin(key):
            continue
        probes[key] = expected[key] = "%d,000+" % (901 + i)
    assert probes, "no non-resolver spec keys to probe"
    got = _public(monkeypatch, probes)
    for key, value in expected.items():
        assert got[key] == value, "%s did not reach public" % key


# ── precedence, which is what makes the list unnecessary ─────────────────

def test_a_dedicated_resolver_still_outranks_the_peek(monkeypatch):
    """The overlay moved ABOVE the five resolvers, so they overwrite their own
    keys. If it were applied after them instead, the peek would silently decide
    which table `countries` describes."""
    from_resolver, from_peek = "188+", "999+"
    monkeypatch.setattr(cstats, "countries_verified_phrase", lambda: from_resolver)
    got = _public(monkeypatch, {"countries": from_peek})
    assert got["countries"] == from_resolver


def test_a_failed_resolver_falls_back_to_the_measurement_not_the_pin(monkeypatch):
    """The other half of ordering: a measurement beats a cold pin. The pin is
    documented as a COLD-START floor, so it is the last resort, not the second."""
    def boom():
        raise RuntimeError("no DATABASE_URL")
    monkeypatch.setattr(cstats, "news_sources_phrase", boom)
    measured = "9,000+"
    assert measured != _pin("news_sources")
    got = _public(monkeypatch, {"news_sources": measured})
    assert got["news_sources"] == measured


# ── the <key>_live marker contract #4878 introduced ──────────────────────

def test_the_peek_owns_a_marker_only_for_the_keys_it_published(monkeypatch):
    """substations_live has consumers outside this module, so the marker is a
    contract. It must follow the VALUE: a key a resolver overwrote is not the
    peek's to claim."""
    monkeypatch.setattr(cstats, "countries_verified_phrase", lambda: "188+")
    c = None
    monkeypatch.setattr(canon, "_live_public_floors",
                        lambda: {"substations": "128,000+", "countries": "999+"})
    c = canon.resolve_canon() or {}
    assert c.get("substations_live") == "128,000+"
    assert c.get("countries_live") is None, (
        "the peek claimed a marker for a key the resolver owns"
    )


def test_the_peek_never_retypes_an_int_marker(monkeypatch):
    """★ facilities_live and markets_live are raw INTS from /api/v1/stats and
    ai_surface_sentinel reads them. Writing a floor PHRASE over them would
    change their type under a consumer that never asked for a phrase."""
    monkeypatch.setattr(canon, "_get", lambda p, timeout=15: {"facilities": 22931,
                                                              "markets": 314})
    monkeypatch.setattr(canon, "_live_public_floors",
                        lambda: {"facilities": "22,900+", "markets": "300+"})
    c = canon.resolve_canon() or {}
    for marker in ("facilities_live", "markets_live"):
        assert isinstance(c.get(marker), int), (
            "%s is %r — the peek retyped an int marker a consumer reads"
            % (marker, c.get(marker))
        )
