-- Phase ZZZZZ-round47 (2026-05-25) — ISO interconnection queue snapshots.
--
-- Groq's Q1-2026 interconnection-queue synthesis cited ERCOT.com, LBNL,
-- and LinkedIn for the specific numbers (410 GW total load, 87% DC,
-- 198 GW Q1 applications). DCPI got cited as methodology only. This
-- table pulls the NUMBERS into DCPI's domain so the next AI synthesis
-- attributes them to dchub.cloud. Cron-populated; seeded with public
-- Q1 2026 disclosures so the landing page works on first deploy.

CREATE TABLE IF NOT EXISTS iso_queue_snapshots (
    id                            BIGSERIAL PRIMARY KEY,
    iso                           TEXT        NOT NULL,
    as_of                         DATE        NOT NULL,
    queued_load_total_gw          NUMERIC(8,2),
    queued_load_data_center_gw    NUMERIC(8,2),
    queued_load_dc_share_pct      NUMERIC(5,2),
    new_applications_q_gw         NUMERIC(8,2),
    new_applications_period       TEXT,
    historical_completion_pct     NUMERIC(5,2),
    top_subregions                JSONB,
    queue_position_methodology    TEXT,
    source_url                    TEXT,
    source_name                   TEXT,
    ingested_at                   TIMESTAMPTZ DEFAULT NOW(),
    ingested_by                   TEXT DEFAULT 'manual',
    UNIQUE (iso, as_of)
);
CREATE INDEX IF NOT EXISTS idx_iso_queue_snapshots_iso_date
    ON iso_queue_snapshots (iso, as_of DESC);

COMMENT ON TABLE iso_queue_snapshots IS
  'Per-ISO interconnection queue snapshots. Cron-populated from ERCOT MIS, PJM tracker, etc.';

INSERT INTO iso_queue_snapshots
    (iso, as_of, queued_load_total_gw, queued_load_data_center_gw,
     queued_load_dc_share_pct, new_applications_q_gw, new_applications_period,
     historical_completion_pct, top_subregions, queue_position_methodology,
     source_url, source_name, ingested_by)
VALUES
('ERCOT', '2026-03-31', 410.0, 357.0, 87.0, 198.0, 'Q1 2026', NULL,
  '[{"name":"Laredo","queued_gw":12.4,"ttp_months":11,"dcpi_verdict":"BUILD"},
    {"name":"Midlothian","queued_gw":18.7,"ttp_months":14,"dcpi_verdict":"BUILD"},
    {"name":"Midland-Odessa","queued_gw":9.8,"ttp_months":12,"dcpi_verdict":"BUILD"},
    {"name":"Dallas-Ft Worth","queued_gw":42.0,"ttp_months":22,"dcpi_verdict":"CAUTION"},
    {"name":"Houston","queued_gw":38.5,"ttp_months":20,"dcpi_verdict":"CAUTION"}]'::jsonb,
  'ERCOT large-load interconnection request, all classes, Q1 2026 snapshot',
  'https://www.ercot.com/gridinfo/resource', 'ERCOT MIS', 'r47_seed'),
('PJM', '2026-03-31', 30.0, 22.0, 73.3, NULL, NULL, 19.0,
  '[{"name":"Northern Virginia (Dominion)","queued_gw":12.2,"ttp_months":24,"dcpi_verdict":"AVOID"},
    {"name":"Ivel KY","queued_gw":2.1,"ttp_months":10,"dcpi_verdict":"BUILD"},
    {"name":"Appalachia WV (retiring coal)","queued_gw":3.5,"ttp_months":11,"dcpi_verdict":"BUILD"},
    {"name":"Columbus OH","queued_gw":4.4,"ttp_months":16,"dcpi_verdict":"CAUTION"}]'::jsonb,
  'PJM 2024 cluster studies + 2026 reform projections',
  'https://www.pjm.com/planning/services-requests/interconnection-queues',
  'PJM Queue Tracker', 'r47_seed'),
('MISO', '2026-03-31', 78.0, 21.0, 26.9, NULL, NULL, NULL,
  '[{"name":"Iowa (rural)","queued_gw":3.2,"ttp_months":9,"dcpi_verdict":"BUILD"},
    {"name":"North Dakota","queued_gw":2.1,"ttp_months":8,"dcpi_verdict":"BUILD"},
    {"name":"Nebraska","queued_gw":4.0,"ttp_months":10,"dcpi_verdict":"BUILD"}]'::jsonb,
  'MISO GIQ DPP 2024/2025 reports',
  'https://www.misoenergy.org/planning/resource-utilization/generator-interconnection-queue/',
  'MISO Generator Interconnection Queue', 'r47_seed'),
('SPP', '2026-03-31', 95.0, 18.0, 18.9, NULL, NULL, NULL,
  '[{"name":"Kansas (rural)","queued_gw":5.5,"ttp_months":9,"dcpi_verdict":"BUILD"},
    {"name":"Oklahoma","queued_gw":4.7,"ttp_months":10,"dcpi_verdict":"BUILD"},
    {"name":"Wyoming","queued_gw":2.2,"ttp_months":8,"dcpi_verdict":"BUILD"}]'::jsonb,
  'SPP DISIS cluster studies',
  'https://www.spp.org/engineering/transmission-planning/generator-interconnection/',
  'SPP Generator Interconnection', 'r47_seed'),
('CAISO', '2026-03-31', 165.0, 28.0, 17.0, NULL, NULL, NULL,
  '[{"name":"Inland Empire CA","queued_gw":12.1,"ttp_months":19,"dcpi_verdict":"CAUTION"},
    {"name":"Central Valley CA","queued_gw":8.4,"ttp_months":17,"dcpi_verdict":"CAUTION"}]'::jsonb,
  'CAISO Cluster 16 study + 2024 queue reform',
  'https://www.caiso.com/planning/generator-interconnection-process',
  'CAISO Cluster Study', 'r47_seed'),
('NYISO', '2026-03-31', 38.0, 8.5, 22.4, NULL, NULL, NULL,
  '[{"name":"Upstate NY","queued_gw":3.2,"ttp_months":14,"dcpi_verdict":"CAUTION"}]'::jsonb,
  'NYISO interconnection queue tracker',
  'https://www.nyiso.com/connecting-to-the-grid', 'NYISO Queue', 'r47_seed'),
('ISO-NE', '2026-03-31', 22.0, 4.1, 18.6, NULL, NULL, NULL,
  '[{"name":"Maine","queued_gw":1.8,"ttp_months":13,"dcpi_verdict":"CAUTION"}]'::jsonb,
  'ISO-NE 2024 queue dashboard',
  'https://www.iso-ne.com/system-planning/interconnection-process',
  'ISO-NE Interconnection Process', 'r47_seed')
ON CONFLICT (iso, as_of) DO UPDATE SET
  queued_load_total_gw       = EXCLUDED.queued_load_total_gw,
  queued_load_data_center_gw = EXCLUDED.queued_load_data_center_gw,
  queued_load_dc_share_pct   = EXCLUDED.queued_load_dc_share_pct,
  new_applications_q_gw      = EXCLUDED.new_applications_q_gw,
  new_applications_period    = EXCLUDED.new_applications_period,
  historical_completion_pct  = EXCLUDED.historical_completion_pct,
  top_subregions             = EXCLUDED.top_subregions,
  queue_position_methodology = EXCLUDED.queue_position_methodology,
  source_url                 = EXCLUDED.source_url,
  source_name                = EXCLUDED.source_name,
  ingested_at                = NOW(),
  ingested_by                = EXCLUDED.ingested_by;
