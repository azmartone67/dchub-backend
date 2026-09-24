"""The LinkedIn quad's legacy builders when they have no data, against a real
Postgres (2026-09-23).

When compose_story_post raises, run() falls back to one legacy builder per
slot topic. #5358 made the dcpi_mover builder's None skip the slot and stamp
'suppressed: no_dcpi_data' on the claim (owner decision: no real row, no
post). The other builders return None too, and run() handed that None on to
`payload["engine_error"] = ...`, which raised TypeError: a Flask 500 that
left the linkedin_quad_posts claim at 'claimed_in_flight', so the media
pulse counted a data drought as an abandoned claim.

The None paths, read off the builders:

  _build_hyperscaler_deal  no DB; the news query raises (swallowed); or no
                           matching headline in the last 3 days
  _build_industry_pulse    no DB; either query raises (swallowed). Empty
                           tables are NOT None: it returns
                           {"news": None, "market": None} and the template
                           renders its own no-news copy
  _build_ai_capex_top5     any urlopen / JSON error. Unreachable from run():
                           no SLOTS entry carries ai_capex_index since the
                           16:00 slot became "capability" (2026-07-18)

  N1 hyperscaler_deal, no fresh matching headline: the real query returns
     no row, run() skips, posts nothing, stamps why on the claim
  N2 hyperscaler_deal, no news table: the swallowed-error path, same outcome
  N3 industry_pulse, no news table: the swallowed-error path, same outcome
  N4 a forced run that lost the claim to a live peer skips without stamping
     the peer's row
  N5 capability (16:00) and a forced agent_demand have no legacy builder:
     they skip and stamp too, instead of the generic template (owner
     decision 2026-09-23)
  P1 industry_pulse quotes a market's DCPI score and verdict, so its market
     must be a quotable row (published, recomputed in 48h, on-band) — one
     case per way a BUILD row with excess > 60 (the old filter) is not
  P2 among ineligible rows the one quotable row is always the one picked
  P3 a quotable row that is not BUILD is never the counter-take market (the
     copy calls it "buildable capacity")
  C1 control: a fresh matching headline is not skipped, is posted, and the
     run still reports the engine error; the post types no "$1B+/week" rate
  C2 control: a fresh headline and a BUILD market are not skipped either

Tables are created from the repo's own DDL: news from
news_aggregator.CREATE_TABLE_SQL, linkedin_quad_posts from the module's
_ensure_table(). news_aggregator is READ, not imported: it sys.exit(1)s when
feedparser is missing, and the db-parity job does not install it (CI run
35926954996 errored every test at setup that way). market_power_scores copies production's column types and
constraint NAMES from tests/test_dcpi_scores_readers_sql.py (read there from
information_schema and pg_constraint, 2026-09-23).

Set LINKEDIN_QUAD_LEGACY_SQL_DSN to run it. CI passes the db-parity service
DSN and then asserts this file did not skip. Owns and recreates only news,
market_power_scores and linkedin_quad_posts.
"""
import ast
import datetime as _dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("flask")

DSN = os.environ.get("LINKEDIN_QUAD_LEGACY_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="LINKEDIN_QUAD_LEGACY_SQL_DSN not set — no Postgres to run against")

_OWNED = ("news", "market_power_scores", "linkedin_quad_posts")

# Constraint names are production's; see tests/test_dcpi_scores_readers_sql.py
# for why an auto-named UNIQUE here turns an import into a lock wait.
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

_ENGINE_ERR = "RuntimeError: engine down"


def _news_ddl():
    """news_aggregator.CREATE_TABLE_SQL, read from its source without running
    the module (see the module docstring)."""
    with open(os.path.join(ROOT, "news_aggregator.py")) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "CREATE_TABLE_SQL" for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("news_aggregator.CREATE_TABLE_SQL not found")


def _connect():
    c = psycopg2.connect(DSN)
    c.autocommit = True
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


# The day the lq fixture froze run()'s clock on. _today() returns it while a
# test runs, so a test that straddles 00:00 UTC still reads the rows run()
# wrote (slot_date = its frozen now.date()).
_CLOCK = {}


def _today():
    return _CLOCK.get("day") or _dt.datetime.now(_dt.timezone.utc).date()


def _mps(slug, name, verdict, excess, constraint, *, published=True, age="0 hours"):
    _exec("INSERT INTO market_power_scores (market_slug, market_name, verdict, "
          "excess_power_score, constraint_score, published, computed_at) "
          "VALUES (%s,%s,%s,%s,%s,%s, now() - %s::interval)",
          params=(slug, name, verdict, excess, constraint, published, age))


# Each is a stored BUILD with excess > 60, so the pre-2026-09-23 filter took
# it. Bands: BUILD needs excess >= 65 and constraint <= 50.
_NOT_QUOTABLE = {
    "unpublished": ("hotel", "Hotel, VA", "BUILD", 70, 30, False, "0 hours"),
    "off_band":    ("india", "India, TX", "BUILD", 62, 30, True, "0 hours"),   # bands say CAUTION
    "stale":       ("juliet", "Juliet, OR", "BUILD", 70, 30, True, "3 days"),
}


def _news(title, age="2 hours"):
    _exec("INSERT INTO news (title, url, source, published_date) "
          "VALUES (%s, %s, 'test', now() - %s::interval)",
          params=(title, "https://example.com/" + title.split()[0].lower(), age))


# Where the lq fixture pins run()'s clock: 10:30 UTC is outside every slot's
# HH:00-HH:14 window (08/12/16/20), so no test depends on when CI starts.
# Override per test with @pytest.mark.parametrize("lq", [(h, m)], indirect=True).
_FROZEN_AT = (10, 30)


@pytest.fixture
def lq(monkeypatch, request):
    """Recreate the owned tables empty, point the module at this database,
    keep canon and the network out of it, stop the engine, and freeze the
    route's clock (see _FROZEN_AT)."""
    import canonical_stats as cs
    import content_publisher
    from routes import linkedin_content_engine, media_editorial
    from routes import linkedin_quad_daily as lq

    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    _exec(*[f"DROP TABLE IF EXISTS {t}" for t in _OWNED])
    _exec(_news_ddl(), _MPS_DDL)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    lq._ensure_table()
    assert _one("SELECT to_regclass('public.linkedin_quad_posts')")[0] is not None

    # The post's canon counts are not under test here; keep canon off this
    # database so it cannot read a half-built fixture.
    monkeypatch.setattr(cs, "_conn", lambda: None)
    monkeypatch.setattr(lq, "_arc_reference", lambda: "")

    def _engine_down(**_kw):
        raise RuntimeError("engine down")

    monkeypatch.setattr(linkedin_content_engine, "compose_story_post", _engine_down)
    monkeypatch.setattr(media_editorial, "editorial_decision",
                        lambda *_a, **_kw: {"post": True, "lead": None})
    # The pre-publish gate is not under test; the controls must reach the post.
    monkeypatch.setattr(content_publisher, "_should_skip_publish",
                        lambda *_a, **_kw: (False, ""))
    posted = []

    def _capture(text, landing, og):
        posted.append(text)
        return {"ok": True, "post_urn": "urn:li:share:test"}

    monkeypatch.setattr(lq, "_post_to_linkedin", _capture)
    lq._test_posted = posted
    _freeze_utc(monkeypatch, lq, *getattr(request, "param", _FROZEN_AT))
    yield lq


def _run(lq, topic):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(lq.linkedin_quad_bp)
    resp = app.test_client().post(f"/api/v1/linkedin-quad/run?force=1&topic={topic}")
    assert resp.status_code == 200, resp.data[:600]
    return resp.get_json()


def _row(hour):
    return _one("SELECT success, error_msg, post_text FROM linkedin_quad_posts "
                "WHERE slot_date = %s AND slot_hour = %s", (_today(), hour))


def _assert_skipped(lq, body, hour, reason):
    assert (body.get("skipped"), body.get("reason")) == (True, reason), body
    assert body.get("engine_error") == _ENGINE_ERR, body
    assert lq._test_posted == [], lq._test_posted
    row = _row(hour)
    assert row is not None, "run() never claimed the slot"
    success, error_msg, post_text = row
    assert success is False and post_text is None, row
    assert error_msg == f"suppressed: {reason} — engine {_ENGINE_ERR}", row


def _freeze_utc(monkeypatch, lq, hour, minute):
    """Pin the clock run() reads (`datetime.datetime.utcnow()` in the route
    module) to hour:minute UTC on the test's day (_CLOCK). Only the module's
    own `datetime` name is swapped, so psycopg2 keeps the real one."""
    import types

    day = _CLOCK.get("day") or _today()
    frozen = _dt.datetime.combine(day, _dt.time(hour, minute))
    monkeypatch.setitem(_CLOCK, "day", day)

    class _Frozen(_dt.datetime):
        @classmethod
        def utcnow(cls):
            return frozen

    shim = types.SimpleNamespace(**{k: getattr(_dt, k) for k in dir(_dt)
                                    if not k.startswith("__")})
    shim.datetime = _Frozen
    monkeypatch.setattr(lq, "datetime", shim)


# ── slot window vs an explicit ?topic= ──────────────────────────────────

@pytest.mark.parametrize("hour", [8, 12, 16, 20])
def test_w1_explicit_topic_wins_inside_another_slots_window(lq, monkeypatch, hour):
    # db-parity runs at any minute. Inside HH:00-HH:14 of a slot hour, run()
    # used to pick the current-hour slot before reading ?topic=, so a forced
    # industry_pulse/hyperscaler_deal ran dcpi_mover at 08:04 (CI runs
    # 35973090730, 35973119359). A forced explicit topic is the override.
    _freeze_utc(monkeypatch, lq, hour, 4)
    topic, slot_hour = (("industry_pulse", 20) if hour != 20
                        else ("hyperscaler_deal", 12))
    _exec("DROP TABLE news")

    body = _run(lq, topic)

    assert body.get("slot", {}).get("topic") == topic, body
    reason = ("no_industry_pulse_data" if topic == "industry_pulse"
              else "no_hyperscaler_news")
    _assert_skipped(lq, body, slot_hour, reason)
    assert _row(hour) is None, "the current-hour slot was claimed instead"


def test_w2_force_without_topic_inside_a_window_keeps_the_hours_slot(lq, monkeypatch):
    # Control: with no ?topic= the current-hour slot still wins.
    _freeze_utc(monkeypatch, lq, 8, 4)
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(lq.linkedin_quad_bp)
    body = app.test_client().post("/api/v1/linkedin-quad/run?force=1").get_json()

    assert body.get("slot", {}).get("topic") == "dcpi_mover", body
    assert _row(8) is not None, body


# ── the fixture's own clock ─────────────────────────────────────────────

def _status_hour(lq):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(lq.linkedin_quad_bp)
    return app.test_client().get("/api/v1/linkedin-quad/status").get_json()["current_utc_hour"]


def test_k1_the_fixture_freezes_the_route_clock(lq):
    # Every other test relies on this: without it they run on the wall clock
    # and a forced topic lands inside HH:00-HH:14 of a slot hour whenever CI
    # happens to start then (PR #5459's db-parity at 08:09 UTC). Both reads
    # below come from the route's own clock; either is off by hours if the
    # fixture stops freezing it. agent_demand's synthesized slot takes
    # now.hour, so its claimed row shows the hour run() saw.
    assert _status_hour(lq) == _FROZEN_AT[0]

    _run(lq, "agent_demand")

    got = _one("SELECT slot_date, slot_hour FROM linkedin_quad_posts "
               "WHERE topic = 'agent_demand'")
    assert got == (_today(), _FROZEN_AT[0]), got


@pytest.mark.parametrize("lq", [(8, 5)], indirect=True)
def test_k2_a_forced_topic_holds_with_the_fixture_frozen_at_0805(lq):
    # The CI failure's shape, pinned: 08:05 is inside dcpi_mover's window, and
    # a forced hyperscaler_deal must still run hyperscaler_deal at 12:00.
    assert _status_hour(lq) == 8
    _exec("DROP TABLE news")

    body = _run(lq, "hyperscaler_deal")

    assert body.get("slot", {}).get("topic") == "hyperscaler_deal", body
    _assert_skipped(lq, body, 12, "no_hyperscaler_news")
    assert _row(8) is None, "dcpi_mover's 08:00 slot was claimed instead"


# ── hyperscaler_deal (12:00) ────────────────────────────────────────────

def test_n1_hyperscaler_no_fresh_headline_skips_and_stamps(lq):
    # A matching headline from 5 days ago and a fresh one that matches no
    # keyword: the real query must find neither.
    _news("NVIDIA books another quarter", age="5 days")
    _news("Utility files a rate case", age="1 hour")
    assert lq._build_hyperscaler_deal() is None

    body = _run(lq, "hyperscaler_deal")

    _assert_skipped(lq, body, 12, "no_hyperscaler_news")


def test_n2_hyperscaler_query_error_skips_and_stamps(lq):
    _exec("DROP TABLE news")
    assert lq._build_hyperscaler_deal() is None

    body = _run(lq, "hyperscaler_deal")

    _assert_skipped(lq, body, 12, "no_hyperscaler_news")


def test_n4_a_forced_run_never_stamps_a_peers_live_claim(lq):
    # A peer claimed 12:00 a moment ago and is mid-publish. force=1 bypasses
    # the claim, so this run loses it and still reaches the builder; its skip
    # must leave the peer's row alone (the peer's _record owns it).
    _exec("INSERT INTO linkedin_quad_posts (slot_date, slot_hour, topic, style, "
          "success, error_msg, claimed_at) VALUES (%s, 12, 'hyperscaler_deal', "
          "'narrative', FALSE, 'claimed_in_flight', NOW() ON CONFLICT DO NOTHING)", params=(_today(),))

    body = _run(lq, "hyperscaler_deal")

    assert (body.get("skipped"), body.get("reason")) == (True, "no_hyperscaler_news"), body
    assert lq._test_posted == [], lq._test_posted
    assert _row(12)[1] == "claimed_in_flight", _row(12)


@pytest.mark.parametrize("topic", ["capability", "agent_demand"])
def test_n5_slots_with_no_legacy_builder_skip_not_the_generic_template(lq, topic):
    # Fresh data sits in both tables, so a skip here cannot be a builder
    # finding nothing: these topics have no builder at all.
    _news("CoreWeave signs a 400 MW lease")

    body = _run(lq, topic)

    assert (body.get("skipped"), body.get("reason")) == (True, "no_legacy_builder"), body
    assert body.get("engine_error") == _ENGINE_ERR, body
    assert lq._test_posted == [], lq._test_posted
    # agent_demand's slot hour is the hour of the request; read by topic.
    row = _one("SELECT success, error_msg, post_text FROM linkedin_quad_posts "
               "WHERE topic = %s", (topic,))
    assert row is not None, "run() never claimed the slot"
    assert row == (False, f"suppressed: no_legacy_builder — engine {_ENGINE_ERR}", None), row


def test_c1_a_fresh_headline_is_posted_and_keeps_the_engine_error(lq):
    _news("CoreWeave signs a 400 MW lease")

    body = _run(lq, "hyperscaler_deal")

    assert not body.get("skipped"), body
    assert body["payload"].get("engine_error") == _ENGINE_ERR, body
    assert len(lq._test_posted) == 1, lq._test_posted
    assert "📰 CoreWeave signs a 400 MW lease" in lq._test_posted[0], lq._test_posted[0]
    # No typed deal rate (owner decision 2026-09-23): nothing computes one.
    assert "$1B" not in lq._test_posted[0] and "/week" not in lq._test_posted[0], lq._test_posted[0]
    success, error_msg, post_text = _row(12)
    assert success is True and "CoreWeave signs a 400 MW lease" in post_text, (success, error_msg)


# ── industry_pulse (20:00) ──────────────────────────────────────────────

def test_n3_industry_pulse_query_error_skips_and_stamps(lq):
    _exec("DROP TABLE news")
    assert lq._build_industry_pulse() is None

    body = _run(lq, "industry_pulse")

    _assert_skipped(lq, body, 20, "no_industry_pulse_data")


@pytest.mark.parametrize("why", sorted(_NOT_QUOTABLE))
def test_p1_industry_pulse_never_quotes_an_unquotable_market(lq, why):
    slug, name, verdict, excess, constraint, published, age = _NOT_QUOTABLE[why]
    _mps(slug, name, verdict, excess, constraint, published=published, age=age)
    _news("Hyperscale campus clears zoning")

    p = lq._build_industry_pulse()

    assert p is not None and p["news"], p      # the control: news was read
    assert p["market"] is None, (why, p["market"])
    body = _run(lq, "industry_pulse")
    assert not body.get("skipped"), body
    assert len(lq._test_posted) == 1 and name not in lq._test_posted[0], lq._test_posted


def test_p2_the_quotable_market_is_the_one_picked(lq):
    for row in _NOT_QUOTABLE.values():
        slug, name, verdict, excess, constraint, published, age = row
        _mps(slug, name, verdict, excess, constraint, published=published, age=age)
    _mps("kilo", "Kilo, NE", "BUILD", 72, 30)
    _news("Hyperscale campus clears zoning")

    # ORDER BY RANDOM(): with the filter gone, 8 draws from 4 rows all
    # landing on Kilo has probability 4^-8.
    picks = {lq._build_industry_pulse()["market"]["market_name"] for _ in range(8)}

    assert picks == {"Kilo, NE"}, picks


def test_p3_a_quotable_caution_market_is_not_the_counter_take(lq):
    _mps("lima", "Lima, KS", "CAUTION", 55, 60)   # published, fresh, on-band
    _news("Hyperscale campus clears zoning")

    p = lq._build_industry_pulse()

    assert p is not None and p["news"], p
    assert p["market"] is None, p["market"]


def test_c2_industry_pulse_with_data_is_posted(lq):
    _news("Hyperscale campus clears zoning")
    _exec("INSERT INTO market_power_scores (market_slug, market_name, verdict, "
          "excess_power_score, constraint_score, published) "
          "VALUES ('kilo', 'Kilo, NE', 'BUILD', 72, 30, true)")
    p = lq._build_industry_pulse()
    assert p and p["news"] and p["market"], p

    body = _run(lq, "industry_pulse")

    assert not body.get("skipped"), body
    assert len(lq._test_posted) == 1, lq._test_posted
    text = lq._test_posted[0]
    assert "⚡ Hyperscale campus clears zoning" in text and "Kilo, NE" in text, text
