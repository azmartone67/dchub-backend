"""brain_product_lead.py — what to do next, and what is merely parked (2026-09-08).

★★★ THE FINDING THIS EXISTS FOR: **generation is healthy everywhere and
consumption is dead everywhere.** Measured 2026-09-08:

  · `brain_enhancement_proposals` — ~20 new ideas a week for eight straight
    weeks. Status vocabulary all time: `proposed` (155), `duplicate` (43),
    `queued` (3, aged 30-49 days). **Not one has ever reached a shipped state.**
    A settle verb was added in #4235 and has never been called once.
  · `brain_lifecycle_proposals` — the SAME job, a different table, and it
    WORKED: 138 of 198 shipped, 47 in May, 90 in June. Then 1 in July, 0 in
    August, **nothing shipped in 38 days**, 60 still open, oldest 2026-05-25.
  · 136 open findings across six detectors have no route to a code fix at all;
    only `consistency_radar` reaches the proposer (48 of its 129).
  · The master tick's own `human_decisions` carry `seen_count` 1220 / 328 /
    1790 — surfaced thousands of times, consumed zero times.

So the gap is NOT detection, and NOT a missing verb. Every queue here already
has an exit. **Nothing decides which item to take.** That is the whole job of
this module, and the reason it is a RANKER and not another generator: adding a
thirteenth detector to a system where six existing ones already have no
consumer makes the problem worse, not better.

★ WHAT IT DELIBERATELY DOES NOT DO. It does not ship, settle, dismiss or open
anything. Fix SELECTION is where this codebase's automation has been most
wrong — on 2026-09-07, four of seven proposed fixes were wrong and two were
actively harmful. It ranks and it routes; a human presses the button, using the
verbs that already exist:

    POST /api/v1/admin/brain/proposals/<id>/settle         (#4235)
    POST /api/v1/brain/lifecycle/proposals/<id>/ship       (L23)
    POST /api/v1/brain/lifecycle/proposals/<id>/dismiss    (L23)

★ THE MOST USEFUL COLUMN IS `blocked_on`, not the score. The question worth
answering is not "what is the biggest number" but **"how much of this is
actually waiting on me?"** — separating work that is ready to act on from work
that is structurally stuck (wrong repo, no consumer) is the judgement a lead
supplies. A ranked list where every row is blocked is a to-do list nobody can
start.

★★★ FLOOR. If every queue comes back empty this reports `unmeasurable`, never
"all clear". Four independent queues reading zero means a broken query far more
often than a finished backlog — and a prioritiser that silently finds nothing
would recommend nothing, forever, exactly like the loops it exists to unstick.
cf feedback_scan_that_can_find_nothing_needs_a_floor.

Auth: X-Admin-Key. Read-only — this module issues no UPDATE.
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

brain_product_lead_bp = Blueprint("brain_product_lead", __name__)

# Below this many rows across ALL queues, assume the reader is broken rather
# than the backlog finished.
_MIN_TOTAL_ROWS = 5

# Recurrence beyond this adds nothing: an item seen 1,790 times is not 1,790x
# more important than one seen 100 times, it is the same signal repeating.
_RECURRENCE_CAP = 100

# Age past this is capped for the same reason — a 400-day item is not 4x a
# 100-day one, both are simply stale.
_AGE_CAP_DAYS = 120


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(expected) and provided == expected


def _conn():
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    import psycopg2
    return psycopg2.connect(url, connect_timeout=8)


def score(age_days: float, recurrence: int, ready: bool) -> float:
    """Rank = staleness x recurrence, with a bonus for work nothing blocks.

    Both inputs are CAPPED. Without a cap `seen_count` alone decides the order,
    and the three loudest items (1220/328/1790) would occupy the top of every
    list forever — which is precisely the state that made the existing signal
    useless. The `ready` bonus is what stops a ranked list from being entirely
    things no one can start today.
    """
    a = min(max(age_days, 0.0), _AGE_CAP_DAYS) / _AGE_CAP_DAYS
    r = min(max(recurrence, 1), _RECURRENCE_CAP) / _RECURRENCE_CAP
    return round((a + r) * (1.5 if ready else 1.0), 4)


def verdict_for(n_items: int, failed: dict | None = None,
                by_blocked: dict | None = None) -> dict:
    """The endpoint's verdict rule, as a function the tests can call directly.

    ★ Extracted on purpose. Re-implementing this branch inside the test file
    would produce a MIRROR: the copy stays green while the route regresses,
    which is the one bug class this whole module is a response to. The route
    below calls this; so do the tests. There is exactly one copy.
    """
    failed = failed or {}
    by_blocked = by_blocked or {}
    if failed:
        return {"verdict": "unmeasurable", "ok": False,
                "why": (f"{len(failed)} queue(s) failed to read: "
                        f"{', '.join(failed)} — a queue that ERRORED is not a "
                        f"queue that is empty")}
    if n_items < _MIN_TOTAL_ROWS:
        return {"verdict": "unmeasurable", "ok": False,
                "why": (f"only {n_items} rows across four queues (floor "
                        f"{_MIN_TOTAL_ROWS}) — a broken reader is likelier than "
                        f"a cleared backlog, so this will not report all-clear")}
    ready = by_blocked.get("ready_to_act", 0)
    return {"verdict": "ok", "ok": True,
            "why": (f"{n_items} parked items; {ready} need no decision, "
                    f"{n_items - ready} are blocked on a person or a missing arm")}


def _rows_enhancement(cur) -> list[dict]:
    """The ring: has a settle verb since #4235, never once used."""
    cur.execute("""
        SELECT id, left(coalesce(title,''), 120), status,
               EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0
          FROM brain_enhancement_proposals
         WHERE status IN ('proposed', 'queued')
         ORDER BY created_at ASC
    """)
    out = []
    for pid, title, status, age in cur.fetchall():
        out.append({
            "queue": "brain_enhancement_proposals",
            "id": pid,
            "what": title or f"(untitled proposal {pid})",
            "state": status,
            "age_days": round(float(age or 0), 1),
            "recurrence": 1,
            "blocked_on": "needs_human_decision",
            "exit": f"POST /api/v1/admin/brain/proposals/{pid}/settle",
            "note": ("this queue's settle verb has existed since #4235 and has "
                     "never been called; nothing consumes 'queued'"),
        })
    return out


def _rows_lifecycle(cur) -> list[dict]:
    """The queue that WORKED — 138 shipped — and then stopped."""
    cur.execute("""
        SELECT id, left(coalesce(proposal_text,''), 120), proposal_kind,
               EXTRACT(EPOCH FROM (NOW() - proposed_at)) / 86400.0, approved
          FROM brain_lifecycle_proposals
         WHERE shipped_at IS NULL AND dismissed_at IS NULL
         ORDER BY proposed_at ASC
    """)
    out = []
    for pid, text, kind, age, approved in cur.fetchall():
        out.append({
            "queue": "brain_lifecycle_proposals",
            "id": pid,
            "what": (text or "").replace("\n", " ")[:120] or f"(proposal {pid})",
            "state": ("approved" if approved else "unreviewed"),
            "kind": kind,
            "age_days": round(float(age or 0), 1),
            "recurrence": 1,
            # An approved proposal is one decision further along: the human has
            # already said yes and only the ship stamp is outstanding.
            "blocked_on": "ready_to_act" if approved else "needs_human_decision",
            "exit": f"POST /api/v1/brain/lifecycle/proposals/{pid}/ship",
        })
    return out


def _rows_orphan_findings(cur) -> list[dict]:
    """Open findings with no code-fix proposal, grouped so 44 instances of one
    pattern read as ONE decision rather than 44 rows of noise."""
    cur.execute("""
        SELECT f.detector, f.issue, count(*) AS n,
               max(coalesce(f.seen_count, 1)) AS seen,
               EXTRACT(EPOCH FROM (NOW() - min(f.first_seen))) / 86400.0 AS age,
               bool_or(f.url LIKE '%%dchub-frontend%%') AS frontend
          FROM brain_findings f
         WHERE f.status = 'open'
           AND NOT EXISTS (SELECT 1 FROM brain_proposed_code_fixes p
                            WHERE p.issue_key = f.issue)
         GROUP BY f.detector, f.issue
         ORDER BY count(*) DESC
    """)
    out = []
    for detector, issue, n, seen, age, frontend in cur.fetchall():
        # A backend proposer cannot patch a frontend file. Saying so is the
        # routing fact; pretending it is actionable manufactures dead PRs.
        blocked = "needs_frontend_arm" if frontend else "no_consumer"
        out.append({
            "queue": "brain_findings",
            "id": issue,
            "what": f"{issue} ({n} instance{'s' if n != 1 else ''})",
            "state": f"open, detector={detector}",
            "instances": n,
            "age_days": round(float(age or 0), 1),
            "recurrence": int(seen or 1),
            "blocked_on": blocked,
            "exit": ("route to dchub-frontend — the code-fix proposer patches "
                     "dchub-backend only" if frontend
                     else "no consumer reads this detector's findings"),
        })
    return out


def _rows_ready_fixes(cur) -> list[dict]:
    """Proposals with a patch and no PR yet — the only rows here that need no
    decision at all, just a drafter run."""
    cur.execute("""
        SELECT id, left(coalesce(rationale, ''), 120), confidence,
               EXTRACT(EPOCH FROM (NOW() - proposed_at)) / 86400.0,
               coalesce(approval_count, 1)
          FROM brain_proposed_code_fixes
         WHERE status = 'proposed' AND pr_url IS NULL
         ORDER BY proposed_at ASC
    """)
    out = []
    for pid, rationale, conf, age, approvals in cur.fetchall():
        out.append({
            "queue": "brain_proposed_code_fixes",
            "id": pid,
            "what": (rationale or "").replace("\n", " ")[:120] or f"(fix {pid})",
            "state": f"proposed, confidence={conf}",
            "age_days": round(float(age or 0), 1),
            "recurrence": int(approvals or 1),
            "blocked_on": "ready_to_act",
            "exit": "POST /api/v1/admin/brain/draft-prs/run",
        })
    return out


@brain_product_lead_bp.route("/api/v1/admin/brain/product-lead", methods=["GET"])
def product_lead():
    try:
        limit = max(1, min(int(request.args.get("limit", 25)), 200))
    except Exception:
        limit = 25

    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401

    c = None
    try:
        c = _conn()
        if c is None:
            return jsonify(ok=False, verdict="unmeasurable",
                           error="no DATABASE_URL"), 200
        items, per_queue, failed = [], {}, {}
        with c.cursor() as cur:
            for name, fn in (("brain_enhancement_proposals", _rows_enhancement),
                             ("brain_lifecycle_proposals", _rows_lifecycle),
                             ("brain_findings", _rows_orphan_findings),
                             ("brain_proposed_code_fixes", _rows_ready_fixes)):
                try:
                    rows = fn(cur)
                    per_queue[name] = len(rows)
                    items.extend(rows)
                except Exception as e:
                    # A queue that ERRORED is not a queue that is empty. Record
                    # it so a broken reader cannot read as a cleared backlog.
                    failed[name] = f"{type(e).__name__}: {str(e)[:120]}"
                    try:
                        cur.execute("ROLLBACK")
                    except Exception:
                        pass
    except Exception as e:
        return jsonify(ok=False, verdict="unmeasurable",
                       error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    for it in items:
        it["score"] = score(it["age_days"], it["recurrence"],
                            it["blocked_on"] == "ready_to_act")
    items.sort(key=lambda x: x["score"], reverse=True)

    by_blocked: dict = {}
    for it in items:
        by_blocked[it["blocked_on"]] = by_blocked.get(it["blocked_on"], 0) + 1

    v = verdict_for(len(items), failed, by_blocked)
    verdict, ok, why = v["verdict"], v["ok"], v["why"]

    return jsonify(
        ok=ok,
        verdict=verdict,
        why=why,
        total_parked=len(items),
        by_blocked_on=by_blocked,
        per_queue=per_queue,
        queues_failed=failed,
        # TWO answers, because they are different questions. `next_move` is
        # the biggest thing; `next_actionable` is the biggest thing anyone can
        # start today. The top of this list is dominated by items with no
        # consumer — real, and NOT a next move for a person this morning.
        next_move=items[0] if items and ok else None,
        next_actionable=next((i for i in items
                              if i["blocked_on"] == "ready_to_act"), None) if ok else None,
        worklist=items[:limit],
        note=("Ranked, not actuated. This module never ships, settles or opens "
              "anything — every row names the verb that already exists, and a "
              "person presses it. Fix SELECTION is where this system has been "
              "most wrong (2026-09-07: four of seven proposed fixes wrong, two "
              "harmful)."),
    ), 200
