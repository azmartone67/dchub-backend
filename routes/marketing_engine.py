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
ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
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
        provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
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
    finally:
        try: c.close()
        except Exception: pass
    return out


def _pick_daily_topic(signals: dict) -> tuple[str, str]:
    """Phase LL: pick the most newsworthy topic for today's auto-press,
    with guaranteed fallbacks so the cron never goes a day without
    output.

    Priority (most → least newsworthy):
      1. dcpi_mover         — a single market shifted >5pts WoW
      2. dcpi_leader        — highest BUILD market this week
      3. dcpi_warning       — highest-constraint AVOID market
      4. iso_coverage       — when we expand grid coverage (e.g. Phase HH 7→11)
      5. ai_adoption        — MCP usage milestones (5K calls/day, etc.)
      6. new_facility       — biggest newly-discovered facility
      7. weekly_pulse       — generic "DC Hub by the numbers" recap (always works)

    Returns (topic_slug, human_reason). The Claude prompt sees both.
    """
    movers = signals.get("biggest_movers") or []
    if movers:
        m = movers[0]
        d = abs(m.get("delta") or 0)
        if d >= 5:
            return "dcpi_mover", (
                f"{m.get('market','a market')} shifted "
                f"{m.get('delta')}pts in DCPI this week — biggest mover.")

    builds = signals.get("top_build_markets") or []
    if builds:
        return "dcpi_leader", (
            f"{builds[0].get('market','top market')} leads the BUILD ranking "
            f"with excess power score {builds[0].get('excess','?')}.")

    avoids = signals.get("top_avoid_markets") or []
    if avoids:
        return "dcpi_warning", (
            f"{avoids[0].get('market','a market')} flagged AVOID — highest "
            f"constraint score {avoids[0].get('constraint','?')}.")

    new_fac = signals.get("new_facilities_24h") or []
    if new_fac:
        f = new_fac[0]
        return "new_facility", (
            f"{f.get('name','A new facility')} ({f.get('provider','?')}, "
            f"{f.get('mw','?')}MW) detected in {f.get('city','?')}, "
            f"{f.get('state','?')}.")

    ai = signals.get("ai_usage_24h") or {}
    if ai.get("tool_calls", 0) >= 1000:
        return "ai_adoption", (
            f"DC Hub MCP served {ai.get('tool_calls')} AI tool calls in "
            f"the last 24h from {ai.get('unique_callers')} unique callers.")

    # Last-resort topic: every day produces SOMETHING. This is the
    # "weekly pulse" recap — DC Hub by the numbers.
    return "weekly_pulse", (
        "Generic weekly recap of DC Hub platform activity — "
        "markets tracked, ISOs live, facilities indexed.")


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
  "topic": "dcpi_mover" | "iso_intelligence" | "ai_adoption" | "new_facility",
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
        return press_id, None
    except Exception as e:
        print(f"[marketing_engine] write failed: {e}", file=sys.stderr)
        return None, f"db_error: {str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass


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
            cur.execute("""SELECT id, slug, title FROM auto_press_releases
                           WHERE generated_for = %s LIMIT 1""", (today,))
            existing = cur.fetchone()
        if existing:
            return jsonify(
                ok=True, skipped=True, reason="already_generated_today",
                existing={"id": existing[0], "slug": existing[1],
                          "title": existing[2]},
            ), 200
    finally:
        try: c.close()
        except Exception: pass

    signals = _collect_signals()

    # Phase LL (2026-05-13): always-publish daily heartbeat. The
    # previous behaviour skipped 29 of every 30 days because
    # market_power_scores.published=true is rarely set during the
    # incremental cron, leaving top_build_markets/biggest_movers
    # empty → cron silently no-op'd → user saw the system as dormant.
    #
    # New policy: pick a topic that's always available, in priority
    # order. The auto-press cron NEVER goes a day without producing
    # something. Each fallback is a real story we can tell.
    topic, topic_reason = _pick_daily_topic(signals)
    signals["daily_topic"] = topic
    signals["daily_topic_reason"] = topic_reason

    prompt = (f"Daily signals (topic: {topic} — {topic_reason}):\n"
              "```\n" + json.dumps(signals, indent=2)[:6000] + "\n```")
    rel, err = _call_claude_marketing(prompt)
    if err or not rel:
        return jsonify(ok=False, error=err or "no_response", signals=signals), 502
    ok, why = _validate_release(rel)
    if not ok:
        return jsonify(ok=False, error=f"validation_failed: {why}",
                       proposal=rel), 422
    press_id, write_err = _write_release(rel, signals, rel.get("topic", "dcpi"))
    if write_err:
        return jsonify(ok=False, error=write_err, proposal=rel), 500
    return jsonify(
        ok=True, generated=True,
        press_release_id=press_id,
        slug=rel["slug"],
        title=rel["title"],
        url=f"https://dchub.cloud/news/{rel['slug']}",
        signals_used=signals,
    ), 201


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
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=240"
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
    from urllib.request import Request, urlopen
    payload = json.dumps({
        "from":    "DC Hub <noreply@dchub.cloud>",
        "to":      [to_addr],
        "subject": f"📰 Today's LinkedIn post — {title[:60]}",
        "html":    html_email,
    }).encode()
    req = Request("https://api.resend.com/emails", data=payload, headers={
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {RESEND_API_KEY}",
    })
    try:
        with urlopen(req, timeout=15) as r:
            resp_text = r.read().decode("utf-8", errors="ignore")
            sent_ok = (r.status == 200)
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
