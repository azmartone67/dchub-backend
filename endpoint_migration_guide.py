"""
DC Hub Endpoint Migration Guide
=================================
Copy-paste these changes into main.py to enforce tier gating.

STEP 1: Add to top of main.py (near other imports):
  from api_tier_gating import init_tier_gating, require_plan

STEP 2: After app = Flask(__name__), add:
  init_tier_gating(app)

STEP 3: Apply decorators per the guide below.
  - FREE endpoints: keep @rate_limit (no change)
  - PRO endpoints: replace @rate_limit with @require_plan('pro')
  - ENTERPRISE endpoints: replace @rate_limit with @require_plan('enterprise')
  - INTERNAL endpoints: add @require_api_key (admin only)

IMPORTANT: Order matters! Route decorator first, then auth decorator:
  @app.route('/api/v1/deals')
  @require_plan('pro')          # <-- this goes AFTER @app.route
  def get_deals():
"""

# ═══════════════════════════════════════════════════════════════
#  FREE TIER — No changes needed (keep @rate_limit)
# ═══════════════════════════════════════════════════════════════
FREE_ENDPOINTS = """
# These stay as-is with @rate_limit (or no decorator):
/health                          # Health check
/api/health                      # Health check
/api/v1/stats                    # Global stats (headline numbers)
/api/v1/facilities/stats         # Facility counts by status/region
/api/news/live                   # Latest 10 news headlines
/api/v1/news                     # News articles (last 20)
/api/v1/announcements            # Same as news
/api/market-report               # Weekly market report (JSON)
/api/v1/markets/list             # List of 20 markets (basic stats)
/api/ai/query?type=stats         # AI stats query (citation-ready)
/api/ai/cite                     # Citation helper
/api/v1/discovery                # AI protocol discovery index
/api/grid/supported-isos         # List of supported grid ISOs
/api/fcc/summary                 # Broadband summary (aggregate)
/api/epa/summary                 # Emissions summary (aggregate)
/api/news-feed                   # News feed
/api/news                        # News
/api/dc-markets                  # Market overview data
/api/v1/lmp/prices               # Sample LMP data
"""

# ═══════════════════════════════════════════════════════════════
#  PRO TIER — Replace @rate_limit with @require_plan('pro')
# ═══════════════════════════════════════════════════════════════
PRO_CHANGES = """
# FIND AND REPLACE for each endpoint:
# Before: @rate_limit
# After:  @require_plan('pro')

LINE 5126: /api/v1/facilities          → @require_plan('pro')
LINE 5216: /api/v1/map                 → @require_plan('pro')  (add decorator)
LINE 5327: /api/v1/search              → @require_plan('pro')
LINE 5708: /api/deals                  → @require_plan('pro')
LINE 5856: /api/v1/transactions        → @require_plan('pro')  (add decorator)
LINE 5865: /api/v1/pipeline            → @require_plan('pro')
LINE 6045: /api/v1/deals               → @require_plan('pro')
LINE 4559: /api/v1/markets/<market>    → @require_plan('pro')  (add decorator)
LINE 4655: /api/v1/markets/compare     → @require_plan('pro')  (add decorator)
LINE 2242: /api/v1/connectivity/score  → @require_plan('pro')  (add decorator)
LINE 2130: /api/v1/connectivity/ixps   → @require_plan('pro')  (add decorator)
LINE 2185: /api/v1/connectivity/fac    → @require_plan('pro')  (add decorator)
LINE 2369: /api/v1/energy/rto/demand   → @require_plan('pro')  (add decorator)
LINE 2421: /api/v1/energy/rto/fuelmix  → @require_plan('pro')  (add decorator)
LINE 2508: /api/v1/energy/retail/rates → @require_plan('pro')  (add decorator)
LINE 2471: /api/v1/energy/naturalgas   → @require_plan('pro')  (add decorator)
LINE 1244: /api/grid/demand            → @require_plan('pro')
LINE 1313: /api/grid/fuel-mix          → @require_plan('pro')
LINE 1371: /api/grid/prices            → @require_plan('pro')
LINE 1558: /api/fcc/broadband          → @require_plan('pro')
LINE 1633: /api/fcc/providers          → @require_plan('pro')
LINE 1790: /api/epa/emissions          → @require_plan('pro')
LINE 1864: /api/epa/facilities         → @require_plan('pro')
LINE 1973: /api/epa/ghg               → @require_plan('pro')
LINE 6089: /api/v1/analytics           → @require_plan('pro')
LINE 4754: /api/reports/generate       → @require_plan('pro')  (has @optional_auth, add @require_plan too)
"""

# ═══════════════════════════════════════════════════════════════
#  ENTERPRISE TIER — @require_plan('enterprise')
# ═══════════════════════════════════════════════════════════════
ENTERPRISE_CHANGES = """
LINE 9572: /api/brain/ask             → @require_plan('enterprise')  (add decorator)
LINE 9596: /api/brain/market/<market>  → @require_plan('enterprise')  (add decorator)
LINE 9612: /api/brain/operator/<op>    → @require_plan('enterprise')  (add decorator)
LINE 1180: /api/v1/grid/status         → @require_plan('pro')  (add decorator)
LINE 1099: /api/v1/grid/caiso/fuelmix  → @require_plan('pro')  (add decorator)
LINE 1151: /api/v1/grid/caiso/demand   → @require_plan('pro')  (add decorator)
LINE 5966: /api/v1/gas-pipelines       → @require_plan('pro')
LINE 2592: /api/v1/oilgas/wells        → @require_plan('pro')  (add decorator)
LINE 2694: /api/v1/oilgas/operators    → @require_plan('pro')  (add decorator)
LINE 2786: /api/v1/oilgas/search       → @require_plan('pro')  (add decorator)
# Land & Power routes (in land_power_routes.py) → @require_plan('pro')
"""

# ═══════════════════════════════════════════════════════════════
#  INTERNAL — @require_api_key (admin key only)
# ═══════════════════════════════════════════════════════════════
INTERNAL_CHANGES = """
# These should already have @require_api_key or similar:
/api/autopilot/*                → @require_api_key (keep as-is)
/api/brain/learn                → @require_api_key
/api/evolution/*                → @require_api_key
/api/discovery/run              → @require_api_key
/api/admin/*                    → @require_api_key
/api/email/*                    → @require_api_key
/api/marketing/stats            → @require_api_key
"""

# ═══════════════════════════════════════════════════════════════
#  SPECIAL: AI QUERY ENDPOINT — Mixed tier based on type param
# ═══════════════════════════════════════════════════════════════
AI_QUERY_CHANGES = """
The /api/ai/query endpoint needs special handling because different
query types should be different tiers:

  ?type=stats     → FREE (keeps AI platforms citing you)
  ?type=facilities → PRO
  ?type=deals      → PRO  
  ?type=capacity   → PRO

IMPLEMENTATION: Replace the ai_query() function with:

@app.route('/api/ai/query')
@rate_limit
def ai_query():
    query_type = request.args.get('type', 'general')
    
    # Stats are always free (AI citation hook)
    if query_type in ('stats', 'general', ''):
        return _ai_query_stats()
    
    # Everything else requires Pro
    # Check for API key or JWT
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if api_key:
        from api_tier_gating import validate_api_key, user_has_access
        valid, info = validate_api_key(api_key)
        if not valid or not user_has_access(info.get('plan', 'free'), 'pro'):
            return jsonify({
                'success': False,
                'error': 'pro_plan_required',
                'message': f'AI query type "{query_type}" requires Pro plan.',
                'free_alternative': '/api/ai/query?type=stats',
                'upgrade_url': 'https://dchub.cloud/pricing',
            }), 403
    else:
        # Check JWT
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({
                'success': False,
                'error': 'authentication_required',
                'message': f'AI query type "{query_type}" requires Pro plan.',
                'free_alternative': '/api/ai/query?type=stats (no auth needed)',
                'upgrade_url': 'https://dchub.cloud/pricing',
            }), 401
    
    # Authorized — proceed with query
    if query_type == 'facilities':
        return _ai_query_facilities()
    elif query_type == 'deals':
        return _ai_query_deals()
    elif query_type == 'capacity':
        return _ai_query_capacity()
    else:
        return _ai_query_stats()
"""

# ═══════════════════════════════════════════════════════════════
#  STRIPE SETUP CHECKLIST
# ═══════════════════════════════════════════════════════════════
STRIPE_SETUP = """
STRIPE DASHBOARD SETUP:
========================

1. CREATE PRODUCTS:
   a) "DC Hub Pro" (if not exists)
      - Monthly: $199/month recurring → save price_id as STRIPE_PRICE_PRO_MONTHLY
      - Annual:  $1,590/year recurring → save price_id as STRIPE_PRICE_PRO_ANNUAL
   
   b) "DC Hub Enterprise" (NEW)
      - Monthly: $699/month recurring → save price_id as STRIPE_PRICE_ENTERPRISE_MONTHLY  
      - Annual:  $5,990/year recurring → save price_id as STRIPE_PRICE_ENTERPRISE_ANNUAL

2. CREATE PAYMENT LINKS:
   - Pro Monthly → update PAYMENT_LINKS in api_tier_gating.py
   - Pro Annual → update PAYMENT_LINKS
   - Enterprise Monthly → update PAYMENT_LINKS
   - Enterprise Annual → update PAYMENT_LINKS

3. SET REPLIT SECRETS:
   STRIPE_PRICE_PRO_MONTHLY=price_xxxxx
   STRIPE_PRICE_PRO_ANNUAL=price_xxxxx
   STRIPE_PRICE_ENTERPRISE_MONTHLY=price_xxxxx
   STRIPE_PRICE_ENTERPRISE_ANNUAL=price_xxxxx

4. UPDATE WEBHOOK:
   - In Stripe Dashboard → Developers → Webhooks
   - Add endpoint: https://dchub.cloud/api/v2/stripe/webhook
   - Events to listen for:
     * checkout.session.completed
     * customer.subscription.created
     * customer.subscription.updated  
     * customer.subscription.deleted
     * invoice.paid
     * invoice.payment_failed

5. TEST:
   - Create test checkout: POST /api/v2/stripe/create-checkout {"plan": "enterprise_monthly"}
   - Verify webhook fires and user.plan updates to "enterprise"
   - Verify API key auto-provisions with enterprise tier
"""

if __name__ == '__main__':
    print("=" * 60)
    print("DC Hub Endpoint Migration Guide")
    print("=" * 60)
    print(f"\nFREE endpoints (no change needed):{FREE_ENDPOINTS}")
    print(f"\nPRO changes:{PRO_CHANGES}")
    print(f"\nENTERPRISE changes:{ENTERPRISE_CHANGES}")
    print(f"\nINTERNAL changes:{INTERNAL_CHANGES}")
    print(f"\nAI Query special handling:{AI_QUERY_CHANGES}")
    print(f"\nStripe setup:{STRIPE_SETUP}")
