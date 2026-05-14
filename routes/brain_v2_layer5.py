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
    _call_claude, ANTHROPIC_API_KEY, BRAIN_MODEL, _require_admin,
)

brain_v2_layer5_bp = Blueprint("brain_v2_layer5", __name__)


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


_LEARN_CODE_SYSTEM = (
    "You are Brain v2 Layer 5 — the code-fix proposal engine for DC Hub's "
    "autonomous loops. A loop has been stale or dead across multiple cron-"
    "babysitter runs even though the cron is firing successfully. That "
    "means there's a silent correctness bug INSIDE the loop's handler — "
    "not a missing endpoint, not auth, but a code-level problem that the "
    "lower self-healing layers cannot reach.\n\n"
    "You will receive: (1) the loop name and its last error, (2) source "
    "code from the relevant file(s), (3) recent babysitter action log.\n\n"
    "Return a STRICT JSON proposal:\n"
    '  {"file": "path/to/file.py",\n'
    '   "search": "<exact substring that must appear in the file verbatim>",\n'
    '   "replace": "<the new code that replaces it>",\n'
    '   "rationale": "<one or two sentences>",\n'
    '   "confidence": <0.0..1.0>}\n\n'
    "Hard rules:\n"
    "  - `search` MUST appear verbatim in the file — no fuzzy matches.\n"
    "  - Include enough context (3-6 lines) in `search` so the substitution "
    "    is unambiguous; the file likely has multiple similar lines.\n"
    "  - `replace` must be syntactically valid Python.\n"
    "  - Do NOT propose changes to imports, decorators, function signatures, "
    "    or anything outside the body of the loop's handler.\n"
    "  - If you cannot isolate the bug from the snippet provided, refuse: "
    '    return {"file":"","search":"","replace":"","rationale":"refused: <why>",'
    '"confidence":0}. Refusing is better than guessing.\n"'
    "  - This proposal will NOT be auto-applied. A human reviews it. "
    "    But low-quality refusals waste their time, so be specific.\n"
)


def _read_window(path: str, max_chars: int = 4000) -> str:
    """Read up to max_chars from `path`. Returns empty string on any
    failure — Layer 5 should keep going even if one file is missing."""
    try:
        # Resolve relative to the repo root (assumed CWD on Railway).
        full = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), path)
        if not os.path.exists(full):
            return ""
        with open(full, encoding="utf-8") as f:
            data = f.read()
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
            conn.commit()
        return True
    except Exception as e:
        print(f"[brain_v2_layer5] init_table: {e}", file=sys.stderr)
        return False


@brain_v2_layer5_bp.post("/api/v1/brain/learn-code")
def learn_code():
    """Phase RR-3 one-shot Layer 5 pass.

    For every chronic-stale loop, generate a code-level proposal.
    Stored in brain_proposed_code_fixes; never auto-applied.
    """
    auth_err = _require_admin()
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

        file_path = (prop.get("file") or "").strip()
        search = (prop.get("search") or "").strip()
        replace = (prop.get("replace") or "").strip()
        rationale = (prop.get("rationale") or "").strip()
        confidence = float(prop.get("confidence", 0) or 0)

        # Empty file/search = explicit refusal per the prompt contract.
        if not file_path or not search:
            results.append({"loop": name, "outcome": "refused",
                            "rationale": rationale[:200]})
            continue

        # Defensive: search must actually appear in the live file. Else
        # the proposal is hallucinated.
        live = _read_window(file_path, max_chars=200000)
        if search not in live:
            results.append({"loop": name, "outcome": "search_not_found",
                            "file": file_path, "rationale": rationale[:200],
                            "search_preview": search[:100]})
            continue

        # Store the proposal — UNIQUE constraint dedupes same proposal
        # across repeated runs (Layer 5 retries are no-ops on already-
        # proposed fixes).
        try:
            import psycopg2
            url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
            with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO brain_proposed_code_fixes
                        (loop_name, file_path, search_text, replace_text,
                         rationale, confidence, model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (loop_name, file_path, search_text) DO NOTHING
                    RETURNING id
                """, (name, file_path, search, replace, rationale,
                      confidence, BRAIN_MODEL))
                row = cur.fetchone()
                conn.commit()
            results.append({
                "loop": name, "outcome": "proposed" if row else "duplicate",
                "id": row[0] if row else None, "file": file_path,
                "confidence": confidence, "rationale": rationale[:200],
            })
        except Exception as e:
            results.append({"loop": name,
                            "outcome": f"store_failed: {str(e)[:200]}"})

    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        loops_examined=len(loops),
        targets_processed=len(targets),
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
    auth_err = _require_admin()
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
        return jsonify(ok=True, skipped=True,
                       reason="no_actionable_backend_issues",
                       as_of=datetime.now(timezone.utc).isoformat()), 200

    results = []
    for issue in backend_issues:
        url = issue.get("url", "")          # e.g. dchub://cron/dcpi_recompute
        label = issue.get("issue", "")[:300]

        # Map to source files. If we don't have a mapping, skip — better
        # to refuse than guess at unknown surfaces.
        source_files = BACKEND_ISSUE_SOURCE_FILES.get(url, [])
        if not source_files:
            results.append({"url": url, "outcome": "no_source_map"})
            continue

        excerpts = {}
        for fp in source_files[:2]:
            ex = _read_window(fp, max_chars=4000)
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
        prompt = _build_code_prompt(loop_state, excerpts, babysitter_log=[])
        text, err = _call_claude(prompt, _LEARN_CODE_SYSTEM)
        if err or not text:
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

        file_path = (prop.get("file") or "").strip()
        search = (prop.get("search") or "").strip()
        replace = (prop.get("replace") or "").strip()
        rationale = (prop.get("rationale") or "").strip()
        confidence = float(prop.get("confidence", 0) or 0)

        if not file_path or not search:
            results.append({"url": url, "outcome": "refused",
                            "rationale": rationale[:200]})
            continue

        live = _read_window(file_path, max_chars=200000)
        if search not in live:
            results.append({"url": url, "outcome": "search_not_found",
                            "file": file_path,
                            "search_preview": search[:100]})
            continue

        try:
            import psycopg2
            db_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
            with psycopg2.connect(db_url, connect_timeout=5) as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO brain_proposed_code_fixes
                        (loop_name, file_path, search_text, replace_text,
                         rationale, confidence, model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (loop_name, file_path, search_text) DO NOTHING
                    RETURNING id
                """, (loop_state["name"], file_path, search, replace,
                      rationale, confidence, BRAIN_MODEL))
                row = cur.fetchone()
                conn.commit()
            results.append({
                "url": url, "outcome": "proposed" if row else "duplicate",
                "id": row[0] if row else None, "file": file_path,
                "confidence": confidence,
                "rationale": rationale[:200],
            })
        except Exception as e:
            results.append({"url": url,
                            "outcome": f"store_failed: {str(e)[:200]}"})

    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        issues_examined=len(backend_issues),
        results=results,
    ), 200


@brain_v2_layer5_bp.get("/api/v1/brain/proposed-code/pending-pr")
def proposed_code_pending_pr():
    """Phase GG (2026-05-14): high-confidence proposals that don't yet
    have a PR opened. Consumed by the brain-layer5-pr-opener GH Actions
    workflow which checks out each one's file, applies the search→replace,
    commits to a new branch, and opens a draft PR.

    Admin-gated because the response includes the full code proposal
    (search/replace text — could leak partial source for paid endpoints).
    """
    auth_err = _require_admin()
    if auth_err: return auth_err

    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            return jsonify(items=[]), 200
        # Defensively add a pr_url column for tracking. ON CONFLICT
        # not relevant here — we just want the schema migration to
        # land before the SELECT.
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    ALTER TABLE brain_proposed_code_fixes
                    ADD COLUMN IF NOT EXISTS pr_url TEXT;
                """)
                conn.commit()
            except Exception:
                conn.rollback()
            min_conf = float(request.args.get("min_confidence", "0.7"))
            limit = int(request.args.get("limit", "10"))
            cur.execute("""
                SELECT id, loop_name, file_path, search_text, replace_text,
                       rationale, confidence, status, proposed_at
                FROM brain_proposed_code_fixes
                WHERE confidence >= %s
                  AND pr_url IS NULL
                  AND COALESCE(status, 'proposed') = 'proposed'
                ORDER BY confidence DESC, proposed_at DESC
                LIMIT %s
            """, (min_conf, limit))
            rows = cur.fetchall()
        items = [{
            "id": r[0], "loop_name": r[1], "file_path": r[2],
            "search_text": r[3], "replace_text": r[4],
            "rationale": r[5], "confidence": r[6],
            "status": r[7],
            "proposed_at": r[8].isoformat() if r[8] else None,
        } for r in rows]
        return jsonify(
            as_of=datetime.now(timezone.utc).isoformat(),
            count=len(items), items=items,
        ), 200
    except Exception as e:
        return jsonify(error=str(e)[:200], items=[]), 200


@brain_v2_layer5_bp.post("/api/v1/brain/proposed-code/<int:proposal_id>/mark-pr")
def mark_proposal_pr(proposal_id):
    """Phase GG: GH Actions PR opener calls this after successfully
    opening a draft PR for a proposal, so the same proposal isn't
    picked up on the next tick.
    """
    auth_err = _require_admin()
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
        return jsonify(ok=True, proposal_id=proposal_id, pr_url=pr_url), 200
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
