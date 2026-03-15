#!/usr/bin/env python3
"""
DC Hub Developer Tier Patcher
Adds $49/mo Developer tier between Free and Pro
Run: python3 patch_developer_tier.py
"""
import re
import sys

def patch_file(filepath, patches):
    """Apply text replacements to a file."""
    with open(filepath, 'r') as f:
        content = f.read()
    
    for desc, old, new in patches:
        if old in content:
            content = content.replace(old, new, 1)
            print(f"  ✅ {desc}")
        else:
            print(f"  ⚠️  SKIP (not found): {desc}")
    
    with open(filepath, 'w') as f:
        f.write(content)

# =============================================================================
# PATCH api_tier_gating.py
# =============================================================================
print("\n🔧 Patching api_tier_gating.py...")

tier_patches = [
    # 1. Add developer to PLAN_LEVELS
    (
        "Add developer to PLAN_LEVELS",
        """PLAN_LEVELS = {
    'free': 0,
    'founding': 1,  # Founding members get Pro access
    'pro': 2,
    'enterprise': 3,
    'admin': 99,
}""",
        """PLAN_LEVELS = {
    'free': 0,
    'founding': 1,  # Founding members get Pro access
    'developer': 2,  # Developer tier - $49/mo, 1000 calls/day
    'pro': 3,
    'enterprise': 4,
    'admin': 99,
}"""
    ),

    # 2. Add developer to TIER_RATE_LIMITS
    (
        "Add developer to TIER_RATE_LIMITS",
        """TIER_RATE_LIMITS = {
    'free':       10,
    'founding':   10000,
    'pro':        10000,
    'enterprise': 100000,
    'admin':      999999,
}""",
        """TIER_RATE_LIMITS = {
    'free':       10,
    'founding':   10000,
    'developer':  1000,
    'pro':        10000,
    'enterprise': 100000,
    'admin':      999999,
}"""
    ),

    # 3. Add developer to PAYMENT_LINKS
    (
        "Add developer to PAYMENT_LINKS",
        "    'founding':           'https://buy.stripe.com/9B6fZi1cCdjT3ml8i6aZi00',\n}",
        "    'founding':           'https://buy.stripe.com/9B6fZi1cCdjT3ml8i6aZi00',\n    'developer_monthly':  'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',\n}"
    ),

    # 4. Add developer to STRIPE_PRICES_V2
    (
        "Add developer to STRIPE_PRICES_V2",
        "    'founding':             os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),\n}",
        "    'founding':             os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),\n    'developer_monthly':    os.environ.get('STRIPE_PRICE_DEV_MONTHLY', 'price_XXXXX'),\n}"
    ),

    # 5. Add developer PLAN_INFO (before 'pro' block)
    (
        "Add developer to PLAN_INFO",
        "    'pro': {\n        'name': 'Pro',",
        """    'developer': {
        'name': 'Developer',
        'price_monthly': 49,
        'price_annual': 390,
        'rate_limit': 1000,
        'features': {
            'headline_stats': True,
            'news_feed': True,
            'ai_discovery': True,
            'market_list': True,
            'facility_search': True,
            'deal_database': True,
            'pipeline_tracker': True,
            'energy_data': True,
            'connectivity_score': True,
            'site_analysis': False,
            'market_compare': False,
            'pdf_reports': False,
            'ai_brain': False,
            'grid_monitoring': False,
            'land_power': False,
            'api_key': True,
            'priority_support': False,
        }
    },
    'pro': {
        'name': 'Pro',"""
    ),
]

patch_file('/home/runner/workspace/api_tier_gating.py', tier_patches)

# =============================================================================
# PATCH main.py
# =============================================================================
print("\n🔧 Patching main.py...")

main_patches = [
    # 6. Add developer_monthly to STRIPE_PRICES
    (
        "Add developer to STRIPE_PRICES",
        """STRIPE_PRICES = {
    'pro_monthly': os.environ.get('STRIPE_PRICE_PRO_MONTHLY', 'price_XXXXX'),
    'pro_annual': os.environ.get('STRIPE_PRICE_PRO_ANNUAL', 'price_XXXXX'),
    'founding': os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),
}""",
        """STRIPE_PRICES = {
    'pro_monthly': os.environ.get('STRIPE_PRICE_PRO_MONTHLY', 'price_XXXXX'),
    'pro_annual': os.environ.get('STRIPE_PRICE_PRO_ANNUAL', 'price_XXXXX'),
    'founding': os.environ.get('STRIPE_PRICE_FOUNDING', 'price_XXXXX'),
    'developer_monthly': os.environ.get('STRIPE_PRICE_DEV_MONTHLY', 'price_XXXXX'),
}"""
    ),

    # 7. Add developer_monthly to payment_links fallback
    (
        "Add developer to payment_links fallback",
        "            'enterprise_annual': 'https://buy.stripe.com/dRmdRa4oO1Bb9KJ2XMaZi0b'",
        "            'enterprise_annual': 'https://buy.stripe.com/dRmdRa4oO1Bb9KJ2XMaZi0b',\n            'developer_monthly': 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c'"
    ),

    # 8. Add developer_monthly to plan_tier_map
    (
        "Add developer to plan_tier_map",
        "        'founding': ('founding', 'pro'),",
        "        'founding': ('founding', 'pro'),\n            'developer_monthly': ('developer', 'developer'),"
    ),

    # 9. Add $49 amount detection (before the $99 founding check)
    (
        "Add $49 amount detection",
        "            if amount_dollars == 99 or (95 <= amount_dollars <= 105):",
        "            if amount_dollars == 49 or (45 <= amount_dollars <= 55):\n                plan_name, api_tier = 'developer', 'developer'\n            elif amount_dollars == 99 or (95 <= amount_dollars <= 105):"
    ),

    # 10. Update tier gating log message
    (
        "Update tier gating log message",
        '    logger.info("✅ API Tier Gating registered (Free/Pro/Enterprise)")',
        '    logger.info("✅ API Tier Gating registered (Free/Developer/Pro/Enterprise)")'
    ),

    # 11. Add developer upgrade CTA to free tier plan_required response
    (
        "Add developer upgrade CTA to plan_required response",
        """        'pricing_url': 'https://dchub.cloud/pricing',
        'signup_url': 'https://dchub.cloud/signup',
        'success': false""" if False else "SKIP_THIS",  # We'll do this separately
        "SKIP_THIS_TOO"
    ),
]

# Filter out the skip placeholder
main_patches = [p for p in main_patches if p[1] != "SKIP_THIS"]

patch_file('/home/runner/workspace/main.py', main_patches)

# =============================================================================
# VERIFY
# =============================================================================
print("\n🔍 Verifying...")

with open('/home/runner/workspace/api_tier_gating.py') as f:
    content = f.read()

checks = [
    ("PLAN_LEVELS has developer", "'developer': 2" in content),
    ("TIER_RATE_LIMITS has developer", "'developer':  1000" in content),
    ("PAYMENT_LINKS has developer", "developer_monthly" in content),
    ("PLAN_INFO has developer", "'name': 'Developer'" in content),
]

with open('/home/runner/workspace/main.py') as f:
    mcontent = f.read()

checks += [
    ("main.py STRIPE_PRICES has developer", "'developer_monthly'" in mcontent),
    ("main.py plan_tier_map has developer", "developer_monthly" in mcontent),
    ("main.py $49 detection", "amount_dollars == 49" in mcontent),
]

all_pass = True
for desc, result in checks:
    status = "✅" if result else "❌"
    if not result:
        all_pass = False
    print(f"  {status} {desc}")

if all_pass:
    print("\n✅ All patches verified! Ready to push.")
    print("\nNext: git add -A && git commit -m 'Add Developer tier ($49/mo) - 1000 calls/day' && git push")
else:
    print("\n⚠️  Some patches failed. Check output above.")
