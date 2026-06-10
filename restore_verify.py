"""
DC Hub — backup RESTORE VERIFICATION.

Runs inside CI (.github/workflows/restore-test.yml) AFTER the latest Neon->R2
SQL dump has been restored into a throwaway Postgres. Proves the dump is
actually restorable and populated — an untested backup is not a backup.

Env:
  RESTORE_TARGET_URL   throwaway PG the dump was restored into (REQUIRED)
  SOURCE_DATABASE_URL  live Neon, read-only, for a table-presence sanity
                       compare (OPTIONAL — gracefully skipped if unreachable)

Exit 0 = PASS, 1 = FAIL. Never writes to either database.
"""
import os
import sys
import psycopg2

MIN_TABLES = 5  # a healthy restore has many public tables; < this == broken dump
TARGET = os.environ.get("RESTORE_TARGET_URL", "")
SOURCE = os.environ.get("SOURCE_DATABASE_URL", "")


def fail(msg):
    print(f"\n[X] RESTORE TEST FAILED: {msg}")
    sys.exit(1)


def public_tables(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname
        """
    )
    return [r[0] for r in cur.fetchall()]


def main():
    if not TARGET:
        fail("RESTORE_TARGET_URL not set")
    # Safety rail: NEVER let restore-verify (or a copy-paste of it) touch prod.
    if "neon.tech" in TARGET:
        fail("RESTORE_TARGET_URL points at neon.tech — refusing (must be a throwaway DB)")

    tconn = psycopg2.connect(TARGET, connect_timeout=30)
    tconn.autocommit = True
    cur = tconn.cursor()
    cur.execute("ANALYZE")  # freshen planner stats before we read
    tables = public_tables(tconn)
    print(f"Restored public tables: {len(tables)}")
    if len(tables) < MIN_TABLES:
        fail(f"only {len(tables)} tables restored (expected >= {MIN_TABLES})")

    counts = {}
    total = 0
    for t in tables:
        cur.execute(f'SELECT count(*) FROM "{t}"')
        n = cur.fetchone()[0]
        counts[t] = n
        total += n
    print(f"Total rows restored: {total:,}")
    print("Top tables by row count:")
    for t, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]:
        print(f"  {t:<34} {n:>12,}")

    # Optional: every table that exists in the live source must exist in the restore.
    if SOURCE:
        try:
            sconn = psycopg2.connect(SOURCE, connect_timeout=20)
            sconn.autocommit = True
            sc = sconn.cursor()
            sc.execute("SET statement_timeout = '15s'")  # never load the 1-replica prod
            src_tables = set(public_tables(sconn))
            missing = sorted(src_tables - set(tables))
            print(f"\nSource public tables: {len(src_tables)}")
            if missing:
                print("Tables present in SOURCE but MISSING from restore:")
                for t in missing:
                    print(f"  - {t}")
                fail(f"{len(missing)} source table(s) not present in restore")
            print("[ok] every source table is present in the restore")
        except Exception as e:
            print(f"(source compare skipped — could not read source: {str(e)[:140]})")

    if total == 0:
        fail("restore contains 0 rows across all tables")

    print("\n[OK] RESTORE TEST PASSED — backup is restorable and populated")
    sys.exit(0)


if __name__ == "__main__":
    main()
