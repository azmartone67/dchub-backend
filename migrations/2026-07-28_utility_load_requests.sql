-- migrations/2026-07-28_utility_load_requests.sql
--
-- The utility data-centre load register. Schema + the 7 curated rows, so both are
-- reviewable in git rather than living only in the prod DB.
--
-- ★★ WHY THE CITATION CONSTRAINT IS THE POINT — DO NOT REMOVE IT.
-- This table replaces uncited aggregates that were sitting in capacity_pipeline
-- (AEP 63,000 MW, Dominion 48,000, PPL 25,200, "Google 150,000 operational") with
-- NO source, NO url and NO notes — plausible-looking numbers with zero provenance,
-- which is how 67% of a published 2,514 GW figure turned out to be unciteable.
-- source_url is NOT NULL and regex-checked; source_title is NOT NULL; as_of_date is
-- NOT NULL because these figures move quarterly. A row cannot exist without a
-- resolvable citation. Verified by attempting to insert
-- ('AEP', 63, 'no-citation', 'remembered from somewhere') -> rejected.
--
-- ★★ WHAT THE ROWS SHOW (the differentiated asset): every utility's headline is
-- 2-3x what it actually forecasts, and each discloses the discount in its own
-- filing. Oncor 271 raw requests -> 122 forecast (2.2x). Dominion 47 GW contracted
-- capacity -> 16.6 GW forecast demand (2.8x), with 64% of the 47 at the weakest
-- ELOA tier (site control + a $250k deposit). Exelon 18 GW committed but only ~45%
-- TSA-secured. request_type exists SO THESE NEVER COLLAPSE INTO ONE "GW" NUMBER
-- AGAIN -- that collapse is exactly what produced the 2,514 GW figure.
--
-- ★ HOW TO ADD A UTILITY (the path that works): the GW figures live in earnings-deck
-- 8-K EXHIBIT 99.2 on EDGAR -- not in the 10-K/10-Q, and not reliably on corporate
-- IR sites (403/404/JS). Recipe:
--   data.sec.gov/submissions/CIK{10-digit}.json -> filter form=='8-K' for the
--   earnings month -> fetch the accession DIRECTORY listing -> grep href=".*ex99.*\.htm"
--   -> fetch -> regex ([0-9.,]+)\s?(GW|gigawatt) with a +/-250 char context window
--   requiring data cent|pipeline|committed|queue|study phase -> paste the sentence
--   VERBATIM into notes. EDGAR requires a descriptive User-Agent; sleep ~0.4s.
-- Never cite a search snippet. No primary figure => no row. Rejected on that basis:
-- PPL (4 conflicting figures in coverage, none in its release or 8-K), Entergy
-- (states the GENERATION built for Meta, never Meta's load), Duke (May-2026 8-K has
-- zero data-centre mentions).

CREATE TABLE IF NOT EXISTS utility_load_requests (
    id              BIGSERIAL PRIMARY KEY,
    utility         TEXT NOT NULL,
    state           TEXT,
    iso             TEXT,
    gw_requested    DOUBLE PRECISION NOT NULL CHECK (gw_requested > 0 AND gw_requested < 500),
    as_of_date      DATE NOT NULL,
    source_url      TEXT NOT NULL CHECK (source_url ~* '^https?://.+\..+'),
    source_title    TEXT NOT NULL,
    filing_type     TEXT,
    request_type    TEXT,
    notes           TEXT,
    verified_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (utility, as_of_date, gw_requested)
);
CREATE INDEX IF NOT EXISTS ix_ulr_utility ON utility_load_requests(utility);
CREATE INDEX IF NOT EXISTS ix_ulr_asof    ON utility_load_requests(as_of_date DESC);

-- ── curated rows (7) ────────────────────────────────────────────────────────
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('Oncor Electric Delivery','TX','ERCOT',271,'2026-03-31','https://www.oncor.com/content/oncorwww/wire/en/home/newsroom/oncor-reports-first-quarter-2026-results.html','Oncor Reports First Quarter 2026 Results, interconnection-queue paragraph','quarterly results','RAW interconnection REQUESTS from data centres','VERBATIM: "Oncor''s active transmission LC&I interconnection queue included 697 requests at the end of the first quarter of 2026. Those requests included approximately 271 gigawatts from data centers and over 18 gigawatts of load from various other industrial sectors." ★★SECONDARY SOURCES WERE STALE/WRONG: coverage reported 186 GW and ~200 GW; the primary says 271 GW. Cite the filing, not the article. ★Pair with the 122 GW row: Oncor''s own ERCOT RTP forecast is 2.2x SMALLER than its raw queue — the same capacity-vs-demand gap Dominion flags (47 vs 16.6).','claude-opus-5 2026-07-28 (page fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('Oncor Electric Delivery','TX','ERCOT',122,'2026-04-01','https://www.oncor.com/content/oncorwww/wire/en/home/newsroom/oncor-reports-first-quarter-2026-results.html','Oncor Reports First Quarter 2026 Results, ERCOT 2026 RTP submission paragraph','ERCOT Regional Transmission Plan submission','FORECAST large load through 2036 (the filtered number)','VERBATIM: "On April 1, 2026, Oncor submitted 122 gigawatts of large load forecast data and 5.2 gigawatts of medium load forecast data through 2036 for inclusion in the Electric Reliability Council of Texas'' ("ERCOT") 2026 Regional Transmission Plan." ★This is what Oncor actually FORECASTS vs 271 GW of raw requests — quote 122 for planning, 271 only as queue volume, never interchangeably.','claude-opus-5 2026-07-28 (page fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('American Electric Power','multi (11-state)','PJM/SPP/ERCOT',63,'2026-07-28','https://www.aep.com/investors','AEP Investors page, Investment Highlights — "Best-in-Class Load Growth: An additional 63GW by 2030, backed by signed customer financial commitments"','investor relations','committed new load (not a request queue)','VERBATIM: "Our five-year, $78 billion capital plan and 63 GW of new load by 2030, backed by signed agreements". ★SEMANTICS: this is COMMITTED/CONTRACTED new load by 2030 backed by signed customer financial commitments — NOT an interconnection-request queue, and NOT data-centre-only. Do not restate it as "63 GW of data centre requests". Checked AEP 10-Q (2026-05-05): mentions data centres ONCE, about new large-load tariff filings, with NO GW figure — so the SEC filings do not carry this number.','claude-opus-5 2026-07-28 (fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('Dominion Energy Virginia','VA','PJM',47,'2025-07-01','https://www.pjm.com/-/media/DotCom/planning/res-adeq/load-forecast/dominion-documentation.pdf','Letter, Stan Blackwell (Director - Data Center Practice, Dominion Energy) to PJM Load Analysis Team, 2026-01-06, section "Contract Structure" / summary','load-forecast submission to PJM','contracted CAPACITY (not demand)','VERBATIM: "as of July 2025, the capacity value of these contracts is 47 GW (9.8 ESA + 7.1 CLOA + 30.1 ELOA)". ★★COMMITMENT LADDER: ESA (electric service agreement, firmest) 9.8 GW · CLOA (construction letter of authorization) 7.1 GW · ELOA (engineering letter of authorization - only site control + engineered site plan + $250k deposit, WEAKEST) 30.1 GW. So 64% of the headline sits at the weakest tier. ★DOMINION ITSELF WARNS AGAINST USING 47: "The Company is forecasting 16.6 GW of demand by 2046, not the 47 GW of capacity. This difference highlights the difference between capacity and demand." See the paired 16.6 GW row.','claude-opus-5 2026-07-28 (PDF fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('Exelon (ComEd/PECO/BGE/Pepco)','IL/PA/MD/DC/NJ','PJM',18,'2026-02-12','https://www.sec.gov/Archives/edgar/data/1109357/000110935726000061/exc-20260506ex992.htm','Exelon 8-K exhibit 99.2 filed 2026-05-06 (EDGAR), footnote (2) and Customer-Focused slide','8-K earnings exhibit (SEC)','COMMITTED data-centre pipeline','VERBATIM: "Committed data center pipeline of ~18 GW (excludes 1 GW of other large load projects) with ~45% secured with TSAs as of Q4 2025 call (February 12, 2026)". ★COMMITMENT LADDER AGAIN: only ~45% is secured with Transmission Security Agreements — same pattern as Dominion (64% at the weakest ELOA tier) and Oncor (271 raw vs 122 forecast). ★Secondary coverage claims a further ~43 GW in study phases and ComEd +19 GW by 2030 — NOT inserted, not in this exhibit. ★NOTE this is the ONE utility whose figure IS in an SEC filing — but in an 8-K EXHIBIT 99.2 (the earnings deck), not the 10-K/10-Q. That is where to look for the rest.','claude-opus-5 2026-07-28 (EDGAR exhibit fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('Dominion Energy Virginia','VA','PJM',16.6,'2026-01-06','https://www.pjm.com/-/media/DotCom/planning/res-adeq/load-forecast/dominion-documentation.pdf','Letter, Stan Blackwell (Director - Data Center Practice, Dominion Energy) to PJM Load Analysis Team, 2026-01-06, summary section','load-forecast submission to PJM','FORECAST DEMAND by 2046 (the citeable number)','VERBATIM: "The Company is forecasting 16.6 GW of demand by 2046, not the 47 GW of capacity." ★THIS is the demand figure Dominion stands behind; 47 GW is contracted capacity and is ~2.8x larger. Quote 16.6 for demand, 47 only as contracted capacity, and never interchangeably.','claude-opus-5 2026-07-28 (PDF fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
INSERT INTO utility_load_requests (utility,state,iso,gw_requested,as_of_date,source_url,source_title,filing_type,request_type,notes,verified_by) VALUES ('Georgia Power (Southern Company)','GA','SERC/Southern',8.5,'2026-07-28','https://www.georgiapower.com/about/company/filings/irp.html','Georgia Power, Integrated Resource Plan page, "Supporting Georgia''s strong economic growth"','IRP (2025, approved)','TOTAL system load growth — NOT data-centre-specific','VERBATIM: "Over the next six years, we project approximately 8,500 megawatts (MW) of electrical load growth-an increase of approximately 2,600 MW by the end of 2030 when compared to projections in the 2023 IRP Update." ★★DO NOT LABEL THIS DATA-CENTRE LOAD: the page says ELECTRICAL LOAD GROWTH with no data-centre attribution anywhere (the only "data center" string on the page is a nav link). Secondary coverage (Utility Dive) reports 33 data-centre projects / 11,332 MW REMOVED from the pipeline and ~9,900 MW sought via All-Source RFP — NOT inserted, not verified against a primary filing. ★Georgia PSC now requires quarterly Large Load Economic Development Reports, which is the primary source to mine for a true DC-specific figure.','claude-opus-5 2026-07-28 (page fetched + verbatim-quoted)') ON CONFLICT (utility, as_of_date, gw_requested) DO NOTHING;
