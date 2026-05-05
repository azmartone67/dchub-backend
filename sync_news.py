#!/usr/bin/env python3
"""Check news table schemas and sync articles from Replit PG to Neon"""
import psycopg2
import os
import sys

neon_url = os.environ.get('NEON_DATABASE_URL', '')
replit_url = os.environ.get('DATABASE_URL', '')

if not neon_url:
    print("ERROR: NEON_DATABASE_URL not set")
    sys.exit(1)

# If DATABASE_URL was overridden to Neon, try the Replit PG directly
# Replit's internal PG is usually on localhost or REPLIT_DB_URL
if 'neon.tech' in replit_url:
    # DATABASE_URL already points to Neon (our override worked)
    # Try to find Replit's original PG
    replit_url = os.environ.get('REPLIT_DB_URL', '')
    if not replit_url:
        # Try pghost-based connection
        pghost = os.environ.get('PGHOST', '')
        pguser = os.environ.get('PGUSER', '')
        pgpass = os.environ.get('PGPASSWORD', '')
        pgdb = os.environ.get('PGDATABASE', '')
        if pghost:
            replit_url = f"postgresql://{pguser}:{pgpass}@{pghost}/{pgdb}"
        else:
            print("Cannot find Replit PG connection. Checking if news exists in SQLite instead...")
            
            # Check SQLite dchub.db
            import sqlite3
            for dbpath in ['dchub.db', 'dc_nexus.db', '/home/runner/workspace/dchub.db', '/home/runner/workspace/dc_nexus.db']:
                if os.path.exists(dbpath):
                    try:
                        sconn = sqlite3.connect(dbpath)
                        scur = sconn.cursor()
                        scur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%news%'")
                        tables = [r[0] for r in scur.fetchall()]
                        if tables:
                            for t in tables:
                                scur.execute(f"SELECT count(*) FROM {t}")
                                count = scur.fetchone()[0]
                                print(f"  SQLite {dbpath} → {t}: {count} rows")
                        sconn.close()
                    except Exception as e:
                        print(f"  SQLite {dbpath}: {e}")

print("=" * 60)
print("STEP 1: Check schemas")
print("=" * 60)

# Connect to Neon
neon = psycopg2.connect(neon_url, connect_timeout=15)
nc = neon.cursor()

nc.execute("""SELECT column_name, data_type FROM information_schema.columns 
    WHERE table_name='news_articles' ORDER BY ordinal_position""")
neon_cols = nc.fetchall()
print(f"\nNeon news_articles columns ({len(neon_cols)}):")
for col, dtype in neon_cols:
    print(f"  {col}: {dtype}")

nc.execute("SELECT count(*) FROM news_articles")
print(f"\nNeon news count: {nc.fetchone()[0]}")

# Try to connect to Replit PG
rep = None
rep_count = 0
rep_cols = []
if replit_url and 'neon.tech' not in replit_url:
    try:
        rep = psycopg2.connect(replit_url, connect_timeout=10)
        rc = rep.cursor()
        rc.execute("""SELECT column_name, data_type FROM information_schema.columns 
            WHERE table_name='news_articles' ORDER BY ordinal_position""")
        rep_cols = rc.fetchall()
        print(f"\nReplit PG news_articles columns ({len(rep_cols)}):")
        for col, dtype in rep_cols:
            print(f"  {col}: {dtype}")
        
        rc.execute("SELECT count(*) FROM news_articles")
        rep_count = rc.fetchone()[0]
        print(f"\nReplit PG news count: {rep_count}")
    except Exception as e:
        print(f"\nReplit PG connection failed: {e}")

# Also check SQLite for news
print("\n" + "=" * 60)
print("STEP 2: Check SQLite databases for news")
print("=" * 60)

sqlite_news_path = None
sqlite_news_table = None
sqlite_news_count = 0

import sqlite3
for dbpath in ['dchub.db', 'dc_nexus.db', '/home/runner/workspace/dchub.db', '/home/runner/workspace/dc_nexus.db']:
    if os.path.exists(dbpath):
        try:
            sconn = sqlite3.connect(dbpath)
            scur = sconn.cursor()
            scur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            all_tables = [r[0] for r in scur.fetchall()]
            news_tables = [t for t in all_tables if 'news' in t.lower() or 'article' in t.lower() or 'announcement' in t.lower()]
            
            if news_tables:
                print(f"\n  {dbpath}:")
                for t in news_tables:
                    scur.execute(f"SELECT count(*) FROM [{t}]")
                    count = scur.fetchone()[0]
                    print(f"    {t}: {count} rows")
                    if count > sqlite_news_count:
                        sqlite_news_count = count
                        sqlite_news_path = dbpath
                        sqlite_news_table = t
                        
                        # Show columns
                        scur.execute(f"PRAGMA table_info([{t}])")
                        cols = scur.fetchall()
                        print(f"    Columns: {[c[1] for c in cols]}")
            sconn.close()
        except Exception as e:
            print(f"  {dbpath}: error - {e}")

# ============================================================
# STEP 3: SYNC
# ============================================================
print("\n" + "=" * 60)
print("STEP 3: Sync news to Neon")
print("=" * 60)

if '--sync' not in sys.argv:
    print("\nDry run. Run with --sync to actually sync.")
    if rep_count > 0:
        print(f"  Source: Replit PG ({rep_count} articles)")
    elif sqlite_news_count > 0:
        print(f"  Source: SQLite {sqlite_news_path} → {sqlite_news_table} ({sqlite_news_count} articles)")
    else:
        print("  No news source found!")
    sys.exit(0)

# Determine source
source = None
if rep_count > 0 and rep:
    source = 'replit_pg'
    print(f"\nSyncing {rep_count} articles from Replit PG → Neon...")
elif sqlite_news_count > 0:
    source = 'sqlite'
    print(f"\nSyncing {sqlite_news_count} articles from SQLite ({sqlite_news_path}/{sqlite_news_table}) → Neon...")
else:
    print("\nNo news source available to sync from!")
    sys.exit(1)

if source == 'replit_pg':
    rc = rep.cursor()
    
    # Get Replit columns
    rc.execute("""SELECT column_name FROM information_schema.columns 
        WHERE table_name='news_articles' ORDER BY ordinal_position""")
    src_cols = [r[0] for r in rc.fetchall()]
    
    # Get Neon columns
    nc.execute("""SELECT column_name FROM information_schema.columns 
        WHERE table_name='news_articles' ORDER BY ordinal_position""")
    dst_cols = [r[0] for r in nc.fetchall()]
    
    # Find common columns
    common = [c for c in src_cols if c in dst_cols]
    print(f"  Common columns: {len(common)} of {len(src_cols)}")
    
    if not common:
        print("  ERROR: No common columns! Schemas are completely different.")
        # Drop and recreate Neon table to match Replit
        print("  Recreating Neon table to match Replit schema...")
        rc.execute("""SELECT column_name, data_type, character_maximum_length 
            FROM information_schema.columns 
            WHERE table_name='news_articles' ORDER BY ordinal_position""")
        schema = rc.fetchall()
        
        nc.execute("DROP TABLE IF EXISTS news_articles CASCADE")
        
        col_defs = []
        for col_name, data_type, max_len in schema:
            if data_type == 'character varying':
                col_defs.append(f'"{col_name}" VARCHAR({max_len or 1000})')
            elif data_type == 'text':
                col_defs.append(f'"{col_name}" TEXT')
            elif data_type == 'integer':
                col_defs.append(f'"{col_name}" INTEGER')
            elif data_type == 'boolean':
                col_defs.append(f'"{col_name}" BOOLEAN')
            elif 'timestamp' in data_type:
                col_defs.append(f'"{col_name}" TIMESTAMP')
            elif data_type == 'double precision' or data_type == 'real':
                col_defs.append(f'"{col_name}" DOUBLE PRECISION')
            else:
                col_defs.append(f'"{col_name}" TEXT')
        
        create_sql = f"CREATE TABLE news_articles ({', '.join(col_defs)})"
        nc.execute(create_sql)
        neon.commit()
        print(f"  ✅ Recreated news_articles with {len(col_defs)} columns")
        common = src_cols  # Now all columns match
    
    # Do the sync
    col_list = ', '.join([f'"{c}"' for c in common])
    placeholders = ', '.join(['%s'] * len(common))
    
    rc.execute(f'SELECT {col_list} FROM news_articles')
    rows = rc.fetchall()
    
    synced = 0
    errors = 0
    for row in rows:
        try:
            nc.execute(f'INSERT INTO news_articles ({col_list}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING', row)
            synced += 1
        except Exception as e:
            errors += 1
            neon.rollback()
            if errors <= 3:
                print(f"  Error: {e}")
    
    neon.commit()
    print(f"\n  ✅ Synced {synced} articles ({errors} errors)")

elif source == 'sqlite':
    sconn = sqlite3.connect(sqlite_news_path)
    # sqlite3.Row removed - PostgreSQL uses RealDictCursor or dict(row)
    scur = sconn.cursor()
    
    # Get SQLite columns
    scur.execute(f"PRAGMA table_info([{sqlite_news_table}])")
    src_cols = [r[1] for r in scur.fetchall()]
    
    # Get Neon columns
    nc.execute("""SELECT column_name FROM information_schema.columns 
        WHERE table_name='news_articles' ORDER BY ordinal_position""")
    dst_cols = [r[0] for r in nc.fetchall()]
    
    common = [c for c in src_cols if c in dst_cols]
    print(f"  Common columns: {len(common)} of {len(src_cols)}")
    
    if not common or len(common) < 3:
        # Recreate table from SQLite schema
        print("  Schema mismatch — recreating Neon table from SQLite schema...")
        nc.execute("DROP TABLE IF EXISTS news_articles CASCADE")
        
        scur.execute(f"PRAGMA table_info([{sqlite_news_table}])")
        schema = scur.fetchall()
        
        col_defs = []
        for col in schema:
            name = col[1]
            dtype = col[2].upper() if col[2] else 'TEXT'
            if 'INT' in dtype:
                col_defs.append(f'"{name}" INTEGER')
            elif 'REAL' in dtype or 'FLOAT' in dtype or 'DOUBLE' in dtype:
                col_defs.append(f'"{name}" DOUBLE PRECISION')
            elif 'BOOL' in dtype:
                col_defs.append(f'"{name}" BOOLEAN')
            elif 'TIMESTAMP' in dtype or 'DATE' in dtype:
                col_defs.append(f'"{name}" TIMESTAMP')
            else:
                col_defs.append(f'"{name}" TEXT')
        
        create_sql = f"CREATE TABLE news_articles ({', '.join(col_defs)})"
        nc.execute(create_sql)
        neon.commit()
        print(f"  ✅ Recreated news_articles with {len(col_defs)} columns")
        common = src_cols
    
    # Sync
    col_list = ', '.join([f'"{c}"' for c in common])
    placeholders = ', '.join(['%s'] * len(common))
    
    scur.execute(f'SELECT {",".join(common)} FROM [{sqlite_news_table}]')
    rows = scur.fetchall()
    
    synced = 0
    errors = 0
    for row in rows:
        try:
            nc.execute(f'INSERT INTO news_articles ({col_list}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING', tuple(row))
            synced += 1
        except Exception as e:
            errors += 1
            neon.rollback()
            if errors <= 3:
                print(f"  Error: {e}")
    
    neon.commit()
    print(f"\n  ✅ Synced {synced} articles ({errors} errors)")
    sconn.close()

# Verify
nc.execute("SELECT count(*) FROM news_articles")
final = nc.fetchone()[0]
print(f"\n  Neon news_articles final count: {final}")

neon.close()
if rep:
    rep.close()

print("\nDone! Restart the app or wait for the news page to refresh.")
