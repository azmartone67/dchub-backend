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
import posixpath
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

squasher_queue_bp = Blueprint("squasher_queue", __name__)

_MAX_PER_DRAIN = 2          # bounded: each item costs a model call + GH calls
_MAX_PR_PER_DAY = 12        # PRs opened/24h — the budget the message meant
_MAX_WORK_PER_DAY = 40      # investigations/24h — model spend, a separate cost

STATUSES = ("queued", "running", "proposed", "refused", "failed",
            "awaiting_ops", "awaiting_decision", "resolved")

# ★ 2026-08-20 — WHY THE QUEUE NEVER DRAINED.
#
# Read live that day: all 25 rows in the queue were 'refused', and not one of
# them was a refusal in any ordinary sense. Every single analysis had reached a
# real conclusion and said so:
#
#   "Do not ship a code change: the evidence points to an operational/data
#    issue" -> "Approve running POST /api/v1/admin/facility-dedup/apply
#    ?country=NL"
#   "No single mechanical fix exists" -> "Choose between (a) re-run the
#    detector scan first ... or (b) close as covered by #2937"
#
# The lane had exactly ONE exit that counted as progress: a PR. A finding whose
# real remedy is an operator ACTION, or a JUDGEMENT only a human can make, hit
# `if not remedy:` and was closed 'refused' — terminal, with the analysis we
# had just paid ~80s of model time for stored and abandoned. The detector then
# re-surfaced the same finding on the next 6h tick, forever. That is the
# 23-28 'terminal_acknowledged' per run in propose-stage/status: the same
# findings being reconsidered and re-closed in a loop with no exit.
#
# Seven of the 25 were ONE root problem (facility-dedup not applied) fanned out
# per country, so the loop also multiplied.
#
# The fix is not a better remedy extractor — the extractor is right that there
# is no find-and-replace here. It is that "PR or nothing" is the wrong set of
# exits. A finding can now settle as:
#
#   awaiting_ops      the analysis named a concrete admin endpoint to run.
#                     Waits for an operator to approve and run it.
#   awaiting_decision the analysis enumerated options for a human.
#                     Waits for an answer.
#
# Both are OPEN states in the human sense but TERMINAL for the lane: the lane
# has finished its work and handed off, so it will not re-investigate and
# re-bill. Both carry the analysis that justifies them, which is the thing that
# was being thrown away.
_NONMECHANICAL_STATUSES = ("awaiting_ops", "awaiting_decision")

# A concrete admin action the analysis is asking to have run. Deliberately
# narrow: an HTTP verb followed by an /api/ path. Prose that merely mentions an
# endpoint in passing does not match, and anything that does not match falls
# through to awaiting_decision rather than being invented into an action.
_ACTION_RE = re.compile(
    r"\b(POST|PUT|PATCH|DELETE|GET)\s+(/api/[A-Za-z0-9/_.\-]*"
    r"(?:\?[A-Za-z0-9=&_%.\-]*)?)")


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
    # ★ 2026-08-09: the investigation was ARRIVING AND BEING THROWN AWAY. Every
    #   click paid for a real ~48s analysis (recommendation, decision_for_human,
    #   confidence) and the queue stored only a one-line refusal. Exactly the
    #   class the QA dashboard fixed in #2231 — recreated here. Keep it.
    for _col, _type in (("analysis", "TEXT"), ("decision", "TEXT"),
                        ("confidence", "REAL"), ("investigation_id", "BIGINT")):
        cur.execute(f"ALTER TABLE squasher_work_queue "
                    f"ADD COLUMN IF NOT EXISTS {_col} {_type}")
    # One OPEN request per finding. A partial unique index is the right shape
    # here, but ON CONFLICT cannot name a partial index as its target
    # (pg_partial_index_on_conflict trap) — so the enqueue path checks for an
    # open row explicitly rather than relying on ON CONFLICT.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS squasher_queue_open_uniq
            ON squasher_work_queue (finding_key)
         WHERE status IN ('queued', 'running')""")


# ── enqueue: must stay FAST (this is what the browser waits on) ─────────

def _register_fix_claim(queue_id, finding_key: str, title: str) -> None:
    """★2026-08-22 Claim Loop step 1: pre-register the finding's expected
    post-fix state in the claim ledger (kind=fix) the moment the loop takes
    it on. The radar's findings carry no red_when, so the inverse is the
    canonical writer marking the row resolved (brain_findings.status =
    'resolved'); horizon 7d — the same window this lane's verdict already
    judges itself on. The outcome is stamped by the L16 cron, never here.
    Fail-soft: the ledger can never block an enqueue."""
    try:
        from routes.claim_ledger import register_finding_claim
        register_finding_claim(finding_key, title, queue_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] claim pre-registration failed for #%s: %s",
                       queue_id, e)


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
            # ★ TWO COSTS, TWO CAPS. One counter guarded both and the message
            #   described only one of them ("cannot burn the PR budget"), so a
            #   REFUSAL — which opens no PR — consumed PR allowance. On
            #   2026-08-09 a broken remedy extractor produced 13 false
            #   refusals, and those alone locked the lever for the rest of the
            #   day: the bug spent the budget for failing.
            #     · PR budget   — only `proposed` opens a PR.
            #     · MODEL spend — `refused` and `proposed` each cost one ~80s
            #       investigation; queued/running are about to. `failed` cost
            #       nothing (most error before the model call) and counts to
            #       neither, which the 08-08 note already established.
            #   The message now names WHICH limit was hit, so the next operator
            #   is not left guessing the way today's hardcoded reason left me.
            cur.execute(
                """SELECT
                     COUNT(*) FILTER (WHERE status = 'proposed'),
                     COUNT(*) FILTER (WHERE status IN
                                      ('proposed','refused','queued','running',
                                       'awaiting_ops','awaiting_decision'))
                     FROM squasher_work_queue
                    WHERE requested_at > NOW() - INTERVAL '24 hours'""")
            _row = cur.fetchone() or (0, 0)
            prs, work = int(_row[0] or 0), int(_row[1] or 0)
            if prs >= _MAX_PR_PER_DAY:
                return {"ok": False, "error": "daily_cap",
                        "reason": f"{prs} PR(s) opened in 24h (cap "
                                  f"{_MAX_PR_PER_DAY}) — the lane will not "
                                  f"open more today."}
            if work >= _MAX_WORK_PER_DAY:
                return {"ok": False, "error": "daily_cap",
                        "reason": f"{work} investigation(s) in 24h (cap "
                                  f"{_MAX_WORK_PER_DAY}) — each costs a model "
                                  f"call, so the lane paces itself. PRs opened "
                                  f"today: {prs}/{_MAX_PR_PER_DAY}."}
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
                   VALUES (%s, %s, %s, 'queued') ON CONFLICT DO NOTHING RETURNING id""",
                (finding_key[:400], (title or "")[:400], (source or "")[:80]))
            new_id = cur.fetchone()[0]
            conn.commit()
            _register_fix_claim(new_id, finding_key, title)
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
                          pr_url, requested_at, finished_at,
                          analysis, decision, confidence
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
                    "analysis": r[9], "decision": r[10], "confidence": r[11],
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
    return (
        f"{subject}{where}. What is the root cause, and is there a single "
        f"unambiguous find-and-replace fix in one file that resolves it?\n\n"
        # ★ THE REMEDY CONTRACT. Measured 2026-08-09: the investigator returns
        #   prose only (recommendation / reasoning / decision_for_human) — it
        #   has NO remedy/fix/proposed_fix key, so the extractor that looked for
        #   one could never succeed and the lane refused 100% of the time while
        #   its own copy claimed it "refuses more often than it succeeds".
        #   Asking for a FENCED BLOCK rather than a structured-output schema is
        #   deliberate: a schema missing additionalProperties:false trips
        #   mark_model_unsupported() and disables structured outputs PROCESS-WIDE
        #   for the planner and proposer in the same worker.
        f"IF AND ONLY IF a single mechanical fix exists, end your answer with "
        f"a fenced block exactly like:\n"
        f"```remedy\n"
        f'{{"file": "routes/example.py", "find": "<exact current text>", '
        f'"replace": "<exact new text>"}}\n'
        f"```\n"
        f"Rules for that block: `find` must be text that appears EXACTLY ONCE "
        f"in that file, copied verbatim; never guess a path or a line number; "
        f"never propose a change under .github/. If the fix is config, data, "
        f"ops, or a judgement call — or you are not certain the find string is "
        f"unique — OMIT the block entirely and say plainly why no mechanical "
        f"fix applies. An omitted block is a correct and expected answer.")


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
        # ★ CARRY THE BACKEND'S OWN NOTE — do not assert a cause we did not
        #   observe. This previously hardcoded "investigator disabled
        #   (BRAIN_INVESTIGATOR_ENABLED)", and on 2026-08-09 seven queue rows
        #   said exactly that while the flag was verifiably `1` on BOTH
        #   dchub-backend and dchub-worker and a live probe returned
        #   enabled=true with a full result in 80s. The message named a config
        #   problem that did not exist and threw away the only evidence of the
        #   real one. Same lesson as the HTTP branch above, one field over:
        #   report what the callee said, not what we guessed it meant.
        note = str(d.get("note") or "").strip()
        return {"ok": False,
                "reason": ("investigator returned enabled=false"
                           + (f": {note[:200]}" if note
                              else " and gave no reason — check "
                                   "BRAIN_INVESTIGATOR_ENABLED on the service "
                                   "that RUNS the drain, and whether this is "
                                   "a rolling deploy"))}
    return {"ok": True, "result": d.get("result") or {},
            "investigation_id": d.get("id")}


_REMEDY_FENCE = re.compile(r"```remedy\s*(\{.*?\})\s*```", re.S | re.I)


def _remedy_from(result: dict) -> dict | None:
    """Extract a single-string remedy, or None.

    ★ MEASURED 2026-08-09: the investigator's result carries NO remedy/fix/
      proposed_fix key — it is `{caveats, cited_evidence, confidence,
      decision_for_human, decomposition, evidence, model, prior_fixes,
      prior_work, question, reasoning, recommendation, refutation,
      targeted_evidence_count}`. The old extractor looked for keys that do not
      exist, so it returned None every time and the lane refused 100% of the
      time BY CONSTRUCTION while claiming the refusals were judgements about
      the finding. Now it reads the fenced ```remedy block the question asks
      for, out of the prose fields that actually exist.

    Conservative by design: all three of file/find/replace must be present,
    file/find non-empty (replace MAY be "" — deleting a line is a valid fix),
    or there is genuinely no mechanical remedy and refusing is correct.
    """
    if not isinstance(result, dict):
        return None
    # The dict form is still honoured in case a future producer emits one.
    for key in ("remedy", "fix", "proposed_fix"):
        r = result.get(key)
        if isinstance(r, dict):
            f, fi, rp = r.get("file"), r.get("find"), r.get("replace")
            if f and fi and rp is not None:
                return {"file": f, "find": fi, "replace": rp}
    blob = "\n".join(str(result.get(k) or "") for k in
                      ("recommendation", "decision_for_human", "reasoning"))
    m = _REMEDY_FENCE.search(blob)
    if not m:
        return None
    try:
        r = json.loads(m.group(1))
    except Exception:
        return None
    if not isinstance(r, dict):
        return None
    f, fi, rp = r.get("file"), r.get("find"), r.get("replace")
    if not f or not fi or rp is None:
        return None
    # .github/ is off-limits here too, not only in the PR opener: every other
    # guard rests on a reviewer seeing the diff, and CI config decides what the
    # reviewer is shown.
    # ★ NORMALISE, then test — and never with lstrip("./"), which strips those
    #   CHARACTERS rather than a prefix, so ".github/ci.yml" becomes
    #   "github/ci.yml" and the guard silently passes. Same shape as the
    #   PR-opener's dead absolute-path check (#2235) and caught here by the
    #   test written to expect the refusal.
    norm = posixpath.normpath(str(f).replace("\\", "/"))
    if norm.startswith("/") or norm.split("/")[0] in ("..", ".github"):
        return None
    return {"file": str(f), "find": str(fi), "replace": str(rp)}


def analysis_of(result: dict) -> dict:
    """The parts of an investigation worth KEEPING on the row."""
    if not isinstance(result, dict):
        return {}
    conf = result.get("confidence")
    return {
        "analysis": (result.get("recommendation") or "")[:4000] or None,
        "decision": (result.get("decision_for_human") or "")[:2000] or None,
        "confidence": float(conf) if isinstance(conf, (int, float)) else None,
    }


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
#
# ★★★ AND THEN #2491 RENAMED THE MESSAGE AND SILENTLY UNPLUGGED THIS.
#   #2491 was right to stop asserting a cause we never observed, so the
#   emitted reason became "investigator returned enabled=false: …". That
#   string does not contain "investigator disabled", so is_retryable() went
#   False for it and BOTH safety nets built for this exact condition went
#   dead: _settle() closed the row TERMINAL instead of re-queueing, and
#   reclaim_misfiled() — the permanent healer written so a forward-only fix
#   would not strand findings — skipped it on every pass.
#   Measured 2026-08-18 on the live queue, the before/after is in one table:
#     7 rows with the OLD string   -> attempts=3, status='failed'  (retried)
#    82 rows with the NEW string   -> attempts=0, status='refused' (burned)
#   42 distinct findings, 08-10 to 08-18, none of which was ever looked at.
#   ★ THE TESTS ENCODED THE DISCONNECTION AND STAYED GREEN. In one file,
#     test_investigator_disabled_is_RETRYABLE_not_a_refusal asserts the
#     marker is the literal "investigator disabled", while
#     test_enabled_false_without_a_note_does_not_invent_a_cause asserts the
#     emitted reason must NOT contain it. Each guard is correct alone;
#     together they prove the lane is broken. Nothing compared them — so the
#     linkage is now asserted directly, by running _investigate()'s own
#     enabled=false branch and classifying the string it actually returns.
#     Never key a classifier on a free-text message and then test the two
#     halves against literals; test them against EACH OTHER.
_RETRYABLE_MARKERS = (
    "investigator disabled",     # flag off / rolling deploy (pre-#2491)
    "enabled=false",             # the post-#2491 wording of the same thing
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


def _store_analysis(item_id: int, inv: dict) -> None:
    """Keep the investigation. STORE FIRST, decide second — losing a ~48s
    analysis to a downstream hiccup rebuilds the hole #2231 closed."""
    a = analysis_of(inv.get("result") or {})
    if not any(a.values()) and not inv.get("investigation_id"):
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE squasher_work_queue
                      SET analysis=%s, decision=%s, confidence=%s,
                          investigation_id=%s
                    WHERE id=%s""",
                (a.get("analysis"), a.get("decision"), a.get("confidence"),
                 inv.get("investigation_id"), item_id))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] store analysis %s: %s", item_id, e)


def _nonmechanical_exit(result: dict) -> tuple[str, str] | None:
    """Route a finding that has no find-and-replace fix to a REAL exit.

    Returns (status, reason) or None if the analysis genuinely said nothing
    actionable — in which case 'refused' remains the honest answer.

    Reads only fields the investigator actually emits. The keys were measured
    2026-08-09 and are: caveats, cited_evidence, confidence, decision_for_human,
    decomposition, evidence, model, prior_fixes, prior_work, question,
    reasoning, recommendation, refutation, targeted_evidence_count. An earlier
    extractor in this file looked for keys that do not exist and so refused
    100% of the time BY CONSTRUCTION while presenting the refusals as
    judgements — do not add a key here without checking it is really produced.
    """
    if not isinstance(result, dict):
        return None
    decision = (result.get("decision_for_human") or "").strip()
    recommendation = (result.get("recommendation") or "").strip()
    if not decision and not recommendation:
        return None

    # An operator ACTION beats a decision: if the analysis named a specific
    # endpoint to run, that is a more concrete hand-off than "choose between".
    m = _ACTION_RE.search(decision) or _ACTION_RE.search(recommendation)
    if m:
        verb, path = m.group(1), m.group(2)
        return ("awaiting_ops",
                f"operator action required: {verb} {path} — "
                f"{(decision or recommendation)[:400]}")

    if decision:
        return ("awaiting_decision",
                f"human decision required: {decision[:500]}")
    return None


_REFUSED_REASON = (
    "investigated — the analysis found no single unambiguous find-and-replace "
    "fix, and named no operator action or decision either. Read the analysis "
    "below: that is this lane's real output for config, data, ops and "
    "judgement findings.")


def _settle_for(result: dict, remedy: dict | None) -> tuple[str, str] | None:
    """How a finished investigation settles. None means: open a PR.

    ★ This is a separate pure function ON PURPOSE. The routing bug it guards
      lived in the drain loop's control flow, not in the classifier — and a
      test that called the classifier directly could not see the call site at
      all. Mutation-checked: replacing the drain loop's routing with a constant
      left every classifier test green. Pulling the whole decision out here
      makes the wiring itself the unit under test, so that mutation now fails.

      The lesson generalises to this repo: testing the helper and leaving the
      decision inline is how a lane refuses 100% of the time with a green
      suite.
    """
    if remedy:
        return None
    routed = _nonmechanical_exit(result)
    if routed:
        return routed
    return ("refused", _REFUSED_REASON)


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


def reclaim_misfiled(cur) -> int:
    """Return rows CLOSED as 'refused' for a reason that was really OUR
    plumbing back to the queue.

    ★ WHY THIS IS PERMANENT, NOT A ONE-OFF SCRIPT. The retry classification
      (#2454) only decides how an item settles from that moment on, so the 8
      findings burned during the 2026-08-08 investigator outage stayed
      'refused — investigator disabled' after the fix shipped: a forward-only
      fix does not heal the damage it was written about. Any future window
      where a settle path is wrong would strand findings the same way. So the
      drain reclaims them every pass, and the same conservative rule applies —
      only reasons is_retryable() recognises, and only under the attempt
      ceiling. A genuine refusal is never reopened.

    ★★★ IT COULD NOT COMMIT, AND IT SAID IT DID (2026-08-18). With #2866's
      marker in place this function finally matched 20 rows — and wrote ZERO,
      while `drain` reported `"reclaimed": 1`. Reclaiming sets status='queued',
      which is covered by the PARTIAL UNIQUE INDEX `squasher_queue_open_uniq`
      (finding_key WHERE status IN ('queued','running')). Of those 20 rows, 6
      collided: 4 shared a finding_key with an earlier row in the SAME batch
      (82 rows are only 42 distinct findings) and 2 with a row still stuck
      'running' since 08-09. The first collision ABORTED the psycopg2
      transaction; `except Exception: pass` swallowed it; every later execute
      raised InFailedSqlTransaction and was swallowed too; and `conn.commit()`
      on an aborted transaction is a ROLLBACK — so the rows that HAD succeeded
      were discarded with the rest.
      Three separate defects, and each one hid the next:
        1. one conflicting row poisoned the whole batch,
        2. the fail-soft except made that invisible,
        3. the counter incremented on "execute did not raise" rather than on
           rows actually changed, so the endpoint reported success for work it
           had just thrown away.
      ★ A fail-soft `except: pass` INSIDE a transaction is not fail-soft — it
        converts one bad row into a silent total rollback. Isolate per row, or
        do not swallow.
    """
    try:
        # Exclude both collision sources IN SQL so the UPDATE cannot raise:
        #   · NOT EXISTS  — the finding already has an open row (the 'running'
        #     strays included), so re-opening a second one is exactly what the
        #     index forbids;
        #   · rn = 1      — at most ONE row per finding_key per batch, oldest
        #     first, so the batch cannot collide with itself.
        # Ordering stays oldest-first (fairness); DISTINCT ON would have forced
        # ordering by finding_key and quietly turned this into alphabetical
        # head-of-line service.
        cur.execute("""
            SELECT id, reason FROM (
                SELECT q.id, q.reason, q.requested_at,
                       ROW_NUMBER() OVER (PARTITION BY q.finding_key
                                          ORDER BY q.requested_at ASC) AS rn
                  FROM squasher_work_queue q
                 WHERE q.status = 'refused'
                   AND COALESCE(q.attempts, 0) < %s
                   AND NOT EXISTS (
                         SELECT 1 FROM squasher_work_queue o
                          WHERE o.finding_key = q.finding_key
                            AND o.status IN ('queued', 'running'))
            ) t
             WHERE rn = 1
             ORDER BY requested_at ASC LIMIT 50""", (_MAX_ATTEMPTS,))
        rows = cur.fetchall()
    except Exception:
        return 0
    n = 0
    for row_id, reason in rows:
        if not is_retryable(reason or ""):
            continue
        # SAVEPOINT per row: the SQL above removes the collisions we know
        # about, but this function's whole failure mode was ONE unexpected row
        # taking the batch down invisibly. Safe here because _conn() is
        # transactional — SAVEPOINT outside a transaction block raises, and a
        # try/except around it would skip every row forever
        # (reference_psycopg2_savepoint_autocommit_trap).
        try:
            cur.execute("SAVEPOINT reclaim_row")
            cur.execute(
                """UPDATE squasher_work_queue
                      SET status='queued', attempts=COALESCE(attempts,0)+1,
                          reason=%s, finished_at=NULL
                    WHERE id=%s AND status='refused'""",
                (f"reclaimed — closed on an infrastructure reason, not a "
                 f"verdict: {reason}"[:600], row_id))
            # ★ Count ROWS CHANGED, not "execute did not raise". The old
            #   counter reported 1 for a batch that wrote nothing.
            n += max(0, cur.rowcount or 0)
            cur.execute("RELEASE SAVEPOINT reclaim_row")
        except Exception:  # noqa: BLE001
            try:
                cur.execute("ROLLBACK TO SAVEPOINT reclaim_row")
            except Exception:  # noqa: BLE001
                return n
    return n


# ★★★ A ROW THAT WENT 'running' AND NEVER CAME BACK IS STRANDED FOREVER.
#   drain() flips a row to 'running' before it does any work, and the ONLY ways
#   out are _finish() and _settle(). If the process dies in between — redeploy,
#   worker recycle, gunicorn's own --timeout kill — nothing ever moves that row
#   again: drain() selects `status='queued'` and reclaim_misfiled() selects
#   `status='refused'`, so neither can see it. Worse, the partial unique index
#   squasher_queue_open_uniq covers status IN ('queued','running'), so enqueue()
#   finds an open row and returns `already` — that finding can never be
#   re-queued by anyone, forever. Measured 2026-08-18: 8 rows (ids 15,16,17,19,
#   27,52,53,121) frozen 'running' since 08-09, 8 findings permanently unreachable.
#   Same class as the misfiled-refusal leak one status over, and #2866 did not
#   touch it.
#
# ★ WHY 15 MINUTES, AND NOT A ROUND NUMBER PICKED FROM THE AIR.
#   The bound is the deployment's, not a guess. start_web.sh runs
#   `gunicorn --timeout 120`, so the request that owns a 'running' row is HARD
#   KILLED at 120s — that is the longest any live process can possibly hold one
#   (the investigate chain is ~48s and a drain does 2, which is why this
#   stranding path exists at all: the work outruns the worker). cron_heartbeat
#   fires the drain every ~600s. 900s is 7.5x the hard process ceiling and 1.5x
#   the cron cadence, so a row this old cannot still be owned by anything alive.
#   ★ Deliberately NOT tuned close to 120s: reclaiming a row a live worker still
#     holds would run the same finding twice and double-spend the model budget.
#     The cost of waiting is one cron tick; the cost of being wrong is a
#     duplicate PR.
_STALE_RUNNING_SECONDS = 15 * 60


def is_stale_running(requested_at, now=None) -> bool:
    """True when a 'running' row is older than any live process could be.

    Conservative in the same spirit as is_retryable(): if the age cannot be
    established — no timestamp, an unparseable one — the answer is False and
    the row is left alone. Guessing that a row is dead when it is not means
    running the same ~48s investigation twice and opening two PRs for it.

    ★ The age test lives HERE, in Python, and not only in the reclaim query's
      WHERE clause, on purpose. A guard that exists only as SQL cannot be
      exercised by a test — a cursor stub serves whatever rows it was handed
      regardless of the WHERE — so its False branch would never once be
      observed, which is the definition of an unverified guard. The SQL filter
      stays as well: it keeps the scan bounded in production. Belt and braces,
      and the braces are the testable half.
    """
    if requested_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        # A naive timestamp out of a driver that lost the tzinfo is read as
        # UTC rather than crashed on — the column is TIMESTAMPTZ.
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = (now - requested_at).total_seconds()
    except Exception:  # noqa: BLE001
        return False
    return age > _STALE_RUNNING_SECONDS


def reclaim_stale_running(cur, now=None) -> int:
    """Return rows abandoned mid-investigate to the queue, or settle them.

    ★ WHY THIS IS PERMANENT, NOT A ONE-OFF SCRIPT — the same argument
      reclaim_misfiled() makes, one status over. Deleting or hand-fixing the 8
      stuck rows would clear today's damage and leave the leak open: every
      future redeploy that lands inside a drain strands whatever was in flight.
      The healer runs every pass so the window closes itself.

    ★ AND UNLIKE reclaim_misfiled, SKIPPING A CEILINGED ROW IS NOT SAFE HERE.
      reclaim_misfiled leaves an exhausted row 'refused' — terminal, closed,
      out of the open-row index. Leaving an exhausted row 'running' recreates
      the exact bug: still open, still blocking its finding_key, still
      invisible to every selector. So a row that has burned its attempts is
      settled 'failed' — a finding that kills its worker three times running is
      a real problem to look at, not one to retry forever, and 'failed' at
      least releases the finding so it can be enqueued again.
    """
    try:
        cur.execute("""
            SELECT id, requested_at, COALESCE(attempts, 0)
              FROM squasher_work_queue
             WHERE status = 'running'
               AND requested_at < NOW() - (%s * INTERVAL '1 second')
             ORDER BY requested_at ASC LIMIT 50""",
                    (_STALE_RUNNING_SECONDS,))
        rows = cur.fetchall()
    except Exception:
        return 0
    n = 0
    for row_id, requested_at, attempts in rows:
        if not is_stale_running(requested_at, now):
            continue
        attempts = int(attempts or 0)
        try:
            if attempts + 1 >= _MAX_ATTEMPTS:
                cur.execute(
                    """UPDATE squasher_work_queue
                          SET status='failed', attempts=%s,
                              reason=%s, finished_at=NOW()
                        WHERE id=%s AND status='running'""",
                    (attempts + 1,
                     f"gave up after {_MAX_ATTEMPTS} attempts — abandoned "
                     f"mid-investigate each time (the process handling it died "
                     f"before it could settle). Look at this finding by hand."
                     [:600],
                     row_id))
            else:
                cur.execute(
                    """UPDATE squasher_work_queue
                          SET status='queued', attempts=%s,
                              reason=%s, finished_at=NULL
                        WHERE id=%s AND status='running'""",
                    (attempts + 1,
                     f"reclaimed — left 'running' with no process to finish it "
                     f"(redeploy, worker recycle or the 120s gunicorn timeout "
                     f"mid-investigate). Retry {attempts + 1}/{_MAX_ATTEMPTS}."
                     [:600],
                     row_id))
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def drain(limit: int = _MAX_PER_DRAIN) -> dict:
    """Process queued items. Bounded, fail-soft, one item at a time."""
    if _disabled():
        return {"ok": True, "skipped": "SQUASHER_QUEUE_DISABLE=1"}
    out = {"ok": True, "processed": 0, "reclaimed": 0,
           "reclaimed_running": 0, "results": []}
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            # Heal mis-filed closures BEFORE selecting work, so a reclaimed
            # row can be picked up in this very pass.
            out["reclaimed"] = reclaim_misfiled(cur)
            # Same reason, one status over: a row abandoned mid-investigate is
            # invisible to the selector below AND blocks its finding_key in the
            # open-row index. Heal it here or it is stranded forever.
            # ★ Reported as its OWN counter, not folded into `reclaimed`. A
            #   bounded lane that does not publish what it moved reads as
            #   "nothing to do" — the same shape as the intake cap that hid
            #   18 starved findings behind `"rows": 8`.
            out["reclaimed_running"] = reclaim_stale_running(cur)
            conn.commit()
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
        # ★Count at the TOP, not the bottom. Every item here was SELECTed and
        # already marked `running`, so it is processed whatever the outcome.
        # This used to sit after the try/except, past two `continue`s, so a
        # refusal reported `processed: 0` beside a returned result — a lying
        # counter on a board whose entire remit is honest numbers.
        out["processed"] += 1
        try:
            inv = _investigate(it)
            if not inv.get("ok"):
                _settle(it["id"], inv.get("reason", "investigate failed"),
                        out["results"], it)
                continue
            _store_analysis(it["id"], inv)      # STORE FIRST, decide second
            remedy = _remedy_from(inv.get("result") or {})
            settled = _settle_for(inv.get("result") or {}, remedy)
            if settled is not None:
                # No find-and-replace. That is NOT the end of the road — the
                # finding goes to the exit its own analysis asked for
                # (awaiting_ops / awaiting_decision), and is only called a
                # refusal if the analysis really named nothing to do.
                status, reason = settled
                _finish(it["id"], status, reason)
                out["results"].append({"id": it["id"], "status": status,
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


_CONVERGENCE_SQL = """
    WITH closed AS (
        SELECT finding_key, id, status, finished_at
          FROM squasher_work_queue
         WHERE finished_at IS NOT NULL
           AND finished_at > NOW() - (%s || ' days')::INTERVAL
    ),
    recurred AS (
        SELECT c.id
          FROM closed c
          JOIN squasher_work_queue later
            ON later.finding_key = c.finding_key
           AND later.requested_at > c.finished_at
    )
    SELECT
      (SELECT COUNT(*) FROM closed),
      (SELECT COUNT(DISTINCT id) FROM recurred),
      (SELECT COUNT(*) FROM closed WHERE status = 'proposed'),
      (SELECT COUNT(DISTINCT c.id) FROM closed c JOIN recurred r ON r.id = c.id
        WHERE c.status = 'proposed'),
      (SELECT COUNT(*) FROM squasher_work_queue
        WHERE status IN ('awaiting_ops','awaiting_decision'))
"""


def convergence(days: int = 30) -> dict:
    """Are fixes STICKING, or is the same finding coming back?

    The number this platform did not have. Over the week to 2026-08-20 the
    backend merged 290 PRs, 104 of them prefixed `fix`, with CI 351/354 green —
    and nothing anywhere measured whether any of it reduced recurrence. Volume
    was the only visible signal, and volume cannot distinguish "we are fixing
    things" from "we are re-fixing the same things".

    recurrence_rate is the honest one: of the findings this lane CLOSED, what
    fraction came back afterwards. A fix that holds means the detector stops
    re-surfacing its finding. A rate that stays high means the lane is
    generating motion, not convergence, and the right response is to stop
    shipping patches and look at why the finding regenerates.

    pr_recurrence_rate narrows it to findings actually closed with a PR — the
    cases where code shipped. If THAT is high, the code fixes specifically are
    not holding, which is a different and worse problem than ops findings
    recurring because nobody ran the action.

    Reported as counts alongside the rates on purpose: a 100% recurrence rate
    over 2 findings is noise, and a rate with no denominator invites exactly
    the over-reading this file has been burned by before.
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(_CONVERGENCE_SQL, (str(int(days)),))
            row = cur.fetchone() or (0, 0, 0, 0, 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] convergence failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}

    closed, recurred, pr_closed, pr_recurred, waiting = (int(x or 0) for x in row)

    def _rate(n, d):
        # None, not 0.0 — "no data" and "zero recurrence" are different claims,
        # and collapsing them is how a dashboard reports success it never
        # measured.
        return round(n / d, 3) if d else None

    return {
        "ok": True,
        "window_days": int(days),
        "closed": closed,
        "recurred": recurred,
        "recurrence_rate": _rate(recurred, closed),
        "closed_with_pr": pr_closed,
        "pr_recurred": pr_recurred,
        "pr_recurrence_rate": _rate(pr_recurred, pr_closed),
        "waiting_on_human": waiting,
        "reading": (
            "recurrence_rate is the convergence signal: the share of closed "
            "findings that came back. Falling = fixes are holding. Flat and "
            "high = the lane is producing motion, not convergence. Rates are "
            "null when the denominator is 0 — that is 'not measured', not "
            "'zero'."
        ),
    }


@squasher_queue_bp.get("/api/v1/brain/squasher/convergence")
def convergence_get():
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    try:
        days = min(max(int(request.args.get("days") or 30), 1), 365)
    except Exception:
        days = 30
    return _no_store(jsonify(convergence(days)))


@squasher_queue_bp.get("/api/v1/brain/squasher/inbox")
def inbox_get():
    """Findings waiting on a human — the exits that did not exist before.

    Without a surface, awaiting_ops/awaiting_decision would be a better-named
    dead end than 'refused' was: the lane would still be handing work to nobody.
    This is the hand-off point, and /resolve below is how a row leaves it.
    """
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    rows = []
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                """SELECT id, finding_key, title, status, reason, confidence,
                          analysis, decision, requested_at, finished_at
                     FROM squasher_work_queue
                    WHERE status IN %s
                 ORDER BY finished_at DESC NULLS LAST, id DESC
                    LIMIT 200""",
                (tuple(_NONMECHANICAL_STATUSES),))
            for r in cur.fetchall() or []:
                rows.append({
                    "id": r[0], "finding_key": r[1], "title": r[2],
                    "status": r[3], "reason": r[4], "confidence": r[5],
                    "analysis": r[6], "decision": r[7],
                    "requested_at": r[8].isoformat() if r[8] else None,
                    "finished_at": r[9].isoformat() if r[9] else None,
                })
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] inbox failed: %s", e)
        return _no_store(jsonify(ok=False, error=str(e)[:200])), 500
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return _no_store(jsonify(ok=True, counts=by, rows=rows))


@squasher_queue_bp.post("/api/v1/brain/squasher/resolve")
def resolve_post():
    """Close a row that was waiting on a human, recording what they decided.

    ★ This endpoint does NOT run the operator action itself. The analyses that
      land in awaiting_ops name things like POST /api/v1/admin/facility-dedup/
      apply?country=NL — mutations against production data, chosen by a model
      at ~0.35 confidence. Executing those automatically would hand an
      unreviewed model the write path to the dedup tables, which is a much
      worse failure than the stalled queue this fixes. The operator runs the
      action; this records that they did, and what happened.

    outcome:
      done      the action was run / the decision was made. Terminal.
      rejected  the human declined. Terminal, and the reason is kept so the
                next investigation of the same finding can see it was
                considered and dismissed rather than missed.
    """
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    b = request.get_json(silent=True) or {}
    try:
        item_id = int(b.get("id") or 0)
    except Exception:
        item_id = 0
    outcome = (b.get("outcome") or "").strip().lower()
    note = (b.get("note") or "").strip()
    if not item_id:
        return _no_store(jsonify(ok=False, error="id required")), 400
    if outcome not in ("done", "rejected"):
        return _no_store(jsonify(
            ok=False, error="outcome must be 'done' or 'rejected'")), 400
    if outcome == "rejected" and not note:
        # A rejection with no reason is indistinguishable from the silent
        # abandonment this whole change exists to end.
        return _no_store(jsonify(
            ok=False, error="a rejection needs a note saying why")), 400
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                """UPDATE squasher_work_queue
                      SET status = %s,
                          reason = LEFT(COALESCE(reason,'') ||
                                        ' | resolved(' || %s || '): ' || %s, 600),
                          finished_at = NOW()
                    WHERE id = %s AND status IN %s
                RETURNING id, status""",
                ("resolved" if outcome == "done" else "refused",
                 outcome, note or "no note", item_id,
                 tuple(_NONMECHANICAL_STATUSES)))
            row = cur.fetchone()
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher_queue] resolve %s failed: %s", item_id, e)
        return _no_store(jsonify(ok=False, error=str(e)[:200])), 500
    if not row:
        return _no_store(jsonify(
            ok=False, error="no row in a waiting state with that id")), 404
    return _no_store(jsonify(ok=True, id=row[0], status=row[1]))


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
