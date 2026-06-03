"""brain_layer14_slo_burn — auto-file a brain finding when /api/v1/slo/error-budget
reports soft_burn or hard_burn. Closes the loop: the next path that starts
5xxing automatically gets investigated instead of waiting for the user.

Runs on its own background interval (5 min). Idempotent: dedupes findings by
(top_pattern, verdict) within a 1h window so we don't spam.
"""
import os
import time
import threading
import logging
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

slo_burn_bp = Blueprint("brain_layer14_slo_burn", __name__)
log = logging.getLogger(__name__)

_INTERVAL_S = 300
_DEDUP_WINDOW_S = 3600
_LAST_FILED = {}  # (pattern, verdict) -> ts


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _file_finding(pattern, verdict, err_pct, n5xx):
    """Insert into brain_findings (best-effort). Schema discovered from brain_consistency_radar."""
    if not (_pg and _dsn()):
        return
    try:
        with _pg.connect(_dsn(), connect_timeout=4) as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_findings (
                    id          BIGSERIAL PRIMARY KEY,
                    detected_at TIMESTAMPTZ DEFAULT NOW(),
                    issue_type  TEXT,
                    url         TEXT,
                    detail      TEXT,
                    severity    TEXT
                )
            """)
            cur.execute(
                "INSERT INTO brain_findings (issue_type, url, detail, severity) "
                "VALUES (%s, %s, %s, %s)",
                (
                    f"slo_{verdict}",
                    pattern,
                    f"SLO {verdict}: pattern {pattern} produced {n5xx} 5xx in 5min "
                    f"(global err_pct={err_pct}%). Pillar: errors_slo_gate.",
                    "critical" if verdict == "hard_burn" else "high",
                ),
            )
            c.commit()
    except Exception as e:
        log.warning("slo-burn finding insert failed: %s", e)


def _scan_once():
    import requests as _rq
    try:
        r = _rq.get("http://127.0.0.1:8080/api/v1/slo/error-budget",
                    timeout=4,
                    headers={"User-Agent": "dchub-brain-l14/1.0",
                             "X-DC-Probe": "slo-burn"})
        data = r.json() or {}
    except Exception as e:
        log.warning("slo-burn scan probe failed: %s", e)
        return
    verdict = data.get("verdict")
    if verdict not in ("soft_burn", "hard_burn"):
        return
    top = (data.get("top_5xx_paths") or [{}])[0]
    pat = top.get("pattern") or "unknown"
    n5xx = top.get("n5xx") or 0
    key = (pat, verdict)
    now = time.time()
    if (now - _LAST_FILED.get(key, 0)) < _DEDUP_WINDOW_S:
        return  # dedup
    _LAST_FILED[key] = now
    _file_finding(pat, verdict, data.get("global_err_pct"), n5xx)
    log.info("[brain-l14] filed slo finding pattern=%s verdict=%s n5xx=%s",
             pat, verdict, n5xx)


def _loop():
    while True:
        try:
            _scan_once()
        except Exception as e:
            log.warning("slo-burn loop iter failed: %s", e)
        time.sleep(_INTERVAL_S)


def start_scheduler():
    if getattr(start_scheduler, "_started", False):
        return
    start_scheduler._started = True
    threading.Thread(target=_loop, daemon=True, name="brain-l14-slo-burn").start()


@slo_burn_bp.route("/api/v1/brain/slo-burn/last", methods=["GET"])
def last_filed():
    return jsonify({
        "last_filed": [
            {"pattern": k[0], "verdict": k[1], "ts": v}
            for k, v in _LAST_FILED.items()
        ],
        "interval_s": _INTERVAL_S,
    }), 200
