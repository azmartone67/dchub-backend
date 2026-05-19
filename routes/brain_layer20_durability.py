"""
Brain L20 — Durability Guard (2026-05-19).

The defense the brain didn't have during the 2026-05-19 crash-loop:
ACTIVE monitoring + auto-mitigation for the failure modes that caused
the map to go down 4 times in a day.

Failure mode this layer prevents:
  - Synchronous Claude call inside a Flask request handler (30-90s)
  - Holding Claude's response body in memory while serializing
  - Memory crosses watchdog threshold mid-call
  - Watchdog marks unhealthy 3x → SIGTERM → restart → cycle repeats

L20 does three things every 30 seconds (lightweight, no LLM):

  1. Polls process RSS via psutil. If RSS > 70% of the watchdog
     threshold, sets an in-memory flag DURABILITY_MEMORY_HIGH=True.
     The kill-switched Claude endpoints (L8/L14/L16/L18) check this
     flag and refuse to start a new Claude call when it's True —
     PREVENTING the memory crossing instead of triggering it.

  2. Watches for in-flight Claude calls. If any has been running
     for >25 seconds, logs a warning to brain_durability_log so
     the next L14 cycle sees it as a finding.

  3. Maintains a sliding-window count of how many Claude calls have
     started in the last minute. If >2 (the threshold above which
     today's crash loop happened), throttles new calls.

This is the brain DEFENDING ITSELF from the architectural mistake
that caused today's outages.

Endpoints:
  GET  /api/v1/brain/durability        — current guard state
  POST /api/v1/brain/durability/clear-flags  — manual reset (admin)

Background thread starts at app init. Module-level globals are
intentional — every layer that does Claude calls reads them.
"""

import os
import time
import threading
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer20_bp = Blueprint("brain_layer20", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

# ── Module-level globals checked by L8/L14/L16/L18 BEFORE starting Claude calls
DURABILITY_MEMORY_HIGH = False        # set True when RSS > 70% threshold
DURABILITY_LAST_RSS_MB = 0            # last observed
DURABILITY_LAST_CHECK_AT = 0.0        # monotonic timestamp
DURABILITY_THRESHOLD_MB = int(os.environ.get(
    "DURABILITY_GUARD_THRESHOLD_MB", "2200"))  # 70% of 3072
DURABILITY_INFLIGHT_CLAUDE_CALLS: dict = {}  # call_id -> (started_at, layer)
DURABILITY_RECENT_STARTS: list = []   # monotonic timestamps, last 60s
DURABILITY_MAX_CONCURRENT = int(os.environ.get(
    "DURABILITY_MAX_CONCURRENT_CLAUDE", "1"))
DURABILITY_MAX_PER_MIN = int(os.environ.get(
    "DURABILITY_MAX_CLAUDE_PER_MIN", "2"))


# ── Public helpers other layers call ────────────────────────────────

def can_start_claude_call(layer_name: str) -> tuple[bool, str]:
    """Returns (allowed, reason). L8/L14/L16/L18 should call this
    BEFORE starting their Claude call. If allowed=False, return 503
    immediately instead of starting the call.
    """
    if DURABILITY_MEMORY_HIGH:
        return False, (f"durability_memory_high: RSS at "
                       f"{DURABILITY_LAST_RSS_MB}MB approaching watchdog "
                       f"threshold. Claude call refused to prevent "
                       f"watchdog SIGTERM.")
    if len(DURABILITY_INFLIGHT_CLAUDE_CALLS) >= DURABILITY_MAX_CONCURRENT:
        return False, (f"durability_concurrent_limit: "
                       f"{len(DURABILITY_INFLIGHT_CLAUDE_CALLS)} Claude "
                       f"calls already in flight (cap "
                       f"{DURABILITY_MAX_CONCURRENT}). Try again in a few s.")
    # Sliding 60-second window
    now = time.monotonic()
    recent = [t for t in DURABILITY_RECENT_STARTS if now - t < 60]
    DURABILITY_RECENT_STARTS.clear()
    DURABILITY_RECENT_STARTS.extend(recent)
    if len(recent) >= DURABILITY_MAX_PER_MIN:
        return False, (f"durability_rate_limit: {len(recent)} Claude "
                       f"calls in last 60s (cap {DURABILITY_MAX_PER_MIN}). "
                       f"Throttling.")
    return True, "ok"


def register_claude_call_start(layer_name: str) -> str:
    """Caller should call this immediately before the actual Claude
    HTTP POST. Returns a call_id token to pass to register_claude_call_end."""
    import uuid
    call_id = str(uuid.uuid4())[:8]
    DURABILITY_INFLIGHT_CLAUDE_CALLS[call_id] = (time.monotonic(), layer_name)
    DURABILITY_RECENT_STARTS.append(time.monotonic())
    return call_id


def register_claude_call_end(call_id: str) -> None:
    """Caller should always call this in finally."""
    DURABILITY_INFLIGHT_CLAUDE_CALLS.pop(call_id, None)


# ── Background watcher thread ───────────────────────────────────────

_watcher_started = False
_watcher_thread = None


def _watcher_loop():
    """Every 30s: sample RSS, update DURABILITY_MEMORY_HIGH flag, log
    any Claude call that's been in-flight >25s (close to the slow-
    request threshold)."""
    global DURABILITY_MEMORY_HIGH, DURABILITY_LAST_RSS_MB, DURABILITY_LAST_CHECK_AT
    import psutil
    proc = psutil.Process(os.getpid())
    while True:
        try:
            rss_mb = int(proc.memory_info().rss / (1024 * 1024))
            DURABILITY_LAST_RSS_MB = rss_mb
            DURABILITY_LAST_CHECK_AT = time.monotonic()
            was_high = DURABILITY_MEMORY_HIGH
            DURABILITY_MEMORY_HIGH = rss_mb > DURABILITY_THRESHOLD_MB
            if DURABILITY_MEMORY_HIGH and not was_high:
                logger.warning(f"[L20-durability] RSS={rss_mb}MB crossed "
                                f"{DURABILITY_THRESHOLD_MB}MB threshold. "
                                f"Pausing new Claude calls to prevent "
                                f"watchdog SIGTERM.")
            elif was_high and not DURABILITY_MEMORY_HIGH:
                logger.info(f"[L20-durability] RSS={rss_mb}MB back below "
                             f"threshold. Resuming Claude calls.")

            # Watch for slow in-flight calls
            now = time.monotonic()
            for call_id, (started_at, layer) in list(DURABILITY_INFLIGHT_CLAUDE_CALLS.items()):
                age = now - started_at
                if age > 25:
                    logger.warning(f"[L20-durability] {layer} Claude call "
                                    f"{call_id} in-flight {age:.1f}s — "
                                    f"approaching slow-request threshold "
                                    f"(30s). RSS={rss_mb}MB.")
        except Exception as e:
            logger.warning(f"[L20-durability] watcher iteration failed: {e}")
        time.sleep(30)


def start_durability_watcher():
    """Idempotent: called from main.py at boot. Spawns the watcher
    daemon thread that maintains the RSS flag."""
    global _watcher_started, _watcher_thread
    if _watcher_started:
        return
    _watcher_started = True
    _watcher_thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="brain-l20-durability")
    _watcher_thread.start()
    logger.info(f"[L20-durability] Watcher started. Threshold "
                 f"{DURABILITY_THRESHOLD_MB}MB. Max concurrent Claude "
                 f"calls: {DURABILITY_MAX_CONCURRENT}. Max per min: "
                 f"{DURABILITY_MAX_PER_MIN}.")


# ── HTTP endpoints ──────────────────────────────────────────────────

@brain_layer20_bp.route("/api/v1/brain/durability", methods=["GET"])
def durability_state():
    """Current guard state — what's gated, what's not, why."""
    now = time.monotonic()
    inflight = []
    for call_id, (started_at, layer) in DURABILITY_INFLIGHT_CLAUDE_CALLS.items():
        inflight.append({
            "call_id": call_id,
            "layer": layer,
            "age_seconds": round(now - started_at, 1),
        })
    recent = [t for t in DURABILITY_RECENT_STARTS if now - t < 60]
    return jsonify(
        ok=True,
        memory_high=DURABILITY_MEMORY_HIGH,
        rss_mb=DURABILITY_LAST_RSS_MB,
        threshold_mb=DURABILITY_THRESHOLD_MB,
        last_check_age_seconds=round(now - DURABILITY_LAST_CHECK_AT, 1)
                                 if DURABILITY_LAST_CHECK_AT else None,
        inflight_claude_calls=inflight,
        claude_calls_last_60s=len(recent),
        max_concurrent_claude=DURABILITY_MAX_CONCURRENT,
        max_claude_per_min=DURABILITY_MAX_PER_MIN,
        verdict=("blocking new calls (memory high)" if DURABILITY_MEMORY_HIGH
                 else ("throttling (rate limit)" if len(recent) >= DURABILITY_MAX_PER_MIN
                        else "allowing")),
        watcher_started=_watcher_started,
    )


@brain_layer20_bp.route("/api/v1/brain/durability/clear-flags",
                        methods=["POST"])
def clear_flags():
    """Admin: manually clear stuck in-flight registrations + recent
    starts list. Use when restarting a worker invalidates the in-memory
    state."""
    if _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    DURABILITY_INFLIGHT_CLAUDE_CALLS.clear()
    DURABILITY_RECENT_STARTS.clear()
    return jsonify(ok=True, cleared_at=_dt.datetime.utcnow().isoformat() + "Z")
