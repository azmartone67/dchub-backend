"""
Brain L11 — QA Agent (2026-05-18).

★ RETIRED 2026-09-12. The 6h sweep below was disabled on 2026-05-19 (container
crash-loop) and never re-enabled; GET went on serving that sweep as current
surface health for 116 days. /api/v1/brain/qa-agent now answers 410 on GET and
POST. Live QA is fast-QA (routes/brain_fast_qa.py). The sweep helpers below are
unreachable and kept only as history; /history still serves timestamped rows.

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
    # Phase FF+7 (2026-05-19) — conversion-flow probes. /upgrade should
    # 302 to /redeem/<code> when given any key; /redeem/INVALID should
    # render the 404-ish "expired/missing code" page (still 200). Both
    # are now in the critical path for closing the funnel L14 surfaced.
    "/upgrade?key=qa-probe&tool=get_grid_intelligence",
    "/api/v1/brain/causal",       # L14
    "/api/v1/brain/expansion",    # L12
    "/api/v1/brain/qa-agent",     # L11 itself (cached GET)
    "/api/v1/redeem/funnel-stats",  # permanent watchdog endpoint
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


RETIRED_AT = "2026-09-12"
LAST_SWEEP_DISABLED = "2026-05-19"


def retired_payload() -> dict:
    """What /api/v1/brain/qa-agent answers now that L11 is retired."""
    return {
        "ok": False,
        "retired": True,
        "retired_at": RETIRED_AT,
        "sweep_disabled_since": LAST_SWEEP_DISABLED,
        "reason": ("L11's surface sweep was disabled on 2026-05-19 after a "
                   "container crash-loop and never re-enabled. This endpoint "
                   "went on serving that May sweep as current surface health, "
                   "so it no longer reports surface health at all."),
        "superseded_by": {
            "live_qa": "fast-QA: routes/brain_fast_qa.py, swept every ~30 min",
            "durable_output": "brain_findings rows with detector = 'fast_qa'",
            "history": "/api/v1/brain/qa-agent/history?path=<path> (timestamped rows)",
        },
    }


@brain_layer11_bp.route("/api/v1/brain/qa-agent", methods=["GET", "POST"])
def qa_agent():
    """RETIRED 2026-09-12 — GET and POST both answer 410. See retired_payload()."""
    # ★ The sweep was DISABLED 2026-05-19 (container crash-loop) and never
    # re-enabled, but GET kept serving that sweep as current surface health for
    # 116 days: brain_self_test passed on it, and L8 and L14 fed its 404s into
    # their prompts as live state. POST is refused too, so the crash-looping
    # sweep cannot be re-armed by hitting this endpoint. /history below is left
    # as-is — every row it returns carries its own probed_at.
    return jsonify(retired_payload()), 410


@brain_layer11_bp.route("/api/v1/brain/qa-agent/history", methods=["GET"])
def qa_history():
    """Per-path history — useful to spot flapping endpoints."""
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify(ok=False, error="missing path param"), 400
    conn = None
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
        return jsonify(ok=True, path=path, count=len(history), history=history)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass
