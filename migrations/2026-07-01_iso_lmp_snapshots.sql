-- 2026-07-01 — Real-time per-ISO LMP snapshots (routes/iso_lmp_ingest.py).
--
-- Bounded per-ISO hub/zone LMP rows (max ~34 rows per ISO per 5-min
-- interval; history pruned to 7 days by the ingestor). interval_ending is
-- ALWAYS UTC — each ISO publishes in its local prevailing time and the
-- ingestor converts on write. congestion/energy/loss are NULLABLE because
-- not every ISO publishes the split (ERCOT's np6-788-cd display is
-- LMP-only); NULL means "not published", never zero.
--
-- NOTE: routes/iso_lmp_ingest.py also creates this table lazily on first
-- ingest (CREATE TABLE IF NOT EXISTS inside the request, never at boot),
-- so applying this migration manually is optional but preferred.

CREATE TABLE IF NOT EXISTS iso_lmp_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    iso                 TEXT        NOT NULL,
    location            TEXT        NOT NULL,   -- zone or trading-hub name
    location_type       TEXT,                   -- 'hub' | 'zone'
    lmp_usd_mwh         NUMERIC(10,2) NOT NULL,
    congestion_usd_mwh  NUMERIC(10,2),          -- NULL = not published (e.g. ERCOT)
    energy_usd_mwh      NUMERIC(10,2),          -- NULL = not published
    loss_usd_mwh        NUMERIC(10,2),          -- NULL = not published
    interval_ending     TIMESTAMPTZ NOT NULL,   -- UTC, converted on ingest
    interval_minutes    INTEGER     DEFAULT 5,
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    source_url          TEXT,
    source_name         TEXT,
    UNIQUE (iso, location, interval_ending)
);

CREATE INDEX IF NOT EXISTS idx_iso_lmp_snapshots_iso_interval
    ON iso_lmp_snapshots (iso, interval_ending DESC);

COMMENT ON TABLE iso_lmp_snapshots IS
  'Real-time hub/zone LMP snapshots per ISO (MISO/NYISO/SPP/CAISO/PJM/ERCOT). UTC intervals. Populated by /api/v1/iso-lmp/ingest.';
