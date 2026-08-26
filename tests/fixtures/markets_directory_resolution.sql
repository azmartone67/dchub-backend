-- Fixture for the /markets/directory slug-resolution query (2026-08-26).
--
-- WHY THIS FILE EXISTS. The three DB-backed tests in
-- tests/test_markets_directory_slug_groups.py SKIP in CI — no workflow in
-- .github/workflows/ injects a database URL into a pytest job, so they have
-- never executed there and a green `unit-tests` says nothing about them.
-- Verified on run 33021627637: all four shape fences PASSED, all three
-- DB-backed tests SKIPPED.
--
-- So the SQL was verified against a real PostgreSQL 18 instead, with this
-- fixture and a must-fail control. Reproduce:
--
--   initdb -D /tmp/pgd -U postgres --auth=trust
--   LC_ALL=C pg_ctl -D /tmp/pgd -o "-p 55433 -k /tmp" start
--   psql -h /tmp -p 55433 -U postgres -f tests/fixtures/markets_directory_resolution.sql
--   # then run the query the handler executes (capture it with _RecordingConn)
--
-- The fixture is built so the OLD and NEW queries MUST disagree — a fixture
-- both pass on would prove nothing.
--
--   NEW query -> 3 rows, every slug a real market:
--       ashburn            Ashburn        VA  2 facilities  107 MW
--       mount-pleasant-wi  Mount Pleasant WI  1              50
--       dallas             Dallas         TX  3              35
--
--   OLD query -> 6 rows, only 1 of them a real market (83% would 404):
--       ashburn-va  dallas-tx  dallas-ga  coburg-de  trento-it   <- all dead
--       mount-pleasant-wi                                        <- the only live one

CREATE TABLE discovered_facilities (
  id serial primary key, city text, state text, power_mw numeric, is_duplicate int
);
CREATE TABLE market_power_scores (market_slug text, score int);

-- Markets that EXIST. 'dallas' is listed three times on purpose: several score
-- rows per market must not fan a facility out and inflate the listing counts.
INSERT INTO market_power_scores VALUES
  ('dallas',1), ('ashburn',2), ('mount-pleasant-wi',3),
  ('dallas',9), ('dallas',9);

INSERT INTO discovered_facilities (city,state,power_mw,is_duplicate) VALUES
  ('Dallas','TX',10,0), ('Dallas','TX',20,0),   -- resolve via city_slug -> dallas
  ('Dallas','GA',5,0),                          -- a DIFFERENT city, same market -> merges
  ('Ashburn','VA',100,0), ('ASHBURN','VA',7,0), -- case variants -> one row, label 'Ashburn'
  ('Mount Pleasant','WI',50,0),                 -- resolves via combo_slug (preferred)
  ('Coburg','DE',3,0), ('Trento','IT',2,0),     -- no market at all -> DROPPED (the fix)
  ('Dallas','TX',999,1);                        -- is_duplicate -> excluded
