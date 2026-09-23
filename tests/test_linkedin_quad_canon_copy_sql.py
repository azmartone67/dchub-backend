"""The LinkedIn quad's legacy post copy against canon on a real Postgres
(2026-09-23).

Two claims the legacy templates in routes/linkedin_quad_daily.py published
with no data behind them:

1. _canon_markets() returned canonical_stats.markets_phrase() unchecked. That
   phrase never fails when canon is unmeasured, it floors the static seed
   (300 -> "300+"), so the post printed "ranks 300+ markets" as if measured.
   It is now gated on stat_is_live("markets"), asked after markets_phrase()
   runs the query, like routes/agent_winback_digest._markets_tracked (#5358).

2. The dcpi_mover template typed "Top BUILD markets right now span 3 ISOs
   (WECC, SPP, ERCOT)". Nothing computed it, and WECC is not an ISO.

  K1 canon measured: both posts that carry the count print canon's floor
     over PUBLISHED markets, not the seed
  K2 canon unmeasured (no market_power_scores): neither post prints a count,
     and neither prints the seed
  I1 the dcpi_mover post makes no ISO claim, and still renders

market_power_scores copies production's column types and constraint NAMES
from tests/test_dcpi_scores_readers_sql.py (read there from information_schema
and pg_constraint, 2026-09-23).

Set LINKEDIN_QUAD_CANON_COPY_SQL_DSN to run it. CI passes the db-parity
service DSN and then asserts this file did not skip. Owns and recreates only
market_power_scores.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("LINKEDIN_QUAD_CANON_COPY_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="LINKEDIN_QUAD_CANON_COPY_SQL_DSN not set — no Postgres to run against")

# Constraint names are production's; see tests/test_dcpi_scores_readers_sql.py
# for why an auto-named UNIQUE here turns canon's read into a lock wait.
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

# canonical_stats._FALLBACK["markets"] floored, i.e. what an unmeasured canon
# printed. Asserted equal to the live module value below, so a seed change
# cannot turn K2's "not the seed" check vacuous.
_SEED_PHRASE = "300+"

_MOVER = {"market": "Lima, NE", "verdict": "BUILD", "score": 71.0,
          "source": "market_power_scores"}


def _connect(autocommit=True):
    c = psycopg2.connect(DSN)
    c.autocommit = autocommit
    return c


def _exec(*stmts):
    c = _connect()
    try:
        with c.cursor() as cur:
            for s in stmts:
                cur.execute(s)
    finally:
        c.close()


@pytest.fixture
def lq(monkeypatch):
    """Recreate market_power_scores empty and start canon cold on it."""
    import canonical_stats as cs
    from routes import linkedin_quad_daily as lq
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    _exec("DROP TABLE IF EXISTS market_power_scores", _MPS_DDL)
    # canonical_stats forces sslmode=require; the throwaway Postgres has no
    # TLS. Only the connection is replaced — canon's own SQL runs unchanged.
    monkeypatch.setattr(cs, "_conn", lambda: _connect(autocommit=False))
    monkeypatch.setattr(cs, "_cache", None)
    monkeypatch.setattr(cs, "_cache_ts", 0.0)
    monkeypatch.setattr(cs, "_live_keys", set())
    assert cs._markets_floor(cs._FALLBACK["markets"]) == _SEED_PHRASE
    yield lq


def _slot(lq, topic):
    return next(s for s in lq.SLOTS if s["topic"] == topic)


def _mover_post(lq):
    return lq._format_post_base(_slot(lq, "dcpi_mover"), _MOVER)


def _pulse_post(lq):
    # Empty news: the builder's own no-news copy, which carries the count.
    return lq._format_post_base(_slot(lq, "industry_pulse"),
                                {"news": None, "market": None})


def test_k1_measured_canon_prints_the_published_floor(lq):
    # 120 published markets and 90 unpublished. Canon floors 120 to "100+";
    # counting the unpublished rows too would give "200+", and the seed is
    # "300+", so "100+" names this measurement alone.
    _exec("INSERT INTO market_power_scores (market_slug, market_name, verdict, "
          "excess_power_score, constraint_score, published) "
          "SELECT 'm-'||g, 'Market '||g, 'CAUTION', 55, 60, g <= 120 "
          "FROM generate_series(1, 210) g")

    mover, pulse = _mover_post(lq), _pulse_post(lq)

    assert "The DC Power Index ranks 100+ markets daily." in mover, mover
    assert "DCPI ranks all 100+ markets on live grid headroom" in pulse, pulse
    assert _SEED_PHRASE not in mover + pulse


def test_k2_unmeasured_canon_prints_no_count_at_all(lq):
    _exec("DROP TABLE market_power_scores")

    mover, pulse = _mover_post(lq), _pulse_post(lq)

    # The count-free sentences render (the control: the templates ran)...
    assert "The DC Power Index ranks markets daily." in mover, mover
    assert "DCPI ranks markets on live grid headroom" in pulse, pulse
    # ...and no number stands in for the measurement, the seed included.
    assert _SEED_PHRASE not in mover + pulse, mover + pulse
    assert "+ markets" not in mover + pulse, mover + pulse


def test_i1_the_mover_post_makes_no_iso_claim(lq):
    mover = _mover_post(lq)

    assert "📊 Lima, NE scores 71.0/100 on the DC Power Index (BUILD)" in mover, mover
    assert "Track them all: " in mover, mover
    assert "ISO" not in mover and "WECC" not in mover, mover
