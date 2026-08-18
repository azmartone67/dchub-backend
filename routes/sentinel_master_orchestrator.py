"""sentinel_master_orchestrator.py — the Site Sentinel "master shell"
(r-sentinel-edge, 2026-07-03).

ONE orchestrated tick that runs the whole enhanced health→cache loop as a single
coherent cycle instead of a bare scan:

  Tier 1 SCAN     — scan_all(): parallel 101-URL sweep (was sequential ~50-100s,
                    now ~the slowest page) + the anonymous edge probe that records
                    cf-cache-status + user-perceived latency.
  Tier 2 PERSIST  — snapshot → CF KV (edge-serveable, outage-proof) and → R2
                    (full-sweep history the latest-row-per-path table can't keep).
  Tier 3 CLASSIFY — cache-coverage rollup (edge_hit / cacheable_miss / force_origin
                    / slow_origin) + latency-regression findings + history prune.
  Tier 4 DIGEST   — HUMAN-GATED #6 candidates: force-origin routes users actually
                    wait on → migrate into the tier-aware cache lane. Proposed
                    only; the tick NEVER edits the paywall boundary.

Fire-and-forget: POST returns 202 in ms and the cycle runs in a daemon thread
behind a non-blocking lock — the exact r-starve pattern from the 07-03 outage fix
(the old scan-now already did this; the master tick chains more work so it matters
more). Report lands in sentinel_master_ticks, served by /master-tick/last.

Auth: X-Admin-Key. Kill switch: SENTINEL_MASTER_DISABLED=1.
"""
from __future__ import annotations

import os
import json
import time
import threading
import datetime as _dt

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

sentinel_master_bp = Blueprint("sentinel_master", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()

# one tick at a time — an overlapping trigger answers 202/already-running.
_tick_running = threading.Lock()

# Users feel a force-origin route only if it's actually slow at the edge.
_CANDIDATE_FLOOR_MS = int(os.environ.get("SENTINEL_TIER_AWARE_FLOOR_MS", "800"))


def _is_admin(req) -> bool:
    if not _ADMIN_KEY:
        return False
    got = (req.headers.get("X-Admin-Key") or req.args.get("admin_key") or "").strip()
    return bool(got) and got == _ADMIN_KEY


def _is_disabled() -> bool:
    return str(os.environ.get("SENTINEL_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _ensure_report_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_master_ticks (
                id       BIGSERIAL PRIMARY KEY,
                ran_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                report   JSONB
            )
        """)


def _persist_report(report: dict):
    try:
        from routes.site_sentinel import _conn
        conn = _conn()
        if conn is None:
            return
        try:
            _ensure_report_table(conn)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO sentinel_master_ticks (report) VALUES (%s) ON CONFLICT DO NOTHING",
                            (json.dumps(report, default=str),))
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        note_swallowed_write("sentinel_master_ticks", where="sentinel_master_orchestrator._persist_report")
        pass


def run_sentinel_master_tick() -> dict:
    """One full cycle. In-process (no HTTP self-calls → no self-DDoS). Every step
    is guarded; the tick returns a report and never raises."""
    started = time.time()
    ran_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    report: dict = {"ok": True, "ran_at": ran_at, "steps": {}}

    # ── Tier 1 — SCAN ────────────────────────────────────────────────
    try:
        from routes.site_sentinel import scan_all
        rows = scan_all() or []
    except Exception as e:
        report["ok"] = False
        report["steps"]["scan"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        report["sweep_ms"] = int((time.time() - started) * 1000)
        _persist_report(report)
        return report

    healthy = sum(1 for r in rows if r.get("healthy"))
    report["steps"]["scan"] = {"ok": True, "total": len(rows), "healthy": healthy,
                               "unhealthy": len(rows) - healthy}

    payload = {
        "total": len(rows), "healthy": healthy, "unhealthy": len(rows) - healthy,
        "results": rows, "generated_at": ran_at,
    }

    # ── Tier 2 — PERSIST (KV + R2) ───────────────────────────────────
    try:
        from routes.sentinel_edge_sink import kv_put_snapshot, r2_archive_scan
        report["steps"]["kv_snapshot"] = kv_put_snapshot(payload)
        report["steps"]["r2_archive"] = r2_archive_scan(payload, ran_at)
    except Exception as e:
        report["steps"]["persist"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    # ── Tier 3 — CLASSIFY (coverage + regressions + prune) ───────────
    coverage: dict = {}
    for r in rows:
        cc = r.get("cache_class") or "unknown"
        coverage[cc] = coverage.get(cc, 0) + 1
    report["cache_coverage"] = coverage

    try:
        from routes.site_sentinel import _conn
        from routes.sentinel_edge_sink import latency_regressions, prune_latency_history
        conn = _conn()
        if conn is not None:
            try:
                regs = latency_regressions(conn)
                report["regressions"] = regs
                report["steps"]["regressions"] = {"ok": True, "count": len(regs)}
                pruned = prune_latency_history(conn)
                report["steps"]["history_prune"] = {"ok": True, "deleted": pruned}
            finally:
                try: conn.close()
                except Exception: pass
    except Exception as e:
        report["steps"]["classify"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    # ── Tier 4 — HUMAN-GATED #6 DIGEST (propose only) ────────────────
    # force-origin routes that users actually wait on → migrate into the
    # tier-aware cache lane (add to TIER_AWARE_CACHE_PREFIXES in the worker,
    # flip TIER_AWARE_CACHE_ENABLED). NEVER auto-applied here — it sits on the
    # paywall boundary a prior leak established.
    candidates = []
    for r in rows:
        if r.get("cache_class") == "force_origin":
            felt = r.get("edge_ms") or r.get("elapsed_ms") or 0
            if felt >= _CANDIDATE_FLOOR_MS:
                candidates.append({
                    "path": r.get("path"), "label": r.get("label"),
                    "edge_ms": r.get("edge_ms"), "origin_ms": r.get("elapsed_ms"),
                    "cf_cache_status": r.get("cf_cache_status"),
                })
    candidates.sort(key=lambda c: (c.get("edge_ms") or c.get("origin_ms") or 0), reverse=True)
    report["tier_aware_candidates"] = {
        "count": len(candidates), "items": candidates[:15],
        "note": ("Human-gated: add a vetted path to TIER_AWARE_CACHE_PREFIXES in "
                 "dchub-frontend/_worker.js and enable TIER_AWARE_CACHE_ENABLED. "
                 "Confirm the response does NOT vary by anything except tier before "
                 "enabling."),
    }

    report["sweep_ms"] = int((time.time() - started) * 1000)
    report["headline"] = {
        "healthy_pct": round(100.0 * healthy / max(len(rows), 1), 1),
        "sweep_ms": report["sweep_ms"],
        "cacheable_misses": coverage.get("cacheable_miss", 0),
        "regressions": len(report.get("regressions", [])),
        "tier_aware_candidates": report["tier_aware_candidates"]["count"],
    }
    _persist_report(report)
    return report


@sentinel_master_bp.route("/api/v1/admin/sentinel/master-tick", methods=["POST"])
def sentinel_master_tick():
    if not _ADMIN_KEY:
        return jsonify({"error": "admin_endpoint_unconfigured",
                        "hint": "Set DCHUB_ADMIN_KEY on Railway."}), 503
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if _is_disabled():
        return jsonify({"ok": True, "skipped": True,
                        "reason": "SENTINEL_MASTER_DISABLED env set"}), 200

    if not _tick_running.acquire(blocking=False):
        return jsonify({"ok": True, "started": False,
                        "note": "master-tick already running; report will land at "
                                "GET /api/v1/admin/sentinel/master-tick/last"}), 202

    def _bg():
        try:
            run_sentinel_master_tick()
        except Exception as e:
            _persist_report({"ok": False, "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                             "error": f"{type(e).__name__}: {str(e)[:200]}"})
        finally:
            try: _tick_running.release()
            except Exception: pass

    threading.Thread(target=_bg, daemon=True, name="sentinel-master-tick").start()
    return jsonify({"ok": True, "started": True,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "note": "cycle running in background; report → "
                            "GET /api/v1/admin/sentinel/master-tick/last"}), 202


@sentinel_master_bp.route("/api/v1/admin/sentinel/master-tick/last", methods=["GET"])
def sentinel_master_tick_last():
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from routes.site_sentinel import _conn
        import psycopg2.extras
        conn = _conn()
        if conn is None:
            return jsonify({"ok": False, "error": "no db"}), 200
        try:
            _ensure_report_table(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, ran_at, report FROM sentinel_master_ticks "
                            "ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
        finally:
            try: conn.close()
            except Exception: pass
        if not row:
            return jsonify({"ok": True, "last": None,
                            "note": "no sentinel master-tick has run yet"}), 200
        return jsonify({"ok": True, "last": row}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}), 200
