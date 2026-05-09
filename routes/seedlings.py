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
    """Scan data tables for new entities not yet covered by existing pages.
    Phase 117C: gracefully handle missing tables — DC Hub deployments vary."""
    _ensure()
    sown = []
    sources_tried = []
    try:
        with _conn() as c, c.cursor() as cur:
            # Probe which news/source tables exist in this deployment
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema='public'
                             AND table_name IN ('industry_news','news_articles','news','press_releases')""")
            existing = {r[0] for r in cur.fetchall()}
            sources_tried = list(existing)

            news_table = None
            for candidate in ('industry_news', 'news_articles', 'news', 'press_releases'):
                if candidate in existing:
                    news_table = candidate
                    break

            if not news_table:
                return jsonify(sown=[], count=0, sources_tried=sources_tried,
                               note="no news table found in this deployment; nothing to sow"), 200

            # Probe column names too — schemas vary
            cur.execute(f"""SELECT column_name FROM information_schema.columns
                           WHERE table_name='{news_table}'""")
            cols = {r[0] for r in cur.fetchall()}
            city_col = next((c for c in ('location_city','city','market_city','location') if c in cols), None)
            time_col = next((c for c in ('created_at','published_at','date','timestamp') if c in cols), None)

            if not city_col or not time_col:
                return jsonify(sown=[], count=0, sources_tried=[news_table],
                               note=f"{news_table} lacks city/time columns ({city_col=}, {time_col=})"), 200

            cur.execute(f"""
                SELECT DISTINCT {city_col}, COUNT(*) AS mentions
                  FROM {news_table}
                 WHERE {city_col} IS NOT NULL
                   AND {time_col} > NOW() - INTERVAL '14 days'
                 GROUP BY {city_col}
                HAVING COUNT(*) >= 3
                 LIMIT 50
            """)
            for city, mentions in cur.fetchall():
                if not city: continue
                slug = str(city).lower().replace(' ','-').replace(',','').replace('.','')
                cur.execute("""SELECT 1 FROM market_power_scores WHERE market_slug=%s LIMIT 1""", (slug,))
                if cur.fetchone(): continue
                cur.execute("""
                    INSERT INTO page_seedlings (slug, page_type, seed_data, source_signals,
                                                 maturity_score, status, last_observed_at)
                    VALUES (%s, 'market', %s, %s, %s, 'seedling', NOW())
                    ON CONFLICT (slug) DO UPDATE SET
                      maturity_score = page_seedlings.maturity_score + 1,
                      last_observed_at = NOW()
                    RETURNING id, slug
                """, (slug, json.dumps({"name": city, "type": "market", "first_seen": news_table}),
                      json.dumps({"news_mentions_14d": mentions, "source": news_table}),
                      min(mentions/3.0, 5)))
                row = cur.fetchone()
                if row: sown.append({"id": row[0], "slug": row[1], "mentions": mentions})
            c.commit()
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}",
                       sown=sown, sources_tried=sources_tried), 200
    return jsonify(sown=sown, count=len(sown), sources_tried=sources_tried), 200



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
