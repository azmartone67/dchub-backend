"""Brain Action Queue — one ranked surface for the human-consumption bottleneck.

WHY THIS EXISTS (2026-06-16): the brain finds plenty (consistency radar,
fast-QA, security detectors, L15) but the throughput limit on "managing
the site" is the operator's attention — findings and brain-drafted PRs are
scattered across endpoints, so clearing them is slow. This collapses them
into ONE ranked list: the single thing to look at to know "what should I
act on next."

Ranking (higher = act sooner):
  · severity parsed from the finding's detail tag ([CRIT] > [HIGH] >
    [MED]/[MEDIUM] > [LOW]/none)
  · seen_count (a finding seen many times is chronic, rank up)
  · age (older open finding ranks up — it's been ignored)
A finding that already has an open brain-drafted PR is marked
`has_open_pr` so the operator knows it just needs a merge, not triage.

Read-only. No DB writes. Public (it leaks no secrets — only the brain's
own findings, which are already public via the consistency radar).

Endpoint:
  GET /api/v1/brain/action-queue?limit=40
"""
import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_action_queue_bp = Blueprint("brain_action_queue", __name__)

_SEV_RANK = {"CRIT": 4, "CRITICAL": 4, "HIGH": 3, "MED": 2, "MEDIUM": 2,
             "LOW": 1}
_SEV_RE = re.compile(r"\[([A-Z]+)\]")


def _severity(detail: str) -> tuple[str, int]:
    m = _SEV_RE.search(detail or "")
    if m:
        tag = m.group(1).upper()
        if tag in _SEV_RANK:
            return tag, _SEV_RANK[tag]
    return "NONE", 0


def _open_pr_trigger_sources() -> list:
    """Best-effort: the trigger_source descriptions of recent brain-drafted
    PRs, so the queue can mark a finding 'just needs a merge'. Reads
    brain_auto_code_actions (the L22 draft log) — never calls GitHub (kept
    cheap/sync-safe). Returns lowercased trigger_source strings."""
    sources: list = []
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return sources
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT trigger_source FROM brain_auto_code_actions "
                "WHERE pr_number IS NOT NULL "
                "AND drafted_at > NOW() - INTERVAL '30 days'")
            for r in cur.fetchall():
                if r[0]:
                    sources.append(str(r[0]).lower())
        except Exception:
            # column/table may differ across schema versions — fail soft
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("action_queue: open-pr lookup failed: %s", e)
    return sources


@brain_action_queue_bp.route("/api/v1/brain/action-queue", methods=["GET"])
def action_queue():
    """One ranked list of everything open the operator could act on."""
    try:
        limit = max(1, min(int(request.args.get("limit", 40)), 200))
    except Exception:
        limit = 40

    rows: list[dict] = []
    counts = {"open_total": 0, "crit": 0, "high": 0, "with_open_pr": 0}
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return jsonify({"ok": False, "error": "no db"}), 500
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT issue, url, detail, detector, seen_count, "
                "       first_seen, last_seen "
                "FROM brain_findings "
                "WHERE status = 'open' "
                "ORDER BY last_seen DESC NULLS LAST LIMIT 1000")
            raw = cur.fetchall()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.exception("action_queue: query failed")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    pr_sources = _open_pr_trigger_sources()
    import datetime
    now = datetime.datetime.utcnow()
    for r in raw:
        issue, url, detail, detector, seen_count, first_seen, last_seen = (
            r[0], r[1], r[2], r[3], r[4] or 1, r[5], r[6])
        sev_tag, sev_rank = _severity(detail or "")
        age_days = 0.0
        try:
            if first_seen:
                fs = first_seen if isinstance(first_seen, datetime.datetime) \
                    else datetime.datetime.fromisoformat(str(first_seen)[:19])
                age_days = max(0.0, (now - fs.replace(tzinfo=None)).days)
        except Exception:
            age_days = 0.0
        il = (issue or "").lower()
        has_pr = bool(il) and any(il in s or s in il for s in pr_sources)
        # score: severity dominates, then chronic-ness, then age
        score = (sev_rank * 1000) + min(int(seen_count), 99) * 10 + \
            min(int(age_days), 30)
        counts["open_total"] += 1
        if sev_rank == 4:
            counts["crit"] += 1
        elif sev_rank == 3:
            counts["high"] += 1
        if has_pr:
            counts["with_open_pr"] += 1
        rows.append({
            "issue": issue,
            "url": url,
            "detail": detail,
            "detector": detector,
            "severity": sev_tag,
            "seen_count": int(seen_count),
            "age_days": int(age_days),
            "has_open_pr": has_pr,
            "next_action": ("merge the open PR" if has_pr else "triage"),
            "score": score,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({
        "ok": True,
        "counts": counts,
        "queue": rows[:limit],
        "note": ("Ranked by severity → chronic-ness (seen_count) → age. "
                 "has_open_pr=true means a brain-drafted fix is waiting on a "
                 "merge, not triage."),
    }), 200, {"Cache-Control": "no-store, max-age=0",
              "X-DC-Hub-Surface": "brain-action-queue"}
