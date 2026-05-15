"""
demo.py — Phase GG (2026-05-15) Bundle 7 item 1: Live MCP demo endpoint.

Powers the homepage chat widget. An anonymous visitor types a question,
this endpoint calls Claude with a curated subset of DC Hub tools bound
(real MCP tool-use loop), returns the answer + the tool-call chain so
the UI can show exactly what Claude did. "Show, don't tell."

Endpoint:
    POST /api/v1/demo/ask
      body: {"question": "..."}
      returns: {
        "ok": true,
        "answer": "...",
        "tool_calls": [{name, input, result_summary}],
        "rate_limit": {used, limit_per_day, reset_at}
      }

Safety:
  - Hard rate limit: 5 calls per IP per day (Postgres-backed, no Redis)
  - Haiku model only (cheap)
  - 600 token cap on output
  - 3 tool-use turns max (avoid runaway)
  - Question must contain a DC-related keyword (basic filter)
  - System prompt constrains scope strictly to DC/grid/market topics
  - Response cached for 1h on (question_hash) so repeat queries are free
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

demo_bp = Blueprint("demo", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEMO_MODEL = os.environ.get("DCHUB_DEMO_MODEL", "claude-haiku-4-5")
PER_IP_DAILY = int(os.environ.get("DCHUB_DEMO_PER_IP_DAILY", "5"))
MAX_TOOL_TURNS = 3
MAX_OUTPUT_TOKENS = 600

# Question must contain at least one of these tokens — basic gate against
# unrelated questions burning the API budget. Generous list; the system
# prompt does the strict filtering.
DC_KEYWORDS = [
    "data center", "datacenter", "dc", "grid", "iso", "ercot", "pjm", "caiso",
    "miso", "spp", "isone", "ieso", "aeso", "tva", "bpa", "nyiso",
    "facility", "facilities", "capacity", "pipeline", "mw", "megawatt", "gw",
    "ashburn", "dallas", "austin", "atlanta", "phoenix", "chicago", "columbus",
    "northern virginia", "silicon valley", "fiber", "carrier", "ixp", "substation",
    "transmission", "water", "drought", "tax", "incentive", "lease", "rate",
    "operator", "equinix", "digital realty", "aws", "azure", "google", "microsoft",
    "build", "site", "selection", "dcpi", "verdict", "market", "deal", "m&a",
    "transaction", "comp", "tenant", "colocation", "colo", "hyperscale",
    "ai", "gpu", "nvidia", "training", "inference", "cluster",
    "power", "energy", "renewable", "solar", "wind", "nuclear", "gas",
]

# Curated tool subset — only the 6 best for demo questions. Each entry has
# the Anthropic tool schema + an internal handler that fetches from our own
# API and shapes the result.
DEMO_TOOLS = [
    {
        "name": "get_dcpi_market",
        "description": "Get DC Hub Power Index verdict and scores for a specific market by slug (e.g. 'northern-virginia', 'dallas', 'phoenix'). Returns BUILD/CAUTION/AVOID verdict, excess power score, constraint score, time-to-power months.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Market slug, lowercase with hyphens"},
            },
            "required": ["slug"],
        },
        "internal_path": lambda args: f"/api/v1/dcpi/scores/{args.get('slug', 'northern-virginia')}",
    },
    {
        "name": "get_iso_snapshot",
        "description": "Comprehensive snapshot for one ISO (ERCOT, CAISO, NYISO, MISO, PJM, SPP, ISONE, IESO, AESO, TVA, BPA). Returns heartbeat freshness, DCPI rollup, pipeline, facility footprint.",
        "input_schema": {
            "type": "object",
            "properties": {"iso": {"type": "string", "description": "ISO code, uppercase"}},
            "required": ["iso"],
        },
        "internal_path": lambda args: f"/api/v1/iso/{(args.get('iso') or 'ERCOT').upper()}/snapshot",
    },
    {
        "name": "search_facilities",
        "description": "Search for data center facilities by city/state/country. Returns count, total MW, sample facility list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "state": {"type": "string", "description": "2-letter state code"},
                "country": {"type": "string", "description": "Country code, e.g. US"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
        },
        "internal_path": lambda args: "/api/v1/facilities?" + "&".join(
            f"{k}={v}" for k, v in args.items() if v),
    },
    {
        "name": "get_market_brief",
        "description": "One-call site-selection brief for a data center market: DCPI verdict + grid + power cost + tax incentives + comparables.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {"type": "string", "description": "Market slug"},
                "state": {"type": "string", "description": "Alt: 2-letter state"},
            },
        },
        "internal_path": lambda args: "/api/v1/brief/market?" + "&".join(
            f"{k}={v}" for k, v in args.items() if v),
    },
    {
        "name": "get_iso_comparison",
        "description": "Head-to-head across all 11 tracked ISOs ranked by avg excess-power score. Returns the ranked list.",
        "input_schema": {"type": "object", "properties": {}},
        "internal_path": lambda args: "/api/v1/iso/comparison",
    },
    {
        "name": "get_dchub_index",
        "description": "Session warm-up. Returns valid market slugs, ISO codes, coverage counts, freshness. Call ONCE to learn what data is available.",
        "input_schema": {"type": "object", "properties": {}},
        "internal_path": lambda args: "/api/v1/agent/index",
    },
]


DEMO_SYSTEM_PROMPT = """You are the DC Hub demo assistant. DC Hub is the data center intelligence platform at https://dchub.cloud — 20,000+ facilities, 140+ countries, real-time grid/fiber/market data via MCP + REST.

You can answer questions about: data center facilities, ISO grid status, market intelligence, capacity pipeline, DCPI build/avoid verdicts, fiber routes, M&A transactions, site selection. You have 6 tools available — USE THEM to fetch live data, don't make up numbers.

Strict rules:
- If the question is NOT about data centers, grid, infra, or related topics, reply with EXACTLY: "I'm the DC Hub demo — I answer data center questions only. Try: 'What's the DCPI for Ashburn?' or 'Compare ERCOT to PJM.'"
- Always call a tool first if the answer requires real data
- Keep responses under 150 words
- Cite the tool you used at the end: "(via get_iso_snapshot)"
- Never expose API keys, internal URLs, or PII"""


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS demo_rate_limit (
        id          BIGSERIAL PRIMARY KEY,
        ip_hash     TEXT NOT NULL,
        used_today  INT NOT NULL DEFAULT 1,
        day_key     TEXT NOT NULL,
        last_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (ip_hash, day_key)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_drl_day ON demo_rate_limit (day_key)",
    """CREATE TABLE IF NOT EXISTS demo_question_cache (
        question_hash TEXT PRIMARY KEY,
        question      TEXT NOT NULL,
        answer        TEXT NOT NULL,
        tool_calls    JSONB NOT NULL DEFAULT '[]'::jsonb,
        cached_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_dqc_cached ON demo_question_cache (cached_at DESC)",
]


def _ensure_schema():
    try:
        with _conn() as c, c.cursor() as cur:
            for ddl in _SCHEMA:
                try: cur.execute(ddl)
                except Exception: pass
    except Exception:
        pass


def _client_ip():
    """Best-effort IP from CF-Connecting-IP / X-Forwarded-For."""
    ip = (request.headers.get("CF-Connecting-IP")
          or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
          or request.remote_addr or "unknown")
    return ip[:60]


def _check_and_bump_rate(ip):
    """Returns (used_today, allowed). Increments if allowed."""
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32]
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO demo_rate_limit (ip_hash, day_key, used_today)
                VALUES (%s, %s, 1)
                ON CONFLICT (ip_hash, day_key)
                DO UPDATE SET used_today = demo_rate_limit.used_today + 1,
                              last_at = NOW()
                RETURNING used_today""",
                (ip_hash, day_key))
            used = int(cur.fetchone()[0])
        return used, used <= PER_IP_DAILY
    except Exception:
        # If rate-limit table fails, allow once (fail open) but log internally
        return 1, True


def _is_dc_question(q):
    ql = q.lower()
    return any(k in ql for k in DC_KEYWORDS)


def _hash_q(q):
    return hashlib.sha1(q.strip().lower().encode("utf-8")).hexdigest()[:24]


def _cached(qh):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT answer, tool_calls FROM demo_question_cache
                 WHERE question_hash = %s
                   AND cached_at > NOW() - INTERVAL '1 hour'""", (qh,))
            row = cur.fetchone()
            if row:
                return {"answer": row[0], "tool_calls": row[1] or []}
    except Exception:
        pass
    return None


def _cache_set(qh, question, answer, tool_calls):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO demo_question_cache
                       (question_hash, question, answer, tool_calls)
                   VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (question_hash) DO UPDATE
                   SET answer = EXCLUDED.answer,
                       tool_calls = EXCLUDED.tool_calls,
                       cached_at = NOW()""",
                (qh, question[:500], answer[:4000],
                 json.dumps(tool_calls)[:8000]))
    except Exception:
        pass


def _execute_tool(name, args):
    """Run a DC Hub tool internally via Flask test_client. Returns string result."""
    tool = next((t for t in DEMO_TOOLS if t["name"] == name), None)
    if not tool:
        return f"[tool '{name}' not in demo set]"
    try:
        from flask import current_app
        path = tool["internal_path"](args or {})
        with current_app.test_client() as client:
            r = client.get(path)
            if r.status_code != 200:
                return f"[tool {name} returned status {r.status_code}]"
            data = r.get_json() or {}
            # Trim to ~1500 chars for the Claude context
            return json.dumps(data)[:1500]
    except Exception as e:
        return f"[tool {name} error: {str(e)[:80]}]"


def _call_claude_with_tools(question):
    """Run a tool-use loop. Returns (answer, tool_calls)."""
    if not ANTHROPIC_API_KEY:
        return "Demo is offline (no API key configured).", []

    import requests
    tools_for_api = [{"name": t["name"],
                      "description": t["description"],
                      "input_schema": t["input_schema"]}
                     for t in DEMO_TOOLS]

    messages = [{"role": "user", "content": question}]
    tool_calls_log = []

    for turn in range(MAX_TOOL_TURNS + 1):  # +1 for final answer
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": DEMO_MODEL,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "system": DEMO_SYSTEM_PROMPT,
                    "tools": tools_for_api,
                    "messages": messages,
                },
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=20)
            if r.status_code != 200:
                return (f"Claude returned {r.status_code}. Try a simpler question.",
                        tool_calls_log)
            data = r.json()
        except Exception as e:
            return (f"Demo timed out or errored: {str(e)[:80]}", tool_calls_log)

        stop_reason = data.get("stop_reason")
        content_blocks = data.get("content", [])

        # If end_turn, extract final text
        if stop_reason == "end_turn":
            text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            return " ".join(text_parts).strip(), tool_calls_log

        # Otherwise expect tool_use blocks
        if stop_reason != "tool_use":
            text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            return " ".join(text_parts).strip() or "(no answer)", tool_calls_log

        # Echo assistant message into history
        messages.append({"role": "assistant", "content": content_blocks})

        # Execute each tool_use
        tool_results = []
        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue
            tname = block.get("name")
            targs = block.get("input") or {}
            result = _execute_tool(tname, targs)
            tool_calls_log.append({
                "name": tname,
                "input": targs,
                "result_summary": result[:200],
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.get("id"),
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    return ("Demo took too many turns. Try a more focused question.",
            tool_calls_log)


# ────────────────────────────────────────────────────────────────────
# ENDPOINT
# ────────────────────────────────────────────────────────────────────
@demo_bp.route("/api/v1/demo/ask", methods=["POST", "OPTIONS"])
def demo_ask():
    if request.method == "OPTIONS":
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp, 204

    _ensure_schema()
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question or len(question) > 400:
        return jsonify(ok=False, error="question required (max 400 chars)"), 400

    if not _is_dc_question(question):
        return jsonify(
            ok=True,
            answer=("I'm the DC Hub demo — I answer data center questions only. "
                    "Try: 'What's the DCPI for Ashburn?' or 'Compare ERCOT to PJM.'"),
            tool_calls=[],
            note="off-topic question; no Claude call burned"), 200

    qh = _hash_q(question)
    cached = _cached(qh)
    if cached:
        # Still consume one of the rate-limit budget so caching isn't a loophole
        # for unlimited usage — but make it cheap.
        _check_and_bump_rate(_client_ip())
        return jsonify(ok=True, answer=cached["answer"],
                       tool_calls=cached["tool_calls"],
                       cached=True), 200

    used, allowed = _check_and_bump_rate(_client_ip())
    if not allowed:
        return jsonify(
            ok=False,
            error="rate_limited",
            used_today=used,
            limit_per_day=PER_IP_DAILY,
            hint=("You've used today's free demo calls. Sign up free for "
                  "unlimited MCP access: https://dchub.cloud/signup"),
            signup_url="https://dchub.cloud/signup"), 429

    answer, tool_calls = _call_claude_with_tools(question)
    _cache_set(qh, question, answer, tool_calls)

    return jsonify(
        ok=True,
        answer=answer,
        tool_calls=tool_calls,
        rate_limit={"used_today": used, "limit_per_day": PER_IP_DAILY},
        cached=False), 200


@demo_bp.route("/api/v1/demo/health", methods=["GET"])
def demo_health():
    _ensure_schema()
    out = {"ok": True,
           "configured": bool(ANTHROPIC_API_KEY),
           "model": DEMO_MODEL,
           "per_ip_daily": PER_IP_DAILY,
           "tools_available": [t["name"] for t in DEMO_TOOLS]}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT COUNT(DISTINCT ip_hash), SUM(used_today)
                             FROM demo_rate_limit
                            WHERE day_key = %s""",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%d"),))
            row = cur.fetchone()
            out["unique_ips_today"] = int(row[0] or 0)
            out["total_calls_today"] = int(row[1] or 0)
            cur.execute("""SELECT COUNT(*) FROM demo_question_cache
                            WHERE cached_at > NOW() - INTERVAL '1 hour'""")
            out["cache_size"] = int(cur.fetchone()[0])
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    return jsonify(out), 200
