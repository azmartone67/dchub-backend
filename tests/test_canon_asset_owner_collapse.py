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
    monkeypatch.setattr(
        canon, "resolve_canon",
        lambda: {"public": {"transmission_lines": raised}},
    )
    got = canon.resolve_public_floors()
    assert got["transmission_lines"] == raised
    assert got["_source"]["transmission_lines"] == "live"


def test_overlay_heals_assets(monkeypatch):
    pinned = _pin("assets")
    assert pinned, "assets must still be a pinned public key"
    raised = "330,000+"
    assert canon._floor_int(raised) > canon._floor_int(pinned)
    monkeypatch.setattr(
        canon, "resolve_canon", lambda: {"public": {"assets": raised}},
    )
    got = canon.resolve_public_floors()
    assert got["assets"] == raised
    assert got["_source"]["assets"] == "live"


def test_overlay_still_refuses_a_live_value_below_the_pin(monkeypatch):
    """Raise-only is the whole safety property: resolve_canon() DEGRADES
    rather than raising, so a live number under the pin is a broken resolver,
    not a shrink. Widening the key set must not widen this hole."""
    pinned = _pin("transmission_lines")
    monkeypatch.setattr(
        canon, "resolve_canon",
        lambda: {"public": {"transmission_lines": "12,000+"}},
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
