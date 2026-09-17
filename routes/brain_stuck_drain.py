"""Brain stuck-queue drainer — forces autopilot to act on findings
the brain has noticed many times but never tried to fix.

Today the brain dashboard showed:
  • 20 stuck issues (up from 12 this morning) — each with seen ×18-37
  • All marked `untried` (no proposal ever generated)
  • Shadowed routes that the brain noticed 19 times across cron cycles
    but the proposal-to-PR pipeline never picked up

The brain is GREAT at observing (4.00/4 self-assessment); the
autopilot pipeline isn't catching up to the observation rate. This
module short-circuits that: hits the brain triage endpoint, picks
the top N stuck findings, and forces them into the proposal queue
so Opus has to write a fix on the next cron tick.

  GET /api/v1/brain/stuck-drain/preview
       — what stuck findings would be drained next (no DB writes)

  POST /api/v1/admin/brain/stuck-drain/run
       — actually drain N findings. Admin-key gated, fail-closed.

The drain runs:
  1. Fetch stuck findings from /api/v1/brain/findings/triage
  2. Pick top N by seen-count descending
  3. For each, write a row to brain_proposed_code_fixes with status=
     "forced-drain-pending" so the autoaction cron picks it up on
     the next run
  4. Returns the list of drained finding IDs

Configurable via DCHUB_BRAIN_DRAIN_BATCH_SIZE env (default 5 per run).

Companion .github/workflows/brain-stuck-drain.yml runs this daily at
08:00 UTC after the morning DCPI recompute.
"""
import os
import urllib.request
import urllib.error
import json as _json
from flask import Blueprint, jsonify, request


brain_stuck_drain_bp = Blueprint("brain_stuck_drain", __name__)


def _fetch_stuck_findings() -> list:
    """Query the brain triage endpoint. Returns [] on any error."""
    try:
        req = urllib.request.Request(
            "http://localhost:8080/api/v1/brain/findings/triage",
            headers={"X-Internal-Probe": "1"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = _json.loads(r.read().decode("utf-8", errors="ignore"))
            items = d.get("stuck") or d.get("untried") or d.get("items") or []
            return items if isinstance(items, list) else []
    except Exception:
        return []


def _select_top_N(findings: list, n: int) -> list:
    """Highest seen-count first; tie-break on most-recent timestamp."""
    return sorted(
        findings,
        key=lambda f: (-(f.get("seen") or f.get("count") or 0),
                       -(int(f.get("ts") or f.get("created_at") or 0)
                         if isinstance(f.get("ts") or f.get("created_at"),
                                       (int, float)) else 0)),
    )[:n]


def _force_to_proposal_queue(findings: list) -> tuple[int, list[str]]:
    """Insert each finding into brain_proposed_code_fixes as
    forced-drain-pending. Returns (count_inserted, errors).
    """
    n_ok = 0
    errs = []
    try:
        import psycopg2
        _du = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL", "")).strip()
        if not _du:
            return 0, ["no_database_url"]
        with psycopg2.connect(_du, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                for f in findings:
                    fid = f.get("id") or (f.get("kind", "?")
                                          + ":" + str(f.get("subject", "?")))
                    rationale = (
                        f"Forced drain from brain stuck-queue. Finding "
                        f"observed {f.get('seen', '?')} times across cron "
                        f"cycles but never tried. Surface to Opus on next "
                        f"layer5 tick."
                    )
                    try:
                        cur.execute("""
                            INSERT INTO brain_proposed_code_fixes
                              (kind, subject, url, rationale, status,
                               source, confidence, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                            ON CONFLICT DO NOTHING
                        """, (
                            "forced_drain",
                            f.get("subject", fid),
                            f.get("url"),
                            rationale,
                            "forced-drain-pending",
                            "brain_stuck_drain",
                            0.50,
                        ))
                        n_ok += 1
                    except Exception as e:
                        errs.append(f"{fid}: {str(e)[:80]}")
            conn.commit()
    except Exception as e:
        errs.append(f"connect: {str(e)[:80]}")
    return n_ok, errs


@brain_stuck_drain_bp.route("/api/v1/brain/stuck-drain/preview",
                             methods=["GET"])
def stuck_drain_preview():
    """Public: what stuck findings would be drained next? (read-only)"""
    findings = _fetch_stuck_findings()
    n = int(os.environ.get("DCHUB_BRAIN_DRAIN_BATCH_SIZE", "5"))
    top = _select_top_N(findings, n)
    return jsonify({
        "ok":              True,
        "total_stuck":     len(findings),
        "batch_size":      n,
        "would_drain":     [
            {"kind":    f.get("kind", "?"),
             "subject": f.get("subject", "?"),
             "seen":    f.get("seen", "?"),
             "url":     f.get("url")}
            for f in top
        ],
        "doc": ("POST /api/v1/admin/brain/stuck-drain/run (admin-key) "
                "to actually drain. Or wait for the daily 08:00 UTC cron."),
    })


@brain_stuck_drain_bp.route("/api/v1/admin/brain/stuck-drain/run",
                             methods=["POST"])
def stuck_drain_run():
    """Admin: actually drain N stuck findings into the proposal queue."""
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    if not admin_key:
        return jsonify({"error": "admin_endpoint_unconfigured"}), 503
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if provided != admin_key:
        return jsonify({"error": "unauthorized"}), 401

    findings = _fetch_stuck_findings()
    n = int(os.environ.get("DCHUB_BRAIN_DRAIN_BATCH_SIZE", "5"))
    top = _select_top_N(findings, n)
    inserted, errs = _force_to_proposal_queue(top)

    return jsonify({
        "ok":             True,
        "total_stuck":    len(findings),
        "selected":       len(top),
        "inserted":       inserted,
        "errors":         errs,
        "drained":        [
            {"kind":    f.get("kind"),
             "subject": f.get("subject"),
             "seen":    f.get("seen")}
            for f in top
        ],
    })
