"""
Phase r43-B (2026-05-27) — Brain Layer 5 free-form codegen.

The brain's Layer 4 (templated text-replace fix) has been quiet at 0
proposals for 30 days because most live findings need *real code*, not
text substitutions. Layer 5 closes that gap: take a finding, look at
the source file(s), ask Claude to write a patch proposal, save it for
human review.

This is NOT auto-apply. The output is a Markdown patch + diff that
the operator can review and apply manually (or via a future PR-bot).

Endpoints:
  POST /api/v1/brain/layer5/propose
       body: {error_class, finding_url, finding_detail, file_path?, line?}
       → calls Claude, returns Markdown patch proposal
       Admin-gated.

  GET  /api/v1/brain/layer5/proposals
       list recent proposals + their review status (admin-gated)

The patch is stored in brain_layer5_proposals so the human can review,
mark accepted/rejected, and the brain can learn what's getting through.
"""

import os
import json
import time
import logging
import datetime
from flask import Blueprint, jsonify, request
from utils.anthropic_helper import anthropic_messages_url

logger = logging.getLogger(__name__)
brain_layer5_bp = Blueprint("brain_layer5", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for code proposals


def _admin_ok():
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or
                request.args.get("admin_key") or "").strip()
    return expected and provided == expected


def _db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    c = _db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_layer5_proposals (
                    id              SERIAL PRIMARY KEY,
                    error_class     TEXT NOT NULL,
                    finding_url     TEXT,
                    finding_detail  TEXT,
                    file_context    TEXT,
                    proposal_md     TEXT NOT NULL,
                    model           TEXT,
                    status          TEXT DEFAULT 'proposed',
                    reviewed_by     TEXT,
                    reviewed_at     TIMESTAMPTZ,
                    review_note     TEXT,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_l5_class "
                         "ON brain_layer5_proposals(error_class)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_l5_status "
                         "ON brain_layer5_proposals(status)")
            c.commit()
    except Exception as e:
        logger.warning(f"brain_layer5_proposals table ensure failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


def _build_codegen_prompt(error_class: str, finding_url: str,
                            finding_detail: str, file_context: str | None) -> str:
    return f"""You are a senior backend engineer at DC Hub, a Python/Flask
platform on Railway + Neon Postgres. The brain's consistency-radar
detected a recurring error class and surfaced a specific finding. Your
job: propose a concrete patch.

Error class: {error_class}
Affected URL: {finding_url}
Detail: {finding_detail}

{("Relevant file context:" + chr(10) + "```python" + chr(10) + file_context[:6000] + chr(10) + "```") if file_context else "(No file context provided — make best-guess proposal based on the detail.)"}

Output a Markdown patch proposal in this exact format:

## Diagnosis
2-3 sentences identifying the root cause.

## Proposed fix
The actual code change. Use a unified diff format (--- old / +++ new)
when possible, or a "Before/After" block when a diff is too noisy.

## Why this is safe
2-3 sentences. What invariants does this preserve? What breaks if we
DON'T apply it? Any tests that would catch a regression?

## Risk
Low / Medium / High. One sentence justifying the level.

## Manual verification
Specific curl command or page URL the operator can hit to confirm
the fix works post-deploy.

Constraints:
- Do not propose a fix that requires a database migration unless the
  detail explicitly mentions a missing column.
- Do not propose adding a new dependency.
- If the right fix is "needs human investigation, no autonomous patch
  is safe," say so explicitly under "## Diagnosis" and leave "## Proposed fix" with the human-action recommendation.

PRE-FLIGHT DIAGNOSIS VERIFICATION — do these BEFORE proposing. (2026-06-07: a batch
of conf-0.90 proposals was 80% wrong because the diagnosis skipped these. Read the
file context, don't just pattern-match the detail.)
  1. ADDING AN IMPORT? First confirm it is NOT already imported — scan the file
     context for `^(from|import).*\\b<name>\\b`. If present, there is NO bug; say so.
  2. ADDING A commit()/rollback()? Read the CALLEE first — if the wrapped function
     already commits/rolls back, an outer one is a no-op. Don't propose it.
  3. REMOVING code? Check the surrounding lines/comments for EMERGENCY / CRITICAL /
     belt-and-suspenders / "do not remove" markers — if present, it's intentional. Skip.
  4. CLAIMING A SYMBOL IS UNDEFINED? First grep the file for `def <name>` /
     `<name> =` / a `@contextmanager` or decorator defining it. If defined, NO bug.
If your diagnosis fails any check, output "needs human investigation" under ## Diagnosis.

Write the proposal only. No preamble, no sign-off.
"""


def _call_claude(prompt: str) -> str | None:
    if not _ANTHROPIC_KEY:
        return None
    try:
        import requests
        r = requests.post(
            anthropic_messages_url(),
            headers={
                "x-api-key": _ANTHROPIC_KEY,
                "User-Agent": "dchub-brain/1.0",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )
        if r.status_code != 200:
            logger.warning(f"layer5 codegen API {r.status_code}: {r.text[:200]}")
            return None
        j = r.json() or {}
        blocks = j.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip() or None
    except Exception as e:
        logger.warning(f"layer5 codegen call failed: {e}")
        return None


def _preflight_diagnosis_check(proposal_md: str, file_context: str | None) -> list:
    """Deterministic verification of an LLM proposal against the REAL file — the
    'pre-flight diagnosis verification' that was missing when 80% of conf-0.90
    proposals shipped wrong (2026-06-07: dup imports, no-op commits, EMERGENCY
    removals, symbols claimed undefined that were defined). Returns warnings;
    non-empty → status downgraded to 'needs_review' (over-flag is safe — it just
    routes to human review, the correct default for an 80%-wrong batch)."""
    import re
    warnings = []
    if not proposal_md:
        return warnings
    fc = file_context or ""
    pm = proposal_md
    low = pm.lower()
    added = [l[1:].strip() for l in pm.splitlines() if l.startswith('+') and not l.startswith('+++')]
    removed = [l[1:].strip() for l in pm.splitlines() if l.startswith('-') and not l.startswith('---')]

    # 1. Already-imported guard (killed PR #1032 class)
    for l in added:
        m = re.match(r'^(?:from\s+\S+\s+import\s+(\w+)|import\s+(\w+))', l)
        if m and fc:
            name = m.group(1) or m.group(2)
            if name and re.search(rf'^(?:from|import)\s+.*\b{re.escape(name)}\b', fc, re.M):
                warnings.append(f"already-imported: '{name}' looks already imported — adding it is a no-op")

    # 2. Callee/commit guard (killed PR #1031 class) — can't fully resolve statically; flag
    if any(re.search(r'\.(commit|rollback)\s*\(', l) for l in added):
        warnings.append("commit/rollback added: confirm the wrapped callee doesn't already commit (outer one may be a no-op)")

    # 3. EMERGENCY-removal guard (killed PR #1033 class)
    if removed and any(r for r in removed if r):
        for kw in ('EMERGENCY', 'CRITICAL', 'belt-and-suspenders', 'do not remove'):
            if kw.lower() in fc.lower():
                warnings.append(f"emergency-removal: file context contains '{kw}' — a removal may delete an intentional safeguard")
                break

    # 4. Symbol-resolution guard (killed PR #1035 class)
    sm = re.search(r"['\"`]?(\w+)['\"`]?\s+is\s+(?:not\s+defined|undefined)|undefined\s+(?:symbol|name|variable)\s+['\"`]?(\w+)", low)
    if sm and fc:
        name = sm.group(1) or sm.group(2)
        if name and re.search(rf'(?:def\s+{re.escape(name)}\b|^\s*{re.escape(name)}\s*=|@contextmanager)', fc, re.M):
            warnings.append(f"symbol-resolution: '{name}' claimed undefined but appears defined in the file")
    return warnings


@brain_layer5_bp.route("/api/v1/brain/layer5/propose", methods=["POST"])
def propose():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized",
                       hint="X-Admin-Key required"), 401
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set"), 503

    _ensure_table()
    data = request.get_json(force=True) or {}
    error_class = (data.get("error_class") or "").strip()
    finding_url = (data.get("finding_url") or "").strip()
    finding_detail = (data.get("finding_detail") or "").strip()
    file_path = (data.get("file_path") or "").strip()

    if not error_class or not finding_detail:
        return jsonify(ok=False, error="error_class + finding_detail required"), 400

    # If file_path is provided, read the relevant slice for context
    file_context = None
    if file_path:
        try:
            # Resolve relative to backend root
            from os.path import abspath, join, dirname
            backend_root = dirname(dirname(abspath(__file__)))
            full_path = file_path
            if not file_path.startswith("/"):
                full_path = join(backend_root, file_path)
            with open(full_path, "r", encoding="utf-8") as f:
                file_context = f.read()
                if len(file_context) > 12000:
                    file_context = file_context[:6000] + "\n\n... [truncated] ...\n\n" + file_context[-3000:]
        except Exception as e:
            logger.warning(f"layer5 file_context read failed for {file_path}: {e}")

    prompt = _build_codegen_prompt(error_class, finding_url, finding_detail, file_context)
    started = time.time()
    proposal = _call_claude(prompt)
    elapsed = round(time.time() - started, 2)

    if not proposal:
        return jsonify(ok=False, error="codegen_failed",
                       hint="Claude API call returned empty"), 503

    # r77 (2026-06-07): deterministic pre-flight diagnosis check against the real
    # file. Catches the 4 misdiagnosis classes that made 80% of conf-0.90 proposals
    # wrong. Non-empty → downgrade status to 'needs_review' + surface inline.
    preflight_warnings = _preflight_diagnosis_check(proposal, file_context)
    proposal_status = "needs_review" if preflight_warnings else "proposed"
    if preflight_warnings:
        proposal = (proposal + "\n\n## ⚠️ Pre-flight diagnosis warnings (auto-check)\n"
                    "Deterministic checks flagged this as a possible misdiagnosis "
                    "(the class that made 80% of conf-0.90 PRs wrong) — REVIEW before merge:\n"
                    + "\n".join(f"- {w}" for w in preflight_warnings))

    # Persist the proposal
    proposal_id = None
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_layer5_proposals
                    (error_class, finding_url, finding_detail, file_context,
                     proposal_md, model, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (error_class, finding_url, finding_detail,
                  (file_context[:6000] if file_context else None),
                  proposal, _MODEL, proposal_status))
            proposal_id = cur.fetchone()[0]
            c.commit()
        c.close()
    except Exception as e:
        logger.warning(f"layer5 proposal persist failed: {e}")

    return jsonify(
        ok=True,
        proposal_id=proposal_id,
        error_class=error_class,
        finding_url=finding_url,
        model=_MODEL,
        elapsed_seconds=elapsed,
        status=proposal_status,
        preflight_warnings=preflight_warnings,
        proposal_markdown=proposal,
        review_url=(f"https://dchub.cloud/api/v1/brain/layer5/proposals/{proposal_id}"
                    if proposal_id else None),
    ), 200


@brain_layer5_bp.route("/api/v1/brain/layer5/proposals", methods=["GET"])
def list_proposals():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    _ensure_table()
    status_filter = (request.args.get("status") or "").strip()
    limit = min(int(request.args.get("limit", 25)), 100)
    c = _db()
    if c is None:
        return jsonify(proposals=[], error="no_database"), 200
    try:
        with c.cursor() as cur:
            if status_filter:
                cur.execute("""SELECT id, error_class, finding_url, finding_detail,
                                      model, status, reviewed_by, created_at
                                FROM brain_layer5_proposals
                               WHERE status = %s
                               ORDER BY created_at DESC LIMIT %s""",
                             (status_filter, limit))
            else:
                cur.execute("""SELECT id, error_class, finding_url, finding_detail,
                                      model, status, reviewed_by, created_at
                                FROM brain_layer5_proposals
                               ORDER BY created_at DESC LIMIT %s""",
                             (limit,))
            rows = cur.fetchall() or []
        return jsonify(
            ok=True,
            proposals=[{
                "id": r[0], "error_class": r[1], "finding_url": r[2],
                "detail": (r[3] or "")[:240],
                "model": r[4], "status": r[5], "reviewed_by": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            } for r in rows],
        ), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_layer5_bp.route("/api/v1/brain/layer5/proposals/<int:proposal_id>",
                        methods=["GET"])
def get_proposal(proposal_id):
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    _ensure_table()
    c = _db()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT error_class, finding_url, finding_detail,
                                  file_context, proposal_md, model, status,
                                  reviewed_by, reviewed_at, review_note,
                                  created_at
                            FROM brain_layer5_proposals
                           WHERE id = %s""", (proposal_id,))
            r = cur.fetchone()
        if not r:
            return jsonify(ok=False, error="not_found"), 404
        return jsonify(
            ok=True,
            id=proposal_id,
            error_class=r[0], finding_url=r[1], finding_detail=r[2],
            file_context_preview=(r[3] or "")[:2000],
            proposal_markdown=r[4],
            model=r[5], status=r[6], reviewed_by=r[7],
            reviewed_at=r[8].isoformat() if r[8] else None,
            review_note=r[9],
            created_at=r[10].isoformat() if r[10] else None,
        ), 200
    finally:
        try: c.close()
        except Exception: pass
