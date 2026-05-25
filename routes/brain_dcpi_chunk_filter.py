"""
brain_dcpi_chunk_filter.py — corrects DCPI false positives.

Phase ZZZZZ-round44 (2026-05-25). Brain counts dcpi_runs with
markets_scored=0 as failures, but DCPI processes in chunks of 100
and the universe is ~285 markets. Tail chunks (200:300, 300:400)
correctly return 0. This module classifies expected vs true failures.
"""
import os, datetime, re
from contextlib import contextmanager
from collections import defaultdict
from flask import Blueprint, jsonify
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

brain_dcpi_filter_bp = Blueprint("brain_dcpi_filter", __name__,
                                  url_prefix="/api/v1/brain/dcpi")
CHUNK_RE = re.compile(r"chunk\[(\d+):(\d+)\]")

def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()

@brain_dcpi_filter_bp.route("/true-health", methods=["GET"])
def true_health():
    out = {"at": datetime.datetime.utcnow().isoformat() + "Z"}
    if not (_pg and _dsn()):
        return jsonify({**out, "error": "no_db"}), 200
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id, started_at, source, markets_scored, error_count FROM dcpi_runs WHERE started_at > NOW() - INTERVAL %s ORDER BY started_at DESC""", ("24 hours",))
            rows = cur.fetchall()
    except Exception as e:
        return jsonify({**out, "error": f"{type(e).__name__}: {str(e)[:140]}"}), 500

    batches = defaultdict(list)
    for r in rows:
        key = r["started_at"].replace(second=0, microsecond=0).isoformat()[:16]
        batches[key].append(r)

    healthy = expected_zero = true_fail = real_err = 0
    for batch_key, runs in batches.items():
        runs.sort(key=lambda r: r["started_at"])
        max_chunk = 0
        for r in runs:
            m = CHUNK_RE.search(r.get("source") or "")
            if m and r["markets_scored"] > 0:
                max_chunk = max(max_chunk, int(m.group(2)))
        for r in runs:
            m = CHUNK_RE.search(r.get("source") or "")
            cs = int(m.group(1)) if m else 0
            if r["error_count"] > 0: real_err += 1
            elif r["markets_scored"] > 0: healthy += 1
            elif cs >= max_chunk and max_chunk > 0: expected_zero += 1
            else: true_fail += 1

    total = healthy + expected_zero + true_fail + real_err
    out.update({
        "total_runs_24h": total,
        "healthy_chunks": healthy,
        "expected_zero_tail": expected_zero,
        "true_failures": true_fail,
        "real_errors": real_err,
        "true_failure_rate": round((true_fail + real_err) / max(1, total), 3),
        "naive_failure_rate": round((true_fail + real_err + expected_zero) / max(1, total), 3),
        "verdict": "healthy" if (true_fail + real_err) == 0 else ("degraded" if (true_fail + real_err) <= 2 else "unhealthy"),
    })
    return jsonify(out), 200
