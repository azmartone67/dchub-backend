-- 2026-07-29 — make the competitor-gap sweep ANSWERABLE.
--
-- The crawler is the inventory growth engine: it added 960 of the ~1,490 new
-- distinct buildings in the last 60 days (64%), versus openstreetmap 334,
-- epa_echo_air 131, news_ner 49, peeringdb 18.
--
-- ★THE QUESTION WE COULD NOT ANSWER. Cloudscene's sitemap carries 11,859
-- data-center URLs and we hold 962 distinct (8.1%). Nothing in the database
-- could tell us whether the other 92% was NEVER REACHED or ALREADY KNOWN,
-- because a candidate that matches an existing facility is dropped in
-- `diff_gaps` (dropped_existing) and leaves no trace — `coverage_gaps` only
-- ever stores gaps. That single unknown decides the whole inventory roadmap:
--   never reached  -> raise the caps. Free, thousands of facilities, no new
--                     integration risk.
--   already known  -> Cloudscene is done at 962 and the next lever is a NEW
--                     source (a project, not a config change).
-- Guessing here would have cost weeks either way.
--
-- The sweep walks a CONTIGUOUS window: offset = day_of_year * limit, wrapping
-- mod the sitemap length, with step == window width. So recording
-- (window_offset, window_size, locs_total) per run is enough to reconstruct
-- exactly which slice of each sitemap has been visited — no need to store
-- 12k URLs per source.
--
-- `dropped_existing` is the other half of the answer: it is the count of
-- candidates we ALREADY had. High dropped_existing + full coverage = the
-- source is genuinely tapped out.

CREATE TABLE IF NOT EXISTS competitor_gap_sweeps (
    id                  BIGSERIAL PRIMARY KEY,
    slug                TEXT        NOT NULL,     -- source slug, e.g. 'cloudscene'
    run_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    locs_total          INTEGER,                  -- URLs in the sitemap this run
    window_offset       INTEGER,                  -- start of the window swept
    window_size         INTEGER,                  -- how many URLs the window covers
    parsed              INTEGER,                  -- candidates parsed from the window
    dropped_existing    INTEGER,                  -- ★ already in our inventory
    dropped_not_facility INTEGER,
    true_gaps           INTEGER,
    gap_only            INTEGER,
    inserted            INTEGER,
    dup                 INTEGER,
    status              INTEGER,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS ix_cgs_slug_run ON competitor_gap_sweeps (slug, run_at DESC);
