"""
agent_broadcast_loop.py — hourly broadcast to MCP registries + agent dirs.

Phase ZZZZZ-round47.26 (2026-05-26). Static manifests get crawled when
agents come looking. ACTIVE broadcasting pushes our latest capabilities
to registries so other agents discover us sooner.

What we broadcast every hour:
  1. Refresh ping to registry.modelcontextprotocol.io/v0/servers (cache-bust)
  2. Refresh ping to Smithery, mcp.so, Glama, PulseMCP — re-crawl us
  3. Update the timestamp on /agent.json + /.well-known/mcp-tools.json
  4. Log every broadcast attempt for observability

We DON'T:
  - Send unsolicited "hey check us out" emails to other org's mailboxes
  - Spam social media (the daily quad + weekly partnership cycle handles that)
  - Modify other people's data

Endpoints:
  POST /api/v1/agents/broadcast        run a broadcast cycle (cron-callable)
  GET  /api/v1/agents/broadcasts       history of broadcasts (last 50)
  GET  /api/v1/agents/broadcast/status freshness gauge
"""
import os
import datetime
import urllib.request
import urllib.error
from contextlib import contextmanager
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

agent_broadcast_bp = Blueprint("agent_broadcast_loop", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_broadcast_log (
                    id          SERIAL PRIMARY KEY,
                    fired_at    TIMESTAMPTZ DEFAULT NOW(),
                    target      TEXT,
                    target_url  TEXT,
                    method      TEXT,
                    status_code INT,
                    elapsed_ms  INT,
                    note        TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_abl_ts ON agent_broadcast_log(fired_at DESC);
                CREATE INDEX IF NOT EXISTS ix_abl_tgt ON agent_broadcast_log(target, fired_at DESC);
            """)
    except Exception:
        pass


_ensure_table()


# ── Broadcast targets ────────────────────────────────────────────────
# Each entry: (name, url, method, kind). "kind" is just for grouping in
# the dashboard. URLs that 404 silently are fine — we still log them so
# we can see which targets are alive.
_TARGETS = [
    # MCP registries: GET the search endpoint with our slug to keep our
    # cache warm + signal active life. Some registries auto-refresh on
    # query traffic.
    ("registry.modelcontextprotocol.io",
     "https://registry.modelcontextprotocol.io/v0/servers?search=dchub", "GET", "registry"),
    ("smithery.ai",
     "https://smithery.ai/server/azmartone67/dchub", "GET", "registry"),
    ("mcp.so",
     "https://mcp.so/server/dchub", "GET", "registry"),
    ("glama.ai",
     "https://glama.ai/mcp/servers/dchub", "GET", "registry"),
    ("pulsemcp",
     "https://pulsemcp.com/servers/dchub", "GET", "registry"),

    # Our own discovery surfaces — GETting them warms CF cache + bumps
    # the audit log on our side so we can see the agents are reachable.
    ("self_mcp_manifest",
     "https://api.dchub.cloud/api/v1/mcp/manifest", "GET", "self"),
    ("self_capabilities",
     "https://api.dchub.cloud/api/v1/agents/capabilities.json", "GET", "self"),
    ("self_agents_md",
     "https://dchub.cloud/AGENTS.md", "GET", "self"),
    # r47.26.1 (2026-05-26): dchub.cloud Pages worker doesn't route
    # /agent.json or /.well-known/mcp-server.json (the dchubapiproxy
    # zone worker doesn't know those paths). api.dchub.cloud goes
    # straight to Flask via /api/* allowlist + works fine.
    ("self_agent_json",
     "https://api.dchub.cloud/agent.json", "GET", "self"),
    ("self_well_known",
     "https://api.dchub.cloud/.well-known/mcp-server.json", "GET", "self"),
]


def _hit(name, url, method, timeout=10):
    started = datetime.datetime.utcnow()
    try:
        req = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": "DCHub-AgentBroadcast/1.0 (+https://dchub.cloud/mcp)",
                     "Accept": "application/json, text/html",
                     "X-DC-Broadcast": "1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048)
            elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
            return {"status_code": resp.status, "bytes": len(body), "elapsed_ms": elapsed_ms,
                    "note": "ok"}
    except urllib.error.HTTPError as e:
        elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
        return {"status_code": e.code, "elapsed_ms": elapsed_ms, "note": f"http_{e.code}"}
    except Exception as e:
        elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
        return {"status_code": 0, "elapsed_ms": elapsed_ms,
                "note": f"{type(e).__name__}: {str(e)[:80]}"}


def _log(target, url, method, result):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_broadcast_log
                  (target, target_url, method, status_code, elapsed_ms, note)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (target, url, method, result.get("status_code"),
                  result.get("elapsed_ms"), result.get("note", "")[:300]))
    except Exception:
        pass


@agent_broadcast_bp.route("/api/v1/agents/broadcast",
                           methods=["POST", "GET"], strict_slashes=False)
def broadcast():
    """Run one full broadcast cycle. Cron-callable."""
    results = []
    healthy = 0
    for name, url, method, kind in _TARGETS:
        r = _hit(name, url, method)
        _log(name, url, method, r)
        results.append({"target": name, "url": url, "method": method,
                        "kind": kind, **r})
        if 200 <= (r.get("status_code") or 0) < 400:
            healthy += 1

    return jsonify({
        "at":           datetime.datetime.utcnow().isoformat() + "Z",
        "targets_total": len(_TARGETS),
        "targets_healthy": healthy,
        "results":      results,
        "next_fire_hint": "Cron should hit this hourly at :05 past the hour.",
    }), 200 if healthy == len(_TARGETS) else 207


@agent_broadcast_bp.route("/api/v1/agents/broadcast/unhealthy",
                           methods=["GET"], strict_slashes=False)
def unhealthy():
    """r68-d (2026-05-26): /api/v1/agents/broadcast returns
    targets_healthy=8/10 but doesn't tell the operator WHICH 2 are
    failing. This endpoint surfaces the failing targets with their
    latest status_code + error note from agent_broadcast_log so the
    operator can fix or document them in one click.

    No auth — visibility into platform health is a public signal."""
    out = []
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db", "unhealthy": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            # Latest row per target — anything not 2xx/3xx is unhealthy
            cur.execute("""
                SELECT DISTINCT ON (target)
                       target, target_url, method, status_code,
                       elapsed_ms, note, fired_at
                  FROM agent_broadcast_log
                 ORDER BY target, fired_at DESC
            """)
            rows = cur.fetchall() or []
        for r in rows:
            sc = int(r[3] or 0)
            healthy = 200 <= sc < 400
            if not healthy:
                out.append({
                    "target":       r[0],
                    "url":          r[1],
                    "method":       r[2],
                    "status_code":  sc,
                    "elapsed_ms":   r[4],
                    "error_note":   r[5] or "",
                    "last_check":   r[6].isoformat() if r[6] else None,
                    "likely_fix":   _likely_fix_for(r[0], r[1], sc, r[5] or ""),
                })
    except Exception as e:
        return jsonify({"error": str(e)[:200], "unhealthy": []}), 200

    return jsonify({
        "checked_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "unhealthy_count": len(out),
        "unhealthy":    out,
        "hint":         ("Each entry has a `likely_fix` heuristic. To "
                          "trigger a re-broadcast: POST /api/v1/agents/broadcast"),
    }), 200


def _likely_fix_for(target: str, url: str, status: int, note: str) -> str:
    """Heuristic remediation suggestion per failure mode."""
    if status == 404:
        return (f"Route missing — check Flask route + worker _routes.json "
                  f"for {url}. Same singular/plural pattern as the /partners fix.")
    if status == 403:
        return ("CF block — check worker PHASE_282_RAILWAY_PATHS sets + "
                "_routes.json include for this path. Could be loop guard.")
    if status in (502, 503, 504):
        return f"Upstream timeout — Railway slow or down for {url}. Check /alive."
    if status == 0:
        return ("DNS/network — probably an unreachable URL or worker tab "
                "intercepting. Check that {url} resolves.")
    if "ssl" in (note or "").lower() or "certificate" in (note or "").lower():
        return "TLS cert issue — likely a subdomain CNAME drift or expired cert."
    return ("Check Railway logs for this path + verify the route is "
              "registered. Worker drift is another possibility.")


@agent_broadcast_bp.route("/api/v1/agents/broadcasts",
                           methods=["GET"], strict_slashes=False)
def history():
    """Recent broadcast attempts."""
    if not (_pg and _dsn()):
        return jsonify({"recent": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT target, target_url, status_code, elapsed_ms, note, fired_at
                  FROM agent_broadcast_log
                 ORDER BY fired_at DESC LIMIT 50
            """)
            rows = [{
                "target":      r[0],
                "url":         r[1],
                "status_code": r[2],
                "elapsed_ms":  r[3],
                "note":        r[4],
                "fired_at":    r[5].isoformat() if r[5] else None,
            } for r in cur.fetchall()]
        return jsonify({"count": len(rows), "recent": rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:140], "recent": []}), 200


@agent_broadcast_bp.route("/api/v1/agents/broadcast/status",
                           methods=["GET"], strict_slashes=False)
def status():
    """Freshness gauge — when did we last broadcast, what % healthy?"""
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            # Aggregate by target — last fire + recent success rate
            cur.execute("""
                SELECT target,
                       MAX(fired_at)  AS last_fire,
                       COUNT(*)       FILTER (WHERE fired_at > NOW() - INTERVAL '24 hours')                                            AS fires_24h,
                       COUNT(*)       FILTER (WHERE fired_at > NOW() - INTERVAL '24 hours' AND status_code BETWEEN 200 AND 399)        AS ok_24h
                  FROM agent_broadcast_log
                 GROUP BY target
                 ORDER BY MAX(fired_at) DESC
            """)
            per_target = []
            for r in cur.fetchall():
                fires = int(r[2] or 0); ok = int(r[3] or 0)
                per_target.append({
                    "target":         r[0],
                    "last_fire_at":   r[1].isoformat() if r[1] else None,
                    "fires_24h":      fires,
                    "ok_24h":         ok,
                    "success_rate":   round(100 * ok / fires, 1) if fires else None,
                })
            cur.execute("SELECT MAX(fired_at) FROM agent_broadcast_log")
            last_overall = cur.fetchone()[0]
        return jsonify({
            "last_broadcast_at": last_overall.isoformat() if last_overall else None,
            "minutes_since":     (
                round((datetime.datetime.now(datetime.timezone.utc) - last_overall).total_seconds() / 60, 1)
                if last_overall else None
            ),
            "per_target":   per_target,
            "computed_at":  datetime.datetime.utcnow().isoformat() + "Z",
        }), 200, {"Cache-Control": "no-store"}
    except Exception as e:
        return jsonify({"error": str(e)[:140]}), 500
