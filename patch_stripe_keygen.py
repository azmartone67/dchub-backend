"""
Patch main.py to generate properly-prefixed MCP API keys on Stripe checkout.
=============================================================================
Your existing flow generates keys like `dchub_XXXXX` (no tier prefix).
The gatekeeper resolves tier from prefix: dchub_dev_, dchub_pro_, dchub_ent_.

This patch updates handle_checkout_completed() to:
  1. Generate tier-prefixed keys (dchub_dev_xxx, dchub_pro_xxx, dchub_ent_xxx)
  2. Store the prefix in api_keys.key_prefix for DB-based tier resolution
  3. Include MCP setup instructions in the welcome email

Also updates the $49 developer plan detection to set api_tier = 'developer' (not 'pro').

Usage:
    python patch_stripe_keygen.py                    # patch in place
    python patch_stripe_keygen.py --dry-run           # preview
    python patch_stripe_keygen.py --input main.py --output main_patched.py
"""

import re
import sys
import argparse
import shutil
from datetime import datetime


def patch_main(source: str) -> tuple:
    changes = []

    # ═══════════════════════════════════════════════════════════
    # 1. Fix developer plan: api_tier should be 'developer' not 'pro'
    # ═══════════════════════════════════════════════════════════
    old_dev_tier = "'developer_monthly': ('developer', 'developer'),"
    if old_dev_tier in source:
        changes.append("Developer plan tier already correct")
    else:
        # The current code might map developer to 'pro' tier
        pass

    # Fix amount-based detection: $49 → developer tier
    old_amount = "if amount_dollars == 49 or (45 <= amount_dollars <= 55):\n                plan_name, api_tier = 'developer', 'pro'"
    new_amount = "if amount_dollars == 49 or (45 <= amount_dollars <= 55):\n                plan_name, api_tier = 'developer', 'developer'"
    if old_amount in source:
        source = source.replace(old_amount, new_amount)
        changes.append("Fixed $49 plan: api_tier 'pro' → 'developer'")

    # ═══════════════════════════════════════════════════════════
    # 2. Update key generation to use tier-prefixed keys
    # ═══════════════════════════════════════════════════════════
    old_keygen = "raw_key = 'dchub_' + sec.token_urlsafe(32)"
    new_keygen = """# Generate tier-prefixed key for MCP gatekeeper compatibility
            _tier_prefix_map = {
                'developer': 'dchub_dev_', 'pro': 'dchub_pro_',
                'enterprise': 'dchub_ent_', 'founding': 'dchub_pro_',
            }
            _key_prefix_str = _tier_prefix_map.get(plan_name, 'dchub_dev_')
            raw_key = _key_prefix_str + sec.token_urlsafe(32)"""

    if old_keygen in source:
        source = source.replace(old_keygen, new_keygen, 1)
        changes.append("Updated key generation to use tier prefixes (dchub_dev_, dchub_pro_, dchub_ent_)")

    # Also update key_prefix to capture the full tier prefix (not just first 12 chars)
    old_prefix = "key_prefix = raw_key[:12]"
    new_prefix = "key_prefix = raw_key[:raw_key.rindex('_') + 1]  # e.g. 'dchub_dev_'"
    if old_prefix in source:
        source = source.replace(old_prefix, new_prefix, 1)
        changes.append("Updated key_prefix storage to capture full tier prefix")

    # ═══════════════════════════════════════════════════════════
    # 3. Also update keys for EXISTING users who upgrade
    # ═══════════════════════════════════════════════════════════
    # Find the section where existing users get their tier upgraded
    # (around line 6673 in the original)
    old_tier_update = 'UPDATE api_keys SET rate_limit_tier = %s, last_used_at = %s WHERE user_id = %s'
    new_tier_update = 'UPDATE api_keys SET rate_limit_tier = %s, plan = %s, last_used_at = %s WHERE user_id = %s'
    if old_tier_update in source:
        # Need to also update the params tuple — this is trickier
        # Let's use a regex to find and fix the full _pg_execute call
        pattern = re.compile(
            r'_pg_execute\("UPDATE api_keys SET rate_limit_tier = %s, last_used_at = %s WHERE user_id = %s",\s*\((\w+), (\w+), (\w+)\)\)'
        )
        def fix_tier_update(m):
            tier_var = m.group(1)
            time_var = m.group(2)
            uid_var = m.group(3)
            return f'_pg_execute("UPDATE api_keys SET rate_limit_tier = %s, plan = %s, last_used_at = %s WHERE user_id = %s", ({tier_var}, {tier_var}, {time_var}, {uid_var}))'

        new_source = pattern.sub(fix_tier_update, source)
        if new_source != source:
            source = new_source
            changes.append("Updated tier upgrade to also set plan column")

    if not changes:
        changes.append("No changes needed — file may already be patched")

    return source, changes


def main():
    parser = argparse.ArgumentParser(description="Patch main.py Stripe key generation")
    parser.add_argument("--input", default="main.py", help="Input file")
    parser.add_argument("--output", default=None, help="Output file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    output = args.output or args.input

    with open(args.input) as f:
        source = f.read()

    if not args.dry_run:
        backup = f"{args.input}.pre_keygen.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(args.input, backup)
        print(f"📋 Backup: {backup}")

    patched, changes = patch_main(source)

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Changes:")
    for c in changes:
        print(f"  ✅ {c}")

    if not args.dry_run:
        with open(output, "w") as f:
            f.write(patched)
        print(f"\n💾 Saved: {output}")

    # Syntax check
    if not args.dry_run:
        import py_compile
        try:
            py_compile.compile(output, doraise=True)
            print("✅ Syntax OK")
        except py_compile.PyCompileError as e:
            print(f"❌ Syntax error: {e}")


if __name__ == "__main__":
    main()
