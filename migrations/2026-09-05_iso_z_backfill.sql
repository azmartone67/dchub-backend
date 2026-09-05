-- Backfill: append `Z` to bare ISO timestamps stored in TEXT columns.
--
-- ★ RUN THIS AFTER THE CODE DEPLOY, NOT BEFORE. The order matters and only one
--   order is safe. If the backfill runs first, the still-deployed old code goes
--   on writing bare values and re-creates the mixed state within minutes. Deploy
--   the code that writes `Z`, then run this once to bring the history forward.
--   Readers tolerate the window: production is Python 3.13, whose
--   datetime.fromisoformat() accepts `Z`, and 75 call sites already normalise it
--   with .replace("Z", "+00:00").
--
-- ★ IDEMPOTENT BY CONSTRUCTION. The predicate is the exact BARE shape — no
--   offset, no Z. A value already ending in `Z` or `+00:00` does not match, so
--   re-running this changes nothing and cannot produce `...ZZ`.
--
-- ★ MATCHES ON VALUES, NEVER ON COLUMN NAMES. An earlier pass tried filtering
--   columns by name and hit `status`, `platform`, `category` and `raw_data`,
--   because the substring "at" appears inside each. Only values shaped exactly
--   like a bare ISO-8601 timestamp are touched.
--
-- ★ SCOPE. The 20 tables below are the ones an AST pass found receiving a bare
--   `utcnow().isoformat()` as a SQL parameter. Columns are discovered at run
--   time from information_schema, because 11 of these tables have no CREATE
--   TABLE anywhere in the repo — the database is the only authority on which of
--   their columns are TEXT.
--
-- ★ DRY RUN FIRST: scripts/audit_bare_iso_timestamps.py reports exactly which
--   columns and how many row-values this will change, read-only.

DO $$
DECLARE
  r          record;
  n          bigint;
  changed    bigint := 0;
  touched    int    := 0;
  bare_iso   text   := '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$';
BEGIN
  FOR r IN
    SELECT c.table_name, c.column_name
      FROM information_schema.columns c
      JOIN information_schema.tables t
        ON t.table_schema = c.table_schema
       AND t.table_name   = c.table_name
       AND t.table_type   = 'BASE TABLE'          -- never a view
     WHERE c.table_schema = 'public'
       AND c.data_type IN ('text', 'character varying')
       AND c.table_name IN (
         'leads','users','facilities','lead_activities','reports',
         'discovered_facilities','email_queue','api_keys','welcome_series',
         'pending_facilities','submissions','discovery_runs','partner_inquiries',
         'ai_access_log','announcements','ai_usage_tracking','tax_incentives',
         'email_tracking','user_plans','signups')
     ORDER BY c.table_name, c.column_name
  LOOP
    EXECUTE format(
      'UPDATE %I SET %I = %I || ''Z'' WHERE %I ~ %L',
      r.table_name, r.column_name, r.column_name, r.column_name, bare_iso);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
      touched := touched + 1;
      changed := changed + n;
      RAISE NOTICE 'backfilled %.% -> % row(s)', r.table_name, r.column_name, n;
    END IF;
  END LOOP;
  RAISE NOTICE 'iso-z backfill complete: % row-value(s) across % column(s)', changed, touched;
END $$;
