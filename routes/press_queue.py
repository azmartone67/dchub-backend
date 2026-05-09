"""Phase 125C — auto-draft press releases on big DCPI moves."""
import os, json, datetime
from flask import Blueprint, jsonify, request, Response
import psycopg2, psycopg2.extras

press_queue_bp = Blueprint("press_queue", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS press_releases_queue (
                id SERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                title TEXT, subheadline TEXT, body TEXT,
                category TEXT DEFAULT 'DCPI',
                trigger_type TEXT,           -- big_move | new_build | new_avoid | weekly
                trigger_data JSONB,
                status TEXT DEFAULT 'draft', -- draft | reviewed | published | rejected
                created_at TIMESTAMPTZ DEFAULT NOW(),
                published_at TIMESTAMPTZ
            )""")
        c.commit()


@press_queue_bp.route("/api/v1/press/queue", methods=["GET"])
def list_queue():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, slug, title, category, trigger_type, status, created_at, published_at
                       FROM press_releases_queue ORDER BY created_at DESC LIMIT 100""")
        rows = cur.fetchall()
    for r in rows:
        for k in ("created_at","published_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(queue=rows, count=len(rows)), 200


@press_queue_bp.route("/api/v1/press/scan", methods=["POST", "GET"])
def scan_for_drafts():
    """Scan DCPI for big moves and queue draft press releases."""
    _ensure()
    drafts = []
    THRESHOLD = 10.0  # 10-point swing is publishable
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest AS (
                  SELECT DISTINCT ON (market_slug) market_slug, market_name, state, iso,
                    excess_power_score AS now_e, constraint_score AS now_c, computed_at
                  FROM market_power_scores ORDER BY market_slug, computed_at DESC
                ),
                week_ago AS (
                  SELECT DISTINCT ON (market_slug) market_slug,
                    excess_power_score AS prev_e, constraint_score AS prev_c
                  FROM market_power_scores
                  WHERE computed_at < NOW() - INTERVAL '7 days'
                  ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_slug, l.market_name, l.state, l.iso,
                       l.now_e, w.prev_e, COALESCE(l.now_e - w.prev_e, 0) AS delta_e,
                       l.now_c, w.prev_c, COALESCE(l.now_c - w.prev_c, 0) AS delta_c
                FROM latest l LEFT JOIN week_ago w ON l.market_slug=w.market_slug
                WHERE w.prev_e IS NOT NULL
                  AND (ABS(COALESCE(l.now_e - w.prev_e, 0)) >= %s
                    OR ABS(COALESCE(l.now_c - w.prev_c, 0)) >= %s)
            """, (THRESHOLD, THRESHOLD))
            big_moves = cur.fetchall()
            for m in big_moves:
                slug = f"dcpi-{m['market_slug']}-{datetime.date.today().isoformat()}"
                # Skip if already drafted today
                cur.execute("SELECT 1 FROM press_releases_queue WHERE slug=%s", (slug,))
                if cur.fetchone(): continue

                de, dc = m["delta_e"], m["delta_c"]
                direction = "rises" if de > 0 else "falls" if de < 0 else "shifts"
                if abs(dc) > abs(de):
                    direction = "tightens" if dc > 0 else "loosens"

                title = f"DCPI {direction.title()}: {m['market_name']} Posts {abs(de):.1f}-Point Weekly Move"
                sub = f"{m['market_name']} ({m['state']}, {m['iso']}) — Excess Power {m['now_e']:.1f} ({de:+.1f} 7d), Constraint {m['now_c']:.1f} ({dc:+.1f} 7d)"
                body = f"""The DC Hub Power Index recorded a notable shift in {m['market_name']} this week.

The Excess Power Score moved from {m['prev_e']:.1f} to {m['now_e']:.1f} — a {de:+.1f}-point change. The Constraint Score moved from {m['prev_c']:.1f} to {m['now_c']:.1f} ({dc:+.1f} 7d).

The DCPI is updated daily from ISO interconnection-queue data, generation pipeline, renewable curtailment, and behind-the-meter capacity signals. Read the full methodology at https://dchub.cloud/dcpi.

Cite as: DC Hub Power Index, {m['market_name']}, {datetime.date.today().isoformat()}, https://dchub.cloud/dcpi/{m['market_slug']}.
"""

                cur.execute("""INSERT INTO press_releases_queue
                    (slug, title, subheadline, body, trigger_type, trigger_data)
                    VALUES (%s, %s, %s, %s, 'big_move', %s) RETURNING id""",
                    (slug, title, sub, body,
                     json.dumps({"market": m["market_slug"], "delta_e": de, "delta_c": dc})))
                rid = cur.fetchone()[0]
                drafts.append({"id": rid, "slug": slug, "title": title})
            c.commit()
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}", drafts=drafts), 500
    return jsonify(drafts=drafts, count=len(drafts), threshold=THRESHOLD), 200


@press_queue_bp.route("/api/v1/press/<slug>", methods=["GET"])
def get_draft(slug):
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM press_releases_queue WHERE slug=%s", (slug,))
        r = cur.fetchone()
    if not r: return jsonify(error="not found"), 404
    for k in ("created_at","published_at"):
        if r.get(k): r[k] = r[k].isoformat()
    return jsonify(r), 200


@press_queue_bp.route("/api/v1/press/<slug>/publish", methods=["POST"])
def publish(slug):
    expected = os.environ.get("DCHUB_ADMIN_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE press_releases_queue SET status='published', published_at=NOW()
                       WHERE slug=%s RETURNING id""", (slug,))
        r = cur.fetchone(); c.commit()
    if not r: return jsonify(error="not found"), 404
    return jsonify(published=slug, id=r[0]), 200
