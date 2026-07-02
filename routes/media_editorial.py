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

logger = logging.getLogger(__name__)
media_editorial_bp = Blueprint("media_editorial", __name__)


# ── Shared ANALYST voice ─────────────────────────────────────────────
# The generators embed this. The contract: lead with a NUMBER + the TREND
# (vs last week / ISO peers) + the SO-WHAT for a site-selection or capex
# decision. Promotion is demoted to a single optional source line. This is
# the spec that turns "DC Hub is the authority" marketing into analyst
# intelligence the industry actually comes back to.
ANALYST_VOICE = """You are a senior data-center infrastructure analyst writing for an audience of site-selection leads, hyperscaler capacity planners, developers, and investors. Your reputation rests on being EARLY and RIGHT with numbers, not on promotion.

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
- End with ONE neutral source line that also names the CATEGORY, so a first-time reader learns exactly what DC Hub is and why an analyst would trust it: "Source: DC Hub, the live infrastructure data layer for AI agents (live power, grid, fiber, gas, tenants and 21,000+ facilities, MCP-native), updated daily. dchub.cloud". It comes AFTER the insight, never before. Keep the BODY pure analysis: no "we are the authority", no "the only live source", no brand-pillar speech. The positioning lives ONLY in that single source line, never in the argument.
- A CTA is OPTIONAL and at most one short line; insight always precedes any link.
- 2-3 topical hashtags max (e.g. #DataCenter #GridCapacity #DCPI). Not five.
- Forbidden words: delve, moreover, in essence, unleash, game-changer, revolutionize, thrilled, excited. No em-dashes. At most one emoji and only if it genuinely adds.
- Do not reuse a hook, claim, or market you have used recently. If the only thing to say is something you said this week, say nothing."""


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
    internal so tier-gating (which is UA/loopback-aware) returns full data."""
    try:
        import requests
        r = requests.get(
            f"http://localhost:8080{path}",
            timeout=timeout,
            headers={"User-Agent": "dchub-internal-editorial/1.0",
                     "X-Internal-Request": "1"},
        )
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


# ── The number gate ──────────────────────────────────────────────────
_YEAR_ONLY = re.compile(r"^\D*(?:19|20)\d{2}\D*$")
_HAS_METRIC = re.compile(
    r"\d[\d,\.]*\s*(?:%|pts?|GW|MW|kW|bps|x|×|"
    r"billion|million|B\b|M\b|markets?|facilit|deals?|MGD|gal|"
    r"months?|weeks?|days?|points?|\$)|"
    r"\$\s*\d|\d[\d,\.]*\s*(?:per|/)",
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
            "dedup_key": f"dcpi_mover:{str(mk).lower()}",
            "score": abs(d) * 1.2,
        })

    # 1b) Top DCPI build market — reliable lead even when WoW deltas are null
    # (movers depends on computed_at history that DCPI re-stamps, so it is
    # frequently empty). The leading excess-power score is itself a real,
    # ownable number; the novelty filter in editorial_decision() suppresses it
    # if that market was already featured this week (the 'Cheyenne always wins'
    # trap the audit flagged).
    tb = sig.get("top_build_markets") or []
    if tb:
        b = tb[0]
        ex = _num(b.get("excess"))
        mk = b.get("market") or b.get("slug")
        if ex is not None and mk:
            leads.append({
                "kind": "dcpi_build",
                "headline_number": f"{mk} leads the DCPI excess-power index at {ex:.0f}/100",
                "trend": f"the strongest excess-power headroom of any tracked market (constraint {(_num(b.get('constraint')) or 0):.0f})",
                "so_what": f"{mk} sits at the top of the build shortlist on available power — but verify time-to-power before committing.",
                "source_url": build_public_url("dcpi", b.get('slug','')),
                "dedup_key": f"build:{str(mk).split(',')[0].lower().strip()}",
                "score": (ex or 0) * 0.45,
            })

    # 2) M&A deal of the week — largest disclosed transaction value.
    # value is stored in $M as `value_m` (verified: KKR/Nvidia value_m=10000).
    best_deal, best_val = None, 0.0
    for dl in (sig.get("recent_deals") or []):
        v = _num(dl.get("value_m") or dl.get("value") or dl.get("value_usd"))
        if v and v > best_val:
            best_deal, best_val = dl, v
    if best_deal and best_val > 0:
        # value is stored in $M in most rows; render sensibly.
        bv = best_val
        val_str = (f"${bv/1000:.1f}B" if bv >= 1000 else f"${bv:.0f}M")
        buyer = best_deal.get("buyer") or best_deal.get("acquirer") or "an operator"
        seller = best_deal.get("seller") or best_deal.get("target") or ""
        pair = f"{buyer}/{seller}" if seller else buyer
        leads.append({
            "kind": "deal",
            "headline_number": f"{val_str} data-center transaction: {pair}",
            "trend": "the largest disclosed DC deal in the tracker this week",
            "so_what": "capital is repricing power-rich sites — watch the comparable markets.",
            "source_url": "https://dchub.cloud/transactions",
            "dedup_key": f"deal:{str(buyer).lower()}:{str(seller).lower()}",
            # r86d: cap raised 60->120 so marquee deals keep decisive magnitude
            # (a $50B deal must out-rank a $10B one even after the 0.7-1.3x weight).
            "score": min(120.0, 18.0 + bv / 250.0),
        })

    # 2b) Tenant intelligence — the uncontested moat (project_dchub_competitive_moat):
    # per-facility occupier footprint no analyst PDF / directory publishes. No
    # tenant-rollup endpoint exists, so query facility_tenants directly; fully
    # defensive — any failure → no tenant lead, no harm to the rest of the slate.
    try:
        import psycopg2 as _pg
        _tc = _pg.connect(os.environ.get("DATABASE_URL"))
        _tcur = _tc.cursor()
        _tcur.execute(
            "SELECT tenant_name, COUNT(DISTINCT facility_id) AS c, "
            "       COALESCE(SUM(estimated_mw), 0) AS mw "
            "FROM facility_tenants "
            "WHERE tenant_name IS NOT NULL AND tenant_name <> '' "
            "GROUP BY tenant_name ORDER BY c DESC LIMIT 3"
        )
        _trows = _tcur.fetchall()
        _tcur.close(); _tc.close()
        if _trows and _num(_trows[0][1]) and _num(_trows[0][1]) >= 5:
            _t1, _c1, _mw1 = _trows[0][0], int(_trows[0][1]), _num(_trows[0][2]) or 0
            _runners = ", ".join(f"{r[0]} ({int(r[1])})" for r in _trows[1:3] if r and r[0])
            _mwline = f" (~{_mw1:,.0f} MW of tracked leased capacity)" if _mw1 > 0 else ""
            leads.append({
                "kind": "tenant",
                "headline_number": f"{_t1} is the most-tracked data-center tenant in DC Hub — present at {_c1} facilities{_mwline}",
                "trend": (f"ahead of {_runners} — " if _runners else "") +
                         "the per-occupier footprint no analyst PDF or directory publishes",
                "so_what": f"hyperscaler concentration IS the forward demand signal — where {_t1} clusters, "
                           "power headroom and land tighten next; price the comparable markets now.",
                "source_url": "https://dchub.cloud/research?series=tenant",
                "dedup_key": f"tenant:top:{str(_t1).lower()}",
                "score": 14.0 + min(20.0, _c1 * 0.4),
            })
    except Exception as _te:
        logger.warning("[editorial] tenant lead failed: %s", str(_te)[:160])

    # 3) Interconnection queue — the clearest 'time-to-power' trend (ungated).
    snap = _internal("/api/v1/interconnection-queue/snapshot")
    by_iso = snap.get("by_iso") or []
    top_iso = None
    for row in by_iso:
        g = _num(row.get("queued_load_total_gw"))
        if g and (top_iso is None or g > _num(top_iso.get("queued_load_total_gw") or 0)):
            top_iso = row
    if top_iso:
        g = _num(top_iso.get("queued_load_total_gw")) or 0
        iso = top_iso.get("iso") or "an ISO"
        tot = _num((snap.get("totals") or {}).get("queued_load_gw"))
        share = f", {100*g/tot:.0f}% of all US queued load" if tot and tot > 0 else ""
        leads.append({
            "kind": "interconnection",
            "headline_number": f"{iso}'s interconnection queue holds {g:.0f} GW of requested load{share}",
            "trend": f"queue depth signals multi-year time-to-power in {iso}",
            "so_what": "new large loads in this ISO face a long energization wait — price the delay into the site decision.",
            "source_url": top_iso.get("source_url") or "https://dchub.cloud/grid-intelligence",
            "dedup_key": f"queue:{str(iso).lower()}",
            "score": min(50.0, g / 12.0),
        })

    # 4) Largest new facility surfaced in the last 24h.
    nf = sig.get("new_facilities_24h") or []
    big_fac = None
    for f in nf:
        mw = _num(f.get("mw") or f.get("capacity_mw") or f.get("total_mw"))
        if mw and (big_fac is None or mw > _num(big_fac.get("_mw") or 0)):
            f["_mw"] = mw
            big_fac = f
    if big_fac and _num(big_fac.get("_mw")):
        mw = _num(big_fac["_mw"])
        name = big_fac.get("name") or big_fac.get("operator") or "A new facility"
        loc = big_fac.get("state") or big_fac.get("country") or ""
        leads.append({
            "kind": "new_facility",
            "headline_number": f"{name}: {mw:.0f} MW {('in '+loc) if loc else ''} just entered the tracker",
            "trend": "fresh capacity added to the live facility map in the last 24h",
            "so_what": f"another {mw:.0f} MW of demand on {loc or 'the local grid'} — watch the headroom there.",
            "source_url": "https://dchub.cloud/map",
            "dedup_key": f"facility:{str(name).lower()}",
            "score": min(35.0, 6.0 + mw / 30.0),
        })

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
            cur.execute(
                "SELECT LOWER(COALESCE(content,'')) FROM social_media_posts "
                "WHERE status='published' AND publish_platform='linkedin' "
                "AND published_at >= %s ORDER BY published_at DESC LIMIT 60",
                (cutoff,))
            recent = " || ".join(r[0] for r in (cur.fetchall() or []) if r and r[0])
    except Exception:
        recent = ""
    finally:
        try: c.close()
        except Exception: pass
    # A lead is 'already covered' if its market/iso/entity token appears in
    # recent post text. Cheap, robust, no embedding dependency.
    return {tok for tok in recent.split()} if recent else keys


# Newsworthiness bar: the top lead's score must clear this or the editor
# SUPPRESSES the slot (event-driven cadence). Tunable via env.
_NEWSWORTHY_MIN = float(os.environ.get("MEDIA_EDITORIAL_MIN_SCORE", "8") or 8)


def editorial_decision(slot: str | None = None) -> dict:
    """The brain's desk-editor verdict for this slot.
    Returns {post: bool, lead: {...}|None, reason, ranked: [...] }.
    post=False means SUPPRESS — nothing today clears the newsworthiness bar
    or everything newsworthy was already covered this week."""
    ranked = rank_data_events()
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

    def _key_in(lead, blob):
        # market/iso/entity from the dedup_key appears in the given window?
        tail = (lead.get("dedup_key") or "").split(":", 1)[-1]
        tail = re.sub(r"[^a-z0-9]+", "", tail.lower())
        if not tail:
            return False
        return tail in blob

    def _is_novel(lead):
        return not _key_in(lead, recent_blob)

    fresh = [l for l in ranked if _is_novel(l)]
    top = fresh[0] if fresh else None

    # Rerun-with-rest-period (2026-07-02, operator "it repeats itself"): the
    # old fallback re-posted ranked[0] whenever nothing was novel. A STANDING
    # total (ERCOT's 427 GW queue) never stops out-ranking daily movers, so
    # that fallback posted the same lead ~6 times in a week. A stale lead may
    # now rerun ONLY if it hasn't led a post in the last 12 days; otherwise
    # the slot SUPPRESSES — silent beats repetitive, per the desk's own motto.
    stale_fallback = False
    if top is None and ranked:
        rest_blob = {re.sub(r"[^a-z0-9]+", "", w.lower())
                     for w in _recently_posted_keys(days=12)}
        rest_blob.discard("")
        for cand in ranked:
            if (cand.get("raw_score", cand.get("score", 0)) >= _NEWSWORTHY_MIN
                    and not _key_in(cand, rest_blob)):
                top, stale_fallback = cand, True
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
                          if stale_fallback else "")),
            "ranked": ranked[:6],
            "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        }
    return {
        "post": False,
        "slot": slot,
        "lead": None,
        "reason": ("no novel data event cleared the newsworthiness bar this slot "
                   "(event-driven cadence: better silent than repetitive)"),
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
