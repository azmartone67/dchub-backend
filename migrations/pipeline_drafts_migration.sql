TEST CONTENT-- ============================================================
-- DC Hub: pipeline_drafts table
-- Staging queue for news-sourced pipeline entries
-- Run against Neon PostgreSQL
-- ============================================================

CREATE TABLE IF NOT EXISTS pipeline_drafts (
    id              SERIAL PRIMARY KEY,

    -- Core fields (mirror capacity_pipeline)
    company         TEXT NOT NULL,
    project         TEXT NOT NULL,
    market          TEXT NOT NULL,           -- e.g. "Putnam County, WV"
    capacity_mw     NUMERIC,                -- MW, nullable if unknown
    investment_m    NUMERIC,                -- Investment in $M, nullable
    status          TEXT DEFAULT 'announced', -- announced | construction | operational
    delivery        TEXT,                    -- e.g. "2027-Q2" or "TBD"
    type            TEXT DEFAULT 'hyperscale', -- hyperscale | ai-gpu | interconnection | adaptive | enterprise
    preleased       BOOLEAN DEFAULT FALSE,

    -- Draft-specific fields
    draft_status    TEXT DEFAULT 'pending'   -- pending | approved | rejected
        CHECK (draft_status IN ('pending', 'approved', 'rejected')),
    confidence      NUMERIC DEFAULT 0.5     -- 0.0-1.0 AI confidence score
        CHECK (confidence >= 0 AND confidence <= 1),
    source_title    TEXT,                    -- news headline that triggered this
    source_url      TEXT,                    -- link to original article
    source_date     TIMESTAMPTZ,            -- when the news was published

    -- Matching / dedup
    matched_pipeline_id  INTEGER,           -- if updating existing entry, FK to capacity_pipeline
    match_type      TEXT DEFAULT 'new'      -- new | update_status | update_capacity | update_operator
        CHECK (match_type IN ('new', 'update_status', 'update_capacity', 'update_operator', 'update_delivery')),

    -- Audit
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ,
    reviewed_by     TEXT,                   -- 'auto' or user email
    notes           TEXT,                   -- reviewer notes or AI reasoning

    -- Prevent exact dupes
    UNIQUE(company, project, source_title)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_drafts_status ON pipeline_drafts(draft_status);
CREATE INDEX IF NOT EXISTS idx_drafts_confidence ON pipeline_drafts(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON pipeline_drafts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_company ON pipeline_drafts(company);

-- View for quick review
CREATE OR REPLACE VIEW pipeline_review_queue AS
SELECT
    id,
    draft_status,
    confidence,
    company,
    project,
    market,
    capacity_mw,
    status,
    delivery,
    match_type,
    source_title,
    created_at
FROM pipeline_drafts
WHERE draft_status = 'pending'
ORDER BY confidence DESC, created_at DESC;
