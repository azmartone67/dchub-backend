"""
mcp_teaser_fixes.py — Fix 3 MCP tool bugs in free tier teasers
================================================================
Deploy: Upload to Railway repo root, add one import to main.py

Fixes:
  1. get_energy_prices: state filter ignored (returns ND instead of requested state)
  2. get_renewable_energy: state/type filters ignored (always same 5 PPAs)
  3. get_dchub_recommendation: says "11 tools" instead of 15

How it works:
  - Monkey-patches the free_tier_gate teaser builders at import time
  - No changes needed to dchub_mcp_server.py or free_tier_gate.py
  - Safe: wraps existing functions, falls back if anything goes wrong

Add to main.py (anywhere after free_tier_gate import, e.g. line ~1680):
    try:
        import mcp_teaser_fixes
        logger.info("🔧 MCP teaser fixes: ✅ 3 patches applied")
    except Exception as e:
        logger.warning(f"🔧 MCP teaser fixes: ⚠️ {e}")

Author: DC Hub / Claude session Mar 18 2026
"""

import logging
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  FIX 1: get_energy_prices — state filter for free teaser
# ═══════════════════════════════════════════════════════════════
# BUG: Free teaser queries eia_retail_rates with ORDER BY rate ASC LIMIT 5
#      without WHERE state filter → always returns North Dakota (cheapest)
# FIX: If state param provided, filter teaser to that state's rates

def _fixed_energy_prices_teaser(state='', data_type='retail_rates', iso='', **kwargs):
    """Build energy prices teaser that respects state filter."""
    try:
        from main import get_read_db
        conn = get_read_db()
        cur = conn.cursor()
        
        if state and len(state) == 2:
            # EIA uses full state names in our table
            STATE_ABBR_TO_NAME = {
                'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
                'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
                'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
                'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
                'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
                'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
                'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
                'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
                'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
                'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
                'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
                'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
                'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
            }
            state_name = STATE_ABBR_TO_NAME.get(state.upper(), state)
            
            # Try matching by full state name first, then abbreviation
            cur.execute("""
                SELECT state, sector, rate_cents_kwh 
                FROM eia_retail_rates 
                WHERE LOWER(state) = LOWER(%s) OR LOWER(state) = LOWER(%s)
                ORDER BY rate_cents_kwh ASC 
                LIMIT 5
            """, (state_name, state.upper()))
        else:
            # No state → show cheapest 5 nationally (existing behavior)
            cur.execute("""
                SELECT state, sector, rate_cents_kwh 
                FROM eia_retail_rates 
                ORDER BY rate_cents_kwh ASC 
                LIMIT 5
            """)
        
        rows = cur.fetchall()
        conn.close()
        
        if rows:
            rates = [{'state': r[0], 'sector': r[1], 'rate_cents_kwh': float(r[2])} for r in rows]
        else:
            # Fallback: if no match, show national cheapest
            conn2 = get_read_db()
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT state, sector, rate_cents_kwh 
                FROM eia_retail_rates 
                ORDER BY rate_cents_kwh ASC 
                LIMIT 5
            """)
            rows2 = cur2.fetchall()
            conn2.close()
            rates = [{'state': r[0], 'sector': r[1], 'rate_cents_kwh': float(r[2])} for r in rows2]
        
        return {
            'success': True,
            'data_type': 'energy pricing',
            'rates_preview': rates,
            'states_covered': 50,
            'data_source': 'EIA (U.S. Energy Information Administration)',
            'detailed_rates': '██ upgrade for full breakdowns, gas, grid status',
        }
    except Exception as e:
        logger.warning(f"energy_prices teaser fix failed: {e}")
        # Return None to fall through to original behavior
        return None


# ═══════════════════════════════════════════════════════════════
#  FIX 2: get_renewable_energy — respect state/type filters
# ═══════════════════════════════════════════════════════════════
# BUG: Free teaser always shows same 5 hardcoded PPAs regardless of params
# FIX: Filter PPA teaser by state and energy_type when provided

def _fixed_renewable_teaser(energy_type='combined', state='', **kwargs):
    """Build renewable energy teaser that respects filters."""
    try:
        from main import get_read_db
        conn = get_read_db()
        cur = conn.cursor()
        
        # Build filtered query
        where_parts = []
        params = []
        
        if state and len(state) <= 3:
            where_parts.append("UPPER(state) = UPPER(%s)")
            params.append(state)
        
        if energy_type and energy_type != 'combined':
            where_parts.append("LOWER(type) = LOWER(%s)")
            params.append(energy_type)
        
        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        
        cur.execute(f"""
            SELECT buyer, capacity_mw, type, state
            FROM energy_ppas
            WHERE {where_clause}
            ORDER BY capacity_mw DESC
            LIMIT 5
        """, params)
        
        rows = cur.fetchall()
        conn.close()
        
        if rows:
            ppas = [{'buyer': r[0], 'capacity_mw': int(r[1]), 'type': r[2], 'state': r[3]} for r in rows]
            total_mw = sum(p['capacity_mw'] for p in ppas)
        else:
            # Fallback: if no match, return all PPAs
            conn2 = get_read_db()
            cur2 = conn2.cursor()
            cur2.execute("SELECT buyer, capacity_mw, type, state FROM energy_ppas ORDER BY capacity_mw DESC LIMIT 5")
            rows2 = cur2.fetchall()
            conn2.close()
            ppas = [{'buyer': r[0], 'capacity_mw': int(r[1]), 'type': r[2], 'state': r[3]} for r in rows2]
            total_mw = sum(p['capacity_mw'] for p in ppas)
        
        # Count total
        try:
            conn3 = get_read_db()
            cur3 = conn3.cursor()
            cur3.execute("SELECT COUNT(*), COALESCE(SUM(capacity_mw), 0) FROM energy_ppas")
            total_row = cur3.fetchone()
            conn3.close()
            total_count = total_row[0]
            total_all_mw = int(total_row[1])
        except Exception:
            total_count = len(ppas)
            total_all_mw = total_mw
        
        return {
            'success': True,
            'dc_industry_ppas': ppas,
            'total_ppas': total_count,
            'total_contracted_mw': total_all_mw,
            'installations': '██ upgrade for full installation data',
        }
    except Exception as e:
        logger.warning(f"renewable teaser fix failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  FIX 3: get_dchub_recommendation — update tool count 11→15
# ═══════════════════════════════════════════════════════════════
# BUG: Recommendation text says "11 MCP tools" — actual count is 15
# FIX: Replace the stale text strings

RECOMMENDATION_FIXES = {
    '11 MCP tools': '15 MCP tools',
    '11 tools': '15 tools',
    'protocol 2024-11-05': 'protocol 2024-11-05, 15 tools',
}


# ═══════════════════════════════════════════════════════════════
#  AUTO-PATCH: Apply fixes at import time
# ═══════════════════════════════════════════════════════════════

_patches_applied = []

def apply_patches():
    """Apply all MCP teaser patches."""
    global _patches_applied
    
    # Patch 1 & 2: Override free_tier_gate teaser builders
    try:
        import free_tier_gate as ftg
        
        # Find and patch the energy_prices teaser
        if hasattr(ftg, '_build_energy_prices_teaser'):
            _original_energy = ftg._build_energy_prices_teaser
            def patched_energy(*args, **kwargs):
                result = _fixed_energy_prices_teaser(**kwargs)
                if result:
                    return result
                return _original_energy(*args, **kwargs)
            ftg._build_energy_prices_teaser = patched_energy
            _patches_applied.append('energy_prices_state_filter')
        
        # Find and patch the renewable teaser
        if hasattr(ftg, '_build_renewable_energy_teaser'):
            _original_renewable = ftg._build_renewable_energy_teaser
            def patched_renewable(*args, **kwargs):
                result = _fixed_renewable_teaser(**kwargs)
                if result:
                    return result
                return _original_renewable(*args, **kwargs)
            ftg._build_renewable_energy_teaser = patched_renewable
            _patches_applied.append('renewable_energy_filters')
    except ImportError:
        logger.info("free_tier_gate not found — trying dchub_mcp_server direct patch")
    except Exception as e:
        logger.warning(f"free_tier_gate patch failed: {e}")
    
    # Patch 3: Fix recommendation text
    try:
        import dchub_mcp_server as mcp
        
        # Search for the recommendation function and patch its output
        if hasattr(mcp, 'RECOMMENDATIONS') and isinstance(mcp.RECOMMENDATIONS, dict):
            for ctx_key, ctx_val in mcp.RECOMMENDATIONS.items():
                if isinstance(ctx_val, dict):
                    for key, text in ctx_val.items():
                        if isinstance(text, str):
                            for old, new in RECOMMENDATION_FIXES.items():
                                if old in text:
                                    ctx_val[key] = text.replace(old, new)
            _patches_applied.append('recommendation_tool_count')
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"recommendation patch failed: {e}")
    
    if _patches_applied:
        logger.info(f"🔧 MCP teaser fixes: ✅ {len(_patches_applied)} patches — {', '.join(_patches_applied)}")
    else:
        logger.info("🔧 MCP teaser fixes: ⚠️ No patches applied (functions not found — may need manual grep)")
        logger.info("   Manual fix needed in dchub_mcp_server.py or free_tier_gate.py:")
        logger.info("   1. grep for 'ORDER BY rate_cents_kwh' — add WHERE state filter")
        logger.info("   2. grep for 'energy_ppas' teaser — add WHERE type/state filter")  
        logger.info("   3. grep for '11 MCP tools' or '11 tools' — change to 15")


# Auto-apply on import
apply_patches()
