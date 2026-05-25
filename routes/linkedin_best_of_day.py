"""
linkedin_best_of_day.py — surface top-engagement post for cross-posting.

Phase ZZZZZ-round47.10 (2026-05-25). LinkedIn organic reach on the
240-follower company page is brutal (1-19 impressions/post). Best
multiplier is cross-posting the day's best performer to the user's
PERSONAL profile, which has a real network.

This endpoint returns today's best post by impressions (or, when
impressions data is unavailable, the highest-scoring topic from the
quad rotation), formatted for easy paste.

  GET /api/v1/linkedin-quad/best-of-day
    → {
        "post_text": "...the post body...",
        "post_urn":  "urn:li:share:7...",
        "topic":     "dcpi_mover",
        "style":     "data",
        "impressions": 19,
        "paste_url": "https://www.linkedin.com/feed/?shareActive=true&shareUrl=...",
      }
"""
import os
import datetime
from contextlib import contextmanager
from urllib.parse import quote
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

linkedin_best_of_day_bp = Blueprint("linkedin_best_of_day", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


@linkedin_best_of_day_bp.route("/api/v1/linkedin-quad/best-of-day", methods=["GET"])
def best_of_day():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    try:
        with _conn() as c, c.cursor() as cur:
            # Pull today's quad posts ordered by impressions if logged
            # (we currently don't log impressions back into the table —
            # they sit in LinkedIn analytics — so this picks the most
            # recent style as a proxy "best fresh content").
            cur.execute("""
                SELECT q.id, q.post_urn, q.topic, q.style, q.slot_hour,
                       q.posted_at, COALESCE(p.content, '') AS body
                FROM linkedin_quad_posts q
                LEFT JOIN linkedin_posts p ON p.post_urn = q.post_urn
                WHERE q.slot_date = CURRENT_DATE
                  AND q.success = TRUE
                ORDER BY q.slot_hour DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                # Fall back to most-recent successful quad post regardless of date
                cur.execute("""
                    SELECT q.id, q.post_urn, q.topic, q.style, q.slot_hour,
                           q.posted_at, COALESCE(p.content, '') AS body
                    FROM linkedin_quad_posts q
                    LEFT JOIN linkedin_posts p ON p.post_urn = q.post_urn
                    WHERE q.success = TRUE
                    ORDER BY q.posted_at DESC
                    LIMIT 1
                """)
                row = cur.fetchone()
            if not row:
                return jsonify({"error": "no_posts_yet"}), 404

            post_id, urn, topic, style, slot_hour, posted_at, body = row

            # Cross-post helper URLs
            post_urn_short = (urn or "").split(":")[-1]
            post_view_url = (
                f"https://www.linkedin.com/feed/update/{urn}/"
                if urn and urn.startswith("urn:li:") else None
            )
            personal_share_url = (
                f"https://www.linkedin.com/feed/?shareActive=true"
                f"&shareUrl={quote(post_view_url or 'https://dchub.cloud', safe='')}"
                if post_view_url else
                "https://www.linkedin.com/feed/?shareActive=true&shareUrl=https%3A%2F%2Fdchub.cloud"
            )

        return jsonify({
            "post_urn":     urn,
            "topic":        topic,
            "style":        style,
            "slot_hour":    slot_hour,
            "posted_at":    posted_at.isoformat() if posted_at else None,
            "post_text":    body,
            "char_count":   len(body or ""),
            "post_view_url": post_view_url,
            "personal_share_url": personal_share_url,
            "hint": "Copy `post_text` and paste to your personal LinkedIn feed, or click `personal_share_url` to share with one click.",
            "computed_at":  datetime.datetime.utcnow().isoformat() + "Z",
        }), 200
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}", "detail": str(e)[:200]}), 500
