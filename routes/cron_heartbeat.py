"""
cron_heartbeat.py — single cron endpoint that runs ALL scheduled warmers.

Phase ZZZZZ-round37.1 (2026-05-24). Railway service-level cron only
takes ONE expression and converts the whole service into a cron job —
which would break dchub-backend as a web service. This module collapses
multiple scheduled jobs (grid-warmer, brain-warming, future additions)
behind a single HTTP endpoint that dispatches by current UTC time.

User configures ONE external cron (GitHub Actions, cron-job.org, or a
separate Railway cron service) that hits /api/v1/cron/heartbeat every
5 minutes. The endpoint decides what to run based on the current minute
+ hour.

Schedule today:
  Every 5 min     → grid-warmer (keeps /grid/<ISO> CF cache warm)
  Every hour @ :03 → brain warming (lighter heartbeat refresh)
  Daily @ 14:00 UTC → full brain-warming (hot compute paths + 7 ISOs)

Add new jobs by editing _DISPATCH below.
"""
import os
import datetime
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request

cron_heartbeat_bp = Blueprint("cron_heartbeat", __name__,
                               url_prefix="/api/v1/cron")

BASE = "https://api.dchub.cloud"


def _hit(url, method="POST", timeout=30):
    try:
        data = b"" if method == "POST" else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"User-Agent": "DCHub-CronHeartbeat/1.0",
                     "X-DC-Internal-Cron": "1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512)
            return {"status": resp.status, "bytes": len(body)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "http"}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:80]}"}


# Job schedule: (label, url, method, predicate(now) → should_run)
# Predicates take a datetime.datetime UTC and return True if the job
# should fire on THIS invocation. Keep cheap → grid every call;
# expensive → only once/hour or once/day.
_DISPATCH = [
    # Grid warmer — every invocation (assumes cron fires every 5 min)
    ("grid_warmer",
     f"{BASE}/api/v1/grid-warmer/warm",
     "POST",
     lambda now: True),

    # Brain heartbeat warmer — once per hour at :03 (to spread load)
    ("brain_warmer_hourly",
     f"{BASE}/api/v1/brain-warming/warm",
     "POST",
     lambda now: now.minute < 5),  # fires on the first invocation per hour

    # Heavy brain detectors run — once daily at 14:00 UTC
    ("brain_detectors_daily",
     f"{BASE}/api/v1/brain-warming/detectors",
     "GET",
     lambda now: now.hour == 14 and now.minute < 5),
]


# AUTO-REPAIR: duplicate route '/heartbeat' also in routes/heartbeat.py:431 — review and remove one
@cron_heartbeat_bp.route("/heartbeat", methods=["GET", "POST"])
def heartbeat():
    """Run every job whose predicate returns True for current UTC time."""
    started = datetime.datetime.utcnow()
    results = []
    for label, url, method, predicate in _DISPATCH:
        if predicate(started):
            r = _hit(url, method=method)
            results.append({"job": label, "url": url, "method": method, **r})
        else:
            results.append({"job": label, "url": url, "method": method,
                            "skipped": True, "reason": "predicate_false"})
    elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    ran = [r for r in results if not r.get("skipped")]
    healthy = sum(1 for r in ran if 200 <= r.get("status", 0) < 400)
    return jsonify({
        "at": started.isoformat() + "Z",
        "elapsed_ms": elapsed_ms,
        "jobs_total": len(_DISPATCH),
        "jobs_ran": len(ran),
        "jobs_skipped": len(_DISPATCH) - len(ran),
        "jobs_healthy": healthy,
        "results": results,
        "next_schedule_hint": ("Call this endpoint every 5 minutes from "
                                "any external cron. The endpoint decides "
                                "which jobs to actually run based on UTC time."),
    }), 200 if healthy == len(ran) else 207

# AUTO-REPAIR: duplicate route '/health' also in index_api.py:516 — review and remove one

@cron_heartbeat_bp.route("/health", methods=["GET"])
def health():
    now = datetime.datetime.utcnow()
    return jsonify({
        "blueprint": "cron_heartbeat_bp",
        "now_utc": now.isoformat() + "Z",
        "dispatch_count": len(_DISPATCH),
        "would_run_now": [
            label for label, _, _, pred in _DISPATCH if pred(now)
        ],
        "phase": "ZZZZZ-round37.1",
    }), 200
