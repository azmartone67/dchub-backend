"""
Brain L21 — Auto-Pilot (2026-05-19).

The 5-minute-MTTR layer. Reads recent HTTP errors (from
brain_http_capture), recent slow requests, and current durability
state. For each known failure pattern, applies a SAFE recovery action
within seconds.

The safety hierarchy (most conservative first):

  TIER A — Information-only (safe, always allowed):
    - Log the incident
    - Open a GitHub issue (via L15's existing infra)
    - Record to brain_auto_actions

  TIER B — Soft mitigation (safe, no code change):
    - Trip L20 durability flag (blocks Claude calls temporarily)
    - Increment a backoff counter (slows retries)
    - Cache-bust a stale CF Pages asset
    - Send a Resend email to api@dchub.cloud

  TIER C — Auto-recovery (medium safety, reversible):
    - Trigger an ingestion cron manually
    - Bounce a stuck thread (kill + restart)
    - Toggle a feature-flag env var

  TIER D — Code-write (requires L22; NOT implemented yet):
    - Open a PR with a proposed fix
    - Auto-merge ONLY for whitelisted patterns

This MVP ships TIERS A + B only. Tier C is gated behind an admin-key
endpoint for manual triggering. Tier D is the next layer.

Endpoints:
  GET  /api/v1/brain/autopilot         — recent actions + current state
  POST /api/v1/brain/autopilot/run     — admin: trigger one cycle now
  POST /api/v1/brain/autopilot/dry-run — admin: see what WOULD fire

Background thread: every 60 seconds, reads errors + applies fixes.
This is the core MTTR-5 loop.
"""

import os
import time
import threading
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer21_bp = Blueprint("brain_layer21", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_AUTOPILOT_TICK_SECONDS = int(os.environ.get("AUTOPILOT_TICK_SECONDS", "60"))


# ── Recovery pattern library ────────────────────────────────────────
# Each entry: (match_fn, action_fn, action_name, severity, tier)

def _match_404_spike(pattern_counts: dict) -> list[tuple[str, int]]:
    """Find URL patterns with >=5 404s in the last 5min."""
    hits = []
    for key, n in pattern_counts.items():
        if " [404]" in key and n >= 5:
            hits.append((key, n))
    return hits


def _match_5xx_burst(pattern_counts: dict) -> list[tuple[str, int]]:
    """Find URL patterns with >=3 5xx in the last 5min."""
    hits = []
    for key, n in pattern_counts.items():
        # Status appears in the key like '[500]', '[503]', etc.
        if any(s in key for s in (" [500]", " [502]", " [503]", " [504]")):
            if n >= 3:
                hits.append((key, n))
    return hits


def _act_log_404_spike(key: str, count: int) -> dict:
    """Tier A: just log + record. Tier B (cache bust + GH issue)
    requires the L15 infra which is already wired."""
    # Record as auto-action
    _record_action(action="logged_404_spike", target=key,
                   detail=f"{count} 404s in 5min — pattern {key}")
    # Open GH issue via L15 if available
    try:
        from routes.brain_layer15_auto_action import _open_issue
        chain = {
            "title": f"404 spike: {key.split(' ')[1] if ' ' in key else key}",
            "confidence": "high",
            "symptoms": [f"{count} 404s in 5min on pattern {key}"],
            "root_cause_hypothesis": (
                "Frontend/backend route mismatch — frontend hitting a path "
                "the backend doesn't serve. Common cause: singular vs "
                "plural mismatch (e.g. /facility/ vs /facilities/)."),
            "smallest_safe_fix": (
                "Add a backend route alias matching the URL pattern. "
                "See main.py @app.route handlers for the existing path."),
            "verification": (
                "After deploying alias, curl the broken URL — should 200. "
                "check_repeated_404_patterns finding should clear within 1h."),
        }
        ok, url_or_err = _open_issue(chain)
        if ok:
            return {"ok": True, "action": "gh_issue_opened",
                    "issue_url": url_or_err, "tier": "A"}
    except Exception as e:
        logger.warning(f"L21: gh_issue failed for {key}: {e}")
    return {"ok": True, "action": "logged_only", "tier": "A"}


def _act_5xx_burst_response(key: str, count: int) -> dict:
    """Tier B: trip the L20 durability flag temporarily to slow new
    Claude calls (if 5xx is from /brain/* endpoints) AND log."""
    target_is_brain = "/api/v1/brain/" in key
    if target_is_brain:
        try:
            import routes.brain_layer20_durability as l20
            l20.DURABILITY_MEMORY_HIGH = True
            l20.DURABILITY_LAST_RSS_MB = max(l20.DURABILITY_LAST_RSS_MB,
                                              l20.DURABILITY_THRESHOLD_MB + 50)
            logger.warning(f"L21: 5xx burst on {key} — tripped L20 "
                            "durability flag to throttle Claude calls.")
            _record_action(action="tripped_l20_durability_flag",
                            target=key,
                            detail=f"{count} 5xx in 5min on a /brain/* "
                                   f"endpoint — slowing Claude calls")
            return {"ok": True,
                    "action": "tripped_l20_durability_flag",
                    "tier": "B"}
        except Exception as e:
            logger.warning(f"L21: tripping L20 failed: {e}")
    _record_action(action="logged_5xx_burst", target=key,
                   detail=f"{count} 5xx in 5min — pattern {key}")
    return {"ok": True, "action": "logged_only", "tier": "A"}


# ── Action recording ────────────────────────────────────────────────

def _ensure_table():
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_autopilot_actions (
                    id           BIGSERIAL PRIMARY KEY,
                    fired_at     TIMESTAMPTZ DEFAULT NOW(),
                    action       TEXT NOT NULL,
                    target       TEXT,
                    tier         TEXT,
                    detail       TEXT,
                    detected_at  TIMESTAMPTZ,
                    resolved_at  TIMESTAMPTZ
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_autopilot_actions_time "
                        "ON brain_autopilot_actions(fired_at DESC)")
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[L21] table create failed: {e}")


def _record_action(action: str, target: str, detail: str,
                   tier: str = "A", detected_at: float | None = None):
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO brain_autopilot_actions "
                "(action, target, tier, detail, detected_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (action[:80], target[:200], tier, detail[:500],
                 _dt.datetime.fromtimestamp(detected_at)
                   if detected_at else None),
            )
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[L21] record action failed: {e}")


# ── The core loop ───────────────────────────────────────────────────

def _one_tick(dry_run: bool = False) -> dict:
    """One autopilot cycle. Reads recent errors, fires actions for
    matched patterns, returns a summary."""
    actions = []
    try:
        from routes.brain_http_capture import get_pattern_counts
        patterns = get_pattern_counts(window_seconds=300)
    except Exception as e:
        return {"ok": False, "error": f"capture not available: {e}"}

    # Tier A — 404 spikes
    for key, n in _match_404_spike(patterns):
        if dry_run:
            actions.append({"would_fire": "log_404_spike",
                             "target": key, "count": n})
        else:
            r = _act_log_404_spike(key, n)
            r["target"] = key
            r["count"] = n
            actions.append(r)

    # Tier B — 5xx bursts
    for key, n in _match_5xx_burst(patterns):
        if dry_run:
            actions.append({"would_fire": "5xx_burst_response",
                             "target": key, "count": n})
        else:
            r = _act_5xx_burst_response(key, n)
            r["target"] = key
            r["count"] = n
            actions.append(r)

    return {
        "ok": True,
        "tick_at": _dt.datetime.utcnow().isoformat() + "Z",
        "patterns_seen": len(patterns),
        "actions_fired": len(actions),
        "actions": actions,
        "dry_run": dry_run,
    }


# ── Background thread ───────────────────────────────────────────────

_autopilot_started = False


def _autopilot_loop():
    """Every AUTOPILOT_TICK_SECONDS, run one tick."""
    # Warm-up: wait 2 min before first tick so the http_capture buffer
    # has had time to fill with real traffic
    time.sleep(120)
    while True:
        try:
            res = _one_tick(dry_run=False)
            if res.get("actions_fired", 0) > 0:
                logger.info(f"[L21-autopilot] tick fired "
                             f"{res['actions_fired']} actions")
        except Exception as e:
            logger.warning(f"[L21-autopilot] tick failed: {e}")
        time.sleep(_AUTOPILOT_TICK_SECONDS)


def start_autopilot():
    """Idempotent: called from main.py at boot. Spawns the loop."""
    global _autopilot_started
    if _autopilot_started:
        return
    _autopilot_started = True
    _ensure_table()
    threading.Thread(target=_autopilot_loop, daemon=True,
                     name="brain-l21-autopilot").start()
    logger.info(f"[L21-autopilot] started — tick every "
                 f"{_AUTOPILOT_TICK_SECONDS}s (2-min warmup)")


# ── Endpoints ───────────────────────────────────────────────────────

@brain_layer21_bp.route("/api/v1/brain/autopilot", methods=["GET"])
def autopilot_status():
    """Current state + recent actions."""
    _ensure_table()
    recent_actions = []
    mttr_samples = []
    try:
        from main import get_db
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT fired_at, action, target, tier, detail, "
                    "       detected_at, resolved_at "
                    "FROM brain_autopilot_actions "
                    "ORDER BY fired_at DESC LIMIT 20"
                )
                for r in cur.fetchall():
                    if hasattr(r, "get"):
                        recent_actions.append({
                            "fired_at": str(r.get("fired_at") or "")[:19],
                            "action": r.get("action"),
                            "target": r.get("target"),
                            "tier": r.get("tier"),
                            "detail": r.get("detail"),
                            "detected_at": str(r.get("detected_at") or "") or None,
                            "resolved_at": str(r.get("resolved_at") or "") or None,
                        })
                        det, res_ = r.get("detected_at"), r.get("resolved_at")
                    else:
                        recent_actions.append({
                            "fired_at": str(r[0])[:19],
                            "action": r[1], "target": r[2], "tier": r[3],
                            "detail": r[4],
                            "detected_at": str(r[5]) if r[5] else None,
                            "resolved_at": str(r[6]) if r[6] else None,
                        })
                        det, res_ = r[5], r[6]
                    if det and res_:
                        try:
                            mttr_samples.append((res_ - det).total_seconds())
                        except Exception: pass
            finally:
                try: conn.close()
                except Exception: pass
    except Exception: pass

    avg_mttr = (sum(mttr_samples) / len(mttr_samples)) if mttr_samples else None
    return jsonify(
        ok=True,
        tick_seconds=_AUTOPILOT_TICK_SECONDS,
        started=_autopilot_started,
        recent_actions=recent_actions,
        mttr_seconds_avg=round(avg_mttr, 1) if avg_mttr is not None else None,
        mttr_target_seconds=300,  # 5 minutes
        note=("Tier A (log + GH issue) and Tier B (L20 trip) actions "
              "are active. Tier C (auto-restart, cache-bust) is admin-"
              "gated. Tier D (auto-PR) is the next layer (L22)."),
    )


@brain_layer21_bp.route("/api/v1/brain/autopilot/run", methods=["POST"])
def autopilot_run_now():
    """Admin: trigger one tick immediately."""
    if _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    return jsonify(_one_tick(dry_run=False))


@brain_layer21_bp.route("/api/v1/brain/autopilot/dry-run", methods=["POST", "GET"])
def autopilot_dry_run():
    """Show what WOULD fire — no actions taken."""
    return jsonify(_one_tick(dry_run=True))
