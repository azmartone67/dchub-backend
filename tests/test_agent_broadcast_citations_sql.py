"""agent_broadcast's ai_citation items: approved AI rows only, never a human
customer quote (2026-09-24).

routes/agent_broadcast._fetch_ai_citations reads ai_testimonials and labels
every row `kind: "ai_citation"`, titled "<who> cited DC Hub". That table also
holds HUMAN quotes: source='claim_quote' rows volunteered via
POST /api/v1/keys/claim/quote, with agent_name = the person and context = their
company, stored approved=FALSE until a human reviews them. The read had no
source filter and also served unapproved rows under 21 days old, so a person's
pending quote could go out on the public, unauthenticated feed as
"Jane Doe cited DC Hub".

  C1 through the shipped /api/v1/agent-broadcast route: a claim_quote row
     (approved and pending) is absent, even though both are the most recent
     rows and would lead the list. Control: approved AI rows ARE served.
  C2 a pending AI row (approved=FALSE, created today) is not broadcast, and
     neither is a NULL `approved`. Approval is the publish gate.

Tables are created with production's column types (main.py CREATE TABLE
ai_testimonials). Set AGENT_BROADCAST_PUBLISHED_SQL_DSN to run it; CI passes
the db-parity service DSN and then asserts this file did not skip. Owns and
recreates only ai_testimonials.
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

_DDL = """CREATE TABLE ai_testimonials (
       id SERIAL PRIMARY KEY, platform TEXT NOT NULL, agent_name TEXT,
       quote TEXT NOT NULL, context TEXT, query TEXT, url TEXT,
       verified BOOLEAN DEFAULT FALSE, approved BOOLEAN DEFAULT FALSE,
       featured BOOLEAN DEFAULT FALSE, category TEXT DEFAULT 'citation',
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, approved_at TIMESTAMP,
       source TEXT DEFAULT 'auto')"""

# (platform, agent_name, quote, context, source, approved, created N min ago)
# The rows that must never be served are the NEWEST, so without the filters
# they would lead the reverse-chron LIMIT 15.
_ROWS = [
    ("mcp_agent", "Jane Pending", "PENDING-HUMAN quote about DC Hub",
     "Acme Pending LLC", "claim_quote", False, 1),
    ("mcp_agent", "John Approved", "APPROVED-HUMAN quote about DC Hub",
     "Acme Approved Inc", "claim_quote", True, 2),
    ("claude", "Claude", "PENDING-AI probe answer about DC Hub",
     "Probed via probe_claude", "probe_claude", False, 3),
    ("gemini", "Gemini", "NULL-FLAG-AI answer about DC Hub",
     None, "probe_gemini", None, 4),
    ("perplexity", "Perplexity", "APPROVED-AI-1 answer about DC Hub",
     None, "verified", True, 60),
    ("chatgpt", "ChatGPT", "APPROVED-AI-2 answer about DC Hub",
     None, "seed", True, 120),
]
_SERVED = {"APPROVED-AI-1", "APPROVED-AI-2"}
_NEVER = {"PENDING-HUMAN", "APPROVED-HUMAN", "PENDING-AI", "NULL-FLAG-AI",
          "Jane Pending", "John Approved", "Acme"}


def _exec(sql, params=None):
    c = psycopg2.connect(DSN)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
    finally:
        c.close()


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    _exec("DROP TABLE IF EXISTS ai_testimonials")
    _exec(_DDL)
    for platform, who, quote, ctx, source, approved, mins in _ROWS:
        _exec("INSERT INTO ai_testimonials (platform, agent_name, quote, "
              "context, source, approved, approved_at, created_at) "
              "VALUES (%s,%s,%s,%s,%s,%s, "
              "CASE WHEN %s THEN now() - make_interval(mins => %s) END, "
              "now() - make_interval(mins => %s))",
              (platform, who, quote, ctx, source, approved,
               bool(approved), mins, mins))
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    from routes import agent_broadcast as ab
    monkeypatch.setattr(ab, "_persist_poller", lambda *a, **k: None)
    return ab


def _markers(items):
    return {m for m in _SERVED | _NEVER
            if any(m in str(i) for i in items)}


def test_c1_broadcast_route_never_serves_a_claim_quote(db):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(db.agent_broadcast_bp)

    resp = app.test_client().get(
        "/api/v1/agent-broadcast?kinds=ai_citation&days=30")

    assert resp.status_code == 200, resp.data[:300]
    items = [i for i in resp.get_json()["items"]
             if i.get("kind") == "ai_citation"]
    # Control: the query ran against this table and approved AI rows serve.
    assert _markers(items) == _SERVED, items
    assert len(items) == 2, items
    titles = {i["title"] for i in items}
    assert titles == {"Perplexity cited DC Hub", "ChatGPT cited DC Hub"}, titles


def test_c2_unapproved_ai_rows_are_not_broadcast(db):
    items = db._fetch_ai_citations(7)

    served = _markers(items)
    assert "PENDING-AI" not in served, items
    assert "NULL-FLAG-AI" not in served, items
    assert served == _SERVED, items
