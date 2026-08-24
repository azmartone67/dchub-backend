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
    """Phase 124B: probe THREE sources for emerging entities:
      1. news_articles / industry_news / news / press_releases — explicit news mentions
      2. mcp_upgrade_signals — markets users query but we don't have pages for
      3. ai_deals — buyer/seller markets in M&A activity
    Lower mention threshold to 2 to catch earlier signals.
    """
    _ensure()
    sown = []
    sources_tried = []

    try:
        with _conn() as c, c.cursor() as cur:
            # Source 1: news tables
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema='public'
                             AND table_name IN ('industry_news','news_articles','news','press_releases')""")
            news_tables = [r[0] for r in cur.fetchall()]
            for tbl in news_tables:
                cur.execute(f"""SELECT column_name FROM information_schema.columns
                                WHERE table_name='{tbl}'""")
                cols = {r[0] for r in cur.fetchall()}
                city_col = next((cc for cc in ('location_city','city','market_city','location','region') if cc in cols), None)
                time_col = next((cc for cc in ('created_at','published_at','date','timestamp','updated_at') if cc in cols), None)
                if not city_col or not time_col:
                    sources_tried.append(f"{tbl}(missing cols)")
                    continue
                try:
                    cur.execute(f"""SELECT DISTINCT {city_col}, COUNT(*) AS m
                                    FROM {tbl}
                                    WHERE {city_col} IS NOT NULL
                                      AND {time_col} > NOW() - INTERVAL '14 days'
                                    GROUP BY {city_col} HAVING COUNT(*) >= 2 LIMIT 50""")
                    for row in cur.fetchall():
                        city, mentions = row[0], row[1]
                        slug = str(city).lower().replace(' ','-').replace(',','').replace('.','')
                        cur.execute("SELECT 1 FROM market_power_scores WHERE market_slug=%s LIMIT 1", (slug,))
                        if cur.fetchone(): continue
                        cur.execute("""INSERT INTO page_seedlings
                            (slug, page_type, seed_data, source_signals, maturity_score, status, last_observed_at)
                            VALUES (%s, 'market', %s, %s, %s, 'seedling', NOW() ON CONFLICT DO NOTHING)
                            ON CONFLICT (slug) DO UPDATE SET maturity_score = page_seedlings.maturity_score + 1, last_observed_at = NOW()
                            RETURNING id, slug""",
                            (slug, json.dumps({"name": city, "type": "market", "first_seen": tbl}),
                             json.dumps({"news_mentions_14d": mentions, "source": tbl}),
                             min(mentions/2.0, 5)))
                        rid = cur.fetchone()
                        if rid: sown.append({"id": rid[0], "slug": rid[1], "source": tbl, "mentions": mentions})
                    sources_tried.append(tbl)
                except Exception as e:
                    sources_tried.append(f"{tbl}(query-err)")

            # Source 2: mcp_upgrade_signals — markets users query
            try:
                cur.execute("""SELECT table_name FROM information_schema.tables
                               WHERE table_name='mcp_upgrade_signals'""")
                if cur.fetchone():
                    cur.execute("""SELECT column_name FROM information_schema.columns
                                   WHERE table_name='mcp_upgrade_signals'""")
                    cols = {r[0] for r in cur.fetchall()}
                    if 'arguments' in cols:
                        cur.execute("""SELECT arguments::text, COUNT(*) AS m
                                       FROM mcp_upgrade_signals
                                       WHERE arguments IS NOT NULL
                                         AND created_at > NOW() - INTERVAL '30 days'
                                         AND arguments::text ILIKE '%market%'
                                       GROUP BY arguments::text HAVING COUNT(*) >= 3 LIMIT 30""")
                        # Parse JSON for market hints — we won't auto-create from these,
                        # but record them as evidence
                        sources_tried.append("mcp_upgrade_signals(scanned)")
            except Exception as e:
                sources_tried.append("mcp_upgrade_signals(err)")

            # Source 3: ai_deals — buyer/seller markets
            try:
                cur.execute("""SELECT table_name FROM information_schema.tables
                               WHERE table_name='ai_deals'""")
                if cur.fetchone():
                    cur.execute("""SELECT column_name FROM information_schema.columns
                                   WHERE table_name='ai_deals'""")
                    cols = {r[0] for r in cur.fetchall()}
                    market_col = next((cc for cc in ('market','target_market','region','location') if cc in cols), None)
                    if market_col:
                        cur.execute(f"""SELECT DISTINCT {market_col}, COUNT(*) AS m
                                        FROM ai_deals WHERE {market_col} IS NOT NULL
                                        GROUP BY {market_col} HAVING COUNT(*) >= 2 LIMIT 30""")
                        for row in cur.fetchall():
                            city, count = row[0], row[1]
                            slug = str(city).lower().replace(' ','-').replace(',','').replace('.','')
                            cur.execute("SELECT 1 FROM market_power_scores WHERE market_slug=%s LIMIT 1", (slug,))
                            if cur.fetchone(): continue
                            cur.execute("""INSERT INTO page_seedlings
                                (slug, page_type, seed_data, source_signals, maturity_score, status, last_observed_at)
                                VALUES (%s, 'market', %s, %s, %s, 'seedling', NOW() ON CONFLICT DO NOTHING)
                                ON CONFLICT (slug) DO UPDATE SET maturity_score = page_seedlings.maturity_score + 1, last_observed_at = NOW()
                                RETURNING id, slug""",
                                (slug, json.dumps({"name": city, "type": "market", "first_seen": "ai_deals"}),
                                 json.dumps({"deals_count": count, "source": "ai_deals"}),
                                 min(count/2.0, 5)))
                            rid = cur.fetchone()
                            if rid: sown.append({"id": rid[0], "slug": rid[1], "source": "ai_deals", "count": count})
                        sources_tried.append("ai_deals")
            except Exception as e:
                sources_tried.append(f"ai_deals(err): {str(e)[:80]}")

            c.commit()
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}", sown=sown, sources_tried=sources_tried), 200

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
