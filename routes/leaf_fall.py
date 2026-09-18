"""Phase 112 — leaf-fall. Pages with low vitality get pruned automatically.

  GET  /api/v1/leaf-fall/health    — vitality scores
  POST /api/v1/leaf-fall/sweep     — mark stale + low-engagement pages for pruning
  GET  /api/v1/leaf-fall/graveyard — what's been pruned
"""
import os, datetime
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras

leaf_fall_bp = Blueprint("leaf_fall", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS page_health (
                id SERIAL PRIMARY KEY,
                surface TEXT UNIQUE NOT NULL,
                views_30d INT DEFAULT 0,
                clicks_30d INT DEFAULT 0,
                last_engagement_at TIMESTAMPTZ,
                vitality_score REAL,
                status TEXT DEFAULT 'alive',  -- alive | wilting | pruned
                computed_at TIMESTAMPTZ DEFAULT NOW(),
                pruned_at TIMESTAMPTZ,
                prune_reason TEXT
            )""")
        c.commit()


def compute_vitality(views, clicks, last_engagement_at):
    """0..100 vitality score."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    age_days = 999
    if last_engagement_at:
        age_days = (now - last_engagement_at).total_seconds() / 86400.0
    # Recency: fresh engagement = high
    recency = max(0.0, 1.0 - (age_days/60.0)) * 50  # 60 days = 0
    # Volume: log views, capped
    import math
    volume = min(30, math.log10(max(views, 1)) * 12)
    # Quality: CTR
    ctr = (clicks/views) if views > 0 else 0
    quality = min(20, ctr * 200)
    return round(recency + volume + quality, 1)


@leaf_fall_bp.route("/api/v1/leaf-fall/health", methods=["GET"])
def health():
    _ensure()
    # Heuristic stub: pull views from page_variants impressions if any
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT ph.*, COALESCE(v.imp, 0) AS view_proxy
              FROM page_health ph
              LEFT JOIN (SELECT surface, SUM(impressions) AS imp FROM page_variants GROUP BY surface) v
                ON ph.surface = v.surface
            ORDER BY vitality_score ASC NULLS FIRST LIMIT 200
        """)
        rows = cur.fetchall()
    for r in rows:
        for k in ("computed_at","pruned_at","last_engagement_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(pages=rows, count=len(rows)), 200


@leaf_fall_bp.route("/api/v1/leaf-fall/sweep", methods=["POST", "GET"])
def sweep():
    """Compute vitality for every freshness_checks surface. Phase 117D: batched
    aggregation in one query rather than per-surface roundtrips."""
    _ensure()
    threshold = float(request.args.get("threshold", "10"))
    swept = []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                WITH agg AS (
                    SELECT
                        fc.surface,
                        COALESCE(SUM(pv.impressions), 0) AS views,
                        COALESCE(SUM(pv.engagements), 0) AS clicks,
                        MAX(pve.created_at) AS last_eng
                    FROM freshness_checks fc
                    LEFT JOIN page_variants pv ON pv.surface = fc.surface
                    LEFT JOIN page_variant_events pve ON pve.variant_id = pv.id AND pve.event = 'engagement'
                    GROUP BY fc.surface
                )
                SELECT surface, views, clicks, last_eng FROM agg
            """)
            for surface, views, clicks, last_eng in cur.fetchall():
                vit = compute_vitality(int(views or 0), int(clicks or 0), last_eng)
                status = 'wilting' if (vit < threshold and views == 0) else 'alive'
                reason = f"no engagement; vitality {vit}" if status == 'wilting' else None
                cur.execute("""
                    INSERT INTO page_health
                      (surface, views_30d, clicks_30d, last_engagement_at,
                       vitality_score, status, computed_at, prune_reason)
                    VALUES (%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING,%s)
                    ON CONFLICT (surface) DO UPDATE SET
                      views_30d = EXCLUDED.views_30d,
                      clicks_30d = EXCLUDED.clicks_30d,
                      last_engagement_at = EXCLUDED.last_engagement_at,
                      vitality_score = EXCLUDED.vitality_score,
                      status = CASE WHEN page_health.status='pruned' THEN 'pruned' ELSE EXCLUDED.status END,
                      computed_at = NOW(),
                      prune_reason = EXCLUDED.prune_reason
                """, (surface, int(views or 0), int(clicks or 0), last_eng, vit, status, reason))
                swept.append({"surface": surface, "vitality": vit, "status": status})
            c.commit()
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}", swept=swept), 500
    return jsonify(swept=swept, count=len(swept), threshold=threshold), 200



@leaf_fall_bp.route("/api/v1/leaf-fall/prune", methods=["POST"])
def prune():
    """Mark surfaces with status='wilting' as 'pruned'."""
    expected = os.environ.get("DCHUB_ADMIN_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE page_health SET status='pruned', pruned_at=NOW()
                       WHERE status='wilting' RETURNING surface""")
        pruned = [r[0] for r in cur.fetchall()]
        c.commit()
    return jsonify(pruned=pruned, count=len(pruned)), 200


@leaf_fall_bp.route("/api/v1/leaf-fall/graveyard", methods=["GET"])
def graveyard():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM page_health WHERE status='pruned' ORDER BY pruned_at DESC")
        rows = cur.fetchall()
    for r in rows:
        for k in ("computed_at","pruned_at","last_engagement_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(graveyard=rows, count=len(rows)), 200
