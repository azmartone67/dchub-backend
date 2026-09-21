-- Exact-location allowance meter (2026-09-21).
--
-- Policy (owner-approved 2026-09-21): an anonymous caller never gets a
-- facility's exact location. A free/identified account — and the Starter plan —
-- gets exact coordinates and street address for up to
-- FREE_EXACT_LOCATIONS_PER_MONTH (default 10) DISTINCT facilities per UTC
-- calendar month; re-viewing one already revealed that month is free.
-- Developer and above are exact everywhere and never touch this table.
--
-- ONE meter across the website, the REST API and MCP: every surface writes
-- through util/location_meter.consume(), keyed on the same two identities —
--
--   account       the account's email, lower-cased; else 'key:' || the first
--                 16 hex of sha256(api key). Never the key itself.
--   facility_key  the facility's frozen canonical_slug (the /facilities/<slug>
--                 identity), so a reveal on the page is free over the API.
--   period        'YYYY-MM', UTC.
--
-- ★ The PRIMARY KEY is the "distinct facilities" rule and the re-view rule at
-- once: a second reveal of the same facility in the same month conflicts and
-- costs nothing. The monthly LIMIT is enforced by consume()'s statement, which
-- serialises each (account, period) on a transaction-scoped advisory lock
-- before counting — see the util module for why a bare INSERT ... WHERE
-- count < limit is not enough under READ COMMITTED.
--
-- ★ Apply by hand, with psql, exactly once per database (it is idempotent):
--       psql "$DATABASE_URL" -1 -v ON_ERROR_STOP=1 \
--            -f migrations/2026-09-21_facility_location_reveals.sql
-- NOT at runtime: db_utils cursors silently drop CREATE TABLE (SKIP_DDL). Until
-- this is applied the meter fails CLOSED — no allowance is granted, nothing
-- 500s — so shipping the code first is safe, and useless until this runs.
--
-- `account` holds email addresses. It is read only by the meter; prune old
-- periods if retention matters (nothing reads a past month).

CREATE TABLE IF NOT EXISTS facility_location_reveals (
    account            TEXT        NOT NULL,
    facility_key       TEXT        NOT NULL,
    period             TEXT        NOT NULL
                       CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    channel            TEXT        NOT NULL
                       CHECK (channel IN ('web', 'api', 'mcp')),
    first_revealed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account, facility_key, period)
);

COMMENT ON TABLE facility_location_reveals IS
    'Exact-location allowance meter: one row per account, facility and UTC month '
    '(util/location_meter.py). Rows are the charge; the PK makes a re-view free.';
