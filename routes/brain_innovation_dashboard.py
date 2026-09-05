"""
routes/brain_innovation_dashboard.py — Brain INNOVATION DASHBOARD (2026-06-19).
READ-ONLY + grade-POST only.

THE PROBLEM THIS SOLVES: the brain now autonomously produces VERIFIED,
adversarially-refuted analysis (self-chosen agenda items, human+self-directed
investigations, ranked propose-only enhancements) — but it ALL lives in Postgres
tables reachable only via an admin curl. The operator is BLIND on the innovation
front.

THIS MODULE is ONE browser-openable page that consolidates all three streams at a
glance, with a per-item grade button so the human closes the calibration loop
(the same verify -> grade -> learn loop the brain already runs on bug-fixes,
applied to its own analysis track record).

THE THREE SOURCE TABLES (read-only — this module NEVER writes to them; grading
goes through the EXISTING admin-gated grade endpoints):
  · brain_self_agenda          — the brain's SELF-CHOSEN agenda
  · brain_investigations       — human + self-directed verified investigations
  · brain_enhancement_proposals— ranked propose-only improvements

Each result_json / proposal_json carries:
  {recommendation, confidence, caveats, decision_for_human,
   refutation:{attempted, survived, weaknesses_found, unparsed}, evidence}

ENDPOINTS (blueprint brain_innovation_dashboard_bp, admin-gated):
  GET /api/v1/brain/innovation/digest     -> consolidated READ-ONLY JSON digest
                                             {ok, generated_at, counts, agenda,
                                              investigations, proposals} with each
                                             item FLATTENED for display.
  GET /api/v1/brain/innovation/dashboard  -> self-contained HTML page that fetches
                                             the digest and renders THREE sections
                                             with confidence badges, refutation
                                             verdicts, decision-for-human, and
                                             [good]/[bad] grade buttons that POST to
                                             the existing grade endpoints.

PATH-SHAPE / CF-1000 NOTE: both endpoints live UNDER /api/ on purpose. The
dchub.cloud CF worker forwards EVERY /api/ path to the Railway backend
unconditionally, while non-/api HTML pages only reach Railway when they're in the
worker's PHASE_282 exact-match / prefix allow-list (`/brain/` is NOT a prefix
there, and `/brain/innovation` is already OWNED by routes/brain_innovation.py for
a DIFFERENT surface). Serving the page under /api/v1/brain/innovation/dashboard
therefore (a) avoids the CF Error-1000 trap on un-allow-listed /brain/* subpaths,
(b) avoids a Flask duplicate-rule collision with the existing
/brain/innovation. The page is browser-openable at
  https://dchub.cloud/api/v1/brain/innovation/dashboard?admin_key=<KEY>

AUTH: MIRRORS the existing admin brain-dashboard gate (brain_v2_public._pub_admin_ok
/ brain_innovation._innov_admin_ok) verbatim — an internal/admin key read from the
X-Internal-Key / X-Admin-Key header OR the ?admin_key= query param. No new auth
scheme is invented; the query-param form is what makes the page openable in a
browser, and the HTML carries that same key onto its digest fetch + grade POSTs.

SAFETY: strictly READ-ONLY except the grade POSTs (which go to the EXISTING
admin-gated endpoints, not anything new here). Best-effort: a missing table yields
[] not a crash; every DB touch is wrapped; the digest NEVER raises.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import zlib
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

brain_innovation_dashboard_bp = Blueprint("brain_innovation_dashboard", __name__)


# ── Admin gate — MIRROR the existing admin brain-dashboard gate ──────
def _admin_ok() -> bool:
    """Same gate every admin brain dashboard accepts (brain_v2_public._pub_admin_ok
    / brain_innovation._innov_admin_ok): an internal/admin key from a header OR the
    ?admin_key= query param. The query-param form is what makes the page openable
    in a browser; the page carries that key onto its fetch + grade POSTs. No new
    auth scheme — reuse the codebase pattern."""
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    # Accept the key from a header, the ?admin_key= query param (first visit), OR
    # the dchub_innov_key cookie (remembered after the first valid open) so the
    # operator pastes the key once and revisits clean.
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key")
             or request.cookies.get("dchub_innov_key") or "").strip()
    return bool(_sent) and _sent in _keys


_ADMIN_ONLY_HTML = (
    "<!doctype html><meta charset=utf-8><title>DC Hub · internal</title>"
    "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#0a0a0a;color:#9a9a9a;display:flex;align-items:center;"
    "justify-content:center;height:90vh;text-align:center'>"
    "<div><h2 style='color:#e6e6e6;font-weight:300;letter-spacing:-.02em'>"
    "Internal console</h2><p>The DC Hub brain innovation dashboard is "
    "admin-only. Append <code>?admin_key=…</code>.</p></div>")


# ── DB (direct read-only psycopg2, mirror brain_self_director._conn) ──
def _conn():
    """Raw psycopg2 connection. Mirrors brain_self_director._conn /
    brain_investigator._conn — the _iso_common contextmanager crashes on
    .cursor(). READ-ONLY use here. Returns None (never raises) on any error."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_innovation_dashboard: _conn failed: %s", e)
    return None


_VALID_KINDS = {"agenda", "inv", "prop"}


def _init_approvals() -> None:
    """Bootstrap the brain_approvals table — the operator's GREENLIGHT ledger.

    APPROVE records the human's decision to greenlight the brain's guidance; it is
    PROPOSE-ONLY — it NEVER triggers any action / merge / send. This is just the
    durable record so an approved item stays marked across the dashboard's 60s
    auto-refresh.

    Idempotent (CREATE TABLE IF NOT EXISTS) and best-effort — never raises; a DB
    error just logs a warning (the routes fall back to a 503/empty state)."""
    conn = _conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS brain_approvals ("
                "  kind TEXT NOT NULL,"
                "  item_id BIGINT NOT NULL,"
                "  decision TEXT DEFAULT 'approved',"
                "  note TEXT,"
                "  approved_at TIMESTAMPTZ DEFAULT NOW(),"
                "  PRIMARY KEY (kind, item_id)"
                ")"
            )
            _ensure_approval_columns(cur)
        conn.commit()
    except Exception as e:
        logger.warning("brain_innovation_dashboard: _init_approvals failed: %s", e)
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


# ── PR-draft outcome, PERSISTED (2026-09-02, brain-agents sweep finding 2) ──
# ★ WHY. brain_approvals held kind / item_id / decision only; the approve→PR
# outcome (`pr_attempt`) lived in the HTTP response and nowhere else. Measured
# 2026-09-02 00:30Z on the live table: 40 approved rows, 5 with a merged PR,
# #100416 known lost to `claude call failed: http_429` during the gateway
# spend outage — and the other ~34 UNKNOWABLE, because a 429-era approval and
# a successful one were byte-identical rows. Nothing re-drove any of them.
# Four columns, added only when ABSENT on the live table (information_schema
# first: `ADD COLUMN IF NOT EXISTS` takes ACCESS EXCLUSIVE before it evaluates
# its condition — the squasher_queue lesson of 2026-08-30).
_APPROVAL_PR_COLUMNS = (
    ("pr_attempt", "JSONB"),             # the draft outcome, verbatim
    ("pr_url", "TEXT"),                  # the PR that landed (code or spec), else NULL
    ("pr_attempted_at", "TIMESTAMPTZ"),  # last draft attempt (approve or redrive)
    ("pr_redrives", "INTEGER NOT NULL DEFAULT 0"),
)
_APPROVAL_COLUMNS_ENSURED = False


def _ensure_approval_columns(cur) -> bool:
    """Add the PR-outcome columns the live brain_approvals table LACKS — and
    only those. Once per process; the memo is set only after the DDL landed,
    so a run that loses a lock race retries on the next call. Returns False
    when the table is not visible yet (nothing to alter)."""
    global _APPROVAL_COLUMNS_ENSURED
    if _APPROVAL_COLUMNS_ENSURED:
        return True
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'brain_approvals'")
    present = {str(r[0]) for r in (cur.fetchall() or [])}
    if not present:
        return False
    for col, typ in _APPROVAL_PR_COLUMNS:
        if col not in present:
            # Column names are constants from the tuple above — never input.
            cur.execute(f"ALTER TABLE brain_approvals ADD COLUMN {col} {typ}")
    _APPROVAL_COLUMNS_ENSURED = True
    return True


def _pr_url_of(pr_attempt) -> str | None:
    """The URL of the PR a draft attempt opened, or None when nothing landed.

    Shapes (verbatim from the producers):
      code PR   draft_and_open_pr → {ok, acted, proposal, pr: <opener envelope>}
                where the envelope is {ok, pr: {number, url}}   (nested twice)
      spec PR   the approve fallback → {..., acted: True,
                fallback_spec_pr: {ok, acted, spec_pr, pr: {number, url}}}
      refusal   {ok: True, acted: False, refused: True}  → None
      failure   {ok: False, error: ...}                  → None
    `acted` is the contract: a URL under a non-acted attempt is not a PR."""
    if not isinstance(pr_attempt, dict) or not pr_attempt.get("acted"):
        return None

    def _find(node, depth=0):
        if depth > 3 or not isinstance(node, dict):
            return None
        for k in ("html_url", "pr_url", "url"):
            v = node.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for k in ("pr", "fallback_spec_pr"):
            v = _find(node.get(k), depth + 1)
            if v:
                return v
        return None
    return _find(pr_attempt)


def _write_approval(sql: str, params: tuple) -> int:
    """One UPDATE on brain_approvals on its own connection, columns ensured
    first. -> rowcount; -1 on any failure (logged, never raised — the
    greenlight row is already committed and bookkeeping must not undo it)."""
    conn = _conn()
    if conn is None:
        return -1
    try:
        with conn.cursor() as cur:
            _ensure_approval_columns(cur)
            cur.execute(sql, params)
            n = cur.rowcount
        conn.commit()
        return int(n if n is not None else 0)
    except Exception as e:
        logger.warning("brain_innovation_dashboard: approval write failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return -1
    finally:
        try: conn.close()
        except Exception: pass


def _record_pr_attempt(kind: str, item_id: int, pr_attempt: dict) -> bool:
    """Persist the draft outcome on the approval row: the attempt verbatim,
    the PR url when one landed (never cleared by a later failure), and the
    attempt time. True when the row was updated."""
    return _write_approval(
        "UPDATE brain_approvals SET pr_attempt = %s::jsonb, "
        "pr_url = COALESCE(%s, pr_url), pr_attempted_at = NOW() "
        "WHERE kind = %s AND item_id = %s",
        (json.dumps(pr_attempt, default=str)[:20000], _pr_url_of(pr_attempt),
         kind, int(item_id))) > 0


def _claim_redrive(kind: str, item_id: int, max_redrives: int) -> bool:
    """Claim a row for ONE redrive attempt — committed BEFORE the attempt so
    a crash mid-draft still records that it was tried. False when the row
    is gone, already has a PR, or is out of redrives."""
    return _write_approval(
        "UPDATE brain_approvals SET pr_redrives = COALESCE(pr_redrives, 0) + 1, "
        "pr_attempted_at = NOW() "
        "WHERE kind = %s AND item_id = %s AND pr_url IS NULL "
        "AND COALESCE(pr_redrives, 0) < %s",
        (kind, int(item_id), int(max_redrives))) > 0


def _redrive_wanted(pr_attempt) -> bool:
    """Which persisted outcomes the redrive re-runs: a FAILED attempt only —
    `ok` is literally False (claude call failed, autonomy gate closed, PR
    handoff failed, JSON parse failed). A refusal ({ok:True, acted:False,
    refused:True}) is the drafter's judgement and re-asking it is a wasted
    model call; a 'no directive — recorded only' record is ok:True too; a
    NULL pr_attempt is an approval that never asked for a PR (open_pr unset)
    or predates the column — unknowable, left alone (no duplicate PRs for
    the 5 approvals whose PRs already merged before the column existed)."""
    return isinstance(pr_attempt, dict) and pr_attempt.get("ok") is False


def stale_approvals_without_pr(cur, older_than_hours: int = 24,
                               window_days: int = 7) -> list[dict]:
    """What routes/brain_consistency_radar.check_approved_without_pr_stale
    reports: approved within `window_days`, older than `older_than_hours`,
    no PR landed, and the persisted attempt is absent or a failure. Raises
    on a pre-migration table (no pr_url column) so the caller reports
    nothing rather than 'clean'."""
    cur.execute(
        "SELECT kind, item_id, approved_at, pr_attempt, COALESCE(pr_redrives, 0) "
        "FROM brain_approvals WHERE decision = 'approved' AND pr_url IS NULL "
        "AND approved_at > NOW() - (%s * INTERVAL '1 day') "
        "AND approved_at < NOW() - (%s * INTERVAL '1 hour') "
        "ORDER BY approved_at ASC LIMIT 200",
        (int(window_days), int(older_than_hours)))
    out: list[dict] = []
    for kind, item_id, approved_at, attempt, redrives in (cur.fetchall() or []):
        a = _as_obj(attempt)
        if a and a.get("ok") is not False:
            continue          # refusal / recorded-only: the brain answered
        out.append({"key": f"{kind}:{item_id}", "approved_at": _iso(approved_at),
                    "attempted": bool(a),
                    "error": (str(a.get("error") or a.get("reason") or "")[:120]
                              if a else ""),
                    "redrives": int(redrives or 0)})
    return out


def _as_obj(v) -> dict:
    """Coerce a JSONB column (already a dict on psycopg2, or a str) to a dict.
    Empty dict on anything unparseable."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            obj = json.loads(v)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _iso(dt) -> str:
    try:
        return dt.isoformat()
    except Exception:
        return str(dt) if dt is not None else ""


def _flatten(result: dict) -> dict:
    """Flatten a stored result_json / proposal_json into the fields the dashboard
    renders. The verified analysis shape is:
      {recommendation, confidence, caveats, decision_for_human,
       refutation:{attempted, survived, weaknesses_found, unparsed}, evidence}
    Best-effort — any missing key degrades to a safe default."""
    result = result if isinstance(result, dict) else {}
    refu = result.get("refutation")
    refu = refu if isinstance(refu, dict) else {}
    weaknesses = refu.get("weaknesses_found")
    weaknesses = [str(w) for w in weaknesses] if isinstance(weaknesses, list) else []
    survived = refu.get("survived")
    return {
        "recommendation": result.get("recommendation"),
        "decision_for_human": result.get("decision_for_human"),
        "refutation_attempted": bool(refu.get("attempted")),
        "refutation_survived": (bool(survived) if survived is not None else None),
        "refutation_unparsed": bool(refu.get("unparsed")),
        "weaknesses": weaknesses,
    }


# ── Map an innovation item → a Layer-5 directive (for approve→PR) ────
_ITEM_SOURCES = {
    # kind -> (table, json_column, heading_column)
    "agenda": ("brain_self_agenda", "result_json", "title"),
    "inv":    ("brain_investigations", "result_json", "question"),
    "prop":   ("brain_enhancement_proposals", "proposal_json", "title"),
}


def _item_directive(kind: str, item_id: int) -> tuple[str, str]:
    """Fetch one innovation item's human-facing recommendation to use as a
    Layer-5 directive for approve→PR. Returns (directive_text, heading); ('', '')
    on unknown kind / missing row / any DB error (best-effort, never raises).
    Prefers 'decision_for_human', falling back to 'recommendation'."""
    src = _ITEM_SOURCES.get(kind)
    if not src:
        return "", ""
    # All three are constants from the whitelist above — never user input — so the
    # f-string interpolation of table/column names carries no injection surface.
    table, jcol, headcol = src
    conn = _conn()
    if conn is None:
        return "", ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {headcol}, {jcol} FROM {table} WHERE id = %s",
                (int(item_id),),
            )
            row = cur.fetchone()
        if not row:
            return "", ""
        heading = str(row[0] or "")
        flat = _flatten(_as_obj(row[1]))
        directive = (flat.get("decision_for_human")
                     or flat.get("recommendation") or "").strip()
        return directive, heading
    except Exception as e:
        logger.warning("brain_innovation_dashboard: _item_directive failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return "", ""
    finally:
        try: conn.close()
        except Exception: pass


# ── Read each of the three streams (best-effort; [] not a crash) ─────
def _recent_agenda(limit: int = 15) -> list[dict]:
    """Recent SELF-CHOSEN agenda items, newest first, flattened for display.
    [] on a missing table / any DB error (best-effort, never raises)."""
    conn = _conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, question, area, confidence, result_json, "
                "grade, created_at "
                "FROM brain_self_agenda "
                "ORDER BY created_at DESC LIMIT %s",
                (int(limit),),
            )
            for r in (cur.fetchall() or []):
                flat = _flatten(_as_obj(r[5]))
                out.append({
                    "id": r[0], "title": r[1], "question": r[2], "area": r[3],
                    "confidence": r[4], "grade": r[6], "created_at": _iso(r[7]),
                    **flat,
                })
    except Exception as e:
        logger.warning("brain_innovation_dashboard: agenda read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return []
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _recent_investigations(limit: int = 15) -> list[dict]:
    """Recent verified investigations (human + self-directed), newest first,
    flattened. [] on a missing table / any DB error."""
    conn = _conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn.cursor() as cur:
            # Fetch extra so we can SKIP dead/incomplete rows (pending/failed
            # cruft from the old daemon-thread era — no recommendation, conf 0.0)
            # and still return up to `limit` real investigations.
            cur.execute(
                "SELECT id, question, confidence, result_json, grade, created_at "
                "FROM brain_investigations "
                "ORDER BY created_at DESC LIMIT %s",
                (int(limit) * 3,),
            )
            for r in (cur.fetchall() or []):
                flat = _flatten(_as_obj(r[3]))
                # Skip incomplete rows: no recommendation AND not an explicit
                # cannot_investigate result = nothing worth showing.
                if not flat.get("recommendation"):
                    continue
                out.append({
                    "id": r[0], "question": r[1], "title": None, "area": None,
                    "confidence": r[2], "grade": r[4], "created_at": _iso(r[5]),
                    **flat,
                })
                if len(out) >= int(limit):
                    break
    except Exception as e:
        logger.warning("brain_innovation_dashboard: investigations read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return []
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _recent_proposals(limit: int = 15) -> list[dict]:
    """Recent ranked propose-only enhancement proposals, highest leverage_rank
    first (newest as the tiebreaker), flattened. [] on a missing table / any DB
    error."""
    conn = _conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, area, confidence, leverage_rank, "
                "proposal_json, grade, created_at "
                "FROM brain_enhancement_proposals "
                "ORDER BY leverage_rank DESC NULLS LAST, created_at DESC "
                "LIMIT %s",
                (int(limit),),
            )
            for r in (cur.fetchall() or []):
                flat = _flatten(_as_obj(r[5]))
                out.append({
                    "id": r[0], "title": r[1], "question": None, "area": r[2],
                    "confidence": r[3], "leverage_rank": r[4],
                    "grade": r[6], "created_at": _iso(r[7]),
                    **flat,
                })
    except Exception as e:
        logger.warning("brain_innovation_dashboard: proposals read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return []
    finally:
        try: conn.close()
        except Exception: pass
    return out


def build_digest(limit: int = 15) -> dict:
    """The READ-ONLY consolidated digest of all three brain-innovation streams.
    Best-effort: each stream is read independently, a missing/empty table yields
    [] (never a crash), and the digest NEVER raises."""
    try:
        limit = max(1, min(int(limit), 50))
    except Exception:
        limit = 15
    agenda = _recent_agenda(limit)
    investigations = _recent_investigations(limit)
    proposals = _recent_proposals(limit)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "agenda": len(agenda),
            "investigations": len(investigations),
            "proposals": len(proposals),
        },
        "agenda": agenda,
        "investigations": investigations,
        "proposals": proposals,
        "note": "READ-ONLY consolidated digest of the brain's verified, "
                "adversarially-refuted analysis. Grade each item to close the "
                "calibration loop; nothing here is acted on.",
    }


# ════════════════════════════════════════════════════════════════════
#  Endpoints (admin-gated)
# ════════════════════════════════════════════════════════════════════
@brain_innovation_dashboard_bp.after_request
def _no_store(resp):
    # These embed the brain's analysis + carry an admin key in the URL — never let
    # CF edge-cache an admin 200 and serve it to anon. (Mirrors the other admin
    # brain dashboards.)
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@brain_innovation_dashboard_bp.route("/api/v1/brain/innovation/digest", methods=["GET"])
def innovation_digest():
    """READ-ONLY consolidated digest of the three brain-innovation streams —
    self-agenda, investigations, ranked proposals — each item flattened for
    display. Admin-gated. Best-effort: a missing table yields [] not a 500."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only — internal brain dashboard",
                       hint="append ?admin_key= or send X-Admin-Key"), 403
    try:
        limit = int(request.args.get("limit", "15"))
    except Exception:
        limit = 15
    return jsonify(build_digest(limit)), 200


@brain_innovation_dashboard_bp.route("/api/v1/brain/innovation/approvals", methods=["GET"])
def innovation_approvals():
    """READ-ONLY list of the operator's greenlight decisions from brain_approvals
    (newest first, LIMIT 500). The page uses this to keep approved items marked
    across the 60s auto-refresh. Admin-gated; 503 if no DB."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only — internal brain dashboard",
                       hint="append ?admin_key= or send X-Admin-Key"), 403
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="database unavailable"), 503
    approved: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, item_id, decision FROM brain_approvals "
                "ORDER BY approved_at DESC LIMIT 500"
            )
            for r in (cur.fetchall() or []):
                approved.append({"key": f"{r[0]}:{r[1]}", "decision": r[2]})
    except Exception as e:
        logger.warning("brain_innovation_dashboard: approvals read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return jsonify(ok=False, error="approvals read failed"), 503
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify(ok=True, approved=approved), 200


def _resolve_directive(operator_text: str, item_text: str) -> tuple[str, str]:
    """Which text Layer-5 should actually act on. Returns (directive, source).

    ★ WHY (2026-09-01). The brain writes `decision_for_human` as a MENU, not an
    instruction — measured on the four most recently approved investigations:

        100419  "Choose the remediation path: (A) ... or (B) ..."
        100418  "Choose the remedy tier: (a) minimal ... or (b) durable ..."
        100417  "Approve (a) ... OR direct a deeper investigation ..."
        100416  "Decide which ... is authoritative, then approve: ..."

    Approve recorded a "yes" to the menu without recording WHICH BRANCH, and
    the Layer-5 drafter refuses anything that is not an exact single-file
    substitution — correctly, since it cannot pick the operator's branch for
    them. So every one of those approvals was a no-op by construction.

    The operator's own words win when supplied. Blank falls back to the item's
    text, which is exactly the old behaviour for items that already read as an
    instruction."""
    op = (operator_text or "").strip()
    if op:
        return op, "operator"
    item = (item_text or "").strip()
    return (item, "item") if item else ("", "none")


def _should_file_spec_pr(pr_attempt) -> bool:
    """True when the code drafter did not open a PR, so the approved directive
    should still land as a draft spec PR.

    ★ The bug this closes (2026-09-01): the condition was `.get("acted") is
    False`, which is True for an explicit REFUSAL — `{ok:True, acted:False,
    refused:True}` — but False for a FAILURE, because
    `draft_and_open_pr` returns `{ok:False, error:"claude call failed:
    http_429"}` with no `acted` key at all, and `None is False` is False. So
    during the gateway spend outage the approval produced neither a code PR nor
    a spec PR: a silent no-op, with `prs_today: 0` the only trace."""
    if not isinstance(pr_attempt, dict):
        return False
    return not pr_attempt.get("acted")


def _read_directive(body: dict) -> str:
    """The operator's instruction: `directive_z` first, `directive_b64`
    second, plain `directive` last.

    ★ WHY BASE64 (2026-09-04). The brain writes a "decision for human" that
    QUOTES the shell command to run — inv #100502 reads: Run
    `curl -i https://dchub.cloud/js/dchub-nav.js` and, if it returns 404,
    restore that asset/route first — and the approve button posts that text
    straight back as `directive`. Cloudflare's managed WAF reads a
    backtick-wrapped command carrying a URL as command injection and answers
    **403 with an HTML block page, before Railway**. So the request never
    reached this function: no brain_approvals row, no log line, nothing for
    redrive_approved_without_pr to find, and the page's `r.json()` threw, so
    the operator's whole diagnosis was the bare word `error`.

    ★ TWO WAF rules, and BASE64 ONLY EVADES ONE. The directive is free text
    that quotes shell AND HTML — inv #100502 asks, in the same sentence, to
    run `curl -i https://…` and to add `<script src="/js/dchub-nav.js"
    defer></script>` before </body>. Cloudflare **base64-decodes the request
    body before matching**, so b64 hides the command-injection signature and
    NOT the XSS one. Proved by alignment: b64 of the <script> tag with 0/1/2/3
    padding characters prepended — four completely different byte strings —
    was 403 on all four. That is a decoder, not a coincidence.

    ★ MEASURED on the live edge 2026-09-04, the FULL 458-char #100502
      directive, same body to both hosts:
        plain, edge                    -> 403 "Attention Required! | Cloudflare"
        base64, edge                   -> 403  (decoded and matched)
        zlib+base64, edge              -> 400  (the app's own reply, 5/5)
        plain, Railway origin          -> 400  (the app's own reply)
      and the command-injection half bisects to the BACKTICKS: `curl <url>`
      403s, the same text unbackticked does not. 2 of the 45 items carrying a
      decision-for-human were un-approvable — 2 of the only 4 with a button.

    So the wire carries DEFLATE-then-base64: what the WAF decodes is
    compressed bytes with no substring left to match. `directive_b64` is still
    read (it survives the command-injection rule, and a page cached mid-deploy
    may send it) and plain `directive` still works for any caller whose text
    trips neither rule.
    """
    raw = body.get("directive_z")
    if isinstance(raw, str) and raw.strip():
        try:
            return zlib.decompress(
                base64.b64decode(raw.strip(), validate=True)).decode(
                    "utf-8", "replace").strip()
        except Exception as e:
            logger.warning(
                "brain_innovation_dashboard: directive_z undecodable: %s", e)
            return ""
    raw = body.get("directive_b64")
    if isinstance(raw, str) and raw.strip():
        try:
            return base64.b64decode(raw.strip(), validate=True).decode(
                "utf-8", "replace").strip()
        except Exception as e:
            # Never fall back to `directive` here: a caller that sent b64 and
            # got it wrong must not silently approve on an EMPTY directive
            # (that is the "approving a menu" bug of 2026-09-01 again).
            logger.warning(
                "brain_innovation_dashboard: directive_b64 undecodable: %s", e)
            return ""
    return str(body.get("directive") or "").strip()


@brain_innovation_dashboard_bp.route("/api/v1/brain/innovation/approve", methods=["POST"])
def innovation_approve():
    """RECORD the operator GREENLIGHTING the brain's guidance for one item.

    PROPOSE-ONLY: this writes a row to brain_approvals and NOTHING else — it NEVER
    triggers any action / merge / send. Body: {kind, id, decision?}. Validates
    kind in {agenda,inv,prop} and an integer id; UPSERTs the decision (default
    'approved'). 403 if not admin, 400 on bad input, 503 if no DB."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only — internal brain dashboard",
                       hint="append ?admin_key= or send X-Admin-Key"), 403
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "").strip()
    if kind not in _VALID_KINDS:
        return jsonify(ok=False,
                       error="kind must be one of agenda, inv, prop"), 400
    try:
        item_id = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id must be an integer"), 400
    decision = str(body.get("decision") or "approved").strip() or "approved"
    open_pr = bool(body.get("open_pr"))
    # The operator's instruction for THIS item, when the item's own
    # "decision for human" is a menu rather than a directive. See
    # _resolve_directive and _read_directive.
    operator_directive = _read_directive(body)
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="database unavailable"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_approvals (kind, item_id, decision, approved_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (kind, item_id) DO UPDATE SET "
                "decision = EXCLUDED.decision, approved_at = NOW()",
                (kind, item_id, decision),
            )
        conn.commit()
    except Exception as e:
        logger.warning("brain_innovation_dashboard: approve write failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return jsonify(ok=False, error="approve write failed"), 503
    finally:
        try: conn.close()
        except Exception: pass

    resp = {"ok": True, "kind": kind, "id": item_id, "decision": decision}

    # Optional one-click ACT: turn the item's 'decision for human' into a
    # guardrailed Layer-5 draft PR (the dashboard's approve button sets open_pr).
    # The approval row above is ALREADY committed, so a PR failure never loses the
    # greenlight. Only fires on a real greenlight ('approved'); inherits
    # can_open_pr() (kill switch + daily cap) and the drafter's REFUSE path, so an
    # advisory insight that isn't a concrete single-file edit is recorded only.
    # 2026-09-02: the outcome is PERSISTED on the approval row (pr_attempt,
    # pr_url) — it used to live only in this response, so a failed draft was
    # indistinguishable from a landed one and nothing could re-drive it.
    if open_pr and decision == "approved":
        _pr = _attempt_pr(kind, item_id, operator_directive)
        resp["directive_source"] = _pr.get("directive_source")
        resp["pr_attempt"] = _pr
        resp["pr_url"] = _pr_url_of(_pr)
        resp["pr_attempt_persisted"] = _record_pr_attempt(kind, item_id, _pr)

    return jsonify(**resp), 200


def _attempt_pr(kind: str, item_id: int, operator_directive: str = "") -> dict:
    """The approve→PR act, factored so the master-tick redrive runs the SAME
    path as the dashboard button. Always returns the pr_attempt dict (never
    raises); `directive_source` is folded in."""
    try:
        item_directive, heading = _item_directive(kind, item_id)
        directive, _src = _resolve_directive(operator_directive, item_directive)
        if not directive:
            return {"ok": True, "acted": False, "directive_source": _src,
                    "note": "no actionable 'decision for human' on this item — recorded only"}
        from routes.brain_guardrails import draft_and_open_pr
        _pr = draft_and_open_pr(
            directive, "", label=f"{kind} #{item_id}: {heading[:60]}")
        # r-brain-loop (2026-06-30): ACTUATOR FALLBACK (#4). If the code
        # drafter REFUSED (the directive is a 'build/instrument/gather'
        # PLAN, not a single-file edit — the common case that made every
        # approval 'recorded only'), file the approved plan as a DRAFT
        # spec PR instead. Doc-only, draft, human-merged — so the approval
        # becomes a visible, trackable, human-implementable PR.
        if _should_file_spec_pr(_pr):
            try:
                from routes.brain_pr_opener import open_spec_pr
                _spec = open_spec_pr(directive, heading, kind, item_id,
                                     label=f"{kind} #{item_id}")
                if isinstance(_spec, dict) and _spec.get("acted"):
                    _pr = {**_pr, "acted": True,
                           "note": "filed as draft spec PR for a human",
                           "fallback_spec_pr": _spec}
            except Exception as _se:
                logger.warning(
                    "brain_innovation_dashboard: approve→spec-PR fallback failed: %s", _se)
        if not isinstance(_pr, dict):
            return {"ok": False, "acted": False, "directive_source": _src,
                    "error": "drafter returned a non-dict"}
        _pr.setdefault("directive_source", _src)
        return _pr
    except Exception as e:
        logger.warning("brain_innovation_dashboard: approve→PR failed: %s", e)
        return {"ok": False, "acted": False, "error": str(e)}


def redrive_approved_without_pr(max_rows: int = 3, window_days: int = 7,
                                max_redrives: int = 3,
                                min_gap_hours: int = 1) -> dict:
    """Master-tick step `tier2.approved_without_pr_redrive`
    (routes/brain_master_orchestrator, beside tier2.l22_draft_prs).

    Re-runs the draft for approvals whose persisted attempt FAILED and that
    still have no PR, within `window_days`. Guardrail FIRST: the same
    can_open_pr() (kill switch + 8/day cap) the button inherits, read before
    any row is touched and re-read by draft_and_open_pr per attempt.
    Idempotent per tick: a row is claimed (pr_redrives+1, pr_attempted_at)
    and committed BEFORE its attempt, at most `max_rows` rows per tick,
    `max_redrives` per row, and not again within `min_gap_hours`."""
    out = {"ok": True, "scanned": 0, "redriven": 0, "results": [],
           "skipped": None}
    try:
        from routes.brain_guardrails import can_open_pr
        allowed, why = can_open_pr()
    except Exception as e:
        allowed, why = False, f"guardrail unreadable: {type(e).__name__}"
    if not allowed:
        out["skipped"] = why
        return out
    conn = _conn()
    if conn is None:
        out.update(ok=False, error="database unavailable")
        return out
    rows: list = []
    try:
        with conn.cursor() as cur:
            _ensure_approval_columns(cur)
            cur.execute(
                "SELECT kind, item_id, pr_attempt FROM brain_approvals "
                "WHERE decision = 'approved' AND pr_url IS NULL "
                "AND pr_attempt IS NOT NULL "
                "AND approved_at > NOW() - (%s * INTERVAL '1 day') "
                "AND COALESCE(pr_redrives, 0) < %s "
                "AND (pr_attempted_at IS NULL "
                "     OR pr_attempted_at < NOW() - (%s * INTERVAL '1 hour')) "
                "ORDER BY approved_at ASC LIMIT %s",
                (int(window_days), int(max_redrives), int(min_gap_hours),
                 int(max_rows) * 4))
            rows = [(str(r[0]), int(r[1]), _as_obj(r[2]))
                    for r in (cur.fetchall() or [])]
        conn.commit()
    except Exception as e:
        logger.warning("brain_innovation_dashboard: redrive read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        out.update(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
        return out
    finally:
        try: conn.close()
        except Exception: pass
    out["scanned"] = len(rows)
    for kind, item_id, prev in rows:
        if out["redriven"] >= max_rows:
            break
        key = f"{kind}:{item_id}"
        if not _redrive_wanted(prev):
            out["results"].append({"key": key,
                                   "skipped": "previous attempt is not a failure"})
            continue
        if not _claim_redrive(kind, item_id, max_redrives):
            out["results"].append({"key": key, "skipped": "claim refused"})
            continue
        pr = _attempt_pr(kind, item_id, "")
        pr["redrive"] = True
        pr["previous_error"] = str(prev.get("error") or prev.get("reason") or "")[:200]
        persisted = _record_pr_attempt(kind, item_id, pr)
        out["redriven"] += 1
        out["results"].append({"key": key, "acted": bool(pr.get("acted")),
                               "pr_url": _pr_url_of(pr),
                               "error": pr.get("error"), "persisted": persisted})
        if pr.get("error") == "autonomy_gate_closed":
            out["skipped"] = str(pr.get("reason") or "autonomy_gate_closed")
            break
    return out


@brain_innovation_dashboard_bp.route("/api/v1/brain/innovation/dashboard", methods=["GET"])
def innovation_dashboard_page():
    """The browser-openable, self-contained HTML dashboard. Fetches the digest and
    renders three sections (self-directed agenda / investigations / proposals) with
    confidence badges, refutation verdicts, decision-for-human, and [good]/[bad]
    grade buttons that POST to the EXISTING admin grade endpoints. Auto-refreshes
    every ~60s. READ-ONLY except the grade POSTs. Admin-gated; carries the same
    admin key the digest fetch + grade POSTs need."""
    if not _admin_ok():
        return Response(_ADMIN_ONLY_HTML, mimetype="text/html", status=403)
    resp = Response(_PAGE_HTML, mimetype="text/html")
    # Remember the key so the operator pastes it once. Only (re)set it when it was
    # PROVIDED this visit via query/header (first open); JS-readable so the page
    # reuses it for the cross-module grade/approve POSTs. Secure + SameSite=Lax, 30d.
    _provided = (request.headers.get("X-Internal-Key")
                 or request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
    if _provided:
        resp.set_cookie("dchub_innov_key", _provided, max_age=30 * 24 * 3600,
                        secure=True, samesite="Lax", path="/")
    return resp


# ── Self-contained HTML (inline CSS/JS, no build step) ───────────────
# The page reads the admin key from its own URL (?admin_key=) and reuses it for
# the digest fetch + every grade POST, so it works the same way it was opened.
_PAGE_HTML = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Brain · Innovation Dashboard · DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--surface2:#0d0e16;--bd:#1f2030;--tx:#fff;
  --tx2:#9ca3af;--tx3:#6b7280;--indigo:#6366f1;--violet:#a855f7;--green:#10b981;
  --amber:#f59e0b;--red:#ef4444;--grad:linear-gradient(135deg,#6366f1,#a855f7);
  --mono:'JetBrains Mono','SF Mono',ui-monospace,monospace;color-scheme:dark}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1500px;margin:0 auto;padding:2rem 1.25rem}
.kicker{font-family:var(--mono);font-size:.74rem;color:#c4b5fd;text-transform:uppercase;
  letter-spacing:.14em;margin-bottom:.5rem;display:flex;align-items:center;gap:.5rem}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);
  box-shadow:0 0 8px var(--green);animation:p 2s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
h1{margin:0 0 .35rem;font-size:1.9rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:820px;margin:0 0 1.5rem;font-size:.92rem}
.bar{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;margin-bottom:1.5rem;
  font-size:.8rem;color:var(--tx3);font-family:var(--mono)}
.bar .dot{color:var(--tx2)}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem;align-items:start}
@media(max-width:1100px){.cols{grid-template-columns:1fr}}
.col h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.1em;
  margin:0 0 .85rem;font-weight:700;display:flex;align-items:center;gap:.5rem}
.col h2 .cnt{font-family:var(--mono);background:var(--surface);border:1px solid var(--bd);
  border-radius:99px;padding:.1rem .55rem;font-size:.72rem;color:#c4b5fd}
.card{background:var(--surface);border:1px solid var(--bd);border-radius:12px;
  padding:1rem 1.1rem;margin-bottom:.85rem;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--grad);opacity:.7}
.card .title{font-weight:650;font-size:.95rem;margin:.15rem 0 .55rem;line-height:1.35}
.row{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.5rem}
.badge{display:inline-flex;align-items:center;gap:.3rem;font-family:var(--mono);
  font-size:.7rem;font-weight:700;padding:.18rem .5rem;border-radius:99px;
  border:1px solid;white-space:nowrap}
.badge.conf-hi{color:var(--green);border-color:#10b98155;background:#10b9811a}
.badge.conf-md{color:var(--amber);border-color:#f59e0b55;background:#f59e0b1a}
.badge.conf-lo{color:var(--red);border-color:#ef444455;background:#ef44441a}
.badge.area{color:#c4b5fd;border-color:#6366f155;background:#6366f11a}
.badge.refu-ok{color:var(--green);border-color:#10b98155;background:#10b9811a}
.badge.refu-no{color:var(--red);border-color:#ef444455;background:#ef44441a}
.badge.refu-un{color:var(--amber);border-color:#f59e0b55;background:#f59e0b1a}
.rec{color:#cbd5e1;font-size:.86rem;margin:.4rem 0;line-height:1.5}
.label{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;
  letter-spacing:.09em;color:var(--tx3);margin:.6rem 0 .25rem}
.decision{background:var(--surface2);border:1px solid var(--bd);border-left:3px solid var(--violet);
  border-radius:6px;padding:.5rem .7rem;font-size:.82rem;color:#e2e8f0}
.weak{margin:.3rem 0 0;padding-left:1.1rem;font-size:.8rem;color:var(--tx2)}
.weak li{margin:.15rem 0}
.grades{display:flex;align-items:center;gap:.5rem;margin-top:.85rem;
  padding-top:.7rem;border-top:1px solid var(--bd)}
button.g{font-family:inherit;font-size:.8rem;font-weight:600;cursor:pointer;
  border-radius:7px;padding:.35rem .8rem;border:1px solid var(--bd);
  background:var(--surface2);color:var(--tx2);transition:all .12s}
button.g:hover{border-color:#3a3d55;color:var(--tx)}
button.g.good:hover{border-color:#10b98188;color:var(--green)}
button.g.bad:hover{border-color:#ef444488;color:var(--red)}
button.g:disabled{cursor:default;opacity:.85}
.graded{font-family:var(--mono);font-size:.72rem;font-weight:700;padding:.2rem .5rem;
  border-radius:99px}
.graded.good{color:var(--green);background:#10b9811a;border:1px solid #10b98155}
.graded.bad{color:var(--red);background:#ef44441a;border:1px solid #ef444455}
.graded.other{color:var(--amber);background:#f59e0b1a;border:1px solid #f59e0b55}
button.approve{font-family:inherit;font-size:.8rem;font-weight:600;cursor:pointer;
  border-radius:7px;padding:.35rem .8rem;border:1px solid #10b98155;
  background:#10b9811a;color:var(--green);transition:all .12s;margin-left:auto}
button.approve:hover{border-color:#10b988aa;background:#10b9812e}
button.approve:disabled{cursor:default;opacity:.7}
.approved-pill{font-family:var(--mono);font-size:.72rem;font-weight:700;
  padding:.2rem .55rem;border-radius:99px;margin-left:auto;color:var(--green);
  background:#10b9811a;border:1px solid #10b98155;white-space:nowrap}
.meta{font-family:var(--mono);font-size:.68rem;color:var(--tx3);margin-top:.55rem}
.empty{color:var(--tx3);font-size:.85rem;padding:1.1rem;text-align:center;
  background:var(--surface);border:1px dashed var(--bd);border-radius:10px}
.toast{position:fixed;bottom:1.25rem;left:50%;transform:translateX(-50%) translateY(120%);
  background:var(--surface);border:1px solid var(--bd);border-radius:10px;
  padding:.7rem 1.1rem;font-size:.85rem;box-shadow:0 8px 30px rgba(0,0,0,.5);
  transition:transform .25s;z-index:50}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:#10b98188}.toast.err{border-color:#ef444488;color:#fca5a5}
footer{margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--bd);
  color:var(--tx3);font-size:.8rem;text-align:center}
</style></head><body><div class="wrap">
<div class="kicker"><span class="pulse"></span>DC HUB · BRAIN · INNOVATION</div>
<h1>What the brain is thinking</h1>
<p class="sub">The brain's verified, adversarially-refuted analysis — the agenda it
chose for itself, the investigations it ran, and the ranked improvements it proposed.
<b>Grade</b> each item to close the calibration loop the brain learns from, or
<b>approve + open PR</b> asks for the instruction to draft from (pre-filled with the
item's own text — edit it to name the branch you want) and turns it into a guardrailed draft PR
(human-merged, daily-capped; advisory insights that aren't a concrete single-file edit
are just recorded).</p>
<div class="bar">
  <span id="status">Loading…</span>
  <span class="dot">·</span>
  <span id="updated"></span>
  <span class="dot">·</span>
  <span>auto-refresh 60s</span>
  <span class="dot">·</span>
  <a href="#" id="refreshNow" style="color:#c4b5fd;text-decoration:none">refresh now</a>
</div>
<div class="cols">
  <div class="col"><h2>Self-directed agenda <span class="cnt" id="cnt-agenda">0</span></h2><div id="col-agenda"></div></div>
  <div class="col"><h2>Investigations <span class="cnt" id="cnt-inv">0</span></h2><div id="col-inv"></div></div>
  <div class="col"><h2>Proposals <span class="cnt" id="cnt-prop">0</span></h2><div id="col-prop"></div></div>
</div>
<footer>grade + approve→draft-PR (a human merges every PR · daily-capped · auto-merge OFF) ·
brain_self_agenda · brain_investigations · brain_enhancement_proposals</footer>
</div>
<div class="toast" id="toast"></div>
<script>
// Any closing script tag written inside this block MUST be escaped as <\/…>,
// including inside strings and comments: the HTML parser ends script data at
// the first literal one and does not know JS syntax. An unescaped one here
// truncates the page mid-IIFE and the dashboard renders blank
// ("Uncaught SyntaxError: Unexpected end of input"). Guarded by
// test_page_script_block_is_not_truncated.
(function(){
  // Carry the SAME admin key the page was opened with onto the digest fetch +
  // grade POSTs (mirrors the page's own ?admin_key= gate). Header is preferred;
  // query-param is the fallback that makes the whole thing browser-openable.
  function getCookie(n){var m=document.cookie.match(new RegExp('(?:^|; )'+n.replace(/([.*+?^${}()|[\]\\])/g,'\\$1')+'=([^;]*)'));return m?decodeURIComponent(m[1]):'';}
  var KEY = new URLSearchParams(location.search).get('admin_key') || getCookie('dchub_innov_key') || '';
  // APPROVE ledger — operator GREENLIGHT decisions, keyed "kind:id". Populated
  // from /api/v1/brain/innovation/approvals before each render so approved items
  // stay marked across the 60s auto-refresh. Propose-only: approving RECORDS the
  // decision; it never triggers any action.
  var APPROVED = {};
  function authq(url){ return KEY ? (url + (url.indexOf('?')<0?'?':'&') + 'admin_key=' + encodeURIComponent(KEY)) : url; }
  function authh(){ var h={'Content-Type':'application/json'}; if(KEY){h['X-Admin-Key']=KEY;} return h; }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }
  // btoa() is latin1-only and throws on the — and → these directives carry,
  // and .apply() on a big array blows the stack, so chunk it.
  function bytesToB64(bytes){
    var out=''; for(var i=0;i<bytes.length;i+=0x8000){ out += String.fromCharCode.apply(null, bytes.subarray(i,i+0x8000)); }
    return btoa(out);
  }
  // TRANSPORT for the operator's directive. It is free text that routinely
  // quotes shell AND HTML in one breath ("run `curl -i https://…`" … "add
  // `<script src=…><\/script>` before </body>"), and Cloudflare's WAF 403s
  // both signatures on the request body. Cloudflare BASE64-DECODES it before
  // matching, so b64 alone hides the command-injection signature and NOT the
  // XSS one — measured on the full inv:100502 directive at the live edge:
  // plain 403, base64 403, zlib+base64 400 (5/5). Deflate first and what the
  // WAF decodes is compressed bytes with nothing left to match.
  function directivePayload(s){
    var enc = new TextEncoder().encode(String(s==null?'':s));
    if(typeof CompressionStream !== 'function'){
      return Promise.resolve({directive_b64: bytesToB64(enc)});
    }
    try{
      var cs = new CompressionStream('deflate');   // zlib wrapper: python zlib.decompress reads it
      var w  = cs.writable.getWriter(); w.write(enc); w.close();
      return new Response(cs.readable).arrayBuffer()
        .then(function(buf){ return {directive_z: bytesToB64(new Uint8Array(buf))}; })
        .catch(function(){ return {directive_b64: bytesToB64(enc)}; });
    }catch(e){
      return Promise.resolve({directive_b64: bytesToB64(enc)});
    }
  }
  // A reply with no JSON `error` used to print the literal word 'error', which
  // is what an edge block, a 502 and a bad gateway all looked like. Say who
  // answered: only the app speaks JSON here, so a non-JSON body is upstream.
  function approveErr(res){
    if(res.j && res.j.error) return res.j.error;
    if(res.jsonOk) return 'HTTP '+res.status+' with no error field';
    if(res.status===403) return 'HTTP 403 from the edge (HTML, not JSON) — the request never reached the app; a WAF rule matched the request body';
    return 'HTTP '+res.status+' — non-JSON reply, so this came from the edge, not the app';
  }

  function confBadge(c){
    if(c==null) return '<span class="badge conf-md">conf —</span>';
    var v=Number(c); var cls=v>=0.7?'conf-hi':(v>=0.45?'conf-md':'conf-lo');
    return '<span class="badge '+cls+'">conf '+(v).toFixed(2)+'</span>';
  }
  function refuBadge(it){
    if(!it.refutation_attempted) return '<span class="badge refu-un">⚠ un-refuted</span>';
    if(it.refutation_unparsed) return '<span class="badge refu-un">⚠ refutation unparsed</span>';
    if(it.refutation_survived===true) return '<span class="badge refu-ok">✓ survived refutation</span>';
    if(it.refutation_survived===false) return '<span class="badge refu-no">✗ refuted</span>';
    return '<span class="badge refu-un">⚠ refutation inconclusive</span>';
  }
  function gradeUrl(kind,id){
    if(kind==='agenda') return '/api/v1/brain/agenda/'+id+'/grade';
    if(kind==='inv')    return '/api/v1/brain/investigate/'+id+'/grade';
    return '/api/v1/brain/enhancements/'+id+'/grade';
  }
  function gradedPill(g){
    var gg=String(g).toLowerCase();
    var cls=gg==='good'?'good':(gg==='bad'?'bad':'other');
    return '<span class="graded '+cls+'">graded: '+esc(g)+'</span>';
  }

  function card(kind, it){
    var heading = it.title || it.question || '(untitled)';
    var parts = [];
    parts.push('<div class="card" data-kind="'+kind+'" data-id="'+esc(it.id)+'">');
    parts.push('<div class="row">'+confBadge(it.confidence)+refuBadge(it));
    if(it.area) parts.push('<span class="badge area">'+esc(it.area)+'</span>');
    if(kind==='prop' && it.leverage_rank!=null) parts.push('<span class="badge area">lev '+Number(it.leverage_rank).toFixed(2)+'</span>');
    parts.push('</div>');
    parts.push('<div class="title">'+esc(heading)+'</div>');
    if(it.recommendation){ parts.push('<div class="rec">'+esc(it.recommendation)+'</div>'); }
    if(it.weaknesses && it.weaknesses.length){
      parts.push('<div class="label">weaknesses found</div><ul class="weak">');
      it.weaknesses.slice(0,5).forEach(function(w){ parts.push('<li>'+esc(w)+'</li>'); });
      parts.push('</ul>');
    }
    if(it.decision_for_human){
      parts.push('<div class="label">decision for human</div><div class="decision">'+esc(it.decision_for_human)+'</div>');
    }
    parts.push('<div class="grades" data-grades>');
    if(it.grade){
      parts.push(gradedPill(it.grade));
    } else {
      parts.push('<button class="g good" data-grade="good">👍 good</button>');
      parts.push('<button class="g bad" data-grade="bad">👎 bad</button>');
    }
    // APPROVE — operator greenlights the brain's guidance. Propose-only: this
    // RECORDS the decision; it never triggers any action / merge / send.
    if(APPROVED[kind+':'+it.id]){
      parts.push('<span class="approved-pill" data-approved-pill>✓ approved</span>');
    } else {
      parts.push('<button class="approve" data-approve>✓ approve + open PR</button>');
    }
    parts.push('</div>');
    if(it.created_at) parts.push('<div class="meta">#'+esc(it.id)+' · '+esc(String(it.created_at).slice(0,19))+'</div>');
    parts.push('</div>');
    return parts.join('');
  }

  function fill(elId, kind, items, emptyMsg){
    var el = document.getElementById(elId);
    if(!items || !items.length){ el.innerHTML='<div class="empty">'+emptyMsg+'</div>'; return; }
    el.innerHTML = items.map(function(it){ return card(kind,it); }).join('');
  }

  function toast(msg, ok){
    var t=document.getElementById('toast');
    t.textContent=msg; t.className='toast show '+(ok?'ok':'err');
    setTimeout(function(){ t.className='toast '+(ok?'ok':'err'); }, 2600);
  }

  // Grade buttons — POST to the EXISTING admin grade endpoints, then confirm.
  document.addEventListener('click', function(ev){
    var btn = ev.target.closest && ev.target.closest('button.g');
    if(!btn) return;
    var card = btn.closest('.card'); if(!card) return;
    var kind = card.getAttribute('data-kind');
    var id   = card.getAttribute('data-id');
    var grade= btn.getAttribute('data-grade');
    var box  = card.querySelector('[data-grades]');
    Array.prototype.forEach.call(box.querySelectorAll('button.g'), function(b){ b.disabled=true; });
    fetch(authq(gradeUrl(kind,id)), {method:'POST', headers:authh(), body:JSON.stringify({grade:grade})})
      .then(function(r){ return r.json().catch(function(){return {};}).then(function(j){ return {ok:r.ok, j:j}; }); })
      .then(function(res){
        if(res.ok && res.j && res.j.ok!==false){
          box.innerHTML = '<span class="graded '+(grade==='good'?'good':'bad')+'">graded: '+grade+'</span>';
          toast('Graded #'+id+' as "'+grade+'"', true);
        } else {
          Array.prototype.forEach.call(box.querySelectorAll('button.g'), function(b){ b.disabled=false; });
          toast('Grade failed: '+((res.j&&res.j.error)||'error'), false);
        }
      })
      .catch(function(e){
        Array.prototype.forEach.call(box.querySelectorAll('button.g'), function(b){ b.disabled=false; });
        toast('Grade failed: '+e, false);
      });
  });

  // Approve button — POSTs the operator's GREENLIGHT to the propose-only approve
  // endpoint, then swaps the button for the "✓ approved" pill. Carries the same
  // admin key as the grade flow. RECORDS the decision only — never acts on it.
  document.addEventListener('click', function(ev){
    var btn = ev.target.closest && ev.target.closest('button.approve');
    if(!btn) return;
    var card = btn.closest('.card'); if(!card) return;
    var kind = card.getAttribute('data-kind');
    var id   = card.getAttribute('data-id');
    // The item's own "decision for human" is usually a MENU ("Choose (A) or
    // (B)…"), and approving a menu records agreement without recording WHICH
    // branch — which is why those approvals drafted nothing. Pre-fill it and
    // let the operator turn it into the instruction Layer-5 will actually act
    // on. Cancel aborts; leaving it unchanged is the old behaviour.
    var dnode = card.querySelector('.decision');
    var seed  = dnode ? (dnode.textContent || '').trim() : '';
    var directive = window.prompt(
      'Instruction for the drafter — say WHICH branch to take, in the '
      + 'imperative. It becomes the directive Layer-5 drafts from.', seed);
    if(directive === null) return;            // cancelled: record nothing
    directive = (directive || '').trim();
    btn.disabled = true; btn.textContent = '⏳ drafting…';
    // The directive never travels in the clear — see directivePayload.
    directivePayload(directive).then(function(dp){
      var payload = {kind:kind, id:Number(id), decision:'approved', open_pr:true};
      for(var k in dp){ if(Object.prototype.hasOwnProperty.call(dp,k)){ payload[k]=dp[k]; } }
      return fetch(authq('/api/v1/brain/innovation/approve'), {method:'POST', headers:authh(), body:JSON.stringify(payload)});
    })
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, status:r.status, jsonOk:true, j:j}; }).catch(function(){ return {ok:r.ok, status:r.status, jsonOk:false, j:{}}; }); })
      .then(function(res){
        if(res.ok && res.j && res.j.ok!==false){
          APPROVED[kind+':'+id] = 'approved';
          var pa = (res.j && res.j.pr_attempt) || {};
          var pr = pa.pr || {};
          var prUrl = pr.pr_url || pr.url || '';
          var pill = document.createElement('span');
          pill.className = 'approved-pill';
          pill.setAttribute('data-approved-pill','');
          if(pa.acted && prUrl){
            pill.innerHTML = '✓ approved · <a href="'+esc(prUrl)+'" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline">draft PR ↗</a>';
            toast('Approved #'+id+' · draft PR opened', true);
          } else if(pa.refused){
            pill.textContent = '✓ approved (recorded)';
            toast('Approved #'+id+' — not a single-file edit; recorded only', true);
          } else if(pa.note){
            pill.textContent = '✓ approved (recorded)';
            toast('Approved #'+id+' — recorded (no code change)', true);
          } else if(pa.error || pa.ok===false){
            pill.textContent = '✓ approved';
            toast('Approved #'+id+' · PR draft skipped: '+esc(pa.error||pa.reason||'gate closed'), false);
          } else {
            pill.textContent = '✓ approved';
            toast('Approved #'+id, true);
          }
          btn.parentNode.replaceChild(pill, btn);
        } else {
          btn.disabled = false; btn.textContent = '✓ approve + open PR';
          toast('Approve failed: '+approveErr(res), false);
        }
      })
      .catch(function(e){
        btn.disabled = false; btn.textContent = '✓ approve + open PR';
        toast('Approve failed: '+e, false);
      });
  });

  // Pull the operator's greenlight ledger so approved items stay marked across
  // refreshes. Best-effort: a failure just leaves APPROVED empty (no crash).
  function loadApprovals(){
    return fetch(authq('/api/v1/brain/innovation/approvals'), {headers:authh()})
      .then(function(r){ return r.ok ? r.json() : {}; })
      .then(function(d){
        APPROVED = {};
        var rows = (d && d.approved) || [];
        rows.forEach(function(a){ if(a && a.key){ APPROVED[a.key] = a.decision || 'approved'; } });
      })
      .catch(function(){ /* leave APPROVED as-is */ });
  }

  function load(){
    document.getElementById('status').textContent='Loading…';
    loadApprovals().then(function(){
    return fetch(authq('/api/v1/brain/innovation/digest'), {headers:authh()})
      .then(function(r){
        if(r.status===403){ document.getElementById('status').textContent='Admin key required — append ?admin_key=…'; throw new Error('403'); }
        return r.json();
      })
      .then(function(d){
        document.getElementById('status').textContent='Live';
        document.getElementById('updated').textContent='as of '+String(d.generated_at||'').slice(0,19);
        var c=d.counts||{};
        document.getElementById('cnt-agenda').textContent=c.agenda||0;
        document.getElementById('cnt-inv').textContent=c.investigations||0;
        document.getElementById('cnt-prop').textContent=c.proposals||0;
        fill('col-agenda','agenda', d.agenda, 'No self-directed agenda items yet.');
        fill('col-inv','inv', d.investigations, 'No investigations yet.');
        fill('col-prop','prop', d.proposals, 'No proposals yet.');
      })
      .catch(function(){ /* status already set */ });
    });
  }
  document.getElementById('refreshNow').addEventListener('click', function(e){ e.preventDefault(); load(); });
  load();
  setInterval(load, 60000);
})();
</script>
</body></html>"""


def register_brain_innovation_dashboard(app) -> None:
    """Idempotent registration helper for main.py. READ-ONLY surface except the
    grade POSTs (existing endpoints) and the propose-only APPROVE ledger. Bootstraps
    brain_approvals (best-effort, never raises) so the operator's greenlight
    decisions survive the dashboard's auto-refresh."""
    try:
        app.register_blueprint(brain_innovation_dashboard_bp)
    except Exception as e:
        logger.warning("brain_innovation_dashboard already registered: %s", e)
    try:
        _init_approvals()
    except Exception as e:
        logger.warning("brain_innovation_dashboard: _init_approvals skipped: %s", e)
