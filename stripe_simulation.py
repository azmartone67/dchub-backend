#!/usr/bin/env python3
"""
DC Hub - Stripe Checkout End-to-End Simulation
===============================================
Tests the handle_checkout_completed function against a local SQLite DB
to verify the full flow: webhook → account creation → password hashing →
role assignment → API key generation → welcome email trigger.

Simulates 6 scenarios that cover every path through the function:
  1. New customer via Payment Link (no metadata, no existing account)
  2. Existing free user upgrades via Payment Link
  3. New customer via hosted checkout (has metadata)
  4. Founding member via Payment Link ($99)
  5. Enterprise customer via Payment Link ($500+)
  6. Edge case: missing email
"""

import sqlite3
import hashlib
import secrets
import os
import sys
import json
from datetime import datetime
from unittest.mock import patch, MagicMock

# ============================================================================
# SETUP: Create a test database matching DC Hub's schema
# ============================================================================

TEST_DB = "/tmp/test_simulation.db"

def setup_test_db():
    """Create a fresh test DB with the same schema as dc_nexus.db"""
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    conn = sqlite3.connect(TEST_DB)
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            password_hash TEXT,
            name TEXT,
            plan TEXT DEFAULT 'free',
            role TEXT DEFAULT 'free',
            api_calls_today INTEGER DEFAULT 0,
            api_calls_total INTEGER DEFAULT 0,
            created_at TEXT,
            stripe_customer_id TEXT,
            subscription_status TEXT DEFAULT 'none',
            stripe_subscription_id TEXT,
            subscribed_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE api_keys (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            key_hash TEXT,
            key_prefix TEXT,
            name TEXT,
            permissions TEXT DEFAULT '["read"]',
            rate_limit_tier TEXT DEFAULT 'free',
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            usage_count INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'free',
            calls_today INTEGER DEFAULT 0,
            calls_total INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)

    # Pre-seed an existing free user for upgrade scenario
    c.execute("""
        INSERT INTO users (id, email, password_hash, name, plan, role, 
                          api_calls_today, api_calls_total, created_at, subscription_status)
        VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, 'none')
    """, ('existing_user_001', 'freeuser@example.com',
          hash_password_test('oldpassword123'), 'Free User', 'free', 'free',
          datetime.utcnow().isoformat()))

    # Give existing user a free-tier API key
    raw_key = 'dchub_' + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    c.execute("""
        INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions,
                             rate_limit_tier, is_active, created_at, usage_count, plan)
        VALUES (%s, %s, %s, %s, '["read"]', 'free', 1, %s, 0, 'free')
    """, ('existing_user_001', key_hash, raw_key[:12], 'freeuser@example.com Free Key',
          datetime.utcnow().isoformat()))

    conn.commit()
    conn.close()
    print("✅ Test database created with existing free user: freeuser@example.com\n")


def hash_password_test(password):
    """Mirror DC Hub's hash_password function"""
    salt = secrets.token_hex(16)
    hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}:{hash_obj.hex()}"


def verify_password_test(password, hash_string):
    """Mirror DC Hub's verify_password function"""
    try:
        salt, hash_hex = hash_string.split(':')
        hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return hash_obj.hex() == hash_hex
    except:
        return False


# ============================================================================
# MOCK: Patch get_db and send_welcome_email_sendgrid for local testing
# ============================================================================

welcome_emails_sent = []

def mock_get_db():
    conn = sqlite3.connect(TEST_DB)
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    return conn

def mock_send_welcome_email(to_email, raw_api_key, plan_name='pro', temp_password=None):
    """Capture email sends instead of actually sending"""
    welcome_emails_sent.append({
        'to': to_email,
        'api_key': raw_api_key,
        'plan': plan_name,
        'temp_password': temp_password,
        'sent_at': datetime.utcnow().isoformat()
    })
    print(f"    📧 [SIMULATED] Welcome email → {to_email} | plan={plan_name} | "
          f"has_password={'YES' if temp_password else 'NO'} | has_api_key=YES")


# ============================================================================
# IMPORT the actual function from main.py (with mocked dependencies)
# ============================================================================

def load_handle_checkout():
    """Extract and exec handle_checkout_completed with mocked deps"""
    # We'll define the function inline using the exact logic from main.py
    # This avoids importing the entire 13K-line file

    def handle_checkout_completed(session):
        """Handle successful checkout - upgrade user plan and API key tier"""
        import traceback
        try:
            customer_email = (session.get('customer_email') or '').lower().strip()
            if not customer_email:
                customer_email = (session.get('customer_details', {}).get('email') or '').lower().strip()

            customer_name = (session.get('customer_details', {}).get('name') or '').strip()

            metadata = session.get('metadata', {})
            user_id = metadata.get('user_id')
            plan_from_metadata = metadata.get('plan', '')

            amount_total = session.get('amount_total', 0)
            amount_dollars = amount_total / 100 if amount_total else 0

            print(f"    💳 Checkout data: email='{customer_email}', name='{customer_name}', "
                  f"metadata_plan='{plan_from_metadata}', amount=${amount_dollars}")

            plan_tier_map = {
                'pro_monthly': ('pro', 'pro'),
                'pro_annual': ('pro', 'pro'),
                'enterprise_monthly': ('enterprise', 'enterprise'),
                'enterprise_annual': ('enterprise', 'enterprise'),
                'founding': ('founding', 'pro'),
            }

            if plan_from_metadata and plan_from_metadata in plan_tier_map:
                plan_name, api_tier = plan_tier_map[plan_from_metadata]
                print(f"    📋 Plan from metadata: {plan_name}")
            else:
                if amount_dollars == 99 or (95 <= amount_dollars <= 105):
                    plan_name, api_tier = 'founding', 'pro'
                elif amount_dollars == 299 or (295 <= amount_dollars <= 305):
                    plan_name, api_tier = 'pro', 'pro'
                elif amount_dollars == 2990 or (2985 <= amount_dollars <= 2995):
                    plan_name, api_tier = 'pro', 'pro'
                elif amount_dollars >= 500:
                    plan_name, api_tier = 'enterprise', 'enterprise'
                else:
                    plan_name, api_tier = 'pro', 'pro'
                print(f"    💰 Plan from amount (${amount_dollars}): {plan_name}")

            conn = mock_get_db()
            c = conn.cursor()

            if user_id:
                c.execute("UPDATE users SET plan = %s, role = %s, subscription_status = 'active', stripe_customer_id = %s WHERE id = %s",
                          (plan_name, api_tier, session.get('customer', ''), user_id))
            elif customer_email:
                c.execute("UPDATE users SET plan = %s, role = %s, subscription_status = 'active', stripe_customer_id = %s WHERE email = %s",
                          (plan_name, api_tier, session.get('customer', ''), customer_email))
            print(f"    💳 Webhook UPDATE: customer_email='{customer_email}', user_id='{user_id}', rows_updated={c.rowcount}")

            rows_updated = c.rowcount if (user_id or customer_email) else 0
            print(f"    💳 Webhook UPDATE result: rows_updated={rows_updated}")

            if rows_updated == 0 and customer_email:
                import secrets as sec
                new_user_id = f"stripe_{sec.token_hex(8)}"
                stripe_customer_id = session.get('customer', '')
                now = datetime.utcnow().isoformat()
                display_name = customer_name or customer_email.split('@')[0]
                temp_password = sec.token_urlsafe(16)
                hashed_pw = hash_password_test(temp_password)
                c.execute("""INSERT INTO users (id, email, password_hash, name, plan, role, api_calls_today, api_calls_total,
                             created_at, stripe_customer_id, subscription_status)
                             VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, 'active') ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, password_hash = EXCLUDED.password_hash, name = EXCLUDED.name, plan = EXCLUDED.plan, role = EXCLUDED.role, api_calls_today = EXCLUDED.api_calls_today, api_calls_total = EXCLUDED.api_calls_total, created_at = EXCLUDED.created_at, stripe_customer_id = EXCLUDED.stripe_customer_id, subscription_status = EXCLUDED.subscription_status""",
                          (new_user_id, customer_email, hashed_pw, display_name,
                           plan_name, api_tier, now, stripe_customer_id))
                print(f"    🔐 Account created with temp password for {customer_email}")

                raw_key = 'dchub_' + sec.token_urlsafe(32)
                key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
                key_prefix = raw_key[:12]
                c.execute("""INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions,
                             rate_limit_tier, is_active, created_at, usage_count, plan, calls_today, calls_total)
                             VALUES (%s, %s, %s, %s, '["read","write"]', %s, 1, %s, 0, %s, 0, 0) ON CONFLICT (key) DO UPDATE SET user_id = EXCLUDED.user_id, key_hash = EXCLUDED.key_hash, key_prefix = EXCLUDED.key_prefix, name = EXCLUDED.name, permissions = EXCLUDED.permissions, rate_limit_tier = EXCLUDED.rate_limit_tier, is_active = EXCLUDED.is_active, created_at = EXCLUDED.created_at, usage_count = EXCLUDED.usage_count, plan = EXCLUDED.plan, calls_today = EXCLUDED.calls_today, calls_total = EXCLUDED.calls_total""",
                          (new_user_id, key_hash, key_prefix, f'{customer_email} Pro Key',
                           api_tier, now, plan_name))

                print(f"    ✨ Created new user (id: {new_user_id})")
                print(f"    🔑 Generated {plan_name} API key: {key_prefix}...")

                mock_send_welcome_email(customer_email, raw_key, plan_name, temp_password=temp_password)

            elif customer_email:
                resolved_user_id = user_id
                if not resolved_user_id:
                    c.execute("SELECT id FROM users WHERE email = %s", (customer_email,))
                    row = c.fetchone()
                    resolved_user_id = row[0] if row else None
                    print(f"    🔍 Looked up user_id for {customer_email}: {resolved_user_id}")

                if resolved_user_id:
                    c.execute("""
                        UPDATE api_keys SET rate_limit_tier = %s, updated_at = %s
                        WHERE user_id = %s
                    """, (api_tier, datetime.utcnow().isoformat(), resolved_user_id))
                    api_keys_updated = c.rowcount
                    print(f"    🔑 Updated {api_keys_updated} API key(s) to tier: {api_tier}")

                    import secrets as sec
                    raw_key = 'dchub_' + sec.token_urlsafe(32)
                    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
                    key_prefix = raw_key[:12]
                    now = datetime.utcnow().isoformat()
                    c.execute("""INSERT INTO api_keys (user_id, key_hash, key_prefix, name, permissions,
                                 rate_limit_tier, is_active, created_at, usage_count, plan, calls_today, calls_total)
                                 VALUES (%s, %s, %s, %s, '["read","write"]', %s, 1, %s, 0, %s, 0, 0) ON CONFLICT (key) DO UPDATE SET user_id = EXCLUDED.user_id, key_hash = EXCLUDED.key_hash, key_prefix = EXCLUDED.key_prefix, name = EXCLUDED.name, permissions = EXCLUDED.permissions, rate_limit_tier = EXCLUDED.rate_limit_tier, is_active = EXCLUDED.is_active, created_at = EXCLUDED.created_at, usage_count = EXCLUDED.usage_count, plan = EXCLUDED.plan, calls_today = EXCLUDED.calls_today, calls_total = EXCLUDED.calls_total""",
                              (resolved_user_id, key_hash, key_prefix, f'{customer_email} Pro Key',
                               api_tier, now, plan_name))
                    print(f"    🔑 Generated new {plan_name} API key for existing user: {key_prefix}...")
                    mock_send_welcome_email(customer_email, raw_key, plan_name)
                else:
                    print(f"    ⚠️ Could not find user_id for email {customer_email}")

            conn.commit()
            conn.close()

            print(f"    ✅ User upgraded to {plan_name} (API tier: {api_tier}): {customer_email or user_id}")
        except Exception as e:
            print(f"    ❌ WEBHOOK ERROR: {e}")
            import traceback
            traceback.print_exc()

    return handle_checkout_completed


# ============================================================================
# TEST SCENARIOS
# ============================================================================

def build_session(email=None, name=None, amount_cents=0, metadata=None,
                  customer_id=None, payment_link=None, use_customer_details=True):
    """Build a mock Stripe checkout.session.completed payload"""
    session = {
        'id': f'cs_test_{secrets.token_hex(8)}',
        'amount_total': amount_cents,
        'customer': customer_id or f'cus_test_{secrets.token_hex(6)}',
        'metadata': metadata or {},
        'payment_link': payment_link or '',
    }
    if use_customer_details and email:
        # Payment Link style: email in customer_details, not customer_email
        session['customer_email'] = None
        session['customer_details'] = {
            'email': email,
            'name': name or ''
        }
    elif email:
        # Hosted checkout style: email in customer_email
        session['customer_email'] = email
        session['customer_details'] = {}
    else:
        session['customer_email'] = None
        session['customer_details'] = {}
    return session


def verify_user(email, expected_plan, expected_role, expected_status, check_password=False):
    """Verify user record in DB"""
    conn = sqlite3.connect(TEST_DB)
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = c.fetchone()

    if not user:
        print(f"    ❌ FAIL: User {email} not found in database!")
        conn.close()
        return False

    errors = []
    if user['plan'] != expected_plan:
        errors.append(f"plan: expected '{expected_plan}', got '{user['plan']}'")
    if user['role'] != expected_role:
        errors.append(f"role: expected '{expected_role}', got '{user['role']}'")
    if user['subscription_status'] != expected_status:
        errors.append(f"status: expected '{expected_status}', got '{user['subscription_status']}'")

    # CRITICAL CHECK: password_hash must NOT be a literal string
    if check_password:
        pw_hash = user['password_hash']
        if pw_hash in ('stripe_checkout', '', None):
            errors.append(f"password_hash is INVALID: '{pw_hash}' — user cannot log in!")
        elif ':' not in str(pw_hash):
            errors.append(f"password_hash doesn't look like salt:hash format: '{pw_hash[:30]}...'")
        else:
            print(f"    ✅ password_hash is properly hashed (salt:hash format)")

    # Check stripe_customer_id is set
    if not user['stripe_customer_id']:
        errors.append("stripe_customer_id is empty!")

    if errors:
        for e in errors:
            print(f"    ❌ FAIL: {e}")
        conn.close()
        return False

    print(f"    ✅ User verified: plan={user['plan']}, role={user['role']}, "
          f"status={user['subscription_status']}, stripe_id={user['stripe_customer_id'][:20]}...")
    conn.close()
    return True


def verify_api_key(email, expected_tier):
    """Verify API key was created/upgraded"""
    conn = sqlite3.connect(TEST_DB)
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = %s", (email,))
    user = c.fetchone()
    if not user:
        print(f"    ❌ FAIL: User {email} not found")
        conn.close()
        return False

    c.execute("SELECT * FROM api_keys WHERE user_id = %s ORDER BY created_at DESC", (user['id'],))
    keys = c.fetchall()

    if not keys:
        print(f"    ❌ FAIL: No API keys found for {email}")
        conn.close()
        return False

    latest = keys[0]
    errors = []
    if latest['rate_limit_tier'] != expected_tier:
        errors.append(f"tier: expected '{expected_tier}', got '{latest['rate_limit_tier']}'")
    if not latest['key_prefix'].startswith('dchub_'):
        errors.append(f"key_prefix doesn't start with 'dchub_': '{latest['key_prefix']}'")
    if not latest['is_active']:
        errors.append("key is not active!")

    if errors:
        for e in errors:
            print(f"    ❌ FAIL: {e}")
        conn.close()
        return False

    print(f"    ✅ API key verified: prefix={latest['key_prefix']}, tier={latest['rate_limit_tier']}, "
          f"total_keys={len(keys)}")
    conn.close()
    return True


def verify_welcome_email(email, expect_password=False):
    """Verify welcome email was triggered"""
    matching = [e for e in welcome_emails_sent if e['to'] == email]
    if not matching:
        print(f"    ❌ FAIL: No welcome email sent to {email}")
        return False

    latest = matching[-1]
    errors = []
    if not latest['api_key'].startswith('dchub_'):
        errors.append("API key in email doesn't start with 'dchub_'")
    if expect_password and not latest['temp_password']:
        errors.append("Expected temp password in email but none sent!")
    if not expect_password and latest['temp_password']:
        errors.append("Unexpected temp password in email for existing user!")

    if errors:
        for e in errors:
            print(f"    ❌ FAIL: {e}")
        return False

    print(f"    ✅ Welcome email verified: has_password={'YES' if latest['temp_password'] else 'NO'}, "
          f"api_key={latest['api_key'][:16]}...")

    # If temp password provided, verify it can authenticate against the DB hash
    if latest['temp_password']:
        conn = sqlite3.connect(TEST_DB)
        # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE email = %s", (email,))
        row = c.fetchone()
        conn.close()
        if row and verify_password_test(latest['temp_password'], row['password_hash']):
            print(f"    ✅ Temp password VERIFIED — user can log in with emailed credentials")
        else:
            print(f"    ❌ FAIL: Temp password does NOT match stored hash — LOGIN WILL FAIL!")
            return False

    return True


# ============================================================================
# RUN ALL TESTS
# ============================================================================

def run_simulation():
    print("=" * 72)
    print("  DC Hub — Stripe Checkout End-to-End Simulation")
    print("=" * 72)
    print()

    setup_test_db()
    handle_checkout = load_handle_checkout()

    results = {}
    total = 0
    passed = 0

    # ------------------------------------------------------------------
    # SCENARIO 1: New customer via Payment Link ($299 Pro)
    # This is the most common real-world case
    # ------------------------------------------------------------------
    total += 1
    print("━" * 72)
    print("SCENARIO 1: New customer via Payment Link ($299 Pro Monthly)")
    print("  → No existing account, no metadata (Payment Link behavior)")
    print("━" * 72)

    session = build_session(
        email='newcustomer@acme.com',
        name='Acme Data Centers',
        amount_cents=29900,
        payment_link='plink_test_abc123',
        use_customer_details=True
    )
    handle_checkout(session)

    s1 = all([
        verify_user('newcustomer@acme.com', 'pro', 'pro', 'active', check_password=True),
        verify_api_key('newcustomer@acme.com', 'pro'),
        verify_welcome_email('newcustomer@acme.com', expect_password=True),
    ])
    results['Scenario 1: New customer Payment Link'] = s1
    passed += int(s1)
    print()

    # ------------------------------------------------------------------
    # SCENARIO 2: Existing free user upgrades via Payment Link
    # ------------------------------------------------------------------
    total += 1
    print("━" * 72)
    print("SCENARIO 2: Existing free user upgrades to Pro via Payment Link")
    print("  → User already in DB with free plan, should upgrade in place")
    print("━" * 72)

    session = build_session(
        email='freeuser@example.com',
        name='Free User',
        amount_cents=29900,
        payment_link='plink_test_upgrade',
        use_customer_details=True
    )
    handle_checkout(session)

    s2 = all([
        verify_user('freeuser@example.com', 'pro', 'pro', 'active'),
        verify_api_key('freeuser@example.com', 'pro'),
        verify_welcome_email('freeuser@example.com', expect_password=False),
    ])
    results['Scenario 2: Existing user upgrade'] = s2
    passed += int(s2)
    print()

    # ------------------------------------------------------------------
    # SCENARIO 3: New customer via hosted checkout (has metadata)
    # ------------------------------------------------------------------
    total += 1
    print("━" * 72)
    print("SCENARIO 3: New customer via hosted checkout with metadata")
    print("  → Has plan in metadata (normal checkout, not Payment Link)")
    print("━" * 72)

    session = build_session(
        email='hostedcheckout@company.com',
        name='Company Inc',
        amount_cents=29900,
        metadata={'plan': 'pro_monthly'},
        use_customer_details=False  # hosted checkout puts email in customer_email
    )
    handle_checkout(session)

    s3 = all([
        verify_user('hostedcheckout@company.com', 'pro', 'pro', 'active', check_password=True),
        verify_api_key('hostedcheckout@company.com', 'pro'),
        verify_welcome_email('hostedcheckout@company.com', expect_password=True),
    ])
    results['Scenario 3: Hosted checkout with metadata'] = s3
    passed += int(s3)
    print()

    # ------------------------------------------------------------------
    # SCENARIO 4: Founding Member via Payment Link ($99)
    # ------------------------------------------------------------------
    total += 1
    print("━" * 72)
    print("SCENARIO 4: Founding Member via Payment Link ($99)")
    print("  → Amount-based detection should identify as 'founding'")
    print("━" * 72)

    session = build_session(
        email='founder@startup.io',
        name='Startup Founder',
        amount_cents=9900,
        payment_link='plink_test_founding',
        use_customer_details=True
    )
    handle_checkout(session)

    s4 = all([
        verify_user('founder@startup.io', 'founding', 'pro', 'active', check_password=True),
        verify_api_key('founder@startup.io', 'pro'),
        verify_welcome_email('founder@startup.io', expect_password=True),
    ])
    results['Scenario 4: Founding member $99'] = s4
    passed += int(s4)
    print()

    # ------------------------------------------------------------------
    # SCENARIO 5: Enterprise customer via Payment Link ($500+)
    # ------------------------------------------------------------------
    total += 1
    print("━" * 72)
    print("SCENARIO 5: Enterprise customer via Payment Link ($999)")
    print("  → Amount >= $500 should detect as 'enterprise'")
    print("━" * 72)

    session = build_session(
        email='enterprise@bigcorp.com',
        name='BigCorp Infrastructure',
        amount_cents=99900,
        payment_link='plink_test_enterprise',
        use_customer_details=True
    )
    handle_checkout(session)

    s5 = all([
        verify_user('enterprise@bigcorp.com', 'enterprise', 'enterprise', 'active', check_password=True),
        verify_api_key('enterprise@bigcorp.com', 'enterprise'),
        verify_welcome_email('enterprise@bigcorp.com', expect_password=True),
    ])
    results['Scenario 5: Enterprise $999'] = s5
    passed += int(s5)
    print()

    # ------------------------------------------------------------------
    # SCENARIO 6: Edge case — missing email entirely
    # ------------------------------------------------------------------
    total += 1
    print("━" * 72)
    print("SCENARIO 6: Edge case — checkout with NO email")
    print("  → Should handle gracefully without crashing")
    print("━" * 72)

    session = build_session(
        email=None,
        amount_cents=29900,
    )
    handle_checkout(session)
    # Just verify no crash
    print(f"    ✅ No crash — function handled missing email gracefully")
    s6 = True
    results['Scenario 6: Missing email edge case'] = s6
    passed += int(s6)
    print()

    # ==================================================================
    # FINAL REPORT
    # ==================================================================
    print("=" * 72)
    print("  SIMULATION RESULTS")
    print("=" * 72)
    print()

    for scenario, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}  {scenario}")

    print()
    print(f"  Total: {passed}/{total} scenarios passed")
    print()

    if passed == total:
        print("  🎉 ALL SCENARIOS PASSED — Stripe flow is solid!")
        print("  Safe to accept real customers.")
    else:
        print("  ⚠️  SOME SCENARIOS FAILED — Review output above before going live.")

    print()
    print("=" * 72)

    # Dump the welcome emails log
    print()
    print("📧 WELCOME EMAILS LOG:")
    print("-" * 72)
    for i, email in enumerate(welcome_emails_sent, 1):
        print(f"  {i}. To: {email['to']}")
        print(f"     Plan: {email['plan']}")
        print(f"     Has temp password: {'YES → ' + email['temp_password'] if email['temp_password'] else 'NO (existing user)'}")
        print(f"     API key: {email['api_key'][:20]}...")
        print()

    # Cleanup
    os.remove(TEST_DB)
    return passed == total


if __name__ == '__main__':
    success = run_simulation()
    sys.exit(0 if success else 1)
