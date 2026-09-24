"""r-hi-crawler-ua (2026-09-24): a self-declared crawler is not a high-intent prospect.

Read on Neon 2026-09-24: 19 mcp_high_intent_sessions rows (15 sessions, 17 tools), all
claim-minted 09-17..09-21, from mcp_client 'brickblue' with UA
'BrickBlueBot/0.1 (+https://brick.blue/bot; agentic-web registry)'. 0 opens, 0 redemptions.
relay_minted in /api/v1/mcp/handoff-funnel counts that table with no read-side filter, so
the write gate in track-paid-hit is the only thing standing between a crawler and the
funnel denominator.

These drive the real route through Flask's test client with _conn replaced by a
recorder. The control proves a human-bearing agent still reaches that recorder, so a
refusal cannot pass by failing earlier (auth, validation, a missing route).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402

import routes.mcp_high_intent_claim as hi  # noqa: E402

KEY = "test-internal-key-for-high-intent-crawler"
HEADERS = {"X-Internal-Key": KEY}
SID = "b71c0de0-5678-4bbb-8888-abcdef987654"
TOOL = "get_grid_intelligence"

# Verbatim from prod, plus the robots-convention crawlers the same shape covers.
CRAWLER_UAS = [
    ("brickblue", "BrickBlueBot/0.1 (+https://brick.blue/bot; agentic-web registry)"),
    ("brickblue", "BrickBlueBot/0.1 (+https://brick.blue/bot; agentic-web indexer)"),
    (None, "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1; +https://openai.com/gptbot)"),
    (None, "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    (None, "Mozilla/5.0 (compatible; Baiduspider-render/2.0; +http://www.baidu.com/search/spider.html)"),
    ("somecrawler", "SomeCrawler/3.2"),
]

# Agents a human is driving. ChatGPT-User's UA contains '/bot' — the reason the rule
# keys on a product token and not the word.
HUMAN_UAS = [
    ("chatgpt", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("claude-ai", "Claude-User (claude-code/2.1.0; +https://support.anthropic.com/)"),
    ("perplexity", "Mozilla/5.0 (compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"),
    ("claude", "node"),
    ("cursor", "Cursor/1.4.2"),
    ("copilot", "GitHubCopilotChat/0.30.0"),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", KEY)
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    opened = []

    def recorder():
        opened.append(True)
        return None  # real _conn's answer with no DSN: the route stops at no_db

    monkeypatch.setattr(hi, "_conn", recorder)
    app = Flask(__name__)
    app.register_blueprint(hi.mcp_high_intent_claim_bp)
    c = app.test_client()
    c.opened = opened
    return c


def _track(client, mcp_client, ua):
    return client.post("/api/v1/mcp/track-paid-hit", headers=HEADERS,
                       json={"session_id": SID, "tool": TOOL,
                             "user_agent": ua, "mcp_client": mcp_client})


@pytest.mark.parametrize("mcp_client,ua", CRAWLER_UAS)
def test_track_paid_hit_records_nothing_for_a_crawler(client, mcp_client, ua):
    r = _track(client, mcp_client, ua)
    assert r.status_code == 200
    body = r.get_json()
    assert body["skipped"] == "crawler_ua"
    assert body["count"] == 0 and body["is_high_intent"] is False
    assert client.opened == []


@pytest.mark.parametrize("mcp_client,ua", HUMAN_UAS)
def test_control_a_human_bearing_agent_still_reaches_the_database(client, mcp_client, ua):
    r = _track(client, mcp_client, ua)
    assert r.status_code == 503, r.get_json()
    assert r.get_json() == {"ok": False, "error": "no_db"}
    assert client.opened == [True]


def _sql_twin_excludes(ua):
    """Evaluate _hi_real_sql's crawler clause the way Postgres ~* does (the pattern
    uses no Python-only syntax, and parity is asserted below)."""
    import re
    frag = hi._hi_real_sql()
    assert f"!~* '{hi._CRAWLER_UA_SQL}'" in frag
    return bool(re.search(hi._CRAWLER_UA_SQL, ua, re.I))


@pytest.mark.parametrize("mcp_client,ua", CRAWLER_UAS)
def test_metric_sql_twin_drops_the_same_crawlers(mcp_client, ua):
    assert _sql_twin_excludes(ua)


@pytest.mark.parametrize("mcp_client,ua", HUMAN_UAS)
def test_metric_sql_twin_keeps_the_same_humans(mcp_client, ua):
    assert not _sql_twin_excludes(ua)


def test_sql_twin_is_the_gate_pattern_and_posix_safe():
    assert hi._CRAWLER_UA_SQL == hi._CRAWLER_UA_RE.pattern
    # POSIX ARE has no \b / lookaround; a quote would break the inlined literal.
    for bad in ("\\b", "(?", "'", "%"):
        assert bad not in hi._CRAWLER_UA_SQL
