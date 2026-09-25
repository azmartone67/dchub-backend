"""The readers of the phantom dcpi_scores table, against a real Postgres (2026-09-23).

dcpi_scores has never existed in production (read-only check 2026-09-23:
to_regclass('public.dcpi_scores') IS NULL), and neither has dcpi_v2_scores.
#5354 removed the radar's SLA row for it. Three readers still named them:

1. routes/agent_winback_digest._changes read `count(DISTINCT market) FROM
   dcpi_scores` with default=232. It always raised inside its SAVEPOINT, so
   every digest with new facilities said "232 tracked power markets". It now
   takes canon's floor (canonical_stats.markets_phrase, gated on
   stat_is_live) and drops the clause when canon has not measured it.

2. routes/linkedin_quad_daily._build_dcpi_mover probed dcpi_v2_scores, then
   dcpi_scores, then market_power_scores inside ONE psycopg2 transaction. The
   first UndefinedTable aborted it and the real probe raised
   InFailedSqlTransaction (replayed read-only on production 2026-09-23:
   "current transaction is aborted", while the same market_power_scores query
   alone returned 334 rows). The function then returned one of eight typed
   markets with typed scores, for a public LinkedIn post. It now reads the
   newest dcpi_daily_snapshots shift via
   mcp_sse_events._fetch_dcpi_verdict_shifts, falls back to a published,
   on-band market_power_scores row, and otherwise returns None, and run()
   skips the slot (owner decision, 2026-09-23).

3. routes/dcpi_cron_health's no-recent-runs fallback read dcpi_v2_scores, so
   the one time it runs (the recompute cron is dead) it had no verdict.

  W1 canon measured: the shipped preview route renders canon's floor, counted
     over PUBLISHED rows only, and never 232
  W2 canon unmeasured (no market_power_scores): no market count at all: not
     232, and not canon's static seed either
  L1 a genuine newest-snapshot shift is the mover, and a restatement with a
     bigger swing is not; the post names both verdicts and both dates
  L2 no shift: a published, on-band market_power_scores row, with the phantom
     tables absent (the exact probe the aborted transaction used to kill); the
     post does not say anything moved
  L3 a shift from a stale snapshot is not a 24h mover
  L4 only ineligible rows (unpublished / off-band / computed 3 days ago): None,
     and run() skips the slot, posts nothing, and stamps why on the claim
  H1 dcpi_cron_health, with no recent dcpi_runs row, measures
     market_power_scores: fresh reads ok, and 30h-old reads stale

Tables are created with production's column types (information_schema,
2026-09-23); linkedin_quad_posts by the module's own _ensure_table().

Set DCPI_SCORES_READERS_SQL_DSN to run it. CI passes the db-parity service DSN
and then asserts this file did not skip. Owns and recreates only
market_power_scores, dcpi_daily_snapshots, discovered_facilities,
linkedin_quad_posts and dcpi_runs, and drops the phantom dcpi_scores,
dcpi_v2_scores, dcpi_v2_runs, dcpi_recompute_log and brain_cron_runs.
"""
import datetime as _dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("flask")

DSN = os.environ.get("DCPI_SCORES_READERS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="DCPI_SCORES_READERS_SQL_DSN not set — no Postgres to run against")

_OWNED = ("market_power_scores", "dcpi_daily_snapshots", "discovered_facilities",
          "linkedin_quad_posts", "dcpi_runs")
# Names that do not exist in production. Dropped so a stray copy in the test
# database cannot mask the failure being tested.
_PHANTOM = ("dcpi_scores", "dcpi_v2_scores", "dcpi_v2_runs",
            "dcpi_recompute_log", "brain_cron_runs")

# Both UNIQUE constraints carry production's NAMES (pg_constraint, 2026-09-23).
# Importing routes.dcpi runs _phase215_ensure_unique(), which ALTERs this table
# unless `market_power_scores_slug_key` exists; canon imports it mid-read, and
# until canon's reads went autocommit (2026-09-23) an auto-named constraint
# turned that ALTER into a lock wait on canon's own open transaction and the
# test hung. Production has the name, so it is a no-op.
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
_SNAP_DDL = """CREATE TABLE dcpi_daily_snapshots (
       id SERIAL PRIMARY KEY, snapshot_date DATE NOT NULL,
       market_slug TEXT NOT NULL, market_name TEXT,
       excess_power_score REAL, constraint_score REAL, verdict TEXT,
       captured_at TIMESTAMPTZ NOT NULL DEFAULT now(), method_version TEXT)"""
_DISC_DDL = """CREATE TABLE discovered_facilities (
       id SERIAL PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
       country TEXT, discovered_at TEXT NOT NULL, canonical_slug TEXT,
       is_duplicate INTEGER NOT NULL DEFAULT 0,
       first_seen TIMESTAMPTZ DEFAULT now())"""
_RUNS_DDL = """CREATE TABLE dcpi_runs (
       id SERIAL PRIMARY KEY, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
       markets_scored INTEGER, error_count INTEGER, source TEXT, notes TEXT)"""

# The eight markets the retired fallback typed in. None may ever be a mover.
_TYPED = {"Cheyenne, WY", "Midlothian, TX", "Rural SPP, Kansas",
          "Council Bluffs, IA", "Hillsboro, OR", "Quincy, WA", "Boardman, OR",
          "New Albany, OH"}


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


def _one(sql, params=None):
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        c.close()


def _mps(slug, name, verdict, excess, constraint, *, published=True,
         age="0 hours", iso="ERCOT"):
    _exec("INSERT INTO market_power_scores (market_slug, market_name, iso, "
          "verdict, excess_power_score, constraint_score, published, "
          "computed_at, method_version) "
          "VALUES (%s,%s,%s,%s,%s,%s,%s, now() - %s::interval, '2.2.1')",
          params=(slug, name, iso, verdict, excess, constraint, published, age))


def _snap(day, slug, name, verdict, excess, constraint, method="2.2.1"):
    _exec("INSERT INTO dcpi_daily_snapshots (snapshot_date, market_slug, "
          "market_name, verdict, excess_power_score, constraint_score, "
          "method_version) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
          params=(day, slug, name, verdict, excess, constraint, method))


def _today():
    return _dt.datetime.now(_dt.timezone.utc).date()


@pytest.fixture
def db(monkeypatch):
    """Recreate the owned tables empty, drop the phantoms, point every reader
    under test at this database, and start canon cold."""
    import canonical_stats as cs
    # libpq reads PGOPTIONS on every connect, the code under test's included:
    # a lock wait fails in seconds instead of hanging the job (see _MPS_DDL).
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    _exec(*[f"DROP TABLE IF EXISTS {t}" for t in _OWNED + _PHANTOM])
    _exec(_MPS_DDL, _SNAP_DDL, _DISC_DDL, _RUNS_DDL)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    # canonical_stats forces sslmode=require; the throwaway Postgres has no TLS.
    # Only the connection is replaced — canon's own SQL runs unchanged.
    monkeypatch.setattr(cs, "_conn", lambda: _connect(autocommit=False))
    monkeypatch.setattr(cs, "_cache", None)
    monkeypatch.setattr(cs, "_cache_ts", 0.0)
    monkeypatch.setattr(cs, "_live_keys", set())
    from routes import linkedin_quad_daily as lq
    lq._ensure_table()
    assert _one("SELECT to_regclass('public.linkedin_quad_posts')")[0] is not None
    for t in _PHANTOM:
        assert _one("SELECT to_regclass(%s)", (f"public.{t}",))[0] is None, t
    yield


# ── agent_winback_digest ────────────────────────────────────────────────

def _preview(monkeypatch):
    """Render the digest through the shipped admin preview route."""
    from flask import Flask
    from routes import agent_winback_digest as awd
    monkeypatch.setattr(awd, "_conn", lambda: _connect(autocommit=False))
    monkeypatch.setattr(awd, "_release", lambda c: c.close())
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    app = Flask(__name__)
    app.register_blueprint(awd.agent_winback_digest_bp)
    resp = app.test_client().get("/api/v1/admin/agent-digest/preview?email=a@example.com",
                                 headers={"X-Admin-Key": "test-admin-key"})
    assert resp.status_code == 200, resp.data[:300]
    return resp.get_data(as_text=True)


def _two_new_facilities():
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for i in (1, 2):
        _exec("INSERT INTO discovered_facilities (source, name, country, "
              "discovered_at, canonical_slug) VALUES ('t', %s, 'US', %s, %s) ON CONFLICT DO NOTHING",
              params=(f"DC {i}", now, f"dc-{i}"))


def test_w1_digest_market_count_is_canons_published_floor(db, monkeypatch):
    _two_new_facilities()
    # 120 published markets and 90 unpublished. Canon floors 120 to "100+";
    # counting the unpublished rows too would give 210 -> "200+", and canon's
    # static seed is 300 -> "300+", so "100+" names this measurement alone.
    _exec("INSERT INTO market_power_scores (market_slug, market_name, verdict, "
          "excess_power_score, constraint_score, published) "
          "SELECT 'm-'||g, 'Market '||g, 'CAUTION', 55, 60, g <= 120 "
          "FROM generate_series(1, 210) g")

    html = _preview(monkeypatch)

    assert ("<strong>2 newly discovered facilities</strong> added across "
            "100+ tracked power markets.</p>") in html, html
    assert "232" not in html, html


def test_w2_digest_says_no_market_count_when_canon_has_none(db, monkeypatch):
    _two_new_facilities()
    _exec("DROP TABLE market_power_scores")

    html = _preview(monkeypatch)

    # The facilities sentence still renders (the control: the digest ran)...
    assert "<strong>2 newly discovered facilities</strong> added.</p>" in html, html
    # ...with no market count at all: not 232, and not canon's unmeasured seed.
    assert "tracked power markets" not in html, html
    assert "232" not in html and "300+" not in html, html


# ── linkedin_quad_daily._build_dcpi_mover ───────────────────────────────

@pytest.fixture
def lq(db, monkeypatch):
    from routes import linkedin_quad_daily as lq
    # The market-count clause in the post is canon's, tested above. Keep it
    # off this database so a 4-market fixture does not read as "0+".
    import canonical_stats as cs
    monkeypatch.setattr(cs, "_conn", lambda: None)
    return lq


def _render(lq, payload):
    return lq._format_post_base(lq.SLOTS[0], payload)


def test_l1_the_mover_is_the_newest_genuine_shift_not_a_restatement(lq):
    t, y = _today(), _today() - _dt.timedelta(days=1)
    for slug, name in (("alpha", "Alpha, TX"), ("bravo", "Bravo, OH"),
                       ("charlie", "Charlie, VA"), ("delta", "Delta, WY")):
        _mps(slug, name, "CAUTION", 55, 60)
    # Genuine, on-band both days: BUILD -> CAUTION, 6 pts.
    _snap(y, "alpha", "Alpha, TX", "BUILD", 66, 40)
    _snap(t, "alpha", "Alpha, TX", "CAUTION", 60, 40)
    # Genuine, on-band both days: CAUTION -> AVOID, 25 pts. The mover.
    _snap(y, "bravo", "Bravo, OH", "CAUTION", 55, 60)
    _snap(t, "bravo", "Bravo, OH", "AVOID", 30, 60)
    # Restatement: method_version changed. 50 pts, so it would win if counted.
    _snap(y, "charlie", "Charlie, VA", "BUILD", 70, 30, method="2.0.1")
    _snap(t, "charlie", "Charlie, VA", "AVOID", 20, 80)
    _snap(y, "delta", "Delta, WY", "BUILD", 70, 30)
    _snap(t, "delta", "Delta, WY", "BUILD", 70, 30)

    p = lq._build_dcpi_mover()

    assert p is not None
    assert (p["market"], p["prior_verdict"], p["verdict"], p["source"]) == \
        ("Bravo, OH", "CAUTION", "AVOID", "dcpi_daily_snapshots"), p
    assert (p["snapshot_date"], p["since"]) == (t.isoformat(), y.isoformat()), p
    assert round(p["score"], 1) == 30.0, p
    text = _render(lq, p)
    assert "Bravo, OH moved CAUTION → AVOID on the DC Power Index" in text, text
    assert f"snapshot {t.isoformat()} vs {y.isoformat()}" in text, text
    assert "just hit" not in text, text


def test_l2_no_shift_falls_back_to_a_published_on_band_score(lq):
    # No snapshots at all, and dcpi_v2_scores / dcpi_scores absent: the shape
    # in which the old chain aborted before reaching this table.
    _mps("echo", "Echo, NE", "BUILD", 70, 30)

    p = lq._build_dcpi_mover()

    assert p is not None and p["market"] not in _TYPED, p
    assert (p["market"], p["verdict"], p["source"]) == \
        ("Echo, NE", "BUILD", "market_power_scores"), p
    assert round(p["score"], 1) == 70.0 and not p.get("prior_verdict"), p
    text = _render(lq, p)
    assert "Echo, NE scores 70.0/100 on the DC Power Index (BUILD)" in text, text
    assert " moved " not in text and "move signals" not in text, text


def test_l3_a_stale_snapshot_shift_is_not_a_24h_mover(lq):
    old, older = _today() - _dt.timedelta(days=10), _today() - _dt.timedelta(days=11)
    # Foxtrot's live row is outside the 48h window, so Golf is the ONLY row
    # the fallback may pick (it orders by RANDOM()): a Foxtrot answer can only
    # come from the stale snapshot.
    _mps("foxtrot", "Foxtrot, IA", "AVOID", 30, 60, age="10 days")
    _snap(older, "foxtrot", "Foxtrot, IA", "BUILD", 70, 30)
    _snap(old, "foxtrot", "Foxtrot, IA", "AVOID", 30, 60)
    _mps("golf", "Golf, OK", "CAUTION", 55, 60)

    p = lq._build_dcpi_mover()

    assert p is not None
    assert (p["market"], p["source"]) == ("Golf, OK", "market_power_scores"), p


def _ineligible_only():
    """One market_power_scores row per reason it must not be posted."""
    _mps("hotel", "Hotel, VA", "BUILD", 70, 30, published=False)   # unpublished
    _mps("india", "India, TX", "BUILD", 40, 80)                     # off-band (bands say AVOID)
    _mps("juliet", "Juliet, OR", "CAUTION", 55, 60, age="3 days")   # not recomputed in 48h


def test_l4_nothing_real_returns_none(lq):
    _ineligible_only()

    assert lq._build_dcpi_mover() is None


def test_l4_run_skips_the_slot_posts_nothing_and_says_why(lq, monkeypatch):
    from flask import Flask
    from routes import linkedin_content_engine, media_editorial
    _ineligible_only()

    def _engine_down(**_kw):
        raise RuntimeError("engine down")

    def _must_not_post(*_a, **_kw):
        raise AssertionError("posted to LinkedIn with no DCPI row")

    monkeypatch.setattr(linkedin_content_engine, "compose_story_post", _engine_down)
    monkeypatch.setattr(media_editorial, "editorial_decision",
                        lambda *_a, **_kw: {"post": True, "lead": None})
    monkeypatch.setattr(lq, "_post_to_linkedin", _must_not_post)
    app = Flask(__name__)
    app.register_blueprint(lq.linkedin_quad_bp)

    resp = app.test_client().post("/api/v1/linkedin-quad/run?force=1&topic=dcpi_mover")

    body = resp.get_json()
    assert resp.status_code == 200, resp.data[:400]
    assert (body.get("skipped"), body.get("reason")) == (True, "no_dcpi_data"), body
    assert "engine down" in (body.get("engine_error") or ""), body
    row = _one("SELECT success, error_msg, post_text FROM linkedin_quad_posts "
               "WHERE slot_date = %s AND slot_hour = 8",
               (_today(),))
    assert row is not None, "run() never claimed the slot"
    success, error_msg, post_text = row
    assert success is False and post_text is None, row
    assert error_msg.startswith("suppressed: no_dcpi_data"), row


# ── dcpi_cron_health ────────────────────────────────────────────────────

def _health():
    from flask import Flask
    from routes import dcpi_cron_health as h
    app = Flask(__name__)
    app.register_blueprint(h.dcpi_health_bp)
    resp = app.test_client().get("/api/v1/cron/dcpi/health")
    assert resp.status_code == 200, resp.data[:300]
    return resp.get_json()


@pytest.mark.parametrize("age,verdict", [("1 hour", "ok"), ("30 hours", "stale")])
def test_h1_cron_health_falls_back_to_market_power_scores(db, age, verdict):
    # A dcpi_runs row, but none in the 48h window: the dead-cron shape.
    _exec("INSERT INTO dcpi_runs (started_at, markets_scored, error_count) "
          "VALUES (now() - interval '5 days', 300, 0)")
    _exec("INSERT INTO market_power_scores (market_slug, market_name, verdict, "
          "excess_power_score, constraint_score, published, computed_at) "
          "SELECT 'm-'||g, 'Market '||g, 'CAUTION', 55, 60, true, "
          "now() - %s::interval FROM generate_series(1, 150) g", params=(age,))

    out = _health()

    fb = out.get("fallback_check") or {}
    assert "_error" not in fb, out
    assert fb.get("source_table") == "market_power_scores", out
    assert fb.get("scores_last_24h") == (150 if verdict == "ok" else 0), out
    assert out.get("verdict") == verdict, out
