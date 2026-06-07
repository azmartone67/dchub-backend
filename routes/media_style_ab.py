"""DC Hub Media — Style A/B Learner (2026-06-07).
================================================

PROBLEM
-------
DC Hub Media has 5 LinkedIn imagery styles in routes/og_cards.py:
  data_brutal, editorial, infographic, ai_hero, fallback

Today the style is HARDCODED per story type in
routes/linkedin_content_engine.py:_card_url_for(). Every DCPI scoop posts as
data_brutal, every shipped-this-week as editorial, etc. We have no idea which
style actually drives the most engagement per TOPIC. The 14 topics from
routes/media_topic_tuner.py:TOPIC_LIBRARY (dcpi_verdict, hyperscaler_deal,
ai_capex, ma_transaction, grid_alert, market_brief, fiber_route, facility_news,
energy_pricing, water_risk, renewable_energy, industry_pulse, ai_citation,
verdict_shift) each have a "true" winning style — we just never measured.

SOLUTION
--------
An epsilon-greedy A/B learner that:
  1. For each new post, RANDOMLY picks a style for the topic during the
     EXPLORE phase (<3 measured posts per style for that topic).
  2. Records the decision to media_style_outcomes (impressions/etc NULL).
  3. After 3+ data points per (topic, style), switches to EPSILON-GREEDY:
     80% pick the highest-scoring style for that topic, 20% explore a
     random alternative so every style keeps accumulating data.
  4. The linkedin_engagement_sync cron back-fills impressions/reactions/
     comments/clicks onto the outcome rows 24h after publish, computing
     score = comments*5 + reactions*2 + clicks*3 + impressions*0.01.
  5. After ~30 days every topic has a measured winner. /admin/media-mix
     surfaces the leader board.

SAFETY
------
  - DCHUB_STYLE_AB_LEARNER_ENABLED=1 → enable the learner. Default OFF on
    first deploy so we OBSERVE engagement before changing behaviour.
  - MEDIA_STYLE_AB_DISABLE=1 → kill switch (wins over the enable flag).
  - MEDIA_STYLE_AB_EPSILON=0.2 → explore rate (env-tunable).
  - Cold-start: if a topic has ZERO rows in media_style_outcomes, fall back
    to the hardcoded story-type → style map from linkedin_content_engine.
  - "fallback" style is excluded from the picker pool (it's the safety net
    for blank cards — never auto-selected as a winner).
  - All DB ops fail-soft: any exception just returns the cold-start default.

ENDPOINTS
---------
  GET  /api/v1/admin/media/style-ab?days=30
       Per-topic style performance + next-pick distribution.
  POST /api/v1/admin/media/style-ab/backfill?days=30
       Seed media_style_outcomes from social_media_posts + linkedin_posts
       in the last N days (one-time).
  GET  /api/v1/admin/media/style-ab/pick?topic=market_brief
       Diagnostic — what would pick_style_for_topic(topic) return now?
"""

from __future__ import annotations

import os
import sys
import json
import random
import datetime
from typing import Any, Optional

from flask import Blueprint, jsonify, request

media_style_ab_bp = Blueprint("media_style_ab", __name__)

# ── Style pool ───────────────────────────────────────────────────────────
# Match the keys in routes/og_cards.py:STYLE_MAP. "fallback" is the safety
# net (drawn by _draw_fallback when the DB / generator fails) — we never
# RECOMMEND it as a winner, but we still accept it as a valid label so
# back-filled rows for failed-render posts can be recorded.
PICKABLE_STYLES = ("data_brutal", "editorial", "infographic", "ai_hero")
ALL_KNOWN_STYLES = PICKABLE_STYLES + ("fallback",)

# Cold-start fallback by story_type / topic — mirrors the hardcoded picks in
# routes/linkedin_content_engine.py:_card_url_for() so the FIRST post per
# topic still gets a sane style. Once the learner has a winner per topic it
# overrides this mapping.
COLD_START_BY_TOPIC = {
    "dcpi_verdict":      "data_brutal",
    "verdict_shift":     "data_brutal",
    "ai_capex":          "data_brutal",
    "market_brief":      "data_brutal",
    "grid_alert":        "data_brutal",
    "energy_pricing":    "editorial",
    "hyperscaler_deal":  "editorial",
    "ma_transaction":    "editorial",
    "fiber_route":       "editorial",
    "facility_news":     "editorial",
    "water_risk":        "editorial",
    "renewable_energy":  "editorial",
    "industry_pulse":    "editorial",
    "ai_citation":       "editorial",
    "other":             "editorial",
}


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
        sys.stderr.write(f"[style-ab] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _enabled() -> bool:
    """Master gate. The picker still RECORDS rows even when disabled
    (so observation still works); only the WIRING in og card builder
    consults _enabled() to decide whether to override the hardcoded map.
    """
    if (os.environ.get("MEDIA_STYLE_AB_DISABLE", "").lower()
            in ("1", "true", "yes")):
        return False
    return (os.environ.get("DCHUB_STYLE_AB_LEARNER_ENABLED", "").lower()
            in ("1", "true", "yes"))


def _epsilon() -> float:
    try:
        v = float(os.environ.get("MEDIA_STYLE_AB_EPSILON", "0.2"))
    except Exception:
        v = 0.2
    return max(0.0, min(1.0, v))


def _min_samples() -> int:
    """Minimum measured posts per (topic, style) before we leave the
    pure-explore phase. Default 3 → 5 styles * 3 = ~15 explore posts
    per topic before exploit kicks in."""
    try:
        return max(1, int(os.environ.get("MEDIA_STYLE_AB_MIN_SAMPLES", "3")))
    except Exception:
        return 3


# ── Schema ──────────────────────────────────────────────────────────────
def init_style_ab_tables() -> bool:
    """Idempotent schema bootstrap. Also added to schema_repair.py's
    SCHEMA_STATEMENTS so it's repaired on every admin/schema/repair run."""
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_style_outcomes (
                    id              BIGSERIAL PRIMARY KEY,
                    post_id         BIGINT,
                    post_urn        TEXT,
                    topic           TEXT NOT NULL,
                    style           TEXT NOT NULL,
                    published_at    TIMESTAMPTZ NOT NULL,
                    impressions     INTEGER,
                    reactions       INTEGER,
                    comments        INTEGER,
                    clicks          INTEGER,
                    score           REAL,
                    measured_at     TIMESTAMPTZ,
                    decision_phase  TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_mso_topic_style "
                "ON media_style_outcomes(topic, style)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_mso_published "
                "ON media_style_outcomes(published_at DESC)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_mso_post_urn "
                "ON media_style_outcomes(post_urn) "
                "WHERE post_urn IS NOT NULL")
        _log("schema bootstrap OK")
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


# Lazy init at import-time. content_publisher / schema_repair also call
# this on boot — belt-and-suspenders.
try:
    _SCHEMA_OK = init_style_ab_tables()
except Exception:
    _SCHEMA_OK = False


# ── Score formula ───────────────────────────────────────────────────────
def compute_score(impressions: Optional[int],
                  reactions: Optional[int],
                  comments: Optional[int],
                  clicks: Optional[int]) -> float:
    """Heavy weight on comments (they drive the LinkedIn algo), light
    weight on raw impressions (the algorithm rewards engagement-per-view,
    not reach alone)."""
    imp = float(impressions or 0)
    rea = float(reactions or 0)
    com = float(comments or 0)
    cli = float(clicks or 0)
    return com * 5.0 + rea * 2.0 + cli * 3.0 + imp * 0.01


# ── Aggregation ─────────────────────────────────────────────────────────
def topic_style_performance(topic: str, days: int = 30) -> list[dict]:
    """Return per-style stats for one topic, sorted by avg_score DESC.

    Rows have NULL impressions if engagement hasn't synced yet; those rows
    still count toward `posts` but contribute 0 to the score until measured.
    """
    conn = _db_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT style,
                       COUNT(*) AS posts,
                       COUNT(score) AS measured,
                       COALESCE(AVG(score), 0.0) AS avg_score,
                       COALESCE(AVG(impressions), 0) AS avg_imps,
                       COALESCE(AVG(comments), 0) AS avg_comments
                  FROM media_style_outcomes
                 WHERE topic = %s
                   AND published_at > NOW() - make_interval(days => %s)
                 GROUP BY style
            """, (topic, int(days)))
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "style":        str(r[0]),
                    "posts":        int(r[1] or 0),
                    "measured":     int(r[2] or 0),
                    "avg_score":    float(r[3] or 0.0),
                    "avg_imps":     float(r[4] or 0.0),
                    "avg_comments": float(r[5] or 0.0),
                })
            rows.sort(key=lambda r: r["avg_score"], reverse=True)
            return rows
    except Exception as e:
        _log(f"topic_style_performance({topic}) failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


def all_topics_performance(days: int = 30) -> dict[str, list[dict]]:
    """Per-topic performance for every topic that has any rows."""
    conn = _db_conn()
    if conn is None:
        return {}
    out: dict[str, list[dict]] = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT topic, style,
                       COUNT(*) AS posts,
                       COUNT(score) AS measured,
                       COALESCE(AVG(score), 0.0) AS avg_score,
                       COALESCE(AVG(impressions), 0) AS avg_imps,
                       COALESCE(AVG(comments), 0) AS avg_comments
                  FROM media_style_outcomes
                 WHERE published_at > NOW() - make_interval(days => %s)
                 GROUP BY topic, style
            """, (int(days),))
            for r in cur.fetchall():
                topic = str(r[0])
                out.setdefault(topic, []).append({
                    "style":        str(r[1]),
                    "posts":        int(r[2] or 0),
                    "measured":     int(r[3] or 0),
                    "avg_score":    float(r[4] or 0.0),
                    "avg_imps":     float(r[5] or 0.0),
                    "avg_comments": float(r[6] or 0.0),
                })
        for topic in out:
            out[topic].sort(key=lambda r: r["avg_score"], reverse=True)
        return out
    except Exception as e:
        _log(f"all_topics_performance failed: {e}")
        return {}
    finally:
        try: conn.close()
        except Exception: pass


# ── Decision logic ──────────────────────────────────────────────────────
def _decide_style(topic: str, perf: list[dict],
                  rng: Optional[random.Random] = None
                  ) -> tuple[str, str]:
    """Pure function: given the perf table for a topic, return
    (chosen_style, decision_phase) where phase is one of
    'cold_start' | 'explore' | 'exploit' | 'epsilon_explore'.

    Pulled out as a separate function so we can unit-test it without DB."""
    rng = rng or random.Random()

    # Cold start: zero rows at all → fall back to the hardcoded map.
    if not perf:
        return COLD_START_BY_TOPIC.get(topic, "editorial"), "cold_start"

    min_n = _min_samples()
    by_style = {p["style"]: p for p in perf}

    # Build the under-sampled pool (any pickable style below min_n).
    under_sampled = [s for s in PICKABLE_STYLES
                     if by_style.get(s, {}).get("measured", 0) < min_n]
    if under_sampled:
        # Pure explore — uniform pick across the under-sampled styles so we
        # GUARANTEE every style hits min_n measured before we leave explore.
        return rng.choice(under_sampled), "explore"

    # Exploit phase. Pickable styles (no fallback). Best measured score wins.
    candidates = [p for p in perf
                  if p["style"] in PICKABLE_STYLES and p["measured"] > 0]
    if not candidates:
        # Edge: all measured rows are for fallback. Fall back to cold start.
        return COLD_START_BY_TOPIC.get(topic, "editorial"), "cold_start"

    winner = max(candidates, key=lambda p: p["avg_score"])["style"]

    eps = _epsilon()
    if rng.random() < eps:
        # Epsilon-explore — random OTHER style so the winner can't get pinned.
        others = [s for s in PICKABLE_STYLES if s != winner]
        if others:
            return rng.choice(others), "epsilon_explore"
    return winner, "exploit"


def _next_pick_distribution(topic: str, perf: list[dict]) -> dict[str, float]:
    """For dashboards — what's the prob of each style being picked next?"""
    if not perf:
        cs = COLD_START_BY_TOPIC.get(topic, "editorial")
        d = {s: 0.0 for s in PICKABLE_STYLES}
        if cs in d:
            d[cs] = 1.0
        return d

    min_n = _min_samples()
    by_style = {p["style"]: p for p in perf}
    under_sampled = [s for s in PICKABLE_STYLES
                     if by_style.get(s, {}).get("measured", 0) < min_n]
    if under_sampled:
        n = len(under_sampled)
        return {s: (1.0 / n if s in under_sampled else 0.0)
                for s in PICKABLE_STYLES}

    eps = _epsilon()
    candidates = [p for p in perf
                  if p["style"] in PICKABLE_STYLES and p["measured"] > 0]
    if not candidates:
        cs = COLD_START_BY_TOPIC.get(topic, "editorial")
        return {s: (1.0 if s == cs else 0.0) for s in PICKABLE_STYLES}
    winner = max(candidates, key=lambda p: p["avg_score"])["style"]
    others = [s for s in PICKABLE_STYLES if s != winner]
    n_oth = len(others)
    return {
        s: ((1.0 - eps) if s == winner
            else (eps / n_oth if n_oth else 0.0))
        for s in PICKABLE_STYLES
    }


# ── Decision-recording helper ───────────────────────────────────────────
def _record_decision(topic: str, style: str, phase: str,
                     post_urn: Optional[str] = None,
                     post_id: Optional[int] = None,
                     published_at: Optional[datetime.datetime] = None
                     ) -> bool:
    """Insert a decision row with NULL engagement metrics. The
    linkedin_engagement_sync backfill flips score / impressions / etc.
    later. Fail-soft: returns False on any DB issue."""
    conn = _db_conn()
    if conn is None:
        return False
    pub = published_at or datetime.datetime.utcnow()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO media_style_outcomes
                  (post_id, post_urn, topic, style, published_at,
                   decision_phase)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (post_id, post_urn, topic, style, pub, phase))
        return True
    except Exception as e:
        _log(f"_record_decision({topic},{style}) failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ── Public picker ───────────────────────────────────────────────────────
def pick_style_for_topic(topic: str,
                         post_urn: Optional[str] = None,
                         post_id: Optional[int] = None,
                         record: bool = True
                         ) -> str:
    """The main entry point. Given a topic, pick a style using the
    epsilon-greedy A/B learner. Records the decision unless record=False.

    SAFE FALLBACK CHAIN: any exception → COLD_START_BY_TOPIC lookup →
    'editorial'. So this never raises and never returns an invalid style.
    """
    try:
        if not topic:
            return "editorial"
        perf = topic_style_performance(topic, days=30)
        style, phase = _decide_style(topic, perf)
        if style not in ALL_KNOWN_STYLES:
            style = "editorial"
        if record:
            _record_decision(topic, style, phase,
                             post_urn=post_urn, post_id=post_id)
        return style
    except Exception as e:
        _log(f"pick_style_for_topic({topic}) failed: {e}")
        return COLD_START_BY_TOPIC.get(topic or "", "editorial")


def pick_style_if_enabled(topic: str,
                          fallback: str,
                          post_urn: Optional[str] = None,
                          post_id: Optional[int] = None
                          ) -> str:
    """Wiring helper for _card_url_for() in linkedin_content_engine.py.
    If the learner is gated OFF (DCHUB_STYLE_AB_LEARNER_ENABLED unset),
    returns the hardcoded fallback so existing behaviour is unchanged.

    Even when OFF the picker would still observe — but we don't record
    in this code path because the hardcoded fallback is what actually
    ran, and recording a different style would lie about the post."""
    if not _enabled():
        return fallback
    return pick_style_for_topic(topic, post_urn=post_urn,
                                post_id=post_id, record=True)


# ── Engagement backfill ─────────────────────────────────────────────────
def backfill_engagement(min_age_hours: int = 24) -> dict:
    """For every media_style_outcomes row with NULL score whose post is
    at least min_age_hours old, pull the engagement from linkedin_posts
    (matched by post_urn) and recompute score.

    Called by linkedin_engagement_sync cron AFTER fetch_linkedin_engagement
    + fetch_linkedin_impressions have populated linkedin_posts.

    Returns: {checked, updated, errors}."""
    out = {"checked": 0, "updated": 0, "errors": 0}
    conn = _db_conn()
    if conn is None:
        out["error"] = "no_db"
        return out
    try:
        with conn, conn.cursor() as cur:
            # Find outcome rows whose post is at least min_age_hours old
            # AND whose score isn't filled in yet AND whose URN matches a
            # linkedin_posts row with engagement data populated.
            cur.execute("""
                SELECT o.id, o.post_urn,
                       COALESCE(p.impressions, 0),
                       COALESCE(p.likes, 0),
                       COALESCE(p.comments, 0),
                       COALESCE(p.clicks, 0)
                  FROM media_style_outcomes o
                  JOIN linkedin_posts p ON p.post_urn = o.post_urn
                 WHERE o.post_urn IS NOT NULL
                   AND o.post_urn <> ''
                   AND o.score IS NULL
                   AND o.published_at < NOW() - make_interval(
                            hours => %s)
                   AND p.engagement_fetched_at IS NOT NULL
            """, (int(min_age_hours),))
            rows = list(cur.fetchall())
        out["checked"] = len(rows)
        if not rows:
            return out

        with conn, conn.cursor() as cur:
            for r in rows:
                try:
                    oid = int(r[0])
                    imps = int(r[2] or 0)
                    rea  = int(r[3] or 0)
                    com  = int(r[4] or 0)
                    cli  = int(r[5] or 0)
                    score = compute_score(imps, rea, com, cli)
                    cur.execute("""
                        UPDATE media_style_outcomes
                           SET impressions = %s,
                               reactions   = %s,
                               comments    = %s,
                               clicks      = %s,
                               score       = %s,
                               measured_at = NOW()
                         WHERE id = %s
                    """, (imps, rea, com, cli, score, oid))
                    out["updated"] += 1
                except Exception as e:
                    _log(f"backfill row {r[0]} failed: {e}")
                    out["errors"] += 1
        return out
    except Exception as e:
        _log(f"backfill_engagement failed: {e}")
        out["error"] = str(e)[:200]
        return out
    finally:
        try: conn.close()
        except Exception: pass


# ── Historical backfill (one-time) ──────────────────────────────────────
def backfill_history(days: int = 30) -> dict:
    """One-time seed: scan last N days of linkedin_posts, derive the
    style from the post_urn → linkedin_posts.content (style is encoded
    in the og_image_url) — or fall back to today's-rotation style from
    og_cards.DAILY_STYLES based on the posted weekday. Tag with topic
    via media_topic_tuner.classify_topics(content).

    Idempotent — skips rows already in media_style_outcomes by post_urn.
    Run via POST /api/v1/admin/media/style-ab/backfill?days=30 once."""
    out = {"seen": 0, "seeded": 0, "skipped_no_urn": 0,
           "skipped_dup": 0, "skipped_no_topic": 0, "errors": 0}

    # Topic classifier (fail-soft — if the module isn't importable, we
    # tag every row as 'other').
    try:
        from routes.media_topic_tuner import classify_topics  # noqa: WPS433
    except Exception:
        def classify_topics(_):  # type: ignore[no-redef]
            return []

    # Day-of-week → style (matches the existing rotation in og_cards.py).
    # Sunday=6, Saturday=5, Friday=4, etc.
    DOW_STYLES = {
        0: "data_brutal",
        1: "editorial",
        2: "infographic",
        3: "ai_hero",
        4: "data_brutal",
        5: "editorial",
        6: "infographic",
    }

    conn = _db_conn()
    if conn is None:
        out["error"] = "no_db"
        return out
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id, post_urn, content, post_type, posted_at,
                       COALESCE(impressions, 0),
                       COALESCE(likes, 0),
                       COALESCE(comments, 0),
                       COALESCE(clicks, 0)
                  FROM linkedin_posts
                 WHERE COALESCE(status, '') = 'success'
                   AND posted_at > NOW() - make_interval(days => %s)
                 ORDER BY posted_at DESC
            """, (int(days),))
            rows = list(cur.fetchall())
        out["seen"] = len(rows)
        if not rows:
            return out

        # Dedup against existing outcomes.
        existing_urns: set[str] = set()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT post_urn
                      FROM media_style_outcomes
                     WHERE post_urn IS NOT NULL
                """)
                existing_urns = {str(r[0]) for r in cur.fetchall()}
        except Exception:
            pass

        with conn, conn.cursor() as cur:
            for r in rows:
                try:
                    pid = int(r[0])
                    urn = str(r[1] or "")
                    content = str(r[2] or "")
                    posted = r[4]
                    imps = int(r[5] or 0)
                    rea  = int(r[6] or 0)
                    com  = int(r[7] or 0)
                    cli  = int(r[8] or 0)
                    if not urn:
                        out["skipped_no_urn"] += 1
                        continue
                    if urn in existing_urns:
                        out["skipped_dup"] += 1
                        continue

                    # Topic — first match wins (the per-row picker only
                    # records ONE topic; if a post matched multiple we
                    # take the first to keep aggregation clean).
                    topics = classify_topics(content) or []
                    topic = topics[0] if topics else "other"
                    if not topic:
                        out["skipped_no_topic"] += 1
                        continue

                    # Style — derive from posted weekday (matches the
                    # rotation that actually shipped). Sunday is index 6.
                    style = "editorial"
                    try:
                        style = DOW_STYLES.get(posted.weekday(), "editorial")
                    except Exception:
                        pass

                    score = compute_score(imps, rea, com, cli)
                    measured_at = (None if imps + rea + com + cli == 0
                                   else datetime.datetime.utcnow())

                    cur.execute("""
                        INSERT INTO media_style_outcomes
                          (post_id, post_urn, topic, style, published_at,
                           impressions, reactions, comments, clicks,
                           score, measured_at, decision_phase)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s)
                    """, (pid, urn, topic, style, posted,
                          imps, rea, com, cli, score, measured_at,
                          "backfill"))
                    out["seeded"] += 1
                except Exception as e:
                    _log(f"backfill_history row {r[0]} failed: {e}")
                    out["errors"] += 1
        return out
    except Exception as e:
        _log(f"backfill_history failed: {e}")
        out["error"] = str(e)[:200]
        return out
    finally:
        try: conn.close()
        except Exception: pass


# ── HTTP endpoints ──────────────────────────────────────────────────────

@media_style_ab_bp.route("/api/v1/admin/media/style-ab", methods=["GET"])
def http_style_ab_status():
    """Per-topic style performance + next-pick distribution.

    Query params:
      days   — engagement window (default 30)
      topic  — filter to one topic (optional)
    """
    if not _admin_or_cron_authorized():
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = int(request.args.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    one_topic = (request.args.get("topic") or "").strip().lower()

    all_perf = all_topics_performance(days=days)
    topics_in_play = list(COLD_START_BY_TOPIC.keys())
    # Union of cold-start topics + any topic that has data (e.g. "other").
    for t in all_perf:
        if t not in topics_in_play:
            topics_in_play.append(t)

    if one_topic:
        topics_in_play = [t for t in topics_in_play if t == one_topic]

    rows = []
    for topic in topics_in_play:
        perf = all_perf.get(topic, [])
        # Mark the winner — highest avg_score among PICKABLE_STYLES with
        # any measured rows. None if pure cold start.
        candidates = [p for p in perf
                      if p["style"] in PICKABLE_STYLES and p["measured"] > 0]
        winner_style = (max(candidates, key=lambda p: p["avg_score"])["style"]
                        if candidates else None)
        samples = []
        for p in perf:
            samples.append({**p, "winner": p["style"] == winner_style})
        rows.append({
            "topic":                    topic,
            "topic_label":              _topic_label(topic),
            "samples":                  samples,
            "winner":                   winner_style,
            "cold_start_default":       COLD_START_BY_TOPIC.get(
                topic, "editorial"),
            "next_pick_distribution":   _next_pick_distribution(topic, perf),
            "phase":                    _phase_for(topic, perf),
        })

    return jsonify(
        ok=True,
        enabled=_enabled(),
        epsilon=_epsilon(),
        min_samples=_min_samples(),
        days=days,
        pickable_styles=list(PICKABLE_STYLES),
        n_topics=len(rows),
        topics=rows,
    )


def _topic_label(topic: str) -> str:
    """Pretty name for a topic slug (sourced from media_topic_tuner)."""
    try:
        from routes.media_topic_tuner import TOPIC_LIBRARY  # noqa: WPS433
        for e in TOPIC_LIBRARY:
            if e.get("topic") == topic:
                return e.get("name", topic)
    except Exception:
        pass
    return topic.replace("_", " ").title()


def _phase_for(topic: str, perf: list[dict]) -> str:
    if not perf:
        return "cold_start"
    by_style = {p["style"]: p for p in perf}
    min_n = _min_samples()
    if any(by_style.get(s, {}).get("measured", 0) < min_n
           for s in PICKABLE_STYLES):
        return "explore"
    return "exploit"


@media_style_ab_bp.route("/api/v1/admin/media/style-ab/backfill",
                         methods=["POST"])
def http_style_ab_backfill():
    """One-time backfill from linkedin_posts. Safe to re-run (dedup
    on post_urn). Query: days=30."""
    if not _admin_or_cron_authorized():
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = int(request.args.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    result = backfill_history(days=days)
    return jsonify(ok=True, days=days, **result)


@media_style_ab_bp.route("/api/v1/admin/media/style-ab/pick",
                         methods=["GET"])
def http_style_ab_pick():
    """Diagnostic: what style would pick_style_for_topic(topic) return
    right now? Does NOT record a decision (so an operator hammering the
    endpoint doesn't poison the dataset).

    Query: topic=market_brief (required)."""
    if not _admin_or_cron_authorized():
        return jsonify(ok=False, error="forbidden"), 403
    topic = (request.args.get("topic") or "").strip().lower()
    if not topic:
        return jsonify(ok=False, error="missing_topic"), 400
    perf = topic_style_performance(topic, days=30)
    style, phase = _decide_style(topic, perf)
    return jsonify(
        ok=True, topic=topic,
        topic_label=_topic_label(topic),
        chosen_style=style,
        phase=phase,
        epsilon=_epsilon(),
        enabled=_enabled(),
        next_pick_distribution=_next_pick_distribution(topic, perf),
        samples=perf,
    )


@media_style_ab_bp.route("/api/v1/admin/media/style-ab/sync",
                         methods=["POST"])
def http_style_ab_engagement_sync():
    """Run backfill_engagement() — pulls engagement from linkedin_posts
    onto outcome rows >= 24h old. Called by linkedin_engagement_sync
    cron AFTER the LinkedIn API refresh. Manually triggerable here."""
    if not _admin_or_cron_authorized():
        return jsonify(ok=False, error="forbidden"), 403
    try:
        min_age = int(request.args.get("min_age_hours", 24))
    except (ValueError, TypeError):
        min_age = 24
    result = backfill_engagement(min_age_hours=min_age)
    return jsonify(ok=True, min_age_hours=min_age, **result)


# ── Admin HTML fragment for /admin/media-mix dashboard ──────────────────
def render_style_ab_card(days: int = 30) -> str:
    """Render the /admin/media-mix "Style A/B" tile. Compact 5-style
    leaderboard per topic + global mode indicator. Returns HTML.
    Empty string on any failure (so the dashboard still loads)."""
    try:
        all_perf = all_topics_performance(days=days)
        # Tile only for topics with at least 1 row OR cold-start defaults.
        topics_list = sorted(set(list(COLD_START_BY_TOPIC.keys())
                                 + list(all_perf.keys())))

        mode_pill = ("LIVE" if _enabled() else "OBSERVE")
        badge = ("background:#0e2a1c;color:#7cf3a0" if _enabled()
                 else "background:#3b2e0a;color:#ffd166")
        eps = _epsilon()
        min_n = _min_samples()

        body_rows: list[str] = []
        for topic in topics_list:
            perf = all_perf.get(topic, [])
            phase = _phase_for(topic, perf)
            phase_color = {
                "cold_start": "#97a3b6",
                "explore":    "#ffd166",
                "exploit":    "#7cf3a0",
            }.get(phase, "#97a3b6")
            label = _topic_label(topic)
            by_style = {p["style"]: p for p in perf}

            # Per-style cells (5 columns; "—" if no measurement yet).
            cells: list[str] = []
            candidates = [p for p in perf
                          if p["style"] in PICKABLE_STYLES
                          and p["measured"] > 0]
            winner = (max(candidates, key=lambda p: p["avg_score"])["style"]
                      if candidates else None)
            for s in PICKABLE_STYLES:
                p = by_style.get(s)
                if not p or p["measured"] == 0:
                    cells.append("<td class='num muted'>—</td>")
                    continue
                star = (" <span style='color:#ffd166'>★</span>"
                        if s == winner else "")
                cells.append(
                    f"<td class='num'>{p['avg_score']:.1f}{star}"
                    f"<br><span class='muted' style='font-size:10px'>"
                    f"n={p['measured']}</span></td>"
                )
            cold = COLD_START_BY_TOPIC.get(topic, "editorial")
            shown_winner = winner or f"({cold}*)"
            body_rows.append(
                f"<tr><td>{label}</td>"
                f"<td><span style='color:{phase_color}'>{phase}</span></td>"
                f"{''.join(cells)}"
                f"<td><b>{shown_winner}</b></td></tr>"
            )

        body = "".join(body_rows) or (
            "<tr><td colspan=8 class='muted'>"
            "No style outcomes seeded yet — run "
            "<code>POST /api/v1/admin/media/style-ab/backfill?days=30</code>."
            "</td></tr>"
        )

        return f"""
<h2>Style A/B Learner <span class="pill" style="{badge}">{mode_pill}</span></h2>
<div class="kpi-row">
  <div class="kpi"><span>Mode</span><b>{mode_pill}</b></div>
  <div class="kpi"><span>Epsilon</span><b>{eps:.2f}</b></div>
  <div class="kpi"><span>Min samples / cell</span><b>{min_n}</b></div>
  <div class="kpi"><span>Window</span><b>{days}d</b></div>
</div>
<p class="muted" style="font-size:12px">
  Each row = a topic. Each cell = avg engagement score
  (comments*5 + reactions*2 + clicks*3 + impressions*0.01) for that
  style, with sample count below. <b>★</b> marks the current winner;
  <code>(style*)</code> in Winner col = cold-start default until a
  topic has measured rows. Phase:
  <span style="color:#97a3b6">cold_start</span> →
  <span style="color:#ffd166">explore</span> →
  <span style="color:#7cf3a0">exploit</span>.
</p>
<table><thead><tr>
  <th>Topic</th><th>Phase</th>
  <th class='num'>data_brutal</th>
  <th class='num'>editorial</th>
  <th class='num'>infographic</th>
  <th class='num'>ai_hero</th>
  <th>Winner</th>
</tr></thead>
<tbody>{body}</tbody></table>
<p class="muted" style="font-size:12px">
  <code>DCHUB_STYLE_AB_LEARNER_ENABLED=1</code> activates the picker.
  <code>MEDIA_STYLE_AB_EPSILON=0.2</code> tunes explore rate.
  <code>MEDIA_STYLE_AB_DISABLE=1</code> kill switch.
  Sync engagement: <code>POST /api/v1/admin/media/style-ab/sync</code>.
</p>"""
    except Exception as e:
        _log(f"render_style_ab_card failed: {e}")
        return ""


def register_media_style_ab(app):
    """Standalone register helper (main.py uses register_blueprint directly,
    this is for any auxiliary surface that wants to mount us)."""
    app.register_blueprint(media_style_ab_bp)
    return media_style_ab_bp
