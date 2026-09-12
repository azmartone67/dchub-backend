#!/usr/bin/env python3
"""repair_one_url_many_rows against a REAL Postgres.

The repair sets `duplicate_of_id` on rows that already share one
`canonical_slug`. Two things have to be true and neither can be shown by a fake:

  1. THE ELECTION IS THE ONE IN THE SCRIPT. Window functions, the HAVING that
     skips groups something already consolidated, and the ordering that must
     never elect a SUPPRESSED keeper — a fake cursor answers a table, not a
     GROUP BY.

  2. ★ THE PAGE DOES NOT MOVE. A pointer redirects only under
     facility_profile_page._twin_redirect_target case B, which needs the
     keeper's slug to be worn by exactly ONE row. Every member of these groups
     wears it, so the condition is false — but that is an argument, and the
     whole point of this file is to run the REAL served_slugs over the REAL
     rows after the REAL update and read where the slug lands.

Set ONE_URL_REPAIR_DSN to run. CI's db-parity job sets it with
ONE_URL_REPAIR_REQUIRE=1 and fails the job if anything here skipped.

Run:  ONE_URL_REPAIR_DSN=postgresql:///served_slug_parity \\
        python3 -m pytest tests/test_one_url_many_rows_repair_sql.py -v
"""
import importlib.util
import os
import pathlib
import sys
import types
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402

DSN = os.environ.get("ONE_URL_REPAIR_DSN", "").strip()
REQUIRE = os.environ.get("ONE_URL_REPAIR_REQUIRE") == "1"

DDL = """
CREATE TABLE discovered_facilities (
  id INTEGER PRIMARY KEY, name TEXT, provider TEXT, city TEXT, state TEXT,
  country TEXT, market TEXT, latitude DOUBLE PRECISION,
  longitude DOUBLE PRECISION, power_mw DOUBLE PRECISION, status TEXT,
  address TEXT, is_duplicate INTEGER, duplicate_of_id INTEGER,
  canonical_slug TEXT, confidence_score DOUBLE PRECISION, source TEXT,
  source_url TEXT, sqft DOUBLE PRECISION, notes TEXT,
  investment_usd DOUBLE PRECISION, acreage DOUBLE PRECISION,
  discovered_at TEXT);
CREATE SEQUENCE discovered_facilities_id_seq START 9000;
ALTER TABLE discovered_facilities
  ALTER COLUMN id SET DEFAULT nextval('discovered_facilities_id_seq');
CREATE TABLE facilities (
  id TEXT PRIMARY KEY, name TEXT, provider TEXT, city TEXT, state TEXT,
  country TEXT, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
  power_mw DOUBLE PRECISION, status TEXT, address TEXT,
  duplicate_of_id TEXT, canonical_slug TEXT);
CREATE TABLE facility_slug_aliases (old_slug TEXT PRIMARY KEY,
  canonical_slug TEXT);
"""

# ── the fixture, one group per shape the election has to decide ─────────────
SRN = "south-reach-networks-fort-pierce-d6d47cf4"   # the live shape: 5 rows, nothing set
PARTIAL = "partly-consolidated-0000aaaa"            # one member already points → SKIP
ALL_DEAD = "every-row-suppressed-0000bbbb"          # no live member → SKIP
SOLO = "one-row-only-0000cccc"                      # not a group at all
TWIN, KEEPER = "twin-slug-0000dddd", "keeper-slug-0000eeee"  # different slugs → not a group

_SITE = dict(city="Fort Pierce", state="FL", country="US", market="Florida",
             latitude=27.4467, longitude=-80.3256, address="1 Orange Ave",
             status="Announced")


def _row(fid, slug, **over):
    r = dict(_SITE, id=fid, name=f"Row {fid}", provider="South Reach Networks",
             power_mw=None, is_duplicate=0, duplicate_of_id=None,
             canonical_slug=slug, confidence_score=0.55,
             source="competitor_gap:cloudscene")
    r.update(over)
    return r


ROWS = [
    # 5 rows, one slug, nothing consolidated. 502 is the richest → the keeper.
    _row(501, SRN),
    _row(502, SRN, confidence_score=0.91, power_mw=12.0),
    _row(503, SRN),
    _row(504, SRN),
    _row(505, SRN),
    # something already pointed here: leave the whole group alone
    _row(511, PARTIAL), _row(512, PARTIAL, duplicate_of_id=511),
    # every member suppressed: electing one would point live rows at a dead keeper
    _row(521, ALL_DEAD, is_duplicate=1), _row(522, ALL_DEAD, is_duplicate=1),
    # a lone row is not a group
    _row(531, SOLO),
    # a real dedup twin: DIFFERENT slugs, so this is the pointer lane's job
    _row(541, TWIN, duplicate_of_id=542), _row(542, KEEPER),
    # a suppressed member beside live ones: pointed, but never elected keeper
    _row(551, SRN + "-x", is_duplicate=1, confidence_score=0.99, power_mw=99.0),
    _row(552, SRN + "-x", confidence_score=0.10),
]


def _insert(cur, rows):
    cols = sorted({k for r in rows for k in r})
    cur.executemany(
        f"INSERT INTO discovered_facilities ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [tuple(r.get(c) for c in cols) for r in rows])


def _repair_module():
    spec = importlib.util.spec_from_file_location(
        "_one_url_repair", ROOT / "repair_one_url_many_rows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_database_is_configured_when_ci_requires_one():
    if not REQUIRE:
        pytest.skip("ONE_URL_REPAIR_DSN not set — no Postgres to run against")
    assert DSN, ("ONE_URL_REPAIR_REQUIRE=1 but ONE_URL_REPAIR_DSN is empty — "
                 "every test in this file would skip and prove nothing")


@pytest.fixture
def db(monkeypatch):
    if not DSN:
        pytest.skip("ONE_URL_REPAIR_DSN not set — no Postgres to run against")
    name = f"one_url_repair_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {name}")
        cur.execute(f"SET search_path TO {name}")
        cur.execute(DDL)
        _insert(cur, ROWS)
    opened = []

    def open_conn():
        c = psycopg2.connect(DSN, options=f"-c search_path={name}")
        opened.append(c)
        return c

    main = types.ModuleType("main")
    main.get_read_db = open_conn
    main.get_db = open_conn
    monkeypatch.setitem(sys.modules, "main", main)
    import util.facility_ner_noindex as ner
    monkeypatch.setattr(ner, "refresh_suppressed_slugs", lambda *_a, **_k: frozenset())
    conn = open_conn()
    try:
        yield conn
    finally:
        for c in opened:
            try:
                c.close()
            except Exception:
                pass
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _elect(conn):
    mod = _repair_module()
    with conn.cursor() as cur:
        cur.execute(mod.ELECTION_SQL)
        return [(int(r[0]), int(r[4])) for r in cur.fetchall()]


def _apply(conn, pointed):
    """Runs the SCRIPT's own UPDATE, not a retyped copy of it."""
    mod = _repair_module()
    with conn.cursor() as cur:
        for rid, keeper in pointed:
            cur.execute(mod.POINT_SQL, (keeper, rid))
    conn.commit()


# ── 1. the election ────────────────────────────────────────────────────────

def test_the_election_points_every_extra_at_one_live_keeper(db):
    pointed = _elect(db)
    assert sorted(pointed) == [(501, 502), (503, 502), (504, 502), (505, 502),
                               (551, 552)], pointed


def test_it_skips_groups_another_lane_has_already_touched(db):
    """A group where anything already points has been decided by another lane
    or a human. Re-deciding it is how two repairs fight."""
    touched = {rid for rid, _k in _elect(db)}
    assert not touched & {511, 512}, "a partly consolidated group was re-decided"
    assert not touched & {521, 522}, "a group with no live row got a dead keeper"
    assert 531 not in touched, "a lone row is not a duplicate of anything"
    assert not touched & {541, 542}, "a real twin pair was treated as one slug"


def test_a_suppressed_row_is_never_the_keeper(db):
    """551 is the richest row in its group by every other ordering key — 0.99
    confidence, 99 MW — and it is suppressed. Electing it would point the live
    row at a dead keeper and the facility would leave BOTH count bases at once."""
    assert (551, 552) in _elect(db), "the suppressed row should be POINTED"
    assert not any(k == 551 for _r, k in _elect(db)), \
        "a suppressed row was elected keeper"


# ── 2. ★ the page does not move ────────────────────────────────────────────

def test_the_page_still_answers_at_the_same_url_after_the_repair(db):
    """The claim the whole repair rests on. served_slugs is the page's own
    resolution: if the pointers made case B fire, the slug would land on the
    keeper's slug and 4 URLs would start 301ing."""
    before = fpp.served_slugs([SRN])
    assert before.get(SRN) == SRN, f"fixture already redirects: {before}"
    _apply(db, _elect(db))
    after = fpp.served_slugs([SRN])
    assert after.get(SRN) == SRN, (
        f"the repair moved the page: /facilities/{SRN} now lands on "
        f"{after.get(SRN)} — every pointer it wrote is a fresh 301")


def test_the_public_listing_now_shows_the_facility_once(db):
    """What the user sees. /api/v1/facilities is
    `FROM discovered_facilities WHERE duplicate_of_id IS NULL` — that predicate
    returned all five rows, which is why the 5-result free preview was one
    building five times."""
    def visible():
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                        " WHERE canonical_slug = %s AND duplicate_of_id IS NULL",
                        (SRN,))
            return cur.fetchone()[0]
    assert visible() == 5
    _apply(db, _elect(db))
    assert visible() == 1


# ── 3. it can be run twice, and undone ─────────────────────────────────────

def test_the_repair_never_suppresses_a_row(db):
    """Pointer only. Setting the flag as well would drop the facility out of the
    FLAG basis too, and a slug whose rows are all suppressed leaves every
    is_duplicate-filtered count while still serving 200 — suppression deletes a
    page, a canonical merges it."""
    _apply(db, _elect(db))
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                    " WHERE canonical_slug = %s AND COALESCE(is_duplicate, 0) <> 0",
                    (SRN,))
        assert cur.fetchone()[0] == 0, "the repair suppressed rows it should only point"


def test_the_write_cannot_overwrite_a_pointer_that_already_exists(db):
    """`AND duplicate_of_id IS NULL` is what makes the rollback exact. Without
    it a re-run could move a pointer another lane set, and restoring NULL would
    then be wrong."""
    mod = _repair_module()
    with db.cursor() as cur:
        cur.execute(mod.POINT_SQL, (999, 512))   # 512 already points at 511
        assert cur.rowcount == 0, "the update overwrote an existing pointer"
        cur.execute("SELECT duplicate_of_id FROM discovered_facilities WHERE id = 512")
        assert cur.fetchone()[0] == 511
    db.rollback()


# ── 4. the WRITE guard, against real SQL ───────────────────────────────────
# The probe asks two questions in one statement. A fake cursor answers the
# statement, not the questions, so only a database can show that BOTH arms
# bite — and each arm is the only one that can catch its own case.

def _fac(**over):
    fac = {"name": "Row 501", "provider": "South Reach Networks",
           "city": "Fort Pierce", "state": "FL", "country": "US",
           "latitude": None, "longitude": None, "power_mw": None, "sqft": None,
           "status": "Announced", "source": "competitor_gap:cloudscene",
           "source_url": "https://cloudscene.com/brand-new-url",
           "confidence_score": 0.55, "discovered_at": "2026-09-12",
           "notes": "", "investment_usd": None, "acreage": None}
    fac.update(over)
    return fac


def _rows_now(conn, **where):
    col, val = next(iter(where.items()))
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM discovered_facilities WHERE {col} = %s",
                    (val,))
        return cur.fetchone()[0]


def test_the_write_refuses_a_sibling_the_freeze_has_not_reached_yet(db):
    """The live case. Row 501 exists with provider+name X and — like every row
    inserted minutes ago — NO stored canonical_slug. Only the provider+name arm
    can see it, and this is the row that became five."""
    import news_facility_extractor as nfe
    with db.cursor() as cur:
        cur.execute("UPDATE discovered_facilities SET canonical_slug = NULL "
                    " WHERE id = 501")
    db.commit()
    before = _rows_now(db, name="Row 501")
    assert nfe.insert_discovered_facility(db, _fac()) is None
    assert _rows_now(db, name="Row 501") == before, "a sixth row was written"


def test_the_write_refuses_a_row_wearing_an_ALREADY_FROZEN_slug(db):
    """The other arm, and the only one that can catch this: the stored slug
    matches what the new row composes, while provider+name are spelled
    differently, so a provider+name-only probe would let it through."""
    import news_facility_extractor as nfe
    from routes.facility_slug_freeze import build_canonical_slug
    slug = build_canonical_slug("Brand New Operator", "Brand New Campus")
    assert slug
    with db.cursor() as cur:
        cur.execute("INSERT INTO discovered_facilities "
                    " (id, name, provider, city, country, canonical_slug) "
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (601, "Different Spelling", "Someone Else", "Fort Pierce",
                     "US", slug))
    db.commit()
    assert nfe.insert_discovered_facility(
        db, _fac(name="Brand New Campus", provider="Brand New Operator")) is None
    assert _rows_now(db, canonical_slug=slug) == 1


def test_the_write_still_inserts_a_genuinely_new_facility(db):
    """Non-vacuity: a guard that refused everything passes both tests above."""
    import news_facility_extractor as nfe
    new_id = nfe.insert_discovered_facility(
        db, _fac(name="Wholly Unrelated Campus", provider="Nobody At All"))
    assert new_id, "the guard refused a facility nothing else holds"
    assert _rows_now(db, name="Wholly Unrelated Campus") == 1


def test_running_it_again_finds_nothing(db):
    _apply(db, _elect(db))
    assert _elect(db) == [], "the repair is not idempotent"


def test_the_rollback_is_exact(db):
    pointed = _elect(db)
    _apply(db, pointed)
    with db.cursor() as cur:
        cur.execute("UPDATE discovered_facilities SET duplicate_of_id = NULL "
                    " WHERE id = ANY(%s)", ([r for r, _k in pointed],))
    db.commit()
    assert sorted(_elect(db)) == sorted(pointed), \
        "after the rollback the election does not reproduce"
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                    " WHERE duplicate_of_id IS NOT NULL")
        # only the fixture's own pre-existing pointers (512, 541) survive
        assert cur.fetchone()[0] == 2
