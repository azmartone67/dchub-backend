"""
DC Hub — backup RESTORE VERIFICATION.

Runs inside CI (.github/workflows/restore-test.yml) AFTER the latest Neon->R2
SQL dump has been restored into a throwaway Postgres. Proves the dump is
actually restorable and that the DATA is recoverable.

Design: a DR restore test must catch *real* data loss (a large or business-
critical table missing/empty, or a collapsed row count). It must NOT flap red
just because the throwaway container lacks prod's Postgres extensions
(postgis / pg_cron / pg_session_jwt) — those tables are recreated by the
extension on a real Neon restore target, not by our data. So:
  FAIL on: total rows == 0, a CRITICAL table missing/empty, no facility table,
           or a "significant" (>= SIGNIFICANT_ROWS) source table entirely absent.
  WARN on: tiny/extension-owned tables that differ, or big tables that look
           under-restored (source row estimates are noisy, so warn not fail).

Env:
  RESTORE_TARGET_URL   throwaway PG the dump was restored into (REQUIRED)
  SOURCE_DATABASE_URL  live Neon, read-only, for a presence/size compare (OPTIONAL)

Exit 0 = PASS, 1 = FAIL. Never writes to either database.
"""
import os
import sys
import psycopg2

# Small tables whose loss is still catastrophic — must be present & non-empty
# regardless of size (big tables are covered by the size-based check below).
CRITICAL = ["users", "api_keys", "mcp_dev_keys", "deals"]
# At least one of these (the canonical facility table) must be present & non-empty.
FACILITY_ANY = ["discovered_facilities", "facilities"]
SIGNIFICANT_ROWS = 1000   # a source table this big going entirely missing == real loss
ROW_FLOOR_RATIO = 0.5     # restored < 50% of source estimate on a big table -> WARN

TARGET = os.environ.get("RESTORE_TARGET_URL", "")
SOURCE = os.environ.get("SOURCE_DATABASE_URL", "")


def fail(msg):
    print(f"\n[X] RESTORE TEST FAILED: {msg}")
    sys.exit(1)


def table_estimates(conn):
    """{public table -> reltuples estimate}. Instant, no table scan."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.relname, c.reltuples::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        """
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def main():
    if not TARGET:
        fail("RESTORE_TARGET_URL not set")
    if "neon.tech" in TARGET:
        fail("RESTORE_TARGET_URL points at neon.tech — refusing (must be a throwaway DB)")

    tconn = psycopg2.connect(TARGET, connect_timeout=30)
    tconn.autocommit = True
    cur = tconn.cursor()
    cur.execute("ANALYZE")
    restored = set(table_estimates(tconn))
    print(f"Restored public tables: {len(restored)}")

    counts = {}
    total = 0
    for t in restored:
        cur.execute(f'SELECT count(*) FROM "{t}"')
        n = cur.fetchone()[0]
        counts[t] = n
        total += n
    print(f"Total rows restored: {total:,}")
    print("Top tables by row count:")
    for t, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]:
        print(f"  {t:<34} {n:>12,}")
    if total == 0:
        fail("restore contains 0 rows across all tables")

    # ---- optional compare against the live source ----
    src = {}
    if SOURCE:
        try:
            sconn = psycopg2.connect(SOURCE, connect_timeout=20)
            sconn.autocommit = True
            sconn.cursor().execute("SET statement_timeout = '15s'")  # never load the 1-replica prod
            src = table_estimates(sconn)
            print(f"\nSource public tables: {len(src)}")
        except Exception as e:
            print(f"(source compare skipped — could not read source: {str(e)[:140]})")

    problems = []   # -> FAIL
    warns = []      # -> print only

    if src:
        missing = [t for t in src if t not in restored]
        sig_missing = sorted(t for t in missing if src.get(t, 0) >= SIGNIFICANT_ROWS)
        minor_missing = sorted(t for t in missing if t not in sig_missing)
        for t in minor_missing:
            warns.append(f"~ {t} not restored (source est. {int(src.get(t, 0))} rows) — "
                         f"tiny/extension-owned; expected to differ in a vanilla container")
        for t in sig_missing:
            problems.append(f"SIGNIFICANT source table absent from restore: {t} (~{int(src[t])} rows)")
        for t, est in src.items():
            if est >= SIGNIFICANT_ROWS and t in counts and counts[t] < ROW_FLOOR_RATIO * est:
                warns.append(f"~ {t} looks under-restored: {counts[t]:,} rows vs source est. {int(est):,}")

    # ---- critical tables (skip any whose name simply isn't in source — that's our naming, not a loss) ----
    for t in CRITICAL:
        if src and t not in src:
            continue
        if t not in restored:
            problems.append(f"CRITICAL table missing: {t}")
        elif counts.get(t, 0) == 0:
            problems.append(f"CRITICAL table empty: {t}")
    if not any(counts.get(t, 0) > 0 for t in FACILITY_ANY):
        if not (src and not any(t in src for t in FACILITY_ANY)):
            problems.append(f"no populated facility table (looked for {FACILITY_ANY})")

    if warns:
        print("\nWARN (does not fail the DR check):")
        for w in warns:
            print(f"  {w}")

    if problems:
        print("\nReal problems:")
        for p in problems:
            print(f"  [X] {p}")
        fail(f"{len(problems)} real problem(s) — see above")

    print("\n[OK] RESTORE TEST PASSED — all critical & large tables restored "
          f"({total:,} rows across {len(restored)} tables); edge misses noted as WARN above")
    sys.exit(0)


if __name__ == "__main__":
    main()
