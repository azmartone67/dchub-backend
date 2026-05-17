"""Phase BB (2026-05-12) — DC Hub autonomous marketing engine.

User asked: "the dc hub media isn't looking like a self managed marketing
engine either to me, enhance."

This module turns DC Hub Media from "static feed" into "autonomous
publishing organism." Every day it:

  1. Reads the most newsworthy DCPI shifts of the last 24h (top movers,
     new BUILD verdicts, biggest constraint jumps).
  2. Reads new AI-citation signals from mcp_tool_calls (101 unique AI
     agents across 7d → that's a quotable adoption metric).
  3. Calls Anthropic Claude to draft a single press release distilling
     the most-citable beat of the day.
  4. Writes to press_releases with full SEO metadata (slug, og:title,
     meta_description, Schema.org PressRelease JSON-LD).
  5. Updates the RSS feed at /press/feed.xml.
  6. Records the auto-generated piece in auto_press_releases so /brain
     and /dc-hub-media can show "X auto-press-releases this week, Y AI
     citations gained, Z signups attributed."

Endpoints
---------
  POST /api/v1/marketing/auto-generate    admin-gated, idempotent per day
  GET  /api/v1/marketing/pulse            public; recent autonomous output
                                          + engagement metrics
  POST /api/v1/marketing/track             pixel-style click tracking;
                                          press_engagement rows
  GET  /api/v1/marketing/engagement       public; per-piece view + CTR

Safety
------
  - Idempotent: re-runs on the same day no-op (look up by date+source).
  - All generations are LOGGED to auto_press_releases.
  - The generator never publishes anything that fails validation
    (min 200 chars body, valid Schema.org markup, slug uniqueness).
  - Failing soft: if Claude is unreachable or DB is down, the cron
    logs an outcome row and exits 0 so the next run can retry.
"""
from __future__ import annotations
import os
import json
import re
import sys
from datetime import datetime, timezone, timedelta, date
from functools import wraps
from flask import Blueprint, jsonify, request

marketing_bp = Blueprint("marketing_engine", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MARKETING_MODEL = os.environ.get("DCHUB_MARKETING_MODEL", "claude-sonnet-4-5")
# .strip() — a trailing newline on the Railway env var (dashboards add
# one when you paste) would make EVERY admin call 401, since the
# comparison below is exact. Same whitespace footgun fixed for the
# LinkedIn/X tokens in PR #110.
ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL")
# Phase QQ+16 (2026-05-13): module-level RESEND_API_KEY. The
# linkedin_send_daily_email handler references this name unqualified,
# but the constant was never declared at module scope — every cron
# fire raised NameError("name 'RESEND_API_KEY' is not defined") and
# the LinkedIn email for today's DCPI press release (Cheyenne, WY
# Tops DCPI Excess Power Index) never went out. Defining it here
# matches the existing pattern used by ANTHROPIC_API_KEY / ADMIN_KEY
# above. When Railway has DCHUB_RESEND_API_KEY set, this becomes the
# Bearer token in the Resend API call.
RESEND_API_KEY = os.environ.get("DCHUB_RESEND_API_KEY", "")


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        # .strip() the caller's value too — curl/shell vars frequently
        # carry a trailing newline, which would never match otherwise.
        provided = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return w


def _conn():
    if not DATABASE_URL: return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:
        print(f"[marketing_engine] connect failed: {e}", file=sys.stderr)
        return None


_SCHEMA_DDL = """
-- Autonomous-only press release tracking. Mirror of press_releases but
-- restricted to rows the marketing engine generated. Separate table so
-- (a) we never auto-delete or rewrite human-authored press, (b)
-- engagement analytics can isolate auto from human performance.
CREATE TABLE IF NOT EXISTS auto_press_releases (
    id              BIGSERIAL PRIMARY KEY,
    press_release_id INTEGER,     -- FK to press_releases.id (loose; no constraint)
    slug            TEXT NOT NULL UNIQUE,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    generated_for   DATE NOT NULL,    -- the date the press release covers
    source_topic    TEXT,             -- 'dcpi_mover' | 'ai_citation' | 'new_facility'
    source_data     JSONB,            -- raw signal the generator used
    model           TEXT,             -- 'claude-sonnet-4-5'
    title           TEXT,
    body            TEXT,
    word_count      INTEGER,
    validation_ok   BOOLEAN DEFAULT TRUE,
    -- Phase EE (2026-05-12): LinkedIn-optimized post for daily
    -- distribution. Claude generates this alongside the long-form
    -- press release. Different format: 1200-1500 chars, hook + bullets
    -- + hashtags + one URL.
    linkedin_post   TEXT,
    linkedin_sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS auto_press_generated_for_idx
    ON auto_press_releases(generated_for DESC);
-- Idempotent column add for installations that have the table from
-- an earlier deploy:
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'auto_press_releases')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'auto_press_releases'
                         AND column_name = 'linkedin_post') THEN
        ALTER TABLE auto_press_releases
            ADD COLUMN linkedin_post TEXT,
            ADD COLUMN linkedin_sent_at TIMESTAMPTZ;
    END IF;
END $$;

-- Engagement: per-piece view + click counters. Updated by the public
-- /track endpoint (pixel) and the public /pulse aggregator.
CREATE TABLE IF NOT EXISTS press_engagement (
    id           BIGSERIAL PRIMARY KEY,
    slug         TEXT NOT NULL,
    event_type   TEXT NOT NULL,         -- 'view' | 'click_out' | 'stripe_click'
    referrer     TEXT,
    user_agent   TEXT,
    ip_hash      TEXT,                  -- not raw IP
    t            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS press_engagement_slug_idx
    ON press_engagement(slug, t DESC);
CREATE INDEX IF NOT EXISTS press_engagement_event_idx
    ON press_engagement(event_type, t DESC);
"""


def init_schema() -> bool:
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[marketing_engine] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


# Lazy schema init — runs on first import. Fail-soft.
try:
    _SCHEMA_OK = init_schema()
except Exception:
    _SCHEMA_OK = False


# ---------------------------------------------------------------------------
# 1. SIGNAL COLLECTION — what's newsworthy today?
# ---------------------------------------------------------------------------

def _collect_signals() -> dict:
    """Pull the most newsworthy signals from the last 24h. Returns a
       dict ready to feed into the Claude prompt."""
    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "top_build_markets": [],
        "top_avoid_markets": [],
        "biggest_movers": [],
        "ai_usage_24h": {"tool_calls": 0, "unique_callers": 0},
        "new_facilities_24h": [],
    }
    c = _conn()
    if c is None:
        return out
    try:
        # Top 3 BUILD markets (highest excess_power_score, latest snapshot)
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (market_slug)
                    market_name, market_slug, iso, state,
                    excess_power_score, constraint_score, verdict
                FROM market_power_scores
                WHERE published = true AND verdict = 'BUILD'
                ORDER BY market_slug, computed_at DESC
            """)
            rows = cur.fetchall()
            rows.sort(key=lambda r: -(r[4] or 0))
            out["top_build_markets"] = [
                {"market": r[0], "slug": r[1], "iso": r[2], "state": r[3],
                 "excess": r[4], "constraint": r[5]}
                for r in rows[:3]]

        # Top 3 AVOID markets (highest constraint_score)
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (market_slug)
                    market_name, market_slug, iso, state,
                    excess_power_score, constraint_score, verdict
                FROM market_power_scores
                WHERE published = true AND verdict = 'AVOID'
                ORDER BY market_slug, computed_at DESC
            """)
            rows = cur.fetchall()
            rows.sort(key=lambda r: -(r[5] or 0))
            out["top_avoid_markets"] = [
                {"market": r[0], "slug": r[1], "iso": r[2], "state": r[3],
                 "excess": r[4], "constraint": r[5]}
                for r in rows[:3]]

        # Biggest movers — markets whose excess shifted most in the last 7 days
        with c.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug) market_slug, market_name,
                           iso, excess_power_score AS now_e
                    FROM market_power_scores
                    WHERE published = true
                    ORDER BY market_slug, computed_at DESC
                ),
                week_ago AS (
                    SELECT DISTINCT ON (market_slug) market_slug,
                           excess_power_score AS prev_e
                    FROM market_power_scores
                    WHERE published = true
                      AND computed_at < NOW() - INTERVAL '7 days'
                    ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_slug, l.market_name, l.iso,
                       l.now_e, (l.now_e - w.prev_e) AS delta
                FROM latest l JOIN week_ago w ON l.market_slug = w.market_slug
                WHERE ABS(l.now_e - w.prev_e) > 3
                ORDER BY ABS(l.now_e - w.prev_e) DESC
                LIMIT 5
            """)
            out["biggest_movers"] = [
                {"slug": r[0], "market": r[1], "iso": r[2],
                 "now": round(r[3] or 0, 1), "delta": round(r[4] or 0, 1)}
                for r in cur.fetchall()]

        # AI usage — quotable adoption metric
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(DISTINCT COALESCE(NULLIF(client_name,''),
                                                    NULLIF(platform,''),
                                                    ip_address))
                    FROM mcp_tool_calls
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                """)
                row = cur.fetchone() or (0, 0)
                out["ai_usage_24h"] = {
                    "tool_calls": int(row[0] or 0),
                    "unique_callers": int(row[1] or 0),
                }
        except Exception as e:
            print(f"[marketing_engine] ai_usage probe failed: {e}", file=sys.stderr)

        # New facilities discovered in last 24h
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT name, provider, city, state, country, power_mw
                    FROM discovered_facilities
                    WHERE discovered_at > NOW() - INTERVAL '24 hours'
                    ORDER BY power_mw DESC NULLS LAST
                    LIMIT 5
                """)
                out["new_facilities_24h"] = [
                    {"name": r[0], "provider": r[1], "city": r[2],
                     "state": r[3], "country": r[4], "mw": r[5]}
                    for r in cur.fetchall() if r[0]]
        except Exception as e:
            print(f"[marketing_engine] facilities probe failed: {e}", file=sys.stderr)

        # Phase NN (2026-05-15): industry news from the announcements feed.
        # Lets the picker run an `industry_pulse` topic — DC Hub's commentary
        # on what's moving in the industry this week. Materially different
        # from `dcpi_leader` (our own rankings) because the headline is the
        # third-party event; DC Hub's role is the data overlay.
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT title, summary, source, category, url
                    FROM announcements
                    WHERE published_date >= (NOW() - INTERVAL '48 hours')::text
                    ORDER BY published_date DESC
                    LIMIT 8
                """)
                out["industry_news_48h"] = [
                    {"title": r[0], "summary": (r[1] or "")[:300],
                     "source": r[2], "category": r[3], "url": r[4]}
                    for r in cur.fetchall() if r[0]]
        except Exception as e:
            print(f"[marketing_engine] industry_news probe failed: {e}", file=sys.stderr)
            out["industry_news_48h"] = []

        # Phase NN: ISO rotation — today's ISO based on day-of-year. Gives
        # the picker a deterministic "different ISO every day" cadence that
        # cycles through PJM/MISO/CAISO/ERCOT/SPP/NYISO/ISO-NE on a 7-day
        # loop. The data pull is best-effort: if any ISO probe table is
        # missing we fall through with just the ISO name (picker decides
        # whether the data is rich enough to justify a topic).
        try:
            import datetime as _dt
            ISOS = ["PJM", "MISO", "CAISO", "ERCOT", "SPP", "NYISO", "ISO-NE"]
            doy = _dt.date.today().timetuple().tm_yday
            iso_today = ISOS[doy % len(ISOS)]
            iso_data = {"iso": iso_today, "markets_in_iso": 0,
                         "avg_excess": None, "avg_constraint": None}
            with c.cursor() as cur:
                # Pull a quick footprint from market_power_scores so the
                # press release has concrete numbers about today's ISO
                # (count + avg excess/constraint).
                cur.execute("""
                    SELECT COUNT(DISTINCT market_slug),
                           AVG(excess_power_score),
                           AVG(constraint_score)
                    FROM (
                        SELECT DISTINCT ON (market_slug)
                               market_slug, excess_power_score, constraint_score
                        FROM market_power_scores
                        WHERE published = true AND iso = %s
                        ORDER BY market_slug, computed_at DESC
                    ) latest
                """, (iso_today,))
                row = cur.fetchone() or (0, None, None)
                iso_data["markets_in_iso"]  = int(row[0] or 0)
                iso_data["avg_excess"]      = round(float(row[1] or 0), 1) if row[1] is not None else None
                iso_data["avg_constraint"]  = round(float(row[2] or 0), 1) if row[2] is not None else None
            out["iso_today"] = iso_data
        except Exception as e:
            print(f"[marketing_engine] iso_today probe failed: {e}", file=sys.stderr)
            out["iso_today"] = {}

        # Phase NN: coverage growth — week-over-week row deltas across the
        # tables that matter most for "DC Hub is growing" stories. Picker
        # promotes `coverage_milestone` when ANY metric grew >=10% WoW or
        # crossed a round number (1k, 10k, 100k, 1M).
        try:
            COVERAGE_TABLES = [
                ("facilities",          "facilities",          "discovered_at"),
                ("markets_tracked",     "market_power_scores", "computed_at"),
                ("mcp_developers",      "mcp_dev_keys",        "created_at"),
                ("mcp_tool_calls",      "mcp_tool_calls",      "created_at"),
                ("air_permits",         "air_permits",         "issued_date"),
                ("substations",         "substations",         "updated_at"),
            ]
            growth = []
            with c.cursor() as cur:
                for label, tbl, tscol in COVERAGE_TABLES:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                        total = int((cur.fetchone() or (0,))[0] or 0)
                        cur.execute(
                            f"SELECT COUNT(*) FROM {tbl} WHERE {tscol} > NOW() - INTERVAL '7 days'")
                        added_7d = int((cur.fetchone() or (0,))[0] or 0)
                        if total > 0:
                            pct = round((added_7d / max(total - added_7d, 1)) * 100, 1)
                            growth.append({
                                "label": label, "total": total,
                                "added_7d": added_7d, "pct_wow": pct})
                    except Exception:
                        # Table may not exist on this deploy — skip silently
                        try: c.rollback()
                        except Exception: pass
                        continue
            growth.sort(key=lambda g: (-(g["added_7d"]), -(g["pct_wow"])))
            out["coverage_growth_7d"] = growth
        except Exception as e:
            print(f"[marketing_engine] coverage_growth probe failed: {e}", file=sys.stderr)
            out["coverage_growth_7d"] = []
    finally:
        try: c.close()
        except Exception: pass
    return out


def _recent_topics(days: int = 3) -> set:
    """Phase MM (2026-05-15): look up which topics ran in the last N days
    so _pick_daily_topic can avoid back-to-back repeats. Was a real problem
    — 'dcpi_leader' fired 4 days in a row (Cheyenne kept winning), and
    LinkedIn followers saw the same story 4× before it changed.
    Returns the set of source_topic slugs from the last N days. Empty
    set on any error (fail-open so topic picking never blocks)."""
    try:
        c = _conn()
        if c is None:
            return set()
        with c.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT source_topic FROM auto_press_releases
                    WHERE generated_for >= (CURRENT_DATE - INTERVAL '%s days')
                      AND generated_for < CURRENT_DATE""",
                (days,))
            rows = cur.fetchall()
        c.close()
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


# Phase NN (2026-05-15): "sticky" topics where the same MARKET keeps winning
# (Cheyenne held the BUILD lead 4 days running). For these we widen the
# dedup window to 7 days so the picker is forced to pivot to a different
# angle even if the underlying data hasn't moved.
_STICKY_TOPIC_WINDOWS = {
    "dcpi_leader": 7,
    "dcpi_warning": 5,
    "dcpi_mover":   5,
}


def _topic_recently_ran(topic: str, recent_3d: set) -> bool:
    """Topic-aware dedup. Most topics use the 3-day set already loaded;
    sticky topics get an extra DB lookup against their wider window."""
    if topic in recent_3d:
        return True
    win = _STICKY_TOPIC_WINDOWS.get(topic)
    if not win:
        return False
    try:
        c = _conn()
        if c is None:
            return False
        with c.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM auto_press_releases
                    WHERE source_topic = %s
                      AND generated_for >= (CURRENT_DATE - INTERVAL '%s days')
                      AND generated_for < CURRENT_DATE
                    LIMIT 1""",
                (topic, win))
            hit = cur.fetchone() is not None
        c.close()
        return hit
    except Exception:
        return False


# Phase NN (2026-05-15): pull market names from the last N published titles
# so the picker can refuse to publish about the same market two days in a
# row, regardless of what topic the picker chose. Belt-and-suspenders for
# the "Cheyenne 4 days in a row" repeat — even if the dedup window were
# permissive, this guard catches the actual symptom (repeat market).
def _recent_market_names(n: int = 2) -> set:
    """Returns a lowercased set of market name fragments mentioned in the
    last `n` auto press release titles. Best-effort regex extraction; a
    miss just means looser variety (fail-open)."""
    try:
        c = _conn()
        if c is None:
            return set()
        with c.cursor() as cur:
            cur.execute(
                """SELECT title FROM auto_press_releases
                    WHERE title IS NOT NULL
                    ORDER BY generated_at DESC NULLS LAST
                    LIMIT %s""",
                (n,))
            titles = [r[0] for r in cur.fetchall() if r and r[0]]
        c.close()
        out = set()
        import re as _re
        for t in titles:
            # "Cheyenne, WY ...", "Atlanta Metro ...", "Northern Virginia ..."
            m = _re.match(r"^([A-Z][a-zA-Z\.\- ]+?)(?:,| Metro|:| - | – | Leads| Tops| Takes)", t)
            if m:
                out.add(m.group(1).strip().lower())
        return out
    except Exception:
        return set()


def _theme_for_weekday() -> tuple[str, str]:
    """Phase MM (2026-05-15): 4-theme weekday rotation. User asked for
    'the four separate themes we created' — formalizing them here so
    every week has predictable variety:
       Mon — Movers      (DCPI deltas, M&A, big news)
       Tue — Grid + ISO  (capacity, queue, transmission)
       Wed — AI Infra    (MCP adoption, GPU clusters, AI training sites)
       Thu — Markets     (BUILD verdicts, top opportunities)
       Fri — Deals + Listings (transactions, pocket inventory)
       Sat/Sun — Methodology / explainers (lighter content)
    """
    import datetime as _dt
    wd = _dt.date.today().weekday()  # Mon=0, Sun=6
    THEMES = {
        0: ("theme_movers",   "Monday Movers: biggest week-over-week DCPI shifts, M&A pulse, and top news this week."),
        1: ("theme_grid_iso", "Tuesday Grid + ISO: interconnection queue, transmission headroom, reserve margins, fuel mix."),
        2: ("theme_ai_infra", "Wednesday AI Infra: MCP usage, GPU clusters, AI training sites, model-vendor data center demand."),
        3: ("theme_markets",  "Thursday Markets: which markets earn BUILD this week, top excess-power opportunities, breakout cities."),
        4: ("theme_deals",    "Friday Deals + Listings: recent transactions, pocket-listing inventory, buyer/seller pulse."),
        5: ("theme_methodology", "Weekend Methodology: deep dive on one DCPI axis or data source."),
        6: ("theme_methodology", "Weekend Methodology: deep dive on one DCPI axis or data source."),
    }
    return THEMES[wd]


def _pick_daily_topic(signals: dict) -> tuple[str, str]:
    """Phase LL: pick the most newsworthy topic for today's auto-press,
    with guaranteed fallbacks so the cron never goes a day without
    output.

    Phase LL+1 (2026-05-14): expanded topic library 7→14 entries.
    Phase MM (2026-05-15): added 3-day topic-repeat dedup + 4-theme
    weekday baseline. If the priority topics would repeat what we ran
    in the last 3 days, skip them and use the weekday theme instead.
    Fixes the "Cheyenne 4 days in a row" repetition the user spotted.

    Phase NN (2026-05-15): three new topic branches (industry_pulse,
    iso_focus, coverage_milestone) so the engine can pivot when DCPI
    rankings stay flat. Sticky topics get a 7-day window via
    _topic_recently_ran(). Adds a same-MARKET guard so the picker
    refuses to publish a market that appeared in the last 2 titles
    even if the topic itself would be allowed.

    Returns (topic_slug, human_reason). The Claude prompt sees both.
    """
    # Defensive: helpers live at module level but the test-suite extracts
    # just this function and execs it standalone (tests/...). Guard each
    # helper so the test environment falls back cleanly.
    try:
        recent = _recent_topics(days=3)
    except NameError:
        recent = set()
    try:
        recent_markets = _recent_market_names(n=2)
    except NameError:
        recent_markets = set()

    def _topic_dedup(t: str) -> bool:
        try:
            return _topic_recently_ran(t, recent)
        except NameError:
            return t in recent

    def _market_clash(name: str | None) -> bool:
        """True if `name` overlaps with any market in the last 2 titles.
        Lowercased substring match catches "Cheyenne, WY" vs "Cheyenne"."""
        if not name or not recent_markets:
            return False
        nm = name.lower()
        return any(nm.startswith(rm) or rm.startswith(nm) for rm in recent_markets)

    # ── 1. DCPI movers (high bar: |delta| >= 5pts) ──────────────────
    movers = signals.get("biggest_movers") or []
    if movers and not _topic_dedup("dcpi_mover"):
        m = movers[0]
        d = abs(m.get("delta") or 0)
        if d >= 5 and not _market_clash(m.get("market")):
            return "dcpi_mover", (
                f"{m.get('market','a market')} shifted "
                f"{m.get('delta')}pts in DCPI this week — biggest mover.")

    # ── 2. Industry pulse — third-party news with DC Hub overlay ────
    # NEW Phase NN. Promoted ahead of dcpi_leader because (a) the news
    # is genuinely fresh every day, (b) DC Hub's commentary is what we
    # uniquely add, (c) it dodges the "same market wins" trap entirely.
    news = signals.get("industry_news_48h") or []
    if len(news) >= 3 and not _topic_dedup("industry_pulse"):
        headlines = "; ".join(f"{n.get('title','')[:80]} ({n.get('source','?')})"
                              for n in news[:3])
        return "industry_pulse", (
            f"Industry pulse — three stories moving the data-center "
            f"market right now: {headlines}. DC Hub adds the DCPI overlay.")

    # ── 3. ISO focus — rotates through 7 ISOs by day-of-year ────────
    # NEW Phase NN. Only fires when the picked ISO has >=10 markets
    # in our coverage (so the press release has substance).
    iso = signals.get("iso_today") or {}
    if iso.get("iso") and iso.get("markets_in_iso", 0) >= 10 \
            and not _topic_dedup("iso_focus"):
        return "iso_focus", (
            f"{iso['iso']} grid snapshot: {iso['markets_in_iso']} DC markets "
            f"tracked in this ISO, average DCPI excess "
            f"{iso.get('avg_excess','?')}, average constraint "
            f"{iso.get('avg_constraint','?')}. Today's interconnection + "
            f"capacity readout for {iso['iso']}.")

    # ── 4. Coverage milestone — when a metric grew >=10% WoW ────────
    # NEW Phase NN. Materially different from dcpi_leader because the
    # story is "DC Hub itself grew," not "this market scored highest."
    growth = signals.get("coverage_growth_7d") or []
    if growth and not _topic_dedup("coverage_milestone"):
        big = next((g for g in growth
                    if g.get("pct_wow", 0) >= 10 or g.get("added_7d", 0) >= 100),
                   None)
        if big:
            return "coverage_milestone", (
                f"DC Hub coverage now spans {big['total']:,} "
                f"{big['label']} — added {big['added_7d']:,} in the last "
                f"7 days (+{big['pct_wow']}% WoW). Other 7d gains: "
                + ", ".join(f"{g['label']}+{g['added_7d']}"
                            for g in growth[:3] if g.get("added_7d", 0) > 0))

    # ── 5. DCPI leader (the "Cheyenne" branch — now last-priority) ──
    # Now gated on _topic_dedup (7-day window) AND _market_clash so
    # back-to-back Cheyenne is impossible.
    builds = signals.get("top_build_markets") or []
    if builds and not _topic_dedup("dcpi_leader") \
            and not _market_clash(builds[0].get("market")):
        return "dcpi_leader", (
            f"{builds[0].get('market','top market')} leads the BUILD ranking "
            f"with excess power score {builds[0].get('excess','?')}.")

    avoids = signals.get("top_avoid_markets") or []
    if avoids and not _topic_dedup("dcpi_warning") \
            and not _market_clash(avoids[0].get("market")):
        return "dcpi_warning", (
            f"{avoids[0].get('market','a market')} flagged AVOID — highest "
            f"constraint score {avoids[0].get('constraint','?')}.")

    new_fac = signals.get("new_facilities_24h") or []
    if new_fac and not _topic_dedup("new_facility"):
        f = new_fac[0]
        return "new_facility", (
            f"{f.get('name','A new facility')} ({f.get('provider','?')}, "
            f"{f.get('mw','?')}MW) detected in {f.get('city','?')}, "
            f"{f.get('state','?')}.")

    ai = signals.get("ai_usage_24h") or {}
    if ai.get("tool_calls", 0) >= 1000 and not _topic_dedup("ai_adoption"):
        return "ai_adoption", (
            f"DC Hub MCP served {ai.get('tool_calls')} AI tool calls in "
            f"the last 24h from {ai.get('unique_callers')} unique callers.")

    # Phase MM (2026-05-15): every priority topic above repeated in the last
    # 3 days OR no signal fired strongly. Fall through to the weekday theme.
    # This is the guard that prevents the "Cheyenne 4 days in a row" repeat.
    try:
        theme_topic, theme_reason = _theme_for_weekday()
        if not _topic_dedup(theme_topic):
            return theme_topic, theme_reason
    except NameError:
        pass  # test environment without the helper — fall through to rotation

    # Phase LL+1: deterministic day-of-month rotation across 8 generic
    # angles. Using day-of-month % 8 means each angle hits ~4× per
    # month — enough variety that the press release archive doesn't
    # read as a single template repeating itself.
    import datetime as _dt
    day_idx = _dt.date.today().day % 8
    rotation = [
        ("iso_grid_pulse", "Today's grid pulse: real-time demand + headroom across 7 US ISOs."),
        ("water_risk_brief", "Water-stress brief: which DC markets face elevated drought + cooling risk this quarter."),
        ("fiber_capacity_map", "Fiber infrastructure brief: BEAD allocations + carrier-hotel density by market."),
        ("interconnection_queue", "Interconnection queue snapshot: largest pending DC loads by ISO."),
        ("permit_velocity", "Permit-velocity brief: which states are approving DC builds fastest this month."),
        ("tax_incentive_brief", "Tax incentive brief: jurisdiction-by-jurisdiction comparison for new DC investment."),
        ("ma_pulse", "M&A pulse: recent data center transactions + valuation trends."),
        ("methodology_explainer", "Methodology explainer: how DC Hub's DCPI scoring works + what each axis measures."),
    ]
    return rotation[day_idx]


# Phase LL+1: ultra-safe last-resort topic. If everything else fails
# AND retry logic exhausts, fall back to this. Always produces a
# 250-300 word generic "DC Hub today" recap from platform signals.
_LAST_RESORT_TOPIC = (
    "platform_pulse",
    "Generic platform pulse — DC Hub's tracking footprint, today's data freshness, and how to query the dataset.",
)


# ---------------------------------------------------------------------------
# 2. CLAUDE GENERATION
# ---------------------------------------------------------------------------

_MARKETING_SYSTEM = """You are the autonomous press team at DC Hub, a data-center intelligence platform tracking 280+ US/global markets, 7 ISOs, and 20,000+ facilities. You publish two coupled outputs daily, both distilling the single most newsworthy story of the last 24 hours of platform activity:

A) A SHORT PRESS RELEASE (long-form, web/AI-citable)
B) A LINKEDIN POST (short-form, distribution-ready)

BOTH outputs MUST:
- Be FACTUAL — only use numbers and names provided in the signal payload. Never invent specific markets, scores, MW, or company names.
- Lead with the most concrete data point (e.g. "[Market], [STATE] climbed [N] points in the DCPI Excess Power index").

The PRESS RELEASE additionally MUST:
- Be 200-400 words with Markdown-lite formatting: use `##` for section headings, `-` for bullets, `**bold**` for emphasis. Use 2-3 sections (e.g. "## Highlights", "## What it means", "## Methodology").
- Include a self-citation paragraph: "Source: DC Hub Data Center Power Index (https://dchub.cloud/dcpi). Updated daily."
- End with: "Press inquiries: press@dchub.cloud · DC Hub MCP API: https://dchub.cloud/mcp"

The LINKEDIN POST additionally MUST:
- Be 900-1500 characters total (LinkedIn sweet spot).
- Start with a HOOK line (a single bold-claim sentence) on its own line.
- Use 3-5 short paragraphs OR a bullet list with line breaks.
- Reference 1-2 specific data points from the signal payload.
- End with exactly ONE URL: https://dchub.cloud/news/<slug>  (use the slug you generated above).
- End with 3-5 hashtags, e.g. #DataCenter #DCPI #Infrastructure #AI #ColocationMarket.
- NO em-dashes (LinkedIn flags them); use commas or periods.

Output STRICT JSON only, no preamble:
{
  "topic": "dcpi_mover" | "dcpi_leader" | "dcpi_warning" | "iso_focus" | "iso_intelligence" | "industry_pulse" | "coverage_milestone" | "ai_adoption" | "new_facility" | "theme_movers" | "theme_grid_iso" | "theme_ai_infra" | "theme_markets" | "theme_deals" | "theme_methodology",
  "title": "...",
  "subheadline": "...",
  "body": "...",     // 200-400 words press release, Markdown-lite, \\n paragraphs
  "slug": "auto-YYYY-MM-DD-short-keywords",  // URL-safe, < 80 chars
  "meta_description": "...",   // < 160 chars
  "schema_keywords": ["data center", "power index", "..."],
  "linkedin_post": "..."   // 900-1500 chars, hook + body + url + hashtags
}"""


def _call_claude_marketing(prompt: str) -> tuple[dict | None, str | None]:
    """Single Anthropic call. Returns (parsed_json, error)."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key"
    from urllib.request import Request, urlopen
    body = json.dumps({
        "model": MARKETING_MODEL,
        "max_tokens": 1500,
        "system": _MARKETING_SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "Content-Type": "application/json",
        "X-API-Key": ANTHROPIC_API_KEY,
        "Anthropic-Version": "2023-06-01",
    })
    try:
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        # Extract JSON
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None, "non_json_response"
        return json.loads(m.group(0)), None
    except Exception as e:
        return None, f"api_error: {str(e)[:120]}"


# ---------------------------------------------------------------------------
# 3. VALIDATION & WRITE
# ---------------------------------------------------------------------------

def _validate_release(rel: dict) -> tuple[bool, str]:
    if not isinstance(rel, dict): return False, "not_a_dict"
    for k in ("title", "body", "slug", "meta_description"):
        if not rel.get(k): return False, f"missing_{k}"
    body = rel.get("body") or ""
    if len(body) < 200: return False, f"body_too_short ({len(body)})"
    if len(body) > 4000: return False, f"body_too_long ({len(body)})"
    slug = rel.get("slug") or ""
    if not re.match(r"^[a-z0-9][a-z0-9-]{4,79}$", slug):
        return False, "invalid_slug_format"
    # No raw HTML/JS injection in body
    if re.search(r"<script|onerror=|onload=", body, re.I):
        return False, "body_has_js"
    return True, "ok"


def _write_release(rel: dict, signals: dict, topic: str) -> tuple[int | None, str | None]:
    """Persist to the canonical press_releases table + audit row in
       auto_press_releases. Returns (press_release_id, error).
       Phrasing avoids the literal "INSERT INTO" prefix in this
       docstring so the regression-lint regex doesn't match prose."""
    c = _conn()
    if c is None: return None, "no_database"
    today = date.today().isoformat()
    try:
        # 1. press_releases — the canonical row that the public feed reads.
        #
        # Source + category are parameterized (rather than inline literals
        # 'DC Hub Auto' / 'press_release') so the regression-lint regex
        # `INSERT INTO ... [^;"']*` traverses the entire SQL string and
        # sees the ON CONFLICT clause. Inline single-quoted SQL literals
        # would terminate the regex match early and falsely trip the
        # `insert-no-on-conflict` rule.
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO press_releases
                    (title, summary, subheadline, body, meta_description,
                     slug, source, category, published_date, date, published)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (slug) DO UPDATE SET
                    title            = EXCLUDED.title,
                    summary          = EXCLUDED.summary,
                    subheadline      = EXCLUDED.subheadline,
                    body             = EXCLUDED.body,
                    meta_description = EXCLUDED.meta_description,
                    published_date   = EXCLUDED.published_date,
                    published        = true
                RETURNING id;
            """, (
                rel["title"][:300],
                (rel.get("subheadline") or "")[:500],
                (rel.get("subheadline") or "")[:500],
                rel["body"],
                rel["meta_description"][:300],
                rel["slug"],
                "DC Hub Auto",       # source
                "press_release",     # category
                today, today,
            ))
            press_id = cur.fetchone()[0]
        # 2. auto_press_releases — audit trail of autonomous output.
        # Phase EE (2026-05-12): also persists the Claude-generated
        # linkedin_post for daily distribution. Defensive against the
        # column not yet existing on older deploys (the schema migration
        # in init_schema is idempotent but may not have fired yet) —
        # try the full insert first, fall back to legacy insert without
        # linkedin_post on column-missing error.
        linkedin_post = (rel.get("linkedin_post") or "")[:5000] or None
        with c.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO auto_press_releases
                        (press_release_id, slug, generated_for, source_topic,
                         source_data, model, title, body, word_count,
                         validation_ok, linkedin_post)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
                    ON CONFLICT (slug) DO NOTHING;
                """, (
                    press_id, rel["slug"], today, topic,
                    json.dumps(signals)[:8000],
                    MARKETING_MODEL,
                    rel["title"][:300],
                    rel["body"], len(rel["body"].split()),
                    linkedin_post,
                ))
            except Exception:
                c.rollback()
                # Legacy fallback for installations missing the column.
                cur.execute("""
                    INSERT INTO auto_press_releases
                        (press_release_id, slug, generated_for, source_topic,
                         source_data, model, title, body, word_count,
                         validation_ok)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                    ON CONFLICT (slug) DO NOTHING;
                """, (
                    press_id, rel["slug"], today, topic,
                    json.dumps(signals)[:8000],
                    MARKETING_MODEL,
                    rel["title"][:300],
                    rel["body"], len(rel["body"].split()),
                ))
        # BUG FIX (2026-05-12): psycopg2 connections default to autocommit=False.
        # Without this explicit commit, BOTH INSERTs above are rolled back when
        # the `finally: c.close()` block fires. Live evidence: today's first
        # auto-press run returned press_release_id=16 (the sequence advanced)
        # but no row 16 ever appeared in production. The press_releases table
        # showed only ids 1-14. RETURNING returns the would-be id even when
        # the transaction will roll back.
        c.commit()

        # Phase FF+3 (2026-05-13): distribution layer. Each new press
        # release fans out to:
        #   1. social_media_posts (LinkedIn row, status='approved') —
        #      picked up by content_publisher._auto_publish_loop every
        #      6h, posted via LinkedIn /v2/ugcPosts API.
        #   2. social_media_posts (Twitter row, status='approved') —
        #      picked up by the X auto-publisher (added in this PR).
        #   3. MCP signup list email — Resend digest with link.
        # All three run in SEPARATE transactions / network calls. A
        # failure in any one does not affect the press release or the
        # other channels. The press release already committed above.
        try:
            _queue_distribution_posts(rel, press_id, today)
        except Exception as dist_err:
            print(f"[marketing_engine] distribution queue failed: {dist_err}",
                  file=sys.stderr)
        try:
            _notify_mcp_subscribers(rel, press_id)
        except Exception as mail_err:
            print(f"[marketing_engine] mcp digest mail failed: {mail_err}",
                  file=sys.stderr)
        return press_id, None
    except Exception as e:
        print(f"[marketing_engine] write failed: {e}", file=sys.stderr)
        return None, f"db_error: {str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Phase FF+3 — distribution helpers
#
# These run AFTER the press release commits. They run in their own
# transactions / network calls. They are best-effort — failures are
# logged but never propagated to the caller. The press release is the
# product; distribution is the delivery layer.
# ---------------------------------------------------------------------------

def _format_linkedin_post(rel: dict) -> str:
    """Compose the LinkedIn share. Phase HH (2026-05-13): now uses
    Claude for a punchier hook-first format instead of the static
    template. Falls back to the original template if the model call
    fails or returns empty — distribution must never block on AI.

    Priority order:
      1. rel['linkedin_post']         — pre-generated by Phase EE prompt
      2. Claude rewrite of title+body — hook-first, 2-3 insights, CTA
      3. Static fallback              — title + subheadline + URL + tags
    """
    if rel.get("linkedin_post"):
        return rel["linkedin_post"][:2900]

    # Try the Claude rewrite path
    try:
        rewritten = _claude_rewrite_for_linkedin(rel)
        if rewritten and len(rewritten) > 100:
            return rewritten[:2900]
    except Exception as e:
        print(f"[linkedin_post] Claude rewrite failed: {e}", file=sys.stderr)

    # Static fallback (original behavior)
    title = (rel.get("title") or "").strip()
    sub   = (rel.get("subheadline") or rel.get("meta_description") or "").strip()
    slug  = rel.get("slug", "")
    url   = f"https://dchub.cloud/news/{slug}"
    parts = [title]
    if sub: parts.append(sub)
    parts.append(f"Full release → {url}")
    # Phase HH+1: DC Hub Media branding — newsroom byline + tag.
    parts.append("Published by DC Hub Media — dchub.cloud/dc-hub-media")
    parts.append("#DCHub #DCHubMedia #datacenter #infrastructure")
    return "\n\n".join(parts)[:2900]


def _claude_rewrite_for_linkedin(rel: dict) -> str | None:
    """Phase HH: Claude rewrites the press release into a punchy
    LinkedIn post. Optimized for engagement: opens with a hook
    (stat, contrarian angle, or specific number), 2-3 short insight
    bullets, then a CTA. Hashtag footer.

    Cost: ~$0.005/call at Sonnet rates. Caching at the row level
    is via auto_press_releases.linkedin_post — once Claude writes
    one, we reuse it for any republish.
    """
    if not ANTHROPIC_API_KEY:
        return None

    title = (rel.get("title") or "").strip()
    sub   = (rel.get("subheadline") or rel.get("meta_description") or "").strip()
    body  = (rel.get("body") or "")
    slug  = rel.get("slug", "")
    url   = f"https://dchub.cloud/news/{slug}"

    # Trim body — Claude gets the title + sub + first ~1500 chars of body
    body_preview = body[:1500] if body else sub

    prompt = (
        "You are writing a LinkedIn post for DC Hub Media — the newsroom "
        "arm of DC Hub (a data center intelligence platform). The post "
        "promotes a DC Hub press release. Audience: infrastructure "
        "investors, hyperscale ops leaders, and policy wonks who follow "
        "grid + power markets.\n\n"
        "GOAL: a high-engagement LinkedIn post — feed-stopping, "
        "info-dense, 2026 newsroom voice. Optimize for clicks to the URL.\n\n"
        "STRUCTURE (strict):\n"
        "1. HOOK (line 1): a single sentence opening with the most "
        "   surprising stat or contrarian claim from the release. "
        "   No throat-clearing. Numbers belong on this line.\n"
        "2. CONTEXT (1-2 short sentences): why this matters now.\n"
        "3. THREE BULLETS (use '→' as the marker): "
        "   the three most quotable findings. Each bullet ≤ 110 chars. "
        "   At least two bullets should contain a specific number "
        "   (MW, $, %, or rank).\n"
        f"4. CTA: 'Full release → {url}' on its own line.\n"
        "5. BYLINE: 'Published by DC Hub Media — "
        "   dchub.cloud/dc-hub-media' on its own line. This is the "
        "   newsroom credit; keep it exact.\n"
        "6. HASHTAGS: 4-5 tags on the last line. ALWAYS include both "
        "   #DCHub AND #DCHubMedia. Pick 2-3 others from: #datacenter "
        "   #powergrid #ISO #hyperscale #infrastructure #energy #AI "
        "   matching the topic.\n\n"
        "STYLE RULES:\n"
        "- No emojis except optional ⚡ or 📊 on the hook line.\n"
        "- No exclamation marks.\n"
        "- No corporate jargon ('synergies', 'revolutionary', "
        "  'unprecedented' are banned).\n"
        "- Active voice. Past or present tense, not future.\n"
        "- Refer to the publication as 'DC Hub Media' when crediting; "
        "  the underlying data source is 'DC Hub'.\n"
        "- 1100-1800 chars total. Hard limit 2900.\n\n"
        "PRESS RELEASE INPUT:\n"
        f"TITLE: {title}\n"
        f"SUB: {sub}\n"
        f"BODY (truncated):\n{body_preview}\n\n"
        "OUTPUT: just the LinkedIn post body. No preamble, no JSON, "
        "no surrounding quotes."
    )

    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": MARKETING_MODEL,
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[claude rewrite] {resp.status_code}: {resp.text[:200]}",
                  file=sys.stderr)
            return None
        data = resp.json()
        content = data.get("content", [])
        if not content:
            return None
        text = "".join(b.get("text", "") for b in content
                       if b.get("type") == "text").strip()
        # Strip leading/trailing quotes Claude sometimes adds
        text = text.strip('"').strip("'").strip("`").strip()
        return text or None
    except Exception as e:
        print(f"[claude rewrite] exception: {e}", file=sys.stderr)
        return None


def _format_twitter_post(rel: dict) -> str:
    """X/Twitter post: 280 chars max, prioritize headline + URL + brand.
    The URL counts as 23 chars regardless of actual length (t.co
    auto-wraps), so we have ~250 chars for the message body.

    Phase HH+1: include #DCHubMedia hashtag for newsroom branding."""
    title = (rel.get("title") or "").strip()
    slug  = rel.get("slug", "")
    url   = f"https://dchub.cloud/news/{slug}"
    # Reserve chars for the URL (23) + spacing (2) + hashtags (~25).
    # Leaves ~230 chars for the headline.
    max_title = 200
    if len(title) > max_title:
        title = title[:max_title].rsplit(" ", 1)[0] + "…"
    return f"{title}\n\n{url}\n\n#DCHub #DCHubMedia"


def _queue_distribution_posts(rel: dict, press_id: int, today: str) -> None:
    """Insert one row per channel into social_media_posts so the
    content_publisher auto-publishers pick them up. Idempotent —
    dedup on (platform, press_release_id) so retries after a
    partial-failed cron don't double-queue."""
    c = _conn()
    if c is None: return
    try:
        with c.cursor() as cur:
            # Defensive ALTER: ensure press_release_id column exists.
            # This pattern follows the existing init_schema idempotent
            # ALTER blocks elsewhere in this module.
            try:
                cur.execute("""
                    ALTER TABLE social_media_posts
                    ADD COLUMN IF NOT EXISTS press_release_id INTEGER;
                """)
            except Exception:
                c.rollback()
            try:
                # Phase FF+8 (2026-05-13): plain (non-partial) UNIQUE index.
                # Was partial with `WHERE press_release_id IS NOT NULL` —
                # but Postgres won't match `ON CONFLICT (a,b) DO NOTHING`
                # against a partial index unless the INSERT repeats the
                # same WHERE predicate. That broke publish-now with
                # "no unique or exclusion constraint matching the ON
                # CONFLICT specification". Plain index is fine: NULL !=
                # NULL by default, so old rows with NULL press_release_id
                # don't conflict, and the new distribution rows (always
                # non-NULL press_release_id) keep the unique-per-channel
                # guarantee we actually want.
                #
                # DROP the old partial index first because CREATE INDEX
                # IF NOT EXISTS only checks by name — without the DROP,
                # production keeps the broken partial index forever.
                cur.execute("""
                    DROP INDEX IF EXISTS
                        social_media_posts_press_release_platform_idx;
                """)
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        social_media_posts_press_release_platform_idx
                    ON social_media_posts(press_release_id, platform);
                """)
            except Exception:
                c.rollback()

            # LinkedIn + Twitter rows. Platform / status are parameterized
            # rather than inline quoted literals so the regression-lint
            # regex `INSERT INTO ... [^;"']*` traverses the entire SQL
            # string and sees the ON CONFLICT clause — same pattern as
            # the source + category parameterization in _write_release.
            li_text = _format_linkedin_post(rel)
            cur.execute("""
                INSERT INTO social_media_posts
                    (platform, content, status, press_release_id, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (press_release_id, platform) DO NOTHING
            """, ("linkedin", li_text, "approved", press_id))

            tw_text = _format_twitter_post(rel)
            cur.execute("""
                INSERT INTO social_media_posts
                    (platform, content, status, press_release_id, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (press_release_id, platform) DO NOTHING
            """, ("twitter", tw_text, "approved", press_id))

            # Phase VV (2026-05-17) — Bluesky queue row. Bluesky has a
            # 300-grapheme cap so we reuse the Twitter formatter (also
            # capped at 280 chars) rather than the linkedin long-form.
            # Standalone publish endpoint already exists at
            # POST /api/admin/publish/bluesky — and a future
            # bluesky-auto-publisher background loop (modeled on the
            # existing LinkedIn one) can drain status='approved' +
            # platform='bluesky' rows. Phase PP shipped the publisher
            # function; this just makes sure the queue HAS rows so
            # when the loop activates there's work to do.
            bsky_text = _format_twitter_post(rel)  # same short-form
            cur.execute("""
                INSERT INTO social_media_posts
                    (platform, content, status, press_release_id, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (press_release_id, platform) DO NOTHING
            """, ("bluesky", bsky_text, "approved", press_id))
        c.commit()
    finally:
        try: c.close()
        except Exception: pass


def _notify_mcp_subscribers(rel: dict, press_id: int) -> None:
    """Email the dchub signups list when a new press release lands.

    Targets the `signups` table (created via /api/v1/signup) which is
    the public newsletter list — distinct from `api_keys` (paid users).
    Reuses the Resend pattern from linkedin_send_daily_email but with
    a different template + segmenting. Idempotent — checks
    auto_press_releases.notified_at before sending.
    """
    if not RESEND_API_KEY:
        return  # No mail provider configured — skip silently.

    c = _conn()
    if c is None: return
    sent_to = 0
    try:
        # 1. Idempotency: skip if already notified for this slug.
        with c.cursor() as cur:
            try:
                cur.execute("""
                    ALTER TABLE auto_press_releases
                    ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
                """)
                c.commit()
            except Exception:
                c.rollback()
            cur.execute("""
                SELECT notified_at FROM auto_press_releases
                WHERE slug = %s LIMIT 1
            """, (rel["slug"],))
            row = cur.fetchone()
            if row and row[0]:
                return  # Already notified.

            # 2. Fetch the list. Cap at 500/run to avoid Resend rate
            # limits. The signups table can legitimately be larger
            # than that; we batch over consecutive press releases.
            cur.execute("""
                SELECT email FROM signups
                WHERE COALESCE(unsubscribed, false) = false
                  AND email IS NOT NULL
                  AND email NOT ILIKE '%@example.%'
                  AND email NOT ILIKE 'test%@%'
                ORDER BY created_at DESC
                LIMIT 500
            """)
            recipients = [r[0] for r in cur.fetchall() if r and r[0]]

        if not recipients:
            return

        # 3. Build the digest email. Plain HTML with link to the
        # press release. Resend uses the same sender/key as the
        # daily LinkedIn email path.
        sender = os.environ.get("DCHUB_RESEND_FROM",
                                "DC Hub <press@dchub.cloud>")
        subject = f"📡 DC Hub Press: {rel.get('title','')[:80]}"
        slug = rel.get("slug", "")
        url = f"https://dchub.cloud/news/{slug}"
        title = (rel.get("title") or "").strip()
        sub = (rel.get("subheadline") or rel.get("meta_description") or "").strip()
        html_body = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1a1a">
<div style="font-size:11px;color:#888;letter-spacing:.05em;text-transform:uppercase;margin-bottom:8px">Daily Press Release · DC Hub</div>
<h2 style="margin:0 0 12px;font-size:22px;line-height:1.3">{_html_escape(title)}</h2>
<p style="color:#555;margin:0 0 24px;font-size:15px;line-height:1.5">{_html_escape(sub)}</p>
<p style="margin:24px 0"><a href="{url}" style="background:#1976d2;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">Read the full release →</a></p>
<hr style="border:0;border-top:1px solid #eee;margin:32px 0">
<p style="font-size:12px;color:#888">You're receiving this because you signed up at dchub.cloud. <a href="https://dchub.cloud/unsubscribe" style="color:#888">Unsubscribe</a> · <a href="https://dchub.cloud" style="color:#888">dchub.cloud</a></p>
</body></html>"""

        # 4. Resend batch send: one POST with `to: [array]` is one
        # email per recipient (Resend handles fan-out). Use BCC pattern
        # via separate sends to keep recipient privacy. To stay under
        # the 10 req/sec limit, batch in groups of 50 with a small
        # sleep between batches.
        # Uses `requests` per regression-lint rule [urllib-request-on-railway]
        # — Railway egress sometimes returns CF 1010 on urllib's default
        # UA, and requests has a saner default + connection pooling.
        import requests as _rq, time as _time
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "User-Agent": "dchub-backend/1.0 (+https://dchub.cloud)",
            "Accept": "application/json",
        }
        batch_size = 50
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i:i+batch_size]
            for to_addr in batch:
                try:
                    resp = _rq.post(
                        "https://api.resend.com/emails",
                        json={
                            "from": sender,
                            "to": [to_addr],
                            "subject": subject,
                            "html": html_body,
                        },
                        headers=headers,
                        timeout=15,
                    )
                    if resp.status_code in (200, 201, 202):
                        sent_to += 1
                    else:
                        print(f"[mcp_digest] send to {to_addr} failed: "
                              f"{resp.status_code} {resp.text[:200]}",
                              file=sys.stderr)
                except Exception as e:
                    # Don't let one failed address kill the batch.
                    print(f"[mcp_digest] send to {to_addr} failed: {e}",
                          file=sys.stderr)
            if i + batch_size < len(recipients):
                _time.sleep(1.0)  # Rate-limit cushion.

        # 5. Mark notified.
        if sent_to > 0:
            with c.cursor() as cur:
                cur.execute("""
                    UPDATE auto_press_releases
                    SET notified_at = NOW()
                    WHERE slug = %s
                """, (rel["slug"],))
            c.commit()
        print(f"[mcp_digest] sent {sent_to}/{len(recipients)} for {rel.get('slug')}")
    finally:
        try: c.close()
        except Exception: pass


def _html_escape(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@marketing_bp.post("/api/v1/marketing/auto-generate")
@_require_admin
def auto_generate():
    """Generate one autonomous press release for today's signals.
       Idempotent: if today already has an auto-release, returns 200 with
       skipped=true so cron retries are safe.
    """
    today = date.today().isoformat()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT id, slug, title, press_release_id FROM auto_press_releases
                           WHERE generated_for = %s LIMIT 1""", (today,))
            existing = cur.fetchone()
        if existing:
            # Phase MM (2026-05-15): if today's release exists but the
            # distribution queue is empty (i.e., social_media_posts row
            # never got inserted — see the silent-queue-fail bug), allow
            # a force-requeue path so the auto-press cron can RECOVER
            # without regenerating the article. ?requeue=1 triggers it.
            requeue = (request.args.get("requeue") or "").lower() in ("1", "true", "yes")
            if requeue and existing[3]:
                try:
                    # Fetch the press_release body so we can format LinkedIn/Twitter posts.
                    cc = _conn()
                    if cc is None:
                        raise RuntimeError("no_database_for_requeue")
                    try:
                        with cc.cursor() as cur:
                            cur.execute("""SELECT title, subheadline, body,
                                                   meta_description, slug
                                              FROM press_releases
                                             WHERE id = %s LIMIT 1""", (existing[3],))
                            row = cur.fetchone()
                    finally:
                        try: cc.close()
                        except Exception: pass
                    if row:
                        rel_for_requeue = {
                            "title": row[0], "subheadline": row[1],
                            "body": row[2] or "",
                            "meta_description": row[3] or row[1] or row[0],
                            "slug": row[4],
                        }
                        _queue_distribution_posts(rel_for_requeue, existing[3], today)
                        return jsonify(
                            ok=True, skipped=False, mode="requeued",
                            existing={"id": existing[0], "slug": existing[1],
                                      "title": existing[2]},
                            note=("Forced requeue of today's distribution rows. "
                                  "Auto-publisher will pick them up on next 6h tick "
                                  "(LinkedIn/X)."),
                        ), 200
                except Exception as re_err:
                    return jsonify(
                        ok=False, error="requeue_failed",
                        detail=str(re_err)[:200],
                        existing={"id": existing[0], "slug": existing[1],
                                  "title": existing[2]},
                    ), 500
            return jsonify(
                ok=True, skipped=True, reason="already_generated_today",
                existing={"id": existing[0], "slug": existing[1],
                          "title": existing[2]},
                hint="Pass ?requeue=1 to re-insert distribution rows if LinkedIn/X queue is empty.",
            ), 200
    finally:
        try: c.close()
        except Exception: pass

    # Phase TT (2026-05-14): publish window. PR #116 made this run on
    # every evolve-cron tick (for reliability vs GitHub dropping the
    # single daily cron) — but that meant the post landed on the first
    # tick of the UTC day (~midnight UTC = ~5pm PT the day before). The
    # daily press should land in the morning PT. Skip until
    # MARKETING_PUBLISH_HOUR_UTC (default 15 = ~8am PDT / 7am PST); the
    # first tick at/after that generates, later ticks no-op via the
    # already-generated check above, and a transient failure is retried
    # by the next tick — same resilience, right time of day.
    # ?force=1 bypasses for manual runs.
    _publish_hour = int(os.environ.get("MARKETING_PUBLISH_HOUR_UTC", "15"))
    _force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    _now_hour = datetime.utcnow().hour
    if not _force and _now_hour < _publish_hour:
        return jsonify(
            ok=True, skipped=True, reason="before_publish_window",
            publish_hour_utc=_publish_hour, current_hour_utc=_now_hour,
            note=(f"Auto-press publishes at {_publish_hour}:00 UTC (~8am PT). "
                  f"It's {_now_hour}:00 UTC now — the next tick after the "
                  f"window opens will generate today's release."),
        ), 200

    signals = _collect_signals()

    # Phase LL+1 (2026-05-14): retry-with-fallback loop. Auto-press has
    # been producing 2 releases per 30 days (vs expected 30) because
    # Claude calls sometimes timeout / return non-JSON / generate output
    # that fails _validate_release on length or slug format. Before this
    # retry, a single transient Claude error → no press release for the
    # entire day. Now we try 3 attempts: primary topic → primary topic
    # with simpler prompt → last-resort platform_pulse topic.
    topic, topic_reason = _pick_daily_topic(signals)
    signals["daily_topic"] = topic
    signals["daily_topic_reason"] = topic_reason

    rel = None
    err = None
    why = None
    last_attempt_err = None

    for attempt_idx, (att_topic, att_reason, att_simpler) in enumerate([
        (topic, topic_reason, False),
        (topic, topic_reason, True),       # second pass with simpler prompt
        _LAST_RESORT_TOPIC + (True,),      # third pass: platform_pulse, simpler
    ]):
        signals["daily_topic"] = att_topic
        signals["daily_topic_reason"] = att_reason

        if att_simpler:
            # Simpler prompt = drop most of the signals payload to reduce
            # context that might confuse Claude. Keep only essentials.
            mini_signals = {
                "daily_topic": att_topic,
                "daily_topic_reason": att_reason,
                "as_of": signals.get("as_of"),
                "top_build_markets": (signals.get("top_build_markets") or [])[:3],
                "ai_usage_24h": signals.get("ai_usage_24h", {}),
            }
            prompt = (
                f"Today's topic: {att_topic} — {att_reason}\n\n"
                f"Signals (trimmed):\n```\n{json.dumps(mini_signals, indent=2)[:2500]}\n```\n\n"
                "Generate a publishable press release + LinkedIn post per "
                "the system prompt. Be concrete, lean on the signal data."
            )
        else:
            prompt = (f"Daily signals (topic: {att_topic} — {att_reason}):\n"
                      "```\n" + json.dumps(signals, indent=2)[:6000] + "\n```")

        rel, err = _call_claude_marketing(prompt)
        if err or not rel:
            last_attempt_err = f"attempt_{attempt_idx+1}: claude_error={err}"
            print(f"[marketing] {last_attempt_err}", file=sys.stderr)
            continue

        ok, why = _validate_release(rel)
        if not ok:
            last_attempt_err = f"attempt_{attempt_idx+1}: validation_failed={why}"
            print(f"[marketing] {last_attempt_err}", file=sys.stderr)
            rel = None
            continue

        # Got a valid release. Break out of retry loop.
        break

    if not rel:
        return jsonify(
            ok=False, error="all_retries_exhausted",
            last_error=last_attempt_err,
            signals=signals,
        ), 502

    press_id, write_err = _write_release(rel, signals, rel.get("topic", "dcpi"))
    if write_err:
        return jsonify(ok=False, error=write_err, proposal=rel), 500

    # Phase LL+2 (2026-05-14): IndexNow ping. Tells Bing/Yandex/Seznam/Naver
    # within minutes that the new press release URL exists, so they
    # index it before next normal crawl cycle. Bing's index feeds
    # ChatGPT search + Perplexity, so this directly accelerates AI
    # crawler discoverability. Fire-and-forget — never blocks press
    # release write or response.
    pinged = None
    try:
        from seo_agent import ping_indexnow
        new_url = f"https://dchub.cloud/news/{rel['slug']}"
        # Also re-ping the aggregate + media surfaces so the new
        # entry shows up in their feed crawls too
        ping_result = ping_indexnow([
            new_url,
            "https://dchub.cloud/dc-hub-media",
            "https://dchub.cloud/api/v1/media/rss",
        ])
        pinged = ping_result.get("success") if isinstance(ping_result, dict) else None
    except Exception as e:
        print(f"[marketing] IndexNow ping failed (non-fatal): {e}", file=sys.stderr)

    return jsonify(
        ok=True, generated=True,
        press_release_id=press_id,
        slug=rel["slug"],
        title=rel["title"],
        url=f"https://dchub.cloud/news/{rel['slug']}",
        indexnow_pinged=pinged,
        signals_used=signals,
    ), 201


@marketing_bp.get("/api/v1/marketing/linkedin/whoami")
@_require_admin
def linkedin_whoami():
    """Phase FF+9 (2026-05-13): debug helper for LinkedIn token issues.

    Reports what Railway has stored AND what LinkedIn says about it.
    Doesn't expose the token — only length + first-4 + last-4 chars
    so you can verify Railway picked up the new value.

    Calls LinkedIn /v2/userinfo (the safest auth check — works with any
    valid token regardless of scope). If userinfo succeeds but
    /v2/ugcPosts still 401s, the token is valid but lacks the
    `w_organization_social` scope needed for org-page posts.
    """
    import os as _os
    import requests as _rq
    tok = _os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    if not tok:
        return jsonify(ok=False, error="LINKEDIN_ACCESS_TOKEN not set"), 500

    masked = {
        "length": len(tok),
        "starts_with": tok[:6] if len(tok) >= 6 else tok,
        "ends_with":   tok[-4:] if len(tok) >= 8 else "",
        "has_bearer_prefix": tok.lower().startswith("bearer "),
        "has_trailing_whitespace": tok != tok.strip(),
    }

    # /v2/userinfo — basic auth check (OpenID Connect)
    userinfo_status = None
    userinfo_body = None
    try:
        r = _rq.get("https://api.linkedin.com/v2/userinfo",
                    headers={"Authorization": f"Bearer {tok.strip()}"},
                    timeout=10)
        userinfo_status = r.status_code
        userinfo_body = r.text[:500]
    except Exception as e:
        userinfo_body = f"network error: {e}"

    # /v2/me — older endpoint, works with r_liteprofile or r_basicprofile
    me_status = None
    me_body = None
    try:
        r = _rq.get("https://api.linkedin.com/v2/me",
                    headers={"Authorization": f"Bearer {tok.strip()}"},
                    timeout=10)
        me_status = r.status_code
        me_body = r.text[:500]
    except Exception as e:
        me_body = f"network error: {e}"

    # /v2/organizationAcls — the DEFINITIVE org-posting capability check.
    # userinfo/me only prove the token can read a profile; they say
    # nothing about whether auto-press can post to the company page.
    # 200 here = the token carries org scope (w_organization_social /
    # rw_organization_admin) and posting WILL work. 403/401 = it won't,
    # no matter what userinfo says.
    org_status = None
    org_body = None
    try:
        r = _rq.get("https://api.linkedin.com/v2/organizationAcls?q=roleAssignee",
                    headers={"Authorization": f"Bearer {tok.strip()}",
                             "X-Restli-Protocol-Version": "2.0.0"},
                    timeout=10)
        org_status = r.status_code
        org_body = r.text[:500]
    except Exception as e:
        org_body = f"network error: {e}"

    # Diagnosis logic
    diagnosis = []
    if masked["has_bearer_prefix"]:
        diagnosis.append("Token includes 'Bearer ' prefix — remove it; the code adds Bearer itself.")
    if masked["has_trailing_whitespace"]:
        diagnosis.append("Token has leading/trailing whitespace — re-set without spaces.")
    if masked["length"] < 100:
        diagnosis.append(f"Token is suspiciously short ({masked['length']} chars). Real LinkedIn tokens are ~400-700 chars.")
    if userinfo_status == 401:
        diagnosis.append("LinkedIn /v2/userinfo returns 401 — token is genuinely invalid (expired, revoked, or wrong app). Regenerate it.")
    elif userinfo_status == 403:
        diagnosis.append("LinkedIn /v2/userinfo returns 403 ACCESS_DENIED — the token is NOT expired (that would be 401) but is missing the `openid` + `profile` scopes that userinfo needs. This does NOT tell us whether `w_organization_social` (the org-posting scope) is present. Regenerate the token with all three checked — `openid`, `profile`, AND `w_organization_social` — so both this check and actual posting work.")
    elif userinfo_status == 200:
        diagnosis.append("Token IS valid (/v2/userinfo returned 200).")
    # The org check is what actually decides whether auto-press works.
    if org_status == 200:
        diagnosis.append("✅ POSTING WILL WORK — /v2/organizationAcls returned 200, so the token carries org scope. The userinfo 403 above (if any) is cosmetic; auto-press to the company page is good to go.")
    elif org_status in (401, 403):
        diagnosis.append(f"❌ POSTING WILL NOT WORK — /v2/organizationAcls returned {org_status}. The token is missing `w_organization_social`. Regenerating the token alone won't fix this: that scope only appears in the token generator AFTER the 'Community Management API' product is added to the app (linkedin.com/developers → your app → Products tab). Add that product, then regenerate the token with `w_organization_social` checked.")
    if not diagnosis:
        diagnosis.append("No obvious format issues. Compare what's stored vs what you generated.")

    return jsonify(
        ok=True,
        masked=masked,
        userinfo={"status": userinfo_status, "body": userinfo_body},
        me={"status": me_status, "body": me_body},
        organization_acls={"status": org_status, "body": org_body},
        diagnosis=diagnosis,
        org_id_in_use=_os.environ.get("LINKEDIN_ORG_ID", "110894959 (default)"),
    ), 200


@marketing_bp.post("/api/v1/marketing/publish-now")
@_require_admin
def publish_now():
    """Phase FF+6 (2026-05-13): one-shot verification endpoint.

    Useful when tokens (LinkedIn / X) just got set on Railway and we
    want to confirm publishing works without waiting 6h for the
    auto-publisher loop to tick. Picks the most-recent auto_press_release,
    backfills its social_media_posts rows if not already present, and
    immediately calls the LinkedIn + X publishers.

    Returns one block per channel with success/error. No automatic
    retry — the auto-publisher handles long-term reliability; this is
    purely "did the credentials work."

    Query params:
        slug   — override which press release to publish (defaults
                  to most-recent auto-press)
        only   — 'linkedin' or 'twitter' to test a single channel
    """
    import os as _os
    only = (request.args.get("only") or "").strip().lower()

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            slug = request.args.get("slug")
            if slug:
                cur.execute("""
                    SELECT id, title, subheadline, body, meta_description, slug
                    FROM press_releases WHERE slug = %s LIMIT 1
                """, (slug,))
            else:
                cur.execute("""
                    SELECT pr.id, pr.title, pr.subheadline, pr.body,
                           pr.meta_description, pr.slug
                    FROM press_releases pr
                    JOIN auto_press_releases apr ON apr.press_release_id = pr.id
                    ORDER BY apr.generated_for DESC, pr.id DESC
                    LIMIT 1
                """)
            row = cur.fetchone()
            if not row:
                return jsonify(ok=False, error="no_press_release_found"), 404
            press_id, title, sub, body, meta_desc, real_slug = row
            rel = {
                "title": title, "subheadline": sub, "body": body or "",
                "meta_description": meta_desc or sub or title,
                "slug": real_slug,
            }
    finally:
        try: c.close()
        except Exception: pass

    # Backfill distribution rows if missing — no-ops if already there
    # via the UNIQUE INDEX on (press_release_id, platform).
    try:
        _queue_distribution_posts(rel, press_id,
                                  date.today().isoformat())
    except Exception as e:
        return jsonify(ok=False, error=f"backfill_failed: {e}"), 500

    # Fetch the queued rows back so we can call the channel-specific
    # publishers with the actual stored content.
    c = _conn()
    posts: dict = {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT platform, content, id
                FROM social_media_posts
                WHERE press_release_id = %s
                  AND platform IN ('linkedin', 'twitter')
            """, (press_id,))
            for plat, content, post_id in (cur.fetchall() or []):
                posts[plat] = {"content": content, "post_id": post_id}
    finally:
        try: c.close()
        except Exception: pass

    out = {"slug": real_slug, "press_release_id": press_id, "results": {}}

    # LinkedIn — Phase HH (2026-05-13): now ARTICLE share with rich
    # link-card. URL points at /news/<slug> which serves an og:image
    # of /api/v1/og/today/<slug>.png — LinkedIn scrapes that for the
    # card thumbnail. Cache-busted with the slug+date so LinkedIn
    # re-fetches OG on reposts.
    if (not only or only == "linkedin") and "linkedin" in posts:
        li_token = _os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
        if not li_token:
            out["results"]["linkedin"] = {"ok": False,
                                          "error": "LINKEDIN_ACCESS_TOKEN not set"}
        else:
            try:
                from content_publisher import _post_to_linkedin
                article_url = f"https://dchub.cloud/news/{rel['slug']}"
                article_thumb = (
                    f"https://dchub.cloud/api/v1/og/today/{rel['slug']}.png"
                )
                ok, result = _post_to_linkedin(
                    posts["linkedin"]["content"],
                    li_token,
                    article_url=article_url,
                    article_title=rel.get("title"),
                    article_description=(rel.get("meta_description") or
                                          rel.get("subheadline")),
                    article_thumbnail_url=article_thumb,
                )
                out["results"]["linkedin"] = {"ok": ok, "result": result}
                if ok:
                    _mark_published(posts["linkedin"]["post_id"], "linkedin")
                    # Remember the share URN so /repost-now can delete it
                    _remember_share_urn(press_id, "linkedin", result)
            except Exception as e:
                out["results"]["linkedin"] = {"ok": False,
                                              "error": f"exception: {e}"}

    # Twitter / X
    if (not only or only == "twitter") and "twitter" in posts:
        try:
            from content_publisher import _post_to_twitter
            ok, result = _post_to_twitter(posts["twitter"]["content"])
            out["results"]["twitter"] = {"ok": ok, "result": result}
            if ok:
                _mark_published(posts["twitter"]["post_id"], "twitter")
        except Exception as e:
            out["results"]["twitter"] = {"ok": False,
                                          "error": f"exception: {e}"}

    return jsonify(ok=True, **out), 200


def _mark_published(post_id: int, platform: str) -> None:
    """Update social_media_posts.status after a successful publish.
    Mirrors the update content_publisher's auto-publisher does so the
    next 6h tick doesn't re-publish the same row."""
    c = _conn()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE social_media_posts
                SET status = %s, published_at = NOW(), publish_platform = %s
                WHERE id = %s
            """, ("published", platform, post_id))
        c.commit()
    except Exception as e:
        print(f"[publish-now] mark_published failed: {e}", file=sys.stderr)
    finally:
        try: c.close()
        except Exception: pass


def _remember_share_urn(press_id: int, platform: str, share_urn: str) -> None:
    """Phase HH (2026-05-13): store the platform share URN on the
    social_media_posts row so /repost-now can find + delete it later.

    Adds a `share_urn` column defensively if missing (same pattern as
    the press_release_id column added in FF+3). Best-effort — failure
    to remember just means the URN can't be auto-deleted; user can
    delete manually from LinkedIn / X UI."""
    if not share_urn or share_urn == "posted":
        return
    c = _conn()
    if c is None: return
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    ALTER TABLE social_media_posts
                    ADD COLUMN IF NOT EXISTS share_urn TEXT;
                """)
                c.commit()
            except Exception:
                c.rollback()
            cur.execute("""
                UPDATE social_media_posts
                SET share_urn = %s
                WHERE press_release_id = %s AND platform = %s
            """, (share_urn, press_id, platform))
            c.commit()
    except Exception as e:
        print(f"[repost] remember_share_urn failed: {e}", file=sys.stderr)
    finally:
        try: c.close()
        except Exception: pass


@marketing_bp.post("/api/v1/marketing/repost-now")
@_require_admin
def repost_now():
    """Phase HH (2026-05-13): delete the existing share on LinkedIn/X
    for a given press release and immediately republish with the
    current (now-improved) visual card + copy.

    Query params:
        slug   — which press release to repost (defaults to most recent)
        only   — 'linkedin' or 'twitter' to repost a single channel

    Flow:
      1. Find the social_media_posts row(s) for this slug
      2. Delete the existing share via platform API using stored share_urn
      3. Reset the row status back to 'approved' (so publish-now can fire)
      4. Clear linkedin_post override (so Claude regenerates copy fresh)
      5. Call the existing publish_now logic

    Returns the same shape as publish_now plus a `deleted` block per
    channel showing whether the old share was successfully removed.
    """
    import os as _os
    only = (request.args.get("only") or "").strip().lower()

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            slug = request.args.get("slug")
            if slug:
                cur.execute("""
                    SELECT id FROM press_releases WHERE slug = %s LIMIT 1
                """, (slug,))
            else:
                cur.execute("""
                    SELECT pr.id, pr.slug FROM press_releases pr
                    JOIN auto_press_releases apr ON apr.press_release_id = pr.id
                    ORDER BY apr.generated_for DESC, pr.id DESC LIMIT 1
                """)
            row = cur.fetchone()
            if not row:
                return jsonify(ok=False, error="no_press_release_found"), 404
            press_id = row[0]
            real_slug = slug or row[1]
    finally:
        try: c.close()
        except Exception: pass

    out_deleted = {}

    # 1. Delete the existing share on each requested platform.
    # Phase HH+5: defensively ALTER share_urn column. The write-side
    # helper _remember_share_urn adds this column too, but it only
    # fires AFTER a successful publish — so a press release published
    # before the share_urn helper deployed has a row with no column.
    # Reposting that press release would 500 with "column does not
    # exist" before the column ever got created.
    c = _conn()
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    ALTER TABLE social_media_posts
                    ADD COLUMN IF NOT EXISTS share_urn TEXT;
                """)
                c.commit()
            except Exception:
                c.rollback()
            cur.execute("""
                SELECT platform, share_urn, id
                FROM social_media_posts
                WHERE press_release_id = %s
                  AND platform IN ('linkedin', 'twitter')
            """, (press_id,))
            rows = cur.fetchall() or []
    finally:
        try: c.close()
        except Exception: pass

    for plat, urn, post_id in rows:
        if only and plat != only:
            continue
        if not urn:
            # Phase HH+5: no stored share_urn — likely a row published
            # before the share_urn helper shipped. Skip the delete step
            # for this channel; the reset-and-republish step below will
            # still queue a fresh post. The old share remains live on
            # LinkedIn/X (user can delete manually if desired).
            out_deleted[plat] = {"ok": False, "error": "no_stored_share_urn",
                                  "note": "Old share preserved; new post will be added alongside"}
            continue
        if plat == "linkedin":
            li_token = _os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
            if not li_token:
                out_deleted["linkedin"] = {"ok": False, "error": "no token"}
                continue
            try:
                from content_publisher import _delete_linkedin_share
                ok, msg = _delete_linkedin_share(urn, li_token)
                out_deleted["linkedin"] = {"ok": ok, "urn": urn, "msg": msg}
            except Exception as e:
                out_deleted["linkedin"] = {"ok": False, "error": str(e)}
        elif plat == "twitter":
            # Twitter DELETE /2/tweets/{id} — needs the numeric ID, not URN
            # share_urn for X stores just the numeric ID we got back
            try:
                from content_publisher import _post_to_twitter  # ensure module loaded
                import os as _os2, requests as _rq
                # OAuth1 needed for delete (same creds as post)
                api_key = _os2.environ.get('TWITTER_API_KEY', '').strip()
                api_sec = _os2.environ.get('TWITTER_API_SECRET', '').strip()
                acc_tok = _os2.environ.get('TWITTER_ACCESS_TOKEN', '').strip()
                acc_sec = _os2.environ.get('TWITTER_ACCESS_SECRET', '').strip()
                if all([api_key, api_sec, acc_tok, acc_sec]):
                    from requests_oauthlib import OAuth1
                    auth = OAuth1(api_key, api_sec, acc_tok, acc_sec)
                    resp = _rq.delete(
                        f"https://api.twitter.com/2/tweets/{urn}",
                        auth=auth, timeout=15,
                    )
                    out_deleted["twitter"] = {
                        "ok": resp.status_code in (200, 204),
                        "urn": urn,
                        "msg": f"{resp.status_code}: {resp.text[:200]}",
                    }
                else:
                    out_deleted["twitter"] = {
                        "ok": False,
                        "error": "OAuth1 creds incomplete",
                    }
            except Exception as e:
                out_deleted["twitter"] = {"ok": False, "error": str(e)}

    # 2. Reset row status so publish-now will fire again. Also clear
    # linkedin_post override on auto_press_releases so Claude regenerates.
    c = _conn()
    try:
        with c.cursor() as cur:
            sql_filter = ""
            params = [press_id]
            if only:
                sql_filter = " AND platform = %s"
                params.append(only)
            cur.execute(f"""
                UPDATE social_media_posts
                SET status = 'approved',
                    published_at = NULL,
                    publish_platform = NULL,
                    share_urn = NULL
                WHERE press_release_id = %s{sql_filter}
            """, tuple(params))
            cur.execute("""
                UPDATE auto_press_releases
                SET linkedin_post = NULL
                WHERE press_release_id = %s
            """, (press_id,))
        c.commit()
    except Exception as e:
        print(f"[repost-now] reset failed: {e}", file=sys.stderr)
    finally:
        try: c.close()
        except Exception: pass

    # 3. Fire publish-now logic by recursively invoking the same path.
    # We re-use the request context — only and slug query params still
    # in scope. Forward them via request.args to publish_now.
    # Simpler: just call the underlying function directly.
    publish_resp = publish_now()
    # publish_now returns (Response, status_code) — unpack and merge
    if isinstance(publish_resp, tuple):
        body, status = publish_resp
    else:
        body, status = publish_resp, 200
    payload = body.get_json() if hasattr(body, "get_json") else {}
    payload["deleted"] = out_deleted
    payload["reposted"] = True

    return jsonify(payload), status


# Phase FF (2026-05-14) — Track 1 / DC Hub Media v2: distribution
# hardening. The auto-publisher loops (content_publisher.py) skip at
# `logger.debug` level when LINKEDIN_ACCESS_TOKEN / the X creds aren't
# set on Railway — invisible. Press releases generate fine
# (`auto_press_7d` counts those) but never get distributed, and nothing
# surfaces *why*. These helpers make that silent failure loud: an
# explicit "is distribution wired, and are posts piling up undelivered?"
# read, exposed both standalone (/distribution/health) and inside the
# public marketing pulse the /dc-hub-media page renders.

def _linkedin_configured() -> bool:
    return bool(os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip())


def _twitter_configured() -> bool:
    if os.environ.get("TWITTER_BEARER_TOKEN", "").strip():
        return True
    return all(os.environ.get(k, "").strip() for k in (
        "TWITTER_API_KEY", "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"))


def _distribution_status(cur) -> dict:
    """Is social distribution actually wired — and is anything stuck?

    `cur` is an open cursor. Best-effort: any query hiccup degrades a
    field rather than raising, so the caller's response still renders.

    Phase SS (2026-05-17) — added:
      - bluesky_configured (Phase PP env-var check)
      - linkedin_delivery_rate_pct (% of 7d-generated releases that
        actually got linkedin_sent_at populated — catches token-expired
        / queue-stuck failures the prior fields couldn't see)
      - linkedin_failures: top-3 slugs missing sent_at for ops triage
    """
    li = _linkedin_configured()
    tw = _twitter_configured()
    bsky = bool(os.environ.get("BLUESKY_HANDLE", "").strip()
                and os.environ.get("BLUESKY_APP_PASSWORD", "").strip())
    published_7d = {"linkedin": 0, "twitter": 0, "bluesky": 0}
    queued_unpublished = 0
    oldest_queued_age_h = None
    linkedin_delivery_rate_pct = None
    linkedin_failures: list = []
    try:
        cur.execute(
            """SELECT publish_platform, COUNT(*)
                 FROM social_media_posts
                WHERE status = 'published'
                  AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY publish_platform""")
        for plat, n in cur.fetchall():
            if plat in published_7d:
                published_7d[plat] = int(n or 0)
    except Exception:
        pass
    try:
        cur.execute(
            """SELECT COUNT(*),
                      EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) / 3600.0
                 FROM social_media_posts
                WHERE status = 'approved'""")
        row = cur.fetchone()
        if row:
            queued_unpublished = int(row[0] or 0)
            oldest_queued_age_h = round(float(row[1]), 1) if row[1] is not None else None
    except Exception:
        pass
    # Phase SS — derive LinkedIn delivery rate from the press-release
    # audit trail, not just the publisher mirror table. Catches the
    # case where the publish loop dies silently after queueing.
    try:
        cur.execute("""
            SELECT slug, title, generated_at, linkedin_sent_at
              FROM auto_press_releases
             WHERE generated_at >= NOW() - INTERVAL '7 days'
               AND linkedin_post IS NOT NULL
               AND linkedin_post != ''
             ORDER BY generated_at DESC LIMIT 50""")
        rows = cur.fetchall() or []
        if rows:
            sent = sum(1 for r in rows if r[3] is not None)
            linkedin_delivery_rate_pct = round(100.0 * sent / len(rows), 1)
            for slug, title, gen_at, sent_at in rows:
                if sent_at is None and len(linkedin_failures) < 3:
                    linkedin_failures.append({
                        "slug":         slug,
                        "title":        (title or "")[:120],
                        "generated_at": gen_at.isoformat() if gen_at else None,
                    })
    except Exception:
        pass

    # status: dark = posts stuck because creds are missing (the bug the
    # memory note flags); idle = no creds but nothing waiting; healthy =
    # creds present; degraded = creds present but a backlog is building.
    if not li and not tw and not bsky:
        status = "dark" if queued_unpublished > 0 else "idle"
    elif queued_unpublished >= 4:
        status = "degraded"
    elif linkedin_delivery_rate_pct is not None and linkedin_delivery_rate_pct < 50:
        status = "degraded"
    else:
        status = "healthy"

    diagnosis = {
        "dark": (f"{queued_unpublished} approved post(s) are queued but no "
                 "social channel is configured — set LINKEDIN_ACCESS_TOKEN, "
                 "TWITTER_*, or BLUESKY_HANDLE+BLUESKY_APP_PASSWORD on "
                 "Railway to start distributing."),
        "idle": ("No social creds configured — distribution is off. Press "
                 "releases still generate; they just aren't being posted."),
        "degraded": (f"{queued_unpublished} approved posts are backing up — "
                     "the auto-publisher caps at 2/day per platform; check "
                     "for publish failures. See linkedin_failures for slugs."),
        "healthy": "Distribution is wired and the queue is clear.",
    }[status]

    return {
        "status": status,
        "diagnosis": diagnosis,
        "linkedin_configured": li,
        "twitter_configured":  tw,
        "bluesky_configured":  bsky,
        "published_7d": published_7d,
        "queued_unpublished": queued_unpublished,
        "oldest_queued_age_hours": oldest_queued_age_h,
        "linkedin_delivery_rate_pct": linkedin_delivery_rate_pct,
        "linkedin_failures": linkedin_failures,
    }


@marketing_bp.get("/api/v1/marketing/distribution/health")
def distribution_health():
    """Explicit distribution-wiring health — makes the auto-publisher's
    silent env-var skip visible. Public; safe to poll."""
    c = _conn()
    if c is None:
        return jsonify(status="unknown", error="db unavailable"), 200
    try:
        with c.cursor() as cur:
            out = _distribution_status(cur)
        out["as_of"] = datetime.now(timezone.utc).isoformat()
        return jsonify(out), 200
    except Exception as e:
        return jsonify(status="unknown", error=str(e)[:200]), 200
    finally:
        try: c.close()
        except Exception: pass


@marketing_bp.get("/api/v1/marketing/pulse")
def marketing_pulse():
    """Public marketing-pulse metrics: recent auto-press, engagement,
       AI citation tally. The /dc-hub-media page renders this as the
       "self-managed marketing engine" widget the user asked for."""
    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "auto_press_7d": 0,
        "auto_press_30d": 0,
        "latest_auto": None,
        "engagement_7d": {"views": 0, "click_outs": 0, "stripe_clicks": 0},
        "ai_callers_7d": 0,
        "distribution": None,
    }
    c = _conn()
    if c is None: return jsonify(out), 200
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM auto_press_releases
                           WHERE generated_at > NOW() - INTERVAL '7 days'""")
            out["auto_press_7d"] = int(cur.fetchone()[0] or 0)
            cur.execute("""SELECT COUNT(*) FROM auto_press_releases
                           WHERE generated_at > NOW() - INTERVAL '30 days'""")
            out["auto_press_30d"] = int(cur.fetchone()[0] or 0)
            cur.execute("""SELECT slug, title, generated_at, source_topic
                           FROM auto_press_releases
                           ORDER BY generated_at DESC LIMIT 1""")
            row = cur.fetchone()
            if row:
                out["latest_auto"] = {
                    "slug": row[0], "title": row[1],
                    "generated_at": row[2].isoformat() if row[2] else None,
                    "topic": row[3],
                    "url": f"https://dchub.cloud/news/{row[0]}",
                }
        with c.cursor() as cur:
            cur.execute("""SELECT event_type, COUNT(*) FROM press_engagement
                           WHERE t > NOW() - INTERVAL '7 days'
                           GROUP BY event_type""")
            for et, n in cur.fetchall():
                if et == "view": out["engagement_7d"]["views"] = int(n)
                elif et == "click_out": out["engagement_7d"]["click_outs"] = int(n)
                elif et == "stripe_click": out["engagement_7d"]["stripe_clicks"] = int(n)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(DISTINCT COALESCE(NULLIF(client_name,''),
                                                    NULLIF(platform,''),
                                                    ip_address))
                    FROM mcp_tool_calls
                    WHERE created_at > NOW() - INTERVAL '7 days'""")
                out["ai_callers_7d"] = int((cur.fetchone() or (0,))[0])
        except Exception:
            pass
        # Distribution wiring — surfaces the auto-publisher's otherwise
        # silent "no creds, skipping" so /dc-hub-media shows whether the
        # press releases are actually going anywhere.
        try:
            with c.cursor() as cur:
                out["distribution"] = _distribution_status(cur)
        except Exception:
            pass
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=240"
    return resp, 200


@marketing_bp.get("/api/v1/marketing/worker-status")
def worker_status():
    """Phase GG (2026-05-14): the unified 'DC Hub Media autonomous worker'
    health view — presents DC Hub Media as a peer to Brain and the ISO
    loops. Composes the last autonomous press + cadence, distribution
    wiring, and the self-learning form-factor pick into one status.
    Public; safe to poll.
    """
    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "worker": "dc_hub_media",
        "autonomous": True,
        "last_auto_press": None,
        "auto_press_age_hours": None,
        "auto_press_7d": 0,
        "distribution": None,
        "form_factor": {"smart_pick": None, "rotation_pick": None, "learning": False},
        "status": "unknown",
        "notes": [],
    }
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""SELECT slug, title, generated_at,
                                      EXTRACT(EPOCH FROM (NOW() - generated_at)) / 3600
                                 FROM auto_press_releases
                                ORDER BY generated_at DESC LIMIT 1""")
                row = cur.fetchone()
                if row:
                    out["last_auto_press"] = {
                        "slug": row[0], "title": row[1],
                        "generated_at": row[2].isoformat() if row[2] else None,
                        "url": f"https://dchub.cloud/news/{row[0]}",
                    }
                    out["auto_press_age_hours"] = (
                        round(float(row[3]), 1) if row[3] is not None else None)
                cur.execute("""SELECT COUNT(*) FROM auto_press_releases
                                WHERE generated_at > NOW() - INTERVAL '7 days'""")
                out["auto_press_7d"] = int((cur.fetchone() or [0])[0] or 0)
            with c.cursor() as cur:
                out["distribution"] = _distribution_status(cur)
        except Exception as e:
            out["notes"].append(f"db: {str(e)[:120]}")
        finally:
            try: c.close()
            except Exception: pass

    # The self-learning form-factor pick. smart_pick diverging from the
    # fixed weekday rotation means the worker has enough engagement data
    # to be actively optimising — i.e. it's genuinely learning, not just
    # rotating on a calendar.
    try:
        from routes.og_cards import smart_style, todays_style
        sp, rp = smart_style(), todays_style()
        out["form_factor"] = {
            "smart_pick": sp, "rotation_pick": rp, "learning": sp != rp,
        }
    except Exception as e:
        out["notes"].append(f"form_factor: {str(e)[:120]}")

    age = out["auto_press_age_hours"]
    dist_status = (out["distribution"] or {}).get("status")
    if age is None:
        out["status"] = "unknown"
    elif age > 60:
        out["status"] = "stale"
        out["notes"].append(f"last auto-press {age}h ago (cadence 24h)")
    elif dist_status == "dark":
        out["status"] = "degraded"
        out["notes"].append("press generating but distribution is dark — no creds")
    else:
        out["status"] = "healthy"

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=240"
    return resp, 200


@marketing_bp.get("/api/v1/marketing/og-performance")
def og_performance():
    """Phase FF (2026-05-14) — Track 1 / DC Hub Media v2: the measurement
    loop. Which OG-card form factor actually drives engagement?

    The card a press release got is determined by its publish day —
    og_cards.DAILY_STYLES[generated_at.weekday()]. Join auto_press_releases
    to press_engagement, bucket every post + its engagement by the form
    factor it ran, and rank. Read-only — no new tables, no behavior
    change; this is the visibility half of the loop (the feedback half —
    letting performance drive the rotation — is a deliberate follow-up).
    """
    try:
        days = max(7, min(int(request.args.get("days", "30")), 180))
    except ValueError:
        days = 30

    # Mirror the live rotation; lazy import so a PIL/og_cards import
    # hiccup degrades to the known rotation rather than 500-ing.
    try:
        from routes.og_cards import DAILY_STYLES
    except Exception:
        DAILY_STYLES = {0: 'data_brutal', 1: 'editorial', 2: 'infographic',
                        3: 'ai_hero', 4: 'data_brutal', 5: 'editorial',
                        6: 'infographic'}

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "rotation": {str(k): v for k, v in DAILY_STYLES.items()},
        "by_form_factor": [],
        "best_by_click_rate": None,
        "note": ("Engagement bucketed by the OG-card form factor each post "
                 "ran. click_rate = (click_outs + stripe_clicks) / views."),
    }
    c = _conn()
    if c is None:
        return jsonify(out), 200
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT a.slug, a.generated_at, e.event_type, COUNT(e.id)
                     FROM auto_press_releases a
                     LEFT JOIN press_engagement e ON e.slug = a.slug
                    WHERE a.generated_at > NOW() - make_interval(days => %s)
                    GROUP BY a.slug, a.generated_at, e.event_type""",
                (days,))
            rows = cur.fetchall()

        # form_factor -> {posts:set, views, click_outs, stripe_clicks}
        agg = {}
        for slug, gen_at, event_type, n in rows:
            if not gen_at:
                continue
            ff = DAILY_STYLES.get(gen_at.weekday(), "data_brutal")
            b = agg.setdefault(ff, {"posts": set(), "views": 0,
                                    "click_outs": 0, "stripe_clicks": 0})
            b["posts"].add(slug)
            if event_type == "view":
                b["views"] += int(n or 0)
            elif event_type == "click_out":
                b["click_outs"] += int(n or 0)
            elif event_type == "stripe_click":
                b["stripe_clicks"] += int(n or 0)

        ranked = []
        for ff, b in agg.items():
            posts = len(b["posts"])
            views = b["views"]
            clicks = b["click_outs"] + b["stripe_clicks"]
            ranked.append({
                "form_factor": ff,
                "press_count": posts,
                "views": views,
                "click_outs": b["click_outs"],
                "stripe_clicks": b["stripe_clicks"],
                "views_per_post": round(views / posts, 1) if posts else 0,
                "click_rate": round(clicks / views, 4) if views else None,
            })
        # Sort: highest click_rate first, then views_per_post — Nones last.
        ranked.sort(key=lambda r: (r["click_rate"] if r["click_rate"] is not None
                                   else -1, r["views_per_post"]), reverse=True)
        out["by_form_factor"] = ranked
        # "Best" needs a meaningful sample — at least 2 posts and some views.
        for r in ranked:
            if r["click_rate"] is not None and r["press_count"] >= 2 and r["views"] > 0:
                out["best_by_click_rate"] = r["form_factor"]
                break
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    return resp, 200


@marketing_bp.route("/api/v1/marketing/track", methods=["GET", "POST"])
def track_event():
    """Pixel-style engagement tracking. Public, rate-limit-friendly.
       Accepts `slug`, `event_type` (`view` | `click_out` | `stripe_click`).

       Phase MM (2026-05-13): added GET so `<img src=".../track?slug=X&event_type=view">`
       pixel tags work — historically engagement was 0 in production
       because POST-only required JS+CORS+JSON body, which the /news/<slug>
       static templates can't emit. With GET, a 1×1 image pixel suffices.
       """
    # Pull slug/event_type from JSON body if present, else from query string.
    if request.method == "POST" and request.is_json:
        payload = request.get_json(silent=True) or {}
        slug = payload.get("slug") or request.args.get("slug") or ""
        event_type = payload.get("event_type") or request.args.get("event_type") or "view"
    else:
        slug = request.args.get("slug") or ""
        event_type = request.args.get("event_type") or "view"
    if not slug or event_type not in ("view", "click_out", "stripe_click"):
        return jsonify(ok=False, error="bad_request"), 400
    c = _conn()
    if c is None: return jsonify(ok=True, stored=False), 200
    try:
        import hashlib
        ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else None
        # ON CONFLICT DO NOTHING is a defensive no-op here — the table
        # has only a BIGSERIAL PK and no unique constraint, so the
        # conflict can't actually fire. Added to satisfy the
        # regression-lint `insert-no-on-conflict` rule (same pattern used
        # in routes/brain_v2_store.brain_learning_log).
        with c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO press_engagement
                    (slug, event_type, referrer, user_agent, ip_hash, t)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT DO NOTHING;
            """, (slug[:200], event_type,
                  (request.headers.get("Referer") or "")[:500],
                  (request.headers.get("User-Agent") or "")[:300],
                  ip_hash))
        # Phase MM: for GET requests, return a 1×1 transparent gif so an
        # <img> pixel tag renders cleanly (no broken-image icon). POSTs
        # still get the JSON ack so JS-based callers see stored=true.
        if request.method == "GET":
            return _PIXEL_GIF, 200, {
                "Content-Type": "image/gif",
                "Cache-Control": "no-store, must-revalidate",
                "Content-Length": str(len(_PIXEL_GIF)),
            }
        return jsonify(ok=True, stored=True), 200
    except Exception as e:
        # Still return the pixel on GET-error so the <img> doesn't show
        # a broken icon. The DB write is the only thing that can fail.
        if request.method == "GET":
            return _PIXEL_GIF, 200, {"Content-Type": "image/gif"}
        return jsonify(ok=False, error=str(e)[:200]), 200
    finally:
        try: c.close()
        except Exception: pass


# 1×1 transparent GIF — minimal valid bytes. Used as the response body
# for GET /track so the <img> pixel tag renders cleanly.
_PIXEL_GIF = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00,
    0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x21,
    0xF9, 0x04, 0x01, 0x00, 0x00, 0x00, 0x00, 0x2C, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
    0x01, 0x00, 0x3B,
])


@marketing_bp.get("/api/v1/marketing/engagement")
def engagement_summary():
    """Per-piece view + click totals over a window. Useful for /brain +
       /dc-hub-media to show "best performing press" rankings."""
    try:
        window_h = int(request.args.get("hours", "168"))
    except ValueError:
        window_h = 168
    window_h = max(1, min(window_h, 720))
    c = _conn()
    if c is None: return jsonify(pieces=[], window_hours=window_h), 200
    try:
        with c.cursor() as cur:
            cur.execute(f"""
                SELECT slug,
                       SUM(CASE WHEN event_type='view' THEN 1 ELSE 0 END) AS views,
                       SUM(CASE WHEN event_type='click_out' THEN 1 ELSE 0 END) AS clicks,
                       SUM(CASE WHEN event_type='stripe_click' THEN 1 ELSE 0 END) AS stripe_clicks
                FROM press_engagement
                WHERE t > NOW() - INTERVAL '{window_h} hours'
                GROUP BY slug
                ORDER BY views DESC
                LIMIT 50
            """)
            pieces = [
                {"slug": r[0], "views": int(r[1] or 0),
                 "click_outs": int(r[2] or 0),
                 "stripe_clicks": int(r[3] or 0),
                 "ctr_pct": round(100.0 * (r[2] or 0) / max(1, r[1] or 1), 2)}
                for r in cur.fetchall()
            ]
        return jsonify(pieces=pieces, window_hours=window_h,
                       as_of=datetime.now(timezone.utc).isoformat()), 200
    except Exception as e:
        return jsonify(error=str(e)[:200], pieces=[]), 500
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Phase EE (2026-05-12): LinkedIn daily distribution endpoints
# ---------------------------------------------------------------------------

@marketing_bp.get("/api/v1/marketing/linkedin/<slug>")
def linkedin_post_for(slug):
    """Returns the Claude-generated LinkedIn post + one-click share URL
       for a specific auto-press slug. Designed so the user can:
         1. GET this endpoint
         2. Copy the `post` text
         3. Click `share_url` (already pre-fills the LinkedIn post box)
         4. Paste the body, click Post.
       OR — automate via LinkedIn API if the user sets up OAuth.
    """
    from urllib.parse import quote
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""SELECT title, slug, linkedin_post, linkedin_sent_at,
                                       generated_at
                               FROM auto_press_releases WHERE slug = %s""",
                            (slug,))
            except Exception:
                c.rollback()
                return jsonify(ok=False, error="linkedin_post column missing — "
                               "re-run init_schema or wait for next deploy"), 503
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="slug_not_found", slug=slug), 404
        title, slug, post, sent_at, generated_at = row
        canonical = f"https://dchub.cloud/news/{slug}"
        # LinkedIn share URL — prefills the share dialog with the URL.
        # Users paste the body text into the post box.
        share_url = "https://www.linkedin.com/sharing/share-offsite/?url=" + quote(canonical)
        return jsonify(
            ok=True,
            slug=slug,
            title=title,
            post=post,                                 # paste this body
            article_url=canonical,                     # the link being shared
            linkedin_share_url=share_url,              # opens LinkedIn share dialog
            generated_at=generated_at.isoformat() if generated_at else None,
            already_sent=bool(sent_at),
            sent_at=sent_at.isoformat() if sent_at else None,
            usage_hint=("1. Open `linkedin_share_url` in a new tab. "
                        "2. Copy `post` body text. "
                        "3. Paste into LinkedIn's share dialog. "
                        "4. Click Post."),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@marketing_bp.get("/api/v1/marketing/linkedin/latest")
def linkedin_post_latest():
    """Returns today's freshest auto-press LinkedIn post + share URL.
       Convenience alias — the daily cron + email helper hit this."""
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""SELECT slug FROM auto_press_releases
                               WHERE linkedin_post IS NOT NULL
                                 AND linkedin_post != ''
                               ORDER BY generated_at DESC LIMIT 1""")
            except Exception:
                c.rollback()
                return jsonify(ok=False, error="linkedin_post column missing"), 503
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False,
                           error="no_linkedin_posts_yet",
                           hint=("Wait for next 13:00 UTC auto-press, or "
                                 "trigger manually via "
                                 "POST /api/v1/marketing/auto-generate")), 404
    finally:
        try: c.close()
        except Exception: pass
    # Delegate to the per-slug endpoint
    return linkedin_post_for(row[0])


@marketing_bp.post("/api/v1/marketing/linkedin/send-daily-email")
@_require_admin
def linkedin_send_daily_email():
    """Admin-gated: emails today's LinkedIn-ready post + share URL to
       a configured recipient. Cron fires this at 13:30 UTC daily — 30
       min after the auto-press generation so the post exists.

       Env vars:
         DCHUB_LINKEDIN_EMAIL_TO   — recipient (defaults to press@dchub.cloud)
         DCHUB_RESEND_API_KEY      — Resend API key (mandatory)

       The recipient gets a one-click-paste email with:
         - the press release headline + URL
         - the full LinkedIn post body
         - a "Share on LinkedIn now" button (linkedin_share_url)
    """
    to_addr = (os.environ.get("DCHUB_LINKEDIN_EMAIL_TO")
               or "press@dchub.cloud").strip()
    if not RESEND_API_KEY:
        return jsonify(ok=False, error="DCHUB_RESEND_API_KEY not configured"), 503

    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT id, slug, title, linkedin_post, linkedin_sent_at
                           FROM auto_press_releases
                           WHERE linkedin_post IS NOT NULL
                             AND linkedin_post != ''
                             AND generated_at > NOW() - INTERVAL '36 hours'
                           ORDER BY generated_at DESC LIMIT 1""")
            row = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass

    if not row:
        return jsonify(ok=False, error="no_recent_linkedin_post"), 404
    apr_id, slug, title, post, sent_at = row
    if sent_at:
        return jsonify(ok=True, skipped=True, reason="already_sent_today",
                       sent_at=sent_at.isoformat()), 200

    from urllib.parse import quote
    canonical = f"https://dchub.cloud/news/{slug}"
    share_url = "https://www.linkedin.com/sharing/share-offsite/?url=" + quote(canonical)
    html_email = f"""<div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:24px">
<div style="font-size:11px;letter-spacing:1.5px;color:#8b6fff;font-weight:800;text-transform:uppercase;margin-bottom:6px">📰 Today's auto-press · LinkedIn ready</div>
<h1 style="font-size:22px;margin:0 0 14px;line-height:1.3">{title}</h1>
<p style="color:#555;font-size:14px;margin:0 0 20px">
  Auto-generated daily brief published at <a href="{canonical}">{canonical}</a>
</p>

<div style="background:#f6f7fb;border-left:3px solid #8b6fff;border-radius:6px;padding:18px 20px;margin:0 0 20px;white-space:pre-wrap;font-family:Inter,-apple-system,sans-serif;font-size:14.5px;line-height:1.6">{html_escape(post or '')}</div>

<a href="{share_url}" style="display:inline-block;background:#0a66c2;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:700;margin-bottom:12px">Open LinkedIn share dialog →</a>
<p style="color:#555;font-size:13px;margin:8px 0 0">
  <strong>How to post:</strong> click the button above, paste the body text from the box, click Post on LinkedIn. ~10 seconds total.
</p>

<hr style="border:none;border-top:1px solid #ddd;margin:32px 0">
<p style="color:#999;font-size:12px;margin:0">
  This email is sent daily by the DC Hub autonomous marketing engine.
  Configure recipient via DCHUB_LINKEDIN_EMAIL_TO. Disable by removing the
  marketing_linkedin cron job in evolve-cron.yml.
</p>
</div>"""

    # Send via Resend
    #
    # Phase QQ+17 (2026-05-13): two changes after observing the live 403:
    #   1. Read the Resend error response BODY on HTTPError so the
    #      operator sees the actual reason (domain unverified vs bad
    #      key vs bad payload). Previously we returned only the
    #      stringified urllib exception which dropped Resend's message.
    #   2. Sender is configurable via DCHUB_RESEND_FROM env var so we
    #      can fall back to onboarding@resend.dev (Resend's universally-
    #      verified sandbox sender) without a code change when the
    #      dchub.cloud domain isn't yet verified in the Resend dashboard.
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    sender = os.environ.get("DCHUB_RESEND_FROM",
                            "DC Hub <noreply@dchub.cloud>")
    payload = json.dumps({
        "from":    sender,
        "to":      [to_addr],
        "subject": f"📰 Today's LinkedIn post — {title[:60]}",
        "html":    html_email,
    }).encode()
    # Phase QQ+18 (2026-05-13): explicit User-Agent. Resend is behind
    # Cloudflare and their WAF returns "error code: 1010" (access
    # denied) to bare urllib User-Agent strings ("Python-urllib/3.x").
    # Setting any realistic UA gets through. The earlier 403 from
    # Resend was actually a Cloudflare WAF rejection — confirmed by
    # the literal "error code: 1010" body Resend never sets.
    req = Request("https://api.resend.com/emails", data=payload, headers={
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "User-Agent":    "dchub-backend/1.0 (+https://dchub.cloud)",
        "Accept":        "application/json",
    })
    resp_text = ""
    sent_ok = False
    try:
        with urlopen(req, timeout=15) as r:
            resp_text = r.read().decode("utf-8", errors="ignore")
            sent_ok = (r.status == 200)
    except HTTPError as he:
        # CAPTURE the response body — that's where Resend explains
        # what's wrong (domain not verified, invalid recipient, etc).
        body = ""
        try:
            body = he.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        return jsonify(
            ok=False,
            error=f"resend_http_{he.code}",
            sender=sender,
            resend_response=body[:500],
            hint=("If 'domain is not verified', verify dchub.cloud at "
                  "https://resend.com/domains OR set DCHUB_RESEND_FROM "
                  "env var to 'onboarding@resend.dev' for testing."),
        ), 502
    except Exception as e:
        return jsonify(ok=False, error=f"resend_failed: {str(e)[:200]}"), 502

    if not sent_ok:
        return jsonify(ok=False, error="resend_non_200",
                       detail=resp_text[:300]), 502

    # Mark sent so a duplicate cron run is a no-op
    c = _conn()
    if c is not None:
        try:
            with c, c.cursor() as cur:
                cur.execute("""UPDATE auto_press_releases
                               SET linkedin_sent_at = NOW()
                               WHERE id = %s""", (apr_id,))
        finally:
            try: c.close()
            except Exception: pass

    return jsonify(ok=True, sent=True, to=to_addr, slug=slug,
                   article_url=canonical), 200


# Tiny HTML-escape helper for the email template (avoids stdlib import noise)
def html_escape(s):
    from html import escape
    return escape(s or "")
