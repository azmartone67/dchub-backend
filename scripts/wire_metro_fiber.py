#!/usr/bin/env python3
"""
Metro Dark Fiber API + MCP Wiring Patch
========================================
Run in Railway workspace shell:
  python3 /tmp/wire_metro_fiber.py

This script patches:
1. main.py — adds /api/v1/fiber/metro endpoint + updates free tier teaser
2. dchub_mcp_server.py — adds metro dark fiber to get_fiber_intel responses

IMPORTANT: Review the output, then git commit + push to deploy.
"""

import os, sys, re

# ── PATCH 1: Add /api/v1/fiber/metro endpoint to main.py ──
# Insert after the existing fiber_routes_api function (around line 9800)

METRO_FIBER_ENDPOINT = '''
@app.route('/api/v1/fiber/metro', methods=['GET'])
@app.route('/api/v1/fiber/metro/<market_name>', methods=['GET'])
def fiber_metro_api(market_name=None):
    """Metro dark fiber intelligence by market — carriers, route miles, density scores."""
    try:
        conn = _pg_connection()
        cur = conn.cursor()
        
        if market_name:
            # Single market detail
            cur.execute("""
                SELECT carrier, route_miles_approx, on_net_buildings, key_endpoints,
                       services, notes, source, fiber_type
                FROM metro_dark_fiber WHERE LOWER(market) = LOWER(%s)
                ORDER BY route_miles_approx DESC
            """, (market_name.replace('-', ' '),))
            cols = ['carrier','route_miles_approx','on_net_buildings','key_endpoints','services','notes','source','fiber_type']
            carriers = [dict(zip(cols, r)) for r in cur.fetchall()]
            
            cur.execute("""
                SELECT market, state, total_carriers, total_route_miles_approx,
                       total_on_net_buildings, fiber_density_score, tier,
                       key_ix_points, key_carrier_hotels, notes
                FROM metro_fiber_summary WHERE LOWER(market) = LOWER(%s)
            """, (market_name.replace('-', ' '),))
            row = cur.fetchone()
            summary = None
            if row:
                summary = {
                    'market': row[0], 'state': row[1], 'total_carriers': row[2],
                    'total_route_miles': row[3], 'total_on_net_buildings': row[4],
                    'fiber_density_score': row[5], 'tier': row[6],
                    'key_ix_points': row[7], 'key_carrier_hotels': row[8], 'notes': row[9]
                }
            cur.close()
            return jsonify({'success': True, 'market': market_name, 'summary': summary, 'carriers': carriers})
        
        else:
            # All markets summary
            carrier_filter = request.args.get('carrier', '')
            cur.execute("""
                SELECT market, state, total_carriers, total_route_miles_approx,
                       total_on_net_buildings, fiber_density_score, tier
                FROM metro_fiber_summary
                ORDER BY fiber_density_score DESC
            """)
            cols = ['market','state','total_carriers','total_route_miles','total_on_net_buildings','fiber_density_score','tier']
            markets = [dict(zip(cols, r)) for r in cur.fetchall()]
            
            if carrier_filter:
                cur.execute("""
                    SELECT market, route_miles_approx, on_net_buildings, services
                    FROM metro_dark_fiber WHERE LOWER(carrier) = LOWER(%s)
                    ORDER BY route_miles_approx DESC
                """, (carrier_filter,))
                cols2 = ['market','route_miles_approx','on_net_buildings','services']
                carrier_markets = [dict(zip(cols2, r)) for r in cur.fetchall()]
                cur.close()
                return jsonify({'success': True, 'carrier': carrier_filter, 'markets': carrier_markets, 'total_markets': len(carrier_markets)})
            
            cur.execute("SELECT COUNT(*), SUM(route_miles_approx) FROM metro_dark_fiber")
            row = cur.fetchone()
            cur.close()
            return jsonify({
                'success': True,
                'markets': markets,
                'total_markets': len(markets),
                'total_carrier_market_records': row[0] or 0,
                'total_route_miles': row[1] or 0,
                'source': 'DC Hub Metro Dark Fiber Intelligence (dchub.cloud)'
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
'''

# ── PATCH 2: Update get_fiber_intel in dchub_mcp_server.py ──
# Replace the existing handler to also query metro dark fiber

MCP_HANDLER_OLD = '''    results["routes"] = _api_get("/api/v1/infrastructure/fiber", route_params)
    # Get carrier sources summary from connectivity_providers
    if include_sources:
        results["sources"] = _api_get("/api/v1/fiber/sources")
    results["source"] = "DC Hub Fiber Intelligence (dchub.cloud)"
    return json.dumps(results, indent=2)'''

MCP_HANDLER_NEW = '''    results["routes"] = _api_get("/api/v1/infrastructure/fiber", route_params)
    # Get carrier sources summary from connectivity_providers
    if include_sources:
        results["sources"] = _api_get("/api/v1/fiber/sources")
    # Metro dark fiber intelligence (market-level carrier data)
    metro_params = {}
    if carrier:
        metro_params["carrier"] = carrier
    metro_data = _api_get("/api/v1/fiber/metro", metro_params)
    if metro_data and metro_data.get("success"):
        results["metro_dark_fiber"] = {
            "markets": metro_data.get("markets", metro_data.get("carrier_markets", [])),
            "total_markets": metro_data.get("total_markets", 0),
            "total_route_miles": metro_data.get("total_route_miles", 0),
        }
    results["source"] = "DC Hub Fiber Intelligence (dchub.cloud)"
    return json.dumps(results, indent=2)'''

# ── PATCH 3: Update free tier teaser in main.py ──

TEASER_OLD = '''        elif tool_name == 'get_fiber_intel':
            teaser = {
                '_user_facing_note': MCP_USER_NOTES['get_fiber_intel'],
                'success': True,
                'carriers_available': '██ upgrade to see carrier details',
                'total_routes': '██',
                '_upgrade': {
                    'tier': 'free_teaser',
                    'message': "Fiber intelligence preview — Developer plan ($49/mo) unlocks full dark fiber routes, carrier networks, and connectivity scoring.",
                    'url': 'https://dchub.cloud/pricing#developer',
                    'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
                    'price': '$49/mo',
                }
            }
            return [{"type": "text", "text": json.dumps(teaser)}]'''

TEASER_NEW = '''        elif tool_name == 'get_fiber_intel':
            # Free tier: show metro fiber market rankings as teaser
            metro_preview = []
            try:
                pg = _pg_connection()
                mc = pg.cursor()
                mc.execute("SELECT market, total_carriers, fiber_density_score, tier FROM metro_fiber_summary ORDER BY fiber_density_score DESC LIMIT 5")
                for r in mc.fetchall():
                    metro_preview.append({"market": r[0], "carriers": r[1], "density_score": r[2], "tier": r[3]})
                mc.close()
            except Exception:
                pass
            teaser = {
                '_user_facing_note': MCP_USER_NOTES['get_fiber_intel'],
                'success': True,
                'metro_fiber_preview': metro_preview,
                'carriers_available': '██ upgrade to see carrier details',
                'total_routes': '██',
                '_upgrade': {
                    'tier': 'free_teaser',
                    'message': "Fiber intelligence preview — Developer plan ($49/mo) unlocks full dark fiber routes, carrier networks, metro fiber density, and connectivity scoring across 19 US markets.",
                    'url': 'https://dchub.cloud/pricing#developer',
                    'checkout': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
                    'price': '$49/mo',
                }
            }
            return [{"type": "text", "text": json.dumps(teaser)}]'''


def apply_patches():
    print("=" * 60)
    print("  Metro Dark Fiber API + MCP Wiring")
    print("=" * 60)
    
    # ── Patch main.py: Add metro fiber endpoint ──
    main_path = 'main.py'
    with open(main_path, 'r') as f:
        main_content = f.read()
    
    # Find insertion point: after fiber_routes_api function
    marker = "def fiber_routes_api():"
    if marker not in main_content:
        print(f"  WARNING: Could not find '{marker}' in main.py")
        print("  Skipping endpoint insertion — add manually")
    else:
        # Check if already patched
        if 'fiber_metro_api' in main_content:
            print("  ⏭️  main.py already has fiber_metro_api endpoint")
        else:
            # Find the end of fiber_routes_api function by looking for next @app.route
            idx = main_content.index(marker)
            # Find next route decorator after this function
            next_route = main_content.find('\n@app.route', idx + len(marker))
            if next_route > 0:
                main_content = main_content[:next_route] + '\n' + METRO_FIBER_ENDPOINT + main_content[next_route:]
                print("  ✅ Added /api/v1/fiber/metro endpoint to main.py")
            else:
                print("  WARNING: Could not find insertion point for endpoint")
    
    # ── Patch main.py: Update free tier teaser ──
    if TEASER_OLD in main_content:
        main_content = main_content.replace(TEASER_OLD, TEASER_NEW)
        print("  ✅ Updated get_fiber_intel free tier teaser with metro preview")
    elif 'metro_fiber_preview' in main_content:
        print("  ⏭️  Free tier teaser already has metro preview")
    else:
        print("  WARNING: Could not find fiber_intel teaser block to patch")
    
    with open(main_path, 'w') as f:
        f.write(main_content)
    
    # ── Patch dchub_mcp_server.py: Add metro data to handler ──
    mcp_path = 'dchub_mcp_server.py'
    with open(mcp_path, 'r') as f:
        mcp_content = f.read()
    
    if MCP_HANDLER_OLD.strip() in mcp_content:
        mcp_content = mcp_content.replace(MCP_HANDLER_OLD.strip(), MCP_HANDLER_NEW.strip())
        print("  ✅ Patched get_fiber_intel MCP handler with metro dark fiber data")
    elif 'metro_dark_fiber' in mcp_content:
        print("  ⏭️  MCP handler already has metro dark fiber")
    else:
        print("  WARNING: Could not find exact MCP handler block to patch")
        print("  Looking for partial match...")
        if 'results["routes"] = _api_get("/api/v1/infrastructure/fiber"' in mcp_content:
            print("  Found routes line — try manual patch")
        else:
            print("  Could not find any fiber handler in MCP server")
    
    with open(mcp_path, 'w') as f:
        f.write(mcp_content)
    
    print(f"\n{'=' * 60}")
    print("  Wiring Complete — Review + Deploy")
    print(f"{'=' * 60}")
    print("  1. Review: git diff main.py dchub_mcp_server.py")
    print("  2. Commit: git add -A && git commit -m 'Wire metro dark fiber to MCP + API'")
    print("  3. Push:   git push origin main")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    os.chdir(os.path.expanduser('~/workspace'))
    apply_patches()
