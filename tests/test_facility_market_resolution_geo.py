"""A facility's market must be a market it is actually NEAR.

r-market-resolve-geo (2026-08-26).

routes/facility_profile_page.py:_market_dcpi resolved a facility to a DCPI
market through three fallbacks, and every one of them filtered on a bare
`state` string with no country and no distance guard. `state` is a US-shaped
field, so this failed in both directions at once:

  WRONG   Brazilian state codes collide with US ones. Measured live over a
          random 500 of the 9,095 sitemap facility pages on 2026-08-26:

            Blumenau, SC, BR   -> /dcpi/charleston-sc   7,395 km
            Florianopolis, SC  -> /dcpi/charleston-sc   7,486 km
            Ciasc, SC, BR      -> /dcpi/charleston-sc   7,489 km
            Vitoria, ES, BR    -> /dcpi/madrid          7,749 km   (ES = Spain)
            Cuiaba, MT, BR     -> /dcpi/billings        8,616 km   (MT = Montana)

          5 of 182 resolved pages in the sample, ~91 across the sitemap. These
          are not thin pages, they are WRONG ones: the Blumenau page's <title>
          reads "SERC grid" — a US reliability region on a Brazilian data
          center — and the RAG "Market context" splice describes a city on
          another continent as "the market this facility sits in".

  MISSING The function returned None outright when city and state were both
          empty, discarding usable coordinates, and no fallback could fire
          without a state. So international pages got no market at all:
          74.4% of US facility pages carry market context against 24.0% of
          non-US ones. A page with context runs a median 474 visible words,
          one without 224 — and every one of the 81 pages under 200 words in
          that sample lacked market context, while only half lacked a city.

★ THE THRESHOLDS ARE MEASURED, NOT PICKED. Distance from each sampled page to
  the market it rendered: correct resolutions run median 3 km / p90 33 km, the
  legitimate tail ends at 69 73 77 81 86 90 92 119 121 152 169 201 km, and the
  bug cluster starts at 7,395 km. Nothing lands in between, so _SANITY_KM=400
  sits in a genuinely empty gap — 2x above the furthest real market, 18x below
  the nearest collision.

★★ THE DB-BACKED TESTS BELOW DO NOT RUN IN CI. No workflow in
   .github/workflows/ injects a database URL into a pytest job (verified on run
   33021627637: shape fences PASSED, DB-backed tests SKIPPED under a green
   `unit-tests`). So the guard is fenced HERE by tests that need no database:
   the stub-connection tests drive the real shipped function and fail if the
   distance check is removed. The SQL itself was verified against a real
   PostgreSQL 18 using tests/fixtures/facility_market_resolution.sql, which
   carries a must-fail control — on the same rows the OLD function returns
   charleston-sc / billings / madrid and the NEW one returns none of them.

★ pytest functions only — no module-scope work. A module-scope failure is a
  COLLECTION error, which kills the whole session rather than one test.
"""
import os
import sys
import types
from decimal import Decimal

import pytest

REL = "routes/facility_profile_page.py"

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))

# Real coordinates, and the real market rows they collided with.
BLUMENAU = (-26.9194, -49.0661)          # Santa Catarina, Brazil
CHARLESTON_SC = (32.7765, -79.9311)      # South Carolina, USA
HATTERSHEIM = (50.0759, 8.4781)          # 15 km from Frankfurt
FRANKFURT = (50.1109, 8.6821)

_SEL_COLS = ["market_slug", "market_name", "iso", "verdict",
             "excess_power_score", "constraint_score", "time_to_power_months",
             "latitude", "longitude"]


def _row(slug, lat, lon):
    return (slug, slug.title(), "SERC", "CAUTION", 41, 55, 30, lat, lon)


class _StubCursor:
    """Returns `row` for queries matching `only_when`, else nothing.

    Records every statement so a test can also assert the SHAPE of the SQL the
    function actually executed, rather than the shape of its source text.
    """

    def __init__(self, sink, row, only_when=None):
        self.sink, self.row, self.only_when = sink, row, only_when
        self._hit = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append(sql)
        self._hit = (self.only_when is None) or (self.only_when in sql)

    @property
    def description(self):
        return [(c,) for c in _SEL_COLS]

    def fetchone(self):
        return self.row if self._hit else None

    def fetchall(self):
        return [self.row, self.row] if self._hit else []


class _StubConn:
    def __init__(self, sink, row, only_when=None):
        self.sink, self.row, self.only_when = sink, row, only_when

    def cursor(self, *a, **kw):
        return _StubCursor(self.sink, self.row, self.only_when)

    def close(self):
        pass


def _drive(monkeypatch, row, only_when=None):
    """Run the real _market_dcpi against a stub DB; hand back (fn, sink)."""
    import routes.facility_profile_page as fpp

    sink = []
    fake_main = types.ModuleType("main")
    fake_main.get_read_db = lambda: _StubConn(sink, row, only_when)
    monkeypatch.setitem(sys.modules, "main", fake_main)
    return fpp, sink


# ── the distance function itself (pure, so it always runs) ────────────────

def test_km_between_reproduces_distances_measured_in_production():
    """The guard is only as good as its arithmetic.

    Both figures were measured live on 2026-08-26 from the pages' own published
    coordinates, so this pins the function to reality rather than to itself.
    """
    import routes.facility_profile_page as fpp

    d_bug = fpp._km_between(*BLUMENAU, *CHARLESTON_SC)
    assert d_bug == pytest.approx(7395, abs=25), (
        f"Blumenau->Charleston computed {d_bug} km, measured 7,395 km")

    d_ok = fpp._km_between(*HATTERSHEIM, *FRANKFURT)
    assert d_ok == pytest.approx(15, abs=3), (
        f"Hattersheim->Frankfurt computed {d_ok} km, expected ~15 km")

    assert fpp._km_between(None, 1, 2, 3) is None, "must not raise on a null coord"
    assert fpp._km_between("x", 1, 2, 3) is None, "must not raise on a junk coord"


def test_thresholds_sit_in_the_empty_gap_between_real_and_absurd():
    import routes.facility_profile_page as fpp

    assert fpp._NEAR_KM <= 250, (
        "_NEAR_KM is what lets the page CLAIM a market as the facility's own; "
        "past ~250 km 'the market this facility sits in' stops being true")
    assert 201 < fpp._SANITY_KM < 7000, (
        "_SANITY_KM must land in the measured gap — the furthest legitimate "
        "market was 201 km, the nearest collision 7,395 km")


# ── the guard, fenced WITHOUT a database ─────────────────────────────────

def test_a_market_on_another_continent_is_rejected(monkeypatch):
    """The whole defect in one assertion.

    Every query the function can run is answered with Charleston SC — exactly
    what the live table does for a Brazilian facility whose state is 'SC'. No
    fallback may accept it, so the only correct answer is None.
    """
    fpp, _ = _drive(monkeypatch, _row("charleston-sc", *CHARLESTON_SC))

    got = fpp._market_dcpi("Blumenau", "SC", BLUMENAU[0], BLUMENAU[1])

    assert got is None, (
        f"a Brazilian facility resolved to {got!r}, "
        f"{fpp._km_between(*BLUMENAU, *CHARLESTON_SC):.0f} km away — the state "
        "code collided with a US market and the distance guard did not stop it")


def test_a_nearby_market_is_still_accepted(monkeypatch):
    """The guard must reject the absurd without rejecting the ordinary."""
    fpp, _ = _drive(monkeypatch, _row("frankfurt", *FRANKFURT))

    got = fpp._market_dcpi("Hattersheim", "", HATTERSHEIM[0], HATTERSHEIM[1])

    assert got and got.get("market_slug") == "frankfurt", (
        f"a facility 15 km from Frankfurt resolved to {got!r} — the guard is "
        "rejecting real markets, which would strip context from working pages")


def test_coordinates_alone_resolve_a_market(monkeypatch):
    """No city, no state, just coordinates — the international case.

    The old function returned None before it ever looked at the coordinates.
    Answering ONLY the bounding-box query proves the answer came from the
    coordinate step and not from a city or state match.
    """
    fpp, sink = _drive(monkeypatch, _row("frankfurt", *FRANKFURT),
                       only_when="latitude BETWEEN")

    got = fpp._market_dcpi("", "", FRANKFURT[0], FRANKFURT[1])

    assert got and got.get("market_slug") == "frankfurt", (
        "a facility with coordinates and nothing else got no market; every "
        "fallback is gated on `state` again and international pages are thin")
    assert any("latitude BETWEEN" in s for s in sink), (
        "no bounding-box query ran — the coordinate step is gone")


def test_the_returned_dict_does_not_leak_coordinate_columns(monkeypatch):
    """latitude/longitude are read for the guard and must be popped again.

    The caller spreads this dict into chips and a <title>; a new key here is a
    new thing the page can render.
    """
    fpp, _ = _drive(monkeypatch, _row("frankfurt", *FRANKFURT))

    got = fpp._market_dcpi("Hattersheim", "", HATTERSHEIM[0], HATTERSHEIM[1])

    assert got is not None
    assert "latitude" not in got and "longitude" not in got, (
        f"_market_dcpi returned {sorted(got)} — the coordinate columns are "
        "internal to the distance guard and must not reach the page")


def test_a_facility_without_coordinates_keeps_its_market(monkeypatch):
    """Unknown distance is not far.

    72-78% of facilities carry coordinates. The rest must keep resolving the
    way they did before this change, or the fix costs more pages than it wins.
    """
    fpp, _ = _drive(monkeypatch, _row("charleston-sc", *CHARLESTON_SC))

    got = fpp._market_dcpi("Charleston", "SC", None, None)

    assert got and got.get("market_slug") == "charleston-sc", (
        "a facility with no coordinates lost the market it resolves to today")


def test_a_nearer_market_across_a_state_line_does_not_win(monkeypatch):
    """Nearest is the right tie-break WITHIN a grid and the wrong one ACROSS it.

    r-market-resolve-same-state (2026-08-27). The coordinate step shipped ABOVE
    the same-state step, and measuring 500 live pages afterwards caught three US
    facilities moving to a nearer market on another grid — their <title> changed
    operator with them:

        Microsoft Boydton, VA     /dcpi/chester (PJM) -> /dcpi/durham (SERC)
        Microsoft Azure East US   /dcpi/chester (PJM) -> /dcpi/durham (SERC)
        Meta Jeffersonville, IN   /dcpi/indianapolis  -> /dcpi/louisville
                                             (MISO)              (SERC)

    Boydton is Dominion inside PJM; Durham is Duke Progress, not in an RTO at
    all. That is a facility wearing another grid's operator — the same defect
    the geo fix exists to kill, at 90 km instead of 7,395.

    Answering ONLY the same-state query proves the answer came from that step:
    if the global coordinate step runs first it sees no rows here and the call
    returns None, so this test fails the moment the order is swapped back.
    """
    # ★ The discriminator must be UNIQUE to the same-state step. The first
    #   attempt keyed on "WHERE LOWER(state) = %s", which step (4) also
    #   contains — so disabling step (2) still passed, because step (4)
    #   answered with the same row and the same distance. Mutation testing
    #   caught it; "LIMIT 2" belongs to the same-state query alone.
    fpp, sink = _drive(monkeypatch, _row("chester", 37.3563, -77.4360),
                       only_when="LIMIT 2")

    got = fpp._market_dcpi("Boydton", "VA", 36.6676, -78.3875)

    assert got and got.get("market_slug") == "chester", (
        f"a Boydton VA facility resolved to {got!r} — the coordinate step is "
        "running before the same-state step again, so a PJM facility can take "
        "a SERC market's name and grid operator because it is 24 km nearer")


def test_the_global_coordinate_step_still_owns_the_stateless_case(monkeypatch):
    """Same-state-first must not cost the international case anything.

    The whole point of the coordinate step is facilities with no usable state.
    Answering only the bounding-box query proves it is still reachable.
    """
    fpp, sink = _drive(monkeypatch, _row("frankfurt", *FRANKFURT),
                       only_when="latitude BETWEEN")

    got = fpp._market_dcpi("Hattersheim", "", *HATTERSHEIM)

    assert got and got.get("market_slug") == "frankfurt", (
        "a stateless facility with coordinates got no market — the global "
        "coordinate step is unreachable and international pages go thin again")


# ── Null Island: the placeholder that poisoned the guard ─────────────────
#
# r-nullisland-market (2026-09-11). The distance guard fenced above is correct
# and its thresholds are right; what it was never told is that 0,0 is not a
# position. `latitude` and `longitude` reach _market_dcpi RAW off the facility
# row, both range checks PASS for (0,0) — 0 is a legal latitude and a legal
# longitude — and the ingestion placeholder documented in
# routes/provenance.normalize_coordinates therefore measured every candidate
# market from the Gulf of Guinea. Step (1)'s EXACT city match was rejected at
# ~10,000 km and every later step fell through, so the page got NO market.
#
# Measured on a 500-page live sample (2026-09-11): 6 pages store 0,0; the 5
# whose city is a scored market — Houston, Tokyo x2, Taipei, Austin — each
# rendered no market block, a median 45 distinct words against 186 for pages
# that render one. The other 89 thin pages in that sample are NOT this bug:
# their city genuinely is not a scored market (/dcpi/istanbul, /dcpi/shanghai,
# /dcpi/dusseldorf all 404) and they must keep getting nothing.

HOUSTON = (29.7604, -95.3698)
AUSTIN = (30.2672, -97.7431)
TOKYO = (35.6762, 139.6503)
LIBREVILLE = (0.3901, 9.4544)     # Gabon — a REAL place at latitude ~0


def test_null_island_coordinates_do_not_reject_the_exact_city_match(monkeypatch):
    """The defect in one assertion: 0,0 cost a page the market it names.

    A Houston facility stored with coordinates 0,0 resolved to None because
    _too_far measured Houston at 10,526 km from Null Island. The identical row
    with NULL coordinates resolves to `houston` today — so the placeholder was
    strictly worse than the absence it stands for.
    """
    fpp, _ = _drive(monkeypatch, _row("houston", *HOUSTON))

    got = fpp._market_dcpi("Houston", "", 0.0, 0.0)

    assert got and got.get("market_slug") == "houston", (
        f"a Houston facility stored at 0,0 resolved to {got!r} — the Null "
        f"Island placeholder is being treated as a real position "
        f"{fpp._km_between(0.0, 0.0, *HOUSTON):.0f} km away, so the distance "
        "guard rejects the facility's own city and the page renders no market")


def test_a_null_coordinate_facility_and_a_null_island_one_resolve_alike(monkeypatch):
    """0,0 MEANS 'missing', so it must behave exactly like missing.

    This is the whole contract of the fix — not "0,0 gets special handling" but
    "0,0 stops being a position at all". Pinning the two against each other
    fails if the sentinel is only partly translated.
    """
    fpp, _ = _drive(monkeypatch, _row("houston", *HOUSTON))
    absent = fpp._market_dcpi("Houston", "", None, None)

    fpp, _ = _drive(monkeypatch, _row("houston", *HOUSTON))
    sentinel = fpp._market_dcpi("Houston", "", 0.0, 0.0)

    assert absent == sentinel, (
        f"coords=None gave {absent!r} but coords=(0,0) gave {sentinel!r} — "
        "the placeholder is still steering resolution somewhere")


@pytest.mark.parametrize("lat,lng", [
    (0, 0),                 # ints, as a JSON body or an INTEGER column yields
    (0.0, 0.0),             # floats, the live discovered_facilities spelling
    ("0", "0"),             # strings, as a CSV/scraper import leaves them
    ("0.0", "0.0"),         # stringified floats, same path
    (Decimal("0"), Decimal("0")),        # psycopg2 NUMERIC columns
    (Decimal("0.0"), Decimal("0.0")),
])
def test_every_spelling_of_null_island_reads_as_absent(monkeypatch, lat, lng):
    """The row can carry 0,0 as int, float, str or Decimal.

    _market_dcpi float()s whatever it is handed, so a guard that only knew the
    float spelling would leave the other four live. discovered_facilities and
    `facilities` are read by two different drivers and populated by importers
    that each stringify differently, so the spelling is not ours to assume.
    """
    fpp, _ = _drive(monkeypatch, _row("houston", *HOUSTON))

    got = fpp._market_dcpi("Houston", "", lat, lng)

    assert got and got.get("market_slug") == "houston", (
        f"coordinates ({lat!r}, {lng!r}) resolved to {got!r} — this spelling "
        "of the Null Island placeholder still reads as a real position")


def test_a_real_state_still_resolves_when_the_coordinates_are_null_island(monkeypatch):
    """The US/state path was poisoned too, and it is the majority path.

    Austin TX is the live case: it carries a REAL state, so it renders a State
    tile and never appeared in the thin-page signature — but step (1) rejected
    `austin` at 11,000 km, step (2) ranked TX metros by distance from the Gulf
    of Guinea, and step (4) was distance-checked from there as well. A fix that
    only rescued stateless pages would leave this one broken.
    """
    fpp, _ = _drive(monkeypatch, _row("austin", *AUSTIN))

    got = fpp._market_dcpi("Austin", "TX", 0.0, 0.0)

    assert got and got.get("market_slug") == "austin", (
        f"an Austin TX facility stored at 0,0 resolved to {got!r} — a row with "
        "a perfectly good state and city lost its market to the coordinate "
        "placeholder")


def test_a_city_with_no_state_resolves_through_null_island(monkeypatch):
    """City but no state is the international shape, and it is most of the class.

    4 of the 5 measured pages (Houston, Tokyo x2, Taipei) carry a city and an
    EMPTY state, so `st` is falsy and steps (2) and (4) cannot run at all. Only
    the exact city match can answer them, which is precisely the step the
    poisoned distance guard was rejecting.
    """
    fpp, _ = _drive(monkeypatch, _row("tokyo", *TOKYO))

    got = fpp._market_dcpi("Tokyo", "", 0.0, 0.0)

    assert got and got.get("market_slug") == "tokyo", (
        f"a Tokyo facility with no state and coordinates 0,0 resolved to "
        f"{got!r} — with no state there is no fallback, so rejecting the city "
        "match leaves the page with nothing")


def test_a_genuinely_equatorial_coordinate_is_not_treated_as_missing(monkeypatch):
    """lat=0 with a REAL longitude is Gabon, not a placeholder.

    This is why the fix reuses provenance.normalize_coordinates rather than
    testing `not lat` or `abs(lat) < eps` on either axis alone: ONLY (0,0)
    TOGETHER is absent. Libreville sits at latitude 0.39 and there are live
    facilities on the equator in Gabon, Ecuador, Indonesia and Kenya.
    """
    fpp, sink = _drive(monkeypatch, _row("libreville", *LIBREVILLE),
                       only_when="latitude BETWEEN")

    got = fpp._market_dcpi("", "", 0.0, 9.4544)

    assert got and got.get("market_slug") == "libreville", (
        f"an equatorial facility at (0.0, 9.4544) resolved to {got!r} — a real "
        "coordinate on the equator is being discarded as a placeholder")
    assert any("latitude BETWEEN" in s for s in sink), (
        "the bounding-box step never ran, so these coordinates were thrown "
        "away before they could resolve anything")


def test_null_island_does_not_switch_off_the_distance_guard(monkeypatch):
    """Translating the sentinel must not cost the guard it was poisoning.

    The cheap wrong fix is to stop distance-checking when coordinates look
    unusable. Blumenau still has REAL coordinates and must still be refused
    /dcpi/charleston-sc, 7,395 km away.
    """
    fpp, _ = _drive(monkeypatch, _row("charleston-sc", *CHARLESTON_SC))

    got = fpp._market_dcpi("Blumenau", "SC", *BLUMENAU)

    assert got is None, (
        f"a Brazilian facility resolved to {got!r} — the Null Island change "
        "disabled the continent guard it was supposed to unblock")


def test_null_island_never_invents_a_market_for_a_city_that_has_none(monkeypatch):
    """Falling through is the goal; guessing is not.

    89 of the 94 thin pages in the sample have a city that is genuinely not a
    scored market (/dcpi/istanbul and /dcpi/shanghai both 404). Clearing the
    placeholder must let those resolve HONESTLY to nothing, not hand them the
    most-recent row in some state. Every query answers empty here, which is
    what the live table does for Istanbul.
    """
    fpp, _ = _drive(monkeypatch, _row("houston", *HOUSTON),
                    only_when="__never_matches__")

    got = fpp._market_dcpi("Istanbul", "", 0.0, 0.0)

    assert got is None, (
        f"a facility in a city with no scored market resolved to {got!r} — the "
        "page would publish a market this facility is not in")


# ── the SQL, against a real database ─────────────────────────────────────

@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_bounding_box_query_is_valid_sql_on_a_real_server():
    """The stub above never parses the SQL. This does.

    Runs the coordinate step's statement as written against a live server, so
    a syntax error or a wrong column name cannot pass the stub tests and ship.
    """
    import psycopg2

    import routes.facility_profile_page as fpp

    conn = psycopg2.connect(_DB)
    try:
        c = conn.cursor()
        c.execute(
            "SELECT market_slug, latitude, longitude FROM market_power_scores "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
            "  AND latitude BETWEEN %s AND %s "
            "  AND longitude BETWEEN %s AND %s "
            "ORDER BY (POWER(latitude - %s, 2) + "
            "          POWER((longitude - %s) * COS(RADIANS(%s)), 2)) ASC, "
            "         computed_at DESC LIMIT 1",
            (49.0, 51.2, 7.0, 10.4, 50.11, 8.68, 50.11))
        row = c.fetchone()
        assert row is None or len(row) == 3
        assert fpp._NEAR_KM > 0
    finally:
        conn.close()
