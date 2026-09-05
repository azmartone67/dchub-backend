"""enterprise_inquiries — ONE schema, two writers.

r-inquiry-schema-fork (2026-09-05). Two modules landed on 2026-06-30 in the
same commit, each with its own `CREATE TABLE IF NOT EXISTS
enterprise_inquiries`, and the two definitions were INCOMPATIBLE:

    routes/enterprise.py          id, created_at, org_name NOT NULL,
                                  email NOT NULL, use_case NOT NULL,
                                  expected_volume NOT NULL, source_ip,
                                  user_agent, relay_status
                                  — and NO `status` column at all

    routes/enterprise_inquiry.py  id, created_at, tier_requested, name,
                                  email, firm, use_case, notes, source,
                                  ip_hash, status NOT NULL, contacted_at,
                                  notes_admin
                                  — and NO org_name / expected_volume

The two INSERTs overlap on `email` and `use_case` and nothing else.

★ IF NOT EXISTS IS WHY THIS IS SILENT. Whichever endpoint took the first POST
created the table; from then on the other module's DDL is a no-op that reports
success, and its INSERT names columns that do not exist. Both are live
lead-capture paths on a revenue surface, so one of:

    POST /api/v1/enterprise/contact   (routes/enterprise.py)
    POST /api/v1/enterprise/inquiry   (routes/enterprise_inquiry.py)

has been failing to store since June, and which one depends on traffic order
in June rather than on anything in the code.

★ THE FIX DOES NOT NEED TO KNOW WHICH ONE WON. Guessing would be a migration
with a coin flip in it. Instead this module is the UNION of both definitions
plus an idempotent heal — ADD COLUMN IF NOT EXISTS for every column, and DROP
NOT NULL on the four columns the *other* writer never supplies (a surviving
`org_name TEXT NOT NULL` would reject every enterprise_inquiry.py insert even
after the column exists). Applied to either historical table, the result is
the same table, and applied twice it is a no-op.

Deliberately NOT here:
  * no SET NOT NULL on `status`. An existing row from the enterprise.py-shaped
    table has no status, so the column arrives NULL-filled; the backfill below
    sets those to 'new', but promoting the constraint would fail the whole
    heal if any row slipped in between. Readers use
    util.status_taxonomy.status_histogram, which tolerates the NULL — that
    helper exists because a NULL status key crashed jsonify on
    /api/v1/markets/<id> the same day (r-status-null-key).
  * no DROP COLUMN, ever. Both column sets carry real submissions.
"""

#: Every column either writer inserts, in one definition.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS enterprise_inquiries (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- routes/enterprise_inquiry.py
    tier_requested  TEXT,
    name            TEXT,
    email           TEXT,
    firm            TEXT,
    use_case        TEXT,
    notes           TEXT,
    source          TEXT,
    ip_hash         TEXT,
    status          TEXT DEFAULT 'new',
    contacted_at    TIMESTAMPTZ,
    notes_admin     TEXT,
    -- routes/enterprise.py
    org_name        TEXT,
    expected_volume TEXT,
    source_ip       TEXT,
    user_agent      TEXT,
    relay_status    TEXT
);
"""

#: Heals a table created by EITHER historical definition. Every statement is
#: idempotent, so this is safe to run on every request path that touches the
#: table, and safe to run twice.
HEAL_SQL = """
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS tier_requested  TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS name            TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS email           TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS firm            TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS use_case        TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS notes           TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS source          TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS ip_hash         TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS status          TEXT DEFAULT 'new';
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS contacted_at    TIMESTAMPTZ;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS notes_admin     TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS org_name        TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS expected_volume TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS source_ip       TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS user_agent      TEXT;
ALTER TABLE enterprise_inquiries ADD COLUMN IF NOT EXISTS relay_status    TEXT;

-- The other writer never supplies these, so a surviving NOT NULL from the
-- enterprise.py-shaped table rejects every enterprise_inquiry.py insert even
-- once the columns exist. DROP NOT NULL is idempotent in Postgres.
ALTER TABLE enterprise_inquiries ALTER COLUMN org_name        DROP NOT NULL;
ALTER TABLE enterprise_inquiries ALTER COLUMN expected_volume DROP NOT NULL;
ALTER TABLE enterprise_inquiries ALTER COLUMN email           DROP NOT NULL;
ALTER TABLE enterprise_inquiries ALTER COLUMN use_case        DROP NOT NULL;

UPDATE enterprise_inquiries SET status = 'new' WHERE status IS NULL;
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS enterprise_inquiries_status_idx
  ON enterprise_inquiries (status, created_at DESC);
CREATE INDEX IF NOT EXISTS enterprise_inquiries_email_idx
  ON enterprise_inquiries (email);
CREATE INDEX IF NOT EXISTS enterprise_inquiries_created_idx
  ON enterprise_inquiries (created_at DESC);
"""


#: Once per process. Both callers invoke this from a REQUEST path, and the
#: heal is DDL: ALTER ... DROP NOT NULL takes an ACCESS EXCLUSIVE lock, and
#: the status backfill scans the table. Re-running that on every POST would
#: put a lock and a scan in front of a lead submission for no gain — the heal
#: is idempotent, so the second run has nothing to do. The CREATE was already
#: per-request before this change; it now shares the same latch.
_ENSURED = False


def ensure_enterprise_inquiries(cur, force=False):
    """Create-or-heal the table. `cur` is a live cursor; caller owns the conn.

    Runs at most once per process unless `force` is set (tests, and any
    caller that genuinely needs to re-assert the schema).
    """
    global _ENSURED
    if _ENSURED and not force:
        return False
    cur.execute(SCHEMA_SQL)
    cur.execute(HEAL_SQL)
    cur.execute(INDEX_SQL)
    _ENSURED = True
    return True


def declared_columns():
    """Column names SCHEMA_SQL declares — the set both writers must fit in."""
    import re
    body = SCHEMA_SQL[SCHEMA_SQL.index("(") + 1: SCHEMA_SQL.rindex(")")]
    cols = []
    for raw in body.split("\n"):
        line = raw.split("--")[0].strip().rstrip(",")
        if not line:
            continue
        m = re.match(r"^([a-z_][a-z0-9_]*)\s+", line)
        if m:
            cols.append(m.group(1))
    return set(cols)
