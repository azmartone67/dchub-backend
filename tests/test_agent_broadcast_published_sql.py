"""agent_broadcast's Pass 2 fallback serves PUBLISHED DCPI rows only (2026-09-23).

routes/agent_broadcast._fetch_dcpi_verdict_shifts is two-pass. Pass 1 reads
dcpi_daily_snapshots, whose writer applies the publish gate. Pass 2 fires on a
day with no genuine shift and reads market_power_scores DIRECTLY, and it had no
`published` filter. The column DEFAULTs to false, and production holds two
unpublished BUILD/AVOID rows: the retired alias twins cheyenne-wy (BUILD) and
northern-virginia (AVOID), last computed 2026-07-19 (read-only check
2026-09-23). They rank 258-259 of 259 by computed_at, below Pass 2's [:20] cut,
so the leak was latent; a recompute of either would have put it at the top of
/api/v1/agent-broadcast/dcpi-shifts as "DCPI rates Cheyenne, WY BUILD".

  P1 through the shipped /dcpi-shifts route, with no genuine shift: the
     fallback runs (control: published rows ARE served) and every unpublished
     row is absent, even though the unpublished ones are the most recently
     computed and would lead the list
  P2 a NULL `published` is not published (fail closed): COALESCE(published,
     true) is the writer-side idiom and must not leak into this reader

Tables are created with production's column types and constraint names
(information_schema / pg_constraint, 2026-09-23). Set
AGENT_BROADCAST_PUBLISHED_SQL_DSN to run it. CI passes the db-parity service
DSN and then asserts this file did not skip. Owns and recreates only
market_power_scores and dcpi_daily_snapshots.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("flask")

DSN = os.environ.get("AGENT_BROADCAST_PUBLISHED_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="AGENT_BROADCAST_PUBLISHED_SQL_DSN not set — no Postgres to run against")

_OWNED = ("market_power_scores", "dcpi_daily_snapshots")

# Production's constraint NAMES, not just types: importing routes.dcpi ALTERs
# this table unless market_power_scores_slug_key exists.
_MPS_DDL = """CREATE TABLE market_power_scores (
       id SERIAL PRIMARY KEY, market_slug TEXT NOT NULL,
       CONSTRAINT market_power_scores_slug_key UNIQUE (market_slug),
       CONSTRAINT market_power_scores_slug_unique UNIQUE (market_slug),
       market_name TEXT NOT NULL, state TEXT, iso TEXT,
       constraint_score REAL, excess_power_score REAL, verdict TEXT,
       computed_at TIMESTAMPTZ DEFAULT now(), published BOOLEAN DEFAULT false,
       method_version TEXT)"""
_SNAP_DDL = """CREATE TABLE dcpi_daily_snapshots (
       id SERIAL PRIMARY KEY, snapshot_date DATE NOT NULL,
       market_slug TEXT NOT NULL, market_name TEXT,
       excess_power_score REAL, constraint_score REAL, verdict TEXT,
       captured_at TIMESTAMPTZ NOT NULL DEFAULT now(), method_version TEXT)"""

# (slug, name, verdict, excess, constraint, published, computed N minutes ago)
_ROWS = [
    # The two production twins, computed MOST recently here so that without
    # the filter they would lead Pass 2's recency-sorted list.
    ("cheyenne-wy",       "Cheyenne, WY",      "BUILD",   70, 30, False, 1),
    ("northern-virginia", "Northern Virginia", "AVOID",   20, 80, False, 2),
    ("null-flag",         "Null Flag, TX",     "BUILD",   70, 30, None,  3),
    ("alpha",             "Alpha, TX",         "BUILD",   70, 30, True,  10),
    ("bravo",             "Bravo, OH",         "AVOID",   20, 80, True,  11),
    ("charlie",           "Charlie, VA",       "CAUTION", 55, 60, True,  12),
]
_PUBLISHED_DECISIVE = {"alpha", "bravo"}
_NEVER = {"cheyenne-wy", "northern-virginia", "null-flag"}


def _connect():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    return c


def _exec(sql, params=None):
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
    finally:
        c.close()


@pytest.fixture
def db(monkeypatch):
    # A lock wait fails in seconds instead of hanging the job.
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    for t in _OWNED:
        _exec(f"DROP TABLE IF EXISTS {t}")
    _exec(_MPS_DDL)
    _exec(_SNAP_DDL)   # empty: no genuine shift, so Pass 2 must run
    for slug, name, verdict, ex, con, pub, mins in _ROWS:
        _exec("INSERT INTO market_power_scores (market_slug, market_name, iso, "
              "verdict, excess_power_score, constraint_score, published, "
              "computed_at) VALUES (%s,%s,'ERCOT',%s,%s,%s,%s, "
              "now() ON CONFLICT DO NOTHING - make_interval(mins => %s))",
              (slug, name, verdict, ex, con, pub, mins))
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    from routes import agent_broadcast as ab
    # The route's poller write-through is not under test; keep it off the
    # shared parity database.
    monkeypatch.setattr(ab, "_persist_poller", lambda *a, **k: None)
    return ab


def _slugs(items):
    """The market slug each dcpi item points at (its url ends in the slug)."""
    return {(i.get("url") or "").rstrip("/").rsplit("/", 1)[-1] for i in items}


def test_p1_dcpi_shifts_route_serves_published_rows_only(db):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(db.agent_broadcast_bp)

    resp = app.test_client().get("/api/v1/agent-broadcast/dcpi-shifts?days=7")

    assert resp.status_code == 200, resp.data[:300]
    items = [i for i in resp.get_json()["items"]
             if i.get("kind") == "dcpi_verdict_shift"]
    served = _slugs(items)
    # Control: the fallback ran and read this database.
    assert served == _PUBLISHED_DECISIVE, (served, items)
    assert not served & _NEVER, items
    assert not any(n in str(items) for n in ("Cheyenne", "Northern Virginia",
                                             "Null Flag")), items


def test_p2_a_null_published_flag_is_not_published(db):
    items = db._fetch_dcpi_verdict_shifts(7)

    assert "null-flag" not in _slugs(items), items
    assert _slugs(items) == _PUBLISHED_DECISIVE, items
