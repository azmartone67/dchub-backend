"""
linkedin_content_engine.py — Phase r49 (2026-05-25).

Replaces the fixed-template post generators in linkedin_quad_daily.py
with a Claude-Sonnet-composed engine that tells STORIES, not lists.

User asked: "the content should be something new, that illustrates
our capabilities, enhancements, or tells an amazing energy story."

So instead of always shipping {market, score, verdict} bullet posts,
this engine rotates through 6 story types:

  capability_spotlight  — "Did you know DC Hub can answer this?"
                          Real MCP tool + a worked example
  energy_narrative      — A real curtailment, grid emergency, or
                          capacity addition told as a story
  dcpi_scoop            — Contrarian market data: high DCPI score,
                          low public awareness
  shipped_this_week     — What we built last 7 days (from
                          auto_press_releases + brain_proposed_fixes)
  hyperscaler_drama     — Real recent news + our DCPI contrarian angle
  market_anomaly        — Biggest WoW score change across the scored markets

Each pulls real DB data, then asks Claude Sonnet to compose a
280-char hook + 2-3 insight beats + CTA + hashtags in DC Hub's voice.

Theme-diversity dedup: track types posted in last 14d, prefer
unused ones. Falls back to existing static templates if Anthropic
API is unavailable, so the slot never goes silent.
"""
from __future__ import annotations

from utils.anthropic_helper import cached_system
import datetime
import html as _html
import json
import logging
import os
import random
import urllib.request
import urllib.error
import urllib.parse
from contextlib import contextmanager
from utils.anthropic_helper import anthropic_messages_url

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── DB ─────────────────────────────────────────────────────────────

def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# ── Story-type registry ───────────────────────────────────────────
# Each entry knows how to pull data, what landing URL fits, and the
# prompt template that turns data into a story.

LANDING_BY_TYPE = {
    "capability_spotlight":   "https://dchub.cloud/mcp",
    "energy_narrative":       "https://dchub.cloud/dcpi",
    "dcpi_scoop":             "https://dchub.cloud/dcpi",
    "shipped_this_week":      "https://dchub.cloud/transparency",
    "hyperscaler_drama":      "https://dchub.cloud/hyperscaler-deals",
    "market_anomaly":         "https://dchub.cloud/dcpi",
    # r-agent-demand (2026-07-17): what AI agents actually asked the live layer
    # for — aggregate demand telemetry, the one dataset that moves while DCPI
    # is flat. /ai is the adoption surface the numbers come from.
    "agent_demand":           "https://dchub.cloud/ai",
    # r-capability-slot (rebuild 2026-07-18): a platform capability / milestone
    # announcement composed ONLY from the editorial cap_* lead's own figures.
    # This default landing is overridden per-post to the lead's own source_url in
    # compose_story_post (each capability card points at its own surface).
    "capability_update":      "https://dchub.cloud/whats-new",
    # r-operator (2026-08-07): the OPERATOR lane. Every other story type here
    # is about DC Hub or about a market; this is the first one about an
    # operator. /facilities is where a reader lands to check the claim.
    "operator_spotlight":     "https://dchub.cloud/facilities",
}

# Each story type maps to ONE of the 4 OG images we already serve.
OG_IMAGE_BY_TYPE = {
    "capability_spotlight":  "https://api.dchub.cloud/static/og/landing-agents.png",
    "energy_narrative":      "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "dcpi_scoop":            "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "shipped_this_week":     "https://api.dchub.cloud/static/og/landing-agents.png",
    "hyperscaler_drama":     "https://api.dchub.cloud/static/og/landing-hyperscaler-deals.png",
    "market_anomaly":        "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "agent_demand":          "https://api.dchub.cloud/static/og/landing-agents.png",
    # r-capability-slot (rebuild): default OG; overridden by the lead's branded
    # data-card (lead['card'] → _data_card_url) in compose_story_post.
    "capability_update":     "https://api.dchub.cloud/static/og/landing-agents.png",
    "operator_spotlight":    "https://api.dchub.cloud/static/og/landing-hyperscaler-deals.png",
}

# 2026-06-07: bridge from the content-engine's `story_type` (6 values, used
# inside this file) to the topic_tuner's 14-topic library (the unit the A/B
# learner aggregates against). One topic per story_type — we keep it 1:1
# instead of trying to match TOPIC_LIBRARY regexes here, because that map
# already classifies real posts; THIS map just gives the freshly-composed
# post a sensible topic key before the regex sees it. routes/media_style_ab.py
# consumes this via the wired pick_style_if_enabled().
_STORY_TYPE_TO_TOPIC = {
    "dcpi_scoop":           "dcpi_verdict",
    "market_anomaly":       "verdict_shift",
    "energy_narrative":     "energy_pricing",
    "hyperscaler_drama":    "hyperscaler_deal",
    "capability_spotlight": "ai_citation",
    "shipped_this_week":    "industry_pulse",
    # r-agent-demand (2026-07-17): same topic the editorial desk maps the
    # agent_demand KIND to (media_editorial._KIND_TO_TOPIC) so the style/topic
    # learners aggregate both vocabularies onto one unit.
    "agent_demand":         "industry_pulse",
    # r-capability-slot (rebuild): capability/milestone announcements aggregate
    # onto the same tuner unit as capability_spotlight (both are "what DC Hub can
    # do / just shipped" first-party angles).
    "capability_update":    "ai_citation",
    # r-operator: operator/portfolio news aggregates onto the deal unit — it is
    # the closest existing tuner topic and keeps both vocabularies on one key.
    "operator_spotlight":   "hyperscaler_deal",
}

# Known MCP tool catalog — used by capability_spotlight to pick a
# tool + describe an example call. Hand-curated from server-card.
#
# r-us-market-count (2026-09-04): the rank_markets "ask" carried a hard-typed
# market count and called the markets US-only; both were wrong (the index is
# global — #3805). NOT canon-bound: this list is module-level, so resolving
# canon here would put a DB query in the import path. An example ask does not
# need a count to be a good example, so it states none — the only form that
# cannot go stale in data evaluated at import.
_MCP_TOOL_HOOKS = [
    {"tool": "rank_markets",      "ask": "rank data-center markets by excess power for AI training"},
    {"tool": "explain_dcpi",      "ask": "explain why Phoenix is AVOID and Cheyenne is BUILD on the DCPI"},
    {"tool": "get_grid_data",     "ask": "pull live ERCOT load + reserve margin in JSON"},
    {"tool": "score_facility",    "ask": "score a candidate Northern Virginia site against 11 factors"},
    {"tool": "find_alternatives", "ask": "find 3 alternatives when NoVA queue is 60 months"},
    {"tool": "get_water_risk",    "ask": "check water stress before committing to a Phoenix build"},
    {"tool": "get_fiber_intel",   "ask": "show dark fiber routes for a Council Bluffs cluster"},
    {"tool": "get_tax_incentives","ask": "compare TX vs OH vs WY tax stacks for a 200MW build"},
    {"tool": "hyperscaler_deals", "ask": "track every Stargate / CoreWeave / AMD capex announcement"},
    {"tool": "ai_capacity_index", "ask": "find the 5 markets where 100MW of training can land in 90 days"},
]


# ── Recency filter for the varied RANDOM pullers ──────────────────
# The dcpi_scoop / market_anomaly pullers ORDER BY RANDOM() with NO recency
# guard, so they could re-pick a market the desk just posted about. This reads
# the durable lead ledger (linkedin_quad_daily.lead_entity) so those pullers
# exclude any market/entity that led a post in the last N days.
def _recent_lead_entities(days: int = 14) -> set[str]:
    if not (_pg and _dsn()):
        return set()
    try:
        with _conn() as c, c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT DISTINCT lead_entity FROM linkedin_quad_posts
                     WHERE success = TRUE
                       AND posted_at > NOW() - make_interval(days => %s)
                       AND lead_entity IS NOT NULL AND lead_entity <> ''
                """, (int(days),))
                return {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception:
                try: c.rollback()
                except Exception: pass
                return set()
    except Exception:
        return set()


def _market_recently_led(market_name: str, recent: set[str]) -> bool:
    """True if this market's normalized city token is in the recent-lead set."""
    if not market_name or not recent:
        return False
    import re as _re
    tok = _re.sub(r"[^a-z0-9]+", "", str(market_name).split(",")[0].lower())
    return bool(tok) and tok in recent


# ── Data pullers (one per story type) ─────────────────────────────

def _pull_capability_spotlight() -> dict:
    """Pick a random tool + add a real-data example for its theme."""
    tool = random.choice(_MCP_TOOL_HOOKS)
    extra = {}
    if not (_pg and _dsn()):
        return {"type": "capability_spotlight", "tool": tool, **extra}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if tool["tool"] in ("rank_markets", "ai_capacity_index", "market_anomaly"):
                cur.execute("""
                    SELECT market_name, verdict, excess_power_score, constraint_score
                      FROM market_power_scores
                     WHERE verdict='BUILD'
                     ORDER BY excess_power_score DESC LIMIT 3
                """)
                extra["sample_markets"] = [dict(r) for r in cur.fetchall()]
            elif tool["tool"] == "score_facility":
                cur.execute("""
                    SELECT name, location, operator
                      FROM facilities
                     WHERE country='United States' AND power_capacity_mw > 100
                     ORDER BY RANDOM() LIMIT 1
                """)
                row = cur.fetchone()
                if row: extra["sample_facility"] = dict(row)
    except Exception:
        pass
    return {"type": "capability_spotlight", "tool": tool, **extra}


def _pull_energy_narrative() -> dict:
    """Story-worthy grid event from last 14d."""
    if not (_pg and _dsn()):
        return {"type": "energy_narrative"}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pull a market with extreme curtailment OR a market with
            # large recent gen additions — both make stories.
            for sql in [
                """SELECT market_name, curtailment_pct, excess_power_score
                     FROM market_power_scores
                    WHERE curtailment_pct > 4
                    ORDER BY curtailment_pct DESC LIMIT 1""",
                """SELECT market_name, gen_additions_12mo_mw, excess_power_score
                     FROM market_power_scores
                    WHERE gen_additions_12mo_mw > 1000
                    ORDER BY gen_additions_12mo_mw DESC LIMIT 1""",
                """SELECT market_name, queue_wait_months, constraint_score
                     FROM market_power_scores
                    WHERE queue_wait_months > 36
                    ORDER BY queue_wait_months DESC LIMIT 1""",
            ]:
                try:
                    cur.execute(sql)
                    row = cur.fetchone()
                    if row:
                        d = dict(row)
                        return {"type": "energy_narrative", "story_data": d}
                except Exception:
                    continue
    except Exception:
        pass
    return {"type": "energy_narrative"}


def _pull_dcpi_scoop() -> dict:
    """Surface a market that's high-DCPI but low-public-awareness."""
    if not (_pg and _dsn()):
        return {"type": "dcpi_scoop"}
    try:
        _recent = _recent_lead_entities()
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # High excess power score, not a top-5 known name. Pull SEVERAL and
            # skip any market that led a post recently (variety fix 2026-07-03).
            cur.execute("""
                SELECT market_name, verdict, excess_power_score,
                       constraint_score, time_to_power_months
                  FROM market_power_scores
                 WHERE excess_power_score > 60
                   AND market_name NOT ILIKE '%%Northern Virginia%%'
                   AND market_name NOT ILIKE '%%Silicon Valley%%'
                   AND market_name NOT ILIKE '%%Loudoun%%'
                   AND market_name NOT ILIKE '%%Atlanta%%'
                   AND market_name NOT ILIKE '%%Dallas%%'
                   AND market_name NOT ILIKE '%%Chicago%%'
                 ORDER BY RANDOM() LIMIT 12
            """)
            rows = cur.fetchall() or []
            fresh = [r for r in rows
                     if not _market_recently_led((r or {}).get("market_name"), _recent)]
            pick = (fresh or rows)
            return {"type": "dcpi_scoop", "scoop": dict(pick[0]) if pick else None}
    except Exception:
        return {"type": "dcpi_scoop"}


def _pull_shipped_this_week() -> dict:
    """What DC Hub ADDED to the live index in the last 7 days — expansion from
    strength (real new data/coverage), NOT internal vanity metrics. r-expansion
    2026-06-17: was leading with internal ops counts (press releases, MCP-call
    volume, brain proposals) which read as navel-gazing and risked the inflated
    mcp_tool_calls number. Now pulls the VETTED /api/v1/whats-new adds (the same
    figures the public What's-New page shows — avoids re-deriving table/column
    names and the 5,532-vs-21,000 facilities-count conflict). Canonical headline
    totals are supplied by the prompt via _canon_media_phrases(), not queried
    here and never typed as literals — the retired '15,000+ facilities' /
    '4,000+ deals' spelling of this line is what shipped for months."""
    adds = {}
    try:
        import urllib.request as _u, json as _j
        base = os.environ.get("DCHUB_INTERNAL_API", "http://localhost:8080")
        with _u.urlopen(f"{base}/api/v1/whats-new", timeout=5) as r:
            wn = _j.loads(r.read().decode("utf-8"))
        for it in (wn.get("items") or wn.get("changes") or []):
            if isinstance(it, dict) and it.get("category") and (it.get("added") or 0) > 0:
                adds[it["category"]] = {"added_7d": it.get("added"),
                                        "added_1d": it.get("added_1d")}
    except Exception:
        pass
    return {"type": "shipped_this_week", "stats": {"added_this_week": adds}}


def _pull_hyperscaler_drama() -> dict:
    """Recent hyperscaler news + contrarian DCPI angle."""
    if not (_pg and _dsn()):
        return {"type": "hyperscaler_drama"}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT title, source, url, published_date
                  FROM news
                 WHERE published_date > NOW() - INTERVAL '3 days'
                   -- r-no-disparage (2026-06-23): dropped openai + anthropic from
                   -- this pull. The "contrarian take" prompt turned a partner-LAB
                   -- news headline ("Anthropic Mythos mess") into a published
                   -- partner-bashing post. Keep infra/hardware capex stories
                   -- (stargate/coreweave/amd/nvidia/microsoft) which are fair for a
                   -- DCPI angle; never frame a peer AI lab as the foil.
                   AND (LOWER(title) LIKE '%%stargate%%'
                        OR LOWER(title) LIKE '%%coreweave%%'
                        OR LOWER(title) LIKE '%%amd%%'
                        OR LOWER(title) LIKE '%%nvidia%%'
                        OR LOWER(title) LIKE '%%microsoft%%')
                 ORDER BY published_date DESC LIMIT 1
            """)
            news = cur.fetchone()
            cur.execute("""
                SELECT market_name, excess_power_score, verdict
                  FROM market_power_scores
                 WHERE verdict='BUILD' AND excess_power_score > 65
                 ORDER BY RANDOM() LIMIT 1
            """)
            mkt = cur.fetchone()
            return {
                "type": "hyperscaler_drama",
                "news": dict(news) if news else None,
                "market": dict(mkt) if mkt else None,
            }
    except Exception:
        return {"type": "hyperscaler_drama"}


def _pull_market_anomaly() -> dict:
    """Biggest WoW DCPI score change."""
    if not (_pg and _dsn()):
        return {"type": "market_anomaly"}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Compare latest vs 7d-prior score
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, excess_power_score AS now_e,
                           verdict AS now_v
                      FROM market_power_scores
                     ORDER BY market_slug, computed_at DESC
                ),
                prev AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, excess_power_score AS prev_e
                      FROM market_power_scores
                     WHERE computed_at < NOW() - INTERVAL '7 days'
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_name, l.now_e, p.prev_e, l.now_v,
                       (l.now_e - p.prev_e) AS delta
                  FROM latest l JOIN prev p ON l.market_slug = p.market_slug
                 WHERE p.prev_e IS NOT NULL
                   -- positive-results policy (2026-07-02): surface the biggest
                   -- GAIN (new headroom coming online), never the biggest drop
                   AND (l.now_e - p.prev_e) > 0
                 ORDER BY (l.now_e - p.prev_e) DESC LIMIT 8
            """)
            rows = cur.fetchall() or []
            # variety fix (2026-07-03): skip any market that led a post recently
            # so the "biggest mover" doesn't lock onto the same market for days.
            _recent = _recent_lead_entities()
            fresh = [r for r in rows
                     if not _market_recently_led((r or {}).get("market_name"), _recent)]
            pick = (fresh or rows)
            return {"type": "market_anomaly", "anomaly": dict(pick[0]) if pick else None}
    except Exception:
        return {"type": "market_anomaly"}


def _pull_agent_demand() -> dict:
    """r-agent-demand (2026-07-17): what AI agents actually ASKED the live
    layer for — per-tool distinct-caller counts (30d), honest distinct-agent
    reach (7d), and the weekly first-time-agent trend. The ONE material whose
    numbers move (and move UP) while DCPI sits flat, and an angle only DC Hub
    can publish. Aggregates only: the shared parser
    (media_editorial._agent_demand_metrics) enforces the k>=5 anonymity floor
    and requires genuine upward movement. demand=None on ANY gap — the
    composer then SKIPs honestly (all _static_fallback paths are dead by
    design; no fallback template)."""
    try:
        from routes.media_editorial import _agent_demand_metrics
    except Exception:
        return {"type": "agent_demand", "demand": None}

    def _get(path: str) -> dict:
        try:
            import urllib.request as _u, json as _j
            base = os.environ.get("DCHUB_INTERNAL_API", "http://localhost:8080")
            req = _u.Request(base + path, headers={
                "User-Agent": "dchub-internal-media/1.0",
                "X-Internal-Request": "1"})
            with _u.urlopen(req, timeout=8) as r:
                return _j.loads(r.read().decode("utf-8")) or {}
        except Exception:
            return {}

    demand = _agent_demand_metrics(_get("/api/v1/mcp/funnel"),
                                   _get("/api/v1/ai/reach"),
                                   _get("/api/v1/mcp/retention"))
    return {"type": "agent_demand", "demand": demand}


def _pull_operator_spotlight() -> dict:
    """Today's operator, from routes.operator_spotlight.

    Returns {"type": ..., "spotlight": None} when nothing clears the bar — and
    compose_story_post SKIPS on that rather than composing a generic operator
    profile. See the module docstring in routes/operator_spotlight.py: a daily
    cadence is a reason to have good material every day, not a reason to invent
    it on a slow one, and the industry reads this feed.
    """
    if not (_pg and _dsn()):
        return {"type": "operator_spotlight", "spotlight": None}
    try:
        from routes.operator_spotlight import pick_spotlight
        with _conn() as c:
            return {"type": "operator_spotlight", "spotlight": pick_spotlight(c)}
    except Exception as e:
        logger.warning("[operator-spotlight] pull failed: %s", e)
        return {"type": "operator_spotlight", "spotlight": None}


_PULLERS = {
    "capability_spotlight": _pull_capability_spotlight,
    "energy_narrative":     _pull_energy_narrative,
    "dcpi_scoop":           _pull_dcpi_scoop,
    "shipped_this_week":    _pull_shipped_this_week,
    # "hyperscaler_drama" retired 2026-07-02 (operator directive): no
    # commentary/contrarian takes on third-party news. The feed reports
    # DC Hub results, additions and capability — not reactions to others.
    "market_anomaly":       _pull_market_anomaly,
    # r-agent-demand (2026-07-17): the agent-demand story — registered here so
    # the rotation can reach it (a kind registered in the desk but not in the
    # composer is the known partially-registered failure mode).
    "agent_demand":         _pull_agent_demand,
    # r-operator (2026-08-07): registered in _PULLERS, LANDING_BY_TYPE,
    # OG_IMAGE_BY_TYPE, _STORY_TYPE_TO_TOPIC, the Claude prompt AND the card
    # fallback — all six. This file records the "half-wired" failure twice
    # (a kind registered in the desk but not the composer); LANDING_BY_TYPE and
    # OG_IMAGE_BY_TYPE are bare [] lookups, so a partial registration is a
    # KeyError at compose time, not a graceful skip.
    "operator_spotlight":   _pull_operator_spotlight,
}


# ── Theme-diversity selector ──────────────────────────────────────

def _pick_story_type(slot_topic: str | None = None) -> str:
    """Pick the next story type, avoiding ones used in the last 14 days
    for the SAME slot. Hardcoded slot→preferred mapping nudges the rotation
    so each slot retains some style identity (data/narrative/listicle/
    contrarian) but content varies.
    """
    # Slot-based preferred set (still varies within each).
    # r-strength 2026-06-17: LEAD every slot with a first-party "our story" type
    # — shipped_this_week (expansions / new data added) or capability_spotlight
    # (what DC Hub can answer). The market-commentary types (market_anomaly /
    # dcpi_scoop / hyperscaler_drama / energy_narrative) are demoted to fallback
    # variety. This is the fix for the feed reading like obscure market trivia
    # (the "Cedar Falls held at 41.9" post) instead of DC Hub from strength.
    # 2026-07-03 VARIETY: give every slot the FULL story-type set as candidates
    # (with a preferred head for slot identity) so the demoted varied pullers
    # (dcpi_scoop / market_anomaly / energy_narrative) are actually reachable in
    # the rotation rather than pinned to fallback. The 14-day dedup below then
    # forces movement across types.
    # r-agent-demand (2026-07-17): agent_demand joins the rotation — at the
    # HEAD for industry_pulse (it IS the pulse, read off real demand) and its
    # own dedicated slot-topic, high for hyperscaler_deal/ai_capex_index. The
    # reachability append below still exposes it to every other slot.
    preferred = {
        "dcpi_mover":         ["shipped_this_week", "capability_spotlight", "dcpi_scoop", "market_anomaly", "energy_narrative"],
        "hyperscaler_deal":   ["capability_spotlight", "agent_demand", "shipped_this_week", "dcpi_scoop", "market_anomaly", "energy_narrative"],
        "ai_capex_index":     ["shipped_this_week", "agent_demand", "capability_spotlight", "market_anomaly", "dcpi_scoop", "energy_narrative"],
        "industry_pulse":     ["agent_demand", "capability_spotlight", "shipped_this_week", "energy_narrative", "dcpi_scoop", "market_anomaly"],
        "agent_demand":       ["agent_demand", "shipped_this_week", "capability_spotlight", "energy_narrative", "dcpi_scoop", "market_anomaly"],
    }
    _all = list(_PULLERS.keys())
    candidates = preferred.get(slot_topic or "", _all)
    # ensure every pullable type is reachable (append any not already listed)
    candidates = candidates + [t for t in _all if t not in candidates]

    if not (_pg and _dsn()):
        return random.choice(candidates)
    try:
        with _conn() as c, c.cursor() as cur:
            # FIX (2026-07-03): the 14-day story-type dedup was a NO-OP — it
            # queried `topic`, but _record wrote the SLOT topic (dcpi_mover/...)
            # there, never the STORY TYPE. Query the dedicated story_type column
            # (written by linkedin_quad_daily._record from composed['story_type']).
            # COALESCE-guard for the pre-migration window where the column may be
            # absent on an un-upgraded replica → fall back gracefully.
            used = set()
            try:
                cur.execute("""
                    SELECT DISTINCT story_type FROM linkedin_quad_posts
                     WHERE posted_at > NOW() - INTERVAL '14 days'
                       AND story_type = ANY(%s)
                """, (candidates,))
                used = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception:
                try: c.rollback()
                except Exception: pass
                used = set()
        fresh = [t for t in candidates if t not in used]
        return random.choice(fresh or candidates)
    except Exception:
        return random.choice(candidates)


# ── Claude composer ───────────────────────────────────────────────

# r86c: analyst voice. Single source of truth lives in routes/media_editorial.py
# (ANALYST_VOICE); import it with an inline fallback so a boot-order hiccup can
# never break composition. This replaced the old brand-evangelism prompt that
# made every post read like marketing ("build DC Hub into THE authority / make
# DC Hub the lens"), which the media audit + user flagged as the core problem.
try:
    from routes.media_editorial import ANALYST_VOICE as _ANALYST_VOICE
except Exception:
    _ANALYST_VOICE = (
        "You are a senior data-center infrastructure analyst. Lead every post "
        "with a specific NUMBER + the TREND (vs last week / ISO peers) + the "
        "SO-WHAT for a site-selection or capex decision, then a non-obvious "
        "implication — written, never announced, and never under a fixed label "
        "such as \"second-order read\". Dry, specific, no promotion. Never invent a figure. "
        "Attribution is one neutral source line AFTER the insight, never before. "
        "No brand-pillar speech, no 'we are the authority'. 2-3 hashtags max.")

_VOICE_SYSTEM = _ANALYST_VOICE + """

OUTPUT CONTRACT (this generator): output the POST TEXT ONLY — no preamble, no
surrounding quotes. 700-1500 characters. If a data lead is provided in the user
message, the FIRST sentence must state its number; do not open with a brand line.
HOOK: LinkedIn truncates the feed at ~140 characters ("...more"). Front-load the
single strongest concrete number + claim into the FIRST ~12 words so the hook
lands BEFORE the fold. Never open with an obscure market name or a setup clause.
A landing URL may be included as a single optional source line after the insight."""


def _recent_block_reasons(days: int = 7, n: int = 200) -> list:
    """Reasons the publish gate REFUSED this desk's own drafts.

    The other half of the composer's editorial memory. _recent_post_openings
    shows it what SHIPPED; this shows it what was THROWN OUT and why — which
    nothing has ever done. Same source `/api/v1/media/self-critique` reads
    (media_review_log), so the endpoint and the prompt cannot disagree about
    what the lessons are.

    Best-effort: empty on any error, and the table may not exist until the
    first block is recorded.
    """
    if not (_pg and _dsn()):
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT reason FROM media_review_log
                 WHERE decision = 'blocked'
                   AND created_at > NOW() - make_interval(days => %s)
                 ORDER BY created_at DESC LIMIT %s
            """, (int(days), int(n)))
            return [(r[0] if not hasattr(r, "get") else r.get("reason")) or ""
                    for r in (cur.fetchall() or [])]
    except Exception as e:
        logger.info("[composer] block-reason read skipped: %s", str(e)[:120])
        return []


def _recent_post_openings(days: int = 14, n: int = 12) -> list[str]:
    """Openings (first ~180 chars) of recently PUBLISHED LinkedIn posts.
    This is the composer's editorial memory (2026-07-02, operator "it tends
    to share the same things"): the model sees what already ran and is told
    to advance the story, not restate it. Best-effort — empty on any error."""
    if not (_pg and _dsn()):
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            # UNION both post ledgers: direct posters (showcase, media hub)
            # record only in linkedin_posts — without it the composer can't
            # see half of what actually shipped.
            cur.execute("""
                SELECT opening FROM (
                    SELECT LEFT(content, 180) AS opening, created_at AS ts
                      FROM social_media_posts
                     WHERE status = 'published' AND publish_platform = 'linkedin'
                       AND created_at > NOW() - make_interval(days => %s)
                    UNION ALL
                    SELECT LEFT(content, 180) AS opening, posted_at AS ts
                      FROM linkedin_posts
                     WHERE posted_at > NOW() - make_interval(days => %s)
                ) u ORDER BY ts DESC LIMIT %s
            """, (days, days, n))
            return [r[0].replace("\n", " ").strip()
                    for r in (cur.fetchall() or []) if r and r[0]]
    except Exception:
        return []


# The composer model. Default is Fable (the brain's model) — the operator
# wants the media desk to lean on the strongest editorial judgment available.
# Retries once on Sonnet if the primary model errors, so a model-access blip
# never silences the desk. NOTE: do NOT enable thinking here — with thinking
# on, reasoning tokens eat max_tokens and the post comes back truncated.
_MEDIA_MODEL = os.environ.get("DCHUB_MEDIA_MODEL", "claude-fable-5")
_MEDIA_MODEL_FALLBACK = "claude-sonnet-4-5"

# Ring buffer of the last 50 compose outcomes — read by
# /api/v1/media/composer-stops so the truncation rate is answerable
# from outside without a deploy or a log grep.
_COMPOSE_STOPS: list = []

# ★ This module had NO module-level logger. The first cut of the stop_reason
#   work called logger.warning() on the max_tokens path — a NameError that the
#   outer `except Exception` around _call() would have swallowed into a silent
#   fallback-model retry, recording nothing. It survived the unit tests because
#   _record_compose_stop wraps its own logging in try/except, so the ONLY
#   visible symptom was a compose that returned None for no stated reason.
#   Caught by a call-site test written after mutation testing showed the
#   helper-level tests were vacuous.
logger = logging.getLogger(__name__)


def _record_compose_stop(model, stop_reason, output_tokens, chars):
    """Record how a compose ENDED, so truncation stops being a guess.

    Best-effort and total: a telemetry write must never fail a compose. Kept
    separate from the caller so the fact is recorded on BOTH the normal and the
    max_tokens path — a stat you only keep on failure cannot show you a rate.
    """
    try:
        logger.info("[composer] model=%s stop_reason=%s output_tokens=%s chars=%s",
                    model, stop_reason, output_tokens, chars)
    except Exception:
        pass
    try:
        _COMPOSE_STOPS.append({
            "model": model, "stop_reason": stop_reason,
            "output_tokens": output_tokens, "chars": chars,
        })
        del _COMPOSE_STOPS[:-50]     # bounded: last 50, never a leak
    except Exception:
        pass


def _compose_with_claude(story_type: str, data: dict, landing: str,
                          lead: dict | None = None) -> str | None:
    """Compose the post with the brain model (Fable by default).

    Returns the post text, the literal string "SKIP" when the model judges
    it has nothing genuinely new to add versus the recently-published feed,
    or None on failure (caller falls back to a static template).
    """
    if not ANTHROPIC_API_KEY:
        return None

    # Per-story-type prompt
    user_prompt = _build_user_prompt(story_type, data, landing)
    if not user_prompt:
        return None

    # r86c: prepend the brain editorial desk's data lead so the post OPENS with
    # a real number+trend+so-what (and clears the number-lead publish gate).
    if lead:
        try:
            from routes.media_editorial import lead_prompt_block
            _lb = lead_prompt_block(lead)
            if _lb:
                user_prompt = _lb + "\n" + user_prompt
        except Exception:
            pass

    # Editorial memory: the recently-published feed + the continuing-story
    # contract. The SKIP escape hatch is what makes the desk intuitive —
    # given the choice, the model can decline to repeat itself and the slot
    # suppresses instead of shipping a rephrase.
    recents = _recent_post_openings()
    if recents:
        user_prompt = (
            "ALREADY PUBLISHED on the DC Hub feed recently (newest first):\n"
            + "\n".join(f"- {r}" for r in recents)
            + "\n\nYou are writing the NEXT installment of one continuing "
            "analyst column, not an isolated post. Assume the reader saw the "
            "posts above. Do not reuse their hooks, markets, headline metrics "
            "or angles — advance the story with a genuinely different lead or "
            "an implication the feed has not drawn yet. If the data below "
            "offers nothing meaningfully new versus the feed above, reply with "
            "exactly SKIP and nothing else.\n\n"
            + user_prompt
        )

    # ★ 2026-08-24: THE PROMPT WAS TEACHING THE TIC. 13 of the 15 posts that
    # actually shipped opened a paragraph with "The second-order read" — and
    # the instruction directly above used to end "...or a second-order read the
    # feed hasn't made yet". The model was handed a stock phrase and used it,
    # which is exactly what it was asked to do. Reworded above.
    #
    # Removing the seed is necessary but not sufficient: any fixed instruction
    # eventually grows its own tic. So the measured overuse is fed back in as a
    # ban list, computed from what was really published rather than guessed.
    # See routes/media_post_quality.overused_openers().
    #
    # ★ STEER, NEVER BLOCK. This is deliberately a prompt input and not a
    # publish gate — blocking 87% of posts to fix a stylistic habit would
    # silence the feed, and silence is the failure this program spent August
    # digging out of.
    try:
        from routes.media_editorial import _recent_post_texts
        from routes.media_post_quality import ban_list_block
        _ban = ban_list_block(_recent_post_texts(limit=30, days=21))
        if _ban:
            user_prompt = user_prompt + "\n" + _ban
    except Exception:
        pass   # fail-open: a style hint must never block composition

    # ★★★ 2026-08-25 — CLOSE THE LOOP THAT WAS ADVERTISED AND ABSENT.
    # /api/v1/media/self-critique has returned a field named
    # `lessons_fed_to_generator` since r66, and its docstring promises "the
    # exact lessons now fed back into the generator's prompt". Nothing read it.
    # Measured: 103 blocked drafts produced ZERO input to the composer, whose
    # references to lesson/critique/blocked/rejected numbered zero.
    #
    # That is the difference between a pipeline with bouncers and an analyst:
    # every quality fix in this codebase's history has been a FILTER added
    # downstream, never a SIGNAL sent upstream. This is the signal.
    #
    # ★ Fail-open, like the ban list. Guidance to a writer must never be able
    #   to silence a slot — that is the failure August was spent undoing.
    try:
        from routes.media_post_quality import lessons_prompt_block
        _lessons = lessons_prompt_block(_recent_block_reasons())
        if _lessons:
            user_prompt = user_prompt + "\n" + _lessons
    except Exception:
        pass

    # ★★★ 2026-08-25 — THE THIRD LOOP. The block above teaches the composer
    # from drafts that were REFUSED. Nothing taught it from posts that were
    # PUBLISHED: the desk had no opinion whatsoever about the quality of its
    # own output once it was out the door.
    #
    # The claim ledger does pre-register every auto-published post, but it
    # grades IMPRESSIONS against floor(0.5 x 30d avg) ~= 17 while the worst
    # kind averages 18.3 — a bar all nine kinds clear on their average — and
    # its outcome is recalled by brain_rag for the BRAIN, never by this
    # composer. Measured 2026-08-25. See routes/media_published_review.
    #
    # ★ Fail-open and advisory, exactly like the block above. The review runs
    #   AFTER publication, so nothing here can suppress a slot.
    try:
        from routes.media_post_quality import published_critique_block
        from routes.media_published_review import recent_published_critiques
        _critiques = published_critique_block(recent_published_critiques())
        if _critiques:
            user_prompt = user_prompt + "\n" + _critiques
    except Exception:
        pass

    def _call(model: str) -> str | None:
        body = json.dumps({
            # 2026-07-15: 1200 clipped rich analyst posts mid-word (the "…15,000+
            # facil" cut the operator flagged) once the body + source footer ran
            # long. 1800 gives an analytical LinkedIn post + citation room to
            # finish; thinking stays OFF so all of it is output, not reasoning.
            "model": model,
            "max_tokens": 1800,
            "system": cached_system(_VOICE_SYSTEM),
            "messages": [{"role": "user", "content": user_prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": ANTHROPIC_API_KEY,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = json.loads(r.read().decode("utf-8"))
        text_parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in text_parts if isinstance(p, dict))
        text = text.strip()
        # Strip any wrapper quotes Claude sometimes adds
        if text.startswith('"') and text.endswith('"') and len(text) > 10:
            text = text[1:-1].strip()

        # ★★★ 2026-08-25: stop_reason was NEVER READ — 0 occurrences in this
        # file before this commit. The API tells us, on every single call,
        # whether the model FINISHED or was CUT OFF at the ceiling, and we
        # discarded that and shipped the severed text downstream.
        #
        # That is why truncation keeps coming back. It has been fixed three
        # times AT THE GATE and never at the source: 2026-07-15 raised
        # max_tokens 1200 -> 1800 after posts clipped mid-word, #3153 added a
        # broken-copy publish gate, #3162 stopped that gate eating headlines.
        # None of them asked the one question the response already answers.
        #
        # Measured live 2026-08-25T08:21:50Z: the deal/nvidia slot composed a
        # 1,328-char draft and TWO independent judges called it cut off — the
        # LLM editor ("Draft cuts off mid-sentence") and the broken-copy gate.
        # 1,328 chars is ~340 tokens against an 1,800 ceiling, so the cap is
        # almost certainly NOT the cause — but nothing recorded stop_reason,
        # so "almost certainly" is the best anyone could say. Now it is a fact
        # on every compose.
        _stop = payload.get("stop_reason")
        _usage = payload.get("usage") or {}
        _out_tok = _usage.get("output_tokens")
        if _stop == "max_tokens":
            # A ceiling hit is a KNOWN-severed draft. Refusing here is cheaper
            # than composing, gating, rejecting and re-electing the lead —
            # and it names the cause instead of leaving the gate to guess.
            logger.warning(
                "[composer] %s hit max_tokens (%s output tokens, %d chars) — "
                "refusing a known-truncated draft rather than shipping it to "
                "the gate", model, _out_tok, len(text))
            _record_compose_stop(model, _stop, _out_tok, len(text))
            return None
        _record_compose_stop(model, _stop, _out_tok, len(text))
        return text or None

    try:
        return _call(_MEDIA_MODEL)
    except Exception:
        try:
            return _call(_MEDIA_MODEL_FALLBACK)
        except Exception:
            return None


def _canon_media_phrases() -> tuple[str, str]:
    """(facilities, deals) as citation-safe canonical phrases — e.g.
    ("18,600+", "1,900+"). ★THE ONE PLACE this module may source either count.

    facilities = ``canonical_stats.facilities_verified_phrase()`` — distinct
    BUILDINGS (COUNT(DISTINCT canonical_slug) WHERE COALESCE(is_duplicate,0)=0),
    floored DOWN. That is byte-for-byte the ceiling
    ``media_fact_check_guard.check_facility_count_claims`` measures published
    copy against, so the composer and the gate agree by construction instead of
    by coincidence. NEVER ``canonical_stats["facilities"]``: that is COUNT(*) =
    raw source ROWS, ~1.4x buildings, and publishing it as facilities is the
    rows_ne_buildings refusal (#3111).

    deals = ``canonical_stats.deals_phrase()`` — DEDUPED distinct deals, floored
    DOWN. NEVER a literal: "4,000+" floored duplicate ROWS (the AUTO deal id
    embeds the ingest date, so one deal accrues a row per day) and sits on
    ai_surface_canon's ``stale_markers`` list against a live ~1,900.

    Fail-open to ("", ""), never to a frozen literal — callers drop the number
    and ship a count-free sentence. A missing count is visible; a stale one
    publishes.
    """
    try:
        from canonical_stats import deals_phrase, facilities_verified_phrase
        return (facilities_verified_phrase() or "", deals_phrase() or "")
    except Exception:
        return "", ""


def _canon_markets_phrase() -> str:
    """Canon-bound DCPI market count, or "" when unresolvable.

    _build_user_prompt already derives this for the LLM path (a hardcoded
    "300+ markets" once tripped the editor's internal-consistency check against
    canon). The static fallback did not, so the two paths could publish
    different counts for the same product.
    """
    try:
        from canonical_stats import markets_phrase
        return markets_phrase() or ""
    except Exception:
        return ""


def _build_user_prompt(story_type: str, data: dict, landing: str) -> str:
    """Per-story-type user prompt with the real data."""
    # r-qa (2026-06-27): pull the standing totals from canonical_stats so the
    # prompt's market/facility counts match what the editor-review gate trusts
    # (both read canonical_stats). A hardcoded "300+ markets" tripped the
    # editor's internal-consistency check against the canonical 311.
    #
    # ★2026-08-23 — `facilities` is COUNT(*) FROM discovered_facilities: raw
    # source ROWS, ~1.4x the building count (the March 2026 backfill wrote
    # several rows per site). This prompt hands its figures to the model under
    # "use these EXACT figures, do not invent others", so the row pile went out
    # verbatim as a building count — "26,334+ facilities" (2026-08-20 h8),
    # "26,327+" (08-19 h12), "26,136+" (08-17 h12), all live on LinkedIn.
    # routes/claim_breaker.py's rows_ne_buildings class was armed 2026-08-21,
    # AFTER those posts: it WILL refuse this copy from now on, exactly as it
    # refused the 16:00 capability slot two days running (#3111).
    #
    # The citeable figure is `facilities_verified` = COUNT(DISTINCT
    # canonical_slug) WHERE COALESCE(is_duplicate,0)=0 AND canonical_slug IS NOT
    # NULL — and that is precisely the ceiling
    # media_fact_check_guard.check_facility_count_claims measures this copy
    # against. Reading it through facilities_verified_phrase() keeps composer and
    # gate on ONE number by construction, and the phrase floors DOWN, so the
    # anchor can never sit above the ceiling even mid-ingest.
    #
    # Deals were worse: a hardcoded "4,000+ tracked deals" against a live 1,932
    # distinct — a >2x over-claim, and a value ai_surface_canon already lists in
    # stale_markers. `deals` rows over-state ~2.9x (the AUTO id embeds the ingest
    # date, so one deal accrues a row per day); deals_phrase() floors the DEDUPED
    # count and is the same string /api/v1/canon/phrases publishes.
    try:
        from canonical_stats import get_canonical_stats as _gcs
        _c = _gcs() or {}
    except Exception:
        _c = {}
    _t_fac, _t_deals = _canon_media_phrases()
    _t_mkt = f"{int(_c.get('markets', 300))}+"
    # Fail-open to a COUNT-FREE anchor. An unreadable canon must never degrade to
    # a frozen literal — a literal in the fallback slot is exactly how "4,000+"
    # survived here for months while every other surface was rebased.
    if _t_fac and _t_deals:
        _t_anchor = (f"{_t_fac} facilities, {_t_mkt} markets, "
                     f"{_t_deals} tracked deals — updated daily")
    else:
        _t_anchor = "the live index — updated daily"
    if story_type == "capability_spotlight":
        tool = data.get("tool") or {}
        sample = data.get("sample_markets") or data.get("sample_facility") or {}
        return f"""Compose a LinkedIn post that spotlights ONE DC Hub MCP
capability. Show what an AI agent can do with it that the agent
couldn't do anywhere else.

TOOL: {tool.get('tool','?')}
EXAMPLE QUESTION IT ANSWERS: {tool.get('ask','?')}
LIVE DATA: {json.dumps(sample, default=str)[:600]}

Open with: "Ask any AI:" and the example question. Then describe
what DC Hub returns — concretely, but ONLY using the LIVE DATA above.
HONESTY (non-negotiable): do NOT invent specific statutes, program
names, tax rates, percentages, dollar figures, or dates that are not
present in LIVE DATA. If you lack specifics, name the CATEGORIES the
tool returns (e.g. "sales-tax exemptions, property-tax abatements,
clawback terms") without fabricating the values. Mention the tool
name. End with a CTA pointing at {landing} and 3-4 hashtags."""

    if story_type == "energy_narrative":
        s = data.get("story_data") or {}
        return f"""Compose a LinkedIn post that tells the STORY of an energy
event from real US power-market data. Make it human — what happened,
why it matters for AI infrastructure decisions.

REAL DATA: {json.dumps(s, default=str)[:600]}

Open with a vivid scene-setter. Explain the data. End with what this
means for someone choosing a data-center site. CTA: {landing}.
3-4 hashtags including #DCPI and one energy-themed tag."""

    if story_type == "dcpi_scoop":
        scoop = data.get("scoop") or {}
        return f"""Compose a LinkedIn post that surfaces a MARKET NOBODY IS
TALKING ABOUT. DC Hub Power Index found it; the press hasn't.

THE MARKET (real data): {json.dumps(scoop, default=str)[:600]}

Open with the contrarian hook (the market name + a one-line
contrast vs the names everyone knows). Show why DCPI flags it.
Why does this matter for AI capacity decisions in 2026?
CTA: {landing}. Hashtags include #DCPI."""

    if story_type == "shipped_this_week":
        adds = (data.get("stats") or {}).get("added_this_week") or {}
        return f"""Compose a LinkedIn post about what DC Hub ADDED to its live
infrastructure index in the last 7 days — an expansion update from a position of
strength. Real adds this week (category: counts):

{json.dumps(adds, default=str)[:400]}

RULES:
- OPEN with the single biggest concrete add as a number in the first sentence
  (e.g. "DC Hub added 133 data-center deals and 19 facilities to its live index
  this week" — pick the largest real numbers from the data above). This is the
  hook; it must land in the first ~12 words, before LinkedIn's "...more" fold.
- The SO-WHAT: an agent-native layer that updates DAILY vs analyst PDFs that
  refresh quarterly — a new interconnect filing or closed deal is queryable in
  hours, not next quarter.
- Anchor on the standing totals (use these EXACT figures, do not invent others):
  {_t_anchor}.
- Confident, factual, no hype words. If the adds data is empty, lead with the
  standing totals + "updated daily" instead.
End with the value line + CTA: {landing}. Hashtags: #DataCenter #AIInfrastructure #DCPI."""

    if story_type == "hyperscaler_drama":
        news = data.get("news") or {}
        mkt = data.get("market") or {}
        return f"""Compose a LinkedIn post that pairs a real hyperscaler
news headline with DCPI's contrarian take.

NEWS HEADLINE: {(news.get('title') or '')[:200]}
NEWS URL: {news.get('url','')}
DCPI MARKET (real data): {json.dumps(mkt, default=str)[:300]}

Open with the news angle. Then pivot to where DC Hub Power Index
shows the actual build is happening. End with both URLs:
news in body, {landing} in CTA. Hashtags include #DCPI."""

    if story_type == "agent_demand":
        d = data.get("demand") or {}
        if not d:
            return ""
        return f"""Compose a LinkedIn post about what AI AGENTS are actually
asking the live infrastructure layer for — DC Hub's own aggregate demand
telemetry, an angle no analyst PDF can publish.

REAL AGGREGATE DATA (the ONLY numbers you may use):
- Distinct AI agents that queried DC Hub in the last 7 days: {d.get('agents_7d')}
- Weekly FIRST-TIME agents: up from {d.get('new_ips_prior')} to {d.get('new_ips_latest')} (complete weeks, {d.get('weeks_apart')} week(s) apart)
- Most-requested intelligence (DISTINCT callers per tool, 30 days):
{json.dumps(d.get('top_tools') or [], default=str)[:400]}

RULES:
- OPEN with the distinct-agent count and how it MOVED ("up from X to Y") in the
  first sentence — number first, before LinkedIn's "...more" fold.
- The SO-WHAT: agent demand is the forward book of AI-infrastructure questions.
  What agents request most (e.g. interconnection headroom) is where siting and
  capex decisions are being made right now — before the press release.
- PRIVACY (non-negotiable): aggregates only. NEVER name or imply a specific
  customer, company, platform or query behind these counts; NEVER invent tool
  parameters, sites, or markets from this data. Only the counts above.
- HONESTY: do not invent any figure not present above. Distinct callers, not
  request volume — say so if you cite the tool counts.
End with the source line + CTA: {landing}. Hashtags: #AIInfrastructure #DataCenter #MCP."""

    if story_type == "capability_update":
        # r-capability-slot (rebuild): compose ONLY from the editorial cap_* lead's
        # own figures (built into data['lead'] by compose_story_post — no puller ran,
        # so there is no unrelated pulled data to leak in). The lead's number+trend+
        # so-what is ALSO prepended by _compose_with_claude via lead_prompt_block, so
        # the post already opens with it; this block frames the honesty fence.
        ld = data.get("lead") or {}
        return f"""Compose a LinkedIn post announcing a DC Hub PLATFORM CAPABILITY /
data milestone — a "what we shipped" update from a position of strength.

THE CAPABILITY (the ONLY numbers you may use):
- Headline: {ld.get('headline_number','')}
- Detail:   {ld.get('trend','')}
- So-what:  {ld.get('so_what','')}

RULES:
- OPEN with the headline number in the FIRST sentence, before LinkedIn's
  "...more" fold (~12 words). No brand-pillar preamble.
- HONESTY (non-negotiable): cite ONLY the figures above. Do NOT invent or pull in
  any OTHER market, facility, deal, ISO, or metric number — this post is about this
  one capability, nothing else. If you need context, name the CATEGORY, never a
  fabricated value.
- Say plainly WHAT shipped / what the milestone is and why it matters to teams
  making AI-infrastructure siting and capex decisions — concrete, dry, no hype.
End with the source line + CTA: {landing}. Hashtags: #DataCenter #AIInfrastructure #DCPI."""

    if story_type == "operator_spotlight":
        sp = data.get("spotlight") or {}
        if not sp:
            return ""
        return f"""Compose a LinkedIn post about ONE data-center OPERATOR and what
DC Hub's tracked records show they have built. This is the first story type on
this desk that is about an operator rather than about DC Hub or about a market.

THE OPERATOR (the ONLY numbers you may use):
- Headline: {sp.get('headline','')}
- Operator: {sp.get('operator','')}
- Angle:    {sp.get('angle','')}

RULES:
- OPEN with the headline EXACTLY as given, in the first sentence, before
  LinkedIn's "...more" fold. It is already number-led and already passes the
  desk's number-lead gate; rewriting the opening breaks both.
- HONESTY (non-negotiable): cite ONLY the figures above. Do NOT add a market
  ranking, a capacity estimate, a pricing figure or a comparison to another
  operator. Every number here is computed over the operator's CANONICAL name
  group; a figure you invent will be checked by the operator themselves.
- POSITIVE-ONLY (operator directive 2026-07-02): this reports what our records
  show. No verdicts, no rankings-against, no downgrades, no "they are behind",
  no advice to the operator. If the post cannot be written without an opinion,
  return SKIP.
- Say plainly what DC Hub tracks for them and why an independent, machine-
  readable record of the fleet is useful to the people financing and siting it.
- Never claim this is the operator's COMPLETE estate: it is what DC Hub tracks.
End with the source line + CTA: {landing}. Hashtags: #DataCenter #AIInfrastructure."""

    if story_type == "market_anomaly":
        a = data.get("anomaly") or {}
        # r-us-market-count (2026-09-04): a hard-typed US market count here
        # went straight into an LLM prompt, so the model repeated it as fact in
        # a published post. Count-free when canon is unreadable — an LLM given
        # no number cannot quote a wrong one.
        try:
            from canonical_stats import markets_phrase as _mp
            _mk = _mp() or ""
        except Exception:
            _mk = ""
        _scope = f"the {_mk} markets DC Hub scores" if _mk else "the markets DC Hub scores"
        return f"""Compose a LinkedIn post about a DCPI anomaly: the
biggest week-over-week score change among {_scope}.

REAL DATA: {json.dumps(a, default=str)[:400]}

Open with the market name + the delta. Explain what could cause
a swing that big (new gen additions, curtailment shifts, queue
movement, demand growth). End with what AI infra teams should
do with this signal. CTA: {landing}. Hashtags include #DCPI."""

    return ""


# ── Static fallbacks (when Claude is unavailable) ─────────────────

def _static_fallback(story_type: str, data: dict, landing: str) -> str:
    """Story-type-aware static template. Used when Claude API fails.
    Each is meant to be more interesting than 'data sample' bullet
    lists but still ground-truth real."""
    _fac, _ = _canon_media_phrases()
    # "N facilities, " or nothing — never a frozen literal (this branch carried
    # a hard-typed facilities+markets pair until 2026-08-23).
    _fac_p = f"{_fac} facilities, " if _fac else ""
    # r-us-market-count (2026-09-04): the 2026-08-23 pass derived the FACILITIES
    # half of that pair and left the MARKETS half typed, so two literals below
    # kept publishing a frozen count on the fallback path — the path that runs
    # precisely when the LLM is unavailable and nobody is reviewing the copy.
    # Same fail-open direction: count-free, never frozen.
    _mkt = _canon_markets_phrase()
    _mkt_p = f"{_mkt} markets" if _mkt else "markets"
    if story_type == "capability_spotlight":
        tool = data.get("tool") or {}
        return (
            f"Ask any AI: \"{tool.get('ask','...')}\"\n\n"
            f"Without DC Hub, the model guesses. With DC Hub's MCP "
            f"{tool.get('tool','tool')}, it returns the real answer "
            f"in milliseconds — pulled from {_fac_p}DCPI-scored markets "
            f"and live ISO grid data.\n\n"
            f"This is what \"AI-ready infrastructure intelligence\" "
            f"means in practice.\n\n"
            f"Try it: {landing}\n\n"
            f"#DCHubMedia #MCP #AIInfrastructure #DataCenter"
        )
    if story_type == "energy_narrative":
        s = data.get("story_data") or {}
        first_value = next(iter(s.values()), None)
        return (
            f"Behind every hyperscale buildout: an energy story most "
            f"investors miss.\n\n"
            f"Latest signal from DCPI on {s.get('market_name','a US market')}: "
            f"{first_value}. That single number changes which markets are "
            f"buildable in the next 36 months — and which aren't.\n\n"
            f"This is why DC Hub scores 300+ power markets weekly "
            f"instead of relying on press releases.\n\n"
            f"Full methodology + live scores: {landing}\n\n"
            f"#DCPI #PowerGrid #DataCenter #DCHubMedia"
        )
    if story_type == "dcpi_scoop":
        scoop = data.get("scoop") or {}
        return (
            f"Quietly, {scoop.get('market_name','this market')} is becoming "
            f"a top-10 BUILD candidate.\n\n"
            f"DCPI Excess Power score: {scoop.get('excess_power_score','?')}. "
            f"Constraint: {scoop.get('constraint_score','?')}. Time-to-power: "
            f"{scoop.get('time_to_power_months','?')} months.\n\n"
            f"You won't read this on the front page — that's exactly why "
            f"DC Hub built the Power Index. {_mkt_p}, weekly, "
            f"data-driven.\n\n"
            f"See the full list: {landing}\n\n"
            f"#DCPI #DataCenter #AIInfrastructure"
        )
    if story_type == "shipped_this_week":
        s = data.get("stats") or {}
        return (
            f"DC Hub shipped this week:\n\n"
            f"• {s.get('press_releases',0)} press releases\n"
            f"• {s.get('mcp_tool_calls',0):,} MCP tool calls served\n"
            f"• {s.get('brain_proposals',0)} brain capability proposals\n"
            f"• {s.get('facilities_discovered',0)} new facilities tracked\n\n"
            f"Built in public. Tracked in public. Audited in public.\n\n"
            f"See it live: {landing}\n\n"
            f"#DCHubMedia #BuildInPublic #DataCenter"
        )
    if story_type == "hyperscaler_drama":
        news = data.get("news") or {}
        mkt = data.get("market") or {}
        return (
            f"📰 {(news.get('title') or 'Latest hyperscale move')[:140]}\n\n"
            f"Press attention is on the announced site. DCPI flags where "
            f"the actual buildable capacity sits: {mkt.get('market_name','?')} "
            f"(Excess Power {mkt.get('excess_power_score','?')}, verdict "
            f"{mkt.get('verdict','?')}).\n\n"
            f"The announcement lags the build by 18-24 months.\n\n"
            f"Where AI infra is really landing: {landing}\n\n"
            f"Source: {news.get('url', landing)}\n\n"
            f"#DCPI #Hyperscaler #DataCenter #DCHubMedia"
        )
    if story_type == "market_anomaly":
        a = data.get("anomaly") or {}
        delta = a.get("delta", 0)
        sign = "+" if (delta or 0) > 0 else ""
        return (
            # r-us-market-count (2026-09-04): the DEFAULT here was "a US
            # market". A missing name is exactly when the scope is unknown, so
            # the fallback asserted the one thing it could not know — and the
            # index is global, so it is wrong for every non-US mover.
            f"DCPI anomaly of the week: {a.get('market_name','a scored market')} "
            f"moved {sign}{delta} on Excess Power score.\n\n"
            f"Current: {a.get('now_e','?')} ({a.get('now_v','?')}). "
            f"7d ago: {a.get('prev_e','?')}.\n\n"
            f"Movements this large signal real underlying change — "
            f"new gen additions, queue movement, or demand shifts. AI "
            f"infra teams should investigate.\n\n"
            f"All {_mkt_p}: {landing}\n\n"
            f"#DCPI #DataCenter #AIInfrastructure"
        )
    return f"DC Hub Media · See {landing}\n\n#DCHub #DataCenter"


# ── Public API ────────────────────────────────────────────────────

# ── Premium dynamic card builder ──────────────────────────────────
# 2026-06-06: every post gets a RICH headline card (same engine as auto-press)
# instead of the frozen-blank static landing-*.png files. Builds a
# /api/v1/og/dynamic.png URL from the story's real headline + key stat.
_OG_DYNAMIC_BASE = (os.environ.get("DCHUB_OG_BASE", "https://api.dchub.cloud").rstrip("/")
                    + "/api/v1/og/dynamic.png")


def _clean(s, n: int) -> str:
    if not s:
        return ""
    try:
        s = _html.unescape(str(s))
    except Exception:
        s = str(s)
    return " ".join(s.split())[:n]


def _f1(v) -> str:
    try:
        return f"{float(v):.1f}"
    except Exception:
        return ""


def _card_url_for(story_type: str, data: dict, text: str) -> str | None:
    """Build a premium dynamic-card URL from the story's real headline + stat.
    Returns None on any problem so the caller keeps the static fallback."""
    try:
        d = data or {}
        title = sub = market = score = verdict = ""
        # 2026-07-03: photographic ai_hero is the DEFAULT card. _draw_ai_hero
        # now ALWAYS resolves a real background photo (curated library floor +
        # SDXL premium), so the good editorial look no longer depends on CF
        # creds or the removed DCHUB_MEDIA_AI_IMAGES gate. data_brutal (the
        # DCPI score gauge) is reserved for pure-number stories where the score
        # itself is the headline.
        style = "ai_hero"

        constraint = ""
        if story_type == "dcpi_scoop":
            s = d.get("scoop") or {}
            market = _clean(s.get("market_name"), 40)
            score = _f1(s.get("excess_power_score"))
            constraint = _f1(s.get("constraint_score"))
            verdict = (s.get("verdict") or "BUILD")
            title = market
            ttp = s.get("time_to_power_months")
            if score:
                sub = f"Excess power {score} · {verdict}" + (f" · power in {int(ttp)} mo" if ttp else "")
            # Pure DCPI score story → the gauge card IS the story.
            style = "data_brutal" if score else "ai_hero"

        elif story_type == "market_anomaly":
            a = d.get("anomaly") or {}
            market = _clean(a.get("market_name"), 40)
            score = _f1(a.get("now_e"))
            verdict = (a.get("now_v") or "BUILD")
            title = market
            dl = a.get("delta")
            if dl is not None and score:
                sub = f"{'+' if float(dl) >= 0 else ''}{_f1(dl)} pt WoW DCPI shift · now {score} · {verdict}"
            elif score:
                sub = f"DCPI {score} · {verdict}"
            # Pure DCPI mover → gauge card; otherwise photographic hero.
            style = "data_brutal" if score else "ai_hero"

        elif story_type == "energy_narrative":
            s = d.get("story_data") or {}
            market = _clean(s.get("market_name"), 40)
            score = _f1(s.get("excess_power_score") or s.get("constraint_score"))
            title = market or "The grid story behind the build-out"
            if s.get("curtailment_pct"):
                sub = f"{_f1(s['curtailment_pct'])}% curtailment — power the grid can't place"
            elif s.get("gen_additions_12mo_mw"):
                sub = f"{int(s['gen_additions_12mo_mw']):,} MW added in 12 months"
            elif s.get("queue_wait_months"):
                sub = f"{int(s['queue_wait_months'])}-month interconnection queue"
            else:
                sub = "Live ISO grid intelligence on DC Hub"
            # Narrative → photographic hero (grid/renewable/transmission photo).
            style = "ai_hero"

        elif story_type == "hyperscaler_drama":
            news = d.get("news") or {}
            mkt = d.get("market") or {}
            title = _clean(news.get("title"), 120) or "The hyperscale build-out, decoded"
            if mkt:
                sub = (f"DCPI's take: {_clean(mkt.get('market_name'), 30)} is "
                       f"{mkt.get('verdict', 'BUILD')} at {_f1(mkt.get('excess_power_score'))}")
            else:
                sub = "Live, agent-native data-center intelligence"
            style = "ai_hero"

        elif story_type == "capability_spotlight":
            tool = d.get("tool") or {}
            ask = _clean(tool.get("ask"), 110) or "rank every US market by excess power for AI"
            title = f"Ask any AI to {ask}"
            sub = f"Live via DC Hub MCP · {tool.get('tool', '')} · 38 agent-native tools"
            style = "ai_hero"

        elif story_type == "agent_demand":
            dm = d.get("demand") or {}
            _a = dm.get("agents_7d")
            title = (f"{_a} AI agents queried DC Hub this week" if _a
                     else "What AI agents asked for this week")
            _tt = (dm.get("top_tools") or [{}])[0]
            if dm.get("new_ips_prior") and dm.get("new_ips_latest"):
                sub = (f"First-time agents up {dm['new_ips_prior']} → "
                       f"{dm['new_ips_latest']}/wk")
                if _tt.get("label"):
                    sub += f" · top ask: {_tt['label']}"
            else:
                sub = "Live agent-demand telemetry · aggregate counts only"
            style = "ai_hero"

        elif story_type == "operator_spotlight":
            sp = d.get("spotlight") or {}
            title = _clean(sp.get("operator"), 40) or "Operator spotlight"
            _n = sp.get("fleet_n")
            _mw = sp.get("fleet_mw") or 0
            # ★ Unknown capacity is NOT zero — most tracked buildings carry no
            # power_mw, so "0 MW" would be a false statement about a real
            # company. Same rule the headline builder applies.
            sub = (f"{_n:,} facilities tracked" if _n else "Tracked by DC Hub")
            if _mw and float(_mw) > 0:
                sub += f" · {float(_mw):,.0f} MW"
            style = "ai_hero"

        elif story_type == "shipped_this_week":
            st = d.get("stats") or {}
            title = "What DC Hub shipped this week"
            parts = []
            if st.get("press_releases"):
                parts.append(f"{st['press_releases']} press releases")
            if st.get("mcp_tool_calls"):
                parts.append(f"{int(st['mcp_tool_calls']):,} MCP calls")
            if st.get("facilities_discovered"):
                parts.append(f"{st['facilities_discovered']} new facilities")
            sub = " · ".join(parts) or "Compounding live intelligence, every day"
            style = "ai_hero"

        # 2026-07-16: MARKET stories → the branded DCPI market scorecard
        # (data_card kind=market) — big verdict pill (the market's REAL verdict,
        # so it can't contradict the text), Excess-Power / Grid-Constraint gauges,
        # time-to-power. Replaces the sparse data_brutal / generic ai_hero for
        # dcpi_scoop + market_anomaly. Returns early with the card URL.
        if story_type in ("dcpi_scoop", "market_anomaly") and market and score:
            _src = d.get("scoop") or d.get("anomaly") or {}
            _mp = {"style": "data_card", "kind": "market", "market": market,
                   "verdict": (verdict or "BUILD"), "excess": score,
                   "constraint": (constraint or "0")}
            if sub:
                _mp["descriptor"] = sub[:200]
            _iso = _clean(_src.get("iso") or _src.get("iso_region"), 12)
            if _iso:
                _mp["iso"] = _iso
            _ttp = _src.get("time_to_power_months")
            if _ttp not in (None, ""):
                try:
                    _mp["ttp"] = int(float(_ttp))
                except (TypeError, ValueError):
                    pass
            _slug = _src.get("market_slug") or _src.get("slug")
            if _slug:
                _mp["footer_tag"] = f"dchub.cloud/dcpi/{_slug}"[:60]
            return _OG_DYNAMIC_BASE + "?" + urllib.parse.urlencode(_mp)

        # Generic fallback — pull the strongest line from the composed text
        if not title:
            for ln in (text or "").splitlines():
                ln = ln.strip().lstrip("📊📰🔌🌊⚡🟢🟣✅•- ").strip()
                if len(ln) > 14:
                    title = ln[:120]
                    break
        if not title:
            return None
        if not sub:
            sub = "Live, agent-native data-center intelligence · dchub.cloud"

        # 2026-07-03: the DCHUB_MEDIA_AI_IMAGES env gate is GONE. It used to be
        # the only thing that ever selected ai_hero (and only for 3 story types,
        # and only when set) — which is why the fleet looked like flat slabs and
        # exactly one card looked great. ai_hero is now the default above and
        # always resolves a real photo (curated library floor + SDXL premium),
        # so no gate is needed.

        # 2026-06-07: A/B style learner. Default OFF (observe first). When
        # DCHUB_STYLE_AB_LEARNER_ENABLED=1 the learner picks a style per
        # topic via epsilon-greedy and records the decision; otherwise the
        # hardcoded `style` above wins. Mapped from story_type → topic
        # because the topic_tuner library is the unit we learn on.
        try:
            from routes.media_style_ab import pick_style_if_enabled
            _topic = _STORY_TYPE_TO_TOPIC.get(story_type, "other")
            style = pick_style_if_enabled(_topic, fallback=style)
        except Exception:
            pass

        params = {"style": style, "title": title, "subheadline": sub}
        if market:
            params["market"] = market
        if score:
            params["score"] = score
        if verdict:
            params["verdict"] = verdict
        if constraint:
            params["constraint"] = constraint
        return _OG_DYNAMIC_BASE + "?" + urllib.parse.urlencode(params)
    except Exception:
        return None


def _data_card_url(card: dict) -> str | None:
    """Build a branded DATA-CARD url (style=data_card) from an editorial lead's
    `card` spec — {kind, nums:{d,v,t,m,dl,c,tl}}. og_cards._draw_data_card owns the
    per-kind layout + copy; the lead's LIVE canonical numbers ride along as params
    so the card always shows real, current figures. Returns None on any problem so
    the caller keeps the story-type card."""
    try:
        if not card or not card.get("kind"):
            return None
        params = {"style": "data_card", "kind": str(card["kind"])[:48]}
        for k, val in (card.get("nums") or {}).items():
            # ★2026-08-23 — `d` (distinct BUILDINGS) is the citeable facility
            # count and the only number a card slot labelled "facilities" may
            # render. It is THIRD in a chain that must all agree: the radar puts
            # it in card["nums"], this allowlist forwards it, og_cards parses it.
            # Dropped here it does not error — _dc_nums falls back to its frozen
            # default and the card publishes a stale figure that looks fine.
            if k in ("d", "v", "t", "m", "dl", "c", "tl") and val not in (None, ""):
                params[k] = int(val)
        return _OG_DYNAMIC_BASE + "?" + urllib.parse.urlencode(params)
    except Exception:
        return None


def compose_story_post(slot_topic: str | None = None, lead: dict | None = None) -> dict:
    """Compose a story-driven LinkedIn post.

    r86c: `lead` is the brain editorial desk's chosen data event
    (number+trend+so-what); when provided, the post opens with that metric.

    Returns dict with:
      story_type, text, landing_url, og_image_url, source ('claude' or 'fallback')
    """
    story_type = _pick_story_type(slot_topic)
    # r-agent-demand (2026-07-17): when the editorial desk chose the
    # agent-demand LEAD, compose from the agent-demand material — any other
    # story type would open with the lead's numbers and then write about
    # unrelated data (the half-wired failure mode).
    if isinstance(lead, dict) and lead.get("kind") == "agent_demand":
        story_type = "agent_demand"
    # r-capability-slot (rebuild 2026-07-18): when the desk chose a CAPABILITY lead
    # (the reserved 16:00 slot restricts to these), force the capability_update
    # story type and build `data` FROM the lead — skip the puller entirely. The
    # prompt is honesty-fenced to use ONLY the lead's own figures, so the post can
    # never open with the capability number and then wander into unrelated pulled
    # data (the same half-wired failure the agent_demand fix closed). Mirrors the
    # agent_demand force-type pattern above.
    _is_capability_lead = False
    if isinstance(lead, dict):
        _lk = lead.get("kind") or ""
        if _lk.startswith("cap_") or _lk in ("capability_launch", "data_milestone"):
            story_type = "capability_update"
            _is_capability_lead = True

    if _is_capability_lead:
        data = {"type": "capability_update", "lead": lead}
    else:
        pull = _PULLERS.get(story_type, _PULLERS["capability_spotlight"])
        data = pull()
    landing = LANDING_BY_TYPE[story_type]
    og_url = OG_IMAGE_BY_TYPE[story_type]
    # Landing override: point at the capability lead's OWN surface (each cap_* lead
    # carries a source_url for the feature it announces) rather than the generic
    # /whats-new default.
    if _is_capability_lead:
        _su = (lead.get("source_url") or "").strip()
        if _su:
            landing = _su

    # r-agent-demand: demand data unavailable (endpoint gap, thin counts, or
    # no upward movement) → SKIP honestly. Never a fallback template — all
    # _static_fallback paths are dead by design.
    # r-operator: no operator cleared the bar today -> SKIP the slot rather than
    # compose a generic profile. linkedin_quad_daily suppresses on skip and never
    # falls through to static filler.
    if story_type == "operator_spotlight" and not (data or {}).get("spotlight"):
        return {"story_type": story_type, "skip": True,
                "reason": "no operator cleared the spotlight bar today"}
    if story_type == "agent_demand" and not (data or {}).get("demand"):
        return {
            "story_type":   story_type,
            "text":         None,
            "skip":         True,
            "skip_reason":  ("agent_demand data unavailable "
                            "(funnel/reach/retention gap or no upward trend)"),
            "landing_url":  landing,
            "og_image_url": og_url,
            "source":       "skip_no_data",
        }

    text = _compose_with_claude(story_type, data, landing, lead=lead)
    source = "claude"
    # SKIP escape hatch (2026-07-02): the composer saw the recently-published
    # feed and judged this data adds nothing new. Suppress the slot — do NOT
    # fall through to the static template, which is exactly the repetitive
    # filler the operator flagged.
    if text and text.strip().upper().rstrip(".") == "SKIP":
        return {
            "story_type": story_type,
            "text": None,
            "skip": True,
            "skip_reason": "composer judged nothing new vs recent feed",
            "landing_url": landing,
            "og_image_url": OG_IMAGE_BY_TYPE[story_type],
            "source": "claude_skip",
        }
    if not text or len(text) < 200:
        # 2026-07-15 (operator: 'the posts are terrible'): silence beats a
        # template. When the analyst composer fails or thins, SKIP the slot
        # rather than fall to a formulaic static template ("Quietly, X is
        # becoming a top-10 BUILD candidate…") — the desk publishes real analysis
        # or nothing, never filler.
        return {
            "story_type":   story_type,
            "text":         None,
            "skip":         True,
            "skip_reason":  "composer unavailable/thin — refusing template fallback",
            "landing_url":  landing,
            "og_image_url": OG_IMAGE_BY_TYPE[story_type],
            "source":       "skip_no_fallback",
        }

    # Premium dynamic card from the story's real headline + stat (replaces the
    # frozen-blank static landing PNG). Falls back to the static map if the
    # builder returns nothing.
    _dyn = _card_url_for(story_type, data, text)
    if _dyn:
        og_url = _dyn

    # 2026-07-14: a capability / platform-update lead (editorial cap_* kinds)
    # carries a `card` spec — render the branded DATA CARD (big hero number +
    # kind-specific viz) instead of the generic story-type ai_hero card. This is
    # what replaces the "ugly gray" card the operator flagged for these posts.
    if lead and isinstance(lead, dict) and lead.get("card"):
        _dc = _data_card_url(lead["card"])
        if _dc:
            og_url = _dc

    return {
        "story_type":   story_type,
        "text":         text,
        "landing_url":  landing,
        "og_image_url": og_url,
        "source":       source,
        "data_used":    data,
    }


# ── Read surface (2026-08-25) ────────────────────────────────────────────────
# ★ The point of recording stop_reason is that someone can ASK. A stat that
#   only exists in a log the operator has to grep is a stat nobody checks —
#   this codebase has relearned that repeatedly (the 08-15 outage was invisible
#   for 3 days because the only honest signal was in Railway logs).
def composer_stops_snapshot() -> dict:
    """Last 50 compose outcomes, aggregated. Pure read, no auth, no secrets."""
    rows = list(_COMPOSE_STOPS)
    by: dict = {}
    for r in rows:
        by[str(r.get("stop_reason"))] = by.get(str(r.get("stop_reason")), 0) + 1
    truncated = by.get("max_tokens", 0)
    total = len(rows)
    return {
        "ok": True,
        "sampled": total,
        "by_stop_reason": by,
        "truncated_at_ceiling": truncated,
        "truncation_rate": (round(truncated / total, 3) if total else None),
        "note": ("stop_reason=max_tokens means the model was CUT OFF at the "
                 "ceiling and the draft is known-severed; the composer refuses "
                 "those rather than handing them to the publish gate. "
                 "An empty sample means no compose has run in this process yet "
                 "— that is not the same as a truncation rate of zero."),
        "recent": rows[-10:],
    }


def register_composer_stops(app):
    """Mount the read surface. Called from main.py alongside the other
    media registrations; safe to call twice."""
    from flask import Blueprint, jsonify
    bp = Blueprint("composer_stops", __name__)

    @bp.route("/api/v1/media/composer-stops", methods=["GET"])
    def _composer_stops():
        try:
            return jsonify(composer_stops_snapshot()), 200
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 200

    try:
        app.register_blueprint(bp)
    except Exception:
        pass   # already registered
