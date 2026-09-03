-- 2026-09-03 — give fiber_routes a searchable BOUNDING BOX so a viewport query
-- can find routes that CROSS it, not only routes that START in it.
--
-- ★ WHAT WAS WRONG. /api/v1/fiber/routes?bbox= filtered on the start point:
--
--     AND start_lng BETWEEN %s AND %s AND start_lat BETWEEN %s AND %s
--
-- A route whose polyline crosses the viewport but begins somewhere else was
-- dropped. Routes are stored as whole carrier segments — a Zayo leased-longhaul
-- segment is one row with 493 vertices spanning several counties — so "starts
-- inside this screen" is close to a random filter at metro zoom.
--
-- MEASURED LIVE 2026-09-03 at 2675 Olthoff Dr, Muskegon MI (the address a
-- customer used to compare us against FiberLocator), viewport
-- -86.30,43.19,-86.08,43.30, against the routes the API returned for Michigan:
--
--     routes whose GEOMETRY passes through the viewport ... 21
--     routes the start-point filter KEEPS ................. 16
--     dropped ............................................. 5
--
-- and the 5 dropped included THE ONLY Zayo route reaching Muskegon —
-- 'Zayo ZAYO LEASED LONGHAUL BACKBONE [Leased-Iru Longhaul] seg2070',
-- 493 vertices, starting near Grand Rapids (-85.737, 42.972).
-- Bluebird Network survived only because its start points happen to fall
-- inside that box. That is the whole of the "they show Zayo/Uniti/US Signal
-- and we only show Bluebird" complaint.
--
-- ★ WHY COLUMNS AND NOT PostGIS. fiber_routes.coordinates is a JSON array of
-- [lng,lat] pairs, not geometry. The GIST index that exists in this schema
-- (idx_fiber_geom, migrations/002_discovery_tables.sql) is on a DIFFERENT
-- table — infrastructure_fiber — and does not cover these 64,836 rows.
-- Materializing four numeric columns gives an indexable box-overlap test with
-- no extension dependency and no change to how geometry is stored or served.
--
-- ★★ NOTHING PUBLISHED IS REWRITTEN. This adds four columns that did not
-- exist and fills them from each row's OWN coordinates. No row is inserted,
-- deleted, renamed, reclassified or redirected; `coordinates`, `start_lat`,
-- `start_lng`, `end_lat`, `end_lng`, `name`, `provider` are all untouched.
--
-- ★★ ROWS THIS CANNOT IDENTIFY KEEP NULL, and the API's bbox filter treats
-- NULL as "unknown extent" and falls back to the endpoint test for those rows
-- (see _build_fiber_routes_geojson). A row with malformed or non-numeric
-- coordinates is therefore never silently dropped from a viewport it belongs
-- in, and never fabricated into one it does not.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + a backfill restricted to rows whose
-- box is still NULL. Safe to re-run after new routes are ingested.

BEGIN;

ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS min_lat DOUBLE PRECISION;
ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS max_lat DOUBLE PRECISION;
ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS min_lng DOUBLE PRECISION;
ALTER TABLE fiber_routes ADD COLUMN IF NOT EXISTS max_lng DOUBLE PRECISION;

-- (1) Multi-vertex rows: true extent of the stored polyline.
--
-- jsonb_array_elements would ERROR on a row whose `coordinates` is a JSON
-- scalar or object rather than an array, and one bad row would abort the whole
-- migration. jsonb_typeof(...)='array' in the WHERE is not enough on its own —
-- the planner may evaluate the LATERAL before the filter — so the set-returning
-- call is wrapped in a subquery that only ever sees arrays of arrays.
WITH pts AS (
    SELECT f.id,
           (pt->>0)::double precision AS lng,
           (pt->>1)::double precision AS lat
      FROM (
            SELECT id, coordinates::jsonb AS c
              FROM fiber_routes
             WHERE coordinates IS NOT NULL
               AND min_lat IS NULL
               AND jsonb_typeof(coordinates::jsonb) = 'array'
           ) f
      CROSS JOIN LATERAL jsonb_array_elements(f.c) AS pt
     WHERE jsonb_typeof(pt) = 'array'
       AND jsonb_array_length(pt) >= 2
       AND jsonb_typeof(pt->0) = 'number'
       AND jsonb_typeof(pt->1) = 'number'
), box AS (
    SELECT id,
           MIN(lat) AS mnla, MAX(lat) AS mxla,
           MIN(lng) AS mnlo, MAX(lng) AS mxlo,
           COUNT(*) AS n
      FROM pts
     -- Reject impossible coordinates outright rather than letting one bad
     -- vertex stretch a route's box across the planet (a route boxed to the
     -- whole world would match EVERY viewport — worse than being missing).
     WHERE lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
     GROUP BY id
    HAVING COUNT(*) >= 2
)
UPDATE fiber_routes f
   SET min_lat = b.mnla, max_lat = b.mxla,
       min_lng = b.mnlo, max_lng = b.mxlo
  FROM box b
 WHERE f.id = b.id;

-- (2) Legacy endpoint-only rows: box = the two endpoints. These are the
-- synthetic 2-point segments; their box is exactly what the old filter
-- already implied, so this changes nothing about where they match — it only
-- puts them on the same indexed path as everything else.
UPDATE fiber_routes
   SET min_lat = LEAST(start_lat, end_lat),
       max_lat = GREATEST(start_lat, end_lat),
       min_lng = LEAST(start_lng, end_lng),
       max_lng = GREATEST(start_lng, end_lng)
 WHERE min_lat IS NULL
   AND start_lat IS NOT NULL AND start_lng IS NOT NULL
   AND end_lat   IS NOT NULL AND end_lng   IS NOT NULL
   AND start_lat BETWEEN -90 AND 90 AND end_lat BETWEEN -90 AND 90
   AND start_lng BETWEEN -180 AND 180 AND end_lng BETWEEN -180 AND 180;

-- Overlap is tested as (min_lng <= box_max AND max_lng >= box_min) on both
-- axes. Postgres can use a btree on the leading column to cut the scan; the
-- partial predicate keeps the index off the rows the filter can't use anyway.
CREATE INDEX IF NOT EXISTS fiber_routes_bbox_lng
    ON fiber_routes (min_lng, max_lng)
    WHERE min_lng IS NOT NULL;
CREATE INDEX IF NOT EXISTS fiber_routes_bbox_lat
    ON fiber_routes (min_lat, max_lat)
    WHERE min_lat IS NOT NULL;

COMMIT;

-- ── VERIFY (run after; all read-only) ──────────────────────────────────────
-- Coverage: how many routes now carry a real extent.
--   SELECT COUNT(*) AS rows,
--          COUNT(min_lat) AS boxed,
--          COUNT(*) - COUNT(min_lat) AS unboxed
--     FROM fiber_routes;
--
-- The Muskegon case this migration exists for. Expect the second number to be
-- strictly larger than the first, and Zayo to appear only in the second.
--   SELECT 'start-point (old)' AS filter, COUNT(*) FROM fiber_routes
--    WHERE start_lng BETWEEN -86.30 AND -86.08
--      AND start_lat BETWEEN  43.19 AND  43.30
--   UNION ALL
--   SELECT 'box-overlap (new)', COUNT(*) FROM fiber_routes
--    WHERE min_lng <= -86.08 AND max_lng >= -86.30
--      AND min_lat <=  43.30 AND max_lat >=  43.19;
--
--   SELECT provider, COUNT(*) FROM fiber_routes
--    WHERE min_lng <= -86.08 AND max_lng >= -86.30
--      AND min_lat <=  43.30 AND max_lat >=  43.19
--    GROUP BY 1 ORDER BY 2 DESC;
