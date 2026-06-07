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
                # linkedin_posts uses either `content` or 'post_text';
                # pick whichever exists.
                col = "content"
                try:
                    cur.execute("""SELECT 1 FROM information_schema.columns
                                   WHERE table_name='linkedin_posts'
                                     AND column_name='content'""")
                    if not cur.fetchone():
                        col = "post_text"
                except Exception:
                    col = "content"
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
            try:
                cur.execute(f"""
                    SELECT id,
                           COALESCE(content, post_text, '') AS body,
                           COALESCE(impressions, 0)         AS impressions,
                           COALESCE(clicks, 0)              AS clicks,
                           COALESCE(likes, 0)               AS likes,
                           COALESCE(comments, 0)            AS comments,
                           COALESCE(shares, 0)              AS shares,
                           media_topic_tags                 AS tags
                      FROM linkedin_posts
                     WHERE posted_at > NOW() - INTERVAL '{int(days)} days'
                       AND COALESCE(content, post_text, '') <> ''
                """)
                rows = cur.fetchall() or []
            except Exception as e:
                _log(f"topic_performance query failed: {e}")
                return out

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
                                NOW(), %s::jsonb)
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                          assisted: dict[str, Any]) -> str:
    """Single-page HTML — top 5 topics, current mix bars, series schedule,
    click-attribution KPIs. No frameworks; mobile-friendly."""
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

<h2 class="muted" style="margin-top:32px;font-size:14px;">Tuner actions</h2>
<p class="muted">
  <code>POST /api/v1/admin/media/tune-now</code> — recompute today's mix<br>
  <code>POST /api/v1/admin/media/backfill-tags?limit=500</code> — tag historic posts<br>
  <code>POST /api/v1/admin/media/series/create?kind=build-top10</code> — create 5-part series<br>
  <code>GET  /api/v1/admin/media/topic-performance?days=30</code> — raw engagement
</p>
</body></html>"""


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
    html = _render_media_mix_html(mix, perf, series, assisted)
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-store"}
