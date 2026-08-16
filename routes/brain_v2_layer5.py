"""
brain_v2_layer5.py — Phase RR-3 (2026-05-13).

Brain v2 Layer 5: code-level proposals for chronic-stale autonomous
loops.

Layer 4 (brain_v2_layer4.py) proposes (find, replace) TEXT
substitutions for HTML placeholder issues. It's bounded to safe
typographic fixes and the master-heal cron applies them after a
2-cycle approval gate.

Layer 5 escalates one level up: when a loop in /api/v1/system/loops
has been STALE or DEAD across multiple babysitter runs (i.e., the
cron babysitter fired the refresh endpoint and the loop still didn't
recover), Layer 5 calls Claude with:
  - the loop name + its last_event_at + error
  - the relevant source file path(s) for that loop
  - a 4KB window of recent code from those files
  - the babysitter's recent action history

Claude returns a structured proposal:
  {
    "file":     "routes/dcpi.py",
    "search":   "<exact text to replace, must appear in file>",
    "replace":  "<new text>",
    "rationale":"<1-2 sentence explanation>",
    "confidence": 0.0..1.0
  }

The proposal is stored in `brain_proposed_code_fixes` and surfaced on
the /brain dashboard. **Layer 5 does NOT auto-apply** — even a
high-confidence proposal requires human review (or a future Layer 6
gate). This is the conservative-by-design escalation that the Phase
RR architecture commits to: increasing autonomy, never giving up the
human checkpoint at the merge step.

Endpoints:
  POST /api/v1/brain/learn-code     admin-gated; one Layer 5 pass
  GET  /api/v1/brain/proposed-code  latest proposals (public, read-only)

The cron firing schedule is intentionally CONSERVATIVE — once every
6 hours — because each call is a billed Anthropic request. With seven
loops and ~5% chronic-stale rate, expect ~1-2 Claude calls per day.

This module is a deliberately small MVP. The two highest-value
additions for future iterations:
  - Auto-extract relevant source files from a "loop → file path" map
    that lives next to LOOP_REFRESH in system_loops.py
  - Open the proposal as a draft PR via the gh CLI (gated behind a
    separate auto_open_pr flag default-false)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional
from flask import Blueprint, jsonify, request

# Reuse Layer 4's Anthropic client + admin gate
from routes.brain_v2_layer4 import (
    _call_claude, ANTHROPIC_API_KEY, BRAIN_MODEL, _require_admin, ADMIN_KEY,
    BRAIN_MAX_LEARN,
)
from routes._swallowed_writes import note_swallowed_write

brain_v2_layer5_bp = Blueprint("brain_v2_layer5", __name__)


def _admin_guard():
    """No-arg admin check → returns an error Response tuple, or None if OK.

    r-admin-gate-clean (2026-08-09): FAIL CLOSED. The old body was
    `if ADMIN_KEY and provided != ADMIN_KEY: return 401` with ADMIN_KEY captured
    at IMPORT (brain_v2_layer4.py:92, `DCHUB_ADMIN_KEY or DCHUB_INTERNAL_KEY`). If
    BOTH env vars were unset/empty at import (a bad deploy or a mid-rotation gap)
    ADMIN_KEY was falsy, the `if` never fired, and this returned None = ALLOWED
    for ANY key — every Layer-5 admin endpoint (/learn-code, /learn-backend-issues,
    /proposed-code/neutralize, ...) silently went unauthenticated. Route through
    the shared request-time gate instead: internal_auth.require_internal_or_admin
    _clean_key()s BOTH sides, re-reads env PER REQUEST, accepts X-Admin-Key /
    X-Internal-Key / ?admin_key, and returns False when NO secret is configured
    (fail-closed). Same fix as brain_mechanical_classifier / brain_inspector.
    _admin_ok (#2468); pinned by tests/test_brain_layer45_fail_closed.py.

    FIX 2026-05-31 (retained): layer4's _require_admin is a DECORATOR
    (def _require_admin(fn)), so Layer-5 endpoints call THIS no-arg guard inline
    (`auth_err = _admin_guard()`) rather than the decorator."""
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    try:
        from internal_auth import require_internal_or_admin
        if require_internal_or_admin(request):
            return None
    except Exception:
        pass
    # Additional accept path: exact raw match against the import-time key. Guarded
    # by `ADMIN_KEY` truthiness so an EMPTY key can NEVER satisfy it — that guard
    # is what makes the missing-env branch reject instead of allow.
    if ADMIN_KEY and provided == ADMIN_KEY:
        return None
    return jsonify(error="unauthorized", hint="X-Admin-Key header required"), 401


@brain_v2_layer5_bp.post("/api/v1/brain/proposed-code/neutralize")
def neutralize_proposed_code():
    """r67 one-off cleanup: neutralize specific proposals by id so the PR-opener
    SKIPS them — sets pr_url to a sentinel + status='rejected' (pending-pr filters
    WHERE pr_url IS NULL AND status='proposed', so either excludes them). Used to
    retire the pre-guard SQLite-conversion hallucinations; the Postgres-stack guard
    in _LEARN_CODE_SYSTEM now prevents new ones. Idempotent (only flips rows still
    'proposed'). Gate: X-Admin-Key==ADMIN_KEY OR X-Internal-Key (the configured DCHUB_INTERNAL_KEY)."""
    ok = bool(ADMIN_KEY) and (request.headers.get("X-Admin-Key") or request.args.get("admin_key")) == ADMIN_KEY
    if not ok:
        try:
            from internal_auth import is_valid_internal_key
            ok = is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
        except Exception:
            ok = False
    if not ok:
        return jsonify(ok=False, error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()]
    reason = (d.get("reason") or "neutralized")[:120]
    if not ids:
        return jsonify(ok=False, error="ids[] required"), 400
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE brain_proposed_code_fixes
                   SET status = 'rejected', pr_url = %s
                 WHERE id = ANY(%s) AND COALESCE(status,'proposed') = 'proposed'
                 RETURNING id
            """, ("neutralized:" + reason, ids))
            flipped = [r[0] for r in cur.fetchall()]
            conn.commit()
        return jsonify(ok=True, neutralized=flipped, requested=ids), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


# ──────────────────────────────────────────────────────────────────
# r-reconcile (2026-06-18): mark already-fixed proposals 'resolved'.
#
# The /brain Layer-5 dashboard showed ~8 "proposed" code fixes but ~7
# were ALREADY fixed in source (the INTERVAL '%s days' bugs, the tz-aware
# utcnow, the boolean is_active=1, the SQLite datetime, the press_releases
# published flag). Each proposal records the EXACT buggy snippet it wanted
# to replace in `search_text` (and changes_json[].search). If that snippet
# is GONE from the live file, the bug was fixed and the proposal is stale.
#
# Status values used by THIS module (free-text `status` column — the
# CREATE TABLE has `status TEXT DEFAULT 'proposed'`, NO check constraint):
#   proposed (default) · rejected · approved · pr_opened ·
#   unfixable_by_sonnet · merged_healthy · reverted
# We add 'resolved' for "verified gone in source, never PR'd". Open set
# we reconcile = COALESCE(status,'proposed') IN ('proposed',
# 'unfixable_by_sonnet') AND pr_url IS NULL.
#
# CONSERVATIVE BY DESIGN: only mark resolved when we are confident the
# diagnosed pattern is GONE. A file that's missing / unreadable / read as
# empty is NEVER treated as "bug gone" (that would falsely resolve every
# open proposal on a transient read error).
# ──────────────────────────────────────────────────────────────────

# Known-bug heuristic patterns. If the proposal's rationale/search clearly
# concerns one of these AND the pattern is absent from the file, that's a
# strong "fixed" signal. Used in addition to the primary search_text check.
_KNOWN_BUG_PATTERNS = (
    "INTERVAL '%s days'",
    "INTERVAL '%s day'",
    "datetime.datetime.utcnow()",
    "datetime.utcnow()",
    "is_active, 1) = 1",
    "is_active, 1)=1",
    "datetime('now'",
    "datetime(\"now\"",
)


def _read_file_full(path: str):
    """Read a repo-relative file in full. Returns (text, ok). ok is False
    when the file is missing or unreadable — the caller must then treat
    the proposal as STILL open (never resolve on a read failure)."""
    try:
        full = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), path)
        if not os.path.exists(full):
            return "", False
        with open(full, encoding="utf-8") as f:
            return f.read(), True
    except Exception:
        return "", False


def _proposal_bug_gone(row: dict):
    """Decide whether the bug a proposal diagnosed is STILL present.

    Returns (gone, reason). `gone` is True ONLY when we are confident the
    pattern is absent from every target file. On ANY uncertainty (missing
    file, unreadable, no usable check derivable) returns (False, reason)
    so the proposal stays open.

    row carries: file_path, search_text, rationale, changes_json (list of
    {file,search,replace} when present).
    """
    # Build the list of (file, search) checks. Prefer changes_json (the
    # canonical multi-file shape); fall back to the legacy columns.
    checks = []
    raw_changes = row.get("changes_json")
    if isinstance(raw_changes, str):
        try:
            raw_changes = json.loads(raw_changes)
        except Exception:
            raw_changes = None
    if isinstance(raw_changes, list) and raw_changes:
        for ch in raw_changes:
            if not isinstance(ch, dict):
                return False, "malformed_changes_json"
            f = (ch.get("file") or "").strip()
            s = (ch.get("search") or "").strip()
            if f and s:
                checks.append((f, s))
    if not checks:
        f = (row.get("file_path") or "").strip()
        s = (row.get("search_text") or "").strip()
        if f and s:
            checks.append((f, s))

    if not checks:
        return False, "no_search_snippet"

    rationale = (row.get("rationale") or "").lower()

    # Every target file's bug must be confirmed gone for the whole
    # proposal to resolve (all-or-nothing, mirroring how proposals are
    # validated/applied as a unit).
    confirmed = []
    for f, s in checks:
        # Only reconcile real source files we can read.
        if not f.endswith(".py"):
            return False, f"non_py_target:{f}"
        text, ok = _read_file_full(f)
        if not ok:
            return False, f"unreadable:{f}"
        if not text.strip():
            return False, f"empty_read:{f}"

        # PRIMARY check: the exact buggy snippet the proposal targeted.
        # If search_text is short/ambiguous (<12 chars) it's an unreliable
        # signal — refuse rather than risk a false resolve.
        if len(s) < 12:
            return False, f"search_too_short:{f}"
        if s in text:
            return False, f"still_present:{f}"

        # SECONDARY (corroborating) check: if the rationale clearly names a
        # known bug class, require that pattern to ALSO be absent from the
        # file before we call it resolved. This guards against a proposal
        # whose search_text drifted (e.g. surrounding lines edited) while
        # the actual bug lingers under a slightly different snippet.
        for pat in _KNOWN_BUG_PATTERNS:
            key = pat.lower().rstrip("'\"")
            # Does the proposal concern this known bug? (rationale or the
            # original search snippet referenced it)
            concerns = key[:18] in rationale or pat in s
            if concerns and pat in text:
                return False, f"known_pattern_lingers:{f}"

        confirmed.append(f)

    return True, "gone:" + ",".join(confirmed)


@brain_v2_layer5_bp.post("/api/v1/brain/proposals/reconcile")
def reconcile_proposals():
    """Admin: mark open Layer-5 proposals 'resolved' when the bug they
    diagnosed is already gone from source (so the dashboard stops showing
    fixed-in-source proposals as still-open).

    Conservative: a proposal is resolved ONLY when its exact buggy snippet
    (search_text / changes_json[].search) is verifiably ABSENT from every
    target .py file. Missing / unreadable / empty reads, short/ambiguous
    snippets, and lingering known-bug patterns all leave it open. Never
    resolves on a parse or read error.

    Returns {checked, resolved, still_open, details:[...]}.
    """
    auth_err = _admin_guard()
    if auth_err:
        return auth_err

    apply = (request.args.get("dry_run") or "").lower() not in ("1", "true", "yes")
    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))

    try:
        import psycopg2
        import psycopg2.extras
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(ok=False, error="no_db_url"), 503
        with psycopg2.connect(url, connect_timeout=5) as conn:
            # pr_url may not exist on very old tables — add defensively so
            # the WHERE clause never aborts the txn.
            with conn.cursor() as cur:
                try:
                    cur.execute("ALTER TABLE brain_proposed_code_fixes "
                                "ADD COLUMN IF NOT EXISTS pr_url TEXT;")
                    conn.commit()
                except Exception:
                    conn.rollback()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, loop_name, file_path, search_text,
                           rationale, status, changes_json
                      FROM brain_proposed_code_fixes
                     WHERE COALESCE(status, 'proposed')
                           IN ('proposed', 'unfixable_by_sonnet')
                       AND pr_url IS NULL
                     ORDER BY proposed_at DESC
                     LIMIT %s
                """, (limit,))
                rows = [dict(r) for r in cur.fetchall()]

            checked = len(rows)
            resolved_ids = []
            details = []
            for row in rows:
                try:
                    gone, reason = _proposal_bug_gone(row)
                except Exception as e:
                    # Never resolve on an evaluation error — stay open.
                    gone, reason = False, f"eval_error:{str(e)[:80]}"
                details.append({
                    "id": row.get("id"),
                    "file": row.get("file_path"),
                    "status": row.get("status") or "proposed",
                    "bug_gone": gone,
                    "reason": reason,
                })
                if gone:
                    resolved_ids.append(row.get("id"))

            applied_ids = []
            if apply and resolved_ids:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE brain_proposed_code_fixes
                           SET status = 'resolved',
                               reviewed_at = NOW(),
                               reviewer_note = COALESCE(reviewer_note, '')
                                   || ' [auto-reconcile: bug gone in source]'
                         WHERE id = ANY(%s)
                           AND COALESCE(status, 'proposed')
                               IN ('proposed', 'unfixable_by_sonnet')
                         RETURNING id
                    """, (resolved_ids,))
                    applied_ids = [r[0] for r in cur.fetchall()]
                conn.commit()

        return jsonify(
            ok=True,
            as_of=datetime.now(timezone.utc).isoformat(),
            dry_run=(not apply),
            checked=checked,
            resolved=(len(applied_ids) if apply else len(resolved_ids)),
            still_open=checked - (len(applied_ids) if apply else len(resolved_ids)),
            resolved_ids=(applied_ids if apply else resolved_ids),
            status_value="resolved",
            details=details,
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


# ──────────────────────────────────────────────────────────────────
# Loop → source-file map. The Layer 5 prompt grabs a window of code
# from these files so Claude has the actual implementation context,
# not just the loop name. Files are LISTED IN ORDER OF RELEVANCE —
# the first entry is the most likely place a bug-fix would land.
# ──────────────────────────────────────────────────────────────────

LOOP_SOURCE_FILES: dict = {
    "brain_learn":        ["routes/brain_v2_layer4.py"],
    "auto_press_daily":   ["routes/marketing_engine.py", "dchub_media.py"],
    "testimonial_ingest": ["routes/dchub_media_hub.py"],
    "dcpi_recompute":     ["routes/dcpi.py"],
    "iso_extract":        ["routes/iso_orchestrator.py", "routes/_iso_common.py"],
}

# Phase GG (2026-05-14): same idea for actionable_backend_issues
# surfaced by /heal/findings (added in PR #70). Each issue label
# starts with `cron_failing_with_errors` / `cron_underproducing` /
# `loop_stale`, all from dchub_self_heal.fix_backend_cron_scan.
# Map them to the same source files we'd touch for the corresponding
# loop. The keys are the synthetic dchub://cron/<name> URLs from
# the detector — keep in sync with dchub_self_heal.py.
BACKEND_ISSUE_SOURCE_FILES: dict = {
    "dchub://cron/dcpi_recompute":   ["routes/dcpi.py"],
    "dchub://cron/auto_press_daily": ["routes/marketing_engine.py"],
}


# 2026-06-04: per-issue-type prompt hints injected into _build_code_prompt
# below. Two issue classes were stuck "untried" for 16-31 cycles because the
# generic _LEARN_CODE_SYSTEM prompt asks Claude to find a "silent correctness
# bug in a loop handler" — but these issues aren't loop bugs, they're route
# duplications and seeder coverage gaps. Give Claude an issue-specific
# diagnosis + the right fix shape, matching the existing config_not_code
# short-circuit pattern: route the proposer at the actual problem.
#
# Keys match the `issue` field on actionable_backend_issues (full string for
# shadowed_route; prefix-match for coverage_gap_gas:XX where XX is a state).
_ISSUE_TYPE_PROMPT_HINTS: dict = {
    "shadowed_route": (
        "\nISSUE-TYPE GUIDANCE (shadowed_route):\n"
        "This finding means TWO @app.route / @bp.route registrations exist for "
        "the SAME URL pattern — Flask silently uses whichever registered first, "
        "so the other handler is dead code. Your job is NOT to rename a route. "
        "Find BOTH registrations in the source (grep the URL in the snippets / "
        "look for duplicate @app.route or @<bp>.route decorators) and propose "
        "REMOVING the duplicate registration — typically the older/legacy one. "
        "The `search` should include the @route decorator + its handler "
        "function (a tight 4-10 line slice). The `replace` should be empty "
        "OR a one-line comment marking the removal. If only one registration "
        "is visible in the snippets, refuse (the duplicate lives off-screen)."
    ),
    "coverage_gap_gas": (
        "\nISSUE-TYPE GUIDANCE (coverage_gap_gas):\n"
        "This finding means a US state code has NO row in market_gas_pricing. "
        "There are TWO valid fixes — pick whichever the snippets support:\n"
        "  (A) ADD-TO-SEEDER: locate the state list / seeder loop in "
        "      routes/powered_land_gas.py (or wherever the gas-pricing crawler "
        "      iterates states) and propose appending the missing state to "
        "      the iterated list. This is the right fix for states with real "
        "      gas pricing data (most CONUS states).\n"
        "  (B) ALLOWLIST-EXCLUDE: if the state has no producing gas market "
        "      (e.g. HI, an island grid), propose adding it to a documented "
        "      INTENTIONAL_GAS_EXCLUSIONS set with a one-line comment "
        "      explaining why. Create the constant near the top of the seeder "
        "      module if it doesn't exist.\n"
        "Prefer (A) when in doubt. Refuse if the seeder file isn't in the "
        "snippets."
    ),
}


def _hint_for_issue(issue_label: str) -> str:
    """Return the issue-type prompt hint for a finding label, or ''.
    Exact match for 'shadowed_route'; prefix match for 'coverage_gap_gas'
    (the live label is e.g. 'coverage_gap_gas:RI')."""
    lbl = (issue_label or "").strip().lower()
    if not lbl:
        return ""
    if lbl == "shadowed_route" or lbl.startswith("shadowed_route:"):
        return _ISSUE_TYPE_PROMPT_HINTS["shadowed_route"]
    if lbl.startswith("coverage_gap_gas"):
        return _ISSUE_TYPE_PROMPT_HINTS["coverage_gap_gas"]
    return ""


_LEARN_CODE_SYSTEM = (
    "You are Brain v2 Layer 5 — the code-fix proposal engine for DC Hub's "
    "autonomous loops. A loop has been stale or dead across multiple cron-"
    "babysitter runs even though the cron is firing successfully. That "
    "means there's a silent correctness bug INSIDE the loop's handler — "
    "not a missing endpoint, not auth, but a code-level problem that the "
    "lower self-healing layers cannot reach.\n\n"
    "You will receive: (1) the loop name and its last error, (2) source "
    "code from the relevant file(s), (3) recent babysitter action log.\n\n"
    "Phase GG#3 (2026-05-14): you may now propose a MULTI-FILE fix when "
    "the bug genuinely spans files (e.g. a caller + its helper). Return "
    "a STRICT JSON proposal in ONE of two shapes:\n\n"
    "  SINGLE-FILE (preferred when the fix is one place):\n"
    '    {"file": "path/to/file.py",\n'
    '     "search": "<exact substring that must appear verbatim>",\n'
    '     "replace": "<the new code>",\n'
    '     "rationale": "<one or two sentences>",\n'
    '     "confidence": <0.0..1.0>}\n\n'
    "  MULTI-FILE (only when the bug truly spans files):\n"
    '    {"changes": [\n'
    '       {"file": "a.py", "search": "...", "replace": "..."},\n'
    '       {"file": "b.py", "search": "...", "replace": "..."}\n'
    '     ],\n'
    '     "rationale": "<why these changes go together>",\n'
    '     "confidence": <0.0..1.0>}\n\n'
    "Hard rules:\n"
    "  - Every `search` MUST appear verbatim in its file — no fuzzy matches.\n"
    "  - Include enough context (3-6 lines) in each `search` so the "
    "    substitution is unambiguous; files often have similar lines.\n"
    "  - Every `replace` must be syntactically valid Python.\n"
    "  - STACK = PostgreSQL (Neon) via psycopg2 — NEVER SQLite. Existing "
    "    `information_schema`, `to_regclass`, `%s` placeholders, `ON CONFLICT`, "
    "    and `RETURNING` are CORRECT Postgres and must NOT be 'fixed'. NEVER "
    "    propose sqlite3, sqlite_master, PRAGMA table_info, AUTOINCREMENT, or "
    "    '?' placeholders. A proposal that rewrites Postgres into SQLite is "
    "    ALWAYS wrong — refuse instead.\n"
    "  - Do NOT propose changes to imports, decorators, function "
    "    signatures, or anything outside the body of the loop's handler.\n"
    "  - PREFER single-file. Only use the multi-file shape when a "
    "    single-file fix would be incomplete or wrong. Max 4 files.\n"
    "  - If you cannot isolate the bug from the snippets provided, "
    'refuse: return {"file":"","search":"","replace":"",'
    '"rationale":"refused: <why>","confidence":0}. '
    "Refusing is better than guessing.\n"
    "  - This proposal will NOT be auto-applied. A human reviews it. "
    "    But low-quality refusals waste their time, so be specific.\n"
)


def _normalize_proposal(prop: dict) -> tuple[list, str]:
    """Phase GG#3: collapse either proposal shape into a canonical
    `changes` list. Returns (changes, error). On refusal or malformed
    input, changes is [] and error explains why.

    changes := [{"file": str, "search": str, "replace": str}, ...]
    """
    if not isinstance(prop, dict):
        return [], "not_a_dict"

    # Multi-file shape
    if "changes" in prop and isinstance(prop["changes"], list):
        raw = prop["changes"]
        if len(raw) > 4:
            return [], f"too_many_files ({len(raw)} > 4)"
        out = []
        for i, ch in enumerate(raw):
            if not isinstance(ch, dict):
                return [], f"change[{i}]_not_a_dict"
            f = (ch.get("file") or "").strip()
            s = (ch.get("search") or "").strip()
            r = ch.get("replace")
            if r is None:
                return [], f"change[{i}]_missing_replace"
            if not f or not s:
                # Empty file/search inside a multi-file = explicit refusal
                return [], "refused"
            out.append({"file": f, "search": s, "replace": str(r)})
        if not out:
            return [], "empty_changes"
        return out, ""

    # Single-file shape (legacy)
    f = (prop.get("file") or "").strip()
    s = (prop.get("search") or "").strip()
    r = prop.get("replace")
    if not f or not s:
        return [], "refused"   # the documented refusal contract
    if r is None:
        return [], "missing_replace"
    return [{"file": f, "search": s, "replace": str(r)}], ""


def _validate_and_store_proposal(source_name: str, prop: dict,
                                  issue_key: str = "") -> dict:
    """Phase GG#3: shared normalize → validate-every-change → store
    pipeline for both learn-code and learn-backend-issues. Returns a
    result dict (the endpoint adds its own loop/url key + appends).

    - Normalizes single OR multi-file Claude output into a `changes` list
    - Validates EVERY change's search text appears verbatim in its
      live file (a hallucinated multi-file proposal is rejected whole —
      we never apply a partial multi-file fix)
    - Stores changes_json (full array) + legacy columns (change[0])
    """
    changes, norm_err = _normalize_proposal(prop)
    rationale = (prop.get("rationale") or "").strip()
    confidence = float(prop.get("confidence", 0) or 0)

    if norm_err == "refused":
        return {"outcome": "refused", "rationale": rationale[:200]}
    if norm_err:
        return {"outcome": f"malformed: {norm_err}",
                "rationale": rationale[:200]}

    # r62-qa: DETERMINISTIC SQLite-hallucination reject. DC Hub is 100%
    # PostgreSQL. The _LEARN_CODE_SYSTEM prompt forbids SQLite, but a system
    # bullet can be ignored when the source excerpt is full of SQLite-looking
    # bait (e.g. api_monetization.py's DB_PATH='dc_nexus.db' + is_active=1/0).
    # A validator cannot be ignored: if any change's REPLACE introduces a
    # SQLite-only token, reject before storing — no proposal that rewrites
    # Postgres into SQLite ever reaches the queue / draft-PR opener.
    import re as _re
    _SQLITE_TOKENS = _re.compile(
        r'sqlite_master|PRAGMA\s+table_info|AUTOINCREMENT|import\s+sqlite3|'
        r'sqlite3\.|\.db[\'"]|cursor\(\)\.execute\([^)]*\?\s*[,)]',
        _re.I)
    for _ch in changes:
        _repl = _ch.get("replace", "") or ""
        if _SQLITE_TOKENS.search(_repl):
            return {"outcome": "rejected_sqlite_hallucination",
                    "file": _ch.get("file"),
                    "rationale": ("Proposal introduces SQLite-only syntax; DC Hub "
                                  "is PostgreSQL. Auto-rejected by the deterministic "
                                  "stack guard.")[:200]}

    # Validate EVERY change — all-or-nothing. A multi-file proposal
    # where one search is hallucinated is rejected entirely; applying
    # a partial multi-file fix would leave the codebase half-changed.
    for ch in changes:
        live = _read_window(ch["file"], max_chars=200000)
        if ch["search"] not in live:
            return {"outcome": "search_not_found",
                    "file": ch["file"],
                    "search_preview": ch["search"][:100],
                    "file_count": len(changes),
                    "rationale": rationale[:200]}

    # r-no-hallucinated-syntax (2026-06-06): the dominant Layer-5 noise was
    # proposing "incomplete function / syntax error / NameError" fixes for files
    # that ALREADY COMPILE CLEAN (Claude inventing bugs from a truncated excerpt
    # — verified: report_narrative/email_service/ai_interconnection/main.py all
    # compile fine yet had 0.95-confidence "syntax error" proposals). If the
    # target .py compiles and the rationale claims a compile-class bug, the bug
    # is hallucinated — reject before it reaches the queue / draft-PR opener.
    _rl = rationale.lower()
    if any(t in _rl for t in ("syntax error", "incomplete", "nameerror",
                              "name error", "fails to import", "unclosed",
                              "missing paren", "would cause", "causes a syntax",
                              "indentation error")):
        for ch in changes:
            if not str(ch.get("file", "")).endswith(".py"):
                continue
            try:
                compile(_read_window(ch["file"], max_chars=400000),
                        ch["file"], "exec")
            except SyntaxError:
                continue   # genuinely broken (or truncated read) -> allow the fix
            except Exception:
                continue
            return {"outcome": "rejected_false_syntax_claim",
                    "file": ch["file"],
                    "rationale": ("File compiles clean; the claimed syntax/"
                                  "incomplete-function bug is hallucinated. "
                                  "Auto-rejected by the compile guard.")[:200]}

    primary = changes[0]
    # Item G (2026-06-02): compute recipe_key for fuzzy cross-cycle
    # dedupe. The (loop_name, file_path, search_text) UNIQUE constraint
    # only catches EXACT-text duplicates; recipe_key catches
    # semantically-similar proposals across cycles so approval_count
    # reflects real recurrence.
    _finding_class = (prop.get("finding_class") or prop.get("issue_label")
                       or source_name or "").strip().lower()
    try:
        from routes.brain_v2_layer4 import recipe_key as _recipe_key
        _rkey = _recipe_key(primary.get("file") or "",
                             _finding_class, rationale)
    except Exception:
        _rkey = None
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            # Look up existing rows by recipe_key for cross-cycle
            # approval_count. If we've seen this recipe before, bump
            # cycles_seen and approval_count BEFORE attempting insert.
            existing_approval = 0
            existing_cycles = 0
            if _rkey:
                try:
                    cur.execute("""
                        SELECT COALESCE(MAX(approval_count), 0),
                               COALESCE(MAX(cycles_seen), 0)
                          FROM brain_proposed_code_fixes
                         WHERE recipe_key = %s
                    """, (_rkey,))
                    _row = cur.fetchone()
                    existing_approval = int(_row[0] or 0) if _row else 0
                    existing_cycles = int(_row[1] or 0) if _row else 0
                except Exception:
                    pass
            new_approval = existing_approval + 1
            new_cycles = existing_cycles + 1
            cur.execute("""
                INSERT INTO brain_proposed_code_fixes
                    (loop_name, file_path, search_text, replace_text,
                     rationale, confidence, model, changes_json,
                     recipe_key, finding_class, approval_count,
                     cycles_seen, issue_key, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT (loop_name, file_path, search_text) DO UPDATE
                  SET approval_count = brain_proposed_code_fixes.approval_count + 1,
                      cycles_seen    = brain_proposed_code_fixes.cycles_seen + 1,
                      issue_key      = COALESCE(EXCLUDED.issue_key,
                                                brain_proposed_code_fixes.issue_key),
                      last_seen_at   = NOW()
                RETURNING id, approval_count, cycles_seen
            """, (source_name, primary["file"], primary["search"],
                  primary["replace"], rationale, confidence, BRAIN_MODEL,
                  json.dumps(changes), _rkey, _finding_class,
                  new_approval, new_cycles,
                  (issue_key or None)))
            row = cur.fetchone()
            # Also bump approval_count + cycles_seen on EVERY row
            # sharing the recipe_key (not just the one we just inserted/
            # touched) so the cross-cycle view stays consistent.
            if _rkey:
                try:
                    cur.execute("""
                        UPDATE brain_proposed_code_fixes
                           SET approval_count = GREATEST(approval_count, %s),
                               cycles_seen    = GREATEST(cycles_seen,    %s),
                               last_seen_at   = NOW()
                         WHERE recipe_key = %s
                    """, (new_approval, new_cycles, _rkey))
                except Exception:
                    note_swallowed_write("brain_proposed_code_fixes", where="brain_v2_layer5._validate_and_store_proposal")
                    pass
            conn.commit()
        # Outcome: "proposed" on first insert, "duplicate" on conflict
        # (the row was already present and we bumped its counts).
        _approval = int(row[1] or 0) if (row and len(row) > 1) else new_approval
        _cycles   = int(row[2] or 0) if (row and len(row) > 2) else new_cycles
        return {
            "outcome": "proposed",
            "id": row[0] if row else None,
            "file": primary["file"],
            "file_count": len(changes),
            "confidence": confidence,
            "rationale": rationale[:200],
            "recipe_key": _rkey,
            "approval_count": _approval,
            "cycles_seen": _cycles,
            "approved_via_recipe": (_approval >= 2),
        }
    except Exception as e:
        return {"outcome": f"store_failed: {str(e)[:200]}"}


def _read_window(path: str, max_chars: int = 4000,
                 center_line: int = None) -> str:
    """Read up to max_chars from `path`. When center_line is given, read a
    window CENTERED on that line — so the model sees the code the
    source-mapper actually located, not just the top of a large file. (Gap-B
    fix: previously we always returned data[:max_chars] from the top, so a
    finding resolved to line 800 of a 1,200-line file showed Claude the
    wrong code and it correctly refused.) Returns empty string on any
    failure — Layer 5 should keep going even if one file is missing."""
    try:
        # Resolve relative to the repo root (assumed CWD on Railway).
        full = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), path)
        if not os.path.exists(full):
            return ""
        with open(full, encoding="utf-8") as f:
            data = f.read()
        if center_line and int(center_line) > 0:
            lines = data.splitlines(keepends=True)
            if lines:
                idx = min(max(int(center_line) - 1, 0), len(lines) - 1)
                lo = hi = idx
                size = len(lines[idx])
                # Expand symmetrically outward until we fill max_chars.
                while size < max_chars and (lo > 0 or hi < len(lines) - 1):
                    if lo > 0:
                        lo -= 1
                        size += len(lines[lo])
                    if hi < len(lines) - 1 and size < max_chars:
                        hi += 1
                        size += len(lines[hi])
                header = (f"# ...{path} — excerpt centered on line "
                          f"{center_line} (source-mapper hit)...\n")
                return header + "".join(lines[lo:hi + 1])
        return data[:max_chars]
    except Exception as e:
        print(f"[brain_v2_layer5] read {path} failed: {e}", file=sys.stderr)
        return ""


def _build_code_prompt(loop_state: dict, source_excerpts: dict,
                        babysitter_log: list) -> str:
    parts = [
        f"Loop name: {loop_state.get('name','?')}",
        f"Status: {loop_state.get('status','?')}",
        f"Last event: {loop_state.get('last_event_at') or '(never)'}",
        f"Age (hours): {loop_state.get('age_hours','?')}",
    ]
    if loop_state.get("error"):
        parts.append(f"Probe error: {loop_state['error']}")
    if babysitter_log:
        parts.append("\nRecent babysitter actions:")
        for a in babysitter_log[-5:]:
            parts.append(f"  - {a}")
    parts.append("\nSource code snippets (most relevant first):")
    for fp, excerpt in source_excerpts.items():
        parts.append(f"\n--- {fp} ---")
        parts.append(f"```python\n{excerpt}\n```")
    parts.append("\nPropose a fix or refuse — JSON only.")
    return "\n".join(parts)


def _init_table() -> bool:
    """Create brain_proposed_code_fixes if missing. Idempotent."""
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return False
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_proposed_code_fixes (
                    id            BIGSERIAL PRIMARY KEY,
                    loop_name     TEXT NOT NULL,
                    file_path     TEXT NOT NULL,
                    search_text   TEXT NOT NULL,
                    replace_text  TEXT NOT NULL,
                    rationale     TEXT,
                    confidence    REAL,
                    model         TEXT,
                    status        TEXT DEFAULT 'proposed',
                    proposed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    reviewed_at   TIMESTAMPTZ,
                    reviewer_note TEXT,
                    CONSTRAINT brain_proposed_code_fixes_unique
                      UNIQUE (loop_name, file_path, search_text)
                );
                CREATE INDEX IF NOT EXISTS brain_proposed_code_fixes_recent_idx
                  ON brain_proposed_code_fixes(proposed_at DESC);
            """)
            # Phase GG#3 (2026-05-14): changes_json holds the full
            # multi-file change list as [{file,search,replace}, ...].
            # Single-file proposals also populate it (1-element array)
            # so the PR-opener has ONE code path. The legacy
            # file_path/search_text/replace_text columns keep holding
            # change[0] for dashboard back-compat + the UNIQUE constraint.
            cur.execute("""
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS changes_json JSONB;
            """)
            # Item G (2026-06-02): recipe_key + cross-cycle dedupe columns.
            # recipe_key collapses semantically-equivalent proposals so
            # `approval_count` reflects "the same recipe seen N times",
            # not "the same exact text seen N times". cycles_seen counts
            # how many sonnet cycles tried this recipe without crossing
            # the approval gate; >=5 escalates to unfixable_by_sonnet.
            cur.execute("""
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS recipe_key      TEXT;
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS finding_class   TEXT;
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS approval_count  INT DEFAULT 1;
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS cycles_seen     INT DEFAULT 1;
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS last_seen_at    TIMESTAMPTZ;
                -- issue_key: the SOURCE FINDING's issue (e.g.
                -- 'page_persistent_5xx:/api/v1/iso/zones'). Distinct from
                -- loop_name, which for a backend-issue proposal is the finding
                -- URL (loop_state["name"] = url), and from finding_class, which
                -- also resolves to a URL/path. Before this column the issue key
                -- reached NO field of the proposal and therefore no field of the
                -- draft PR body — so sentinel_auto_merge's GATE_SENTINEL_DERIVED,
                -- which greps the body for 'page_persistent_5xx:<path>', could
                -- never match and rejected 100% of PRs regardless of merit.
                ALTER TABLE brain_proposed_code_fixes
                ADD COLUMN IF NOT EXISTS issue_key       TEXT;
                CREATE INDEX IF NOT EXISTS brain_proposed_recipe_idx
                  ON brain_proposed_code_fixes(recipe_key)
                  WHERE recipe_key IS NOT NULL;
            """)
            conn.commit()
        return True
    except Exception as e:
        print(f"[brain_v2_layer5] init_table: {e}", file=sys.stderr)
        return False


# ────────────────────────────────────────────────────────────────────
# Propose-stage three-state instrumentation (2026-08-07, audit SH52-040).
#
# The 08-07 audit + the Brain Mirror found the loop's stall point: 55
# actionable findings, 0 pending proposals, 0 brain-landed code since ~07-19
# — while brain-layer5.yml ran green every 6h doing nothing. Green-with-
# zero-output must read BLOCKED, not success (the #1921 three-state rule).
# Every learn run now records considered / generated / outcome-histogram;
# a streak of considered>0 → generated==0 files a dup-suppressed brain
# finding, and /api/v1/brain/propose-stage/status exposes the truth for the
# mirror, shell #52 lane I, and humans.
# ────────────────────────────────────────────────────────────────────

_PROPOSE_JAM_STREAK = 6   # runs with work available and zero output → jammed


def _record_propose_run(source: str, considered: int, results: list) -> dict:
    """Persist one propose-stage run + evaluate the zero-output streak.
    Fail-soft: instrumentation must never break the run it measures."""
    from collections import Counter
    outcomes = Counter((r.get("outcome") or "?").split(":")[0]
                       for r in (results or []))
    generated = outcomes.get("proposed", 0)
    row = {"source": source, "considered": considered,
           "generated": generated, "outcomes": dict(outcomes)}
    try:
        import psycopg2
        url = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL"))
        if not url:
            row["recorded"] = False
            return row
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_propose_runs (
                    id           BIGSERIAL PRIMARY KEY,
                    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    run_key      TEXT UNIQUE NOT NULL,
                    source       TEXT NOT NULL,
                    considered   INT NOT NULL,
                    generated    INT NOT NULL,
                    outcomes     JSONB
                )""")
            # run_key = source + UTC minute: a heartbeat double-fire or a
            # caller retry within the window records ONE run, so the jam
            # streak can never be inflated by duplicate rows (and the
            # insert is idempotent per the house ingest rule).
            _rk = "%s:%s" % (source, datetime.now(timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M"))
            cur.execute("""
                INSERT INTO brain_propose_runs
                    (run_key, source, considered, generated, outcomes)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_key) DO NOTHING""",
                (_rk, source, considered, generated,
                 json.dumps(dict(outcomes))))
            # The jam signal: work was AVAILABLE and nothing came out, K runs
            # in a row (across both learn sources — the pipeline is one).
            cur.execute("""
                SELECT considered, generated FROM brain_propose_runs
                 ORDER BY ts DESC LIMIT %s""", (_PROPOSE_JAM_STREAK,))
            recent = cur.fetchall()
            jammed = (len(recent) >= _PROPOSE_JAM_STREAK
                      and all(c > 0 and g == 0 for c, g in recent))
            row["recorded"] = True
            row["jam_streak"] = sum(1 for c, g in recent
                                    if c > 0 and g == 0)
            row["jammed"] = jammed
            if jammed:
                try:
                    from routes.brain_findings_writer import \
                        upsert_brain_finding
                    top = ", ".join("%s=%d" % kv
                                    for kv in outcomes.most_common(3))
                    upsert_brain_finding(
                        cur, issue="propose_stage_jammed",
                        url="/api/v1/brain/propose-stage/status",
                        detail=("%d consecutive runs with findings available "
                                "and 0 proposals generated; dominant "
                                "outcomes: %s. The detect half is feeding a "
                                "propose half that emits nothing — fix the "
                                "dominant rejection cause, do not loosen "
                                "merge gates." % (len(recent), top))[:400],
                        detector="propose_stage_instrumentation")
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        row["recorded"] = False
        row["record_error"] = str(e)[:120]
    return row


@brain_v2_layer5_bp.get("/api/v1/brain/propose-stage/status")
def propose_stage_status():
    """Public read-only: the propose stage's recent three-state truth."""
    out = {"ok": True, "streak_threshold": _PROPOSE_JAM_STREAK, "runs": []}
    try:
        import psycopg2
        url = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL"))
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute("""
                SELECT ts, source, considered, generated, outcomes
                  FROM brain_propose_runs ORDER BY ts DESC LIMIT 30""")
            for ts, src, c, g, oc in cur.fetchall():
                out["runs"].append({
                    "ts": ts.isoformat(), "source": src, "considered": c,
                    "generated": g, "outcomes": oc})
        zero = [r for r in out["runs"][:_PROPOSE_JAM_STREAK]
                if r["considered"] > 0 and r["generated"] == 0]
        out["jam_streak"] = len(zero)
        out["verdict"] = ("JAMMED" if len(zero) >= _PROPOSE_JAM_STREAK
                          else ("no_recent_runs" if not out["runs"]
                                else "flowing"))
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=str(e)[:120])
    # Tag-team routing view: the honest backlog is `active` (findings with
    # no terminal triage outcome); the rest are routed to owners. Fail-soft —
    # the three-state truth above must survive a router error.
    try:
        from routes.brain_finding_router import classify_live
        _cl = classify_live()
        out["routing"] = _cl.get("counts")
        out["honest_backlog"] = (_cl.get("counts") or {}).get("active")
    except Exception:  # noqa: BLE001
        pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_v2_layer5_bp.post("/api/v1/brain/learn-code")
def learn_code():
    """Phase RR-3 one-shot Layer 5 pass.

    For every chronic-stale loop, generate a code-level proposal.
    Stored in brain_proposed_code_fixes; never auto-applied.
    """
    auth_err = _admin_guard()
    if auth_err: return auth_err

    if not ANTHROPIC_API_KEY:
        return jsonify(ok=False, error="no_anthropic_key",
                       hint="Set ANTHROPIC_API_KEY env var on Railway"), 503

    if not _init_table():
        return jsonify(ok=False, error="db_unavailable"), 503

    # Get current loop state via the internal helper from system_loops
    try:
        from routes.system_loops import _gather_loops_internal
        loops = _gather_loops_internal()
    except Exception as e:
        return jsonify(ok=False, error=f"loops_fetch_failed: {e}"), 500

    # Stale/dead loops only — skip healthy ones AND loops that have no
    # source-file map (engagement_track, mcp_traffic have no codepath
    # we can fix; they're real-time signals).
    targets = [l for l in loops
               if l.get("status") in ("stale", "dead")
               and l.get("name") in LOOP_SOURCE_FILES]

    if not targets:
        # considered=0: a no-work run is NOT a jam signal (the streak
        # predicate requires work to have been available).
        _record_propose_run("learn_code", 0, [])
        return jsonify(ok=True, as_of=datetime.now(timezone.utc).isoformat(),
                       skipped=True, reason="no_chronic_stale_loops",
                       loops_examined=len(loops)), 200

    results = []
    for loop in targets:
        name = loop.get("name", "?")
        # Pull source excerpts for the loop's mapped files
        files = LOOP_SOURCE_FILES.get(name, [])
        excerpts = {}
        for fp in files[:2]:  # cap at 2 files per loop to bound token spend
            ex = _read_window(fp, max_chars=4000)
            if ex:
                excerpts[fp] = ex
        if not excerpts:
            results.append({"loop": name, "outcome": "no_source_files",
                            "files_attempted": files})
            continue

        prompt = _build_code_prompt(loop, excerpts, babysitter_log=[])
        text, err = _call_claude(prompt, _LEARN_CODE_SYSTEM)
        if err or not text:
            results.append({"loop": name, "outcome": f"claude_error: {err}"})
            continue

        # Parse JSON (Claude often wraps in code fence)
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            results.append({"loop": name, "outcome": "non_json_response"})
            continue
        try:
            prop = json.loads(m.group(0))
        except Exception as e:
            results.append({"loop": name, "outcome": f"parse_fail: {e}"})
            continue

        # Phase GG#3: normalize (single OR multi-file) → validate every
        # change → store. Shared helper used by both learn endpoints.
        res = _validate_and_store_proposal(name, prop)
        res["loop"] = name
        results.append(res)

    propose_run = _record_propose_run("learn_code", len(targets), results)
    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        loops_examined=len(loops),
        targets_processed=len(targets),
        propose_run=propose_run,
        results=results,
    ), 200


# ────────────────────────────────────────────────────────────────────
# Phase GG (2026-05-14): backend-issue intake. Reads
# actionable_backend_issues from /heal/findings (added in PR #70 —
# backend_cron_scan detector). For each issue, fetches the relevant
# source file and asks Claude for a code-level fix. Same storage
# table + same hard-rules contract as the loop intake above; this
# just feeds a different source of issues into the same engine.
#
# Use case: when DCPI recompute fails with UniqueViolation 4 days
# in a row, backend_cron_scan surfaces it, this endpoint proposes
# a fix (e.g. add ON CONFLICT DO UPDATE), the GH Actions PR opener
# (brain-layer5-pr-opener.yml) picks up high-confidence proposals
# and opens a draft PR for human review.
# ────────────────────────────────────────────────────────────────────

@brain_v2_layer5_bp.post("/api/v1/brain/learn-backend-issues")
def learn_backend_issues():
    """Process actionable_backend_issues from /heal/findings and emit
    code proposals for each. Same storage + contract as learn_code."""
    auth_err = _admin_guard()
    if auth_err: return auth_err

    if not ANTHROPIC_API_KEY:
        return jsonify(ok=False, error="no_anthropic_key"), 503
    if not _init_table():
        return jsonify(ok=False, error="db_unavailable"), 503

    # Fetch backend issues via the same in-process helper Layer 4 uses.
    # (No HTTP self-call — Phase 63 fix pattern.)
    try:
        from flask import current_app
        with current_app.test_client() as _c:
            _r = _c.get("/api/v1/heal/findings")
            findings_payload = _r.get_json() if _r.status_code == 200 else {}
    except Exception as e:
        return jsonify(ok=False, error=f"findings_fetch_failed: {e}"), 500

    backend_issues = findings_payload.get("actionable_backend_issues", []) or []
    if not backend_issues:
        _record_propose_run("learn_backend_issues", 0, [])
        return jsonify(ok=True, skipped=True,
                       reason="no_actionable_backend_issues",
                       as_of=datetime.now(timezone.utc).isoformat()), 200

    # 2026-05-30 WIDEN LEARNING SURFACE: the loop already iterates the full
    # actionable_backend_issues list (~95 live), but only issues whose url is
    # in BACKEND_ISSUE_SOURCE_FILES are code-fixable and reach _call_claude.
    # Bound the model calls to BRAIN_MAX_LEARN (now 10, was effectively 3) so a
    # large finding set can't burn the model budget in one cron tick, while
    # still iterating every issue so unmapped ones get a recorded outcome.
    # Proposals go to the review queue only — no auto-PR / auto-apply here.
    #
    # r78 (2026-06-12) STARVATION FIX: two pathologies starved the 10-call
    # budget for 12 straight days (proposals flatlined 06-05): every cycle
    # burned all calls on the same head-of-list issues, which then failed
    # deterministically (model refusals / validator rejections), while 34
    # findings sat at deferred_rate_cap forever.
    #   1. PERMAFAIL SLOW LANE — issues whose persisted last_outcome is a
    #      deterministic failure retry ~weekly (1 cycle in 28), not every
    #      cycle.
    #   2. ROTATION — start the scan at a cycle-dependent offset so tail
    #      findings get budget even when the head is dense with fixables.
    import time as _t
    import zlib as _zlib
    _cycle_no = int(_t.time() // 21600)            # one tick per 6h cron
    _PERMAFAIL = {"refused", "rejected_false_syntax_claim",
                  "rejected_sqlite_hallucination"}
    try:
        from routes.brain_v2_store import last_outcomes_map as _lom, seen_counts_map as _scm
        _prev_outcomes = _lom()
        _seen_counts = _scm()
    except Exception:
        _prev_outcomes = {}
        _seen_counts = {}
    if len(backend_issues) > 1:
        _rot = _cycle_no % len(backend_issues)
        backend_issues = backend_issues[_rot:] + backend_issues[:_rot]

    results = []
    claude_calls = 0
    for issue in backend_issues:
        # r64 (2026-06-01): populate brain MEMORY (persistence + temporal) from
        # the BACKEND finding stream too. Previously only Layer-4's heal loop
        # wrote these, and it iterates actionable_FRONTEND_issues — currently 0
        # items (site frontend is clean) — so memory_depth was stuck at ~2 rows
        # / 1-of-4 while the real 84-deep backlog flows through HERE untracked.
        # Wrapped so a telemetry hiccup never breaks the learn loop.
        try:
            # 2026-06-10: persistence bump MOVED below the loop so it records the
            # finding's REAL outcome (proposed / config_not_code / no_source_map /
            # deferred_rate_cap) in ONE call. The bare bump here only ever wrote a
            # NULL last_outcome, so ~21 findings showed a permanent false "untried"
            # and re-surfaced every cycle, inflating seen_count. Temporal memory
            # still bumps here (it has no outcome dimension).
            from routes.brain_learning import bump_temporal as _bt
            _bt(issue.get("issue") or "", issue.get("url") or "")
        except Exception:
            pass
        url = issue.get("url", "")          # e.g. dchub://cron/dcpi_recompute
        label = issue.get("issue", "")[:300]

        # S1.4 (2026-06-16) TERMINAL ACKNOWLEDGE — drain the chronically-stuck
        # worklist. A finding parked on a NON-code-fixable outcome (config /
        # availability / no-source / permafail / refused) for many cycles is
        # owner-action or won't-fix; it inflated seen_count (×60+) with no
        # progress and kept the "stuck worklist" perpetually noisy. Past the
        # threshold, ACK it once: stop re-feeding + surface to a human a SINGLE
        # time. The ack outcome is its own once-guard — next cycle the persisted
        # last_outcome is 'terminal_acknowledged', so it silent-skips here.
        _ik = ((issue.get("issue") or "")[:200], url)
        _prev_oc = (_prev_outcomes.get(_ik)
                    or _prev_outcomes.get((issue.get("issue") or "", url)) or "")
        _seen_n = (_seen_counts.get(_ik)
                   or _seen_counts.get((issue.get("issue") or "", url)) or 0)
        if _prev_oc == "terminal_acknowledged":
            results.append({"url": url, "outcome": "terminal_acknowledged"})
            continue
        _TERMINAL_OC = ("config_not_code", "not_code_availability", "no_source_map", "refused")
        if _seen_n >= 40 and (_prev_oc in _TERMINAL_OC or _prev_oc.startswith("skipped_permafail")):
            try:
                from routes.brain_v2_store import log_event as _le_ack
                _le_ack({"issue_label": label[:200], "outcome": "terminal_acknowledged",
                         "url": url,
                         "detail": (f"Stuck on '{_prev_oc}' for {_seen_n} cycles — acknowledged "
                                    f"(owner-action / won't-fix). Surfaced once; dropped from the "
                                    f"active learn worklist so it stops churning.")})
            except Exception:
                pass
            results.append({"url": url, "outcome": "terminal_acknowledged"})
            continue

        # Gap A (Phase RR-4): cron-schedule / workflow-config findings are
        # NOT Python code bugs — they're scheduling/config issues living in
        # .github/workflows/*.yml (the model correctly refuses to "fix" a
        # cron collision inside a Flask route). Route them away from the
        # code-fixer instead of burning a Claude call on a guaranteed
        # refusal. Detect a bare 5-field cron expression as the url, a yaml
        # workflow path, or a schedule/cron concern named in the label.
        import re as _re
        _u = (url or "").strip()
        _lbl = (label or "").lower()
        _is_cron_expr = (len(_u.split()) == 5
                         and bool(_re.match(r'^[\d*/,\-\s]+$', _u)))
        if (_is_cron_expr
                or _u.endswith(('.yml', '.yaml'))
                or '.github/workflows/' in _u
                or 'cron_schedule_collision' in _lbl
                or 'cron_phase_missing_schedule' in _lbl
                or ('schedule' in _lbl and 'cron' in _lbl)):
            results.append({"url": url, "outcome": "config_not_code"})
            continue

        # r78b: AVAILABILITY / METRIC findings are not source-code bugs — a
        # page being unreachable/slow, a health probe down, or a KPI below
        # floor cannot be fixed by editing a code window, so the model
        # always (correctly) refuses. Feeding them to the code-fixer was
        # burning the 10-call budget on guaranteed refusals and STARVING the
        # genuinely code-fixable findings into deferred_rate_cap. Route them
        # out to autopilot/operational handling instead of a Claude call.
        # (These surface transiently after every deploy when self-probes hit
        # the restarting backend — pages verified live-up; not real outages.)
        _AVAIL_PREFIXES = (
            'frontend_endpoint_unreachable', 'frontend_endpoint_slow',
            'multi_cloud_both_down', 'native_discoverability_surface_down',
            'endpoint_unreachable', 'endpoint_slow', 'site_url_unhealthy',
            'page_unreachable',
        )
        if any(_lbl.startswith(p) for p in _AVAIL_PREFIXES):
            results.append({"url": url, "outcome": "not_code_availability"})
            continue

        # Permafail slow lane (see r78 note above the loop): a finding whose
        # last attempt ended in a deterministic failure gets retried roughly
        # weekly — same window + same prompt will just refuse again, and the
        # 3 unsafe_db_conn_pattern refusals alone were eating 3 of 10 calls
        # every cycle. crc32 (not hash()) so the lane is stable across
        # worker restarts despite PYTHONHASHSEED randomization.
        _prev = (_prev_outcomes.get(((issue.get("issue") or "")[:200], url))
                 or _prev_outcomes.get((issue.get("issue") or "", url)))
        if (_prev in _PERMAFAIL
                and (_cycle_no % 28) !=
                    (_zlib.crc32(f"{label}|{url}".encode()) % 28)):
            results.append({"url": url,
                            "outcome": f"skipped_permafail:{_prev}"})
            continue

        # Map to source files. First try the static hand-curated map;
        # then fall back to the source-mapper (Phase RR-4), which resolves
        # the abstract finding (url path / route / filename / table / text)
        # to concrete (file, line) candidates by walking the repo. This is
        # what turns the ~87 chronic "no_source_map" findings into real,
        # human-reviewed proposals.
        source_files = BACKEND_ISSUE_SOURCE_FILES.get(url, [])
        # source_locations pairs each file with an optional line, so the
        # excerpt can be CENTERED on the code the mapper found (gap B).
        source_locations = [(f, None) for f in source_files]
        # 2026-06-04: coverage_gap_gas:XX findings carry the state code as the
        # url (e.g. "RI"), which the route/file/table indexes can't resolve.
        # Hand-curate the seeder file so these stop short-circuiting to
        # no_source_map after 16-31 cycles untried.
        _lbl_lc = (label or "").strip().lower()
        if not source_locations and _lbl_lc.startswith("coverage_gap_gas"):
            source_locations = [("routes/powered_land_gas.py", None)]
        # r37 (2026-05-31): DETERMINISTIC filename resolution BEFORE the heuristic
        # mapper. Many code-fixable findings name their file directly (e.g.
        # unsafe_db_conn_pattern: url='ai_interconnection.py', detail names it).
        # The heuristic resolver's text/symbol fallback landed on the WRONG file
        # in prod — the live learn-backend-issues run handed Claude snippets from
        # news_digests_read.py / partnership_email_drafts.py for an
        # ai_interconnection.py finding, so Claude correctly REFUSED every one and
        # ~0 useful proposals shipped. Resolve any named .py file to its real repo
        # path first; only fall back to the mapper when no concrete file is named.
        if not source_locations:
            import re as _re_fn, os as _os_fn
            _repo = _os_fn.path.dirname(_os_fn.path.dirname(_os_fn.path.abspath(__file__)))
            _text = " ".join(str(issue.get(k) or "") for k in ("url", "detail", "issue"))
            for _fn in _re_fn.findall(r"\b([a-z][a-z0-9_]*\.py)\b", _text):
                for _cand in (_fn, _os_fn.path.join("routes", _fn)):
                    if (_os_fn.path.exists(_os_fn.path.join(_repo, _cand))
                            and _cand not in [p for p, _ in source_locations]):
                        source_locations.append((_cand, None))
                if len(source_locations) >= 2:
                    break
        if not source_locations:
            try:
                from routes.brain_source_map import resolve_finding_to_sources
                resolved_candidates = resolve_finding_to_sources(issue)
            except Exception:
                resolved_candidates = []
            # De-dupe by file (preserve rank order), cap at 2 to bound token
            # spend — and KEEP the mapper's line so the excerpt is centered.
            seen_f = set()
            for c in resolved_candidates:
                f = c.get("file")
                if f and f not in seen_f:
                    seen_f.add(f)
                    source_locations.append((f, c.get("line")))
                if len(source_locations) >= 2:
                    break
        if not source_locations:
            results.append({"url": url, "outcome": "no_source_map"})
            continue

        # Rate-cap the code-fixable issues fed to the model this cycle. Issues
        # beyond the cap are deferred to the next cron tick (recorded, not lost).
        if claude_calls >= BRAIN_MAX_LEARN:
            results.append({"url": url, "outcome": "deferred_rate_cap"})
            continue

        excerpts = {}
        for fp, line in source_locations[:2]:
            ex = _read_window(fp, max_chars=4000, center_line=line)
            if ex:
                excerpts[fp] = ex
        if not excerpts:
            results.append({"url": url, "outcome": "no_readable_source"})
            continue

        # Synthesize a "loop_state" shape so we can reuse _build_code_prompt
        loop_state = {
            "name": url.replace("dchub://cron/", ""),
            "status": "failing",
            "error": label,
            "age_hours": None,
            "note": f"backend_cron_scan issue: {label[:200]}",
        }
        # 2026-06-04: inject issue-type-specific guidance for the two stuck
        # categories (shadowed_route + coverage_gap_gas:XX). Appended to the
        # generic prompt so Claude diagnoses the right shape of fix.
        _hint = _hint_for_issue(label)
        prompt = _build_code_prompt(loop_state, excerpts, babysitter_log=[])
        if _hint:
            prompt = prompt + _hint
        claude_calls += 1
        text, err = _call_claude(prompt, _LEARN_CODE_SYSTEM)
        if err or not text:
            # err now carries the full exception repr from _call_claude
            # (egress diagnostics fix) so api_error rows are diagnosable.
            results.append({"url": url, "outcome": f"claude_error: {err}"})
            continue

        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            results.append({"url": url, "outcome": "non_json_response"})
            continue
        try:
            prop = json.loads(m.group(0))
        except Exception as e:
            results.append({"url": url, "outcome": f"parse_fail: {e}"})
            continue

        # Phase GG#3: shared normalize → validate → store pipeline.
        # Handles single-file AND multi-file proposals identically.
        # loop_state["name"] is the finding URL, NOT the finding key — pass the
        # issue explicitly so it survives into the proposal and the draft PR.
        res = _validate_and_store_proposal(loop_state["name"], prop,
                                            issue_key=(issue.get("issue") or ""))
        res["url"] = url
        results.append(res)

    # 2026-06-10: persist each finding's REAL outcome to brain_issue_persistence.
    # `results` is 1:1 and index-aligned with `backend_issues` (every issue path
    # appends exactly once), so zip pairs each finding with its outcome. Writing
    # the true outcome lets most_persistent_unfixed() DROP findings that were
    # actually 'proposed' and show the rest by their real reason — instead of the
    # permanent false "untried" the bare top-of-loop bump produced. ONE bump per
    # finding per cycle (no double-increment). Never breaks the learn response.
    try:
        from routes.brain_v2_store import (bump_persistence as _bp_oc,
                                           log_event as _le_oc,
                                           MAX_ISSUE_LABEL_LEN as _MIL_oc)
        for _iss, _res in zip(backend_issues, results):
            _oc = (_res.get("outcome") or _res.get("status") or "").strip()
            _bp_oc(issue_label=(_iss.get("issue") or "")[:_MIL_oc],
                   url=_iss.get("url") or "",
                   last_outcome=(_oc or None))
            # r78: Layer-5 now writes brain_learning_log too. Before this,
            # the backend learn path had NO log writer, so last_log_at froze
            # (2026-05-29) while ~40 real learn calls/day ran invisibly and
            # /brain/status kept reporting a stale log as "healthy". Skip
            # the high-volume non-attempt outcomes so the log stays signal.
            if _oc and _oc not in ("deferred_rate_cap", "config_not_code",
                                   "no_source_map", "not_code_availability") \
                   and not _oc.startswith("skipped_permafail"):
                try:
                    _le_oc({"issue_label": (_iss.get("issue") or "")[:_MIL_oc],
                            "outcome": _oc[:200],
                            "url": _iss.get("url") or ""})
                except Exception:
                    pass
    except Exception:
        pass

    propose_run = _record_propose_run("learn_backend_issues",
                                      len(backend_issues), results)
    # Escalation ladder step 1 (tag-team charter): after each learn pass,
    # hand the mcp-server-owned findings to their repo as ONE deduped issue.
    # Daily-guarded inside sync_mcp_issue; fail-soft — routing must never
    # cost us the learn run's own result.
    mcp_issue_sync = None
    try:
        from routes.brain_finding_router import sync_mcp_issue
        mcp_issue_sync = sync_mcp_issue()
    except Exception:  # noqa: BLE001
        pass
    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        issues_examined=len(backend_issues),
        claude_calls=claude_calls,
        max_learn_per_cycle=BRAIN_MAX_LEARN,
        propose_run=propose_run,
        mcp_issue_sync=mcp_issue_sync,
        results=results,
    ), 200


# ────────────────────────────────────────────────────────────────────
# Phase GG#2 (2026-05-14): confidence calibration.
#
# GG#4 populates merge_outcome ∈ {merged_healthy, merged_reverted} on
# each proposal after its PR merges + Railway redeploys. This block
# turns that history into a SELF-TUNING per-source threshold.
#
# The intuition: the brain proposes fixes for different "sources"
# (loop_name — e.g. dcpi_recompute, auto_press_daily). Some sources
# the brain understands well; others it keeps getting wrong. Rather
# than a flat 0.85 confidence bar for everything, each source earns
# its own threshold from its track record:
#
#   - source with all merged_healthy → LOWER threshold (trust it more,
#     let its lower-confidence proposals through to a PR)
#   - source with reverts            → RAISE threshold (trust it less,
#     only its most-confident proposals get a PR)
#
# Under 3 resolved outcomes for a source → not enough data → base 0.85.
# ────────────────────────────────────────────────────────────────────

_CALIB_BASE_THRESHOLD = 0.85
_CALIB_ADJ_RANGE = 0.30          # ±0.15 swing around the base
_CALIB_MIN_SAMPLES = 3           # need ≥3 resolved outcomes to tune
_CALIB_FLOOR = 0.70              # never auto-PR below this
_CALIB_CEIL = 0.99               # effectively "blocked" upper bound


def _calibration_stats(cur) -> dict:
    """Per-source merge-outcome tally + computed threshold. cur is an
    open psycopg2 cursor. Returns {loop_name: {...}}."""
    try:
        cur.execute("""
            SELECT loop_name,
                   COUNT(*) FILTER (WHERE merge_outcome = 'merged_healthy')  AS healthy,
                   COUNT(*) FILTER (WHERE merge_outcome = 'merged_reverted') AS reverted,
                   COUNT(*) FILTER (WHERE merge_outcome IS NOT NULL)         AS resolved,
                   COUNT(*)                                                 AS total
            FROM brain_proposed_code_fixes
            GROUP BY loop_name
        """)
        rows = cur.fetchall()
    except Exception:
        # merge_outcome column may not exist yet (no GG#4 callbacks fired).
        # CRITICAL: roll back the failed statement so it doesn't leave the
        # caller's transaction ABORTED — that aborted txn silently broke
        # pending-pr for the loop's ENTIRE history (its later SELECT raised
        # "current transaction is aborted" → caught → items:[] → the PR
        # opener saw 0 forever). Degrade gracefully → base threshold.
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return {}

    out = {}
    for loop_name, healthy, reverted, resolved, total in rows:
        healthy = healthy or 0
        reverted = reverted or 0
        resolved = resolved or 0
        if resolved >= _CALIB_MIN_SAMPLES:
            trust_ratio = healthy / resolved          # 0.0 .. 1.0
            # ratio 1.0 → threshold base − 0.15  (more permissive)
            # ratio 0.5 → threshold base
            # ratio 0.0 → threshold base + 0.15  (more strict)
            threshold = _CALIB_BASE_THRESHOLD - (trust_ratio - 0.5) * _CALIB_ADJ_RANGE
            threshold = max(_CALIB_FLOOR, min(_CALIB_CEIL, threshold))
            tuned = True
        else:
            trust_ratio = None
            threshold = _CALIB_BASE_THRESHOLD
            tuned = False
        out[loop_name] = {
            "healthy": healthy,
            "reverted": reverted,
            "resolved": resolved,
            "total_proposed": total or 0,
            "trust_ratio": round(trust_ratio, 3) if trust_ratio is not None else None,
            "threshold": round(threshold, 3),
            "tuned": tuned,
        }
    return out


# ---------------------------------------------------------------------------
# Item G (2026-06-02): TTL escalation.
#
# When the same recipe_key has been seen >=5 cycles without ever crossing
# the approval gate (status='proposed' for the whole window), sonnet has
# demonstrably failed to land it. Surface those rows on the opus retry
# queue so a higher-capability pass can take a shot. The escalation is
# idempotent — every poll of /api/v1/brain/opus-queue lazily flips status
# to 'unfixable_by_sonnet' for the matching rows before returning them.
# ---------------------------------------------------------------------------

_OPUS_TTL_CYCLES = 5


def _escalate_ttl_breaches(cur) -> int:
    """Flip status -> 'unfixable_by_sonnet' on rows that have been seen
    at least _OPUS_TTL_CYCLES times without ever progressing past
    'proposed'. Returns the number of rows escalated. Safe to call on
    every read of the opus queue (idempotent)."""
    try:
        cur.execute("""
            UPDATE brain_proposed_code_fixes
               SET status = 'unfixable_by_sonnet'
             WHERE COALESCE(cycles_seen, 0) >= %s
               AND COALESCE(status, 'proposed') = 'proposed'
        """, (_OPUS_TTL_CYCLES,))
        return cur.rowcount or 0
    except Exception:
        note_swallowed_write("brain_proposed_code_fixes", where="brain_v2_layer5._escalate_ttl_breaches")
        return 0


@brain_v2_layer5_bp.get("/api/v1/brain/opus-queue")
def brain_opus_queue():
    """Item G (2026-06-02): findings seen >=5 cycles without crossing
    the approval gate are flipped to 'unfixable_by_sonnet' and surfaced
    here for opus retry. Idempotent; the escalation runs lazily on each
    poll so the queue is always current."""
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(ok=False, error="no_db_url",
                           as_of=datetime.now(timezone.utc).isoformat()), 503
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            escalated = _escalate_ttl_breaches(cur)
            conn.commit()
            cur.execute("""
                SELECT id, loop_name, file_path, search_text, replace_text,
                       rationale, recipe_key, finding_class,
                       approval_count, cycles_seen, status,
                       proposed_at, last_seen_at
                  FROM brain_proposed_code_fixes
                 WHERE status = 'unfixable_by_sonnet'
                 ORDER BY cycles_seen DESC, proposed_at ASC
                 LIMIT 100
            """)
            rows = cur.fetchall() or []
        items = [{
            "id": int(r[0]),
            "loop_name": r[1],
            "file_path": r[2],
            "search_preview": (r[3] or "")[:200],
            "replace_preview": (r[4] or "")[:200],
            "rationale": (r[5] or "")[:240],
            "recipe_key": r[6],
            "finding_class": r[7],
            "approval_count": int(r[8] or 0),
            "cycles_seen": int(r[9] or 0),
            "status": r[10],
            "proposed_at": r[11].isoformat() if r[11] else None,
            "last_seen_at": r[12].isoformat() if r[12] else None,
        } for r in rows]
        return jsonify(
            ok=True,
            as_of=datetime.now(timezone.utc).isoformat(),
            ttl_cycles=_OPUS_TTL_CYCLES,
            escalated_this_call=escalated,
            count=len(items),
            note=("Findings seen >=" + str(_OPUS_TTL_CYCLES) +
                  " cycles without crossing the sonnet approval gate. "
                  "Surface to opus retry."),
            items=items,
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200],
                       as_of=datetime.now(timezone.utc).isoformat()), 500


@brain_v2_layer5_bp.get("/api/v1/brain/templated-recipes")
def brain_templated_recipes():
    """Item G-followup (2026-06-02): expose L5 proposals with a stable
    recipe_key for the master-heal workflow to pick up.

    Returns proposals filtered to either:
      - status='approved' (human-blessed), OR
      - approval_count >= 2 AND finding_class IN a safe whitelist
        (the same whitelist used in layer4 to permit HTML-introducing
        rewrites).

    Groups by recipe_key. The newest row in each group is the one
    surfaced (latest text + the running approval_count/cycles_seen).
    """
    # Same whitelist as routes/brain_v2_layer4.py:189 (single source
    # of truth there; mirrored here so the response shape stays stable
    # if layer4 is refactored).
    _SAFE_FINDING_CLASSES = (
        "brand_uniformity", "brand_uniform",
        "class_addition", "brand-uniformity",
    )
    # 2026-06-04 FIX: the 2-cycle gate ABOVE only fires when finding_class
    # is in the 4-entry brand-uniformity whitelist. That means a 0.95
    # confidence boot-guard syntax fix (finding_class is "boot_syntax" /
    # "loop_stale" / etc — outside the whitelist) seen even 5+ cycles in
    # a row still doesn't auto-merge: the proposer keeps re-proposing the
    # SAME exact fix every cron tick (observed: id X proposed 2026-06-04
    # AND 2026-06-05 with identical search/replace). Add a confidence-
    # bypass: a proposal at >=0.95 confidence after a single cycle is as
    # trustworthy as a brand-whitelist proposal after 2 cycles. Log the
    # gate decision per-row so the dashboard / logs show WHY a proposal
    # cleared (or didn't).
    _HIGH_CONFIDENCE_BYPASS = 0.95
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(ok=False, error="no_db_url",
                           recipes=[], total=0,
                           as_of=datetime.now(timezone.utc).isoformat()), 503
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            # DISTINCT ON (recipe_key) keeps the newest row per group.
            # WHERE clause clears a recipe if ANY of:
            #   (a) status='approved' (human-blessed)
            #   (b) approval_count>=2 AND finding_class in brand whitelist
            #       (the original safe-finding-class path)
            #   (c) confidence>=0.95 (high-confidence bypass — added
            #       2026-06-04 because real high-confidence syntax fixes
            #       outside the brand whitelist were stuck in proposed
            #       indefinitely. confidence is the proposer's own self-
            #       assessment + survives _validate_and_store_proposal's
            #       SQLite-hallucination + search-not-found guards.)
            cur.execute("""
                SELECT DISTINCT ON (recipe_key)
                       recipe_key, file_path, finding_class,
                       search_text, replace_text,
                       approval_count, cycles_seen, status, proposed_at,
                       confidence
                  FROM brain_proposed_code_fixes
                 WHERE recipe_key IS NOT NULL
                   AND (
                        status = 'approved'
                     OR (COALESCE(approval_count, 0) >= 2
                         AND COALESCE(LOWER(finding_class), '')
                             = ANY(%s))
                     OR COALESCE(confidence, 0.0) >= %s
                   )
                 ORDER BY recipe_key, proposed_at DESC
                 LIMIT 500
            """, (list(_SAFE_FINDING_CLASSES), _HIGH_CONFIDENCE_BYPASS))
            rows = cur.fetchall() or []
            # Per-row gate reasoning. Visible in Railway logs so the operator
            # can audit WHY each surfaced recipe cleared (or, if none did,
            # diagnose what bar each candidate fell short of).
            for _r in rows:
                _fc = (_r[2] or "").lower()
                _ac = int(_r[5] or 0)
                _st = _r[7]
                _cf = float(_r[9] or 0.0)
                if _st == "approved":
                    _reason = "human_approved"
                elif _cf >= _HIGH_CONFIDENCE_BYPASS:
                    _reason = f"high_confidence_bypass(conf={_cf:.2f})"
                elif _ac >= 2 and _fc in _SAFE_FINDING_CLASSES:
                    _reason = f"safe_class_2cycle({_fc},approval={_ac})"
                else:
                    _reason = "unknown"
                print(f"[brain-l5-gate] recipe={_r[0][:40]!s} "
                      f"file={_r[1]} cleared_by={_reason}",
                      file=sys.stderr)
        recipes = [{
            "recipe_key": r[0],
            "page_url_template": r[1],
            "finding_class": r[2],
            "find_text": r[3],
            "replace_text": r[4],
            "approval_count": int(r[5] or 0),
            "cycles_seen": int(r[6] or 0),
            "status": r[7],
        } for r in rows]
        return jsonify(
            ok=True,
            as_of=datetime.now(timezone.utc).isoformat(),
            whitelist=list(_SAFE_FINDING_CLASSES),
            recipes=recipes,
            total=len(recipes),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200],
                       recipes=[], total=0,
                       as_of=datetime.now(timezone.utc).isoformat()), 500


@brain_v2_layer5_bp.get("/api/v1/brain/calibration")
def brain_calibration():
    """Phase GG#2: public read of the per-source confidence calibration.
    Surfaces on the /brain dashboard so a human can see which proposal
    sources the brain has earned trust on. No secrets in the response."""
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(as_of=datetime.now(timezone.utc).isoformat(),
                           base_threshold=_CALIB_BASE_THRESHOLD,
                           sources={}), 200
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            stats = _calibration_stats(cur)
        return jsonify(
            as_of=datetime.now(timezone.utc).isoformat(),
            base_threshold=_CALIB_BASE_THRESHOLD,
            min_samples_to_tune=_CALIB_MIN_SAMPLES,
            floor=_CALIB_FLOOR,
            ceil=_CALIB_CEIL,
            sources=stats,
            note=("Threshold per source self-tunes from merge_outcome "
                  "history. Sources with a clean merged_healthy record "
                  "get a lower bar; sources that produced reverts get a "
                  "higher bar. Under 3 resolved outcomes → base 0.85."),
        ), 200
    except Exception as e:
        return jsonify(error=str(e)[:200], sources={}), 200


@brain_v2_layer5_bp.get("/api/v1/brain/proposed-code/pending-pr")
def proposed_code_pending_pr():
    """Phase GG (2026-05-14): high-confidence proposals that don't yet
    have a PR opened. Consumed by the brain-layer5-pr-opener GH Actions
    workflow which checks out each one's file, applies the search→replace,
    commits to a new branch, and opens a draft PR.

    Phase GG#2 (2026-05-14): the confidence bar is now a SELF-TUNING
    per-source threshold (see _calibration_stats above), not a flat
    value. The `min_confidence` query param is still honored but as a
    FLOOR — a source's tuned threshold can be stricter than it, never
    looser. So a brand-new untested source uses base 0.85; a source
    with a clean track record can clear PRs at 0.70.

    Admin-gated because the response includes the full code proposal
    (search/replace text — could leak partial source for paid endpoints).
    """
    auth_err = _admin_guard()
    if auth_err: return auth_err

    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(items=[]), 200
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS pr_url TEXT;
                """)
                # Root fix: the calibration query reads merge_outcome; if the
                # live table predates that column, the query aborts the txn
                # and pending-pr returns 0. Ensure it exists.
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS merge_outcome TEXT;
                """)
                # Same reason: a live table predating issue_key would abort this
                # SELECT and pending-pr would return 0 with no error surfaced.
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS issue_key TEXT;
                """)
                conn.commit()
            except Exception:
                conn.rollback()

            # Phase GG#2: compute per-source tuned thresholds.
            calib = _calibration_stats(cur)
            # `min_confidence` query param acts as a hard floor — the
            # caller can demand stricter, never looser, than calibration.
            floor_conf = float(request.args.get("min_confidence", "0.0") or 0.0)
            limit = int(request.args.get("limit", "10"))

            # Pull ALL un-PR'd proposed rows, then filter in Python using
            # the per-source threshold (SQL can't easily do per-row
            # dynamic thresholds without a CASE the length of the source
            # list — Python is clearer and the row count is tiny).
            # Phase GG#3: also pull changes_json so the PR-opener can
            # apply multi-file proposals. Older single-file rows have
            # changes_json NULL — the opener falls back to the legacy
            # file_path/search_text/replace_text columns for those.
            cur.execute("""
                SELECT id, loop_name, file_path, search_text, replace_text,
                       rationale, confidence, status, proposed_at,
                       changes_json, issue_key
                FROM brain_proposed_code_fixes
                WHERE pr_url IS NULL
                  AND COALESCE(status, 'proposed') = 'proposed'
                ORDER BY confidence DESC, proposed_at DESC
            """)
            rows = cur.fetchall()

        items = []
        for r in rows:
            loop_name = r[1]
            confidence = r[6] or 0.0
            src_threshold = calib.get(loop_name, {}).get(
                "threshold", _CALIB_BASE_THRESHOLD)
            effective = max(src_threshold, floor_conf)
            if confidence < effective:
                continue
            # changes_json is JSONB → psycopg2 returns it already-parsed
            # (list of dicts) OR as a string depending on driver version.
            raw_changes = r[9]
            if isinstance(raw_changes, str):
                try:
                    raw_changes = json.loads(raw_changes)
                except Exception:
                    raw_changes = None
            if not raw_changes:
                # Legacy single-file row → synthesize the canonical shape
                raw_changes = [{
                    "file": r[2], "search": r[3], "replace": r[4],
                }]
            items.append({
                "id": r[0], "loop_name": loop_name, "file_path": r[2],
                "search_text": r[3], "replace_text": r[4],
                "changes": raw_changes,
                "file_count": len(raw_changes),
                "rationale": r[5], "confidence": confidence,
                "status": r[7],
                "proposed_at": r[8].isoformat() if r[8] else None,
                "threshold_applied": round(effective, 3),
                "source_tuned": calib.get(loop_name, {}).get("tuned", False),
                # The SOURCE FINDING key. The draft-PR writer emits this into
                # the PR body so sentinel_auto_merge's GATE_SENTINEL_DERIVED can
                # recognise a sentinel-derived fix. loop_name is the finding URL
                # and will never satisfy that gate.
                "issue_key": r[10],
            })
            if len(items) >= limit:
                break

        return jsonify(
            as_of=datetime.now(timezone.utc).isoformat(),
            count=len(items), items=items,
            calibration_in_effect={k: v["threshold"] for k, v in calib.items()},
        ), 200
    except Exception as e:
        return jsonify(error=str(e)[:200], items=[]), 200


@brain_v2_layer5_bp.post("/api/v1/brain/proposed-code/<int:proposal_id>/mark-pr")
def mark_proposal_pr(proposal_id):
    """Phase GG: GH Actions PR opener calls this after successfully
    opening a draft PR for a proposal, so the same proposal isn't
    picked up on the next tick.
    """
    auth_err = _admin_guard()
    if auth_err: return auth_err

    body = request.get_json(silent=True) or {}
    pr_url = (body.get("pr_url") or "").strip()
    if not pr_url:
        return jsonify(ok=False, error="pr_url required"), 400

    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE brain_proposed_code_fixes
                SET pr_url = %s,
                    status = 'pr_opened'
                WHERE id = %s
                RETURNING id
            """, (pr_url, proposal_id))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return jsonify(ok=False, error="proposal_not_found"), 404
        # Phase r60-evolution: notify on L5 proposal acceptance.
        try:
            from routes.brain_evolution import log_notification as _logn
            _logn(
                kind="l5_proposal_pr_opened",
                summary=f"Brain Layer 5 proposal #{proposal_id} → PR opened",
                detail={"proposal_id": proposal_id, "pr_url": pr_url},
                url=pr_url,
                severity="win",
            )
        except Exception:
            pass
        return jsonify(ok=True, proposal_id=proposal_id, pr_url=pr_url), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


@brain_v2_layer5_bp.post("/api/v1/brain/proposed-code/<int:proposal_id>/mark-merge-outcome")
def mark_proposal_merge_outcome(proposal_id):
    """Phase GG#4 (2026-05-14): record what happened AFTER a brain PR
    merged. The brain-pr-post-merge-guard workflow calls this once
    Railway has redeployed and /api/health has been probed.

    outcome ∈ {merged_healthy, merged_reverted}

    This is the feedback signal GG#2 (confidence calibration) reads:
      - merged_healthy  → the brain's fix was good. Trust ↑.
      - merged_reverted → the brain's fix broke prod + got auto-reverted.
                          Trust ↓.
    Over time the merged_healthy ratio per source (loop_name prefix,
    confidence band) tunes the auto-PR threshold.

    Adds a `merge_outcome` + `merge_outcome_at` column defensively —
    same idempotent ALTER pattern used elsewhere in this module.
    """
    auth_err = _admin_guard()
    if auth_err: return auth_err

    body = request.get_json(silent=True) or {}
    outcome = (body.get("outcome") or "").strip()
    detail = (body.get("detail") or "").strip()[:500]
    if outcome not in ("merged_healthy", "merged_reverted"):
        return jsonify(ok=False,
                       error="outcome must be merged_healthy or merged_reverted"), 400

    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS merge_outcome TEXT;
                """)
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS merge_outcome_at TIMESTAMPTZ;
                """)
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS merge_outcome_detail TEXT;
                """)
                conn.commit()
            except Exception:
                conn.rollback()
            cur.execute("""
                UPDATE brain_proposed_code_fixes
                SET merge_outcome = %s,
                    merge_outcome_at = NOW(),
                    merge_outcome_detail = %s,
                    status = %s
                WHERE id = %s
                RETURNING id
            """, (outcome, detail,
                  'merged_healthy' if outcome == 'merged_healthy' else 'reverted',
                  proposal_id))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return jsonify(ok=False, error="proposal_not_found"), 404
        # r64 (2026-06-01): bridge the merge outcome into brain_fix_outcomes so
        # self-assessment fix_success finally has a denominator. The prober
        # (probe_outcomes) only checks Layer-4 TEXT proposals — of which there
        # are 0 — so fix_success read null forever, even though CODE PRs DO
        # carry a real merged_healthy/reverted signal; it just never reached the
        # outcomes table the grade reads. Wrapped so it can't fail the endpoint.
        try:
            from routes.brain_learning import record_proposal_outcome
            record_proposal_outcome(
                proposal_id, 'code',
                still_broken=(outcome == 'merged_reverted'),
                evidence_note=f"post-merge guard: {outcome}")
        except Exception:
            pass
        return jsonify(ok=True, proposal_id=proposal_id, outcome=outcome), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


@brain_v2_layer5_bp.get("/api/v1/brain/proposed-code")
def proposed_code():
    """Public read of recent Layer 5 proposals. Surfaces on the
    /brain dashboard alongside the existing Layer 4 proposed-fixes."""
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(as_of=datetime.now(timezone.utc).isoformat(),
                           count=0, items=[],
                           hint="DATABASE_URL not set"), 200
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, loop_name, file_path, search_text, replace_text,
                           rationale, confidence, status, proposed_at
                    FROM brain_proposed_code_fixes
                    ORDER BY proposed_at DESC LIMIT 25
                """)
                rows = cur.fetchall()
        items = [{
            "id": r[0], "loop_name": r[1], "file_path": r[2],
            "search_preview": (r[3] or "")[:200],
            "replace_preview": (r[4] or "")[:200],
            "rationale": r[5], "confidence": r[6], "status": r[7],
            "proposed_at": r[8].isoformat() if r[8] else None,
        } for r in rows]
        return jsonify(
            as_of=datetime.now(timezone.utc).isoformat(),
            count=len(items), items=items,
        ), 200
    except Exception as e:
        return jsonify(error=str(e)[:200], items=[]), 200
