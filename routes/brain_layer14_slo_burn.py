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
    """File a burn finding (best-effort) through the canonical writer.

    History worth keeping: an early version invented its own
    (issue_type, severity) schema and 100% of its inserts failed silently.
    The replacement hand-rolled the "canonical" column list instead, which
    is the same bet one step smaller — it assumed a live schema rather than
    asking. Since 2026-08-29 it goes through
    routes.brain_findings_writer.upsert_brain_finding(), which introspects.

    Severity stays encoded in `issue` ('slo_hard_burn' vs 'slo_soft_burn')
    and `detector` ('brain_l14_slo_burn')."""
    if not (_pg and _dsn()):
        return
    try:
        with _pg.connect(_dsn(), connect_timeout=4) as c, c.cursor() as cur:
            # ★2026-08-29 lane 8: canonical writer. The hand-rolled INSERT
            # promised "idempotent UPSERT-like: bump last_seen + count
            # instead of duplicating" in its comment and did no such thing
            # — it was a plain INSERT. upsert_brain_finding actually does it.
            from routes.brain_findings_writer import upsert_brain_finding
            upsert_brain_finding(
                cur,
                issue=f"slo_{verdict}",
                url=pattern,
                count=1,
                detail=(f"SLO {verdict}: pattern {pattern} produced {n5xx} 5xx "
                        f"in 5min (global err_pct={err_pct}%). "
                        f"Pillar: errors_slo_gate."),
                detector="brain_l14_slo_burn",
                status="open",
                count_kind="occurrence")
            c.commit()
    except Exception as e:
        log.warning("slo-burn finding insert failed: %s", e)


def _scan_once():
    import requests as _rq
    try:
        _port = int(os.environ.get("PORT", 8080))
        r = _rq.get(f"http://127.0.0.1:{_port}/api/v1/slo/error-budget",
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
    # r-slo-actuate (2026-07-16): DETECT -> ACT. A hard_burn on a DB-backed /api
    # endpoint IS the facility-hard_burn class (unindexed seq-scan starving the
    # pool). Trigger the self-growing index engine off-cycle so the missing index
    # gets added NOW, not at the weekly tick. Safe: the engine only auto-applies
    # additive single-column indexes (IF NOT EXISTS) + has its own kill switch.
    # Runs in a daemon thread so this 300s scan loop never blocks on a build.
    if verdict == "hard_burn" and isinstance(pat, str) and pat.startswith("/api/"):
        try:
            import threading as _th

            def _actuate(_pat=pat):
                try:
                    import self_growing_index as _sgi
                    out = _sgi.run_index_advisor(reason=f"slo_burn:{_pat}")
                    log.info("[brain-l14] slo_burn->index engine: applied=%d proposed=%d",
                             len(out.get("applied") or []), len(out.get("proposed") or []))
                except Exception as _e:
                    log.warning("[brain-l14] index-engine actuation failed: %s", str(_e)[:120])

            _th.Thread(target=_actuate, daemon=True, name="l14-slo-index").start()
        except Exception:
            pass


def _loop():
    # Warm-up: at boot gunicorn isn't listening on localhost:$PORT yet, so an
    # immediate self-probe times out (read timeout=4) and logs a false
    # "slo-burn scan probe failed" warning on every deploy. Sleep one interval
    # first so the first probe lands after the server is actually serving.
    time.sleep(_INTERVAL_S)
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
