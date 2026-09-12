"""routes/market_brief._facility_match must see a facility that only its CITY names.

THE BUG (owner report, 2026-09-12, /markets/midland-tx/brief on a PRO seat)
──────────────────────────────────────────────────────────────────────────
At-a-Glance read 0 facilities, 0 MW operational, 0 MW pipeline and no top
operator; Pipeline and Operator Footprint were both empty (coverage 3/8) — while
/api/v1/facilities?state=TX&city=Midland returned Crusoe Energy Permian Basin,
50 MW, status Operational, v=verified (so it passes COALESCE(is_duplicate,0)=0).

The matcher could not see it from any angle. Its `market` tag is not
"midland-odessa"; the DCPI name "Midland-Odessa" is joined by an EN DASH, so
name.split(",")[0] yielded the whole dual-city string, which appears in no
column; and latitude/longitude are NULL (coordinates_status "unknown"), so the
~0.6 degree proximity box — the only rescue the matcher had — could not fire.

WHAT THIS ASSERTS
The predicate these tests run is the one _facility_match SHIPS. The SQL string
and its params are taken from the function and mechanically transported to
SQLite — `= ANY(%s)` to `IN (?,...)`, `~` to a REGEXP function that maps
Postgres's \\y to \\b — so the column names, the clause structure and the state
scoping all come from the shipped string. Nothing here re-implements the
matching rules; change the predicate and these tests follow it.
"""
import importlib.util
import pathlib
import re
import sqlite3

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("_mb", REPO / "routes" / "market_brief.py")
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)

COLS = ("name", "market", "city", "state", "latitude", "longitude",
        "power_mw", "status", "is_duplicate")

# The real row, as /api/v1/facilities returned it on 2026-09-12.
CRUSOE = ("Crusoe Energy Permian Basin", None, "Midland", "TX", None, None,
          50, "Operational", 0)
# Controls. Each must STAY OUT, or the fix has traded a miss for a wrong number.
MIDLAND_MI = ("Some DC", None, "Midland", "MI", 43.6, -84.2, 10, "Operational", 0)
FAR_TX = ("Houston DC", None, "Houston", "TX", 29.76, -95.37, 99, "Operational", 0)
DUP_MIDLAND = ("Dup", None, "Midland", "TX", None, None, 500, "Operational", 1)

MIDLAND_HERO = {"name": "Midland–Odessa", "lat": 31.99, "lng": -102.07, "state": "TX"}


def _regexp(pattern, value):
    return re.search(pattern.replace(r"\y", r"\b"), value or "") is not None


def _run(hero, rows):
    """Execute the SHIPPED predicate against `rows`; return matched names."""
    sql, params = mb._facility_match(hero)
    out_sql, out_params, i = [], [], 0
    # Expand `ANY(%s)` (a list param) to an IN list; leave every other %s a ?.
    for chunk in re.split(r"(= ANY\(%s\)|~ %s|%s)", sql):
        if chunk == "= ANY(%s)":
            vals = params[i]; i += 1
            out_sql.append("IN (" + ",".join("?" * len(vals)) + ")" if vals else "IN (NULL)")
            out_params.extend(vals)
        elif chunk == "~ %s":
            out_sql.append("REGEXP ?"); out_params.append(params[i]); i += 1
        elif chunk == "%s":
            out_sql.append("?"); out_params.append(params[i]); i += 1
        else:
            out_sql.append(chunk)
    assert i == len(params), f"consumed {i} of {len(params)} params — placeholder/param mismatch"

    con = sqlite3.connect(":memory:")
    con.create_function("REGEXP", 2, lambda p, v: _regexp(p, v))
    con.execute(f"CREATE TABLE discovered_facilities ({','.join(COLS)})")
    con.executemany(
        f"INSERT INTO discovered_facilities VALUES ({','.join('?' * len(COLS))})", rows)
    q = (f"SELECT name FROM discovered_facilities WHERE ({''.join(out_sql)}) "
         f"AND COALESCE(is_duplicate, 0) = 0")
    return {r[0] for r in con.execute(q, out_params)}


def test_a_city_only_facility_is_found():
    """The reported defect: city + state populated, no market tag, NULL coords."""
    assert "Crusoe Energy Permian Basin" in _run(MIDLAND_HERO, [CRUSOE])


def test_the_city_clause_is_what_finds_it():
    """CONTROL. With no state on the hero the state-scoped city clause cannot
    fire, and the row goes back to being invisible — which is what the matcher
    did for every market before this clause existed. If this ever starts
    passing, the assertion above has stopped testing the city clause."""
    no_state = {**MIDLAND_HERO, "state": None}
    assert _run(no_state, [CRUSOE]) == set()


def test_the_dual_city_name_reaches_both_halves():
    odessa = ("Odessa DC", None, "Odessa", "TX", None, None, 20, "Operational", 0)
    found = _run(MIDLAND_HERO, [CRUSOE, odessa])
    assert found == {"Crusoe Energy Permian Basin", "Odessa DC"}


@pytest.mark.parametrize("row,why", [
    (MIDLAND_MI, "a same-named city in another STATE must not be claimed"),
    (FAR_TX, "same state, 600 km away, not a name component"),
    (DUP_MIDLAND, "is_duplicate=1 is still excluded"),
])
def test_controls_stay_out(row, why):
    assert _run(MIDLAND_HERO, [row]) == set(), why


def test_the_change_can_only_add_rows():
    """Monotonic: every pre-existing clause is untouched, so anything the old
    predicate matched the new one still matches. Asserted on the shipped SQL —
    the new clause is a pure OR appended after the proximity box."""
    sql, _ = mb._facility_match(MIDLAND_HERO)
    head, _, tail = sql.partition(" OR (state = %s AND (LOWER(COALESCE(city, ''))")
    assert tail, "the city clause is no longer an appended OR — monotonicity is unproven"
    assert "LOWER(COALESCE(market, '')) = ANY(%s)" in head
    assert "latitude IS NOT NULL" in head


@pytest.mark.parametrize("name,expect", [
    ("Midland–Odessa", ["midland", "midland–odessa", "odessa"]),
    ("Cheyenne, WY", ["cheyenne"]),
    ("Raleigh–Durham", ["durham", "raleigh", "raleigh–durham"]),
    ("Portland", ["portland"]),      # "and" inside a word must not split it
    ("Highland", ["highland"]),
    ("", []),
])
def test_market_name_tokens(name, expect):
    assert mb._market_name_tokens(name) == expect
