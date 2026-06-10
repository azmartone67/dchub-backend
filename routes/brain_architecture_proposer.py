"""
brain_architecture_proposer.py — Brain proposes refactors (2026-06-07 R3).
============================================================================

PROBLEM
-------
The Code Scanner (sibling module) inventories routes/*.py. By itself an
inventory is a list — not a plan. The brain needs to LOOK AT the
inventory + the cross-session findings + the recent PR outcomes, spot
patterns (3 nearly-identical route files; a SQL fragment recurring in
8 places; a critical path with 0 tests; growing churn on an untested
hotspot), and Claude-write a 200-word architectural refactor spec.

This module is conservative by design:

  · 1 proposal/week max (env: BRAIN_R3_ARCH_WEEKLY_CAP, default 1)
  · DRY-RUN by default — proposals land in the DB, NOT in GitHub Issues,
    until the operator flips BRAIN_R3_ARCHITECTURE_DRY_RUN=0
  · High-confidence only (>=0.85) gates the Issue opening
  · Conceptual specs, NOT patches — no auto-PR; the spec describes
    "extract a shared module", "consolidate these 3 route files",
    "add a trait pattern for X" — the human writes the diff

PIPELINE
--------
  1.  gather_code_inventory_context()              top-N hotspots
  2.  Detect candidate patterns:
       · CLUSTER_SIMILAR_ROUTES  → 3+ files matching the same shape
                                    (line range, public func names share
                                    a stem, similar bytes/line ratio)
       · UNTESTED_CRITICAL_PATH  → high-churn untested file
       · LIKELY_DEAD_LARGE       → 0 callers, ≥200 lines, modified
                                    recently (zombie code)
       · COMPLEXITY_HOTSPOT      → nesting depth ≥7 on a critical file
  3.  Rank candidates → pick top-1 by score (weighted by recent churn
       + sentinel-grade impact if known)
  4.  Claude (sonnet) drafts a 200-word refactor spec → JSON envelope:
        {pattern, title, spec_text, confidence, urgency, why_now,
         files_affected[], suggested_modules[], rollback_plan,
         tests_to_add[]}
  5.  Persist to brain_architecture_proposals.
  6.  If confidence ≥ 0.85 AND not DRY_RUN AND not over cap:
       · Open a draft GitHub Issue labeled brain-l7-architecture +
         proposal. Title = "Architectural Proposal: <pattern>".
  7.  Strategic synthesis (L6) can read past proposals to avoid
       proposing the same pattern twice.

ENDPOINTS
---------
  POST /api/v1/admin/brain/architecture/run          trigger (admin)
  GET  /api/v1/admin/brain/architecture/preview      candidates (admin)
  GET  /api/v1/admin/brain/architecture/status       config (admin)
  GET  /api/v1/brain/architecture/proposals          last N (public read)

CRON
----
  crawler_scheduler.SCHEDULE "brain_architecture_propose" (4, 4).
  Self-gates on rolling 7-day cap inside the runner.

COST
----
  · 1 Claude (Sonnet) call per accepted proposal: ~$0.02-0.05
  · Budget gate: BRAIN_R3_DAILY_BUDGET_USD (default $3) applies across
    the 3 Round-3 unlocks combined; this module is the smallest spender
    by far. ~$2.60/yr if cap stays at 1/wk.

SAFETY
------
  · BRAIN_R3_DISABLE=1                        master kill
  · BRAIN_ARCH_PROPOSER_DISABLE=1             per-unlock kill
  · BRAIN_R3_ARCHITECTURE_DRY_RUN=1           DEFAULT ON; proposals
                                              persist but no GH Issue
  · BRAIN_R3_ARCH_WEEKLY_CAP=1                hard cap per ISO week
  · Conceptual only — no code patch, no auto-PR
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_architecture_bp = Blueprint("brain_architecture_proposer", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
_GITHUB_REPO = (os.environ.get("GITHUB_REPO")
                 or "azmartone67/dchub-backend").strip()


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return (_truthy(os.environ.get("BRAIN_R3_DISABLE"))
            or _truthy(os.environ.get("BRAIN_ARCH_PROPOSER_DISABLE")))


def _dry_run_on() -> bool:
    """Default ON — DRY_RUN=1 unless explicitly set to 0/false."""
    raw = os.environ.get("BRAIN_R3_ARCHITECTURE_DRY_RUN")
    if raw is None or str(raw).strip() == "":
        return True   # safe default
    return _truthy(raw)


def _weekly_cap() -> int:
    try:
        n = int(os.environ.get("BRAIN_R3_ARCH_WEEKLY_CAP") or "1")
        return max(1, min(n, 5))
    except Exception:
        return 1


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


# ─── DB ─────────────────────────────────────────────────────────────

def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_architecture_proposals (
    id                  BIGSERIAL PRIMARY KEY,
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    iso_week            TEXT NOT NULL,
    pattern             TEXT NOT NULL,
    title               TEXT NOT NULL,
    spec_text           TEXT NOT NULL,
    confidence          NUMERIC NOT NULL DEFAULT 0,
    urgency             TEXT,
    files_affected      JSONB NOT NULL DEFAULT '[]'::jsonb,
    suggested_modules   JSONB NOT NULL DEFAULT '[]'::jsonb,
    tests_to_add        JSONB NOT NULL DEFAULT '[]'::jsonb,
    rollback_plan       TEXT,
    github_issue_url    TEXT,
    github_issue_number INTEGER,
    status              TEXT NOT NULL DEFAULT 'proposed',
    model_used          TEXT,
    claude_cost_cents   NUMERIC,
    dedupe_key          TEXT,
    raw_payload         JSONB
);
CREATE INDEX IF NOT EXISTS ix_bap_proposed_at
    ON brain_architecture_proposals(proposed_at DESC);
CREATE INDEX IF NOT EXISTS ix_bap_week
    ON brain_architecture_proposals(iso_week);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bap_dedupe_30d
    ON brain_architecture_proposals(dedupe_key)
    WHERE proposed_at > NOW() - INTERVAL '30 days';
"""


def _ensure_schema(c) -> None:
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("architecture _ensure_schema failed: %s", e)


def _iso_week() -> str:
    today = _dt.date.today()
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


# ─── Pattern detection ─────────────────────────────────────────────

def _gather_candidates() -> list:
    """Pull candidate patterns from the code inventory. Each candidate is
    {pattern, score, evidence, files[]}. Higher score → likelier proposal.

    Patterns scored:
      CLUSTER_SIMILAR_ROUTES   3+ files w/ similar size + stem
      UNTESTED_CRITICAL_PATH   high churn + no tests + many callers
      LIKELY_DEAD_LARGE        0 callers + ≥200 lines + modified recently
      COMPLEXITY_HOTSPOT       nesting ≥7 on a churn-heavy file
    """
    try:
        from routes.brain_code_scanner import gather_code_inventory_context
    except Exception as e:
        return [{"pattern": "ERR",
                 "evidence": f"scanner_import_failed:{e}",
                 "score": 0}]

    inv = gather_code_inventory_context() or {}
    if inv.get("_note") in ("no_db", "read_failed"):
        return [{"pattern": "ERR", "evidence": inv.get("_note"),
                 "score": 0}]

    candidates = []

    # UNTESTED_CRITICAL_PATH: untested + recent churn + many callers
    for f in (inv.get("untested_large") or []):
        score = (f.get("commits_7d", 0) * 10
                 + f.get("called_from_count", 0) * 2
                 + f.get("line_count", 0) // 100)
        if score >= 15:
            candidates.append({
                "pattern": "UNTESTED_CRITICAL_PATH",
                "score": score,
                "evidence": (
                    f"{f['file_path']}: {f['line_count']} lines, "
                    f"{f.get('called_from_count', 0)} callers, "
                    f"{f.get('commits_7d', 0)} commits/7d, NO TESTS"),
                "files": [f["file_path"]],
            })

    # LIKELY_DEAD_LARGE
    for f in (inv.get("likely_dead") or []):
        score = f.get("line_count", 0) // 50
        candidates.append({
            "pattern": "LIKELY_DEAD_LARGE",
            "score": score,
            "evidence": (
                f"{f['file_path']}: {f['line_count']} lines, "
                f"0 callers, {f.get('public_func_count', 0)} public "
                f"funcs"),
            "files": [f["file_path"]],
        })

    # COMPLEXITY_HOTSPOT
    for f in (inv.get("highest_complexity") or []):
        if f.get("max_nesting", 0) >= 7:
            score = (f.get("max_nesting", 0) * 3
                     + f.get("commits_7d", 0) * 4
                     + (0 if f.get("has_tests") else 10))
            candidates.append({
                "pattern": "COMPLEXITY_HOTSPOT",
                "score": score,
                "evidence": (
                    f"{f['file_path']}: nesting={f['max_nesting']}, "
                    f"lines={f['line_count']}, "
                    f"tests={'✓' if f.get('has_tests') else '✗'}, "
                    f"churn={f.get('commits_7d', 0)}/7d"),
                "files": [f["file_path"]],
            })

    # CLUSTER_SIMILAR_ROUTES: scan all files in the inventory + group
    # by stem prefix (e.g. brain_layer*, market_*, *_brief). 3+ files
    # in the same prefix bucket with similar line counts → candidate.
    candidates.extend(_detect_clusters())

    # Rank by score desc, dedup by file
    seen_files = set()
    out = []
    for c in sorted(candidates, key=lambda x: -x.get("score", 0)):
        key = tuple(c.get("files") or [])
        if key in seen_files:
            continue
        seen_files.add(key)
        out.append(c)
    return out


def _detect_clusters() -> list:
    """Scan the inventory for 3+ files sharing a stem prefix with
    similar size — suggests a repeated route pattern that could be
    consolidated. Reads the inventory table directly."""
    c = _get_db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT file_path, line_count
                     FROM brain_code_inventory
                    WHERE line_count >= 100""")
            rows = cur.fetchall() or []
        # Group by stem before _<num> or _<word>
        groups = {}
        for path, lc in rows:
            base = os.path.basename(path).replace(".py", "")
            # Strip trailing _<N> or _<word> to get the stem
            m = re.match(r"^([a-z][a-z0-9_]*?)_(\w+?)$", base)
            stem = m.group(1) if m else base
            groups.setdefault(stem, []).append((path, int(lc or 0)))

        out = []
        for stem, files in groups.items():
            if len(files) < 3:
                continue
            # Size similarity: stdev / mean < 0.5 = "similar"
            sizes = [s for _, s in files]
            mean = sum(sizes) / len(sizes)
            if mean == 0:
                continue
            stdev = (sum((s - mean) ** 2 for s in sizes)
                     / len(sizes)) ** 0.5
            if stdev / mean > 0.6:
                continue
            score = len(files) * 5 + min(int(mean // 100), 20)
            out.append({
                "pattern": "CLUSTER_SIMILAR_ROUTES",
                "score": score,
                "evidence": (
                    f"stem '{stem}_*': {len(files)} files, "
                    f"mean={int(mean)} lines, σ={int(stdev)}"),
                "files": [p for p, _ in files[:8]],
            })
        return out
    except Exception as e:
        logger.warning("architecture _detect_clusters: %s", e)
        return []
    finally:
        try: c.close()
        except Exception: pass


def _already_proposed_recently(dedupe_key: str) -> bool:
    """Check the 30d UNIQUE index. True if we already filed this pattern
    + file-set recently — caller skips."""
    c = _get_db()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT id FROM brain_architecture_proposals
                    WHERE dedupe_key = %s
                      AND proposed_at > NOW() - INTERVAL '30 days'
                    LIMIT 1""", (dedupe_key,))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _proposals_this_week() -> int:
    c = _get_db()
    if c is None:
        return 0
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM brain_architecture_proposals
                    WHERE iso_week = %s""", (_iso_week(),))
            r = cur.fetchone()
            return int(r[0] or 0) if r else 0
    except Exception:
        return 0
    finally:
        try: c.close()
        except Exception: pass


# ─── Claude call ──────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the DC Hub Brain's ARCHITECTURE voice.

Today: spot one CONCEPTUAL refactor — NOT a code patch. You are not
writing a diff. You are writing a 200-word spec that a human architect
will read and decide whether to act on.

Given the candidate pattern + evidence below, output a single JSON
object with EXACTLY this shape (no markdown fences):

{
  "pattern": "<echo of the input pattern>",
  "title": "<short imperative noun phrase, <80 chars>",
  "spec_text": "<200-word refactor spec: problem, proposed shape, files affected, rollback plan. Plain prose, no markdown headings.>",
  "confidence": <float 0.0-1.0>,
  "urgency": "low|medium|high",
  "why_now": "<one sentence: why this pattern is worth addressing now>",
  "files_affected": ["routes/foo.py", "routes/bar.py", ...],
  "suggested_modules": ["routes/_shared/<name>.py", ...],
  "tests_to_add": ["test_<name>.py", ...],
  "rollback_plan": "<one sentence: how to revert if the refactor regresses>"
}

RULES
─────
1. spec_text is EXACTLY a refactor proposal — describe the END SHAPE,
   not the diff. "Extract a `BriefRenderer` mixin shared by
   market_brief.py + hyperscaler_brief.py + state_brief.py."
2. confidence is your honest grade. 0.85+ triggers a GitHub Issue.
   Below 0.7 means "this is a vague hunch — file as low-conf so the
   operator can read but no Issue."
3. NEVER propose security-sensitive changes (auth/, tier_*, stripe_*,
   schema_repair) — set confidence=0 and explain in spec_text.
4. files_affected MUST be a subset of the evidence files (plus 1-2
   close siblings if obvious from the pattern). DO NOT make up paths.
5. rollback_plan is concrete — name the env var to flip, the commit to
   revert, the import line to comment out.
6. Reply with ONLY the JSON object."""


def _call_claude(prompt: str) -> Optional[dict]:
    """Sonnet by default (cheap; this is creative work, not deep). Falls
    back along the brain_models chain on 404."""
    if not _ANTHROPIC_KEY:
        logger.warning("architecture: ANTHROPIC_API_KEY unset")
        return None

    try:
        from routes.brain_models import brain_model_for, resolve_chain
        from utils.anthropic_helper import anthropic_messages_url
    except Exception as e:
        logger.warning("architecture: model import failed: %s", e)
        return None

    chain = resolve_chain(brain_model_for("routine"))
    last_err = None
    for model in chain:
        try:
            import requests
            r = requests.post(
                anthropic_messages_url(),
                headers={
                    "x-api-key":         _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "User-Agent":        "dchub-brain-architecture/1.0",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 1500,
                    "system":     _SYSTEM_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            if r.status_code == 404:
                last_err = f"{model}:404"
                continue
            if r.status_code != 200:
                last_err = f"{model}:{r.status_code}:{r.text[:200]}"
                logger.warning("architecture: %s", last_err)
                continue
            body = r.json() or {}
            text = "".join(
                b.get("text", "")
                for b in (body.get("content") or [])
                if b.get("type") == "text").strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1] if "```" in text else text
                if text.startswith("json"):
                    text = text[4:].lstrip("\n")
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
            try:
                parsed = json.loads(text)
                parsed["_model_used"] = model
                parsed["_input_tokens_est"] = len(prompt) // 4
                parsed["_output_tokens_est"] = len(text) // 4
                return parsed
            except Exception as je:
                last_err = f"parse:{je}"
                logger.warning("architecture: parse fail: %s; first 200: %s",
                                je, text[:200])
                continue
        except Exception as e:
            last_err = f"{model}:exc:{e}"
            continue
    logger.error("architecture: all models failed; last=%s", last_err)
    return None


def _estimate_cost_cents(in_tok: int, out_tok: int, model: str) -> float:
    rates = {
        "claude-opus-4-8":     (15.0, 75.0),
        "claude-opus-4-7":     (15.0, 75.0),
        "claude-opus-4-5":     (15.0, 75.0),
        "claude-sonnet-4-5":   (3.0,  15.0),
        "claude-haiku-4-5":    (0.80, 4.0),
    }
    r_in, r_out = rates.get(model, (3.0, 15.0))
    usd = in_tok / 1e6 * r_in + out_tok / 1e6 * r_out
    return round(usd * 100, 3)


# ─── GitHub Issue ────────────────────────────────────────────────

def _open_github_issue(proposal: dict) -> dict:
    """Open a draft GitHub Issue. Labels: brain-l7-architecture, proposal.
    Defensive — never raises."""
    if not _GITHUB_TOKEN:
        return {"ok": False, "error": "no_github_token"}

    title = (proposal.get("title")
              or f"Architectural Proposal: {proposal.get('pattern')}")
    title = f"[brain-arch] {title[:100]}"

    body_lines = [
        "## Brain — Architectural Proposal",
        "",
        f"**Pattern**: `{proposal.get('pattern')}`",
        f"**Confidence**: {proposal.get('confidence')}",
        f"**Urgency**: {proposal.get('urgency')}",
        "",
        "### Why now",
        proposal.get("why_now") or "(no why_now provided)",
        "",
        "### Spec",
        proposal.get("spec_text") or "(no spec)",
        "",
        "### Files affected",
    ]
    for f in (proposal.get("files_affected") or [])[:10]:
        body_lines.append(f"- `{f}`")
    body_lines.append("")
    if proposal.get("suggested_modules"):
        body_lines.append("### Suggested new modules")
        for m in proposal["suggested_modules"][:6]:
            body_lines.append(f"- `{m}`")
        body_lines.append("")
    if proposal.get("tests_to_add"):
        body_lines.append("### Tests to add")
        for t in proposal["tests_to_add"][:6]:
            body_lines.append(f"- `{t}`")
        body_lines.append("")
    body_lines.append("### Rollback plan")
    body_lines.append(proposal.get("rollback_plan")
                       or "Revert the commit; no env var change required.")
    body_lines.append("")
    body_lines.append("---")
    body_lines.append(
        "*Auto-generated by `routes/brain_architecture_proposer.py`. "
        "This is a CONCEPTUAL proposal, not a code patch. Close + "
        "comment with rationale if rejected so the brain learns.*")

    try:
        import requests
        r = requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
            headers={
                "Authorization": f"Bearer {_GITHUB_TOKEN}",
                "Accept":        "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title,
                  "body":  "\n".join(body_lines),
                  "labels": ["brain-l7-architecture", "proposal"]},
            timeout=15,
        )
        if r.status_code not in (200, 201):
            return {"ok": False,
                    "error": f"github_{r.status_code}: {r.text[:200]}"}
        data = r.json() or {}
        return {"ok": True, "url": data.get("html_url"),
                "number": data.get("number")}
    except Exception as e:
        return {"ok": False,
                "error": f"{type(e).__name__}:{str(e)[:200]}"}


# ─── Persistence ──────────────────────────────────────────────────

def _persist_proposal(proposal: dict, dedupe_key: str,
                       cost_cents: float,
                       gh_url: Optional[str],
                       gh_number: Optional[int],
                       status: str = "proposed") -> int:
    c = _get_db()
    if c is None:
        return 0
    _ensure_schema(c)
    try:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_architecture_proposals (
                       iso_week, pattern, title, spec_text, confidence,
                       urgency, files_affected, suggested_modules,
                       tests_to_add, rollback_plan, github_issue_url,
                       github_issue_number, status, model_used,
                       claude_cost_cents, dedupe_key, raw_payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb,
                           %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s,
                           %s, %s::jsonb) ON CONFLICT DO NOTHING
                   RETURNING id""",
                (_iso_week(),
                 proposal.get("pattern") or "UNKNOWN",
                 (proposal.get("title") or "")[:240],
                 (proposal.get("spec_text") or "")[:4000],
                 float(proposal.get("confidence") or 0),
                 (proposal.get("urgency") or "low")[:16],
                 json.dumps(proposal.get("files_affected") or []),
                 json.dumps(proposal.get("suggested_modules") or []),
                 json.dumps(proposal.get("tests_to_add") or []),
                 (proposal.get("rollback_plan") or "")[:1000],
                 gh_url, gh_number, status,
                 proposal.get("_model_used"),
                 cost_cents, dedupe_key,
                 json.dumps(proposal, default=str)))
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        return int(row[0]) if row else 0
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.error("architecture _persist failed: %s", e)
        return 0
    finally:
        try: c.close()
        except Exception: pass


# ─── Run ──────────────────────────────────────────────────────────

def _format_prompt(candidate: dict) -> str:
    return (
        f"CANDIDATE PATTERN: {candidate.get('pattern')}\n"
        f"SCORE: {candidate.get('score')}\n"
        f"EVIDENCE: {candidate.get('evidence')}\n"
        f"FILES IDENTIFIED:\n"
        + "\n".join(f"  - {f}" for f in (candidate.get('files') or []))
        + "\n\n──── End evidence. Reply with the JSON envelope only. ────"
    )


def run_architecture_proposer(force: bool = False) -> dict:
    """Pick the top candidate, draft a spec, persist, optionally file
    a GitHub Issue. Returns a summary dict (never raises)."""
    started = time.time()

    if _kill_switch_on():
        return {"ok": False, "reason": "kill_switch_on",
                "env": "BRAIN_R3_DISABLE or BRAIN_ARCH_PROPOSER_DISABLE"}

    if not force and _proposals_this_week() >= _weekly_cap():
        return {"ok": True, "skipped": "weekly_cap_hit",
                "iso_week": _iso_week(),
                "cap": _weekly_cap()}

    candidates = _gather_candidates()
    if not candidates or candidates[0].get("pattern") == "ERR":
        return {"ok": True, "skipped": "no_candidates",
                "details": candidates[:1]}

    top = candidates[0]

    # Dedupe across 30d window
    dedupe_key = hashlib.sha256(
        (top.get("pattern", "") + "|"
         + "|".join(sorted(top.get("files") or []))).encode("utf-8")
    ).hexdigest()[:32]

    if not force and _already_proposed_recently(dedupe_key):
        return {"ok": True, "skipped": "duplicate_30d",
                "dedupe_key": dedupe_key,
                "pattern": top.get("pattern")}

    prompt = _format_prompt(top)
    payload = _call_claude(prompt)
    if not payload:
        return {"ok": False, "reason": "claude_call_failed",
                "candidate": top}

    in_tok = payload.get("_input_tokens_est") or 0
    out_tok = payload.get("_output_tokens_est") or 0
    cost_cents = _estimate_cost_cents(
        in_tok, out_tok,
        payload.get("_model_used") or "claude-sonnet-4-5")

    confidence = float(payload.get("confidence") or 0)
    gh_url = None
    gh_number = None
    status = "proposed"

    open_issue = (confidence >= 0.85
                   and not _dry_run_on()
                   and bool(_GITHUB_TOKEN))
    if open_issue:
        gh = _open_github_issue(payload)
        if gh.get("ok"):
            gh_url = gh.get("url")
            gh_number = gh.get("number")
            status = "issue_opened"
        else:
            status = f"issue_failed:{gh.get('error', '')[:60]}"

    row_id = _persist_proposal(payload, dedupe_key, cost_cents,
                                 gh_url, gh_number, status)

    duration_ms = int((time.time() - started) * 1000)
    return {
        "ok": True,
        "row_id": row_id,
        "pattern": payload.get("pattern"),
        "title": payload.get("title"),
        "confidence": confidence,
        "urgency": payload.get("urgency"),
        "status": status,
        "github_issue_url": gh_url,
        "github_issue_number": gh_number,
        "model_used": payload.get("_model_used"),
        "cost_cents": cost_cents,
        "duration_ms": duration_ms,
        "dry_run": _dry_run_on(),
        "iso_week": _iso_week(),
        "weekly_cap": _weekly_cap(),
    }


# ─── HTTP routes ─────────────────────────────────────────────────

@brain_architecture_bp.route(
    "/api/v1/admin/brain/architecture/run", methods=["POST", "GET"])
def architecture_run():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    force = (_truthy(request.args.get("force"))
              or _truthy(body.get("force")))
    return jsonify(run_architecture_proposer(force=force)), 200


@brain_architecture_bp.route(
    "/api/v1/admin/brain/architecture/preview", methods=["GET"])
def architecture_preview():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    cands = _gather_candidates()
    return jsonify(
        ok=True,
        candidates=cands[:10],
        total_candidates=len(cands),
        weekly_cap=_weekly_cap(),
        proposals_this_week=_proposals_this_week(),
        dry_run=_dry_run_on(),
    ), 200


@brain_architecture_bp.route(
    "/api/v1/admin/brain/architecture/status", methods=["GET"])
def architecture_status():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        dry_run=_dry_run_on(),
        weekly_cap=_weekly_cap(),
        proposals_this_week=_proposals_this_week(),
        iso_week=_iso_week(),
        anthropic_key_set=bool(_ANTHROPIC_KEY),
        github_token_set=bool(_GITHUB_TOKEN),
        github_repo=_GITHUB_REPO,
        env_overrides={
            "BRAIN_R3_DISABLE":             "master kill (1=off)",
            "BRAIN_ARCH_PROPOSER_DISABLE":  "per-unlock kill (1=off)",
            "BRAIN_R3_ARCHITECTURE_DRY_RUN":
                "1 = persist only, no Issue (DEFAULT ON)",
            "BRAIN_R3_ARCH_WEEKLY_CAP":     "max proposals/week (default 1)",
        },
    ), 200


@brain_architecture_bp.route(
    "/api/v1/brain/architecture/proposals", methods=["GET"])
def architecture_proposals():
    """PUBLIC read — last N proposals."""
    try:
        limit = int(request.args.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 200))
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, proposed_at, iso_week, pattern, title,
                          spec_text, confidence, urgency,
                          files_affected, github_issue_url,
                          github_issue_number, status, model_used,
                          claude_cost_cents
                     FROM brain_architecture_proposals
                    ORDER BY proposed_at DESC LIMIT %s""", (limit,))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            files = r[8] if isinstance(r[8], list) else (
                json.loads(r[8]) if r[8] else [])
            out.append({
                "id": int(r[0]),
                "proposed_at": r[1].isoformat() if r[1] else None,
                "iso_week": r[2],
                "pattern": r[3],
                "title": r[4],
                "spec_text": (r[5] or "")[:1200],
                "confidence": float(r[6] or 0),
                "urgency": r[7],
                "files_affected": files[:10],
                "github_issue_url": r[9],
                "github_issue_number": (int(r[10])
                                          if r[10] is not None else None),
                "status": r[11],
                "model_used": r[12],
                "cost_cents": (float(r[13])
                                if r[13] is not None else None),
            })
        return jsonify(ok=True, count=len(out), proposals=out), 200
    finally:
        try: c.close()
        except Exception: pass


# Public helper for L6 strategic synthesis: don't re-propose.
def gather_recent_proposals(window_days: int = 30) -> list:
    """List of {pattern, title, status, files_affected} for the last
    N days so the strategic synthesis doesn't repeat itself."""
    c = _get_db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT pattern, title, status, files_affected,
                          proposed_at, confidence
                     FROM brain_architecture_proposals
                    WHERE proposed_at > NOW() - INTERVAL '%s days'
                    ORDER BY proposed_at DESC LIMIT 20""",
                (window_days,))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            files = r[3] if isinstance(r[3], list) else (
                json.loads(r[3]) if r[3] else [])
            out.append({
                "pattern": r[0], "title": r[1], "status": r[2],
                "files_affected": files[:6],
                "proposed_at": r[4].isoformat() if r[4] else None,
                "confidence": float(r[5] or 0),
            })
        return out
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass
