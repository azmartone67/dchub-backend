-- facility_market_resolution.sql — r-market-resolve-geo (2026-08-26).
--
-- Fixture for routes/facility_profile_page.py:_market_dcpi. Built so the OLD
-- and NEW resolution MUST disagree on the same rows: if a revert lands, the
-- harness that drives this fixture goes red rather than quietly passing.
--
-- ★ NO pytest job in this repo has a database (verified on run 33021627637),
--   so the DB-backed assertions never run in CI. This file is how the SQL is
--   actually verified: a throwaway PostgreSQL 18, `LC_ALL=C` (without it the
--   local server dies with "postmaster became multithreaded").
--
-- Coordinates are real. The three US/BR collisions below are the ones measured
-- live on 2026-08-26 over a random 500 of the 9,095 sitemap facility pages.
DROP TABLE IF EXISTS market_power_scores;
CREATE TABLE market_power_scores (
    market_slug           TEXT,
    market_name           TEXT,
    iso                   TEXT,
    verdict               TEXT,
    excess_power_score    NUMERIC,
    constraint_score      NUMERIC,
    time_to_power_months  NUMERIC,
    state                 TEXT,
    latitude              DOUBLE PRECISION,
    longitude             DOUBLE PRECISION,
    computed_at           TIMESTAMPTZ
);

INSERT INTO market_power_scores
    (market_slug, market_name, iso, verdict, excess_power_score,
     constraint_score, time_to_power_months, state, latitude, longitude, computed_at) VALUES
-- US markets. `charleston-sc` and `billings` are the rows that a Brazilian
-- state code collides with; `atlanta` and `chester` are the correct answers
-- for two facilities the old code mis-resolved or resolved at distance.
 ('charleston-sc','Charleston','SERC','CAUTION', 41, 55, 30, 'SC', 32.7765, -79.9311, now()),
 ('billings',     'Billings',  'WECC','CAUTION', 38, 52, 28, 'MT', 45.7833,-108.5007, now()),
 ('atlanta',      'Atlanta',   'SERC','BUILD',   72, 30, 18, 'GA', 33.7490, -84.3880, now() - interval '2 days'),
 ('chester',      'Chester',   'PJM', 'BUILD',   68, 34, 20, 'VA', 37.3563, -77.4360, now()),
 ('ashburn',      'Ashburn',   'PJM', 'BUILD',   88, 22, 14, 'VA', 39.0438, -77.4874, now()),
-- Durham NC sits 90 km from Boydton VA; Chester VA sits 114 km away. Durham is
-- NEARER and on a DIFFERENT grid (Duke Progress/SERC vs Dominion/PJM), which is
-- exactly the pick the same-state-first ordering has to refuse.
 ('durham',       'Durham',    'SERC','BUILD',   64, 36, 21, 'NC', 35.9940, -78.8986, now()),
-- Non-US markets. NOTE `state` here: the live table stores a Spanish market
-- with state 'ES', which is also Brazil's Espirito Santo — that pair is the
-- collision, and it is why a country column would not have saved this either.
 ('madrid',       'Madrid',    NULL,  'CAUTION', 50, 48, 26, 'ES', 40.4168,  -3.7038, now()),
 ('frankfurt',    'Frankfurt', NULL,  'BUILD',   70, 35, 22, NULL, 50.1109,   8.6821, now()),
 ('barueri',      'Barueri',   NULL,  'CAUTION', 45, 50, 30, 'SP',-23.5106, -46.8761, now()),
 ('london',       'London',    NULL,  'BUILD',   66, 38, 24, NULL, 51.5074,  -0.1278, now()),
-- A coordinate-less namesake: /dcpi/athens is Greece. It carries state NULL,
-- which satisfies the `state IS NULL OR ...` arm of the exact-slug match for a
-- facility in Athens, GEORGIA — an 9,039 km "market".
 ('athens',       'Athens',    NULL,  'CAUTION', 44, 51, 27, NULL, 37.9838,  23.7275, now());
