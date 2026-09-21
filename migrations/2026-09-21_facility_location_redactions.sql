-- Operator-requested location redaction (2026-09-21).
--
-- An operator can ask that the exact location of a facility — street address
-- and coordinates — not be published, typically for physical-security reasons.
-- The facility stays listed (name, operator, city, region, country, status);
-- nothing more precise than the city is kept on the rows the site reads.
--
-- ★ Why a write-side trigger and not a filter in the readers. Facility
-- coordinates are read by dozens of surfaces — the HTML page, its JSON twin,
-- REST, MCP, map layers, exports, carrier presence — and written by several
-- loaders (PeeringDB, OSM, geo repair, slug freeze). A per-reader filter is a
-- list that is always one reader short, and a loader re-syncing the upstream
-- record would quietly put the value back. Enforcing it on write, the same way
-- df_normalize_source enforces its invariant, makes it hold for every writer,
-- including ones added later. The readers only need to SAY the location is
-- withheld (routes/location_redaction.py); they cannot leak what is not stored.
--
-- ★ The registry holds identities only, never the location itself:
--     df:<discovered_facilities.id>      f:<facilities.id>
--     slug:<canonical_slug>
--     src:<lower(source)>:<source_id>    (PeeringDB ids without the pdb_ prefix,
--                                         matching df_normalize_source)
--     url:<source_url>
-- A redaction should list every identity it knows, so a re-ingest that mints a
-- new row id for the same upstream object is still caught by src:/url:.
--
-- Idempotent. Apply with psql inside one transaction (lock_timeout needs one).
-- Triggers are named trg_zz_* so they fire AFTER the existing BEFORE triggers
-- (PostgreSQL fires same-event triggers in name order): the source/slug
-- normalisers run first, and nothing that runs later can re-set a coordinate.

CREATE TABLE IF NOT EXISTS facility_location_redactions (
    match_key   TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT 'operator_request',
    note        TEXT,                               -- private; never served
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION facility_location_redaction_keys(
        id_prefix TEXT, row_id TEXT, canonical_slug TEXT,
        source TEXT, source_id TEXT, source_url TEXT)
RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $fn$
    -- A NULL part makes its element NULL, and a NULL element never matches.
    SELECT ARRAY[
        id_prefix || ':' || btrim(row_id),
        'slug:' || NULLIF(btrim(canonical_slug), ''),
        'src:' || lower(btrim(source)) || ':'
               || regexp_replace(NULLIF(btrim(source_id), ''), '^pdb_', ''),
        'url:' || NULLIF(btrim(source_url), '')
    ]
$fn$;

CREATE OR REPLACE FUNCTION facility_location_is_redacted(keys TEXT[])
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT EXISTS (SELECT 1 FROM facility_location_redactions
                    WHERE match_key = ANY (keys))
$fn$;

CREATE OR REPLACE FUNCTION df_location_redaction_guard() RETURNS trigger AS $fn$
BEGIN
    IF facility_location_is_redacted(facility_location_redaction_keys(
           'df', NEW.id::text, NEW.canonical_slug,
           NEW.source, NEW.source_id, NEW.source_url)) THEN
        NEW.latitude        := NULL;
        NEW.longitude       := NULL;
        NEW.address         := NULL;
        -- An OSM node URL opens a map at the point; raw_data is the upstream
        -- record verbatim (PeeringDB address1/zipcode/latitude/longitude).
        NEW.source_url      := NULL;
        NEW.raw_data        := NULL;
        NEW.substation_band := NULL;    -- derived from the coordinates
    END IF;
    RETURN NEW;
END $fn$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION f_location_redaction_guard() RETURNS trigger AS $fn$
BEGIN
    IF facility_location_is_redacted(facility_location_redaction_keys(
           'f', NEW.id, NEW.canonical_slug,
           NEW.source, NEW.source_id, NEW.source_url)) THEN
        NEW.latitude        := NULL;
        NEW.longitude       := NULL;
        NEW.lat             := NULL;    -- facilities carries both pairs
        NEW.lon             := NULL;
        NEW.address         := NULL;
        NEW.source_url      := NULL;
        NEW.raw_data        := NULL;
        NEW.substation_band := NULL;
    END IF;
    RETURN NEW;
END $fn$ LANGUAGE plpgsql;

-- carrier_facility_presence stores the coordinates of the DC Hub facility a
-- PeeringDB carrier list was attached to. facility_pdb_id is either the
-- PeeringDB facility itself ('11014'-style, its own point) or
-- 'nearby-<pdb>-<dchub id>' (the DC Hub row's point). Only the PLAIN form is
-- matched as a PeeringDB identity: a 'nearby-' row carries a NEIGHBOUR's
-- coordinates, which are that neighbour's to publish; its dchub_facility_id
-- decides it instead.
CREATE OR REPLACE FUNCTION cfp_location_redaction_guard() RETURNS trigger AS $fn$
BEGIN
    IF facility_location_is_redacted(ARRAY[
           'df:' || btrim(NEW.dchub_facility_id),
           'f:'  || btrim(NEW.dchub_facility_id),
           'src:peeringdb:' || NULLIF(btrim(NEW.facility_pdb_id), '')]) THEN
        NEW.facility_lat := NULL;
        NEW.facility_lng := NULL;
    END IF;
    RETURN NEW;
END $fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_zz_location_redaction ON discovered_facilities;
CREATE TRIGGER trg_zz_location_redaction
    BEFORE INSERT OR UPDATE ON discovered_facilities
    FOR EACH ROW EXECUTE FUNCTION df_location_redaction_guard();

DROP TRIGGER IF EXISTS trg_zz_location_redaction ON facilities;
CREATE TRIGGER trg_zz_location_redaction
    BEFORE INSERT OR UPDATE ON facilities
    FOR EACH ROW EXECUTE FUNCTION f_location_redaction_guard();

DROP TRIGGER IF EXISTS trg_zz_location_redaction ON carrier_facility_presence;
CREATE TRIGGER trg_zz_location_redaction
    BEFORE INSERT OR UPDATE ON carrier_facility_presence
    FOR EACH ROW EXECUTE FUNCTION cfp_location_redaction_guard();

-- Adding a redaction: INSERT its match keys, then touch the matching rows so
-- the triggers clear what is already stored, e.g.
--   UPDATE discovered_facilities SET latitude = latitude
--    WHERE facility_location_is_redacted(facility_location_redaction_keys(
--              'df', id::text, canonical_slug, source, source_id, source_url));
-- (same for facilities with 'f', and carrier_facility_presence by id), then
-- purge the facility's URLs at the edge. The rows are data, not code: keep
-- them out of this public repository.
