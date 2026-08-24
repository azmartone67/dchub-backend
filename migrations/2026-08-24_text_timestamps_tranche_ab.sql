-- 2026-08-24 — TEXT -> timestamptz, tranches A and B.
--
-- 178 TEXT columns across 115 tables hold ISO-8601 timestamps. Re-measured
-- against production on 2026-08-24 — still exactly 178 columns / 115 tables:
--
--   SELECT count(*), count(DISTINCT table_name)
--     FROM information_schema.columns
--    WHERE data_type='text' AND table_schema='public'
--      AND (column_name LIKE '%\_at' ESCAPE '\'
--        OR column_name LIKE '%\_date' ESCAPE '\'
--        OR column_name='timestamp');
--
-- This is not cosmetic. It breaks production today:
--
--   GET /api/v1/media/diagnose
--     "news": "operator does not exist: text > timestamp with time zone
--              LINE 1: ...COUNT(*) FROM news_articles WHERE published_at > NOW()..."
--
-- The endpoint returns an ERROR STRING where a count belongs, so media's
-- news-freshness number is not low, it is ABSENT — and it has been reading as
-- a missing feature rather than a broken query. news_articles is in tranche B
-- below precisely because converting it un-breaks that endpoint.
--
-- ============================================================================
-- ★ RUN PR #3128 FIRST. THIS IS A HARD ORDERING, NOT A PREFERENCE.
-- ============================================================================
-- There are THREE reader styles in the codebase, and they do NOT agree:
--
--     col::timestamptz > NOW() - INTERVAL ...   -> becomes a no-op cast. safe.
--     col LIKE 'YYYY-MM-DD%'                    -> BREAKS. you cannot LIKE a
--                                                  timestamptz.
--     NULLIF(col,'')::timestamptz               -> BREAKS TOO.
--     COALESCE(col,'') != ''  /  col <> ''      -> BREAKS TOO.
--     col ~ '^[0-9]{4}-'                        -> BREAKS TOO.
--
-- ★★ THAT THIRD GROUP WAS THE SURPRISE. It looks like the "safe" cast, and
-- `NULLIF(last_used_at,'')::timestamptz` was actually RECOMMENDED as the way
-- to read api_keys while it is TEXT. It is not safe. The empty string is why:
-- these columns store '' rather than NULL, so every reader that copes with
-- that does so by NAMING '' — and '' is not a timestamptz. Verified live
-- 2026-08-24 against press_releases.published_at, already timestamptz:
--
--     published_at <> ''                    -> invalid input syntax for type
--     COALESCE(published_at,'') != ''          timestamp with time zone: ""
--     NULLIF(published_at,'')::timestamptz
--     published_at ~ '^[0-9]{4}'            -> operator does not exist
--
-- PR #3128 converted BOTH classes: 14 prefix-LIKE readers to half-open ranges
-- (`col >= day AND col < day + 1`) and 6 empty-string guards to `col::text`
-- (`NULLIF(col::text,'')`). Both forms are correct against BOTH types, so the
-- readers do not need editing again when a later tranche lands. If that PR is
-- not merged AND DEPLOYED, this migration takes out the key-retention
-- instrument and both news lanes, and silently stops the publisher counting
-- today's posts. Confirm before running:
--
--   git log --oneline origin/main | grep 'half-open'
--
-- ============================================================================
-- ★★ THE USING CLAUSE, AND WHY IT IS NOT JUST `::timestamptz`
-- ============================================================================
-- Two hazards, both measured on the live data 2026-08-24:
--
-- (1) BLANKS, NOT NULLS. These columns store '' rather than NULL, and
--     ''::timestamptz raises. Counts of blank rows:
--         announcements.published_at          15,047 of 15,047   (100%)
--         news_articles.created_at            13,159 of 13,159   (100%)
--         construction_permits.discovered_at     915 of 915      (100%)
--         construction_permits.issued_date       915 of 915      (100%)
--         social_media_posts.approved_at       2,462 of 2,466    (99.8%)
--         api_keys.expires_at                    152 of 154
--         api_keys.last_reset_date               136 of 154
--         api_keys.last_used_at                  106 of 154
--
-- (2) MIXED TIMEZONE AWARENESS IN THE SAME COLUMN. Some values carry an
--     offset ('...+00') or a Z, most are naive ('2026-08-24T06:42:40').
--     Measured, non-blank rows carrying an explicit offset:
--         api_keys.created_at                86 of 154      <- mixed!
--         api_keys.last_used_at              19 of 48       <- mixed!
--         api_keys.last_reset_date           18 of 18
--         construction_permits.created_at   915 of 915
--         social_media_posts.approved_at      4 of 4
--         news_articles.published_at          0 of 13,159
--         news_articles.fetched_at            0 of 13,159
--
--     A bare `col::timestamptz` interprets a NAIVE value in the SESSION's
--     TimeZone. This server is currently `GMT` (SHOW TimeZone, 2026-08-24), so
--     a bare cast happens to be right TODAY — and would silently shift every
--     naive value by the offset if that setting ever changed, or if the
--     migration were run from a psql session with a different TimeZone. These
--     values are all UTC (they are written by datetime.utcnow()), so the
--     CASE below says so EXPLICITLY rather than inheriting it.
--
-- The expression was validated read-only against every row of all 11 columns
-- before being written here: it raised on none of them (so the ALTER cannot
-- abort part-way through a rewrite), and every converted value landed inside
-- [2000-01-01, now + 2 years]. Round-trip spot checks:
--     '2026-08-19T19:37:38.644950'    -> 2026-08-19 19:37:38.644950+00
--     '2026-07-28 06:40:36.049322+00' -> 2026-07-28 06:40:36.049322+00
--     '2026-08-24T06:42:40'           -> 2026-08-24 06:42:40+00
--
-- Zero non-ISO values were found in any of these columns, so NO data cleaning
-- is required — the ALTER is mechanically safe.
--
-- ============================================================================
-- ★★ ONE VISIBLE CONSEQUENCE: the news API gains a UTC offset
-- ============================================================================
-- psycopg2 returns a TEXT column as `str` and a timestamptz as a tz-aware
-- `datetime`. routes/deals_routes.py already calls .isoformat() on it, so
-- nothing breaks — but the rendered value changes:
--
--     before   "published_at": "2026-08-24T06:42:40"
--     after    "published_at": "2026-08-24T06:42:40+00:00"
--
-- on /api/news/live, /api/news-feed, /api/v1/news and /api/agent/news.
--
-- This is a FIX, not a regression: JavaScript `new Date("2026-08-24T06:42:40")`
-- parses a naive string as LOCAL time, so every non-UTC reader has been seeing
-- news timestamps shifted by their own offset. The `+00:00` form says what the
-- value always meant. Flagged and accepted by the operator before the run.
--
-- ============================================================================
-- ★★★ NOT IN THIS FILE: agent_requests.timestamp
-- ============================================================================
-- 19,109,917 rows (a plain COUNT(*) times out; a filtered count got through).
-- `ALTER TABLE ... TYPE` rewrites the whole table under ACCESS EXCLUSIVE — on
-- 19M rows that is an outage, not a migration. It needs the add-column /
-- backfill-in-batches / swap pattern and belongs in its own change.
--
-- Do not let "178 columns" suggest 178 equal units of work. One column is most
-- of the risk.
--
-- ALSO NOTE: pg_stat_user_tables.n_live_tup is badly stale on this database —
-- it reported news_articles at 260 rows against an actual 13,159, and
-- agent_requests at 703,074 against 19.1M. Use exact counts (with a raised
-- statement_timeout) for anything you plan around.
--
-- ============================================================================
-- HOW TO RUN
-- ============================================================================
-- Apply tranche A, verify, THEN apply tranche B. Each ALTER takes an ACCESS
-- EXCLUSIVE lock for the length of its table rewrite; the largest table here is
-- news_articles at 13,159 rows, so each is sub-second, but they are still
-- writes to a live database. Wrap each tranche in its own transaction so a
-- surprise rolls the whole tranche back rather than leaving a half-converted
-- table.
--
-- Indexes are rebuilt automatically by the ALTER. The ones on target columns:
--     news_articles.idx_news_articles_published  (published_at DESC)
--     news_articles.idx_news_published           (published_at DESC)  <- dupe
--     news_articles.idx_news_fetched             (fetched_at DESC)
-- (idx_news_articles_published and idx_news_published are DUPLICATES of each
-- other on the same column. Not this migration's job, but worth dropping one.)

SET statement_timeout = '600s';
SET lock_timeout = '30s';    -- fail fast rather than queue behind a long read


-- ============================================================================
-- TRANCHE A — columns that are 100% (or 99.8%) blank.
-- Zero risk: there is essentially nothing to convert. This exists to prove the
-- pipeline end to end before touching a column with real data in it.
-- ============================================================================
BEGIN;

ALTER TABLE announcements
  ALTER COLUMN published_at TYPE timestamptz
  USING CASE
          WHEN NULLIF(btrim(published_at), '') IS NULL THEN NULL
          WHEN btrim(published_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
            THEN btrim(published_at)::timestamptz
          ELSE (btrim(published_at)::timestamp AT TIME ZONE 'UTC')
        END;

ALTER TABLE social_media_posts
  ALTER COLUMN approved_at TYPE timestamptz
  USING CASE
          WHEN NULLIF(btrim(approved_at), '') IS NULL THEN NULL
          WHEN btrim(approved_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
            THEN btrim(approved_at)::timestamptz
          ELSE (btrim(approved_at)::timestamp AT TIME ZONE 'UTC')
        END;

-- Expect: announcements.published_at 0 non-null, social_media_posts.approved_at 4.
SELECT 'announcements.published_at' AS col, count(published_at) AS non_null
  FROM announcements
UNION ALL
SELECT 'social_media_posts.approved_at', count(approved_at) FROM social_media_posts;

COMMIT;


-- ============================================================================
-- TRANCHE B — small tables that hold REAL data.
-- Run only after tranche A has committed and the app is still healthy.
-- ============================================================================
BEGIN;

-- ★★ api_keys IS BEHIND A VIEW. The first attempt at this tranche aborted with
--    `FeatureNotSupported: cannot alter type of a column used by a view or
--    rule` and rolled back (2026-08-24). `api_keys_active` selects all four of
--    the columns below, and Postgres will not retype a column a view reads.
--    Measured before dropping it, so the recreate is faithful:
--        owner       neondb_owner   (same role this migration connects as)
--        relacl      NULL           (no explicit grants to restore)
--        reloptions  NULL
--        rows        83
--    It is not referenced anywhere in deployed Python — the drop window only
--    has to survive this transaction, and DDL is transactional in Postgres, so
--    a failure below puts the view back untouched.
--    construction_permits and news_articles have NO dependent views (checked
--    via pg_depend AND information_schema.view_column_usage).
DROP VIEW IF EXISTS api_keys_active;

-- api_keys (154 rows). last_used_at being TEXT is why the key-activity read
-- needed NULLIF(last_used_at,'')::timestamptz as a workaround — a workaround
-- that itself breaks here; see the third-group note above.
ALTER TABLE api_keys
  ALTER COLUMN created_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(created_at), '') IS NULL THEN NULL
             WHEN btrim(created_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(created_at)::timestamptz
             ELSE (btrim(created_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE api_keys
  ALTER COLUMN expires_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(expires_at), '') IS NULL THEN NULL
             WHEN btrim(expires_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(expires_at)::timestamptz
             ELSE (btrim(expires_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE api_keys
  ALTER COLUMN last_used_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(last_used_at), '') IS NULL THEN NULL
             WHEN btrim(last_used_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(last_used_at)::timestamptz
             ELSE (btrim(last_used_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE api_keys
  ALTER COLUMN last_reset_date TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(last_reset_date), '') IS NULL THEN NULL
             WHEN btrim(last_reset_date) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(last_reset_date)::timestamptz
             ELSE (btrim(last_reset_date)::timestamp AT TIME ZONE 'UTC') END;

-- construction_permits (915 rows; discovered_at and issued_date are 100% blank)
ALTER TABLE construction_permits
  ALTER COLUMN created_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(created_at), '') IS NULL THEN NULL
             WHEN btrim(created_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(created_at)::timestamptz
             ELSE (btrim(created_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE construction_permits
  ALTER COLUMN discovered_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(discovered_at), '') IS NULL THEN NULL
             WHEN btrim(discovered_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(discovered_at)::timestamptz
             ELSE (btrim(discovered_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE construction_permits
  ALTER COLUMN issued_date TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(issued_date), '') IS NULL THEN NULL
             WHEN btrim(issued_date) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(issued_date)::timestamptz
             ELSE (btrim(issued_date)::timestamp AT TIME ZONE 'UTC') END;

-- news_articles (13,159 rows) — THIS is the one that un-breaks
-- /api/v1/media/diagnose. Rebuilds idx_news_articles_published,
-- idx_news_published and idx_news_fetched.
ALTER TABLE news_articles
  ALTER COLUMN created_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(created_at), '') IS NULL THEN NULL
             WHEN btrim(created_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(created_at)::timestamptz
             ELSE (btrim(created_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE news_articles
  ALTER COLUMN fetched_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(fetched_at), '') IS NULL THEN NULL
             WHEN btrim(fetched_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(fetched_at)::timestamptz
             ELSE (btrim(fetched_at)::timestamp AT TIME ZONE 'UTC') END;

ALTER TABLE news_articles
  ALTER COLUMN published_at TYPE timestamptz
  USING CASE WHEN NULLIF(btrim(published_at), '') IS NULL THEN NULL
             WHEN btrim(published_at) ~ '(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
               THEN btrim(published_at)::timestamptz
             ELSE (btrim(published_at)::timestamp AT TIME ZONE 'UTC') END;

-- Recreate the view dropped above, verbatim from pg_get_viewdef as measured
-- 2026-08-24. It must come back inside THIS transaction.
CREATE VIEW api_keys_active AS
SELECT id, user_id, key_hash, key_prefix, name, permissions, rate_limit_tier,
       is_active, last_used_at, created_at, expires_at, usage_count, plan,
       last_reset_date, calls_today, calls_total, last_used, trial_expires_at,
       is_active_bool
  FROM api_keys
 WHERE (is_active = 1);

-- Expect 83, the count measured before the drop.
SELECT count(*) AS api_keys_active_rows FROM api_keys_active;

COMMIT;


-- ============================================================================
-- VERIFY (run after COMMIT, not inside it)
-- ============================================================================
-- 1. Every target column is now timestamptz:
--
--   SELECT table_name, column_name, data_type
--     FROM information_schema.columns
--    WHERE table_schema='public'
--      AND (table_name, column_name) IN (
--            ('announcements','published_at'),('social_media_posts','approved_at'),
--            ('api_keys','created_at'),('api_keys','expires_at'),
--            ('api_keys','last_used_at'),('api_keys','last_reset_date'),
--            ('construction_permits','created_at'),
--            ('construction_permits','discovered_at'),
--            ('construction_permits','issued_date'),
--            ('news_articles','created_at'),('news_articles','fetched_at'),
--            ('news_articles','published_at'))
--    ORDER BY table_name, column_name;
--
-- 2. The count that was an error string is now a number. This is the actual
--    point of tranche B — before: "operator does not exist: text > timestamp
--    with time zone"; after: an integer.
--
--   SELECT COUNT(*) FROM news_articles WHERE published_at > NOW() - INTERVAL '7 days';
--
-- 3. And from the outside — `errors.news` must be GONE, and counts.news present:
--
--   curl -s https://dchub.cloud/api/v1/media/diagnose | python3 -m json.tool
--
-- 4. Non-null counts must match what was measured before the change:
--       api_keys.created_at            154
--       api_keys.last_used_at           48
--       api_keys.last_reset_date        18
--       api_keys.expires_at              2
--       construction_permits.created_at 915
--       news_articles.fetched_at    13,159
--       news_articles.published_at  13,159
--       announcements.published_at       0
--       news_articles.created_at         0
--       construction_permits.discovered_at 0
--       construction_permits.issued_date   0
--       social_media_posts.approved_at     4
--
-- ============================================================================
-- ROLLBACK
-- ============================================================================
-- Converting back to TEXT is possible but is NOT a true undo — the reverse cast
-- re-renders the value in Postgres's output format ('2026-08-24 06:42:40+00'),
-- not the original ISO 'T' form ('2026-08-24T06:42:40'), and blanks come back
-- as NULL rather than ''. Readers written against the old strings may not match
-- what they matched before. Prefer rolling forward.
--
--   ALTER TABLE news_articles ALTER COLUMN published_at TYPE text
--     USING to_char(published_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS');
