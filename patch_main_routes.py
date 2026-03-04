import re, shutil, os, sys

MAIN = '/home/runner/workspace/main.py'
BAK  = '/home/runner/workspace/main.py.bak'

print(f"Backing up {MAIN} → {BAK}")
shutil.copy2(MAIN, BAK)

with open(MAIN, 'r', encoding='utf-8') as f:
    src = f.read()

# PATCH 1 - Remove @require_plan('pro') from /api/deals
OLD = "@app.route('/api/deals', methods=['GET'])\n@require_plan('pro')\n@protect_data\ndef get_deals():"
NEW = "@app.route('/api/deals', methods=['GET'])\n@protect_data\ndef get_deals():"
if OLD in src:
    src = src.replace(OLD, NEW, 1)
    print("✓ PATCH 1: removed @require_plan from /api/deals")
else:
    print("⚠ PATCH 1 SKIPPED: pattern not found")

# PATCH 2 - Fix value formatting in deals
OLD2 = """                    db_deals.append({
                        'id': row[0], 'date': row[1], 'year': row[2],
                        'buyer': buyer, 'seller': seller, 'value': row[5],
                        'mw': row[6], 'type': row[7], 'region': row[8], 'market': row[9]
                    })"""
NEW2 = """                    val_m = float(row[5] or 0)
                    val_display = f"${val_m/1000:.1f}B" if val_m >= 1000 else (f"${val_m:.0f}M" if val_m > 0 else None)
                    db_deals.append({
                        'id': row[0], 'date': row[1], 'year': row[2],
                        'buyer': buyer, 'seller': seller,
                        'value': val_m,
                        'value_display': val_display,
                        'value_confirmed': val_m > 0,
                        'mw': row[6], 'type': row[7], 'region': row[8], 'market': row[9]
                    })"""
if OLD2 in src:
    src = src.replace(OLD2, NEW2, 1)
    print("✓ PATCH 2: value formatting fixed in /api/deals")
else:
    print("⚠ PATCH 2 SKIPPED: pattern not found")

# PATCH 3 - Add /api/market-intelligence if missing
if "'/api/market-intelligence'" not in src:
    ROUTE = '''
@app.route('/api/market-intelligence', methods=['GET'])
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
        import psycopg2.extras
        with pg_connection() as conn:
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
        markets = []
        for r in rows:
            v = r.get('vacancy_rate')
            g = r.get('growth_rate')
            markets.append({
                **dict(r),
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
            'success': True, 'markets': markets,
            'summary': {
                'total_markets':   agg.get('total_markets', len(rows)) if agg else len(rows),
                'avg_vacancy':     f"{float(av)*100:.1f}%" if av else 'N/A',
                'total_mw':        round(tm, 0),
                'tightest_market': tightest,
                'fastest_growing': fastest,
            },
            'regions': regions, 'filter': region or 'all', 'sort': sort_by,
        })
    except Exception as e:
        logger.error(f"market-intelligence error: {e}")
        return jsonify({'success': False, 'error': str(e), 'markets': []}), 500
'''
    INSERT_BEFORE = "if __name__ == '__main__':"
    if INSERT_BEFORE in src:
        src = src.replace(INSERT_BEFORE, ROUTE + INSERT_BEFORE, 1)
    else:
        src += ROUTE
    print("✓ PATCH 3: /api/market-intelligence route added")
else:
    print("✓ PATCH 3 SKIPPED: route already exists")

with open(MAIN, 'w', encoding='utf-8') as f:
    f.write(src)

print(f"\n✅ Done. Backup at {BAK}")
