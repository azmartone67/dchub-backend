"""
Brain L11 — QA Agent (2026-05-18).

Probes every public surface on a 6h cron and answers the founder's
question: "is every page dynamic, error-free, and fast?"

For each surface in the canonical probe list:
  - HTTP status (>=400 = error)
  - TTFB + full-response time
  - Dynamic-vs-static classification:
      static  = pure HTML, no fetch/XHR/dynamic placeholder
      dynamic = contains JS fetch() or live-data placeholders that get
                hydrated (look for class="loading|placeholder|n" + spine ids)
  - Regression vs previous run (status changed? slower by >2x?)

Writes JSON to GET /api/v1/brain/qa-agent (cached) and the per-page
detail to /api/v1/brain/qa-agent/page?path=/foo. No admin gate — read-only.

Schema:
  brain_qa_probes (id, probed_at, path, status, ttfb_ms, total_ms,
                   bytes, classification, dynamic_signal, error)

Cron: every 6h at :05 (offset from L2 :25, L8 :45, narrative refresh)
"""

import os
import json
import time
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer11_bp = Blueprint("brain_layer11", __name__)

# Canonical probe list — every surface a real visitor or AI agent would hit
PROBE_PATHS = [
    # Marketing surfaces
    "/", "/markets", "/dcpi", "/dcpi/methodology",
    "/partnerships", "/media/outreach", "/pricing", "/signup",
    "/about", "/api-docs", "/AGENTS.md",
    # Public data pages
    "/iso/caiso", "/iso/ercot", "/iso/pjm", "/iso/miso",
    # Pulse + brain dashboards
    "/dashboard", "/brain", "/intelligence",
    # API endpoints (anon allowed)
    "/api/health",
    "/api/v1/stats",
    "/api/v1/energy/summary",
    "/api/v1/markets/list",
    "/api/v1/brain/orchestrator",
    "/api/v1/brain/consistency-radar",
    "/api/v1/brain/memory/stats",
    "/api/v1/brain/predictions",
    "/api/v1/brain/proposed-detectors",
    "/api/v1/mcp/funnel",
    "/api/v1/marketing/worker-status",
    "/api/v1/media/journalists",
    "/api/v1/media/outreach-log",
    "/.well-known/ai-agents.json",
]

_DYNAMIC_HINTS = (
    b"fetch(", b"axios.", b"XMLHttpRequest", b"class=\"loading",
    b"class=\"placeholder", b"id=\"spine-", b"data-live=", b"<script",
)
_BASE = "https://dchub.cloud"
_TIMEOUT = 12


def _ensure_table():
    """Idempotent: create brain_qa_probes table if missing."""
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn: return False
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_qa_probes (
                id            SERIAL PRIMARY KEY,
                probed_at     TIMESTAMPTZ DEFAULT NOW(),
                path          TEXT NOT NULL,
                status        INTEGER,
                ttfb_ms       INTEGER,
                total_ms      INTEGER,
                bytes         INTEGER,
                classification TEXT,
                dynamic_signal TEXT,
                error         TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qa_probes_path_time "
                    "ON brain_qa_probes(path, probed_at DESC)")
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"L11 table create failed: {e}")
        return False


def _probe(path: str) -> dict:
    """Single probe — returns {status, ttfb_ms, total_ms, bytes, classification, ...}."""
    import requests
    url = _BASE + path
    t0 = time.monotonic()
    out = {"path": path, "status": None, "ttfb_ms": None, "total_ms": None,
           "bytes": 0, "classification": "unknown", "dynamic_signal": "",
           "error": None}
    try:
        r = requests.get(url, timeout=_TIMEOUT, stream=True,
                         headers={"User-Agent": "DCHub-Brain-L11/1.0 QA"})
        out["ttfb_ms"] = int((time.monotonic() - t0) * 1000)
        out["status"] = r.status_code
        body = r.raw.read(64 * 1024) if r.raw else (r.content or b"")
        out["bytes"] = len(body)
        out["total_ms"] = int((time.monotonic() - t0) * 1000)
        try: r.close()
        except Exception: pass

        # Classify dynamic-vs-static
        if path.startswith("/api/") or path.endswith(".json"):
            out["classification"] = "api"
            try:
                data = json.loads(body.decode("utf-8", "ignore"))
                if isinstance(data, dict):
                    # Look for time-varying fields
                    keys = set(data.keys())
                    time_keys = {"as_of", "computed_at", "answered_at",
                                 "updated_at", "timestamp", "served_at"}
                    if keys & time_keys:
                        out["dynamic_signal"] = "has-timestamp"
                    elif data.get("cached") is True:
                        out["dynamic_signal"] = "cached-but-live"
                    else:
                        out["dynamic_signal"] = "live-json"
                else:
                    out["dynamic_signal"] = "list-or-scalar"
            except Exception:
                out["dynamic_signal"] = "non-json"
        else:
            # HTML surface — look for fetch() / placeholder hints
            hits = [h.decode() for h in _DYNAMIC_HINTS if h in body]
            if hits:
                out["classification"] = "dynamic"
                out["dynamic_signal"] = ",".join(hits[:4])
            else:
                out["classification"] = "static"
                out["dynamic_signal"] = "no-js-no-placeholder"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        out["total_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def _record(rows: list[dict]):
    if not _ensure_table(): return
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn: return
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                "INSERT INTO brain_qa_probes "
                "(path, status, ttfb_ms, total_ms, bytes, classification, "
                " dynamic_signal, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (r["path"], r.get("status"), r.get("ttfb_ms"),
                 r.get("total_ms"), r.get("bytes"),
                 r.get("classification"), r.get("dynamic_signal"),
                 r.get("error")),
            )
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception as e:
        logger.warning(f"L11 record failed: {e}")


def _previous_run_summary() -> dict:
    """Pull most-recent-but-one probe per path, for regression diffing."""
    out = {}
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn: return out
        cur = conn.cursor()
        cur.execute("""
            SELECT path, status, total_ms FROM (
                SELECT path, status, total_ms,
                       ROW_NUMBER() OVER (PARTITION BY path ORDER BY probed_at DESC) AS rn
                FROM brain_qa_probes
            ) t WHERE rn = 2
        """)
        for row in cur.fetchall():
            out[row[0]] = {"status": row[1], "total_ms": row[2]}
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception:
        pass
    return out


def _regressions(current: list[dict], previous: dict) -> list[dict]:
    out = []
    for r in current:
        prev = previous.get(r["path"])
        if not prev: continue
        # Status regression
        if prev["status"] and r["status"] and prev["status"] < 400 <= r["status"]:
            out.append({"path": r["path"], "kind": "status_regressed",
                        "from": prev["status"], "to": r["status"]})
        # 2x slowdown (only meaningful above 500ms baseline)
        pt, ct = prev.get("total_ms") or 0, r.get("total_ms") or 0
        if pt >= 500 and ct >= 2 * pt:
            out.append({"path": r["path"], "kind": "slowdown",
                        "from_ms": pt, "to_ms": ct})
    return out


@brain_layer11_bp.route("/api/v1/brain/qa-agent", methods=["GET", "POST"])
def qa_agent():
    """Run the QA sweep (POST) or read the latest sweep summary (GET)."""
    if request.method == "POST":
        # Probe with a small inter-request pause so we don't trip our own
        # rate limiter (which fired 429s on /iso/* and /brain on first run).
        rows = []
        for p in PROBE_PATHS:
            rows.append(_probe(p))
            time.sleep(0.5)
        prev = _previous_run_summary()
        _record(rows)
        regr = _regressions(rows, prev)

        by_class: dict = {}
        for r in rows:
            by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1

        errors = [r for r in rows if (r.get("status") or 0) >= 400
                  or r.get("error")]
        slow   = [r for r in rows if (r.get("total_ms") or 0) > 3000]
        static_html = [r for r in rows if r["classification"] == "static"
                       and not r["path"].endswith(".md")
                       and not r["path"].endswith(".json")]

        return jsonify(
            ok=True,
            ran_at=_dt.datetime.utcnow().isoformat() + "Z",
            probed=len(rows),
            by_classification=by_class,
            errors=[{"path": r["path"], "status": r["status"],
                     "error": r["error"]} for r in errors],
            slow_pages=[{"path": r["path"], "ms": r["total_ms"]} for r in slow],
            static_html_pages=[{"path": r["path"]} for r in static_html],
            regressions=regr,
            verdict=("clean" if not errors and not regr else
                     "regressions" if regr else "errors"),
        )

    # GET — cached summary from latest run
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn:
            return jsonify(ok=False, error="db unavailable"), 503
        cur = conn.cursor()
        cur.execute("SELECT MAX(probed_at) FROM brain_qa_probes")
        latest = (cur.fetchone() or [None])[0]
        if not latest:
            return jsonify(ok=True, note="No QA sweep yet — POST /api/v1/brain/qa-agent")
        # Pull the most-recent row per path
        cur.execute("""
            SELECT path, status, ttfb_ms, total_ms, classification,
                   dynamic_signal, error, probed_at
            FROM (
                SELECT path, status, ttfb_ms, total_ms, classification,
                       dynamic_signal, error, probed_at,
                       ROW_NUMBER() OVER (PARTITION BY path ORDER BY probed_at DESC) AS rn
                FROM brain_qa_probes
            ) t WHERE rn = 1
        """)
        rows = [{"path": r[0], "status": r[1], "ttfb_ms": r[2],
                 "total_ms": r[3], "classification": r[4],
                 "dynamic_signal": r[5], "error": r[6],
                 "probed_at": str(r[7])} for r in cur.fetchall()]
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

        by_class: dict = {}
        for r in rows:
            by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
        errors = [r for r in rows if (r.get("status") or 0) >= 400
                  or r.get("error")]
        slow   = [r for r in rows if (r.get("total_ms") or 0) > 3000]

        return jsonify(
            ok=True,
            latest_sweep_at=str(latest),
            probes=len(rows),
            by_classification=by_class,
            errors=errors,
            slow_pages=slow,
            verdict=("clean" if not errors else "errors"),
            rows=rows,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503


@brain_layer11_bp.route("/api/v1/brain/qa-agent/history", methods=["GET"])
def qa_history():
    """Per-path history — useful to spot flapping endpoints."""
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify(ok=False, error="missing path param"), 400
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn:
            return jsonify(ok=False, error="db unavailable"), 503
        cur = conn.cursor()
        cur.execute(
            "SELECT probed_at, status, total_ms, classification, error "
            "FROM brain_qa_probes WHERE path = %s "
            "ORDER BY probed_at DESC LIMIT 30",
            (path,),
        )
        history = [{"at": str(r[0]), "status": r[1], "total_ms": r[2],
                    "classification": r[3], "error": r[4]}
                   for r in cur.fetchall()]
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return jsonify(ok=True, path=path, count=len(history), history=history)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503
