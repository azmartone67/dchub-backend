"""
mcp_sse_events.py — SSE event stream for MCP agents to subscribe.

Phase ZZZZZ-round38 (2026-05-25). MCP spec advertises "resources" +
"prompts" + "subscribe" capabilities but DC Hub's MCP server only ships
"tools/listChanged". Agents that want to be notified of "new M&A deal",
"DCPI verdict change", or "ISO price spike" can't subscribe — they have
to poll, which is wasteful.

This is a lightweight precursor to full MCP resources/subscribe: a plain
SSE feed that any HTTP client (not just MCP) can connect to and receive
typed events. Once MCP resources are wired in dchub-mcp-server, this
becomes the underlying event source.

Endpoint:
  GET /api/v1/mcp/events.sse           — full event stream (heartbeat + events)
  GET /api/v1/mcp/events.sse?topic=X   — filter by topic
  GET /api/v1/mcp/events/recent        — JSON snapshot of last 50 events (no SSE)

Topics emitted:
  - dcpi_verdict_shift   (market changed from CAUTION → BUILD etc)
  - hyperscaler_deal     (new $1B+ AI deal detected in news)
  - iso_price_spike      (ISO LMP > 2x 30-day average)
  - new_facility         (newly discovered facility added)
  - heartbeat            (every 30s, keeps connection alive)
"""
import os
import time
import json
import datetime
import threading
from collections import deque
from contextlib import contextmanager
from flask import Blueprint, Response, request, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

mcp_sse_bp = Blueprint("mcp_sse", __name__,
                       url_prefix="/api/v1/mcp")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# In-memory event ring — last 200 events, FIFO
_EVENTS = deque(maxlen=200)
_LOCK = threading.Lock()


def _push_event(topic, data):
    evt = {
        "id": str(int(time.time() * 1000)),
        "topic": topic,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "data": data,
    }
    with _LOCK:
        _EVENTS.append(evt)


def _fetch_recent_dcpi_shifts(since_minutes=15):
    """Look for recent DCPI verdict changes in last N minutes."""
    out = []
    if not (_pg and _dsn()): return out
    try:
        with _conn() as c, c.cursor() as cur:
            # Try the DCPI history table (name may vary)
            for table, col_market, col_verdict, col_ts in [
                ("dcpi_scores_history", "market_id", "verdict", "computed_at"),
                ("dcpi_history",        "market",    "verdict", "ts"),
                ("dcpi_v2_scores",      "market",    "verdict", "computed_at"),
            ]:
                try:
                    cur.execute(f"""
                        SELECT {col_market}, {col_verdict}, {col_ts}
                        FROM {table}
                        WHERE {col_ts} > NOW() - INTERVAL '{since_minutes} minutes'
                        ORDER BY {col_ts} DESC LIMIT 25
                    """)
                    rows = cur.fetchall()
                    for r in rows:
                        out.append({"market": r[0], "verdict": r[1],
                                    "at": r[2].isoformat() if r[2] else None})
                    if rows: break
                except Exception: continue
    except Exception: pass
    return out


def _fetch_recent_hyperscaler_deals(since_minutes=60):
    out = []
    if not (_pg and _dsn()): return out
    try:
        with _conn() as c, c.cursor() as cur:
            for keyword_sql in [
                "LOWER(title) LIKE '%stargate%' OR LOWER(title) LIKE '%openai%' OR LOWER(title) LIKE '%coreweave%'",
                "LOWER(title) LIKE '%hyperscaler%' OR LOWER(title) LIKE '%gpu cluster%'",
            ]:
                try:
                    cur.execute(f"""
                        SELECT id, title, source, url, published_date
                        FROM news
                        WHERE ({keyword_sql})
                          AND published_date > CURRENT_DATE - INTERVAL '1 day'
                        ORDER BY published_date DESC LIMIT 10
                    """)
                    rows = cur.fetchall()
                    for r in rows:
                        out.append({"id": r[0], "title": r[1], "source": r[2],
                                    "url": r[3], "published": r[4].isoformat() if r[4] else None})
                    if rows: break
                except Exception: continue
    except Exception: pass
    return out


@mcp_sse_bp.route("/events.sse", methods=["GET"])
def sse_stream():
    """SSE stream — connect with `curl -N` or EventSource client."""
    topic_filter = (request.args.get("topic") or "").strip()

    def generate():
        # Replay recent events first
        with _LOCK:
            recent = list(_EVENTS)
        for evt in recent[-10:]:
            if not topic_filter or evt["topic"] == topic_filter:
                yield f"event: {evt['topic']}\ndata: {json.dumps(evt)}\n\n"

        last_id = recent[-1]["id"] if recent else "0"
        deadline = time.time() + 90  # 90s connection then ask client to reconnect

        while time.time() < deadline:
            # Heartbeat every 30s
            yield f"event: heartbeat\ndata: {json.dumps({'at': datetime.datetime.utcnow().isoformat() + 'Z'})}\n\n"

            # Look for new events
            with _LOCK:
                new_events = [e for e in _EVENTS if e["id"] > last_id]
            for evt in new_events:
                if not topic_filter or evt["topic"] == topic_filter:
                    yield f"id: {evt['id']}\nevent: {evt['topic']}\ndata: {json.dumps(evt)}\n\n"
                last_id = evt["id"]

            time.sleep(30)

        # Tell client to reconnect
        yield "event: reconnect\ndata: {\"reason\": \"90s_lifecycle\"}\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # nginx — don't buffer SSE
        "Access-Control-Allow-Origin": "*",
    })


@mcp_sse_bp.route("/events/recent", methods=["GET"])
def recent():
    with _LOCK:
        events = list(_EVENTS)
    topic = (request.args.get("topic") or "").strip()
    if topic:
        events = [e for e in events if e["topic"] == topic]
    return jsonify({
        "count": len(events),
        "events": events[-50:],
        "topics_supported": [
            "dcpi_verdict_shift", "hyperscaler_deal", "iso_price_spike",
            "new_facility", "heartbeat",
        ],
    }), 200


@mcp_sse_bp.route("/events/refresh", methods=["GET", "POST"])
def refresh():
    """Cron-callable. Polls DB for new events + pushes to ring."""
    started = datetime.datetime.utcnow()
    pushed = {"dcpi_verdict_shift": 0, "hyperscaler_deal": 0}

    for shift in _fetch_recent_dcpi_shifts(since_minutes=20):
        _push_event("dcpi_verdict_shift", shift)
        pushed["dcpi_verdict_shift"] += 1

    for deal in _fetch_recent_hyperscaler_deals(since_minutes=60):
        _push_event("hyperscaler_deal", deal)
        pushed["hyperscaler_deal"] += 1

    return jsonify({
        "at": started.isoformat() + "Z",
        "pushed": pushed,
        "ring_size": len(_EVENTS),
    }), 200


@mcp_sse_bp.route("/events/health", methods=["GET"])
def health():
    with _LOCK:
        events = list(_EVENTS)
    topics = {}
    for e in events:
        topics[e["topic"]] = topics.get(e["topic"], 0) + 1
    return jsonify({
        "ring_size": len(events),
        "topics": topics,
        "blueprint": "mcp_sse_bp",
    }), 200
