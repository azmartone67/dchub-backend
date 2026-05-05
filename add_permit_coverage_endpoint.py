"""
add_permit_coverage_endpoint.py
================================
Adds GET /api/facilities/permit-coverage to main.py.
Returns live stats for the research page — no auth required.
Run from ~/workspace on Railway.
"""
import pathlib

MAIN_PY = pathlib.Path('/home/runner/workspace/main.py')

ENDPOINT = '''
@app.route('/api/facilities/permit-coverage', methods=['GET'])
def permit_coverage_stats():
    """
    Public endpoint — returns permit date coverage stats for research page.
    No auth required. Cached-friendly (add Cache-Control header).
    """
    conn = None
    try:
        conn = get_read_db()
        cur = conn.cursor()

        # Total facilities with permit_date
        cur.execute("SELECT COUNT(*) FROM facilities WHERE permit_date IS NOT NULL")
        total = cur.fetchone()[0]

        # Breakdown by source
        cur.execute("""
            SELECT permit_source, COUNT(*) as cnt
            FROM facilities
            WHERE permit_date IS NOT NULL AND permit_source IS NOT NULL
            GROUP BY permit_source
            ORDER BY cnt DESC
            LIMIT 10
        """)
        sources = [{"source": r[0], "count": r[1]} for r in cur.fetchall()]

        # Market breakdown (US only)
        cur.execute("""
            SELECT city, state, COUNT(*) as cnt
            FROM facilities
            WHERE permit_date IS NOT NULL
              AND country = 'US'
              AND city IS NOT NULL
            GROUP BY city, state
            ORDER BY cnt DESC
            LIMIT 20
        """)
        markets = [{"city": r[0], "state": r[1], "count": r[2]} for r in cur.fetchall()]

        # Average confidence
        cur.execute("""
            SELECT ROUND(AVG(permit_confidence)::numeric, 3)
            FROM facilities
            WHERE permit_date IS NOT NULL AND permit_confidence IS NOT NULL
        """)
        avg_conf = float(cur.fetchone()[0] or 0)

        # Total facilities
        cur.execute("SELECT COUNT(*) FROM facilities")
        total_facilities = cur.fetchone()[0]

        # US facilities with permit_date
        cur.execute("""
            SELECT COUNT(*) FROM facilities
            WHERE permit_date IS NOT NULL AND country = 'US'
        """)
        us_count = cur.fetchone()[0]

        # US total
        cur.execute("SELECT COUNT(*) FROM facilities WHERE country = 'US'")
        us_total = cur.fetchone()[0]

        return jsonify({
            "success": True,
            "count": total,
            "us_count": us_count,
            "us_total": us_total,
            "us_coverage_pct": round(us_count / us_total * 100, 1) if us_total else 0,
            "avg_confidence": avg_conf,
            "total_facilities": total_facilities,
            "sources": sources,
            "markets": markets,
            "updated_at": __import__('datetime').datetime.utcnow().isoformat() + "Z"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except: pass

'''

# Find anchor — insert before the permit_scraper job endpoint
t = MAIN_PY.read_text()

ANCHOR = "@app.route('/api/jobs/permit-scraper'"

if '/api/facilities/permit-coverage' in t:
    print("✓ ALREADY EXISTS — endpoint already in main.py")
elif ANCHOR in t:
    MAIN_PY.write_text(t.replace(ANCHOR, ENDPOINT + ANCHOR, 1))
    print("✓ APPLIED — /api/facilities/permit-coverage added before permit-scraper job")
else:
    # Fallback: find any @app.route and insert before first one in bottom half
    import re
    routes = list(re.finditer(r"^@app\.route\('/api/", t, re.MULTILINE))
    if routes:
        # Insert before the last quarter of routes
        insert_at = routes[len(routes)//2].start()
        MAIN_PY.write_text(t[:insert_at] + ENDPOINT + t[insert_at:])
        print("✓ APPLIED (fallback) — endpoint inserted")
    else:
        print("✗ FAILED — could not find anchor")

# Verify
t2 = MAIN_PY.read_text()
if '/api/facilities/permit-coverage' in t2:
    print("✓ VERIFIED — endpoint present in main.py")
    print("\nNext: git add main.py && git commit -m 'Add permit-coverage stats endpoint' && git push")
else:
    print("✗ VERIFY FAILED")
