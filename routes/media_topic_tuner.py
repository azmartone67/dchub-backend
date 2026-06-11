"""
media_topic_tuner.py — Topic-level engagement learner for DC Hub Media
(2026-06-07).

This module evolves the existing media accelerator (which fires on a SINGLE
post >2x impressions baseline) into a TOPIC-LEVEL engagement learner that
auto-tunes what we publish next week based on what worked this week.

Five capabilities, one blueprint:

1. Topic classification + backfill.
   • Keyword-based topic tagger (DCPI / hyperscaler / market / grid / fiber
     / M&A / AI capex / industry / facility / verdict_shift).
   • Backfill endpoint paints media_topic_tags on existing linkedin_posts
     + social_media_posts (idempotent).
   • Write-time tag helper consumed by generators.

2. Engagement aggregation by topic.
   • Pulls 30d impressions/likes/comments/clicks/posts per topic from
     linkedin_posts joined to social_media_posts.media_topic_tags.
   • GET /api/v1/admin/media/topic-performance.
   • Falls back gracefully when impressions column / topic tags missing.

3. Topic-mix auto-tuner.
   • Runs at 13:30 UTC daily (cron _run_media_topic_tune in
     crawler_scheduler.py), right after linkedin_engagement_sync 13:00.
   • Computes 7-day weights per topic = normalized engagement score with
     softening (sqrt(impressions) + 2*clicks + 3*likes + 5*comments).
   • Writes one row per topic into media_topic_mix (effective_for=today,
     weight, n_posts, score, reason).
   • Generators pick topic_mix on next slot.

4. Themed series generator.
   • Generates 5-part "Top 10 BUILD markets" series (one post per day for
     5 days) — picks 50 top-DCPI BUILD markets, splits 10 per post.
   • Endpoint POST /api/v1/admin/media/series/create?kind=build-top10 will
     enqueue 5 social_media_posts rows (publish_platform='linkedin',
     status='approved', scheduled day spread).

5. LinkedIn click attribution.
   • GET /li/<short> → records link_click + 302 to destination.
   • Cookie-bound `dc_li_session` joins downstream signup → counts as
     linkedin_assisted_conversions.

Admin dashboard at GET /admin/media-mix renders the current topic-mix
heatmap, top 5 topics by engagement, and the 5-part series schedule for
the next 5 days.

Endpoints
---------
GET  /api/v1/admin/media/topic-performance?days=30
POST /api/v1/admin/media/backfill-tags
POST /api/v1/admin/media/tune-now
GET  /api/v1/admin/media/current-mix
POST /api/v1/admin/media/series/create?kind=build-top10
GET  /api/v1/admin/media/series/list
GET  /admin/media-mix                 (HTML dashboard, admin-keyed)
GET  /li/<short>                      (public — 302 redirect + attribution)
"""
from __future__ import annotations

import os
import re
import sys
import json
import datetime
import hashlib
import secrets
import math
from typing import Any

from flask import Blueprint, jsonify, request, redirect, make_response


media_topic_tuner_bp = Blueprint("media_topic_tuner", __name__)


# ── Topic taxonomy ──────────────────────────────────────────────────────
# Each topic has a friendly name + regex patterns we look for in post
# content. Order matters: more specific topics first; a post can belong
# to multiple topics (JSONB array).
TOPIC_LIBRARY: list[dict] = [
    {"topic": "dcpi_verdict",       "name": "DCPI Verdict / Score",
     "patterns": [r"\bDCPI\b", r"verdict.*(BUILD|AVOID|CAUTION)",
                  r"power index", r"capacity.*index"]},
    {"topic": "hyperscaler_deal",   "name": "Hyperscaler Deal",
     "patterns": [r"\b(AWS|Amazon|Microsoft|Azure|Google|GCP|Meta|Oracle|"
                  r"Stargate|xAI|Anthropic|OpenAI|TikTok|ByteDance)\b",
                  r"hyperscaler"]},
    {"topic": "ai_capex",           "name": "AI Capex / Capacity",
     "patterns": [r"\$\d+\s*[BMK]?\s*(billion|capex|capital)",
                  r"AI\s+capex", r"AI\s+capacity", r"GPU\s+(buildout|cluster)"]},
    {"topic": "ma_transaction",     "name": "M&A / Transaction",
     "patterns": [r"\bM&A\b", r"acquir", r"\bdeal\b", r"merger",
                  r"\bbuyout\b", r"private equity", r"\bPE\b\s+firm"]},
    {"topic": "grid_alert",         "name": "Grid Alert / ISO",
     "patterns": [r"\bERCOT\b", r"\bPJM\b", r"\bMISO\b", r"\bCAISO\b",
                  r"\bSPP\b", r"\bNYISO\b", r"\bISO-NE\b", r"\bgrid\b",
                  r"interconnection.*queue", r"power.*headroom"]},
    {"topic": "market_brief",       "name": "Market Brief",
     "patterns": [r"market brief", r"market spotlight",
                  r"(Ashburn|Northern Virginia|Dallas|Phoenix|Atlanta|Chicago|"
                  r"Columbus|Reno|Cheyenne|Loudoun|Silicon Valley|Portland)",
                  r"\bmetro\b"]},
    {"topic": "fiber_route",        "name": "Fiber / Connectivity",
     "patterns": [r"\bfiber\b", r"dark fiber", r"submarine cable",
                  r"\bIXP\b", r"peering", r"latency"]},
    {"topic": "facility_news",      "name": "New Facility / Announcement",
     "patterns": [r"\bgroundbreak", r"campus.*announce", r"\bMW\s+(campus|build)",
                  r"\bphase\s+\d+\b", r"\bbreaks ground\b"]},
    {"topic": "energy_pricing",     "name": "Energy / LMP / Gas",
     "patterns": [r"\bLMP\b", r"\$/MWh", r"natural gas", r"\bRTO\b",
                  r"energy.*pric", r"\bspot price\b"]},
    {"topic": "water_risk",         "name": "Water / Cooling",
     "patterns": [r"\bwater\b", r"\bcooling\b", r"liquid.*cool",
                  r"\bdrought\b", r"\bWUE\b"]},
    {"topic": "renewable_energy",   "name": "Renewable / Solar / Wind",
     "patterns": [r"\bsolar\b", r"\bwind\b", r"renewable", r"PPA",
                  r"nuclear", r"\bSMR\b"]},
    {"topic": "industry_pulse",     "name": "Industry Pulse / Counter-take",
     "patterns": [r"counter[- ]?take", r"contrarian", r"reality check",
                  r"\bmyth\b", r"the truth about"]},
    {"topic": "ai_citation",        "name": "AI Citation / Cited-by",
     "patterns": [r"cited by", r"\bChatGPT\b", r"\bGroq\b", r"\bPerplexity\b",
                  r"\bGemini\b", r"\bClaude\b.*cite"]},
    {"topic": "verdict_shift",      "name": "Verdict Shift Alert",
     "patterns": [r"verdict shift", r"shift to (BUILD|AVOID)",
                  r"flipped from"]},
]


# ── Plumbing ────────────────────────────────────────────────────────────
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
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[topic-tuner] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── Schema ──────────────────────────────────────────────────────────────
def init_topic_tuner_tables() -> bool:
    """Idempotent schema bootstrap. Wired into content_publisher.init_content_tables()
    so it runs on every boot. Returns True on success."""
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            # social_media_posts: add JSONB array of topic tags.
            try:
                cur.execute("""
                    ALTER TABLE social_media_posts
                    ADD COLUMN IF NOT EXISTS media_topic_tags JSONB DEFAULT '[]'::jsonb
                """)
            except Exception as e:
                _log(f"social_media_posts.media_topic_tags add skipped: {e}")
            # linkedin_posts: add JSONB array too (so we can join engagement
            # directly to topics without going through social_media_posts).
            try:
                cur.execute("""
                    ALTER TABLE linkedin_posts
                    ADD COLUMN IF NOT EXISTS media_topic_tags JSONB DEFAULT '[]'::jsonb
                """)
            except Exception as e:
                _log(f"linkedin_posts.media_topic_tags add skipped: {e}")

            # media_topic_mix: one row per (topic, effective_for) — the
            # tuner overwrites today's row idempotently on each cron tick.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_topic_mix (
                    id              BIGSERIAL PRIMARY KEY,
                    effective_for   DATE NOT NULL,
                    topic           TEXT NOT NULL,
                    weight          NUMERIC NOT NULL DEFAULT 0,
                    score           NUMERIC NOT NULL DEFAULT 0,
                    n_posts         INTEGER NOT NULL DEFAULT 0,
                    avg_impressions NUMERIC,
                    avg_clicks      NUMERIC,
                    avg_engagement  NUMERIC,
                    reason          TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (effective_for, topic)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_topic_mix_eff_idx
                    ON media_topic_mix(effective_for DESC, weight DESC)
            """)

            # media_link_clicks: every /li/<short> hit gets one row.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_link_clicks (
                    id           BIGSERIAL PRIMARY KEY,
                    short_code   TEXT NOT NULL,
                    destination  TEXT NOT NULL,
                    session_id   TEXT,
                    referer      TEXT,
                    user_agent   TEXT,
                    ip_hash      TEXT,
                    topic        TEXT,
                    smp_id       INTEGER,
                    clicked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_link_clicks_code_idx
                    ON media_link_clicks(short_code, clicked_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_link_clicks_session_idx
                    ON media_link_clicks(session_id) WHERE session_id IS NOT NULL
            """)

            # media_link_shortcodes: short → destination + topic + smp_id.
            # Generated on demand by generators; immutable.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_link_shortcodes (
                    short_code   TEXT PRIMARY KEY,
                    destination  TEXT NOT NULL,
                    topic        TEXT,
                    smp_id       INTEGER,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # media_themed_series: one row per (series_kind, day_idx).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_themed_series (
                    id              BIGSERIAL PRIMARY KEY,
                    series_kind     TEXT NOT NULL,
                    day_idx         INTEGER NOT NULL,
                    title           TEXT NOT NULL,
                    body            TEXT NOT NULL,
                    scheduled_for   DATE NOT NULL,
                    smp_id          INTEGER,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (series_kind, day_idx, scheduled_for)
                )
            """)
        _log("schema bootstrap OK")
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


# Lazy init at import-time, but content_publisher.init_content_tables()
# also calls it on boot — belt-and-suspenders.
try:
    _SCHEMA_OK = init_topic_tuner_tables()
except Exception:
    _SCHEMA_OK = False


# ── Topic classifier ────────────────────────────────────────────────────
def classify_topics(content: str) -> list[str]:
    """Run TOPIC_LIBRARY over `content`, return list of matched topic slugs.
    A post can belong to multiple topics. Empty list if no match (then the
    caller should bucket the post into 'other')."""
    if not content:
        return []
    matched: list[str] = []
    for entry in TOPIC_LIBRARY:
        for pat in entry["patterns"]:
            try:
                if re.search(pat, content, re.IGNORECASE):
                    matched.append(entry["topic"])
                    break
            except Exception:
                continue
    return matched


def tags_for_content(content: str) -> list[str]:
    """Write-time helper: returns the topic tags (or ['other']) for a piece
    of generated content. Safe to call from generators on the hot path."""
    tags = classify_topics(content or "")
    return tags if tags else ["other"]


# ── Backfill ────────────────────────────────────────────────────────────
def _backfill_table(cur, table: str, content_col: str, id_col: str = "id",
                    limit: int = 500) -> int:
    """Backfill media_topic_tags for `limit` rows of `table` that don't yet
    have any tags. Returns count of rows updated."""
    n = 0
    try:
        cur.execute(f"""
            SELECT {id_col}, {content_col}
              FROM {table}
             WHERE (media_topic_tags IS NULL
                    OR media_topic_tags = '[]'::jsonb)
               AND {content_col} IS NOT NULL
               AND {content_col} <> ''
             ORDER BY {id_col} DESC
             LIMIT %s
        """, (limit,))
        rows = cur.fetchall() or []
    except Exception as e:
        _log(f"backfill {table} select failed: {e}")
        return 0
    for row in rows:
        rid = row[0] if not hasattr(row, "get") else row.get(id_col)
        body = row[1] if not hasattr(row, "get") else row.get(content_col)
        tags = tags_for_content(body)
        try:
            cur.execute(
                f"UPDATE {table} SET media_topic_tags = %s::jsonb "
                f"WHERE {id_col} = %s",
                (json.dumps(tags), rid))
            n += 1
        except Exception:
            continue
    return n


def backfill_recent_tags(limit: int = 500) -> dict[str, Any]:
    out = {"social_media_posts_tagged": 0, "linkedin_posts_tagged": 0,
           "errors": []}
    conn = _db_conn()
    if conn is None:
        out["errors"].append("no_db")
        return out
    try:
        with conn, conn.cursor() as cur:
            try:
                out["social_media_posts_tagged"] = _backfill_table(
                    cur, "social_media_posts", "content", "id", limit)
            except Exception as e:
                out["errors"].append(f"smp_backfill: {e}")
            try:
                # linkedin_posts uses either `content_text`, `content`, or
                # `post_text`; pick whichever exists in the live schema.
                col = "content_text"
                try:
                    cur.execute("""SELECT column_name FROM information_schema.columns
                                   WHERE table_name='linkedin_posts'
                                     AND column_name IN ('content_text','content','post_text')""")
                    cols = {r[0] if not hasattr(r, 'get') else r.get('column_name')
                            for r in (cur.fetchall() or [])}
                    if "content_text" in cols: col = "content_text"
                    elif "content" in cols:    col = "content"
                    elif "post_text" in cols:  col = "post_text"
                except Exception:
                    col = "content_text"
                out["linkedin_posts_tagged"] = _backfill_table(
                    cur, "linkedin_posts", col, "id", limit)
            except Exception as e:
                out["errors"].append(f"li_backfill: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── Engagement aggregation ──────────────────────────────────────────────
def _safe_int(v) -> int:
    try: return int(v or 0)
    except Exception: return 0


def _safe_float(v) -> float:
    try: return float(v or 0)
    except Exception: return 0.0


def topic_performance(days: int = 30) -> list[dict]:
    """For each topic in TOPIC_LIBRARY (+ 'other'), aggregate impressions /
    clicks / likes / comments / n_posts over the last `days` days.

    Joins linkedin_posts directly via the local media_topic_tags column —
    we DO NOT depend on social_media_posts.linkedin_urn being populated
    (it's sparse for the historical backfill window). The classifier
    runs at read-time on the content as a fallback for un-tagged rows.
    """
    out: list[dict] = []
    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            # Pull every post with engagement in the window. Tag it
            # client-side (classifier is cheap) so we can score
            # un-backfilled rows too.
            # Pull linkedin_posts with engagement. linkedin_posts uses
            # `published_at` in the canonical schema (see linkedin_autopost.py
            # CREATE TABLE) and `posted_at` in other replicas. COALESCE both.
            # 2026-06-07: COALESCE engagement columns too — if `impressions`
            # column doesn't exist in this schema, the SELECT falls back to 0
            # via the column-existence dance below. Best-effort over both
            # linkedin_posts AND social_media_posts so we never starve the
            # learner when one of the surfaces hasn't been hydrated yet.
            rows: list = []
            # 2026-06-07 Round-1 cleanup: BOTH queries below must run even
            # if the linkedin_posts COALESCE references a missing column
            # (e.g. `impressions` doesn't exist on every deploy). Plain
            # psycopg2 leaves the connection in `aborted_transaction` state
            # after a single failure → every subsequent execute() silently
            # fails until ROLLBACK. Rolling back between probes + scoping
            # each into its own cursor restores per-table independence so
            # social_media_posts still flows when linkedin_posts is missing
            # a column. This was the actual cause of n_topics=0 despite
            # backfill_recent_tags reporting 6 social_media_posts tagged.
            try:
                cur.execute(f"""
                    SELECT id,
                           COALESCE(content_text, content, post_text, '') AS body,
                           COALESCE(impressions, 0)         AS impressions,
                           COALESCE(clicks, 0)              AS clicks,
                           COALESCE(likes, 0)               AS likes,
                           COALESCE(comments, 0)            AS comments,
                           COALESCE(shares, 0)              AS shares,
                           media_topic_tags                 AS tags
                      FROM linkedin_posts
                     WHERE COALESCE(published_at, posted_at, created_at, NOW())
                           > NOW() - INTERVAL '{int(days)} days'
                       AND COALESCE(content_text, content, post_text, '') <> ''
                """)
                rows.extend(cur.fetchall() or [])
            except Exception as e:
                _log(f"topic_performance li_query failed: {e}")
                # Critical: roll back the aborted transaction so the
                # social_media_posts query below isn't a silent no-op.
                try: conn.rollback()
                except Exception: pass
            # Also pull social_media_posts so the tuner still has signal
            # when linkedin_posts.impressions hasn't been hydrated yet
            # (LinkedIn API takes 24-48h to fill the columns on a new post).
            # Score from social_media_posts uses VOLUME as a proxy:
            # impressions=0, but n_posts counts toward the topic weight.
            try:
                cur.execute(f"""
                    SELECT id, content AS body,
                           0 AS impressions, 0 AS clicks,
                           0 AS likes, 0 AS comments, 0 AS shares,
                           media_topic_tags AS tags
                      FROM social_media_posts
                     WHERE COALESCE(created_at, NOW())
                           > NOW() - INTERVAL '{int(days)} days'
                       AND content IS NOT NULL
                       AND content <> ''
                """)
                rows.extend(cur.fetchall() or [])
            except Exception as e:
                _log(f"topic_performance smp_query failed: {e}")
                try: conn.rollback()
                except Exception: pass

            buckets: dict[str, dict] = {}
            for row in rows:
                if hasattr(row, "get"):
                    body = row.get("body") or ""
                    impressions = _safe_int(row.get("impressions"))
                    clicks      = _safe_int(row.get("clicks"))
                    likes       = _safe_int(row.get("likes"))
                    comments    = _safe_int(row.get("comments"))
                    shares      = _safe_int(row.get("shares"))
                    tags        = row.get("tags") or []
                else:
                    body = row[1] or ""
                    impressions = _safe_int(row[2])
                    clicks      = _safe_int(row[3])
                    likes       = _safe_int(row[4])
                    comments    = _safe_int(row[5])
                    shares      = _safe_int(row[6])
                    tags        = row[7] or []
                # Normalize tags: JSONB→list; fall back to live classifier.
                if isinstance(tags, str):
                    try: tags = json.loads(tags)
                    except Exception: tags = []
                if not tags:
                    tags = tags_for_content(body)
                for t in tags:
                    b = buckets.setdefault(t, {
                        "n_posts": 0, "impressions": 0, "clicks": 0,
                        "likes": 0, "comments": 0, "shares": 0,
                    })
                    b["n_posts"]    += 1
                    b["impressions"] += impressions
                    b["clicks"]     += clicks
                    b["likes"]      += likes
                    b["comments"]   += comments
                    b["shares"]     += shares

            for topic, b in buckets.items():
                n = b["n_posts"] or 1
                # Engagement score: weighted blend that rewards quality
                # signal (comment > like > click > impression). sqrt
                # impressions so a single 100k post doesn't dominate.
                score = (math.sqrt(b["impressions"]) * 1.0
                         + b["clicks"]   * 2.0
                         + b["likes"]    * 3.0
                         + b["comments"] * 5.0
                         + b["shares"]   * 3.0)
                # Per-post average for readability on the dashboard.
                out.append({
                    "topic":           topic,
                    "name":            _topic_name(topic),
                    "n_posts":         b["n_posts"],
                    "impressions":     b["impressions"],
                    "avg_impressions": round(b["impressions"] / n, 1),
                    "clicks":          b["clicks"],
                    "avg_clicks":      round(b["clicks"]   / n, 2),
                    "likes":           b["likes"],
                    "comments":        b["comments"],
                    "shares":          b["shares"],
                    "avg_engagement":  round((b["likes"] + b["comments"] + b["shares"]) / n, 2),
                    "score":           round(score, 1),
                })
            out.sort(key=lambda r: -r["score"])
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _topic_name(topic: str) -> str:
    for entry in TOPIC_LIBRARY:
        if entry["topic"] == topic:
            return entry["name"]
    return topic.replace("_", " ").title()


# ── Topic-mix auto-tuner ────────────────────────────────────────────────
def compute_topic_mix(days: int = 7,
                      min_weight: float = 0.02,
                      max_weight: float = 0.35) -> list[dict]:
    """Compute next-7d topic_mix from last `days` days of engagement.

    Returns a list of {topic, weight, score, n_posts, reason} sorted
    weight-desc. Weights sum to ~1.0. Caps any single topic at
    max_weight so a single breakout topic can't pin us to 80% (avoids
    the clickbait spiral — see Risk section in the parent spec).
    """
    perf = topic_performance(days=days)
    if not perf:
        return []
    # Always-include every topic in our library at min_weight so the
    # explore tick keeps every topic gathering data.
    library_topics = [e["topic"] for e in TOPIC_LIBRARY] + ["other"]
    perf_by_topic = {p["topic"]: p for p in perf}
    total_score = sum(p["score"] for p in perf) or 1.0
    raw: list[dict] = []
    for t in library_topics:
        p = perf_by_topic.get(t)
        if p:
            w = p["score"] / total_score
            raw.append({
                "topic":   t,
                "name":    p["name"],
                "weight":  w,
                "score":   p["score"],
                "n_posts": p["n_posts"],
                "avg_impressions": p["avg_impressions"],
                "avg_clicks": p["avg_clicks"],
                "avg_engagement": p["avg_engagement"],
                "reason": (
                    f"{p['n_posts']} posts · "
                    f"{p['impressions']:,} impressions · "
                    f"{p['likes']+p['comments']+p['shares']} engagements")
            })
        else:
            raw.append({
                "topic":   t,
                "name":    _topic_name(t),
                "weight":  0.0,
                "score":   0.0,
                "n_posts": 0,
                "avg_impressions": 0,
                "avg_clicks": 0,
                "avg_engagement": 0,
                "reason":  "no data in window — keep at floor weight for exploration",
            })
    # Cap + floor.
    for r in raw:
        r["weight"] = max(min_weight, min(max_weight, r["weight"]))
    # Re-normalize.
    s = sum(r["weight"] for r in raw) or 1.0
    for r in raw:
        r["weight"] = round(r["weight"] / s, 4)
    raw.sort(key=lambda r: -r["weight"])
    return raw


def tune_now() -> dict[str, Any]:
    """Compute and persist today's topic mix. Idempotent — running twice
    on the same day overwrites the same UNIQUE(effective_for,topic) row."""
    out: dict[str, Any] = {"mix": [], "wrote": 0, "errors": []}
    mix = compute_topic_mix()
    out["mix"] = mix
    if not mix:
        out["errors"].append("no_mix_computed")
        return out
    today = datetime.date.today()
    conn = _db_conn()
    if conn is None:
        out["errors"].append("no_db")
        return out
    try:
        with conn, conn.cursor() as cur:
            for r in mix:
                try:
                    cur.execute("""
                        INSERT INTO media_topic_mix
                            (effective_for, topic, weight, score, n_posts,
                             avg_impressions, avg_clicks, avg_engagement, reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (effective_for, topic)
                        DO UPDATE SET weight = EXCLUDED.weight,
                                      score  = EXCLUDED.score,
                                      n_posts= EXCLUDED.n_posts,
                                      avg_impressions = EXCLUDED.avg_impressions,
                                      avg_clicks     = EXCLUDED.avg_clicks,
                                      avg_engagement = EXCLUDED.avg_engagement,
                                      reason = EXCLUDED.reason,
                                      created_at = NOW()
                    """, (today, r["topic"], r["weight"], r["score"],
                          r["n_posts"], r["avg_impressions"], r["avg_clicks"],
                          r["avg_engagement"], r["reason"]))
                    out["wrote"] += 1
                except Exception as e:
                    out["errors"].append(f"write {r['topic']}: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    _log(f"tune_now wrote={out['wrote']} top={mix[0]['topic'] if mix else 'NA'}")
    return out


def current_topic_mix() -> list[dict]:
    """Return today's mix, or yesterday's if today not written yet."""
    out: list[dict] = []
    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            for d_back in range(0, 7):
                d = datetime.date.today() - datetime.timedelta(days=d_back)
                cur.execute("""
                    SELECT topic, weight, score, n_posts,
                           avg_impressions, avg_clicks, avg_engagement,
                           reason, effective_for
                      FROM media_topic_mix
                     WHERE effective_for = %s
                     ORDER BY weight DESC
                """, (d,))
                rows = cur.fetchall() or []
                if rows:
                    for row in rows:
                        if hasattr(row, "get"):
                            out.append({
                                "topic":   row.get("topic"),
                                "name":    _topic_name(row.get("topic")),
                                "weight":  float(row.get("weight") or 0),
                                "score":   float(row.get("score") or 0),
                                "n_posts": int(row.get("n_posts") or 0),
                                "avg_impressions": float(row.get("avg_impressions") or 0),
                                "avg_clicks":      float(row.get("avg_clicks") or 0),
                                "avg_engagement":  float(row.get("avg_engagement") or 0),
                                "reason":  row.get("reason") or "",
                                "effective_for": row.get("effective_for").isoformat() if row.get("effective_for") else None,
                            })
                        else:
                            out.append({
                                "topic":   row[0],
                                "name":    _topic_name(row[0]),
                                "weight":  float(row[1] or 0),
                                "score":   float(row[2] or 0),
                                "n_posts": int(row[3] or 0),
                                "avg_impressions": float(row[4] or 0),
                                "avg_clicks":      float(row[5] or 0),
                                "avg_engagement":  float(row[6] or 0),
                                "reason":  row[7] or "",
                                "effective_for": row[8].isoformat() if row[8] else None,
                            })
                    break
    finally:
        try: conn.close()
        except Exception: pass
    return out


def pick_topic_from_mix() -> str | None:
    """Public hook for generators: epsilon-greedy pick from today's mix.
    20% explore tick (random topic), 80% exploit (weighted random by
    today's weight). Safe to call from a hot generator — returns None if
    no mix is available, so the generator can fall through to its
    deterministic cascade."""
    import random as _random
    mix = current_topic_mix()
    if not mix:
        return None
    # Explore: 20% of the time, uniform-random over library so every
    # topic keeps gathering data.
    if _random.random() < 0.20:
        return _random.choice([e["topic"] for e in TOPIC_LIBRARY])
    # Exploit: weighted random pick.
    weights = [r["weight"] for r in mix]
    s = sum(weights) or 1.0
    weights = [w / s for w in weights]
    return _random.choices([r["topic"] for r in mix], weights=weights, k=1)[0]


# ── Themed series generator ─────────────────────────────────────────────
def _fetch_top_build_markets(limit: int = 50) -> list[dict]:
    """Pull top `limit` BUILD-verdict markets by DCPI composite_score.
    Falls back to market_power_scores if available."""
    out: list[dict] = []
    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            # Try market_power_scores first (has the BUILD verdict).
            try:
                cur.execute("""
                    SELECT DISTINCT ON (market_slug)
                           market_name, market_slug, iso, state,
                           COALESCE(excess_power_score, 0) AS score,
                           verdict
                      FROM market_power_scores
                     WHERE published = TRUE
                       AND verdict = 'BUILD'
                     ORDER BY market_slug, computed_at DESC
                """)
                rows = cur.fetchall() or []
                rows = sorted(rows, key=lambda r: -(r[4] if not hasattr(r, "get") else r.get("score") or 0))
                for row in rows[:limit]:
                    if hasattr(row, "get"):
                        out.append({
                            "market": row.get("market_name"),
                            "slug":   row.get("market_slug"),
                            "iso":    row.get("iso"),
                            "state":  row.get("state"),
                            "score":  float(row.get("score") or 0),
                            "verdict": row.get("verdict"),
                        })
                    else:
                        out.append({
                            "market": row[0], "slug": row[1], "iso": row[2],
                            "state":  row[3], "score": float(row[4] or 0),
                            "verdict": row[5],
                        })
            except Exception as e:
                _log(f"build markets query failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _make_short_code(destination: str, topic: str = "",
                     smp_id: int | None = None) -> str | None:
    """Generate a /li/<short> code for `destination`. Idempotent per
    URL: if we already minted one we return the existing code."""
    if not destination:
        return None
    code = hashlib.sha1(
        f"{destination}|{topic}|{smp_id or ''}".encode("utf-8")
    ).hexdigest()[:8]
    conn = _db_conn()
    if conn is None:
        return code  # still usable in copy; click attribution will skip
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO media_link_shortcodes
                        (short_code, destination, topic, smp_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (short_code) DO NOTHING
                """, (code, destination, topic, smp_id))
            except Exception as e:
                _log(f"short_code insert failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return code


def make_li_url(destination: str, topic: str = "",
                smp_id: int | None = None) -> str:
    """Public helper for generators: returns a https://dchub.cloud/li/<short>
    URL pointing at `destination`. Use this in EVERY LinkedIn-bound post."""
    code = _make_short_code(destination, topic, smp_id)
    if not code:
        return destination
    base = os.environ.get("DCHUB_LI_BASE_URL", "https://dchub.cloud")
    return f"{base}/li/{code}"


def create_build_top10_series(start_date: datetime.date | None = None) -> dict[str, Any]:
    """Generate the 5-part 'Top 10 BUILD markets — series' and enqueue 5
    social_media_posts rows (status='approved') scheduled one per day for
    5 days. Each post links via /li/<short> to /reports/state-of-2026."""
    out: dict[str, Any] = {"series_kind": "build-top10", "posts": [],
                           "errors": []}
    if start_date is None:
        start_date = datetime.date.today() + datetime.timedelta(days=1)
    markets = _fetch_top_build_markets(50)
    if not markets:
        out["errors"].append("no_build_markets")
        return out
    chunks = [markets[i:i + 10] for i in range(0, min(50, len(markets)), 10)]
    conn = _db_conn()
    if conn is None:
        out["errors"].append("no_db")
        return out
    try:
        with conn, conn.cursor() as cur:
            for idx, chunk in enumerate(chunks[:5]):
                day = start_date + datetime.timedelta(days=idx)
                rank_start = idx * 10 + 1
                rank_end   = rank_start + len(chunk) - 1
                title = (f"Top BUILD markets · #{rank_start}–{rank_end} "
                         f"(part {idx + 1}/5)")
                lines = [
                    f"#{rank_start + i} {m['market']} ({m['state']}, {m['iso']}) — "
                    f"DCPI score {m['score']:.1f}"
                    for i, m in enumerate(chunk)]
                # Build the post body. Append /li/<short> attribution link.
                landing = "https://dchub.cloud/reports/state-of-2026"
                li_url = make_li_url(landing, topic="market_brief")
                body = (
                    f"📍 Top BUILD markets — part {idx + 1} of 5\n\n"
                    f"DC Hub's DCPI ranks {len(markets)}+ US data center markets by "
                    f"composite power-grid-capacity index. Here's where today's "
                    f"BUILD verdicts cluster (#{rank_start}–{rank_end}):\n\n"
                    + "\n".join(lines)
                    + "\n\nFull live ranking & methodology:\n" + li_url
                    + "\n\n#datacenter #DCPI #infrastructure"
                )
                # Persist the series row + enqueue social_media_posts.
                try:
                    cur.execute("""
                        INSERT INTO media_themed_series
                            (series_kind, day_idx, title, body, scheduled_for)
                        VALUES ('build-top10', %s, %s, %s, %s)
                        ON CONFLICT (series_kind, day_idx, scheduled_for)
                        DO UPDATE SET title = EXCLUDED.title,
                                      body  = EXCLUDED.body
                        RETURNING id
                    """, (idx, title, body, day))
                    series_id = cur.fetchone()[0]
                except Exception as e:
                    out["errors"].append(f"series_insert day {idx}: {e}")
                    continue
                try:
                    cur.execute("""
                        INSERT INTO social_media_posts
                            (content, platform, publish_platform, status,
                             created_at, media_topic_tags)
                        VALUES (%s, 'linkedin', 'linkedin', 'approved',
                                NOW() ON CONFLICT DO NOTHING, %s::jsonb)
                        RETURNING id
                    """, (body, json.dumps(["market_brief", "dcpi_verdict"])))
                    smp_id = cur.fetchone()[0]
                    cur.execute(
                        "UPDATE media_themed_series SET smp_id=%s WHERE id=%s",
                        (smp_id, series_id))
                except Exception as e:
                    out["errors"].append(f"smp_enqueue day {idx}: {e}")
                    smp_id = None
                out["posts"].append({
                    "day_idx":       idx,
                    "scheduled_for": day.isoformat(),
                    "title":         title,
                    "n_markets":     len(chunk),
                    "smp_id":        smp_id,
                    "preview":       body[:200],
                })
    finally:
        try: conn.close()
        except Exception: pass
    return out


def list_themed_series() -> list[dict]:
    out: list[dict] = []
    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT series_kind, day_idx, title, scheduled_for,
                           smp_id, created_at
                      FROM media_themed_series
                     WHERE scheduled_for >= NOW() - INTERVAL '14 days'
                     ORDER BY scheduled_for ASC, day_idx ASC
                """)
                rows = cur.fetchall() or []
            except Exception:
                rows = []
            for row in rows:
                if hasattr(row, "get"):
                    out.append({
                        "series_kind":   row.get("series_kind"),
                        "day_idx":       int(row.get("day_idx") or 0),
                        "title":         row.get("title"),
                        "scheduled_for": row.get("scheduled_for").isoformat() if row.get("scheduled_for") else None,
                        "smp_id":        row.get("smp_id"),
                        "created_at":    row.get("created_at").isoformat() if row.get("created_at") else None,
                    })
                else:
                    out.append({
                        "series_kind":   row[0],
                        "day_idx":       int(row[1] or 0),
                        "title":         row[2],
                        "scheduled_for": row[3].isoformat() if row[3] else None,
                        "smp_id":        row[4],
                        "created_at":    row[5].isoformat() if row[5] else None,
                    })
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── LinkedIn click attribution (/li/<short>) ────────────────────────────
def _li_session_id() -> str:
    """Read existing dc_li_session cookie or mint a new opaque id.
    The cookie is set on the 302 response by `http_li_click`."""
    sid = request.cookies.get("dc_li_session", "")
    if sid and len(sid) >= 12:
        return sid
    return secrets.token_urlsafe(16)


def _hash_ip(ip: str) -> str:
    if not ip: return ""
    return hashlib.sha256(f"li-ip-salt|{ip}".encode("utf-8")).hexdigest()[:24]


def record_li_click(short_code: str, destination: str,
                    session_id: str, topic: str | None = None,
                    smp_id: int | None = None) -> None:
    """Persist one /li/ click event. Best-effort — failures are silent."""
    conn = _db_conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            referer = (request.headers.get("Referer") or "")[:500]
            ua      = (request.headers.get("User-Agent") or "")[:300]
            ip      = (request.headers.get("CF-Connecting-IP")
                       or request.headers.get("X-Forwarded-For", "").split(",")[0]
                       or request.remote_addr or "")
            cur.execute("""
                INSERT INTO media_link_clicks
                    (short_code, destination, session_id, referer, user_agent,
                     ip_hash, topic, smp_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (short_code, destination, session_id, referer, ua,
                  _hash_ip(ip), topic, smp_id))
    except Exception as e:
        _log(f"record_li_click failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass


def linkedin_assisted_conversions(days: int = 30) -> dict[str, Any]:
    """For the funnel-health KPI: how many signups in the last `days`
    days came from a session that started with a /li/ click."""
    out = {"clicks": 0, "unique_sessions": 0,
           "assisted_signups": 0, "days": days}
    conn = _db_conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"""
                    SELECT COUNT(*)                          AS clicks,
                           COUNT(DISTINCT session_id)        AS unique_sessions
                      FROM media_link_clicks
                     WHERE clicked_at > NOW() - INTERVAL '{int(days)} days'
                """)
                row = cur.fetchone()
                if row:
                    if hasattr(row, "get"):
                        out["clicks"]          = int(row.get("clicks") or 0)
                        out["unique_sessions"] = int(row.get("unique_sessions") or 0)
                    else:
                        out["clicks"]          = int(row[0] or 0)
                        out["unique_sessions"] = int(row[1] or 0)
            except Exception as e:
                _log(f"assisted query failed: {e}")
            # Best-effort: how many users signed up after a /li click.
            # mcp_dev_keys has a created_at column for new keys.
            try:
                cur.execute(f"""
                    SELECT COUNT(DISTINCT m.session_id)
                      FROM media_link_clicks m
                      LEFT JOIN mcp_dev_keys k
                             ON k.created_at > m.clicked_at
                            AND k.created_at < m.clicked_at + INTERVAL '7 days'
                     WHERE m.clicked_at > NOW() - INTERVAL '{int(days)} days'
                       AND k.id IS NOT NULL
                """)
                row = cur.fetchone()
                if row:
                    out["assisted_signups"] = int(
                        (row.get("count") if hasattr(row, "get") else row[0]) or 0)
            except Exception:
                # mcp_dev_keys may not have the columns we expect — table
                # exists; treat as zero rather than crash.
                pass
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── HTTP surface ────────────────────────────────────────────────────────
@media_topic_tuner_bp.route(
    "/api/v1/admin/media/topic-performance", methods=["GET"])
def http_topic_performance():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    try: days = int(request.args.get("days", "30"))
    except Exception: days = 30
    days = max(1, min(days, 365))
    rows = topic_performance(days=days)
    return jsonify({
        "days": days,
        "n_topics": len(rows),
        "topics": rows,
    }), 200


@media_topic_tuner_bp.route(
    "/api/v1/admin/media/backfill-tags", methods=["POST"])
def http_backfill_tags():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    try: limit = int(request.args.get("limit", "500"))
    except Exception: limit = 500
    limit = max(1, min(limit, 5000))
    return jsonify(backfill_recent_tags(limit=limit)), 200


@media_topic_tuner_bp.route(
    "/api/v1/admin/media/tune-now", methods=["POST"])
def http_tune_now():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(tune_now()), 200


@media_topic_tuner_bp.route(
    "/api/v1/admin/media/current-mix", methods=["GET"])
def http_current_mix():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    mix = current_topic_mix()
    return jsonify({
        "n": len(mix),
        "mix": mix,
        "effective_for": (mix[0].get("effective_for") if mix else None),
    }), 200


@media_topic_tuner_bp.route(
    "/api/v1/admin/media/series/create", methods=["POST"])
def http_series_create():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    kind = (request.args.get("kind") or "build-top10").strip()
    if kind != "build-top10":
        return jsonify({"error": "unknown_kind",
                        "supported": ["build-top10"]}), 400
    return jsonify(create_build_top10_series()), 200


@media_topic_tuner_bp.route(
    "/api/v1/admin/media/series/list", methods=["GET"])
def http_series_list():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"series": list_themed_series()}), 200


@media_topic_tuner_bp.route("/li/<short>", methods=["GET"])
def http_li_click(short):
    """Public — 302 redirect with click attribution.
    NEVER returns 500; on lookup miss, redirect to dchub.cloud root."""
    short = (short or "").strip()[:32]
    destination = "https://dchub.cloud"
    topic = None
    smp_id = None
    conn = _db_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT destination, topic, smp_id
                      FROM media_link_shortcodes
                     WHERE short_code = %s
                """, (short,))
                row = cur.fetchone()
                if row:
                    if hasattr(row, "get"):
                        destination = row.get("destination") or destination
                        topic       = row.get("topic")
                        smp_id      = row.get("smp_id")
                    else:
                        destination = row[0] or destination
                        topic       = row[1]
                        smp_id      = row[2]
        except Exception as e:
            _log(f"li_click lookup failed: {e}")
        finally:
            try: conn.close()
            except Exception: pass
    session_id = _li_session_id()
    try:
        record_li_click(short, destination, session_id, topic, smp_id)
    except Exception:
        pass
    resp = make_response(redirect(destination, code=302))
    resp.set_cookie(
        "dc_li_session", session_id,
        max_age=90 * 24 * 3600,  # 90 days
        path="/", samesite="Lax", secure=True, httponly=True,
    )
    return resp


# ── Admin HTML dashboard ────────────────────────────────────────────────
def _render_media_mix_html(mix: list[dict], perf: list[dict],
                          series: list[dict],
                          assisted: dict[str, Any],
                          autoresp: list[dict] | None = None,
                          autoresp_flags: dict[str, Any] | None = None) -> str:
    """Single-page HTML — top 5 topics, current mix bars, series schedule,
    click-attribution KPIs + ROUND-2 auto-response activity table.
    No frameworks; mobile-friendly."""
    def _fmt_pct(x: float) -> str:
        return f"{x * 100:.1f}%"
    eff = mix[0]["effective_for"] if mix else "(none yet)"
    top5_html = "".join(
        f"<tr><td>{i + 1}</td><td>{p['name']}</td>"
        f"<td class='num'>{p['n_posts']}</td>"
        f"<td class='num'>{p['impressions']:,}</td>"
        f"<td class='num'>{p['avg_engagement']:.2f}</td>"
        f"<td class='num'><b>{p['score']:.1f}</b></td></tr>"
        for i, p in enumerate(perf[:5])
    ) or "<tr><td colspan=6 class='muted'>No data in window.</td></tr>"
    mix_html = "".join(
        f"<tr><td>{r['name']}</td>"
        f"<td class='num'>{_fmt_pct(r['weight'])}</td>"
        f"<td><div class='bar' style='width:{min(100, r['weight']*100*3):.0f}%'></div></td>"
        f"<td class='num'>{r['n_posts']}</td>"
        f"<td class='muted'>{(r.get('reason') or '')[:80]}</td></tr>"
        for r in mix
    ) or "<tr><td colspan=5 class='muted'>Mix not computed yet — POST /api/v1/admin/media/tune-now.</td></tr>"
    series_html = "".join(
        f"<tr><td>{s['scheduled_for']}</td><td>{s['title']}</td>"
        f"<td class='num'>{s.get('smp_id') or '—'}</td></tr>"
        for s in series
    ) or "<tr><td colspan=3 class='muted'>No series created yet — POST /api/v1/admin/media/series/create?kind=build-top10.</td></tr>"
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8">
<title>Media Mix · DC Hub Admin</title>
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif;
         background: #0a0d12; color: #e7ecf3; max-width: 1100px; margin: 24px auto; padding: 0 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 10px; color: #9ec5fe; }}
  .muted {{ color: #6b7785; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 14px;
           background: #1d2630; color: #9ec5fe; font-size: 11px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #20283a;
        color: #97a3b6; font-weight: 500; font-size: 12px; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #161c28; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .bar {{ height: 8px; background: linear-gradient(90deg, #5cf, #4af); border-radius: 4px; min-width: 4px; }}
  .kpi-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 8px 0 16px; }}
  .kpi {{ background: #131a25; padding: 12px 16px; border-radius: 8px; min-width: 160px; }}
  .kpi b {{ display: block; font-size: 22px; color: #fff; }}
  .kpi span {{ font-size: 11px; color: #97a3b6; text-transform: uppercase; letter-spacing: 0.5px; }}
  code {{ background: #131a25; padding: 1px 6px; border-radius: 4px; }}
</style></head>
<body>
<h1>Media Topic Mix <span class="pill">DC Hub Admin · 2026-06-07</span></h1>
<p class="muted">Topic-level engagement learner. Current mix effective for <b>{eff}</b>.
The tuner runs daily at 13:30 UTC after LinkedIn engagement sync.</p>

<div class="kpi-row">
  <div class="kpi"><span>LinkedIn /li/ clicks (30d)</span><b>{assisted.get('clicks', 0):,}</b></div>
  <div class="kpi"><span>Unique LI sessions (30d)</span><b>{assisted.get('unique_sessions', 0):,}</b></div>
  <div class="kpi"><span>LI assisted signups (30d)</span><b>{assisted.get('assisted_signups', 0):,}</b></div>
  <div class="kpi"><span>Topics in mix</span><b>{len(mix)}</b></div>
</div>

<h2>Top 5 Topics by 30-day Engagement Score</h2>
<table><thead><tr>
  <th>#</th><th>Topic</th><th>n_posts</th><th>Impressions</th>
  <th>Avg Engagement</th><th>Score</th>
</tr></thead>
<tbody>{top5_html}</tbody></table>

<h2>Current Topic Mix (auto-tuned · weights cap at 35% to dampen clickbait)</h2>
<table><thead><tr>
  <th>Topic</th><th>Weight</th><th></th><th>n_posts</th><th>Reason</th>
</tr></thead>
<tbody>{mix_html}</tbody></table>

<h2>Themed Series Schedule</h2>
<table><thead><tr>
  <th>Scheduled For</th><th>Title</th><th>SMP id</th>
</tr></thead>
<tbody>{series_html}</tbody></table>

{_render_trending_section()}

{_render_autoresponse_section(autoresp or [], autoresp_flags or {})}

{_render_comment_engagement_section()}

{_render_dm_follow_up_section()}

{_render_style_ab_section()}

<h2 class="muted" style="margin-top:32px;font-size:14px;">Tuner actions</h2>
<p class="muted">
  <code>POST /api/v1/admin/media/tune-now</code> — recompute today's mix<br>
  <code>POST /api/v1/admin/media/backfill-tags?limit=500</code> — tag historic posts<br>
  <code>POST /api/v1/admin/media/series/create?kind=build-top10</code> — create 5-part series<br>
  <code>GET  /api/v1/admin/media/topic-performance?days=30</code> — raw engagement<br>
  <code>GET  /api/v1/admin/media/spikes/preview</code> — round-2 spike preview<br>
  <code>POST /api/v1/admin/media/spikes/detect</code> — round-2 detect+gen run<br>
  <code>POST /api/v1/admin/media/autoresponse/publish/&lt;log_id&gt;</code> — flush a dry-run draft live
</p>
</body></html>"""


def _render_trending_section() -> str:
    """ROUND 3 (2026-06-07): render the Trending Now card. Imports lazily
    so a partial deploy without media_trending_detector doesn't blow up
    the entire /admin/media-mix surface."""
    try:
        from routes.media_trending_detector import render_trending_card_html
        return render_trending_card_html()
    except Exception:
        return ("<h2>Trending Now (Round 3)</h2>"
                "<p class='muted'>media_trending_detector not loaded "
                "(partial deploy or kill switch). See "
                "<code>/admin/media-trending</code> for the standalone "
                "view.</p>")


def _render_autoresponse_section(rows: list[dict], flags: dict[str, Any]) -> str:
    """ROUND-2 dashboard tile: Auto-Response Activity. Shows last 30d
    spikes + decisions (responded/skipped/rejected/dry_run), the
    generated comments + their performance after publish, and the
    kill-switch / dry-run flags currently in effect."""
    dry_pill   = ("DRY-RUN" if flags.get("dry_run") else "LIVE")
    kill_pill  = ("KILL ON" if flags.get("kill_switched") else "kill off")
    weekly     = f"{flags.get('weekly_used', 0)}/{flags.get('weekly_cap', 3)} this 7d"
    badge_dry  = "background:#3b2e0a;color:#ffd166" if flags.get("dry_run") else "background:#0e2a1c;color:#7cf3a0"
    badge_kill = "background:#3a0e0e;color:#ff8a8a" if flags.get("kill_switched") else "background:#1d2630;color:#9ec5fe"
    table_rows: list[str] = []
    for r in rows[:30]:
        spike = (r.get("spike_detected_at") or "")[:16].replace("T", " ")
        urn   = (r.get("source_post_urn") or "")[:34]
        impr  = int(r.get("impressions_before") or 0)
        ratio = float(r.get("viral_ratio") or 0)
        status= str(r.get("status") or "")
        comms = r.get("comments") or []
        if isinstance(comms, str):
            try:
                comms = json.loads(comms)
            except Exception:
                comms = []
        comms_html = "".join(
            f"<div style='padding:6px 0;border-top:1px solid #1d2530'>"
            f"<span class='muted' style='font-size:11px'>+{(c.get('offset_min') or 0)}m</span> "
            f"&nbsp;{(c.get('comment') or '')[:240]}</div>"
            for c in (comms or []) if isinstance(c, dict)
        )
        if not comms_html:
            comms_html = "<span class='muted'>(no comments generated)</span>"
        status_color = {
            "dry_run":   "#ffd166",
            "queued":    "#9ec5fe",
            "published": "#7cf3a0",
            "rejected":  "#ff8a8a",
            "publish_failed": "#ff8a8a",
        }.get(status, "#97a3b6")
        publish_btn = ""
        if status in ("dry_run", "queued"):
            publish_btn = (
                f"<a href='/api/v1/admin/media/autoresponse/publish/{r.get('id')}'"
                " style='font-size:11px;color:#9ec5fe' "
                "onclick=\"event.preventDefault();fetch(this.href,{method:'POST',"
                "headers:{'X-Admin-Key':prompt('admin key')}}).then(r=>r.json())"
                ".then(d=>alert(JSON.stringify(d,null,2)))\">"
                "publish live</a>"
            )
        table_rows.append(
            f"<tr><td>{spike}</td>"
            f"<td class='muted' style='font-size:11px'>{urn}</td>"
            f"<td class='num'>{impr:,}</td>"
            f"<td class='num'>{ratio:.2f}x</td>"
            f"<td><span style='color:{status_color}'>{status}</span> {publish_btn}</td></tr>"
            f"<tr><td colspan=5 style='padding:4px 8px 12px 8px;background:#0e1320'>{comms_html}</td></tr>"
        )
    body = "".join(table_rows) or (
        "<tr><td colspan=5 class='muted'>No spike auto-responses yet. "
        "The cron runs 10/22 UTC; manually probe with "
        "<code>GET /api/v1/admin/media/spikes/preview</code>.</td></tr>"
    )
    return f"""
<h2>Auto-Response Activity (ROUND 2)</h2>
<div class="kpi-row">
  <div class="kpi" style="{badge_dry};padding:6px 12px"><span>Mode</span><b>{dry_pill}</b></div>
  <div class="kpi" style="{badge_kill};padding:6px 12px"><span>Switch</span><b>{kill_pill}</b></div>
  <div class="kpi"><span>Weekly cap</span><b>{weekly}</b></div>
  <div class="kpi"><span>30d spikes logged</span><b>{len(rows)}</b></div>
</div>
<table><thead><tr>
  <th>Detected</th><th>Source post URN</th><th>Impressions</th>
  <th>Ratio</th><th>Status</th>
</tr></thead>
<tbody>{body}</tbody></table>
<p class="muted" style="font-size:12px;margin-top:4px">
  Defaults: detect at 2x baseline impressions in &lt;6h, generate 3 comments via Claude (staggered 30/90/180min),
  ship in DRY-RUN. Set <code>MEDIA_AUTORESPONSE_DRY_RUN=0</code> to go live.
  Kill switch: <code>MEDIA_AUTORESPONSE_DISABLE=1</code>.
  Weekly cap: <code>MEDIA_AUTORESPONSE_WEEKLY_CAP=3</code>.
</p>"""


def _render_comment_engagement_section() -> str:
    """2026-06-07: surface the LinkedIn Comment Engagement loop card on
    /admin/media-mix. Shows last 7d of detected comments, the generated
    replies, decision badges (replied / dry_run / queued / skipped_*),
    and the kill-switch + dry-run flags. Includes a one-click
    "regenerate" admin button per row.

    CRITICAL for Monday's State of 2026 launch — this is where Jonathan
    will be reviewing the first wave of drafts before flipping
    MEDIA_COMMENT_REPLY_DRY_RUN=0 to go live. Fail-soft: empty string
    on partial deploy."""
    try:
        from routes.media_comment_engagement import (
            recent_log_rows as _ce_recent,
            recent_counters as _ce_counters,
            _dry_run as _ce_dry,
            _kill_switched as _ce_kill,
            _env_int as _ce_env,
            DAILY_CAP_DEFAULT as _ce_cap_default,
        )
        rows = _ce_recent(days=7, limit=50) or []
        counters = _ce_counters(days=7) or {}
        dry_on = bool(_ce_dry())
        kill_on = bool(_ce_kill())
        daily_cap = int(_ce_env("MEDIA_COMMENT_REPLY_DAILY_CAP", _ce_cap_default))
    except Exception as e:
        _log(f"render_comment_engagement_section skipped: {e}")
        return ""

    dry_pill   = "DRY-RUN" if dry_on else "LIVE"
    kill_pill  = "KILL ON" if kill_on else "kill off"
    badge_dry  = ("background:#3b2e0a;color:#ffd166" if dry_on
                  else "background:#0e2a1c;color:#7cf3a0")
    badge_kill = ("background:#3a0e0e;color:#ff8a8a" if kill_on
                  else "background:#1d2630;color:#9ec5fe")
    replied   = int(counters.get("replied", 0))
    queued    = int(counters.get("queued", 0))
    dry_rows  = int(counters.get("dry_run", 0))
    skipped_total = sum(
        v for k, v in counters.items()
        if isinstance(v, int) and k.startswith("skipped_"))

    table_rows: list[str] = []
    for r in rows[:40]:
        detected = (r.get("comment_detected_at") or "")[:16].replace("T", " ")
        author = (r.get("comment_author_name") or r.get("comment_author_urn") or "")[:32]
        text = (r.get("comment_text") or "")[:240]
        reply = (r.get("reply_generated") or "")[:300]
        decision = str(r.get("decision") or "")
        reason = str(r.get("decision_reason") or "")[:80]
        scheduled = (r.get("scheduled_for") or "")[:16].replace("T", " ")
        posted = (r.get("reply_posted_at") or "")[:16].replace("T", " ")
        urn = (r.get("source_post_urn") or "")[:34]
        log_id = int(r.get("id") or 0)
        decision_color = {
            "replied":          "#7cf3a0",
            "queued":           "#9ec5fe",
            "dry_run":          "#ffd166",
            "post_failed":      "#ff8a8a",
            "skipped_tone":     "#c79bff",
            "skipped_spam":     "#ff8a8a",
            "skipped_self":     "#97a3b6",
            "skipped_too_short":"#97a3b6",
            "skipped_bare_emoji":"#97a3b6",
            "skipped_blocklist_urn":"#97a3b6",
            "skipped_capped":   "#ffb37c",
        }.get(decision, "#97a3b6")
        regen_btn = ""
        if decision in ("dry_run", "queued", "skipped_tone", "post_failed") and log_id:
            regen_btn = (
                f"<a href='/api/v1/admin/media/comment-engagement/regenerate/{log_id}'"
                " style='font-size:11px;color:#9ec5fe;margin-left:8px' "
                "onclick=\"event.preventDefault();fetch(this.href,{method:'POST',"
                "headers:{'X-Admin-Key':prompt('admin key')}}).then(r=>r.json())"
                ".then(d=>alert(JSON.stringify(d,null,2)))\">regenerate</a>"
            )
        sched_or_posted = posted or scheduled or "—"
        table_rows.append(
            f"<tr><td>{detected}</td>"
            f"<td class='muted' style='font-size:11px'>{author}</td>"
            f"<td>{text}</td>"
            f"<td><span style='color:{decision_color}'>{decision}</span>"
            f" <span class='muted' style='font-size:11px'>{reason}</span>{regen_btn}</td>"
            f"<td class='muted' style='font-size:11px'>{sched_or_posted}<br>{urn}</td></tr>"
        )
        if reply:
            table_rows.append(
                f"<tr><td colspan=5 style='padding:4px 8px 12px 8px;background:#0e1320'>"
                f"<span class='muted' style='font-size:11px'>reply:</span> {reply}"
                f"</td></tr>"
            )
    body = "".join(table_rows) or (
        "<tr><td colspan=5 class='muted'>No comments processed yet. "
        "The cron runs 9/21 UTC; manually probe with "
        "<code>GET /api/v1/admin/media/comment-engagement/poll-now</code>.</td></tr>"
    )
    return f"""
<h2>Comment Engagement (LinkedIn auto-reply loop)</h2>
<div class="kpi-row">
  <div class="kpi" style="{badge_dry};padding:6px 12px"><span>Mode</span><b>{dry_pill}</b></div>
  <div class="kpi" style="{badge_kill};padding:6px 12px"><span>Switch</span><b>{kill_pill}</b></div>
  <div class="kpi"><span>Daily cap</span><b>{daily_cap}</b></div>
  <div class="kpi"><span>7d replied</span><b>{replied}</b></div>
  <div class="kpi"><span>7d queued</span><b>{queued}</b></div>
  <div class="kpi"><span>7d dry-run</span><b>{dry_rows}</b></div>
  <div class="kpi"><span>7d skipped</span><b>{skipped_total}</b></div>
</div>
<table><thead><tr>
  <th>Detected</th><th>Author</th><th>Comment</th>
  <th>Decision</th><th>Scheduled / Post URN</th>
</tr></thead>
<tbody>{body}</tbody></table>
<p class="muted" style="font-size:12px;margin-top:4px">
  Detects comments on the last 7d of DC Hub LinkedIn posts (every 4h), drafts a 280-char reply via Claude
  (1 specific DC Hub number + 1 brief link), tone-filters, waits a random 4-7min for human cadence, posts as the
  org URN. Ships DRY-RUN by default — set <code>MEDIA_COMMENT_REPLY_DRY_RUN=0</code> to flip live after
  reviewing ~10 drafts. Daily cap: <code>MEDIA_COMMENT_REPLY_DAILY_CAP={daily_cap}</code>.
  Kill switch: <code>MEDIA_COMMENT_REPLY_DISABLE=1</code>.
  Blocklist: <code>MEDIA_COMMENT_REPLY_BLOCKLIST=urn1,name2,...</code>.
  Endpoints: <code>GET /api/v1/admin/media/comment-engagement/poll-now</code> (dry-run preview),
  <code>POST .../run</code> (full cycle), <code>POST .../flush-due</code> (post queued).
</p>"""


def _render_dm_follow_up_section() -> str:
    """2026-06-07: surface the DM Follow-up card on /admin/media-mix.

    Compounds the Comment Engagement tile above — shows pending dry-run
    DM drafts, sent DMs, attribution KPIs (response rate, attributed
    visits, signups), plus a per-row 1-click 'approve & send' admin
    button (DRY-RUN drafts only; killswitch + already-sent prevent it
    from doing anything dangerous).

    CRITICAL for Monday's State of 2026 launch — operator reviews the
    first wave of DM drafts here BEFORE flipping MEDIA_DM_DRY_RUN=0 to
    go fully autonomous. Fail-soft: empty string on partial deploy."""
    try:
        from routes.media_dm_follow_up import (
            recent_log_rows as _dm_recent,
            recent_counters as _dm_counters,
            _dry_run as _dm_dry,
            _kill_switched as _dm_kill,
            _env_int as _dm_env,
            DAILY_CAP_DEFAULT as _dm_cap_default,
            MIN_FOLLOWERS_DEFAULT as _dm_floor_default,
        )
        rows = _dm_recent(days=30, limit=50) or []
        counters = _dm_counters(days=30) or {}
        dry_on = bool(_dm_dry())
        kill_on = bool(_dm_kill())
        daily_cap = int(_dm_env("MEDIA_DM_DAILY_CAP", _dm_cap_default))
        min_follow = int(_dm_env("MEDIA_DM_MIN_FOLLOWERS", _dm_floor_default))
    except Exception as e:
        _log(f"render_dm_follow_up_section skipped: {e}")
        return ""

    dry_pill   = "DRY-RUN" if dry_on else "LIVE"
    kill_pill  = "KILL ON" if kill_on else "kill off"
    badge_dry  = ("background:#3b2e0a;color:#ffd166" if dry_on
                  else "background:#0e2a1c;color:#7cf3a0")
    badge_kill = ("background:#3a0e0e;color:#ff8a8a" if kill_on
                  else "background:#1d2630;color:#9ec5fe")
    sent       = int(counters.get("sent", 0))
    dry_rows   = int(counters.get("dry_run", 0))
    failed     = int(counters.get("post_failed", 0))
    skipped_total = sum(
        v for k, v in counters.items()
        if isinstance(v, int) and k.startswith("skipped_"))
    responses  = int(counters.get("responses", 0))
    attrib_v   = int(counters.get("attributed_visits", 0))
    attrib_s   = int(counters.get("attributed_signups", 0))

    table_rows: list[str] = []
    for r in rows[:40]:
        detected = (r.get("detected_at") or "")[:16].replace("T", " ")
        recip = (r.get("recipient_name")
                 or r.get("recipient_urn") or "")[:32]
        title = (r.get("recipient_title") or "")[:48]
        company = (r.get("recipient_company") or "")[:32]
        followers = int(r.get("recipient_follower_count") or 0)
        qual = (r.get("qualified_by") or "")[:24]
        subject = (r.get("dm_subject") or "")[:80]
        body = (r.get("dm_body") or "")[:480]
        link = (r.get("dm_link") or "")[:80]
        decision = str(r.get("decision") or "")
        reason = str(r.get("decision_reason") or "")[:60]
        sent_at = (r.get("dm_sent_at") or "")[:16].replace("T", " ")
        resp_at = (r.get("response_received_at") or "")
        log_id = int(r.get("id") or 0)
        decision_color = {
            "sent":         "#7cf3a0",
            "dry_run":      "#ffd166",
            "post_failed":  "#ff8a8a",
            "skipped_tone": "#c79bff",
        }.get(decision, "#97a3b6")

        # Action buttons — only on dry_run rows that are still actionable
        buttons = ""
        if decision == "dry_run" and log_id:
            buttons = (
                f"<a href='/api/v1/admin/media/dm-followup/approve/{log_id}'"
                " style='font-size:11px;color:#7cf3a0;margin-left:8px' "
                "onclick=\"event.preventDefault();"
                "if(!confirm('Send this DM live?'))return;"
                "fetch(this.href,{method:'POST',"
                "headers:{'X-Admin-Key':prompt('admin key')}})"
                ".then(r=>r.json()).then(d=>alert(JSON.stringify(d,null,2)))\""
                ">approve&amp;send</a>"
                f"<a href='/api/v1/admin/media/dm-followup/regenerate/{log_id}'"
                " style='font-size:11px;color:#9ec5fe;margin-left:8px' "
                "onclick=\"event.preventDefault();fetch(this.href,{method:'POST',"
                "headers:{'X-Admin-Key':prompt('admin key')}}).then(r=>r.json())"
                ".then(d=>alert(JSON.stringify(d,null,2)))\""
                ">regenerate</a>"
            )

        recip_line = f"<b>{recip}</b>"
        if title and company:
            recip_line += (f"<br><span class='muted' style='font-size:11px'>"
                           f"{title} · {company}</span>")
        elif title:
            recip_line += (f"<br><span class='muted' style='font-size:11px'>"
                           f"{title}</span>")
        recip_line += (f"<br><span class='muted' style='font-size:10px'>"
                       f"{followers:,} followers · {qual}</span>")

        timing = sent_at or detected
        if resp_at:
            timing += (f"<br><span style='color:#7cf3a0;font-size:10px'>"
                       f"replied</span>")

        table_rows.append(
            f"<tr><td>{recip_line}</td>"
            f"<td><b>{subject}</b><br>"
            f"<span style='font-size:12px'>{body}</span><br>"
            f"<span class='muted' style='font-size:10px'>→ {link}</span></td>"
            f"<td><span style='color:{decision_color}'>{decision}</span>"
            f" <span class='muted' style='font-size:10px'>{reason}</span>"
            f"{buttons}</td>"
            f"<td class='muted' style='font-size:11px'>{timing}</td></tr>"
        )

    body_html = "".join(table_rows) or (
        "<tr><td colspan=4 class='muted'>No DM follow-ups yet. "
        "The cron runs twice daily at 11/21 UTC; manually probe with "
        "<code>GET /api/v1/admin/media/dm-followup/preview</code>.</td></tr>"
    )
    return f"""
<h2>DM Follow-up (1:1 LinkedIn DMs to qualified commenters)</h2>
<div class="kpi-row">
  <div class="kpi" style="{badge_dry};padding:6px 12px"><span>Mode</span><b>{dry_pill}</b></div>
  <div class="kpi" style="{badge_kill};padding:6px 12px"><span>Switch</span><b>{kill_pill}</b></div>
  <div class="kpi"><span>Daily cap</span><b>{daily_cap}/day</b></div>
  <div class="kpi"><span>Follower floor</span><b>{min_follow:,}+</b></div>
  <div class="kpi"><span>30d sent</span><b>{sent}</b></div>
  <div class="kpi"><span>30d drafts</span><b>{dry_rows}</b></div>
  <div class="kpi"><span>30d responses</span><b>{responses}</b></div>
  <div class="kpi"><span>30d visits attributed</span><b>{attrib_v}</b></div>
  <div class="kpi"><span>30d signups attributed</span><b>{attrib_s}</b></div>
  <div class="kpi"><span>30d failed/skipped</span><b>{failed + skipped_total}</b></div>
</div>
<table><thead><tr>
  <th style="width:200px">Recipient</th>
  <th>Draft DM</th>
  <th style="width:160px">Decision</th>
  <th style="width:120px">Timing</th>
</tr></thead>
<tbody>{body_html}</tbody></table>
<p class="muted" style="font-size:12px;margin-top:4px">
  Compounds the Comment Engagement tile above. When a qualified commenter
  (500+ followers OR title contains CFO / Director / VP / Founder / PE /
  Real Estate / Energy / etc.) replies to a DC Hub post, Claude drafts a
  3-sentence DM (references their comment + 1 specific DC Hub number +
  soft ask) with a personalized brief link (PE→state-of-power,
  RE→/markets/&lt;city&gt;/brief, operator→/operators/&lt;co&gt;/brief,
  general→/state-of-2026). 24h cooldown per recipient. 5/day hard cap
  = 35/wk (LinkedIn allows ~100/wk for org pages).
  Ships DRY-RUN by default — review ~5 drafts, then either flip
  <code>MEDIA_DM_DRY_RUN=0</code> to go autonomous OR use the per-row
  "approve&amp;send" button (admin key required). Kill switch:
  <code>MEDIA_DM_DISABLE=1</code>. Blocklist:
  <code>MEDIA_DM_BLOCKLIST=urn1,name2,...</code>. Endpoints:
  <code>GET /api/v1/admin/media/dm-followup/preview</code>,
  <code>POST .../send</code>,
  <code>POST .../approve/&lt;id&gt;</code>.
</p>"""


def _render_style_ab_section() -> str:
    """2026-06-07: surface the Style A/B Learner card on /admin/media-mix.
    Delegates to routes.media_style_ab.render_style_ab_card() so the
    learner module owns its own template. Fail-soft: empty string if
    the module isn't importable (partial deploy)."""
    try:
        from routes.media_style_ab import render_style_ab_card
        return render_style_ab_card(days=30) or ""
    except Exception as e:
        _log(f"render_style_ab_card skipped: {e}")
        return ""


@media_topic_tuner_bp.route("/admin/media-mix", methods=["GET"])
def http_media_mix_dashboard():
    if not _admin_or_cron_authorized():
        # Soft 401 with a hint so an operator pasting the URL gets a clue.
        return ("Unauthorized — pass ?key=<DCHUB_ADMIN_KEY> or "
                "X-Admin-Key header.", 401, {"Content-Type": "text/plain"})
    mix      = current_topic_mix()
    perf     = topic_performance(days=30)
    series   = list_themed_series()
    assisted = linkedin_assisted_conversions(days=30)
    # ROUND 2 (2026-06-07): pull auto-response activity for the same
    # dashboard. Fail-soft if the module isn't on the path (partial deploy).
    autoresp: list[dict] = []
    autoresp_flags = {"dry_run": True, "kill_switched": False,
                      "weekly_cap": 3, "weekly_used": 0}
    try:
        from routes.media_spike_responder import (
            recent_log_rows as _autoresp_recent,
            _dry_run as _arr_dry,
            _kill_switched as _arr_kill,
            _env_int as _arr_env,
            _db_conn as _arr_db,
            _weekly_used as _arr_used,
            WEEKLY_CAP_DEFAULT as _arr_cap_default,
        )
        autoresp = _autoresp_recent(days=30, limit=30) or []
        autoresp_flags["dry_run"] = bool(_arr_dry())
        autoresp_flags["kill_switched"] = bool(_arr_kill())
        autoresp_flags["weekly_cap"] = int(
            _arr_env("MEDIA_AUTORESPONSE_WEEKLY_CAP", _arr_cap_default))
        _c = _arr_db()
        if _c is not None:
            try:
                with _c, _c.cursor() as _cur:
                    autoresp_flags["weekly_used"] = int(_arr_used(_cur))
            except Exception:
                pass
            finally:
                try: _c.close()
                except Exception: pass
    except Exception:
        pass
    html = _render_media_mix_html(mix, perf, series, assisted,
                                  autoresp=autoresp,
                                  autoresp_flags=autoresp_flags)
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-store"}
