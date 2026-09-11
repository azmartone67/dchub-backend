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
from utils.anthropic_helper import anthropic_messages_url
from routes.brain_llm_spend import instrumented_post as _llm_post

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
    # ★2026-08-12 — was `mem = r.json() if r.ok else {}`. A non-200 became {},
    # which flowed into `candidates = mem.get("top_recurring_issues") or []`
    # and read as "nothing recurring to propose against" — L7 would quietly
    # propose nothing and look healthy. The except-branch already returned None
    # for "unknown"; the non-200 branch did not, so the two failure modes
    # disagreed inside one function. Envelope: util/internal_fetch.
    from util.internal_fetch import probe
    env = probe("/api/v1/brain/memory/stats", 8)
    if not env["ok"]:
        return None, 0, []
    mem = env["data"]

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


# The proposal shape, sent natively as a JSON schema (routes/brain_llm_structured)
# so the API guarantees syntactically-valid JSON when the model finishes. The
# legacy fence-strip parse stays as the fallback for a model without structured
# support / the kill switch / a 400 on the structured attempt.
_DETECTOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "detector_name": {"type": "string"},
        "detector_code": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["detector_name", "detector_code", "rationale"],
}
_L7_SYSTEM = ("You are the DC Hub brain — a Python module that detects "
              "operational issues by polling endpoints and reading database "
              "tables. Reply with ONLY the JSON object, no other text.")

# Outcome classes of one proposal call. Everything that is NOT "ok" used to
# collapse into `None` → a 503 whose hint named ANTHROPIC_API_KEY, whatever the
# real cause (2026-08-31→09-01: 6/6 runs 503 while llm-spend recorded 28 calls,
# 0 HTTP failures — the model answered, the JSON parse was what failed).
CALL_OK = "ok"
CALL_NO_KEY = "no_key"
CALL_HTTP_ERROR = "http_error"
CALL_EXCEPTION = "exception"
CALL_NONJSON = "nonjson"          # 200 from the model, text is not the JSON asked for
CALL_DECLINED = "declined"        # JSON, but no detector_code — the model passed
DECLINED_REASON = "model_declined_or_nonjson"
_HINT_BY_CLASS = {
    CALL_NO_KEY: "ANTHROPIC_API_KEY is unset on this service",
    CALL_HTTP_ERROR: ("non-200 from the messages endpoint — read `detail` "
                      "(gateway spend rule / rate limit / auth); not a parse "
                      "problem and not necessarily the key"),
    CALL_EXCEPTION: "transport exception before a response — read `detail`",
}


def _model() -> str:
    # 2026-05-24 r30: route via brain_models tier registry. L7 evolving is
    # "reasoning" tier — multi-step thinking benefits from the 1M context.
    from routes.brain_models import brain_model_for
    return brain_model_for("reasoning")


def _parse_detector_text(text: str, structured: bool) -> dict | None:
    """The model's text block → the proposal dict, or None when it is not the
    JSON object asked for. Structured mode is a strict parse (the API
    guarantees the syntax when the model finishes); legacy strips fences."""
    try:
        from routes import brain_llm_structured as _so
    except Exception:  # noqa: BLE001
        _so = None
    if structured and _so is not None:
        return _so.parse_structured_json(text)
    t = (text or "").strip()
    # Strip code fences if Claude added them anyway
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t else t
        if t.startswith("json"):
            t = t[4:].lstrip("\n")
    import json
    try:
        parsed = json.loads(t)
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def _call_claude_for_detector(scope: str, summaries: list[str]) -> dict:
    """ONE proposal call. Always returns an envelope — never None:

        {"status": CALL_*, "proposal": dict | None, "raw": str, "detail": str}

    status ok       → proposal is the parsed dict (has detector_code)
    status nonjson  → 200 from the model, text was not the JSON asked for
                      (raw carries text[:300]); declined → JSON without a
                      detector_code. Both are the model PASSING, not the
                      key failing — the route answers 200, not 503.
    status no_key / http_error / exception → the call did not produce a
    reply; detail names why so the 503 hint can too."""
    if not _ANTHROPIC_KEY:
        return {"status": CALL_NO_KEY, "proposal": None, "raw": "",
                "detail": "ANTHROPIC_API_KEY unset"}
    prompt = f"""We've shipped {len(summaries)} fixes for the same scope ({scope})
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
        from routes import brain_llm_structured as _so
    except Exception:  # noqa: BLE001
        _so = None
    try:
        model = _model()
        headers = {
            "x-api-key": _ANTHROPIC_KEY,
            "User-Agent": "dchub-brain/1.0",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        messages = [{"role": "user", "content": prompt}]
        attempts = ((True, False)
                    if (_so is not None
                        and _so.structured_active(model, _DETECTOR_SCHEMA))
                    else (False,))
        r = None
        structured = False
        for structured in attempts:
            if _so is not None:
                body_dict, structured = _so.build_messages_body(
                    model, _L7_SYSTEM, messages, 2000,
                    _DETECTOR_SCHEMA if structured else None)
            else:
                body_dict = {"model": model, "max_tokens": 2000,
                             "system": _L7_SYSTEM, "messages": messages}
            r = _llm_post("brain_layer7_evolving", anthropic_messages_url(),
                          headers=headers, json=body_dict, timeout=45)
            if structured and r.status_code == 400:
                # The structured param is plausibly the cause — memoize when
                # the error blames it, then retry the SAME model legacy.
                if _so is not None and _so.looks_like_structured_rejection(
                        400, getattr(r, "text", "") or ""):
                    _so.mark_model_unsupported(model)
                continue
            break
        if r is None or r.status_code != 200:
            code = getattr(r, "status_code", None)
            detail = f"http_{code}: {(getattr(r, 'text', '') or '')[:200]}"
            logger.warning("L7 Claude %s", detail)
            return {"status": CALL_HTTP_ERROR, "proposal": None, "raw": "",
                    "detail": detail}
        body = r.json() or {}
        text = "".join(b.get("text", "") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"L7 Claude call failed: {e}")
        return {"status": CALL_EXCEPTION, "proposal": None, "raw": "",
                "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    proposal = _parse_detector_text(text, structured)
    if proposal is None:
        # The model answered — with prose, not the object. Log what it said
        # so the next operator reads the reply instead of rotating a key.
        logger.warning("L7 model reply is not the JSON asked for (%s chars): %r",
                       len(text), text[:300])
        return {"status": CALL_NONJSON, "proposal": None, "raw": text[:300],
                "detail": "reply is not a JSON object"}
    if not str(proposal.get("detector_code") or "").strip():
        return {"status": CALL_DECLINED, "proposal": None, "raw": text[:300],
                "detail": "JSON without detector_code — the model declined"}
    return {"status": CALL_OK, "proposal": proposal, "raw": text[:300],
            "detail": ""}


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

    res = _call_claude_for_detector(scope, summaries)
    if res.get("status") in (CALL_NONJSON, CALL_DECLINED):
        # The call SUCCEEDED and the model passed on proposing. That is a
        # 200 with a reason, not a 503 — the key is fine and the operator
        # must not be sent to rotate it (6/6 runs did exactly that,
        # 2026-08-31→09-01, while llm-spend showed 0 HTTP failures).
        return jsonify(ok=True, proposed=None, reason=DECLINED_REASON,
                       raw=res.get("raw", ""), scope=scope,
                       based_on_count=n_fixes, detail=res.get("detail", "")), 200
    proposal = res.get("proposal") if res.get("status") == CALL_OK else None
    if not proposal:
        cls = res.get("status") or CALL_EXCEPTION
        return jsonify(ok=False, error="Claude call failed", error_class=cls,
                       detail=res.get("detail", ""),
                       hint=_HINT_BY_CLASS.get(cls, "read `detail`")), 503

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
