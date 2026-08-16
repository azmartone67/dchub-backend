"""r-namesake (2026-08-07) — a market's facilities must live in that market's
country.

THE BUG: /dcpi/manchester published "Manchester · DCPI 25.7 · ISONE grid" at
(42.97, -71.47) — Manchester, NEW HAMPSHIRE — under a facility list that was
Manchester, ENGLAND: Equinix MA1/MA3/MA4, Joule House, Kilburn, ANS MAN4.
Two more markets had the same shape: dublin shipped as OH/PJM with an all-Irish
list (AWS EU-West-1, Meta Clonee, Equinix DB), and vienna shipped as VA/PJM
with a list that MIXED Ashburn and Wien.

Two holes, both required:
  1. _load_markets_dynamic is US-only by construction, yet won every slug
     collision in _build_markets_list. Three US facilities in Manchester NH
     cleared its `HAVING COUNT(*) >= 3` bar and silently redefined a curated
     international market's state, ISO and coordinates.
  2. The market-scoped facility queries carried no country predicate, so "the
     facilities in this market" meant "every facility on earth whose city
     string matches". This one also contaminated markets whose geography was
     CORRECT: /dcpi/birmingham listed Pulsant Birmingham WM-1 (UK) beside
     DC BLOX Birmingham (AL); /dcpi/richmond listed AAPT Richmond (Melbourne)
     beside QTS Richmond (VA).

r-orphan-geography already pinned johannesburg and markham as tuples, and the
class still produced three more instances. So these are INVARIANTS over every
market, not four more pins:
  * test_no_market_scoped_facility_query_is_country_blind — every query in
    routes/dcpi.py that selects facilities for ONE market carries a country
    predicate. Read out of the AST, so a match inside a comment cannot pass it.
  * test_every_market_excludes_its_foreign_namesake — the production predicate
    is EXECUTED (against sqlite) for all ~119 curated markets against a planted
    same-city foreign twin, and must reject it every time.
  * test_us_namesake_never_redefines_an_international_market — a synthetic
    dynamic loader claims a US row for EVERY international slug at once; all of
    them must keep their curated geography.
"""
import ast
import re
import sqlite3

import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402


# --------------------------------------------------------------------------
# 1. Structural: no market-scoped facility query may be country-blind.
# --------------------------------------------------------------------------
def _sql_literals(path):
    """Every SQL string routes/dcpi.py actually hands to a cursor, read out of
    the AST. Comments and docstrings-that-look-like-SQL are invisible here,
    which is the point — a grep-based version of this test passes on a query
    that was only ever described in a comment."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    # An f-string's literal chunks are Constant children of the JoinedStr, so a
    # plain ast.walk() yields each chunk a SECOND time, detached from the
    # {_ctry_sql} hole that scopes it. Claim them for the JoinedStr first.
    claimed = set()
    joined = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            buf = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    claimed.add(id(v))
                    buf.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    # keep the interpolated NAME, so a predicate injected via
                    # {_ctry_sql} reads as present
                    claimed.add(id(v))
                    buf.append(f" {ast.unparse(v.value)} ")
            joined.append((node.lineno, "".join(buf)))
    out = list(joined)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in claimed):
            out.append((node.lineno, node.value))
    return out


# A query is scoped to ONE market when it filters by that market's name —
# `city` or `market`. Queries that aggregate the whole table (the
# COUNT(DISTINCT country) footprint stats) and the coordinate-bbox local-infra
# query are not market-name-scoped and are correctly out of scope here.
_NAME_SCOPED = re.compile(r"\b(lower\s*\(\s*city|city\s*=|city\s*ilike|market\s*=)", re.I)
_COUNTRY_SCOPED = re.compile(r"(country|_ctry_sql|_fac_ctry_sql|_lite_ctry_sql)", re.I)


def test_no_market_scoped_facility_query_is_country_blind():
    offenders = []
    for lineno, sql in _sql_literals(dcpi.__file__.replace(".pyc", ".py")):
        if "from discovered_facilities" not in sql.lower():
            continue
        if not _NAME_SCOPED.search(sql):
            continue                      # whole-table or bbox-scoped
        if not _COUNTRY_SCOPED.search(sql):
            offenders.append((lineno, " ".join(sql.split())[:160]))
    assert not offenders, (
        "market-scoped discovered_facilities queries with no country "
        "predicate — this is how Manchester UK was published as Manchester "
        "NH:\n" + "\n".join(f"  routes/dcpi.py:{ln}  {s}" for ln, s in offenders))


# --------------------------------------------------------------------------
# 2. Behavioral: run the REAL predicate over a planted foreign namesake.
# --------------------------------------------------------------------------
_SQLITE_DDL = """
CREATE TABLE discovered_facilities (
  id INTEGER PRIMARY KEY, name TEXT, city TEXT, state TEXT, country TEXT,
  latitude REAL, longitude REAL
)
"""


def _select_scoped(rows, name, iso, state, lat=None, lon=None):
    """Apply the production country predicate to `rows` and return the ids it
    keeps. The fragment under test is the one gather_metrics_for_market and the
    page facility list both use — not a re-typed copy of it."""
    ctry_sql, ctry_params = dcpi._market_country_scope(iso, state, lat, lon)
    con = sqlite3.connect(":memory:")
    con.execute(_SQLITE_DDL)
    con.executemany(
        "INSERT INTO discovered_facilities "
        "(id,name,city,state,country,latitude,longitude) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        rows)
    sql = ("SELECT id FROM discovered_facilities WHERE LOWER(city) = LOWER(?) "
           + ctry_sql).replace("%s", "?")
    return {r[0] for r in con.execute(sql, [name] + list(ctry_params))}


# One planted foreign twin per market direction. Coordinates are real: an
# international market must reject the US twin, a US market the foreign one.
_US_TWIN = ("US", 39.04, -77.49)          # Ashburn VA
_FOREIGN_TWIN = ("GB", 53.48, -2.24)      # Manchester UK


def test_every_market_excludes_its_foreign_namesake():
    """THE general invariant: for every curated market, a same-city facility in
    the wrong country never reaches that market's saturation terms."""
    leaks = []
    for m in dcpi._MARKETS_HARDCODED:
        if not (isinstance(m, tuple) and len(m) >= 6):
            continue
        slug, name, state, iso, lat, lon = m[:6]
        intl = dcpi._is_intl_market(m)
        wrong = _US_TWIN if intl else _FOREIGN_TWIN
        rows = [
            # id 1: this market's own facility, in its own country.
            (1, f"{name} DC", name, state, "GB" if intl else "US", lat, lon),
            # id 2: the namesake in the wrong country — must NOT be selected.
            (2, f"{name} NAMESAKE", name, state, wrong[0], wrong[1], wrong[2]),
        ]
        kept = _select_scoped(rows, name, iso, state, lat, lon)
        if 2 in kept:
            leaks.append(f"{slug} ({state}/{iso}, intl={intl}) admitted a "
                         f"{wrong[0]} facility called {name}")
        if 1 not in kept:
            leaks.append(f"{slug} ({state}/{iso}, intl={intl}) DROPPED its own "
                         f"facility — the scope is eating real footprint")
    assert not leaks, "cross-border facility leak:\n  " + "\n  ".join(leaks)


def test_us_territories_keep_their_own_facilities():
    """r-namesake-territory (2026-08-07) — REGRESSION, caught in production.
    The first cut of this scope accepted only country IN ('US','USA'), which
    cut /dcpi/san-juan from 19 published facilities to 2: Puerto Rico rows
    carry country='PR'. DCPI scores PR/GU/VI US-style (PREPA/GPA/WAPA), so the
    market's own territory code has to be an accepted country."""
    cases = [
        # slug,          state, iso,     lat,    lon,     country, name
        ("san-juan",      "PR", "PREPA", 18.47, -66.10,  "PR", "Claro Puerto Rico"),
        ("guam",          "GU", "GPA",   13.50, 144.79,  "GU", "GTA Guam"),
        ("virgin-islands","VI", "WAPA",  18.34, -64.93,  "VI", "Viya STT"),
    ]
    for slug, state, iso, lat, lon, ctry, fac in cases:
        rows = [(1, fac, slug, state, ctry, lat, lon),
                (2, "mainland US row", slug, state, "US", lat, lon),
                (3, "Manchester UK namesake", slug, state, "GB", 53.48, -2.24)]
        kept = _select_scoped(rows, slug, iso, state, lat, lon)
        assert 1 in kept, f"{slug} dropped its own country='{ctry}' facility"
        assert 2 in kept, f"{slug} dropped a country='US' facility"
        assert 3 not in kept, f"{slug} admitted a GB facility"


def test_guam_is_not_disproved_by_a_north_america_box():
    """The first cut used a fixed North America bbox (lat 15..72, lon
    -170..-60) to disprove unknown-country rows. Guam is at 144.79E — every
    coordinate-bearing unknown-country row there was outside the box and
    dropped, and a box holding both Guam and the mainland would span half the
    planet and disprove nothing. The test is distance from the MARKET."""
    rows = [(1, "unknown country, in Guam", "guam", "GU", "", 13.50, 144.79),
            (2, "unknown country, in Perth AU", "guam", "GU", "", -31.95, 115.86)]
    kept = _select_scoped(rows, "guam", "GPA", "GU", 13.50, 144.79)
    assert 1 in kept, "a row sitting ON Guam was disproved for Guam"
    assert 2 not in kept, "a row 6,000 km away was credited to Guam"


def test_coordinateless_market_keeps_failing_open():
    """A market with no centre has nothing to measure against. It must credit
    unknown-country rows exactly as the pre-r-namesake code did — the fix is
    allowed to remove provably-foreign rows, never to punish absent data."""
    rows = [(1, "unknown country, no coords", "nowhere", "TX", "", None, None),
            (2, "unknown country, far away",  "nowhere", "TX", "", -31.95, 115.86),
            (3, "explicit GB",               "nowhere", "TX", "GB", 53.48, -2.24)]
    kept = _select_scoped(rows, "nowhere", "ERCOT", "TX", None, None)
    assert {1, 2} <= kept, "coordinate-less market must not filter on distance"
    assert 3 not in kept, "an explicitly foreign row is still excluded"


def test_unknown_country_is_credited_to_us_only_when_geography_allows():
    """The old US branch credited `country IS NULL OR country=''` outright.
    That is the latent half of the same class: the US shares two-letter
    subdivision codes with the rest of the world (WA is also Western
    Australia, ON Ontario, GA Gauteng), so an unknown-country foreign row
    matching on city+state was countable as US footprint.

    The rule now: unknown country is still credited — DCPI never penalises a
    market for absent data, and part of the US fleet predates the column — but
    only when nothing disproves it. Coordinates abroad disprove it."""
    rows = [
        (1, "no country, no coords",   "Perth", "WA", "",   None,  None),
        (2, "no country, US coords",   "Perth", "WA", "",   47.60, -122.33),   # Seattle
        (3, "no country, AU coords",   "Perth", "WA", "",  -31.95,  115.86),   # Perth AU
        (4, "explicit US",             "Perth", "WA", "US", 47.60, -122.33),
    ]
    # market centre = Seattle WA, the US market a country-blind scope would
    # have handed Perth, Western Australia
    kept = _select_scoped(rows, "Perth", "WECC", "WA", 47.60, -122.33)
    assert 3 not in kept, ("a row whose own coordinates are in Australia was "
                           "counted as US footprint")
    assert {1, 2, 4} <= kept, ("unknown-country rows that are NOT disproved "
                               "must keep counting — this fix must not shrink "
                               "a legitimate US footprint")


# --------------------------------------------------------------------------
# 3. Precedence: the US-only dynamic loader may not redefine an intl market.
# --------------------------------------------------------------------------
def test_us_namesake_never_redefines_an_international_market(monkeypatch):
    """_load_markets_dynamic emits US rows only. A collision with a curated
    international slug is therefore always a different city with the same
    name — never better data about the same one."""
    intl = [m for m in dcpi._MARKETS_HARDCODED
            if isinstance(m, tuple) and len(m) >= 6 and dcpi._is_intl_market(m)]
    assert len(intl) > 20, "sanity: the curated international set is not empty"

    # A synthetic loader that claims a US row for EVERY international slug at
    # once — the Manchester NH shape, applied across the board.
    fake = [(m[0], m[1], "NH", "ISONE", 42.97, -71.47) for m in intl]
    monkeypatch.setattr(dcpi, "_load_markets_dynamic", lambda: fake)
    monkeypatch.setattr(dcpi, "_load_scored_orphans", lambda known: [])

    built = {m[0]: m for m in dcpi._build_markets_list() if isinstance(m, tuple)}
    hijacked = []
    for m in intl:
        got = built.get(m[0])
        if got is None:
            hijacked.append(f"{m[0]} vanished from the market universe")
        elif (got[2], got[3], got[4], got[5]) != (m[2], m[3], m[4], m[5]):
            hijacked.append(f"{m[0]}: curated {m[2]}/{m[3]} ({m[4]},{m[5]}) "
                            f"-> published {got[2]}/{got[3]} ({got[4]},{got[5]})")
    assert not hijacked, ("US namesakes overwrote international markets:\n  "
                          + "\n  ".join(hijacked))


def test_us_markets_still_yield_to_the_dynamic_loader():
    """The guard above must stay narrow. For a US market the dynamic row IS
    the same market, with live centroid coords and the r-iso-taxonomy ISO —
    it must keep winning, or that whole lane silently reverts."""
    us = [m for m in dcpi._MARKETS_HARDCODED
          if isinstance(m, tuple) and len(m) >= 6 and not dcpi._is_intl_market(m)]
    assert us, "sanity: curated US markets exist"
    assert not dcpi._is_intl_market(("ashburn", "Ashburn", "VA", "PJM", 39.04, -77.49))
    assert not dcpi._is_intl_market(("san-juan", "San Juan", "PR", "PREPA", 18.47, -66.11))
    # PREPA/GPA/WAPA sit in BOTH _US_DCPI_ISOS and _INTL_ISO_LABELS (r71 bundled
    # the territories into the intl splice for merge purposes only). The US test
    # must run first or the territories flip international.
    assert dcpi._US_DCPI_ISOS & dcpi._INTL_ISO_LABELS, (
        "if this stops overlapping, the ordering comment in _is_intl_market is stale")


# --------------------------------------------------------------------------
# 4. Regression pins for the three measured instances.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("slug,state,iso,lat_lo,lat_hi,lon_lo,lon_hi", [
    # measured live 2026-08-07 as NH/ISONE (42.97, -71.47)
    ("manchester", "UK", "NGESO",     53.0, 54.0, -3.0, -1.5),
    # measured live 2026-08-07 as OH/PJM  (40.06, -83.16)
    ("dublin",     "IE", "EirGrid",   53.0, 54.0, -7.0, -6.0),
    # measured live 2026-08-07 as VA/PJM  (38.91, -77.22)
    ("vienna",     "AT", "ENTSOE-AT", 48.0, 48.5, 16.0, 17.0),
])
def test_hijacked_markets_are_the_international_city(slug, state, iso,
                                                     lat_lo, lat_hi, lon_lo, lon_hi):
    rows = [m for m in dcpi._MARKETS_HARDCODED
            if isinstance(m, tuple) and m[0] == slug]
    assert len(rows) == 1, f"{slug} must appear exactly once in _MARKETS_HARDCODED"
    _, _, st, got_iso, lat, lon = rows[0][:6]
    assert (st, got_iso) == (state, iso)
    assert lat_lo < lat < lat_hi and lon_lo < lon < lon_hi
    assert dcpi._is_intl_market(rows[0]), (
        f"{slug} must classify as international, or the dynamic loader's US "
        f"namesake takes it again on the next recompute")
