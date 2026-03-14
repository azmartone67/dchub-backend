"""
RANKINGS TIER GATING
====================
Add this logic inside each rankings endpoint to gate by plan.

Free tier: Top 3 states, basic fields only (state_name, rank, primary metric)
Pro tier: All states, all fields, operators, insights

Usage: Insert the free_tier_response() call after generating results
in each endpoint. The require_plan decorator isn't needed since we
want free users to see a teaser (not a 403).
"""

# --- Add this helper function inside _register_rankings_routes ---

def _gate_rankings(results, category_data):
    """
    Apply tier gating to rankings results.
    Free users: top 3 states with basic fields + upgrade prompt
    Pro/Enterprise: full results
    
    Call from within each endpoint after generating results.
    """
    from flask import request as req
    
    # Check user plan from JWT token, API key, or cookie
    user_plan = 'free'  # default
    
    # Check API key
    api_key = req.headers.get('X-API-Key', '') or req.args.get('api_key', '')
    if api_key.startswith('dchub_en_'):
        user_plan = 'enterprise'
    elif api_key.startswith('dchub_pr_'):
        user_plan = 'pro'
    
    # Check X-Internal-Key (MCP/internal calls)
    internal_key = req.headers.get('X-Internal-Key', '')
    if internal_key:
        import os
        if internal_key == os.environ.get('INTERNAL_API_KEY', ''):
            user_plan = 'enterprise'
    
    # Check JWT from cookie (for web users)
    if user_plan == 'free':
        auth_header = req.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            try:
                # Attempt to decode JWT and check plan
                import jwt, os
                token = auth_header.replace('Bearer ', '')
                decoded = jwt.decode(token, os.environ.get('JWT_SECRET', ''), algorithms=['HS256'])
                user_plan = decoded.get('plan', 'free')
            except Exception:
                pass
    
    if user_plan in ('pro', 'enterprise'):
        return {
            "tier": user_plan,
            "rankings": results,
            "gated": False
        }
    
    # Free tier: top 3, stripped fields
    free_results = []
    for r in results[:3]:
        free_entry = {
            'rank': r.get('rank'),
            'state_name': r.get('state_name') or r.get('state'),
        }
        # Include primary metric only
        primary = category_data.get('primary_metric', 'total_mw')
        if primary in r:
            free_entry[primary] = r[primary]
        free_results.append(free_entry)
    
    return {
        "tier": "free",
        "rankings": free_results,
        "gated": True,
        "total_available": len(results),
        "showing": len(free_results),
        "upgrade_message": f"Showing top 3 of {len(results)} states. Upgrade to Pro for full rankings, operator details, and analysis tools.",
        "upgrade_url": "https://dchub.cloud/pricing"
    }


# ---------------------------------------------------------------
# INTEGRATION EXAMPLE - how to use in rankings_construction:
# ---------------------------------------------------------------
"""
# At the end of rankings_construction(), before the return jsonify:

    gate_result = _gate_rankings(results, {
        "primary_metric": "total_mw",
        "category": "construction"
    })
    
    response = {
        "success": True,
        "category": "construction",
        "title": "Data Centers Under Construction",
        "subtitle": f"in the United States (As of {datetime.utcnow().strftime('%b %d, %Y')})",
        "metric_label": "Pipeline MW Under Construction",
        "primary_metric": "total_mw",
        "secondary_metric": "project_count",
        "rankings": gate_result["rankings"],
        "summary": {
            "total_states": len(results),  # Always show full stats as teaser
            "total_projects": total_projects,
            "total_mw": float(total_mw),
        },
        "source": "DC Hub | dchub.cloud",
        "generated_at": datetime.utcnow().isoformat(),
    }
    
    # Add gating metadata
    if gate_result.get("gated"):
        response["gated"] = True
        response["showing"] = gate_result["showing"]
        response["total_available"] = gate_result["total_available"]
        response["upgrade_message"] = gate_result["upgrade_message"]
        response["upgrade_url"] = gate_result["upgrade_url"]
    
    response["tier"] = gate_result["tier"]
    
    return jsonify(response)
"""
