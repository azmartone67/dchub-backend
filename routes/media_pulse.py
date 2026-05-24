"""Phase DDD (2026-05-16) — DCHub Media as a living organism.

Two endpoints turn the auto-press machine from a one-way publisher
into a feedback-driven citizen of the news ecosystem:

  1. /api/v1/media/source-of-truth
     Are WE the source agents + journalists cite for data-center
     intelligence? Aggregates AI citations (from ai_citations table)
     + brand mentions in news.title/summary + auto-press freshness.
     Returns a 0-100 "source-of-truth score" and a 30-day trend.

  2. /api/v1/media/topic-pulse
     What should we publish NEXT? Reads the news table for industry
     stories from the last 24-48h, finds intersections with our DCPI
     markets / facilities / pipelines, and surfaces 1-5 ranked topic
     suggestions with the suggested angle ("DCK reports X; our data
     shows Y — write the comparison piece").

Brain consumes both for autonomous remediation:
  - source_of_truth_decline (>25% drop wow)         → escalation
  - media_topic_unaddressed (hot topic, no response) → auto-fire
    /api/v1/marketing/auto-generate with the topic id

Together with mcp_growth.py, this turns Media from "we publish daily"
into "we are the canonical voice in the data center conversation."
"""

from __future__ import annotations

import os
import json
import datetime
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


media_pulse_bp = Blueprint("media_pulse", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS media_pulse_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_date     DATE NOT NULL,
    source_of_truth_score INT NOT NULL DEFAULT 0,
    ai_citations_7d   INT NOT NULL DEFAULT 0,
    news_mentions_7d  INT NOT NULL DEFAULT 0,
    auto_press_7d     INT NOT NULL DEFAULT 0,
    unique_markets_in_press_7d INT NOT NULL DEFAULT 0,
    payload           JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_media_pulse_snapshot_date
    ON media_pulse_snapshots(snapshot_date DESC);
"""

def _ensure_schema():
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur: cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[media_pulse] schema: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass

try: _ensure_schema()
except Exception: pass


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    return not expected or provided == expected


# ── Source-of-truth scoring ───────────────────────────────────────────
def _compute_source_of_truth() -> dict:
    """Compose a 0-100 score from the three signals available:
      - AI citations (our own ai_citations table) — how often LLMs cite us
      - Brand mentions in news (news.title + summary containing "DC Hub")
      - Auto-press freshness (releases per week as proxy for activity)
    Each contributes ~33 pts. Capped at 100. Honest reading of where
    we actually stand as a 'source'."""
    out = {
        "score":                    0,
        "ai_citations_7d":          0,
        "ai_cited_pct":             0.0,
        "news_mentions_7d":         0,
        "news_mentions_30d":        0,
        "auto_press_7d":            0,
        "unique_markets_in_press_7d": 0,
        "competitors_mentioned_7d": {"dchawk": 0, "dcbyte": 0},
        "computed_at":              datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _conn()
    if c is None: return out
    try:
        with c.cursor() as cur:
            # AI citations
            try:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE observed_at >= NOW() - INTERVAL '7 days') AS total,
                           COUNT(*) FILTER (WHERE dchub_cited = true
                                             AND observed_at >= NOW() - INTERVAL '7 days') AS cited
                      FROM ai_citations
                """)
                r = cur.fetchone()
                if r:
                    total_obs = int(r[0] or 0)
                    cited = int(r[1] or 0)
                    out["ai_citations_7d"] = cited
                    if total_obs > 0:
                        out["ai_cited_pct"] = round(100.0 * cited / total_obs, 1)
            except Exception:
                pass

            # News mentions of our brand (rough — title/summary substring)
            try:
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE published_date >= NOW() - INTERVAL '7 days') AS w7,
                      COUNT(*) FILTER (WHERE published_date >= NOW() - INTERVAL '30 days') AS w30
                      FROM news
                     WHERE (LOWER(COALESCE(title,'')) LIKE %s
                            OR LOWER(COALESCE(summary,'')) LIKE %s)
                """, ('%dc hub%', '%dc hub%'))
                r = cur.fetchone()
                if r:
                    out["news_mentions_7d"]  = int(r[0] or 0)
                    out["news_mentions_30d"] = int(r[1] or 0)
            except Exception:
                pass

            # Competitor mentions in same news pool — gives us a ratio
            for comp, name_low in (("dchawk", "datacenterhawk"), ("dcbyte", "dcbyte")):
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM news
                         WHERE published_date >= NOW() - INTERVAL '7 days'
                           AND (LOWER(COALESCE(title,'')) LIKE %s
                                OR LOWER(COALESCE(summary,'')) LIKE %s)
                    """, (f"%{name_low}%", f"%{name_low}%"))
                    out["competitors_mentioned_7d"][comp] = int((cur.fetchone() or [0])[0] or 0)
                except Exception:
                    pass

            # Auto-press freshness
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM auto_press_releases
                     WHERE generated_for >= CURRENT_DATE - INTERVAL '7 days'
                """)
                out["auto_press_7d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                pass

            # Unique markets covered in press last 7d (diversity)
            try:
                import re as _re
                cur.execute("""
                    SELECT title FROM auto_press_releases
                     WHERE generated_for >= CURRENT_DATE - INTERVAL '7 days'
                       AND title IS NOT NULL
                """)
                seen = set()
                for r in cur.fetchall():
                    m = _re.match(r"^([A-Z][a-zA-Z\.\- ]+?)(?:,| Metro|:| - | – | Leads| Tops| Takes)", r[0] or "")
                    if m: seen.add(m.group(1).strip().lower())
                out["unique_markets_in_press_7d"] = len(seen)
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass

    # Compose score
    score = 0
    # AI citations: 0-35 pts
    cited = out["ai_citations_7d"]
    if   cited >= 30: score += 35
    elif cited >= 10: score += 25
    elif cited >= 3:  score += 15
    elif cited >= 1:  score += 5
    # News mentions: 0-35 pts (vs competitor baseline)
    mentions = out["news_mentions_7d"]
    if   mentions >= 10: score += 35
    elif mentions >= 5:  score += 25
    elif mentions >= 2:  score += 15
    elif mentions >= 1:  score += 5
    # Auto-press health: 0-30 pts (7+ per week with 5+ unique markets = full)
    aw = out["auto_press_7d"]
    uw = out["unique_markets_in_press_7d"]
    if   aw >= 7 and uw >= 5: score += 30
    elif aw >= 5 and uw >= 3: score += 20
    elif aw >= 3:              score += 10
    elif aw >= 1:              score += 5
    out["score"] = max(0, min(100, score))

    # Score interpretation
    if   score >= 80: out["interpretation"] = "Dominant source — agents + journalists cite us by name"
    elif score >= 60: out["interpretation"] = "Strong source — visible but room to grow citation share"
    elif score >= 40: out["interpretation"] = "Emerging source — gaining traction but still under-cited"
    elif score >= 20: out["interpretation"] = "Niche source — visible in our category, not yet mainstream"
    else:             out["interpretation"] = "Invisible — citations + mentions are anemic; growth investments needed"

    return out


@media_pulse_bp.route("/api/v1/media/source-of-truth", methods=["GET"])
def api_source_of_truth():
    """Public — the canonical 'are we the source?' probe."""
    payload = _compute_source_of_truth()
    # Trend from prior snapshot
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT source_of_truth_score, snapshot_date
                      FROM media_pulse_snapshots
                     WHERE snapshot_date < CURRENT_DATE - INTERVAL '6 days'
                     ORDER BY snapshot_date DESC LIMIT 1
                """)
                r = cur.fetchone()
                if r and r[0] is not None:
                    prev = int(r[0])
                    delta = payload["score"] - prev
                    payload["score_wow_delta"] = delta
                    payload["score_7d_ago"]    = prev
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@media_pulse_bp.route("/api/v1/media/source-of-truth/snapshot", methods=["POST"])
def api_source_of_truth_snapshot():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    payload = _compute_source_of_truth()
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO media_pulse_snapshots
                    (snapshot_date, source_of_truth_score, ai_citations_7d,
                     news_mentions_7d, auto_press_7d, unique_markets_in_press_7d,
                     payload)
                VALUES (CURRENT_DATE, %s,%s,%s,%s,%s, %s::jsonb)
                ON CONFLICT DO NOTHING
            """, (
                payload["score"], payload["ai_citations_7d"],
                payload["news_mentions_7d"], payload["auto_press_7d"],
                payload["unique_markets_in_press_7d"],
                json.dumps(payload, default=str),
            ))
        return jsonify(ok=True, persisted=True, snapshot=payload), 200
    finally:
        try: c.close()
        except Exception: pass


# ── Topic pulse — what should we publish next? ────────────────────────
@media_pulse_bp.route("/api/v1/media/topic-pulse", methods=["GET"])
def api_topic_pulse():
    """Reads news from last 48h, finds intersection with DCPI markets +
    pipeline projects + facilities, ranks topic suggestions. The
    media organism's 'next move' inference."""
    out: dict = {
        "topic_suggestions":   [],
        "news_last_48h":       0,
        "_window_days":        7,  # r34e+1 explicit so callers know the math
        "computed_at":         datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _conn()
    if c is None: return jsonify(out), 200
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 2026-05-24 r34e+1: per-table counts (UNION was failing
            # atomically on any single bad subquery, dropping all 3
            # counts to 0). Sum across news + news_articles + announcements,
            # each wrapped in its own try/except. Widened to 7d so a
            # quiet news cycle doesn't blank the surface.
            for tbl_q in (
                "SELECT COUNT(*) FROM news "
                "WHERE published_date >= NOW() - INTERVAL '7 days'",
                "SELECT COUNT(*) FROM news_articles "
                "WHERE published_at >= NOW() - INTERVAL '7 days'",
                "SELECT COUNT(*) FROM announcements "
                "WHERE COALESCE(published_at, published_date) "
                "      >= NOW() - INTERVAL '7 days'",
            ):
                try:
                    cur.execute(tbl_q)
                    n_row = cur.fetchone() or {"count": 0}
                    out["news_last_48h"] += int(n_row.get("count") or 0)
                except Exception:
                    # Per-source try/except — one missing column / table
                    # can't zero out the others. Rollback the failed
                    # transaction so the next query can run.
                    try: c.rollback()
                    except Exception: pass

            # Pull recent headlines from ALL three tables.
            news_items: list = []
            for tbl, date_col in (
                ("news",          "published_date"),
                ("news_articles", "published_at"),
                ("announcements", "COALESCE(published_at, published_date)"),
            ):
                try:
                    cur.execute(f"""
                        SELECT title, COALESCE(summary, '') AS summary,
                               COALESCE(source, '') AS source,
                               {date_col} AS published_date,
                               COALESCE(url, '') AS url
                          FROM {tbl}
                         WHERE {date_col} >= NOW() - INTERVAL '7 days'
                           AND title IS NOT NULL
                         ORDER BY {date_col} DESC LIMIT 200
                    """)
                    news_items.extend(cur.fetchall())
                except Exception:
                    continue
            # Dedup by title to avoid double-matching the same story
            seen_titles: set = set()
            unique_items: list = []
            for n in news_items:
                t = (n.get("title") or "").strip().lower()
                if t and t not in seen_titles:
                    seen_titles.add(t)
                    unique_items.append(n)
            news_items = unique_items[:300]

            # Pull DCPI market names for intersection
            market_names = {}
            try:
                cur.execute("""
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, state, verdict,
                           excess_power_score, constraint_score
                      FROM market_power_scores
                     WHERE published = true
                     ORDER BY market_slug, computed_at DESC
                """)
                for r in cur.fetchall():
                    nm = (r["market_name"] or "").lower()
                    if nm: market_names[nm] = r
            except Exception:
                pass

        # Match news to markets
        topic_hits = {}  # market_slug → {market, news[], excess, constraint, verdict}
        for n in news_items:
            text = (((n["title"] or "") + " " + (n["summary"] or "")).lower())
            for mn, market in market_names.items():
                # Match on full market name OR first word (e.g. "Ashburn" matches "Ashburn, VA")
                if mn in text or (len(mn.split()) > 1 and mn.split()[0] in text):
                    slug = market["market_slug"]
                    if slug not in topic_hits:
                        topic_hits[slug] = {
                            "market_slug":   slug,
                            "market_name":   market["market_name"],
                            "state":         market["state"],
                            "verdict":       market["verdict"],
                            "excess_power":  market["excess_power_score"],
                            "constraint":    market["constraint_score"],
                            "news_items":    [],
                        }
                    topic_hits[slug]["news_items"].append({
                        "title":  n["title"][:200] if n["title"] else "",
                        "source": n["source"],
                        "url":    n["url"],
                    })

        # Rank by news_volume × verdict-newsworthiness
        ranked = []
        for slug, info in topic_hits.items():
            news_count = len(info["news_items"])
            verdict_weight = 3 if info["verdict"] == "BUILD" else (
                            2 if info["verdict"] == "AVOID" else 1)
            score = news_count * verdict_weight
            ranked.append({
                "market_slug":   slug,
                "market_name":   info["market_name"],
                "state":         info["state"],
                "verdict":       info["verdict"],
                "excess_power":  info["excess_power"],
                "news_count":    news_count,
                "leverage_score": score,
                "suggested_title": _suggest_title(info),
                "sample_news":   info["news_items"][:3],
            })
        ranked.sort(key=lambda x: -x["leverage_score"])
        out["topic_suggestions"] = ranked[:10]
    finally:
        try: c.close()
        except Exception: pass

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def _suggest_title(info: dict) -> str:
    """Compose a plausible press-release title for a market+news topic."""
    market = info["market_name"] or "Unspecified Market"
    verdict = (info["verdict"] or "LOW_SIGNAL").upper()
    if verdict == "BUILD":
        return f"{market} in the News: DC Hub DCPI Confirms BUILD Verdict with {info['excess_power']} Excess Power Score"
    if verdict == "AVOID":
        return f"{market} Headlines: DC Hub Data Shows Why AVOID Verdict Holds (Constraint Score {info['constraint']})"
    return f"{market} in the Headlines: DC Hub DCPI Tracks the Story with Live Power-Market Data"
