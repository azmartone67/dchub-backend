"""brain_bug_squash — preemptive bug-class detection wired into the brain.

Companion to scripts/bug_squash.py. This module exposes the bug-squash
scanner via HTTP + persists findings into the canonical brain_findings
table, so the same surfaces that consume other brain detectors (the
inspector brief, weekly digest, autopilot) automatically see them too.

Endpoints:
    POST /api/v1/brain/bug-squash/run       — trigger a fresh scan; persist
                                                findings; return summary
    POST /api/v1/brain/bug-squash/file      — receive findings posted by
                                                the CLI script and persist
                                                (admin-gated)
    GET  /api/v1/brain/bug-squash/findings  — list open findings (by
                                                pattern, by severity)
    POST /api/v1/brain/bug-squash/resolve   — mark a finding resolved
                                                (admin-gated)

Safety:
    - Scan endpoint is read-only — it walks the filesystem and inserts
      INTO brain_findings (UPSERT-style). It never executes shell or
      modifies source.
    - Per safety boundaries in reference_dchub_autonomy_core.md: this
      layer surfaces findings only; remediation is left to humans or
      to layers that have explicit approval to act.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("brain_bug_squash")
brain_bug_squash_bp = Blueprint("brain_bug_squash", __name__)


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(expected) and provided == expected


# ──────────────────────────────────────────────────────────────────────
# Findings persistence (UPSERT-style: bumps last_seen on re-detection)
# ──────────────────────────────────────────────────────────────────────
_BUG_SQUASH_FINDINGS_DDL = """
-- Extends the canonical brain_findings table — no new schema needed,
-- we just use issue='bug_squash:<pattern_id>' and detector='brain_bug_squash'.
-- The UPSERT below is idempotent on (issue, url).
SELECT 1;
"""


def _persist_findings(findings: list[dict]) -> dict:
    """Insert findings into brain_findings. Returns stats."""
    if not findings:
        return {"inserted": 0, "updated": 0}
    dsn = _dsn()
    if not dsn:
        return {"error": "DATABASE_URL not set"}
    inserted = updated = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=6) as c:
            with c.cursor() as cur:
                # 2026-06-06: the ON CONFLICT DO UPDATE referenced
                # seen_count (phantom column on the live table) + needs
                # a UNIQUE(issue,url) constraint that may not exist, so
                # every conflicting write failed silently. Delegate to
                # the canonical brain_findings_writer (introspects schema,
                # restores seen_count, constraint-agnostic upsert).
                from routes.brain_findings_writer import upsert_brain_finding
                for f in findings:
                    try:
                        res = upsert_brain_finding(
                            cur,
                            issue=f.get("issue", "bug_squash:unknown"),
                            url=f.get("url", ""),
                            count=1,
                            detail=f.get("detail", ""),
                            detector=f.get("detector", "brain_bug_squash"),
                            status="open")
                        if res == "inserted":
                            inserted += 1
                        elif res == "updated":
                            updated += 1
                    except Exception as e:
                        log.warning("bug_squash insert failed for %s: %s",
                                    f.get("url", "?"), e)
            c.commit()
    except Exception as e:
        return {"error": str(e)[:200]}
    return {"inserted": inserted, "updated": updated}


# ──────────────────────────────────────────────────────────────────────
# Scan trigger
# ──────────────────────────────────────────────────────────────────────
@brain_bug_squash_bp.route("/api/v1/brain/bug-squash/run", methods=["POST"])
def run_scan():
    """Run a fresh bug-squash sweep, persist findings to brain_findings,
    return a summary.

    Body params (all optional):
        pattern   str  — limit to a single pattern id
        dry_run   bool — return findings but don't persist
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401

    body = request.get_json(silent=True) or {}
    pattern_filter = body.get("pattern")
    dry_run = bool(body.get("dry_run"))

    # Lazy-import scripts/bug_squash so the brain still boots even if the
    # script is moved or broken (defense in depth).
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from scripts.bug_squash import BugSquash
    except Exception as e:
        return jsonify(ok=False, error=f"bug_squash import failed: {e}"), 500

    try:
        sq = BugSquash()
        findings = sq.scan(pattern_filter=pattern_filter)
    except Exception as e:
        log.error("bug_squash scan crashed: %s", e)
        return jsonify(ok=False, error=f"scan crashed: {str(e)[:200]}"), 500

    summary = {
        "total": len(findings),
        "by_severity": {},
        "by_pattern": {},
    }
    for f in findings:
        summary["by_severity"][f.severity] = summary["by_severity"].get(f.severity, 0) + 1
        summary["by_pattern"][f.pattern_id] = summary["by_pattern"].get(f.pattern_id, 0) + 1

    if dry_run:
        return jsonify(
            ok=True, dry_run=True, summary=summary,
            findings_preview=[
                {"pattern": f.pattern_id, "file": f.file, "line": f.line,
                 "severity": f.severity}
                for f in findings[:20]
            ],
        )

    persist = _persist_findings([f.to_brain_finding() for f in findings])
    return jsonify(ok=True, summary=summary, persist=persist)


# ──────────────────────────────────────────────────────────────────────
# File-from-CLI receiver (used by scripts/bug_squash.py --file-to-brain)
# ──────────────────────────────────────────────────────────────────────
@brain_bug_squash_bp.route("/api/v1/brain/bug-squash/file", methods=["POST"])
def file_findings():
    """Receive a payload of brain_findings-shaped dicts (from the CLI
    script or a GH Actions cron) and persist them."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    body = request.get_json(silent=True) or {}
    findings = body.get("findings") or []
    if not isinstance(findings, list):
        return jsonify(ok=False, error="findings must be a list"), 400
    persist = _persist_findings(findings)
    return jsonify(ok=True, persist=persist, received=len(findings))


# ──────────────────────────────────────────────────────────────────────
# Read endpoints
# ──────────────────────────────────────────────────────────────────────
@brain_bug_squash_bp.route("/api/v1/brain/bug-squash/findings", methods=["GET"])
def list_findings():
    """List open bug-squash findings. Query params:
        pattern    — filter to issue='bug_squash:<pattern>'
        status     — open|resolved (default open)
        limit      — default 50, max 500
    """
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="DATABASE_URL not set"), 503
    pattern = (request.args.get("pattern") or "").strip()
    status = (request.args.get("status") or "open").strip()
    try:
        limit = min(int(request.args.get("limit") or 50), 500)
    except (TypeError, ValueError):
        limit = 50

    where = ["detector = %s"]
    params: list = ["brain_bug_squash"]
    if status:
        where.append("status = %s")
        params.append(status)
    if pattern:
        where.append("issue = %s")
        params.append(f"bug_squash:{pattern}")
    else:
        where.append("issue LIKE 'bug_squash:%%'")

    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=6) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, issue, url, detail, first_seen, last_seen, "
                    "seen_count, status "
                    "FROM brain_findings "
                    "WHERE " + " AND ".join(where) + " "
                    "ORDER BY last_seen DESC LIMIT %s",
                    params + [limit],
                )
                rows = cur.fetchall()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    return jsonify(
        ok=True,
        count=len(rows),
        findings=[
            {
                "id": r[0],
                "pattern_id": (r[1] or "").replace("bug_squash:", ""),
                "url": r[2],
                "detail": r[3],
                "first_seen": r[4].isoformat() if r[4] else None,
                "last_seen": r[5].isoformat() if r[5] else None,
                "seen_count": r[6],
                "status": r[7],
            }
            for r in rows
        ],
    )


@brain_bug_squash_bp.route("/api/v1/brain/bug-squash/resolve", methods=["POST"])
def resolve_finding():
    """Mark a finding as resolved. Body: { id: int } or
    { pattern: str, url: str }."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    body = request.get_json(silent=True) or {}
    where_sql = ""
    params: list = []
    if body.get("id"):
        where_sql = "id = %s"
        params = [int(body["id"])]
    elif body.get("pattern") and body.get("url"):
        where_sql = "issue = %s AND url = %s"
        params = [f"bug_squash:{body['pattern']}", body["url"]]
    else:
        return jsonify(ok=False, error="provide id or (pattern, url)"), 400

    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="DATABASE_URL not set"), 503
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=6) as c:
            with c.cursor() as cur:
                cur.execute(
                    f"UPDATE brain_findings SET status = 'resolved', "
                    f"last_seen = NOW() WHERE {where_sql}",
                    params,
                )
                touched = cur.rowcount
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, resolved=touched)


def register_brain_bug_squash(app):
    """Idempotent registration helper for main.py."""
    try:
        app.register_blueprint(brain_bug_squash_bp)
    except Exception as e:
        log.warning("brain_bug_squash already registered or failed: %s", e)
