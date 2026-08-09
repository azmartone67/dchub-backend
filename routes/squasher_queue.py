"""routes/squasher_queue.py — the operator's manual "fix this one" lever.

WHY
===
The squasher portal reported the loop's true state and gave the operator no
way to act on it: 25 findings visible, propose emitting 0, and nothing to
press. The propose stage's own histogram says 15 of those are code-fixable and
sitting in `deferred_rate_cap` — real work, throttled behind a rotation the
operator cannot influence. This is the lever: pick one, put it at the head of
the queue.

THE SHAPE, AND WHY IT IS NOT ONE SYNCHRONOUS POST
=================================================
★ A browser POST to an admin route dies at the CF edge after 15s
  (ROUTE_TIMEOUTS DEFAULT; there is no /api/v1/admin/ prefix rule). The
  investigate chain alone is ~48s. Doing this synchronously reproduces the
  #2235 trap exactly: the operator reads the worker's 503 as "it failed" while
  gunicorn runs on, opens the PR anyway, and spends the daily budget. So:

    queue  (fast INSERT, milliseconds)  ← the button
    drain  (slow, off-request, cron)    ← the work
    status (the portal polls)           ← the answer, including refusals

★ THE REFUSAL IS THE PRODUCT. This lane declines more often than it succeeds,
  by design — most findings here are config, data, or a choice between two
  remedies, not single-string fixes. `'find' appears 3x — ambiguous` is a
  useful answer; silence is not. Every terminal state carries its reason to
  the UI.

★ It calls the EXISTING lanes (brain investigate, brain_pr_opener). No new
  fix machinery, no new authority: the PR opener still never merges, still
  honours brain_guardrails' kill switch and daily budget.

Surface:  POST /api/v1/brain/squasher/queue    {key,title,source}  (admin)
          POST /api/v1/brain/squasher/drain                        (admin/cron)
          GET  /api/v1/brain/squasher/queue                        (admin)
Kill:     SQUASHER_QUEUE_DISABLE=1
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

squasher_queue_bp = Blueprint("squasher_queue", __name__)

_MAX_PER_DRAIN = 2          # bounded: each item costs a model call + GH calls
_MAX_PER_DAY = 12           # a stuck operator cannot burn the budget by clicking

STATUSES = ("queued", "running", "proposed", "refused", "failed")


def _disabled() -> bool:
    return os.environ.get("SQUASHER_QUEUE_DISABLE", "0") == "1"


def _db_url():
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def _conn():
    import psycopg2
    return psycopg2.connect(_db_url(), connect_timeout=5)


def _ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS squasher_work_queue (
            id           BIGSERIAL PRIMARY KEY,
            finding_key  TEXT NOT NULL,
            title        TEXT,
            source       TEXT,
            status       TEXT NOT NULL DEFAULT 'queued',
            reason       TEXT,
            pr_url       TEXT,
            attempts     INTEGER NOT NULL DEFAULT 0,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at  TIMESTAMPTZ
        )""")
    # Added 2026-08-09 for retryable infra failures; older tables lack it.
    cur.execute("ALTER TABLE squasher_work_queue "
                "ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0")
    # One OPEN request per finding. A partial unique index is the right shape
    # here, but ON CONFLICT cannot name a partial index as its target
    # (pg_partial_index_on_conflict trap) — so the enqueue path checks for an
    # open row explicitly rather than relying on ON CONFLICT.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS squasher_queue_open_uniq
            ON squasher_work_queue (finding_key)
         WHERE status IN ('queued', 'running')""")


# ── enqueue: must stay FAST (this is what the browser waits on) ─────────

def enqueue(finding_key: str, title: str = "", source: str = "") -> dict:
    if _disabled():
        return {"ok": False, "error": "SQUASHER_QUEUE_DISABLE=1"}
    if not finding_key:
        return {"ok": False, "error": "finding_key required"}
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            # ★ Count only rows that reached (or are still heading toward) a
            #   REAL verdict. An infra failure — investigator flag off, an HTTP
            #   error — is not an attempt at the finding, and letting it burn a
            #   budget unit means a transient outage silently spends the day's
            #   allowance. Observed 2026-08-08: 8 of 12 slots consumed by
            #   'investigator disabled' during a window when the flag was off.
            cur.execute(
                """SELECT COUNT(*) FROM squasher_work_queue
                    WHERE requested_at > NOW() - INTERVAL '24 hours'
                      AND status <> 'failed'""")
            if int(cur.fetchone()[0]) >= _MAX_PER_DAY:
                return {"ok": False, "error": "daily_cap",
                        "reason": f"{_MAX_PER_DAY} requests in 24h — the lane "
                                  f"is rate-capped so a stuck operator cannot "
                                  f"burn the PR budget by clicking."}
            cur.execute(
                """SELECT id, status FROM squasher_work_queue
                    WHERE finding_key = %s AND status IN ('queued','running')
                    LIMIT 1""", (finding_key[:400],))
            row = cur.fetchone()
            if row:
                return {"ok": True, "already": True, "id": row[0],
                        "status": row[1]}
            cur.execute(
                """INSERT INTO squasher_work_queue
                       (finding_key, title, source, status)
                   VALUES (%s, %s, %s, 'queued') RETURNING id""",
                (finding_key[:400], (title or "")[:400], (source or "")[:80]))
            new_id = cur.fetchone()[0]
            conn.commit()
            return {"ok": True, "id": new_id, "status": "queued"}
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] enqueue failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


def queue_rows(limit: int = 25) -> list:
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                """SELECT id, finding_key, title, source, status, reason,
                          pr_url, requested_at, finished_at
                     FROM squasher_work_queue
                    ORDER BY requested_at DESC LIMIT %s""", (limit,))
            out = []
            for r in cur.fetchall():
                out.append({
                    "id": r[0], "finding_key": r[1], "title": r[2],
                    "source": r[3], "status": r[4], "reason": r[5],
                    "pr_url": r[6],
                    "requested_at": r[7].isoformat() if r[7] else None,
                    "finished_at": r[8].isoformat() if r[8] else None,
                })
            return out
    except Exception:
        return []


# ── drain: the slow half. NEVER call this from a browser. ───────────────

def _self_headers() -> dict:
    h = {}
    a = os.environ.get("DCHUB_ADMIN_KEY")
    i = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("INTERNAL_KEY")
    if a:
        h["X-Admin-Key"] = a
    if i:
        h["X-Internal-Key"] = i
    return h


def investigation_question(item: dict) -> str:
    """Phrase a finding as the QUESTION the investigator actually takes.

    ★ /api/v1/brain/investigate requires exactly one field: `question`
      (brain_investigator.ask). The first version of this lane posted
      {finding, url} and every drain came back `investigate HTTP 400` — the
      chain was wired correctly and asked the wrong shape. Locked by a test so
      the field name cannot drift back.

    The phrasing matters: the investigator is a business-question engine, so
    naming the finding AND asking for a single-file remedy is what makes a
    mechanical fix extractable downstream."""
    title = (item.get("title") or "").strip()
    key = (item.get("finding_key") or "").strip()
    subject = title or key or "an unnamed finding"
    where = f" (observed at: {key})" if key and key != title else ""
    return (f"{subject}{where}. What is the root cause, and is there a single "
            f"unambiguous find-and-replace fix in one file that resolves it? "
            f"If there is no mechanical fix, say so plainly and explain why.")


def _investigate(item: dict) -> dict:
    """Run the brain's investigator. Synchronous (~48s) by its own design."""
    from flask import current_app
    with current_app.test_client() as c:
        r = c.post("/api/v1/brain/investigate", headers=_self_headers(),
                   json={"question": investigation_question(item)})
        if r.status_code != 200:
            body = ""
            try:
                body = ((r.get_json() or {}).get("error") or "")[:120]
            except Exception:
                pass
            # Carry the backend's OWN error text: "investigate HTTP 400" sent
            # me reading source; "HTTP 400: question required" would not have.
            return {"ok": False,
                    "reason": f"investigate HTTP {r.status_code}"
                              + (f": {body}" if body else "")}
        d = r.get_json() or {}
    # ★ Flag-off returns 200 with enabled:false and NO result. Storing that as
    #   an analysis makes "never ran" read as "looked and found nothing".
    if d.get("enabled") is False:
        return {"ok": False, "reason": "investigator disabled "
                                       "(BRAIN_INVESTIGATOR_ENABLED)"}
    return {"ok": True, "result": d.get("result") or {}}


def _remedy_from(result: dict) -> dict | None:
    """Extract a single-string remedy, or None. Conservative: all three of
    file/find/replace must be present and non-empty, or there is no mechanical
    fix here and the honest answer is the refusal."""
    if not isinstance(result, dict):
        return None
    for key in ("remedy", "fix", "proposed_fix"):
        r = result.get(key)
        if isinstance(r, dict):
            f, fi, rp = r.get("file"), r.get("find"), r.get("replace")
            if f and fi and rp is not None:
                return {"file": f, "find": fi, "replace": rp}
    return None


def _open_pr(item: dict, remedy: dict) -> dict:
    from flask import current_app
    title = ("[squasher] " + (item.get("title") or item["finding_key"]))[:120]
    with current_app.test_client() as c:
        r = c.post("/api/v1/brain/open-pr-for-finding",
                   headers=_self_headers(),
                   json={"issue": "generic_find_replace",
                         "pr_title": title,
                         "url": item.get("finding_key"),
                         "detail": (item.get("title") or "")[:400],
                         **remedy})
        d = r.get_json() or {}
    if r.status_code == 200 and d.get("ok"):
        return {"ok": True, "pr_url": d.get("pr_url") or d.get("url")}
    return {"ok": False,
            "reason": (d.get("reason") or d.get("error")
                       or f"pr-opener HTTP {r.status_code}")[:300]}


# ★★★ AN INFRA FAILURE IS NOT A VERDICT ABOUT THE FINDING.
#   Observed 2026-08-08: 8 of 12 queued items were closed 'refused —
#   investigator disabled (BRAIN_INVESTIGATOR_ENABLED)' during a window when
#   that flag was off. Every one was recorded TERMINAL, so when the
#   investigator came back (verified 4/4 minutes later: enabled=True, real
#   results) those findings were already burned and never retried. Two costs:
#   the finding is lost, and the refusal reads on the board as "the brain
#   looked and declined" when the brain never looked at all.
#   This is BLIND != RED, applied to the queue's own outcomes.
_RETRYABLE_MARKERS = (
    "investigator disabled",     # flag off / rolling deploy
    "investigate http",          # any non-200 from the chain
    "pr-opener http",
    "timeout", "timed out", "connection", "unavailable",
    "502", "503", "504", "econnreset",
)
_MAX_ATTEMPTS = 3


def is_retryable(reason: str) -> bool:
    """True when the reason describes OUR plumbing, not the finding.

    Conservative on purpose: an unrecognised reason is treated as a real
    verdict (terminal), because re-queueing a genuine refusal forever would
    spin the lane. Only reasons we recognise as infrastructure retry.
    """
    r = (reason or "").lower()
    return any(m in r for m in _RETRYABLE_MARKERS)


def _requeue(item_id: int, reason: str) -> bool:
    """Send an item back to 'queued' unless it has exhausted its attempts."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(attempts, 0) FROM squasher_work_queue "
                        "WHERE id=%s", (item_id,))
            row = cur.fetchone()
            attempts = int(row[0]) if row else 0
            if attempts + 1 >= _MAX_ATTEMPTS:
                cur.execute(
                    """UPDATE squasher_work_queue
                          SET status='failed', attempts=attempts+1,
                              reason=%s, finished_at=NOW()
                        WHERE id=%s""",
                    (f"gave up after {_MAX_ATTEMPTS} attempts — {reason}"[:600],
                     item_id))
                conn.commit()
                return False
            cur.execute(
                """UPDATE squasher_work_queue
                      SET status='queued', attempts=attempts+1,
                          reason=%s, finished_at=NULL
                    WHERE id=%s""",
                (f"retrying (attempt {attempts + 1}/{_MAX_ATTEMPTS}) — "
                 f"{reason}"[:600], item_id))
            conn.commit()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] requeue %s failed: %s", item_id, e)
        return False


def _settle(item_id: int, reason: str, results: list, item: dict) -> None:
    """Terminal-vs-retry decision for a NON-success outcome."""
    if is_retryable(reason):
        requeued = _requeue(item_id, reason)
        results.append({"id": item_id,
                        "status": "queued" if requeued else "failed",
                        "retryable": True, "reason": reason})
    else:
        _finish(item_id, "refused", reason)
        results.append({"id": item_id, "status": "refused", "reason": reason})


def _finish(item_id: int, status: str, reason: str = "", pr_url: str = ""):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE squasher_work_queue
                      SET status=%s, reason=%s, pr_url=%s, finished_at=NOW()
                    WHERE id=%s""",
                (status, (reason or "")[:600], pr_url or None, item_id))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] finish %s failed: %s", item_id, e)


def drain(limit: int = _MAX_PER_DRAIN) -> dict:
    """Process queued items. Bounded, fail-soft, one item at a time."""
    if _disabled():
        return {"ok": True, "skipped": "SQUASHER_QUEUE_DISABLE=1"}
    out = {"ok": True, "processed": 0, "results": []}
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                """SELECT id, finding_key, title, source
                     FROM squasher_work_queue WHERE status='queued'
                      AND COALESCE(attempts, 0) < %s
                    ORDER BY attempts ASC, requested_at ASC LIMIT %s""",
                (_MAX_ATTEMPTS, limit))
            items = [{"id": r[0], "finding_key": r[1], "title": r[2],
                      "source": r[3]} for r in cur.fetchall()]
            for it in items:
                cur.execute(
                    "UPDATE squasher_work_queue SET status='running' "
                    "WHERE id=%s", (it["id"],))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

    for it in items:
        try:
            inv = _investigate(it)
            if not inv.get("ok"):
                _settle(it["id"], inv.get("reason", "investigate failed"),
                        out["results"], it)
                continue
            remedy = _remedy_from(inv.get("result") or {})
            if not remedy:
                # The honest terminal state: investigated, no mechanical fix.
                reason = ("investigated — no single-string remedy. This is a "
                          "config/data/judgement finding, not a find-replace. "
                          "The analysis is attached to the finding.")
                _finish(it["id"], "refused", reason)
                out["results"].append({"id": it["id"], "status": "refused",
                                       "reason": reason})
                continue
            pr = _open_pr(it, remedy)
            if pr.get("ok"):
                _finish(it["id"], "proposed", "PR opened for review",
                        pr.get("pr_url", ""))
                out["results"].append({"id": it["id"], "status": "proposed",
                                       "pr_url": pr.get("pr_url")})
            else:
                _settle(it["id"], pr.get("reason", "pr refused"),
                        out["results"], it)
        except Exception as e:  # noqa: BLE001
            # An exception in OUR loop is infrastructure by definition.
            _settle(it["id"], f"drain exception: {type(e).__name__}: {e}"[:300],
                    out["results"], it)
        out["processed"] += 1
    return out


# ── endpoints ───────────────────────────────────────────────────────────

def _admin_ok() -> bool:
    keys = set()
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.cookies.get("dchub_innov_key") or "").strip()
    return bool(sent) and sent in keys


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


@squasher_queue_bp.post("/api/v1/brain/squasher/queue")
def queue_post():
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    b = request.get_json(silent=True) or {}
    res = enqueue((b.get("key") or "").strip(),
                  (b.get("title") or "").strip(),
                  (b.get("source") or "").strip())
    return _no_store(jsonify(res)), (200 if res.get("ok") else 400)


@squasher_queue_bp.get("/api/v1/brain/squasher/queue")
def queue_get():
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(ok=True, rows=queue_rows()))


@squasher_queue_bp.post("/api/v1/brain/squasher/drain")
def drain_post():
    """★ Cron/origin only. Through the CF edge this WILL 503 at 15s while the
    origin keeps working — read the queue afterwards, not this response."""
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    try:
        limit = min(int(request.args.get("limit") or _MAX_PER_DRAIN), 5)
    except Exception:
        limit = _MAX_PER_DRAIN
    return _no_store(jsonify(drain(limit)))
