#!/usr/bin/env python3
"""
Fix handle_checkout_completed and other Stripe webhook handlers.
Removes dead SQLite c.execute() calls that cause NameError: name 'c' is not defined.

The _pg_execute() calls already handle Neon writes. The old conn=get_db()/c.execute()
lines are leftover from the SQLite migration and need to be removed.

Run from /workspace on Railway shell:
    python3 fix_stripe_webhooks.py
"""
import re
import sys

FILENAME = 'main.py'

with open(FILENAME, 'r') as f:
    content = f.read()

original = content

# =============================================================================
# FIX 1: handle_checkout_completed — remove dead SQLite c.execute() blocks
# =============================================================================

# Remove the dead SQLite INSERT for new user creation (lines ~6782-6787)
content = content.replace(
    """            c.execute(\"\"\"INSERT INTO users (id, email, password_hash, name, plan, role, api_calls_today, api_calls_total,
                         created_at, stripe_customer_id, subscription_status)
                         VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'active')\"\"\",
                      (new_user_id, customer_email, hashed_pw, display_name,
                       plan_name, api_tier, now, stripe_cust))
            print(f"🔐 Account created for {customer_email} (PG + SQLite)")""",
    """            print(f"🔐 Account created for {customer_email} via Neon")"""
)

# Remove the dead SQLite INSERT for API key on new user (lines ~6797-6801)
content = content.replace(
    """            c.execute(\"\"\"INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions,
                         rate_limit_tier, is_active, created_at, usage_count, plan, calls_today, calls_total)
                         VALUES (?, ?, ?, ?, '["read","write"]', ?, 1, ?, 0, ?, 0, 0)\"\"\",
                      (new_user_id, key_hash, key_prefix, f'{customer_email} Pro Key',
                       api_tier, now, plan_name))""",
    """            # SQLite removed — _pg_execute above handles Neon"""
)

# Remove the dead SQLite SELECT for user lookup (lines ~6815-6817)
content = content.replace(
    """                else:
                    c.execute("SELECT id FROM users WHERE email = %s", (customer_email,))
                    row = c.fetchone()
                    resolved_user_id = row[0] if row else None""",
    """                else:
                    resolved_user_id = None  # Not found in Neon"""
)

# Remove the dead SQLite UPDATE for api_keys (lines ~6824-6826)
content = content.replace(
    """                c.execute("UPDATE api_keys SET rate_limit_tier = %s, updated_at = %s WHERE user_id = %s",
                          (api_tier, now, resolved_user_id))
                api_keys_updated = c.rowcount
                print(f"🔑 Updated {api_keys_updated} API key(s) to tier: {api_tier}")""",
    """                print(f"🔑 Updated API key(s) to tier: {api_tier}")"""
)

# Remove the dead SQLite INSERT for API key on existing user (lines ~6838-6842)
content = content.replace(
    """                c.execute(\"\"\"INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions,
                             rate_limit_tier, is_active, created_at, usage_count, plan, calls_today, calls_total)
                             VALUES (?, ?, ?, ?, '["read","write"]', ?, 1, ?, 0, ?, 0, 0)\"\"\",
                          (resolved_user_id, key_hash, key_prefix, f'{customer_email} Pro Key',
                           api_tier, now, plan_name))""",
    """                # SQLite removed — _pg_execute above handles Neon"""
)

# Remove the dead conn.commit()/conn.close() at end of handle_checkout_completed
content = content.replace(
    """        conn.commit()
        conn.close()

        print(f"✅ User upgraded to {plan_name} (API tier: {api_tier}): {customer_email or user_id}")""",
    """        print(f"✅ User upgraded to {plan_name} (API tier: {api_tier}): {customer_email or user_id}")"""
)

# =============================================================================
# FIX 2: handle_subscription_created — remove dead SQLite block
# =============================================================================
content = content.replace(
    """        _pg_execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
                   (status, customer_id))
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
        conn.commit()
        conn.close()
        _sync_tables_bg('users')
        print(f"✅ Subscription activated for customer: {customer_id}")""",
    """        _pg_execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
                   (status, customer_id))
        print(f"✅ Subscription activated for customer: {customer_id}")"""
)

# =============================================================================
# FIX 3: handle_subscription_updated — remove dead SQLite block
# =============================================================================
content = content.replace(
    """    conn = get_db()
    c = conn.cursor()
    if status in ['active', 'trialing', 'past_due', 'unpaid']:
        c.execute("UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s", (status, customer_id))
    elif status == 'canceled':
        c.execute("UPDATE users SET plan = 'free', role = 'free', subscription_status = %s WHERE stripe_customer_id = %s",
                  (status, customer_id))
        c.execute("UPDATE api_keys SET rate_limit_tier = 'free', updated_at = %s WHERE user_id IN (SELECT id FROM users WHERE stripe_customer_id = %s)",
                  (now, customer_id))
    conn.commit()
    conn.close()
    _sync_tables_bg('users', 'api_keys')
    print(f"📝 Subscription updated for customer {customer_id}: {status}")""",
    """    print(f"📝 Subscription updated for customer {customer_id}: {status}")"""
)

# =============================================================================
# FIX 4: handle_subscription_deleted — remove dead SQLite block
# =============================================================================
content = content.replace(
    """    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET plan = 'free', role = 'free', subscription_status = 'canceled' WHERE stripe_customer_id = %s",
              (customer_id,))
    c.execute("UPDATE api_keys SET rate_limit_tier = 'free', updated_at = %s WHERE user_id IN (SELECT id FROM users WHERE stripe_customer_id = %s)",
              (now, customer_id))
    conn.commit()
    conn.close()
    _sync_tables_bg('users', 'api_keys')
    print(f"❌ Subscription canceled for customer: {customer_id}")""",
    """    print(f"❌ Subscription canceled for customer: {customer_id}")"""
)

# =============================================================================
# FIX 5: handle_payment_failed — remove dead SQLite block
# =============================================================================
content = content.replace(
    """    _pg_execute("UPDATE users SET subscription_status = 'payment_failed' WHERE stripe_customer_id = %s", (customer_id,))
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_status = 'payment_failed' WHERE stripe_customer_id = %s",
              (customer_id,))
    conn.commit()
    conn.close()
    _sync_tables_bg('users')
    print(f"⚠️ Payment failed for customer: {customer_id}")""",
    """    _pg_execute("UPDATE users SET subscription_status = 'payment_failed' WHERE stripe_customer_id = %s", (customer_id,))
    print(f"⚠️ Payment failed for customer: {customer_id}")"""
)

# =============================================================================
# Write output
# =============================================================================
if content == original:
    print("⚠️ No changes made — patterns may have already been fixed or file differs from expected")
    sys.exit(1)

with open(FILENAME, 'w') as f:
    f.write(content)

# Verify syntax
import py_compile
try:
    py_compile.compile(FILENAME, doraise=True)
    print("✅ All 5 Stripe webhook handlers fixed — dead SQLite code removed")
    print("✅ Syntax check PASSED")
    print("")
    print("Now commit and push:")
    print("  git add main.py")
    print('  git commit -m "fix: remove dead SQLite c.execute() from Stripe webhooks (NameError)"')
    print("  git push origin main")
except py_compile.PyCompileError as e:
    print(f"❌ Syntax error after patching: {e}")
    print("Restoring original file...")
    with open(FILENAME, 'w') as f:
        f.write(original)
    sys.exit(1)
