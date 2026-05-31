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

brain_v2_layer5_bp = Blueprint("brain_v2_layer5", __name__)


def _admin_guard():
    """No-arg admin check → returns an error Response tuple, or None if OK.

    FIX 2026-05-31: layer4's _require_admin is a DECORATOR (def _require_admin(fn)),
    but every Layer-5 endpoint called it INLINE as `_require_admin()` → TypeError
    "missing 1 required positional argument: 'fn'" on EVERY request. The cron
    POSTs these endpoints and exits 0 regardless, so it was silent — but it meant
    Layer 5 generated 0 proposals for ~30 days. This mirrors layer4's key check."""
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key header required"), 401
    return None


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


def _validate_and_store_proposal(source_name: str, prop: dict) -> dict:
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

    primary = changes[0]
    try:
        import psycopg2
        url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_proposed_code_fixes
                    (loop_name, file_path, search_text, replace_text,
                     rationale, confidence, model, changes_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (loop_name, file_path, search_text) DO NOTHING
                RETURNING id
            """, (source_name, primary["file"], primary["search"],
                  primary["replace"], rationale, confidence, BRAIN_MODEL,
                  json.dumps(changes)))
            row = cur.fetchone()
            conn.commit()
        return {
            "outcome": "proposed" if row else "duplicate",
            "id": row[0] if row else None,
            "file": primary["file"],
            "file_count": len(changes),
            "confidence": confidence,
            "rationale": rationale[:200],
        }
    except Exception as e:
        return {"outcome": f"store_failed: {str(e)[:200]}"}


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
    results = []
    claude_calls = 0
    for issue in backend_issues:
        url = issue.get("url", "")          # e.g. dchub://cron/dcpi_recompute
        label = issue.get("issue", "")[:300]

        # Map to source files. If we don't have a mapping, skip — better
        # to refuse than guess at unknown surfaces.
        source_files = BACKEND_ISSUE_SOURCE_FILES.get(url, [])
        if not source_files:
            results.append({"url": url, "outcome": "no_source_map"})
            continue

        # Rate-cap the code-fixable issues fed to the model this cycle. Issues
        # beyond the cap are deferred to the next cron tick (recorded, not lost).
        if claude_calls >= BRAIN_MAX_LEARN:
            results.append({"url": url, "outcome": "deferred_rate_cap"})
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
        res = _validate_and_store_proposal(loop_state["name"], prop)
        res["url"] = url
        results.append(res)

    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        issues_examined=len(backend_issues),
        claude_calls=claude_calls,
        max_learn_per_cycle=BRAIN_MAX_LEARN,
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
        # merge_outcome column may not exist yet (no GG#4 callbacks
        # fired). Degrade gracefully → empty stats → everyone uses base.
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
                       changes_json
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
