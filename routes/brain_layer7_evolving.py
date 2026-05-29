"""
Brain L7 — Self-Evolving (2026-05-19).

L0-L6 use HUMAN-WRITTEN detectors. L7 has Claude PROPOSE NEW DETECTORS
based on recent fixes + memory patterns. The idea: if we've shipped 3
fixes for the same root cause, brain should propose a detector that
would have caught all 3 earlier.

How L7 works:
1. Read brain L3 memory (top recurring fix scopes from past 14 days)
2. For each scope with ≥3 fixes, pull the actual commit messages + diffs
3. Ask Claude: "given these N fixes for {scope}, what detector would
   have caught the root cause earlier? Reply with a Python function
   matching the consistency_radar signature."
4. Persist the proposal in brain_detector_proposals (NOT auto-merged)
5. Surface via GET /api/v1/brain/proposed-detectors for human review

Daily-limited (1 proposal per day max). Detector code is NEVER auto-
merged — humans review every proposal before adoption. The point isn't
to remove humans; it's to give them a candidate detector that the
brain has reasoned about, so they spend their time CHOOSING instead
of WRITING.

Endpoints:
  POST /api/v1/brain/propose-detector   admin-gated; runs Claude proposal
  GET  /api/v1/brain/proposed-detectors latest proposals

Rate-limited via DCHUB_L7_DAILY_MAX (default 3).
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer7_bp = Blueprint("brain_layer7", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_DAILY_MAX = int(os.environ.get("DCHUB_L7_DAILY_MAX", "3"))


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_detector_proposals (
    id              BIGSERIAL PRIMARY KEY,
    scope           TEXT NOT NULL,
    based_on_count  INT NOT NULL,
    detector_name   TEXT NOT NULL,
    detector_code   TEXT NOT NULL,
    rationale       TEXT,
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ,
    reviewer_decision TEXT,    -- 'accepted' | 'rejected' | 'pending'
    reviewer_notes  TEXT
);
CREATE INDEX IF NOT EXISTS ix_bdp_scope ON brain_detector_proposals(scope);
CREATE INDEX IF NOT EXISTS ix_bdp_proposed ON brain_detector_proposals(proposed_at DESC);
"""

_SCHEMA_INIT = False

def _ensure_schema():
    global _SCHEMA_INIT
    if _SCHEMA_INIT: return
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(_SCHEMA)
            try: c.commit()
            except Exception: pass
            _SCHEMA_INIT = True
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"L7 schema init failed: {e}")


def _count_today() -> int:
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM brain_detector_proposals
                 WHERE proposed_at >= NOW() - INTERVAL '24 hours'
            """)
            return (cur.fetchone() or [0])[0]
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        return 0


def _top_scope_with_count() -> tuple[str | None, int, list[str]]:
    """Read brain L3 memory, find top recurring scope with most fixes
    that hasn't been proposed-against in the last 14 days. Returns
    (scope, count, recent_summaries)."""
    try:
        import requests
        r = requests.get("http://localhost:8080/api/v1/brain/memory/stats",
                         timeout=8)
        mem = r.json() if r.ok else {}
    except Exception:
        return None, 0, []

    candidates = (mem.get("top_recurring_issues") or [])
    if not candidates:
        return None, 0, []

    # Find scope we haven't proposed-against recently
    try:
        c = _conn()
        try:
            cur = c.cursor()
            for cand in candidates:
                scope = cand.get("issue_type", "")
                if not scope: continue
                cur.execute("""
                    SELECT COUNT(*) FROM brain_detector_proposals
                     WHERE scope = %s
                       AND proposed_at >= NOW() - INTERVAL '14 days'
                """, (scope,))
                seen_recently = (cur.fetchone() or [0])[0]
                if seen_recently > 0:
                    continue
                # Look up the actual summaries for this scope
                try:
                    lk = requests.get(
                        f"http://localhost:8080/api/v1/brain/memory/lookup?issue={scope}",
                        timeout=5).json()
                    summaries = [a.get("fix_summary", "")[:200]
                                  for a in (lk.get("attempts") or [])][:8]
                except Exception:
                    summaries = []
                return scope, cand.get("occurrences", 0), summaries
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        pass
    return None, 0, []


def _call_claude_for_detector(scope: str, summaries: list[str]) -> dict | None:
    if not _ANTHROPIC_KEY:
        return None
    prompt = f"""You are the DC Hub brain — a Python module that detects
operational issues by polling endpoints and reading database tables.
We've shipped {len(summaries)} fixes for the same scope ({scope})
in the last 14 days:

{chr(10).join(f'  {i+1}. {s}' for i, s in enumerate(summaries))}

Propose a NEW Python detector function that would have caught the
root cause earlier. It must follow this shape:

```python
def check_<descriptive_name>() -> list[dict]:
    \"\"\"<one-paragraph docstring>\"\"\"
    findings: list[dict] = []
    # ... your logic — call endpoints or read DB
    # When fires, append:
    findings.append({{
        "issue":  "<short snake_case label>",
        "url":    "<path or scope>",
        "count":  <int>,
        "detail": "<actionable diagnostic + specific fix recommendation>",
    }})
    return findings
```

Constraints:
- Use `import requests as _req` for HTTP probes (8s timeout max).
- Use `_db()` helper for DB queries (already imported in the module).
- Keep total runtime under 5 seconds.
- Make the detail field ACTIONABLE — include the specific fix to apply.
- Return a single JSON block (no markdown fences) with these keys:
    detector_name (the function name, e.g. "check_xyz")
    detector_code (the full Python function as a string, with proper escapes)
    rationale     (2-3 sentences explaining what failure pattern this catches
                   and why it's worth adding to brain)

Reply with ONLY the JSON object, no other text."""
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
                # 2026-05-24 r30: route via brain_models tier registry.
                # L7 evolving is "reasoning" tier — multi-step thinking
                # benefits from Opus 4.7's 1M context.
                "model": (
                    __import__("routes.brain_models", fromlist=["brain_model_for"])
                    .brain_model_for("reasoning")
                ),
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )
        if r.status_code != 200:
            logger.warning(f"L7 Claude {r.status_code}: {r.text[:200]}")
            return None
        body = r.json() or {}
        text = "".join(b.get("text", "") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
        # Strip code fences if Claude added them anyway
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"):
                text = text[4:].lstrip("\n")
        import json
        return json.loads(text)
    except Exception as e:
        logger.warning(f"L7 Claude call failed: {e}")
        return None


@brain_layer7_bp.route("/api/v1/brain/propose-detector",
                         methods=["POST", "GET"])
def propose_detector():
    """Trigger one detector-proposal pass. Admin-gated on POST."""
    if request.method == "POST":
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if _ADMIN_KEY and provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401

    _ensure_schema()

    if _count_today() >= _DAILY_MAX:
        return jsonify(ok=False,
                       error=f"daily cap reached ({_DAILY_MAX} proposals/24h)",
                       hint="raise DCHUB_L7_DAILY_MAX or wait"), 429

    scope, n_fixes, summaries = _top_scope_with_count()
    if not scope or n_fixes < 3:
        return jsonify(ok=False,
                       error="no scope with 3+ unaddressed fixes",
                       hint=("Brain memory needs accumulation. Bootstrap "
                             "with POST /api/v1/brain/memory/backfill-from-commits")), 404

    proposal = _call_claude_for_detector(scope, summaries)
    if not proposal:
        return jsonify(ok=False, error="Claude call failed",
                       hint="check ANTHROPIC_API_KEY"), 503

    # Persist
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                INSERT INTO brain_detector_proposals
                  (scope, based_on_count, detector_name, detector_code,
                   rationale, reviewer_decision)
                VALUES (%s, %s, %s, %s, %s, 'pending') ON CONFLICT DO NOTHING
                RETURNING id, proposed_at
            """, (
                scope, n_fixes,
                proposal.get("detector_name", "check_unknown"),
                proposal.get("detector_code", ""),
                proposal.get("rationale", ""),
            ))
            r = cur.fetchone()
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    return jsonify(
        ok=True,
        id=r[0],
        scope=scope,
        based_on_count=n_fixes,
        detector_name=proposal.get("detector_name"),
        rationale=proposal.get("rationale"),
        detector_code_preview=(proposal.get("detector_code", "")[:500]),
        note=(f"Detector proposed. Review at GET /api/v1/brain/"
              f"proposed-detectors then copy into routes/brain_"
              f"consistency_radar.py if you accept."),
    ), 200


@brain_layer7_bp.route("/api/v1/brain/proposed-detectors",
                         methods=["GET"])
def list_proposals():
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT id, scope, based_on_count, detector_name,
                       detector_code, rationale, proposed_at,
                       reviewer_decision, reviewer_notes
                FROM brain_detector_proposals
                ORDER BY proposed_at DESC LIMIT 50
            """)
            rows = cur.fetchall() or []
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    proposals = [{
        "id":             r[0],
        "scope":          r[1],
        "based_on_count": r[2],
        "detector_name":  r[3],
        "detector_code":  r[4],
        "rationale":      r[5],
        "proposed_at":    r[6].isoformat() if r[6] else None,
        "decision":       r[7] or "pending",
        "notes":          r[8],
    } for r in rows]

    return jsonify(
        ok=True,
        count=len(proposals),
        proposals=proposals,
        note=("L7 self-evolving brain proposals. Humans review + paste "
              "accepted ones into routes/brain_consistency_radar.py."),
    ), 200
