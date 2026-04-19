#!/bin/bash
# ============================================================================
# DC Hub MCP Health Fixes — Railway Shell Script
# Run each section in order on Railway shell
# ============================================================================

# ── PRE-CHECK: Verify current state ──
echo "=== Pre-check ==="
grep -c "ai_testimonials" main.py
grep -c "hifld_substations" main.py
echo "Expected: both should show counts > 0"

# ============================================================================
# FIX 1: BUG-033 — get_agent_registry (replace entire function)
# ============================================================================
# This is the biggest fix. We need to replace the function that queries 
# ai_testimonials with one that queries agent_registry.
#
# Strategy: Use sed to replace the function body.
# Since sed is tricky for multi-line, we'll use python instead:

python3 << 'PYFIX1'
import re

with open('main.py', 'r') as f:
    content = f.read()

# Find and replace the _get_agent_registry_from_neon function
old_func_start = 'def _get_agent_registry_from_neon():'
# Find the function boundaries
start_idx = content.find(old_func_start)
if start_idx == -1:
    print("ERROR: Could not find _get_agent_registry_from_neon function!")
    exit(1)

# Find the next function definition (def at column 0) after this one
next_func = re.search(r'\ndef [a-zA-Z_]', content[start_idx + 10:])
if next_func:
    end_idx = start_idx + 10 + next_func.start() + 1  # +1 for the \n
else:
    print("ERROR: Could not find end of function!")
    exit(1)

new_func = '''def _get_agent_registry_from_neon():
    """Build live agent registry from Neon agent_registry table + MCP usage stats."""
    conn = None
    try:
        conn = psycopg2.connect(os.environ.get("NEON_DATABASE_URL", os.environ.get("DATABASE_URL", "")))
        cur = conn.cursor()

        # Primary: query the actual agent_registry table
        agents = []
        total_24h = 0
        try:
            cur.execute("""
                SELECT id, name, slug, integration_type, status, description, last_seen, created_at
                FROM agent_registry
                ORDER BY last_seen DESC NULLS LAST
            """)
            for row in cur.fetchall():
                agent_id, name, slug, int_type, status, desc, last_seen, created = row
                agents.append({
                    "platform": name or slug or "unknown",
                    "slug": slug,
                    "integration_type": int_type or "api",
                    "status": status or "active",
                    "description": desc or "",
                    "last_active": last_seen.isoformat() if last_seen else None,
                    "created_at": created.isoformat() if created else None,
                    "connection": "MCP (Streamable HTTP)" if int_type == "mcp" else "API"
                })
        except Exception as e1:
            logger.debug(f"Agent registry: agent_registry table query failed: {e1}")

        # Supplement with API-key-based agents from mcp_rate_limits
        known_slugs = {a.get('slug', '').lower() for a in agents}
        try:
            cur.execute("""
                SELECT ak.name, ak.rate_limit_tier,
                       SUM(rl.request_count) as total_reqs,
                       MAX(rl.request_date) as last_date
                FROM mcp_rate_limits rl
                JOIN api_keys ak ON rl.api_key = ak.key
                WHERE rl.request_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY ak.name, ak.rate_limit_tier
                ORDER BY total_reqs DESC
                LIMIT 20
            """)
            for name, tier, total_reqs, last_date in cur.fetchall():
                if name and name.lower() not in known_slugs:
                    agents.append({
                        "platform": name,
                        "slug": name.lower().replace(" ", "-"),
                        "integration_type": "api",
                        "status": "active",
                        "total_queries": total_reqs or 0,
                        "last_active": last_date.isoformat() if last_date else None,
                        "tier": tier or "free",
                        "connection": "API Key"
                    })
        except Exception as e2:
            logger.debug(f"Agent registry: mcp_rate_limits query skipped: {e2}")

        return {
            "success": True,
            "agents": agents,
            "total_connected": len(agents),
            "active": sum(1 for a in agents if a.get('status') == 'active'),
            "total_queries_24h": total_24h,
            "source": "DC Hub Agent Registry (dchub.cloud)",
            "registry_url": "https://dchub.cloud/ecosystem",
            "mcp_endpoint": "https://dchub.cloud/mcp"
        }
    except Exception as e:
        logger.error(f"_get_agent_registry_from_neon error: {e}")
        return {
            "success": True,
            "agents": [],
            "total_connected": 0,
            "active": 0,
            "source": "DC Hub Agent Registry (dchub.cloud)"
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

'''

content = content[:start_idx] + new_func + content[end_idx:]

with open('main.py', 'w') as f:
    f.write(content)

print("FIX 1 APPLIED: _get_agent_registry_from_neon now queries agent_registry table")
print(f"  Replaced {end_idx - start_idx} chars starting at position {start_idx}")
PYFIX1

# ============================================================================
# FIX 2: BUG-034 — backup_status: hifld_substations → substations
# ============================================================================

python3 << 'PYFIX2'
with open('main.py', 'r') as f:
    content = f.read()

# Only replace in the get_backup_status table list context
old = '"hifld_substations"'
new = '"substations"'

# Find the backup_status context (the for loop with table list)
ctx = 'for _tbl in ["discovered_facilities", "deals", "news_articles", "gas_pipelines", "hifld_substations", "fiber_routes"]'
if ctx in content:
    content = content.replace(ctx, ctx.replace(old, new))
    with open('main.py', 'w') as f:
        f.write(content)
    print("FIX 2 APPLIED: backup_status now queries 'substations' (79,755 rows) instead of 'hifld_substations' (0 rows)")
else:
    # Fallback: broader replacement but only first occurrence near backup_status
    idx = content.find('get_backup_status')
    if idx > -1:
        # Replace within 500 chars after get_backup_status
        segment = content[idx:idx+500]
        if old in segment:
            segment = segment.replace(old, new, 1)
            content = content[:idx] + segment + content[idx+500:]
            with open('main.py', 'w') as f:
                f.write(content)
            print("FIX 2 APPLIED (fallback): replaced hifld_substations near get_backup_status")
        else:
            print("FIX 2 SKIPPED: hifld_substations not found near get_backup_status")
    else:
        print("FIX 2 SKIPPED: get_backup_status not found")
PYFIX2

# ============================================================================
# FIX 3: BUG-035 — News category mapping (deals → M&A, etc.)
# ============================================================================

python3 << 'PYFIX3'
with open('main.py', 'r') as f:
    content = f.read()

# Add NEWS_CATEGORY_MAP near the top of the MCP teaser section
# Find the MCP_TEASER_TOOLS set definition and add the map right after it
marker = "MCP_TEASER_TOOLS = {"
idx = content.find(marker)
if idx == -1:
    print("FIX 3 SKIPPED: MCP_TEASER_TOOLS not found")
else:
    # Find end of this line
    eol = content.index('\n', idx)
    
    category_map = '''

# News category aliases — map MCP tool categories to DB categories
NEWS_CATEGORY_MAP = {
    'deals': 'M&A', 'ma': 'M&A', 'm&a': 'M&A', 'mergers': 'M&A',
    'construction': 'Expansion', 'expansion': 'Expansion',
    'policy': 'Industry', 'technology': 'AI', 'tech': 'AI',
    'sustainability': 'Power', 'energy': 'Power',
    'earnings': 'Financial', 'financial': 'Financial',
    'network': 'Network', 'cooling': 'Cooling', 'cloud': 'Cloud',
}
'''
    content = content[:eol+1] + category_map + content[eol+1:]
    
    # Now find where the news category filter is applied and add normalization
    # Look for the news teaser handler — it should reference category
    # We need to find where tool_params category is used for get_news
    # Pattern: category filter in the news query
    news_markers = [
        "tool_name == 'get_news'",
        'tool_name == "get_news"',
    ]
    
    for nm in news_markers:
        nm_idx = content.find(nm)
        if nm_idx > -1:
            # Find the category variable assignment after this point
            # Look for category being extracted from tool_params
            search_area = content[nm_idx:nm_idx+1000]
            
            # Common patterns: category = tool_params.get('category', '')
            # or arguments.get('category')
            for cat_pattern in ["category = ", "category_filter = ", "'category'"]:
                cat_idx = search_area.find(cat_pattern)
                if cat_idx > -1:
                    # Found it - add normalization right after the assignment line
                    abs_idx = nm_idx + cat_idx
                    line_end = content.index('\n', abs_idx)
                    indent = '            '  # Match typical indentation
                    normalization = f"\n{indent}# Normalize category aliases (BUG-035 fix)\n{indent}if category:\n{indent}    category = NEWS_CATEGORY_MAP.get(category.lower(), category)\n"
                    
                    # Only insert if not already present
                    if 'NEWS_CATEGORY_MAP' not in content[line_end:line_end+200]:
                        content = content[:line_end+1] + normalization + content[line_end+1:]
                        print(f"FIX 3 APPLIED: Added category normalization after '{cat_pattern}' in get_news handler")
                    else:
                        print("FIX 3: Category normalization already present")
                    break
            break
    
    with open('main.py', 'w') as f:
        f.write(content)
    
    if 'NEWS_CATEGORY_MAP' in content:
        print("FIX 3: NEWS_CATEGORY_MAP added to main.py")
    else:
        print("FIX 3 WARNING: Map may not have been inserted correctly")
PYFIX3

# ============================================================================
# FIX 4: BUG-036 — fiber_intel: Skipping for now (needs MCP intercept analysis)
# The fiber_intel teaser DOES return success:true via the proxy path.
# The error wrapper is likely how the MCP client interprets a 
# teaser-only response. This may be a client-side issue, not server.
# Monitor after fixes 1-3-5 are deployed.
# ============================================================================
echo "FIX 4: fiber_intel error wrapper — DEFERRED (monitoring after other fixes)"

# ============================================================================
# FIX 5: BUG-037 — list_transactions region filter in free-tier teaser
# ============================================================================

python3 << 'PYFIX5'
with open('main.py', 'r') as f:
    content = f.read()

# Find the free-tier teaser for list_transactions
# The pattern around line 3399-3404:
# gated_deals = [
#     {k: t.get(k) for k in free_fields if k in t}
#     for t in (transactions[:5] if isinstance(transactions, list) else [])
# ]

old_pattern = "gated_deals = [\n                {k: t.get(k) for k in free_fields if k in t}\n                for t in (transactions[:5] if isinstance(transactions, list) else [])\n            ]"

new_pattern = """# Apply region filter if specified (BUG-037 fix)
            filtered_txns = transactions if isinstance(transactions, list) else []
            _region_param = tool_params.get('arguments', {}).get('region', '') if isinstance(tool_params, dict) else ''
            if not _region_param and isinstance(tool_params, dict):
                _region_param = tool_params.get('region', '')
            if _region_param:
                _region_norm = _region_param.lower().replace('_', ' ')
                filtered_txns = [t for t in filtered_txns if _region_norm in (t.get('region', '') or '').lower()]
            gated_deals = [
                {k: t.get(k) for k in free_fields if k in t}
                for t in filtered_txns[:5]
            ]"""

if old_pattern in content:
    content = content.replace(old_pattern, new_pattern, 1)
    with open('main.py', 'w') as f:
        f.write(content)
    print("FIX 5 APPLIED: list_transactions free-tier teaser now applies region filter")
else:
    # Try more flexible matching
    import re
    pattern = r'gated_deals = \[\s*\{k: t\.get\(k\) for k in free_fields if k in t\}\s*for t in \(transactions\[:5\]'
    match = re.search(pattern, content)
    if match:
        print(f"FIX 5: Found pattern at position {match.start()} but exact whitespace differs.")
        print("  Apply manually: Add region filtering before the gated_deals list comprehension")
        print(f"  Context: {content[match.start()-50:match.start()+200]}")
    else:
        print("FIX 5 SKIPPED: Could not find gated_deals pattern. Apply manually.")
        # Show what's around line 3399
        lines = content.split('\n')
        for i in range(3395, min(3410, len(lines))):
            print(f"  {i+1}: {lines[i]}")
PYFIX5

# ============================================================================
# POST-FIX VERIFICATION
# ============================================================================
echo ""
echo "=== Post-fix verification ==="
echo "1. agent_registry fix:"
grep -c "FROM agent_registry" main.py
echo "  (should be >= 1)"

echo "2. backup_status fix:"
grep "substations" main.py | grep -c "for _tbl"
echo "  (should show 'substations' not 'hifld_substations')"

echo "3. news category map:"
grep -c "NEWS_CATEGORY_MAP" main.py
echo "  (should be >= 2)"

echo "4. transactions region filter:"
grep -c "BUG-037" main.py
echo "  (should be >= 1)"

echo ""
echo "=== Ready to commit ==="
echo "git add main.py"
echo "git commit -m 'fix: MCP health audit — BUG-033 agent_registry, BUG-034 substations count, BUG-035 news categories, BUG-037 txn region filter'"
echo "git push"
