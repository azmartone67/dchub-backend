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

Write the proposal only. No preamble, no sign-off.
"""


def _call_claude(prompt: str) -> str | None:
    if not _ANTHROPIC_KEY:
        return None
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _ANTHROPIC_KEY,
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

    # Persist the proposal
    proposal_id = None
    try:
        c = _db()
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_layer5_proposals
                    (error_class, finding_url, finding_detail, file_context,
                     proposal_md, model, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'proposed') ON CONFLICT DO NOTHING
                RETURNING id
            """, (error_class, finding_url, finding_detail,
                  (file_context[:6000] if file_context else None),
                  proposal, _MODEL))
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
