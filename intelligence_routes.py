"""
DC Hub Intelligence Routes Blueprint
======================================
Phase 5: Intelligence Enhancements

Provides:
  - /api/v1/intelligence/trends — facility growth trends over time
  - /api/v1/intelligence/market-compare — side-by-side market comparison
  - /api/v1/intelligence/portfolio/<provider> — operator portfolio tracker
  - /api/v1/intelligence/delivery-forecast — predictive delivery based on pipeline status
  - /api/v1/intelligence/market-velocity — fastest-growing markets by facility count

Tables used: discovered_facilities (primary), facilities (secondary)

discovered_facilities columns:
  id, source, source_id, name, provider, market, city, state, country, address,
  latitude, longitude, power_mw, sqft, status, facility_type, source_url,
  raw_data, discovered_at, merged_at, merged_facility_id, is_duplicate, confidence_score

Usage in main.py:
    from routes.intelligence_routes import intelligence_bp, init_intelligence_routes
    app.register_blueprint(intelligence_bp)
    init_intelligence_routes(get_pg_connection_fn, return_pg_connection_fn)
"""

import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger('intelligence')

intelligence_bp = Blueprint('intelligence_v2', __name__)

_get_pg = None
_return_pg = None


def init_intelligence_routes(get_pg_fn, return_pg_fn):
    global _get_pg, _return_pg
    _get_pg = get_pg_fn
    _return_pg = return_pg_fn
    logger.info("Intelligence routes (Phase 5) initialized")


def _conn():
    if _get_pg is None:
        raise RuntimeError("intelligence_routes not initialized")
    return _get_pg()


def _release(conn):
    if _return_pg and conn:
        try:
            _return_pg(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# 1. TREND ANALYSIS — facility growth over time
# =============================================================================

@intelligence_bp.route('/api/v1/intelligence/trends', methods=['GET'])
def facility_trends():
    """
    Facility growth trends — count of facilities discovered per month.

    Params:
        country: Filter by country code (e.g. US, DE, SG)
        market: Filter by market name (e.g. Northern Virginia)
        provider: Filter by operator
        granularity: month (default), quarter, year
        months: Lookback period in months (default 24)
    """
    country = request.args.get('country', '').upper()
    market = request.args.get('market', '').strip()
    provider = request.args.get('provider', '').strip()
    granularity = request.args.get('granularity', 'month').lower()
    months = min(request.args.get('months', 24, type=int), 120)

    if granularity == 'quarter':
        trunc = "date_trunc('quarter', discovered_at)"
        fmt = "to_char(date_trunc('quarter', discovered_at), 'YYYY-\"Q\"Q')"
    elif granularity == 'year':
        trunc = "date_trunc('year', discovered_at)"
        fmt = "to_char(date_trunc('year', discovered_at), 'YYYY')"
    else:
        trunc = "date_trunc('month', discovered_at)"
        fmt = "to_char(date_trunc('month', discovered_at), 'YYYY-MM')"

    conditions = ["discovered_at IS NOT NULL",
                   f"discovered_at >= NOW() - INTERVAL '{months} months'"]
    params = []

    if country:
        conditions.append("country = %s")
        params.append(country)
    if market:
        conditions.append("market ILIKE %s")
        params.append(f"%{market}%")
    if provider:
        conditions.append("provider ILIKE %s")
        params.append(f"%{provider}%")

    where = " AND ".join(conditions)

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        # Time series
        cur.execute(f"""
            SELECT {fmt} AS period,
                   COUNT(*) AS new_facilities,
                   COUNT(CASE WHEN power_mw IS NOT NULL AND power_mw > 0 THEN 1 END) AS with_power,
                   COALESCE(SUM(power_mw), 0) AS total_mw_added
            FROM discovered_facilities
            WHERE {where}
            GROUP BY {trunc}
            ORDER BY {trunc}
        """, params)
        cols = [d[0] for d in cur.description]
        trend_data = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Convert Decimal to float
        for row in trend_data:
            row['total_mw_added'] = float(row['total_mw_added'])

        # Running total
        running = 0
        for row in trend_data:
            running += row['new_facilities']
            row['cumulative_total'] = running

        # Summary
        cur.execute(f"""
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN power_mw > 0 THEN 1 END) AS with_power,
                   COALESCE(SUM(power_mw), 0) AS total_mw,
                   COUNT(DISTINCT provider) AS unique_providers,
                   COUNT(DISTINCT market) AS unique_markets
            FROM discovered_facilities
            WHERE {where}
        """, params)
        summary_row = cur.fetchone()
        summary_cols = [d[0] for d in cur.description]
        summary = dict(zip(summary_cols, summary_row))
        summary['total_mw'] = float(summary['total_mw'])

        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'filters': {
                    'country': country or None,
                    'market': market or None,
                    'provider': provider or None,
                    'granularity': granularity,
                    'months': months,
                },
                'summary': summary,
                'trend': trend_data,
            }
        })

    except Exception as e:
        logger.error(f"Trends error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# =============================================================================
# 2. MARKET COMPARISON — side-by-side market analysis
# =============================================================================

@intelligence_bp.route('/api/v1/intelligence/market-compare', methods=['GET'])
def market_compare():
    """
    Compare 2-5 markets side by side.

    Params:
        markets: Comma-separated market names (e.g. "Northern Virginia,Dallas,Phoenix")
    """
    markets_raw = request.args.get('markets', '').strip()
    if not markets_raw:
        return jsonify({
            'success': False,
            'error': 'markets param required (comma-separated, e.g. "Northern Virginia,Dallas,Phoenix")'
        }), 400

    markets = [m.strip() for m in markets_raw.split(',') if m.strip()][:5]

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        comparisons = []
        for market_name in markets:
            cur.execute("""
                SELECT
                    COUNT(*) AS total_facilities,
                    COUNT(CASE WHEN status = 'Operational' THEN 1 END) AS operational,
                    COUNT(CASE WHEN status IN ('Under Construction', 'Planned', 'In Development') THEN 1 END) AS pipeline,
                    COUNT(DISTINCT provider) AS unique_providers,
                    COALESCE(SUM(power_mw), 0) AS total_power_mw,
                    ROUND(AVG(confidence_score)::numeric, 3) AS avg_confidence,
                    COUNT(CASE WHEN power_mw IS NOT NULL AND power_mw > 0 THEN 1 END) AS facilities_with_power,
                    COALESCE(AVG(CASE WHEN power_mw > 0 THEN power_mw END), 0) AS avg_facility_mw,
                    COUNT(DISTINCT facility_type) AS facility_types,
                    COUNT(DISTINCT country) AS countries
                FROM discovered_facilities
                WHERE market ILIKE %s
            """, (f"%{market_name}%",))

            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            stats = dict(zip(cols, row))

            # Convert Decimals
            for k in ('total_power_mw', 'avg_confidence', 'avg_facility_mw'):
                stats[k] = float(stats[k]) if stats[k] else 0

            # Top providers in this market
            cur.execute("""
                SELECT provider, COUNT(*) AS count
                FROM discovered_facilities
                WHERE market ILIKE %s AND provider IS NOT NULL AND provider != ''
                GROUP BY provider
                ORDER BY count DESC
                LIMIT 5
            """, (f"%{market_name}%",))
            stats['top_providers'] = [{'name': r[0], 'count': r[1]} for r in cur.fetchall()]

            # Facility types breakdown
            cur.execute("""
                SELECT facility_type, COUNT(*) AS count
                FROM discovered_facilities
                WHERE market ILIKE %s AND facility_type IS NOT NULL AND facility_type != ''
                GROUP BY facility_type
                ORDER BY count DESC
            """, (f"%{market_name}%",))
            stats['facility_type_breakdown'] = [{'type': r[0], 'count': r[1]} for r in cur.fetchall()]

            comparisons.append({
                'market': market_name,
                'stats': stats,
            })

        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'markets_compared': len(comparisons),
                'comparisons': comparisons,
            }
        })

    except Exception as e:
        logger.error(f"Market compare error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# =============================================================================
# 3. OPERATOR PORTFOLIO TRACKER
# =============================================================================

@intelligence_bp.route('/api/v1/intelligence/portfolio/<path:provider_name>', methods=['GET'])
def operator_portfolio(provider_name):
    """
    Full portfolio view for an operator — all facilities, geographic spread,
    aggregate stats, market presence.

    Params (URL path):
        provider_name: Operator name (e.g. "Equinix" or "Digital Realty")
    Params (query):
        include_facilities: Include full facility list (default true)
        limit: Max facilities to return (default 100)
    """
    include_facilities = request.args.get('include_facilities', 'true').lower() == 'true'
    limit = min(request.args.get('limit', 100, type=int), 500)

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        # Aggregate stats
        cur.execute("""
            SELECT
                COUNT(*) AS total_facilities,
                COUNT(CASE WHEN status = 'Operational' THEN 1 END) AS operational,
                COUNT(CASE WHEN status IN ('Under Construction', 'Planned', 'In Development') THEN 1 END) AS pipeline,
                COUNT(DISTINCT country) AS countries,
                COUNT(DISTINCT state) AS us_states,
                COUNT(DISTINCT market) AS markets,
                COUNT(DISTINCT city) AS cities,
                COALESCE(SUM(power_mw), 0) AS total_power_mw,
                COALESCE(AVG(CASE WHEN power_mw > 0 THEN power_mw END), 0) AS avg_facility_mw,
                COALESCE(MAX(power_mw), 0) AS max_facility_mw,
                ROUND(AVG(confidence_score)::numeric, 3) AS avg_confidence,
                COUNT(CASE WHEN power_mw IS NOT NULL AND power_mw > 0 THEN 1 END) AS facilities_with_power,
                COUNT(CASE WHEN sqft IS NOT NULL AND sqft > 0 THEN 1 END) AS facilities_with_sqft,
                COALESCE(SUM(sqft), 0) AS total_sqft
            FROM discovered_facilities
            WHERE provider ILIKE %s
        """, (f"%{provider_name}%",))

        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        stats = dict(zip(cols, row))
        for k in ('total_power_mw', 'avg_facility_mw', 'max_facility_mw', 'avg_confidence', 'total_sqft'):
            stats[k] = float(stats[k]) if stats[k] else 0

        if stats['total_facilities'] == 0:
            cur.close()
            return jsonify({'success': False, 'error': f'No facilities found for provider "{provider_name}"'}), 404

        # Geographic breakdown by country
        cur.execute("""
            SELECT country, COUNT(*) AS count, COALESCE(SUM(power_mw), 0) AS power_mw
            FROM discovered_facilities
            WHERE provider ILIKE %s AND country IS NOT NULL AND country != ''
            GROUP BY country
            ORDER BY count DESC
        """, (f"%{provider_name}%",))
        geo_country = [{'country': r[0], 'count': r[1], 'power_mw': float(r[2])} for r in cur.fetchall()]

        # Market presence
        cur.execute("""
            SELECT market, COUNT(*) AS count, COALESCE(SUM(power_mw), 0) AS power_mw
            FROM discovered_facilities
            WHERE provider ILIKE %s AND market IS NOT NULL AND market != ''
            GROUP BY market
            ORDER BY count DESC
            LIMIT 20
        """, (f"%{provider_name}%",))
        markets = [{'market': r[0], 'count': r[1], 'power_mw': float(r[2])} for r in cur.fetchall()]

        # Status breakdown
        cur.execute("""
            SELECT status, COUNT(*) AS count
            FROM discovered_facilities
            WHERE provider ILIKE %s AND status IS NOT NULL
            GROUP BY status
            ORDER BY count DESC
        """, (f"%{provider_name}%",))
        statuses = [{'status': r[0], 'count': r[1]} for r in cur.fetchall()]

        # Facility type breakdown
        cur.execute("""
            SELECT facility_type, COUNT(*) AS count
            FROM discovered_facilities
            WHERE provider ILIKE %s AND facility_type IS NOT NULL AND facility_type != ''
            GROUP BY facility_type
            ORDER BY count DESC
        """, (f"%{provider_name}%",))
        types = [{'type': r[0], 'count': r[1]} for r in cur.fetchall()]

        result = {
            'provider': provider_name,
            'summary': stats,
            'geographic_presence': geo_country,
            'market_presence': markets,
            'status_breakdown': statuses,
            'facility_types': types,
        }

        # Facility list
        if include_facilities:
            cur.execute("""
                SELECT id, name, city, state, country, market, power_mw, sqft,
                       status, facility_type, latitude, longitude, confidence_score
                FROM discovered_facilities
                WHERE provider ILIKE %s
                ORDER BY power_mw DESC NULLS LAST, name
                LIMIT %s
            """, (f"%{provider_name}%", limit))
            fac_cols = [d[0] for d in cur.description]
            facilities = []
            for r in cur.fetchall():
                fac = dict(zip(fac_cols, r))
                fac['power_mw'] = float(fac['power_mw']) if fac['power_mw'] else None
                fac['sqft'] = float(fac['sqft']) if fac['sqft'] else None
                fac['confidence_score'] = float(fac['confidence_score']) if fac['confidence_score'] else 0
                facilities.append(fac)
            result['facilities'] = facilities

        cur.close()

        return jsonify({'success': True, 'data': result})

    except Exception as e:
        logger.error(f"Portfolio error for {provider_name}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# =============================================================================
# 4. MARKET VELOCITY — fastest-growing markets
# =============================================================================

@intelligence_bp.route('/api/v1/intelligence/market-velocity', methods=['GET'])
def market_velocity():
    """
    Fastest-growing markets by facility additions in the last N months.

    Params:
        months: Lookback period (default 6)
        min_facilities: Minimum facilities to qualify (default 5)
        limit: Max markets to return (default 20)
        country: Filter by country code
    """
    months = min(request.args.get('months', 6, type=int), 60)
    min_fac = request.args.get('min_facilities', 5, type=int)
    limit = min(request.args.get('limit', 20, type=int), 50)
    country = request.args.get('country', '').upper()

    conditions = ["market IS NOT NULL AND market != ''"]
    params = []

    if country:
        conditions.append("country = %s")
        params.append(country)

    where = " AND ".join(conditions)

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        params_full = params + [months, min_fac, limit]

        cur.execute(f"""
            WITH recent AS (
                SELECT market,
                       COUNT(*) AS recent_additions
                FROM discovered_facilities
                WHERE {where}
                  AND discovered_at >= NOW() - INTERVAL '%s months'
                GROUP BY market
            ),
            totals AS (
                SELECT market,
                       COUNT(*) AS total_facilities,
                       COUNT(DISTINCT provider) AS providers,
                       COALESCE(SUM(power_mw), 0) AS total_mw,
                       COUNT(CASE WHEN status IN ('Under Construction', 'Planned', 'In Development') THEN 1 END) AS pipeline
                FROM discovered_facilities
                WHERE {where}
                GROUP BY market
            )
            SELECT t.market,
                   t.total_facilities,
                   COALESCE(r.recent_additions, 0) AS recent_additions,
                   ROUND(COALESCE(r.recent_additions, 0)::numeric / GREATEST(t.total_facilities, 1) * 100, 1) AS growth_pct,
                   t.providers,
                   t.total_mw,
                   t.pipeline
            FROM totals t
            LEFT JOIN recent r ON t.market = r.market
            WHERE t.total_facilities >= %s
            ORDER BY COALESCE(r.recent_additions, 0) DESC, t.total_facilities DESC
            LIMIT %s
        """, params_full)

        cols = [d[0] for d in cur.description]
        markets = []
        for r in cur.fetchall():
            m = dict(zip(cols, r))
            m['total_mw'] = float(m['total_mw'])
            m['growth_pct'] = float(m['growth_pct'])
            markets.append(m)

        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'period_months': months,
                'min_facilities_threshold': min_fac,
                'country_filter': country or None,
                'markets': markets,
            }
        })

    except Exception as e:
        logger.error(f"Market velocity error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# =============================================================================
# 5. DELIVERY FORECAST — pipeline status analysis
# =============================================================================

@intelligence_bp.route('/api/v1/intelligence/delivery-forecast', methods=['GET'])
def delivery_forecast():
    """
    Pipeline delivery forecast — facilities under construction / planned,
    grouped by market and estimated delivery window.

    Params:
        country: Filter by country code
        market: Filter by market name
    """
    country = request.args.get('country', '').upper()
    market = request.args.get('market', '').strip()

    conditions = ["status IN ('Under Construction', 'Planned', 'In Development', 'Pre-Construction')"]
    params = []

    if country:
        conditions.append("country = %s")
        params.append(country)
    if market:
        conditions.append("market ILIKE %s")
        params.append(f"%{market}%")

    where = " AND ".join(conditions)

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        # Pipeline summary by market
        cur.execute(f"""
            SELECT
                COALESCE(market, city, 'Unknown') AS market_name,
                country,
                status,
                COUNT(*) AS count,
                COALESCE(SUM(power_mw), 0) AS total_mw,
                COALESCE(AVG(power_mw), 0) AS avg_mw
            FROM discovered_facilities
            WHERE {where}
            GROUP BY COALESCE(market, city, 'Unknown'), country, status
            ORDER BY total_mw DESC, count DESC
        """, params)
        cols = [d[0] for d in cur.description]
        by_market = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            row['total_mw'] = float(row['total_mw'])
            row['avg_mw'] = float(row['avg_mw'])
            by_market.append(row)

        # Pipeline by provider
        cur.execute(f"""
            SELECT
                provider,
                COUNT(*) AS count,
                COALESCE(SUM(power_mw), 0) AS total_mw,
                COUNT(DISTINCT COALESCE(market, city)) AS markets
            FROM discovered_facilities
            WHERE {where}
              AND provider IS NOT NULL AND provider != ''
            GROUP BY provider
            ORDER BY total_mw DESC, count DESC
            LIMIT 20
        """, params)
        cols2 = [d[0] for d in cur.description]
        by_provider = []
        for r in cur.fetchall():
            row = dict(zip(cols2, r))
            row['total_mw'] = float(row['total_mw'])
            by_provider.append(row)

        # Overall pipeline stats
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_pipeline,
                COALESCE(SUM(power_mw), 0) AS total_pipeline_mw,
                COUNT(DISTINCT provider) AS providers,
                COUNT(DISTINCT COALESCE(market, city)) AS markets,
                COUNT(DISTINCT country) AS countries
            FROM discovered_facilities
            WHERE {where}
        """, params)
        summary_row = cur.fetchone()
        summary_cols = [d[0] for d in cur.description]
        summary = dict(zip(summary_cols, summary_row))
        summary['total_pipeline_mw'] = float(summary['total_pipeline_mw'])

        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'filters': {
                    'country': country or None,
                    'market': market or None,
                },
                'summary': summary,
                'by_market': by_market,
                'by_provider': by_provider,
            }
        })

    except Exception as e:
        logger.error(f"Delivery forecast error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# =============================================================================
# 6. TOP OPERATORS — global leaderboard
# =============================================================================

@intelligence_bp.route('/api/v1/intelligence/top-operators', methods=['GET'])
def top_operators():
    """
    Top operators ranked by facility count, power capacity, or geographic reach.

    Params:
        sort: count (default), power, markets, countries
        country: Filter by country code
        limit: Max results (default 25)
    """
    sort_by = request.args.get('sort', 'count').lower()
    country = request.args.get('country', '').upper()
    limit = min(request.args.get('limit', 25, type=int), 100)

    conditions = ["provider IS NOT NULL AND provider != ''"]
    params = []

    if country:
        conditions.append("country = %s")
        params.append(country)

    where = " AND ".join(conditions)

    sort_map = {
        'count': 'total_facilities DESC',
        'power': 'total_mw DESC',
        'markets': 'markets DESC',
        'countries': 'countries DESC',
    }
    order = sort_map.get(sort_by, 'total_facilities DESC')

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()

        cur.execute(f"""
            SELECT
                provider,
                COUNT(*) AS total_facilities,
                COUNT(CASE WHEN status = 'Operational' THEN 1 END) AS operational,
                COUNT(CASE WHEN status IN ('Under Construction', 'Planned', 'In Development') THEN 1 END) AS pipeline,
                COALESCE(SUM(power_mw), 0) AS total_mw,
                COUNT(DISTINCT country) AS countries,
                COUNT(DISTINCT market) AS markets,
                COUNT(DISTINCT city) AS cities,
                ROUND(AVG(confidence_score)::numeric, 3) AS avg_confidence
            FROM discovered_facilities
            WHERE {where}
            GROUP BY provider
            HAVING COUNT(*) >= 2
            ORDER BY {order}
            LIMIT %s
        """, params + [limit])

        cols = [d[0] for d in cur.description]
        operators = []
        for rank, r in enumerate(cur.fetchall(), 1):
            op = dict(zip(cols, r))
            op['rank'] = rank
            op['total_mw'] = float(op['total_mw'])
            op['avg_confidence'] = float(op['avg_confidence']) if op['avg_confidence'] else 0
            operators.append(op)

        cur.close()

        return jsonify({
            'success': True,
            'data': {
                'sort_by': sort_by,
                'country_filter': country or None,
                'operators': operators,
            }
        })

    except Exception as e:
        logger.error(f"Top operators error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _release(conn)


# ---------------------------------------------------------------------------
print("   🧠 Intelligence routes (Phase 5) loaded:")
print("      /api/v1/intelligence/trends")
print("      /api/v1/intelligence/market-compare?markets=...")
print("      /api/v1/intelligence/portfolio/<provider>")
print("      /api/v1/intelligence/market-velocity")
print("      /api/v1/intelligence/delivery-forecast")
print("      /api/v1/intelligence/top-operators")
