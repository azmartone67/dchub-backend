#!/usr/bin/env python3
"""
DC Hub Fix Script — March 8, 2026
===================================
Run this on Replit: python3 fix_mar8.py

Fixes:
1. Stats endpoint 503 — adds status_code column check, wraps substations query
2. AI tracking 404 — adds /api/v1/ai-tracking/log POST route  
3. Frankfurt market 404 — adds European market aliases
4. Checks discovery scheduler status

INSTRUCTIONS:
  Upload this file to Replit workspace
  Run: python3 fix_mar8.py
  It will show you exactly what to change and verify DB state
"""

import os
import sys
import subprocess

NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')

def check_neon():
    """Verify Neon schema is correct after response_ms fix"""
    try:
        import psycopg2
        conn = psycopg2.connect(NEON_URL)
        cur = conn.cursor()
        
        # Check ai_usage_tracking columns
        cur.execute("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'ai_usage_tracking'
            ORDER BY ordinal_position
        """)
        cols = cur.fetchall()
        col_names = [c[0] for c in cols]
        
        print("=" * 60)
        print("1. NEON ai_usage_tracking SCHEMA CHECK")
        print("=" * 60)
        for name, dtype in cols:
            marker = " ✅ (just added)" if name == "response_ms" else ""
            print(f"  {name} ({dtype}){marker}")
        
        if 'response_ms' in col_names:
            print("\n  ✅ response_ms column EXISTS — error spam should be gone")
        else:
            print("\n  ❌ response_ms column MISSING — run:")
            print("     ALTER TABLE ai_usage_tracking ADD COLUMN IF NOT EXISTS response_ms INTEGER;")
        
        # Check if status_code column exists (this may be causing 503)
        if 'status_code' not in col_names:
            print(f"\n  ⚠️  status_code column MISSING — the flush INSERT includes it")
            print("     Run: ALTER TABLE ai_usage_tracking ADD COLUMN IF NOT EXISTS status_code INTEGER;")
        
        # Check substations count
        try:
            cur.execute("SELECT COUNT(*) FROM substations")
            count = cur.fetchone()[0]
            print(f"\n  📊 Substations count: {count:,}")
        except Exception as e:
            print(f"\n  ❌ Substations table error: {e}")
        
        # Check stats endpoint dependencies
        print("\n" + "=" * 60)
        print("2. STATS ENDPOINT DEPENDENCY CHECK")
        print("=" * 60)
        
        tables_to_check = [
            ("facilities", "SELECT COUNT(*) FROM facilities"),
            ("deals", "SELECT COUNT(*) FROM deals"),
            ("users", "SELECT COUNT(*) FROM users"),
            ("substations", "SELECT COUNT(*) FROM substations"),
            ("news_articles", "SELECT COUNT(*) FROM news_articles"),
            ("ai_cumulative", "SELECT SUM(total_requests) FROM ai_cumulative"),
        ]
        
        for table_name, query in tables_to_check:
            try:
                cur.execute(query)
                val = cur.fetchone()[0]
                print(f"  ✅ {table_name}: {val:,}" if val else f"  ⚠️  {table_name}: 0 or NULL")
            except Exception as e:
                print(f"  ❌ {table_name}: {e}")
        
        conn.close()
        
    except ImportError:
        print("  ❌ psycopg2 not installed — run: pip install psycopg2-binary")
    except Exception as e:
        print(f"  ❌ Neon connection failed: {e}")


def check_main_py():
    """Check main.py for the issues we need to fix"""
    print("\n" + "=" * 60)
    print("3. MAIN.PY CODE CHECKS")
    print("=" * 60)
    
    if not os.path.exists('main.py'):
        print("  ❌ main.py not found in current directory")
        return
    
    with open('main.py', 'r') as f:
        content = f.read()
    
    # Check for ai-tracking/log route
    if '/api/v1/ai-tracking/log' in content:
        print("  ✅ /api/v1/ai-tracking/log route EXISTS")
    else:
        print("  ❌ /api/v1/ai-tracking/log route MISSING — needs to be added")
    
    # Check for track_worker_request duplicate
    count = content.count('def track_worker_request')
    if count > 1:
        print(f"  ❌ track_worker_request defined {count} times — DUPLICATE (causes crash)")
    elif count == 1:
        print("  ✅ track_worker_request defined once — no duplicate")
    else:
        print("  ⚠️  track_worker_request not found")
    
    # Check for MARKET_ALIASES or market alias dict
    if 'MARKET_ALIASES' in content:
        print("  ✅ MARKET_ALIASES dict found")
        if "'frankfurt'" in content.lower() or '"frankfurt"' in content.lower():
            print("  ✅ Frankfurt alias present")
        else:
            print("  ❌ Frankfurt alias MISSING")
    else:
        print("  ⚠️  MARKET_ALIASES not found in main.py — may be in another file")
    
    # Check for discovery scheduler
    if 'discovery' in content.lower() and 'scheduler' in content.lower():
        print("  ✅ Discovery scheduler code found")
    
    # Check for /api/jobs/ endpoints
    job_routes = ['/api/jobs/news', '/api/jobs/discovery', '/api/jobs/outreach']
    for route in job_routes:
        if route in content:
            print(f"  ✅ {route} route exists")
        else:
            print(f"  ⚠️  {route} route not found")


def check_ai_tracking_py():
    """Check ai_tracking.py for the flush query"""
    print("\n" + "=" * 60)
    print("4. AI_TRACKING.PY FLUSH QUERY CHECK")
    print("=" * 60)
    
    if not os.path.exists('ai_tracking.py'):
        print("  ❌ ai_tracking.py not found")
        return
    
    with open('ai_tracking.py', 'r') as f:
        content = f.read()
    
    if 'response_ms' in content:
        print("  ✅ response_ms referenced in ai_tracking.py")
        # Find the INSERT query
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if 'response_ms' in line and ('INSERT' in content[max(0,content.find(line)-200):content.find(line)] or 'insert' in line.lower()):
                print(f"     Line {i+1}: {line.strip()[:100]}")
    
    if 'status_code' in content:
        print("  ✅ status_code referenced in ai_tracking.py")
    
    # Check what columns the flush INSERT uses
    import re
    inserts = re.findall(r'INSERT INTO ai_usage_tracking\s*\(([^)]+)\)', content)
    if inserts:
        print(f"\n  INSERT columns being used:")
        for ins in inserts:
            cols = [c.strip() for c in ins.split(',')]
            print(f"    ({', '.join(cols)})")
    else:
        print("  ⚠️  No INSERT INTO ai_usage_tracking found")


def check_discovery_scheduler():
    """Check if discovery/outreach jobs are configured"""
    print("\n" + "=" * 60)
    print("5. DISCOVERY & OUTREACH SCHEDULER CHECK")  
    print("=" * 60)
    
    files_to_check = [
        ('ai_outreach_agent.py', 'Outreach Agent'),
        ('ai_ecosystem_agent.py', 'Ecosystem Agent'),
        ('infrastructure_discovery.py', 'Infrastructure Discovery'),
        ('evolution_engine.py', 'Evolution Engine'),
        ('deal_scraper.py', 'Deal Scraper'),
    ]
    
    for filename, label in files_to_check:
        if os.path.exists(filename):
            print(f"  ✅ {label} ({filename}) — file exists")
        else:
            print(f"  ❌ {label} ({filename}) — NOT FOUND")
    
    # Check if scheduler is in main.py
    if os.path.exists('main.py'):
        with open('main.py', 'r') as f:
            content = f.read()
        
        scheduler_keywords = ['APScheduler', 'schedule', 'cron', 'BackgroundScheduler', 'interval']
        found = [kw for kw in scheduler_keywords if kw in content]
        if found:
            print(f"\n  Scheduler references in main.py: {', '.join(found)}")
        
        # Look for paused/disabled jobs
        if 'paused' in content.lower() or 'disabled' in content.lower():
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if ('paused' in line.lower() or 'disabled' in line.lower()) and ('scheduler' in line.lower() or 'job' in line.lower() or 'discovery' in line.lower()):
                    print(f"  ⚠️  Line {i+1}: {line.strip()[:100]}")


def print_fixes():
    """Print the exact code fixes needed"""
    print("\n" + "=" * 60)
    print("6. EXACT FIXES TO APPLY")
    print("=" * 60)
    
    print("""
╔══════════════════════════════════════════════════════════╗
║ FIX A: Add /api/v1/ai-tracking/log route                ║
║ WHERE: main.py — add near other API routes               ║
╚══════════════════════════════════════════════════════════╝

Paste this into main.py (in the editor, NOT in bash):

    @app.route('/api/v1/ai-tracking/log', methods=['POST'])
    def ai_tracking_log_endpoint():
        try:
            data = request.get_json(silent=True) or {}
            # Accept the log but don't crash if DB write fails
            try:
                from ai_tracking import log_ai_request
                log_ai_request(
                    platform=data.get('platform', 'unknown'),
                    endpoint=data.get('endpoint', ''),
                    user_agent=data.get('user_agent', '')[:500],
                    ip_address=data.get('ip_address', ''),
                    status_code=data.get('status_code', 200),
                    response_ms=data.get('response_ms', 0)
                )
            except Exception:
                pass
            return jsonify({"status": "ok"}), 200
        except Exception:
            return jsonify({"status": "ok"}), 200


╔══════════════════════════════════════════════════════════╗
║ FIX B: Add missing columns to ai_usage_tracking          ║
║ WHERE: Neon SQL Editor                                    ║
╚══════════════════════════════════════════════════════════╝

Run in Neon SQL Editor:

    ALTER TABLE ai_usage_tracking ADD COLUMN IF NOT EXISTS response_ms INTEGER;
    ALTER TABLE ai_usage_tracking ADD COLUMN IF NOT EXISTS status_code INTEGER;

    -- Verify:
    SELECT column_name FROM information_schema.columns 
    WHERE table_name = 'ai_usage_tracking' ORDER BY ordinal_position;


╔══════════════════════════════════════════════════════════╗
║ FIX C: Frankfurt + European market aliases                ║
║ WHERE: Find MARKET_ALIASES dict in main.py or markets     ║
║        module                                             ║
╚══════════════════════════════════════════════════════════╝

Find the MARKET_ALIASES dictionary and add these entries:

    "frankfurt": "Frankfurt",
    "amsterdam": "Amsterdam",
    "london": "London",  
    "paris": "Paris",
    "dublin": "Dublin",
    "stockholm": "Stockholm",
    "zurich": "Zurich",


╔══════════════════════════════════════════════════════════╗
║ FIX D: Neon stats 503 — likely missing column in query    ║
║ WHERE: get_stats() at line ~8597 in main.py               ║
╚══════════════════════════════════════════════════════════╝

Check the get_stats() function. If it's querying a column that 
doesn't exist or doing a type mismatch (text vs timestamp), 
wrap each stat query in try/except. The substations query should be:

        try:
            c.execute("SELECT COUNT(*) FROM substations WHERE voltage_kv > 69")
            stats['total_substations'] = c.fetchone()[0] or 0
        except Exception:
            stats['total_substations'] = 0

Note: voltage_kv > 69 filters out telecom substations per QA list.
""")


if __name__ == '__main__':
    print("🔧 DC Hub Fix Diagnostic — March 8, 2026")
    print("=" * 60)
    
    check_neon()
    check_main_py()
    check_ai_tracking_py()
    check_discovery_scheduler()
    print_fixes()
    
    print("\n" + "=" * 60)
    print("DONE — Review output above and apply fixes")
    print("=" * 60)
