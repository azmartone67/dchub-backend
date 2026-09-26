"""canonical_stats._query_live(): one failing read must not unmeasure the rest (2026-09-23).

_query_live() opens ONE psycopg2 connection (autocommit off by default) and
runs its early metric reads (facilities, the keeper-distinct count,
facilities_distinct, countries, countries_verified, markets, dcpi_countries,
deals, news_sources) each in `try: ... except Exception: pass`, with no
rollback between them. psycopg2 aborts the whole transaction on a failed
statement, so one failure (a statement timeout, a dropped column) made every
later read raise InFailedSqlTransaction. Each of those kept the cached or seed
value, and _live_keys never marked it, so stat_is_live() said "not measured"
about numbers the database would have returned. The per-count loop further down
already rolled back for exactly this reason; the early block did not.

Same abort-cascade class as linkedin_quad_daily._build_dcpi_mover (#5358) and
the one routes/mcp_sse_events._fetch_dcpi_verdict_shifts documents. Latent in
production today: every early read succeeds there.

The fix puts the connection in autocommit, so each read is its own
transaction. A fake cursor cannot hold an aborted transaction, so this runs
against a real Postgres.

  C1 discovered_facilities has no is_duplicate column: the keeper-distinct and
     countries_verified reads fail. facilities_distinct, which runs right after
     the keeper read, and markets, which runs right after countries_verified,
     are still measured from the seeded rows.
  C2 discovered_facilities is absent: all five facility reads fail and stay
     UNMEASURED (the seed value, never 0), and markets is still measured.
  C0 the control: every table healthy, so every one of those keys is measured.
     C1's "not measured" is therefore caused by the missing column, not by the
     fixture.

Set CANON_LIVE_READS_SQL_DSN to run it. CI passes the db-parity service DSN and
then asserts this file did not skip. Owns and recreates only
discovered_facilities and market_power_scores.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")
# canon imports routes.dcpi mid-read (dcpi_countries), and that module needs flask.
pytest.importorskip("flask")

DSN = os.environ.get("CANON_LIVE_READS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="CANON_LIVE_READS_SQL_DSN not set — no Postgres to run against")

# Both UNIQUE constraints carry production's NAMES. Importing routes.dcpi runs
# _phase215_ensure_unique(), which ALTERs this table unless
# `market_power_scores_slug_key` exists. Production has the name, so there it
# is a no-op, and here too. Before the autocommit fix canon imported it while
# holding its own transaction open on this table, so an auto-named constraint
# made the ALTER wait on canon (lock_timeout below turns that into a failure).
_MPS_DDL = """CREATE TABLE market_power_scores (
       id SERIAL PRIMARY KEY, market_slug TEXT NOT NULL,
       CONSTRAINT market_power_scores_slug_key UNIQUE (market_slug),
       CONSTRAINT market_power_scores_slug_unique UNIQUE (market_slug),
       market_name TEXT NOT NULL, state TEXT, iso TEXT,
       latitude REAL, longitude REAL, constraint_score REAL,
       excess_power_score REAL, time_to_power_months REAL, verdict TEXT,
       tier_required TEXT DEFAULT 'free',
       computed_at TIMESTAMPTZ DEFAULT now(), published BOOLEAN DEFAULT false,
       quality_score INTEGER DEFAULT 0, iso_type TEXT, signal_tier TEXT,
       method_version TEXT)"""


def _disc_ddl(with_is_duplicate):
    return ("CREATE TABLE discovered_facilities ("
            " id SERIAL PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,"
            " country TEXT, discovered_at TEXT NOT NULL, canonical_slug TEXT"
            + (", is_duplicate INTEGER NOT NULL DEFAULT 0" if with_is_duplicate else "")
            + ")")


# Every key below is read in the early block. countries is the one that has
# never marked itself live, so it is checked by value instead.
_FACILITY_KEYS = ("facilities", "facilities_with_keeper_distinct",
                  "facilities_distinct",
                  "countries_verified")
_OWNED = ("discovered_facilities", "market_power_scores")


def _connect(autocommit=True):
    c = psycopg2.connect(DSN)
    c.autocommit = autocommit
    return c


def _exec(*stmts, params=None):
    c = _connect()
    try:
        with c.cursor() as cur:
            for s in stmts:
                cur.execute(s, params)
    finally:
        c.close()


def _seed_facilities():
    # 5 rows, 4 distinct slugs, 3 distinct countries.
    _exec("INSERT INTO discovered_facilities (source, name, country, "
          "discovered_at, canonical_slug) VALUES "
          "('t','A1','US','2026-09-01','a'), ('t','A2','US','2026-09-01','a'), "
          "('t','B','DE','2026-09-01','b'), ('t','C','JP','2026-09-01','c'), "
          "('t','D','US','2026-09-01','d')")


def _seed_markets():
    # 7 distinct published market names. Excluded, and each would move the
    # count if the published/aggregate filters broke: an unpublished row, an
    # aggregate region, and a second slug for a name already counted.
    _exec("INSERT INTO market_power_scores (market_slug, market_name, state, iso, "
          "published) SELECT 'm-'||g, 'Market '||g, 'TX', 'ERCOT', true "
          "FROM generate_series(1, 7) g",
          "INSERT INTO market_power_scores (market_slug, market_name, state, iso, "
          "published) VALUES "
          "('m-unpub', 'Unpublished', 'TX', 'ERCOT', false), "
          "('rural-spp', 'Rural SPP', 'KS', 'SPP', true), "
          "('m-1-tx', 'Market 1', 'TX', 'ERCOT', true)")


@pytest.fixture
def canon(monkeypatch):
    """Recreate the owned tables empty, point canon at this database with a
    default (autocommit OFF) psycopg2 connection, and start canon cold."""
    import canonical_stats as cs
    # libpq reads PGOPTIONS on every connect, the code under test's included:
    # a lock wait fails in seconds instead of hanging the job (see _MPS_DDL).
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    _exec(*[f"DROP TABLE IF EXISTS {t}" for t in _OWNED])
    _exec(_MPS_DDL)
    _seed_markets()
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    # canonical_stats forces sslmode=require; the throwaway Postgres has no TLS.
    # Only the connection is replaced, and it is handed over exactly as
    # psycopg2.connect() returns it: autocommit off. Canon's own SQL and its
    # own transaction handling run unchanged.
    monkeypatch.setattr(cs, "_conn", lambda: _connect(autocommit=False))
    monkeypatch.setattr(cs, "_cache", None)
    monkeypatch.setattr(cs, "_cache_ts", 0.0)
    monkeypatch.setattr(cs, "_live_keys", set())
    return cs


def test_c0_control_every_early_read_is_measured(canon):
    _exec(_disc_ddl(with_is_duplicate=True))
    _seed_facilities()

    out = canon.get_canonical_stats(force=True)

    for k in _FACILITY_KEYS + ("markets",):
        assert canon.stat_is_live(k), (k, out.get(k))
    assert out["facilities"] == 5
    assert out["facilities_with_keeper_distinct"] == 4
    assert out["facilities_distinct"] == 4
    assert out["countries"] == 3
    assert out["countries_verified"] == 3
    assert out["markets"] == 7


def test_c1_a_dropped_column_does_not_unmeasure_the_reads_after_it(canon):
    _exec(_disc_ddl(with_is_duplicate=False))
    _seed_facilities()

    out = canon.get_canonical_stats(force=True)

    # The failures happened: both reads that name is_duplicate are unmeasured.
    assert not canon.stat_is_live("facilities_with_keeper_distinct")
    assert not canon.stat_is_live("countries_verified")
    # The read right after the keeper read is measured...
    assert canon.stat_is_live("facilities_distinct")
    assert out["facilities_distinct"] == 4
    assert out["countries"] == 3
    # ...and so is the one right after countries_verified: the DCPI market
    # count, from the seeded rows, not canon's seed.
    assert canon.stat_is_live("markets")
    assert out["markets"] == 7


def test_c2_an_absent_table_stays_unmeasured_and_markets_is_still_read(canon):
    seed = dict(canon._FALLBACK)

    out = canon.get_canonical_stats(force=True)

    # All five facility reads failed and stay ABSENT: not live, and the seed
    # value rather than a 0, which would be a measurement.
    for k in _FACILITY_KEYS:
        assert not canon.stat_is_live(k), k
        assert out.get(k) == seed.get(k), (k, out.get(k), seed.get(k))
    assert out.get("countries") == seed.get("countries")
    # The next read, on a different table, is measured.
    assert canon.stat_is_live("markets")
    assert out["markets"] == 7
