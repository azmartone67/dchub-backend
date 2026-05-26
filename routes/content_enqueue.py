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
         bluesky) on every call. Idempotent within a 4-hour window
         (so the cron firing every 2h enqueues fresh content but
         doesn't spam the queue if something stalls).

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

import datetime
import json
import os
import random

from flask import Blueprint, jsonify, request


content_enqueue_bp = Blueprint("content_enqueue", __name__)


# Don't enqueue the same (platform, topic_key) twice within this window
_DEDUP_WINDOW_HOURS = 4


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
    linkedin_quad_daily but split into a tiny helper."""
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
                   AND verdict IN ('BUILD','CAUTION','AVOID')
                 ORDER BY RANDOM()
                 LIMIT 1
            """)
            r = cur.fetchone()
            if not r: return None
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
    """~280-char punchy LinkedIn post with arc thread."""
    arc_line = ""
    if arc:
        hook = (arc.get("channel_hooks") or {}).get("linkedin") or ""
        # extract the arc title for a one-line callout
        arc_title = (arc.get("arc") or "")[:80]
        if arc_title:
            arc_line = f"\n\nPart of our current narrative: {arc_title}"

    return (
        f"📍 {mover['name']} · {mover['iso']} · DCPI verdict: {mover['verdict']}\n\n"
        f"Excess Power: {mover['excess']:.1f}/100  ·  Constraint: {mover['constraint']:.1f}/100\n\n"
        f"Live page: https://dchub.cloud/dcpi/{mover['slug']}"
        f"{arc_line}\n\n"
        f"#datacenter #dcpi #{mover['iso'].lower().replace('-','')} #powergrid"
    )


def _shape_twitter(mover: dict, arc: dict | None) -> str:
    """≤280 char X post."""
    base = (f"{mover['name']} · DCPI {mover['verdict']} · "
            f"ExcessPower {mover['excess']:.0f}/100\n\n"
            f"https://dchub.cloud/dcpi/{mover['slug']}")
    if len(base) < 240:
        base += f"\n\n#datacenter #{mover['iso'].lower().replace('-','')}"
    return base[:280]


def _shape_bluesky(mover: dict, arc: dict | None) -> str:
    """≤300 char Bluesky post."""
    return (
        f"DCPI · {mover['name']} ({mover['iso']}) — verdict: {mover['verdict']}\n\n"
        f"Excess power {mover['excess']:.0f}/100, "
        f"constraint {mover['constraint']:.0f}/100.\n\n"
        f"Daily-refreshing scorecard: https://dchub.cloud/dcpi/{mover['slug']}"
    )[:300]


# ── Dedup + enqueue ─────────────────────────────────────────────────

def _already_enqueued_recently(platform: str, topic_key: str) -> bool:
    """Did we enqueue same platform+topic within window?"""
    c = _db_conn()
    if not c: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM social_media_posts
                 WHERE platform = %s
                   AND content LIKE %s
                   AND created_at > NOW() - (%s || ' hours')::interval
            """, (platform, f"%{topic_key[:60]}%", str(_DEDUP_WINDOW_HOURS)))
            n = (cur.fetchone() or [0])[0]
            return int(n or 0) > 0
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _enqueue_post(content: str, platform: str) -> int | None:
    """Insert a single approved row. Returns new id or None."""
    c = _db_conn()
    if not c: return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO social_media_posts
                       (content, platform, status, created_at)
                VALUES (%s, %s, 'approved', NOW())
                RETURNING id
            """, (content, platform))
            new_id = (cur.fetchone() or [None])[0]
            c.commit()
            return new_id
    except Exception as e:
        return None
    finally:
        try: c.close()
        except Exception: pass


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

    # LinkedIn — only enqueue if linkedin_quad_daily didn't already
    # fire this slot (lighter dedup since quad-daily writes its own
    # linkedin_quad_posts table; here we just check social_media_posts).
    if not _already_enqueued_recently("linkedin", topic_key):
        new_id = _enqueue_post(_shape_linkedin(mover, arc), "linkedin")
        if new_id:
            results["enqueued"].append({"platform": "linkedin", "id": new_id})
        else:
            results["skipped"].append({"platform": "linkedin",
                                         "reason": "insert_failed"})
    else:
        results["skipped"].append({"platform": "linkedin",
                                     "reason": "dedup_hit"})

    # Twitter
    if not _already_enqueued_recently("twitter", topic_key):
        new_id = _enqueue_post(_shape_twitter(mover, arc), "twitter")
        if new_id:
            results["enqueued"].append({"platform": "twitter", "id": new_id})
        else:
            results["skipped"].append({"platform": "twitter",
                                         "reason": "insert_failed"})
    else:
        results["skipped"].append({"platform": "twitter",
                                     "reason": "dedup_hit"})

    # Bluesky
    if not _already_enqueued_recently("bluesky", topic_key):
        new_id = _enqueue_post(_shape_bluesky(mover, arc), "bluesky")
        if new_id:
            results["enqueued"].append({"platform": "bluesky", "id": new_id})
        else:
            results["skipped"].append({"platform": "bluesky",
                                         "reason": "insert_failed"})
    else:
        results["skipped"].append({"platform": "bluesky",
                                     "reason": "dedup_hit"})

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

            # Published in last 24h
            cur.execute("""
                SELECT publish_platform, COUNT(*)
                  FROM social_media_posts
                 WHERE status = 'published'
                   AND published_at LIKE %s
                 GROUP BY publish_platform
                 ORDER BY publish_platform
            """, (datetime.datetime.utcnow().strftime("%Y-%m-%d") + "%",))
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
