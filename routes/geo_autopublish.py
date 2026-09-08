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
    # ★2026-09-07: this pack used to SUM(queue_capacity_mw) over market_power_scores
    # GROUP BY iso. That is not an ISO queue total — those rows are not
    # market-scoped slices of one queue (ERCOT read 474,958 MW PER MARKET ROW), so
    # summing 19 of them produced 9,024,200 MW = 9.02 TW for ERCOT: roughly the
    # world's entire installed generating capacity, and 40x ERCOT's own published
    # >225 GW large-load queue. The draft was never wrong about the row it was
    # handed — the row was wrong. Repointed at interconnect_queue, the per-project
    # table the live /api/v1/interconnection-queue/by-iso route already aggregates
    # exactly this way (routes/interconnection_queues.py:1194).
    "queue_by_iso": {
        "tools": "get_interconnection_queue",
        # ★2026-09-07 BASIS: `WHERE capacity_mw IS NOT NULL` silently dropped 182
        # of 5,538 rows, so the draft read "ERCOT (1,826 projects, 455,350 MW)"
        # when ERCOT has 1,907 queued projects — 81 with no published capacity.
        # True, but the basis was invisible, which is the 178-vs-186 countries
        # lesson. The filter now lives in a COUNT FILTER instead of the WHERE, so
        # BOTH numbers reach the draft; SUM/AVG ignore NULLs on their own.
        "sql": """SELECT upper(iso) AS iso,
                      COUNT(*) AS projects_total,
                      COUNT(capacity_mw) AS projects_with_capacity,
                      ROUND(SUM(capacity_mw)::numeric,0) AS total_queue_mw,
                      ROUND(AVG(capacity_mw)::numeric,1) AS avg_project_mw
                 FROM interconnect_queue
                WHERE iso IS NOT NULL
                GROUP BY upper(iso)
                ORDER BY SUM(capacity_mw) DESC NULLS LAST LIMIT 8""",
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
    # ★2026-09-07: "How do I research data center M&A transactions and deal flow?"
    # is a MEASURED loss (cited to DCK, DCD, CBRE, JLL) that had no pack and was
    # warned about on every run. It grounds in canon, NOT in new SQL over `deals`:
    # that table carries ~2.9x duplication (the AUTO id embeds the ingest date, so
    # the same transaction re-ingests daily), and a bare COUNT(*) over it published
    # 5,222 against a canon of 1,400+ once already. canonical_stats.deals_phrase()
    # is the deduped, quarantine-filtered, floored figure. util/deals.py exists
    # because that predicate was hand-copied into seven files — writing an eighth
    # copy here is exactly what tests/test_deals_guard.py censuses for.
    "ma_coverage": {
        "tools": "list_transactions, hyperscaler_deals and deal_autopsy",
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
    ("ma_coverage",       ("m&a", "m and a", "merger", "acquisition", "acquisitions",
                           "deal flow", "transaction", "transactions", "who bought",
                           "acquired", "divestiture")),
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
        # require_competitor=False on purpose. This caller wants COVERAGE — is
        # there an answer we do not own? — not head-to-head. Under the strict
        # form only 5 of the 12-query battery ever qualified, because a loss is
        # only counted when one of seven hardcoded competitor regexes matches.
        # Measured 2026-09-07: DCMap took "What AI tools track data center
        # construction pipeline + capacity?" while we were absent, and the loop
        # saw no loss at all.
        rows = _find_lost_queries(conn, days=_GAP_LOOKBACK_DAYS,
                                  limit=max(int(limit) * 3, 12),
                                  require_competitor=False) or []
        # Head-to-head losses first: a query a named rival is winning is a
        # stronger signal than one nobody owns, and only `limit` get published.
        rows.sort(key=lambda r: (not (r.get("competitors") or []),))
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


# ── physical plausibility ceilings ────────────────────────────────────────────
# ★ NOT a correctness check, and it must never be described as one. This catches
# ORDER-OF-MAGNITUDE errors only — the class where an aggregate is summed over
# the wrong grain and the draft then faithfully publishes the result, which is
# exactly how queue_by_iso came within one cron tick of publishing 9.02 TW as
# ERCOT's interconnection queue (2026-09-07). It cannot tell a right number from
# a slightly wrong one. An empty result means nothing was CAUGHT, not that
# anything was VERIFIED.
#
# The ceilings are physical, not tuned: US installed generating capacity is
# ~1.2 TW and the whole world ~9 TW, so no ISO queue, market or facility row is
# a terawatt. A value over the ceiling is not "high" — it is a different unit or
# a double count.
_CEILINGS = (
    ("_mw", 1_000_000.0, "1 TW — larger than any ISO queue, market or facility"),
    ("_gw", 1_000.0, "1 TW expressed in GW"),
    ("_months", 600.0, "50 years"),
    ("_score", 100.0, "scores are 0-100"),
    ("_pct", 100.0, "percentages are 0-100"),
)


def _implausible_facts(rows: list) -> list:
    """[(field, value, ceiling_reason)] for every value that breaks a ceiling."""
    import decimal
    bad = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            # bool is an int subclass; Decimal is what psycopg2 returns for
            # ::numeric, and is NOT a numbers.Real — miss it and this guard
            # silently inspects nothing at all on live data.
            if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
                continue
            key = str(k).lower()
            for suffix, ceiling, why in _CEILINGS:
                if key.endswith(suffix) and abs(float(v)) > ceiling:
                    bad.append((str(k), float(v), why))
                    break
    return bad


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
                        ("markets_scored", "markets_phrase"),
                        # deduped + quarantine-filtered + floored DOWN. Never
                        # COUNT(*) FROM deals — see the ma_coverage pack note.
                        ("ma_deals_tracked", "deals_phrase")):
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
    "verified facts you are given. Neutral, specific, no hype. "
    # 2026-09-07: a subset presented as the whole is the quiet way an honest
    # number becomes a wrong one — see the projects_total / projects_with_capacity
    # split on queue_by_iso.
    "STATE THE BASIS: when a row carries both a total and a subset of it "
    "(e.g. projects_total alongside projects_with_capacity), never present the "
    "subset as the whole — say what the figure is over, e.g. '1,826 of 1,907 "
    "projects with a published capacity'."
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


# ── did publishing the page do anything? (2026-09-07) ────────────────────────
# Nothing linked a published page back to the query it was written to win, so
# nothing could say whether any of this moves the 3.9% citation rate. This
# ledger records what was published against which query; the MEASUREMENT is then
# free, because citation_hunter already probes those exact queries daily — the
# outcome read is a JOIN against citation_probes on probe_date > published_at,
# not a second prober.
_OUTCOME_DDL = """
CREATE TABLE IF NOT EXISTS geo_page_outcomes (
    slug         TEXT PRIMARY KEY,
    query        TEXT NOT NULL,
    pack         TEXT,
    source       TEXT,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _record_publish(slug: str, query: str, pack: str, source: str) -> None:
    """Best-effort ledger write. NEVER raises and never blocks a publish — the
    page is already live by the time this runs; losing the row costs us the
    measurement, not the page."""
    conn = None
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(_OUTCOME_DDL)
            # ★ ONE string literal, not two adjacent ones. regression_lint's
            # insert-no-on-conflict rule matches `INSERT INTO \w+[^;"']*`, which
            # stops dead at the first quote — so an ON CONFLICT living in the
            # SECOND literal is invisible to it and the INSERT reads as unguarded.
            # Keep the clause inside the same literal so the guard can see it.
            # DO NOTHING (not DO UPDATE) on purpose: a slug is published once, and
            # a re-publish must not reset published_at — that timestamp is the
            # split point for every before/after rate on the board.
            cur.execute("""
                INSERT INTO geo_page_outcomes (slug, query, pack, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (slug) DO NOTHING
            """, (slug[:200], (query or "")[:500], (pack or "")[:60], (source or "")[:20]))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception as e:
        logger.warning("geo_autopublish outcome ledger write failed for %s: %s",
                       slug, str(e)[:140])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _cite_rate_pct(cited: int, n: int):
    """None, not 0.0, when nothing was probed.

    ★ An unprobed page has NO rate. Returning 0.0 would put it on the board as a
    measured failure next to pages that really were probed and really were never
    cited — absence rendered as a zero, which is the confusion this whole loop
    keeps tripping over."""
    return round(100.0 * cited / n, 1) if n else None


def page_outcomes(limit: int = 50) -> dict:
    """Per published page: how the query it targeted has scored SINCE it went up.

    ★ This is an OBSERVATION, not an attribution. The battery probes 5 of 12
    queries a day, so n is small per page, and a query can start being cited for
    reasons that have nothing to do with the page. It reports before/after rates
    and the sample sizes they rest on; it does not claim the page caused the
    change, and no caller should phrase it that way.
    """
    out = {"ok": True, "pages": [], "note": (
        "before/after citation rate for the query each page targets. "
        "OBSERVATIONAL — small n, no causal claim.")}
    conn = None
    try:
        import psycopg2.extras
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return {"ok": True, "pages": [], "note": "no_db"}
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT o.slug, o.query, o.pack, o.source, o.published_at,
                       COUNT(p.*) FILTER (
                           WHERE p.probe_date > o.published_at::date)  AS probes_after,
                       COUNT(p.*) FILTER (
                           WHERE p.probe_date > o.published_at::date
                             AND COALESCE(p.dchub_mentioned, FALSE))   AS cited_after,
                       COUNT(p.*) FILTER (
                           WHERE p.probe_date <= o.published_at::date) AS probes_before,
                       COUNT(p.*) FILTER (
                           WHERE p.probe_date <= o.published_at::date
                             AND COALESCE(p.dchub_mentioned, FALSE))   AS cited_before
                  FROM geo_page_outcomes o
                  LEFT JOIN citation_probes p ON p.query = o.query
                 GROUP BY o.slug, o.query, o.pack, o.source, o.published_at
                 ORDER BY o.published_at DESC
                 LIMIT %s
            """, (int(limit),))
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("geo page_outcomes failed: %s", str(e)[:160])
        return {"ok": True, "pages": [], "note": f"unavailable: {type(e).__name__}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for r in rows:
        pa, ca = int(r["probes_after"] or 0), int(r["cited_after"] or 0)
        pb, cb = int(r["probes_before"] or 0), int(r["cited_before"] or 0)
        out["pages"].append({
            "slug": r["slug"], "query": r["query"], "pack": r["pack"],
            "source": r["source"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            "probes_before": pb, "cited_before": cb, "rate_before_pct": _cite_rate_pct(cb, pb),
            "probes_after": pa, "cited_after": ca, "rate_after_pct": _cite_rate_pct(ca, pa),
        })
    return out


@geo_autopublish_bp.route("/api/v1/media/geo/outcomes", methods=["GET"])
# ★ EDGE-CACHED. There is no CF bypass rule for /api/v1/media/; the catch-all
# `path contains "/api/v1/"` rule (#1 of 25, last-match-wins) applies, and it
# sets mode override_origin — so any Cache-Control we send here is ignored.
# The daily workflow reads this with a unique ?cb=<run id>, which is a distinct
# cache key and therefore always reaches origin. A HUMAN opening the bare URL
# can get a stale board. Not worth a live ruleset change plus a canon repin for
# a board that moves once a day; documented instead of silently shipped.
def page_outcomes_endpoint():
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 200))
    except Exception:
        limit = 50
    return jsonify(page_outcomes(limit=limit)), 200


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
        bad = _implausible_facts(rows)
        if bad:
            # Loud: a pack producing terawatts is a data defect somebody must fix,
            # not a page to quietly not write.
            logger.error("geo_autopublish REFUSED %s — implausible facts: %s",
                         item["slug"], bad[:3])
            considered.append({
                "slug": item["slug"], "skip": "implausible_facts", "source": src,
                "pack": item.get("pack"),
                "offending": [{"field": f, "value": v, "ceiling": w}
                              for f, v, w in bad[:3]]})
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
        if res.get("ok"):
            _record_publish(item["slug"], item.get("query") or "",
                            item.get("pack") or "", src)
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
