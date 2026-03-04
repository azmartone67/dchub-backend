"""
Neon-backed market intelligence — replaces hardcoded MARKET_DATA
Register in main.py: from market_intelligence_neon import market_intel_neon_bp
                     app.register_blueprint(market_intel_neon_bp)
"""
from flask import Blueprint, jsonify, request
import os, psycopg2, psycopg2.extras

market_intel_neon_bp = Blueprint('market_intelligence_neon', __name__)

def get_neon():
    return psycopg2.connect(os.environ['DATABASE_URL'])

@market_intel_neon_bp.route('/api/market-intelligence', methods=['GET'])
def get_market_intelligence():
    region  = request.args.get('region')
    sort_by = request.args.get('sort', 'vacancy_asc')
    SORT_MAP = {
        'vacancy_asc':     'vacancy_rate ASC NULLS LAST',
        'vacancy_desc':    'vacancy_rate DESC NULLS LAST',
        'growth_desc':     'growth_rate DESC NULLS LAST',
        'facilities_desc': 'facility_count DESC NULLS LAST',
    }
    order = SORT_MAP.get(sort_by, 'vacancy_rate ASC NULLS LAST')
    try:
        conn = get_neon()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        conditions, params = [], []
        if region and region.lower() != 'all':
            conditions.append("region = %s")
            params.append(region)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"""
            SELECT id, market_name, region, facility_count, total_mw,
                   vacancy_rate, growth_rate, power_constraints,
                   key_operators, trends, last_updated
            FROM market_intelligence {where} ORDER BY {order}
        """, params)
        rows = cur.fetchall()
        cur.execute("""
            SELECT COUNT(*) AS total_markets, AVG(vacancy_rate) AS avg_vacancy,
                   SUM(total_mw) AS total_mw, MAX(growth_rate) AS max_growth
            FROM market_intelligence
        """)
        agg = cur.fetchone()
        cur.execute("SELECT DISTINCT region FROM market_intelligence WHERE region IS NOT NULL ORDER BY region")
        regions = [r['region'] for r in cur.fetchall()]
        cur.close(); conn.close()

        markets = []
        for r in rows:
            v = r.get('vacancy_rate')
            g = r.get('growth_rate')
            markets.append({
                **dict(r),
                'market': r['market_name'],
                'vacancy_display': f"{float(v)*100:.1f}%" if v is not None else 'N/A',
                'growth_display':  f"+{float(g)*100:.1f}%" if g and g > 0 else (f"{float(g)*100:.1f}%" if g is not None else 'N/A'),
            })

        valid_v = [r for r in rows if r.get('vacancy_rate') is not None]
        valid_g = [r for r in rows if r.get('growth_rate') is not None]
        tightest = min(valid_v, key=lambda r: r['vacancy_rate'])['market_name'] if valid_v else 'N/A'
        fastest  = max(valid_g, key=lambda r: r['growth_rate'])['market_name'] if valid_g else 'N/A'
        av = agg.get('avg_vacancy') if agg else None
        tm = float(agg.get('total_mw') or 0) if agg else 0

        return jsonify({
            'success': True,
            'markets': markets,
            'count': len(markets),
            'summary': {
                'total_markets':   len(markets),
                'avg_vacancy':     f"{float(av)*100:.1f}%" if av else 'N/A',
                'total_mw':        round(tm, 0),
                'tightest_market': tightest,
                'fastest_growing': fastest,
            },
            'regions': regions,
            'filter':  region or 'all',
            'sort':    sort_by,
            'source':  'neon',
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'markets': []}), 500


@market_intel_neon_bp.route('/api/market-intelligence/<market_name>', methods=['GET'])
def get_market_detail(market_name):
    try:
        conn = get_neon()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM market_intelligence WHERE lower(market_name) = lower(%s)", [market_name])
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return jsonify({'success': False, 'error': 'Market not found'}), 404
        return jsonify({'success': True, 'market': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
