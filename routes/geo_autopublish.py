"""
geo_autopublish.py — 2026-07-03. FULLY self-driving GEO answer authoring.

The last wire of the acquisition loop: pick an uncovered high-intent query,
pull REAL data from the DB, have the LLM compose a citable answer grounded
ONLY in those verified facts (own-merits, no competitor mentions, no fabricated
numbers), then publish it via geo_answer_publisher (render → commit to frontend
→ CF Pages deploy). No human in the loop.

SAFETY (inherits + adds):
  · Facts are pulled from the DB and handed to the LLM as the ONLY allowed
    numbers — the same anti-hallucination contract media_citation_gap uses.
  · Anti-disparagement: the prompt forbids naming competitors.
  · Publisher is template-constrained + escapes everything (no HTML injection).
  · Idempotent: skips any slug already live on the frontend.
  · Rate: ONE page per call. Env-gated GEO_AUTOPUBLISH_ENABLED (default off →
    ships inert; a dry run always previews). Never raises.

Endpoint:
  POST /api/v1/admin/geo/autopublish        acts (admin-gated); ?dry=1 previews
"""

import os
import json
import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
geo_autopublish_bp = Blueprint("geo_autopublish", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _enabled() -> bool:
    return os.environ.get("GEO_AUTOPUBLISH_ENABLED", "").strip().lower() in ("1", "true", "yes")


# High-intent queries an agent's host LLM gets asked, each with the REAL data
# to ground the answer and the tools that serve it. `sql` returns the verified
# facts (rows) — the LLM may use NO number not present in them.
_QUERY_PLAN = [
    {"slug": "where-to-build-200mw-data-center-with-available-power",
     "query": "Where can I build a 200MW data center with available power?",
     "tools": "rank_markets and get_grid_intelligence",
     "sql": """SELECT market_name, state, iso, excess_power_score,
                      time_to_power_months, queue_capacity_mw
                 FROM market_power_scores
                WHERE verdict='BUILD' AND excess_power_score IS NOT NULL
                ORDER BY excess_power_score DESC NULLS LAST LIMIT 8"""},
    {"slug": "data-center-markets-with-fastest-time-to-power",
     "query": "Which data center markets have the fastest time to power?",
     "tools": "get_market_dcpi_rank and get_interconnection_queue",
     "sql": """SELECT market_name, state, iso, time_to_power_months, verdict,
                      excess_power_score
                 FROM market_power_scores
                WHERE time_to_power_months IS NOT NULL AND time_to_power_months > 0
                ORDER BY time_to_power_months ASC LIMIT 8"""},
    {"slug": "data-center-water-risk-by-market",
     "query": "How do I check water risk for a data center site?",
     "tools": "get_water_risk and score_facility",
     "sql": """SELECT market_name, state, iso, verdict, excess_power_score
                 FROM market_power_scores
                WHERE verdict='BUILD' ORDER BY excess_power_score DESC LIMIT 6"""},
    {"slug": "data-center-markets-to-avoid-2026",
     "query": "Which data center markets should I avoid in 2026?",
     "tools": "rank_markets and get_market_dcpi_rank",
     "sql": """SELECT market_name, state, iso, excess_power_score, verdict,
                      queue_wait_months
                 FROM market_power_scores
                WHERE verdict='AVOID' AND excess_power_score IS NOT NULL
                ORDER BY excess_power_score ASC NULLS LAST LIMIT 8"""},
    {"slug": "interconnection-queue-data-by-iso-for-ai-agents",
     "query": "How do I get interconnection queue data by ISO into an AI agent?",
     "tools": "get_interconnection_queue",
     "sql": """SELECT iso, COUNT(*) AS markets,
                      ROUND(AVG(queue_wait_months)::numeric,1) AS avg_queue_wait_months,
                      ROUND(SUM(queue_capacity_mw)::numeric,0) AS total_queue_mw
                 FROM market_power_scores
                WHERE iso IS NOT NULL AND queue_capacity_mw IS NOT NULL
                GROUP BY iso ORDER BY total_queue_mw DESC NULLS LAST LIMIT 8"""},
]


def _gather_facts(item: dict) -> list:
    conn = None
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return []
        cur = conn.cursor()
        cur.execute(item["sql"])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows
    except Exception as e:
        logger.warning("geo_autopublish facts failed for %s: %s",
                       item.get("slug"), str(e)[:120])
        return []
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


def _page_exists(slug: str) -> bool:
    """True if answers/<slug>.html is already on the frontend (idempotency)."""
    try:
        import requests
        tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
        repo = (os.environ.get("GEO_FRONTEND_REPO") or "azmartone67/dchub-frontend").strip()
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/answers/{slug}.html",
                         headers={"Authorization": f"Bearer {tok}",
                                  "Accept": "application/vnd.github+json",
                                  "User-Agent": "dchub-geo-autopublish/1.0"},
                         timeout=15)
        return r.status_code == 200
    except Exception:
        return True  # fail-closed: if we can't confirm, don't republish


_SYSTEM = (
    "You compose short, factual, citable reference answers for an LLM-readable "
    "knowledge page about DC Hub (dchub.cloud), the live MCP-native data layer "
    "for data-center / power infrastructure. Answer on DC Hub's own merits ONLY. "
    "Never name or compare competitors. Use NO number that is not in the "
    "verified facts you are given. Neutral, specific, no hype."
)


def _llm_draft(item: dict, rows: list) -> dict | None:
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key or not rows:
        return None
    try:
        from utils.anthropic_helper import anthropic_messages_url
    except Exception:
        return None
    user = (
        f"QUESTION: {item['query']}\n\n"
        f"VERIFIED FACTS (the ONLY numbers you may use — a JSON list of rows):\n"
        f"{json.dumps(rows, default=str)[:2200]}\n\n"
        f"The tools that serve this data: {item['tools']}.\n\n"
        "Return ONLY a JSON object with these keys:\n"
        '  "title": a specific, search-shaped page title (<=90 chars)\n'
        '  "lede": one sentence contrasting a general model\'s vague answer with the live one (<=200 chars)\n'
        '  "short_answer": 2-3 sentences opening with the single most relevant verified fact, mentioning DC Hub\'s MCP (<=380 chars)\n'
        '  "sections": array of 2 objects {"h": heading, "p": paragraph} — one listing the specific markets/numbers, one on how to get the live data via the tools\n'
        '  "meta_description": <=155 chars, includes 2-3 specifics\n'
        "No prose outside the JSON. No fabricated numbers."
    )
    body = json.dumps({
        "model": "claude-sonnet-4-5", "max_tokens": 1100, "system": _SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    try:
        import urllib.request
        req = urllib.request.Request(
            anthropic_messages_url(), data=body,
            headers={"Content-Type": "application/json", "X-API-Key": api_key,
                     "User-Agent": "dchub-brain/1.0", "Anthropic-Version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = json.loads(r.read().decode("utf-8"))
        text = "".join(p.get("text", "") for p in (payload.get("content") or [])
                       if isinstance(p, dict)).strip()
        # tolerate ```json fences
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip() if "```json" in text \
                else text.split("```")[1].strip()
        draft = json.loads(text)
        # strict shape validation
        if not all(draft.get(k) for k in ("title", "lede", "short_answer", "meta_description")):
            return None
        secs = draft.get("sections") or []
        if not isinstance(secs, list) or not secs:
            return None
        return {
            "slug": item["slug"], "question": item["query"], "tools": item["tools"],
            "title": str(draft["title"])[:120], "lede": str(draft["lede"])[:260],
            "short_answer": str(draft["short_answer"])[:600],
            "meta_description": str(draft["meta_description"])[:170],
            "sections": [{"h": str(s.get("h", ""))[:90], "p": str(s.get("p", ""))[:700]}
                         for s in secs if isinstance(s, dict)][:3],
        }
    except Exception as e:
        logger.warning("geo_autopublish LLM draft failed %s: %s",
                       item.get("slug"), str(e)[:120])
        return None


def autopublish_next(dry: bool = False) -> dict:
    """Publish (or preview) the next uncovered high-intent GEO page. One per call."""
    if not dry and not _enabled():
        return {"ok": True, "acted": False, "reason": "GEO_AUTOPUBLISH_ENABLED != 1"}
    considered = []
    for item in _QUERY_PLAN:
        if _page_exists(item["slug"]):
            considered.append({"slug": item["slug"], "skip": "already_live"})
            continue
        rows = _gather_facts(item)
        if not rows:
            considered.append({"slug": item["slug"], "skip": "no_facts"})
            continue
        draft = _llm_draft(item, rows)
        if not draft:
            considered.append({"slug": item["slug"], "skip": "draft_failed"})
            continue
        if dry:
            return {"ok": True, "acted": False, "dry_run": True,
                    "would_publish": {"slug": draft["slug"], "title": draft["title"],
                                      "short_answer": draft["short_answer"]},
                    "considered": considered}
        try:
            from routes.geo_answer_publisher import publish_answer
            res = publish_answer(draft, overwrite=False)
        except Exception as e:
            return {"ok": False, "error": f"publish:{str(e)[:120]}", "considered": considered}
        return {"ok": bool(res.get("ok")), "acted": bool(res.get("ok")),
                "published": res, "considered": considered}
    return {"ok": True, "acted": False, "reason": "all_covered_or_unavailable",
            "considered": considered}


@geo_autopublish_bp.route("/api/v1/admin/geo/autopublish", methods=["POST"])
def autopublish_endpoint():
    if _ADMIN_KEY:
        if (request.headers.get("X-Admin-Key") or "").strip() != _ADMIN_KEY:
            return jsonify(ok=False, error="unauthorized"), 401
    dry = request.args.get("dry") == "1"
    return jsonify(autopublish_next(dry=dry)), 200
