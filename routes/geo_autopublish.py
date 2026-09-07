"""
geo_autopublish.py — 2026-07-03. FULLY self-driving GEO answer authoring.

The last wire of the acquisition loop: pick an uncovered high-intent query,
pull REAL data from the DB, have the LLM compose a citable answer grounded
ONLY in those verified facts (own-merits, no competitor mentions, no fabricated
numbers), then publish it via geo_answer_publisher (render → commit to frontend
→ CF Pages deploy). No human in the loop.

2026-09-07 — WHERE THE QUERIES COME FROM. This module used to author against a
hardcoded 5-slug list, 4 of which were already live, so it was structurally out
of work. It now also authors against MEASURED losses: citation_hunter probes a
12-query battery daily and records, per query, whether a competitor was cited
and we were not (citation_probes). Over 2026-08-08..09-07 that was 6 citations
in 155 probes (3.9%), with a rival cited and DC Hub absent on 20 of 31 days —
and nothing read those rows. See _lost_query_plan below.

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

from utils.anthropic_helper import cached_system
import os
import re
import json
import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
geo_autopublish_bp = Blueprint("geo_autopublish", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _enabled() -> bool:
    return os.environ.get("GEO_AUTOPUBLISH_ENABLED", "").strip().lower() in ("1", "true", "yes")


# ── verified fact packs ───────────────────────────────────────────────────────
# Hand-written, REVIEWED SQL, addressed by name. An LLM never authors SQL here:
# it is handed the rows these queries return and may use no number absent from
# them. Packs are named so a gap-driven page (below) reuses vetted facts instead
# of inventing a query for a question we have never seen before.
_FACT_PACKS = {
    "build_markets": {
        "tools": "rank_markets and get_grid_intelligence",
        "sql": """SELECT market_name, state, iso, excess_power_score,
                      time_to_power_months, queue_capacity_mw
                 FROM market_power_scores
                WHERE verdict='BUILD' AND excess_power_score IS NOT NULL
                ORDER BY excess_power_score DESC NULLS LAST LIMIT 8""",
    },
    "time_to_power": {
        "tools": "get_market_dcpi_rank and get_interconnection_queue",
        "sql": """SELECT market_name, state, iso, time_to_power_months, verdict,
                      excess_power_score
                 FROM market_power_scores
                WHERE time_to_power_months IS NOT NULL AND time_to_power_months > 0
                ORDER BY time_to_power_months ASC LIMIT 8""",
    },
    "water_risk": {
        "tools": "get_water_risk and score_facility",
        "sql": """SELECT market_name, state, iso, verdict, excess_power_score
                 FROM market_power_scores
                WHERE verdict='BUILD' ORDER BY excess_power_score DESC LIMIT 6""",
    },
    "avoid_markets": {
        "tools": "rank_markets and get_market_dcpi_rank",
        "sql": """SELECT market_name, state, iso, excess_power_score, verdict,
                      queue_wait_months
                 FROM market_power_scores
                WHERE verdict='AVOID' AND excess_power_score IS NOT NULL
                ORDER BY excess_power_score ASC NULLS LAST LIMIT 8""",
    },
    "queue_by_iso": {
        "tools": "get_interconnection_queue",
        "sql": """SELECT iso, COUNT(*) AS markets,
                      ROUND(AVG(queue_wait_months)::numeric,1) AS avg_queue_wait_months,
                      ROUND(SUM(queue_capacity_mw)::numeric,0) AS total_queue_mw
                 FROM market_power_scores
                WHERE iso IS NOT NULL AND queue_capacity_mw IS NOT NULL
                GROUP BY iso ORDER BY total_queue_mw DESC NULLS LAST LIMIT 8""",
    },
    # ── added 2026-09-07 so gap-driven pages don't all land on one pack ──
    "market_ranking": {
        "tools": "rank_markets and get_market_dcpi_rank",
        "sql": """SELECT market_name, state, iso, verdict, excess_power_score,
                      constraint_score, time_to_power_months
                 FROM market_power_scores
                WHERE COALESCE(published, TRUE) = TRUE
                  AND excess_power_score IS NOT NULL
                ORDER BY (COALESCE(excess_power_score,0)
                          - COALESCE(constraint_score,0)) DESC LIMIT 10""",
    },
    # Identity / "which platform" / "is there an MCP server" losses are the ones
    # we lose hardest (0/20 on the category phrasings, 2026-09-03) and no SQL
    # answers them — they want coverage counts. `facts` names a vetted Python
    # getter instead of SQL; canonical_stats floors DOWN and never over-claims.
    "platform_coverage": {
        "tools": "discover_tools, get_market_dcpi_rank and search_facilities",
        "facts": "canonical",
    },
}

# The original hand-written seed pages, expressed against the packs. Fixed slugs
# and fixed phrasing — these are the 5 we chose, not the ones we measured.
_QUERY_PLAN = [
    {"slug": "where-to-build-200mw-data-center-with-available-power",
     "query": "Where can I build a 200MW data center with available power?",
     "pack": "build_markets"},
    {"slug": "data-center-markets-with-fastest-time-to-power",
     "query": "Which data center markets have the fastest time to power?",
     "pack": "time_to_power"},
    {"slug": "data-center-water-risk-by-market",
     "query": "How do I check water risk for a data center site?",
     "pack": "water_risk"},
    {"slug": "data-center-markets-to-avoid-2026",
     "query": "Which data center markets should I avoid in 2026?",
     "pack": "avoid_markets"},
    {"slug": "interconnection-queue-data-by-iso-for-ai-agents",
     "query": "How do I get interconnection queue data by ISO into an AI agent?",
     "pack": "queue_by_iso"},
]
for _it in _QUERY_PLAN:
    _pk = _FACT_PACKS[_it["pack"]]
    _it["tools"] = _pk["tools"]
    _it["sql"] = _pk.get("sql")
    _it["facts"] = _pk.get("facts")
    _it["source"] = "seed"


# ── gap-driven plan: pages the MEASUREMENT asks for ───────────────────────────
# citation_hunter probes a rotating battery daily and records, per query, whether
# DC Hub was cited and which competitors were (citation_probes). A "lost query"
# is a row where a competitor IS cited and we are NOT. Until 2026-09-07 nothing
# read those rows: this module's target list was the 5 literals above, 4 of which
# were already live, so the self-driving author had one page of work left in it
# forever. Routing losses into the plan is what makes it a loop.
_GAP_LOOKBACK_DAYS = int(os.environ.get("GEO_AUTOPUBLISH_GAP_DAYS", "30") or 30)
_GAP_MAX_ITEMS = int(os.environ.get("GEO_AUTOPUBLISH_GAP_MAX", "6") or 6)


def _gap_enabled() -> bool:
    """Killable without a deploy, but ON by default — a wire that ships dark is
    the defect this change exists to fix. All publishing still sits behind
    GEO_AUTOPUBLISH_ENABLED."""
    return os.environ.get("GEO_AUTOPUBLISH_GAP_ENABLED", "1").strip().lower() \
        not in ("0", "false", "no", "off")


# Ordered; first match wins. A question we cannot ground in a vetted pack is
# REPORTED as no_fact_pack, never guessed at — an unroutable loss is the signal
# that the next pack is worth writing.
_PACK_ROUTER = [
    ("avoid_markets",     ("avoid", "worst", "overbuilt", "stay away")),
    ("time_to_power",     ("time to power", "fastest", "how soon", "lead time",
                           "energization", "how quickly")),
    ("water_risk",        ("water", "drought", "cooling")),
    ("queue_by_iso",      ("interconnection", "queue", " iso", "grid connection",
                           "construction pipeline", "pipeline")),
    ("build_markets",     ("available power", "power availability", "power capacity",
                           "capacity availability", "where to build", "site selection",
                           "land + power", "land and power", "powered land",
                           "evaluate land", "capacity for new data centers")),
    ("market_ranking",    ("largest data center markets", "biggest data center markets",
                           "growth rates", "compare data center sites", "hyperscale",
                           "market data", "rank", "best data center markets")),
    ("platform_coverage", ("intelligence platform", "research platform", "which platform",
                           "mcp server", "agent integration", "which companies",
                           "data source", "news", "analytics", "dcpi",
                           "data center power index", "api")),
]


def _route_to_pack(query: str) -> str | None:
    q = (query or "").lower()
    for pack_name, needles in _PACK_ROUTER:
        if any(n in q for n in needles):
            return pack_name
    return None


def _slugify(query: str) -> str:
    """Deterministic slug from the question text — the page URL then matches the
    query it was written to answer. Trimmed on a word boundary, never mid-word."""
    s = re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-")
    if len(s) > 80:
        s = s[:80].rsplit("-", 1)[0]
    return s.strip("-")


def _lost_query_plan(limit: int) -> tuple[list, list]:
    """(items, skipped) built from measured citation losses. Never raises."""
    conn = None
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return [], [{"skip": "gap_no_db", "source": "gap"}]
        from routes.media_citation_gap import _find_lost_queries
        rows = _find_lost_queries(conn, days=_GAP_LOOKBACK_DAYS,
                                  limit=max(int(limit) * 3, 12)) or []
    except Exception as e:
        logger.warning("geo_autopublish gap plan failed: %s", str(e)[:140])
        return [], [{"skip": f"gap_error:{type(e).__name__}", "source": "gap"}]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    items: list = []
    skipped: list = []
    seen = {i["slug"] for i in _QUERY_PLAN}
    for r in rows:
        q = (r.get("query") or "").strip()
        if not q:
            continue
        pack_name = _route_to_pack(q)
        if not pack_name:
            skipped.append({"query": q, "skip": "no_fact_pack", "source": "gap",
                            "competitors": r.get("competitors") or []})
            continue
        slug = _slugify(q)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        pack = _FACT_PACKS[pack_name]
        items.append({"slug": slug, "query": q, "pack": pack_name,
                      "tools": pack["tools"], "sql": pack.get("sql"),
                      "facts": pack.get("facts"), "source": "gap",
                      "competitors": r.get("competitors") or []})
        if len(items) >= int(limit):
            break
    return items, skipped


def _canonical_rows() -> list:
    """Coverage counts as a single verified row. Reuses canonical_stats' floored
    phrase helpers — the same source media_citation_gap grounds on. [] on any
    failure, so a fact we cannot read is omitted rather than invented."""
    try:
        import canonical_stats as cs
        facts = {}
        for key, fn in (("facilities_tracked", "facilities_phrase"),
                        ("facilities_verified", "facilities_verified_phrase"),
                        ("countries", "countries_phrase"),
                        ("markets_scored", "markets_phrase")):
            try:
                v = getattr(cs, fn)()
                if v:
                    facts[key] = v
            except Exception:
                pass
        try:
            facts["grid_coverage"] = cs.grid_coverage_phrase("short")
        except Exception:
            pass
        return [facts] if facts else []
    except Exception as e:
        logger.warning("geo_autopublish canonical rows failed: %s", str(e)[:120])
        return []


def _gather_facts(item: dict) -> list:
    if item.get("facts") == "canonical":
        return _canonical_rows()
    if not item.get("sql"):
        return []
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
        "model": "claude-sonnet-4-5", "max_tokens": 1100, "system": cached_system(_SYSTEM),
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
    """Publish (or preview) the next uncovered high-intent GEO page. One per call.

    Order: the 5 hand-written seed pages first, then pages the MEASUREMENT asks
    for — queries where citation_hunter observed a competitor cited and DC Hub
    not. The seed list is finite and nearly exhausted; the gap list refills
    itself daily, which is the difference between a backlog and a loop.
    """
    if not dry and not _enabled():
        return {"ok": True, "acted": False, "reason": "GEO_AUTOPUBLISH_ENABLED != 1"}
    considered = []
    gap_items: list = []
    gap_skipped: list = []
    if _gap_enabled():
        gap_items, gap_skipped = _lost_query_plan(_GAP_MAX_ITEMS)
    considered.extend(gap_skipped)
    for item in list(_QUERY_PLAN) + list(gap_items):
        src = item.get("source", "seed")
        if _page_exists(item["slug"]):
            considered.append({"slug": item["slug"], "skip": "already_live", "source": src})
            continue
        rows = _gather_facts(item)
        if not rows:
            considered.append({"slug": item["slug"], "skip": "no_facts", "source": src})
            continue
        draft = _llm_draft(item, rows)
        if not draft:
            considered.append({"slug": item["slug"], "skip": "draft_failed", "source": src})
            continue
        if dry:
            return {"ok": True, "acted": False, "dry_run": True,
                    "source": src, "pack": item.get("pack"),
                    "would_publish": {"slug": draft["slug"], "title": draft["title"],
                                      "short_answer": draft["short_answer"]},
                    "gap_candidates": len(gap_items), "considered": considered}
        try:
            from routes.geo_answer_publisher import publish_answer
            res = publish_answer(draft, overwrite=False)
        except Exception as e:
            return {"ok": False, "error": f"publish:{str(e)[:120]}", "considered": considered}
        return {"ok": bool(res.get("ok")), "acted": bool(res.get("ok")),
                "source": src, "pack": item.get("pack"), "question": item.get("query"),
                "published": res, "gap_candidates": len(gap_items),
                "considered": considered}
    return {"ok": True, "acted": False, "reason": "all_covered_or_unavailable",
            "gap_candidates": len(gap_items), "considered": considered}


@geo_autopublish_bp.route("/api/v1/admin/geo/autopublish", methods=["POST"])
def autopublish_endpoint():
    if _ADMIN_KEY:
        if (request.headers.get("X-Admin-Key") or "").strip() != _ADMIN_KEY:
            return jsonify(ok=False, error="unauthorized"), 401
    dry = request.args.get("dry") == "1"
    return jsonify(autopublish_next(dry=dry)), 200
