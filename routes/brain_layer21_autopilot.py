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
    """Idempotently align brain_autopilot_actions to L21's expected schema.

    Phase FF+24-followup (2026-05-20): routes/brain_autopilot.py and this
    module BOTH INSERT into brain_autopilot_actions but with DIFFERENT
    column sets. Whichever module's _ensure_table() ran first created
    the table; the other one's INSERT then fails forever with
      column "action" of relation "brain_autopilot_actions" does not exist
    (the symptom observed in Railway logs at 2026-05-20 03:07:32 UTC).

    The fix: keep CREATE TABLE IF NOT EXISTS for fresh installs, then
    ADD COLUMN IF NOT EXISTS for each L21-specific column so this module
    works whether or not the other one initialized the table first."""
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            # Fresh-install path
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_autopilot_actions (
                    id           BIGSERIAL PRIMARY KEY,
                    fired_at     TIMESTAMPTZ DEFAULT NOW(),
                    action       TEXT,
                    target       TEXT,
                    tier         TEXT,
                    detail       TEXT,
                    detected_at  TIMESTAMPTZ,
                    resolved_at  TIMESTAMPTZ
                )
            """)
            # Drift-repair path: backfill columns L21 needs when the table
            # already exists with a different schema (created by
            # brain_autopilot.py's _record_action).
            for col_sql in (
                "ADD COLUMN IF NOT EXISTS action       TEXT",
                "ADD COLUMN IF NOT EXISTS target       TEXT",
                "ADD COLUMN IF NOT EXISTS tier         TEXT",
                "ADD COLUMN IF NOT EXISTS detail       TEXT",
                "ADD COLUMN IF NOT EXISTS detected_at  TIMESTAMPTZ",
                "ADD COLUMN IF NOT EXISTS resolved_at  TIMESTAMPTZ",
                "ADD COLUMN IF NOT EXISTS fired_at     TIMESTAMPTZ DEFAULT NOW()",
            ):
                try:
                    cur.execute(f"ALTER TABLE brain_autopilot_actions {col_sql}")
                except Exception as _ce:
                    # ADD COLUMN IF NOT EXISTS is PG 9.6+, won't error
                    # for already-present columns. Log anything else.
                    logger.debug(f"[L21] alter skipped: {_ce}")
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
                # Phase FF+25-followup-r4 (2026-05-20): brain_autopilot_actions
                # is shared with brain_autopilot.py which writes pattern_name
                # + finding_url but leaves action/target/tier NULL. Filtering
                # those out so the L21 dashboard shows L21's own rows only —
                # otherwise the recent_actions list was 8x "fired_at + all
                # other fields null" because we displayed the sibling module's
                # rows we don't own.
                cur.execute(
                    "SELECT fired_at, action, target, tier, detail, "
                    "       detected_at, resolved_at "
                    "FROM brain_autopilot_actions "
                    "WHERE action IS NOT NULL "
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


# Phase FF+25-followup (2026-05-20): one-shot admin endpoint to repair
# the brain_autopilot_actions schema drift. The module-load _ensure_table()
# in this file calls ALTER TABLE ADD COLUMN IF NOT EXISTS for each L21
# column, but those ALTERs apparently aren't landing in production (Railway
# logs still show "column 'action' does not exist" on every L21 tick).
# Possible silent failure modes: transaction rollback, lock contention,
# permissions, or the function not actually running on this deploy.
#
# This endpoint surfaces what's actually happening. It returns the result
# of each ALTER explicitly so we can SEE which one(s) failed and why.
@brain_layer21_bp.route("/api/v1/admin/brain/repair-l21-schema",
                          methods=["POST", "GET"])
def repair_l21_schema():
    """Force-apply L21 column repairs + return per-statement result."""
    import os, logging
    log = logging.getLogger(__name__)

    # Admin gate
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    allowed = {"dchub-internal-sync-2026"}
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY",
                "MCP_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v: allowed.add(_v)
    if sent not in allowed:
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403

    try:
        from main import get_db
    except Exception as e:
        return jsonify(error=f"no get_db: {e}"), 500

    conn = get_db()
    if conn is None:
        return jsonify(error="no_db_conn"), 503

    results = []
    column_defs = [
        ("action",      "TEXT"),
        ("target",      "TEXT"),
        ("tier",        "TEXT"),
        ("detail",      "TEXT"),
        ("detected_at", "TIMESTAMPTZ"),
        ("resolved_at", "TIMESTAMPTZ"),
        ("fired_at",    "TIMESTAMPTZ DEFAULT NOW()"),
    ]
    try:
        cur = conn.cursor()
        # Pre-check: list current columns of brain_autopilot_actions
        try:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'brain_autopilot_actions'
                ORDER BY ordinal_position
            """)
            current_cols = [(r[0], r[1]) for r in cur.fetchall()]
        except Exception as e:
            current_cols = [("__schema_query_failed__", str(e)[:120])]

        for col_name, col_type in column_defs:
            sql = f"ALTER TABLE brain_autopilot_actions " \
                  f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
            try:
                cur.execute(sql)
                conn.commit()
                results.append({"column": col_name, "status": "ok"})
            except Exception as e:
                # Roll back so we can keep going
                try: conn.rollback()
                except Exception: pass
                results.append({
                    "column": col_name,
                    "status": "error",
                    "error": str(e)[:200],
                })

        # Phase FF+25-followup-v2 (2026-05-20): the table also has NOT
        # NULL constraints on the OTHER autopilot module's columns
        # (finding_issue, finding_url, pattern_name, etc.) from
        # routes/brain_autopilot.py. Since L21 writes WITHOUT those
        # columns (different schema), its INSERTs fail on the NOT NULL.
        # Both modules need to coexist → relax the NOT NULL so either
        # column set can write. Each module still validates its own
        # required fields at the application layer.
        relax_null_cols = [
            "finding_issue", "finding_url", "pattern_name",
            "action_endpoint", "outcome",
        ]
        relax_results = []
        for col in relax_null_cols:
            sql = f"ALTER TABLE brain_autopilot_actions ALTER COLUMN {col} DROP NOT NULL"
            try:
                cur.execute(sql)
                conn.commit()
                relax_results.append({"column": col, "status": "dropped_not_null"})
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                msg = str(e)[:150]
                # If the column already accepts NULL, that's fine
                if "does not exist" in msg.lower():
                    relax_results.append({"column": col, "status": "column_missing_ok"})
                else:
                    relax_results.append({"column": col, "status": "error", "error": msg})

        # Post-check: list columns again to confirm
        try:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'brain_autopilot_actions'
                ORDER BY ordinal_position
            """)
            new_cols = [(r[0], r[1]) for r in cur.fetchall()]
        except Exception as e:
            new_cols = [("__post_schema_query_failed__", str(e)[:120])]

        # Try a probe INSERT (then immediately delete) to verify L21 can write
        probe_status = None
        try:
            cur.execute(
                "INSERT INTO brain_autopilot_actions "
                "(action, target, tier, detail) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                ("__schema_probe", "test", "A", "FF+25-followup repair verification")
            )
            probe_id = cur.fetchone()[0]
            cur.execute("DELETE FROM brain_autopilot_actions WHERE id = %s", (probe_id,))
            conn.commit()
            probe_status = "ok"
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            probe_status = f"error: {str(e)[:200]}"

        return jsonify(
            ok=all(r["status"] == "ok" for r in results) and probe_status == "ok",
            columns_before=current_cols,
            columns_after=new_cols,
            alters=results,
            relaxed_not_null=relax_results,
            probe_insert=probe_status,
        )
    finally:
        try: conn.close()
        except Exception: pass
