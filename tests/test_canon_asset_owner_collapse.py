"""The canon overlay must heal EVERY pinned public key, and `assets` must be
summed from the same member table /api/v1/infrastructure/stats sums.

★ THE BUG THESE PIN. resolve_public_floors() is the gate every canon consumer
is required to go through, and it applied live values only to a four-name
tuple: ("facilities", "deals", "markets", "countries"). Every other key in
PINNED['public'] was copied through and stamped value_source "pinned" NO
MATTER HOW WELL IT MEASURED. Measured on live /api/v1/canon/phrases
2026-09-19, the partition was exact — and it was on tuple membership, not on
whether anything had been measured:

    live   : facilities, deals, markets, countries
    pinned : assets, substations, fiber_routes, transmission_lines,
             dcpi_countries, dcpi_regions, news_sources

Five of those seven had a working _PUBLIC_FLOOR_SPECS entry AND a live query
behind it. substations was queried from 2026-09-07; fiber_routes and
transmission_lines landed with their queries the same day. All three published
their pin anyway. transmission_lines was the one carrying real drift: pinned
"94,000+" against a live COUNT(*) of 95,569.

`assets` was worse — it had no derivation anywhere, so it could not heal even
in principle: "320,000+" hand-walked on 2026-08-01 against a live
infrastructure_assets_total of 330,961.
"""
import pytest

import ai_surface_canon as canon
import canonical_stats as cstats


# ── the overlay ──────────────────────────────────────────────────────────

def _pin(key):
    return (canon.PINNED.get("public") or {}).get(key)


def _measured(monkeypatch, **vals):
    """Declare `vals` as the floors a real query measured, key -> phrase.

    ★ Tests that monkeypatch resolve_canon() alone prove nothing about the
    label: resolve_canon DEGRADES to the pin, so a key can appear in its
    payload without having been measured. The overlay reads
    _live_public_floors() to tell those apart, so a heal test has to say which
    side of that line its fixture is on.

    ★2026-09-19 LATE-2 — this used to pass `None` for every value, which was
    enough while membership alone decided the LABEL and resolve_canon() supplied
    the VALUE. It is not enough now: for a non-governance key this peek is the
    value, so a fixture that declares a key measured without saying what it
    measured cannot tell a published measurement from a published pin — the
    exact confusion that shipped assets "320,000+"/live."""
    monkeypatch.setattr(canon, "_live_public_floors", lambda: dict(vals))


def test_overlay_heals_a_key_outside_the_governance_four(monkeypatch):
    """The regression. A measured key not named in _PUBLIC_FLOOR_KEYS used to
    publish its pin forever; transmission_lines is the one that was actually
    wrong in production when this was written."""
    pinned = _pin("transmission_lines")
    assert pinned, "transmission_lines must still be a pinned public key"
    raised = "95,000+"
    assert canon._floor_int(raised) > canon._floor_int(pinned), (
        "fixture must RAISE the floor or it proves nothing about the overlay"
    )
    _measured(monkeypatch, transmission_lines=raised)
    # resolve_canon() has NO writer for this key, so in production it returns
    # the PIN here. Feeding it `raised` would let an echo pass the test.
    monkeypatch.setattr(
        canon, "resolve_canon",
        lambda: {"public": {"transmission_lines": pinned}},
    )
    got = canon.resolve_public_floors()
    assert got["transmission_lines"] == raised
    assert got["_source"]["transmission_lines"] == "live"


def test_overlay_heals_assets(monkeypatch):
    pinned = _pin("assets")
    assert pinned, "assets must still be a pinned public key"
    raised = "330,000+"
    assert canon._floor_int(raised) > canon._floor_int(pinned)
    _measured(monkeypatch, assets=raised)
    monkeypatch.setattr(
        canon, "resolve_canon", lambda: {"public": {"assets": pinned}},
    )
    got = canon.resolve_public_floors()
    assert got["assets"] == raised
    assert got["_source"]["assets"] == "live"


def test_overlay_still_refuses_a_live_value_below_the_pin(monkeypatch):
    """Raise-only is the whole safety property: resolve_canon() DEGRADES
    rather than raising, so a live number under the pin is a broken resolver,
    not a shrink. Widening the key set must not widen this hole."""
    pinned = _pin("transmission_lines")
    _measured(monkeypatch, transmission_lines="12,000+")
    monkeypatch.setattr(
        canon, "resolve_canon",
        lambda: {"public": {"transmission_lines": pinned}},
    )
    got = canon.resolve_public_floors()
    assert got["transmission_lines"] == pinned
    assert got["_source"]["transmission_lines"] == "pinned"
    assert any(r.startswith("transmission_lines=") for r in got["_rejected"])


def test_overlay_leaves_a_countless_phrase_alone(monkeypatch):
    """Walking every key means walking keys that are prose. _floor_int()
    returns None for those, and None must mean 'skip', not 'publish'."""
    monkeypatch.setattr(
        canon, "resolve_canon",
        lambda: {"public": {k: "lots and lots" for k in (canon.PINNED.get("public") or {})}},
    )
    got = canon.resolve_public_floors()
    for key, pinned in (canon.PINNED.get("public") or {}).items():
        assert got[key] == pinned
        assert got["_source"][key] == "pinned"


def test_overlay_survives_a_degraded_resolver(monkeypatch):
    def boom():
        raise RuntimeError("no DATABASE_URL")
    # Cold peek pinned explicitly: canonical_stats._live_keys is module state
    # that is never cleared, so a sibling test that warms it would otherwise
    # decide what this one asserts.
    monkeypatch.setattr(canon, "_live_public_floors", dict)
    monkeypatch.setattr(canon, "resolve_canon", boom)
    got = canon.resolve_public_floors()
    for key, pinned in (canon.PINNED.get("public") or {}).items():
        assert got[key] == pinned


# ── the asset total ──────────────────────────────────────────────────────

class _FakeCur:
    """Answers the two statements _measure_member actually issues, by reading
    the SQL it is given. `absent` names tables that do not exist."""

    def __init__(self, counts, absent=()):
        self.counts, self.absent, self._row = counts, set(absent), None

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            table = params[0].split(".", 1)[1]
            self._row = (None if table in self.absent else ("public." + table),)
        elif sql.startswith("SELECT COUNT(*) FROM "):
            self._row = (self.counts[sql.split("FROM ", 1)[1].strip()],)
        else:
            raise AssertionError("unexpected SQL: %r" % sql)

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _asset_members():
    from routes.infrastructure_data_routes import _STATS_MEMBERS
    return [(k, t) for k, t, role in _STATS_MEMBERS if role == "asset"]


def test_asset_total_sums_exactly_the_asset_members():
    """By construction, not by a second list: the sum must follow
    _STATS_MEMBERS, and must EXCLUDE the facility and subset members."""
    from routes.infrastructure_data_routes import _STATS_MEMBERS
    counts, expected = {}, 0
    for i, (key, table, role) in enumerate(_STATS_MEMBERS):
        counts[table] = n = 1000 + i
        if role == "asset":
            expected += n
    assert expected > 0
    got = cstats._measure_asset_total(_FakeCur(counts), _FakeConn())
    assert got == expected


def test_asset_total_excludes_facilities_and_subsets():
    """A guard that only checks the sum would pass if a non-asset member were
    added to it, because every count is positive. Name the exclusion."""
    from routes.infrastructure_data_routes import _STATS_MEMBERS
    non_asset = [t for _k, t, role in _STATS_MEMBERS if role != "asset"]
    assert non_asset, "fixture assumes at least one excluded member"
    counts = {t: 7 for _k, t, _r in _STATS_MEMBERS}
    base = cstats._measure_asset_total(_FakeCur(counts), _FakeConn())
    contaminated = 7 * len(_STATS_MEMBERS)
    assert base is not None and base < contaminated, (
        "asset total must not include %s" % non_asset
    )


@pytest.mark.parametrize("mode", ["absent", "zero"])
def test_asset_total_is_all_or_nothing(mode):
    """A canon PHRASE carries no `members_unmeasured` block, so a partial sum
    is a silent under-claim. One unmeasured member must void the total."""
    from routes.infrastructure_data_routes import _STATS_MEMBERS
    members = _asset_members()
    victim_table = members[0][1]
    counts = {t: 500 for _k, t, _r in _STATS_MEMBERS}
    absent = ()
    if mode == "absent":
        absent = (victim_table,)
    else:
        counts[victim_table] = 0          # 0 is unmeasured, never a count
    assert cstats._measure_asset_total(_FakeCur(counts, absent), _FakeConn()) is None


def test_asset_total_is_absent_not_raising_without_the_routes_module(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_routes(name, *a, **k):
        if name.startswith("routes."):
            raise ImportError("no flask in this process")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_routes)
    assert cstats._measure_asset_total(_FakeCur({}), _FakeConn()) is None


# ── the spec table ───────────────────────────────────────────────────────

def test_assets_has_a_floor_spec_wired_to_the_measured_key():
    stat_key, floor = cstats._PUBLIC_FLOOR_SPECS["assets"]
    assert stat_key == "assets"
    assert floor(330961) == "330,000+", "must round DOWN on a 10k step"
    assert floor(339999) == "330,000+"


def test_assets_floor_step_matches_mcp_facts_export():
    """The two owners must round identically or they disagree in public at
    every boundary. mcp_facts_export floors the same total with step 10000."""
    import mcp_facts_export
    _stat, floor = cstats._PUBLIC_FLOOR_SPECS["assets"]
    for n in (330961, 330000, 329999, 1234567):
        assert floor(n) == mcp_facts_export._floor(n, 10000)


def test_the_assets_seed_does_not_sit_below_the_pin_it_seeds():
    """Same invariant substations failed on 2026-09-07: a cold start must not
    claim more than the module itself believes it has."""
    seed = cstats._FALLBACK["assets"]
    pin = canon._floor_int((canon.PINNED.get("public") or {}).get("assets"))
    assert seed >= pin, "seed %s < pin %s" % (seed, pin)


# ── the label must be earned ─────────────────────────────────────────────

def test_a_degraded_key_is_not_relabelled_live(monkeypatch):
    """★ THE PRODUCTION REGRESSION, 2026-09-19 late.

    resolve_canon() degrades: a key it cannot measure comes back carrying the
    PINNED literal rather than being absent. So live_pub[key] == the pin,
    live_i == pin_i, and the `live_i < pin_i` rejection does not fire. The
    overlay stamped value_source "live" over a hand-walked number — measured in
    production as assets "320,000+"/live against a live 330,961, and
    transmission_lines "94,000+"/live against 95,569.

    A wrong number labelled "pinned" is honest. The same number labelled "live"
    silences ai_surface_sentinel and the frontend heal, which both branch on
    value_source. Nothing measured means nothing relabelled."""
    monkeypatch.setattr(canon, "_live_public_floors", dict)   # nothing measured
    for key, pinned in (canon.PINNED.get("public") or {}).items():
        if key in canon._PUBLIC_FLOOR_KEYS:
            continue
        monkeypatch.setattr(
            canon, "resolve_canon", lambda: {"public": dict(canon.PINNED["public"])},
        )
        got = canon.resolve_public_floors()
        assert got["_source"][key] == "pinned", (
            "%s was relabelled live off a degraded payload" % key
        )
        assert got[key] == pinned


def test_a_measured_key_still_heals_after_the_narrowing(monkeypatch):
    """The fix must not pin the keys it was written to free."""
    pinned = _pin("transmission_lines")
    raised = "95,000+"
    assert canon._floor_int(raised) > canon._floor_int(pinned)
    _measured(monkeypatch, transmission_lines=raised)
    monkeypatch.setattr(
        canon, "resolve_canon", lambda: {"public": {"transmission_lines": pinned}},
    )
    got = canon.resolve_public_floors()
    assert got["transmission_lines"] == raised
    assert got["_source"]["transmission_lines"] == "live"


def test_the_governance_four_keep_healing_without_a_floors_witness(monkeypatch):
    """The narrowing is deliberately NOT applied to the governance four.

    Their resolvers predate _live_public_floors() and do not all report through
    it — markets, countries and deals reach resolve_canon() by other paths — so
    requiring a floors witness for them would re-pin the keys that have been
    healing correctly for months. This is the conservative half of the fix and
    nothing else pins it: a mutation that drops the `key not in
    _PUBLIC_FLOOR_KEYS` clause otherwise passes the whole suite."""
    monkeypatch.setattr(canon, "_live_public_floors", dict)   # no witness at all
    pinned = _pin("facilities")
    raised = "%s0,000+" % (canon._floor_int(pinned) // 10000 + 1)
    assert canon._floor_int(raised) > canon._floor_int(pinned)
    monkeypatch.setattr(
        canon, "resolve_canon", lambda: {"public": {"facilities": raised}},
    )
    got = canon.resolve_public_floors()
    assert got["facilities"] == raised
    assert got["_source"]["facilities"] == "live"


# ── the label must be earned BY THE VALUE, not by a sibling key ──────────

def test_a_measured_key_publishes_the_measurement_not_the_resolver_echo(monkeypatch):
    """★ THE RESIDUAL HOLE, 2026-09-19 late-2.

    Gating the label on membership in _live_public_floors() and then publishing
    live_pub[key] checks the REFERENCE, not the DERIVATION: it confirms a query
    measured the key, then ships a number that did not come from that query.

    resolve_canon() assigns c["public"][...] for five keys only — deals,
    facilities, markets, countries, news_sources. `assets`, `substations`,
    `fiber_routes` and `transmission_lines` have no writer into it, so live_pub
    carries the PIN for them no matter how warm canonical_stats is. And it IS
    warm on this path: resolve_canon() calls countries_verified_phrase() ->
    get_canonical_stats(), which populates _live_keys before the peek runs. So
    membership passes, live_i == pin_i, raise-only does not fire, and the pin
    ships stamped "live" — assets "320,000+" against a measured 330,961.

    The fixture is the production shape: measured, and resolve_canon echoing the
    pin. Publishing the pin here must not be reachable."""
    for key, measured in (("assets", "330,000+"), ("transmission_lines", "95,000+")):
        pinned = _pin(key)
        assert canon._floor_int(measured) > canon._floor_int(pinned), (
            "%s fixture must outrank its pin or it proves nothing" % key
        )
        _measured(monkeypatch, **{key: measured})
        monkeypatch.setattr(
            canon, "resolve_canon",
            lambda: {"public": dict(canon.PINNED["public"])},   # degrades to the pin
        )
        got = canon.resolve_public_floors()
        assert got[key] == measured, (
            "%s published %r — the pin echoed back by a degraded resolver, "
            "not the measurement its 'live' label claims" % (key, got[key])
        )
        assert got["_source"][key] == "live"


def test_an_unmeasured_key_is_pinned_even_when_the_resolver_offers_a_raise(monkeypatch):
    """The cold-worker control, and the other half of the same rule.

    On the first request in a fresh worker the peek is empty. A resolver value
    that would RAISE the floor is still not publishable, because nothing has
    measured it — raise-only answers "is this sane", never "was this measured"."""
    pinned = _pin("assets")
    monkeypatch.setattr(canon, "_live_public_floors", dict)      # cold worker
    monkeypatch.setattr(
        canon, "resolve_canon", lambda: {"public": {"assets": "990,000+"}},
    )
    got = canon.resolve_public_floors()
    assert got["assets"] == pinned
    assert got["_source"]["assets"] == "pinned"


def test_a_measurement_publishes_even_when_the_resolver_blows_up(monkeypatch):
    """The peek does not run through resolve_canon(), so a resolver that raises
    must not strand a floor that canonical_stats already measured. This is the
    direction the pin-as-cold-start contract promises and nothing else pins."""
    measured = "330,000+"
    def boom():
        raise RuntimeError("no DATABASE_URL")
    _measured(monkeypatch, assets=measured)
    monkeypatch.setattr(canon, "resolve_canon", boom)
    got = canon.resolve_public_floors()
    assert got["assets"] == measured
    assert got["_source"]["assets"] == "live"


def test_no_public_key_can_be_live_while_holding_its_pin_unmeasured(monkeypatch):
    """The general invariant, stated once over every public key.

    Whatever the resolver returns, a key may only read "live" if the peek
    measured it. This is what ai_surface_sentinel and the frontend heal branch
    on, so it is the property that has to hold key-wise, not just for the two
    that happened to drift."""
    monkeypatch.setattr(canon, "_live_public_floors", dict)
    monkeypatch.setattr(
        canon, "resolve_canon", lambda: {"public": dict(canon.PINNED["public"])},
    )
    got = canon.resolve_public_floors()
    for key in (canon.PINNED.get("public") or {}):
        if key in canon._PUBLIC_FLOOR_KEYS:
            continue
        assert got["_source"][key] == "pinned", "%s claimed live unmeasured" % key


def test_the_governance_four_prefer_their_resolver_over_the_peek(monkeypatch):
    """The governance four have TWO derivations, and this one is not authority.

    _PUBLIC_FLOOR_SPECS derives facilities from `facilities_verified` at
    step=100, while resolve_canon() derives it from /api/v1/stats — and
    countries is required to be measured on the SAME table as the facility
    count it is paired with (see resolve_canon()). So when both are available
    the dedicated resolver wins, and swapping that preference is a silent
    change of which population the headline numbers describe.

    ★ Its sibling test only covers the case where there is NO floors witness,
    which leaves the branch unpinned exactly when a witness exists: a mutation
    making these four read the peek whenever it has them survived the whole
    suite when this was written."""
    for key in canon._PUBLIC_FLOOR_KEYS:
        pinned = _pin(key)
        if not pinned or canon._floor_int(pinned) is None:
            continue
        pin_i = canon._floor_int(pinned)
        from_resolver = f"{pin_i + 1000:,}+"
        from_peek = f"{pin_i + 900000:,}+"       # would also pass raise-only
        assert canon._floor_int(from_peek) > canon._floor_int(from_resolver)
        _measured(monkeypatch, **{key: from_peek})
        monkeypatch.setattr(
            canon, "resolve_canon", lambda: {"public": {key: from_resolver}},
        )
        got = canon.resolve_public_floors()
        assert got[key] == from_resolver, (
            "%s published %r — the _PUBLIC_FLOOR_SPECS peek, not the dedicated "
            "resolver that governs it" % (key, got[key])
        )
        assert got["_source"][key] == "live"
