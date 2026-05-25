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
  market_anomaly        — Biggest WoW score change among 285 markets

Each pulls real DB data, then asks Claude Sonnet to compose a
280-char hook + 2-3 insight beats + CTA + hashtags in DC Hub's voice.

Theme-diversity dedup: track types posted in last 14d, prefer
unused ones. Falls back to existing static templates if Anthropic
API is unavailable, so the slot never goes silent.
"""
from __future__ import annotations

import datetime
import json
import os
import random
import urllib.request
import urllib.error
from contextlib import contextmanager

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
}

# Each story type maps to ONE of the 4 OG images we already serve.
OG_IMAGE_BY_TYPE = {
    "capability_spotlight":  "https://api.dchub.cloud/static/og/landing-agents.png",
    "energy_narrative":      "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "dcpi_scoop":            "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "shipped_this_week":     "https://api.dchub.cloud/static/og/landing-agents.png",
    "hyperscaler_drama":     "https://api.dchub.cloud/static/og/landing-hyperscaler-deals.png",
    "market_anomaly":        "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
}

# Known MCP tool catalog — used by capability_spotlight to pick a
# tool + describe an example call. Hand-curated from server-card.
_MCP_TOOL_HOOKS = [
    {"tool": "rank_markets",      "ask": "rank 285 US data-center markets by excess power for AI training"},
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
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # High excess power score, not a top-5 known name.
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
                 ORDER BY RANDOM() LIMIT 1
            """)
            row = cur.fetchone()
            return {"type": "dcpi_scoop", "scoop": dict(row) if row else None}
    except Exception:
        return {"type": "dcpi_scoop"}


def _pull_shipped_this_week() -> dict:
    """What we built in the last 7 days — proof of velocity."""
    if not (_pg and _dsn()):
        return {"type": "shipped_this_week"}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM auto_press_releases WHERE generated_at >= NOW() - INTERVAL '7 days'")
            press_7d = (cur.fetchone() or [0])[0]
            cur.execute("SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at >= NOW() - INTERVAL '7 days'")
            calls_7d = (cur.fetchone() or [0])[0]
            cur.execute("SELECT COUNT(*) FROM auto_press_releases WHERE linkedin_sent_at >= NOW() - INTERVAL '7 days' AND linkedin_sent_at IS NOT NULL")
            li_7d = (cur.fetchone() or [0])[0]
            cur.execute("SELECT COUNT(*) FROM brain_lifecycle_proposals WHERE proposed_at >= NOW() - INTERVAL '7 days'")
            proposals_7d = (cur.fetchone() or [0])[0]
            cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE COALESCE(last_seen_at, first_seen_at) >= NOW() - INTERVAL '7 days'")
            disc_7d = (cur.fetchone() or [0])[0]
            return {
                "type": "shipped_this_week",
                "stats": {
                    "press_releases":      press_7d,
                    "mcp_tool_calls":      calls_7d,
                    "linkedin_posts":      li_7d,
                    "brain_proposals":     proposals_7d,
                    "facilities_discovered": disc_7d,
                },
            }
    except Exception:
        return {"type": "shipped_this_week"}


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
                   AND (LOWER(title) LIKE '%%stargate%%'
                        OR LOWER(title) LIKE '%%openai%%'
                        OR LOWER(title) LIKE '%%coreweave%%'
                        OR LOWER(title) LIKE '%%amd%%'
                        OR LOWER(title) LIKE '%%nvidia%%'
                        OR LOWER(title) LIKE '%%microsoft%%'
                        OR LOWER(title) LIKE '%%anthropic%%')
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
                 ORDER BY ABS(l.now_e - p.prev_e) DESC LIMIT 1
            """)
            row = cur.fetchone()
            return {"type": "market_anomaly", "anomaly": dict(row) if row else None}
    except Exception:
        return {"type": "market_anomaly"}


_PULLERS = {
    "capability_spotlight": _pull_capability_spotlight,
    "energy_narrative":     _pull_energy_narrative,
    "dcpi_scoop":           _pull_dcpi_scoop,
    "shipped_this_week":    _pull_shipped_this_week,
    "hyperscaler_drama":    _pull_hyperscaler_drama,
    "market_anomaly":       _pull_market_anomaly,
}


# ── Theme-diversity selector ──────────────────────────────────────

def _pick_story_type(slot_topic: str | None = None) -> str:
    """Pick the next story type, avoiding ones used in the last 14 days
    for the SAME slot. Hardcoded slot→preferred mapping nudges the rotation
    so each slot retains some style identity (data/narrative/listicle/
    contrarian) but content varies.
    """
    # Slot-based preferred set (still varies within each)
    preferred = {
        "dcpi_mover":         ["dcpi_scoop", "market_anomaly", "energy_narrative"],
        "hyperscaler_deal":   ["hyperscaler_drama", "capability_spotlight", "shipped_this_week"],
        "ai_capex_index":     ["capability_spotlight", "market_anomaly", "dcpi_scoop"],
        "industry_pulse":     ["energy_narrative", "hyperscaler_drama", "shipped_this_week"],
    }
    candidates = preferred.get(slot_topic or "", list(_PULLERS.keys()))

    if not (_pg and _dsn()):
        return random.choice(candidates)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT topic FROM linkedin_quad_posts
                 WHERE posted_at > NOW() - INTERVAL '14 days'
                   AND topic = ANY(%s)
            """, (candidates,))
            used = {r[0] for r in cur.fetchall()}
        fresh = [t for t in candidates if t not in used]
        return random.choice(fresh or candidates)
    except Exception:
        return random.choice(candidates)


# ── Claude composer ───────────────────────────────────────────────

_VOICE_SYSTEM = """You write LinkedIn posts for DC Hub Media — the
leading data-center intelligence platform. Voice rules:
  - Confident, expert, story-driven. Cite specific numbers.
  - One opening hook that earns the scroll-stop (≤140 chars).
  - 2-3 short paragraphs with insight, not bullet lists.
  - One concrete CTA with the landing URL provided.
  - End with 3-4 hashtags (#DCHub or #DCHubMedia, #DCPI when DCPI
    is the proof point, plus 1-2 topic tags).
  - 800-1800 chars total (LinkedIn algorithm sweet spot).
  - Forbidden: 'delve', 'moreover', 'in essence', 'unleash',
    'game-changer', 'revolutionize'. No em-dashes (use ' — ' only
    where the typographic dash improves rhythm, never as comma
    substitute). No emoji-heavy openings (max 1 emoji, in the hook).
  - Never repeat a claim from a recent post. Be specific.
Output the POST TEXT ONLY. No preamble, no quotes."""


def _compose_with_claude(story_type: str, data: dict, landing: str) -> str | None:
    """Send a tailored prompt to Sonnet and return the post text.

    Returns None on any failure so the caller can fall back to a
    static template.
    """
    if not ANTHROPIC_API_KEY:
        return None

    # Per-story-type prompt
    user_prompt = _build_user_prompt(story_type, data, landing)
    if not user_prompt:
        return None

    body = json.dumps({
        "model": "claude-sonnet-4-5",
        "max_tokens": 800,
        "system": _VOICE_SYSTEM,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANTHROPIC_API_KEY,
            "Anthropic-Version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        text_parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in text_parts if isinstance(p, dict))
        text = text.strip()
        # Strip any wrapper quotes Claude sometimes adds
        if text.startswith('"') and text.endswith('"') and len(text) > 10:
            text = text[1:-1].strip()
        return text or None
    except Exception:
        return None


def _build_user_prompt(story_type: str, data: dict, landing: str) -> str:
    """Per-story-type user prompt with the real data."""
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
what DC Hub returns — concretely. Mention the tool name. End with
CTA pointing at {landing} and 3-4 hashtags."""

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
        stats = data.get("stats") or {}
        return f"""Compose a LinkedIn post that demonstrates DC Hub's
build velocity. Last 7 days of real activity:

{json.dumps(stats, default=str)[:400]}

Frame it as "what we built so AI agents have better data this
week." Be specific with the numbers. End with what this enables.
CTA: {landing}. Hashtags include #DCHubMedia and #BuildInPublic."""

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

    if story_type == "market_anomaly":
        a = data.get("anomaly") or {}
        return f"""Compose a LinkedIn post about a DCPI anomaly: the
biggest week-over-week score change among 285 US markets.

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
    if story_type == "capability_spotlight":
        tool = data.get("tool") or {}
        return (
            f"Ask any AI: \"{tool.get('ask','...')}\"\n\n"
            f"Without DC Hub, the model guesses. With DC Hub's MCP "
            f"{tool.get('tool','tool')}, it returns the real answer "
            f"in milliseconds — pulled from 21,401 facilities, 285 "
            f"DCPI-scored markets, and live ISO grid data.\n\n"
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
            f"This is why DC Hub scores 285 US power markets weekly "
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
            f"DC Hub built the Power Index. 285 markets, weekly, "
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
            f"DCPI anomaly of the week: {a.get('market_name','a US market')} "
            f"moved {sign}{delta} on Excess Power score.\n\n"
            f"Current: {a.get('now_e','?')} ({a.get('now_v','?')}). "
            f"7d ago: {a.get('prev_e','?')}.\n\n"
            f"Movements this large signal real underlying change — "
            f"new gen additions, queue movement, or demand shifts. AI "
            f"infra teams should investigate.\n\n"
            f"All 285 markets: {landing}\n\n"
            f"#DCPI #DataCenter #AIInfrastructure"
        )
    return f"DC Hub Media · See {landing}\n\n#DCHub #DataCenter"


# ── Public API ────────────────────────────────────────────────────

def compose_story_post(slot_topic: str | None = None) -> dict:
    """Compose a story-driven LinkedIn post.

    Returns dict with:
      story_type, text, landing_url, og_image_url, source ('claude' or 'fallback')
    """
    story_type = _pick_story_type(slot_topic)
    pull = _PULLERS.get(story_type, _PULLERS["capability_spotlight"])
    data = pull()
    landing = LANDING_BY_TYPE[story_type]
    og_url = OG_IMAGE_BY_TYPE[story_type]

    text = _compose_with_claude(story_type, data, landing)
    source = "claude"
    if not text or len(text) < 200:
        text = _static_fallback(story_type, data, landing)
        source = "fallback"

    return {
        "story_type":   story_type,
        "text":         text,
        "landing_url":  landing,
        "og_image_url": og_url,
        "source":       source,
        "data_used":    data,
    }
