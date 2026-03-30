"""
AI Deals API Module for DC Hub Backend
=======================================
Flask routes for AI-detected M&A deal tracking with Neon PostgreSQL storage.

Plug into main.py:
    from ai_deals_api import register_ai_deals_routes
    register_ai_deals_routes(app, get_db)
"""
import hashlib
import json
import logging
from datetime import datetime, date
from functools import wraps
from flask import request, jsonify

logger = logging.getLogger(__name__)

def _deal_hash(buyer, seller, deal_date, deal_value_str):
    raw = f"{buyer.strip().lower()}|{seller.strip().lower()}|{deal_date}|{(deal_value_str or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()

def _parse_value_str(val_str):
    if not val_str or val_str.lower() in ('undisclosed', 'n/a', ''):
        return None, 'Undisclosed'
    clean = val_str.replace('$', '').replace(',', '').strip()
    multiplier = 1
    if clean.upper().endswith('B'):
        multiplier = 1_000_000_000
        clean = clean[:-1]
    elif clean.upper().endswith('M'):
        multiplier = 1_000_000
        clean = clean[:-1]
    elif clean.upper().endswith('K'):
        multiplier = 1_000
        clean = clean[:-1]
    try:
        numeric = float(clean) * multiplier
        return numeric, val_str
    except (ValueError, TypeError):
        return None, val_str or 'Undisclosed'

def _require_api_key(get_db):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
            if not api_key:
                return jsonify({"error": "API key required"}), 401
            try:
                conn = get_db()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT rate_limit_tier FROM api_keys WHERE key = %s AND is_active = TRUE", (api_key,))
                    row = cur.fetchone()
                    if not row:
                        return jsonify({"error": "Invalid or inactive API key"}), 403
                    if row[0] not in ('pro', 'enterprise', 'admin'):
                        return jsonify({"error": "Pro or higher tier required for ingestion"}), 403
                    except Exception as e:
                    logger.error(f"API key check failed: {e}")
                    return jsonify({"error": "Auth check failed"}), 500
                    return f(*args, **kwargs)
                finally:
                    conn.close()
        return decorated
    return decorator

def register_ai_deals_routes(app, get_db):

    # ── Init table on startup ─────────────────────────────────
    def init_ai_deals_table():
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ai_deals (
                        id SERIAL PRIMARY KEY,
                        deal_hash VARCHAR(64) UNIQUE NOT NULL,
                        buyer VARCHAR(255) NOT NULL,
                        seller VARCHAR(255) NOT NULL,
                        deal_value_usd NUMERIC(18,2),
                        deal_value_str VARCHAR(50),
                        deal_type VARCHAR(50) NOT NULL DEFAULT 'unknown',
                        confidence REAL DEFAULT 0.85,
                        deal_date DATE NOT NULL,
                        region VARCHAR(100),
                        market VARCHAR(255),
                        source_url TEXT,
                        source_name VARCHAR(255),
                        description TEXT,
                        ai_detected BOOLEAN DEFAULT TRUE,
                        status VARCHAR(30) DEFAULT 'detected',
                        assets TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        ingestion_batch VARCHAR(64)
                    )
                """)
                for idx_sql in [
                    "CREATE INDEX IF NOT EXISTS idx_ai_deals_date ON ai_deals(deal_date DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_ai_deals_type ON ai_deals(deal_type)",
                    "CREATE INDEX IF NOT EXISTS idx_ai_deals_buyer ON ai_deals(buyer)",
                    "CREATE INDEX IF NOT EXISTS idx_ai_deals_seller ON ai_deals(seller)",
                    "CREATE INDEX IF NOT EXISTS idx_ai_deals_value ON ai_deals(deal_value_usd DESC NULLS LAST)",
                ]:
                    cur.execute(idx_sql)
                conn.commit()
                logger.info("ai_deals table initialized")
                except Exception as e:
                logger.error(f"Failed to init ai_deals table: {e}")

            finally:
                conn.close()
    init_ai_deals_table()

    # ── GET /api/ai-deals ─────────────────────────────────────
    @app.route('/api/ai-deals', methods=['GET'])
    def get_ai_deals():
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                limit = min(int(request.args.get('limit', 50)), 200)
                offset = int(request.args.get('offset', 0))
                deal_type = request.args.get('type', '').strip()
                buyer = request.args.get('buyer', '').strip()
                seller = request.args.get('seller', '').strip()
                min_value = request.args.get('min_value', '')
                date_from = request.args.get('date_from', '').strip()
                date_to = request.args.get('date_to', '').strip()
                sort = request.args.get('sort', 'deal_date').strip()
                order = 'ASC' if request.args.get('order', 'desc').lower() == 'asc' else 'DESC'

                where_clauses = []
                params = []
                if deal_type:
                    where_clauses.append("deal_type = %s"); params.append(deal_type)
                if buyer:
                    where_clauses.append("buyer ILIKE %s"); params.append(f"%{buyer}%")
                if seller:
                    where_clauses.append("seller ILIKE %s"); params.append(f"%{seller}%")
                if min_value:
                    where_clauses.append("deal_value_usd >= %s"); params.append(float(min_value))
                if date_from:
                    where_clauses.append("deal_date >= %s"); params.append(date_from)
                if date_to:
                    where_clauses.append("deal_date <= %s"); params.append(date_to)

                where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
                allowed_sorts = {'deal_date', 'deal_value_usd', 'buyer', 'seller', 'deal_type', 'confidence', 'created_at'}
                if sort not in allowed_sorts:
                    sort = 'deal_date'

                cur.execute(f"SELECT COUNT(*) FROM ai_deals{where_sql}", params)
                total = cur.fetchone()[0]

                cur.execute(f"""
                    SELECT id, buyer, seller, deal_value_usd, deal_value_str, deal_type,
                           confidence, deal_date, region, market, source_url, source_name,
                           description, ai_detected, status, created_at
                    FROM ai_deals {where_sql}
                    ORDER BY {sort} {order} NULLS LAST
                    LIMIT %s OFFSET %s
                """, params + [limit, offset])

                columns = [desc[0] for desc in cur.description]
                deals = []
                for row in cur.fetchall():
                    deal = dict(zip(columns, row))
                    for k, v in deal.items():
                        if isinstance(v, (date, datetime)):
                            deal[k] = v.isoformat()
                        elif hasattr(v, 'as_tuple'):
                            deal[k] = float(v)
                    deals.append(deal)

                return jsonify({"success": True, "deals": deals, "total": total, "limit": limit, "offset": offset, "has_more": (offset + limit) < total})
                except Exception as e:
                logger.error(f"GET /api/ai-deals failed: {e}")
                return jsonify({"error": str(e)}), 500

            finally:
                conn.close()
    # ── GET /api/ai-deals/summary ─────────────────────────────
    @app.route('/api/ai-deals/summary', methods=['GET'])
    def get_ai_deals_summary():
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) AS total_deals,
                           COUNT(CASE WHEN deal_value_usd IS NOT NULL THEN 1 END) AS deals_with_value,
                           COALESCE(SUM(deal_value_usd), 0) AS total_volume_usd,
                           ROUND(AVG(confidence)::numeric, 2) AS avg_confidence,
                           MIN(deal_date) AS earliest_deal,
                           MAX(deal_date) AS latest_deal,
                           MAX(created_at) AS last_ingestion
                    FROM ai_deals
                """)
                row = cur.fetchone()
                columns = [desc[0] for desc in cur.description]
                summary = dict(zip(columns, row))
                for k, v in summary.items():
                    if isinstance(v, (date, datetime)):
                        summary[k] = v.isoformat()
                    elif hasattr(v, 'as_tuple'):
                        summary[k] = float(v)
                return jsonify({"success": True, "summary": summary})
                except Exception as e:
                logger.error(f"GET /api/ai-deals/summary failed: {e}")
                return jsonify({"error": str(e)}), 500

            finally:
                conn.close()
    # ── GET /api/ai-deals/by-type ─────────────────────────────
    @app.route('/api/ai-deals/by-type', methods=['GET'])
    def get_ai_deals_by_type():
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT deal_type, COUNT(*) AS deal_count,
                           COALESCE(SUM(deal_value_usd), 0) AS total_value_usd,
                           ROUND(AVG(confidence)::numeric, 2) AS avg_confidence
                    FROM ai_deals GROUP BY deal_type ORDER BY total_value_usd DESC
                """)
                columns = [desc[0] for desc in cur.description]
                rows = []
                for row in cur.fetchall():
                    r = dict(zip(columns, row))
                    for k, v in r.items():
                        if hasattr(v, 'as_tuple'):
                            r[k] = float(v)
                    rows.append(r)
                return jsonify({"success": True, "by_type": rows})
                except Exception as e:
                logger.error(f"GET /api/ai-deals/by-type failed: {e}")
                return jsonify({"error": str(e)}), 500

            finally:
                conn.close()
    # ── POST /api/ai-deals/ingest ─────────────────────────────
    @app.route('/api/ai-deals/ingest', methods=['POST'])
    @_require_api_key(get_db)
    def ingest_ai_deals():
        try:
            data = request.get_json(force=True)
            deals = data.get('deals', [])
            batch_id = data.get('batch_id', datetime.utcnow().strftime('batch_%Y%m%d_%H%M%S'))
            if not deals:
                return jsonify({"error": "No deals provided"}), 400

            conn = get_db()
            try:
                cur = conn.cursor()
                inserted = 0
                updated = 0
                errors = []

                for i, deal in enumerate(deals):
                    try:
                        buyer = deal.get('buyer', '').strip()
                        seller = deal.get('seller', '').strip()
                        deal_date = deal.get('deal_date', '')
                        deal_value_str = deal.get('deal_value_str', 'Undisclosed')
                        if not buyer or not seller or not deal_date:
                            errors.append({"index": i, "error": "Missing buyer, seller, or deal_date"})
                            continue

                        deal_value_usd, display_str = _parse_value_str(deal_value_str)
                        dhash = _deal_hash(buyer, seller, deal_date, deal_value_str)

                        cur.execute("""
                            INSERT INTO ai_deals (deal_hash, buyer, seller, deal_value_usd, deal_value_str,
                                deal_type, confidence, deal_date, region, market, source_url, source_name,
                                description, ai_detected, status, ingestion_batch
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (deal_hash) DO UPDATE SET
                                deal_value_usd = COALESCE(EXCLUDED.deal_value_usd, ai_deals.deal_value_usd),
                                deal_value_str = COALESCE(EXCLUDED.deal_value_str, ai_deals.deal_value_str),
                                confidence = GREATEST(EXCLUDED.confidence, ai_deals.confidence),
                                source_url = COALESCE(EXCLUDED.source_url, ai_deals.source_url),
                                ingestion_batch = EXCLUDED.ingestion_batch
                            RETURNING (xmax = 0) AS is_insert
                        """, (dhash, buyer, seller, deal_value_usd, display_str,
                              deal.get('deal_type', 'unknown'), deal.get('confidence', 0.85),
                              deal_date, deal.get('region'), deal.get('market'),
                              deal.get('source_url'), deal.get('source_name'),
                              deal.get('description'), deal.get('ai_detected', True),
                              deal.get('status', 'detected'), batch_id))

                        is_insert = cur.fetchone()[0]
                        if is_insert:
                            inserted += 1
                        else:
                            updated += 1
                    except Exception as e:
                        errors.append({"index": i, "error": str(e)})

                conn.commit()
                logger.info(f"AI Deals ingestion: {inserted} inserted, {updated} updated, {len(errors)} errors (batch: {batch_id})")
                return jsonify({"success": True, "batch_id": batch_id, "inserted": inserted, "updated": updated, "errors": errors[:20], "total_processed": inserted + updated + len(errors)})
                except Exception as e:
                logger.error(f"POST /api/ai-deals/ingest failed: {e}")
                return jsonify({"error": str(e)}), 500

            finally:
                conn.close()
    # ── GET /api/ai-deals/health ──────────────────────────────
    @app.route('/api/ai-deals/health', methods=['GET'])
    def ai_deals_health():
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) AS total_deals, MAX(deal_date) AS latest_deal_date,
                           MAX(created_at) AS last_ingestion,
                           COUNT(CASE WHEN created_at > NOW() - INTERVAL '7 days' THEN 1 END) AS deals_last_7d,
                           COUNT(CASE WHEN created_at > NOW() - INTERVAL '24 hours' THEN 1 END) AS deals_last_24h
                    FROM ai_deals
                """)
                row = cur.fetchone()
                columns = [desc[0] for desc in cur.description]
                health = dict(zip(columns, row))
                for k, v in health.items():
                    if isinstance(v, (date, datetime)):
                        health[k] = v.isoformat()
                health['status'] = 'healthy' if health.get('total_deals', 0) > 0 else 'empty'
                return jsonify({"success": True, "health": health})
                except Exception as e:
                logger.error(f"GET /api/ai-deals/health failed: {e}")
                return jsonify({"error": str(e), "status": "error"}), 500

            finally:
                conn.close()
    logger.info("AI Deals API routes registered: /api/ai-deals/*")
