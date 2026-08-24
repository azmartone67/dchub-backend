"""
content_enqueue.py — Phase r61 (2026-05-25).

Constant outreach drumbeat. The publishers in content_publisher.py
(LinkedIn 6h, Twitter 6h gated, Bluesky 6h gated) pull from
social_media_posts WHERE status='approved'. Until r61, that table
only got fresh rows when a press release was published (1/day max).

This module fills the queue continuously:

  POST /api/v1/content-engine/enqueue
       — Reuses the topic builders from linkedin_quad_daily +
         the narrative arc to drop 3 rows (linkedin + twitter +
         bluesky) on every call. Idempotent on the shaped content's
         normalized opening within a 7-day window (2026-07-11: was a
         broken 4h topic_key LIKE that never matched — see
         _DEDUP_WINDOW_HOURS), so the 2h cron keeps the queue primed
         without flooding it with identical copies.

  GET  /api/v1/content-engine/status
       — Snapshot: queue depth per platform, posts published in
         last 24h, last enqueue timestamp.

Cron pairs with .github/workflows/content-enqueue-hourly.yml which
fires every 2h. Net effect: queue stays primed; publishers always
have fresh material; LinkedIn fires its own 4-slot rotation in
parallel; Twitter/Bluesky drain the queue at their 6h cadence.

Goal: 24/7 cadence with no operator hand-holding. Set
TWITTER_PUBLISHER_ENABLED=true once the dev-portal app is in a
Project + BLUESKY_HANDLE/BLUESKY_APP_PASSWORD env vars to unlock
those two channels.
"""
from __future__ import annotations
from routes.url_registry import build_public_url

import datetime
import json
import os
import random

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write
from ai_surface_canon import canon_text


content_enqueue_bp = Blueprint("content_enqueue", __name__)


# Don't enqueue the same post content twice within this window.
#
# 2026-07-11 (LinkedIn queue-drain audit): was 4h keyed on a "slug:VERDICT"
# topic_key matched with content LIKE '%slug:VERDICT%' — a substring that NEVER
# appears in any shaped post (content carries ".../dcpi/<slug>" and "rates
# BUILD", never "slug:BUILD"), so the dedup NEVER fired and the 2h cron
# enqueued the SAME market copy 8-10x/week (Papillion alone: 8 identical rows).
# The publish-side duplicate gate (content_publisher._is_recent_linkedin_
# duplicate, 7d window on the normalized opening) then correctly refused every
# repeat, and the drain marked them 'failed' — 21 bogus "failures" in 7 days.
# Fix: dedup on the SAME key the publish gate uses (normalized content
# opening) over the SAME 7-day window, so we never enqueue a row the
# publisher is guaranteed to refuse. A market whose score CHANGES gets a new
# opening (the score is embedded in the first sentence) and re-enqueues fine.
_DEDUP_WINDOW_HOURS = 168


def _content_dedup_key(content: str, n: int = 60) -> str:
    """First `n` chars of whitespace-normalized content — same style of key as
    the publish-side duplicate gate (first-80 normalized), so enqueue-side and
    publish-side can never disagree about what "the same post" means."""
    return " ".join((content or "").split())[:n]


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards so content chars can't wildcard-match."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _fetch_narrative_arc() -> dict | None:
    """Hit the local narrative-arc endpoint. Best-effort."""
    try:
        import urllib.request
        base = (os.environ.get("DCHUB_INTERNAL_API")
                or "http://localhost:8080")
        with urllib.request.urlopen(
            f"{base}/api/v1/narrative/current", timeout=8
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _pick_dcpi_mover() -> dict | None:
    """Random recent market for content. Reuses the same SQL as
    linkedin_quad_daily but split into a tiny helper.

    Positive-results editorial policy (2026-07-02, operator directive): the
    social feed showcases WHERE THE POWER IS — BUILD markets, capacity wins,
    platform enhancements. AVOID/CAUTION downgrades stay on the live DCPI
    pages and in subscriber alerts (that's the product), but the media feed
    never leads with doom commentary. Prefer the highest-excess BUILD market
    of the week over a random one so the post always has a real bragging
    right (top headroom, not a coin flip)."""
    c = _db_conn()
    if not c: return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT market_name, market_slug, verdict, iso,
                       excess_power_score, constraint_score
                  FROM market_power_scores
                 WHERE published = TRUE
                   AND computed_at > NOW() - INTERVAL '7 days'
                   AND verdict = 'BUILD'
                   AND excess_power_score IS NOT NULL
                 ORDER BY excess_power_score DESC
                 LIMIT 10
            """)
            top = cur.fetchall()
            if not top: return None
            import random as _rnd
            r = _rnd.choice(top)
            return {
                "name":   r[0],
                "slug":   r[1],
                "verdict": r[2],
                "iso":    r[3],
                "excess": float(r[4] or 0),
                "constraint": float(r[5] or 0),
            }
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _pick_recent_news() -> dict | None:
    """Recent industry news for contrarian takes."""
    c = _db_conn()
    if not c: return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT title, source, url, published_date
                  FROM news
                 WHERE published_date > NOW() - INTERVAL '5 days'
                   AND (LOWER(title) LIKE '%%data center%%'
                        OR LOWER(title) LIKE '%%hyperscale%%'
                        OR LOWER(title) LIKE '%%ai capex%%'
                        OR LOWER(title) LIKE '%%power grid%%'
                        OR LOWER(title) LIKE '%%interconnect%%')
                 ORDER BY RANDOM()
                 LIMIT 1
            """)
            r = cur.fetchone()
            if not r: return None
            return {"title": r[0], "source": r[1], "url": r[2],
                    "date": r[3]}
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


# ── Per-platform content shapers ────────────────────────────────────

def _shape_linkedin(mover: dict, arc: dict | None) -> str:
    """r47.38 (2026-05-26): narrative LinkedIn post, not a status-line spam.

    Previous version emitted '📍 Chantilly · PJM · DCPI verdict: AVOID' which
    is unreadable to humans + unactionable for journalists / prospects. User
    flagged it as 'spam and ugly texts on linkedin' during the dchub-media
    inspection. New shape:
      • Leads with the *reason this market shifted* (1 sentence of context)
      • One sentence of investor-relevant implication
      • One data point + live page link
      • Stripped #spam hashtags down to 2-3 relevant ones
    """
    name    = mover.get('name', '?')
    iso     = mover.get('iso', '?')
    excess  = mover.get('excess') or 0
    constr  = mover.get('constraint') or 0
    slug    = mover.get('slug', '')

    # Positive-results policy (2026-07-02): the mover is always a BUILD-rated
    # market with real headroom. The post is a capacity FIND for the reader —
    # where the power is, what the numbers say, how to verify — never a
    # downgrade commentary.
    opener = (f"{name} ({iso}) rates BUILD on the DC Hub Power Index this "
              f"week, with an Excess Power score of {excess:.0f}/100 — one "
              f"of the strongest grid-headroom readings we track.")
    implication = (f"For teams screening {iso} for AI capacity, that "
                   f"combination — headroom {excess:.0f}/100 against a grid "
                   f"constraint of just {constr:.0f}/100 — usually means "
                   f"shorter interconnection timelines and real negotiating "
                   f"leverage on power contracts. Markets like this are why "
                   f"the shortlist should be rebuilt from live grid data, "
                   f"not last quarter's PDF.")

    arc_line = ""
    if arc:
        arc_title = (arc.get("arc") or "")[:80]
        if arc_title:
            arc_line = f"\n\nContext: {arc_title}"

    return (
        f"{opener}\n\n"
        f"{implication}\n\n"
        f"Daily-refreshed score, methodology + sources on the live page: "
        # r-fixpack (2026-07-02): registry chokepoint, not a raw f-string
        # URL — tests/test_url_registry_chokepoint.py fails CI on the raw
        # form (this line was the red gauntlet on main).
        f"{build_public_url('dcpi', slug)}"
        f"{arc_line}\n\n"
        f"#datacenter #DCPI #{iso.replace('-','').lower()}"
    )


# 2026-07-17 X-editorial fix: _shape_twitter and _shape_bluesky DELETED. They
# were the last copies of the retired '<City> (<ISO>) rates BUILD on the DC Hub
# Power Index' template — dead code since the 2026-07-15 composer rewrite, but
# the byte-identical template they minted was still draining to X from queued
# legacy rows (all 7 X posts in the 14d audit, incl. a Cheyenne repeat AFTER
# the 07-14 LinkedIn-only diversity fix). content_publisher's retired-template
# gate now terminal-rejects those queued rows at publish time; the short
# platforms speak ONLY via _shorten_analytical over the desk-led composed post.


# Editorial-desk lead kind -> composer slot topic. The drumbeat rides the SAME
# desk selection the LinkedIn quad uses (kinds + per-kind cooldowns + the
# 14-day (kind, entity) ledger), so X/Bluesky inherit the full anti-repeat
# machinery instead of a static DCPI template.
_LEAD_KIND_TO_SLOT_TOPIC = {
    "dcpi_mover":       "dcpi_mover",
    "dcpi_build":       "dcpi_mover",
    "deal":             "hyperscaler_deal",
    "tenant":           "hyperscaler_deal",
    "agent_demand":     "agent_demand",
    "interconnection":  "industry_pulse",
    "new_facility":     "industry_pulse",
    "capability_launch": "industry_pulse",
    "data_milestone":   "industry_pulse",
    # r-milestone (2026-08-07): the NUMBERS lane rides the same slot topic as
    # data_milestone. Unmapped kinds fall back to "dcpi_mover" here, which would
    # hand a "4,000,000+ requests served" lead to the DCPI composer.
    "platform_milestone": "industry_pulse",
    # r-operator: the operator lane was likewise unmapped and defaulting to
    # dcpi_mover on the X/Bluesky drumbeat.
    "operator_spotlight": "hyperscaler_deal",
}


def _lead_entity_token(lead) -> str:
    """Normalized entity token from a lead's dedup_key ('kind:entity[:x]') —
    mirrors media_editorial._entity_tail / linkedin_quad_daily._lead_entity_from
    so all three ledgers key identically. '' when unknown."""
    import re as _re
    if not isinstance(lead, dict):
        return ""
    tail = (lead.get("dedup_key") or "").split(":", 1)[-1]
    return _re.sub(r"[^a-z0-9]+", "", tail.lower())


def _compose_linkedin_analytical(mover: dict, arc: dict | None):
    """2026-07-15 (operator: 'isn't thinking like an analyst'): route the LinkedIn
    drumbeat through the BRAIN composer — the same Fable analyst that writes the
    quad-daily posts (the good 'curtailment is a price signal' read) — instead of a
    formulaic f-string template that hardcodes 'rates BUILD' regardless of the
    market's real verdict (the BUILD-text vs CAUTION-card contradiction the operator
    flagged). Returns analytical, verdict-honest text; None to SKIP this cycle when
    the composer judges nothing new (silence beats template filler). Falls back to
    the old template ONLY on a hard composer error (no API key / import), so the
    drumbeat never goes fully dark — but that is the exception, not the default."""
    try:
        from routes.linkedin_content_engine import compose_story_post
        # 2026-07-17 X-editorial fix: the drumbeat now asks the EDITORIAL DESK
        # what to lead with — the same selection the quad uses (kinds, per-kind
        # cooldowns, the durable 14-day (kind, entity) ledger, agent_demand
        # included) — replacing the hardcoded Tue/Fri agent_demand/dcpi_mover
        # rotation that ignored the anti-repeat machinery entirely. Desk says
        # SUPPRESS → the whole drumbeat slot (LinkedIn + the X/Bluesky posts
        # derived from it) stays silent; silence beats a repeat.
        _lead = None
        try:
            from routes.media_editorial import editorial_decision
            _ed = editorial_decision("content_drumbeat") or {}
            if not _ed.get("post"):
                return None
            _lead = _ed.get("lead")
        except Exception:
            _lead = None  # desk hiccup → composer picks its own angle
        _drum_topic = _LEAD_KIND_TO_SLOT_TOPIC.get(
            (_lead or {}).get("kind") or "", "dcpi_mover")
        composed = compose_story_post(slot_topic=_drum_topic, lead=_lead) or {}
        if composed.get("skip"):
            return None
        text = composed.get("text")
        if text and len(text.strip()) >= 200:
            # 2026-07-16: return the composer's INTENDED card too so the drain
            # attaches the same good branded card the quad uses.
            # 2026-07-17: + the desk lead, so enqueue() stamps (kind, entity)
            # onto the queue rows — that stamp IS the X anti-repeat ledger.
            return {"text": text.strip(),
                    "og_image_url": composed.get("og_image_url"),
                    "lead": _lead}
    except Exception as e:
        try:
            note_swallowed_write("content_enqueue",
                                 where=f"_compose_linkedin_analytical:{type(e).__name__}")
        except Exception:
            pass
    # Composer hard-failure (no API key / import error) → SKIP, never a template.
    # Operator: 'the posts are terrible' — silence beats formulaic filler. The
    # analytical quad path still owns the LinkedIn feed 4x/day either way.
    return None


def _shorten_analytical(li_text, max_chars: int, platform: str = "twitter"):
    """2026-07-15: derive a <=max_chars X/Bluesky post from the composed analytical
    LinkedIn text — the lead analytical sentence(s) + the post's own DCPI link — so
    the short platforms speak in the SAME analyst voice (not the old 'rates BUILD'
    template) at ZERO extra LLM cost. Returns None when there's nothing usable to
    shorten (→ SKIP that platform; silence beats a template).

    2026-07-31 (X-diversity port): SCORE-AWARE. 11 of 16 X rejections in the 14d
    window were 'quality < 0.60' on the mechanically-shortened WIRE text — the
    greedy first-sentences prefix drops the labeled stat / freshness token the
    full LinkedIn post carries further down, so good analyst leads died at the
    drain while the press template sailed through. The shortener now builds
    candidate excerpts (greedy prefix first — the prior behaviour — then each
    later-sentence window) and returns the FIRST whose as_published() wire text
    clears the same _quality_score/QUALITY_MIN gate the drain applies. Scorer
    unavailable → plain prefix (fail-open, exactly the old output). Nothing
    clears → None: the 07-11 rule — never enqueue a row the publisher is
    guaranteed to refuse."""
    import re
    if not li_text:
        return None
    m = re.search(r'https?://\S+', li_text)
    url = (m.group(0).rstrip('.,)') if m else "")
    # analytical body = everything before the first URL / footer / hashtags / stamp
    body = re.split(r'https?://|\n\nSource:|\n\nFull index|\n\n#|\n\nDaily|\n\n\(DC Hub data',
                    li_text)[0].strip()
    budget = max_chars - (len(url) + 2 if url else 0)
    sents = [s for s in re.split(r'(?<=[.!?])\s+', body) if s.strip()]

    def _pack(start: int) -> str:
        out = ""
        for sent in sents[start:]:
            cand = (out + " " + sent).strip()
            if len(cand) > budget:
                break
            out = cand
        return out

    def _wire(text: str) -> str:
        return (f"{text}\n\n{url}" if url else text)[:max_chars]

    # Candidate excerpts, prefix-first so index 0 IS the pre-2026-07-31 output.
    cands, _seen = [], set()
    for i in range(len(sents)):
        t = _pack(i)
        if len(t) >= 60 and t not in _seen:
            _seen.add(t)
            cands.append(t)
    if not cands:
        return None

    try:
        from content_publisher import _quality_score, as_published, QUALITY_MIN
    except Exception:
        return _wire(cands[0])  # fail-open: the old greedy-prefix behaviour
    for t in cands:
        w = _wire(t)
        try:
            if _quality_score(as_published(w, platform)) >= QUALITY_MIN:
                return w
        except Exception:
            return _wire(cands[0])  # scorer blew up mid-way: fail-open
    return None


# ── Metrics-showcase template (r64, 2026-05-30) ─────────────────────
# A punchy credibility post that weaves DC Hub's AI-adoption + coverage
# metrics. Distinct from the per-market DCPI shaper above — this is the
# "why agents trust us" post, run on a LOW (≈weekly) cadence so it never
# crowds out the daily DCPI movers. It ends with a /built-for-ai (or
# /ai-capacity-index) URL so it inherits the MANDATORY branded OG card
# from content_publisher._post_to_linkedin (r64 step 1): even though that
# page may lack a scrape-able og:image, the publisher now always attaches
# https://dchub.cloud/api/v1/og/today/<slug>.png.

# Fallback constants — used when the live registry pull fails or omits a
# field. These are the audit-supplied figures (2026-05-30). The live
# /api/v1/ai-agents.json currently exposes `data_coverage.facilities`
# (e.g. "21,418"); the AI-platform / cumulative-request / grid counts are
# not in that payload yet, so they default to these constants until the
# registry surfaces them.
_METRICS_FALLBACK = {
    # HONEST floor (2026-06-02): the prior 97 / 392,743 were the inflated raw
    # counts (internal/probe/transport buckets included). Real recognized external
    # AI platforms ~10 cumulative; real external agent requests ~195K (the honest
    # /api/public/mcp-count external total). These are only a FALLBACK — live data
    # now surfaces honest numbers and max()/or lets it grow from here.
    "ai_platforms": 10,
    "agent_requests": 195000,
    "facilities": 21417,
    "grids": 51,
}

# Stable substring present in every metrics-showcase post (the hook's
# opening clause). Used as the weekly-cadence dedup key — independent of
# the daily DCPI topic_key — so the LOW-cadence slot can be rate-limited
# with a content LIKE lookback. Keep in sync with _shape_linkedin_metrics.
_METRICS_TOPIC_MARKER = "AI agents don't guess about data centers"


def _fetch_dchub_metrics() -> dict:
    """Best-effort live pull of DC Hub expansion metrics for the
    metrics-showcase post. Reads https://dchub.cloud/api/v1/ai-agents.json
    (the public agent-registry doc) and overlays any numeric fields it
    finds onto the audit-supplied fallback constants. NEVER raises — any
    network/parse miss just leaves the fallback in place, so the post
    always renders with credible numbers.
    """
    metrics = dict(_METRICS_FALLBACK)
    try:
        import urllib.request

        def _to_int(v):
            try:
                return int(str(v).replace(",", "").strip())
            except (TypeError, ValueError):
                return None

        url = (os.environ.get("DCHUB_AI_AGENTS_URL")
               or "https://dchub.cloud/api/v1/ai-agents.json")
        req = urllib.request.Request(
            url, headers={"User-Agent": "DCHub-ContentEngine/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            doc = json.loads(r.read().decode("utf-8"))

        # IMPORTANT: coverage metrics only ever RATCHET UP. The fallback
        # constants are the audited floor; the registry doc enumerates only a
        # subset of named ISOs (it omits the utility/BA grids counted in the
        # 51 figure), so a naive override would SHRINK the numbers and make the
        # post less impressive than reality. Use max() so live data can only
        # enrich, never downgrade.
        cov = doc.get("data_coverage") or {}
        fac = _to_int(cov.get("facilities"))
        if fac and fac > 0:
            metrics["facilities"] = max(metrics["facilities"], fac)

        # grids / ISOs — v2 schema lists US ISOs in prose + an
        # `international_markets` array of {country, iso, markets}.
        dcpi = doc.get("dcpi_coverage") or {}
        intl = dcpi.get("international_markets")
        if isinstance(intl, list) and intl:
            intl_grids = {
                (m.get("iso") or "").strip()
                for m in intl if isinstance(m, dict) and m.get("iso")
            }
            # 7 US ISOs (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISO-NE) + the
            # distinct international grid operators enumerated in the doc.
            counted = 7 + len(intl_grids)
            if counted > 0:
                metrics["grids"] = max(metrics["grids"], counted)

        # AI-platform / cumulative-request counts — overlay only if the
        # registry ever starts exposing them (several plausible key names).
        for key, names in (
            ("ai_platforms", ("ai_platforms", "platforms",
                               "unique_ai_platforms", "ai_agent_platforms")),
            ("agent_requests", ("agent_requests", "ai_agent_requests",
                                "cumulative_agent_requests", "tool_calls",
                                "total_requests")),
        ):
            for src in (doc, doc.get("adoption") or {},
                        doc.get("usage") or {}, doc.get("stats") or {}):
                if not isinstance(src, dict):
                    continue
                for n in names:
                    if n in src:
                        iv = _to_int(src.get(n))
                        if iv and iv > 0:
                            metrics[key] = iv
                        break
                else:
                    continue
                break
    except Exception:
        # Fail-open: keep the fallback constants.
        return dict(_METRICS_FALLBACK)
    return metrics


def _shape_linkedin_metrics(arc: dict | None = None) -> str:
    """r64 (2026-05-30): metrics-showcase LinkedIn post.

    Leads with a hook, weaves the live (or fallback) DC Hub adoption +
    coverage numbers as proof, and ends with a /built-for-ai URL so the
    publisher's mandatory-image step attaches a branded OG card. Kept to
    2-3 hashtags to match the de-spammed _shape_linkedin house style.
    """
    m = _fetch_dchub_metrics()
    platforms = m.get("ai_platforms") or _METRICS_FALLBACK["ai_platforms"]
    requests_n = m.get("agent_requests") or _METRICS_FALLBACK["agent_requests"]
    facilities = m.get("facilities") or _METRICS_FALLBACK["facilities"]
    grids = m.get("grids") or _METRICS_FALLBACK["grids"]

    arc_line = ""
    if arc:
        arc_title = (arc.get("arc") or "")[:80]
        if arc_title:
            arc_line = f"\n\nThis week's arc: {arc_title}"

    return (
        "AI agents don't guess about data centers — they query DC Hub.\n\n"
        f"{platforms} AI platforms have now hit our agent endpoints, "
        f"{requests_n:,} cumulative agent requests and counting.\n\n"
        "Why they keep coming back: it's the one data-center intelligence "
        "source an LLM can both read and cite. Every competitor blocks the "
        "crawlers or hides behind a login.\n\n"
        f"What's behind the API: {facilities:,} facilities, live grid "
        f"intelligence across {grids} grids/ISOs, plus fiber, substations, "
        "gas pipelines and water risk — one machine-readable, citable query.\n\n"
        "Built for the agents your team already uses: "
        "https://dchub.cloud/built-for-ai"
        f"{arc_line}\n\n"
        "#AI #DataCenter #MCP"
    )


# ── Agent-acquisition lane (2026-07-03) ─────────────────────────────
# The DCPI-mover + metrics posts above are analyst-voice, aimed at the
# HUMAN LinkedIn feed. This lane is different: an explicit ACQUISITION
# pitch that tells a developer/agent-builder exactly HOW to connect an
# agent to DC Hub in one line — the content that turns a reader into a
# new MCP connection. Runs on a LOW cadence (default 4d) across LinkedIn
# (decision-makers) + Bluesky (dev/AI audience). Kill: env
# DCHUB_AGENT_PITCH_DISABLED=1. Stable marker = weekly-cadence dedup key.
_AGENT_PITCH_MARKER = "Give your AI agent live data-center ground truth"


def _shape_agent_pitch_linkedin(arc: dict | None = None) -> str:
    m = _fetch_dchub_metrics()
    facilities = m.get("facilities") or _METRICS_FALLBACK["facilities"]
    grids = m.get("grids") or _METRICS_FALLBACK["grids"]
    return (
        "Give your AI agent live data-center ground truth — in one line.\n\n"
        "Most agents reason about physical infrastructure from stale "
        "training data. DC Hub is the live, MCP-native data layer they can "
        "query instead:\n\n"
        f"• {facilities:,} facilities across 170+ countries\n"
        f"• real-time grid telemetry across {grids} grids/ISOs\n"
        "• fiber, substations, gas pipelines, water risk, 4,000+ M&A deals\n"
        "• 53 tools, machine-readable + citable, free tier (no card)\n\n"
        "Connect Claude / Cursor / any MCP client:\n"
        "claude mcp add --transport http dchub https://dchub.cloud/mcp\n\n"
        "Or try it live, no signup: https://dchub.cloud/playground\n\n"
        "#AI #MCP #DataCenter #AIagents"
    )


def _shape_agent_pitch_bluesky(arc: dict | None = None) -> str:
    m = _fetch_dchub_metrics()
    facilities = m.get("facilities") or _METRICS_FALLBACK["facilities"]
    return (
        "Give your AI agent live data-center ground truth in one line:\n\n"
        "claude mcp add --transport http dchub https://dchub.cloud/mcp\n\n"
        f"{facilities:,} facilities, live grid telemetry, fiber + gas + M&A "
        "— 53 MCP tools, citable, free tier. Or try live: "
        "https://dchub.cloud/playground"
    )[:300]


# ── Dedup + enqueue ─────────────────────────────────────────────────

def _already_enqueued_recently(platform: str, content: str) -> bool:
    """Did we enqueue this same post (by normalized opening) for this platform
    within the dedup window? Takes the SHAPED content, not a topic key — see
    the 2026-07-11 note on _DEDUP_WINDOW_HOURS for why (the old topic_key
    substring never matched any row, so the check was a no-op).

    Prefix-match on the whitespace-normalized opening: every shaped post
    starts with its market/hook sentence, and the DCPI score embedded there
    makes a genuinely-new story (score moved) produce a new key. Fail-open."""
    key = _content_dedup_key(content)
    if not key:
        return False
    c = _db_conn()
    if not c: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM social_media_posts
                 WHERE platform = %s
                   AND regexp_replace(btrim(COALESCE(content,'')), '\\s+', ' ', 'g') LIKE %s
                   AND created_at > NOW() - (%s || ' hours')::interval
            """, (platform, _like_escape(key) + "%", str(_DEDUP_WINDOW_HOURS)))
            n = (cur.fetchone() or [0])[0]
            return int(n or 0) > 0
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _enqueued_within_days(platform: str, marker: str, days: int) -> bool:
    """r64 (2026-05-30): LOW-cadence dedup. True if a post containing
    `marker` was enqueued for `platform` within the last `days` days,
    REGARDLESS of status (so an already-published weekly post still
    blocks a re-enqueue). Fail-open (returns False) on any DB error so a
    transient blip can't permanently suppress the slot."""
    c = _db_conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM social_media_posts
                 WHERE platform = %s
                   AND content LIKE %s
                   AND created_at > NOW() - (%s || ' days')::interval
            """, (platform, f"%{marker[:80]}%", str(int(days))))
            n = (cur.fetchone() or [0])[0]
            return int(n or 0) > 0
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


_QUEUE_CAP = 50  # per-platform approved backlog beyond which enqueue refuses


def _enqueue_post(content: str, platform: str, og_image: str | None = None,
                  lead_kind: str | None = None,
                  lead_entity: str | None = None) -> int | None:
    """Insert a single approved row. Returns new id or None.

    2026-07-17: `lead_kind`/`lead_entity` stamp WHICH editorial-desk lead the
    row was composed from — the durable (kind, entity) anti-repeat ledger for
    the non-quad platforms (X reads it via _x_lead_recently_used).

    2026-07-16: `og_image` carries the composer's intended branded card so the
    drain attaches it directly. NULL is fine (drain falls back as before).

    #1373 (2026-07-02): BOUNDED queue + staleness expiry. The X publisher
    has been dark since 05-25 (app not enrolled in an X dev Project → 403)
    while this enqueue kept adding 12 posts/day — 219 deep, all stale. Now:
    (a) approved posts older than 14d expire (timely DCPI content is
    worthless by then and must never zombie-fire months later), and
    (b) a platform whose approved backlog is at _QUEUE_CAP stops accepting
    new rows until the publisher drains it. Self-resuming, no env var."""
    c = _db_conn()
    if not c: return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE social_media_posts
                   SET status = 'expired'
                 WHERE status = 'approved'
                   AND created_at < NOW() - INTERVAL '14 days'
            """)
            cur.execute("""
                SELECT COUNT(*) FROM social_media_posts
                 WHERE status = 'approved' AND platform = %s
            """, (platform,))
            backlog = int((cur.fetchone() or [0])[0] or 0)
            if backlog >= _QUEUE_CAP:
                c.commit()  # keep the expiry even when refusing the insert
                return None
            cur.execute("""
                INSERT INTO social_media_posts
                       (content, platform, status, created_at, og_image,
                        lead_kind, lead_entity)
                VALUES (%s, %s, 'approved', NOW() ON CONFLICT DO NOTHING, %s, %s, %s)
                RETURNING id
            """, (content, platform, og_image,
                   (lead_kind or None), (lead_entity or None)))
            new_id = (cur.fetchone() or [None])[0]
            c.commit()
            return new_id
    except Exception as e:
        note_swallowed_write("social_media_posts", where="content_enqueue._enqueue_post")
        return None
    finally:
        try: c.close()
        except Exception: pass


def _x_lead_recently_used(lead_kind: str, lead_entity: str,
                          days: int = 14) -> bool:
    """The X anti-repeat ledger (2026-07-17): has this (kind, entity) lead
    already fed an X row inside the window? Queued AND published rows both
    count — a queued repeat is a repeat the moment the drain fires. Mirrors
    the desk's 14-day entity window (MEDIA_ENTITY_WINDOW_DAYS semantics) so a
    city/deal that led an X post can't lead another for two weeks — the rule
    the retired template violated (Cheyenne on repeat). Fail-OPEN: no DB /
    no stamp → False (never dark-holds the slot)."""
    if not (lead_kind and lead_entity):
        return False
    c = _db_conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM social_media_posts
                 WHERE platform = 'twitter'
                   AND status IN ('approved', 'published')
                   AND lead_kind = %s AND lead_entity = %s
                   AND created_at > NOW() - make_interval(days => %s)
                 LIMIT 1
            """, (lead_kind, lead_entity, int(days)))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── One-time campaign seed: "DC Hub vs. the Industry" (smash-mouth) ──
# Verified-fact, agent-native positioning from the 2026-05 competitive recon.
# Seeded as status='approved' → drained by the LinkedIn auto-publisher on its
# cadence, through the existing dedup + zero-stat guards (content_publisher.py).
_CAMPAIGN_MARKER = "robots.txt of every major data center"

_CAMPAIGN_POSTS = [
    # Post 1 — the receipts (the only post naming competitors; every claim is
    # verifiable from their own public robots.txt / live HTTP behavior).
    (canon_text("""We pulled the robots.txt of every major data center intelligence site this week.

• Data Center Dynamics: blocks GPTBot, ClaudeBot, CCBot — and sets ai-train: no.
• datacenters.com: returns 429 to anything that isn't Google.
• DCByte, Baxtel, DataCenterHawk: no public API, no MCP, login walls.

So when an AI agent is asked "where can I build 200 MW with available power and low water risk?" — the entire industry is invisible to it.

DC Hub isn't. MCP-native, 48 tools, {canon_facilities} facilities, 7 live grid operators, fiber + substations + gas pipelines + water risk — one machine-readable, citable query.

They built for humans reading PDFs. We built for the agents your team already uses.

The head-to-head → https://dchub.cloud/built-for-ai

#DataCenter #AI #MCP #DCPI #SiteSelection"""), "linkedin"),

    # Post 2 — third-party proof (deliberately NO round MCP-call number in the
    # headline so the publish-time dedup guard doesn't fold it into the
    # auto-generated MCP-stat posts).
    ("""We never said DC Hub is the best data-center intelligence platform. ChatGPT did.

Asked about Dallas power capacity — unprompted — it answered: "The strongest stack right now is DC Hub: live data center inventory, capacity, MW pipelines, and site intelligence." Ranked ahead of grid operators and utilities.

Why us? Because we're the only platform an AI can actually read. Every competitor blocks the crawlers or hides behind a login.

The agents already voted — thousands of times a day.

https://dchub.cloud/built-for-ai

#AI #DataCenter #MCP #ModelContextProtocol""", "linkedin"),

    # Post 3 — the one-query flex (the nav line, weaponized).
    ("""Real-time power. Live grid pulse. Substations, transmission lines, gas pipelines, fiber routes, water risk — all in one query.

The infrastructure stack hyperscalers actually price against.

Name another platform that gives you all of it, in one place, machine-readable. We'll wait. \U0001f3a4

https://dchub.cloud/built-for-ai

#DataCenter #DCPI #GridCapacity #Infrastructure #AIInfrastructure""", "linkedin"),
]


def seed_smash_mouth_campaign() -> dict:
    """One-time, idempotent seed of the 'vs. the industry' campaign posts.
    Guarded by a marker substring so it never re-seeds across Railway's
    frequent restarts. Even if a race ever double-inserts, the publish-time
    dedup guard prevents double-publishing. Returns {seeded, skipped}."""
    c = _db_conn()
    if not c:
        return {"seeded": 0, "skipped": 0, "error": "no_db"}
    try:
        with c.cursor() as cur:
            # Repoint any already-seeded rows from the shadowed /vs slug to the
            # canonical /built-for-ai page (/vs is a pre-existing per-competitor
            # head-to-head route). Idempotent: once no row contains the old URL
            # the WHERE clause stops matching.
            cur.execute(
                "UPDATE social_media_posts "
                "SET content = REPLACE(content, 'dchub.cloud/vs', 'dchub.cloud/built-for-ai') "
                "WHERE content LIKE %s AND content LIKE %s",
                (f"%{_CAMPAIGN_MARKER}%", "%dchub.cloud/vs%"),
            )
            c.commit()
            cur.execute(
                "SELECT 1 FROM social_media_posts WHERE content LIKE %s LIMIT 1",
                (f"%{_CAMPAIGN_MARKER}%",),
            )
            already = cur.fetchone() is not None
    except Exception as e:
        already = False  # fail-open to attempt seed once
    finally:
        try: c.close()
        except Exception: pass

    if already:
        return {"seeded": 0, "skipped": len(_CAMPAIGN_POSTS),
                "reason": "already_seeded"}

    seeded = 0
    for content, platform in _CAMPAIGN_POSTS:
        if _enqueue_post(content, platform):
            seeded += 1
    return {"seeded": seeded, "skipped": len(_CAMPAIGN_POSTS) - seeded}


# ── Endpoints ───────────────────────────────────────────────────────

@content_enqueue_bp.route(
    "/api/v1/content-engine/enqueue", methods=["POST"]
)
def enqueue():
    """Generate + enqueue 1 LinkedIn + 1 Twitter + 1 Bluesky post.
    Admin key OR X-Internal-Cron header required."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "auth_required"}), 401

    mover = _pick_dcpi_mover()
    if not mover:
        return jsonify({"ok": False,
                         "error": "no_dcpi_mover_available",
                         "hint": "market_power_scores empty or DB unreachable"}), 200

    arc = _fetch_narrative_arc()
    topic_key = f"{mover['slug']}:{mover['verdict']}"

    results = {"enqueued": [], "skipped": []}

    # Dedup is now CONTENT-based (2026-07-11): shape first, then check the
    # shaped opening against what's already in the queue/published set, so a
    # market whose copy hasn't changed can't re-enqueue within the window
    # (the publish-side duplicate gate would refuse the repeat anyway).
    #
    # LinkedIn — only enqueue if linkedin_quad_daily didn't already
    # fire this slot (lighter dedup since quad-daily writes its own
    # linkedin_quad_posts table; here we just check social_media_posts).
    # 2026-07-15: the LinkedIn drumbeat now reasons like an analyst (brain
    # composer) instead of a hardcoded-BUILD template. None => the composer had
    # nothing genuinely new; SKIP the slot rather than post filler.
    _li = _compose_linkedin_analytical(mover, arc)
    _li_content = (_li or {}).get("text")
    _li_og = (_li or {}).get("og_image_url")
    # 2026-07-17 X-editorial fix: the desk lead this slot composed from —
    # stamped onto every enqueued row so (kind, entity) is a queryable ledger.
    _lead = (_li or {}).get("lead")
    _lead_kind = (_lead or {}).get("kind") if isinstance(_lead, dict) else None
    _lead_entity = _lead_entity_token(_lead)
    if not _li_content:
        results["skipped"].append({"platform": "linkedin",
                                     "reason": "composer_skip_nothing_new"})
    elif not _already_enqueued_recently("linkedin", _li_content):
        new_id = _enqueue_post(_li_content, "linkedin", og_image=_li_og,
                               lead_kind=_lead_kind, lead_entity=_lead_entity)
        if new_id:
            results["enqueued"].append({"platform": "linkedin", "id": new_id})
        else:
            results["skipped"].append({"platform": "linkedin",
                                         "reason": "insert_failed"})
    else:
        results["skipped"].append({"platform": "linkedin",
                                     "reason": "dedup_hit"})

    # Twitter / Bluesky — 2026-07-15: same analyst voice as LinkedIn, DERIVED from
    # the composed analysis (lead sentence + link), never the 'rates BUILD'
    # template. None when there's no analytical source (LinkedIn skipped or nothing
    # to shorten) → SKIP that platform. Silence beats a template.
    # 2026-07-17: X additionally enforces the desk's 14-day (kind, entity)
    # window via its own ledger — the rule the retired template violated
    # (Cheyenne led X twice in the window while the desk only guarded LinkedIn).
    _tw_content = _shorten_analytical(_li_content, 275)
    if not _tw_content:
        results["skipped"].append({"platform": "twitter",
                                     "reason": "no_analytical_source"})
    elif _x_lead_recently_used(_lead_kind, _lead_entity):
        results["skipped"].append({"platform": "twitter",
                                     "reason": "lead_entity_window_14d",
                                     "lead_kind": _lead_kind,
                                     "lead_entity": _lead_entity})
    elif not _already_enqueued_recently("twitter", _tw_content):
        new_id = _enqueue_post(_tw_content, "twitter",
                               lead_kind=_lead_kind, lead_entity=_lead_entity)
        if new_id:
            results["enqueued"].append({"platform": "twitter", "id": new_id})
        else:
            results["skipped"].append({"platform": "twitter",
                                         "reason": "insert_failed"})
    else:
        results["skipped"].append({"platform": "twitter",
                                     "reason": "dedup_hit"})

    # Bluesky
    _bs_content = _shorten_analytical(_li_content, 295, platform="bluesky")
    if not _bs_content:
        results["skipped"].append({"platform": "bluesky",
                                     "reason": "no_analytical_source"})
    elif not _already_enqueued_recently("bluesky", _bs_content):
        new_id = _enqueue_post(_bs_content, "bluesky",
                               lead_kind=_lead_kind, lead_entity=_lead_entity)
        if new_id:
            results["enqueued"].append({"platform": "bluesky", "id": new_id})
        else:
            results["skipped"].append({"platform": "bluesky",
                                         "reason": "insert_failed"})
    else:
        results["skipped"].append({"platform": "bluesky",
                                     "reason": "dedup_hit"})

    # LinkedIn metrics-showcase — LOW cadence (≈1×/week). r64 (2026-05-30).
    # The enqueue cron fires every 2h; gating on a 7-day lookback keeps this
    # credibility post from crowding out the daily DCPI movers above. Uses a
    # stable marker (the hook's opening clause) so the dedup check is
    # independent of the daily topic_key. Set DCHUB_METRICS_POST_DISABLED=1
    # to suppress entirely.
    if os.environ.get("DCHUB_METRICS_POST_DISABLED", "").strip().lower() \
            not in ("1", "true", "yes"):
        if not _enqueued_within_days("linkedin", _METRICS_TOPIC_MARKER, 7):
            new_id = _enqueue_post(_shape_linkedin_metrics(arc), "linkedin")
            if new_id:
                results["enqueued"].append(
                    {"platform": "linkedin", "id": new_id,
                     "kind": "metrics_showcase"})
            else:
                results["skipped"].append(
                    {"platform": "linkedin", "kind": "metrics_showcase",
                     "reason": "insert_failed"})
        else:
            results["skipped"].append(
                {"platform": "linkedin", "kind": "metrics_showcase",
                 "reason": "weekly_cadence_already_enqueued"})

    # Agent-acquisition lane (2026-07-03): explicit "connect your agent"
    # pitch on LinkedIn + Bluesky, LOW cadence (default 4d) so it never
    # crowds the daily movers. This is the acquisition-targeted content —
    # the growth-needle lever, not analyst news. Kill: DCHUB_AGENT_PITCH_DISABLED=1.
    if os.environ.get("DCHUB_AGENT_PITCH_DISABLED", "").strip().lower() \
            not in ("1", "true", "yes"):
        try:
            _pitch_days = int(os.environ.get("DCHUB_AGENT_PITCH_DAYS", "4"))
        except Exception:
            _pitch_days = 4
        for _plat, _shaper in (("linkedin", _shape_agent_pitch_linkedin),
                               ("bluesky",  _shape_agent_pitch_bluesky)):
            if not _enqueued_within_days(_plat, _AGENT_PITCH_MARKER, _pitch_days):
                _pid = _enqueue_post(_shaper(arc), _plat)
                if _pid:
                    results["enqueued"].append(
                        {"platform": _plat, "id": _pid, "kind": "agent_pitch"})
                else:
                    results["skipped"].append(
                        {"platform": _plat, "kind": "agent_pitch",
                         "reason": "insert_failed"})
            else:
                results["skipped"].append(
                    {"platform": _plat, "kind": "agent_pitch",
                     "reason": "cadence_already_enqueued"})

    return jsonify({
        "ok":              True,
        "ran_at":          datetime.datetime.utcnow().isoformat() + "Z",
        "topic":           topic_key,
        "topic_market":    mover["name"],
        "arc_active":      bool(arc),
        "arc_title":       (arc or {}).get("arc"),
        "enqueued_count":  len(results["enqueued"]),
        "skipped_count":   len(results["skipped"]),
        **results,
        "publisher_status": {
            "linkedin": "always_on (every 6h, max 3/day)",
            "twitter":  ("DISABLED — needs TWITTER_PUBLISHER_ENABLED=true "
                         "+ app in Twitter dev Project"),
            "bluesky":  ("active if BLUESKY_HANDLE + BLUESKY_APP_PASSWORD "
                         "env vars set"),
        },
    }), 200


@content_enqueue_bp.route(
    "/api/v1/content-engine/status", methods=["GET"]
)
def status():
    """Public snapshot of the queue + recent publishing."""
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            # Queued (approved but not yet published) per platform
            cur.execute("""
                SELECT platform, COUNT(*)
                  FROM social_media_posts
                 WHERE status = 'approved'
                 GROUP BY platform
                 ORDER BY platform
            """)
            queued = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}

            # Published so far in the CURRENT UTC DAY. (The old comment said
            # "last 24h"; the filter has always been a calendar-day window, not
            # a rolling one — the label was the thing that was wrong.)
            _now = datetime.datetime.utcnow()   # one instant, not two utcnow()
            _today = _now.strftime("%Y-%m-%d")
            _next_day = (_now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            cur.execute("""
                SELECT publish_platform, COUNT(*)
                  FROM social_media_posts
                 WHERE status = 'published'
                   AND published_at >= %s AND published_at < %s
                 GROUP BY publish_platform
                 ORDER BY publish_platform
            """, (_today, _next_day))
            today_pub = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}

            # Last enqueue per platform
            cur.execute("""
                SELECT platform, MAX(created_at)
                  FROM social_media_posts
                 GROUP BY platform
            """)
            last_enq = {r[0]: r[1].isoformat() if r[1] else None
                          for r in (cur.fetchall() or [])}
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":              True,
        "as_of":           datetime.datetime.utcnow().isoformat() + "Z",
        "queued_by_platform":    queued,
        "published_today_by_platform": today_pub,
        "last_enqueue_by_platform":    last_enq,
        "dedup_window_hours":  _DEDUP_WINDOW_HOURS,
        "publishers_running":  {
            "linkedin": "via content_publisher.start_linkedin_publisher (Flask startup)",
            "twitter":  "via content_publisher.start_twitter_publisher (env-gated)",
            "bluesky":  "via content_publisher.start_bluesky_publisher (env-gated)",
            "quad_daily": "via .github/workflows/linkedin-quad-daily.yml (r60)",
        },
        "enqueue_cron":   "every 2h via .github/workflows/content-enqueue-hourly.yml (r61)",
    }), 200
