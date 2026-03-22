#!/usr/bin/env python3
"""
DC Hub — Infrastructure Expansion Orchestrator
Runs schema creation, then all discovery scripts in sequence.

Usage:
  python3 run_discovery.py                    # Run all
  python3 run_discovery.py --layer midstream   # Run only midstream
  python3 run_discovery.py --layer pricing     # Run only EIA pricing
  python3 run_discovery.py --layer fiber       # Run only fiber/IX
  python3 run_discovery.py --verify            # Just verify tables

Requires: DATABASE_URL or NEON_DATABASE_URL env var
"""

import os
import sys
import subprocess
import psycopg2
from datetime import datetime

DATABASE_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def run_schema():
    """Create tables if they don't exist."""
    print("=" * 60)
    print("SCHEMA CREATION")
    print("=" * 60)
    
    schema_file = os.path.join(SCRIPT_DIR, "schema_creation.sql")
    if not os.path.exists(schema_file):
        print(f"ERROR: {schema_file} not found")
        return False
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        with open(schema_file) as f:
            sql = f.read()
        
        # Execute each statement separately
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    # Ignore "already exists" errors
                    if "already exists" not in str(e):
                        print(f"  Warning: {str(e)[:80]}")
                    conn.rollback()
                    cur = conn.cursor()
        
        conn.commit()
        conn.close()
        print("  ✓ Schema ready")
        return True
    except Exception as e:
        print(f"  ✗ Schema error: {e}")
        return False

def run_script(name, filename):
    """Run a discovery script as subprocess."""
    print(f"\n{'#' * 60}")
    print(f"# Running: {name}")
    print(f"{'#' * 60}")
    
    filepath = os.path.join(SCRIPT_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  ✗ {filepath} not found")
        return False
    
    try:
        result = subprocess.run(
            [sys.executable, filepath],
            timeout=600,  # 10 min timeout
            env=os.environ.copy()
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ✗ TIMEOUT: {name} exceeded 10 minutes")
        return False
    except Exception as e:
        print(f"  ✗ ERROR: {e}")
        return False

def verify():
    """Check all tables and report counts."""
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    
    tables = [
        ("gas_compressor_stations", "Midstream"),
        ("gas_processing_plants", "Midstream"),
        ("lng_terminals", "Midstream"),
        ("eia_electricity_rates", "Pricing"),
        ("eia_natural_gas_prices", "Pricing"),
        ("eia_gas_storage_weekly", "Pricing"),
        ("fcc_fiber_availability", "Fiber"),
        ("peeringdb_ix_facilities", "Fiber"),
        ("peeringdb_network_facilities", "Fiber"),
    ]
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        total = 0
        
        for table, group in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                status = "✓" if count > 0 else "⚠"
                print(f"  {status} [{group}] {table}: {count:,}")
                total += count
            except Exception as e:
                print(f"  ✗ [{group}] {table}: {str(e)[:50]}")
                conn.rollback()
                cur = conn.cursor()
        
        conn.close()
        print(f"\n  GRAND TOTAL: {total:,} records")
        return total > 0
    except Exception as e:
        print(f"ERROR: {e}")
        return False

def main():
    print("=" * 60)
    print("DC Hub — Infrastructure Expansion")
    print(f"Time: {datetime.now().isoformat()}")
    print(f"Database: {DATABASE_URL[:40]}..." if DATABASE_URL else "NO DATABASE")
    print("=" * 60)
    
    if not DATABASE_URL:
        print("ERROR: Set DATABASE_URL or NEON_DATABASE_URL")
        sys.exit(1)
    
    # Parse args
    layer = None
    verify_only = False
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--verify":
            verify_only = True
        elif sys.argv[1] == "--layer" and len(sys.argv) > 2:
            layer = sys.argv[2]
    
    if verify_only:
        verify()
        return
    
    # Run schema first
    if not run_schema():
        print("Schema creation failed — attempting to continue anyway")
    
    # Run discovery scripts
    results = {}
    
    if layer is None or layer == "midstream":
        results["midstream"] = run_script("Midstream Gas", "midstream_discovery.py")
    
    if layer is None or layer == "pricing":
        results["pricing"] = run_script("EIA Pricing", "eia_pricing_discovery.py")
    
    if layer is None or layer == "fiber":
        results["fiber"] = run_script("Fiber/IX", "fiber_connectivity_discovery.py")
    
    # Verify
    verify()
    
    # Final status
    print("\n" + "=" * 60)
    print("STATUS")
    print("=" * 60)
    for k, v in results.items():
        status = "✓ PASS" if v else "✗ FAIL"
        print(f"  {k}: {status}")
    
    all_pass = all(results.values())
    print(f"\n  {'ALL PASSED' if all_pass else 'SOME FAILED'}")
    sys.exit(0 if all_pass else 1)

if __name__ == "__main__":
    main()
