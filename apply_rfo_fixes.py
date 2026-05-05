#!/usr/bin/env python3
"""
RFO Corrective Action Fixes — Run on Replit workspace root
1. Add hash format validation on login (auth_routes.py)
2. Add legacy 100k compat to main.py fallback verify_password
3. Log warning for mismatched hash formats

Usage: python3 apply_rfo_fixes.py
"""

import re

# ============================================================
# FIX 1: auth_routes.py — Add hash format validation on login
# ============================================================
print("=" * 60)
print("FIX 1: Adding hash format validation to login route")
print("=" * 60)

with open('routes/auth_routes.py', 'r') as f:
    auth_content = f.read()

# Find the login verify block and add hash format check before it
old_login_check = """            if not pw_hash or not verify_password(password, pw_hash):
                return jsonify({'error': 'Invalid credentials'}), 401"""

new_login_check = """            # RFO Fix: Validate hash format before verify attempt
            if pw_hash and ':' not in pw_hash:
                logger.warning(f"HASH_FORMAT_MISMATCH: user {user_email} (id={user_id}) has non-standard password hash (len={len(pw_hash)}, prefix={pw_hash[:10]}). Expected salt:hash PBKDF2 format.")
                return jsonify({'error': 'Invalid credentials. Please reset your password at /forgot-password or contact support.'}), 401
            if not pw_hash or not verify_password(password, pw_hash):
                return jsonify({'error': 'Invalid credentials'}), 401"""

if old_login_check in auth_content:
    auth_content = auth_content.replace(old_login_check, new_login_check)
    with open('routes/auth_routes.py', 'w') as f:
        f.write(auth_content)
    print("  ✅ Hash format validation added to login route")
else:
    print("  ⚠️  Could not find login check block — may already be patched")
    print("     Looking for pattern...")
    if 'HASH_FORMAT_MISMATCH' in auth_content:
        print("  ✅ Already patched!")
    else:
        print("  ❌ Manual fix needed — login verify block has changed")

# ============================================================
# FIX 2: main.py — Add 100k legacy compat to fallback verify
# ============================================================
print()
print("=" * 60)
print("FIX 2: Adding legacy 100k compat to main.py fallback verify")
print("=" * 60)

with open('main.py', 'r') as f:
    main_content = f.read()

old_fallback_verify = """    def verify_password(p, hs):
        try:
            s, hx = hs.split(':')
            return _hlib.pbkdf2_hmac('sha256', p.encode(), s.encode(), 10000).hex() == hx
        except: return False"""

new_fallback_verify = """    def verify_password(p, hs):
        try:
            if ':' not in hs:
                logger.warning(f"HASH_FORMAT_MISMATCH in fallback verify: non-standard hash (len={len(hs)})")
                return False
            s, hx = hs.split(':')
            # Try 10k iterations first (current standard)
            if _hlib.pbkdf2_hmac('sha256', p.encode(), s.encode(), 10000).hex() == hx:
                return True
            # Legacy compat: try 100k iterations (api_server.py used this)
            if _hlib.pbkdf2_hmac('sha256', p.encode(), s.encode(), 100000).hex() == hx:
                return True
            return False
        except: return False"""

if old_fallback_verify in main_content:
    main_content = main_content.replace(old_fallback_verify, new_fallback_verify)
    with open('main.py', 'w') as f:
        f.write(main_content)
    print("  ✅ Legacy 100k compat added to fallback verify_password")
else:
    print("  ⚠️  Could not find fallback verify block — may already be patched")
    if 'Legacy compat: try 100k iterations' in main_content:
        print("  ✅ Already patched!")
    else:
        print("  ❌ Manual fix needed — fallback verify block has changed")

# ============================================================
# FIX 3: Verify api_server.py and auto_pilot.py are not
#         creating users in production (informational)
# ============================================================
print()
print("=" * 60)
print("FIX 3: Audit — non-production user creation paths")
print("=" * 60)

try:
    with open('api_server.py', 'r') as f:
        api_content = f.read()
    if 'def register_user' in api_content:
        # Check if it uses SQLite (harmless) or Neon (dangerous)
        if 'sqlite' in api_content.lower() or '?' in api_content[api_content.index('def register_user'):api_content.index('def register_user')+500]:
            print("  ℹ️  api_server.py register_user uses SQLite — not production, safe to ignore")
        else:
            print("  ⚠️  api_server.py register_user may write to production DB — REVIEW NEEDED")
except FileNotFoundError:
    print("  ℹ️  api_server.py not found — skipping")

try:
    with open('auto_pilot.py', 'r') as f:
        ap_content = f.read()
    if 'def register_user' in ap_content:
        if 'sqlite' in ap_content.lower() or 'get_db()' in ap_content[ap_content.index('def register_user'):ap_content.index('def register_user')+500]:
            print("  ℹ️  auto_pilot.py register_user uses SQLite — not production, safe to ignore")
        else:
            print("  ⚠️  auto_pilot.py register_user may write to production DB — REVIEW NEEDED")
except FileNotFoundError:
    print("  ℹ️  auto_pilot.py not found — skipping")

print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
print("  After running this script:")
print("  1. git add routes/auth_routes.py main.py")
print("  2. git commit -m 'fix: RFO corrective actions - hash validation, legacy compat'")
print("  3. git push origin main")
print("  4. Test: curl login with a valid user to confirm no regression")
print("=" * 60)
