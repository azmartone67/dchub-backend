-- 2026-08-05 — Phase 0 of the brain detector-supply pipeline.
-- Spec: docs/brain-detector-supply-pipeline.md
--
-- ★WHY THIS TABLE EXISTS. The white-glove BRAIN lane
-- (routes/white_glove_loop_master_shell.py:246) has been critical-red on
-- purpose since 2026-07-30 with a measured diagnosis: the six mechanical
-- transform classes are EXHAUSTED, only EIGHT proposals are blocked SOLELY by
-- the class gate, and "autonomy is capped by DETECTOR SUPPLY". Adding a
-- detector is a human job that has happened six times, ever.
--
-- Phase 0 is the cheapest falsifiable step toward mechanising that: scout
-- GitHub for repos carrying known-shape code-transform corpora (codemods,
-- semgrep/ruff rule sets, libcst transforms) and record what survives a
-- DETERMINISTIC filter. NO LLM call, NO proposal, NO PR — this phase only
-- answers "is there a funnel here at all?".
--
-- ★THE EXIT CRITERION IS A NUMBER, AND IT CAN FAIL. >=20 repos surviving the
-- filter over 2 weeks. If the funnel comes back empty the query set is wrong
-- and the expensive stages (Reader / Extractor / Score) are premature — that
-- is the outcome this table is built to be able to report, not to hide.
--
-- Only ONE table ships here. `detector_candidates` is specified in the doc but
-- belongs to Phases 1-2; creating it now would leave an always-empty table that
-- reads like shipped capability.

CREATE TABLE IF NOT EXISTS detector_scout_repos (
    id            BIGSERIAL PRIMARY KEY,
    full_name     TEXT        NOT NULL,   -- owner/repo
    html_url      TEXT,
    description   TEXT,
    head_sha      TEXT,                   -- default-branch tip when last seen
    stars         INTEGER,
    language      TEXT,
    licence       TEXT,                   -- SPDX id, lowercased; NULL = unlicensed
    pushed_at     TIMESTAMPTZ,
    -- queued  : survived the filter, awaiting Phase 1 (which does not exist yet)
    -- rejected: the filter refused it; reject_reason names the ONE rule that fired
    status        TEXT        NOT NULL DEFAULT 'queued',
    reject_reason TEXT,
    query_slug    TEXT,                   -- which scout query surfaced it
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT detector_scout_repos_uniq UNIQUE (full_name)
);

-- The status surface reads "what is queued, newest first" on every tick.
CREATE INDEX IF NOT EXISTS detector_scout_repos_status_idx
    ON detector_scout_repos (status, last_seen_at DESC);

-- The exit criterion is a COUNT OVER A WINDOW of first_seen_at, so it gets its
-- own index rather than riding the status one.
--
-- ★A plain timestamptz index, NOT an expression index over ::date. Indexing
-- `timestamptz::date` is non-IMMUTABLE and Postgres rejects it — this codebase
-- has its own allowlisted `immutable_index` transform class precisely because
-- that trap has bitten it before (routes/brain_mechanical_classifier.py).
CREATE INDEX IF NOT EXISTS detector_scout_repos_first_seen_idx
    ON detector_scout_repos (first_seen_at DESC);
