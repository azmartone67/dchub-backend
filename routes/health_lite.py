"""
health_lite.py — DB-free health endpoint for high-concurrency probes.

Phase ZZZZZ-round45 (2026-05-25). Load test showed /health timed out
15/16 times at 16 concurrent requests despite gunicorn 4→16 thread
bump. /health touches the DB pool + memory introspection — under load,
threads block on Neon round-trips and starve each other.

This module exposes /healthz (Kubernetes-style) that returns 200 OK
immediately with no DB/disk access. Use for:
  - CF worker health probes
  - GitHub Actions cron heartbeat probes
  - Railway healthcheck
  - Any high-frequency liveness check

Existing /health endpoint stays for the rich diagnostic (memory,
facility count, pool stats) but should NOT be hit at high concurrency.
"""
import time, os
from flask import Blueprint, jsonify

health_lite_bp = Blueprint("health_lite", __name__)

# Process start time captured at module load (boot-up)
_BOOT_TS = time.time()

@health_lite_bp.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Always-200 OK. No DB, no allocations. Sub-1ms response."""
    return jsonify({
        "ok": True,
        "uptime_s": round(time.time() - _BOOT_TS, 1),
        "service": "dchub-backend",
    }), 200

@health_lite_bp.route("/livez", methods=["GET", "HEAD"])
def livez():
    return ("OK\n", 200, {"Content-Type": "text/plain; charset=utf-8"})

@health_lite_bp.route("/readyz", methods=["GET", "HEAD"])
def readyz():
    """Light readiness — checks env vars not DB."""
    needed = ["DATABASE_URL"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        return jsonify({"ready": False, "missing_env": missing}), 503
    return jsonify({"ready": True}), 200
