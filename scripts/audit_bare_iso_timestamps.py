#!/usr/bin/env python3
"""Report TEXT columns holding bare ISO timestamps — the backfill's dry run.

★ WHY THIS EXISTS AND WHY IT IS READ-ONLY. The utcnow retirement makes the
serialized wire shape `...Z`. For columns typed timestamp/timestamptz that is a
no-op: Postgres parses the Z and stores the same instant. For columns typed
TEXT it is not — new rows get `2026-09-05T04:24:58.917155Z` while every row
written before the deploy keeps `2026-09-05T04:24:58.917155`. One column, two
formats, and nothing in CI can see it because CI never reads production rows.

★ IT CANNOT BE ANSWERED FROM THE REPO. 11 of the 20 tables that receive one of
these timestamps have no CREATE TABLE anywhere in the source (users,
lead_activities, reports, welcome_series, pending_facilities, submissions,
ai_access_log, announcements, tax_incentives, email_tracking, user_plans). The
authoritative schema is the database. So this asks the database.

★ IT MATCHES ON VALUES, NOT ON COLUMN NAMES. An earlier attempt filtered
columns by name and matched `status`, `platform`, `category` and `raw_data`,
because the substring "at" appears inside all of them. The predicate below is
the exact bare-ISO shape and nothing else.

Usage:
    DATABASE_URL=... python3 scripts/audit_bare_iso_timestamps.py
    DATABASE_URL=... python3 scripts/audit_bare_iso_timestamps.py --all-tables
"""
import os
import sys

# Bare ISO-8601: no offset, no Z. Anything already carrying `Z` or `+00:00`
# fails this and is therefore never counted and never updated.
BARE_ISO = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$'

# Tables observed (by AST) to receive a bare `utcnow().isoformat()` as a SQL
# parameter. --all-tables widens to every text column in the public schema.
TABLES = [
    'leads', 'users', 'facilities', 'lead_activities', 'reports',
    'discovered_facilities', 'email_queue', 'api_keys', 'welcome_series',
    'pending_facilities', 'submissions', 'discovery_runs', 'partner_inquiries',
    'ai_access_log', 'announcements', 'ai_usage_tracking', 'tax_incentives',
    'email_tracking', 'user_plans', 'signups',
]


def main(argv):
    dsn = os.environ.get('DATABASE_URL') or os.environ.get('NEON_REPLICA_URL')
    if not dsn:
        print('DATABASE_URL (or NEON_REPLICA_URL) is required', file=sys.stderr)
        return 2
    import psycopg2  # imported late so --help works without the driver

    all_tables = '--all-tables' in argv
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()

    cur.execute("""
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND data_type IN ('text', 'character varying')
         ORDER BY table_name, column_name
    """)
    cols = [(t, c) for t, c in cur.fetchall() if all_tables or t in TABLES]

    print(f'{"table":<26} {"column":<24} {"bare":>8} {"total":>9}')
    print('-' * 72)
    total_bare = 0
    hits = []
    for t, c in cols:
        try:
            cur.execute(
                f'SELECT count(*) FILTER (WHERE "{c}" ~ %s), count("{c}") FROM "{t}"',
                (BARE_ISO,))
            bare, tot = cur.fetchone()
        except Exception as e:            # a view, a permission, an odd type
            conn.rollback()
            continue
        if bare:
            hits.append((t, c, bare, tot))
            total_bare += bare
            print(f'{t:<26} {c:<24} {bare:>8} {tot:>9}')
    print('-' * 72)
    print(f'{len(hits)} column(s) hold bare ISO timestamps; {total_bare} row-values would change.')
    if not hits:
        print('Nothing to backfill.')
    else:
        print('\nRun migrations/2026-09-05_iso_z_backfill.sql to append Z to exactly these.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
