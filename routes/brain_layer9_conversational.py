"""
Brain L9 — Conversational (2026-05-19).

POST natural-language questions; brain calls Claude with FULL current
state as context, returns conversational answers. Brain becomes a
coworker you can chat with, not just a dashboard to read.

Examples:
  POST /api/v1/brain/ask {"q": "Why are MCP calls declining?"}
  POST /api/v1/brain/ask {"q": "Should I ship L7 proposal #2?"}
  POST /api/v1/brain/ask {"q": "What's the most likely root cause of
                                the current heartbeat staleness?"}
  POST /api/v1/brain/ask {"q": "If only one thing tonight, what?"}

Context fed to Claude:
  - All L0 findings (top 30)
  - L3 memory (top recurring fix scopes)
  - L6 predictions
  - L7 proposed detectors (pending)
  - Funnel state
  - Publisher state
  - Outreach log summary
  - Last 24h commits
  - The L8 orchestrator's most recent plan (if any)

No admin gate — read-only Q&A on already-public brain state. Rate-
limited by ANTHROPIC_API_KEY budget; default ~$0.01 per question.

rag-gap-l9: answers were grounded ONLY in the shallow live snapshot above
(4 internal endpoints) — zero recall of the brain's own corpus. Now each
question also fail-soft retrieves semantic recall (findings, strategic
recommendations, news, deals) + past-outcome lessons from routes.brain_rag
and injects them as a RETRIEVED CONTEXT / PAST LESSONS block ALONGSIDE the
snapshot (additive — the snapshot is never replaced, and any retrieval
failure degrades to the exact pre-RAG prompt).
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request
from utils.anthropic_helper import anthropic_messages_url
from routes.brain_llm_spend import instrumented_post as _llm_post

logger = logging.getLogger(__name__)
brain_layer9_bp = Blueprint("brain_layer9", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _internal(path: str, timeout: int = 6) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


def _gather_full_context() -> dict:
    """Same as L8 but shallower — Q&A doesn't need every field."""
    findings_raw = (_internal("/api/v1/brain/consistency-radar", 20).get("findings") or [])
    memory = _internal("/api/v1/brain/memory/stats")
    predict = _internal("/api/v1/brain/predictions")
    proposed = _internal("/api/v1/brain/proposed-detectors")
    funnel = _internal("/api/v1/mcp/funnel")
    ws = _internal("/api/v1/marketing/worker-status")
    outreach = _internal("/api/v1/media/outreach-log")
    orchestrator = _internal("/api/v1/brain/orchestrator")

    # Recent commits
    commits = []
    try:
        import requests
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
        since = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).isoformat() + "Z"
        h = {"Accept": "application/vnd.github+json"}
        if token: h["Authorization"] = f"Bearer {token}"
        r = requests.get(f"https://api.github.com/repos/{repo}/commits",
                         params={"since": since, "per_page": 25},
                         headers=h, timeout=8)
        if r.status_code == 200:
            commits = [(c.get("commit",{}).get("message") or "").split("\n")[0]
                       for c in (r.json() or [])][:15]
    except Exception: pass

    return {
        "findings_count":     len(findings_raw),
        "top_findings":       [{"issue": f.get("issue"),
                                "url":   f.get("url"),
                                "detail": (f.get("detail") or "")[:200]}
                               for f in findings_raw[:15]],
        "memory_records":     memory.get("total_records", 0),
        "top_recurring":      memory.get("top_recurring_issues", [])[:5],
        "predictions":        (predict.get("predictions") or [])[:5],
        "proposed_detectors": [{"name": p.get("detector_name"),
                                "scope": p.get("scope"),
                                "rationale": p.get("rationale"),
                                "decision": p.get("decision")}
                               for p in (proposed.get("proposals") or [])[:3]],
        # r85: feed the SEGMENTED funnel so the brain reasons on the real
        # convertible base, not the automation-diluted raw 36K. Raw tool_calls
        # are dominated by a few looping anonymous power-keys (0 distinct IPs);
        # the REAL convertible funnel is unique_ips_7d_real (distinct callers/wk)
        # -> keys_by_tier paid. The Inspector briefs keep panicking on "36K -> 8"
        # because they never saw this split.
        "funnel":             {k: funnel.get(k) for k in
                                ("tool_calls_7d", "tool_calls_7d_real",
                                 "real_external_calls_7d",
                                 "real_external_signals_7d",
                                 "unique_ips_7d_real",
                                 "upgrade_signals_7d", "conversions_30d",
                                 "keys_by_tier", "time_to_convert_90d")},
        "addressable_pool_paid_tools": (funnel.get("paid_tool_demand_30d") or [])[:6],
        "looping_anon_signal_tools":   (funnel.get("top_signal_tools_30d") or [])[:6],
        "funnel_field_meaning": (
            "tool_calls_7d is RAW and automation-inflated. The CONVERTIBLE base "
            "is unique_ips_7d_real (distinct real callers/wk) and "
            "real_external_signals_7d — compute conversion rate on THAT, not raw "
            "calls. ★2026-08-05: real_external_signals_7d counts UPGRADE/PAYWALL "
            "SIGNALS (rows in mcp_funnel_real, a view over mcp_upgrade_signals) — "
            "NOT sessions and NOT calls. Earlier copy here called it 'real "
            "sessions/wk', which was wrong in both directions: an agent can raise "
            "several signals in one session and most sessions raise none. Weekly "
            "external CALL volume is real_external_calls_7d, a different table and "
            "a different population — the two routinely move in OPPOSITE "
            "directions (2026-08-05: calls +87.4% WoW while signals -13.7%). Never "
            "compare or divide one by the other. "
            "addressable_pool_paid_tools = distinct FREE users hitting each "
            "paywalled tool (the real upgrade pool). looping_anon_signal_tools = "
            "a few anonymous keys looping with ~0 distinct IPs (not real demand)."
        ),
        "publisher":          ws.get("distribution", {}),
        "outreach": {
            "total_sent":  outreach.get("total"),
            "replied":     outreach.get("replied"),
            "recent":      [{"to": x.get("to"), "sent_at": x.get("sent_at"),
                             "replied_at": x.get("replied_at")}
                            for x in (outreach.get("log") or [])[:5]],
        },
        "recent_commits":     commits,
        "current_plan":       orchestrator.get("plan") if orchestrator.get("ok") else None,
    }


# ── rag-gap-l9: semantic recall grounding (fail-soft, additive) ────────
_RAG_QA_CORPORA = ["brain_findings", "brain_strategic_recommendations",
                   "news_articles", "deals"]


def _retrieve_grounding(question: str):
    """Fail-soft semantic recall for Q&A grounding. Returns (items, lessons).
    ANY failure (module missing, no embed key, DB down) → ([], []) so the
    prompt degrades to snapshot-only, byte-identical to the pre-RAG prompt."""
    if not (question or "").strip():
        return [], []
    items, lessons = [], []
    try:
        from routes.brain_rag import retrieve_context
        items = retrieve_context(question, k=6, corpus=_RAG_QA_CORPORA) or []
    except Exception:
        items = []
    try:
        from routes.brain_rag import retrieve_lessons
        lessons = retrieve_lessons(question, k=3) or []
    except Exception:
        lessons = []
    return items, lessons


def _grounding_block(items, lessons) -> str:
    """Render retrieval results as a prompt block. Returns "" when nothing was
    retrieved so the final prompt stays IDENTICAL to the pre-RAG prompt."""
    if not items and not lessons:
        return ""
    parts = []
    if items:
        parts.append(
            "RETRIEVED CONTEXT (semantic recall over brain findings, strategic "
            "recommendations, market news and M&A deals — the items most "
            "relevant to this question):\n"
            + json.dumps(items, indent=2, default=str)[:4500])
    if lessons:
        parts.append(
            "PAST LESSONS (outcomes of prior fixes/strategies — what actually "
            "worked or failed before; don't recommend a known failure):\n"
            + json.dumps(lessons, indent=2, default=str)[:2500])
    parts.append(
        "Grounding rules: when a claim relies on a retrieved item above, cite "
        "which item grounds it, e.g. [brain_findings #<source_id>] or "
        "[deals #<source_id>]. If neither the live context snapshot nor the "
        'retrieved items cover the question, say "not in my context" instead '
        "of guessing.")
    return "\n" + "\n\n".join(parts) + "\n"


# AUTO-REPAIR: duplicate route '/api/v1/brain/ask' also in routes/brain_qa.py:645 — review and remove one
@brain_layer9_bp.route("/api/v1/brain/ask", methods=["POST", "GET"])
def ask():
    """Natural-language Q&A against full brain state."""
    if not _ANTHROPIC_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set"), 503

    if request.method == "GET":
        question = request.args.get("q", "").strip()
    else:
        body = request.get_json(silent=True) or {}
        question = (body.get("q") or body.get("question") or "").strip()

    if not question:
        return jsonify(
            ok=False,
            error="missing q parameter",
            usage=("POST {q: 'your question'} or GET ?q=your+question. "
                   "Brain calls Claude with full current state as context."),
            example_questions=[
                "Why are MCP calls declining?",
                "Should I ship L7 proposal #2?",
                "What's the highest-leverage thing to do tonight?",
                "Has Rich Miller replied yet?",
                "Which 3 detectors fire most often this week?",
                "Is the publisher backlog growing or shrinking?",
            ],
        ), 400

    ctx = _gather_full_context()

    # rag-gap-l9: fail-soft semantic recall — additive alongside the snapshot,
    # never replacing it. On any retrieval failure grounding == "" and the
    # prompt below is byte-identical to the pre-RAG prompt.
    retrieved_items, retrieved_lessons = _retrieve_grounding(question)
    grounding = _grounding_block(retrieved_items, retrieved_lessons)

    prompt = f"""You are the DC Hub brain (L9 conversational). The founder
just asked you a question. You see the full current state of the system
as JSON context below. Answer the question directly, factually, and
briefly (2-4 sentences unless a list is warranted).

If the question is about WHAT TO DO, give a concrete recommendation
with reasoning. If the question is about WHY something is happening,
cite the specific signal from the context that supports your answer.
If you don't have enough data to answer, say so + name the signal you'd
need.

Question: {question}

Context:
{json.dumps(ctx, indent=2, default=str)[:9000]}
{grounding}
Reply with plain prose (no markdown headers, no JSON). Be a coworker,
not a report. End with one sentence on what you'd do next if you were
the founder."""

    # r85: reason with the Fable-5 reasoning tier (was hardcoded sonnet-4-5).
    # The founder wants conversion analysis done with the brain's best
    # reasoning model; brain_model_for('reasoning') respects
    # DCHUB_BRAIN_MODEL_REASONING + walks the fallback chain on a 404 so a
    # not-yet-provisioned model degrades instead of zeroing out the answer.
    from routes.brain_models import brain_model_for, fallback_for
    model = brain_model_for("reasoning")
    text = ""
    try:
        import requests
        for _attempt in range(4):
            r = _llm_post("brain_layer9_conversational",
                anthropic_messages_url(),
                headers={"x-api-key": _ANTHROPIC_KEY,
                         "User-Agent": "dchub-brain/1.0",
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": model,
                      "max_tokens": 3000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=45,
            )
            if r.status_code == 404:
                # ANY 404 from the messages API is a model-availability issue.
                # Don't gate on the word "model" in the body — the real-world
                # error that bit us was "Claude Fable 5 is not available. Please
                # use Opus 4.8" (no "model" token), so the gate silently failed
                # and the whole answer 404'd instead of degrading one tier.
                nxt = fallback_for(model)
                if not nxt:
                    return jsonify(ok=False,
                                   error=f"model {model} unavailable, no fallback"), 503
                model = nxt
                continue
            if r.status_code != 200:
                return jsonify(ok=False,
                               error=f"Claude API {r.status_code}",
                               detail=r.text[:200]), 503
            body = r.json() or {}
            text = "".join(b.get("text","") for b in (body.get("content") or [])
                           if b.get("type") == "text").strip()
            break
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 503

    return jsonify(
        ok=True,
        question=question,
        answer=text,
        model_used=model,
        based_on={
            "findings_count":    ctx["findings_count"],
            "memory_records":    ctx["memory_records"],
            "recent_commits":    len(ctx["recent_commits"]),
            "outreach_sent":     (ctx["outreach"] or {}).get("total_sent"),
            "has_current_plan":  ctx["current_plan"] is not None,
            # rag-gap-l9: how much semantic recall grounded this answer
            # (0/0 = retrieval unavailable → snapshot-only, pre-RAG behavior).
            "retrieved_items":   len(retrieved_items),
            "retrieved_lessons": len(retrieved_lessons),
        },
        answered_at=_dt.datetime.utcnow().isoformat() + "Z",
    ), 200
