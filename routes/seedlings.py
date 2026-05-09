"""Phase 113 — root-planting. Watches the data for new entities (markets,
hyperscalers, transactions, ISOs) and seeds new pages automatically.

  GET  /api/v1/seedlings        — current seedlings
  POST /api/v1/seedlings/sow    — scan data, generate new seedlings
  POST /api/v1/seedlings/promote/<id> — promote to live
"""
import os, json, datetime
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras

seedlings_bp = Blueprint("seedlings", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS page_seedlings (
                id SERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                page_type TEXT,             -- market | facility | hyperscaler | transaction
                seed_data JSONB,
                source_signals JSONB,       -- where we saw the entity
                maturity_score REAL DEFAULT 0,
                status TEXT DEFAULT 'seedling',  -- seedling | growing | promoted | killed
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_observed_at TIMESTAMPTZ DEFAULT NOW(),
                promoted_at TIMESTAMPTZ
            )""")
        c.commit()


@seedlings_bp.route("/api/v1/seedlings", methods=["GET"])
def list_seedlings():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM page_seedlings
                       WHERE status IN ('seedling','growing')
                       ORDER BY maturity_score DESC, last_observed_at DESC LIMIT 100""")
        rows = cur.fetchall()
    for r in rows:
        for k in ("created_at","last_observed_at","promoted_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(seedlings=rows, count=len(rows)), 200


@seedlings_bp.route("/api/v1/seedlings/sow", methods=["POST", "GET"])
def sow():
    """Scan data tables for new entities not yet covered by existing pages."""
    _ensure()
    sown = []
    try:
        with _conn() as c, c.cursor() as cur:
            # Look for cities mentioned in news that aren't in existing markets
            cur.execute("""
                SELECT DISTINCT location_city, COUNT(*) AS mentions
                  FROM industry_news
                 WHERE location_city IS NOT NULL
                   AND created_at > NOW() - INTERVAL '14 days'
                 GROUP BY location_city
                HAVING COUNT(*) >= 3
                 LIMIT 50
            """)
            for city, mentions in cur.fetchall():
                if not city: continue
                slug = city.lower().replace(' ','-').replace(',','').replace('.','')
                cur.execute("""
                    SELECT 1 FROM market_power_scores WHERE market_slug=%s LIMIT 1
                """, (slug,))
                if cur.fetchone(): continue
                cur.execute("""
                    INSERT INTO page_seedlings (slug, page_type, seed_data, source_signals,
                                                 maturity_score, status, last_observed_at)
                    VALUES (%s, 'market', %s, %s, %s, 'seedling', NOW())
                    ON CONFLICT (slug) DO UPDATE SET
                      maturity_score = page_seedlings.maturity_score + 1,
                      last_observed_at = NOW()
                    RETURNING id, slug
                """, (slug,
                      json.dumps({"name": city, "type": "market", "first_seen": "news"}),
                      json.dumps({"news_mentions_14d": mentions}),
                      min(mentions/3.0, 5)))
                row = cur.fetchone()
                if row: sown.append({"id": row[0], "slug": row[1], "mentions": mentions})
            c.commit()
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}", sown=sown), 500
    return jsonify(sown=sown, count=len(sown)), 200


@seedlings_bp.route("/api/v1/seedlings/promote/<int:sid>", methods=["POST"])
def promote(sid):
    expected = os.environ.get("DCHUB_ADMIN_KEY")
    provided = request.headers.get("X-Admin-Key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE page_seedlings SET status='promoted', promoted_at=NOW() WHERE id=%s RETURNING slug",
                    (sid,))
        r = cur.fetchone(); c.commit()
    if not r: return jsonify(error="not found"), 404
    return jsonify(promoted=r[0], note="seedling marked promoted; create the actual route in code"), 200
