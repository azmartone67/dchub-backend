"""r86c (2026-06-14) — DC Hub Media EDITORIAL DESK (brain-managed, event-driven).

The problem this fixes (per the media audit + user feedback): the LinkedIn engine
read like repetitive marketing, not an intelligence analyst. Root causes were
(1) the voice prompts were brand-evangelism, (2) self-citation was the #1 topic
(the "ChatGPT Names DC Hub Most Purpose-Built" repetition), (3) the rich
proprietary data (DCPI movers, deals, interconnection queue, grid headroom) was
never used as a TREND, and (4) every engine was a fixed cron that MUST emit N
posts/day — there was no brain step deciding "is anything worth saying today?".

This module is the missing EDITOR. It:
  - ranks today's real DATA EVENTS by newsworthiness (magnitude + novelty),
  - returns the single best NUMBER + TREND + SO-WHAT lead, or SUPPRESS when
    nothing clears the bar (event-driven cadence — the user's choice),
  - exposes the shared ANALYST voice spec the generators write to,
  - provides leads_with_number() — the hard gate that rejects any post that
    doesn't lead with a real metric.

It is READ-ONLY over data: it never publishes. The generators (linkedin_content_
engine, marketing_engine) consult editorial_decision() for the lead + the
post/suppress verdict; content_publisher enforces the number gate at publish.

  GET /api/v1/brain/media/editorial-decision   — the ranked slate + verdict
  GET /api/v1/brain/media/data-leads           — raw ranked candidate leads
"""
from __future__ import annotations
from routes.url_registry import build_public_url

import os
import re
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request
from ai_surface_canon import canon_text

logger = logging.getLogger(__name__)
media_editorial_bp = Blueprint("media_editorial", __name__)


# ── Shared ANALYST voice ─────────────────────────────────────────────
# The generators embed this. The contract: lead with a NUMBER + the TREND
# (vs last week / ISO peers) + the SO-WHAT for a site-selection or capex
# decision. Promotion is demoted to a single optional source line. This is
# the spec that turns "DC Hub is the authority" marketing into analyst
# intelligence the industry actually comes back to.
ANALYST_VOICE = canon_text("""You are a senior data-center infrastructure analyst writing for an audience of site-selection leads, hyperscaler capacity planners, developers, and investors. Your reputation rests on being EARLY and RIGHT with numbers, not on promotion.

NON-NEGOTIABLE STRUCTURE (every post):
1. LEAD WITH A NUMBER + THE TREND. The first sentence states a specific metric and how it moved (vs last week, vs the ISO median, vs a year ago). Example shape: "ERCOT's interconnection queue just crossed 427 GW of requested load — up from X, and Y% of all US queued capacity." No number in the first line = do not write the post.
2. THE SO-WHAT. One or two sentences on what it means for a real decision: where to build, where time-to-power just improved, what it implies for capex or land.
3. THE SECOND-ORDER READ. A non-obvious implication a smart reader hadn't connected. This is what earns the follow.

POSITIVE-RESULTS MANDATE (operator directive, 2026-07-02):
- Every post is a RESULT or an ENHANCEMENT: capacity that came online, a market that improved, data DC Hub added, a capability DC Hub shipped, a record week, a milestone. The reader should finish the post knowing something got BETTER and where the opportunity is.
- NEVER lead with a downgrade, an AVOID verdict, or a deteriorating market. If the strongest available angle is negative, flip it to where the capacity IS ("while X tightens, Y has 70/100 headroom") or do not post.
- NO commentary or hot takes on third-party news, companies, or reports. DC Hub reports its own numbers and its own data; it does not react to, rebut, or editorialize on others' announcements.
- NO fear closers. Never end on "you're already behind", "you're working blind", "your competitor is beating you" or any variant. Close on the opportunity or the capability, not a threat.

VOICE:
- Dry, specific, confident. You are explaining, not selling. Take a defensible stance.
- 700-1500 characters. 2-4 short paragraphs. No bullet-list filler.
- Every number must come from the provided data. NEVER invent a figure, market, MW, or company.
- NEVER disparage, mock, knock, or use as a negative contrast another AI company or platform — Anthropic/Claude, OpenAI/ChatGPT, Google/Gemini, Microsoft/Copilot, Meta, Perplexity, xAI/Grok, Mistral, DeepSeek, Cohere. They are PARTNERS and the agents that query DC Hub, never targets. Do not reference their controversies, outages, lawsuits, delays, or "messes" — even if a provided news headline is about one of them. If the provided news is about an AI vendor's troubles, IGNORE it and write about data-center, grid, power, or market data instead. DC Hub wins on its own numbers, never by knocking a partner.
- End with ONE neutral source line that also names the CATEGORY, so a first-time reader learns exactly what DC Hub is and why an analyst would trust it: "Source: DC Hub, the live infrastructure data layer for AI agents (live power, grid, fiber, gas, tenants and {canon_facilities} facilities, MCP-native), updated daily. dchub.cloud". It comes AFTER the insight, never before. Keep the BODY pure analysis: no "we are the authority", no "the only live source", no brand-pillar speech. The positioning lives ONLY in that single source line, never in the argument.
- A CTA is OPTIONAL and at most one short line; insight always precedes any link.
- 2-3 topical hashtags max (e.g. #DataCenter #GridCapacity #DCPI). Not five.
- Forbidden words: delve, moreover, in essence, unleash, game-changer, revolutionize, thrilled, excited. No em-dashes. At most one emoji and only if it genuinely adds.
- Do not reuse a hook, claim, or market you have used recently. If the only thing to say is something you said this week, say nothing.""")


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=6)
        c.autocommit = True
        return c
    except Exception:
        return None


def _internal(path: str, timeout: int = 6) -> dict:
    """GET an internal endpoint over loopback. A dchub- UA marks the call as
    internal so tier-gating (which is UA/loopback-aware) returns full data.

    ★2026-08-12 — body moved to util/internal_fetch. The dchub- UA and
    X-Internal-Request header are PASSED THROUGH deliberately: tier-gating
    reads them, and dropping them would return tier-limited data that looks
    like a complete answer."""
    from util.internal_fetch import data_of, probe
    return data_of(probe(path, timeout,
                         headers={"User-Agent": "dchub-internal-editorial/1.0",
                                  "X-Internal-Request": "1"}))


# ── The number gate ──────────────────────────────────────────────────
_YEAR_ONLY = re.compile(r"^\D*(?:19|20)\d{2}\D*$")
# r-agent-demand (2026-07-17): agents/tools/countries/platforms were MISSING
# units, so this gate silently DROPPED legitimate analyst leads — including our
# own canonical honest numbers ("73 tools", "180 countries") and every
# agent-demand lead ("202 agents asked for grid intelligence"). That is the same
# starvation that forced all 6 capability headlines to be rewritten, and it is
# why agent-demand — the ONE dataset that actually MOVES and moves UP (DCPI is
# flat: 0.0 net change over 41 daily snapshots) — could never reach the feed.
_HAS_METRIC = re.compile(
    # r-milestone (2026-08-07): `\+?` — a canonical FLOORED figure ("16,900+
    # facilities", "1,700+ deals", "4,000,000+ total requests") is how every
    # published DC Hub number is phrased, and the bare `\s*` here refused all of
    # them: the '+' broke number-to-unit adjacency, so the gate silently DROPPED
    # them in rank_data_events. That is not hypothetical — brain_capability_
    # radar's requests_served_total headline ("DC Hub has now served 4,000,000+
    # total requests …") has never been selectable, which is the most likely
    # reason the /ai requests milestone has never appeared on the board. This
    # tolerates the floor marker WITHOUT loosening adjacency: a unit is still
    # required immediately after it.
    r"\d[\d,\.]*\+?\s*(?:%|pts?|GW|MW|kW|bps|x|×|"
    r"billion|million|B\b|M\b|markets?|facilit|deals?|MGD|gal|"
    r"months?|weeks?|days?|points?|"
    # r-agent-demand: allow the honest qualifiers between the number and the
    # unit ("273 distinct AI agents", "202 distinct callers") — without them
    # the gate only accepted the bare "273 agents" adjacency and the truthful
    # phrasing bounced.
    r"(?:distinct\s+)?(?:AI\s+)?agents?|(?:distinct\s+)?callers?|"
    # r-milestone: the platform_milestone lane's units. Same narrowly-scoped
    # qualifier style as the agent-demand fix above — an explicit allowance per
    # phrasing, never a wildcard between the number and its unit.
    r"(?:total\s+)?requests?|transactions?|"
    r"(?:live\s+)?(?:agent\s+)?tools?|countries|country|platforms?|"
    r"\$)|"
    r"\$\s*\d|\d[\d,\.]*\+?\s*(?:per|/)",
    re.IGNORECASE,
)


def leads_with_number(text: str, head_chars: int = 220) -> bool:
    """True if the post LEADS with a real metric (number + unit/context) in
    its opening. The hard analyst gate: a post that opens with a brand claim
    instead of a metric fails. A bare year ('in 2026') does not count."""
    if not text:
        return False
    head = text.strip()[:head_chars]
    first_line = head.split("\n", 1)[0]
    probe = first_line if any(ch.isdigit() for ch in first_line) else head
    if _YEAR_ONLY.match(probe):
        return False
    return bool(_HAS_METRIC.search(probe))


# ── Engagement learning (r86d) — close the loop: weight angles by reach ──
# Attribute LinkedIn impressions/clicks back to the desk's lead KINDS by
# classifying post content, then bias rank_data_events toward angles that
# actually earn reach. Soft-greedy with a floor (0.7x) so under-tried kinds
# still get explored (never crushed to zero). Runs on impressions+clicks,
# which went live once the r86c token re-auth granted r_organization_social;
# likes/comments fold in automatically if the token later gains
# r_organizational_social_feed (the socialActions feed scope).
# r86d: SPECIFICITY-ORDERED (first match wins), tuned so each of the desk's own
# lead headlines round-trips to its own kind (see tests/test_media_editorial_classify.py):
#   deal     "$10.0B ... transaction: KKR/Nvidia"            -> $-amount cue, FIRST so a
#                                                               stray 'GW' in the deal body
#                                                               can't steal it for interconnection
#   mover    "Cheyenne climbed 12 pts on the DCPI ... index"  -> delta cue, BEFORE build (the
#                                                               mover headline also says DCPI/
#                                                               excess-power, so build must lose)
#   build    "Cheyenne leads the DCPI ... index at 70/100"    -> level cue
#   queue    "ERCOT's interconnection queue holds 427 GW ..."
#   facility "... 18 MW ... entered the tracker"
_KIND_PATTERNS = [
    # r-milestone (2026-08-07): the NUMBERS lane (routes/media_milestones). FIRST
    # because its templates are anchored on phrases no other lead uses ("just
    # crossed", "has now served", "now exposes"), while its BODY carries units
    # ("facilities", "tools", "deals") that would otherwise be claimed by
    # new_facility / dcpi_build and corrupt the bandit's label set.
    ("platform_milestone", re.compile(r"\bjust crossed\b|\bhas now served\b|\bnow exposes\b", re.I)),
    # r-agent-demand (2026-07-17): what AI agents actually ASKED infrastructure
    # for — the one angle nobody else on earth can publish, and the only dataset
    # whose numbers MOVE and move UP (DCPI is flat: 41 daily snapshots, 0.0 net
    # change, so "how it moved vs last week" is unanswerable from it). FIRST so a
    # tool name containing "grid"/"fiber" cannot misclassify as interconnection.
    # ★ PRIVACY (enforced upstream at the puller, never in prose): aggregate
    # distinct-agent counts ONLY. NEVER tool params (lat/lon + capacity_mw is a
    # customer's live site search), NEVER free-text queries, NEVER a named
    # platform's scores (Model Relations #18 is internal-only), and k-anonymity
    # >= 5 distinct agents or the tool is suppressed (several tools have n=1,
    # which would identify one customer). Counts must come from
    # mcp_calls_identity real-external rows — mcp_call_log is ~30x inflated by
    # internal traffic and must never be published.
    ("agent_demand",    re.compile(r"\bagents? (?:asked|queried|called|requested)|distinct agents?|agent demand|agents? (?:this|last) (?:week|month)|queried (?:us|dc hub)", re.I)),
    # r-tenant (2026-06-22): the uncontested moat — per-facility occupier data no
    # analyst PDF or directory publishes. FIRST so its "MW" mention doesn't
    # misclassify as new_facility; tenant-specific tokens won't match other leads.
    ("tenant",          re.compile(r"\btenant\b|occupie|hyperscaler footprint|leased capacity|most-tracked (?:tenant|occupier)", re.I)),
    ("deal",            re.compile(r"\$\s?\d|\bacquisition\b|acquir|\btransaction\b|\bM&A\b|\bbuyer\b|\bsold\b", re.I)),
    ("dcpi_mover",      re.compile(r"climbed|slid|biggest mover|shifted|moved \d|\bpts?\b|\bpoints?\b", re.I)),
    ("dcpi_build",      re.compile(r"leads the DCPI|index at|at \d+/100|build (?:signal|market)|excess.?power|\bDCPI\b", re.I)),
    ("interconnection", re.compile(r"interconnection|\bqueue\b|\bGW\b|time.?to.?power", re.I)),
    ("new_facility",    re.compile(r"new facility|campus|came online|entered the (?:tracker|map)|\bMW\b", re.I)),
]


def _classify_kind(text: str) -> str:
    # Classify on the LEAD (first ~220 chars) — that's where the angle is set;
    # scanning the whole body let stray tokens (a '$' or 'GW' mid-post)
    # mis-attribute the post to the wrong kind.
    t = (text or "")[:220]
    for k, rx in _KIND_PATTERNS:
        if rx.search(t):
            return k
    return "other"


_ENG_CACHE: dict = {}   # days -> (epoch_ts, value)
_ENG_TTL = 600          # 45d aggregate doesn't need per-call freshness (single-replica backend)


def engagement_by_kind(days: int = 45) -> dict:
    """Per-lead-kind reach performance from linkedin_posts:
       {kind: {eng_rate=(clicks+reactions)/impr, avg_impr, posts}}.
    Empty kinds omitted. Fully defensive (returns {} on any error).
    r86d: process-level TTL cache so the editorial hot path (called per-slot by
    both engines, twice per generation) doesn't re-query the 45d aggregate each
    time — matters on the single-replica backend that flaps under sync load."""
    import time
    _now = time.time()
    _hit = _ENG_CACHE.get(days)
    if _hit and (_now - _hit[0]) < _ENG_TTL:
        return _hit[1]
    out: dict = {}
    c = _conn()
    if c is None:
        return out
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(content,'') AS content,
                       COALESCE(impressions,0) AS impr,
                       COALESCE(clicks,0) + COALESCE(likes,0)
                         + COALESCE(comments,0) + COALESCE(shares,0) AS eng
                  FROM linkedin_posts
                 WHERE impressions IS NOT NULL AND impressions > 0
                   AND posted_at > NOW() - make_interval(days => %s)
            """, (int(days),))
            agg: dict = {}
            for r in cur.fetchall():
                k = _classify_kind(r["content"])
                a = agg.setdefault(k, {"impr": 0, "eng": 0, "n": 0})
                a["impr"] += int(r["impr"] or 0)
                a["eng"] += int(r["eng"] or 0)
                a["n"] += 1
            for k, a in agg.items():
                if a["impr"] > 0:
                    out[k] = {"eng_rate": round(a["eng"] / a["impr"], 4),
                              "avg_impr": round(a["impr"] / max(1, a["n"]), 1),
                              "posts": a["n"]}
    except Exception:
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    _ENG_CACHE[days] = (_now, out)
    return out


def _engagement_weights(eng: dict) -> dict:
    """kind -> multiplicative score factor (soft-greedy, floor 0.7x, best ~1.3x).
    Untried kinds aren't in the map → treated as neutral 1.0x by callers, so
    they keep getting explored."""
    rates = [v.get("eng_rate") for v in eng.values() if v.get("eng_rate") is not None]
    hi = max(rates) if rates else 0
    if not hi or hi <= 0:
        return {}
    return {k: round(0.7 + 0.6 * (v["eng_rate"] / hi), 3)
            for k, v in eng.items() if v.get("eng_rate") is not None}


def engagement_signal_block() -> str:
    """One prompt line telling the generator which angles earn the most reach,
    so the analyst voice leans toward what lands. Empty until data accrues."""
    eng = engagement_by_kind()
    if not eng:
        return ""
    ranked = sorted(eng.items(), key=lambda kv: kv[1].get("eng_rate", 0), reverse=True)[:3]
    parts = ", ".join(
        f"{k} ({v['eng_rate']*100:.1f}% eng/impr, ~{v['avg_impr']:.0f} impr)"
        for k, v in ranked)
    return f"\nRECENT REACH (these angles earned the most reach lately — lean toward them): {parts}.\n"


# ── Candidate data-event leads ───────────────────────────────────────
def _num(x):
    try:
        return float(x)
    except Exception:
        return None


# ── Agent-demand lead (r-agent-demand, 2026-07-17) ───────────────────
# What AI agents actually ASKED the live infrastructure layer for — the ONE
# angle whose data satisfies the analyst-voice contract (a specific metric that
# MOVED, and moved UP) while DCPI is flat (one distinct value across 41 daily
# snapshots), and a dataset nobody else can publish. Sources (all aggregate,
# already privacy-safe):
#   funnel     /api/v1/mcp/funnel     -> paid_tool_demand_30d (per-tool DISTINCT callers)
#   reach      /api/v1/ai/reach       -> distinct_agents_7d (honest distinct public IPs)
#   retention  /api/v1/mcp/retention  -> ip_cohort weekly new_ips trend (complete weeks)
# ★ PRIVACY (enforced HERE, never left to prose): aggregate distinct-caller
# counts ONLY. k-anonymity floor of 5 — a tool with fewer distinct callers
# could identify one customer and is suppressed. NEVER tool params, NEVER
# free-text queries, NEVER a named platform's numbers.
_AGENT_DEMAND_K_MIN = 5        # k-anonymity: suppress tools with < 5 distinct callers
_AGENT_DEMAND_MIN_AGENTS = 20  # never lead with a noisy/embarrassing small count

# Analyst-friendly label per flagship tool, so the post says what the market is
# ASKING (interconnection headroom), not an API symbol. Unlisted tools fall
# back to the tool name.
_AGENT_DEMAND_TOOL_LABELS = {
    "get_grid_intelligence":    "interconnection headroom",
    "get_fiber_intel":          "fiber routes",
    "analyze_site":             "site analysis",
    "compare_sites":            "site comparisons",
    "get_dchub_recommendation": "build recommendations",
}


def _agent_demand_metrics(funnel: dict, reach: dict, retention: dict) -> dict | None:
    """Parse + validate the three AGGREGATE payloads into the agent-demand
    metrics dict, or None when any leg is missing/thin/not-moving-up — the
    composer then SKIPs honestly (no fallback template, by design).
    Pure + stdlib-only so tests exercise it without Flask/DB (AST-extracted,
    same pattern as tests/test_media_editorial_classify.py)."""
    try:
        tools = []
        for t in (funnel or {}).get("paid_tool_demand_30d") or []:
            try:
                users = int((t or {}).get("users") or 0)
                name = str((t or {}).get("tool") or "").strip()
            except Exception:
                continue
            if name and users >= _AGENT_DEMAND_K_MIN:
                tools.append({"tool": name, "users": users,
                              "label": _AGENT_DEMAND_TOOL_LABELS.get(name, name)})
        tools.sort(key=lambda t: t["users"], reverse=True)
        agents = int((reach or {}).get("distinct_agents_7d") or 0)
        cohort = [r for r in ((retention or {}).get("ip_cohort") or [])
                  if isinstance(r, dict) and r.get("new_ips") is not None]
        if not tools or agents < _AGENT_DEMAND_MIN_AGENTS or len(cohort) < 2:
            return None
        latest = int(cohort[-1].get("new_ips") or 0)
        prior_row = cohort[-3] if len(cohort) >= 3 else cohort[0]
        prior = int(prior_row.get("new_ips") or 0)
        weeks_apart = min(len(cohort) - 1, 2)
        # Analyst contract: the first line states a metric AND how it MOVED.
        # POSITIVE-RESULTS MANDATE: never lead with a decline — if new-agent
        # arrivals aren't growing, there is no agent-demand lead this week.
        if prior <= 0 or latest <= prior:
            return None
        return {"agents_7d": agents, "top_tools": tools[:3],
                "new_ips_latest": latest, "new_ips_prior": prior,
                "weeks_apart": weeks_apart}
    except Exception:
        return None


def _agent_demand_lead(funnel: dict, reach: dict, retention: dict) -> dict | None:
    """The ranked-slate lead for the agent_demand kind. None => no lead (honest
    skip). The headline leads with the distinct-agent count and states the move
    UP ('up from X to Y'), so it clears leads_with_number AND the
    never-lead-with-a-downgrade mandate by construction."""
    m = _agent_demand_metrics(funnel, reach, retention)
    if not m:
        return None
    top = m["top_tools"][0]
    runners = ", ".join(f"{t['users']} on {t['tool']}" for t in m["top_tools"][1:])
    _wk = _dt.datetime.utcnow().isocalendar()
    headline = (
        f"{m['agents_7d']} distinct AI agents queried DC Hub's live "
        f"infrastructure layer this week, and weekly first-time agents are up "
        f"from {m['new_ips_prior']} to {m['new_ips_latest']}")
    trend = (
        f"top ask: {top['label']} — {top['users']} distinct callers on "
        f"{top['tool']} in 30 days"
        + (f" (then {runners})" if runners else ""))
    return {
        "kind": "agent_demand",
        "headline_number": headline,
        "trend": trend,
        "so_what": ("agent demand is the forward book of AI-infrastructure "
                    "questions: the tools agents call most are where siting "
                    "and capex decisions are being made right now."),
        "source_url": "https://dchub.cloud/ai",
        # Week-stamped entity so the 14-day ENTITY window can't block next
        # week's refreshed numbers; the per-kind cooldown paces intra-week.
        "dedup_key": f"agent_demand:wk{_wk[0]}{_wk[1]:02d}",
        "score": round(m["agents_7d"]
                       * _KIND_SCORE_SEED.get("agent_demand", 0.60), 2),
    }


def _queue_leads_from_snapshot(snap: dict, limit: int | None = None) -> list[dict]:
    """EVERY operator in the queue snapshot is its own analyst story.

    ★2026-08-24 SUPPLY FIX. This lane used to return exactly ONE lead — a
    max() over `by_iso` — while the live snapshot carries TEN operators with
    real queued load (NESO 600 GW, ERCOT 440, MISO 223, SPP 188, PJM 171,
    CAISO 75, AESO 25, IESO 17, ISO-NE 14, NYISO 10). Nine genuine,
    number-led leads were discarded on every run, and the desk then
    suppressed slots for "no novel data event".

    ★THE CLASS: max() collapses the ANGLE to one candidate, so the whole
    lane goes quiet the moment that one candidate is on cooldown. The
    dcpi_build lane fixed exactly this on 2026-07-03 ("taking [0] alone gave
    the feed one recurring build lead forever") and the operator lane fixed
    it again in #2722. This is the same fix, third lane.

    Scope rules are UNCHANGED and now applied PER ROW:
      · US operator  → share over the US rows ONLY, via
        media_claim_verify.queue_share_clause (drops the clause unless the
        rounded pct recomputes within ±5%);
      · non-US operator → honest region label, NO share clause (a
        single-operator region share is 100% — not a story);
      · structured queue_gw / queue_scope / queue_scope_total_gw ride on
        each lead for downstream verification.

    Ranked by queued GW, with a small per-rank decay so the leader still
    wins by default but the runners-up stay newsworthy (>= _NEWSWORTHY_MIN)
    and can lead a slot when the leader is inside its entity window.
    """
    try:
        from routes.media_claim_verify import (
            OPERATOR_SCOPE, SCOPE_REGION_LABEL, queue_share_clause)
        by_iso = (snap or {}).get("by_iso") or []
        if limit is None:
            try:
                limit = max(1, int(os.environ.get("MEDIA_QUEUE_ROTATE_TOPN", "6")))
            except Exception:
                limit = 6

        # Same-scope denominators, computed ONCE over the whole snapshot —
        # never the mixed all-ISO total (that mix is exactly how 609 GW
        # became "35% of all US").
        scope_totals: dict = {}
        rows: list = []
        for row in by_iso:
            g = _num(row.get("queued_load_total_gw"))
            if not g or g <= 0:
                continue
            iso = row.get("iso") or ""
            scope = OPERATOR_SCOPE.get(str(iso).strip().lower(), "US")
            scope_totals[scope] = scope_totals.get(scope, 0.0) + g
            rows.append((g, iso, scope, row))

        rows.sort(key=lambda t: -t[0])
        leads: list = []
        for rank, (g, iso, scope, row) in enumerate(rows[:limit]):
            iso = iso or "an ISO"
            scope_total = scope_totals.get(scope, 0.0)
            share = queue_share_clause(g, scope_total, scope) if scope == "US" else ""
            region = SCOPE_REGION_LABEL.get(scope, scope)
            _headline_op = (f"{iso}'s" if scope == "US" else f"{region}'s {iso}")
            leads.append({
                "kind": "interconnection",
                "headline_number": (f"{_headline_op} interconnection queue holds "
                                    f"{g:.0f} GW of requested load{share}"),
                "trend": f"queue depth signals multi-year time-to-power in {iso}",
                "so_what": ("new large loads in this grid face a long energization "
                            "wait — price the delay into the site decision."),
                "source_url": row.get("source_url") or "https://dchub.cloud/grid-intelligence",
                "dedup_key": f"queue:{str(iso).lower()}",
                "score": round(min(50.0, g / 12.0) * (1.0 - 0.04 * rank), 2),
                "queue_gw": g,
                "queue_scope": scope,
                "queue_scope_total_gw": round(scope_total, 1),
            })
        return leads
    except Exception as e:
        logger.warning("[editorial] queue leads failed: %s", str(e)[:160])
        return []


def _queue_lead_from_snapshot(snap: dict) -> dict | None:
    """The single TOP queue lead. Retained as the pure, stdlib-only entry the
    scope/consistency tests exercise (tests/test_media_entity_consistency.py);
    rank_data_events now takes the full rotation via
    _queue_leads_from_snapshot."""
    leads = _queue_leads_from_snapshot(snap, limit=1)
    return leads[0] if leads else None


def _norm_entity(s: str) -> str:
    """Same normalization the (kind, entity) ledger uses, so an operator
    featured recently is recognized regardless of spelling."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _known_operator_tokens(conn) -> set:
    """Every tracked operator's canonical key, normalized like the ledger.

    This is the ONLY set a text token may join the operator veto through — see
    _operator_exclusion_parts(). Empty set on any failure, which the caller
    reads as "cannot narrow", not as "nothing is an operator".
    """
    if conn is None:
        return set()
    from routes.operator_spotlight import _spelling_index
    with conn.cursor() as cur:
        return {t for t in (_norm_entity(k) for k in _spelling_index(cur))
                if t}


def _operator_exclusion_parts(conn=None) -> tuple[set, set, dict]:
    """The operator veto, split into its two independent causes.

    Returns (ledger_tokens, text_tokens, meta). They are kept SEPARATE because
    a candidate blocked because we featured it last Tuesday and a candidate
    blocked because some other publisher's prose happened to contain its name
    need opposite fixes, and the debug endpoint could not tell them apart —
    it reported one `rotation_blocked` boolean over the union. Two of the three
    causes that endpoint exists to separate were still blurred together.

    ★★★ 2026-08-23 — WHY THE TEXT SIDE IS INTERSECTED. READ BEFORE WIDENING.

    _recently_posted_keys() is documented as returning "dedup_keys (normalized
    markets/isos/deals)". It does not. It returns EVERY WHITESPACE TOKEN of the
    concatenated bodies of up to 120 recent posts —
    `{tok for tok in recent.split()}` — and the old code unioned that whole bag
    straight into the operator veto. Measured live the same day:

        exclusion_tokens_n: 799
        sample: 10 100 1000 2026 2030 5000 5000000 18603 15573 5b 10day

    Nine of those tokens can come from the ledger (operator_spotlight fired 9
    times in 14d). The other ~790 are prose. Meanwhile every one of the lane's
    10 threshold-clearing candidates was rotation_blocked and the lane returned
    nothing, against a supply of 491 builds/30d and 5,448 tracked operators.

    And the bag is read as a set of NAMES, so it vetoes unevenly: tokens are
    split on whitespace BEFORE normalization, so a multi-word operator
    ("Frontier Tampa" -> frontiertampa) can never match a text token, while a
    single-word one (Meta, Oracle, Switch, Stack, Aligned, Vantage) is vetoed
    by any prose containing that word — including prose about switches, stacks
    and aligned racks. The source of that prose is not even ours: the SQL reads
    social_media_posts + linkedin_posts, and the quad writes
    linkedin_quad_posts. A different publisher's copy was vetoing this lane.

    The narrowing is a POSITIVE test, not a stopword blocklist: a text token
    may veto only if it is itself a tracked operator key. That deletes the
    ~790 prose tokens without touching a single genuine "we just wrote about
    Oracle" veto, so it CANNOT recreate the nLighten stalemate the text side
    was added for (2026-08-07, preserved below) — a token that names a real
    operator still vetoes exactly as before.

    KILL SWITCH: MEDIA_OPERATOR_TEXT_VETO_KEYS_ONLY_DISABLE=1 restores the raw
    bag-of-words. The same fallback happens automatically, and is reported as
    mode="all_tokens_fallback", whenever the operator key space cannot be read
    — on a DB hiccup the veto must not silently get more permissive than the
    behaviour it replaced.
    """
    ledger = {_norm_entity(x["entity"])
              for x in recent_lead_ledger(_MARKET_WINDOW_DAYS)
              if x.get("kind") == "operator_spotlight" and x.get("entity")}
    ledger.discard("")

    # ★2026-08-07 live: the ledger only sees QUAD posts, but the nLighten
    # piece shipped via the news path — so the lane kept offering nLighten
    # and the desk's recent_text guard (correctly) killed it every slot:
    # permanent stalemate, feed silent. Also exclude any operator whose
    # canonical key appears in recent post TEXT (same 4-day window the
    # desk's own guard uses), so the lane always offers someone the desk
    # can actually run.
    meta = {"mode": "keys_only", "raw_text_tokens_n": 0, "known_operators_n": 0}
    text: set = set()
    try:
        raw = {re.sub(r"[^a-z0-9]+", "", w.lower())
               for w in _recently_posted_keys(days=4)}
        raw.discard("")
        meta["raw_text_tokens_n"] = len(raw)
        # ★ DEFAULT TO THE OLD, WIDER VETO AND NARROW ONLY ON SUCCESS. Written
        # the other way round (start empty, fill on success) any failure below
        # makes the veto MORE permissive than the behaviour being replaced —
        # the lane would start offering operators the desk is about to refuse,
        # which is the stalemate this whole text side exists to prevent. A
        # degraded read must fail toward the old behaviour, never past it.
        text = raw
        if (os.environ.get("MEDIA_OPERATOR_TEXT_VETO_KEYS_ONLY_DISABLE")
                or "").strip() == "1":
            meta["mode"] = "all_tokens_disabled"
        else:
            try:
                known = _known_operator_tokens(conn)
            except Exception as e:  # noqa: BLE001
                known = set()
                meta["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            meta["known_operators_n"] = len(known)
            if known:
                text = raw & known
            else:
                # Could not read the operator key space — keep the OLD, wider
                # veto rather than quietly letting the lane offer an operator
                # the desk is about to refuse.
                meta["mode"] = "all_tokens_fallback"
    except Exception as e:  # noqa: BLE001
        meta["mode"] = "error"
        meta["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    text.discard("")
    return ledger, text, meta


def _operator_exclusion_set(conn=None) -> set:
    """Normalized entity tokens the operator lane must NOT offer today.

    ★ Shared with the operator-lane-debug endpoint ON PURPOSE. A diagnostic that
    re-derives the exclude set can drift from the one the lane actually applies,
    and then confidently reports the wrong cause — the failure mode the endpoint
    exists to end. One function, one answer.
    """
    ledger, text, _ = _operator_exclusion_parts(conn)
    featured = ledger | text
    featured.discard("")
    return featured


def _operator_spotlight_lead():
    """One operator-of-the-day lead — capacity + new projects — for a FRESH
    operator (not featured within MEDIA_ENTITY_WINDOW_DAYS). Returns a lead
    dict or None. Kill: MEDIA_OPERATOR_LANE_DISABLE=1.

    The lane's pick_spotlight(conn, exclude_keys) already refuses to fabricate
    (None on no material); this wrapper adds the daily rotation by excluding
    canonical keys of operators the ledger shows we featured recently, and
    renders the capacity + new-projects the operator asked to see."""
    if (os.environ.get("MEDIA_OPERATOR_LANE_DISABLE") or "").strip() == "1":
        return None
    try:
        from routes.operator_spotlight import pick_spotlight
    except Exception as e:  # noqa: BLE001
        logger.warning("[editorial] operator_spotlight import: %s", str(e)[:120])
        return None
    c = _conn()
    if c is None:
        return None
    try:
        featured = _operator_exclusion_set(c)
        exclude: set = set()
        sp = None
        for _ in range(8):   # bounded: skip past recently-featured operators
            cand = pick_spotlight(c, exclude_keys=exclude)
            if not cand:
                break
            if (_norm_entity(cand.get("operator", "")) in featured
                    or _norm_entity(cand.get("key", "")) in featured):
                exclude.add(cand.get("key"))
                continue
            sp = cand
            break
        if not sp:
            return None

        op = sp.get("operator") or "This operator"
        fleet_n = sp.get("fleet_n")
        fleet_mw = sp.get("fleet_mw")
        added = sp.get("added") or 0
        sites = [s for s in (sp.get("sites") or []) if s][:3]

        # Capacity line — UNKNOWN IS NOT ZERO (most buildings carry no power_mw),
        # so quote MW only when we actually have it.
        cap = ""
        if isinstance(fleet_n, int) and fleet_n > 0:
            cap = f"{fleet_n:,} tracked buildings"
            if isinstance(fleet_mw, (int, float)) and fleet_mw > 0:
                cap += f" · {fleet_mw:,.0f} MW"

        # ★ Newsworthiness score on the SAME SCALE as every other lead (the
        # desk's bar is raw_score >= _NEWSWORTHY_MIN=8; agent_demand tops ~50,
        # a deal ~120, dcpi_build ~14-21). The first cut seeded a flat 0.90
        # here — below the bar, so the operator lead was PRODUCED but could
        # never be selected and the feed stayed silent. Scale it with the
        # operator's real activity: a dependable daily lead that clears the bar
        # and beats the repetitive dcpi_build one-liners, but still yields to a
        # genuine big deal / demand story. Capped so it never dominates.
        if sp.get("angle") == "portfolio_growth":
            headline = (f"{op} added {added} new "
                        f"{'sites' if added != 1 else 'site'} to DC Hub's map "
                        f"in the last 30 days")
            new_projects = ("New this month: " + ", ".join(sites)
                            if sites else "New sites across multiple markets")
            score = min(45.0, 12.0 + added * 0.6
                        + (fleet_n or 0) / 60.0)
        else:  # a closed transaction, sized in MW (money deliberately absent)
            mw = sp.get("mw") or 0
            where = f" in {sp.get('market')}" if sp.get("market") else ""
            headline = (f"{op} closed a new "
                        f"{mw:,.0f} MW acquisition{where}"
                        if mw else f"{op} closed a new acquisition{where}")
            new_projects = (f"Latest addition{where}" if where
                            else "Latest portfolio addition")
            score = min(45.0, 15.0 + (mw or 0) / 25.0)

        trend = (f"{op} now operates {cap} that DC Hub tracks live"
                 if cap else f"{op}'s live footprint on DC Hub's map")
        return {
            "kind": "operator_spotlight",
            "headline_number": headline,
            "trend": trend,
            "so_what": (f"{new_projects}. See the full operator footprint and "
                        f"pipeline on DC Hub."),
            "source_url": "https://dchub.cloud/facilities",
            "entity": op,
            "dedup_key": f"operator_spotlight:{sp.get('key')}",
            "score": round(score, 2),
            # structured fields ride along for the claim-verify gate + renderers
            "operator": op,
            "fleet_n": fleet_n,
            "fleet_mw": fleet_mw,
            "new_sites_30d": added,
            "new_site_markets": sites,
        }
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def rank_data_events() -> list[dict]:
    """Gather today's real data events and rank by newsworthiness. Each lead:
       {kind, headline_number, trend, so_what, source_url, dedup_key, score}.
    Reuses marketing_engine._collect_signals() (DCPI movers, deals, facilities)
    and augments with the interconnection-queue snapshot. Fully defensive —
    any source that errors is simply skipped."""
    leads: list[dict] = []

    # 0) Capability / data-milestone radar (2026-06-21) — the autonomous "what
    # can we announce" input: NEW data sources (a feed/tool/layer we just
    # shipped) and milestone jumps become ranked leads here, so the desk + quad
    # announce product enhancements on their own. Registry: brain_capability_radar.
    try:
        from routes.brain_capability_radar import capability_radar_leads
        leads += capability_radar_leads() or []
    except Exception as e:
        logger.warning("[editorial] capability radar failed: %s", str(e)[:160])

    # 0a2) PLATFORM MILESTONES (2026-08-07) — the NUMBERS lane, and the other
    # half of the operator's directive ("new product rollouts is a story, new
    # numbers"): a canonical headline figure crossing a round threshold (the /ai
    # counter passing 4,000,000 total requests served, the index crossing
    # 17,000 facilities). The radar above owns PRODUCT rollouts; this owns
    # NUMBERS, and the two de-conflict rather than duplicate — see below and
    # routes/media_milestones. Fully defensive.
    try:
        from routes.media_milestones import platform_milestone_leads
        _ms_leads = platform_milestone_leads() or []
        # SAME-RUN de-confliction: four of the radar's registry rows are numeric
        # milestones over the same metrics. If the radar already emitted a
        # data_milestone for one of them in THIS slate, that crossing is spoken
        # for — do not put a second lead about it on the board. (The cross-run
        # half is the shared baseline in media_milestones.mark_milestone_
        # announced.)
        _radar_ms = {str(l.get("dedup_key") or "").split(":", 1)[-1]
                     for l in leads if (l or {}).get("kind") == "data_milestone"}
        for _ml in _ms_leads:
            if _ml.get("radar_key") and _ml["radar_key"] in _radar_ms:
                logger.info("[editorial] milestone %s deferred — the radar "
                            "already leads that crossing this run",
                            _ml.get("metric_key"))
                continue
            leads.append(_ml)
    except Exception as e:
        logger.warning("[editorial] platform milestones failed: %s", str(e)[:160])

    # 0b) Brain insight bridge (2026-06-24) — the brain's graded, refutation-
    # survived DATA-coverage findings (e.g. "311 DCPI markets across 7 live ISOs")
    # become candidate leads so the analyst can tell the data story the brain
    # vetted. READ-ONLY + DRAFT-ONLY + dark by default (BRAIN_MEDIA_BRIDGE_ENABLED);
    # they compete in the same ranked slate and pass the same publish guards +
    # human approval. Module: routes/brain_media_bridge.
    try:
        from routes.brain_media_bridge import brain_insight_leads
        leads += brain_insight_leads() or []
    except Exception as e:
        logger.warning("[editorial] brain insight bridge failed: %s", str(e)[:160])

    # 0c) OPERATOR OF THE DAY (2026-08-07) — the daily operator feature and the
    # cure for the empty-slate starvation that silenced the feed for days. The
    # operator lane holds abundant, positive, number-led material (portfolio
    # growth: "nLighten +33, STACK +27, Equinix +7 in 30d") and rotates to a
    # DIFFERENT operator daily via the durable (kind, entity) ledger, so it is
    # a dependable lead that never repeats. Fully defensive.
    try:
        op_lead = _operator_spotlight_lead()
        if op_lead:
            leads.append(op_lead)
    except Exception as e:
        logger.warning("[editorial] operator spotlight failed: %s", str(e)[:160])

    # Core signals (movers, deals, facilities) — reuse the tested collector.
    sig = {}
    try:
        from routes.marketing_engine import _collect_signals
        sig = _collect_signals() or {}
    except Exception as e:
        logger.warning("[editorial] _collect_signals failed: %s", str(e)[:160])

    # 1) DCPI movers — week-over-week shift on the excess-power index.
    for m in (sig.get("biggest_movers") or [])[:6]:
        d = _num(m.get("delta"))
        mk = m.get("market") or m.get("metro")
        if d is None or not mk or abs(d) < 5:
            continue
        direction = "climbed" if d > 0 else "slid"
        verdict = "a BUILD signal strengthening" if d > 0 else "a constraint emerging"
        leads.append({
            "kind": "dcpi_mover",
            "headline_number": f"{mk} {direction} {abs(d):.0f} pts on the DCPI excess-power index this week",
            "trend": f"{'+' if d>0 else ''}{d:.0f} pts WoW — the largest move in the index",
            "so_what": f"{verdict}: re-rank {mk} in the site-selection shortlist.",
            "source_url": "https://dchub.cloud/dcpi",
            # 2026-07-03: split on comma like dcpi_build so the tail normalizes
            # to the CITY ("cheyenne") not "cheyennewy" — the old tail never
            # matched the whitespace tokens in _recently_posted_keys, so
            # mover-lead dedup was silently a no-op (see media variety audit).
            "dedup_key": f"dcpi_mover:{str(mk).split(',')[0].lower().strip()}",
            # r-agent-demand (2026-07-17): 1.2 -> seeded 1.0 — DCPI is flat, so
            # while it stays flat the demand/deal kinds should own the rotation.
            "score": abs(d) * _KIND_SCORE_SEED.get("dcpi_mover", 1.0),
        })

    # 1b) Top DCPI build market — reliable lead even when WoW deltas are null
    # (movers depends on computed_at history that DCPI re-stamps, so it is
    # frequently empty). The leading excess-power score is itself a real,
    # ownable number; the novelty filter in editorial_decision() suppresses it
    # if that market was already featured this week (the 'Cheyenne always wins'
    # trap the audit flagged).
    # 2026-07-03 VARIETY FIX: emit the top-N BUILD markets as SEPARATE candidate
    # leads (not just [0]). The deterministic #1 (Cheyenne) is the same market
    # every day until its score changes, so taking [0] alone gave the feed one
    # recurring build lead forever. editorial_decision() then applies the durable
    # (kind, entity) ledger + kind-cooldown to pick a fresh one — so on the day
    # #1 was already posted, #2/#3 lead instead of suppressing the whole kind.
    tb = sig.get("top_build_markets") or []
    try:
        _build_n = max(1, int(os.environ.get("MEDIA_BUILD_ROTATE_TOPN", "5")))
    except Exception:
        _build_n = 5
    for _rank, b in enumerate(tb[:_build_n]):
        ex = _num(b.get("excess"))
        mk = b.get("market") or b.get("slug")
        if ex is None or not mk:
            continue
        _lead_word = "leads the" if _rank == 0 else "ranks top-5 on the"
        leads.append({
            "kind": "dcpi_build",
            "headline_number": f"{mk} {_lead_word} DCPI excess-power index at {ex:.0f}/100",
            "trend": f"among the strongest excess-power headroom of any tracked market (constraint {(_num(b.get('constraint')) or 0):.0f})",
            "so_what": f"{mk} sits near the top of the build shortlist on available power — but verify time-to-power before committing.",
            "source_url": build_public_url("dcpi", b.get('slug','')),
            "dedup_key": f"build:{str(mk).split(',')[0].lower().strip()}",
            # keep #1 the strongest, decay lower ranks slightly so the sort still
            # prefers the leader when nothing blocks it, but the runners-up remain
            # newsworthy (>= _NEWSWORTHY_MIN) and can win when #1 is on cooldown.
            # r-media-goldmine (2026-07-14): de-weighted 0.45 -> 0.30 so a ~70/100 build
            # market scores ~21 (still > _NEWSWORTHY_MIN=8, so it never leaves the board on
            # a genuinely quiet day) but loses to the evergreen moat/pillar leads (score 62-64)
            # — the DCPI-Cheyenne repeat was this lead out-ranking a starved capability pool.
            # r-agent-demand (2026-07-17): 0.30 -> seeded 0.25 (one more notch down while
            # DCPI stays flat) so agent_demand + hyperscaler_deal dominate the rotation
            # until the index has real movement. Still ~17.5 for a 70/100 market — on the
            # board (> _NEWSWORTHY_MIN=8), just no longer winning by default.
            "score": (ex or 0) * _KIND_SCORE_SEED.get("dcpi_build", 0.25)
                     * (1.0 - 0.04 * _rank),
        })

    # 2) M&A deals — the top-N disclosed transactions, EACH its own lead.
    # value is stored in $M as `value_m` (verified: KKR/Nvidia value_m=10000).
    # ★2026-08-24 SUPPLY FIX: this used to pick a single best_deal by max()
    # value out of the 6 rows _collect_signals fetches, so the deal ANGLE went
    # quiet for days whenever that one buyer/seller pair was inside its entity
    # window — with five perfectly good tracked transactions sitting unused.
    # Same collapse the queue + dcpi_build lanes fixed; see
    # _queue_leads_from_snapshot for the class.
    try:
        _deal_n = max(1, int(os.environ.get("MEDIA_DEAL_ROTATE_TOPN", "4")))
    except Exception:
        _deal_n = 4
    _valued_deals = []
    for dl in (sig.get("recent_deals") or []):
        v = _num(dl.get("value_m") or dl.get("value") or dl.get("value_usd"))
        if v and v > 0:
            _valued_deals.append((v, dl))
    _valued_deals.sort(key=lambda t: -t[0])
    for _rank, (bv, best_deal) in enumerate(_valued_deals[:_deal_n]):
        # value is stored in $M in most rows; render sensibly.
        val_str = (f"${bv/1000:.1f}B" if bv >= 1000 else f"${bv:.0f}M")
        buyer = best_deal.get("buyer") or best_deal.get("acquirer") or "an operator"
        seller = best_deal.get("seller") or best_deal.get("target") or ""
        pair = f"{buyer}/{seller}" if seller else buyer
        # Only the leader may claim "the largest ... this week"; the
        # runners-up must not inherit a superlative that is no longer true.
        _trend = ("the largest disclosed DC deal in the tracker this week"
                  if _rank == 0 else
                  "among the largest disclosed DC transactions in the tracker")
        leads.append({
            "kind": "deal",
            "headline_number": f"{val_str} data-center transaction: {pair}",
            "trend": _trend,
            "so_what": "capital is repricing power-rich sites — watch the comparable markets.",
            "source_url": "https://dchub.cloud/transactions",
            "dedup_key": f"deal:{str(buyer).lower()}:{str(seller).lower()}",
            # r86d: cap raised 60->120 so marquee deals keep decisive magnitude
            # (a $50B deal must out-rank a $10B one even after the 0.7-1.3x weight).
            "score": round(min(120.0, 18.0 + bv / 250.0) * (1.0 - 0.04 * _rank), 2),
        })

    # 2b) Tenant intelligence — the uncontested moat (project_dchub_competitive_moat):
    # per-facility occupier footprint no analyst PDF / directory publishes. No
    # tenant-rollup endpoint exists, so query facility_tenants directly; fully
    # defensive — any failure → no tenant lead, no harm to the rest of the slate.
    # ★2026-08-24 SUPPLY FIX (two bugs, one block):
    #   1. it already SELECTed the top 3 tenants but emitted a lead for _trows[0]
    #      only — rows 1 and 2 were spent as prose ("ahead of X (12), Y (9)").
    #   2. the >= 5 facilities bar was tested against _trows[0] ALONE, so one
    #      thin leader disqualified the whole ANGLE. That is the #2722 operator-
    #      lane bug exactly: A THRESHOLD MUST DISQUALIFY A CANDIDATE, NOT THE
    #      ANGLE. It is now applied per row and the loop walks past a failure.
    try:
        _tenant_n = max(1, int(os.environ.get("MEDIA_TENANT_ROTATE_TOPN", "4")))
    except Exception:
        _tenant_n = 4
    try:
        import psycopg2 as _pg
        _tc = _pg.connect(os.environ.get("DATABASE_URL"))
        _tcur = _tc.cursor()
        _tcur.execute(
            "SELECT tenant_name, COUNT(DISTINCT facility_id) AS c, "
            "       COALESCE(SUM(estimated_mw), 0) AS mw "
            "FROM facility_tenants "
            "WHERE tenant_name IS NOT NULL AND tenant_name <> '' "
            "GROUP BY tenant_name ORDER BY c DESC LIMIT %s",
            (int(_tenant_n) + 2,))
        _trows = _tcur.fetchall()
        _tcur.close(); _tc.close()
        _emitted = 0
        for _rank, _row in enumerate(_trows or []):
            if _emitted >= _tenant_n:
                break
            if not _row or not _row[0]:
                continue
            _c1 = int(_num(_row[1]) or 0)
            if _c1 < 5:
                continue          # per-CANDIDATE bar; the angle survives
            _t1 = _row[0]
            _mw1 = _num(_row[2]) or 0
            _runners = ", ".join(f"{r[0]} ({int(r[1])})"
                                 for r in (_trows or [])[:3]
                                 if r and r[0] and r[0] != _t1)
            _mwline = f" (~{_mw1:,.0f} MW of tracked leased capacity)" if _mw1 > 0 else ""
            # Only the true leader may claim "the most-tracked"; a runner-up
            # inheriting that superlative would be a straightforward lie.
            _headline = (
                f"{_t1} is the most-tracked data-center tenant in DC Hub — "
                f"present at {_c1} facilities{_mwline}"
                if _emitted == 0 else
                f"{_t1} is tracked at {_c1} data-center facilities in DC Hub{_mwline}")
            leads.append({
                "kind": "tenant",
                "headline_number": _headline,
                "trend": (f"alongside {_runners} — " if _runners else "") +
                         "the per-occupier footprint no analyst PDF or directory publishes",
                "so_what": f"hyperscaler concentration IS the forward demand signal — where {_t1} clusters, "
                           "power headroom and land tighten next; price the comparable markets now.",
                "source_url": "https://dchub.cloud/research?series=tenant",
                "dedup_key": f"tenant:top:{str(_t1).lower()}",
                "score": round((14.0 + min(20.0, _c1 * 0.4)) * (1.0 - 0.04 * _emitted), 2),
            })
            _emitted += 1
    except Exception as _te:
        logger.warning("[editorial] tenant lead failed: %s", str(_te)[:160])

    # 2c) AGENT DEMAND (r-agent-demand, 2026-07-17) — what AI agents actually
    # asked the live layer for. The ONE dataset whose numbers MOVE (and move
    # UP) while DCPI is flat, and an angle only DC Hub can publish. Aggregates
    # only — see the privacy notes on _agent_demand_metrics. Fully defensive:
    # any endpoint failure → no lead, never a fabricated one.
    try:
        _ad = _agent_demand_lead(
            _internal("/api/v1/mcp/funnel", timeout=8),
            _internal("/api/v1/ai/reach", timeout=8),
            _internal("/api/v1/mcp/retention", timeout=8),
        )
        if _ad:
            leads.append(_ad)
    except Exception as _ae:
        logger.warning("[editorial] agent-demand lead failed: %s", str(_ae)[:160])

    # 3) Interconnection queue — the clearest 'time-to-power' trend (ungated).
    # 2026-07-17 (post 100292): the snapshot now mixes US ISOs with
    # international operators (NESO=GB, IESO/AESO=CA) and its totals sum ALL
    # rows — so the old code paired a GB operator's GW with a GB+US
    # denominator and hardcoded 'US' into the sentence ("609 GW ... NESO's
    # interconnection queue, 35% of all US queued load"). _queue_lead_from_
    # snapshot is scope-aware: the share clause is US-only over a US-only
    # denominator, non-US operators get an honest region label and NO share,
    # and the rendered percentage must recompute within ±5% or it is dropped.
    snap = _internal("/api/v1/interconnection-queue/snapshot")
    # 2026-08-24: take the top-N operators, not just the leader — see
    # _queue_leads_from_snapshot for why max() starved this lane.
    leads += _queue_leads_from_snapshot(snap)

    # 4) New facilities surfaced in the last 24h — the top-N by MW, each its
    # own lead. ★2026-08-24 SUPPLY FIX: the old loop kept a single big_fac by
    # max() MW, so a quiet day for the leader silenced the whole lane even when
    # the tracker had several fresh sites. Same class as the queue/deal/tenant
    # lanes above.
    try:
        _fac_n = max(1, int(os.environ.get("MEDIA_FACILITY_ROTATE_TOPN", "3")))
    except Exception:
        _fac_n = 3
    _fresh_facs = []
    for f in (sig.get("new_facilities_24h") or []):
        mw = _num(f.get("mw") or f.get("capacity_mw") or f.get("total_mw"))
        if mw and mw > 0:
            _fresh_facs.append((mw, f))
    _fresh_facs.sort(key=lambda t: -t[0])
    for _rank, (mw, big_fac) in enumerate(_fresh_facs[:_fac_n]):
        name = big_fac.get("name") or big_fac.get("operator") or "A new facility"
        loc = big_fac.get("state") or big_fac.get("country") or ""
        leads.append({
            "kind": "new_facility",
            "headline_number": f"{name}: {mw:.0f} MW {('in '+loc) if loc else ''} just entered the tracker",
            "trend": "fresh capacity added to the live facility map in the last 24h",
            "so_what": f"another {mw:.0f} MW of demand on {loc or 'the local grid'} — watch the headroom there.",
            "source_url": "https://dchub.cloud/map",
            "dedup_key": f"facility:{str(name).lower()}",
            "score": round(min(35.0, 6.0 + mw / 30.0) * (1.0 - 0.04 * _rank), 2),
        })

    # 5) Weekly Analyst Note (2026-07-04) — the brain-authored, fenced + cited
    # weekly synthesis surfaces as a LEAD only; the desk's existing gates
    # (number-lead, novelty, claim-verify, approval) decide posting.
    try:
        from routes.analyst_note import analyst_note_lead
        _an_lead = analyst_note_lead()
        if _an_lead:
            leads.append(_an_lead)
    except Exception as e:
        logger.warning("[editorial] analyst note lead failed: %s", str(e)[:160])

    # r86d BANDIT: bias toward angles that actually earn reach. Each lead's
    # newsworthiness score is multiplied by its kind's learned reach factor
    # (soft-greedy, floor 0.7x; untried kinds stay neutral 1.0x → explored).
    # raw_score/eng_rate/eng_weight are kept for the scoreboard + transparency.
    eng = engagement_by_kind()
    weights = _engagement_weights(eng)
    for l in leads:
        k = l.get("kind")
        l["raw_score"] = l.get("score", 0)
        l["eng_rate"] = (eng.get(k) or {}).get("eng_rate")
        f = weights.get(k, 1.0)
        l["eng_weight"] = f
        l["score"] = round(l["raw_score"] * f, 2)
    # r86c-compliance (2026-06-27): a lead whose headline_number can't pass the
    # number-lead PUBLISH gate (number not adjacent to a unit — e.g. a brain
    # data-coverage finding title like "311 DCPI markets across 7 live ISOs",
    # where "DCPI" sits between "311" and "markets") composes a post that then
    # bounces off _should_skip_publish — wasting the slot. This is why the feed
    # sat at 0 published / 500 blocked: on quiet days the only surviving lead was
    # a malformed brain-insight title. Drop non-compliant leads so the desk only
    # ever offers leads that WILL publish; the structured leads (dcpi_build
    # "at 70/100", mover "12 pts", deal "$10B", interconnection "X GW") all pass.
    _before = len(leads)
    leads = [L for L in leads
             if leads_with_number(str((L or {}).get("headline_number") or ""))]
    if len(leads) != _before:
        logger.info("[editorial] dropped %d non-number-lead-compliant lead(s) of %d",
                    _before - len(leads), _before)
    leads.sort(key=lambda x: x.get("score", 0), reverse=True)
    return leads


def _recently_posted_keys(days: int = 9) -> set:
    """dedup_keys (normalized markets/isos/deals) that already shipped to
    LinkedIn recently, so the editor never re-leads with the same event."""
    keys: set = set()
    c = _conn()
    if c is None:
        return keys
    try:
        with c.cursor() as cur:
            cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat()
            # UNION both post ledgers (2026-07-02): posts shipped by the direct
            # posters (showcase, media hub, wins poster) land ONLY in
            # linkedin_posts — reading social_media_posts alone left the desk
            # blind to them, so a deal that led TWO posts yesterday still
            # ranked as novel today (the Brookfield $25B repeat).
            cur.execute(
                "SELECT LOWER(COALESCE(content,'')) FROM social_media_posts "
                "WHERE status='published' AND publish_platform='linkedin' "
                "AND published_at >= %s "
                "UNION ALL "
                "SELECT LOWER(COALESCE(content,'')) FROM linkedin_posts "
                "WHERE posted_at >= %s::timestamptz "
                "LIMIT 120",
                (cutoff, cutoff))
            recent = " || ".join(r[0] for r in (cur.fetchall() or []) if r and r[0])
    except Exception:
        recent = ""
    finally:
        try: c.close()
        except Exception: pass
    # A lead is 'already covered' if its market/iso/entity token appears in
    # recent post text. Cheap, robust, no embedding dependency.
    return {tok for tok in recent.split()} if recent else keys


# ── Semantic repetition guard (rag-wire-media-semantic, 2026-07-03) ───
# The token guard above catches EXACT entity repeats but re-leads the same THEME
# reworded ("ERCOT's queue holds 427 GW" vs "Texas grid faces a 427 GW wait" —
# different tokens, same story). This upgrades novelty to MEANING: embed the
# candidate lead's headline+so-what and cosine-compare against the LEAD text of
# the last ~30 posts; reject a candidate that's semantically near a recent post.
# Uses brain_rag's low-level Cohere embed helper (_embed) — imported lazily and
# wrapped so ANY embed/RAG/import failure degrades to the token guard (fail-soft;
# a RAG outage must never dark-hold the feed). NOTE: we embed PROSE only (the
# lead's headline+so-what), never fabricated metrics — retrieval augments the
# novelty reasoning; the numbers stay in the SQL leads untouched.

# env-tunable so the operator can loosen/tighten without a deploy.
try:
    _SEMANTIC_DEDUP_THRESHOLD = float(
        os.environ.get("MEDIA_SEMANTIC_DEDUP_THRESHOLD", "0.82") or 0.82)
except Exception:
    _SEMANTIC_DEDUP_THRESHOLD = 0.82
try:
    _SEMANTIC_RECENT_POSTS = max(1, int(
        os.environ.get("MEDIA_SEMANTIC_RECENT_POSTS", "30")))
except Exception:
    _SEMANTIC_RECENT_POSTS = 30


def _recent_post_texts(limit: int = 30, days: int = 21) -> list[str]:
    """The LEAD text (first ~300 chars) of the last `limit` LinkedIn posts,
    newest first — the material the semantic guard compares candidates against.
    UNIONs both post ledgers like _recently_posted_keys(). Fully defensive → []
    on any error (→ the caller degrades to the token guard)."""
    out: list[str] = []
    c = _conn()
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat()
            cur.execute(
                "SELECT content, ts FROM ("
                "  SELECT COALESCE(content,'') AS content, published_at AS ts "
                "    FROM social_media_posts "
                "   WHERE status='published' AND publish_platform='linkedin' "
                "     AND published_at >= %s "
                "  UNION ALL "
                "  SELECT COALESCE(content,'') AS content, posted_at AS ts "
                "    FROM linkedin_posts WHERE posted_at >= %s::timestamptz "
                ") u "
                "WHERE content <> '' "
                "ORDER BY ts DESC NULLS LAST "
                "LIMIT %s",
                (cutoff, cutoff, int(limit)))
            for r in (cur.fetchall() or []):
                t = (r[0] or "").strip()
                if t:
                    out.append(t[:300])
    except Exception:
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _cosine(a, b) -> float:
    """Cosine similarity of two equal-length float vectors. 0.0 on any mismatch
    (fail-soft: a bad vector never rejects a lead)."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0; na = 0.0; nb = 0.0
        for x, y in zip(a, b):
            fx = float(x); fy = float(y)
            dot += fx * fy; na += fx * fx; nb += fy * fy
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / ((na ** 0.5) * (nb ** 0.5))
    except Exception:
        return 0.0


def _lead_fingerprint(lead: dict) -> str:
    """The PROSE fingerprint of a lead's THEME: headline + so-what. This is what
    the reader experiences as 'the story', so two leads that reword the same
    story fingerprint near-identically. Never includes raw scores/keys."""
    parts = [str((lead or {}).get("headline_number") or ""),
             str((lead or {}).get("so_what") or "")]
    return " ".join(p for p in parts if p).strip()


def _semantic_repeat_predicate(leads: list[dict]):
    """Build a predicate `is_repeat(lead) -> bool` that returns True when a
    lead's THEME is semantically near a recent post (cosine >= threshold).

    Embeds recent-post lead text + candidate fingerprints in at most TWO Cohere
    calls (batched via brain_rag._embed), then does pure-Python cosine. Returns
    a predicate that ALWAYS says False (→ no semantic rejection) if anything
    fails — embed import/outage, empty history, dimension mismatch — so the
    caller cleanly degrades to the token/ledger guards. Never raises."""
    _no_op = lambda lead: False
    try:
        # Lazy import so a brain_rag import error can't break module load.
        from routes.brain_rag import _embed as _rag_embed
    except Exception as e:
        logger.info("[editorial] semantic guard: _embed import unavailable (%s); "
                    "degrading to token guard", str(e)[:120])
        return _no_op

    try:
        recent = _recent_post_texts(limit=_SEMANTIC_RECENT_POSTS)
        if not recent:
            return _no_op

        fingerprints = [_lead_fingerprint(l) for l in leads]
        # index map: only leads with a non-empty fingerprint get an embedding.
        idx = [i for i, fp in enumerate(fingerprints) if fp]
        if not idx:
            return _no_op

        # Embed recent posts as documents, candidate themes as queries (Cohere
        # v3 asymmetric) — matches how brain_rag itself embeds store vs recall.
        post_vecs = _rag_embed(recent, input_type="search_document")
        cand_vecs = _rag_embed([fingerprints[i] for i in idx],
                               input_type="search_query")
        if (not post_vecs or not cand_vecs
                or len(cand_vecs) != len(idx) or not post_vecs[0]):
            logger.info("[editorial] semantic guard: embed returned empty/short; "
                        "degrading to token guard")
            return _no_op

        # candidate id(lead) -> its query vector (id() is stable within this call).
        vec_by_lead: dict = {}
        for pos, i in enumerate(idx):
            vec_by_lead[id(leads[i])] = cand_vecs[pos]

        thr = _SEMANTIC_DEDUP_THRESHOLD

        def _is_repeat(lead) -> bool:
            try:
                cv = vec_by_lead.get(id(lead))
                if not cv:
                    return False
                for pv in post_vecs:
                    if _cosine(cv, pv) >= thr:
                        return True
                return False
            except Exception:
                return False

        return _is_repeat
    except Exception as e:
        logger.warning("[editorial] semantic guard failed (%s); degrading to "
                       "token guard", str(e)[:160])
        return _no_op


def _entity_tail(lead: dict) -> str:
    """The normalized ENTITY token from a lead's dedup_key — the market/iso/
    deal-party the lead is ABOUT. dedup_key is 'kind:entity[:extra]'; the tail
    after the first ':' is the entity (already city-normalized upstream). We
    alnum-squash it so 'Cheyenne, WY'→'cheyenne' matches consistently no matter
    which lead produced it (build vs mover)."""
    dk = (lead or {}).get("dedup_key") or ""
    tail = dk.split(":", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "", tail.lower())


# ── Durable (content_type, entity) ledger ────────────────────────────
# The audit's Cause A.3: the whitespace-token substring match was fragile and
# the story-type dedup was a no-op. This reads the AUTHORITATIVE ledger — the
# quad's own linkedin_quad_posts rows, which record the desk's chosen
# lead_kind + lead_entity per slot (written by linkedin_quad_daily._record) —
# so dedup keys on (type, entity) exactly, over a real window. Fully defensive.
def recent_lead_ledger(days: int = 14) -> list[dict]:
    """Return [{kind, entity, posted_at, days_ago}] for successful quad posts in
    the window. Empty on any error (fail-open → variety guards simply relax)."""
    out: list[dict] = []
    c = _conn()
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT lead_kind, lead_entity, "
                "       EXTRACT(EPOCH FROM (NOW() - posted_at))/86400.0 AS days_ago "
                "  FROM linkedin_quad_posts "
                " WHERE success = TRUE "
                "   AND posted_at > NOW() - make_interval(days => %s) "
                "   AND lead_kind IS NOT NULL "
                " ORDER BY posted_at DESC LIMIT 200",
                (int(days),))
            for r in (cur.fetchall() or []):
                out.append({
                    "kind": (r[0] or "").strip(),
                    "entity": re.sub(r"[^a-z0-9]+", "", (r[1] or "").lower()),
                    "days_ago": float(r[2] or 0),
                })
    except Exception:
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


# ── Publish-time rejection feedback (2026-08-15) ─────────────────────
# ★ THE DEADLOCK THIS BREAKS — READ BEFORE LOOSENING ANY THRESHOLD HERE.
#
# recent_lead_ledger() above reads `success = TRUE`. So a lead the PUBLISH gate
# REFUSES is invisible to every variety guard in editorial_decision: the desk
# re-elects it next slot, the gate refuses it again, and the feed goes silent
# while a full board of eligible leads sits underneath it. Selection and
# publication each behave correctly in isolation; the loop between them is open.
#
# Measured 2026-08-15 off /api/v1/linkedin-quad/status — 8 consecutive slots,
# 2026-08-12T20:29Z → 2026-08-15T19:04Z, every one of them:
#     lead_kind=agent_demand  lead_entity=wk202633  success=false
#     error_msg="gate: duplicate opening hook (…) already posted within 5d"
# The dedup_key is WEEK-bucketed (wk202633), so nothing would have changed
# until the ISO week rolled over. Last successful post: 2026-08-12T18:40Z.
#
# This is the THIRD recurrence of the class. See r86e in editorial_decision()
# ("DEADLOCKED the entire LinkedIn feed — 0 quad posts for 7 days") and the
# nLighten stalemate documented in _operator_spotlight_lead(). Both previous
# fixes retuned a threshold. A threshold cannot fix this, because the desk is
# not being too strict — it is being told nothing at all about what happened
# downstream. The durable fix is to close the loop: a rejection is EVIDENCE
# about the lead, so feed it back into selection.
#
# ★ ONLY `gate:`-prefixed errors count. Those are deterministic content
# refusals — the same lead composed again produces the same text and is refused
# again, so retrying is always futile. Transient failures (claimed_in_flight,
# LinkedIn 5xx, token errors) must NEVER suppress a lead: the story is fine and
# it should be retried. Widening this predicate would turn a publisher outage
# into an editorial blackout, which is the failure this function exists to end.
try:
    _PUBLISH_BLOCK_DAYS = max(1, int(
        os.environ.get("MEDIA_PUBLISH_BLOCK_DAYS", "5") or 5))
except Exception:
    _PUBLISH_BLOCK_DAYS = 5
try:
    # 2 = one wasted slot's worth of evidence before standing the lead down.
    # 1 would react to a single transient gate; >2 burns a slot per extra try.
    _PUBLISH_BLOCK_MIN = max(1, int(
        os.environ.get("MEDIA_PUBLISH_BLOCK_THRESHOLD", "2") or 2))
except Exception:
    _PUBLISH_BLOCK_MIN = 2


def recent_publish_blocked_keys(days: int | None = None,
                                threshold: int | None = None) -> set:
    """Normalized entity tails whose lead the PUBLISH gate refused at least
    `threshold` times inside the window — the leads the desk must stop
    re-electing.

    Returns a set of alnum-squashed entity tokens, matching _entity_tail() and
    recent_lead_ledger()'s normalization so the three agree on identity.

    Fail-OPEN (empty set) on any error or when
    MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE=1: a bad read here must relax the
    guard, never dark-hold the feed."""
    if (os.environ.get("MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE") or "").strip() == "1":
        return set()
    days = _PUBLISH_BLOCK_DAYS if days is None else days
    threshold = _PUBLISH_BLOCK_MIN if threshold is None else threshold
    blocked: set = set()
    c = _conn()
    if c is None:
        return blocked
    try:
        with c.cursor() as cur:
            # NOTE: 'gate:%%' — psycopg2 treats a literal % in the SQL as a
            # parameter marker when args are passed, so it MUST be doubled.
            cur.execute(
                "SELECT LOWER(COALESCE(lead_entity,'')) AS ent, COUNT(*) AS n "
                "  FROM linkedin_quad_posts "
                " WHERE success = FALSE "
                "   AND posted_at > NOW() - make_interval(days => %s) "
                "   AND COALESCE(error_msg,'') LIKE 'gate:%%' "
                "   AND COALESCE(lead_entity,'') <> '' "
                " GROUP BY 1 HAVING COUNT(*) >= %s",
                (int(days), int(threshold)))
            for r in (cur.fetchall() or []):
                ent = re.sub(r"[^a-z0-9]+", "", (r[0] or "").lower())
                if ent:
                    blocked.add(ent)
    except Exception:
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return blocked


# Policy windows (env-tunable — the point is variety, NOT a new single default):
#   MARKET window  — do not re-lead with the same ENTITY within N days.
#   KIND cooldown  — do not lead with the same content_type N days running.
try:
    _MARKET_WINDOW_DAYS = max(1, int(os.environ.get("MEDIA_ENTITY_WINDOW_DAYS", "14")))
except Exception:
    _MARKET_WINDOW_DAYS = 14
try:
    _KIND_COOLDOWN_DAYS = max(1, int(os.environ.get("MEDIA_KIND_COOLDOWN_DAYS", "2")))
except Exception:
    _KIND_COOLDOWN_DAYS = 2

# r-agent-demand (2026-07-17): per-kind SCORE SEEDS — the hand-set magnitude
# coefficients the lead builders apply BEFORE the learned (engagement /
# topic-mix) weights kick in. This is the anti-starvation lever, not a gate:
# agent_demand is seeded HIGH (0.60 per distinct agent → ~273 agents scores
# ~164, out-ranking every DCPI lead) because it is the one dataset that MOVES
# and moves UP while DCPI is flat; the dcpi kinds are nudged DOWN one more
# notch (build 0.30→0.25, mover 1.2→1.0) so agent_demand + hyperscaler_deal
# own the rotation until the index shows real movement. Revisit when DCPI's
# computed_at history shows genuine WoW deltas again.
_KIND_SCORE_SEED = {
    "agent_demand": 0.60,
    "dcpi_build":   0.25,
    "dcpi_mover":   1.0,
    # operator_spotlight (2026-08-07): the daily OPERATOR feature. A dependable
    # floor score — high enough that the desk ALWAYS has a positive, non-
    # repeating story to publish (the lane rotates to a fresh operator each
    # day), so "post: false, slate empty" starvation cannot silence the feed
    # for days again; but a genuine big DCPI mover (score = |Δ|·1.0, so a 5+pt
    # swing scores 5+) or a real agent-demand story still outranks it.
    "operator_spotlight": 0.90,
}

# r-agent-demand (2026-07-17): per-kind COOLDOWN overrides on top of the global
# _KIND_COOLDOWN_DAYS. agent_demand's source data refreshes WEEKLY (retention
# ip_cohort completes on ISO-week boundaries), so a 4-day cooldown means at
# most ~2 agent-demand leads per week — the high seed above never turns into
# the same numbers posted daily.
_KIND_COOLDOWN_OVERRIDES = {
    "agent_demand": 4,
}


def _kind_cooldown_days(kind: str) -> int:
    """Cooldown window for a lead kind (per-kind override, else the global)."""
    try:
        return int(_KIND_COOLDOWN_OVERRIDES.get(kind, _KIND_COOLDOWN_DAYS))
    except Exception:
        return _KIND_COOLDOWN_DAYS


# Bridge from the desk's lead KINDS to the topic-tuner's engagement-weighted
# topic library (media_topic_mix). This is what finally WIRES the dormant tuner
# into selection: a kind whose mapped topic has a higher learned weight gets a
# rotation-weight bump. Unmapped kinds default to neutral. (grep-confirmed: the
# tuner was armed + cron-live but NO poster-path code read media_topic_mix.)
_KIND_TO_TOPIC = {
    # r-agent-demand (2026-07-17): must be mapped or the kind gets NO learned
    # engagement weight and silently loses selection forever (_engagement_weights
    # keys off the mapped topic). Rides "industry_pulse" — it IS the pulse of who
    # is actually building, read off real demand rather than a static index.
    "agent_demand":     "industry_pulse",
    "dcpi_build":       "dcpi_verdict",
    "dcpi_mover":       "verdict_shift",
    "deal":             "ma_transaction",
    "interconnection":  "grid_alert",
    "tenant":           "hyperscaler_deal",
    "new_facility":     "facility_news",
    "capability_launch": "ai_citation",
    "data_milestone":   "industry_pulse",
    # r-milestone (2026-08-07): must be mapped or the kind gets NO learned
    # engagement weight and silently loses selection forever (the partially-
    # registered-kind failure this codebase keeps hitting). Rides the same
    # tuner unit as data_milestone — both are "a DC Hub number moved" stories.
    "platform_milestone": "industry_pulse",
}


def _topic_mix_weights() -> dict:
    """kind -> engagement weight (normalized ~1.0 mean) from media_topic_mix.
    Empty dict when the tuner hasn't written a mix yet → callers treat every
    kind as neutral 1.0 (variety still driven by the ledger + kind cooldown)."""
    try:
        from routes.media_topic_tuner import current_topic_mix
        mix = current_topic_mix() or []
    except Exception:
        return {}
    if not mix:
        return {}
    by_topic = {m.get("topic"): float(m.get("weight") or 0) for m in mix}
    if not by_topic:
        return {}
    _vals = [v for v in by_topic.values() if v > 0]
    _mean = (sum(_vals) / len(_vals)) if _vals else 0
    if _mean <= 0:
        return {}
    out: dict = {}
    for kind, topic in _KIND_TO_TOPIC.items():
        w = by_topic.get(topic)
        if w is None:
            continue
        # scale to a gentle 0.6x–1.6x band around the mix mean so a strong topic
        # is favored but a weak one is never fully crushed (keeps exploration).
        out[kind] = round(max(0.6, min(1.6, w / _mean)), 3)
    return out


# Newsworthiness bar: the top lead's score must clear this or the editor
# SUPPRESSES the slot (event-driven cadence). Tunable via env.
_NEWSWORTHY_MIN = float(os.environ.get("MEDIA_EDITORIAL_MIN_SCORE", "8") or 8)


# r-capability-slot (rebuild 2026-07-18): slot topics that are RESERVED for the
# evergreen capability data-cards. editorial_decision() ranks GLOBALLY, so the
# capability leads (score 62-64) lost every daily slot to agent_demand (~164) and
# live M&A deals (~98) and never posted. For a reserved slot we restrict the
# ranked slate to capability leads only — the cards win the slot without having to
# out-score the news. Kept as a set so more reserved topics can be added later.
_CAPABILITY_SLOT_TOPICS = {"capability"}


def _is_capability_lead(lead: dict) -> bool:
    """True for a brain_capability_radar lead: an evergreen moat/pillar card
    (kind=='cap_<key>') or a launch/milestone announcement. This is the ONLY
    predicate the reserved-slot restriction uses — it must match exactly the
    kinds capability_radar_leads() emits."""
    kind = (lead or {}).get("kind") or ""
    return kind.startswith("cap_") or kind in ("capability_launch", "data_milestone")


def editorial_decision(slot: str | None = None) -> dict:
    """The brain's desk-editor verdict for this slot.
    Returns {post: bool, lead: {...}|None, reason, ranked: [...] }.
    post=False means SUPPRESS — nothing today clears the newsworthiness bar
    or everything newsworthy was already covered this week."""
    ranked = rank_data_events()
    # r-capability-slot (rebuild 2026-07-18): the reserved capability slot restricts
    # the ranked slate to capability leads ONLY, so a moat/pillar card wins the slot
    # instead of being buried under the higher-scoring news leads. Applied to ONLY
    # the reserved slot(s) — every other slot keeps the full board (do NOT add an
    # `elif slot:` that EXCLUDES caps elsewhere; that would starve the X drumbeat of
    # the capability angle). FALL-THROUGH GUARD: if a bad-data day yields zero
    # capability leads, keep the full board rather than suppress the slot into
    # silence (the reserved slot must never go dark just because the radar was empty).
    _reserved_slate = False
    if slot in _CAPABILITY_SLOT_TOPICS:
        _caps = [l for l in ranked if _is_capability_lead(l)]
        if _caps:
            ranked = _caps
            _reserved_slate = True
    # r86e (2026-06-17): the novelty filter DEADLOCKED the entire LinkedIn feed
    # — 0 quad posts for 7 days (last success 2026-06-10). Two compounding bugs:
    #   (1) greedy SUBSTRING novelty against EVERY whitespace token of the last
    #       9 days of post text — a short entity tail ("amazon", "cheyenne")
    #       matched *something* almost always, so `fresh` was perpetually empty;
    #   (2) when `fresh` was empty the gate SUPPRESSED outright, even with a
    #       score-58 lead on the board (Amazon $10B), so the desk went dark.
    # Fix: exact-token novelty over a 4-day window (was 9), and — critically —
    # fall back to the best newsworthy lead instead of silence. The quad composer
    # (6 story-type rotation + its own 14-day text dedup) already blocks
    # byte-identical repeats, so a strong stale-angle lead beats posting nothing.
    recent_blob = {re.sub(r"[^a-z0-9]+", "", w.lower())
                   for w in _recently_posted_keys(days=4)}
    recent_blob.discard("")

    # 2026-07-03 VARIETY: the DURABLE (kind, entity) ledger is the primary guard
    # now (the old whitespace-token blob is kept as a secondary net). Build:
    #   entity_window  — entities that led a post within MARKET_WINDOW days.
    #   kind_cooldown  — kinds that led a post within KIND_COOLDOWN days.
    # r-agent-demand: the ledger window must cover the LONGEST per-kind
    # cooldown, or an override longer than the global would silently truncate.
    _max_cooldown = max([_KIND_COOLDOWN_DAYS]
                        + list(_KIND_COOLDOWN_OVERRIDES.values()))
    ledger = recent_lead_ledger(days=max(_MARKET_WINDOW_DAYS, _max_cooldown))
    # ★ Leads the PUBLISH gate already refused — see recent_publish_blocked_keys().
    # This must be applied on EVERY selection path below (strict, relaxed,
    # stale-rerun and the reserved-capability bypass); a path that skips it is a
    # path the deadlock comes back through.
    publish_blocked = recent_publish_blocked_keys()
    entity_window = {row["entity"] for row in ledger
                     if row["entity"] and row["days_ago"] <= _MARKET_WINDOW_DAYS}
    # ★ 2026-08-23 — HOW LONG AGO each entity actually led, not merely whether
    # it sits inside the window. `entity_window` is a set: it can answer "is
    # this blocked?" but not "has it rested long enough?", and the relax rung
    # at the bottom of the ladder needs the second question. Normalization
    # matches _entity_tail()/recent_lead_ledger() so all three agree on identity.
    entity_last_led: dict = {}
    for _row in ledger:
        _e = _row.get("entity")
        if not _e:
            continue
        _d = float(_row.get("days_ago") or 0.0)
        if _e not in entity_last_led or _d < entity_last_led[_e]:
            entity_last_led[_e] = _d
    kind_cooldown = {row["kind"] for row in ledger
                     if row["kind"]
                     and row["days_ago"] <= _kind_cooldown_days(row["kind"])}
    # topic-tuner engagement weights (finally wired) — reorder ranked so a
    # kind the learner favors leads first among the still-eligible candidates.
    mix_w = _topic_mix_weights()
    if mix_w:
        ranked = sorted(
            ranked,
            key=lambda l: l.get("score", 0) * mix_w.get(l.get("kind"), 1.0),
            reverse=True)

    # SEMANTIC novelty (rag-wire-media-semantic): build a predicate that flags a
    # candidate whose THEME is a reworded near-repeat of a recent post (cosine >=
    # threshold), catching the class of repeat the token/ledger guards miss (same
    # story, different tokens). Built ONCE over `ranked` (all embeds up front, ≤2
    # Cohere calls), and is a hard no-op if embeddings are unavailable — so every
    # path below degrades cleanly to the existing token/ledger guards.
    _sem_repeat = _semantic_repeat_predicate(ranked)

    def _key_in(lead, blob):
        # market/iso/entity from the dedup_key appears in the given window?
        tail = _entity_tail(lead)
        if not tail:
            return False
        return tail in blob

    def _novelty_reason(lead):
        # "" == FRESH. Otherwise the sub-gate that suppressed it. Making the
        # reason explicit turns post:false from a black box into a diagnosis —
        # it rides in the `ranked` output so "why is media silent" is always
        # answerable without a deploy (2026-08-07: three deploys were spent
        # blind because this verdict was internal).
        kind = lead.get("kind") or ""
        # operator_spotlight self-rotates: its own lane already excludes any
        # operator featured within the entity window, so the desk's CROSS-KIND
        # entity_window + type cooldown are redundant AND actively block the
        # daily operator feature (a different operator each day IS the variety).
        # It still honors the real duplicate-CONTENT guards below.
        _self_rotating = kind == "operator_spotlight"
        ent = _entity_tail(lead)
        # ★ Checked FIRST, and it honors no self-rotation exemption: if the
        # publisher has already refused this lead, no amount of editorial
        # novelty makes re-electing it produce a post.
        if ent and ent in publish_blocked:
            return f"publish_blocked:{ent}"
        if ent and ent in entity_window and not _self_rotating:
            return f"entity_window:{ent}"
        if _key_in(lead, recent_blob):
            return f"recent_text:{ent}"
        if kind in kind_cooldown and not _self_rotating:
            return f"kind_cooldown:{kind}"
        if _sem_repeat(lead):
            return "semantic_repeat"
        return ""

    def _is_novel(lead):
        return not _novelty_reason(lead)

    # Annotate every lead with its novelty verdict so the ranked output is
    # self-diagnosing (attached in-place; harmless to downstream consumers).
    for _l in ranked:
        _l["_novelty"] = _novelty_reason(_l) or "fresh"

    fresh = [l for l in ranked if _is_novel(l)]
    top = fresh[0] if fresh else None

    # Relaxation ladder: if the strict filter left nothing, drop the KIND
    # cooldown first (a fresh ENTITY is more valuable than type-rotation), then
    # fall to the stale-rest path below. This prevents whole-feed deadlock while
    # still preferring a different content type when one is available.
    if top is None and ranked:
        relaxed = [l for l in ranked
                   if _entity_tail(l) not in entity_window
                   and _entity_tail(l) not in publish_blocked
                   and not _key_in(l, recent_blob)
                   and not _sem_repeat(l)]
        if relaxed:
            top = relaxed[0]

    # Rerun-with-rest-period (2026-07-02, operator "it repeats itself"): the
    # old fallback re-posted ranked[0] whenever nothing was novel. A STANDING
    # total (ERCOT's 427 GW queue) never stops out-ranking daily movers, so
    # that fallback posted the same lead ~6 times in a week. A stale lead may
    # now rerun ONLY if it hasn't led a post in the last 12 days; otherwise
    # the slot SUPPRESSES — silent beats repetitive, per the desk's own motto.
    stale_fallback = False
    entity_relaxed = False
    _rest_days = 5
    if top is None and ranked:
        # 2026-07-03: parameterize the rest window (was a hardcoded 12d, which
        # over-starved the feed — the desk deadlocked to 0 posts/7d). Default 5d
        # via MEDIA_EDITORIAL_REST_DAYS so a STRONG stale lead (raw_score >=
        # _NEWSWORTHY_MIN) re-runs sooner; the downstream number-lead +
        # claim-verify gates still apply, so this cannot lower quality.
        try:
            _rest_days = max(1, int(os.environ.get("MEDIA_EDITORIAL_REST_DAYS", "5")))
        except Exception:
            _rest_days = 5
        rest_blob = {re.sub(r"[^a-z0-9]+", "", w.lower())
                     for w in _recently_posted_keys(days=_rest_days)}
        rest_blob.discard("")
        # A stale lead may rerun only if BOTH the text-blob AND the durable
        # (kind,entity) ledger agree it hasn't led recently — so a STANDING lead
        # (ERCOT 427 GW queue, Cheyenne build) can never cycle back inside its
        # entity window even if the shorter rest_blob has aged out.
        for cand in ranked:
            ent = _entity_tail(cand)
            if (cand.get("raw_score", cand.get("score", 0)) >= _NEWSWORTHY_MIN
                    and not (ent and ent in publish_blocked)
                    and not _key_in(cand, rest_blob)
                    and not (ent and ent in entity_window)
                    and not _sem_repeat(cand)):
                top, stale_fallback = cand, True
                break

        # ★★★ 2026-08-23 — THE MISSING RUNG. Read this before tightening it.
        #
        # Every rung above still enforces entity_window: `relaxed` drops the
        # KIND cooldown only, and the stale-rerun path asserts `not (ent and ent
        # in entity_window)` outright. So entity_window was an ABSOLUTE gate
        # with no relaxation anywhere in a ladder whose stated purpose (r86e,
        # 2026-06-17) is "fall back to the best newsworthy lead instead of
        # silence". That promise cannot be kept when entity_window is the gate
        # that binds — which is exactly what happened.
        #
        # MEASURED LIVE 2026-08-23 off /api/v1/brain/media/editorial-decision:
        # post:false, "no novel data event cleared the newsworthiness bar",
        # carrying NINE leads — every one at or above _NEWSWORTHY_MIN, two at
        # 62. Their `_novelty` verdicts were 8 × entity_window and 1 ×
        # publish_blocked. Not one was a scoring failure. Quad slots that week:
        # 08-21 2/4, 08-22 0/4, 08-23 1/4, and the only slot that could fire at
        # all was 16:00, which is exempt via reserved_slot_bypass.
        #
        # WHY IT IS STRUCTURAL, not a thin week: 7 non-capability leads on the
        # board ÷ a 14-day entity window ≈ 3.5 posts/week, against 21
        # non-capability slots/week and a 21/wk pulse floor. No board of that
        # size can meet that cadence behind an absolute 14-day gate. Third
        # recurrence of the class (06-17 r86e; 07-24 reserved_slot_bypass,
        # which fixed the reserved slot ALONE and left the general gate).
        #
        # This rung is LAST — it runs only when silence is the sole remaining
        # outcome — and it relaxes the DURABLE ENTITY WINDOW AND NOTHING ELSE,
        # from _MARKET_WINDOW_DAYS down to the same _rest_days the text gate
        # directly above already uses. It never abolishes the rest period.
        # Every guard that speaks to whether the post would be BAD rather than
        # merely REPEATED is still enforced, unchanged:
        #   * raw_score >= _NEWSWORTHY_MIN — the newsworthiness bar;
        #   * publish_blocked — a lead the publisher keeps REFUSING is not a
        #     novelty question; re-electing it produces nothing but another
        #     refusal (the 2026-08-15 open loop). Never relaxed here;
        #   * _sem_repeat — a reworded near-repeat of a recent post is a BAD
        #     post, not a stale one, so the theme guard is never relaxed;
        #   * rest_blob — the text-token half of the rest check, as above.
        # Net effect: rung 3 and rung 4 differ by exactly one term, the durable
        # window's length. Downstream, claim_breaker + verify_media_text still
        # gate the composed text, so this cannot publish a false number.
        #
        # KILL SWITCH: MEDIA_ENTITY_WINDOW_RELAX_DISABLE=1 restores the old
        # absolute gate without a deploy.
        if top is None and (
                os.environ.get("MEDIA_ENTITY_WINDOW_RELAX_DISABLE") or "").strip() != "1":
            for cand in ranked:
                ent = _entity_tail(cand)
                # An entity absent from the ledger has never led inside the
                # window — treat it as fully rested rather than as 0 days ago,
                # or the sentinel itself becomes the new deadlock.
                # ★ Today this default is UNREACHABLE and is a fail-safe only:
                # this rung differs from the one above by exactly the
                # entity_window term, so it only ever evaluates entities that
                # ARE in entity_window, and entity_window / entity_last_led are
                # built from the same ledger rows. Keep the default anyway — if
                # those two ever stop sharing a source, `0.0` here would make
                # never-posted leads the only ones this rung refuses. It has no
                # test because no reachable input can distinguish it.
                _rested = entity_last_led.get(ent, float("inf")) if ent else float("inf")
                if (cand.get("raw_score", cand.get("score", 0)) >= _NEWSWORTHY_MIN
                        and not (ent and ent in publish_blocked)
                        and not _key_in(cand, rest_blob)
                        and not _sem_repeat(cand)
                        and _rested >= _rest_days):
                    top, entity_relaxed = cand, True
                    logger.info(
                        "editorial: entity-window relaxed — %s/%s led %.1fd ago "
                        "(window %sd, rest %sd); %d leads on the board, none fresh",
                        cand.get("kind"), ent, _rested,
                        _MARKET_WINDOW_DAYS, _rest_days, len(ranked))
                    break

    # r86d: judge the SUPPRESS bar on intrinsic newsworthiness (raw_score), so
    # the engagement weight only re-ORDERS leads — it never floors a genuinely
    # newsworthy lead below the bar nor promotes noise above it. (ranked is
    # already sorted by the weighted score, so `top` is the best-by-reach lead.)
    if top and top.get("raw_score", top.get("score", 0)) >= _NEWSWORTHY_MIN:
        # NOTE: a capability/milestone lead is retired (baseline advanced) only
        # when the quad ACTUALLY posts it (linkedin_quad_daily, on a successful
        # LinkedIn publish) — NOT here. editorial_decision() is also called by
        # previews (marketing_engine, the read endpoints), so marking here would
        # consume the "new source" signal without ever posting it.
        return {
            "post": True,
            "slot": slot,
            "lead": top,
            "reason": (f"{top['kind']} cleared the bar (score {top['score']:.0f} "
                       f">= {_NEWSWORTHY_MIN:.0f})"
                       + ("; stale-lead fallback (no novel event, strong lead)"
                          if stale_fallback else "")
                       # Named in the verdict, not just in a log line: the
                       # operator asked to be able to SEE when this fires.
                       + (f"; entity-window relaxed {_MARKET_WINDOW_DAYS}d → "
                          f"{_rest_days}d rest (no fresh lead on a "
                          f"{len(ranked)}-lead board; silence was the only "
                          "alternative)" if entity_relaxed else "")),
            "stale_fallback": stale_fallback,
            "entity_window_relaxed": entity_relaxed,
            "ranked": ranked[:6],
            "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        }
    # r-capability-slot-unstarve (2026-07-24): the reserved slot exists because
    # evergreen capability cards are BY DESIGN not news — yet the gates above
    # (entity-window / recent-blob / semantic-repeat) judge them "not novel"
    # every day, so the 16:00 slot claimed then suppressed and "What We Shipped"
    # never fired (0 posts since the 07-23 deploy; both days died as
    # claimed_in_flight). Rotation for cap leads is owned by the RADAR's
    # announced/repost_days ledger (brain_capability_radar only emits a card
    # when it is due, and the baseline advances only on a real publish), so the
    # desk's novelty gates are the wrong filter for the reserved slate — take
    # the top-ranked due card instead of going dark. Applies ONLY when the
    # slate was restricted to capability leads; every other slot keeps the full
    # gate chain. kind_cooldown is honored as defense-in-depth (a card that
    # actually LED a post recently steps aside for the next due card).
    if _reserved_slate:
        # publish_blocked is applied to BOTH tiers of this bypass — the whole
        # point of the bypass is to ignore novelty gates, but a card the
        # publisher keeps refusing is not a novelty question, and letting it
        # through here would re-open the deadlock on the reserved slot alone.
        _cap = [c for c in ranked if _is_capability_lead(c)
                and _entity_tail(c) not in publish_blocked]
        _elig = ([c for c in _cap
                  if (c.get("kind") or "") not in kind_cooldown] or _cap)
        if _elig and _elig[0].get("raw_score", _elig[0].get("score", 0)) >= _NEWSWORTHY_MIN:
            _t = _elig[0]
            return {
                "post": True,
                "slot": slot,
                "lead": _t,
                "reason": (f"reserved capability slot: {_t['kind']} taken past the "
                           f"novelty gates (score {_t.get('score', 0):.0f}; rotation "
                           "owned by the radar announced/repost ledger)"),
                "reserved_slot_bypass": True,
                "stale_fallback": stale_fallback,
                "entity_window_relaxed": entity_relaxed,
                "ranked": ranked[:6],
                "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
            }
    return {
        "post": False,
        "slot": slot,
        "lead": None,
        "reason": ("no novel data event cleared the newsworthiness bar this slot "
                   "(event-driven cadence: better silent than repetitive)"),
        # Always present, on every return path: a consumer asking "did the desk
        # relax the window?" must never have to distinguish False from absent.
        "stale_fallback": stale_fallback,
        "entity_window_relaxed": entity_relaxed,
        "ranked": ranked[:6],
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
    }


def lead_prompt_block(lead: dict | None) -> str:
    """Render a lead as a prompt block the generators prepend so the post
    LEADS with this number+trend+so-what."""
    if not lead:
        return ""
    block = (
        "TODAY'S DATA LEAD (open the post with this — number first):\n"
        f"  - Number: {lead.get('headline_number','')}\n"
        f"  - Trend: {lead.get('trend','')}\n"
        f"  - So-what: {lead.get('so_what','')}\n"
        f"  - Source line (optional, after the insight): {lead.get('source_url','')}\n"
    )
    # r86d: append the learned reach signal so the analyst leans toward angles
    # that have been landing (best-effort; empty until engagement data accrues).
    try:
        block += engagement_signal_block()
    except Exception:
        pass
    return block


@media_editorial_bp.route("/api/v1/brain/media/editorial-decision", methods=["GET"])
def editorial_decision_endpoint():
    slot = request.args.get("slot")
    try:
        return jsonify(editorial_decision(slot)), 200
    except Exception as e:
        # Fail-open to post=True so a desk bug never dark-holds the feed; the
        # generators still apply their own dedup + the number gate.
        return jsonify({"post": True, "lead": None,
                        "reason": f"editorial_error_failopen:{type(e).__name__}",
                        "stale_fallback": False,
                        "entity_window_relaxed": False,
                        "ranked": []}), 200


@media_editorial_bp.route("/api/v1/brain/media/data-leads", methods=["GET"])
def data_leads_endpoint():
    try:
        return jsonify({"ok": True, "leads": rank_data_events(),
                        "generated_at": _dt.datetime.utcnow().isoformat() + "Z"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200


@media_editorial_bp.route("/api/v1/brain/media/insight-leads", methods=["GET"])
def brain_insight_leads_endpoint():
    """Preview the brain-insight bridge: the graded, refutation-survived DATA
    findings that WOULD become candidate analyst leads. preview=True shows them
    even while BRAIN_MEDIA_BRIDGE_ENABLED is off, so the operator can review the
    slate before flipping the live gate. Draft-only — nothing here publishes."""
    import os as _os
    try:
        from routes.brain_media_bridge import brain_insight_leads, _enabled, leads_diagnostics
        leads = brain_insight_leads(preview=True)
        _dbg = leads_diagnostics() if request.args.get("debug") in ("1", "true") else None
        return jsonify({
            "ok": True,
            "gate_enabled_live": _enabled(),
            "candidate_count": len(leads),
            "leads": leads,
            "diagnostics": _dbg,
            "note": ("Draft-only candidates. When the gate is live these compete in "
                     "editorial_decision() and still pass claim-verify + "
                     "partner-disparagement guards + human approval before publish."),
            "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


@media_editorial_bp.route("/api/v1/brain/media/operator-lane-debug", methods=["GET"])
def operator_lane_debug_endpoint():
    """★ WHY THIS ENDPOINT EXISTS (2026-08-15).

    The operator lane went absent from every ranked lead and there was no way
    to tell WHICH of three unrelated causes was live — the discovery feeds
    wrote no new buildings (supply), no candidate cleared _MIN_FLEET
    (selection), or every eligible operator was inside the rotation window
    (rotation) — without shipping a deploy to find out. Three causes, three
    different fixes, and the tempting one (lower a threshold) is wrong for two
    of them. This answers it from outside instead.

    READ-ONLY and admin-gated. Reports the counts, the per-candidate
    added/fleet numbers with a verdict each, the exclude set the lane actually
    applies, and whether the lane returns a lead right now.
    """
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_only"}), 403
    payload: dict = {"ok": True,
                     "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
                     "lane_disabled": (os.environ.get("MEDIA_OPERATOR_LANE_DISABLE")
                                       or "").strip() == "1",
                     "entity_window_days": _MARKET_WINDOW_DAYS}
    c = _conn()
    if c is None:
        payload["ok"] = False
        payload["error"] = "no_db_connection"
        return jsonify(payload), 200

    # ★ The veto is reported by CAUSE, not as one boolean. "We featured this
    # operator on Tuesday" and "another publisher's prose contained its name"
    # are different problems with opposite fixes, and reporting their union as
    # `rotation_blocked` re-created exactly the ambiguity this endpoint exists
    # to remove. The lane calls the same function with the same connection, so
    # the split reported here is the split actually applied.
    led: set = set()
    txt: set = set()
    try:
        led, txt, xmeta = _operator_exclusion_parts(c)
        payload["exclusion_text_mode"] = xmeta.get("mode")
        payload["exclusion_raw_text_tokens_n"] = xmeta.get("raw_text_tokens_n")
        payload["exclusion_known_operators_n"] = xmeta.get("known_operators_n")
        if xmeta.get("error"):
            payload["exclusion_text_error"] = xmeta["error"]
    except Exception as e:  # noqa: BLE001
        payload["exclusion_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    featured = led | txt
    payload["exclusion_tokens_n"] = len(featured)
    payload["exclusion_ledger_n"] = len(led)
    payload["exclusion_text_n"] = len(txt)
    # Sample the two SEPARATELY — a merged sorted() sample is all digits and
    # says nothing about either cause.
    payload["exclusion_ledger_sample"] = sorted(led)[:40]
    payload["exclusion_text_sample"] = sorted(txt)[:40]
    payload["exclusion_tokens_sample"] = sorted(featured)[:60]
    try:
        from routes.operator_spotlight import spotlight_diagnostics
        # The lane's own rotation filter runs on the RETURNED candidate, so the
        # diagnostic is taken unfiltered and the rotation verdict is reported
        # per candidate below — otherwise "excluded" and "ineligible" blur.
        diag = spotlight_diagnostics(c)
        for row in (diag.get("portfolio_candidates") or []) + \
                   (diag.get("deal_candidates") or []):
            _toks = {_norm_entity(row.get("operator") or ""),
                     _norm_entity(row.get("key") or "")} - {""}
            row["blocked_by_ledger"] = bool(_toks & led)
            row["blocked_by_text"] = bool(_toks & txt)
            row["rotation_blocked"] = bool(row["blocked_by_ledger"]
                                           or row["blocked_by_text"])
        payload["diagnostics"] = diag
    except Exception as e:  # noqa: BLE001
        payload["diagnostics_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass

    # Ground truth: what the lane returns right now, same call rank_data_events
    # makes. If this is None while a candidate above is eligible AND not
    # rotation_blocked, the bug is in the wrapper, not the data.
    try:
        lead = _operator_spotlight_lead()
        payload["lane_returns_lead"] = bool(lead)
        payload["lead"] = lead
    except Exception as e:  # noqa: BLE001
        payload["lane_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return jsonify(payload), 200


@media_editorial_bp.route("/api/v1/brain/media/linkedin-engagement-scoreboard", methods=["GET"])
def engagement_scoreboard_endpoint():
    """r86d: which angles actually earn reach. Drives the desk's bandit and is
    the honest 'is the media engine landing?' surface (reach, not post count)."""
    try:
        eng = engagement_by_kind()
        weights = _engagement_weights(eng)
        ranked = sorted(eng.items(), key=lambda kv: kv[1].get("eng_rate", 0), reverse=True)
        return jsonify({
            "ok": True,
            "by_kind": [
                {"kind": k, **v, "score_weight": weights.get(k, 1.0)}
                for k, v in ranked
            ],
            "best_angle": (ranked[0][0] if ranked else None),
            "note": ("eng_rate = (clicks+likes+comments+shares)/impressions over 45d; "
                     "likes/comments require the r_organizational_social_feed scope "
                     "(impressions+clicks are live now)."),
            "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
