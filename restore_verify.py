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
import json
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
# r-qa (2026-06-27): app tables that USE a Neon-only extension (h3 / postgis /
# pg_cron / pg_session_jwt) but are NOT owned by it — so pg_depend deptype='e'
# misses them — drop from a vanilla postgres test container yet restore fine on a
# real Neon target. Treat as expected-absent (WARN, never FAIL) so the DR gate
# fails ONLY on genuinely missing app data. A multi-week false-RED (the chronic
# state this fixes) would otherwise mask a truly unrestorable backup. Extend this
# set when a restore run reports a new "SIGNIFICANT source table absent" that is
# really just extension-dependent.
NEON_EXT_DEPENDENT = {
    "fcc_fiber_hex",       # h3 hex geo index
    "eia_gas_prices",      # cascades off the h3/postgis ingest chain
    "generator_inventory",
    "planned_generators",
}

TARGET = os.environ.get("RESTORE_TARGET_URL", "")
MANIFEST_PATH = os.environ.get("BACKUP_MANIFEST", "backup_manifest.json")
SOURCE = os.environ.get("SOURCE_DATABASE_URL", "")


def fail(msg):
    print(f"\n[X] RESTORE TEST FAILED: {msg}")
    sys.exit(1)


# 2026-07-30: run 30264550927 died on the connect below with
#   FATAL: the database system is not yet accepting connections
#   DETAIL: Consistent recovery state has not been yet reached.
# That is CRASH RECOVERY, not a slow first boot — restore-test.yml already gates
# on pg_isready for 90s when the container starts. It means the throwaway server
# went DOWN mid-job (disk exhaustion or OOM during the ~17GB restore) and is
# replaying WAL, which is slow precisely because we run it with fsync=off /
# full_page_writes=off for restore speed.
#
# Waiting is correct; waiting SILENTLY is not — a restart we don't record turns a
# loud failure into a slow green. So: bounded retry, and every attempt is printed
# so the workflow's post-mortem step can attribute the delay.
_RECOVERY_STATES = (
    "not yet accepting connections",
    "consistent recovery state",
    "starting up",
    "recovery mode",
)


def _connect_when_ready(dsn, budget_seconds=600, interval=10):
    """Connect, tolerating a server still in crash recovery. Bounded, and loud.

    Only recovery states are retried — bad credentials, wrong host and refused
    connections still fail on the first attempt, exactly as before.
    """
    import time

    deadline = time.time() + budget_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            conn = psycopg2.connect(dsn, connect_timeout=30)
            if attempt > 1:
                # Never let this pass unremarked: a recovering server means the
                # restore ENVIRONMENT failed, even when the verify then succeeds.
                print(f"::warning::target Postgres was in recovery; connected on "
                      f"attempt {attempt}. The server RESTARTED mid-job — see the "
                      f"container post-mortem step for why.")
            return conn
        except psycopg2.OperationalError as e:
            msg = str(e).lower()
            if not any(s in msg for s in _RECOVERY_STATES):
                raise
            if time.time() >= deadline:
                fail(f"target Postgres still in recovery after {budget_seconds}s "
                     f"({attempt} attempts): {str(e)[:200]}")
            print(f"  target in recovery (attempt {attempt}), retrying in {interval}s ...")
            time.sleep(interval)


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


def load_manifest(path=None):
    """The dump's own table inventory, or None.

    ★2026-08-31 — THE BUG THIS CLOSES. This gate compared the restored dump
    against LIVE prod. Those are two different points in time, and the gap is
    hours: the 08-31 run restored a dump begun 09:41:50 and compared it against
    prod as read at 11:27. `gsc_daily_performance` was created and backfilled in
    between (all 55,071 rows stamped 10:15:53-10:18:38), so it was legitimately
    absent from a correct dump — and the gate called it "SIGNIFICANT source table
    absent from restore" and failed.

    Nothing was lost. The backup was fine. The comparison was wrong, and a DR
    gate that cries wolf is one nobody reads — which is exactly how a real
    unrestorable backup would slip past.

    The dump now ships an inventory of what existed when it ran. Compare against
    that and the race cannot happen: a table that did not exist cannot be missing.
    """
    path = path or MANIFEST_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"(manifest at {path} unreadable: {str(e)[:120]})")
        return None
    tables = m.get("tables")
    if not isinstance(tables, dict) or not tables:
        # An empty inventory would claim prod had no tables and excuse EVERY
        # missing table. Refuse it and fall back to the live compare.
        print(f"(manifest at {path} has no usable table inventory — ignoring)")
        return None
    return {"taken_at": m.get("taken_at") or "unknown", "tables": tables}


def classify_missing(src, restored, skip, significant_rows=SIGNIFICANT_ROWS):
    """Split source tables absent from the restore into three buckets.

    Pure: no DB, no env. `skip(name)` marks a table whose absence from a vanilla
    test container is expected (extension-owned / extension-typed / Neon-only).
    Returns (expected_absent, minor_missing, sig_missing), each name-sorted.
    """
    missing = [t for t in src if t not in restored]
    expected_absent = sorted(t for t in missing if skip(t))
    real_missing = [t for t in missing if not skip(t)]
    sig_missing = sorted(t for t in real_missing if src.get(t, 0) >= significant_rows)
    minor_missing = sorted(t for t in real_missing if t not in set(sig_missing))
    return expected_absent, minor_missing, sig_missing


def main():
    if not TARGET:
        fail("RESTORE_TARGET_URL not set")
    if "neon.tech" in TARGET:
        fail("RESTORE_TARGET_URL points at neon.tech — refusing (must be a throwaway DB)")

    tconn = _connect_when_ready(TARGET)
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
    source_ok = False
    ext_owned = set()  # tables CREATED by an extension (e.g. postgis.spatial_ref_sys)
    ext_typed = set()  # tables that merely USE an extension-provided column type
    if SOURCE:
        try:
            sconn = psycopg2.connect(SOURCE, connect_timeout=20)
            sconn.autocommit = True
            sc = sconn.cursor()
            sc.execute("SET statement_timeout = '15s'")  # never load the 1-replica prod
            src = table_estimates(sconn)
            sc.execute(
                """
                SELECT c.relname
                FROM pg_depend d
                JOIN pg_class c ON c.oid = d.objid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE d.deptype = 'e' AND n.nspname = 'public' AND c.relkind = 'r'
                """
            )
            ext_owned = {r[0] for r in sc.fetchall()}
            # Tables that merely USE an extension-provided column TYPE (postgis
            # geometry/geography, pgvector vector, h3 h3index) are NOT extension-
            # OWNED, so the pg_depend deptype='e' query above misses them — yet
            # they still cannot be created in a vanilla test container (the column
            # type does not exist there) and legitimately fail to restore. Detect
            # them dynamically so a newly added geo/vector table never re-reds this
            # DR gate (the chronic false-RED this class of table caused).
            sc.execute(
                """
                SELECT DISTINCT c.relname
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_type ty ON ty.oid = a.atttypid
                JOIN pg_depend d ON d.objid = ty.oid AND d.deptype = 'e'
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND a.attnum > 0 AND NOT a.attisdropped
                """
            )
            ext_typed = {r[0] for r in sc.fetchall()}
            source_ok = True
            print(f"\nSource public tables: {len(src)}  "
                  f"(extension-owned: {len(ext_owned)}, extension-typed: {len(ext_typed)})")
        except Exception as e:
            print(f"(source compare skipped — could not read source: {str(e)[:140]})")

    # Prefer the dump's OWN inventory over live prod. Only when the live read
    # succeeded, because the extension-owned/typed sets come from there and
    # without them every geo/vector table reads as catastrophic loss.
    manifest = load_manifest()
    if manifest and source_ok:
        live_only = sorted(set(src) - set(manifest["tables"]))
        src = manifest["tables"]
        print(f"Comparing against the DUMP MANIFEST (taken_at={manifest['taken_at']}, "
              f"{len(src)} tables) rather than live prod — a table created after the\n"
              f"dump is not data loss.")
        if live_only:
            print("  tables that appeared in prod AFTER this dump (correctly not in it): "
                  + ", ".join(live_only[:12]) + (" ..." if len(live_only) > 12 else ""))
    elif manifest and not source_ok:
        print("(manifest present but the live source was unreadable — skipping the "
              "absent-table compare entirely rather than failing every extension-typed table)")
    elif src:
        print("::warning::no manifest for this dump — comparing against LIVE prod, which "
              "has moved on since the dump. A table created in between will read as loss.")

    problems = []   # -> FAIL
    warns = []      # -> print only

    if src:
        # Extension-owned tables (postgis spatial_ref_sys, etc.) AND extension-typed
        # tables (geometry/vector columns) are recreated by CREATE EXTENSION on a
        # real Neon target — their absence in a vanilla container is expected, never
        # data loss, regardless of row count.
        skip = lambda t: t in ext_owned or t in ext_typed or t in NEON_EXT_DEPENDENT
        expected_absent, minor_missing, sig_missing = classify_missing(src, restored, skip)
        if expected_absent:
            print("INFO — extension-owned/typed tables absent from the vanilla test "
                  "container (restored by CREATE EXTENSION on a real Neon target):")
            for t in expected_absent:
                print(f"  . {t}  (source est. {int(src.get(t, 0))} rows)")
        for t in minor_missing:
            warns.append(f"~ {t} not restored (source est. {int(src.get(t, 0))} rows) — "
                         f"tiny or depends on a Neon-only extension; restores fine on Neon")
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
