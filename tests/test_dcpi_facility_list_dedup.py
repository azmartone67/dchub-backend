"""r-list-dedup (2026-08-08) — a rendered facility list must not show the same
building twice.

THE BUG: the r80 SEO internal-link mesh on /dcpi/<market> selected the market's
facilities with no duplicate-visibility predicate at all, so a building that had
been twinned appeared once per row and the repeats ate the LIMIT 50.

Measured on https://dchub.cloud/dcpi/manchester (2026-08-08): the 50 rendered
<li> items carried only 28 distinct names — "Equinix MA1 - Manchester,
Williams/Kilburn", "Joule House", "Itility BVDC", "Teledata Manchester - Delta
House", "Greenheys", "ANS - MAN4", "ANS - MAN5", "ANS - MAN6" and "Delta House"
each appeared twice. 25 of the 50 rows carried a duplicate_of_id. Manchester has
36 non-duplicate facilities, so 8 real ones were pushed off the end of the list
by their own twins. Across the estate: 9,459 of 24,676 discovered_facilities
rows (38%) carry a duplicate_of_id, and 301 of 322 scored markets had at least
one repeat inside their top 50.

★ VISIBILITY IS `duplicate_of_id` ALONE, NEVER `is_duplicate`.
`is_duplicate` is a suppression bit: it drops the row from counts and from the
sitemap, and a slug whose rows are ALL flagged still serves 200. The pointer is
what says "this row is another row's twin". Same predicate as
routes/facilities_by_dims.py, routes/d1_sync.py, and the canonical in
routes/facility_profile_page.py. A test that accepted `is_duplicate = 0` here
would pass on a fix that consolidates nothing.

SCOPE — deliberately row-rendering queries only. The invariant asserted here is
the one this change establishes: a query that selects individual facility ROWS
FOR DISPLAY is deduped. Aggregates are out of scope by construction, not by
oversight.

r-sat-dedup (2026-08-08): the saturation footprint aggregate in
gather_metrics_for_market — untouched when this file was written, because
filtering it rescores every published market — now carries the same predicate.
Its own invariants (both branches scoped, from one shared definition) live in
tests/test_dcpi_saturation_footprint_dedup.py, which imports `_sql_nodes` from
here rather than growing a second copy of it.
"""
import ast
import re
import sqlite3

import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402


_SRC = dcpi.__file__.replace(".pyc", ".py")


def _sql_nodes(path):
    """(lineno, sql) for every SQL string routes/dcpi.py hands to a cursor,
    read out of the AST so a query that only exists in a COMMENT cannot satisf
    this test. f-string holes are rendered as the interpolated NAME, so a
    predicate injected via {_fac_ctry_sql} reads as present."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    claimed, out = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            buf = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    claimed.add(id(v))
                    buf.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    claimed.add(id(v))
                    buf.append(f" {ast.unparse(v.value)} ")
            out.append((node.lineno, "".join(buf)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in claimed):
            out.append((node.lineno, node.value))
    return out


# Scoped to ONE market: filters on that market's name.
_NAME_SCOPED = re.compile(r"\b(lower\s*\(\s*city|city\s*=|city\s*ilike|market\s*=)", re.I)
# The pointer, and ONLY the pointer. `is_duplicate` deliberately does not match.
_DEDUPED = re.compile(r"duplicate_of_id\s+is\s+null", re.I)
# A row-rendering select: names a bare `name` column outside an aggregate.
# COUNT(*)/SUM(power_mw)/MAX(state) selects do not match.
_AGGREGATE = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.I)


def _selected_columns(sql):
    m = re.search(r"\bselect\b(.*?)\bfrom\b", sql, re.I | re.S)
    return m.group(1) if m else ""


def _is_row_rendering(sql):
    cols = _selected_columns(sql)
    if not cols.strip():
        return False
    # strip aggregate calls, then look for a bare `name` column
    bare = re.sub(r"\b(count|sum|avg|min|max)\s*\([^)]*\)", " ", cols, flags=re.I)
    return re.search(r"(^|,)\s*name\s*(,|$)", bare.strip(), re.I) is not None


def test_no_market_scoped_facility_list_shows_duplicates():
    """THE invariant: any query that pulls individual facility rows for ONE
    market carries the duplicate-visibility predicate."""
    offenders = []
    for lineno, sql in _sql_nodes(_SRC):
        if "from discovered_facilities" not in sql.lower():
            continue
        if not _NAME_SCOPED.search(sql):
            continue
        if not _is_row_rendering(sql):
            continue                       # aggregate — see module docstring
        if not _DEDUPED.search(sql):
            offenders.append((lineno, " ".join(sql.split())[:170]))
    assert not offenders, (
        "market-scoped facility LIST query with no duplicate_of_id predicate — "
        "this is how /dcpi/manchester rendered 50 rows holding 28 distinct "
        "facilities:\n" + "\n".join(f"  routes/dcpi.py:{ln}  {s}"
                                    for ln, s in offenders))


def test_the_predicate_is_the_pointer_not_the_flag():
    """`is_duplicate` is a suppression bit, not a consolidation mechanism.
    Guards against a 'fix' that swaps in the flag: it would drop rows from the
    sitemap while still rendering both twins whenever the flag was never set
    (6,192 of the flagged rows carry a pointer; 3,188 carry a pointer while
    staying UNflagged — those are exactly the repeats we measured)."""
    for lineno, sql in _sql_nodes(_SRC):
        low = sql.lower()
        if "from discovered_facilities" not in low:
            continue
        if not (_NAME_SCOPED.search(sql) and _is_row_rendering(sql)):
            continue
        assert _DEDUPED.search(sql), f"routes/dcpi.py:{lineno} lost the pointer"
        assert not re.search(r"is_duplicate\s*=", sql, re.I), (
            f"routes/dcpi.py:{lineno} scopes visibility on is_duplicate. "
            "Visibility is duplicate_of_id ALONE — the flag also removes the "
            "row from counts and the sitemap and does not consolidate.")


# --------------------------------------------------------------------------
# Behavioral: EXECUTE the production query over planted twins.
# --------------------------------------------------------------------------
_SQLITE_DDL = """
CREATE TABLE discovered_facilities (
  id INTEGER PRIMARY KEY, name TEXT, provider TEXT, power_mw REAL,
  city TEXT, market TEXT, state TEXT, country TEXT,
  latitude REAL, longitude REAL, duplicate_of_id INTEGER, is_duplicate INTEGER
)
"""


def _production_list_sql():
    """The page's facility-list SQL, taken from the AST — not a retyped copy.
    The {_fac_ctry_sql} hole is filled with the real fragment for the market
    under test, so this executes the same predicate production executes."""
    for lineno, sql in _sql_nodes(_SRC):
        low = sql.lower()
        if ("from discovered_facilities" in low
                and "id, name, provider, power_mw" in low):
            return sql
    pytest.fail("could not locate the DCPI facility-list query in routes/dcpi.py")


def _run_list(rows, market_name, iso, state, lat, lon):
    ctry_sql, ctry_params = dcpi._market_country_scope(iso, state, lat, lon)
    sql = _production_list_sql().replace(" _fac_ctry_sql ", " " + ctry_sql + " ")
    sql = sql.replace("%s", "?")
    con = sqlite3.connect(":memory:")
    con.execute(_SQLITE_DDL)
    con.executemany(
        "INSERT INTO discovered_facilities (id,name,provider,power_mw,city,"
        "market,state,country,latitude,longitude,duplicate_of_id,is_duplicate)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    params = [market_name, market_name] + list(ctry_params)
    return con.execute(sql, params).fetchall()


def _row(i, name, dup_of=None, flag=0, city="Manchester", country="GB",
         lat=53.48, lon=-2.24, mw=10.0):
    return (i, name, "Equinix", mw, city, city, "", country, lat, lon,
            dup_of, flag)


def test_manchester_shape_renders_each_building_once():
    """The measured production shape: three real Manchester facilities, each
    with a twin row pointing at it. The list must render three items."""
    rows = [
        _row(1, "Equinix MA1 - Manchester, Williams/Kilburn", mw=30),
        _row(2, "Joule House", mw=20),
        _row(3, "Teledata Manchester - Delta House", mw=10),
        # the twins — pointer set, flag NEVER set (this is the common shape:
        # 3,188 rows carry a pointer while staying unflagged)
        _row(4, "Equinix MA1 - Manchester, Williams/Kilburn", dup_of=1, mw=30),
        _row(5, "Joule House", dup_of=2, mw=20),
        _row(6, "Teledata Manchester - Delta House", dup_of=3, mw=10),
    ]
    got = _run_list(rows, "Manchester", "NGESO", "", 53.48, -2.24)
    ids = [r[0] for r in got]
    names = [r[1] for r in got]
    assert ids == [1, 2, 3], f"twins leaked into the rendered list: {ids}"
    assert len(names) == len(set(names)), f"duplicate names rendered: {names}"


def test_a_flagged_but_unpointed_row_still_renders():
    """is_duplicate alone must NOT hide a facility. A row flagged with no
    pointer is a keeperless suppression, not a twin — dropping it is how a live
    facility silently loses its slot."""
    rows = [
        _row(1, "Real Facility A", mw=30),
        _row(2, "Real Facility B", dup_of=None, flag=1, mw=20),
    ]
    got = _run_list(rows, "Manchester", "NGESO", "", 53.48, -2.24)
    assert {r[0] for r in got} == {1, 2}, (
        "a flagged-but-unpointed row was dropped — visibility must key on the "
        f"pointer alone, got {got}")


def test_real_facilities_reclaim_the_slots_the_twins_held():
    """The consequence that made this a defect and not a cosmetic repeat: with
    a LIMIT, every twin costs a real facility its slot."""
    limit = int(re.search(r"limit\s+(\d+)", _production_list_sql(), re.I).group(1))
    # Enough twinned pairs to OVERFLOW the limit on row count, while the
    # distinct buildings alone leave room to spare — the measured Manchester
    # shape (62 rows, 36 distinct, limit 50).
    pairs = limit - 5
    assert 2 * pairs > limit > pairs, "scenario must overflow only via twins"
    rows = []
    for i in range(pairs):
        rows.append(_row(i * 2 + 1, f"Twinned Site {i}", mw=100 + i))
        rows.append(_row(i * 2 + 2, f"Twinned Site {i}", dup_of=i * 2 + 1,
                         mw=100 + i))
    tail_id = pairs * 2 + 1
    rows.append(_row(tail_id, "Crowded Out DC", mw=1.0))
    got = _run_list(rows, "Manchester", "NGESO", "", 53.48, -2.24)
    names = [r[1] for r in got]
    assert len(names) == len(set(names)), "twins still occupying slots"
    assert tail_id in {r[0] for r in got}, (
        "the real low-MW facility is still crowded out of the LIMIT "
        f"{limit} by duplicate rows")
