#!/usr/bin/env python3
"""
Fix deferred_db_init connection leaks in main.py.

Problem: init_new_tables() and init_partner_inquiries_table() use get_db() + conn.close()
which checks out a pool connection and destroys it instead of returning it.
When called from deferred_db_init background thread, these hold connections for 72+ seconds
triggering FORCED RECLAIM warnings.

Fix: Use return_pg_connection(conn) instead of conn.close(), and add proper finally blocks.

Also fixes the unsubscribe endpoint at ~line 5513 which has the same pattern.

Run from /workspace on Railway shell:
    python3 fix_deferred_init_leaks.py
"""
import sys

FILENAME = 'main.py'

with open(FILENAME, 'r') as f:
    content = f.read()

original = content
changes = 0

# =============================================================================
# FIX 1: init_new_tables — use return_pg_connection instead of conn.close()
# =============================================================================
old_init_new_tables = '''def init_new_tables():
    """Initialize new tables for v74 features"""
    conn = get_db()
    try:
        c = conn.cursor()
        _init_new_tables_inner(conn, c)
    finally:
        try: conn.close()
        except: pass'''

new_init_new_tables = '''def init_new_tables():
    """Initialize new tables for v74 features"""
    conn = get_db()
    try:
        c = conn.cursor()
        _init_new_tables_inner(conn, c)
    finally:
        return_pg_connection(conn)'''

if old_init_new_tables in content:
    content = content.replace(old_init_new_tables, new_init_new_tables)
    changes += 1
    print("  ✅ Fix 1: init_new_tables — conn.close() → return_pg_connection()")
else:
    print("  ⚠️ Fix 1: init_new_tables pattern not found (may already be fixed)")

# =============================================================================
# FIX 2: init_partner_inquiries_table — use return_pg_connection + finally
# =============================================================================
old_partner = '''def init_partner_inquiries_table():
    """Initialize partner inquiries table"""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS partner_inquiries (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT NOT NULL,
            company TEXT,
            partner_type TEXT,
            message TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT,
            responded_at TEXT,
            notes TEXT
        )
    """)
    conn.commit()
    conn.close()'''

new_partner = '''def init_partner_inquiries_table():
    """Initialize partner inquiries table"""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS partner_inquiries (
                id TEXT PRIMARY KEY,
                name TEXT,
                email TEXT NOT NULL,
                company TEXT,
                partner_type TEXT,
                message TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT,
                responded_at TEXT,
                notes TEXT
            )
        """)
        conn.commit()
    finally:
        return_pg_connection(conn)'''

if old_partner in content:
    content = content.replace(old_partner, new_partner)
    changes += 1
    print("  ✅ Fix 2: init_partner_inquiries_table — added try/finally + return_pg_connection()")
else:
    print("  ⚠️ Fix 2: init_partner_inquiries_table pattern not found")

# =============================================================================
# FIX 3: unsubscribe endpoint — same conn.close() leak pattern
# =============================================================================
old_unsub = '''    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE leads SET subscribed = 0 WHERE email = %s", (email,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Unsubscribed successfully'})'''

new_unsub = '''    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("UPDATE leads SET subscribed = 0 WHERE email = %s", (email,))
        conn.commit()
    finally:
        return_pg_connection(conn)
    
    return jsonify({'success': True, 'message': 'Unsubscribed successfully'})'''

if old_unsub in content:
    content = content.replace(old_unsub, new_unsub)
    changes += 1
    print("  ✅ Fix 3: unsubscribe endpoint — added try/finally + return_pg_connection()")
else:
    print("  ⚠️ Fix 3: unsubscribe pattern not found")

# =============================================================================
# FIX 4: Rankings blueprint — ensure unique name to avoid collision
# Check if the duplicate registration still exists
# =============================================================================
# The energy_routes import with rankings_bp
old_rankings = '''try:
    from routes.energy_routes import rankings_bp, _register_rankings_routes
    _register_rankings_routes(rankings_bp, db_pool=_pg_pool_obj, require_plan=require_plan)
    app.register_blueprint(rankings_bp)
    print("⚡ Energy Routes Blueprint: ✅ Registered")
except Exception as e:
    print(f"⚡ Energy Routes Blueprint: ⚠️ Failed to load: {e}")'''

new_rankings = '''try:
    from routes.energy_routes import energy_bp as _energy_bp
    app.register_blueprint(_energy_bp)
    print("⚡ Energy Routes Blueprint: ✅ Registered")
except ImportError:
    try:
        from routes.energy_routes import rankings_bp as _energy_rankings_bp, _register_rankings_routes as _ereg
        _ereg(_energy_rankings_bp, db_pool=_pg_pool_obj, require_plan=require_plan)
        app.register_blueprint(_energy_rankings_bp, name='energy_rankings')
        print("⚡ Energy Routes Blueprint: ✅ Registered (legacy, unique name)")
    except Exception as e2:
        print(f"⚡ Energy Routes Blueprint: ⚠️ Failed to load: {e2}")
except Exception as e:
    print(f"⚡ Energy Routes Blueprint: ⚠️ Failed to load: {e}")'''

if old_rankings in content:
    content = content.replace(old_rankings, new_rankings)
    changes += 1
    print("  ✅ Fix 4: Rankings blueprint — added unique name='energy_rankings' to avoid collision")
else:
    print("  ⚠️ Fix 4: Rankings blueprint pattern not found (may already be fixed)")

# =============================================================================
# Write and verify
# =============================================================================
if changes == 0:
    print("\n⚠️ No changes made — patterns may differ from expected")
    sys.exit(1)

with open(FILENAME, 'w') as f:
    f.write(content)

import py_compile
try:
    py_compile.compile(FILENAME, doraise=True)
    print(f"\n✅ {changes} fix(es) applied — syntax check PASSED")
    print("\nCommit and push:")
    print("  git add main.py")
    print('  git commit -m "fix: deferred_db_init connection leaks + rankings blueprint collision"')
    print("  git push origin main")
except py_compile.PyCompileError as e:
    print(f"\n❌ Syntax error: {e}")
    print("Restoring original...")
    with open(FILENAME, 'w') as f:
        f.write(original)
    sys.exit(1)
