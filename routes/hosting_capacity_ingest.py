"""Shell #35 (2026-07-26) — utility FEEDER hosting-capacity ingest.

Turns "can I get N MW near this point" from substation-proximity guess
into utility-published feeder truth. v1 sources = the two VERIFIED open
public FeatureServers (survey 2026-07-26):

  * PHI (Pepco/Delmarva/ACE, Exelon) — kW-precise feeder hosting capacity
    with FeederID/Substation/voltage/queued-gen and per-row update dates.
  * National Grid New York SDP — MW feeder max/min HC, NYISO zone, stable
    utility-hosted URL.

Explicitly SKIPPED on terms/access grounds: Georgia Power (tool terms
mark results confidential), PG&E (registration-gated). Dominion VA
public layer is binned with no feeder id — candidate v1.5 overlay, not
ingested here. ★ The original "Duke + ComEd (secured-proxy 403)" and
"SCE (registration-gated)" entries are STALE and all three now ship:
Duke Ohio and SCE answer anonymously, and ComEd's 403 was header-only
(see the ComEd entry's recorded access attempts).

Trigger: called (weekly-gated, budget-capped, daemon-safe) from
depth_master_shell._act_hosting_capacity — no new cron. Kill switch:
HOSTING_CAPACITY_INGEST_DISABLE=1.

Serving: feeders_near(lat, lng, radius_km) powers the feeder_hosting
block in /api/v1/grid/hosting-capacity (fail-soft: endpoint keeps its
substation-proximity answer when no feeder rows are near).

── LOAD expansion, tier 1 (probed + re-probed 2026-07-29) ─────────────
capacity_type is the whole point of this table: `load` is what a NEW
DATA-CENTRE LOAD CAN DRAW (the question that converts), `gen` is DER
export headroom (never relayable as siteable load), `bus_headroom` is
transmission bus MW. Every SOURCES entry now DECLARES its type — the
old implicit `.get("capacity_type", "gen")` default meant a new entry
could inherit a type nobody chose. check_source_contract() refuses to
ingest a source that does not declare one, and refuses a `load` source
whose capacity field is a known GENERATION field name.

Added here:
  * Dominion Energy VA EV/load layer — Loudoun/NoVA + Richmond, the
    largest data-centre market on earth. MW, precise to 0.01, no feeder
    id (spatial join only). ★ The layer is DISSOLVED ON ATTRIBUTES:
    5,546 rows == 5,546 distinct (LIMIT_VAL, PRI_NOM_VOLT_CD,
    OPERATINGVOLTAGE) triples, and 35/40 sampled rows are MULTIPART
    (OBJECTID 1 = 38 paths spanning 3.1 deg lon x 2.8 deg lat) — so
    _rep_point()'s paths[0] midpoint would have discarded ~95% of the
    territory and pinned each value at a random spot. explode_multipart
    fixes it. Kept STRICTLY SEPARATE from the shipped `dominion_va`
    entry, which is a different (binned, gen) layer.
  * SCE DRPEP/ICA Uniform Load — LA basin, Inland Empire, Orange
    County. ★ Order by the Double `..._legend`, never the String
    twin: verified live that ordering by the string returns 'NA' rows
    first (lexicographic), which with capacity-DESC paging would have
    filled the whole cap with UNMEASURED rows.

NOT added — no endpoint exists to point config at:
  * FirstEnergy Ohio (Ohio Edison / Illuminating / Toledo Edison). The
    only map link on the official page is gisdxportal.fenetwork.com,
    NXDOMAIN on public DNS (re-checked 2026-07-29; google.com resolved
    in the same run), and the AGOL item is inaccessible. A config
    pointing at an NXDOMAIN host fails silently every run, so there is
    no entry. Needs a human to ask FirstEnergy for the public URL.

── AUTHORISED sources (probed 2026-07-29) ─────────────────────────────
Three entries whose publishers authorise the use, each carrying a trap
that only config can defuse:
  * nvenergy_lhc (load) — the GENERATION twin GHC and the photovoltaic
    twin PHC are Doubles in the SAME ROW as LHC; only the field name
    stops the swap. LHC=20.0 is a CENSORING CEILING ("Over 20MW"),
    proven by counting (200 rows at exactly 20, none above), so those
    rows are EXCLUDED rather than published as a maximum. Rows are line
    sections, 781.5 per feeder, folded on SYN_FEEDER_ID — and the fold
    is NOT lossless, which the basis string says out loud.
  * nvenergy_gna (bus_headroom) — a DEFICIENCY register. Flag only: no
    number is stored, because every numeric-looking column is a
    formatted STRING ('11,660 kVA' → _num() reads 0.011 MW) and the one
    positive column describes a PLANNED PROJECT AT ANOTHER FACILITY.
  * comed_ev_load (load) — reachable only with a Referer, which never
    travels without our own identifying User-Agent. Layer 28 is the
    circuit grain; 29-32 are PLSS tiles carrying the highest circuit
    value in each tile, which overstates any specific site. The service
    NAME AND ITEM ID BOTH ROTATE MONTHLY, so it has a real runtime
    url_resolver instead of a pin that dies as a silent empty ingest.
Attribution is EXACTLY "NV Energy" on both NV entries — the string the
SERVICE ROOT publishes, where the layer endpoints publish an empty one.

── SDG&E, the second CPUC ICA load layer (probed 2026-07-30) ──────────
  * sdge_ica_load (load) — San Diego, 497,700 rows: the LARGEST single load
    source in this table. Found by searching the ArcGIS Online catalog for
    owner:SDGE_ICA rather than by guessing a URL, which also surfaced that
    the PROD layer (ICA_MAP_PROD_*) is the one to read, not the QA twin.
    Three traps, all measured before the entry was written:
      - 10 MW is a CENSORING CEILING (1,827 rows at exactly 10, ZERO above,
        0.1-step values below). Excluded, not published as a maximum — the
        same call as nvenergy_lhc's "Over 20MW".
      - FOUR generation Doubles share the row with the load field; all four
        are now in _GEN_ONLY_FIELDS.
      - Rows are ICA GRID CELLS at 771.6 per circuit. ★ The ratio had to be
        ENUMERATED by paged groupBy: this service silently ignores
        multi-field returnDistinctValues and reported 497,700 distinct
        (SUBID, CIRCUIT_NAME) pairs — the row count, and impossible for 645
        circuits over 103 substations. Believing it would have turned every
        row count into a feeder count.
    Also recorded: WOF and WNOF load are IDENTICAL on all 497,700 rows (so
    neither is the conservative read), LABELTEXT_LCA is display binning whose
    top bucket collapses 2 MW to the ceiling, and the service publishes NO
    copyright string at layer or root — so the attribution names the
    publisher and says so, rather than inventing a licence.

★ WHY THE US GAP IS NOT AN INGEST BACKLOG (catalog sweep, 2026-07-30).
Searched the ArcGIS Online catalog for a public capacity feature service in
every major uncovered data-centre market. Oncor 0 · CenterPoint 0 · APS 0 ·
SRP 0 · Duke Carolinas 0 · Evergy 0 · Puget Sound 0 · Portland General 0 ·
Dominion SC 0 · TVA 0. Hosting-capacity maps exist where a PUC ordered them;
Texas, Arizona and the Southeast publish nothing, so no amount of ingest work
closes those markets. Do not file them as TODOs — they are absent upstream.
Still deliberately skipped on terms grounds, unchanged: Georgia Power (tool
terms mark results confidential) and PG&E (registration-gated; the only
PGE_ICA item on the public catalog is a THIRD-PARTY copy, and ingesting that
would launder a gated source).

Budget note: the two load layers are big (Dominion ~104k exploded
rows, SCE 637,977 usable). _ingest_order() runs LOAD sources FIRST so
a tight HOSTING_CAPACITY_INGEST_BUDGET_S starves gen refreshes rather
than the load answer; anything the budget does not reach is recorded
as status "budget_exhausted", never as a successful empty ingest. A
full backfill wants HOSTING_CAPACITY_INGEST_BUDGET_S=900 and force=1.
"""

from __future__ import annotations

import os
import json
import math
import time
import logging
import datetime

logger = logging.getLogger(__name__)

# ★ Every request carries this, and it must stay honest: it names us, links a
# page about us, and gives an operator a way to reach a human. ComEd's service
# needs a Referer to answer at all (below), and a Referer without an
# identifying UA is a request that hides who is making it — so the UA is set
# FIRST in _fetch_pages and a per-source `headers` block must never override it.
_UA = ("DCHub-GridData/1.0 (+https://dchub.cloud; public-gis-ingest; "
       "contact: info@dchub.cloud)")
_BUDGET_S = float(os.environ.get("HOSTING_CAPACITY_INGEST_BUDGET_S", "300"))
_PAGE_SIZE = 2000
_MAX_ROWS_PER_SOURCE = int(os.environ.get("HOSTING_CAPACITY_MAX_ROWS", "20000"))
_GATE_DAYS = 6

# The only three things a row in this table is allowed to mean. Anything
# else is a mislabelled capacity, which is worse than no row at all.
_ALLOWED_CAPACITY_TYPES = ("load", "gen", "bus_headroom")

# ISO-3166-1 alpha-2, and an EXPLICIT allow-list rather than a regex, so adding a
# geography is a decision someone made rather than a typo that validated.
# ★ Why this column exists at all: /coverage sums total_feeders across every row.
# Before `country`, ingesting a non-US utility would have silently folded NZ/AU/UK
# feeders into a figure every US-facing consumer reads as the US footprint — the
# same unit-mixing defect as publishing GIS rows as feeders, one axis over.
# total_feeders therefore stays US-ONLY and the rest are reported separately;
# see hosting_capacity_coverage_endpoint().
_ALLOWED_COUNTRIES = ("US", "NZ", "AU", "GB", "CA")

# Field names that are DER/GENERATION capacity wherever they appear. If a
# source declares capacity_type "load" and maps one of these into mw_max /
# mw_min, it is a mis-wire and check_source_contract() refuses the source.
# ★ SCE is the live trap: on L0 the gen fields uniform_generation and
# ica_overall_pv sit in the SAME ROW as the load field, both Doubles, both
# would map cleanly — nothing but this list stops the swap.
# (a plain set literal, not frozenset(...) — see the SOURCES note below:
# the guard test reads these by ast.literal_eval, which a call breaks)
_GEN_ONLY_FIELDS = {
    "uniform_generation", "uniform_generation_static_grid",
    "uniform_generation_static_grid_legend", "ica_overall_pv",
    "ica_overall_gen", "uniform_generation_op_flex",
    # ★ SDG&E is the SCE trap again, four times over: on
    # ICA_MAP_PROD_LoadCapacityGrids_VW these four generation Doubles sit in
    # the SAME ROW as ICAWOF_UNILOAD and would all map cleanly into mw_max.
    # Nothing but these names stops a gen figure shipping as siteable load.
    "ICAWOF_UNIGENERATION", "ICAWNOF_UNIGENERATION",
    "ICAWOF_PVGENERATION", "ICAWNOF_PVGENERATION",
}

# Sources whose ROWS ARE NOT FEEDERS, and the knob that must be declared to
# handle it. Publishing a row count as a feeder count is the standing defect
# in this table (documented at ~15x Ameren to ~29x Rhode Island); these are
# the sources where it would be worst, so the contract check enforces it.
#   knob "explode_multipart" — one row aggregates many disjoint line runs
#   knob "feeder_field"      — fields["feeder"] must be set so distinct
#                              feeders can be counted separately from rows
#   knob "dedupe_key"        — identical attributes repeated per GIS vertex
_ROW_NOT_FEEDER_SOURCES = {
    # 5,546 rows == 5,546 distinct attribute triples: every row is the union
    # of every segment sharing a capacity+voltage combination (measured
    # 2026-07-29 via returnDistinctValues+returnCountOnly).
    "dominion_va_ev_load": ("attribute_dissolved_multipart", "explode_multipart"),
    # 676,467 rows -> 3,723 circuits (~172 line sections per circuit).
    "sce_ica_load": ("line_sections", "feeder_field"),
    # 497,700 rows -> 645 circuits = 771.6 GRID CELLS per circuit, the
    # second-worst ratio here. ENUMERATED by paged groupBy over
    # (SUBID, CIRCUIT_NAME) on 2026-07-30 — the single-shot
    # returnDistinctValues+returnCountOnly returned 497,700 (the ROW count),
    # impossible for 645 circuits over 103 substations, because this service
    # silently ignores multi-field distinct counting. A believed 1.0x would
    # have made every row count a feeder count.
    "sdge_ica_load": ("ica_grid_cells", "feeder_field"),
    # 287,307 rows -> 2,317 distinct base_circuitid. ★ MEASURED 123.5x, NOT
    # the ~285x carried in the survey brief — the brief's figure is 2.3x too
    # high. Enumerated on 2026-07-29 via paged groupBy covering all 2,317
    # circuits (rows/circuit min 1, median 83, max 730). Recorded because a
    # wrong inflation factor is how a row count gets "corrected" back into a
    # fabricated feeder count.
    "pseg_nj_load": ("gis_vertices", "dedupe_key"),
    # 977,693 rows -> 1,251 distinct SYN_FEEDER_ID = 781.5 LINE SECTIONS per
    # feeder, the worst ratio in this table (measured 2026-07-29 on layer 31:
    # returnCountOnly 977,693; returnDistinctValues+returnCountOnly on
    # DRP_GIS_DATA.DRP_SECTIONS.SYN_FEEDER_ID 1,251). One row per DMS_LINK_ID
    # section, so an unfolded row count would inflate a 1,251-feeder territory
    # into a 977k-feeder one.
    "nvenergy_lhc": ("line_sections", "dedupe_key"),
    # 19,764 source rows -> 197 buses (100.3x, measured 2026-07-30): each row
    # is one (bus × study size × limiting element × contingency) tuple from a
    # generation-interconnection study, and the point-only key collapsed them
    # to the per-bus MAXIMUM under the capacity-DESC crawl (bus 'Huetter'
    # stored at 200 MW while failing 2 of its 10 constraints at 20 MW — read
    # back from the live table). key_extra keeps every constraint row its own
    # key; fields["feeder"] stays None so no row count is ever a feeder count.
    "avista_bus": ("per_constraint_study_rows", "key_extra"),
}

# Sources whose publisher CLIPS the study at a ceiling, so the rows sitting on
# that ceiling are CENSORED ("Over 20MW", "Above 2") rather than measured. Those
# rows must be EXCLUDED by the source's `where`, never published as a maximum:
# doing so states an unmeasured quantity as a number AND understates the very
# circuits a large load cares about most.
#
# Two instances now, from two different utilities — so this is a CLASS, not a
# quirk, and it gets a registry instead of a second hand-written special case.
# The evidence shape that identifies one: a pile of rows at exactly the max with
# ZERO above it, on a field whose values otherwise step continuously.
#
#   key -> (field, ceiling, evidence)
_CENSORING_CEILINGS = {
    # 200 sections at exactly 20.0, none above; NV labels it "Over 20MW".
    "nvenergy_lhc": ("DRP_GIS_DATA.DRP_HCA.LHC", 20,
                     "200 sections at exactly 20.0, 0 above; publisher label "
                     "'Over 20MW'"),
    # Measured 2026-07-30: >10 = 0 rows, =10 = 1,827 rows, and the values below
    # step 9.5/9.6/9.7/9.8/9.9 — a 0.1 scale that stops dead at 10.
    "sdge_ica_load": ("ICAWOF_UNILOAD", 10,
                      "1,827 cells at exactly 10, 0 above, 0.1-step values "
                      "below (9.5-9.9); published ceiling becomes 9.9"),
}

# Cap on parts exploded from one multipart feature — a backstop against a
# pathological geometry, NOT a sampling knob.
# ★ Set from measurement, not taste. Dominion path counts over 900 features
# (2026-07-29, ordered capacity-DESC): 168 single-part, 725 at 2-99, 6 at
# 100-999, and ONE at 3,086 — OBJECTID 5545, which is LIMIT_VAL = 24, the
# TOP capacity value in the layer. A 2000 cap (the first value tried here)
# silently deleted 1,086 parts of the single most valuable row on the map.
# The row cap is the real budget backstop; this one only has to be above any
# honest geometry. Anything dropped is COUNTED and surfaced as parts_dropped.
_MAX_PARTS_PER_FEATURE = 20000

# Per-source row-cap overrides live HERE, not inside SOURCES, because
# ★ SOURCES MUST STAY A PURE LITERAL: the pre-merge guard test reads it with
# ast.literal_eval rather than importing this module (importing it registers a
# Flask blueprint and reads env). One os.environ.get() inside the list makes
# the whole table unreadable to the guard and the contract goes unchecked.
_MAX_ROWS_OVERRIDE = {
    "dominion_va_ev_load": int(
        os.environ.get("HOSTING_CAPACITY_DOMINION_EV_MAX_ROWS", "0")) or None,
    "sce_ica_load": int(
        os.environ.get("HOSTING_CAPACITY_SCE_MAX_ROWS", "0")) or None,
    # 495,873 usable cells (497,700 minus the 1,827 censored at the 10 MW
    # clip) over 645 circuits. The default below is the capacity-DESC head;
    # raise this for a fuller backfill (which really wants sharding by SUBID).
    "sdge_ica_load": int(
        os.environ.get("HOSTING_CAPACITY_SDGE_MAX_ROWS", "0")) or None,
    # 287,307 vertices / 2,317 circuits. The default 40k is the capacity-DESC
    # HEAD, so a routine run holds the highest-capacity circuits; raise this
    # for a complete backfill (which really wants sharding by base_circuitid).
    "pseg_nj_load": int(
        os.environ.get("HOSTING_CAPACITY_PSEG_MAX_ROWS", "0")) or None,
    # 70,541 flagged segments. Flag-only rows, so a partial sample is honest
    # (present-only signal) but never an all-clear — see _disqualified_near().
    "ladwp_no_capacity": int(
        os.environ.get("HOSTING_CAPACITY_LADWP_MAX_ROWS", "0")) or None,
    # 977,493 usable sections (977,693 minus the 200 censored at LHC=20) over
    # 1,251 feeders. The default below is the capacity-DESC head; raise this
    # for a fuller backfill (which really wants sharding by SYN_SUB_NAME).
    "nvenergy_lhc": int(
        os.environ.get("HOSTING_CAPACITY_NVENERGY_MAX_ROWS", "0")) or None,
}

SOURCES = [
    {"utility": "PHI (Pepco/Delmarva/ACE)",
     "key": "phi",
     "capacity_type": "gen",   # Feeder_Large_Gen_HC = DER export headroom
     "country": "US",
     "url": ("https://services3.arcgis.com/agWTKEK7X5K1Bx7o/arcgis/rest/"
             "services/PHI_Hosting_Capacity_Public/FeatureServer/0/query"),
     "fields": {"feeder": "FeederID", "substation": "Substation",
                "state": "State", "region": "Region", "voltage_kv": "Voltage",
                "mw_max": ("Feeder_Large_Gen_HC", 0.001),   # kW → MW
                "mw_min": None,
                "queued_kw": "Total_Pending_Gen_kW",
                "updated": "Last_Updated"}},
    {"utility": "National Grid NY",
     "key": "ngrid_ny",
     "capacity_type": "gen",   # NYSDP feeder_max_hc = DER hosting capacity
     "country": "US",
     "url": ("https://systemdataportal.nationalgrid.com/arcgis/rest/"
             "services/NYSDP/Hosting_Capacity_Data/MapServer/0/query"),
     "fields": {"feeder": "Master_CDF", "substation": None,
                "state": None, "region": "nyiso_load_zone",
                "voltage_kv": "feeder_voltage",
                "mw_max": ("feeder_max_hc", 1.0),
                "mw_min": ("feeder_min_hc", 1.0),
                "queued_kw": None,
                "updated": "hca_refresh_date"}},
    # ── WS9 expansion (2026-07-27 probes, all sample-verified) ──────────
    # Dominion VA: THE NoVA market. Public layer is BINNED (LIMIT_VAL map
    # class, no feeder id) — ingested honestly as approximate class-MW.
    # ★★ THIS IS THE GEN LAYER. Its sibling `dominion_va_ev_load` below is a
    # DIFFERENT service (EV_Hosting_Capacity_Available_EB L14) and is LOAD.
    # Keep them distinct: same utility, opposite meaning. Dominion publishes
    # three hosting-capacity services — Primary (utility-scale DER, this
    # one), Residential (DER), and EV (new charging LOAD).
    {"utility": "Dominion Energy VA (binned)",
     "key": "dominion_va",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services.arcgis.com/DmE6Z8jKWf8lv84J/arcgis/rest/"
             "services/Primary_Hosting_Capacity_Available_EB/"
             "FeatureServer/6/query"),
     "fields": {"feeder": None, "substation": None,
                "state": None, "region": None,
                "voltage_kv": ("Line_Voltage", 0.001),   # volts → kV
                "mw_max": ("LIMIT_VAL", 1.0),            # binned class value
                "mw_min": None, "queued_kw": None, "updated": None}},
    {"utility": "Con Edison NY",
     "key": "coned",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services.arcgis.com/ciPnsNFi1JLWVjva/arcgis/rest/"
             "services/CECONY_NodalHCV_Prod/FeatureServer/0/query"),
     "fields": {"feeder": "FEEDER_ID", "substation": "FRIENDLY_CIRCUIT_NAME",
                "state": None, "region": "NYISO_LOAD_ZONE",
                "voltage_kv": "LOCAL_VOLTAGE",
                "mw_max": ("LOCAL_MAX", 1.0),
                "mw_min": ("LOCAL_MIN", 1.0),
                "queued_kw": None, "updated": "HC_REFESH_DATE"}},
    {"utility": "Orange & Rockland NY",
     "key": "oru",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services.arcgis.com/ciPnsNFi1JLWVjva/arcgis/rest/"
             "services/ORU_NodalHCV_Prod/FeatureServer/0/query"),
     # ORU sibling uses CIRCUIT (not FEEDER_ID) — probed 2026-07-27.
     "fields": {"feeder": "CIRCUIT", "substation": None,
                "state": None, "region": "NYISO_LOAD_ZONE",
                "voltage_kv": "LOCAL_VOLTAGE",
                "mw_max": ("LOCAL_MAX", 1.0),
                "mw_min": ("LOCAL_MIN", 1.0),
                "queued_kw": None, "updated": "HC_REFESH_DATE"}},
    {"utility": "NYSEG/RG&E",
     "key": "nyseg_rge",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services.arcgis.com/c0HK6TaWF3mGiNhc/arcgis/rest/"
             "services/NY_Nodal_HC_HFS/FeatureServer/0/query"),
     "fields": {"feeder": "circuit_1", "substation": "SUBSTATION",
                "state": None, "region": "Zone",
                "voltage_kv": "VOLTAGE",
                "mw_max": ("MAX_hostin", 1.0),
                "mw_min": ("MIN_hostin", 1.0),
                "queued_kw": None, "updated": "HCA_Date"}},
    {"utility": "Rhode Island Energy",
     "key": "ri_energy",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services.arcgis.com/NTSXKyJwdnK9ffCb/arcgis/rest/"
             "services/RI_Hosting_Capacity_2025/FeatureServer/0/query"),
     "fields": {"feeder": "Network_ID", "substation": "Substation",
                "state": None, "region": "Area",
                "voltage_kv": "Voltage",
                "mw_max": ("HC", 1.0),   # official criteria-constrained MW
                "mw_min": None, "queued_kw": None,
                "updated": "DG_Refresh_Date"}},
    # BGE (Baltimore Gas & Electric, Exelon — same AGOL org as PHI).
    # Layer 37 = finest grid (37.7k polys, 25.2k with >100kW remaining).
    # NOTE: MAP_NAME is a map-grid tile id, NOT a feeder id — left unset
    # rather than mislabeled. Capacity = REMAINING hosting capacity (kW).
    {"utility": "BGE (Baltimore)",
     "key": "bge",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services3.arcgis.com/agWTKEK7X5K1Bx7o/arcgis/rest/"
             "services/BGE_HOSTING_CAPACITY_AGOL/FeatureServer/37/query"),
     "fields": {"feeder": None, "substation": None,
                "state": None, "region": None, "voltage_kv": None,
                "mw_max": ("Max_Hosting_Capacity_Remaining_kW", 0.001),
                "mw_min": ("Min_Hosting_Capacity_Remaining_kW", 0.001),
                "queued_kw": "Sum_DER_Installed_and_approved_kW",
                "updated": None}},
    # Xcel NSP (MN/ND/SD): service is RENAMED MONTHLY — try candidates,
    # first that yields rows wins. ★ Values are MW (not kW) despite the
    # kW-named DG columns in the same row (verified 2026-07-27).
    {"utility": "Xcel NSP (MN/ND/SD)",
     "key": "xcel_nsp",
     "capacity_type": "gen",
     "country": "US",
     "url_candidates": [
        ("https://services1.arcgis.com/eM84fwjsSggLQk61/arcgis/rest/"
         "services/NSP_HCA_Blurred_GEN_Popup_July_2026/FeatureServer/0/query"),
        ("https://services1.arcgis.com/eM84fwjsSggLQk61/arcgis/rest/"
         "services/NSP_HCA_Popup_June_2026/FeatureServer/0/query"),
     ],
     "fields": {"feeder": "Feeder", "substation": "Substation",
                "state": None, "region": None,
                "voltage_kv": "NominalVoltage",
                "mw_max": ("MaxHostingCap", 1.0),
                "mw_min": ("MinHostingCap", 1.0),
                "queued_kw": "FeederQueuedDG",
                "updated": "NewQrtDataCuttoff"}},
    # ── WS11 (2026-07-27 verified probes) ───────────────────────────────
    {"utility": "Xcel PSCO (Colorado)",
     "key": "xcel_psco",
     "capacity_type": "gen",
     "country": "US",
     "url_candidates": [
        ("https://services1.arcgis.com/eM84fwjsSggLQk61/arcgis/rest/"
         "services/PSCO_Blurred_Popup_GEN_June_2026/FeatureServer/0/query"),
     ],
     "fields": {"feeder": "Circuit", "substation": "Substation",
                "state": None, "region": None, "voltage_kv": "NOMINAL_VO",
                "mw_max": ("Maximum__MW_", 1.0),
                "mw_min": ("Minimum__MW_", 1.0),
                "queued_kw": "fddg_kva", "updated": "Data_Cutoff"}},
    # ★★ LOAD-side hosting capacity (what a DATA CENTER actually needs —
    # most utility maps publish GENERATION HC). AEP covers Columbus OH.
    {"utility": "AEP Ohio & I&M (load)",
     "key": "aep_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services.arcgis.com/ZnwBsu4Q8SvSAofV/arcgis/rest/"
             "services/PROD_MI_HC_GRID/FeatureServer/0/query"),
     "fields": {"feeder": "CIRCUITID", "substation": "SUBSTATION",
                "state": "STATE_ABBR", "region": None,
                "voltage_kv": "CIRCUIT_VOLTAGE_CLASS",
                "mw_max": ("MAX_HCLOAD", 0.001),          # kW → MW
                "mw_min": ("HCLOAD", 0.001),
                "queued_kw": "DER_QUEUED_CAPACITY",
                "updated": "LAST_REFRESH_DATE"}},
    {"utility": "Ameren Illinois (load)",
     "key": "ameren_il_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services5.arcgis.com/3jEEGnl6c1x9Sze7/arcgis/rest/"
             "services/AIC_LC_Grids/FeatureServer/0/query"),
     # MAXLOADMW_TXT is a STRING holding MW — _num() casts it.
     "fields": {"feeder": "FEEDERID", "substation": None,
                "state": None, "region": None,
                "voltage_kv": "OPERATINGVOLTAGE",
                "mw_max": ("MAXLOADMW_TXT", 1.0),
                "mw_min": None, "queued_kw": None, "updated": None}},
    {"utility": "DTE Electric (MI)",
     "key": "dte",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services.arcgis.com/jVbCQRNRZvxyQyrx/arcgis/rest/"
             "services/HCA_June_2023/FeatureServer/0/query"),
     # Service NAME says 2023 but Date_of_La verified 2026-04-14.
     "fields": {"feeder": "Circuit_1", "substation": None,
                "state": None, "region": None, "voltage_kv": "Voltage__k",
                "mw_max": ("Hosting__1", 0.001),          # kW → MW
                "mw_min": None, "queued_kw": "Installed",
                "updated": "Date_of_La"}},
    # ★★ AVISTA — RETYPED bus_headroom → gen, 2026-07-30. This shipped as
    # "Avista (bus headroom)" ("transmission bus MW available"), and it is
    # NOT that. Per the publisher's own AGOL item metadata (item
    # 34c5773cf6dc44a798b300d0ebab0ecb: snippet "Contains data used by
    # Generation Interconnection maps and apps", description "Release:
    # 2026.GenerationInterconnectionHeatMap.R1"), the layer is a
    # GENERATION-interconnection transfer study — "can I inject", not what
    # a load can draw. The field list corroborates it (Direction_Source,
    # Trans_Lim, Percent_OTDF, Limiting_CTG = transfer-capability/OTDF
    # language). We hold ZERO measured transmission LOAD capability.
    # ★★ MW_Available IS A PASS/FAIL FLAG, NOT A MEASUREMENT: across all
    # 19,764 source rows it takes exactly two values — MW_Input (the
    # studied request size passed) or 0 (it failed) — over nine discrete
    # study sizes (20/40/80/100/120/150/200/250/300 MW). The old
    # territory max "300.0" was the largest scenario Avista chose to
    # study, not a capacity.
    # ★★ THE OLD POINT-ONLY KEY KEPT THE MOST FLATTERING ROW: rows are
    # (bus × study size × limiting element × contingency) tuples, ~100x
    # per bus (19,764 → 197), and capacity-DESC paging + keep-first dedup
    # collapsed each bus to its MAXIMUM passing size, discarding every
    # binding constraint (bus 'Huetter' fails 2/10 constraints at 20 MW
    # yet was stored at 200 MW — read back from the live table
    # 2026-07-30). key_extra mirrors dominion_va_ev_load: a key collision
    # now means genuinely identical data, so every constraint row lands.
    # The old mislabelled rows are deleted by _RETRACTED_UTILITIES below.
    {"utility": "Avista (gen interconnection study)",
     "key": "avista_bus",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services3.arcgis.com/WlYQgAChrqj0tuQi/arcgis/rest/"
             "services/HeatMap_MW_Impact_PRD/FeatureServer/0/query"),
     "key_extra": ("MW_Input", "Limiting_Element", "Limiting_CTG"),
     "delay_s": 2.0,
     "capacity_basis": ("GENERATION-interconnection study value — NOT load "
                        "capacity and NOT a measured bus headroom. Avista's "
                        "own AGOL metadata labels the layer 'Generation "
                        "Interconnection' ('2026.GenerationInterconnection"
                        "HeatMap.R1', item 34c5773cf6dc44a798b300d0ebab0ecb). "
                        "MW_Available is a PASS/FAIL flag: it equals the "
                        "studied injection size (MW_Input, one of nine "
                        "discrete scenarios from 20 to 300 MW) where that "
                        "size cleared one limiting element under one "
                        "contingency, and 0 where it failed. Read a row as "
                        "'this injection size passed/failed THIS constraint', "
                        "never as a continuous headroom; the binding "
                        "capability of a bus is the MINIMUM across its "
                        "constraint rows. Rows are (bus × study size × "
                        "limiting element × contingency) tuples, not "
                        "feeders."),
     "fields": {"feeder": None, "substation": "Bus_Name",
                "state": None, "region": None, "voltage_kv": "Bus_Voltage",
                "mw_max": ("MW_Available", 1.0),
                "mw_min": None, "queued_kw": None, "updated": None}},
    # ★★ CENTRAL HUDSON — the best find of the WS11 sweep: an actual
    # LOAD-headroom publication in MW (summer + winter), not solar HC.
    # Most rows are geometry-only stubs → filter on Feeder NOT NULL.
    {"utility": "Central Hudson (load headroom)",
     "key": "cenhud_load",
     "capacity_type": "load",
     "country": "US",
     "where": "Feeder IS NOT NULL",
     "url": ("https://services1.arcgis.com/CEN9MBRF2dIzEmKF/arcgis/rest/"
             "services/Electrification_HC/FeatureServer/0/query"),
     # Summer is the binding season → headline; winter kept alongside.
     "fields": {"feeder": "Feeder", "substation": "Substation",
                "state": None, "region": None, "voltage_kv": "Voltage_kV",
                "mw_max": ("Summer_Headroom", 1.0),
                "mw_min": ("Winter_Headroom", 1.0),
                "queued_kw": None, "updated": "RefreshDate"}},
    {"utility": "Central Hudson (DER HC)",
     "key": "cenhud_gen",
     "capacity_type": "gen",
     "country": "US",
     "where": "Feeder IS NOT NULL",
     "url": ("https://services1.arcgis.com/CEN9MBRF2dIzEmKF/arcgis/rest/"
             "services/Hosting_Capacity_Stage3/FeatureServer/0/query"),
     "fields": {"feeder": "Feeder", "substation": "Substation",
                "state": None, "region": None, "voltage_kv": "Voltage_kV",
                "mw_max": ("HCMax", 1.0), "mw_min": ("HCMin", 1.0),
                "queued_kw": "QUEUEDDER", "updated": "HCA_REFRESH_DATE"}},
    # National Grid MA — the earlier 403 was header-only; the portal
    # answers with a browser UA + its own Referer (verified 2026-07-27).
    {"utility": "National Grid MA",
     "key": "ngrid_ma",
     "capacity_type": "gen",
     "country": "US",
     "headers": {"User-Agent": "Mozilla/5.0 (compatible; DCHub-GridData/1.0)",
                 "Referer": "https://systemdataportal.nationalgrid.com/"},
     # ★ MASDP_HostingCapacity returns EMPTY geometry (server config) —
     # its Nodal_Hosting_Capacity_MA sibling carries paths AND feeder
     # max/min HC, same schema family as the NY portal. Verified 07-27.
     "url": ("https://systemdataportal.nationalgrid.com/arcgis/rest/"
             "services/MASDP/Nodal_Hosting_Capacity_MA/MapServer/0/query"),
     "fields": {"feeder": "feeder_cdf", "substation": "substation_name",
                "state": None, "region": None,
                "voltage_kv": "feeder_voltage",
                "mw_max": ("feeder_max_hc", 1.0),
                "mw_min": ("feeder_min_hc", 1.0),
                "queued_kw": "feeder_queued_dg",
                "updated": "hca_refresh_date"}},
    # Eversource CT — published by Cadmus Group (a consultancy), NOT by
    # Eversource itself. Ingested with the third-party provenance IN THE
    # UTILITY NAME so no popup can imply a first-party source.
    {"utility": "Eversource CT (via Cadmus)",
     "key": "eversource_ct",
     "capacity_type": "gen",
     "country": "US",
     "url": ("https://services3.arcgis.com/p04uQpu9ausDBOAh/arcgis/rest/"
             "services/DG_Hosting_CT_Final_full/FeatureServer/56/query"),
     "fields": {"feeder": "CIRCUITID", "substation": "DIST_SUB_NAME",
                "state": None, "region": None,
                "voltage_kv": "SECTION_OPERATING_VOLTAGE",
                "mw_max": ("HOSTING_CAPACITY_MW", 1.0),
                "mw_min": None, "queued_kw": "IN_QUEUE_DG_KW",
                "updated": "DATE_UPDATED"}},
    # ══ LOAD tier 1 (probed 2026-07-29, every number below re-measured) ══
    #
    # ★★ DOMINION ENERGY VA — EV/LOAD layer. Loudoun/NoVA + Richmond.
    # This is NOT the `dominion_va` entry above: different service, and
    # LOAD not gen. How we know it is load, four ways:
    #   (1) Dominion publishes three separate HC services — Primary
    #       (utility-scale DER), Residential (DER), and EV. Only EV is
    #       about new load connecting.
    #   (2) their AGOL web maps name the intent: "Hosting Capacity
    #       Electric Vehicle WM EB" vs "... Utility Scale WM EB".
    #   (3) the EV web map's companion layers are DC Fast Charging
    #       Stations / Alternative Fuel Corridor — a load-siting map.
    #   (4) type contrast proves they are different data, not a copy:
    #       Primary L6 LIMIT_VAL is an Integer over 6 unique values and
    #       returns 50 rows (BINNED); this layer is a Double with 2,381
    #       distinct values over 5,546 rows (PRECISE).
    # Units: MW, stated by the publisher — the EV web map's popupInfo for
    # this exact layer URL reads "Up To {LIMIT_VAL} MW".
    # ★ NO FEEDER ID. The full field list is OBJECTID, PRI_NOM_VOLT_CD,
    # LIMIT_VAL, OPERATINGVOLTAGE, Shape__Length — nothing identifies a
    # circuit, so the join is PURELY GEOMETRIC and distinct_feeders is
    # UNAVAILABLE for this source (emits None; never rows-as-feeders).
    # ★★ explode_multipart is load-bearing, not cosmetic — see
    # _ROW_NOT_FEEDER_SOURCES and _explode_features().
    # ★ key_extra: with no feeder id the key is lat/lng at 4dp (~11 m), so
    # two different capacity values could collide onto one key and the
    # capacity-DESC ordering would keep the FLATTERING one. Adding the
    # attribute triple to the key makes a collision mean genuinely
    # identical data instead of a silently dropped lower number.
    {"utility": "Dominion Energy VA (EV load)",
     "key": "dominion_va_ev_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services.arcgis.com/DmE6Z8jKWf8lv84J/arcgis/rest/"
             "services/EV_Hosting_Capacity_Available_EB/"
             "FeatureServer/14/query"),
     "explode_multipart": True,
     "key_extra": ("LIMIT_VAL", "PRI_NOM_VOLT_CD", "OPERATINGVOLTAGE"),
     "delay_s": 2.0,
     # ~5,546 features x ~18.8 paths each ≈ 104k exploded rows (measured
     # over 600 features on 2026-07-29). Cap set above that so the whole
     # territory fits; truncation would silently delete map coverage.
     # Env override: HOSTING_CAPACITY_DOMINION_EV_MAX_ROWS (_MAX_ROWS_OVERRIDE).
     "max_rows": 120000,
     "attribution": "Dominion Energy Virginia (public hosting-capacity GIS)",
     "capacity_basis": ("MW available for NEW EV/charging LOAD, publisher-"
                        "stated units ('Up To {LIMIT_VAL} MW' in Dominion's "
                        "own map popup). Precise (0.01 MW steps), not binned. "
                        "No feeder identifier is published — location is the "
                        "only join, and distinct feeders cannot be counted. "
                        "Values rounded to 3dp on ingest."),
     "fields": {"feeder": None, "substation": None,
                "state": None, "region": None,
                "voltage_kv": ("PRI_NOM_VOLT_CD", 0.001),   # volts → kV
                "mw_max": ("LIMIT_VAL", 1.0),
                "mw_min": None, "queued_kw": None, "updated": None}},
    #
    # ★★ SCE (Southern California Edison) DRPEP / ICA — LA basin, Inland
    # Empire, Orange County. LOAD, and the service says so itself: L7-L9
    # are "ICA - Load ...", L15 is "ICA - Uniform Load Static Grid", while
    # L2-L6 / L10-L14 are Generation / Solar / Uniform Generation.
    # ★★ ONE DIGIT FROM A DISASTER: L14 is "ICA - Uniform GENERATION
    # Static Grid" and L15 is "ICA - Uniform LOAD Static Grid". The URL
    # below MUST end /FeatureServer/15/ — asserted in the guard test.
    # ★★ CAPACITY FIELD: use the Double `uniform_load_static_grid_legend`,
    # NOT the String `uniform_load_static_grid`. Despite the _legend name
    # it is not a bin — it mirrors the same value (32.5788 == '32.5788')
    # and carries -1 where the string reads 'NA'. Two reasons this matters:
    #   ORDERING — _fetch_pages orders capacity DESC so a capped crawl
    #     keeps the highest-capacity rows. Ordering by the STRING is
    #     LEXICOGRAPHIC: verified live 2026-07-29 that
    #     orderByFields=uniform_load_static_grid DESC returns six straight
    #     'NA' rows, i.e. the cap would have filled with UNMEASURED rows.
    #     Ordering by the Double returned 32.5788 first.
    #   UNMEASURED ≠ 0 — the 38,490 'NA' rows are unmeasured and are
    #     EXCLUDED by the where-clause so they emit no row at all, while
    #     the 221,340 genuine 0.0 rows are a real measured zero and are
    #     kept. -1 must never reach the table as a capacity.
    # ★ ROWS ARE LINE SECTIONS: 676,467 rows -> 3,723 circuits. Row count
    # is never a feeder count; distinct_feeders is reported separately.
    # Counts re-measured 2026-07-29: 676,467 total / 637,977 legend>=0 /
    # 3,723 distinct circuit_name.
    # ★ Right base path is /arcgis_server/ — /arcgis/ returns a 404
    # "site is down for maintenance" page that masquerades as an outage.
    {"utility": "SCE (Southern California Edison, load)",
     "key": "sce_ica_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://drpep.sce.com/arcgis_server/rest/services/Hosted/"
             "ICA_Layer/FeatureServer/15/query"),
     "where": "uniform_load_static_grid_legend >= 0",
     # Explicit even though it matches mw_max — this is THE ordering fix,
     # and naming it here means a future edit to mw_max cannot silently
     # move the crawl back onto a string field.
     "order_field": "uniform_load_static_grid_legend",
     "delay_s": 2.0,
     # 637,977 usable rows against a 20k default = a 3% sample. Raised to
     # the capacity-DESC HEAD, which is the site-selection-relevant tail;
     # the run records rows_scanned + truncated so the sample is never
     # mistaken for the whole territory. Sharding by system_name (49) or
     # substation_name (623) is the follow-up for a complete backfill.
     # Env override: HOSTING_CAPACITY_SCE_MAX_ROWS (_MAX_ROWS_OVERRIDE).
     "max_rows": 40000,
     "attribution": ("Southern California Edison — DRPEP/ICA public "
                     "hosting-capacity data"),
     "capacity_basis": ("MW of LOAD capacity available on a circuit line "
                        "section (CPUC ICA). Units MW: max 32.5788 on a "
                        "33 kV circuit and 1.4-3.7 typical on 12 kV is "
                        "MW-plausible and kW-absurd, and sibling tables in "
                        "the same service name min_load_mw / max_load_mw. "
                        "Precise values (4dp at source, rounded to 3dp on "
                        "ingest); the map's class breaks are display only. "
                        "Rows are LINE SECTIONS, not feeders. 'NA' sections "
                        "are UNMEASURED and are not ingested; a reported 0.0 "
                        "is a real measured zero and is kept."),
     "fields": {"feeder": "circuit_name", "substation": "substation_name",
                "state": None, "region": "system_name",
                "voltage_kv": "circuit_voltage",       # kV as a string
                "mw_max": ("uniform_load_static_grid_legend", 1.0),
                "mw_min": None, "queued_kw": None,
                "updated": "changed_date_forecast"}},
    #
    # ★★ SDG&E — San Diego. The second CPUC ICA load layer, and the largest
    # single LOAD source in this table: 497,700 rows measured live 2026-07-30.
    # Sibling of sce_ica_load (same CPUC mandate, same ICA vocabulary), so the
    # vetting below is the SCE pattern re-measured, not assumed from it.
    # ★★ TRAP 1 — 10 IS A CENSORING CEILING, NOT A MAXIMUM. Measured:
    #   ICAWOF_UNILOAD > 10   ->      0 rows
    #   ICAWOF_UNILOAD = 10   ->  1,827 rows
    #   9 < ICAWOF_UNILOAD < 10 -> 1,474 rows
    #   distinct values above 9.4: 9.5, 9.6, 9.7, 9.8, 9.9, 10
    # A 0.1-step scale that stops dead at 10 with 1,827 rows piled on the
    # boundary and none beyond is a clip, exactly like NV Energy's "Over 20MW"
    # (200 sections at exactly 20.0). Publishing those 1,827 as 10 MW would
    # UNDERSTATE the best circuits in San Diego and cap the layer at a value
    # SDG&E never claimed. They are EXCLUDED by the `where`, not published —
    # the same call as nvenergy_lhc. A censored row is unmeasured, not 10.
    # ★★ TRAP 2 — FOUR GENERATION DOUBLES SIT IN THE SAME ROW. ICAWOF_/
    # ICAWNOF_ UNIGENERATION and PVGENERATION are all Doubles alongside the
    # load field and would all map cleanly into mw_max. Only the field name
    # stops the swap, so all four are in _GEN_ONLY_FIELDS and
    # check_source_contract() refuses this source if one is ever wired in.
    # ★ WOF vs WNOF is a NON-CHOICE here, recorded so nobody "corrects" it to
    # the other in search of a conservative read: ICAWNOF_UNILOAD equals
    # ICAWOF_UNILOAD on all 497,700 rows (measured: 0 rows where either is
    # greater). They are the same data under two names on this layer.
    # ★ DO NOT READ LABELTEXT_LCA. It is the map's display binning —
    # '0', 'Up To 1.00', '1.00-1.50', '1.50-2.00', 'Above 2' — so its top
    # bucket collapses everything from 2 MW to the ceiling. The Double is
    # precise to 0.1; the string is a legend.
    # ★ ROWS ARE 771.6x FEEDERS, the second-worst ratio in this table.
    # ENUMERATED, not extrapolated: a paged groupBy over (SUBID, CIRCUIT_NAME)
    # returned 645 groups for 497,700 rows. ★ The single-shot
    # returnDistinctValues+returnCountOnly on that pair returned 497,700 —
    # i.e. the row count — which is arithmetically impossible for 645 circuits
    # across 103 substations (<= 66,435 combinations). The server silently
    # ignores multi-field distinct counting on this service. Trusting it would
    # have recorded a 1.0x ratio and turned every row count into a feeder
    # count. Page the groupBy; never believe a distinct-count you did not
    # enumerate.
    # ★ CIRCUIT_NAME is globally unique: distinct CIRCUIT_NAME (645) equals
    # distinct (SUBID, CIRCUIT_NAME) (645), so the feeder field alone is a
    # sound fold key and does not need the substation prefixed.
    # ★ 31,328 rows are exactly 0 and are KEPT — a measured zero is an answer
    # ("this circuit has no load capacity"), unlike the censored 10s. No NULLs
    # exist in the field. RESTRICTED is 'N' on all 497,700 rows, so there is no
    # withheld subset to filter.
    # ★ NO ATTRIBUTION IS PUBLISHED. copyrightText is '' on both the layer and
    # the service root, and serviceDescription/description are empty too — so
    # unlike the NV entries there is no publisher string to copy. The
    # attribution below therefore names the publisher and says the service
    # itself asserts none, rather than inventing a licence.
    # ★ Geometry is POLYGON (grid cells), not polyline: rings, one part each,
    # so explode_multipart is not needed.
    {"utility": "SDG&E (San Diego Gas & Electric, load)",
     "key": "sdge_ica_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services.arcgis.com/S0EUI1eVapjRPS5e/arcgis/rest/"
             "services/ICA_MAP_PROD_LoadCapacityGrids_VW/FeatureServer/0/query"),
     # Excludes the 1,827 censored rows at the 10 MW clip (TRAP 1). The
     # >= 0 half keeps the 31,328 measured zeros.
     "where": "ICAWOF_UNILOAD >= 0 AND ICAWOF_UNILOAD < 10",
     # Explicit, for the same reason as SCE: a later edit to mw_max must not
     # silently move the capacity-DESC crawl onto another field.
     "order_field": "ICAWOF_UNILOAD",
     "delay_s": 2.0,
     # 495,873 usable rows (497,700 minus the 1,827 censored). The default is
     # the capacity-DESC HEAD, not the territory; the run records
     # rows_scanned + truncated. Sharding by SUBID (103) is the follow-up for a
     # complete backfill.
     # Env override: HOSTING_CAPACITY_SDGE_MAX_ROWS (_MAX_ROWS_OVERRIDE).
     "max_rows": 40000,
     "attribution": ("San Diego Gas & Electric — ICA (Integration Capacity "
                     "Analysis) public map, CPUC-mandated. The service "
                     "publishes no copyright or licence string of its own."),
     "capacity_basis": (
         "MW of LOAD capacity available in an ICA grid cell (CPUC Integration "
         "Capacity Analysis), from SDG&E's own production ICA map. Units are "
         "MW: the field is a Double on a 0.1 step with a 10 MW study clip, "
         "which is MW-plausible and kW-absurd on 12 kV circuits, and it is "
         "the load-side sibling of SCE's identically-scoped ICA layer. "
         "The four GENERATION twins in the same row (ICAWOF_/ICAWNOF_ "
         "UNIGENERATION and PVGENERATION) are NOT read. "
         "★ SDG&E clips the study at 10 MW: the 1,827 cells reported as "
         "exactly 10.0 are CENSORED, not measured, and are excluded rather "
         "than published as a maximum — so this layer's published ceiling is "
         "9.9 MW and the true value on those cells is unknown and higher. "
         "A reported 0.0 is a real measured zero (31,328 cells) and is kept. "
         "Rows are GRID CELLS, 771.6 per circuit (497,700 rows over 645 "
         "circuits, enumerated by paged groupBy), folded to one row per "
         "circuit; THE FOLD IS NOT LOSSLESS — capacity varies across a "
         "circuit's cells, so the row kept is the HIGHEST-capacity cell on "
         "that circuit AT THAT CELL'S OWN LOCATION. Read it as 'SDG&E "
         "publishes this MW at this point', never as the capacity of the "
         "whole circuit. Coverage is the capacity-DESC head, not the full "
         "territory. LABELTEXT_LCA is display binning and is not read."),
     "fields": {"feeder": "CIRCUIT_NAME", "substation": "SUBID",
                "state": None, "region": None,
                "voltage_kv": "VOLTAGE",               # kV, SmallInteger
                "mw_max": ("ICAWOF_UNILOAD", 1.0),
                "mw_min": None, "queued_kw": None,
                # No date field of any kind exists on this layer (checked
                # every field for a Date type and for 'date' in the name).
                "updated": None}},
    #
    # ══ LOAD expansion, tier 2 (probed + re-probed 2026-07-29) ═══════════
    #
    # ★★ DUKE ENERGY OHIO — Cincinnati. LOAD, and the publisher says so: the
    # service is Ohio_Load_Map and every capacity alias reads "... Load (MW)".
    # Freshest data in the whole sweep — create_dttm_v is
    # '2026-05-10T14:47:24.534582', identical on all 7,451 rows (one refresh
    # stamp for the layer, not a per-row date).
    # ★★ TRAP 1 — READ THE RAW COLUMNS, NEVER THE `_v` TWINS. The `_v` family
    # COERCES NULL TO 0. Measured live 2026-07-29: spot_load IS NULL on 7,252
    # rows while spot_load_v IS NULL on 0 rows and spot_load_v = 0 on 7,258.
    # Mapping the _v twin would publish "0 MW" for thousands of hexes that
    # were never measured — the exact inversion of "UNMEASURED emits None,
    # never 0". Raw and _v agree wherever both are present, so the raw column
    # loses nothing. max_load / min_load are non-null on all 7,451 rows.
    # ★★ TRAP 2 — circuit_ids_v IS A COMMA-SEPARATED LIST on 2,471 of 7,451
    # rows (33%): a hex tile overlaps several circuits. The parallel lists do
    # not even align positionally — a live row carries 4 ids against 3 ratings
    # — so no value can be attributed to a named circuit. `id_is_multi` makes
    # map_feature emit feeder_id=None for those rows instead of storing the
    # comma string, which would invent a feeder literally named
    # "H4920580052, H492102000A, H492102000B, H4921060041" and corrupt
    # feeder_key. distinct_feeders therefore counts only the identified
    # single-circuit hexes and UNDER-states rather than fabricating.
    # ★ TRAP 3 — VOLTAGE IS DELIBERATELY NOT MAPPED. circuit_voltage_class_v
    # is multi-valued too ('15, 35', '15, 35, 5') and _num() takes the MAX of
    # a comma list, so mapping it would silently publish the HIGHEST kV class
    # present. substation_voltage_class_v reads '160' on 3,701 rows despite a
    # kV alias, and Duke Ohio runs 138/69 kV — so '160' is UNVERIFIED as a
    # voltage. Neither is published.
    # ★ queued LOAD is not mapped: spot_load ("Load in Queue") is MW, while
    # the column is queued_gen_KW and means queued GENERATION. Writing MW into
    # it would be both a 1000x error and a load/gen category error.
    # ★ The sibling Ohio_Generation_Map in the SAME AGOL org (renderer field
    # max_generation_v) is the DER/export layer. It is capacity_type=gen and
    # is deliberately not ingested here — the "one config serves Duke
    # Carolinas too" expectation is only half true: the org is shared, but the
    # only Carolinas service is Generation Hosting Capacity, GEN ONLY, so
    # Carolinas adds nothing to LOAD coverage.
    {"utility": "Duke Energy Ohio (Cincinnati, load)",
     "key": "duke_oh_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services3.arcgis.com/oX5r75R7mapdoI2F/arcgis/rest/"
             "services/Ohio_Load_Map/FeatureServer/0/query"),
     "id_is_multi": True,
     "delay_s": 2.0,
     "attribution": "Duke Energy Ohio (public hosting-capacity GIS)",
     "capacity_basis": ("MW of LOAD capacity on a hex tile, from Duke's own "
                        "Ohio_Load_Map (aliases read 'Maximum Load (MW)' / "
                        "'Minimum Load (MW)'). Read from the RAW max_load / "
                        "min_load columns, because the '_v' display twins "
                        "coerce NULL to 0 and would publish unmeasured tiles "
                        "as 0 MW. Rows are HEX TILES, not feeders, and 33% of "
                        "them overlap several circuits — those carry no "
                        "feeder id at all rather than a fabricated one. Note "
                        "Duke's own map headlines the MINIMUM; on a "
                        "multi-circuit tile the maximum is the "
                        "least-constrained circuit, so read the pair as a "
                        "range. Refreshed 2026-05-10 (one stamp for the "
                        "layer). Voltage is not published here: both voltage "
                        "columns are multi-valued or unverified."),
     "fields": {"feeder": "circuit_ids_v", "substation": "substation_names_v",
                "state": None, "region": None,
                "voltage_kv": None,          # TRAP 3 — never publish the max
                "mw_max": ("max_load", 1.0),
                "mw_min": ("min_load", 1.0),
                "queued_kw": None, "updated": "create_dttm_v"}},
    #
    # ★★ PSE&G NEW JERSEY — Newark / Jersey City corridor. LOAD.
    # ★★ UNITS RESOLVED, closing the standing "do not ingest a magnitude we
    # can't name" exclusion below. The service carries no unit, but the
    # publisher's own web map renders SYMBOLOGY with kW-labelled classes
    # ('> 1000 kW', '500kW - 1,000kW', '< 500kW'). That is a falsifiable
    # prediction, and it was tested against the data 2026-07-29:
    #   SYMBOLOGY=1 AND CAPACITY_REMAINING > 1.0   -> 120,381 of 120,381
    #   SYMBOLOGY=1 AND CAPACITY_REMAINING > 1000  ->       0 of 120,381
    # The three legend classes partition all 283,582 valued rows perfectly on
    # the MW reading and the kW reading matches nothing. UNITS ARE MW.
    # ★★ BASIS WARNING, and it is the important one: CAPACITY_REMAINING IS
    # NOT THERMAL FEEDER HEADROOM. PEAKLOAD + CAPACITY_REMAINING is exactly
    # constant per circuit, and that constant takes only two values across all
    # 2,317 circuits — 8.0 MW on 1,107 and 2.5 MW on 1,056 — splitting
    # PERFECTLY on the 4th character of base_circuitid (the voltage class),
    # zero exceptions. So this is an EV-screening ALLOWANCE minus peak load.
    # Publishing it as feeder hosting capacity would overstate what a data
    # centre can actually get, so the label and the basis both say so.
    # ★★ VERTEX DEDUPE on base_circuitid — 287,307 rows for 2,317 circuits.
    # Proven lossless: across the 1,000 largest circuits the number whose
    # CAPACITY_REMAINING varies within the circuit is ZERO, and a live sample
    # shows two vertices of ADA8012 carrying identical -1.3217 / 9.3217.
    # ★★ NEGATIVES ARE REAL AND MUST SURVIVE — 106,638 rows (37.6%) are < 0,
    # meaning peak load already exceeds the allowance (over-subscribed). They
    # are never clamped and never dropped; _dedupe_rows deliberately keeps the
    # FIRST row per circuit rather than the max, so a negative can never be
    # hidden behind a positive sibling. CAPACITY_REMAINING is never exactly 0
    # (measured count = 0), so a 0 in our table would be a bug, not data.
    # ★ The where-clause drops the 3,725 rows the renderer calls "Analysis
    # Pending" (SYMBOLOGY/value NULL) — UNMEASURED emits no row at all.
    # ★ COVERAGE IS THE CAPACITY-DESC HEAD. 40k of 287k vertices, so the
    # over-subscribed (negative) circuits sit below the cut. That biases
    # COVERAGE, not values; a complete backfill wants sharding by
    # base_circuitid prefix. truncated + rows_scanned are reported so the
    # sample is never mistaken for the territory.
    {"utility": "PSE&G New Jersey (EV load allowance)",
     "key": "pseg_nj_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services.arcgis.com/yHb9HdkiNl1PZaOr/arcgis/rest/"
             "services/EVCapacity/FeatureServer/0/query"),
     "where": "CAPACITY_REMAINING IS NOT NULL",
     "dedupe_key": "base_circuitid",
     "delay_s": 2.0,
     "max_rows": 40000,
     "attribution": "PSE&G (Public Service Electric & Gas, New Jersey)",
     "capacity_basis": ("MW of EV-screening LOAD ALLOWANCE remaining on a "
                        "circuit — NOT thermal feeder headroom. PSE&G grants "
                        "each circuit a fixed allowance by voltage class "
                        "(8.0 MW or 2.5 MW, verified constant across all "
                        "2,317 circuits) and publishes allowance minus peak "
                        "load, so it understates nothing but describes a "
                        "screening rule rather than the wire. Units are MW, "
                        "proven against the publisher's own kW-labelled "
                        "legend (the MW reading partitions all 283,582 valued "
                        "rows; the kW reading matches none). NEGATIVE values "
                        "are real and mean the circuit is already "
                        "over-subscribed. Rows are GIS VERTICES (123.5 per "
                        "circuit) and are folded to one row per circuit."),
     "fields": {"feeder": "base_circuitid", "substation": None,
                "state": None, "region": None, "voltage_kv": None,
                "mw_max": ("CAPACITY_REMAINING", 1.0),
                "mw_min": None, "queued_kw": None, "updated": None}},
    #
    # ★★ PECO — Philadelphia + southeastern PA. LOAD.
    # ★★ THE UNIT IS MVA, NOT MW — confirmed, and the confirmation is in
    # PECO's own classBreaks renderer, independently on BOTH layers: '< 0.5
    # MVA', '0.5 - 1.5 MVA', '1.5 - 3 MVA', '3 - 5 MVA', '> 5 MVA'. The unit
    # is spelled out five times per layer in the legend, just never in the
    # field alias — which is why the earlier "one unlabeled number per grid
    # tile" exclusion below is now stale.
    # ★★ NO POWER-FACTOR CONVERSION IS APPLIED, DELIBERATELY. MW = MVA x PF
    # and PF is published nowhere in this service. Assuming 0.95 puts the
    # figure ~5% low, assuming unity puts it ~5-10% high — precisely the
    # "figure ~10% off" failure. The raw MVA is stored and the unit rides in
    # BOTH the utility label and capacity_basis, which the serving layer
    # attaches to every row it returns. A derived MW must never be shown
    # without the PF it assumed. ★ Residual risk, stated plainly: the column
    # is named capacity_mw_max, so this is the one source in the table whose
    # stored magnitude is not MW. MVA >= MW always, so it errs HIGH — the
    # labels are what stop that being read as siteable MW.
    # ★ LAYER 0 (Major_Quad, 3,443 rows) is shipped, not layer 1 (Minor_Quad,
    # 208,141 rows). Layer 1 is finer but 39.3% of its capacity values are
    # NULL and it would be truncated to a partial sample; layer 0 is 89.9%
    # populated and fits whole, so coverage is complete rather than spotty.
    # ★ NO FEEDER ID EXISTS. MAJOR is a map-grid tile id ("50-152H2"), the
    # same class as the BGE MAP_NAME left unset above. feeder_id stays NULL
    # and there is no feeder count to report at all.
    # ★ The swapped LAT/LON columns (LAT holds a longitude, live sample
    # LAT=-75.217 / LON=39.995) are a LAYER 1 hazard — layer 0 has no LAT/LON
    # columns at all, only State Plane XMIN/YMIN. Neither is read: geometry
    # with outSR=4326 comes back correctly ordered and _rep_point handles it.
    # ★ No update/refresh date field exists on either layer, so src_updated is
    # None rather than a guess.
    {"utility": "PECO (Philadelphia) — MVA, not MW",
     "key": "peco_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services3.arcgis.com/agWTKEK7X5K1Bx7o/arcgis/rest/"
             "services/PECO_Available_Distribution_Capacity_Map/"
             "FeatureServer/0/query"),
     "where": "NET_AVAILABLE_CAPACITY IS NOT NULL",
     "delay_s": 2.0,
     "attribution": "PECO Energy (David Brett; Paul Miller) — service copyrightText",
     "capacity_basis": ("NET AVAILABLE CAPACITY in MVA — NOT MW. The unit is "
                        "PECO's own: their map legend labels every class in "
                        "MVA ('< 0.5 MVA' ... '> 5 MVA'), though the field "
                        "alias omits it. No power-factor conversion is "
                        "applied because PECO publishes no power factor; "
                        "MW = MVA x PF, so treat this as an UPPER BOUND on "
                        "MW (roughly 5-10% high). Values are per MAJOR QUAD "
                        "GRID TILE, not per feeder — PECO publishes no "
                        "circuit identifier here, so no feeder count exists. "
                        "PECO publishes no refresh date for this layer."),
     "fields": {"feeder": None, "substation": None,
                "state": None, "region": None, "voltage_kv": None,
                "mw_max": ("NET_AVAILABLE_CAPACITY", 1.0),
                "mw_min": None, "queued_kw": None, "updated": None}},
    #
    # ★★ LADWP (City of Los Angeles) — INGESTED FOR THE NEGATIVE SIGNAL ONLY.
    # ★★ THIS SOURCE PUBLISHES NO NUMBER WE MAY REPEAT. Capacity_Range_KW is
    # a STRING holding a text range and there is NO numeric column anywhere on
    # the layer (full field list: OBJECTID, GISID, OH_UG, Capacity_Range_KW,
    # Capacity_Status, Voltage_Class, Shape__Length). The binning is worse
    # than "0-600 kW buckets": it is BIMODAL by voltage class — 4.8 kV
    # segments get '0 - 150' ... '450 - 600' (kW), 34.5 kV segments get
    # '1000-1500' ... '7000-7500' and an unbounded '>7500'.
    # ★★ WHY IT RANKS LAST AMONG THE LOAD SOURCES, and why no value is
    # published: the modal bin is '450 - 600' at 77,398 rows (36.7% of the
    # layer), it is the TOP bin of the 4.8 kV scale (censored/saturated), and
    # LADWP badges it Capacity_Status='High Capacity'. A segment labelled
    # "High Capacity" here means AT MOST 0.6 MW. Relaying that to a
    # site-selection team would be actively misleading — 0.6 MW is not a
    # data-centre answer. So mw_max/mw_min stay None: never a bin midpoint,
    # never a 0, never a number at all. `negative_signal` is what lets the
    # row exist at all, since map_feature otherwise drops a row with no MW.
    # ★★ THE ONE TRUSTWORTHY SIGNAL is the disqualifying flag, and it lives
    # redundantly in two fields: Capacity_Range_KW='NO CAPACITY' (70,541 rows)
    # and Capacity_Status='No Capacity' (70,541) — identical counts, and the
    # server's SQL comparison is case-insensitive (all four casings return
    # 70,541), so either predicate is safe. 33.4% of segments are disqualified.
    # ★ GISID is a per-SEGMENT id, one per row (211,078 distinct = the row
    # count), NOT a circuit id — there is no feeder identifier on this layer,
    # so feeder stays None and 211,078 must never be shown as a feeder count.
    # GISID is used only as key_extra so two distinct segments cannot collide
    # onto one lat/lng key.
    # ★ Voltage_Class is a string like '4.8-KV' which _num() cannot parse, so
    # it is left unmapped rather than mapped to a silent None.
    # ★ PRESENT-ONLY, and the serving side enforces it: a hit means LADWP
    # flagged those segments. Absence of hits is NEVER evidence of available
    # capacity, because this ingest is row-capped and therefore partial.
    # ★ Placed LAST among the load sources on purpose — _ingest_order() is a
    # stable sort, so list position decides who yields budget first.
    {"utility": "LADWP (City of LA) — NO-CAPACITY flag only",
     "key": "ladwp_no_capacity",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services7.arcgis.com/ZzOj15zjzIfDG8aL/arcgis/rest/"
             "services/PowerCapacity/FeatureServer/0/query"),
     "where": "Capacity_Status = 'No Capacity'",
     "negative_signal": {"field": "Capacity_Status", "value": "No Capacity"},
     "key_extra": ("GISID",),
     "order_field": "GISID",     # no capacity field to order by; stable paging
     "delay_s": 2.0,
     "attribution": "Los Angeles Department of Water and Power (LADWP)",
     "capacity_basis": ("DISQUALIFYING FLAG ONLY — no capacity figure. LADWP "
                        "publishes capacity as TEXT RANGES, binned by voltage "
                        "class, and badges its top 4.8 kV bin (at most "
                        "0.6 MW) 'High Capacity' — so no number from this "
                        "source can be repeated to a site-selection team and "
                        "none is stored. What IS trustworthy is the explicit "
                        "'NO CAPACITY' flag on 70,541 of 211,078 segments "
                        "(33.4%). Rows are line SEGMENTS, not feeders. "
                        "Present-only: the absence of a flag is not evidence "
                        "of available capacity."),
     "fields": {"feeder": None, "substation": None,
                "state": None, "region": None, "voltage_kv": None,
                "mw_max": None, "mw_min": None,
                "queued_kw": None, "updated": None}},
    #
    # ══ AUTHORISED sources (owner-authorised, probed 2026-07-29) ═══════════
    # ★ ORDER INSIDE THIS BLOCK IS DELIBERATE. _ingest_order() is a stable
    # sort, so list position decides who yields the shared budget first. ComEd
    # is 5,164 rows and finishes in three pages; nvenergy_lhc is a 977k-row
    # layer whose pages measured ~19s each, so it CANNOT finish inside the
    # 300s default and will report budget_exhausted on a routine run (a full
    # pass wants HOSTING_CAPACITY_INGEST_BUDGET_S=900). Putting the giant
    # first would have starved the small complete source behind it.
    #
    # ★★ ComEd (Commonwealth Edison, Exelon) — CHICAGO. LOAD. The "ComEd
    # (secured-proxy 403)" exclusion in this file was header-only, like
    # National Grid MA: the service answers with a Referer.
    # ★★ ACCESS, and the two attempts are recorded because the second one is
    # the kind of header a crawler should have to justify:
    #   (1) plain GET, our own UA, NO Referer  -> HTTP 200 carrying
    #       {"error":{"code":403,"messageCode":"GWM_0003"}}
    #   (2) same request + Referer = the bare portal origin -> the SAME 403,
    #       so the header is not a formality; + Referer = the EXACT FULL
    #       webappviewer app URL -> 200 and the layer list.
    # The app URL below is ComEd's own public map (AGOL item
    # 8d4f22be2a3b47b0bb86ca5438a8dd69, "ComEd_EV_Load_Capacity"), i.e. the
    # page a human clicks to see this data — we are describing the context we
    # really are in, not spoofing a browser. ★ NO User-Agent KEY HERE, ever:
    # _fetch_pages sets _UA first and then applies this dict, so adding one
    # would strip our identity off the request that carries the Referer. The
    # guard test asserts both halves.
    # ★★ THE SERVICE NAME AND ITS ITEM ID BOTH ROTATE MONTHLY —
    # ComEd_EV_Load_Capacity_JUN2026 today, ComEd_EV_Load_Capacity_032026 in
    # March, and the usrsvcs path embeds the AGOL item id, which is a NEW item
    # each month. A pinned URL therefore dies within weeks as a silent empty
    # ingest, which is why url_resolver exists and is not optional. How it was
    # found, and how the resolver repeats it at runtime: the AGOL public search
    # (no key, no Referer) for owner:e087224_ComEd, newest first, keeping
    # Feature Services whose title contains 'comed_ev_' — that matches both
    # namings used so far ('ComEd_EV_Load_Capacity_JUN2026_2ndService' and
    # 'ComEd_EV_Hosting_Capacity_032026_2ndService') while excluding the PV and
    # BESS siblings in the same account. The item's own `url` field is the
    # FeatureServer, and layer 28 is appended. The pinned url below is the
    # JUN2026 endpoint verified today and is only the FALLBACK.
    # ★★ LAYER GRAIN — 28, NEVER 29-32, confirmed against the live layer list:
    # L28 is 5,164 buffered CIRCUIT polygons for 5,163 distinct Feeders
    # (ratio 1.0002 — one row per circuit), while L29 (13,669 rows, PLSS
    # SECTION/TWPNUM/RNGNUM), L30 (47,994), L31 (377) and L32 (203,181,
    # qtr_sectio) are TILE aggregations that repeat one circuit's value across
    # every tile it touches and show only the highest circuit inside a shared
    # tile — 203,181 rows for 3,006 feeders. A tile value OVERSTATES any
    # specific site in that tile, and its row count would inflate feeders 39x.
    # ★ UNITS are kW, from the layer's own name "Estimated EV Hosting Capacity
    # (kW)" and the field EV_HC_kW -> x0.001 to MW. The top circuit is
    # 13,000 kW = 13 MW, MW-plausible and MVA-irrelevant (no power factor is
    # involved: this is a charger-count allowance, and Lvl_2_Char reads
    # '677 - 1806 Chargers' on that same row).
    # ★ 0 IS A REAL MEASURED ZERO, not a gap: EV_HC_kW IS NULL returns 0 rows,
    # 218 rows are exactly 0, and their own label reads '0 - 0 Chargers'. They
    # are kept. The where-clause is belt and braces so an unmeasured row could
    # never arrive as a zero.
    # ★ NO SUBSTATION, VOLTAGE OR REFRESH DATE EXISTS on this layer (full field
    # list: OBJECTID, BUFF_DIST, ORIG_FID, Feeders, EV_HC_kW, Lvl_2_Char,
    # Lvl_3_Char, Shape__Area, Shape__Length), so those stay None rather than
    # being guessed. BUFF_DIST is 150 (the polygon buffer), not a rating.
    {"utility": "ComEd (Chicago) — EV load capacity",
     "key": "comed_ev_load",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://utility.arcgis.com/usrsvcs/servers/"
             "3e659d584f4b47c1b6647b229f93fe48/rest/services/"
             "ComEd_EV_Load_Capacity_JUN2026/FeatureServer/28/query"),
     "url_resolver": {"kind": "agol_item_search",
                      "portal": "https://www.arcgis.com/sharing/rest/search",
                      "q": "owner:e087224_ComEd",
                      "item_type": "Feature Service",
                      "title_contains": "comed_ev_",
                      "layer_path": "/28/query"},
     "headers": {"Referer": ("https://exelonutilities.maps.arcgis.com/apps/"
                             "webappviewer/index.html"
                             "?id=8d4f22be2a3b47b0bb86ca5438a8dd69")},
     "where": "EV_HC_kW IS NOT NULL",
     "delay_s": 2.0,
     # A 2,000-polygon page measured 14.0s and a cold one blew the 25s default
     # (the source then failed on LATENCY, with the data perfectly fine).
     # ★ The proxy also returned a TRANSIENT HTTP 504 on the second page in one
     # of three dry runs; the same offset served 2,000 features in 11.6s
     # immediately after. _fetch_pages records that as status "http_504" and
     # keeps the pages it did get, so a bad run is visible and partial rather
     # than silent — and the upsert leaves the previous complete run's rows in
     # place. Do not read a one-off 504 here as a block.
     "timeout_s": 60,
     "attribution": "ComEd (Commonwealth Edison)",
     "capacity_basis": ("MW of EV-charging LOAD capacity estimated on a "
                        "CIRCUIT, converted from ComEd's own kW field "
                        "(EV_HC_kW; the layer is named 'Estimated EV Hosting "
                        "Capacity (kW)'). Layer 28 is the circuit grain — one "
                        "row per circuit, 5,164 rows for 5,163 circuits — and "
                        "the coarser layers in the same service are PLSS TILE "
                        "aggregations that show the highest circuit value in "
                        "each tile, so a tile figure would overstate any "
                        "specific site and none is ingested. This is ComEd's "
                        "EV-charging screening estimate (the popup counts it "
                        "in chargers), not a contracted data-centre service "
                        "figure. A reported 0 is a real measured zero (218 "
                        "circuits) and is kept; no value is NULL. ComEd "
                        "publishes no substation, voltage or refresh date on "
                        "this layer, so none is shown."),
     "fields": {"feeder": "Feeders", "substation": None,
                "state": None, "region": None, "voltage_kv": None,
                "mw_max": ("EV_HC_kW", 0.001),          # kW → MW
                "mw_min": None, "queued_kw": None, "updated": None}},
    #
    # ★★ NV ENERGY — LAS VEGAS + RENO. The "NV Energy (login)" exclusion in the
    # note below this list is STALE: the DRP web-app service answers ANONYMOUS
    # GETs. Service root, and it is the root that carries the terms-relevant
    # string: copyrightText is EXACTLY "NV Energy" at
    # .../DRP/DRP_WebApp/MapServer, while every LAYER endpoint under it returns
    # an EMPTY copyrightText — so a layer-level read would have concluded "no
    # attribution published" and dropped a credit the operator requires. Both
    # NV entries therefore carry attribution="NV Energy", the exact string the
    # service publishes, and it rides on the rows via _SOURCE_ATTRIBUTION.
    #
    # ★★ FIELD NAMES ARE FULLY QUALIFIED (DRP_GIS_DATA.<TABLE>.<COL>) because
    # these layers are JOINED views. Verified live: outFields must use the
    # qualified names — the bare name 'SUBSTATION' returns HTTP 200 with
    # {"error":{"code":400,"message":"Failed to execute query."}}, which
    # _fetch_pages records as service_error rather than mistaking for empty.
    #
    # ★★ THE GENERATION TWIN SITS IN THE SAME ROW. Layer 31 carries
    # DRP_GIS_DATA.DRP_HCA.LHC ("LHC (MW)", LOAD — the one we want),
    # DRP_GIS_DATA.DRP_HCA.GHC ("GHC (MW)", generation/DER export) and
    # DRP_GIS_DATA.DRP_HCA.PHC ("PHC (MW)", photovoltaic) — three Doubles, one
    # row, all three would map cleanly. Only the field NAME stops the swap, so
    # GHC/PHC are not mapped anywhere and check_source_contract() plus the
    # guard test both assert it.
    #
    # ★★ 20.0 IS A CENSORING CEILING, NOT A MEASUREMENT. NV clips the study at
    # "Over 20MW". Proven by counting, 2026-07-29: LHC < 20 -> 977,493 rows;
    # total 977,693; i.e. exactly 200 rows sit AT 20.0 and NONE above it, and
    # 19 <= LHC < 20 returns 125. A value that 200 rows share and nothing
    # exceeds is a clip. Publishing 20.0 for those 200 sections would state a
    # FLOOR as a maximum and UNDERSTATE the best feeders on the map, so the
    # where-clause excludes them: they are unmeasured-above-20, and this table
    # has no way to say "at least 20", so it says nothing.
    #
    # ★★ ROWS ARE LINE SECTIONS: 977,693 rows / 1,251 feeders = 781.5 each
    # (registered in _ROW_NOT_FEEDER_SOURCES with the dedupe_key knob).
    # ★★ AND THE FOLD IS NOT LOSSLESS — unlike PSE&G, where the value was
    # proven constant within a circuit, LHC VARIES ENORMOUSLY ALONG A FEEDER.
    # Measured with outStatistics min/max grouped by SYN_FEEDER_ID: BLV284
    # spans 0.0 to 20.0 MW over 4,110 sections, MTE1212 0.3-2.0 over 2,518,
    # CCT1202 0.3-4.1 over 1,210. So the folded row is NOT "the feeder's
    # capacity". Because _fetch_pages pages capacity-DESC and _dedupe_rows
    # keeps the FIRST row per key, the survivor is the feeder's HIGHEST-LHC
    # section CARRYING THAT SECTION'S OWN lat/lng — the pair (point, MW) is
    # exactly what NV publishes for that spot, which is the honest reading and
    # the one capacity_basis states. It must never be generalised to the whole
    # feeder: a site elsewhere on the same wire can be an order of magnitude
    # lower (64.7% of all sections are below 1 MW, 83.3% below 2 MW).
    # ★ Distribution measured 2026-07-29: LHC < 0.5 -> 287,716 rows / 640
    # feeders; < 1 -> 632,380 / 1,063; < 2 -> 814,622 / 1,128.
    # ★ VOLTAGE IS DELIBERATELY NOT MAPPED: DRP_SECTIONS.FDR_VLT is a STRING
    # reading '24.9 KV', which _num() cannot parse — mapping it would publish a
    # silent None on every row and look like a field that exists but is empty.
    # ★ LHC_LF (limiting factor, e.g. 'Thermal') has no column in this schema;
    # it is left unmapped rather than crammed into `region`.
    {"utility": "NV Energy (load hosting capacity)",
     "key": "nvenergy_lhc",
     "capacity_type": "load",
     "country": "US",
     "url": ("https://services.nvenergy.com/GISSvc/1.0/retrieveMapInfoDRP/"
             "arcgis/rest/services/DRP/DRP_WebApp/MapServer/31/query"),
     # Excludes the 200 censored 'Over 20MW' sections (see above). NULLs are
     # not a factor here: LHC IS NULL returns 0 rows.
     "where": "DRP_GIS_DATA.DRP_HCA.LHC < 20",
     # Explicit even though it equals mw_max — naming it here means a later
     # edit to mw_max cannot silently move the capacity-DESC crawl onto
     # another column, and the DESC order is what makes the fold keep the
     # best-published section rather than an arbitrary one.
     "order_field": "DRP_GIS_DATA.DRP_HCA.LHC",
     "dedupe_key": "DRP_GIS_DATA.DRP_SECTIONS.SYN_FEEDER_ID",
     "delay_s": 2.0,
     # 977,493 usable sections against a 20k default would be a 2% sample.
     # This is the capacity-DESC HEAD — the site-selection-relevant tail —
     # and truncated + rows_scanned + deduped_rows are all reported so it is
     # never mistaken for the whole territory. ★ Sized from measurement, not
     # taste: 40k rows is where the DESC order reaches LHC ~5 MW, and
     # 5 <= LHC < 20 is 38,571 sections spread over 559 of the 1,251 feeders
     # (44.7% of the territory); LHC >= 10 would be only 5,870 rows / 126
     # feeders, so a smaller cap buys far less coverage than it looks.
     # Env override: HOSTING_CAPACITY_NVENERGY_MAX_ROWS (_MAX_ROWS_OVERRIDE).
     "max_rows": 40000,
     "attribution": "NV Energy",
     "capacity_basis": ("MW of LOAD hosting capacity (LHC) on a distribution "
                        "LINE SECTION, from NV Energy's own DRP hosting-"
                        "capacity layer; the field alias is 'LHC (MW)', so the "
                        "unit is the publisher's. The GENERATION twin (GHC) "
                        "and the photovoltaic twin (PHC) sit in the same row "
                        "and are NOT read. NV clips the study at 'Over 20MW': "
                        "the 200 sections reported as exactly 20.0 are "
                        "CENSORED, not measured, and are excluded rather than "
                        "published as a maximum. Rows are line sections "
                        "(781.5 per feeder) folded to one row per feeder, and "
                        "THE FOLD IS NOT LOSSLESS — LHC varies along a feeder "
                        "(one feeder spans 0.0-20.0 MW across 4,110 "
                        "sections), so the row kept is the HIGHEST-capacity "
                        "section on that feeder AT THAT SECTION'S OWN "
                        "LOCATION. Read it as 'NV publishes this MW at this "
                        "point', never as the capacity of the whole feeder: "
                        "64.7% of all sections are below 1 MW. Coverage is the "
                        "capacity-DESC head, not the full territory."),
     "fields": {"feeder": "DRP_GIS_DATA.DRP_SECTIONS.SYN_FEEDER_ID",
                "substation": "DRP_GIS_DATA.DRP_SECTIONS.SYN_SUB_NAME",
                "state": None, "region": None,
                "voltage_kv": None,       # FDR_VLT is '24.9 KV' — unparseable
                "mw_max": ("DRP_GIS_DATA.DRP_HCA.LHC", 1.0),
                "mw_min": None, "queued_kw": None, "updated": None}},
    #
    # ★★ NV ENERGY GNA (Grid Needs Assessment) — TRANSMISSION/BANK side.
    # ★★ THIS SOURCE PUBLISHES NO NUMBER WE MAY REPEAT, for three independent
    # reasons measured on 2026-07-29:
    #   (1) EVERY NUMERIC-LOOKING FIELD IS A FORMATTED STRING. CRITERIA,
    #       INI_DEF, MAX_DEF and TND_RES_MARG are esriFieldTypeString holding
    #       values like '11,660 kVA'. _num() splits on commas and float()s each
    #       part, so '11,660 kVA' parses to [11.0] ('660 kVA' raises) and would
    #       be stored as 0.011 MW — a ~1000x understatement that looks
    #       perfectly plausible in a popup. One row is even mis-keyed at
    #       source ('10,5400 kVA' where SYMBOLOGY says 105400).
    #   (2) THE ONLY POSITIVE FIELD BELONGS TO A DIFFERENT FACILITY.
    #       TND_RES_MARG is the reserve margin of the PLANNED UPGRADE PROJECT
    #       (it has its own TND_PROJ_NAME and EST_COST), not of the row's own
    #       bank: HAZEN BK 1 has CRITERIA '270 kVA' and TND_RES_MARG
    #       '25,160 kVA' — 93x its own rating — because the margin belongs to
    #       the new Jersey Lane substation somewhere else. Attaching it to this
    #       row's point would put 25 MVA at the wrong place on the map.
    #   (3) INI_DEF / MAX_DEF ARE DEFICIENCIES, i.e. how far load EXCEEDS
    #       criteria. Publishing one as capacity would invert its sign.
    #   And the unit is kVA (apparent power) with no power factor published —
    #   the PECO problem on top of all of the above.
    # ★★ WHAT IS TRUSTWORTHY is the register itself: appearing here means NV
    # Energy's GNA has this bank/transformer FORECAST DEFICIENT, which
    # disqualifies the bus for new load rather than sizing it. So this is a
    # negative_signal source exactly like LADWP: mw_max/mw_min stay None,
    # never a parsed number, never a 0.
    # ★★ LAYER 70 IS A TABLE WITH NO GEOMETRY (type "Table", geometryType
    # null, 35 rows). Configuring it would have been a SILENT NO-OP:
    # map_feature() drops every row whose _rep_point() yields no lat. The
    # geometry-bearing GNA views are layers 9-17 (Substations GNA, points) and
    # 59-67 (Feeders GNA, polylines).
    # ★★ THE GNA LAYERS ARE YEAR-SLICED, one study year each — verified by
    # querying every one: L9=2025 (7 rows), L10=2026 (4), L11=2027 (6),
    # L12=2028 (6), L13=2029 (3), L14-L17 empty; the feeder siblings L59-L64
    # hold 2025-2030 (1-2 rows each). 26 transformer rows + 9 feeder rows = the
    # 35 in the table. One SOURCES entry is one layer, so this ingests the
    # NEAREST-TERM slice (2025, already at/over criteria and the largest) and
    # the later years are NOT ingested — stated in capacity_basis rather than
    # implied. ★ The where-clause PINS the year on purpose: if NV re-publishes
    # and the layer advances to 2026, this returns zero features and reports
    # status "empty" instead of quietly ingesting a different year's register
    # under a basis string that says 2025. A visibly empty ingest beats
    # confidently mislabelled rows.
    # ★ FAC_TYPE is 'Transformer' on all 26 rows of L9-L13 (the feeder-type
    # rows live on the L59+ layers), so the where-clause and the
    # negative_signal predicate are deliberately THE SAME TEST — a row that
    # reaches map_feature can never fail the flag and be silently dropped.
    # ★ key_extra: with no feeder id the key is the rounded point alone, and
    # one substation can carry several GNA records (ARDEN appears twice in the
    # 2027 layer with two alternative projects). FAC_ID + SEASON_ID make a key
    # collision mean genuinely the same record.
    # ★ QC_PASS exists on the table but NOT on layer 9 — requesting it is what
    # produced the 400 above. Only fields present on the joined view are named.
    {"utility": "NV Energy (GNA bank deficiency register) — flag only",
     "key": "nvenergy_gna",
     "capacity_type": "bus_headroom",
     "country": "US",
     "url": ("https://services.nvenergy.com/GISSvc/1.0/retrieveMapInfoDRP/"
             "arcgis/rest/services/DRP/DRP_WebApp/MapServer/9/query"),
     "where": ("DRP_GIS_DATA.DRP_GNA.DRP_YEAR = '2025' AND "
               "DRP_GIS_DATA.DRP_GNA.FAC_TYPE = 'Transformer'"),
     "negative_signal": {"field": "DRP_GIS_DATA.DRP_GNA.FAC_TYPE",
                         "value": "Transformer"},
     "key_extra": ("DRP_GIS_DATA.DRP_GNA.FAC_ID",
                   "DRP_GIS_DATA.DRP_GNA.SEASON_ID"),
     # No capacity field to order by; a named field keeps paging stable.
     "order_field": "DRP_GIS_DATA.DRP_GNA.FAC_ID",
     "delay_s": 2.0,
     "attribution": "NV Energy",
     "capacity_basis": ("DISQUALIFYING FLAG ONLY — no capacity figure. NV "
                        "Energy's Grid Needs Assessment lists transformer "
                        "banks it forecasts DEFICIENT (load above criteria), "
                        "so presence here argues AGAINST new load at that bus "
                        "and sizes nothing. No number is stored, and three "
                        "independent reasons say none may be: every "
                        "numeric-looking field is a formatted STRING "
                        "('11,660 kVA', which the numeric parser would read as "
                        "0.011 MW); the only positive field, the T&D upgrade "
                        "reserve margin, belongs to a PLANNED PROJECT at "
                        "another facility (one bank rated 270 kVA carries a "
                        "25,160 kVA project margin); and the deficiency fields "
                        "measure how far load EXCEEDS criteria, so publishing "
                        "one as capacity would invert its sign. Units would "
                        "have been kVA with no published power factor in any "
                        "case. Scope: the 2025 study year only (7 banks) — NV "
                        "slices the register into one layer per year and this "
                        "is the nearest-term slice; 2026-2029 are not "
                        "ingested. Present-only: the absence of a flag is not "
                        "evidence of available bus headroom."),
     "fields": {"feeder": None,
                "substation": "DRP_GIS_DATA.DRP_GNA.SUBSTATION",
                "state": None, "region": "DRP_GIS_DATA.DRP_GNA.REGION",
                "voltage_kv": None,
                "mw_max": None, "mw_min": None,
                "queued_kw": None, "updated": None}},
    {
     "key": "ausgrid_uhc_primary_load",
     "capacity_type": "load",
     "country": "AU",
     "url": ("https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
             "Ausgrid_UHC_Data/FeatureServer/0/query"),
     # A LITERAL year, not a clock read. SOURCES must stay evaluable from a bare
     # literal (the contract harness execs it with only `os` in scope), and a
     # self-advancing pin would silently select a year Ausgrid may not publish.
     # test_ausgrid_forecast_year_pin_is_current fails when this needs bumping.
     "where": "year = 2026",
     "order_field": "available_capacity__load__at_n_",
     "delay_s": 2.0,
     "attribution": ("Ausgrid \u2014 Unlocking Hosting Capacity (UHC) "
                     "transmission hosting-capacity data, published via the NSW "
                     "Government data portal (data.nsw.gov.au, dataset "
                     "80dbe042b881417c94ea34e229ce920b). The service publishes "
                     "no copyright or licence string of its own."),
     "capacity_basis": ("Available LOAD capacity at N-1 on an Ausgrid PRIMARY (sub-transmission) substation, 216 substations, from Ausgrid's own UHC layer. "
                          "\u2605 UNITS ARE NOT PUBLISHED BY AUSGRID and are NOT asserted as MW "
                          "here. The layer carries no description and no copyrightText, "
                          "and the field alias is bare. Australian distribution-planning "
                          "practice (AER DAPR guidelines) states available substation "
                          "capacity in MVA \u2014 apparent power \u2014 and the observed "
                          "range (0.0-420.1 at 132 kV) fits MVA and MW equally, so the "
                          "numbers cannot settle it. Treated as MVA: MVA >= MW at any "
                          "power factor below unity, so reading it as MW would OVER-state "
                          "deliverable real power \u2014 the direction this repo refuses. "
                          "Same handling as the PECO layer. Converting needs the "
                          "substation power factor, which Ausgrid does not publish. "
                          "\u2605 A FORECAST, NOT A LIVE READING, AND NOT FRESH: every row "
                          "carries extract_date=20240627 (Ausgrid extracted it on "
                          "2024-06-27) while the year column spans 2025-2034. The row "
                          "ingested is the pinned year below \u2014 a value projected "
                          "about two years earlier for a year that has since arrived. It "
                          "does not bind live and is never present-day measured headroom. "
                          "\u2605 ONE ROW PER SUBSTATION PER FORECAST YEAR upstream: 2,160 = "
                          "216 substations x 10 years (primary), 2,120 = 212 x 10 "
                          "(secondary). The where-clause pins ONE year, so rows == "
                          "substations and nothing is folded; unfiltered it would publish "
                          "ten futures of one substation as ten assets. Capacity DECLINES "
                          "across the forecast (Chullora STSS load 420.1 in 2025 -> 409.5 "
                          "in 2030), so the pinned year is the highest of the ten: right "
                          "for \"available now\", wrong for any later year. The pin is a "
                          "LITERAL, fenced by test_ausgrid_forecast_year_pin_is_current \u2014 "
                          "rolling it forward requires re-verifying Ausgrid still "
                          "publishes that year, because an empty year would publish "
                          "\"no capacity\" where the truth is \"this forecast expired\". "
                          "N-1 basis: capacity available with one element out of service \u2014 "
                          "the planning standard, not nameplate. Geometry is published "
                          "in EPSG:3857 and requested as EPSG:4326."),
     "fields": {"feeder": None, "substation": "substation", "state": None,
                "region": None, "voltage_kv": "voltage_level_primary",
                "mw_max": ("available_capacity__load__at_n_", 1.0), "mw_min": None,
                "queued_kw": None, "updated": None},
     "utility": "Ausgrid NSW (Sydney/Hunter/Central Coast) \u2014 primary, load",
    },
    {
     "key": "ausgrid_uhc_primary_gen",
     "capacity_type": "gen",
     "country": "AU",
     "url": ("https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
             "Ausgrid_UHC_Data/FeatureServer/0/query"),
     # A LITERAL year, not a clock read. SOURCES must stay evaluable from a bare
     # literal (the contract harness execs it with only `os` in scope), and a
     # self-advancing pin would silently select a year Ausgrid may not publish.
     # test_ausgrid_forecast_year_pin_is_current fails when this needs bumping.
     "where": "year = 2026",
     "order_field": "available_capacity__generation_",
     "delay_s": 2.0,
     "attribution": ("Ausgrid \u2014 Unlocking Hosting Capacity (UHC) "
                     "transmission hosting-capacity data, published via the NSW "
                     "Government data portal (data.nsw.gov.au, dataset "
                     "80dbe042b881417c94ea34e229ce920b). The service publishes "
                     "no copyright or licence string of its own."),
     "capacity_basis": ("Available GENERATION hosting capacity at N-1 on an Ausgrid PRIMARY (sub-transmission) substation, 216 substations. DER injection headroom \u2014 it does NOT serve new demand. "
                          "\u2605 UNITS ARE NOT PUBLISHED BY AUSGRID and are NOT asserted as MW "
                          "here. The layer carries no description and no copyrightText, "
                          "and the field alias is bare. Australian distribution-planning "
                          "practice (AER DAPR guidelines) states available substation "
                          "capacity in MVA \u2014 apparent power \u2014 and the observed "
                          "range (0.0-420.1 at 132 kV) fits MVA and MW equally, so the "
                          "numbers cannot settle it. Treated as MVA: MVA >= MW at any "
                          "power factor below unity, so reading it as MW would OVER-state "
                          "deliverable real power \u2014 the direction this repo refuses. "
                          "Same handling as the PECO layer. Converting needs the "
                          "substation power factor, which Ausgrid does not publish. "
                          "\u2605 A FORECAST, NOT A LIVE READING, AND NOT FRESH: every row "
                          "carries extract_date=20240627 (Ausgrid extracted it on "
                          "2024-06-27) while the year column spans 2025-2034. The row "
                          "ingested is the pinned year below \u2014 a value projected "
                          "about two years earlier for a year that has since arrived. It "
                          "does not bind live and is never present-day measured headroom. "
                          "\u2605 ONE ROW PER SUBSTATION PER FORECAST YEAR upstream: 2,160 = "
                          "216 substations x 10 years (primary), 2,120 = 212 x 10 "
                          "(secondary). The where-clause pins ONE year, so rows == "
                          "substations and nothing is folded; unfiltered it would publish "
                          "ten futures of one substation as ten assets. Capacity DECLINES "
                          "across the forecast (Chullora STSS load 420.1 in 2025 -> 409.5 "
                          "in 2030), so the pinned year is the highest of the ten: right "
                          "for \"available now\", wrong for any later year. The pin is a "
                          "LITERAL, fenced by test_ausgrid_forecast_year_pin_is_current \u2014 "
                          "rolling it forward requires re-verifying Ausgrid still "
                          "publishes that year, because an empty year would publish "
                          "\"no capacity\" where the truth is \"this forecast expired\". "
                          "N-1 basis: capacity available with one element out of service \u2014 "
                          "the planning standard, not nameplate. Geometry is published "
                          "in EPSG:3857 and requested as EPSG:4326."),
     "fields": {"feeder": None, "substation": "substation", "state": None,
                "region": None, "voltage_kv": "voltage_level_primary",
                "mw_max": ("available_capacity__generation_", 1.0), "mw_min": None,
                "queued_kw": None, "updated": None},
     "utility": "Ausgrid NSW (Sydney/Hunter/Central Coast) \u2014 primary, generation",
    },
    {
     "key": "ausgrid_uhc_secondary_load",
     "capacity_type": "load",
     "country": "AU",
     "url": ("https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
             "Ausgrid_UHC_Data/FeatureServer/1/query"),
     # A LITERAL year, not a clock read. SOURCES must stay evaluable from a bare
     # literal (the contract harness execs it with only `os` in scope), and a
     # self-advancing pin would silently select a year Ausgrid may not publish.
     # test_ausgrid_forecast_year_pin_is_current fails when this needs bumping.
     "where": "year = 2026",
     "order_field": "available_capacity__load__at_n_",
     "delay_s": 2.0,
     "attribution": ("Ausgrid \u2014 Unlocking Hosting Capacity (UHC) "
                     "transmission hosting-capacity data, published via the NSW "
                     "Government data portal (data.nsw.gov.au, dataset "
                     "80dbe042b881417c94ea34e229ce920b). The service publishes "
                     "no copyright or licence string of its own."),
     "capacity_basis": ("Available LOAD capacity at N-1 on an Ausgrid SECONDARY (zone) substation, 212 substations. voltage_kv is the HV (primary) side \u2014 33-132 kV \u2014 because that is the side the N-1 capacity is assessed on; the LV side is 11 kV on every row. "
                          "\u2605 UNITS ARE NOT PUBLISHED BY AUSGRID and are NOT asserted as MW "
                          "here. The layer carries no description and no copyrightText, "
                          "and the field alias is bare. Australian distribution-planning "
                          "practice (AER DAPR guidelines) states available substation "
                          "capacity in MVA \u2014 apparent power \u2014 and the observed "
                          "range (0.0-420.1 at 132 kV) fits MVA and MW equally, so the "
                          "numbers cannot settle it. Treated as MVA: MVA >= MW at any "
                          "power factor below unity, so reading it as MW would OVER-state "
                          "deliverable real power \u2014 the direction this repo refuses. "
                          "Same handling as the PECO layer. Converting needs the "
                          "substation power factor, which Ausgrid does not publish. "
                          "\u2605 A FORECAST, NOT A LIVE READING, AND NOT FRESH: every row "
                          "carries extract_date=20240627 (Ausgrid extracted it on "
                          "2024-06-27) while the year column spans 2025-2034. The row "
                          "ingested is the pinned year below \u2014 a value projected "
                          "about two years earlier for a year that has since arrived. It "
                          "does not bind live and is never present-day measured headroom. "
                          "\u2605 ONE ROW PER SUBSTATION PER FORECAST YEAR upstream: 2,160 = "
                          "216 substations x 10 years (primary), 2,120 = 212 x 10 "
                          "(secondary). The where-clause pins ONE year, so rows == "
                          "substations and nothing is folded; unfiltered it would publish "
                          "ten futures of one substation as ten assets. Capacity DECLINES "
                          "across the forecast (Chullora STSS load 420.1 in 2025 -> 409.5 "
                          "in 2030), so the pinned year is the highest of the ten: right "
                          "for \"available now\", wrong for any later year. The pin is a "
                          "LITERAL, fenced by test_ausgrid_forecast_year_pin_is_current \u2014 "
                          "rolling it forward requires re-verifying Ausgrid still "
                          "publishes that year, because an empty year would publish "
                          "\"no capacity\" where the truth is \"this forecast expired\". "
                          "N-1 basis: capacity available with one element out of service \u2014 "
                          "the planning standard, not nameplate. Geometry is published "
                          "in EPSG:3857 and requested as EPSG:4326."),
     "fields": {"feeder": None, "substation": "substation", "state": None,
                "region": None, "voltage_kv": "voltage_level_primary",
                "mw_max": ("available_capacity__load__at_n_", 1.0), "mw_min": None,
                "queued_kw": None, "updated": None},
     "utility": "Ausgrid NSW (Sydney/Hunter/Central Coast) \u2014 zone, load",
    },
    {
     "key": "ausgrid_uhc_secondary_gen",
     "capacity_type": "gen",
     "country": "AU",
     "url": ("https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
             "Ausgrid_UHC_Data/FeatureServer/1/query"),
     # A LITERAL year, not a clock read. SOURCES must stay evaluable from a bare
     # literal (the contract harness execs it with only `os` in scope), and a
     # self-advancing pin would silently select a year Ausgrid may not publish.
     # test_ausgrid_forecast_year_pin_is_current fails when this needs bumping.
     "where": "year = 2026",
     "order_field": "available_capacity__generation_",
     "delay_s": 2.0,
     "attribution": ("Ausgrid \u2014 Unlocking Hosting Capacity (UHC) "
                     "transmission hosting-capacity data, published via the NSW "
                     "Government data portal (data.nsw.gov.au, dataset "
                     "80dbe042b881417c94ea34e229ce920b). The service publishes "
                     "no copyright or licence string of its own."),
     "capacity_basis": ("Available GENERATION hosting capacity at N-1 on an Ausgrid SECONDARY (zone) substation, 212 substations. DER injection headroom; does NOT serve new demand. voltage_kv is the HV (primary) side. "
                          "\u2605 UNITS ARE NOT PUBLISHED BY AUSGRID and are NOT asserted as MW "
                          "here. The layer carries no description and no copyrightText, "
                          "and the field alias is bare. Australian distribution-planning "
                          "practice (AER DAPR guidelines) states available substation "
                          "capacity in MVA \u2014 apparent power \u2014 and the observed "
                          "range (0.0-420.1 at 132 kV) fits MVA and MW equally, so the "
                          "numbers cannot settle it. Treated as MVA: MVA >= MW at any "
                          "power factor below unity, so reading it as MW would OVER-state "
                          "deliverable real power \u2014 the direction this repo refuses. "
                          "Same handling as the PECO layer. Converting needs the "
                          "substation power factor, which Ausgrid does not publish. "
                          "\u2605 A FORECAST, NOT A LIVE READING, AND NOT FRESH: every row "
                          "carries extract_date=20240627 (Ausgrid extracted it on "
                          "2024-06-27) while the year column spans 2025-2034. The row "
                          "ingested is the pinned year below \u2014 a value projected "
                          "about two years earlier for a year that has since arrived. It "
                          "does not bind live and is never present-day measured headroom. "
                          "\u2605 ONE ROW PER SUBSTATION PER FORECAST YEAR upstream: 2,160 = "
                          "216 substations x 10 years (primary), 2,120 = 212 x 10 "
                          "(secondary). The where-clause pins ONE year, so rows == "
                          "substations and nothing is folded; unfiltered it would publish "
                          "ten futures of one substation as ten assets. Capacity DECLINES "
                          "across the forecast (Chullora STSS load 420.1 in 2025 -> 409.5 "
                          "in 2030), so the pinned year is the highest of the ten: right "
                          "for \"available now\", wrong for any later year. The pin is a "
                          "LITERAL, fenced by test_ausgrid_forecast_year_pin_is_current \u2014 "
                          "rolling it forward requires re-verifying Ausgrid still "
                          "publishes that year, because an empty year would publish "
                          "\"no capacity\" where the truth is \"this forecast expired\". "
                          "N-1 basis: capacity available with one element out of service \u2014 "
                          "the planning standard, not nameplate. Geometry is published "
                          "in EPSG:3857 and requested as EPSG:4326."),
     "fields": {"feeder": None, "substation": "substation", "state": None,
                "region": None, "voltage_kv": "voltage_level_primary",
                "mw_max": ("available_capacity__generation_", 1.0), "mw_min": None,
                "queued_kw": None, "updated": None},
     "utility": "Ausgrid NSW (Sydney/Hunter/Central Coast) \u2014 zone, generation",
    },
]

# ── NOT INGESTED, tier 1 (2026-07-29) ───────────────────────────────────
# FirstEnergy Ohio (Ohio Edison / The Illuminating Company / Toledo
# Edison) — Cleveland/Akron/Toledo. NO PUBLIC ENDPOINT EXISTS. The
# landing page (firstenergycorp.com/feconnect/ohio-interconnection/
# oh-hosting-capacity-map.html, HTTP 200) offers exactly one GIS link:
# gisdxportal.fenetwork.com/portal/apps/experiencebuilder/... &draft=true
# — and that host is NXDOMAIN on public DNS (re-checked 2026-07-29:
# gisdxportal.fenetwork.com, fenetwork.com, gis./arcgis.firstenergycorp.com
# all fail to resolve while google.com resolves in the same run). The AGOL
# item returns CONT_0001 "Item does not exist or is inaccessible". This is
# NOT a robots/ToS block — robots.txt does not disallow the path. It is
# plain unreachability, so there is deliberately NO SOURCES entry: config
# pointing at an NXDOMAIN host would fail silently on every run and read
# as an empty ingest. Needs a human to obtain the public URL.

# Attribution and basis travel WITH the data, not just in this file: these
# maps are keyed by the same `utility` string stored on every row, and the
# serving endpoints below attach them to each feeder they return. Several
# operators require attribution as a condition of use (NV Energy's terms
# do so explicitly and forbid removing the copyright notice), so this must
# stay a data path, never a comment.
_SOURCE_ATTRIBUTION = {s["utility"]: s["attribution"]
                       for s in SOURCES if s.get("attribution")}
_SOURCE_CAPACITY_BASIS = {s["utility"]: s["capacity_basis"]
                          for s in SOURCES if s.get("capacity_basis")}
# Verified-but-EXCLUDED (agent probe 2026-07-27; the two entries this list
# used to carry for PSE&G and PECO were RESOLVED on 2026-07-29 and both now
# ship — see their SOURCES entries for the proofs): BGE EPRI service
# (MW + substation-level but DATE_GIS_UPDATED reads 2022 — freshness
# unproven), NGrid MA MASDP_Feeders (5-yr peak-MVA forecast — excellent,
# but headroom needs a rating−peak derivation the mapper can't express
# yet), PSEG Long Island (org-restricted 403), Eversource's OWN host
# (times out; the Cadmus mirror stands in), Consumers Energy MI
# (feeder/substation/voltage buried in a JSON string column — v2),
# PGE Oregon (feeder names *REDACTED*, no true HC field), Ameren IL GEN +
# subtransmission (redundant with LOAD / null feeder), Avista DER polyline
# (509k rows, no substation/voltage/date). NOT public: PSE (email request),
# PacifiCorp, Hawaiian Electric (non-Esri), all Texas (no ERCOT DER-HC
# mandate), SRP/TEP/SMUD (none found), Ameren MO (none),
# JCP&L/PPL/United Illuminating/Unitil/GMP/CMP (no public REST service).
# ★★ "NV Energy (login)" and "ComEd (secured-proxy 403)" were BOTH WRONG and
# both now ship: NV's DRP_WebApp MapServer answers anonymous GETs, and ComEd's
# 403 is a missing-Referer 403, not an authorisation wall (2026-07-29).
# ★ Xcel renames services MONTHLY — publisher account HCAPublisher_xeago
# (org eM84fwjsSggLQk61); re-resolve newest Feature Service by `modified`
# rather than by name pattern when the candidates go stale. ComEd rotates the
# same way and now has a real runtime resolver (_resolve_agol_item_search);
# Xcel's url_candidates are the older, weaker form of the same fix.
# NGrid-MA (MASDP): probed 2026-07-27 → HTTP 403 (folder forbidden, unlike
# NYSDP). Not ingested — do not guess. Georgia Power remains excluded on
# terms (its tool marks results confidential).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosting_capacity_feeders (
    id           BIGSERIAL PRIMARY KEY,
    utility      TEXT NOT NULL,
    feeder_key   TEXT NOT NULL,
    feeder_id    TEXT,
    substation   TEXT,
    state        TEXT,
    region       TEXT,
    voltage_kv   DOUBLE PRECISION,
    capacity_mw_max DOUBLE PRECISION,
    capacity_mw_min DOUBLE PRECISION,
    queued_gen_kw   DOUBLE PRECISION,
    lat          DOUBLE PRECISION,
    lng          DOUBLE PRECISION,
    src_updated  TEXT,
    capacity_type TEXT NOT NULL DEFAULT 'gen',
    -- ISO-3166-1 alpha-2. Defaults to US because every row that predates this
    -- column IS US, but SOURCES entries must DECLARE it (check_source_contract)
    -- so a new source can never inherit a geography nobody chose -- the same
    -- lesson capacity_type taught when it defaulted to 'gen'.
    country TEXT NOT NULL DEFAULT 'US',
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_hcf_feeder
    ON hosting_capacity_feeders (utility, feeder_key);
CREATE INDEX IF NOT EXISTS ix_hcf_latlng
    ON hosting_capacity_feeders (lat, lng);
"""


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


# ★★ RETRACTED utility labels — stored rows under these labels were published
# under a WRONG MEANING and must be deleted, not merely re-typed. The upsert
# has no delete path (ON CONFLICT keeps orphans forever — the stale-twin
# trap), so retraction is an explicit step. Entries are exact `utility`
# strings; each names its evidence:
#   "Avista (bus headroom)" — a generation-interconnection study (publisher's
#   own AGOL metadata: "GenerationInterconnectionHeatMap") stored as per-bus
#   FLATTERING MAXIMA of a pass/fail flag, typed as transmission bus
#   headroom. Confirmed against the live table 2026-07-30: every stored value
#   was one of the nine discrete study sizes, and bus 'Huetter' read 200 MW
#   while failing 2/10 constraints at 20 MW. Re-ingested honestly as
#   "Avista (gen interconnection study)" (key avista_bus, capacity_type gen,
#   per-constraint keys) — the new label lands under new keys, so the old
#   rows would otherwise sit beside it forever.
_RETRACTED_UTILITIES = (
    "Avista (bus headroom)",
)


def run_capacity_retractions() -> dict:
    """Delete rows stored under retracted utility labels. Idempotent: after
    the first pass every DELETE matches nothing. Fail-soft (a DB problem
    reports itself and never blocks the ingest that follows), and runs
    BEFORE the weekly gate — a retraction is a correction, not an ingest,
    and must not wait for the next crawl window."""
    out: dict = {"deleted": {}}
    if not _RETRACTED_UTILITIES:
        return out
    c = _conn()
    if c is None:
        out["status"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            for label in _RETRACTED_UTILITIES:
                cur.execute("DELETE FROM hosting_capacity_feeders "
                            "WHERE utility = %s", (label,))
                if cur.rowcount:
                    out["deleted"][label] = cur.rowcount
        c.commit()
        if out["deleted"]:
            logger.warning("hosting_capacity: RETRACTED mislabelled rows: %s",
                           out["deleted"])
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        # e.g. the table does not exist yet on a fresh DB — nothing to retract
        out["status"] = "retraction_failed: %s" % str(e)[:80]
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def check_source_contract(src: dict) -> str | None:
    """Return a REASON STRING if `src` must not be ingested, else None.

    Enforced at runtime (not only in tests) so a mislabelled or
    mis-granular source is refused loudly instead of writing wrong rows.
    Mislabelling gen as load is the worst defect this table can carry:
    it turns "what a feeder can EXPORT to solar" into "what your data
    centre can DRAW", which is the number a site-selection team acts on.
    """
    key = src.get("key")
    ct = src.get("capacity_type")
    if ct not in _ALLOWED_CAPACITY_TYPES:
        return ("capacity_type %r is not one of %s — every source must "
                "DECLARE what its megawatts mean" % (ct, list(_ALLOWED_CAPACITY_TYPES)))
    fields = src.get("fields") or {}
    # ★ bus_headroom gets the SAME gen-field protection as load, not less:
    # both assert "the RIGHT physical quantity" (what can be drawn / what a
    # bus holds), so a known GENERATION field mapped into either is a
    # mis-wire. The old `if ct == "load"` exempted bus_headroom entirely —
    # the gap that let a generation-interconnection study ship typed as
    # transmission bus headroom (Avista, retyped 2026-07-30).
    if ct in ("load", "bus_headroom"):
        for slot in ("mw_max", "mw_min"):
            spec = fields.get(slot)
            name = spec[0] if isinstance(spec, tuple) else spec
            if name and name in _GEN_ONLY_FIELDS:
                return ("%s maps generation field %r into %s but declares "
                        "capacity_type %r" % (key, name, slot, ct))
    gran = _ROW_NOT_FEEDER_SOURCES.get(key)
    if gran:
        granularity, knob = gran
        if knob == "feeder_field":
            if not fields.get("feeder"):
                return ("%s rows are %s, so a feeder field is required to "
                        "count feeders apart from rows" % (key, granularity))
        elif not src.get(knob):
            return ("%s rows are %s and %r is not declared" %
                    (key, granularity, knob))
    # A registered censoring ceiling must actually be excluded by the query.
    # Enforced at RUNTIME, not only in tests: the failure mode is publishing a
    # censored row as a measured maximum, which no downstream reader can detect.
    ceil = _CENSORING_CEILINGS.get(key)
    if ceil:
        field, value, _evidence = ceil
        where = (src.get("where") or "").replace(" ", "")
        if ("%s<%s" % (field, value)).replace(" ", "") not in where:
            return ("%s has a censoring ceiling at %s = %s (%s) but its "
                    "where-clause %r does not exclude it — those rows are "
                    "unmeasured, not equal to %s"
                    % (key, field, value, _evidence, src.get("where"), value))

    # ── geography, LAST ──
    # Declared, never defaulted: a source that inherits "US" by omission would
    # land non-US feeders inside the US-only total_feeders figure on /coverage,
    # which is unit-mixing a caller cannot see.
    # ★ Ordered last DELIBERATELY. An earlier draft checked this first, and a
    # source with BOTH a mislabelled generation field and no country reported
    # only the missing country — the country message masked the units bug, which
    # is the more dangerous of the two. Two sibling contract tests caught it by
    # asserting on the specific refusal text. Cheap-and-structural checks last,
    # meaning-of-the-megawatts checks first.
    cc = src.get("country")
    if cc not in _ALLOWED_COUNTRIES:
        return ("country %r is not one of %s — every source must DECLARE the "
                "geography its rows describe (ISO-3166-1 alpha-2)"
                % (cc, list(_ALLOWED_COUNTRIES)))
    return None


def _source_last_ingested() -> dict:
    """utility -> MAX(ingested_at), for staleness ordering. {} if unavailable."""
    c = _conn()
    if c is None:
        return {}
    try:
        with c.cursor() as cur:
            cur.execute("SELECT utility, MAX(ingested_at) "
                        "FROM hosting_capacity_feeders GROUP BY utility")
            return {u: t for u, t in cur.fetchall() if u}
    except Exception:
        return {}
    finally:
        try:
            c.close()
        except Exception:
            pass


def _ingest_order():
    """LOAD sources first, then STALEST first inside each group.

    ★★ 2026-07-30 — WHY STALENESS AND NOT DECLARATION ORDER. This used to be
    `sorted(SOURCES, key=load-first)` with declaration order as the stable
    tie-break, which made the tail of the load group UNREACHABLE IN PRACTICE, not
    merely last. Measured that day:

      · comed_ev_load and nvenergy_lhc had been configured, correct, and live in
        SOURCES for days with ZERO rows in the table.
      · They sit 11th and 12th of 12 load sources, behind ~240k rows of
        Dominion / SCE / SDG&E / PSE&G.
      · A forced run reached exactly ONE source — aep_load, position 1 — wrote
        20,000 rows at 08:05:23, and died.
      · It died because this ingest is an in-process daemon THREAD ON THE WEB
        SERVICE, and the web service redeployed at 08:06:44. Railway showed SIX
        deploys in ELEVEN MINUTES that morning; the budget is 300s. On an active
        repo the process is replaced far more often than a full pass takes.

    Fixed declaration order + a process that rarely survives a full pass = the
    run always restarts at position 1, re-does the same head, and the tail is
    never reached. Raising the budget does not fix that; it just makes the window
    the deploy has to hit slightly wider.

    Staleness-first makes progress MONOTONIC under repeated kills: whatever a
    killed run did not refresh is what the next run starts with, so the frontier
    advances no matter how often the process dies. A never-ingested source sorts
    FIRST (epoch), which is exactly the ComEd / NV Energy case.

    Fail-soft: if the DB is unreachable, _source_last_ingested() returns {} and
    every source sorts equal, degrading to the previous declaration order rather
    than refusing to run.
    """
    last = _source_last_ingested()
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

    def _key(s):
        seen = last.get(s.get("utility"))
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=datetime.timezone.utc)
        return (0 if s.get("capacity_type") == "load" else 1, seen or epoch)

    return sorted(SOURCES, key=_key)


def _explode_features(feat: dict, src: dict, stats: dict) -> list:
    """Multipart geometry → one single-part feature per part.

    ★ Why this exists: _rep_point() takes paths[0] and returns ITS midpoint.
    For a layer dissolved on attributes — Dominion's EV layer is one row per
    (capacity, line voltage, source voltage) triple, verified 5,546 rows ==
    5,546 distinct triples on 2026-07-29 — a single row aggregates every
    segment in the territory sharing those values. OBJECTID 1 carries 38
    paths spanning lon -79.367..-76.290 / lat 35.823..38.602, so the naive
    read keeps one midpoint and discards the rest. Since that source
    publishes no feeder id, the spatial join is the ONLY join, which makes
    this fatal rather than cosmetic.

    Off by default: for one-row-per-section layers the parts are already
    single and exploding would be a no-op.
    """
    if not src.get("explode_multipart"):
        return [feat]
    geom = feat.get("geometry") or {}
    kind = "paths" if geom.get("paths") else ("rings" if geom.get("rings") else None)
    if not kind:
        return [feat]
    parts = geom.get(kind) or []
    if len(parts) < 2:
        return [feat]
    kept = parts[:_MAX_PARTS_PER_FEATURE]
    dropped = len(parts) - len(kept)
    if dropped:
        # counted, then surfaced as parts_dropped — a cap that silently ate
        # geometry would be indistinguishable from a sparse territory.
        stats["parts_dropped"] = stats.get("parts_dropped", 0) + dropped
    attrs = feat.get("attributes") or {}
    return [{"attributes": attrs, "geometry": {kind: [p]}} for p in kept]


def _dedupe_rows(rows: list, src: dict) -> tuple:
    """Collapse rows that are the SAME feeder repeated per GIS vertex.

    Only for sources verified to publish identical attributes on every
    vertex of a circuit (PSE&G NJ is the known ~285x case, on
    base_circuitid). Keeps the FIRST row per key — deliberately not the
    max, because "keep the biggest" would hide a negative capacity behind
    a positive sibling. ★ Negative values are REAL on load layers (the
    circuit is over-subscribed); they are never clamped and never dropped.

    Trades the circuit's geometric extent for an honest feeder count, so it
    is enabled per source only where the duplication is pure.
    """
    field = src.get("dedupe_key")
    if not field:
        return rows, 0
    seen, out = set(), []
    for r in rows:
        k = r.get("_dedupe")
        if k in (None, ""):
            out.append(r)          # unkeyed rows cannot be folded — kept
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, len(rows) - len(out)


def _rep_point(geom: dict):
    """Representative point from an ArcGIS polyline/polygon/point geom."""
    try:
        if "y" in geom:
            return float(geom["y"]), float(geom["x"])
        paths = geom.get("paths") or geom.get("rings")
        if paths and paths[0]:
            pts = paths[0]
            mid = pts[len(pts) // 2]
            return float(mid[1]), float(mid[0])
    except Exception:
        pass
    return None, None


def _num(v, scale=1.0):
    """Tolerant numeric parse. Some utilities publish capacity as a STRING,
    and Ameren publishes a COMMA-SEPARATED LIST of values per cell
    (e.g. "9.9,9.9,5.31,2.08,0.85"). For a list we return the MAX; the
    caller pairs it with _num_lo() for the binding MIN so the range is
    shown honestly rather than a single flattering number."""
    vals = _num_all(v, scale)
    return max(vals) if vals else None


def _num_lo(v, scale=1.0):
    vals = _num_all(v, scale)
    return min(vals) if vals else None


def _num_all(v, scale=1.0):
    if v is None:
        return []
    if isinstance(v, (int, float)):
        try:
            return [round(float(v) * scale, 3)]
        except (TypeError, ValueError):
            return []
    out = []
    for part in str(v).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(round(float(part) * scale, 3))
        except ValueError:
            continue
    return out


def map_feature(feat: dict, src: dict) -> dict | None:
    """Pure: one ArcGIS feature → a hosting_capacity_feeders row dict."""
    attrs = feat.get("attributes") or {}
    f = src["fields"]

    def g(spec, scale=1.0):
        if spec is None:
            return None
        if isinstance(spec, tuple):
            return _num(attrs.get(spec[0]), spec[1])
        return attrs.get(spec)

    lat, lng = _rep_point(feat.get("geometry") or {})
    if lat is None:
        return None
    mw_max = g(f["mw_max"])
    # ★★ negative_signal sources publish NO number we are allowed to repeat —
    # only a disqualifying flag. LADWP bins capacity into text ranges and
    # badges its top 4.8 kV bin, at most 0.6 MW, "High Capacity"; relaying
    # that as a capacity figure would mislead a site-selection team. Such a
    # source is ingested for the FLAG alone: capacity stays NULL — never 0,
    # never a bin midpoint. Without this branch the mw_max guard below would
    # drop every one of its rows and the source would be a silent no-op.
    neg = src.get("negative_signal")
    if mw_max is None and neg:
        _flag = str(attrs.get(neg["field"]) or "").strip().lower()
        if _flag != str(neg["value"]).strip().lower():
            return None      # not flagged → nothing trustworthy to store
    elif mw_max is None:
        return None
    # Multi-value cell (Ameren): pair the max with its binding min so the
    # popup can show a range instead of only the flattering number.
    mw_min = g(f["mw_min"]) if f["mw_min"] else None
    if mw_min is None and f["mw_max"] is not None:
        _spec = f["mw_max"]
        _raw = attrs.get(_spec[0] if isinstance(_spec, tuple) else _spec)
        _scale = _spec[1] if isinstance(_spec, tuple) else 1.0
        _lo = _num_lo(_raw, _scale)
        if _lo is not None and _lo != mw_max:
            mw_min = _lo
    feeder_id = g(f["feeder"])
    # ★★ id_is_multi: some utilities pack SEVERAL circuit ids into one cell
    # when a tile overlaps several circuits. Duke Ohio does it on 2,471 of
    # 7,451 rows, and the parallel value lists do not even align positionally
    # (a live row carries 4 ids against 3 ratings), so no value can honestly
    # be attributed to a named circuit. Storing the raw cell would invent a
    # feeder called "H4920580052, H492102000A, ..." and corrupt feeder_key, so
    # those rows carry NO feeder id — they stay unidentified rather than
    # fabricated, and distinct_feeders under-states instead of inflating.
    if (src.get("id_is_multi") and isinstance(feeder_id, str)
            and "," in feeder_id):
        feeder_id = None
    # key_extra: for a source with NO feeder id the key is only the rounded
    # point (~11 m). Two different capacity values landing on one key would
    # be silently collapsed to whichever the capacity-DESC order saw first —
    # i.e. to the flattering one. Appending the source's own discriminating
    # attributes makes a collision mean genuinely identical data.
    extra = ""
    for attr_name in (src.get("key_extra") or ()):
        extra += "|%s" % (attrs.get(attr_name),)
    row = {"utility": src["utility"],
           "feeder_key": f"{feeder_id or ''}|{round(lat,4)},{round(lng,4)}{extra}",
           "feeder_id": str(feeder_id) if feeder_id is not None else None,
           "substation": g(f["substation"]),
           "state": g(f["state"]), "region": g(f["region"]),
           "voltage_kv": _num(g(f["voltage_kv"])),
           "capacity_mw_max": mw_max,
           "capacity_mw_min": mw_min,
           "queued_gen_kw": _num(g(f["queued_kw"])),
           "lat": lat, "lng": lng,
           "src_updated": str(g(f["updated"]) or "")[:40] or None,
           # gen = DER/generation hosting capacity (the common utility
           # publication); load = load-serving capacity (what a data
           # center needs); bus_headroom = transmission bus MW available.
           # No .get() default any more: every entry DECLARES its type and
           # check_source_contract() refuses the source otherwise, so a new
           # source can never inherit a meaning nobody chose.
           "capacity_type": src["capacity_type"],
           # Same rule as capacity_type: DECLARED, never defaulted. src["country"]
           # not src.get(), so a source missing it raises here instead of quietly
           # landing in the US-only total_feeders figure.
           "country": src["country"]}
    if src.get("dedupe_key"):
        # carried out-of-band for _dedupe_rows(); never written to the table
        row["_dedupe"] = attrs.get(src["dedupe_key"])
    return row


def _probe_endpoint(url: str, src: dict) -> bool:
    """Does this endpoint answer with rows? Uses the SOURCE'S OWN headers.

    ★ The Referer matters here, not just on the data pages: ComEd's proxy
    answers a bare count query with an HTTP 200 carrying a 403 error envelope,
    so a probe sent without the source's headers would reject a perfectly good
    endpoint — and a probe that only checked the status code would accept a
    403 as a working one.
    """
    import requests
    hdrs = {"User-Agent": _UA}
    hdrs.update(src.get("headers") or {})
    try:
        r = requests.get(url, params={
            "where": "1=1", "returnCountOnly": "true", "f": "json"},
            timeout=15, headers=hdrs)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict) or data.get("error"):
            return False
        return (data.get("count") or 0) > 0
    except Exception:
        return False


def _resolve_agol_item_search(res: dict, src: dict) -> str | None:
    """Find a MONTHLY-ROTATING service by asking the portal what exists now.

    ComEd re-publishes its EV load-capacity service every month under a new
    name AND a new AGOL item id (the usrsvcs path embeds the item id), so any
    pinned URL becomes a silent empty ingest within weeks. This asks the public
    AGOL search — anonymous, no key, no Referer needed — for the owning
    account's Feature Services, newest `modified` first, keeps the ones whose
    title matches, and reads the FeatureServer URL off the item itself.

    Returns None on anything unexpected so the caller falls back to the pinned
    URL rather than crawling something it did not identify.
    """
    import requests
    try:
        r = requests.get(res["portal"], params={
            "q": res["q"], "f": "json", "num": 100,
            "sortField": "modified", "sortOrder": "desc"},
            timeout=20, headers={"User-Agent": _UA})
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
    except Exception as exc:
        logger.warning("hosting_capacity: %s AGOL search failed: %s",
                       src.get("key"), str(exc)[:120])
        return None
    want_type = res.get("item_type")
    needle = (res.get("title_contains") or "").lower()
    for item in results:                      # already newest-first
        if want_type and item.get("type") != want_type:
            continue
        if needle and needle not in (item.get("title") or "").lower():
            continue
        base = (item.get("url") or "").rstrip("/")
        if not base.startswith("https://"):
            continue
        cand = base + res["layer_path"]
        if _probe_endpoint(cand, src):
            logger.info("hosting_capacity: %s resolved %s -> %s",
                        src.get("key"), item.get("title"), cand)
            return cand
        time.sleep(float(src.get("delay_s", 0.5)))
    return None


_URL_RESOLVERS = {"agol_item_search": _resolve_agol_item_search}


def _resolve_url(src: dict) -> str | None:
    """Live resolver first, then the fixed url, then url_candidates.

    ★ Order matters. A pinned url that still 200s is not evidence it is
    CURRENT — ComEd's stale months keep answering — so the resolver runs first
    and the pin is only the fallback for the day the portal search changes
    shape. Xcel's url_candidates keep working the same way: first candidate
    that answers with rows wins.
    """
    res = src.get("url_resolver")
    if res:
        found = _URL_RESOLVERS.get(res.get("kind"), lambda *_a: None)(res, src)
        if found:
            return found
        logger.warning("hosting_capacity: %s url_resolver (%s) found nothing; "
                       "falling back to the pinned url", src.get("key"),
                       res.get("kind"))
    if src.get("url"):
        return src["url"]
    for cand in src.get("url_candidates") or []:
        if _probe_endpoint(cand, src):
            return cand
        time.sleep(0.5)
    return None


def _fetch_pages(src: dict, budget_deadline: float) -> dict:
    """Crawl one source. Returns a RESULT DICT, never a bare list.

    ★ Every exit carries a status. The old version returned [] for "the
    endpoint 404'd", "the crawl blew the budget" and "the territory is
    genuinely empty" alike, so a dead source was indistinguishable from a
    successful empty ingest in the run summary.
    """
    import requests
    res = {"rows": [], "status": "empty", "rows_scanned": 0, "features": 0,
           "pages": 0, "truncated": False, "parts_dropped": 0,
           "distinct_feeders": None, "deduped_rows": 0, "http_status": None}
    out, offset = [], 0
    url = _resolve_url(src)
    if not url:
        logger.warning("hosting_capacity: %s no working endpoint", src["key"])
        res["status"] = "no_endpoint"
        return res
    field_names = [x[0] if isinstance(x, tuple) else x
                   for x in src["fields"].values() if x]
    field_names += [k for k in (src.get("key_extra") or ())]
    if src.get("dedupe_key"):
        field_names.append(src["dedupe_key"])
    # ★ A negative_signal source maps NO capacity field, so its flag column is
    # not in fields.values() and would never be requested — the flag would
    # arrive empty on every row and map_feature would drop all of them. The
    # source would then report a clean, and completely false, empty ingest.
    if src.get("negative_signal"):
        field_names.append(src["negative_signal"]["field"])
    outfields = ",".join(sorted(set(field_names)))
    # Capacity-DESC ordering: when a big layer hits its row cap, keep the
    # HIGHEST-capacity feeders (the site-selection-relevant tail), not an
    # arbitrary 20k.
    # ★ order_field may be declared explicitly. SCE needs it: its capacity
    # is published twice, as a String and as a Double, and ordering by the
    # String is LEXICOGRAPHIC — verified live that it returns 'NA' rows
    # first, so a capped crawl would keep nothing but UNMEASURED rows.
    mw_spec = src["fields"].get("mw_max")
    order_field = (src.get("order_field")
                   or (mw_spec[0] if isinstance(mw_spec, tuple) else mw_spec))
    max_rows = int(_MAX_ROWS_OVERRIDE.get(src["key"])
                   or src.get("max_rows") or _MAX_ROWS_PER_SOURCE)
    delay_s = float(src.get("delay_s", 0.5))
    stats = {}
    while len(out) < max_rows:
        if time.monotonic() >= budget_deadline:
            res["status"] = "budget_exhausted"
            break
        try:
            params = {
                "where": src.get("where", "1=1"), "outFields": outfields,
                "f": "json", "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
                "returnGeometry": "true", "outSR": 4326,
            }
            if order_field:
                params["orderByFields"] = f"{order_field} DESC"
            _hdrs = {"User-Agent": _UA}
            _hdrs.update(src.get("headers") or {})
            # ★ Per-source read timeout. 25s is right for most services, but a
            # full 2,000-feature POLYGON page from ComEd's proxy measured 14.0s
            # on 2026-07-29 (500 features: 2.7s) and a first, cold request
            # exceeded 25s outright — so the whole source failed with
            # fetch_error over latency, not over data. Sources whose pages are
            # heavy declare their own headroom rather than everyone paying for
            # the slowest.
            r = requests.get(url, params=params, headers=_hdrs,
                             timeout=float(src.get("timeout_s", 25)))
            res["http_status"] = r.status_code
            if r.status_code != 200:
                res["status"] = "http_%s" % r.status_code
                break
            data = r.json()
            # ArcGIS answers 200 with an error envelope; that is a failure,
            # not an empty territory.
            if isinstance(data, dict) and data.get("error"):
                res["status"] = "service_error: %s" % (
                    str(data["error"].get("message"))[:80])
                break
            feats = data.get("features") or []
            res["pages"] += 1
            res["features"] += len(feats)
            for ft in feats:
                for part in _explode_features(ft, src, stats):
                    row = map_feature(part, src)
                    if row:
                        out.append(row)
            if not data.get("exceededTransferLimit") and len(feats) < _PAGE_SIZE:
                res["status"] = "ok" if out else "empty"
                break
            offset += len(feats)
            time.sleep(delay_s)
        except Exception as e:
            logger.warning("hosting_capacity: %s fetch failed: %s",
                           src["key"], str(e)[:120])
            res["status"] = "fetch_error: %s" % str(e)[:80]
            break
    else:
        # loop ended on the row cap, not on end-of-data
        res["status"] = "ok"
        res["truncated"] = True
    res["parts_dropped"] = stats.get("parts_dropped", 0)
    res["rows_scanned"] = len(out)
    out, folded = _dedupe_rows(out, src)
    res["deduped_rows"] = folded
    # ★ NEVER publish a raw row count as a feeder count. distinct_feeders is
    # None — not 0, not len(rows) — when the utility publishes no identifier
    # (Dominion's EV layer has none at all), because "unknown" and "zero"
    # are different answers.
    if src["fields"].get("feeder"):
        res["distinct_feeders"] = len({r["feeder_id"] for r in out
                                       if r.get("feeder_id")})
    res["rows"] = out
    if out and not res["status"].startswith(("http_", "fetch_error",
                                             "service_error", "budget")):
        res["status"] = "ok"
    return res


def _ran_recently() -> bool:
    c = _conn()
    if c is None:
        return True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT MAX(ingested_at) > NOW() - %s::interval "
                        "FROM hosting_capacity_feeders", (f"{_GATE_DAYS} days",))
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def run_hosting_capacity_ingest(force: bool = False) -> dict:
    if os.environ.get("HOSTING_CAPACITY_INGEST_DISABLE") == "1":
        return {"status": "disabled"}
    # Retractions run BEFORE the weekly gate: deleting rows published under a
    # wrong meaning must not wait ~6 days for the next crawl window, and the
    # gate reads MAX(ingested_at), which a recent (mislabelled) ingest sets.
    retracted = run_capacity_retractions()
    if not force and _ran_recently():
        out = {"status": "skipped_recent"}
        if retracted.get("deleted"):
            out["retracted"] = retracted["deleted"]
        return out
    deadline = time.monotonic() + _BUDGET_S
    out = {"status": "ok", "sources": {}, "rows": 0}
    if retracted.get("deleted"):
        out["retracted"] = retracted["deleted"]
    c = _conn()
    if c is None:
        out["status"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
        c.commit()
        # capacity_type was added after the first ship. The ALTER needs an
        # ACCESS EXCLUSIVE lock, which a concurrent backup/dump (holding
        # AccessShareLock on every table) will block — so attempt it with a
        # SHORT lock_timeout and NEVER let it fail the run. Then INTROSPECT
        # the live column set and build the INSERT from the intersection
        # (house rule: the repo DDL lies, the live table is the truth).
        try:
            with c.cursor() as cur:
                cur.execute("SET lock_timeout = '3s'")
                cur.execute("ALTER TABLE hosting_capacity_feeders "
                            "ADD COLUMN IF NOT EXISTS capacity_type "
                            "TEXT NOT NULL DEFAULT 'gen'")
                # DEFAULT 'US' backfills every pre-existing row correctly:
                # all 28 shipped sources are US utilities. Verified before the
                # column existed, so the backfill is a statement of fact, not
                # an assumption.
                cur.execute("ALTER TABLE hosting_capacity_feeders "
                            "ADD COLUMN IF NOT EXISTS country "
                            "TEXT NOT NULL DEFAULT 'US'")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_hcf_country "
                            "ON hosting_capacity_feeders (country)")
            c.commit()
        except Exception:
            c.rollback()
            logger.info("hosting_capacity: capacity_type/country ALTER deferred "
                        "(table locked); continuing without it")
        with c.cursor() as cur:
            cur.execute("SET lock_timeout = 0")
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'hosting_capacity_feeders'")
            live_cols = {r[0] for r in cur.fetchall()}
        c.commit()
        has_type = "capacity_type" in live_cols
        out["capacity_type_column"] = has_type
        # Same live-column intersection as capacity_type: the ALTER above may be
        # deferred under lock, so the INSERT is built from what the table HAS.
        has_country = "country" in live_cols
        out["country_column"] = has_country
        # WS9 hardening: fetch → BATCH-write → COMMIT per source. Single-row
        # round-trips took ~20ms each (60k rows ≈ 20 min — thread died on
        # web worker recycle before anything committed). execute_values
        # batches + per-source commits make progress durable.
        from psycopg2.extras import execute_values
        # ★ Build the optional column list and the matching SET list as NAMED
        # parts and interpolate by name. An earlier draft used positional %s and
        # a concatenated tuple, which silently mapped the capacity_type SET
        # clause into the COLUMN list — a two-optional-column INSERT is where
        # positional interpolation stops being readable. The two lists must stay
        # in the SAME ORDER as the value tuples appended below.
        _opt_cols = ""
        _opt_sets = ""
        if has_type:
            _opt_cols += ", capacity_type"
            _opt_sets += " capacity_type = EXCLUDED.capacity_type,"
        if has_country:
            _opt_cols += ", country"
            _opt_sets += " country = EXCLUDED.country,"
        _UPSERT_SQL = """
            INSERT INTO hosting_capacity_feeders
              (utility, feeder_key, feeder_id, substation, state,
               region, voltage_kv, capacity_mw_max, capacity_mw_min,
               queued_gen_kw, lat, lng, src_updated{cols})
            VALUES %s
            ON CONFLICT (utility, feeder_key) DO UPDATE SET
              capacity_mw_max = EXCLUDED.capacity_mw_max,
              capacity_mw_min = EXCLUDED.capacity_mw_min,
              queued_gen_kw = EXCLUDED.queued_gen_kw,
              voltage_kv = EXCLUDED.voltage_kv,
              src_updated = EXCLUDED.src_updated,{sets}
              ingested_at = NOW()
        """.format(cols=_opt_cols, sets=_opt_sets)
        for src in _ingest_order():
            key = src["key"]
            # ── contract first: a mislabelled source is never crawled ──
            bad = check_source_contract(src)
            if bad:
                out["sources"][key] = {"status": "refused_contract",
                                       "reason": bad, "rows": 0}
                logger.error("hosting_capacity: REFUSED %s — %s", key, bad)
                continue
            if time.monotonic() >= deadline:
                # ★ recorded, not skipped. "we ran out of time" and "the
                # utility published nothing" must never look the same.
                out["sources"][key] = {"status": "budget_exhausted", "rows": 0,
                                       "capacity_type": src["capacity_type"],
                                       "country": src["country"]}
                continue
            res = _fetch_pages(src, deadline)
            st = {"status": res["status"], "rows": 0,
                  "capacity_type": src["capacity_type"],
                  "country": src["country"],
                  "rows_scanned": res["rows_scanned"],
                  "features_fetched": res["features"],
                  # None means the utility publishes no feeder identifier —
                  # rows_scanned is NOT a substitute for it.
                  "distinct_feeders": res["distinct_feeders"],
                  "truncated": res["truncated"],
                  "attribution": src.get("attribution")}
            if res["parts_dropped"]:
                st["parts_dropped"] = res["parts_dropped"]
            if res["deduped_rows"]:
                st["vertex_rows_folded"] = res["deduped_rows"]
            out["sources"][key] = st
            rows = res["rows"]
            if not rows:
                continue
            # ★ NEVER hold one connection across the slow rate-limited
            # fetches (documented failure: Neon recycles it mid-run →
            # "SSL bad record mac" then "connection already closed" for
            # every later source). Fresh short-lived conn per WRITE.
            try:
                c.close()
            except Exception:
                pass
            c = _conn()
            if c is None:
                st["status"] = "write_failed: no_connection"
                continue
            # In-batch dedup on the conflict key (ON CONFLICT can't see two
            # identical keys inside one VALUES page).
            seen, vals = set(), []
            for r in rows:
                k = (r["utility"], r["feeder_key"])
                if k in seen:
                    continue
                seen.add(k)
                base = (r["utility"], r["feeder_key"], r["feeder_id"],
                        r["substation"], r["state"], r["region"],
                        r["voltage_kv"], r["capacity_mw_max"],
                        r["capacity_mw_min"], r["queued_gen_kw"],
                        r["lat"], r["lng"], r["src_updated"])
                vals.append(base
                            + ((r["capacity_type"],) if has_type else ())
                            + ((r["country"],) if has_country else ()))
            try:
                with c.cursor() as cur:
                    execute_values(cur, _UPSERT_SQL, vals, page_size=500)
                c.commit()
                out["rows"] += len(vals)
                st["rows"] = len(vals)
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                st["status"] = f"write_failed: {str(e)[:80]}"
                logger.warning("hosting_capacity: %s write failed: %s",
                               key, str(e)[:160])
        # Per-source roll-up so a run can be read at a glance and a source
        # that produced nothing always says WHY.
        _st = {k: (v.get("status") if isinstance(v, dict) else v)
               for k, v in out["sources"].items()}
        out["sources_ok"] = sorted(k for k, s in _st.items() if s == "ok")
        out["sources_not_ok"] = {k: s for k, s in _st.items() if s != "ok"}
        out["load_rows"] = sum(
            v.get("rows", 0) for v in out["sources"].values()
            if isinstance(v, dict) and v.get("capacity_type") == "load")
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            with c.cursor() as cur:
                upsert_brain_finding(
                    cur, issue="grid_depth:hosting_capacity_ingest",
                    url="dchub://grid/hosting-capacity",
                    count=out["rows"],
                    detail=(f"hosting-capacity rows upserted: {out['rows']} "
                            f"({out['load_rows']} load) | statuses: "
                            f"{_st}")[:2000],
                    detector="hosting_capacity_ingest", status="resolved")
            c.commit()
        except Exception:
            pass
    except Exception as e:
        out["status"] = "partial"
        out["error"] = str(e)[:160]
    finally:
        try:
            c.close()
        except Exception:
            pass
    logger.info("hosting_capacity_ingest: %s", out)
    return out


# ── WS9: admin force endpoint (the weekly gate otherwise blocks new-source
# backfill until the next window). Safe-zone registered in main.py.
from flask import Blueprint, jsonify, request as _rq  # noqa: E402

hosting_capacity_bp = Blueprint("hosting_capacity_ingest", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


@hosting_capacity_bp.route("/api/v1/grid/hosting-capacity/feeders",
                           methods=["GET"])
def hosting_capacity_feeders_endpoint():
    """Map layer feed: utility-published feeder hosting capacity within a
    bbox. Public data (all sourced from public utility GIS), anon-open,
    capacity-DESC so the cap keeps the most useful rows. ?bbox=w,s,e,n."""
    try:
        w, s, e, n = [float(x) for x in
                      (_rq.args.get("bbox") or "").split(",")]
    except Exception:
        return jsonify(error="bbox_required",
                       hint="bbox=west,south,east,north (lng/lat)"), 400
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        return jsonify(error="bad_bbox"), 400
    limit = max(1, min(int(_rq.args.get("limit", 3000) or 3000), 4000))
    # Optional ?country=US filter. Validated against the allow-list rather than
    # passed through, so a typo returns 400 instead of an empty layer that reads
    # as "no feeders here".
    want_country = (_rq.args.get("country") or "").strip().upper() or None
    if want_country and want_country not in _ALLOWED_COUNTRIES:
        return jsonify(error="bad_country",
                       hint="country must be one of %s"
                            % list(_ALLOWED_COUNTRIES)), 400
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            # Live-column probe: the country ALTER can be deferred under lock.
            cur.execute("SELECT column_name FROM information_schema.columns "
                        " WHERE table_name = 'hosting_capacity_feeders' "
                        "   AND column_name = 'country'")
            _has_country = bool(cur.fetchall())
            if want_country and not _has_country:
                return jsonify(error="country_filter_unavailable",
                               hint=("the country column has not been created on "
                                     "this deploy yet; retry after the next "
                                     "ingest applies the migration")), 503
            _params = [w, e, s, n]
            _where = ""
            if want_country:
                _where = " AND country = %s"
                _params.append(want_country)
            _params.append(limit)
            cur.execute("""
                SELECT utility, feeder_id, substation, region, voltage_kv,
                       capacity_mw_max, capacity_mw_min, lat, lng,
                       src_updated, capacity_type{cc}
                  FROM hosting_capacity_feeders
                 WHERE lng BETWEEN %s AND %s AND lat BETWEEN %s AND %s
                   AND capacity_mw_max IS NOT NULL{where}
                 ORDER BY capacity_mw_max DESC
                 LIMIT %s
            """.format(cc=", country" if _has_country else "",
                       where=_where), tuple(_params))
            # Attribution and basis ride on EVERY row, not in a footnote —
            # several operators require the credit as a condition of use, and
            # a megawatt figure without its basis is not publishable here.
            feeders = [{"utility": r[0], "feeder_id": r[1], "substation": r[2],
                        "region": r[3], "voltage_kv": r[4],
                        "capacity_mw_max": r[5], "capacity_mw_min": r[6],
                        "lat": r[7], "lng": r[8], "src_updated": r[9],
                        "capacity_type": r[10],
                        "country": (r[11] if _has_country and len(r) > 11 else "US"),
                        "attribution": _SOURCE_ATTRIBUTION.get(r[0]),
                        "capacity_basis": _SOURCE_CAPACITY_BASIS.get(r[0])}
                       for r in cur.fetchall()]
        resp = jsonify(feeders=feeders, count=len(feeders), limit=limit,
                       attributions=sorted({f["attribution"] for f in feeders
                                            if f["attribution"]}),
                       source="utility-published hosting-capacity GIS",
                       capacity_types={
                           "gen": ("DER/generation hosting capacity — how "
                                   "much generation the feeder can accept "
                                   "(the common utility publication)"),
                           "load": ("LOAD-serving capacity — what a new "
                                    "data-center load can draw"),
                           "bus_headroom": ("transmission bus MW available "
                                            "(not distribution feeder HC)")},
                       note=("Informational, not binding interconnection "
                             "guidance; verify with the utility."))
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as ex:
        return jsonify(error=str(ex)[:160]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


# Market labels for the covered utilities (map jump-list). Derived
# centers/bboxes come from the data itself — only the human label is here.
_MARKET_LABELS = {
    "PHI (Pepco/Delmarva/ACE)": "DC · Baltimore · Delmarva",
    "Con Edison NY": "New York City · Westchester",
    "Orange & Rockland NY": "Hudson Valley NY",
    "NYSEG/RG&E": "Upstate NY · Rochester",
    "Rhode Island Energy": "Providence · Rhode Island",
    "National Grid NY": "Albany · Syracuse · Buffalo",
    "Dominion Energy VA (binned)": "Northern Virginia · Richmond",
    "Dominion Energy VA (EV load)": "Northern Virginia · Loudoun · Richmond",
    "SCE (Southern California Edison, load)":
        "Los Angeles Basin · Inland Empire · Orange County",
}


@hosting_capacity_bp.route("/api/v1/grid/hosting-capacity/coverage",
                           methods=["GET"])
def hosting_capacity_coverage_endpoint():
    """Which markets have feeder coverage — per utility: count, center,
    bbox, best MW. Powers the map's market jump-list. Anon-open."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            # The country ALTER can be deferred under lock (see the ingest), so
            # probe the LIVE column set rather than assuming the repo DDL — a
            # bare SELECT of a missing column would 500 the whole endpoint.
            cur.execute("SELECT column_name FROM information_schema.columns "
                        " WHERE table_name = 'hosting_capacity_feeders' "
                        "   AND column_name = 'country'")
            _has_country = bool(cur.fetchall())
            cur.execute("""
                SELECT utility, COUNT(*), COUNT(DISTINCT feeder_id),
                       ROUND(AVG(lat)::numeric, 4), ROUND(AVG(lng)::numeric, 4),
                       ROUND(MIN(lat)::numeric, 4), ROUND(MIN(lng)::numeric, 4),
                       ROUND(MAX(lat)::numeric, 4), ROUND(MAX(lng)::numeric, 4),
                       ROUND(MAX(capacity_mw_max)::numeric, 1),
                       MAX(capacity_type){cc}
                  FROM hosting_capacity_feeders
                 WHERE lat IS NOT NULL AND lng IS NOT NULL
                 GROUP BY utility ORDER BY COUNT(*) DESC
            """.format(cc=", MAX(country)" if _has_country else ""))
            markets = []
            for r in cur.fetchall():
                # ★ rows ≠ feeders. Rows are GIS geometry (vertices, line
                # sections, exploded multipart parts) at a measured 15x-29x
                # the feeder count, so `feeders` reports DISTINCT feeder ids
                # and `geometry_rows` reports what they were counted from.
                # A utility that publishes no feeder id gets None, not 0 —
                # unknown is a different answer from none.
                _distinct = int(r[2] or 0)
                markets.append({
                    "utility": r[0],
                    "market": _MARKET_LABELS.get(r[0], r[0]),
                    "feeders": _distinct or None,
                    "geometry_rows": int(r[1]),
                    "center": {"lat": float(r[3]), "lng": float(r[4])},
                    "bbox": {"south": float(r[5]), "west": float(r[6]),
                             "north": float(r[7]), "east": float(r[8])},
                    "max_capacity_mw": float(r[9]) if r[9] is not None else None,
                    "capacity_type": r[10] or "gen",
                    "binned": "binned" in (r[0] or ""),
                    "attribution": _SOURCE_ATTRIBUTION.get(r[0]),
                    "capacity_basis": _SOURCE_CAPACITY_BASIS.get(r[0]),
                    # Explicit on every row. Before this column existed the
                    # geography was an unstated assumption that happened to be
                    # true; an unstated assumption is what breaks the first time
                    # a non-US source lands.
                    "country": (r[11] if _has_country and len(r) > 11 else "US"),
                })
        # ★★ total_feeders IS US-ONLY, AND STAYS US-ONLY. Every consumer of this
        # figure today — the map jump-list, the coverage headline, the MCP
        # blurb — reads it as the US footprint, because until `country` existed
        # every row WAS US. Silently widening it the moment a non-US source
        # lands would change what a published number means without changing its
        # name: the same defect as counting GIS vertices as feeders, one axis
        # over. Non-US coverage is reported in `by_country` and
        # `total_feeders_non_us`, so growth is visible and additive rather than
        # retroactively redefining the headline.
        _us = [m for m in markets if m["country"] == "US"]
        _by_country = {}
        for m in markets:
            b = _by_country.setdefault(m["country"], {
                "utilities": 0, "feeders": 0, "geometry_rows": 0})
            b["utilities"] += 1
            b["feeders"] += m["feeders"] or 0
            b["geometry_rows"] += m["geometry_rows"]
        resp = jsonify(markets=markets,
                       # `feeders` is None for utilities that publish no
                       # identifier, so it cannot be summed blindly — and the
                       # total is explicitly PARTIAL rather than pretending
                       # those territories contribute zero feeders.
                       total_feeders=sum(m["feeders"] or 0 for m in _us),
                       total_feeders_basis=(
                           "US ONLY — sum of DISTINCT feeder ids over rows with "
                           "country='US'. Utilities publishing no feeder "
                           "identifier contribute 0 and report feeders=null "
                           "(unknown, not none). Non-US coverage is NOT in this "
                           "figure: see by_country and total_feeders_non_us. "
                           "This total was US-only before the country column "
                           "existed and is held US-only deliberately, so its "
                           "meaning does not change as new geographies land."),
                       total_feeders_non_us=sum(
                           m["feeders"] or 0 for m in markets
                           if m["country"] != "US"),
                       by_country=_by_country,
                       countries_covered=sorted(_by_country),
                       utilities_without_feeder_ids=sorted(
                           m["utility"] for m in markets if not m["feeders"]),
                       total_geometry_rows=sum(m["geometry_rows"] for m in markets),
                       note=("Utility-published hosting capacity. "
                             "Informational only — verify with the utility."))
        resp.headers["Cache-Control"] = "public, max-age=900"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as ex:
        return jsonify(error=str(ex)[:160]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@hosting_capacity_bp.route("/api/v1/grid/hosting-capacity/ingest",
                           methods=["POST"])
def hosting_capacity_ingest_endpoint():
    provided = (_rq.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    force = _rq.args.get("force") == "1"
    if _rq.args.get("sync") == "1":
        return jsonify(run_hosting_capacity_ingest(force=force)), 200
    import threading
    threading.Thread(target=lambda: run_hosting_capacity_ingest(force=force),
                     name="hosting-capacity-manual", daemon=True).start()
    return jsonify(status="spawned", force=force), 202


# How many raw rows to pull before de-duplicating vertices into feeders. The
# table stores one row per GIS geometry VERTEX (see _fold_vertices), measured at
# ~15x (Ameren) to ~29x (Rhode Island Energy) rows per distinct feeder, so the
# old LIMIT 12 could not surface more than ~1-4 real feeders. Ordered
# capacity-DESC, so this is the high-capacity head — which is what the block
# wants anyway.
_FEEDERS_NEAR_SCAN = 600


def _fold_vertices(rows):
    """Fold GIS vertex rows into one entry per feeder, keeping its max capacity.

    hosting_capacity_feeders stores one row per geometry vertex of a feeder
    line, NOT one row per feeder. Counting rows over-states the feeder count by
    more than an order of magnitude and lets the same feeder occupy every slot
    in a top-N list (live 2026-07-28: Providence returned feeder 2295 three
    times in a 6-row top_feeders). Capacity is constant across a feeder's
    vertices, so folding on (utility, feeder_id) is lossless.

    Rows with no feeder_id (Avista publishes none) cannot be de-duplicated and
    each stay their own entry rather than collapsing into one phantom feeder.
    """
    folded = {}
    for i, r in enumerate(rows):
        fid = r[1] if r[1] not in (None, "") else None
        key = (r[0], fid if fid is not None else "@row%d" % i)
        prev = folded.get(key)
        if prev is None or (r[4] or 0) > (prev[4] or 0):
            folded[key] = r
    return list(folded.values())


def _disqualified_near(c, lat, lng, dlat, dlng) -> dict:
    """Utility-flagged NO-CAPACITY segments near a point.

    ★ Kept OUT of the main feeders query deliberately. These rows carry no
    number, and the main query is the one that answers "how many MW" — mixing
    a NULL-capacity row into it would break its ORDER BY / max(). This runs as
    its own small aggregate and is fully fail-soft: any problem returns {} and
    the capacity answer is untouched.

    ★ The selector is self-maintaining. Every other path in map_feature()
    drops a row whose mw_max is None, so a NULL capacity_mw_max can only come
    from a negative_signal source. No extra column is needed to find them, and
    a stray NULL from anywhere else would be a bug worth surfacing anyway.

    ★★ PRESENT-ONLY. A hit means the utility itself flagged those segments as
    having no capacity. The ABSENCE of hits is never evidence that capacity IS
    available: these ingests are row-capped, so coverage is partial by design,
    and "we did not ingest it" must never read as "the utility says it's fine".
    """
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT utility, COUNT(*)
                  FROM hosting_capacity_feeders
                 WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                   AND capacity_mw_max IS NULL
                 GROUP BY utility
                 ORDER BY COUNT(*) DESC LIMIT 20
            """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
            hits = cur.fetchall()
        if not hits:
            return {}
        return {"disqualified_segments": {
            "by_utility": {h[0]: int(h[1]) for h in hits},
            "attributions": sorted({a for a in
                                    (_SOURCE_ATTRIBUTION.get(h[0])
                                     for h in hits) if a}),
            "meaning": ("Distribution segments the utility itself flags as "
                        "having NO available capacity. They carry no MW "
                        "figure because the utility publishes only binned "
                        "text ranges — none is invented here."),
            "not_an_all_clear": ("Present-only signal. No hits does NOT mean "
                                 "capacity is available; coverage is a capped "
                                 "sample of the utility's flagged segments."),
        }}
    except Exception as ex:
        try:
            logger.warning("disqualified_near failed: %s: %s",
                           type(ex).__name__, str(ex)[:160])
        except Exception:
            pass
        return {}


def feeders_near(lat: float, lng: float, radius_km: float = 40.0) -> dict:
    """Feeder-truth block for the hosting-capacity endpoint. {} fail-soft."""
    # The caller passes request.args values STRAIGHT through, so lat/lng arrive
    # as STRINGS on the ?lat=&lon= path. `lat - deg` then raised TypeError into
    # the bare except below and this function returned {} for every point query
    # since it shipped — silently, because the except logged nothing. The
    # ?market= path happened to work only because market_power_scores yields
    # floats. Coerce first (mirrors _substation_headroom, which calls _num()).
    try:
        lat = float(lat)
        lng = float(lng)
        radius_km = float(radius_km)
    except (TypeError, ValueError):
        return {}
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return {}
    c = _conn()
    if c is None:
        return {}
    try:
        # Longitude degrees shrink with latitude; without the cos correction the
        # east-west window was ~33% wider than the requested radius at lat 41.
        # Mirrors _substation_headroom's bbox math.
        dlat = max(0.05, radius_km / 111.0)
        dlng = max(0.05, radius_km / (111.0 * max(0.1, math.cos(math.radians(lat)))))
        with c.cursor() as cur:
            cur.execute("""
                SELECT utility, feeder_id, substation, voltage_kv,
                       capacity_mw_max, capacity_mw_min, lat, lng, src_updated,
                       capacity_type
                  FROM hosting_capacity_feeders
                 WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                   AND capacity_mw_max IS NOT NULL
                 ORDER BY capacity_mw_max DESC LIMIT %s
            """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng,
                  _FEEDERS_NEAR_SCAN))
            rows = cur.fetchall()
        # Flag-only sources carry no MW, so they are invisible to the query
        # above by construction. Fetched separately and merged below.
        disq = _disqualified_near(c, lat, lng, dlat, dlng)
        if not rows:
            # ★ A site can sit where every nearby segment is flagged NO
            # CAPACITY and no priced feeder row exists — exactly where the
            # flag matters most. Returning {} here would throw away the one
            # honest thing we know about that location.
            return disq
        feeders = _fold_vertices(rows)
        feeders.sort(key=lambda r: r[4] or 0, reverse=True)
        top = [{"utility": r[0], "feeder_id": r[1], "substation": r[2],
                "voltage_kv": r[3], "capacity_mw_max": r[4],
                "capacity_mw_min": r[5], "lat": r[6], "lng": r[7],
                "src_updated": r[8], "capacity_type": r[9],
                # required credit + the basis of the number, on the row that
                # carries the number (not in a footnote a caller can drop)
                "attribution": _SOURCE_ATTRIBUTION.get(r[0]),
                "capacity_basis": _SOURCE_CAPACITY_BASIS.get(r[0])}
               for r in feeders[:6]]
        types = sorted({(r[9] or "gen") for r in feeders})
        # ** first so the explicit keys below always win.
        return {**disq,
                "feeder_count_in_bbox": len(feeders),
                "attributions": sorted({a for a in
                                        (_SOURCE_ATTRIBUTION.get(r[0])
                                         for r in feeders) if a}),
                "geometry_rows_scanned": len(rows),
                "max_feeder_capacity_mw": max(r[4] for r in feeders),
                "top_feeders": top,
                "utilities": sorted({r[0] for r in feeders}),
                "capacity_types": types,
                "basis": "utility-published feeder hosting-capacity (ingested)",
                # capacity_type decides whether these MW mean anything for a
                # data center. 14 of 18 utilities publish 'gen' (DER export
                # headroom — what the feeder can ACCEPT from solar/storage),
                # which is NOT load a data center can draw. Saying so here
                # matters because this block answers "can this site get power?".
                "capacity_type_meaning": {
                    "load": ("LOAD-serving capacity — what a new data-center "
                             "load can draw."),
                    "gen": ("DER/generation hosting capacity — what the feeder "
                            "can accept from solar/storage for EXPORT. Not "
                            "available load; do not read it as siteable "
                            "data-center capacity."),
                    "bus_headroom": ("Transmission bus MW available, not "
                                     "distribution-feeder hosting capacity."),
                },
                "counting": ("feeder_count_in_bbox counts DISTINCT feeders; "
                             "geometry_rows_scanned is the raw GIS vertex rows "
                             "they were folded from (one row per vertex)."),
                "note": ("Utility hosting-capacity maps are informational, "
                         "not binding interconnection guidance.")}
    except Exception as ex:
        # Stays fail-soft — a feeder-table problem must not break the endpoint —
        # but no longer SILENT. The swallowed TypeError above is exactly why
        # this path was dead in production without anyone noticing.
        try:
            logger.warning("feeders_near failed: %s: %s",
                           type(ex).__name__, str(ex)[:200])
        except Exception:
            pass
        return {}
    finally:
        try:
            c.close()
        except Exception:
            pass
