"""
Brain L18 — Memory Consolidation (2026-05-19).

The "sleep cycle" the brain has been missing. Right now the brain's
memory is 94+ raw episode records — every individual fix, every
detector firing, every chain L14 found. Useful as forensic record but
USELESS as a learning signal because Claude doesn't know which
episodes generalize.

L18 is the consolidator. Every 12h it asks Claude:

  "Read the last 50 verified outcomes from brain_predictions_log,
   the last 30 fix-scope records from brain_finding_outcomes, and
   the last 15 L14 causal chains. Distill them into 5-10 NAMED
   LESSONS (~1 sentence each) that future analyses should know.
   Example: 'Shadow Stripe webhooks always break MCP attribution.'
   'CF Pages routing requires both _routes.json AND PHASE_282 set
   updates — neither alone is sufficient.' 'When the publisher loops
   show a connection-pool symptom, ALL three loops are leaking, not
   just one.'"

These lessons go into brain_consolidated_lessons. L14 reads them at
every causal-analysis call. The brain gets smarter from its own
history — and we can SEE what it has learned.

Schema:
  brain_consolidated_lessons (id, learned_at, lesson, evidence_count,
                              source_episodes, supersedes_lesson_id)

Endpoints:
  GET  /api/v1/brain/lessons           — current consolidated lessons
  POST /api/v1/brain/lessons/consolidate — admin: run a sleep pass

Cron: every 12h at 04:40 and 16:40 UTC (after L16 + L8 cycles).
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer18_bp = Blueprint("brain_layer18", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _ensure_table():
    try:
        from main import get_db
        conn = get_db()
        if not conn: return False
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_consolidated_lessons (
                    id                  SERIAL PRIMARY KEY,
                    learned_at          TIMESTAMPTZ DEFAULT NOW(),
                    lesson              TEXT NOT NULL,
                    evidence_count      INTEGER,
                    source_episodes     JSONB,
                    supersedes_lesson_id INTEGER,
                    active              BOOLEAN DEFAULT TRUE
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lessons_active "
                        "ON brain_consolidated_lessons(active, learned_at DESC)")
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"L18 table create failed: {e}")
        return False


def _internal(path: str, timeout: int = 8) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


def _gather_episodes() -> dict:
    """Pull the raw material L18 will consolidate."""
    out: dict = {"verified_predictions": [], "fix_outcomes": [],
                  "causal_chains": []}
    try:
        from main import get_db
        conn = get_db()
        if not conn: return out
        try:
            cur = conn.cursor()
            # Verified predictions from L16
            try:
                cur.execute(
                    "SELECT chain_title, confidence, was_correct, "
                    "       actual_outcome, calibration_bucket "
                    "FROM brain_predictions_log "
                    "WHERE verified_at IS NOT NULL "
                    "ORDER BY verified_at DESC LIMIT 50"
                )
                for r in cur.fetchall():
                    if hasattr(r, "get"):
                        out["verified_predictions"].append({
                            "title": r.get("chain_title"),
                            "confidence": r.get("confidence"),
                            "was_correct": r.get("was_correct"),
                            "outcome": (r.get("actual_outcome") or "")[:300],
                            "calibration": r.get("calibration_bucket"),
                        })
                    else:
                        out["verified_predictions"].append({
                            "title": r[0], "confidence": r[1],
                            "was_correct": r[2],
                            "outcome": (r[3] or "")[:300],
                            "calibration": r[4],
                        })
            except Exception: pass

            # Fix outcomes from brain memory (L3)
            try:
                cur.execute(
                    "SELECT issue_scope, fix_applied, success, "
                    "       observed_at FROM brain_finding_outcomes "
                    "ORDER BY observed_at DESC LIMIT 30"
                )
                for r in cur.fetchall():
                    if hasattr(r, "get"):
                        out["fix_outcomes"].append({
                            "scope": r.get("issue_scope"),
                            "fix": (r.get("fix_applied") or "")[:200],
                            "success": r.get("success"),
                            "at": str(r.get("observed_at") or "")[:19],
                        })
                    else:
                        out["fix_outcomes"].append({
                            "scope": r[0],
                            "fix": (r[1] or "")[:200],
                            "success": r[2],
                            "at": str(r[3])[:19],
                        })
            except Exception: pass

            # Recent L14 chains
            try:
                cur.execute(
                    "SELECT chain_title, chain_confidence, root_cause, "
                    "       opened_at "
                    "FROM brain_auto_actions "
                    "ORDER BY opened_at DESC LIMIT 15"
                )
                for r in cur.fetchall():
                    if hasattr(r, "get"):
                        out["causal_chains"].append({
                            "title": r.get("chain_title"),
                            "confidence": r.get("chain_confidence"),
                            "root_cause": (r.get("root_cause") or "")[:300],
                            "at": str(r.get("opened_at") or "")[:19],
                        })
                    else:
                        out["causal_chains"].append({
                            "title": r[0],
                            "confidence": r[1],
                            "root_cause": (r[2] or "")[:300],
                            "at": str(r[3])[:19],
                        })
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"L18 gather failed: {e}")
    return out


def _existing_lessons() -> list[str]:
    """Pull current active lessons so consolidation can supersede or
    avoid duplicating them."""
    out: list[str] = []
    try:
        from main import get_db
        conn = get_db()
        if not conn: return out
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT lesson FROM brain_consolidated_lessons "
                "WHERE active = TRUE ORDER BY learned_at DESC LIMIT 20"
            )
            for r in cur.fetchall():
                lesson = r.get("lesson") if hasattr(r, "get") else r[0]
                if lesson: out.append(lesson)
        finally:
            try: conn.close()
            except Exception: pass
    except Exception: pass
    return out


def _consolidate() -> dict:
    """Single sleep-pass: gather, call Claude, write distilled lessons."""
    if not _ANTHROPIC_KEY:
        return {"ok": False, "error": "no anthropic key"}
    episodes = _gather_episodes()
    if not any(episodes.values()):
        return {"ok": True, "note": "no episodes to consolidate",
                "lessons_written": 0}
    existing = _existing_lessons()

    prompt = f"""You are the DC Hub Brain L18 — the Memory Consolidation Layer.
You are the "sleep" pass. Your job: read the brain's recent episodes
(verified predictions, fix outcomes, causal chains) and DISTILL them
into 3-8 NAMED LESSONS — short, generalizable rules that future causal
analyses should know.

A good lesson:
  - Is 1 sentence, ≤30 words
  - Names a CONCRETE pattern, not a single instance
  - Generalizes across episodes (multiple supporting examples)
  - Tells the next brain pass HOW TO ACT differently

Good lesson examples:
  - "Duplicate Flask blueprint registrations on Stripe webhook paths
     silently kill conversions — always grep for shadowed_route on
     /api/v1/stripe/* when MCP funnel is broken."
  - "Cron jobs pinned to ':00' minute collide with the hourly cron
     and never fire — move to ':05' or unique minutes."
  - "Connection-leak symptoms surface as widespread endpoint timeouts
     while pure-Python endpoints stay fast — pool exhaustion, not
     code crash."

Bad lessons to avoid:
  - "Fix the Stripe webhook." (too specific, one-time)
  - "Make the brain better." (too vague)
  - Anything that just restates an episode without abstracting

For each new lesson, also pick which (if any) EXISTING lesson it
supersedes. Existing lessons:
{json.dumps(existing, indent=2) if existing else "(none yet)"}

Episodes (last 50 verified predictions, 30 fix outcomes, 15 chains):
{json.dumps(episodes, indent=2, default=str)[:8000]}

Return a JSON array (no markdown fences) of objects:
[
  {{
    "lesson":              "<1 sentence, ≤30 words>",
    "evidence_count":      <int — how many episodes support this>,
    "source_episode_titles": [<list of episode titles that taught this>],
    "supersedes":          "<exact text of existing lesson it replaces, or null>"
  }}
]

Cap at 8 lessons. Quality over quantity. Reply with ONLY the JSON array."""

    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5",
                  "max_tokens": 2500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"claude_{r.status_code}: {r.text[:200]}"}
        body = r.json() or {}
        text = "".join(b.get("text", "") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"): text = text[4:].lstrip("\n")
        lessons = json.loads(text)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    if not isinstance(lessons, list):
        return {"ok": False, "error": "claude returned non-list"}

    # Write lessons + supersede where applicable
    written = []
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return {"ok": False, "error": "no db on writeback"}
        try:
            cur = conn.cursor()
            for L in lessons[:8]:
                lesson = (L.get("lesson") or "").strip()
                if not lesson: continue
                supersedes = (L.get("supersedes") or "").strip() or None
                supersedes_id = None
                if supersedes:
                    cur.execute(
                        "SELECT id FROM brain_consolidated_lessons "
                        "WHERE lesson = %s AND active = TRUE LIMIT 1",
                        (supersedes,),
                    )
                    row = cur.fetchone()
                    if row:
                        supersedes_id = (row.get("id") if hasattr(row, "get")
                                          else row[0])
                        cur.execute(
                            "UPDATE brain_consolidated_lessons "
                            "SET active = FALSE WHERE id = %s",
                            (supersedes_id,),
                        )
                cur.execute(
                    "INSERT INTO brain_consolidated_lessons "
                    "(lesson, evidence_count, source_episodes, "
                    " supersedes_lesson_id) "
                    "VALUES (%s, %s, %s::jsonb, %s)",
                    (lesson,
                     int(L.get("evidence_count") or 1),
                     json.dumps(L.get("source_episode_titles") or []),
                     supersedes_id),
                )
                written.append({"lesson": lesson,
                                "supersedes_id": supersedes_id,
                                "evidence_count": int(L.get("evidence_count") or 1)})
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return {"ok": False, "error": f"writeback_{type(e).__name__}: {str(e)[:200]}"}

    return {"ok": True, "lessons_written": len(written), "written": written}


def get_active_lessons(limit: int = 15) -> list[dict]:
    """Public helper for other brain layers (esp. L14) to read the
    current lesson base into their prompts."""
    try:
        from main import get_db
        conn = get_db()
        if not conn: return []
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT lesson, evidence_count, learned_at "
                "FROM brain_consolidated_lessons "
                "WHERE active = TRUE "
                "ORDER BY learned_at DESC LIMIT %s",
                (limit,),
            )
            out = []
            for r in cur.fetchall():
                if hasattr(r, "get"):
                    out.append({
                        "lesson": r.get("lesson"),
                        "evidence_count": r.get("evidence_count"),
                        "learned_at": str(r.get("learned_at") or "")[:19],
                    })
                else:
                    out.append({"lesson": r[0],
                                 "evidence_count": r[1],
                                 "learned_at": str(r[2])[:19]})
            return out
        finally:
            try: conn.close()
            except Exception: pass
    except Exception: return []


@brain_layer18_bp.route("/api/v1/brain/lessons", methods=["GET"])
def lessons_list():
    """Current consolidated lessons (the brain's distilled knowledge)."""
    _ensure_table()
    lessons = get_active_lessons(limit=20)
    return jsonify(
        ok=True,
        count=len(lessons),
        active_lessons=lessons,
        note=("These are not findings — they're distilled rules. L14 "
              "reads them at every causal-analysis call. Updated by "
              "the L18 sleep cycle every 12h."),
    )


@brain_layer18_bp.route("/api/v1/brain/lessons/consolidate",
                        methods=["POST", "GET"])
def lessons_consolidate():
    if request.method == "POST" and _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    _ensure_table()
    result = _consolidate()
    # Pop ok out before splatting to avoid duplicate-keyword TypeError
    _ok = result.pop("ok", False)
    return jsonify(
        ok=_ok,
        ran_at=_dt.datetime.utcnow().isoformat() + "Z",
        **result,
    )
