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
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

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
        "funnel":             {k: funnel.get(k) for k in
                                ("tool_calls_7d", "upgrade_signals_7d",
                                 "conversions_30d", "keys_by_tier")},
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

Reply with plain prose (no markdown headers, no JSON). Be a coworker,
not a report. End with one sentence on what you'd do next if you were
the founder."""

    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5",
                  "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if r.status_code != 200:
            return jsonify(ok=False,
                           error=f"Claude API {r.status_code}",
                           detail=r.text[:200]), 503
        body = r.json() or {}
        text = "".join(b.get("text","") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 503

    return jsonify(
        ok=True,
        question=question,
        answer=text,
        based_on={
            "findings_count":    ctx["findings_count"],
            "memory_records":    ctx["memory_records"],
            "recent_commits":    len(ctx["recent_commits"]),
            "outreach_sent":     (ctx["outreach"] or {}).get("total_sent"),
            "has_current_plan":  ctx["current_plan"] is not None,
        },
        answered_at=_dt.datetime.utcnow().isoformat() + "Z",
    ), 200
