-- 2026-07-16: STATEFUL proposal dedup — brain_enhancement_proposals and
-- brain_self_agenda gain a nullable `fingerprint` column: a stable hash of
-- the UNDERLYING CONDITION a proposal addresses (area + number-stripped
-- signal/title, falling back to the normalized question — see
-- routes/brain_proposal_dedup.py). Drafting pipelines skip a condition whose
-- fingerprint already has an OPEN proposal, and re-draft after the
-- BRAIN_PROPOSAL_REDRAFT_DAYS cooldown only when its measured figures moved
-- materially. Verified live before shipping: 33/37 stored proposals (31 still
-- open) were the SAME data_coverage condition, +14 twins in the self-agenda.
--
-- NOTE: the live tables are ALSO migrated at boot by init_enhancer_schema() /
-- init_self_director_schema() via direct psycopg2 (safe_db SKIPs DDL), so
-- this file documents the DDL for fresh environments / DR restores.

ALTER TABLE brain_enhancement_proposals ADD COLUMN IF NOT EXISTS fingerprint TEXT;
CREATE INDEX IF NOT EXISTS ix_brain_enh_props_fp
    ON brain_enhancement_proposals (fingerprint, created_at DESC);

ALTER TABLE brain_self_agenda ADD COLUMN IF NOT EXISTS fingerprint TEXT;
CREATE INDEX IF NOT EXISTS ix_brain_self_agenda_fp
    ON brain_self_agenda (fingerprint, created_at DESC);
