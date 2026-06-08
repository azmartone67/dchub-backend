"""
brain_qa.py — Brain ops Q&A endpoint (2026-06-07 R3, Unlock #3).
============================================================================

PROBLEM
-------
The operator has a question: "What's our MRR?" or "What did the brain
ship last week?" or "Why is sentinel inbox red?". Today they have to
click through 5 different admin dashboards to assemble an answer. The
brain has all this context already (funnel-health, brain-backlog,
sentinel, media, state-of-2026, brain_self_perception, code inventory).

This endpoint turns the brain into an ops Q&A bot. POST a question +
admin key, get a Claude-written answer with cited sources.

KEYWORD ROUTING
---------------
The endpoint inspects the question for keyword classes and pulls only
the relevant context (keeps prompts cheap; Sonnet by default):

  MRR / revenue / conversion / paid     → funnel-health
  brain / PR / proposal / strategic     → backlog + recent recs +
                                           code inventory + arch proposals
  media / posts / engagement / linkedin → media pulse + topic mix
  sentinel / health / uptime / 5xx      → sentinel inbox
  claims / trial / high-intent          → high-intent stats
  state of 2026 / launch                → QA harness + state pulse
  code / file / scanner / hotspot       → brain_code_inventory

A question that hits multiple buckets gets multiple context blocks; one
that hits zero gets a small fallback "system overview".

ENDPOINTS
---------
  POST /api/v1/brain/ask                  ask a question (admin)
  GET  /api/v1/brain/ask/status           config + cost-today (admin)
  GET  /api/v1/brain/ask/log              recent Q&A audit log (admin)
  GET  /admin/ask-brain                   simple chat UI

SCHEMA
------
  brain_qa_log (id, asked_at, question, answer, sources_json,
                 retrieved_data_json, model_used, claude_cost_cents,
                 asker_admin_key_hash, latency_ms)

COST CONTROL
------------
  · BRAIN_R3_DAILY_BUDGET_USD  (default $3) applies across the 3 R3
    unlocks combined. Q&A is the biggest spender — cap inside this
    module is 50 questions/day per admin key.
  · Each call is Sonnet by default (~$0.01-0.03 per Q).
  · Per-Q max-tokens cap: 1500 output.
  · 8 KB / ~2000 token context budget — keyword routing keeps it small.

SAFETY
------
  · BRAIN_R3_DISABLE=1              master kill
  · BRAIN_QA_DISABLE=1              per-unlock kill
  · ADMIN-KEY GATED — no public exposure
  · Every Q logged to brain_qa_log (with hashed key for auditing)
  · 50 Q/day per key cap (BRAIN_QA_DAILY_CAP)
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

from flask import Blueprint, jsonify, render_template_string, request

logger = logging.getLogger(__name__)

brain_qa_bp = Blueprint("brain_qa", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
_INTERNAL_BASE = (os.environ.get("INTERNAL_BASE_URL")
                  or "http://localhost:8080").rstrip("/")
_RAILWAY_BASE = "https://dchub-backend-production.up.railway.app"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return (_truthy(os.environ.get("BRAIN_R3_DISABLE"))
            or _truthy(os.environ.get("BRAIN_QA_DISABLE")))


def _daily_cap() -> int:
    try:
        return max(1, min(int(os.environ.get("BRAIN_QA_DAILY_CAP")
                                or "50"), 200))
    except Exception:
        return 50


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _provided_admin_key() -> str:
    return (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key")
            or request.args.get("admin_key")
            or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = _provided_admin_key()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


def _key_hash(k: str) -> str:
    if not k:
        return ""
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16]


# ─── DB ─────────────────────────────────────────────────────────────

def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_qa_log (
    id                   BIGSERIAL PRIMARY KEY,
    asked_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asked_on             DATE NOT NULL DEFAULT CURRENT_DATE,
    question             TEXT NOT NULL,
    answer               TEXT,
    sources_json         JSONB NOT NULL DEFAULT '[]'::jsonb,
    retrieved_data_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
    classes_matched      JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_used           TEXT,
    claude_cost_cents    NUMERIC,
    asker_admin_key_hash TEXT,
    latency_ms           INTEGER,
    error                TEXT,
    confidence           NUMERIC
);
CREATE INDEX IF NOT EXISTS ix_bqa_asked_at
    ON brain_qa_log(asked_at DESC);
CREATE INDEX IF NOT EXISTS ix_bqa_per_key_per_day
    ON brain_qa_log(asker_admin_key_hash, asked_on);
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
        logger.warning("brain_qa _ensure_schema failed: %s", e)


def _calls_today_for(key_hash: str) -> int:
    c = _get_db()
    if c is None:
        return 0
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM brain_qa_log
                    WHERE asker_admin_key_hash = %s
                      AND asked_on = CURRENT_DATE""", (key_hash,))
            r = cur.fetchone()
            return int(r[0] or 0) if r else 0
    except Exception:
        return 0
    finally:
        try: c.close()
        except Exception: pass


def _spend_today_cents() -> float:
    """Total Claude $ spent across ALL R3 surfaces TODAY in cents.
    Used by the daily budget gate."""
    c = _get_db()
    if c is None:
        return 0.0
    total = 0.0
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(claude_cost_cents), 0)
                     FROM brain_qa_log
                    WHERE asked_on = CURRENT_DATE""")
            r = cur.fetchone()
            total += float(r[0] or 0) if r else 0.0
            cur.execute(
                """SELECT COALESCE(SUM(claude_cost_cents), 0)
                     FROM brain_architecture_proposals
                    WHERE proposed_at::date = CURRENT_DATE""")
            r2 = cur.fetchone()
            total += float(r2[0] or 0) if r2 else 0.0
        return total
    except Exception:
        return 0.0
    finally:
        try: c.close()
        except Exception: pass


def _daily_budget_usd() -> float:
    try:
        return max(0.10, float(os.environ.get("BRAIN_R3_DAILY_BUDGET_USD")
                                or "3.00"))
    except Exception:
        return 3.00


# ─── Keyword routing ──────────────────────────────────────────────

_CLASSES = {
    "funnel": re.compile(
        r"\b(mrr|revenue|paid|convert|conversion|funnel|stripe|"
        r"upgrade|churn|customer|payment|subscription)\b", re.I),
    "brain": re.compile(
        r"\b(brain|layer.?\d|propos\w*|strategic|backlog|pr\b|"
        r"pull.?request|self.?perception|self.?assess)\b", re.I),
    "media": re.compile(
        r"\b(media|post|posts|linkedin|engagement|impression|topic|"
        r"viral|like|comment|share|twitter|bluesky|hn\b|hacker.?news)\b",
        re.I),
    "sentinel": re.compile(
        r"\b(sentinel|health|uptime|5xx|outage|grade|page|404|"
        r"endpoint|broken)\b", re.I),
    "high_intent": re.compile(
        r"\b(claim|trial|high.?intent|key|mcp.?key|signal|paywall)\b",
        re.I),
    "state_2026": re.compile(
        r"\b(state.?of.?2026|state-of-2026|launch|monday|qa.?harness|"
        r"living.?doc|narrative)\b", re.I),
    "code": re.compile(
        r"\b(code|file|scanner|inventory|hotspot|complexity|nesting|"
        r"untested|dead.?code|refactor|architecture)\b", re.I),
}


def _classify(question: str) -> list:
    """Return list of matched context-class names."""
    return [name for name, pat in _CLASSES.items() if pat.search(question)]


# ─── Context retrievers ────────────────────────────────────────────

def _http_get_json(path: str, timeout: int = 6) -> dict:
    """Same fail-soft pattern as brain_self_perception."""
    import urllib.request
    headers = {"X-Internal-Probe": "1",
               "User-Agent": "dchub-brain-qa/1.0"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
        headers["X-Internal-Key"] = ak
    for base in (_RAILWAY_BASE, _INTERNAL_BASE):
        try:
            req = urllib.request.Request(f"{base}{path}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, (dict, list)) else {}
        except Exception:
            continue
    return {}


def _call_in_process(name: str) -> Optional[dict]:
    """Avoid the 1-replica self-request deadlock by calling builders
    directly when we can."""
    try:
        if name == "funnel-health":
            from routes.funnel_health import _data_cached
            return _data_cached() or {}
        if name == "brain-backlog":
            from routes.brain_backlog_admin import _backlog_snapshot
            return _backlog_snapshot() or {}
    except Exception:
        return None
    return None


def _retrieve(classes: list) -> dict:
    """Fan out to dashboards based on matched classes. Returns
    {class_name: {data, source_path, _truncated_chars}}. Each block is
    capped at 2 KB so the combined prompt stays small."""
    out = {}
    cap = 2048

    def _trunc(obj, lim=cap):
        s = json.dumps(obj, default=str, indent=2)[:lim]
        return s, len(json.dumps(obj, default=str))

    if "funnel" in classes:
        data = (_call_in_process("funnel-health")
                or _http_get_json("/api/v1/admin/funnel-health?format=json")
                or {})
        body, full = _trunc(data)
        out["funnel"] = {"data": body, "source":
                          "/admin/funnel-health", "full_chars": full}
    if "brain" in classes:
        b = (_call_in_process("brain-backlog")
             or _http_get_json("/api/v1/admin/brain/backlog") or {})
        # also recent self_perception + proposals
        sp = _http_get_json("/api/v1/brain/self-perception/latest") or {}
        props = _http_get_json(
            "/api/v1/brain/architecture/proposals?limit=10") or {}
        merged = {"backlog": b, "self_perception": sp,
                  "architecture_proposals": props}
        body, full = _trunc(merged, lim=cap * 2)
        out["brain"] = {"data": body,
                         "source": "/admin/brain-backlog + self_perception "
                                    "+ arch_proposals", "full_chars": full}
    if "media" in classes:
        pulse = _http_get_json("/api/v1/media/pulse") or {}
        topics = _http_get_json("/api/v1/media/topic-pulse") or {}
        merged = {"pulse": pulse, "topics": topics}
        body, full = _trunc(merged)
        out["media"] = {"data": body,
                         "source": "/api/v1/media/{pulse,topic-pulse}",
                         "full_chars": full}
    if "sentinel" in classes:
        s = _http_get_json("/api/v1/admin/sentinel-inbox") or {}
        body, full = _trunc(s)
        out["sentinel"] = {"data": body, "source":
                            "/admin/sentinel-inbox", "full_chars": full}
    if "high_intent" in classes:
        hi = (_http_get_json("/api/v1/admin/mcp-high-intent/stats")
              or _http_get_json("/api/v1/mcp/high-intent/stats") or {})
        body, full = _trunc(hi)
        out["high_intent"] = {"data": body,
                               "source": "/admin/mcp-high-intent/stats",
                               "full_chars": full}
    if "state_2026" in classes:
        qa = _http_get_json("/api/v1/qa/state-of-2026") or {}
        pulse = (_http_get_json("/api/v1/state-of-2026/pulse")
                 or _http_get_json("/api/v1/admin/state-of-2026-pulse")
                 or {})
        merged = {"qa": qa, "pulse": pulse}
        body, full = _trunc(merged)
        out["state_2026"] = {"data": body,
                              "source": "/qa/state-of-2026 + "
                                         "/state-of-2026/pulse",
                              "full_chars": full}
    if "code" in classes:
        try:
            from routes.brain_code_scanner import gather_code_inventory_context
            ctx = gather_code_inventory_context() or {}
        except Exception:
            ctx = {}
        body, full = _trunc(ctx)
        out["code"] = {"data": body,
                        "source": "brain_code_inventory (hotspots)",
                        "full_chars": full}

    if not out:
        # Fallback overview: pull a tiny system snapshot so the brain
        # can still answer "how are we doing today"
        funnel = _call_in_process("funnel-health") or {}
        sent = _http_get_json("/api/v1/admin/sentinel-inbox") or {}
        merged = {"funnel_summary": (funnel.get("summary") or {}),
                  "sentinel_summary":
                      {"healthy": sum(1 for i in (sent.get("items") or [])
                                       if i.get("healthy")),
                       "total": len(sent.get("items") or [])}}
        body, full = _trunc(merged)
        out["_overview"] = {"data": body,
                             "source": "system overview fallback",
                             "full_chars": full}
    return out


# ─── Claude call ──────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the DC Hub Brain — an ops Q&A bot for the
operator. You have direct access to live dashboards. Today's job: read
the retrieved data below + the question, write a SHORT, FACTUAL answer
backed by specific numbers.

Output a single JSON object (no markdown fences, no commentary):

{
  "answer": "<2-5 sentences. Lead with the number/fact. Cite specific dashboards if relevant. NO generic preamble like 'Great question'.>",
  "key_numbers": [{"label": "...", "value": "..."}],
  "sources": ["/admin/funnel-health", ...],
  "confidence": <float 0.0-1.0>,
  "follow_up_questions": ["...", "..."]
}

RULES
─────
1. Lead with the specific number. "MRR is $X" not "Looking at MRR...".
2. If the retrieved data doesn't contain the answer, say so explicitly:
   "I don't have <metric> in the retrieved context — try /admin/<x>".
   Set confidence < 0.5.
3. NEVER make up numbers. If you can't find it, say so.
4. Cite the source paths in `sources` (use the paths from the data
   blocks, e.g. "/admin/funnel-health").
5. follow_up_questions are 1-3 things the operator might want to ask
   next given this answer.
6. Reply with ONLY the JSON object."""


def _call_claude(prompt: str) -> Optional[dict]:
    if not _ANTHROPIC_KEY:
        return None
    try:
        from routes.brain_models import brain_model_for, resolve_chain
        from utils.anthropic_helper import anthropic_messages_url
    except Exception as e:
        logger.warning("qa: model import failed: %s", e)
        return None

    chain = resolve_chain(brain_model_for("routine"))   # Sonnet by default
    last_err = None
    for model in chain:
        try:
            import requests
            r = requests.post(
                anthropic_messages_url(),
                headers={
                    "x-api-key":         _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "User-Agent":        "dchub-brain-qa/1.0",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 1500,
                    "system":     _SYSTEM_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=45,
            )
            if r.status_code == 404:
                last_err = f"{model}:404"
                continue
            if r.status_code != 200:
                last_err = f"{model}:{r.status_code}:{r.text[:200]}"
                logger.warning("qa: %s", last_err)
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
                logger.warning("qa: parse fail: %s; first 200: %s",
                                je, text[:200])
                continue
        except Exception as e:
            last_err = f"{model}:exc:{e}"
            continue
    logger.error("qa: all models failed; last=%s", last_err)
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


def _log_qa(question: str, answer: Optional[dict],
             retrieved: dict, classes: list, latency_ms: int,
             key_hash: str, error: Optional[str] = None) -> int:
    c = _get_db()
    if c is None:
        return 0
    _ensure_schema(c)
    try:
        sources = (answer.get("sources") if answer else []) or []
        in_tok = ((answer or {}).get("_input_tokens_est") or 0)
        out_tok = ((answer or {}).get("_output_tokens_est") or 0)
        model = (answer or {}).get("_model_used")
        cost_cents = _estimate_cost_cents(in_tok, out_tok,
                                            model or "claude-sonnet-4-5")
        confidence = float((answer or {}).get("confidence") or 0)
        # Strip the large data bodies before logging retrieved_data;
        # keep source paths and full_chars for audit.
        slim = {k: {"source": v.get("source"),
                    "full_chars": v.get("full_chars")}
                for k, v in (retrieved or {}).items()}
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_qa_log (
                       question, answer, sources_json,
                       retrieved_data_json, classes_matched,
                       model_used, claude_cost_cents,
                       asker_admin_key_hash, latency_ms, error,
                       confidence)
                   VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                           %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                   RETURNING id""",
                (question[:2000],
                 ((answer or {}).get("answer") or "")[:4000],
                 json.dumps(sources), json.dumps(slim),
                 json.dumps(classes), model, cost_cents, key_hash,
                 latency_ms, error, confidence))
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        return int(row[0]) if row else 0
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.error("qa _log failed: %s", e)
        return 0
    finally:
        try: c.close()
        except Exception: pass


# ─── End-to-end run ────────────────────────────────────────────────

def ask_brain(question: str, provided_key_hash: str) -> dict:
    started = time.time()

    if _kill_switch_on():
        return {"ok": False, "error": "kill_switch_on",
                "env": "BRAIN_R3_DISABLE or BRAIN_QA_DISABLE"}

    # Per-key daily cap
    used_today = _calls_today_for(provided_key_hash)
    cap = _daily_cap()
    if used_today >= cap:
        return {"ok": False, "error": "daily_cap_hit",
                "used_today": used_today, "cap": cap}

    # Budget gate (across all R3 surfaces)
    spent_cents = _spend_today_cents()
    budget_cents = _daily_budget_usd() * 100
    if spent_cents >= budget_cents:
        return {"ok": False, "error": "daily_budget_hit",
                "spent_cents": round(spent_cents, 2),
                "budget_cents": round(budget_cents, 2)}

    classes = _classify(question)
    retrieved = _retrieve(classes)

    # Build prompt
    blocks = [f"QUESTION: {question.strip()}",
              f"MATCHED CONTEXT CLASSES: {classes or ['(none — fallback)']}"]
    for cname, b in retrieved.items():
        blocks.append(f"\n── {cname.upper()} (source: {b.get('source')}, "
                       f"full_chars={b.get('full_chars')}) ──\n"
                       + b.get("data", ""))
    blocks.append(
        "\n──── End retrieved data. Reply with the JSON envelope ONLY. ────")
    prompt = "\n".join(blocks)

    payload = _call_claude(prompt)
    latency_ms = int((time.time() - started) * 1000)

    if not payload:
        _log_qa(question, None, retrieved, classes, latency_ms,
                 provided_key_hash, error="claude_call_failed")
        return {"ok": False, "error": "claude_call_failed",
                "classes_matched": classes,
                "context_kb": round(len(prompt) / 1024, 2),
                "latency_ms": latency_ms}

    in_tok = payload.get("_input_tokens_est") or 0
    out_tok = payload.get("_output_tokens_est") or 0
    model = payload.get("_model_used")
    cost_cents = _estimate_cost_cents(in_tok, out_tok,
                                        model or "claude-sonnet-4-5")

    row_id = _log_qa(question, payload, retrieved, classes, latency_ms,
                      provided_key_hash)

    # Build the public response
    return {
        "ok": True,
        "id": row_id,
        "answer": payload.get("answer"),
        "key_numbers": payload.get("key_numbers") or [],
        "sources": payload.get("sources") or [],
        "confidence": float(payload.get("confidence") or 0),
        "follow_up_questions":
            payload.get("follow_up_questions") or [],
        "classes_matched": classes,
        "model_used": model,
        "latency_ms": latency_ms,
        "cost_cents": cost_cents,
        "calls_today": used_today + 1,
        "daily_cap": cap,
        "retrieved_data": {k: {"source": v.get("source"),
                                "full_chars": v.get("full_chars")}
                            for k, v in retrieved.items()},
    }


# ─── HTTP routes ─────────────────────────────────────────────────

# AUTO-REPAIR: duplicate route '/api/v1/brain/ask' also in routes/brain_layer9_conversational.py:111 — review and remove one
@brain_qa_bp.route("/api/v1/brain/ask", methods=["POST", "GET"])
def brain_ask():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    q = (body.get("question") or request.args.get("question")
          or request.args.get("q") or "").strip()
    if not q:
        return jsonify(ok=False, error="missing question"), 400
    if len(q) > 2000:
        return jsonify(ok=False, error="question too long (>2000)"), 400
    result = ask_brain(q, _key_hash(_provided_admin_key()))
    return jsonify(result), (200 if result.get("ok") else 200)


@brain_qa_bp.route("/api/v1/brain/ask/status", methods=["GET"])
def brain_ask_status():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    key_hash = _key_hash(_provided_admin_key())
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        anthropic_key_set=bool(_ANTHROPIC_KEY),
        admin_key_set=bool(_admin_key()),
        daily_cap=_daily_cap(),
        calls_today_for_this_key=_calls_today_for(key_hash),
        daily_budget_usd=_daily_budget_usd(),
        spent_today_cents=round(_spend_today_cents(), 2),
        env_overrides={
            "BRAIN_R3_DISABLE":          "master kill (1=off)",
            "BRAIN_QA_DISABLE":          "per-unlock kill (1=off)",
            "BRAIN_QA_DAILY_CAP":        "questions/day per key (def 50)",
            "BRAIN_R3_DAILY_BUDGET_USD": "$/day cross-unlock cap (def 3.00)",
        },
        endpoints={
            "POST /api/v1/brain/ask":        "ask a question (admin)",
            "GET  /api/v1/brain/ask/status": "this page (admin)",
            "GET  /api/v1/brain/ask/log":    "recent Q&A audit (admin)",
            "GET  /admin/ask-brain":         "chat UI",
        },
    ), 200


@brain_qa_bp.route("/api/v1/brain/ask/log", methods=["GET"])
def brain_ask_log():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    limit = max(1, min(limit, 500))
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT id, asked_at, question, answer, sources_json,
                          classes_matched, model_used, claude_cost_cents,
                          asker_admin_key_hash, latency_ms, error,
                          confidence
                     FROM brain_qa_log
                    ORDER BY asked_at DESC LIMIT %s""", (limit,))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            sources = r[4] if isinstance(r[4], list) else (
                json.loads(r[4]) if r[4] else [])
            classes = r[5] if isinstance(r[5], list) else (
                json.loads(r[5]) if r[5] else [])
            out.append({
                "id": int(r[0]),
                "asked_at": r[1].isoformat() if r[1] else None,
                "question": r[2],
                "answer": (r[3] or "")[:800],
                "sources": sources, "classes_matched": classes,
                "model_used": r[6],
                "cost_cents": (float(r[7])
                                if r[7] is not None else None),
                "asker_key_hash": r[8],
                "latency_ms": int(r[9] or 0),
                "error": r[10],
                "confidence": (float(r[11])
                                if r[11] is not None else None),
            })
        return jsonify(ok=True, count=len(out), log=out), 200
    finally:
        try: c.close()
        except Exception: pass


_CHAT_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Ask the Brain</title>
<style>
 body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 800px; margin: 0 auto; padding: 2rem 1rem;
         background: #fafafa; color: #111; }
 h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
 .meta { color:#666; font-size:0.85rem; margin-bottom:1.5rem; }
 #log { background:#fff; border:1px solid #e5e5e5; border-radius:6px;
         padding:1rem; min-height:300px; max-height:600px;
         overflow-y:auto; margin-bottom:1rem; }
 .q { font-weight:600; margin:1rem 0 0.25rem; color:#06f; }
 .a { color:#111; white-space:pre-wrap; line-height:1.5; }
 .sources { font-size:0.75rem; color:#666; margin-top:0.4rem; }
 .meta-r { font-size:0.7rem; color:#999; margin-top:0.2rem; }
 .input-row { display:flex; gap:0.5rem; }
 input[type=text] { flex:1; padding:0.6rem; border:1px solid #ccc;
                     border-radius:4px; font-size:1rem; }
 button { background:#06f; color:#fff; border:0; padding:0.6rem 1.2rem;
           border-radius:4px; cursor:pointer; }
 button:disabled { opacity:0.5; cursor:not-allowed; }
 .followups { margin-top:0.6rem; }
 .followups button { background:#eef; color:#06f; font-size:0.8rem;
                      padding:0.3rem 0.6rem; margin:0.2rem 0.2rem 0 0; }
 .err { color:#b00; background:#fee; padding:0.5rem;
         border-radius:4px; }
 .keynums { margin-top:0.4rem; font-size:0.85rem; color:#444; }
 .keynums span { display:inline-block; background:#eef;
                  padding:0.1rem 0.4rem; margin-right:0.3rem;
                  border-radius:3px; }
</style></head><body>
<h1>Ask the Brain</h1>
<div class="meta">Admin-keyed Q&A. Question must arrive with admin key.
Cap: 50/day per key. Spends from BRAIN_R3_DAILY_BUDGET_USD ($3).</div>
<div id="log"></div>
<form id="askform" class="input-row">
  <input type="text" id="q" placeholder="What's our current MRR?"
          autocomplete="off" required>
  <button type="submit" id="btn">Ask</button>
</form>
<script>
const ADMIN_KEY =
  new URLSearchParams(location.search).get('admin_key') || '';
const log = document.getElementById('log');
const form = document.getElementById('askform');
const qIn = document.getElementById('q');
const btn = document.getElementById('btn');

function append(html) {
  log.insertAdjacentHTML('beforeend', html);
  log.scrollTop = log.scrollHeight;
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function ask(q) {
  qIn.value = q;
  btn.disabled = true;
  append('<div class="q">' + esc(q) + '</div>');
  append('<div class="a" id="thinking">Thinking…</div>');
  try {
    const url = '/api/v1/brain/ask'
      + (ADMIN_KEY ? '?admin_key=' + encodeURIComponent(ADMIN_KEY) : '');
    const r = await fetch(url, {
      method:'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({question: q}),
    });
    const j = await r.json();
    document.getElementById('thinking').remove();
    if (!j.ok) {
      append('<div class="err">' + esc(j.error || 'error') + '</div>');
      btn.disabled = false;
      return;
    }
    let kn = '';
    if (j.key_numbers && j.key_numbers.length) {
      kn = '<div class="keynums">' + j.key_numbers.map(n =>
        '<span>' + esc(n.label) + ': <b>' + esc(n.value) + '</b></span>'
      ).join('') + '</div>';
    }
    let sources = '';
    if (j.sources && j.sources.length) {
      sources = '<div class="sources">Sources: ' +
        j.sources.map(esc).join(', ') + '</div>';
    }
    let meta = '<div class="meta-r">Model: ' + esc(j.model_used) +
      ' · Conf: ' + (j.confidence||0).toFixed(2) +
      ' · ' + esc(j.latency_ms) + 'ms · ' +
      (j.cost_cents||0).toFixed(2) + '¢ · ' +
      esc(j.calls_today) + '/' + esc(j.daily_cap) + ' today</div>';
    let fu = '';
    if (j.follow_up_questions && j.follow_up_questions.length) {
      fu = '<div class="followups">' + j.follow_up_questions.map(q =>
        '<button onclick="ask(\\'' + q.replace(/\\'/g, "\\\\'") +
        '\\')">' + esc(q) + '</button>').join('') + '</div>';
    }
    append('<div class="a">' + esc(j.answer) + kn + sources + meta + fu +
            '</div>');
  } catch (e) {
    document.getElementById('thinking').remove();
    append('<div class="err">' + esc(e) + '</div>');
  }
  btn.disabled = false;
  qIn.value = '';
  qIn.focus();
}
window.ask = ask;
form.addEventListener('submit', e => {
  e.preventDefault();
  const q = qIn.value.trim();
  if (q) ask(q);
});
</script>
</body></html>"""


@brain_qa_bp.route("/admin/ask-brain", methods=["GET"])
def brain_ask_chat():
    return render_template_string(_CHAT_HTML)
