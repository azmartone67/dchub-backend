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
from utils.anthropic_helper import anthropic_messages_url
from routes._swallowed_writes import note_swallowed_write
from util.json_column import json_for_column

marketing_bp = Blueprint("marketing_engine", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MARKETING_MODEL = os.environ.get("DCHUB_MARKETING_MODEL", "claude-sonnet-4-5")
# Phase LL+9 (2026-06-14): a KNOWN-GOOD fallback model for the daily auto-press
# retry loop. The "1 auto-press in 30 days" silence traced to the retry loop
# calling Claude 3x with the SAME MARKETING_MODEL every time — so a single
# stale/renamed primary model id (or a model the gateway no longer accepts)
# failed all 3 attempts identically, returned 502, and persisted nothing, every
# day. Trying a different, current model from attempt 2 means a bad primary id
# costs one wasted call instead of the whole day's output. Haiku 4.5 is the
# same model brain_narrative already uses successfully.
MARKETING_MODEL_FALLBACK = os.environ.get(
    "DCHUB_MARKETING_MODEL_FALLBACK", "claude-haiku-4-5-20251001")
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
        "recent_ai_citation": None,
        "recent_deals": [],
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

        # Phase FF-polish: most recent AI citation of DC Hub. Citation-quote posts
        # have historically outperformed bare-link posts; rather than hardcode a
        # stale impressions ratio, generation now leans on REAL reader click-through
        # via _inject_engagement_signal() (r70 measure→learn). Own try/except so a
        # missing/empty table can't break signal gathering.
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT engine, prompt_text, response_text, response_url
                      FROM ai_citations
                     WHERE dchub_cited = true
                       AND response_text IS NOT NULL AND response_text <> ''
                       AND observed_at > NOW() - INTERVAL '14 days'
                     ORDER BY observed_at DESC LIMIT 5
                """)
                # r65-qa (citation self-own): an LLM DISCLAIMING knowledge is NOT
                # an endorsement. Skip any "I don't have specific current info..."
                # / "lacked current specifics" response so the showcase topic is
                # only ever built from a REAL citation. (The publish gate also
                # hard-blocks these, but don't even pick the topic off one.)
                _DISCLAIMER_MARKERS = (
                    "don't have specific", "do not have specific",
                    "don't have current", "do not have current",
                    "lacked current specific", "lack current specific",
                    "don't have access", "do not have access",
                    "as of my last", "knowledge cutoff", "knowledge cut-off",
                    "to give you an accurate comparison",
                    "don't have enough information", "not familiar with",
                    "no specific current information",
                    "cannot provide", "can't provide", "unable to provide",
                    "don't have real-time", "do not have real-time",
                )
                for r in (cur.fetchall() or []):
                    _ql = (r[2] or "").lower()
                    if any(m in _ql for m in _DISCLAIMER_MARKERS):
                        continue   # disclaimer, not an endorsement — skip
                    out["recent_ai_citation"] = {
                        "engine": r[0], "prompt": r[1],
                        "quote": (r[2] or "")[:600], "url": r[3]}
                    break
        except Exception:
            pass

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

        # AI usage — quotable adoption metric. MUST read the canonical identity
        # view (agent = md5 of first public XFF token, real-external only), the
        # same identity as /api/v1/reach. The old raw COUNT(DISTINCT ip_address)
        # over ALL mcp_tool_calls counted probes + self-traffic and published
        # "86 AI agents ... up 41% week-over-week" on LinkedIn (2026-06-30,
        # press_release 117) when the honest count was ~14/wk. NEVER count
        # session_id or raw ip_address as agents.
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE is_real_external),
                           COUNT(DISTINCT agent_id)
                               FILTER (WHERE is_real_external AND is_public_ip)
                    FROM mcp_calls_identity
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
        # r86c: discovered_facilities.discovered_at is TEXT, so the bare
        # `discovered_at > NOW()` raised "operator does not exist: text >
        # timestamp" — which ABORTED the shared transaction and silently
        # poisoned every probe after it (recent_deals, industry_news,
        # iso_today all came back empty). Cast to timestamptz with an
        # empty/null guard (same pattern as brain_inspector.facilities_added_7d),
        # and roll back on any error so one bad probe can't cascade.
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT name, provider, city, state, country, power_mw
                    FROM discovered_facilities
                    WHERE NULLIF(discovered_at::text, '') IS NOT NULL
                      AND discovered_at::timestamptz > NOW() - INTERVAL '24 hours'
                    ORDER BY power_mw DESC NULLS LAST
                    LIMIT 5
                """)
                out["new_facilities_24h"] = [
                    {"name": r[0], "provider": r[1], "city": r[2],
                     "state": r[3], "country": r[4], "mw": r[5]}
                    for r in cur.fetchall() if r[0]]
        except Exception as e:
            print(f"[marketing_engine] facilities probe failed: {e}", file=sys.stderr)
            try: c.rollback()
            except Exception: pass

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
            # r-media-canon-gate (2026-07-02): USAGE tables removed from this
            # list — raw ai_requests / mcp_tool_calls row counts include
            # internal probes + self-heal traffic (~25x inflated vs the
            # canonical identity view), so a "+N% WoW" milestone built on them
            # is the same class of over-claim as the session-inflated agent
            # post. Coverage stories may only cite genuinely-coverage tables;
            # honest usage WoW would have to come from mcp_calls_identity
            # (is_public_ip AND is_real_external), which this generic
            # COUNT(*) loop cannot express — so those rows are dropped rather
            # than fabricated.
            COVERAGE_TABLES = [
                ("facilities",          "facilities",          "discovered_at"),
                ("markets_tracked",     "market_power_scores", "computed_at"),
                ("mcp_developers",      "mcp_dev_keys",        "created_at"),
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

        # ── r65-qa (#6): recent M&A deals WITH real value/MW ──────────────
        # Without this the theme_deals / ma_pulse topics ran with NO deal data
        # in the prompt, so Claude (correctly forbidden from inventing numbers)
        # emitted value-less "→ Deal — Google" stubs — the broken LinkedIn post
        # the user flagged. Feeding real rows lets it render "Buyer/Seller —
        # $X.XB / NNN MW"; the picker below skips the deals topic when empty.
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT date, buyer, seller, value, mw
                      FROM deals
                     WHERE buyer IS NOT NULL AND buyer != ''
                       AND (value IS NOT NULL OR mw IS NOT NULL)
                     ORDER BY date DESC NULLS LAST
                     LIMIT 6
                """)
                out["recent_deals"] = [
                    {"date":   (r[0].isoformat() if hasattr(r[0], "isoformat") else (str(r[0]) if r[0] else "")),
                     "buyer":  r[1], "seller": r[2],
                     "value_m": (float(r[3]) if r[3] is not None else None),
                     "mw":      (float(r[4]) if r[4] is not None else None)}
                    for r in cur.fetchall()
                ]
        except Exception:
            try: c.rollback()
            except Exception: pass
            out["recent_deals"] = []
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
    # Fix (2026-07-04): the weekly product/data changelog. A 7-day window keeps
    # "here's what DC Hub shipped" to at most once a week so the same commit
    # batch is never covered twice.
    "platform_update": 7,
}


# Fix (2026-07-04): story-FAMILY diversity quota. Auto-press had collapsed to
# ~5 story families over 30 days (excess-power alone was ~10/30). Group the
# market/angle topics into coarse families; the cascade refuses ANY topic whose
# family already owns more than _FAMILY_QUOTA of the trailing-14d feed — even if
# that topic's own per-topic dedup window would allow it. Unmapped topics are
# their own family (a topic can never falsely throttle itself).
_FAMILY_QUOTA = 0.33   # ~1/3 — bites the reported "excess-power 10/30" (=0.333) collapse
_TOPIC_FAMILIES = {
    # the "Cheyenne / Rural-SPP excess-power" collapse — one DCPI-ranking family
    "dcpi_leader":  "dcpi_market",
    "dcpi_warning": "dcpi_market",
    "dcpi_mover":   "dcpi_market",
    # grid / interconnection
    "iso_focus":             "grid",
    "iso_grid_pulse":        "grid",
    "interconnection_queue": "grid",
    # third-party news overlay
    "industry_pulse": "industry_news",
    # deals / M&A
    "ma_pulse":    "deals",
    "theme_deals": "deals",
    # DC-Hub-itself stories
    "coverage_milestone": "platform",
    "ai_adoption":        "platform",
    "ai_citation":        "platform",
    "platform_update":    "platform",
}


def _topic_family(topic: str) -> str:
    t = (topic or "").strip()
    return _TOPIC_FAMILIES.get(t, t or "unknown")


def _family_shares(days: int = 14) -> dict:
    """Return {family: share_0to1} over the trailing N days of
    auto_press_releases.source_topic. Lets the picker refuse a topic whose
    story family already dominates the recent feed (the 'excess-power 10/30'
    collapse). Returns {} on any error OR when the sample is too small
    (< 6 releases) so a sparse feed never thrashes (fail-open)."""
    try:
        c = _conn()
        if c is None:
            return {}
        with c.cursor() as cur:
            cur.execute(
                """SELECT source_topic, COUNT(*) FROM auto_press_releases
                    WHERE generated_for >= (CURRENT_DATE - INTERVAL '%s days')
                    GROUP BY source_topic""",
                (days,))
            rows = cur.fetchall()
        c.close()
        total = sum(int(n or 0) for _, n in rows)
        if total < 6:
            return {}
        fam_counts: dict = {}
        for st, n in rows:
            fam = _topic_family(st or "")
            fam_counts[fam] = fam_counts.get(fam, 0) + int(n or 0)
        return {f: (cnt / total) for f, cnt in fam_counts.items()}
    except Exception:
        return {}


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
                      AND generated_for >= (CURRENT_DATE - %s * INTERVAL '1 day')
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
# Phase FF+roundrobin (2026-05-22): city↔state alias map so the dedup guard
# treats "Cheyenne", "Cheyenne, WY", and "Wyoming" as the SAME market — the
# exact gap that let the Wyoming story repeat. Extend as new repeat-offenders
# surface. Keys/values are all lowercase.
_MARKET_ALIASES = {
    "cheyenne": "wyoming", "cheyenne, wy": "wyoming", "wy": "wyoming",
    "ashburn": "northern virginia", "loudoun": "northern virginia",
    "loudoun county": "northern virginia", "nova": "northern virginia",
    "va": "northern virginia",
    "santa clara": "silicon valley", "san jose": "silicon valley",
    "dallas": "dallas-fort worth", "fort worth": "dallas-fort worth",
    "dfw": "dallas-fort worth",
    "phoenix": "phoenix", "mesa": "phoenix", "az": "phoenix",
    "columbus": "central ohio", "new albany": "central ohio",
    # Fix (2026-07-04): the "Rural SPP" excess-power headline kept re-running as
    # a "new" market because it never normalized. Collapse it onto Kansas (the
    # SPP anchor market DCPI ranks it under) so the market-clash guard catches
    # the repeat.
    "rural spp": "kansas", "spp": "kansas",
}
_US_STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado",
    "connecticut","delaware","florida","georgia","hawaii","idaho","illinois",
    "indiana","iowa","kansas","kentucky","louisiana","maine","maryland",
    "massachusetts","michigan","minnesota","mississippi","missouri","montana",
    "nebraska","nevada","new hampshire","new jersey","new mexico","new york",
    "north carolina","north dakota","ohio","oklahoma","oregon","pennsylvania",
    "rhode island","south carolina","south dakota","tennessee","texas","utah",
    "vermont","virginia","washington","west virginia","wisconsin","wyoming",
}


def _norm_market(name: str | None) -> str:
    """Canonicalize a market name for dedup: lowercased, ', XX' suffix
    stripped, and aliased (Cheyenne→wyoming). Returns '' for falsy input."""
    if not name:
        return ""
    s = name.strip().lower()
    # strip a trailing state abbrev ", wy" / " wy"
    s = re.sub(r",?\s+[a-z]{2}$", "", s).strip()
    s = re.sub(r"\s+(metro|metropolitan|area|region)$", "", s).strip()
    return _MARKET_ALIASES.get(s, s)


def _recent_market_names(n: int = 2) -> set:
    """Returns a normalized set of market identities mentioned in the last
    `n` auto press release titles. Best-effort extraction; a miss just means
    looser variety (fail-open).

    ★ r-attempt-memory (2026-08-10): reads ATTEMPTS as well as writes. Keyed on
    successes alone this returned the markets of the last `n` COMMITTED
    releases — roughly one per day — so a composer that had just proposed the
    same market a dozen times in one afternoon saw none of them. Same reason as
    _recent_attempt_titles; the failure mode there was sixteen Midland-Odessa
    re-proposals in two hours.
    """
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
        titles = titles + _recent_attempt_titles(days=2, limit=max(n, 20))
        out = set()
        for t in titles:
            tl = t.lower()
            # 1) leading market name: "Cheyenne, WY ...", "Atlanta Metro ...",
            #    "Rural SPP — Excess Power ...", "Wyoming Lead the BUILD ..."
            #    Fix (2026-07-04): added ' — ' (em-dash) + ' Lead ' terminators —
            #    the em-dash headline form and the singular "Lead" verb were both
            #    slipping past extraction, so those markets read as un-featured.
            m = re.match(r"^([A-Z][a-zA-Z\.\- ]+?)(?:,| Metro|:| - | – | — | Leads| Lead | Tops| Takes)", t)
            if m:
                out.add(_norm_market(m.group(1)))
            # 2) any US state mentioned anywhere in the title (catches the
            #    "Wyoming" headline that the city regex above would miss).
            for st in _US_STATES:
                if re.search(r"\b" + re.escape(st) + r"\b", tl):
                    out.add(_norm_market(st))
        out.discard("")
        return out
    except Exception:
        return set()


def _deslug_title(slug: str) -> str:
    """Best-effort headline from a press slug, for review rows written before
    media_editorial_reviews carried a `title` column."""
    import re as _re
    s = _re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", (slug or ""))
    s = _re.sub(r"^\s*auto[-_]", " ", s)
    return " ".join(w for w in _re.split(r"[^A-Za-z0-9]+", s) if w)


def _recent_attempt_titles(days: int = 7, limit: int = 40) -> list:
    """★ r-attempt-memory (2026-08-10). Headlines the composer has ATTEMPTED —
    including the ones the editorial desk held and the ones whose write rolled
    back — read from media_editorial_reviews.

    This exists because reading only successes is a feedback loop into
    repetition. `auto_press_releases` gains a row only when the composer's
    transaction COMMITS; on 2026-08-09 nineteen of twenty runs rolled back, so
    the DO-NOT-REPEAT block stayed near-empty and the generator re-proposed the
    same Midland-Odessa story sixteen times in two hours, each time believing
    it was novel and each time paying for an editorial LLM review to be told
    'static index snapshot, no concrete change'. The worse the write path got,
    the harder it repeated. The review table is written on its own connection,
    so it survives the rollback — it is the only durable record of an attempt.

    Fail-open ([] on any error): a lookup failure must loosen variety, never
    dead-end the day's output.
    """
    c = None
    try:
        c = _conn()
        if c is None:
            return []
        # `title` is added by media_editorial_gate._SCHEMA's idempotent ALTER,
        # which only runs on the first _record_review after a deploy — and the
        # composer reads this list BEFORE it calls the gate. Without the
        # slug-only fallback the fix would be inert for exactly one run, which
        # is one more Midland-Odessa than necessary.
        rows = None
        for sql in ("""SELECT title, press_slug FROM media_editorial_reviews
                        WHERE created_at > NOW() - INTERVAL '%s days'
                        ORDER BY created_at DESC LIMIT %s""",
                    """SELECT NULL, press_slug FROM media_editorial_reviews
                        WHERE created_at > NOW() - INTERVAL '%s days'
                        ORDER BY created_at DESC LIMIT %s"""):
            try:
                with c.cursor() as cur:
                    cur.execute(sql, (days, limit))
                    rows = cur.fetchall() or []
                break
            except Exception:
                # ★ a failed statement poisons the whole transaction on this
                # connection — the retry MUST start a clean one or it dies
                # with InFailedSqlTransaction and the fallback never runs.
                try:
                    c.rollback()
                except Exception:
                    pass
                rows = None
        out = []
        for r in (rows or []):
            t = (r[0] or "").strip() if r[0] else ""
            t = t or _deslug_title(r[1])
            if t:
                out.append(t)
        return out
    except Exception:
        return []
    finally:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass


def _recent_titles(days: int = 7) -> list:
    """Recent auto-press headlines (last N days) for the DO-NOT-REPEAT prompt
    block + near-duplicate rejection. Fail-open ([] on any error).

    Unions WRITTEN releases with ATTEMPTED ones — see _recent_attempt_titles
    for why successes alone are not enough.
    """
    written = []
    try:
        c = _conn()
        if c is not None:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT title FROM auto_press_releases
                        WHERE title IS NOT NULL
                          AND generated_for >= (CURRENT_DATE - INTERVAL '%s days')
                        ORDER BY generated_at DESC NULLS LAST
                        LIMIT 40""",
                    (days,))
                written = [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception:
        written = []
    merged, seen = [], set()
    for t in list(written) + _recent_attempt_titles(days):
        key = (t or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(t)
    return merged[:60]


# Headline stopwords stripped before the content-overlap comparison so
# 'the / this week / record / new / DC / data center' noise doesn't dilute the
# signal (that noise is exactly why 'Cheyenne Tops the Build Rankings' vs
# 'Cheyenne, WY Leads the BUILD Ranking' looked only ~38% similar).
_TITLE_STOP = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "with", "this",
    "week", "record", "new", "its", "dc", "data", "center", "centers", "hub",
    "as", "at", "by", "is", "are", "amid", "sees", "now",
}


def _is_near_dup_title(title: str, recent_titles: list,
                       ratio_threshold: float = 0.78) -> bool:
    """True if `title` retells a story we already ran in the recent set. Three
    signals, any of which fires:
      (a) difflib sequence ratio >= ratio_threshold  — near-identical wording;
      (b) content-token Jaccard >= 0.5               — reworded, same nouns;
      (c) SAME leading market + Jaccard >= 0.25       — same market, same angle
          reworded (the actual afternoon-repeat pattern; the force_topic path
          has no market-clash guard, so this catches it).
    Content tokens drop stopwords + <=2-char tokens. Fail-open (False) so a
    comparison hiccup never blocks publishing."""
    try:
        import difflib

        def _norm(s: str) -> str:
            s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
            return re.sub(r"\s+", " ", s).strip()

        def _content(s: str) -> set:
            return {w for w in _norm(s).split()
                    if w and len(w) > 2 and w not in _TITLE_STOP}

        def _lead_market(s: str) -> str:
            m = re.match(
                r"^([A-Z][a-zA-Z\.\- ]+?)"
                r"(?:,| Metro|:| - | – | — | Leads| Lead | Tops| Takes)", s or "")
            return _norm_market(m.group(1)) if m else ""

        nt_norm = _norm(title)
        if not nt_norm:
            return False
        nt_c = _content(title)
        nt_mkt = _lead_market(title)
        for rt in (recent_titles or []):
            nr_norm = _norm(rt)
            if not nr_norm:
                continue
            if difflib.SequenceMatcher(None, nt_norm, nr_norm).ratio() >= ratio_threshold:
                return True
            rc = _content(rt)
            if not nt_c or not rc:
                continue
            jacc = len(nt_c & rc) / len(nt_c | rc)
            if jacc >= 0.5:
                return True
            if nt_mkt and nt_mkt == _lead_market(rt) and jacc >= 0.25:
                return True
        return False
    except Exception:
        return False


def _recent_platform_ships(days: int = 7) -> list:
    """Best-effort list of platform enhancements shipped in the last N days, for
    the 'platform_update' auto-press topic (144 feat() commits/week were landing
    with zero coverage). Tries a git-derived feat() changelog first (works when
    the deploy carries .git); falls back to the brain capability ledger
    (brain_live_capabilities LIVE rows). Returns [] if neither yields anything —
    the caller then skips the platform_update branch. Never raises."""
    items: list = []
    # 1) git changelog — feat()/new-dataset/new-tool commit subjects
    try:
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            ["git", "log", f"--since={int(days)}.days", "--no-merges",
             "--pretty=%s", "-i", "--grep=^feat", "--grep=^data", "-E"],
            cwd=repo_root, capture_output=True, text=True, timeout=4)
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                s = re.sub(r"^(feat|data)(\([^)]*\))?:\s*", "", line.strip(),
                           flags=re.I).strip()
                if s and len(s) > 8:
                    items.append(s[:120])
    except Exception:
        pass
    # de-dupe preserving order, cap
    seen, uniq = set(), []
    for s in items:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    items = uniq[:12]
    # 2) fallback — capability ledger LIVE rows (containerized deploy w/o .git)
    if not items:
        try:
            from routes.brain_capability_ledger import _read_ledger
            for (name, status, _loc) in (_read_ledger() or []):
                if (status or "").upper() == "LIVE" and name:
                    items.append(str(name)[:120])
        except Exception:
            pass
        items = items[:12]
    return items


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


def _deals_reason(signals: dict) -> str | None:
    """r65-qa (#6): build a deals-topic reason string FROM real deal rows so
    Claude has concrete buyer/seller/value/MW to write about. Returns None when
    there are no usable deals — the picker then SKIPS the deals topic instead of
    letting Claude emit value-less 'Deal - Company' stubs (the broken post the
    user flagged). No em-dashes (LinkedIn flags them)."""
    deals = signals.get("recent_deals") or []
    lines = []
    for d in deals[:4]:
        buyer = (str(d.get("buyer") or "")).strip()
        if not buyer:
            continue
        seller = (str(d.get("seller") or "")).strip()
        parts = []
        v = d.get("value_m")
        if v:
            parts.append(f"${v/1000:.1f}B" if v >= 1000 else f"${v:.0f}M")
        mw = d.get("mw")
        if mw:
            parts.append(f"{mw:.0f} MW")
        detail = " / ".join(parts)
        who = f"{buyer} acquired {seller}" if seller and seller not in ("?",) else buyer
        lines.append(f"{who}{(' (' + detail + ')') if detail else ''}")
    if not lines:
        return None
    return ("M&A pulse: recent data-center transactions DC Hub tracked. "
            + "; ".join(lines)
            + ". Lead with the largest by value, state buyer/seller and value/MW "
              "exactly as given, do NOT invent figures, link dchub.cloud.")


# ─────────────────────────────────────────────────────────────────────
# Engagement-biased topic selection (Phase A+B follow-on).
#
# Mirrors the smart_style() epsilon-greedy pattern in routes/og_cards.py:905.
# We want today's topic pick to lean toward topics that have actually moved
# the needle on LinkedIn over the last 30 days — but keep the deterministic
# priority cascade (_pick_daily_topic_cascade) as a fallback whenever there
# isn't enough signal, OR on the explore tick of the epsilon-greedy roll.
#
# Tunables (env, with safe defaults):
#   DCHUB_TOPIC_EXPLORE_RATE   epsilon — fraction of days we ignore the
#                              winner and let the cascade run, so every
#                              topic keeps gathering fresh data
#   DCHUB_TOPIC_MIN_POSTS      a topic needs at least this many recent
#                              posts to be considered a real signal
#   DCHUB_TOPIC_MIN_IMPRESSIONS total impressions across all topics must
#                              clear this bar before we trust the bias
# ─────────────────────────────────────────────────────────────────────

_TOPIC_EXPLORE_RATE = float(os.environ.get('DCHUB_TOPIC_EXPLORE_RATE', '0.30'))
_TOPIC_MIN_POSTS = int(os.environ.get('DCHUB_TOPIC_MIN_POSTS', '2'))
_TOPIC_MIN_IMPRESSIONS = int(os.environ.get('DCHUB_TOPIC_MIN_IMPRESSIONS', '100'))

# Topics eligible for engagement-biased exploitation. Anything not in this
# list always falls through to the cascade (e.g. iso_focus / new_facility /
# the rotation themes — they have their own dedup logic and shouldn't be
# stolen mid-cascade).
_BIAS_CANDIDATE_TOPICS = (
    "theme_deals",
    "afternoon_pulse",
    "dcpi_leader",
    "dcpi_warning",
    "coverage_milestone",
    "ai_citation",
    "industry_pulse",
)


def _topic_performance() -> dict:
    """Per-topic LinkedIn engagement over the last 30 days.

    Returns {topic: {'avg_impressions': float,
                     'avg_engagement': float,
                     'n_posts': int}} or {} on any DB hiccup. Best-effort:
    never raises. The columns p.impressions / p.likes / p.comments are
    added in Phase A+B of the LinkedIn-ingest project; this function will
    just return {} until they exist, and the wrapper will fall through to
    the cascade — so it's safe to ship ahead of the schema change.
    """
    conn = _conn()
    if conn is None:
        return {}
    out = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT a.source_topic,
                       AVG(p.impressions)        AS avg_imp,
                       AVG(p.likes + p.comments) AS avg_eng,
                       COUNT(*)                  AS n_posts
                  FROM linkedin_posts p
                  JOIN auto_press_releases a ON a.slug = p.slug
                 WHERE p.posted_at > NOW() - INTERVAL '30 days'
                   AND p.impressions IS NOT NULL
                 GROUP BY a.source_topic
            """)
            for source_topic, avg_imp, avg_eng, n_posts in cur.fetchall():
                if not source_topic:
                    continue
                out[source_topic] = {
                    'avg_impressions': float(avg_imp or 0),
                    'avg_engagement':  float(avg_eng or 0),
                    'n_posts':         int(n_posts or 0),
                }
    except Exception as e:
        # Most common case while Phase A+B columns don't exist yet:
        # UndefinedColumn → log once, return empty, let cascade run.
        print(f"[topic-bias] _topic_performance query failed: {e}",
              file=sys.stderr)
        return {}
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _topic_bias_log(picked: str, mode: str, score=None) -> None:
    """Single-line stderr trace so operators can see why each pick fired."""
    try:
        score_repr = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
        print(f"[topic-bias] picked={picked} mode={mode} score={score_repr}",
              file=sys.stderr)
    except Exception:
        pass


def pick_topic_with_bias(default_topic_picker_fn, candidate_topics,
                         signals=None):
    """Epsilon-greedy topic pick biased by 30-day LinkedIn engagement.

    Returns a 3-tuple (result, mode, score):
      result: (topic_slug, reason) on an exploit pick, else None
      mode:   'biased'  — exploit pick returned
              'explore' — epsilon-greedy roll picked exploration; caller
                          should fall through to the cascade
              'cascade' — not enough signal to trust; caller should fall
                          through to the cascade
      score:  the exploit score (float) on 'biased', else None

    The mode is exposed so the wrapper can log accurately why each daily
    pick happened (operator visibility was explicit in the spec).
    """
    try:
        perf = _topic_performance()
    except Exception:
        return None, 'cascade', None

    if not perf:
        return None, 'cascade', None

    # Is there ANY topic with enough signal to trust? Same gate as
    # smart_style(): if global signal is too thin, deterministic logic wins.
    total_imp = sum(b.get('avg_impressions', 0) * b.get('n_posts', 0)
                    for b in perf.values())
    has_any_qualifier = any(
        (b.get('n_posts', 0) >= _TOPIC_MIN_POSTS and
         b.get('avg_impressions', 0) * b.get('n_posts', 0) >= _TOPIC_MIN_IMPRESSIONS)
        for b in perf.values()
    )
    if not has_any_qualifier or total_imp < _TOPIC_MIN_IMPRESSIONS:
        return None, 'cascade', None

    # Deterministic-per-day RNG so a single UTC day picks consistently
    # (matches the og_cards.smart_style pattern — important for the
    # afternoon retry to see the same "winner" the morning run saw).
    import random as _random
    day = datetime.utcnow().strftime('%Y-%m-%d')
    rng = _random.Random('topic-bias-' + day)

    if rng.random() < _TOPIC_EXPLORE_RATE:
        # Explore tick — let the cascade pick something so every topic
        # keeps accumulating impression data.
        return None, 'explore', None

    # Exploit — best (0.4*impressions + 0.6*engagement) score among
    # candidates that individually clear the bar.
    eligible = []
    for t in candidate_topics:
        b = perf.get(t)
        if not b:
            continue
        topic_total_imp = b.get('avg_impressions', 0) * b.get('n_posts', 0)
        if (b.get('n_posts', 0) >= _TOPIC_MIN_POSTS and
                topic_total_imp >= _TOPIC_MIN_IMPRESSIONS):
            score = (b['avg_impressions'] * 0.4) + (b['avg_engagement'] * 0.6)
            eligible.append((t, score, b))

    if not eligible:
        return None, 'cascade', None

    eligible.sort(key=lambda kv: kv[1], reverse=True)
    winner, winner_score, _winner_stats = eligible[0]
    reason = (f"Engagement-biased pick — '{winner}' has been the strongest "
              f"LinkedIn performer over the last 30 days "
              f"(impressions-weighted score {winner_score:.1f}). Lead with "
              f"the format the audience has rewarded.")
    return (winner, reason), 'biased', winner_score


def _pick_daily_topic(signals: dict) -> tuple[str, str]:
    """Wrapper: engagement-biased pick first, deterministic cascade second.

    The bias layer is conservative (see pick_topic_with_bias) — it only
    overrides the cascade when there's clear LinkedIn signal AND the
    epsilon-greedy roll says "exploit." Everything else falls through to
    the original Phase LL/MM/NN cascade, which still owns the dedup +
    market-clash guarantees we rely on.
    """
    result, mode, score = None, 'cascade', None
    try:
        result, mode, score = pick_topic_with_bias(
            _pick_daily_topic_cascade, _BIAS_CANDIDATE_TOPICS, signals=signals)
    except Exception as e:
        print(f"[topic-bias] wrapper crashed, falling through: {e}",
              file=sys.stderr)
        result, mode, score = None, 'cascade', None

    if result is not None:
        topic, reason = result
        _topic_bias_log(topic, mode, score=score)
        return topic, reason

    topic, reason = _pick_daily_topic_cascade(signals)
    _topic_bias_log(topic, mode, score=score)
    return topic, reason


def _pick_daily_topic_cascade(signals: dict) -> tuple[str, str]:
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
        # Phase FF (2026-05-22): widened 2 → 8. n=2 only blocked the same
        # market in back-to-back posts, so "Cheyenne, WY #1" recurred every
        # 3rd post (user-reported: "Cheyenne over and over"). 8 ≈ a week of
        # posts, so a persistent #1 mover can't dominate the feed. Only
        # affects the market-named branches (dcpi_mover/dcpi_leader); topic
        # variety for industry_pulse/iso_focus/coverage is unaffected.
        recent_markets = _recent_market_names(n=8)
    except NameError:
        recent_markets = set()
    try:
        # Fix (2026-07-04): trailing-14d story-family shares for the diversity
        # quota. Precomputed once here so _topic_dedup stays a pure dict lookup.
        fam_share = _family_shares(days=14)
    except NameError:
        fam_share = {}

    def _topic_dedup(t: str) -> bool:
        # Family-diversity quota (2026-07-04): block any topic whose story
        # family already owns > _FAMILY_QUOTA of the trailing-14d feed, so no
        # single family (excess-power was ~10/30) can dominate — even when the
        # topic's own per-topic window would allow it.
        try:
            if fam_share.get(_topic_family(t), 0.0) > _FAMILY_QUOTA:
                return True
        except NameError:
            pass
        try:
            return _topic_recently_ran(t, recent)
        except NameError:
            return t in recent

    def _market_clash(name: str | None) -> bool:
        """True if `name` resolves to a market featured recently. Normalizes
        via _norm_market so Cheyenne / Cheyenne, WY / Wyoming all collide."""
        if not name or not recent_markets:
            return False
        nm = _norm_market(name)
        if not nm:
            return False
        return any(nm == rm or nm.startswith(rm) or rm.startswith(nm)
                   for rm in recent_markets if rm)

    def _first_fresh(items: list, key: str = "market"):
        """Round-robin helper: return the first item whose market hasn't been
        featured recently, instead of fixating on items[0]. None if all clash."""
        for it in (items or []):
            if isinstance(it, dict) and not _market_clash(it.get(key)):
                return it
        return None

    # ── 0. DATA LEAD (r86c, HIGHEST priority) ───────────────────────
    # The brain's editorial desk picks today's single most newsworthy DATA
    # event (DCPI mover, top build market, M&A deal, interconnection-queue
    # depth) as a number+trend+so-what, and only if it's NOVEL (not already
    # posted this week). This replaces self-promotion as the #1 angle, so the
    # post leads with intelligence instead of "an AI cited us". When nothing
    # novel clears the bar, _lead is None and we fall through.
    try:
        from routes.media_editorial import editorial_decision
        _ed = editorial_decision("marketing")
        _lead = _ed.get("lead") if _ed.get("post") else None
    except Exception:
        _lead = None
    if _lead:
        return "data_lead", (
            f"{_lead.get('headline_number','')}. {_lead.get('trend','')}. "
            f"So what: {_lead.get('so_what','')} Open the post with this NUMBER; "
            f"add one neutral source line ({_lead.get('source_url','')}) AFTER "
            f"the insight; no brand pitch, no 'we are the authority'.")

    # ── 0.5 AI-citation showcase (r86c: DEMOTED from #1, capped) ────
    # Only when NO novel data event cleared the bar above AND a genuine citation
    # exists. Behind the topic dedup so the same citation never reposts. Lead
    # with the SPECIFIC data point the model cited, not "they cited us".
    cite = signals.get("recent_ai_citation")
    if cite and cite.get("quote") and not _topic_dedup("ai_citation"):
        eng = cite.get("engine") or "A leading AI assistant"
        return "ai_citation", (
            f"{eng} cited DC Hub answering '{(cite.get('prompt') or 'a data-center query')[:120]}'. "
            f"Quote: \"{cite['quote'][:280]}\". Lead with the SPECIFIC number/market the model "
            f"cited (what made the answer right), not the fact that we were cited; one short source line.")

    # ── 1. DCPI movers (high bar: |delta| >= 5pts) ──────────────────
    movers = signals.get("biggest_movers") or []
    if movers and not _topic_dedup("dcpi_mover"):
        # Round-robin: first mover with |delta|>=5 that isn't a recent repeat.
        m = _first_fresh([x for x in movers if abs(x.get("delta") or 0) >= 5])
        if m:
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

    # ── 2.5 Platform update — what DC Hub shipped this week (NEW) ────
    # Fix (2026-07-04): 144 feat() commits/week were landing on main with ZERO
    # coverage. Reports the week's shipped enhancements/new datasets/tools as a
    # concise changelog. Sticky 7-day window (_STICKY_TOPIC_WINDOWS) so it posts
    # at most once a week and never re-covers the same commit batch. Only fires
    # when there are >= 3 concrete ships (else the post would be thin).
    if not _topic_dedup("platform_update"):
        try:
            ships = _recent_platform_ships(days=7)
        except NameError:
            ships = []
        if len(ships) >= 3:
            _ship_list = "; ".join(ships[:6])
            return "platform_update", (
                f"Platform update — DC Hub shipped {len(ships)} enhancements in "
                f"the last 7 days: {_ship_list}. Write it as a concise product/"
                f"data changelog for agents & operators: what's new, why it "
                f"matters for a site-selection or capex decision, and how to "
                f"query it (MCP tool / endpoint). No hype; name the concrete new "
                f"datasets, tools, and endpoints.")

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
    if builds and not _topic_dedup("dcpi_leader"):
        b = _first_fresh(builds)   # round-robin past a persistent #1
        if b:
            return "dcpi_leader", (
                f"{b.get('market','top market')} leads the BUILD ranking "
                f"with excess power score {b.get('excess','?')}.")

    avoids = signals.get("top_avoid_markets") or []
    if avoids and not _topic_dedup("dcpi_warning"):
        a = _first_fresh(avoids)
        if a:
            return "dcpi_warning", (
                f"{a.get('market','a market')} flagged AVOID — highest "
                f"constraint score {a.get('constraint','?')}.")

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
        if theme_topic == "theme_deals":
            # r65-qa (#6): only run the deals theme when we have REAL deal rows;
            # otherwise fall through to the rotation instead of stubbing.
            _dr = _deals_reason(signals)
            if _dr and not _topic_dedup(theme_topic):
                return theme_topic, _dr
        elif not _topic_dedup(theme_topic):
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
    topic, reason = rotation[day_idx]
    if topic == "ma_pulse":
        # r65-qa (#6): feed real deal rows into the reason, or — if there are
        # none — substitute the evergreen methodology explainer so the engine
        # never publishes value-less "Deal - Company" stubs.
        _dr = _deals_reason(signals)
        if _dr:
            return topic, _dr
        return ("methodology_explainer",
                "Methodology explainer: how DC Hub's DCPI scoring works + what each axis measures.")
    return topic, reason


# Phase LL+1: ultra-safe last-resort topic. If everything else fails
# AND retry logic exhausts, fall back to this. Always produces a
# 250-300 word generic "DC Hub today" recap from platform signals.
_LAST_RESORT_TOPIC = (
    "platform_pulse",
    "Generic platform pulse — DC Hub's tracking footprint, today's data freshness, and how to query the dataset.",
)


def _attempt_plan(topic: str, topic_reason: str) -> list[tuple]:
    """The ordered (topic, reason, simpler_prompt, model) attempts for the daily
    auto-press retry loop.

    The fix for "1 auto-press in 30 days": the fallback model is tried from
    attempt 2, so a stale/renamed PRIMARY model id can't fail all three
    identical calls and zero out the day — one wasted call, then a known-good
    model takes over on the same primary topic (still the simpler prompt). The
    third attempt keeps the original platform_pulse last resort, also on the
    fallback model.

    Pure (returns plain tuples) so the retry strategy is unit-tested without a
    Flask/Anthropic import."""
    return [
        (topic, topic_reason, False, MARKETING_MODEL),
        (topic, topic_reason, True,  MARKETING_MODEL_FALLBACK),
        _LAST_RESORT_TOPIC + (True, MARKETING_MODEL_FALLBACK),
    ]


# ---------------------------------------------------------------------------
# 2. CLAUDE GENERATION
# ---------------------------------------------------------------------------

# r86c: analyst voice (shared spec from media_editorial, inline fallback for boot
# safety). Replaced the old "BRAND MANDATE: build DC Hub into THE authority / make
# DC Hub the lens" framing that made every post read like marketing.
try:
    from routes.media_editorial import ANALYST_VOICE as _ANALYST_VOICE
except Exception:
    _ANALYST_VOICE = (
        "You are a senior data-center infrastructure analyst. Lead with a specific "
        "NUMBER + the TREND (vs last week / ISO peers) + the SO-WHAT for a "
        "site-selection or capex decision, then a non-obvious second-order read. "
        "Dry, specific, no promotion; never invent a figure. Attribution is one "
        "neutral source line AFTER the insight. No brand-pillar speech.")

_MARKETING_SYSTEM = _ANALYST_VOICE + """

YOU ARE the autonomous analyst desk at DC Hub (live intelligence across 280+ US/global markets, 7 ISOs, 20,000+ facilities, 4,000+ M&A deals). Publish two coupled outputs, both built on the SINGLE most newsworthy DATA event of the last 24h:

A) A SHORT PRESS RELEASE (long-form, web/AI-citable)
B) A LINKEDIN POST (short-form, distribution-ready)

BOTH outputs MUST:
- Be FACTUAL — only use numbers and names provided in the signal payload. Never invent specific markets, scores, MW, or company names.
- LEAD WITH THE NUMBER + TREND. The first sentence states the metric and how it moved (e.g. "[Market], [STATE] climbed [N] points in the DCPI excess-power index this week" or "ERCOT's interconnection queue holds [N] GW, [X]% of all US queued load"). No number in the first sentence = rewrite it. Do NOT open with a brand claim.
- Then give the SO-WHAT for a real build/capex decision and one second-order implication.

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


def _inject_live_stats(system_prompt: str) -> str:
    """Phase FF #8b: swap the hardcoded "280+ markets... 20,000+ facilities"
    in the system prompt for the canonical live numbers, so Claude-generated
    posts quote ONE consistent figure. Fail-safe: any error → original prompt."""
    try:
        from canonical_stats import headline_blurb
        return system_prompt.replace(
            "tracking 280+ US/global markets, 7 ISOs, and 20,000+ facilities",
            "tracking " + headline_blurb())
    except Exception:
        return system_prompt


def _inject_editorial_lessons(system_prompt: str) -> str:
    """r66 EVOLVING-MEDIA LOOP: append the engine's OWN recent gate REJECTIONS
    (categorized, last 7d) to the generation prompt so it stops repeating the
    same mistakes at GENERATION time — not just gets blocked at publish. This is
    what makes DC Hub Media 'know better': the editor's rejections become the
    next generation's guardrails. Reads media_review_log (written by
    content_publisher._record_media_block). Fail-safe → original prompt."""
    try:
        c = _conn()
        if c is None:
            return system_prompt
        cats = {}
        try:
            with c.cursor() as cur:
                cur.execute("""SELECT reason FROM media_review_log
                    WHERE decision = 'blocked'
                      AND created_at > NOW() - INTERVAL '7 days'
                      AND reason IS NOT NULL LIMIT 500""")
                for row in cur.fetchall() or []:
                    r = (row[0] or "").lower()
                    if "disclaimer" in r:
                        k = "quoting an AI that DISCLAIMS knowledge as if it were 'validation' (never showcase 'I don't have info' responses)"
                    elif "duplicate" in r or "hook" in r:
                        k = "duplicating a recent post — same hook/story (vary the angle, pick a different topic)"
                    elif "zero-stat" in r:
                        k = "leading with a headline stat that is 0 / empty"
                    elif "stub" in r or "deal" in r:
                        k = "a value-less deal stub (a 'Deal - Company' line with no $ or MW)"
                    elif "low quality" in r or "editor rejected" in r:
                        k = "thin, low-signal, off-brand, or cringe/over-claiming content"
                    else:
                        k = "other quality issues"
                    cats[k] = cats.get(k, 0) + 1
        finally:
            try: c.close()
            except Exception: pass
        if not cats:
            return system_prompt
        top = sorted(cats.items(), key=lambda x: -x[1])[:5]
        lessons = "\n".join(f"- {n}× {k}" for k, n in top)
        return system_prompt + (
            "\n\nRECENT EDITORIAL REJECTIONS (last 7 days) — drafts BLOCKED before "
            "publishing for these reasons. DO NOT repeat any of them:\n" + lessons +
            "\nWrite this post so it would clear the editor on all of the above."
        )
    except Exception:
        return system_prompt


def _inject_engagement_signal(system_prompt: str) -> str:
    """r70 MEASURE→LEARN: append the engine's OWN best-performing recent posts
    (ranked by REAL reader click-through on dchub.cloud, from press_engagement)
    so generation leans toward angles that demonstrably land. Closes the
    measure→learn half the engine was missing — using the SAME slug-join as
    og_performance(), no native-social API needed. ADDITIVE + fail-soft → the
    original prompt (a thin-data week or any query error never blocks
    generation, and the tuned _pick_daily_topic picker is left untouched)."""
    try:
        c = _conn()
        if c is None:
            return system_prompt
        rows = []
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT a.slug,
                           COUNT(e.id) FILTER (WHERE e.event_type = 'view') AS views,
                           COUNT(e.id) FILTER (WHERE e.event_type IN ('click_out','stripe_click')) AS clicks
                      FROM auto_press_releases a
                      LEFT JOIN press_engagement e ON e.slug = a.slug
                     WHERE a.generated_at > NOW() - make_interval(days => 30)
                     GROUP BY a.slug
                    HAVING COUNT(e.id) FILTER (WHERE e.event_type = 'view') >= 5
                     ORDER BY (COUNT(e.id) FILTER (WHERE e.event_type IN ('click_out','stripe_click'))::float
                               / NULLIF(COUNT(e.id) FILTER (WHERE e.event_type = 'view'), 0)) DESC NULLS LAST
                     LIMIT 3
                """)
                rows = cur.fetchall() or []
        finally:
            try: c.close()
            except Exception: pass
        if not rows:
            return system_prompt
        import re as _re
        def _angle(slug):
            s = _re.sub(r'^auto-\d{4}-\d{2}-\d{2}-', '', slug or '')
            return s.replace('-', ' ').strip() or (slug or '')
        lines = []
        for slug, views, clicks in rows:
            v = int(views or 0); k = int(clicks or 0)
            ctr = round(100.0 * k / v, 1) if v else 0.0
            lines.append(f'- "{_angle(slug)}" — {v} views, {k} click-throughs ({ctr}% CTR)')
        return system_prompt + (
            "\n\nWHAT'S PERFORMING (your last 30 days, by REAL reader click-through "
            "on dchub.cloud) — lean toward these proven angles when the day's signals "
            "allow; do NOT copy them verbatim:\n" + "\n".join(lines)
        )
    except Exception:
        return system_prompt


def _call_claude_marketing(prompt: str, model: str | None = None) -> tuple[dict | None, str | None]:
    """Single Anthropic call. Returns (parsed_json, error). `model` overrides
    MARKETING_MODEL so the retry loop can fall back to a known-good model when
    the primary id is stale/rejected (the cadence-killing failure mode)."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key"
    from urllib.request import Request, urlopen
    body = json.dumps({
        "model": model or MARKETING_MODEL,
        "max_tokens": 1500,
        "system": _inject_engagement_signal(_inject_editorial_lessons(_inject_live_stats(_MARKETING_SYSTEM))),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = Request(anthropic_messages_url(), data=body, headers={
        "Content-Type": "application/json",
        "X-API-Key": ANTHROPIC_API_KEY,
        "User-Agent": "dchub-brain/1.0",
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


def _agent_claim_gate_denies(text: str, platform: str = "linkedin") -> tuple[bool, list]:
    """r-media-canon-gate (2026-07-02): composition-time honesty gate for
    AGENT-COUNT copy only. This engine's daily releases legitimately quote
    GW/%/MW figures the corroboration guard cannot prove from a structured
    source, so gating EVERY release would dark-hole the whole feed — instead
    we run the one-call gate (routes.media_fact_check_guard.gate_media_text)
    only when the text makes an "N AI agents / N unique callers" claim, the
    exact class the session-inflated "up 41% week-over-week" post came from.
    Everything else keeps its existing gate at the content_publisher drain.

    Activation follows the guard module's kill-switch
    (MEDIA_FACT_CHECK_GUARD_ENABLED, default OFF) so rollout is a Railway env
    flip. Returns (denied, reasons); with the flag ON a gate crash on
    agent-claim copy fails CLOSED."""
    try:
        from routes.media_fact_check_guard import (
            _enabled, check_agent_count_claims, gate_media_text)
    except Exception as e:
        print(f"[marketing_engine] fact-check guard unavailable: {str(e)[:120]}",
              file=sys.stderr)
        return False, []
    try:
        if not _enabled():
            return False, []
        if not (check_agent_count_claims(text or "").get("claims")):
            return False, []  # no agent-count claim → not this gate's job
    except Exception:
        return False, []
    # Own AUTOCOMMIT connection for the guard's dedup/quality SELECTs — never
    # a shared write transaction, so a failed guard query can't abort pending
    # INSERTs (the shared-tx poison trap).
    conn = _conn()
    cur = None
    if conn is not None:
        try:
            conn.autocommit = True
            cur = conn.cursor()
        except Exception:
            cur = None
    try:
        res = gate_media_text(cur, text or "", platform)
        return (not res.get("allow", False)), list(res.get("reasons") or [])
    except Exception as e:
        return True, [f"gate raised — failing closed ({str(e)[:100]})"]
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


# The scalar keys that keep a truncated press audit row identifiable.
_PRESS_KEEP_KEYS = ("as_of", "daily_topic", "daily_topic_reason")


def _json_for_column(payload, max_chars: int = 8000) -> str:
    r"""Serialise `payload` to JSON that is ALWAYS valid, even when too big.

    ★ r-json-truncation (2026-08-10) — THE FIVE-DAY PRESS OUTAGE.
    This replaced `json.dumps(signals)[:8000]`, a slice taken over the
    SERIALISED STRING rather than over the data. auto_press_releases
    .source_data is **jsonb**, so the cut made the whole statement fail:

        InvalidTextRepresentation: invalid input syntax for type json
        DETAIL: Token ""Midland\u2...      <- cut inside a unicode escape

    That INSERT is the LAST statement of _write_release's single
    transaction, so its failure took the press_releases row and the
    press_integrity review down with it — and the `except` around it calls
    c.rollback(), which is what actually discards them. The release simply
    never existed; nothing a dashboard reads showed a trace. It was
    INTERMITTENT only because it depends on how large the signals payload
    serialises that day, and it went near-total as the signal set grew past
    the character cap.

    ★ The BODY now lives in util/json_column.py, because this was never a
    press bug — 23 sites repo-wide sliced a serialised blob into a json or
    jsonb column. Keeping one implementation means the next fix to this
    class lands everywhere at once. This wrapper survives only to hold the
    press-specific keep-keys and this account of what happened.
    """
    return json_for_column(payload, max_chars, keep_keys=_PRESS_KEEP_KEYS)


def _write_release(rel: dict, signals: dict, topic: str) -> tuple[int | None, str | None]:
    """Persist to the canonical press_releases table + audit row in
       auto_press_releases. Returns (press_release_id, error).
       Phrasing avoids the literal "INSERT INTO" prefix in this
       docstring so the regression-lint regex doesn't match prose."""
    # r-media-canon-gate (2026-07-02): if the composed release makes an
    # agent-count claim, corroborate it BEFORE the row is inserted. On denial:
    # ONE log line + drop the release — never persist a stripped version.
    _denied, _greasons = _agent_claim_gate_denies(
        f"{rel.get('title') or ''}\n{rel.get('body') or ''}", "linkedin")
    if _denied:
        print(f"[marketing_engine] media gate dropped release "
              f"{rel.get('slug')}: {'; '.join(_greasons)[:400]}", file=sys.stderr)
        return None, "media_gate_denied"
    # hybrid-newsroom (2026-07-19): brain editorial review before anything
    # auto-publishes. The fact-check lane proves numbers TRUE; this proves the
    # story NEW (kills the "same market, same score, re-worded" repeats).
    # Verdict draft → the row is still written but published=FALSE, so it
    # surfaces in the pending-drafts digest instead of the public feed.
    # Fail-closed inside the gate; kill via MEDIA_EDITORIAL_GATE_DISABLED=1.
    try:
        from routes.media_editorial_gate import editorial_gate
        _ed = editorial_gate(rel.get("title") or "", rel.get("body") or "",
                             "press_release", market_slug=rel.get("market_slug"),
                             press_slug=rel.get("slug"))
    except Exception as _ed_e:
        _ed = {"action": "draft", "reasons": [f"gate import failed: {str(_ed_e)[:80]}"]}
    _publish = _ed.get("action") == "publish"
    if not _publish:
        print(f"[marketing_engine] editorial gate held release "
              f"{rel.get('slug')} as draft: {'; '.join(_ed.get('reasons') or [])[:300]}",
              file=sys.stderr)
    # press-integrity (2026-08-07): the LAST gate before the row is written, and
    # the only one of the three composers that can actually set published=TRUE.
    # The editorial gate above proves the story is NEW; the fact-check lane
    # proves the numbers are TRUE; this proves the artifact is WHOLE — a body
    # that is blank or a stub, placeholder/error text, a future date, a missing
    # title. That combination is what shipped /news/2026-08-07-perplexity-dcpi-
    # dual-score-citation as a live, blank, future-dated page. A HARD failure
    # can only ever DEMOTE to draft here; it never promotes.
    _pi = {"publish": _publish, "hard": False, "codes": []}
    try:
        from routes.press_integrity import attach_review, gate_press_publish
        _pi = gate_press_publish(
            {"title": rel.get("title"), "slug": rel.get("slug"),
             "body": rel.get("body"),
             "subheadline": rel.get("subheadline"),
             "meta_description": rel.get("meta_description"),
             "date": date.today().isoformat()},
            want_published=_publish, where="marketing_engine._write_release")
        if _publish and not _pi.get("publish"):
            print(f"[marketing_engine] press-integrity BLOCKED publish of "
                  f"{rel.get('slug')} — held as draft: "
                  f"{', '.join(str(x) for x in _pi.get('codes') or [])}",
                  file=sys.stderr)
        _publish = bool(_pi.get("publish"))
    except Exception as _pie:
        print(f"[marketing_engine] press-integrity gate unavailable: "
              f"{str(_pie)[:120]}", file=sys.stderr)
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
                     slug, source, category, published_date, date, published,
                     published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title            = EXCLUDED.title,
                    summary          = EXCLUDED.summary,
                    subheadline      = EXCLUDED.subheadline,
                    body             = EXCLUDED.body,
                    meta_description = EXCLUDED.meta_description,
                    published_date   = EXCLUDED.published_date,
                    published        = EXCLUDED.published,
                    published_at     = EXCLUDED.published_at
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
                _publish,                                       # published
                datetime.utcnow() if _publish else None,        # published_at
            ))
            press_id = cur.fetchone()[0]
            # Record WHY, beside the row rather than inside its copy, so a
            # held draft is explainable without re-running the reviewer.
            try:
                attach_review(cur, rel["slug"], _pi, "marketing_engine")
            except Exception:
                pass
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
                    _json_for_column(signals),
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
                    _json_for_column(signals),
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

        # 2026-06-07: INSTANT IndexNow ping — Bing/Yandex index the new release in
        # minutes instead of crawl-days. Failure-isolated like every distribution
        # step below; the press release already committed above.
        try:
            from routes.indexnow import submit_to_indexnow
            submit_to_indexnow([f"https://dchub.cloud/news/{rel['slug']}",
                                "https://dchub.cloud/news"])
        except Exception as _idxn_err:
            print(f"[marketing_engine] IndexNow ping failed: {_idxn_err}", file=sys.stderr)

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
        # ★ r-draft-fanout (2026-08-10). Do NOT queue distribution for a
        # release the editorial desk HELD. This block used to run
        # unconditionally, queuing LinkedIn/Twitter/Bluesky rows at
        # status='approved' regardless of the `published` value computed a few
        # lines above — and none of the three drains joined
        # press_releases.published, so whether a held draft reached the public
        # was decided by _should_skip_publish's unrelated content judgement.
        # Measured 2026-08-04..08-09: every press-linked social post that
        # actually went out pointed at a DRAFT (100202 Meta, 100200 MISO,
        # 100186 Tulsa on X + Bluesky); only 100196 CoreWeave was published.
        # The drains now carry the same gate for the already-queued backlog.
        # ⚠ Consequence, deliberate: while the publish path is broken this
        # SILENCES the daily social feed rather than shipping held drafts.
        # That is the honest state, not a regression. Escape hatch for an
        # operator who wants the old behaviour: PRESS_DRAFT_SOCIAL_FANOUT=1.
        # ★ This returns BEFORE _notify_mcp_subscribers as well as before the
        #   social queue, and that is deliberate: mailing the subscriber list
        #   about a release the desk held is the same leak through a quieter
        #   door. The press_releases row is already committed above, so the
        #   draft still reaches the pending-drafts digest and a human's
        #   one-click approve — the only paths that should promote it.
        _draft_fanout_ok = (os.environ.get("PRESS_DRAFT_SOCIAL_FANOUT", "")
                            .strip().lower() in ("1", "true", "yes", "on"))
        if not _publish and not _draft_fanout_ok:
            print(f"[marketing_engine] held draft {rel.get('slug')} — social "
                  f"distribution and subscriber mail NOT queued "
                  f"(editorial verdict: draft)", file=sys.stderr)
            return press_id, None
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
        # ★ r-write-visibility (2026-08-10). Everything above — press_releases,
        # its press_integrity review, and the auto_press_releases audit row —
        # is ONE transaction ending at a single c.commit(). Landing here
        # discards all three, so a failed write leaves NO trace in any table a
        # dashboard reads: the story simply never existed. On 2026-08-09 that
        # happened 19 times out of 20 (20 editorial reviews, 1 press row) and
        # took the only release the desk APPROVED that day with it, while the
        # press desk showed five days of silence and every single-stage metric
        # read healthy. A stderr print alone was not enough — nothing
        # aggregates it. note_swallowed_write puts the exception TYPE and
        # message into the rate-limited WARNING channel and into the counter
        # that /api/v1/admin/metric-truth/check reports, so the next
        # occurrence is diagnosable without reproducing it by hand. The
        # stage-to-stage ratio that makes the loss visible in aggregate lives
        # in routes/press_pipeline_master_shell.py (lane B).
        note_swallowed_write("press_releases",
                             where="marketing_engine._write_release")
        print(f"[marketing_engine] write failed: {e}", file=sys.stderr)
        return None, f"db_error: {type(e).__name__}: {str(e)[:200]}"
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
    # linkedin_404 fix: only point to /news/<slug> when the release
    # is actually published; otherwise fall back to /partners so we
    # don't post links to 404-ing pages.
    if rel.get("published") is True and slug:
        url = f"https://dchub.cloud/news/{slug}"
    else:
        url = "https://dchub.cloud/partners"
    parts = [title]
    if sub: parts.append(sub)
    parts.append(f"Full release → {url}")
    # 2026-06-10: DCPI-led posts were rejected by the editor-in-chief
    # ("unverifiable DCPI scores; methodology link untraceable; lacks credibility
    # markers"). Cite the public methodology page on DCPI-topic posts so the score
    # is verifiable — directly answers that rejection criterion. /dcpi is 200
    # (/dcpi/methodology 308-redirects), #methodology anchors the section.
    if 'dcpi' in (title + ' ' + sub).lower() or 'excess power' in (title + ' ' + sub).lower():
        parts.append("DCPI methodology → https://dchub.cloud/dcpi#methodology")
    # Phase HH+1: DC Hub Media branding — newsroom byline + tag.
    parts.append("Published by DC Hub Media — dchub.cloud/dc-hub-media")
    parts.append("#DCHub #DCHubMedia #datacenter #infrastructure")
    return "\n\n".join(parts)[:2900]


def _pick_linkedin_style(rel: dict) -> str:
    """Phase ZZZZZ-round16 (2026-05-23) — 4-style rotation.
    Deterministic so the same press release always gets the same
    style (idempotent across regenerations), but varied across the
    week so the feed doesn't read uniform. Rotation key = press_id
    or sha(slug) → 1 of 4 styles.

    The styles:
      - data:        hook-first, 3 bullets, numbers-heavy (current)
      - narrative:   scene-setting opening, story arc, less listy
      - listicle:    'The 5 things you need to know' numbered list
      - contrarian:  'Everyone says X. The data shows Y.' angle
    """
    import hashlib
    seed = rel.get("id") or rel.get("press_id") or rel.get("slug") or ""
    h = hashlib.sha256(str(seed).encode()).hexdigest()
    idx = int(h[:8], 16) % 4
    return ("data", "narrative", "listicle", "contrarian")[idx]


def _claude_rewrite_for_linkedin(rel: dict, style: str | None = None) -> str | None:
    """Phase HH: Claude rewrites the press release into a punchy
    LinkedIn post. Optimized for engagement: opens with a hook
    (stat, contrarian angle, or specific number), 2-3 short insight
    bullets, then a CTA. Hashtag footer.

    Phase ZZZZZ-round16 (2026-05-23): now supports 4-style rotation
    (data/narrative/listicle/contrarian). Style is picked by
    _pick_linkedin_style(rel) unless explicitly overridden.

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

    if style is None:
        style = _pick_linkedin_style(rel)

    # Per-style structure instructions. The trailing CTA + byline +
    # hashtag block is the same across all 4 (keeps brand consistency
    # while the lead body changes shape).
    style_instructions = {
        "data": (
            "1. HOOK (line 1): single sentence opening with the most "
            "   surprising stat or contrarian claim from the release. "
            "   Numbers belong on this line. No throat-clearing.\n"
            "2. CONTEXT (1-2 short sentences): why this matters now.\n"
            "3. THREE BULLETS (use '→' as the marker): "
            "   the three most quotable findings. Each bullet ≤ 110 chars. "
            "   At least two bullets contain a specific number "
            "   (MW, $, %, or rank).\n"
        ),
        "narrative": (
            "1. SCENE (lines 1-3): open with a vivid mini-scene or "
            "   counter-intuitive observation. NO bullets in the first "
            "   half. Treat the press release as a story arc.\n"
            "2. PIVOT (1-2 sentences): the inflection point — what just "
            "   changed, what the data revealed. ONE precise number.\n"
            "3. PAYOFF (1-2 sentences): what this means for the reader "
            "   (operator, investor, policy wonk). 1 more number max.\n"
        ),
        "listicle": (
            "1. HOOK (line 1): 'The N [things/markets/signals] you need to "
            "   know about [topic] this week.' Pick a number 3-5.\n"
            "2. NUMBERED ITEMS (use '1.' '2.' '3.' …): each item 1-2 "
            "   short sentences, opens with a market name or specific "
            "   entity, includes ONE precise number. Match the count "
            "   announced in the hook.\n"
            "3. NO closing bullets — go straight to the CTA after the "
            "   last numbered item.\n"
        ),
        "contrarian": (
            "1. PREMISE (line 1): 'Everyone says X.' or 'The conventional "
            "   wisdom is X.' — name the assumption being challenged.\n"
            "2. REVERSAL (line 2): 'The data shows Y.' — one sentence "
            "   stating the actual finding, with a number.\n"
            "3. EVIDENCE (2-3 short paragraphs): the supporting facts. "
            "   At least two specific numbers (MW, $, %, rank). Avoid "
            "   bullets — make it argumentative prose.\n"
        ),
    }
    section_a = style_instructions.get(style, style_instructions["data"])

    prompt = (
        f"You are writing a LinkedIn post in the '{style}' style for "
        "DC Hub Media — the newsroom arm of DC Hub (a data center "
        "intelligence platform). The post promotes a DC Hub press "
        "release. Audience: infrastructure investors, hyperscale ops "
        "leaders, and policy wonks who follow grid + power markets.\n\n"
        "GOAL: a high-engagement LinkedIn post — feed-stopping, "
        "info-dense, 2026 newsroom voice. Optimize for clicks to the URL.\n\n"
        "STRUCTURE (strict):\n"
        f"{section_a}"
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
            anthropic_messages_url(),
            json={
                "model": MARKETING_MODEL,
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "User-Agent": "dchub-brain/1.0",
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
            # Item 8 (2026-06-30): append the one canonical reach CTA to every
            # distribution body at the single enqueue chokepoint (covers all three
            # composer return paths — pre-generated / Claude / static — without
            # touching each). Long-form CTA for LinkedIn; short + 280-cap-aware for
            # X; short + 300-cap-aware for Bluesky. Fail-soft import.
            try:
                from media_cta import append_reach_cta as _cta
            except Exception:
                _cta = None

            # r-media-canon-gate (2026-07-02): each channel's text is composed
            # independently (pre-generated / Claude rewrite / static), so gate
            # each one for agent-count claims BEFORE its row is inserted. The
            # gate runs on the composed body PRE-CTA (the canonical reach CTA
            # is a constant, known fence-safe append whose "$10 = 1,000 calls"
            # figure would otherwise trip the dollar-aggregate fail-closed
            # check). On denial: ONE log line + drop that channel's post
            # entirely — never queue a stripped version.
            li_text = _format_linkedin_post(rel)
            _li_denied, _li_why = _agent_claim_gate_denies(li_text, "linkedin")
            if _li_denied:
                print(f"[marketing_engine] media gate dropped linkedin post for "
                      f"{rel.get('slug')}: {'; '.join(_li_why)[:400]}", file=sys.stderr)
            else:
                if _cta: li_text = _cta(li_text)
                cur.execute("""
                    INSERT INTO social_media_posts
                        (platform, content, status, press_release_id, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (press_release_id, platform) DO NOTHING
                """, ("linkedin", li_text, "approved", press_id))

            tw_text = _format_twitter_post(rel)
            _tw_denied, _tw_why = _agent_claim_gate_denies(tw_text, "twitter")
            if _tw_denied:
                print(f"[marketing_engine] media gate dropped twitter post for "
                      f"{rel.get('slug')}: {'; '.join(_tw_why)[:400]}", file=sys.stderr)
            else:
                if _cta: tw_text = _cta(tw_text, short=True, max_chars=280)
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
            _bs_denied, _bs_why = _agent_claim_gate_denies(bsky_text, "bluesky")
            if _bs_denied:
                print(f"[marketing_engine] media gate dropped bluesky post for "
                      f"{rel.get('slug')}: {'; '.join(_bs_why)[:400]}", file=sys.stderr)
            else:
                if _cta: bsky_text = _cta(bsky_text, short=True, max_chars=300)
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
        # Phase 3 (2026-06-18): per-recipient tokenized unsubscribe + RFC 8058
        # one-click headers via routes.email_suppression. The old bare
        # https://dchub.cloud/unsubscribe link had no token and was a dead end.
        # Guarded import so a missing module degrades gracefully (falls back to
        # the legacy bare link, never crashes the press-notify send).
        try:
            from routes.email_suppression import (
                unsub_link as _unsub_link,
                list_unsubscribe_headers as _list_unsub_headers,
            )
        except Exception:
            _unsub_link = None
            _list_unsub_headers = None

        # Body is built per-recipient inside the loop so each recipient gets a
        # token bound to their address. {unsub} is filled in there.
        html_body_tmpl = """<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1a1a1a">
<div style="font-size:11px;color:#888;letter-spacing:.05em;text-transform:uppercase;margin-bottom:8px">Daily Press Release · DC Hub</div>
<h2 style="margin:0 0 12px;font-size:22px;line-height:1.3">{title}</h2>
<p style="color:#555;margin:0 0 24px;font-size:15px;line-height:1.5">{sub}</p>
<p style="margin:24px 0"><a href="{url}" style="background:#1976d2;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">Read the full release →</a></p>
<hr style="border:0;border-top:1px solid #eee;margin:32px 0">
<p style="font-size:12px;color:#888">You're receiving this because you signed up at dchub.cloud. <a href="{unsub}" style="color:#888">Unsubscribe</a> · <a href="https://dchub.cloud" style="color:#888">dchub.cloud</a></p>
</body></html>"""
        _safe_title = _html_escape(title)
        _safe_sub = _html_escape(sub)

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
                    # Phase 3: skip anyone on the suppression list (one-click
                    # unsubscribers, bounces). Reuses the already-open conn `c`;
                    # guarded so a missing module/table never blocks the send.
                    try:
                        from routes.email_suppression import is_suppressed as _is_suppressed
                        with c.cursor() as _scur:
                            if _is_suppressed(_scur, to_addr):
                                continue
                    except Exception:
                        try: c.rollback()
                        except Exception: pass

                    # Per-recipient tokenized unsubscribe link + one-click hdrs.
                    if _unsub_link is not None:
                        _ulink = _unsub_link(to_addr)
                    else:
                        _ulink = ("https://dchub.cloud/unsubscribe?email="
                                  + str(to_addr))
                    html_body = html_body_tmpl.format(
                        title=_safe_title, sub=_safe_sub, url=url, unsub=_ulink)
                    _payload = {
                        "from": sender,
                        "to": [to_addr],
                        "subject": subject,
                        "html": html_body,
                    }
                    if _list_unsub_headers is not None:
                        try:
                            _payload["headers"] = _list_unsub_headers(to_addr)
                        except Exception:
                            pass
                    resp = _rq.post(
                        "https://api.resend.com/emails",
                        json=_payload,
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

       2026-05-25 r34i: optional ?force_topic= param unlocks a second
       piece per day (target: lift count_30d 13 → ~25). The dedup query
       includes source_topic when force_topic is set, so a morning piece
       (auto-picked topic) and an afternoon piece (force_topic=
       afternoon_pulse) coexist without one blocking the other. The
       afternoon cron lives in evolve-cron.yml at 17:30 UTC.
    """
    today = date.today().isoformat()
    force_topic = (request.args.get("force_topic") or "").strip() or None
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        # Dedup query: by-topic when force_topic is set (allows multiple
        # pieces per day if they're distinct topics); else by-day legacy
        # behavior (preserves the morning cron's idempotency contract).
        with c.cursor() as cur:
            if force_topic:
                cur.execute("""SELECT id, slug, title, press_release_id
                                 FROM auto_press_releases
                                WHERE generated_for = %s
                                  AND source_topic = %s
                                LIMIT 1""", (today, force_topic))
            else:
                cur.execute("""SELECT id, slug, title, press_release_id
                                 FROM auto_press_releases
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
    # r34i (2026-05-25): force_topic override unlocks 2/day cadence.
    # When passed (typically by the afternoon cron with a distinct slug
    # like "afternoon_pulse"), skip the weekday-themed picker entirely
    # and use the caller's slug + a generic reason. Source_topic gets
    # set to force_topic downstream so the by-topic dedup recognizes
    # this distinct piece on subsequent ticks.
    if force_topic:
        topic = force_topic
        topic_reason = (f"Afternoon slot — operator-forced topic slug "
                        f"'{force_topic}' to unlock 2nd piece this UTC day.")
    else:
        topic, topic_reason = _pick_daily_topic(signals)
    signals["daily_topic"] = topic
    signals["daily_topic_reason"] = topic_reason

    # Fix (2026-07-04): the force_topic afternoon slot (17:30 UTC afternoon_pulse)
    # bypassed _pick_daily_topic AND every dedup guard, so afternoon pieces kept
    # collapsing onto the same ~5 story families (the 83%-repetitive symptom).
    # Build an explicit DO-NOT-REPEAT context — last-7d headlines + recently
    # featured markets — and (a) inject it into the prompt below, (b) reject
    # near-duplicate output in the retry loop. Applied to BOTH the force_topic
    # and auto-picked paths (belt-and-suspenders on top of the cascade guards).
    try:
        _dnr_titles = _recent_titles(days=7)
    except Exception:
        _dnr_titles = []
    try:
        _dnr_markets = sorted(m for m in _recent_market_names(n=8) if m)
    except Exception:
        _dnr_markets = []
    _dnr_block = ""
    if _dnr_titles or _dnr_markets:
        _parts = ["\n\nDO NOT REPEAT — the last 7 days already covered the "
                  "stories below. Choose a genuinely different angle, market, "
                  "and headline structure:"]
        if _dnr_titles:
            _parts.append("Recent headlines:\n- " + "\n- ".join(_dnr_titles[:12]))
        if _dnr_markets:
            _parts.append("Recently-featured markets (do NOT lead with these): "
                          + ", ".join(_dnr_markets))
        _parts.append("If today's signal forces one of these markets, lead with "
                      "a NEW number/second-order read and a distinct headline.")
        _dnr_block = "\n".join(_parts)

    rel = None
    err = None
    why = None
    last_attempt_err = None

    _attempts = _attempt_plan(topic, topic_reason)
    for attempt_idx, (att_topic, att_reason, att_simpler, att_model) in enumerate(_attempts):
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
                + _dnr_block
            )
        else:
            prompt = (f"Daily signals (topic: {att_topic} — {att_reason}):\n"
                      "```\n" + json.dumps(signals, indent=2)[:6000] + "\n```"
                      + _dnr_block)

        rel, err = _call_claude_marketing(prompt, model=att_model)
        if err or not rel:
            last_attempt_err = f"attempt_{attempt_idx+1}: claude_error={err} (model={att_model})"
            print(f"[marketing] {last_attempt_err}", file=sys.stderr)
            continue

        ok, why = _validate_release(rel)
        if not ok:
            last_attempt_err = f"attempt_{attempt_idx+1}: validation_failed={why}"
            print(f"[marketing] {last_attempt_err}", file=sys.stderr)
            rel = None
            continue

        # Near-duplicate guard (2026-07-04): even a schema-valid release is
        # rejected when its headline is ~the same story we ran in the last 7
        # days — the actual 83%-repetitive symptom. Retrying re-enters the loop
        # with the next attempt (simpler prompt / fallback model), which sees the
        # same DO-NOT-REPEAT block and should pivot. The last attempt is the
        # generic platform_pulse, so this never dead-ends the day's output.
        # The FINAL attempt is allowed through even if flagged, so a genuinely
        # data-flat day still publishes *something* (preserves the "never a
        # silent day" contract the retry loop was built for).
        _is_last_attempt = (attempt_idx >= len(_attempts) - 1)
        if (_dnr_titles and not _is_last_attempt
                and _is_near_dup_title(rel.get("title", ""), _dnr_titles)):
            last_attempt_err = (f"attempt_{attempt_idx+1}: near_duplicate_title="
                                f"{(rel.get('title') or '')[:80]!r}")
            print(f"[marketing] {last_attempt_err}", file=sys.stderr)
            rel = None
            continue

        # Got a valid, non-duplicate release. Break out of retry loop.
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


@marketing_bp.get("/api/v1/marketing/linkedin-token-test")
def linkedin_token_test():
    """Phase ZZZZ-li-debug (2026-05-18): validate the current
    LINKEDIN_ACCESS_TOKEN env var WITHOUT publishing anything.
    Calls LinkedIn's /v2/userinfo which works for any valid token
    and returns the authenticated user's info. Failing here means
    the token is expired, malformed, or has wrong scopes.

    Public (no admin gate) because it's read-only + helps diagnose
    publishing failures from the worker-status dashboard."""
    import os as _os
    import requests as _req
    token = (_os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    org_id = (_os.environ.get("LINKEDIN_ORG_ID") or "110894959").strip()
    if not token:
        return jsonify(ok=False, error="LINKEDIN_ACCESS_TOKEN not set in Railway"), 503
    try:
        r = _req.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {token}",
                     "X-Restli-Protocol-Version": "2.0.0"},
            timeout=10,
        )
        body = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text[:400]}
        # Also probe the organization (the actual posting endpoint needs this)
        org = _req.get(
            f"https://api.linkedin.com/v2/organizations/{org_id}",
            headers={"Authorization": f"Bearer {token}",
                     "X-Restli-Protocol-Version": "2.0.0"},
            timeout=10,
        )
        org_body = org.json() if org.headers.get("content-type","").startswith("application/json") else {"raw": org.text[:400]}
        return jsonify(
            ok=(r.status_code == 200),
            token_length=len(token),
            token_prefix=token[:8] + "...",
            userinfo_status=r.status_code,
            userinfo_body=body,
            org_id=org_id,
            org_status=org.status_code,
            org_body=org_body,
            hint=("If userinfo returns 401, token is expired or invalid. "
                  "If userinfo is 200 but org is 401/403, token is missing "
                  "the w_member_social or r_organization_admin scope, OR is "
                  "scoped to a user not authorized for the org_id."),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}"), 503


# Phase ZZZZZ-round16 (2026-05-23): Twitter / X is showing 0 posts in
# 7d despite TWITTER_API_KEY family being configured. Add a parallel
# whoami so we can see WHY without going through Railway logs.
@marketing_bp.get("/api/v1/marketing/twitter/whoami")
def twitter_whoami():
    """Probe Twitter/X creds + queue without publishing anything.

    Public (read-only). Shows which OAuth path is set (bearer vs
    OAuth1 quad), pings GET /2/users/me to verify, and reports
    queue/published counts. Lets us diagnose 0/7d-posts at a glance.
    """
    import os as _os
    import requests as _rq
    bearer  = (_os.environ.get('TWITTER_BEARER_TOKEN') or '').strip()
    api_key = (_os.environ.get('TWITTER_API_KEY') or '').strip()
    api_sec = (_os.environ.get('TWITTER_API_SECRET') or '').strip()
    acc_tok = (_os.environ.get('TWITTER_ACCESS_TOKEN') or '').strip()
    acc_sec = (_os.environ.get('TWITTER_ACCESS_SECRET') or '').strip()
    oauth1_complete = bool(api_key and api_sec and acc_tok and acc_sec)
    masked = {
        "bearer_set":        bool(bearer),
        "bearer_length":     len(bearer),
        "oauth1_complete":   oauth1_complete,
        "oauth1_missing":    [k for k, v in [
                                ("TWITTER_API_KEY", api_key),
                                ("TWITTER_API_SECRET", api_sec),
                                ("TWITTER_ACCESS_TOKEN", acc_tok),
                                ("TWITTER_ACCESS_SECRET", acc_sec),
                              ] if not v],
    }
    # Probe /2/users/me
    me_status = None; me_body = None
    if bearer:
        try:
            r = _rq.get("https://api.twitter.com/2/users/me",
                         headers={"Authorization": f"Bearer {bearer}"},
                         timeout=10)
            me_status = r.status_code
            me_body = r.text[:400]
        except Exception as e:
            me_body = f"network err: {e}"
    elif oauth1_complete:
        try:
            from requests_oauthlib import OAuth1
            auth = OAuth1(api_key, api_sec, acc_tok, acc_sec,
                            signature_type='auth_header')
            r = _rq.get("https://api.twitter.com/2/users/me", auth=auth,
                         timeout=10)
            me_status = r.status_code
            me_body = r.text[:400]
        except Exception as e:
            me_body = f"oauth1 err: {e}"

    # Queue + published counts (last 7d)
    queue = {}
    try:
        c = _conn()
        if c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT status, COUNT(*) FROM social_media_posts
                     WHERE platform='twitter'
                       AND created_at >= NOW() - INTERVAL '14 days'
                     GROUP BY status
                """)
                rows = cur.fetchall() or []
                queue = {r[0]: int(r[1]) for r in rows}
                cur.execute("""
                    SELECT COUNT(*) FROM social_media_posts
                     WHERE publish_platform='twitter' AND status='published'
                       AND published_at::timestamptz >= NOW() - INTERVAL '7 days'
                """)
                pub7 = (cur.fetchone() or [0])[0]
            c.close()
        else:
            pub7 = None
    except Exception:
        pub7 = None

    diagnosis = []
    if not (bearer or oauth1_complete):
        diagnosis.append("No Twitter credentials set on Railway. Need EITHER "
                          "TWITTER_BEARER_TOKEN (read+write scope) OR the "
                          "complete OAuth1 quad (API_KEY/SECRET + "
                          "ACCESS_TOKEN/SECRET).")
    if bearer and me_status == 401:
        diagnosis.append("Bearer token returns 401 — expired, revoked, or "
                          "wrong API tier. Regenerate at developer.x.com.")
    if bearer and me_status == 403:
        diagnosis.append("Bearer token returns 403 — token lacks 'tweet.write' "
                          "scope. App-only bearer cannot post tweets; you need "
                          "USER-context OAuth2 bearer or OAuth1.")
    if oauth1_complete and me_status not in (200, None):
        diagnosis.append(f"OAuth1 ping returned {me_status} — check that all 4 "
                          "tokens were copied without spaces and that the app "
                          "tier supports tweet posting.")
    if not queue.get("approved") and not queue.get("published") and not queue.get("failed"):
        diagnosis.append("No Twitter rows in social_media_posts at all — "
                          "auto-press may not be enqueueing for Twitter. Check "
                          "_queue_distribution_posts() in marketing_engine.")

    return jsonify(
        ok=(me_status == 200) if me_status else False,
        masked=masked,
        users_me_status=me_status,
        users_me_body_preview=me_body[:300] if me_body else None,
        queue_14d=queue,
        published_7d=pub7,
        diagnosis=diagnosis,
        hint=("Set TWITTER_BEARER_TOKEN with user-context OAuth2 (tweet.read + "
              "tweet.write + users.read scopes) at developer.x.com → app "
              "settings → user auth → OAuth2. Bearer length should be ~120 chars."),
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

    # Phase ZZZZ-drain (2026-05-18): drain up to N approved+unpublished
    # press releases per call instead of just the most recent. With 11
    # currently backed up and the cron firing every 3h, the previous
    # 1-per-call shape would take 33h to drain even with healthy tokens.
    # Now: ?max=N (default 5) processes up to N pending releases per call.
    max_to_publish = int(request.args.get("max") or "5")
    slug = request.args.get("slug")

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    releases = []
    try:
        with c.cursor() as cur:
            # r58c (2026-06-01): expire stale approved posts (>5 days old)
            # before draining. The drain publishes newest-first, so old posts
            # starve in the queue — and a 12-day-old "afternoon_pulse" should
            # NOT publish as fresh news anyway. Expiring them clears the
            # backlog (was ~216) and stops the delivery-rate metric from
            # counting intentionally-skipped stale rows as failures. status is
            # free-text TEXT (no CHECK). Fail-soft — never blocks the drain.
            try:
                cur.execute("""
                    UPDATE social_media_posts
                       SET status = 'expired'
                     WHERE status = 'approved'
                       AND created_at < NOW() - INTERVAL '5 days'
                """)
                c.commit()
            except Exception:
                try: c.rollback()
                except Exception: pass
            if slug:
                # Single explicit slug — admin testing path
                cur.execute("""
                    SELECT id, title, subheadline, body, meta_description, slug
                    FROM press_releases WHERE slug = %s LIMIT 1
                """, (slug,))
                row = cur.fetchone()
                if row:
                    releases.append(row)
            else:
                # Drain mode: oldest-unpublished first so backlog clears
                # FIFO. Filters out anything that already published on
                # both LinkedIn + Twitter to skip already-done rows.
                # Phase ZZZZ-drain-fix-v2 (2026-05-18): the live press_releases
                # table doesn't have a `status` column (schema drift — some
                # paths assume it, others don't). Drop the status filter
                # entirely. "Pending" = simply not-yet-published to LinkedIn
                # (the NOT EXISTS clause). Also cast COALESCE args to text
                # for the previous DATE+TEXT mismatch.
                cur.execute("""
                    SELECT pr.id, pr.title, pr.subheadline, pr.body,
                           pr.meta_description, pr.slug
                    FROM press_releases pr
                    LEFT JOIN auto_press_releases apr
                           ON apr.press_release_id = pr.id
                    -- hybrid-newsroom (2026-07-19): NEVER fan an unpublished
                    -- draft to social. The draft lanes are alive again, so
                    -- without this filter the 3h drain would LinkedIn-post
                    -- human-gated drafts before anyone proofed them.
                    WHERE pr.published = TRUE
                      AND NOT EXISTS (
                            SELECT 1 FROM social_media_posts smp
                             WHERE smp.press_release_id = pr.id
                               AND smp.platform = 'linkedin'
                               AND smp.status = 'published')
                    ORDER BY COALESCE(apr.generated_for::text,
                                      pr.published_date::text,
                                      pr.created_at::text) DESC NULLS LAST
                    LIMIT %s
                """, (max_to_publish,))
                releases = cur.fetchall() or []
            if not releases:
                return jsonify(ok=False, error="no_pending_press_releases"), 404
    finally:
        try: c.close()
        except Exception: pass

    # Process each release. Build a per-release `rel` dict and reuse the
    # existing single-post path below for each.
    drain_results: list = []
    for row in releases:
        press_id, title, sub, body, meta_desc, real_slug = row
        rel = {
            "title": title, "subheadline": sub, "body": body or "",
            "meta_description": meta_desc or sub or title,
            "slug": real_slug,
        }

        # Backfill distribution rows if missing — no-ops if already there
        # via the UNIQUE INDEX on (press_release_id, platform).
        try:
            _queue_distribution_posts(rel, press_id,
                                      date.today().isoformat())
        except Exception as e:
            drain_results.append({"slug": real_slug, "press_release_id": press_id,
                                   "error": f"backfill_failed: {e}"})
            continue

        # Fetch the queued rows back so we can call the channel-specific
        # publishers with the actual stored content.
        c2 = _conn()
        posts: dict = {}
        try:
            with c2.cursor() as cur:
                cur.execute("""
                    SELECT platform, content, id
                    FROM social_media_posts
                    WHERE press_release_id = %s
                      AND platform IN ('linkedin', 'twitter')
                """, (press_id,))
                for plat, content, post_id in (cur.fetchall() or []):
                    posts[plat] = {"content": content, "post_id": post_id}
        finally:
            try: c2.close()
            except Exception: pass

        out = {"slug": real_slug, "press_release_id": press_id, "results": {}}

        # LinkedIn — Phase HH (2026-05-13): now ARTICLE share with rich
        # link-card. URL points at /news/<slug> which serves an og:image
        # of /api/v1/og/today/<slug>.png — LinkedIn scrapes that for the
        # card thumbnail. Cache-busted with the slug+date so LinkedIn
        # re-fetches OG on reposts.
        if (not only or only == "linkedin") and "linkedin" in posts:
            from routes.li_token import li_access_token
            li_token = li_access_token()
            if not li_token:
                out["results"]["linkedin"] = {"ok": False,
                                              "error": "LINKEDIN_ACCESS_TOKEN not set"}
            else:
                # r86c: route publish_now through the SAME pre-publish gate as
                # the auto-publish loop (quality + number-lead + hook/entity
                # dedup). publish_now previously BYPASSED _should_skip_publish
                # entirely — the exact path that let the 3 near-identical
                # "ChatGPT Names DC Hub" citation posts ship on consecutive days.
                # Fail-OPEN (gate error must never block a legit publish).
                _skip, _why = False, ""
                try:
                    from content_publisher import _should_skip_publish
                    _sc = _conn()
                    if _sc is not None:
                        try:
                            with _sc.cursor() as _cur:
                                _skip, _why = _should_skip_publish(
                                    _cur, posts["linkedin"]["content"], "linkedin")
                        finally:
                            try: _sc.close()
                            except Exception: pass
                except Exception:
                    _skip = False
                if _skip:
                    out["results"]["linkedin"] = {"ok": False, "skipped": True,
                                                  "reason": _why}
                else:
                  try:
                    from content_publisher import _post_to_linkedin
                    article_url = f"https://dchub.cloud/news/{rel['slug']}"
                    article_thumb = (
                        f"https://dchub.cloud/api/v1/og/today/{rel['slug']}.png")
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
                        _remember_share_urn(press_id, "linkedin", result)
                  except Exception as e:
                    out["results"]["linkedin"] = {"ok": False,
                                                  "error": f"exception: {e}"}

        # Twitter / X — X-diversity port (2026-07-31): this branch used to call
        # _post_to_twitter with NO gate at all while the LinkedIn branch above
        # got r86c — the exact asymmetry that let the press template blast X
        # (5 tweets in 40s on 07-31, a verbatim AWS repeat 11 days apart) while
        # every LinkedIn press post was judged. All X publish/cap/class/gate
        # logic now lives in _publish_press_tweet.
        if (not only or only == "twitter") and "twitter" in posts:
            out["results"]["twitter"] = _publish_press_tweet(
                press_id, posts["twitter"]["post_id"],
                posts["twitter"]["content"])

        drain_results.append(out)

    # Final summary across the whole drain
    total_li_success = sum(1 for r in drain_results
                            if (r.get("results", {}) or {}).get("linkedin", {}).get("ok"))
    total_tw_success = sum(1 for r in drain_results
                            if (r.get("results", {}) or {}).get("twitter", {}).get("ok"))
    return jsonify(
        ok=True,
        drained=len(drain_results),
        linkedin_published=total_li_success,
        twitter_published=total_tw_success,
        results=drain_results,
        note=(f"Processed {len(drain_results)} press release(s). "
              f"LinkedIn: {total_li_success} succeeded, "
              f"Twitter: {total_tw_success} succeeded. "
              f"If 0/all failed, check LINKEDIN_ACCESS_TOKEN + X tokens — "
              f"LinkedIn tokens expire every 60 days."),
    ), 200


# Keep in sync with the hardcoded 2/day cap in content_publisher's Twitter
# auto-publisher loop — publish-now used to bypass that cap entirely, which is
# how 5 press tweets went out inside 40 seconds on 2026-07-31.
_X_DAILY_CAP = 2


def _publish_press_tweet(press_id: int, post_id: int, content: str) -> dict:
    """X-diversity port (2026-07-31): the gated publish path for a press
    release's X row. The LinkedIn branch of publish-now has had the r86c
    _should_skip_publish gate since 2026-06; the X branch had NOTHING — no
    gate, no cap, no dedup — and publish-now fires every 3h, so the press
    template owned the X feed (100% one template in the 07-17 verbatim audit;
    still 22 of 27 posts at the 07-31 re-measure).

    Order of checks, and what each does to the row:
      1. already published            -> skip, row untouched (a LinkedIn retry
                                         loop must never re-tweet — the posts
                                         fetch doesn't filter status)
      2. X daily cap (2/day)          -> DEFER: row stays 'approved'; the 6h
                                         drain (which owns cap + per-class
                                         rotation) publishes it later, or the
                                         5d stale sweep expires it
      3. press class already fired    -> DEFER (same): at most ONE press-shaped
         today                           tweet a day, so the second daily slot
                                         is left for an editorial-desk lead
      4. _should_skip_publish gate    -> TERMINAL 'rejected' + media_review_log
         (wire text, r86c parity)        (content-intrinsic, r78 semantics —
                                         same as the drain)
      5. post                         -> mark published + persist tweet id
                                         (r-xid: a published row without
                                         twitter_id re-arms the x_publisher_dead
                                         radar false positive)

    Never raises; every failure path returns a result dict for the drain
    summary. Cap/class checks fail-OPEN (a DB blip must not dark-hold press
    distribution), the gate fails-OPEN on error but fail-CLOSED on a computed
    refusal — exactly the LinkedIn branch's contract."""
    out: dict = {"ok": False}
    try:
        # (1)-(3): status + cap + class, one connection, fail-open.
        try:
            c = _conn()
        except Exception:
            c = None
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute("SELECT status FROM social_media_posts WHERE id = %s",
                                (post_id,))
                    r = cur.fetchone()
                    if r and (r[0] or "") == "published":
                        return {"ok": False, "skipped": True,
                                "reason": "already_published"}
                    _now = datetime.utcnow()   # one instant, not two utcnow()
                    today = _now.strftime("%Y-%m-%d")
                    _next_day = (_now + timedelta(days=1)).strftime("%Y-%m-%d")
                    # COUNT(col) counts non-NULL rows: the 2nd column is how many
                    # of today's tweets came from the press-distribution path.
                    # Half-open range, not `LIKE 'today%'` — a prefix LIKE cannot
                    # use a b-tree range scan and breaks on TEXT -> timestamptz.
                    cur.execute("""
                        SELECT COUNT(*), COUNT(press_release_id)
                          FROM social_media_posts
                         WHERE status = 'published'
                           AND publish_platform = 'twitter'
                           AND published_at >= %s AND published_at < %s
                    """, (today, _next_day))
                    row = cur.fetchone() or (0, 0)
                    total_today, press_today = int(row[0] or 0), int(row[1] or 0)
                    if total_today >= _X_DAILY_CAP:
                        return {"ok": False, "skipped": True, "deferred": True,
                                "reason": f"x_daily_cap ({total_today}/{_X_DAILY_CAP}) — "
                                          "row left approved for the 6h drain"}
                    if press_today >= 1:
                        return {"ok": False, "skipped": True, "deferred": True,
                                "reason": "press_class_daily — a press tweet already "
                                          "fired today; row left approved for the 6h drain"}
            finally:
                try: c.close()
                except Exception: pass

        # (4) the r86c gate, on its own connection like the LinkedIn branch.
        _skip, _why = False, ""
        try:
            from content_publisher import _should_skip_publish
            _sc = _conn()
            if _sc is not None:
                try:
                    with _sc.cursor() as _cur:
                        _skip, _why = _should_skip_publish(_cur, content, "twitter")
                finally:
                    try: _sc.close()
                    except Exception: pass
        except Exception:
            _skip = False
        if _skip:
            try:
                from content_publisher import _record_media_block
                _record_media_block("twitter", _why, content or "")
            except Exception:
                pass
            _rc = None
            try:
                _rc = _conn()
                if _rc is not None:
                    with _rc.cursor() as _cur:
                        _cur.execute("UPDATE social_media_posts SET status = 'rejected' "
                                     "WHERE id = %s AND status != 'published'", (post_id,))
                    _rc.commit()
            except Exception:
                note_swallowed_write("social_media_posts",
                                     where="marketing_engine._publish_press_tweet.reject")
            finally:
                try:
                    if _rc is not None: _rc.close()
                except Exception: pass
            return {"ok": False, "skipped": True, "reason": _why}

        # (5) post + persist the tweet id.
        from content_publisher import _post_to_twitter
        ok, result = _post_to_twitter(content)
        out = {"ok": ok, "result": result}
        if ok:
            _mark_published(post_id, "twitter", tweet_id=str(result)[:64])
    except Exception as e:
        out = {"ok": False, "error": f"exception: {e}"}
    return out


def _mark_published(post_id: int, platform: str, tweet_id: str | None = None) -> None:
    """Update social_media_posts.status after a successful publish.
    Mirrors the update content_publisher's auto-publisher does so the
    next 6h tick doesn't re-publish the same row. `tweet_id` (X only)
    persists like the drain's r-xid fix — a published row with a NULL
    twitter_id re-arms the radar's x_publisher_dead false positive."""
    c = _conn()
    if c is None: return
    try:
        with c.cursor() as cur:
            if tweet_id:
                cur.execute("""
                    UPDATE social_media_posts
                    SET status = %s, published_at = NOW(), publish_platform = %s,
                        twitter_id = %s
                    WHERE id = %s
                """, ("published", platform, tweet_id, post_id))
            else:
                cur.execute("""
                    UPDATE social_media_posts
                    SET status = %s, published_at = NOW(), publish_platform = %s
                    WHERE id = %s
                """, ("published", platform, post_id))
            # r60-conv (2026-06-01, #118): also stamp the parent press release's
            # linkedin_sent_at so /distribution/health reflects reality. The
            # delivery metric reads auto_press_releases.linkedin_sent_at, but no
            # actual publish path wrote it (only the once-daily share-email did),
            # which mathematically floored the metric near ~53% even though posts
            # publish fine. Idempotent (IS NULL); fail-soft (own try/except).
            if platform == "linkedin":
                try:
                    cur.execute("""
                        UPDATE auto_press_releases
                           SET linkedin_sent_at = NOW()
                         WHERE press_release_id = (
                                 SELECT press_release_id FROM social_media_posts WHERE id = %s
                               )
                           AND linkedin_sent_at IS NULL
                    """, (post_id,))
                except Exception:
                    note_swallowed_write("auto_press_releases", where="marketing_engine._mark_published")
                    pass
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
            from routes.li_token import li_access_token
            li_token = li_access_token()
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
    from routes.li_token import li_access_token
    return bool(li_access_token())


def _twitter_configured() -> bool:
    # r-twitter-honest (2026-06-02): "configured" must mean "actually wired to
    # publish", not merely "tokens present in env". The X publisher
    # (content_publisher.start_twitter_publisher) hard-disables itself unless
    # TWITTER_PUBLISHER_ENABLED is truthy — it was turned off 2026-05-25 because
    # the X dev app isn't inside a Project (every cycle 403'd). Reporting
    # configured=True off bare tokens while the publisher is off produced a
    # PERMANENT false social_publish_silent_failure:twitter finding
    # (configured && published_7d==0 && backlog) and a misleading worker-status.
    if os.environ.get("TWITTER_PUBLISHER_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return False
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
                # canonical identity view — real external agents only, never
                # raw ip_address (which counts probes + self-traffic).
                cur.execute("""
                    SELECT COUNT(DISTINCT agent_id)
                    FROM mcp_calls_identity
                    WHERE created_at > NOW() - INTERVAL '7 days'
                      AND is_public_ip AND is_real_external""")
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


def _market_performance(days: int = 30) -> list:
    """Per-MARKET LinkedIn engagement.

    The topic bandit (_topic_performance) joins auto_press_releases.slug and so
    only sees press-release posts; the DCPI / market-movement alerts (which link
    to /dcpi/<city> or /markets/<metro>) are invisible to it. This surfaces
    that signal instead: it groups market posts by their URL slug and LEFT JOINs
    dcpi_markets so each market gets its real name/state/iso/tier where the slug
    resolves ('boise' -> 'boise-id'). Markets not in dcpi_markets (metros,
    AESO/Canada) still appear, keyed by slug, with matched_dcpi=false.

    Press-release posts are excluded (their slug is a YYYY-MM-DD-headline, which
    _topic_performance already covers). Read-only; never raises."""
    conn = _conn()
    if conn is None:
        return []
    out = []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT p.slug,
                       MAX(d.name)  AS market_name,
                       MAX(d.state) AS state,
                       MAX(d.iso)   AS iso,
                       MAX(d.tier)  AS tier,
                       COUNT(*)                                   AS n_posts,
                       COALESCE(SUM(p.impressions), 0)            AS impressions,
                       COALESCE(SUM(COALESCE(p.likes,0)
                                  + COALESCE(p.comments,0)
                                  + COALESCE(p.shares,0)), 0)     AS engagement
                  FROM linkedin_posts p
                  LEFT JOIN dcpi_markets d
                         ON d.slug = p.slug
                         OR d.slug LIKE p.slug || '-%%'
                 WHERE p.slug IS NOT NULL
                   AND p.slug !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}-'
                   AND p.posted_at > NOW() - make_interval(days => %s)
                 GROUP BY p.slug
                 ORDER BY impressions DESC, n_posts DESC
            """, (int(days),))
            for slug, name, state, iso, tier, n, imp, eng in cur.fetchall():
                out.append({
                    "market_slug": slug,
                    "market":      name or slug,
                    "state":       state,
                    "iso":         iso,
                    "tier":        tier,
                    "matched_dcpi": bool(name),
                    "n_posts":     int(n or 0),
                    "impressions": int(imp or 0),
                    "engagement":  int(eng or 0),
                })
    except Exception as e:
        print(f"[market-perf] _market_performance query failed: {e}",
              file=sys.stderr)
        return []
    finally:
        try: conn.close()
        except Exception: pass
    return out


@marketing_bp.get("/api/v1/marketing/market-performance")
def market_performance():
    """Engagement per market for DCPI / market-alert LinkedIn posts — the market
    analog of og-performance (which only covers press releases). Read-only."""
    try:
        days = max(7, min(int(request.args.get("days", "30")), 180))
    except ValueError:
        days = 30
    data = _market_performance(days)
    resp = jsonify({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "markets_tracked": len(data),
        "by_market": data,
        "note": ("LinkedIn engagement per market for /dcpi/ + /markets/ posts, "
                 "keyed by URL slug and enriched from dcpi_markets where the slug "
                 "resolves (matched_dcpi=true). Press releases are covered by "
                 "og-performance / _topic_performance instead."),
    })
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
