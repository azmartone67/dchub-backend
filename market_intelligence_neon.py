from flask import Blueprint, jsonify, request
import os, psycopg2, psycopg2.extras

market_intel_neon_bp = Blueprint('market_intelligence_neon', __name__)

def get_neon():
    return psycopg2.connect(os.environ['DATABASE_URL'])

# AUTO-REPAIR: duplicate route '/api/market-intelligence' also in main.py:15052 — review and remove one
@market_intel_neon_bp.route('/api/market-intelligence', methods=['GET'])
def get_market_intelligence():
    region  = request.args.get('region')
    sort_by = request.args.get('sort', 'vacancy_asc')
    SORT_MAP = {
        'vacancy_asc':     'vacancy_rate ASC NULLS LAST',
        'vacancy_desc':    'vacancy_rate DESC NULLS LAST',
        'growth_desc':     'yoy_price_change DESC NULLS LAST',
        'price_desc':      'avg_asking_rate DESC NULLS LAST',
        'price_asc':       'avg_asking_rate ASC NULLS LAST',
        'absorption_desc': 'absorption_mw DESC NULLS LAST',
        'facilities_desc': 'facility_count DESC NULLS LAST',
        'pipeline_desc':   'under_construction_mw DESC NULLS LAST',
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
                   vacancy_rate, growth_rate, avg_asking_rate, asking_rate_unit,
                   yoy_price_change, absorption_mw, absorption_period,
                   under_construction_mw, pre_leased_pct,
                   power_constraints, key_operators, trends, last_updated
            FROM market_intelligence {where} ORDER BY {order}
        """, params)
        rows = cur.fetchall()
        cur.execute("""
            SELECT COUNT(*) AS total_markets,
                   AVG(vacancy_rate) AS avg_vacancy,
                   SUM(total_mw) AS total_mw,
                   SUM(absorption_mw) AS total_absorption,
                   SUM(under_construction_mw) AS total_pipeline
            FROM market_intelligence
        """)
        agg = cur.fetchone()
        cur.execute("SELECT DISTINCT region FROM market_intelligence WHERE region IS NOT NULL ORDER BY region")
        regions = [r['region'] for r in cur.fetchall()]
        cur.close(); conn.close()

        markets = []
        for r in rows:
            v   = float(r.get('vacancy_rate') or 0)
            yoy = float(r.get('yoy_price_change') or 0)
            markets.append({
                **dict(r),
                'market':            r['market_name'],
                'vacancy_rate':      v,
                'vacancy_display':   f"{v:.1f}%",
                'avg_asking_rate':   r.get('avg_asking_rate'),
                'asking_rate_unit':  r.get('asking_rate_unit') or '$/kW/mo',
                'yoy_price_change':  yoy,
                'yoy_display':       f"+{yoy:.1f}%" if yoy > 0 else f"{yoy:.1f}%",
                'absorption_mw':     r.get('absorption_mw') or 0,
                'absorption_period': r.get('absorption_period') or 'H1 2025',
                'under_construction_mw': r.get('under_construction_mw') or 0,
                'pre_leased_pct':    r.get('pre_leased_pct') or 0,
                'num_facilities':    r.get('facility_count') or 0,
                'inventory_mw':      r.get('total_mw') or 0,
                'top_providers':     [p.strip() for p in (r.get('key_operators') or '').split(',')][:5],
            })

        valid_v = [m for m in markets if m['vacancy_rate'] > 0]
        valid_g = [m for m in markets if m['yoy_price_change'] > 0]
        tightest = min(valid_v, key=lambda m: m['vacancy_rate'])['market_name'] if valid_v else 'N/A'
        fastest  = max(valid_g, key=lambda m: m['yoy_price_change'])['market_name'] if valid_g else 'N/A'
        av  = float(agg.get('avg_vacancy') or 0) if agg else 0
        tm  = float(agg.get('total_mw') or 0) if agg else 0
        tab = float(agg.get('total_absorption') or 0) if agg else 0
        tpp = float(agg.get('total_pipeline') or 0) if agg else 0

        return jsonify({
            'success':     True,
            'markets':     markets,
            'count':       len(markets),
            'last_updated': 'CBRE/JLL H1 2025',
            'summary': {
                'total_markets':        len(markets),
                'avg_vacancy':          f"{av:.1f}%",
                'total_inventory_mw':   round(tm, 0),
                'total_absorption_mw':  round(tab, 0),
                'total_pipeline_mw':    round(tpp, 0),
                'tightest_market':      tightest,
                'tightest_vacancy':     f"{min(valid_v, key=lambda m: m['vacancy_rate'])['vacancy_rate']:.1f}%" if valid_v else 'N/A',
                'fastest_growing':      fastest,
                'fastest_yoy':          f"+{max(valid_g, key=lambda m: m['yoy_price_change'])['yoy_price_change']:.1f}%" if valid_g else 'N/A',
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
