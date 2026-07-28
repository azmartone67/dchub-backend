#!/usr/bin/env python3
"""
load_lbnl_queue.py — one-time bulk load of the LBNL "Queued Up" interconnection
queue into `lbnl_queue` (2026-07-28).

★ WHAT THIS IS — AND WHAT IT IS NOT.
This is a GRID-HEADROOM feed, NOT a facility-discovery feed. I originally proposed
this workbook as an inventory source, claiming "utility large-load interconnection
queues name the data centre". That conflated two different datasets:

  · LBNL "Queued Up" (THIS file) = GENERATION interconnection — solar, wind, battery,
    gas, nuclear, storage seeking to SUPPLY the grid.
  · Utility large-load queues     = data-centre LOAD requests (AEP ~63 GW, Dominion
    ~48 GW). Published per-utility in rate filings/IRPs. NOT in this file.

Searching all 70,426 shared strings for "data cent*" returns exactly SIX hits, and
they are generation projects NAMED after nearby data centres ("NMRD Data Center II",
"Giga Texas Data Center"). So it discovers ~nothing and is deliberately not wired to
discovered_facilities.

What it IS worth is the headroom question DC Hub actually needs to answer: for a given
county, how much generation is queued, of what type, and what share historically
reaches commercial operation rather than withdrawing. Sheet "03. Complete Queue Data"
gives 36,441 requests with county+state+fips, poi_name (the interconnection SUBSTATION,
present on 36,427 rows — joinable to our substations layer), mw1/2/3 by type, utility,
region, q_status, and the date fields (on_date vs wd_date) that reveal built-vs-
withdrawn.

★NO COORDINATES AND NO VOLTAGE in this sheet. I first claimed both, having read
"latitude"/"longitude"/"interconnection_voltage_kv" out of the workbook's shared-
strings pool — that pool is GLOBAL across sheets and those names belong to the
"04. Data Codebook" sheet, which DESCRIBES the wider dataset. Verified against sheet
03's real 31-column header. Geography here is county-level only.

★The workbook is static (data "thru 2024"), so this is a one-time load, not a cron,
and idempotency is a full TRUNCATE + reinsert.
★NO natural-key PK: q_id is NOT unique — (q_id, region) collapses 36,441 rows to
33,702, so an upsert on it would have SILENTLY DROPPED 2,739 legitimate queue
entries. The dry run caught that; the table uses a surrogate BIGSERIAL id.

Usage:  DATABASE_URL=... python3 load_lbnl_queue.py [--apply]   (dry-run default)
"""
from __future__ import annotations

import os
import re
import sys
import zipfile

XLSX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "LBNL_Ix_Queue_Data_File_thru2024_v2.xlsx")
SHEET = "xl/worksheets/sheet6.xml"          # '03. Complete Queue Data' (rId6)

# ★CORRECTED 2026-07-28: sheet 03 has NO latitude/longitude/voltage/
# interconnecting_entity. Those names DO appear in the workbook's shared-strings
# pool — but that pool is GLOBAL across all sheets, and they belong to the
# "04. Data Codebook" sheet which DESCRIBES the wider dataset. I read the pool and
# attributed them to this sheet. Verified against the real header of sheet 03:
# 31 columns, no geo, no kV. What IS here is county+state+fips (joinable), poi_name
# on 36,427 of 36,441 rows (the interconnection SUBSTATION — joinable to our
# substations layer), mw1/2/3 by type, status, and the date fields that reveal
# built-vs-withdrawn.
WANT = ("q_id", "q_status", "q_date", "on_date", "wd_date", "ia_date",
        "IA_status_clean", "county", "state", "fips_codes", "poi_name", "region",
        "project_name", "utility", "developer", "entity", "service",
        "project_type", "type_clean", "mw1", "mw2", "mw3", "q_year")

DDL = """
CREATE TABLE IF NOT EXISTS lbnl_queue (
    -- ★Surrogate PK on purpose. q_id is NOT unique: (q_id, region) collapses
    -- 36,441 rows to 33,702, so a natural-key PK would have SILENTLY DROPPED
    -- 2,739 legitimate queue entries. The dry run caught it.
    id              BIGSERIAL PRIMARY KEY,
    q_id            TEXT,
    region          TEXT,
    q_status        TEXT,
    q_date          TEXT,
    on_date         TEXT,
    ia_date         TEXT,
    ia_status       TEXT,
    county          TEXT,
    state           TEXT,
    fips_codes      TEXT,
    wd_date         TEXT,
    poi_name        TEXT,
    project_name    TEXT,
    entity          TEXT,
    service         TEXT,
    mw3             DOUBLE PRECISION,
    utility         TEXT,
    developer       TEXT,
    project_type    TEXT,
    type_clean      TEXT,
    mw1             DOUBLE PRECISION,
    mw2             DOUBLE PRECISION,
    q_year          INTEGER,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_lbnl_state    ON lbnl_queue(state);
CREATE INDEX IF NOT EXISTS ix_lbnl_status   ON lbnl_queue(q_status);
CREATE INDEX IF NOT EXISTS ix_lbnl_poi      ON lbnl_queue(poi_name);
CREATE INDEX IF NOT EXISTS ix_lbnl_county   ON lbnl_queue(state, county);
"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def parse() -> tuple[list, list]:
    z = zipfile.ZipFile(XLSX)
    ss = [re.sub(r"<.*?>", "", v) for v in re.findall(
        r"<si>(.*?)</si>",
        z.read("xl/sharedStrings.xml").decode("utf-8", "replace"), re.S)]
    xml = z.read(SHEET).decode("utf-8", "replace")

    def cells(rowxml):
        """(ref, value) per cell, resolving shared strings. The t attribute can
        appear before or after r=, so match attributes as a block."""
        out = []
        for m in re.finditer(r"<c\s([^>]*)>(?:<v>(.*?)</v>)?", rowxml, re.S):
            attrs, val = m.group(1), m.group(2)
            ref = (re.search(r'r="([A-Z]+)\d+"', attrs) or [None, None])[1]
            is_s = 't="s"' in attrs
            if is_s and val is not None and val.isdigit():
                v = ss[int(val)] if int(val) < len(ss) else None
            else:
                v = val
            out.append((ref, v))
        return out

    rows = re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S)
    header, recs = None, []
    for r in rows:
        cs = cells(r)
        vals = {ref: v for ref, v in cs if ref}
        if header is None:
            names = [v for _, v in cs if v]
            if "q_id" in names:
                header = {}
                for ref, v in cs:
                    if ref and v:
                        header[v] = ref
            continue
        rec = {k: vals.get(header[k]) for k in WANT if k in header}
        if not rec.get("q_id"):
            continue
        recs.append(rec)
    return recs, sorted(header.keys()) if header else []


def main() -> int:
    apply = "--apply" in sys.argv
    recs, cols = parse()
    print(f"parsed {len(recs):,} queue rows · {len(cols)} columns available")
    with_poi = sum(1 for r in recs if r.get("poi_name"))
    with_mw = sum(1 for r in recs if _num(r.get("mw1")) is not None)
    print(f"  with POI substation: {with_poi:,}")
    print(f"  with MW1           : {with_mw:,}")
    st = {}
    for r in recs:
        st[r.get("state") or "?"] = st.get(r.get("state") or "?", 0) + 1
    print("  top states:", sorted(st.items(), key=lambda kv: -kv[1])[:8])
    if recs:
        s = recs[0]
        print("  sample:", {k: s.get(k) for k in
                            ("q_id", "state", "poi_name", "utility", "type_clean",
                             "mw1", "q_status")})
    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    dsn = os.environ.get("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    import psycopg2
    from psycopg2.extras import execute_values
    c = psycopg2.connect(dsn, sslmode="require", connect_timeout=10)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute(DDL)
            payload = [(
                r.get("q_id"), r.get("region"), r.get("q_status"), r.get("q_date"),
                r.get("on_date"), r.get("wd_date"), r.get("ia_date"),
                r.get("IA_status_clean"), r.get("county"), r.get("state"),
                r.get("fips_codes"), r.get("poi_name"), r.get("project_name"),
                r.get("entity"), r.get("service"), r.get("utility"),
                r.get("developer"), r.get("project_type"), r.get("type_clean"),
                _num(r.get("mw1")), _num(r.get("mw2")), _num(r.get("mw3")),
                _int(r.get("q_year"))) for r in recs]
            # The workbook is a static snapshot ("thru 2024"), so a full replace is
            # the correct idempotency model — an upsert needs a unique key and q_id
            # is not one.
            cur.execute("TRUNCATE lbnl_queue")
            # dedupe on the PK within the batch — ON CONFLICT cannot fix a batch
            # that contains the same key twice ("cannot affect row a second time").
            uniq = payload
            print(f"  writing all {len(uniq):,} rows (no dedupe — q_id is not unique)")
            execute_values(cur, """
                INSERT INTO lbnl_queue
                  (q_id, region, q_status, q_date, on_date, wd_date, ia_date,
                   ia_status, county, state, fips_codes, poi_name, project_name,
                   entity, service, utility, developer, project_type, type_clean,
                   mw1, mw2, mw3, q_year)
                VALUES %s""", uniq, page_size=500)
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT poi_name), "
                        "COUNT(DISTINCT state||'|'||COALESCE(county,'')) "
                        "FROM lbnl_queue")
            n, p, cc = cur.fetchone()
            print(f"\nAFTER: {n:,} rows · {p:,} distinct POI substations · "
                  f"{cc:,} county-state pairs")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
