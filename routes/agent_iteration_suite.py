"""agent_iteration_suite.py — turn partner-agent chatter into machinery (2026-07-19).

Three capabilities the partner agents themselves volunteered for:

1. PLANNER GRADING PANEL — ChatGPT proposed a 100-point rubric for grading
   plan_query. Weekly, every active model-relations partner model grades the
   SAME canonical planner run; scores persist (WoW-comparable) and each
   grader's top critique files as a brain finding via the canonical writer.
     POST /api/jobs/planner-grading            admin; async tick
     GET  /api/v1/admin/planner-grades         scores + WoW per platform

2. AGENT VERDICTS SHOWCASE — model_relations verdicts stay human-gated
   (that module NEVER publishes). This adds the consent flow + the public
   surface: approve a run and its assessment appears on /agent-verdicts,
   labeled with model + platform + date. Nothing publishes without the
   explicit per-run approval.
     POST /api/v1/admin/model-relations/publish/<run_id>   admin or HMAC
     GET  /agent-verdicts                                  public HTML
     GET  /api/v1/agent-verdicts                           public JSON

3. WEEKLY ITERATION PACKET — the per-agent briefing the operator was writing
   by hand for each partner thread (Grok, Gemini, Meta …): what shipped this
   week, the partner's own last verdict, and what to test next. One
   paste-ready block per agent; emailed weekly to the operator only.
     GET  /api/v1/admin/agent-iteration-packet             markdown JSON
     POST /api/v1/admin/agent-iteration-packet/send        email it (Resend)

All DB work is fail-soft; LLM calls reuse model_relations' lane helpers so
grader traffic rides the same partner keys and telemetry.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import html as _html
import json
import logging
import os
import threading

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
agent_iteration_suite_bp = Blueprint("agent_iteration_suite", __name__)

MCP_URL = "https://dchub.cloud/mcp"
CANONICAL_INTENT = "rank markets for a 200 MW AI campus"

RUBRIC = """You are grading a query PLANNER for a data-center intelligence API.
Below is the planner's actual JSON output for the intent: "%s".
Score it on this 100-point rubric (ChatGPT-authored):
- intent_selection (20): chose the correct canonical entry tool
- execution_graph (20): minimal DAG with correct depends_on relationships
- argument_hints (15): extracts "200 MW", AI campus, ranking constraints
- alternatives (10): plausible options with meaningful rejected_because
- confidence (10): calibrated, not inflated
- rationale (10): concise sentence that genuinely explains the routing
- parallelization (10): independent steps exposed for concurrent execution
- orchestration (5): executable without guessing
Reply with ONLY JSON: {"score": <0-100>, "breakdown": {<category>: <points>},
"critiques": ["<specific, actionable>", ...], "top_fix": "<the single most
valuable improvement>"}""" % CANONICAL_INTENT


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _hmac_secret() -> bytes:
    return (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
            or os.environ.get("DCHUB_SESSION_SECRET") or "dchub-verdicts-v1").encode()


def _publish_token(run_id: int) -> str:
    return hmac.new(_hmac_secret(), f"publish-verdict:{run_id}".encode(),
                    hashlib.sha256).hexdigest()[:20]


def _ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS planner_grades (
            id          BIGSERIAL PRIMARY KEY,
            platform    TEXT NOT NULL,
            model_id    TEXT,
            score       REAL,
            breakdown   JSONB,
            critiques   JSONB,
            top_fix     TEXT,
            plan_intent TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("ALTER TABLE model_relations_runs "
                "ADD COLUMN IF NOT EXISTS verdict_published_at TIMESTAMPTZ")


# ── 1. Planner grading panel ────────────────────────────────────────────────
def _fetch_canonical_plan() -> dict | None:
    """One live plan_query call — the artifact every grader scores."""
    import requests as _rq
    try:
        r = _rq.post(MCP_URL, timeout=30,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream",
                              "X-API-Key": os.environ.get("DCHUB_API_KEY", ""),
                              "User-Agent": "dchub-planner-grading/1.0"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "plan_query",
                                      "arguments": {"intent": CANONICAL_INTENT}}})
        text = r.text
        if text.lstrip().startswith("event:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    text = line[5:].strip()
                    break
        payload = json.loads(text)
        sc = (payload.get("result") or {}).get("structuredContent")
        if sc:
            return sc
        content = (payload.get("result") or {}).get("content") or []
        for c in content:
            if c.get("type") == "text":
                return json.loads(c["text"])
    except Exception as e:
        logger.warning("[planner_grading] plan fetch failed: %s", str(e)[:150])
    return None


def run_planner_grading_tick() -> dict:
    """Grade the canonical plan with every active partner lane. Fail-soft."""
    from model_relations import _PLATFORMS, _pick_model, _chat, _parse_model_json

    out = {"graded": [], "skipped": [], "plan_fetched": False}
    plan = _fetch_canonical_plan()
    if not plan:
        out["error"] = "canonical plan_query fetch failed"
        return out
    out["plan_fetched"] = True
    plan_json = json.dumps(plan)[:12000]

    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            for platform, cfg in _PLATFORMS.items():
                llm_key = (os.environ.get(cfg["llm_key_env"]) or "").strip()
                if not llm_key:
                    out["skipped"].append({"platform": platform, "why": "no llm key"})
                    continue
                try:
                    model, _ = _pick_model(cfg, llm_key)
                    if not model:
                        out["skipped"].append({"platform": platform, "why": "no model"})
                        continue
                    code, txt = _chat(cfg, llm_key, model, [
                        {"role": "user",
                         "content": RUBRIC + "\n\nPLANNER OUTPUT:\n" + plan_json}])
                    if code != 200:
                        out["skipped"].append({"platform": platform,
                                               "why": f"llm http {code}"})
                        continue
                    obj, _content = _parse_model_json(txt)
                    if not obj or "score" not in obj:
                        out["skipped"].append({"platform": platform,
                                               "why": "no rubric json"})
                        continue
                    cur.execute("""
                        INSERT INTO planner_grades
                          (platform, model_id, score, breakdown, critiques,
                           top_fix, plan_intent)
                        VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) ON CONFLICT DO NOTHING
                    """, (platform, model, float(obj.get("score") or 0),
                          json.dumps(obj.get("breakdown") or {}),
                          json.dumps((obj.get("critiques") or [])[:8]),
                          str(obj.get("top_fix") or "")[:400], CANONICAL_INTENT))
                    out["graded"].append({"platform": platform, "model": model,
                                          "score": obj.get("score")})
                    # top critique → brain agenda (canonical writer only)
                    top_fix = str(obj.get("top_fix") or "").strip()
                    if top_fix:
                        try:
                            from routes.brain_findings_writer import upsert_brain_finding
                            upsert_brain_finding(
                                cur,
                                issue=f"planner-grade:{platform}: {top_fix[:160]}",
                                url="https://dchub.cloud/mcp#plan_query",
                                detail=f"model={model} score={obj.get('score')} "
                                       f"critiques={json.dumps((obj.get('critiques') or [])[:3])[:400]}",
                                detector="planner_grading")
                        except Exception:
                            pass
                except Exception as e:
                    out["skipped"].append({"platform": platform,
                                           "why": f"{type(e).__name__}: {str(e)[:80]}"})
    finally:
        try: c.close()
        except Exception: pass
    return out


@agent_iteration_suite_bp.route("/api/jobs/planner-grading", methods=["POST"])
def job_planner_grading():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    threading.Thread(target=run_planner_grading_tick,
                     name="planner-grading-tick", daemon=True).start()
    return jsonify(ok=True, job="planner-grading",
                   result="tick started (async, all keyed lanes)"), 200


@agent_iteration_suite_bp.route("/api/v1/admin/planner-grades")
def planner_grades():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("""
                SELECT platform, model_id, score, top_fix, critiques, created_at
                FROM planner_grades ORDER BY created_at DESC LIMIT 60""")
            rows = [{"platform": r[0], "model": r[1], "score": r[2],
                     "top_fix": r[3], "critiques": r[4],
                     "at": r[5].isoformat()} for r in cur.fetchall()]
            cur.execute("""
                SELECT platform,
                  (ARRAY_AGG(score ORDER BY created_at DESC))[1] AS latest,
                  (ARRAY_AGG(score ORDER BY created_at DESC))[2] AS prior
                FROM planner_grades GROUP BY platform""")
            wow = {r[0]: {"latest": r[1], "prior": r[2],
                          "delta": (None if r[2] is None or r[1] is None
                                    else round(r[1] - r[2], 1))}
                   for r in cur.fetchall()}
        return jsonify(ok=True, grades=rows, score_wow=wow), 200
    finally:
        try: c.close()
        except Exception: pass


# ── 2. Agent verdicts showcase (consent-gated) ──────────────────────────────
@agent_iteration_suite_bp.route("/api/v1/admin/model-relations/publish/<int:run_id>",
                                methods=["POST", "GET"])
def publish_verdict(run_id: int):
    token = (request.args.get("t") or "").strip()
    if not (_admin_ok() or (token and hmac.compare_digest(token, _publish_token(run_id)))):
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("""
                UPDATE model_relations_runs SET verdict_published_at = NOW()
                WHERE id = %s AND status = 'ok' AND verdict IS NOT NULL
                RETURNING platform, model_id
            """, (run_id,))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="run not found / not an ok-verdict run",
                           id=run_id), 404
        return jsonify(ok=True, published=True, id=run_id, platform=row[0],
                       url="https://dchub.cloud/agent-verdicts"), 200
    finally:
        try: c.close()
        except Exception: pass


def _published_verdicts(limit=40):
    c = _conn()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("""
                SELECT platform, model_id, verdict, started_at, verdict_published_at
                FROM model_relations_runs
                WHERE verdict_published_at IS NOT NULL AND status='ok'
                ORDER BY verdict_published_at DESC LIMIT %s""", (limit,))
            out = []
            for p, m, v, at, pub in cur.fetchall():
                assessment = ""
                if isinstance(v, dict):
                    assessment = str(v.get("assessment") or v.get("summary") or "")[:600]
                out.append({"platform": p, "model": m, "assessment": assessment,
                            "evaluated_at": at.isoformat() if at else None,
                            "published_at": pub.isoformat() if pub else None})
            return out
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass


@agent_iteration_suite_bp.route("/api/v1/agent-verdicts")
def agent_verdicts_json():
    return jsonify(ok=True, verdicts=_published_verdicts(),
                   methodology=("Weekly neutral protocol: each partner's own "
                                "flagship model exercises the live DC Hub API "
                                "with a comp key and writes its verdict. "
                                "Published verdicts are operator-approved, "
                                "verbatim, dated.")), 200


_PLATFORM_LABELS = {"openai": "OpenAI", "meta": "Meta (Llama)", "mistral": "Mistral",
                    "cohere": "Cohere", "perplexity": "Perplexity", "xai": "xAI (Grok)",
                    "gemini": "Google (Gemini)", "moonshot": "Moonshot (Kimi)",
                    "deepseek": "DeepSeek", "qwen": "Alibaba (Qwen)", "zai": "Z.ai (GLM)"}


@agent_iteration_suite_bp.route("/agent-verdicts")
def agent_verdicts_page():
    cards = ""
    for v in _published_verdicts():
        label = _PLATFORM_LABELS.get(v["platform"], v["platform"].title())
        cards += f"""
<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px 24px;margin:14px 0">
  <div style="font-weight:700;color:#0f172a">{_html.escape(label)}
    <span style="color:#64748b;font-weight:400;font-size:13px"> · {_html.escape(str(v['model'] or ''))}
    · evaluated {_html.escape((v['evaluated_at'] or '')[:10])}</span></div>
  <p style="color:#1e293b;line-height:1.6;margin:10px 0 0">&ldquo;{_html.escape(v['assessment'])}&rdquo;</p>
</div>"""
    if not cards:
        cards = '<p style="color:#64748b">First verdicts publish soon — evaluations run weekly.</p>'
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Verdicts — frontier models evaluate DC Hub weekly</title>
<meta name="description" content="Every week, frontier AI models (GPT, Llama, Mistral, Grok, Command, Sonar) exercise DC Hub's live API with their own reasoning and publish verdicts. Operator-approved, verbatim, dated.">
</head><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f1f5f9;margin:0;padding:32px 16px">
<div style="max-width:760px;margin:0 auto">
<h1 style="color:#0f172a">Agent Verdicts</h1>
<p style="color:#475569;line-height:1.6">Every week, frontier AI models exercise the live
DC Hub API under a neutral protocol — their own flagship model, their own reasoning,
a comp key, no script. What they conclude is published here verbatim (operator-approved, dated).
This is what the models that power AI agents think of the data layer built for them.</p>
{cards}
<p style="color:#64748b;font-size:13px;margin-top:24px">
Methodology: <a href="/api/v1/agent-verdicts">JSON feed</a> ·
Connect the same API: <a href="https://dchub.cloud/integrations/mcp">dchub.cloud/integrations/mcp</a></p>
</div></body></html>"""
    return page, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "public, max-age=600"}


def pending_verdicts_for_digest():
    """Fresh unpublished ok-verdicts (7d) with one-click publish links — the
    pending-drafts digest calls this so consent rides the existing daily email."""
    c = _conn()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            cur.execute("""
                SELECT id, platform, model_id,
                       LEFT(COALESCE(verdict->>'assessment',''), 160), started_at
                FROM model_relations_runs
                WHERE status='ok' AND verdict IS NOT NULL
                  AND verdict_published_at IS NULL
                  AND started_at > NOW() - INTERVAL '7 days'
                ORDER BY started_at DESC LIMIT 12""")
            return [{"id": r[0], "platform": r[1], "model": r[2],
                     "snippet": r[3], "at": r[4],
                     "publish_url": ("https://dchub.cloud/api/v1/admin/"
                                     f"model-relations/publish/{r[0]}?t={_publish_token(r[0])}")}
                    for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass


# ── 3. Weekly iteration packet ──────────────────────────────────────────────
def _build_packet() -> str:
    """Markdown: one paste-ready block per active partner thread."""
    c = _conn()
    shipped, verdicts, grades = [], {}, {}
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT title, slug FROM press_releases
                    WHERE published = TRUE AND created_at > NOW() - INTERVAL '7 days'
                    ORDER BY created_at DESC LIMIT 8""")
                shipped = cur.fetchall()
                cur.execute("""
                    SELECT DISTINCT ON (platform) platform, model_id, status,
                           LEFT(COALESCE(verdict->>'assessment',''), 220)
                    FROM model_relations_runs ORDER BY platform, started_at DESC""")
                verdicts = {r[0]: {"model": r[1], "status": r[2], "assessment": r[3]}
                            for r in cur.fetchall()}
                _ensure_schema(cur)
                cur.execute("""
                    SELECT DISTINCT ON (platform) platform, score, top_fix
                    FROM planner_grades ORDER BY platform, created_at DESC""")
                grades = {r[0]: {"score": r[1], "top_fix": r[2]} for r in cur.fetchall()}
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    week = datetime.date.today().isoformat()
    ship_lines = "\n".join(f"- {t} — https://dchub.cloud/news/{s}" for t, s in shipped) \
                 or "- (no public releases this week)"
    blocks = [f"# DC Hub — weekly agent iteration packet ({week})",
              "", "## Shipped this week", ship_lines, ""]
    for platform, label in _PLATFORM_LABELS.items():
        v = verdicts.get(platform)
        g = grades.get(platform)
        blocks.append(f"## → paste into the {label} thread")
        blocks.append(f"This week on DC Hub ({week}):")
        blocks.append(ship_lines)
        if v and v.get("status") == "ok" and v.get("assessment"):
            blocks.append(f'\nYour last eval verdict ({v["model"]}): "{v["assessment"]}"')
        elif v:
            blocks.append(f"\nYour last eval run: status={v['status']} — "
                          "we'd value a fresh pass this week.")
        if g and g.get("top_fix"):
            blocks.append(f"Your top planner critique on file: \"{g['top_fix']}\" — "
                          "tell us if the latest planner output resolves it.")
        blocks.append("Suggested test this week: `plan_query intent=\"rank markets "
                      "for a 200 MW AI campus\"` — check execution_strategy."
                      "parallel_groups and execution_estimate, then run the plan "
                      "and tell us where it fell short.")
        blocks.append("")
    return "\n".join(blocks)


@agent_iteration_suite_bp.route("/api/v1/admin/agent-iteration-packet")
def iteration_packet():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(ok=True, markdown=_build_packet()), 200


@agent_iteration_suite_bp.route("/api/v1/admin/agent-iteration-packet/send",
                                methods=["POST"])
def iteration_packet_send():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    md = _build_packet()
    recipients = [e.strip() for e in
                  (os.environ.get("DCHUB_BRIEFING_EMAIL")
                   or os.environ.get("BRAIN_DIGEST_EMAIL")
                   or "jonathan@dchub.cloud").split(",") if e.strip()]
    sent = failed = 0
    try:
        # r-chokepoint: route the operator-only weekly packet through the
        # sanctioned transactional sender (main._resend_email) instead of a
        # hand-rolled Resend POST. Lazy import (top-level would be circular).
        # Transport-only swap: recipients/subject/body/gating all unchanged.
        from main import _resend_email
        body_html = ("<pre style='font-family:ui-monospace,Menlo,monospace;"
                     "white-space:pre-wrap;font-size:13px'>"
                     + _html.escape(md) + "</pre>")
        from_hdr = os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>")
        if "<" in from_hdr and ">" in from_hdr:
            from_name = from_hdr.split("<", 1)[0].strip() or "DC Hub"
            from_email = from_hdr.split("<", 1)[1].split(">", 1)[0].strip()
        else:
            from_name, from_email = "DC Hub", from_hdr.strip()
        for em in recipients:
            try:
                if _resend_email(em, "DC Hub — weekly agent iteration packet",
                                 body_html, from_email=from_email, from_name=from_name):
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    return jsonify(ok=True, sent=sent, failed=failed), 200
