"""
brain_lessons — compile what the brain's agents LEARNED into one page per
finding family, and hand that page back to every agent that is about to try
again.

WHY THIS EXISTS (measured 2026-09-24)
  The brain records outcomes in four places, and no proposer reads any of them
  at the moment it writes a fix:

    brain_fix_outcomes ⨝ brain_proposed_code_fixes   L5 fix merged → held / did not
    brain_issue_persistence.last_outcome              L5's own guards refused it
    squasher_work_queue.agent_*                       the squasher agent's PR fate
    brain_review_decisions (decision='reject')        a human closed it

  30d live: 134 of 199 graded fixes FAILED (fix_success 32.7%), September MTD
  24.8% vs August 77.4%. `class_success_weight` learns WHICH lane to work on,
  but at lane granularity — nothing told the drafter "this exact family of
  fix has failed 9 times; the same edit will fail a tenth". So it repeated.

  This is the Karpathy "LLM wiki" idea applied to the brain itself: COMPILE
  raw events into a small curated page per topic instead of piling up more
  rows, and make the page what the next agent reads.

WHAT IT DOES
  compile_lessons(events)  PURE. Events → one lesson per family:
                           counts, a verdict (works / fails / mixed / thin),
                           files already tried, and deterministic guidance.
  refresh()                reads the four sources, compiles, upserts brain_lessons.
  lessons_for(label)       the hint text for one finding label — "" when there
                           is nothing worth saying. Read by:
                             · L5 code proposer (brain_v2_layer5, next to _hint_for_issue)
                             · the directive drafter (brain_guardrails.draft_and_open_pr)
                             · the squasher agent brief (squasher_agent_lane.brief_of)

  No model call. The compiler is deterministic so it is cheap, testable and
  cannot hallucinate a lesson the data does not contain.

ENDPOINTS (admin)
  POST /api/v1/admin/brain/lessons/compile   refresh now
  GET  /api/v1/admin/brain/lessons           the compiled pages (?family=)

Kill: BRAIN_LESSONS_DISABLE=1 — lessons_for() returns "" everywhere.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_lessons_bp = Blueprint("brain_lessons", __name__)

WINDOW_DAYS = 90
#: A family needs this many GRADED (held / did-not-hold) outcomes before the
#: compiler will call it "works" or "fails". Below it the verdict is "thin" —
#: two outcomes can swing a rate 50 points.
MIN_GRADED = 3
WORKS_AT = 0.70
FAILS_AT = 0.30
_MAX_FILES = 5
_MAX_NOTES = 3
_CACHE_TTL_S = 600

# Outcome vocabulary — every source maps into exactly one of these.
# ★ REFUSED and DECLINED are different events (split 2026-09-24 after the first
# live compile). REFUSED = L5's deterministic guards threw the proposal out
# because it claimed a syntax/SQLite bug the file does not have. DECLINED = the
# model itself returned an empty edit — L5's documented refusal contract
# (`return [], "refused"`), meaning "this is not expressible as a code change".
# Folding them together told 64 inspector_l22_handoff declines that they had
# hallucinated a bug.
WORKED, FAILED, REJECTED, REFUSED, DECLINED, NEEDS_HUMAN = (
    "worked", "failed", "rejected", "refused", "declined", "needs_human")
_OUTCOMES = (WORKED, FAILED, REJECTED, REFUSED, DECLINED, NEEDS_HUMAN)

#: L5's deterministic guards (brain_v2_layer5._PERMAFAIL). Duplicated as a
#: literal on purpose: importing layer5 here would pull its whole import graph
#: into every reader of lessons_for().
_PERMAFAIL = ("refused", "rejected_false_syntax_claim",
              "rejected_sqlite_hallucination")
_GUARD_OUTCOME = {"refused": DECLINED,
                  "rejected_false_syntax_claim": REFUSED,
                  "rejected_sqlite_hallucination": REFUSED}

_FAMILY_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")


def _disabled() -> bool:
    return (os.environ.get("BRAIN_LESSONS_DISABLE") or "").strip() == "1"


_FILE_EXT_RE = re.compile(r"\.(py|js|mjs|ts|html?|css|json|ya?ml|md|txt|sql|sh)$")


def family_of(label) -> str:
    """The finding FAMILY a label belongs to: its leading token, lowercased.

    `iso_metric_count_zero_24h:WACM` and `iso_metric_count_zero_24h:WAUW` are
    one family — the lesson is about the KIND of problem, not the site. ""
    when the label has no usable token, which callers treat as "no lesson".

    ★ Finding families are snake_case. The first live compile also produced
    `https`, `dchub`, `table` and `ai_interconnection.py` — a URL scheme, bare
    words and a filename, from proposals whose loop_name is a path or URL.
    Those are not kinds of problem, so they are dropped: a token with no
    underscore, or one ending in a file extension, is not a family.
    """
    m = _FAMILY_RE.match((label or "").strip().lower())
    fam = m.group(0)[:80] if m else ""
    if "_" not in fam or _FILE_EXT_RE.search(fam):
        return ""
    return fam


# ═══════════════════════════════════════════════════════════════════════
# the compiler — PURE
# ═══════════════════════════════════════════════════════════════════════
def verdict_for(worked: int, failed: int) -> str:
    graded = worked + failed
    if graded < MIN_GRADED:
        return "thin"
    rate = worked / graded
    if rate >= WORKS_AT:
        return "works"
    if rate <= FAILS_AT:
        return "fails"
    return "mixed"


def _guidance(fam: str, c: dict, files_failed: list, files_worked: list,
              notes: list) -> str:
    """Deterministic, second-person guidance for the NEXT attempt.

    ★ Every sentence is conditional on a count the data actually holds, so a
    family with nothing to teach produces "" rather than filler — an empty
    hint is better than one the drafter learns to skim past.
    """
    w, f, r, x, d, h = (c[k] for k in _OUTCOMES)
    graded = w + f
    lines = []
    v = verdict_for(w, f)
    if v == "fails":
        lines.append(
            f"Fixes for `{fam}` did NOT hold {f} of {graded} times — the "
            f"finding kept firing after the fix landed. Do not repeat the same "
            f"kind of edit: look for the upstream cause, or refuse and say "
            f"what a human must decide.")
        if files_failed:
            lines.append("Already tried without effect: "
                         + ", ".join(f"`{p}`" for p in files_failed) + ".")
    elif v == "mixed":
        lines.append(f"Fixes for `{fam}` held {w} of {graded} times. "
                     f"Prefer the shape of the ones that held.")
        if files_worked:
            lines.append("Held: " + ", ".join(f"`{p}`" for p in files_worked) + ".")
        if files_failed:
            lines.append("Did not hold: "
                         + ", ".join(f"`{p}`" for p in files_failed) + ".")
    elif v == "works":
        lines.append(f"Fixes for `{fam}` held {w} of {graded} times — this "
                     f"family is well understood; keep the same shape.")
        if files_worked:
            lines.append("Held: " + ", ".join(f"`{p}`" for p in files_worked) + ".")
    if r:
        lines.append(f"A human closed {r} proposal(s) for `{fam}` without "
                     f"merging. Do not re-propose the same change.")
    if x >= 2:
        lines.append(f"{x} proposals for `{fam}` were refused by the "
                     f"deterministic guards (claimed a syntax or SQLite bug the "
                     f"file does not have). Quote the exact lines you are "
                     f"changing and confirm they exist.")
    if d >= 2:
        lines.append(f"The drafter declined {d} times to propose an edit for "
                     f"`{fam}` — it could not express it as a code change. "
                     f"Unless you have new evidence, treat this as a config, "
                     f"data or product action and name it, rather than "
                     f"forcing an edit.")
    if h >= 2 and not graded:
        lines.append(f"The agent handed `{fam}` to a human {h} times. If you "
                     f"cannot do better than that, say so immediately.")
    if notes and lines:
        lines.append("Recent notes: " + " | ".join(notes) + ".")
    return " ".join(lines)


def compile_lessons(events) -> dict:
    """{family: lesson}. PURE — no DB, no clock, no network.

    events: [{"family", "outcome", "file"?, "note"?}, ...] with outcome in the
    module's outcome vocabulary (_OUTCOMES). Unknown outcomes and family-less events are
    DROPPED, not counted: a row we cannot place must not move a verdict.
    """
    acc: dict = {}
    for e in events or []:
        fam = family_of((e or {}).get("family"))
        oc = (e or {}).get("outcome")
        if not fam or oc not in _OUTCOMES:
            continue
        a = acc.setdefault(fam, {"counts": dict.fromkeys(_OUTCOMES, 0),
            "files_failed": [], "files_worked": [], "notes": []})
        a["counts"][oc] += 1
        fp = (e.get("file") or "").strip()
        if fp:
            bucket = ("files_worked" if oc == WORKED
                      else "files_failed" if oc == FAILED else None)
            if bucket and fp not in a[bucket] and len(a[bucket]) < _MAX_FILES:
                a[bucket].append(fp)
        note = re.sub(r"\s+", " ", (e.get("note") or "")).strip()[:160]
        if note and oc in (FAILED, REJECTED) and note not in a["notes"] \
                and len(a["notes"]) < _MAX_NOTES:
            a["notes"].append(note)

    out = {}
    for fam, a in acc.items():
        c = a["counts"]
        graded = c[WORKED] + c[FAILED]
        out[fam] = {
            "family": fam,
            "counts": c,
            "graded": graded,
            "success_rate": round(c[WORKED] / graded, 3) if graded else None,
            "verdict": verdict_for(c[WORKED], c[FAILED]),
            "files_failed": a["files_failed"],
            "files_worked": a["files_worked"],
            "guidance": _guidance(fam, c, a["files_failed"],
                                  a["files_worked"], a["notes"]),
        }
    return out


# ═══════════════════════════════════════════════════════════════════════
# sources → events
# ═══════════════════════════════════════════════════════════════════════
def _conn():
    """A TRANSACTIONAL connection. ★ ai_reach._conn() hands back
    autocommit=True, under which every SAVEPOINT below raises
    NoActiveSqlTransaction — the first live run read all sources as
    "unreadable" (2026-09-24). It is a fresh, unpooled connection, so
    flipping it here affects no one else."""
    try:
        from routes.ai_reach import _conn as _raw
        c = _raw()
        if c is not None:
            c.autocommit = False
        return c
    except Exception:
        return None


def _rows(cur, sql, args=None):
    """Rows, or [] — one missing source table must not blank the others."""
    try:
        cur.execute("SAVEPOINT lessons_src")
        cur.execute(sql, args) if args is not None else cur.execute(sql)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT lessons_src")
        return rows
    except Exception as e:  # noqa: BLE001
        try:
            cur.execute("ROLLBACK TO SAVEPOINT lessons_src")
        except Exception:
            pass
        logger.info("[brain-lessons] source unreadable: %s", str(e)[:160])
        return None


_L5_OUTCOMES_SQL = (
    "SELECT COALESCE(NULLIF(p.finding_class, ''), p.issue_key, p.loop_name),"
    "       f.still_broken, p.file_path, f.evidence_note"
    "  FROM brain_fix_outcomes f"
    "  JOIN brain_proposed_code_fixes p ON p.id = f.proposal_id"
    " WHERE f.proposal_kind = 'code' AND f.still_broken IS NOT NULL"
    "   AND COALESCE(f.checked_at, f.applied_at) >= NOW() - %s * INTERVAL '1 day'")
# ★ Added 2026-09-24. brain_fix_outcomes holds TWO graded id spaces:
# proposal_kind='code' → brain_proposed_code_fixes.id (the join above), and
# proposal_kind='autopilot' → brain_autopilot_actions.id, mirrored in by
# brain_learning's autopilot grader. Live, 173 of the latest 189 graded rows
# were 'autopilot' — the code-only join saw 63 of 199 in 30d. The action row
# carries the finding (finding_issue) and what was tried (pattern_name).
_AUTOPILOT_OUTCOMES_SQL = (
    "SELECT a.finding_issue, f.still_broken, a.pattern_name, f.evidence_note"
    "  FROM brain_fix_outcomes f"
    "  JOIN brain_autopilot_actions a ON a.id = f.proposal_id"
    " WHERE f.proposal_kind = 'autopilot' AND f.still_broken IS NOT NULL"
    "   AND COALESCE(f.checked_at, f.applied_at) >= NOW() - %s * INTERVAL '1 day'")
#: Every graded outcome in the window, whatever its kind — the denominator
#: for `coverage`, so an id space no source joins is SEEN, not silently lost.
_GRADED_TOTAL_SQL = (
    "SELECT proposal_kind, COUNT(*) FROM brain_fix_outcomes"
    " WHERE still_broken IS NOT NULL"
    "   AND COALESCE(checked_at, applied_at) >= NOW() - %s * INTERVAL '1 day'"
    " GROUP BY 1")
_L5_REFUSED_SQL = (
    "SELECT issue_label, last_outcome FROM brain_issue_persistence"
    " WHERE last_outcome = ANY(%s)"
    "   AND last_seen_at >= NOW() - %s * INTERVAL '1 day'")
_AGENT_SQL = (
    "SELECT finding_key, agent_state, COALESCE(agent_summary, reason)"
    "  FROM squasher_work_queue"
    " WHERE agent_state IN ('fixed', 'merged_unverified', 'pr_closed',"
    "                       'needs_human')"
    "   AND COALESCE(agent_finished_at, requested_at)"
    "       >= NOW() - %s * INTERVAL '1 day'")
_REJECT_SQL = (
    "SELECT issue_label, reviewer_note FROM brain_review_decisions"
    " WHERE decision = 'reject'"
    "   AND decided_at >= NOW() - %s * INTERVAL '1 day'")

_AGENT_OUTCOME = {"fixed": WORKED, "merged_unverified": FAILED,
                  "needs_human": NEEDS_HUMAN}
# ★ pr_closed is NOT mapped here: the same close is written to
# brain_review_decisions by squasher_agent_lane._record_rejection, and
# counting it from both tables would double every human "no".


def outcome_coverage(by_kind: dict | None, seen: dict) -> dict | None:
    """PURE. How many graded outcomes the joins actually reached.

    by_kind: {proposal_kind: graded rows in window} or None (unreadable).
    None when unmeasurable; `unjoined_kinds` names any graded kind that no
    source reads — the shape of the gap this was added to expose.
    """
    if by_kind is None:
        return None
    total = sum(by_kind.values())
    joined_by = {"code": seen.get("l5_fix_outcomes"),
                 "autopilot": seen.get("autopilot_fix_outcomes")}
    joined = sum(v or 0 for v in joined_by.values())
    return {"graded_total": total, "joined": joined,
            "pct": round(100.0 * joined / total, 1) if total else None,
            "by_kind": dict(by_kind),
            "unjoined_kinds": sorted(k for k in by_kind if k not in joined_by)}


def read_events(cur, days: int = WINDOW_DAYS) -> tuple[list, dict]:
    """(events, per-source row counts). A source that could not be read is
    reported as None — unmeasured, never 0."""
    events, seen = [], {}
    rows = _rows(cur, _L5_OUTCOMES_SQL, (days,))
    seen["l5_fix_outcomes"] = None if rows is None else len(rows)
    for fam, broken, fp, note in rows or []:
        events.append({"family": fam, "outcome": FAILED if broken else WORKED,
                       "file": fp, "note": note if broken else ""})
    rows = _rows(cur, _AUTOPILOT_OUTCOMES_SQL, (days,))
    seen["autopilot_fix_outcomes"] = None if rows is None else len(rows)
    for fam, broken, pattern, note in rows or []:
        events.append({"family": fam, "outcome": FAILED if broken else WORKED,
                       "file": f"autopilot:{pattern}" if pattern else "",
                       "note": note if broken else ""})
    rows = _rows(cur, _L5_REFUSED_SQL, (list(_PERMAFAIL), days))
    seen["l5_guard_refusals"] = None if rows is None else len(rows)
    for label, oc in rows or []:
        mapped = _GUARD_OUTCOME.get(oc)
        if mapped:
            events.append({"family": label, "outcome": mapped, "note": oc})
    rows = _rows(cur, _AGENT_SQL, (days,))
    seen["squasher_agent"] = None if rows is None else len(rows)
    for key, state, note in rows or []:
        oc = _AGENT_OUTCOME.get(state)
        if oc:
            events.append({"family": key, "outcome": oc,
                           "note": note if oc == FAILED else ""})
    rows = _rows(cur, _REJECT_SQL, (days,))
    seen["human_rejections"] = None if rows is None else len(rows)
    for label, note in rows or []:
        events.append({"family": label, "outcome": REJECTED, "note": note})
    return events, seen


# ═══════════════════════════════════════════════════════════════════════
# store
# ═══════════════════════════════════════════════════════════════════════
_DDL = """
CREATE TABLE IF NOT EXISTS brain_lessons (
    family       TEXT PRIMARY KEY,
    verdict      TEXT NOT NULL,
    graded       INTEGER NOT NULL DEFAULT 0,
    success_rate REAL,
    counts       JSONB NOT NULL DEFAULT '{}'::jsonb,
    guidance     TEXT NOT NULL DEFAULT '',
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    compiled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)"""


def refresh(days: int = WINDOW_DAYS) -> dict:
    """Read every source, compile, replace brain_lessons. Never raises."""
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no db connection"}
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
            events, seen = read_events(cur, days)
            kinds = _rows(cur, _GRADED_TOTAL_SQL, (days,))
            coverage = outcome_coverage(
                None if kinds is None else {k: n for k, n in kinds}, seen)
            if all(v is None for v in seen.values()):
                # ★ Every source unreadable is NOT "nothing learned": keep the
                # last good pages rather than truncating them to empty.
                c.rollback()
                return {"ok": False, "sources": seen,
                        "error": "every source unreadable — kept last pages"}
            lessons = compile_lessons(events)
            cur.execute("DELETE FROM brain_lessons")
            for fam, l in lessons.items():
                cur.execute("""
                    INSERT INTO brain_lessons (family, verdict, graded,
                        success_rate, counts, guidance, detail, compiled_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, NOW() ON CONFLICT DO NOTHING)
                    ON CONFLICT (family) DO UPDATE SET
                        verdict = EXCLUDED.verdict, graded = EXCLUDED.graded,
                        success_rate = EXCLUDED.success_rate,
                        counts = EXCLUDED.counts, guidance = EXCLUDED.guidance,
                        detail = EXCLUDED.detail, compiled_at = NOW()""",
                    (fam, l["verdict"], l["graded"], l["success_rate"],
                     json.dumps(l["counts"]), l["guidance"],
                     json.dumps({"files_failed": l["files_failed"],
                                 "files_worked": l["files_worked"]})))
        c.commit()
    except Exception as e:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            c.close()
        except Exception:
            pass
    _CACHE.clear()
    by_verdict: dict = {}
    for l in lessons.values():
        by_verdict[l["verdict"]] = by_verdict.get(l["verdict"], 0) + 1
    return {"ok": True, "window_days": days, "sources": seen,
            "outcome_coverage": coverage,
            "events": len(events), "families": len(lessons),
            "by_verdict": by_verdict,
            "with_guidance": sum(1 for l in lessons.values() if l["guidance"])}


# ═══════════════════════════════════════════════════════════════════════
# the read path every agent uses
# ═══════════════════════════════════════════════════════════════════════
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _load_guidance() -> dict:
    """{family: guidance} from brain_lessons, cached for _CACHE_TTL_S.

    L5 asks once per candidate issue inside one pass; without the cache every
    one of those is a round-trip to the pooler.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get("g")
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    data: dict = {}
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT family, guidance FROM brain_lessons"
                            " WHERE guidance <> ''")
                data = {f: g for f, g in cur.fetchall()}
        except Exception:
            data = {}
        finally:
            try:
                c.close()
            except Exception:
                pass
    with _CACHE_LOCK:
        _CACHE["g"] = (now, data)
    return data


def format_hint(family: str, guidance: str) -> str:
    if not family or not guidance:
        return ""
    return (f"\n\nLESSONS FROM PAST ATTEMPTS ON `{family}` (compiled from "
            f"verified outcomes and human reviews — weigh these before "
            f"proposing):\n{guidance}\n")


def lessons_for(label) -> str:
    """Hint text for one finding label, or "". NEVER raises.

    Fails OPEN to "" — a lessons outage must not change what a proposer does.
    """
    if _disabled():
        return ""
    try:
        fam = family_of(label)
        if not fam:
            return ""
        return format_hint(fam, _load_guidance().get(fam, ""))
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════
# endpoints
# ═══════════════════════════════════════════════════════════════════════
def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key") or "").strip()
    return bool(expected) and bool(got) and hmac.compare_digest(got, expected)


@brain_lessons_bp.route("/api/v1/admin/brain/lessons/compile", methods=["POST"])
def brain_lessons_compile():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    try:
        days = max(7, min(365, int(request.args.get("days") or WINDOW_DAYS)))
    except ValueError:
        days = WINDOW_DAYS
    return jsonify(refresh(days)), 200


@brain_lessons_bp.route("/api/v1/admin/brain/lessons", methods=["GET"])
def brain_lessons_read():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    fam = family_of(request.args.get("family") or "")
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no db connection"}), 200
    try:
        with c.cursor() as cur:
            sql = ("SELECT family, verdict, graded, success_rate, counts,"
                   " guidance, detail, compiled_at FROM brain_lessons")
            args = None
            if fam:
                sql += " WHERE family = %s"
                args = (fam,)
            sql += " ORDER BY graded DESC, family LIMIT 200"
            cur.execute(sql, args) if args else cur.execute(sql)
            rows = [{"family": r[0], "verdict": r[1], "graded": r[2],
                     "success_rate": r[3], "counts": r[4], "guidance": r[5],
                     "detail": r[6],
                     "compiled_at": r[7].isoformat() if r[7] else None}
                    for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify({"ok": True, "count": len(rows), "lessons": rows,
                    "generated_at": datetime.now(timezone.utc).isoformat()}), 200
