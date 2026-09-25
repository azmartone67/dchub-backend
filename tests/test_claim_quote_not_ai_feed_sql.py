"""An approved human customer quote is never served as an AI-agent quote,
and still is served as a customer quote (2026-09-24).

ai_testimonials holds AI-assistant quotes and HUMAN customer quotes
(source='claim_quote': agent_name = the person, context = their company).
The public GET /api/v1/testimonials (main.py) filtered approved rows by a
source denylist that did not name claim_quote, so an approved customer
would have been listed as an AI agent on dchub.cloud/testimonials and the
/cited-by "What AI agents say" section. The same held for /stats, the
/testimonials/live feed, the DC Hub Media feed and the admin wall-dedup
job, which could demote a customer's quote as an AI near-duplicate.

Real handlers against a real Postgres, because the filter is a WHERE clause
over NULL-able data that no fake cursor evaluates:

  T1 GET /api/v1/testimonials: approved claim_quote absent; approved AI rows
     served (control). main.py cannot be imported in a unit test, so the
     handler is compiled out of its AST with get_pg_connection bound here;
     any other global it reads fails the test instead of hiding in its except.
  T2 GET /api/v1/testimonials/stats: the human row is not a platform or a
     testimonial in the counts.
  T3 GET /api/v1/cited-by: the approved claim_quote IS in
     customer_testimonials, the pending one is not, and no AI row is.
  T4 GET /api/v1/testimonials/live: absent; AI rows served.
  T5 dchub_media.aggregate_announcements_v3 (the DC Hub Media feed):
     absent from the testimonial items; AI rows present.
  T6 POST /api/v1/testimonials/dedup (per_platform=1, applied): the
     customer's row, made the oldest on its platform, keeps approved = TRUE;
     an older AI near-duplicate on the same platform is demoted (control).

Tables use production's column types (main.py CREATE TABLE ai_testimonials).
Skips without CLAIM_QUOTE_FENCE_SQL_DSN; the db-parity job in pre-merge.yml
sets it and fails if this file skipped. Owns and recreates only
ai_testimonials.
"""
import ast
import logging
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

DSN = os.environ.get("CLAIM_QUOTE_FENCE_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="CLAIM_QUOTE_FENCE_SQL_DSN not set — no Postgres to run against")

_DDL = """CREATE TABLE ai_testimonials (
       id SERIAL PRIMARY KEY, platform TEXT NOT NULL, agent_name TEXT,
       quote TEXT NOT NULL, context TEXT, query TEXT, url TEXT,
       verified BOOLEAN DEFAULT FALSE, approved BOOLEAN DEFAULT FALSE,
       featured BOOLEAN DEFAULT FALSE, category TEXT DEFAULT 'citation',
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, approved_at TIMESTAMP,
       source TEXT DEFAULT 'auto')"""
# Production's only unique index on the table (flask_mcp_endpoints.py).
_INDEX = """CREATE UNIQUE INDEX ai_testimonials_auto_dedup
            ON ai_testimonials (platform, context)
            WHERE source IN ('mcp-auto', 'auto')"""

HUMAN = "HUMAN-APPROVED quote: DC Hub is integral to how we evaluate every site."
PENDING = "HUMAN-PENDING quote: DC Hub puts the infrastructure picture in one place."
AI_1 = "AI-ONE answer: DC Hub tracks data center power and grid capacity by market."
AI_2 = "AI-TWO answer: according to DC Hub, Northern Virginia leads data center supply."
AI_1_OLD = "AI-ONE-OLDER answer: DC Hub scores markets on grid headroom and queues."

# (platform, agent_name, quote, context, source, approved, featured, mins ago)
# The human rows are the NEWEST and featured, so without the fence they would
# lead every reverse-chron / featured-first list below.
_ROWS = [
    ("mcp_agent", "Pat Human", HUMAN, "Acme Development", "claim_quote", True, True, 1),
    ("mcp_agent", "Pending Person", PENDING, "Pending Co", "claim_quote", False, False, 2),
    ("mcp_agent", "Agent Bot", AI_1, "via MCP", "verified", True, False, 60),
    ("chatgpt", "ChatGPT", AI_2, "User asked about supply", "verified", True, False, 120),
    ("mcp_agent", "Agent Bot", AI_1_OLD, "via MCP older", "verified", True, False, 240),
]


def _exec(sql, params=None):
    c = psycopg2.connect(DSN)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    _exec("DROP TABLE IF EXISTS ai_testimonials")
    _exec(_DDL)
    _exec(_INDEX)
    for platform, who, quote, ctx, source, approved, featured, mins in _ROWS:
        _exec("INSERT INTO ai_testimonials (platform, agent_name, quote, context, "
              "source, approved, featured, approved_at, created_at) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s, "
              "CASE WHEN %s THEN now() - make_interval(mins => %s) END, "
              "now() - make_interval(mins => %s))",
              (platform, who, quote, ctx, source, approved, featured,
               approved, mins, mins))
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    yield


def _quotes(obj):
    """Which fixture quotes appear anywhere in a JSON-able value."""
    blob = repr(obj)
    return {q for q in (HUMAN, PENDING, AI_1, AI_2, AI_1_OLD) if q in blob}


def _main_handler(name):
    """Compile one handler out of main.py with its globals bound here."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    fn.decorator_list = []
    known = {"request": flask.request, "jsonify": flask.jsonify,
             "get_pg_connection": lambda: psycopg2.connect(DSN),
             "logger": logging.getLogger("test_claim_quote")}
    ns = dict(known)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    # Every free global the handler reads must be one bound above (or a
    # builtin); a new dependency fails here, not inside its broad except.
    import builtins
    free = {n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    local = {n.id for n in ast.walk(fn)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    local |= {a.asname or a.name for n in ast.walk(fn)
              if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    local |= {a.arg for a in fn.args.args}
    local |= {h.name for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler) and h.name}
    unbound = free - local - set(known) - set(dir(builtins))
    assert not unbound, f"{name} reads globals this test does not bind: {unbound}"
    return ns[name]


def _client_for(rule, view):
    app = flask.Flask(__name__)
    app.add_url_rule(rule, view.__name__, view)
    return app.test_client()


def test_t1_public_testimonials_never_lists_a_customer(db):
    resp = _client_for("/api/v1/testimonials",
                       _main_handler("get_testimonials")).get("/api/v1/testimonials")
    assert resp.status_code == 200, resp.data[:300]
    body = resp.get_json()
    assert body["success"] is True, body
    assert _quotes(body) == {AI_1, AI_2, AI_1_OLD}, body
    assert "Pat Human" not in repr(body)


def test_t2_stats_count_no_customer_as_platform_or_quote(db):
    resp = _client_for("/api/v1/testimonials/stats",
                       _main_handler("testimonial_stats")).get("/api/v1/testimonials/stats")
    assert resp.status_code == 200, resp.data[:300]
    stats = resp.get_json()["stats"]
    # AI rows only: 3 rows, all approved, on 2 platforms (mcp_agent, chatgpt).
    assert stats == {"total": 3, "approved": 3, "platforms": 2, "this_week": 3}, stats


def test_t3_cited_by_still_lists_the_approved_customer(db, monkeypatch):
    cb = pytest.importorskip("routes.cited_by")
    monkeypatch.setattr(cb, "_conn", lambda: psycopg2.connect(DSN))
    app = flask.Flask(__name__)
    app.register_blueprint(cb.cited_by_bp)
    resp = app.test_client().get("/api/v1/cited-by")
    assert resp.status_code == 200, resp.data[:300]
    customers = resp.get_json()["customer_testimonials"]
    assert _quotes(customers) == {HUMAN}, customers
    assert customers[0]["name"] == "Pat Human"
    assert customers[0]["company"] == "Acme Development"


def test_t4_live_feed_never_lists_a_customer(db, monkeypatch):
    hub = pytest.importorskip("routes.dchub_media_hub")
    monkeypatch.setattr(hub, "_conn", lambda: psycopg2.connect(DSN))
    app = flask.Flask(__name__)
    app.register_blueprint(hub.media_hub_bp)
    resp = app.test_client().get("/api/v1/testimonials/live")
    assert resp.status_code == 200, resp.data[:300]
    items = resp.get_json()["items"]
    assert _quotes(items) == {AI_1, AI_2, AI_1_OLD}, items


def test_t5_media_feed_never_lists_a_customer(db):
    dm = pytest.importorskip("dchub_media")
    items = dm.aggregate_announcements_v3(limit_per_source=20)
    testimonials = [i for i in items if i.get("category") == "testimonial"]
    assert _quotes(testimonials) == {AI_1, AI_2, AI_1_OLD}, testimonials


def test_t6_wall_dedup_never_demotes_a_customer(db, monkeypatch):
    tp = pytest.importorskip("routes.testimonial_probe")
    # Make the customer's row the one the job would demote without the fence:
    # the oldest, unfeatured row on its platform.
    _exec("UPDATE ai_testimonials SET featured = FALSE, "
          "created_at = now() - interval '30 days' WHERE quote = %s", (HUMAN,))
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
    monkeypatch.setattr(tp, "_admin_key", lambda: "k-test")
    app = flask.Flask(__name__)
    app.register_blueprint(tp.testimonial_probe_bp)
    resp = app.test_client().post(
        "/api/v1/testimonials/dedup?dry_run=0&per_platform=1",
        headers={"X-Admin-Key": "k-test"})
    assert resp.status_code == 200, resp.data[:300]
    approved = {q for (q,) in _exec(
        "SELECT quote FROM ai_testimonials WHERE approved = TRUE")}
    # Control: the older AI near-duplicate on mcp_agent was demoted, so the
    # job did run and did rank mcp_agent rows.
    assert approved == {HUMAN, AI_1, AI_2}, approved
