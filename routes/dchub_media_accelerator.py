"""
dchub_media_accelerator.py — DC Hub Media Acceleration Trigger (2026-06-05).

When a LinkedIn post outperforms the 30-day average impressions by 2x or
more within 6 hours of publish, this module auto-enqueues identical
cross-posts to Twitter/X + Bluesky so the early viral signal is
amplified everywhere at once. We INSERT the cross-posts with
status='approved' and platform='twitter'/'bluesky' — we do NOT auto-fire
them. The existing start_twitter_publisher + start_bluesky_publisher
loops in content_publisher.py pick those up under the same 2/day
per-platform cap, so we cannot accidentally spam.

The trigger also flags the original LinkedIn social_media_posts row
(accelerate=TRUE, viral_score=<ratio>) so an analyst can review what
broke through.

Endpoints
---------
POST /api/v1/admin/media/accelerator/scan
    Admin-keyed. Computes a 30-day average impressions baseline from
    linkedin_posts (impressions > 0). For each LinkedIn post published
    in the last 24h, computes viral_score = impressions / baseline. If
    score >= 2.0 AND post is <6h old AND accelerate=FALSE, marks the
    row, finds the matching auto_press_releases.slug (best-effort) and
    enqueues approved Twitter + Bluesky cross-posts with the same
    content_text.

GET  /api/v1/admin/media/accelerator/recent?days=7
    Admin-keyed diagnostic — recently accelerated rows + viral_score.

Constants
---------
VIRAL_THRESHOLD       = 2.0   (impressions / baseline ratio that fires)
RECENT_PUBLISH_HOURS  = 6     (post must be this young to qualify)
LOOKBACK_HOURS        = 24    (only scan posts published in last 24h)
BASELINE_LOOKBACK_DAYS = 30   (the baseline window)
"""
from __future__ import annotations

import os
import sys
import datetime
from typing import Any

from flask import Blueprint, jsonify, request


dchub_media_accelerator_bp = Blueprint("dchub_media_accelerator", __name__)


# ── Tunables ───────────────────────────────────────────────────────────
VIRAL_THRESHOLD        = 2.0
RECENT_PUBLISH_HOURS   = 6
LOOKBACK_HOURS         = 24
BASELINE_LOOKBACK_DAYS = 30


# ── Plumbing ───────────────────────────────────────────────────────────
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
        sys.stderr.write(f"[media-accelerator] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── Core scan ──────────────────────────────────────────────────────────
def _compute_baseline(cur) -> float | None:
    """30-day average impressions across non-zero linkedin_posts."""
    try:
        cur.execute(f"""
            SELECT AVG(impressions)::float
              FROM linkedin_posts
             WHERE posted_at > NOW() - INTERVAL '{BASELINE_LOOKBACK_DAYS} days'
               AND impressions IS NOT NULL
               AND impressions > 0
        """)
        row = cur.fetchone()
        if not row:
            return None
        val = row[0] if not hasattr(row, 'get') else row.get('avg')
        return float(val) if val is not None else None
    except Exception as e:
        _log(f"baseline_compute_failed: {e}")
        return None


def _recent_linkedin_posts(cur) -> list[dict]:
    """LinkedIn posts published in the last LOOKBACK_HOURS with
    real impressions ingested. Returns a list of plain dicts so the
    caller never has to care about RealDictCursor vs tuple cursor."""
    out: list[dict] = []
    try:
        cur.execute(f"""
            SELECT id,
                   COALESCE(linkedin_post_id, post_urn, '')          AS li_id,
                   COALESCE(content, content_text, '')              AS content,
                   posted_at,
                   impressions,
                   EXTRACT(EPOCH FROM (NOW() - posted_at))/3600.0   AS age_hours
              FROM linkedin_posts
             WHERE posted_at > NOW() - INTERVAL '{LOOKBACK_HOURS} hours'
               AND impressions IS NOT NULL
               AND impressions > 0
             ORDER BY posted_at DESC
        """)
        for row in cur.fetchall() or []:
            if hasattr(row, 'get'):
                out.append({
                    "id":          row.get("id"),
                    "li_id":       row.get("li_id"),
                    "content":     row.get("content") or "",
                    "posted_at":   row.get("posted_at"),
                    "impressions": int(row.get("impressions") or 0),
                    "age_hours":   float(row.get("age_hours") or 0.0),
                })
            else:
                out.append({
                    "id":          row[0],
                    "li_id":       row[1] or "",
                    "content":     row[2] or "",
                    "posted_at":   row[3],
                    "impressions": int(row[4] or 0),
                    "age_hours":   float(row[5] or 0.0),
                })
    except Exception as e:
        _log(f"recent_posts_query_failed: {e}")
    return out


def _find_matching_slug(cur, content_text: str) -> str | None:
    """Best-effort: find an auto_press_releases.slug whose linkedin_post
    text matches the LinkedIn content (first ~80 chars). Returns None if
    no match — caller still cross-posts, just without a slug pointer."""
    if not content_text:
        return None
    snippet = content_text.strip()[:80]
    if not snippet:
        return None
    try:
        cur.execute("""
            SELECT slug FROM auto_press_releases
             WHERE linkedin_post IS NOT NULL
               AND POSITION(%s IN linkedin_post) > 0
             ORDER BY generated_at DESC
             LIMIT 1
        """, (snippet,))
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if not hasattr(row, 'get') else row.get('slug')
    except Exception:
        return None


def _find_social_media_post_row(cur, li_id: str, content_text: str) -> dict | None:
    """Locate the social_media_posts row that mirrors this LinkedIn
    post. Try by linkedin_urn first (cleanest), then by content prefix."""
    if li_id:
        try:
            cur.execute("""
                SELECT id, accelerate
                  FROM social_media_posts
                 WHERE linkedin_urn = %s
                 LIMIT 1
            """, (li_id,))
            row = cur.fetchone()
            if row:
                if hasattr(row, 'get'):
                    return {"id": row.get("id"), "accelerate": bool(row.get("accelerate"))}
                return {"id": row[0], "accelerate": bool(row[1])}
        except Exception:
            pass
    if content_text:
        snippet = content_text.strip()[:120]
        try:
            cur.execute("""
                SELECT id, accelerate
                  FROM social_media_posts
                 WHERE publish_platform = 'linkedin'
                   AND status = 'published'
                   AND POSITION(%s IN content) > 0
                 ORDER BY created_at DESC
                 LIMIT 1
            """, (snippet,))
            row = cur.fetchone()
            if row:
                if hasattr(row, 'get'):
                    return {"id": row.get("id"), "accelerate": bool(row.get("accelerate"))}
                return {"id": row[0], "accelerate": bool(row[1])}
        except Exception:
            pass
    return None


def _enqueue_cross_post(cur, platform: str, content_text: str) -> bool:
    """INSERT an approved row that the existing per-platform publisher
    loop will pick up under its 2/day cap. Returns True on success."""
    try:
        cur.execute("""
            INSERT INTO social_media_posts (content, platform, status, created_at, approved_at)
            VALUES (%s, %s, 'approved', NOW(), NOW()::text)
        """, (content_text, platform))
        return True
    except Exception as e:
        _log(f"enqueue_{platform}_failed: {e}")
        return False


def scan_for_viral_posts() -> dict[str, Any]:
    """Main scan. Returns a result dict — safe to call from cron or
    from the admin endpoint."""
    result: dict[str, Any] = {
        "scanned":      0,
        "accelerated":  0,
        "baseline":     None,
        "threshold":    VIRAL_THRESHOLD,
        "lookback_hours": LOOKBACK_HOURS,
        "recent_publish_window_hours": RECENT_PUBLISH_HOURS,
        "posts":        [],
        "errors":       [],
    }
    conn = _db_conn()
    if conn is None:
        result["errors"].append("no_db_connection")
        return result
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            baseline = _compute_baseline(cur)
            result["baseline"] = baseline
            if not baseline or baseline <= 0:
                result["errors"].append("no_baseline")
                return result

            recent = _recent_linkedin_posts(cur)
            result["scanned"] = len(recent)

            for post in recent:
                impressions = post["impressions"]
                age_hours   = post["age_hours"]
                content     = post["content"]
                if impressions <= 0:
                    continue
                viral_score = round(impressions / baseline, 3)

                qualifies = (
                    viral_score >= VIRAL_THRESHOLD
                    and age_hours < RECENT_PUBLISH_HOURS
                )
                if not qualifies:
                    continue

                smp = _find_social_media_post_row(cur, post["li_id"], content)
                if smp is None:
                    result["errors"].append(
                        f"no_smp_match li_id={post['li_id'] or 'NA'}")
                    continue
                if smp["accelerate"]:
                    # Already fired in an earlier scan — skip.
                    continue

                slug = _find_matching_slug(cur, content)

                # Flag the original LinkedIn row.
                try:
                    cur.execute("""
                        UPDATE social_media_posts
                           SET accelerate = TRUE,
                               viral_score = %s
                         WHERE id = %s
                    """, (viral_score, smp["id"]))
                except Exception as e:
                    result["errors"].append(f"flag_failed id={smp['id']}: {e}")
                    continue

                # Enqueue cross-posts. We always try both platforms; if
                # one fails the other still ships.
                tw_ok = _enqueue_cross_post(cur, "twitter", content)
                bs_ok = _enqueue_cross_post(cur, "bluesky", content)

                _log(
                    f"ACCELERATED smp_id={smp['id']} li_id={post['li_id'] or 'NA'} "
                    f"slug={slug or 'NA'} viral_score={viral_score} "
                    f"impressions={impressions} baseline={round(baseline,1)} "
                    f"age_h={round(age_hours,2)} tw={tw_ok} bs={bs_ok}"
                )

                result["accelerated"] += 1
                result["posts"].append({
                    "smp_id":      smp["id"],
                    "li_id":       post["li_id"] or None,
                    "slug":        slug,
                    "viral_score": viral_score,
                    "impressions": impressions,
                    "baseline":    round(baseline, 1),
                    "age_hours":   round(age_hours, 2),
                    "twitter_enqueued": tw_ok,
                    "bluesky_enqueued": bs_ok,
                })

            try:
                conn.commit()
            except Exception as e:
                result["errors"].append(f"commit_failed: {e}")
                try: conn.rollback()
                except Exception: pass
    except Exception as e:
        result["errors"].append(f"scan_failed: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    return result


# ── HTTP surface ───────────────────────────────────────────────────────
@dchub_media_accelerator_bp.route(
    "/api/v1/admin/media/accelerator/scan", methods=["POST"])
def http_scan():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    out = scan_for_viral_posts()
    return jsonify(out), 200


@dchub_media_accelerator_bp.route(
    "/api/v1/admin/media/accelerator/recent", methods=["GET"])
def http_recent():
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        days = int(request.args.get("days", "7"))
    except Exception:
        days = 7
    days = max(1, min(days, 90))

    out: dict[str, Any] = {
        "days":  days,
        "count": 0,
        "posts": [],
    }
    conn = _db_conn()
    if conn is None:
        out["error"] = "no_db"
        return jsonify(out), 200
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"""
                    SELECT id, platform, publish_platform, status,
                           viral_score, accelerate, content, created_at,
                           published_at, linkedin_urn
                      FROM social_media_posts
                     WHERE accelerate = TRUE
                       AND created_at > NOW() - INTERVAL '{days} days'
                     ORDER BY viral_score DESC NULLS LAST, created_at DESC
                     LIMIT 100
                """)
                rows = cur.fetchall() or []
            except Exception as e:
                out["error"] = f"query_failed: {e}"
                rows = []
            for row in rows:
                if hasattr(row, 'get'):
                    d = {
                        "id":               row.get("id"),
                        "platform":         row.get("platform"),
                        "publish_platform": row.get("publish_platform"),
                        "status":           row.get("status"),
                        "viral_score":      float(row.get("viral_score")) if row.get("viral_score") is not None else None,
                        "accelerate":       bool(row.get("accelerate")),
                        "content_preview":  (row.get("content") or "")[:180],
                        "created_at":       row.get("created_at").isoformat() if row.get("created_at") else None,
                        "published_at":     row.get("published_at"),
                        "linkedin_urn":     row.get("linkedin_urn"),
                    }
                else:
                    d = {
                        "id":               row[0],
                        "platform":         row[1],
                        "publish_platform": row[2],
                        "status":           row[3],
                        "viral_score":      float(row[4]) if row[4] is not None else None,
                        "accelerate":       bool(row[5]),
                        "content_preview":  (row[6] or "")[:180],
                        "created_at":       row[7].isoformat() if row[7] else None,
                        "published_at":     row[8],
                        "linkedin_urn":     row[9],
                    }
                out["posts"].append(d)
            out["count"] = len(out["posts"])
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify(out), 200
