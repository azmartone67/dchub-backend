"""Shell #35 (2026-07-26) — utility FEEDER hosting-capacity ingest.

Turns "can I get N MW near this point" from substation-proximity guess
into utility-published feeder truth. v1 sources = the two VERIFIED open
public FeatureServers (survey 2026-07-26):

  * PHI (Pepco/Delmarva/ACE, Exelon) — kW-precise feeder hosting capacity
    with FeederID/Substation/voltage/queued-gen and per-row update dates.
  * National Grid New York SDP — MW feeder max/min HC, NYISO zone, stable
    utility-hosted URL.

Explicitly SKIPPED on terms/access grounds: Georgia Power (tool terms
mark results confidential), Duke + ComEd (secured-proxy 403), PG&E/SCE
(registration-gated). Dominion VA public layer is binned with no feeder
id — candidate v1.5 overlay, not ingested here.

Trigger: called (weekly-gated, budget-capped, daemon-safe) from
depth_master_shell._act_hosting_capacity — no new cron. Kill switch:
HOSTING_CAPACITY_INGEST_DISABLE=1.

Serving: feeders_near(lat, lng, radius_km) powers the feeder_hosting
block in /api/v1/grid/hosting-capacity (fail-soft: endpoint keeps its
substation-proximity answer when no feeder rows are near).
"""

from __future__ import annotations

import os
import json
import math
import time
import logging
import datetime

logger = logging.getLogger(__name__)

_UA = "DCHub-GridData/1.0 (+https://dchub.cloud; public-gis-ingest)"
_BUDGET_S = float(os.environ.get("HOSTING_CAPACITY_INGEST_BUDGET_S", "300"))
_PAGE_SIZE = 2000
_MAX_ROWS_PER_SOURCE = int(os.environ.get("HOSTING_CAPACITY_MAX_ROWS", "20000"))
_GATE_DAYS = 6

SOURCES = [
    {"utility": "PHI (Pepco/Delmarva/ACE)",
     "key": "phi",
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
    {"utility": "Dominion Energy VA (binned)",
     "key": "dominion_va",
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
     "url": ("https://services.arcgis.com/jVbCQRNRZvxyQyrx/arcgis/rest/"
             "services/HCA_June_2023/FeatureServer/0/query"),
     # Service NAME says 2023 but Date_of_La verified 2026-04-14.
     "fields": {"feeder": "Circuit_1", "substation": None,
                "state": None, "region": None, "voltage_kv": "Voltage__k",
                "mw_max": ("Hosting__1", 0.001),          # kW → MW
                "mw_min": None, "queued_kw": "Installed",
                "updated": "Date_of_La"}},
    # Transmission BUS headroom (Spokane/Post Falls) — not distribution
    # HC; typed separately so it can never be blended with feeder HC.
    {"utility": "Avista (bus headroom)",
     "key": "avista_bus",
     "capacity_type": "bus_headroom",
     "url": ("https://services3.arcgis.com/WlYQgAChrqj0tuQi/arcgis/rest/"
             "services/HeatMap_MW_Impact_PRD/FeatureServer/0/query"),
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
     "url": ("https://services3.arcgis.com/p04uQpu9ausDBOAh/arcgis/rest/"
             "services/DG_Hosting_CT_Final_full/FeatureServer/56/query"),
     "fields": {"feeder": "CIRCUITID", "substation": "DIST_SUB_NAME",
                "state": None, "region": None,
                "voltage_kv": "SECTION_OPERATING_VOLTAGE",
                "mw_max": ("HOSTING_CAPACITY_MW", 1.0),
                "mw_min": None, "queued_kw": "IN_QUEUE_DG_KW",
                "updated": "DATE_UPDATED"}},
]
# Verified-but-EXCLUDED (agent probe 2026-07-27): PSE&G NJ EVCapacity
# (CAPACITY_REMAINING units UNLABELED — likely MW but unproven; do not
# ingest a magnitude we can't name), PECO (one unlabeled number per grid
# tile, no ids/date, and LAT/LON columns are SWAPPED), BGE EPRI service
# (MW + substation-level but DATE_GIS_UPDATED reads 2022 — freshness
# unproven), NGrid MA MASDP_Feeders (5-yr peak-MVA forecast — excellent,
# but headroom needs a rating−peak derivation the mapper can't express
# yet), PSEG Long Island (org-restricted 403), Eversource's OWN host
# (times out; the Cadmus mirror stands in), Consumers Energy MI
# (feeder/substation/voltage buried in a JSON string column — v2),
# PGE Oregon (feeder names *REDACTED*, no true HC field), Ameren IL GEN +
# subtransmission (redundant with LOAD / null feeder), Avista DER polyline
# (509k rows, no substation/voltage/date). NOT public: NV Energy (login),
# PSE (email request), PacifiCorp, Hawaiian Electric (non-Esri), all Texas
# (no ERCOT DER-HC mandate), SRP/TEP/SMUD (none found), Ameren MO (none),
# JCP&L/PPL/United Illuminating/Unitil/GMP/CMP (no public REST service).
# ★ Xcel renames services MONTHLY — publisher account HCAPublisher_xeago
# (feeder/substation/voltage are buried in a JSON string column — v2),
# PGE Oregon (feeder names *REDACTED*, no true HC field), Ameren IL GEN +
# subtransmission (redundant with LOAD / null feeder), Avista DER polyline
# (509k rows, no substation/voltage/date). NOT public: NV Energy (login),
# PSE (email request), PacifiCorp, Hawaiian Electric (non-Esri), all Texas
# (no ERCOT DER-HC mandate), SRP/TEP/SMUD (none found), Ameren MO (none).
# ★ Xcel renames services MONTHLY — publisher account HCAPublisher_xeago
# (org eM84fwjsSggLQk61); re-resolve newest Feature Service by `modified`
# rather than by name pattern when the candidates go stale.
# NGrid-MA (MASDP): probed 2026-07-27 → HTTP 403 (folder forbidden, unlike
# NYSDP). Not ingested — do not guess. Georgia Power/Duke/ComEd remain
# excluded per ToS/proxy findings.

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
    if mw_max is None:
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
    row = {"utility": src["utility"],
           "feeder_key": f"{feeder_id or ''}|{round(lat,4)},{round(lng,4)}",
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
           "capacity_type": src.get("capacity_type", "gen")}
    return row


def _resolve_url(src: dict) -> str | None:
    """Fixed url, or first url_candidate that answers 200 with features
    (WS9: Xcel renames its service monthly)."""
    if src.get("url"):
        return src["url"]
    import requests
    for cand in src.get("url_candidates") or []:
        try:
            r = requests.get(cand, params={
                "where": "1=1", "returnCountOnly": "true", "f": "json"},
                timeout=15, headers={"User-Agent": _UA})
            if r.status_code == 200 and (r.json().get("count") or 0) > 0:
                return cand
        except Exception:
            continue
        time.sleep(0.5)
    return None


def _fetch_pages(src: dict, budget_deadline: float) -> list:
    import requests
    out, offset = [], 0
    url = _resolve_url(src)
    if not url:
        logger.warning("hosting_capacity: %s no working endpoint", src["key"])
        return out
    outfields = ",".join(x[0] if isinstance(x, tuple) else x
                         for x in src["fields"].values() if x)
    # Capacity-DESC ordering: when a big layer hits _MAX_ROWS_PER_SOURCE,
    # keep the HIGHEST-capacity feeders (the site-selection-relevant tail),
    # not an arbitrary 20k.
    mw_spec = src["fields"].get("mw_max")
    order_field = mw_spec[0] if isinstance(mw_spec, tuple) else mw_spec
    while len(out) < _MAX_ROWS_PER_SOURCE and time.monotonic() < budget_deadline:
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
            r = requests.get(url, params=params, timeout=25, headers=_hdrs)
            if r.status_code != 200:
                break
            data = r.json()
            feats = data.get("features") or []
            for ft in feats:
                row = map_feature(ft, src)
                if row:
                    out.append(row)
            if not data.get("exceededTransferLimit") and len(feats) < _PAGE_SIZE:
                break
            offset += len(feats)
            time.sleep(0.5)
        except Exception as e:
            logger.warning("hosting_capacity: %s fetch failed: %s",
                           src["key"], str(e)[:120])
            break
    return out


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
    if not force and _ran_recently():
        return {"status": "skipped_recent"}
    deadline = time.monotonic() + _BUDGET_S
    out = {"status": "ok", "sources": {}, "rows": 0}
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
            c.commit()
        except Exception:
            c.rollback()
            logger.info("hosting_capacity: capacity_type ALTER deferred "
                        "(table locked); continuing without it")
        with c.cursor() as cur:
            cur.execute("SET lock_timeout = 0")
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'hosting_capacity_feeders'")
            live_cols = {r[0] for r in cur.fetchall()}
        c.commit()
        has_type = "capacity_type" in live_cols
        out["capacity_type_column"] = has_type
        # WS9 hardening: fetch → BATCH-write → COMMIT per source. Single-row
        # round-trips took ~20ms each (60k rows ≈ 20 min — thread died on
        # web worker recycle before anything committed). execute_values
        # batches + per-source commits make progress durable.
        from psycopg2.extras import execute_values
        _UPSERT_SQL = """
            INSERT INTO hosting_capacity_feeders
              (utility, feeder_key, feeder_id, substation, state,
               region, voltage_kv, capacity_mw_max, capacity_mw_min,
               queued_gen_kw, lat, lng, src_updated%s)
            VALUES %%s
            ON CONFLICT (utility, feeder_key) DO UPDATE SET
              capacity_mw_max = EXCLUDED.capacity_mw_max,
              capacity_mw_min = EXCLUDED.capacity_mw_min,
              queued_gen_kw = EXCLUDED.queued_gen_kw,
              voltage_kv = EXCLUDED.voltage_kv,
              src_updated = EXCLUDED.src_updated,%s
              ingested_at = NOW()
        """ % ((", capacity_type", " capacity_type = EXCLUDED.capacity_type,")
               if has_type else ("", ""))
        for src in SOURCES:
            rows = _fetch_pages(src, deadline)
            out["sources"][src["key"]] = len(rows)
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
                out["sources"][src["key"]] = "write_failed: no_connection"
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
                vals.append(base + ((r.get("capacity_type", "gen"),)
                                    if has_type else ()))
            try:
                with c.cursor() as cur:
                    execute_values(cur, _UPSERT_SQL, vals, page_size=500)
                c.commit()
                out["rows"] += len(vals)
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                out["sources"][src["key"]] = f"write_failed: {str(e)[:80]}"
                logger.warning("hosting_capacity: %s write failed: %s",
                               src["key"], str(e)[:160])
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            with c.cursor() as cur:
                upsert_brain_finding(
                    cur, issue="grid_depth:hosting_capacity_ingest",
                    url="dchub://grid/hosting-capacity",
                    count=out["rows"],
                    detail=(f"feeder hosting-capacity rows upserted: "
                            f"{out['sources']}")[:2000],
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
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT utility, feeder_id, substation, region, voltage_kv,
                       capacity_mw_max, capacity_mw_min, lat, lng,
                       src_updated, capacity_type
                  FROM hosting_capacity_feeders
                 WHERE lng BETWEEN %s AND %s AND lat BETWEEN %s AND %s
                   AND capacity_mw_max IS NOT NULL
                 ORDER BY capacity_mw_max DESC
                 LIMIT %s
            """, (w, e, s, n, limit))
            feeders = [{"utility": r[0], "feeder_id": r[1], "substation": r[2],
                        "region": r[3], "voltage_kv": r[4],
                        "capacity_mw_max": r[5], "capacity_mw_min": r[6],
                        "lat": r[7], "lng": r[8], "src_updated": r[9],
                        "capacity_type": r[10]}
                       for r in cur.fetchall()]
        resp = jsonify(feeders=feeders, count=len(feeders), limit=limit,
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
            cur.execute("""
                SELECT utility, COUNT(*),
                       ROUND(AVG(lat)::numeric, 4), ROUND(AVG(lng)::numeric, 4),
                       ROUND(MIN(lat)::numeric, 4), ROUND(MIN(lng)::numeric, 4),
                       ROUND(MAX(lat)::numeric, 4), ROUND(MAX(lng)::numeric, 4),
                       ROUND(MAX(capacity_mw_max)::numeric, 1),
                       MAX(capacity_type)
                  FROM hosting_capacity_feeders
                 WHERE lat IS NOT NULL AND lng IS NOT NULL
                 GROUP BY utility ORDER BY COUNT(*) DESC
            """)
            markets = []
            for r in cur.fetchall():
                markets.append({
                    "utility": r[0],
                    "market": _MARKET_LABELS.get(r[0], r[0]),
                    "feeders": int(r[1]),
                    "center": {"lat": float(r[2]), "lng": float(r[3])},
                    "bbox": {"south": float(r[4]), "west": float(r[5]),
                             "north": float(r[6]), "east": float(r[7])},
                    "max_capacity_mw": float(r[8]) if r[8] is not None else None,
                    "capacity_type": r[9] or "gen",
                    "binned": "binned" in (r[0] or ""),
                })
        resp = jsonify(markets=markets,
                       total_feeders=sum(m["feeders"] for m in markets),
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
        if not rows:
            return {}
        feeders = _fold_vertices(rows)
        feeders.sort(key=lambda r: r[4] or 0, reverse=True)
        top = [{"utility": r[0], "feeder_id": r[1], "substation": r[2],
                "voltage_kv": r[3], "capacity_mw_max": r[4],
                "capacity_mw_min": r[5], "lat": r[6], "lng": r[7],
                "src_updated": r[8], "capacity_type": r[9]} for r in feeders[:6]]
        types = sorted({(r[9] or "gen") for r in feeders})
        return {"feeder_count_in_bbox": len(feeders),
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
