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
import time
import logging
import datetime

logger = logging.getLogger(__name__)

_UA = "DCHub-GridData/1.0 (+https://dchub.cloud; public-gis-ingest)"
_BUDGET_S = float(os.environ.get("HOSTING_CAPACITY_INGEST_BUDGET_S", "180"))
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
]

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
    try:
        return round(float(v) * scale, 3)
    except (TypeError, ValueError):
        return None


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
    feeder_id = g(f["feeder"])
    row = {"utility": src["utility"],
           "feeder_key": f"{feeder_id or ''}|{round(lat,4)},{round(lng,4)}",
           "feeder_id": str(feeder_id) if feeder_id is not None else None,
           "substation": g(f["substation"]),
           "state": g(f["state"]), "region": g(f["region"]),
           "voltage_kv": _num(g(f["voltage_kv"])),
           "capacity_mw_max": mw_max,
           "capacity_mw_min": g(f["mw_min"]),
           "queued_gen_kw": _num(g(f["queued_kw"])),
           "lat": lat, "lng": lng,
           "src_updated": str(g(f["updated"]) or "")[:40] or None}
    return row


def _fetch_pages(src: dict, budget_deadline: float) -> list:
    import requests
    out, offset = [], 0
    outfields = ",".join(x[0] if isinstance(x, tuple) else x
                         for x in src["fields"].values() if x)
    while len(out) < _MAX_ROWS_PER_SOURCE and time.monotonic() < budget_deadline:
        try:
            r = requests.get(src["url"], params={
                "where": "1=1", "outFields": outfields, "f": "json",
                "resultOffset": offset, "resultRecordCount": _PAGE_SIZE,
                "returnGeometry": "true", "outSR": 4326,
            }, timeout=25, headers={"User-Agent": _UA})
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
    rows_all = []
    for src in SOURCES:
        rows = _fetch_pages(src, deadline)
        out["sources"][src["key"]] = len(rows)
        rows_all.extend(rows)
    c = _conn()
    if c is None:
        out["status"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            for r in rows_all:
                try:
                    cur.execute("""
                        INSERT INTO hosting_capacity_feeders
                          (utility, feeder_key, feeder_id, substation, state,
                           region, voltage_kv, capacity_mw_max, capacity_mw_min,
                           queued_gen_kw, lat, lng, src_updated)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (utility, feeder_key) DO UPDATE SET
                          capacity_mw_max = EXCLUDED.capacity_mw_max,
                          capacity_mw_min = EXCLUDED.capacity_mw_min,
                          queued_gen_kw = EXCLUDED.queued_gen_kw,
                          voltage_kv = EXCLUDED.voltage_kv,
                          src_updated = EXCLUDED.src_updated,
                          ingested_at = NOW()
                    """, (r["utility"], r["feeder_key"], r["feeder_id"],
                          r["substation"], r["state"], r["region"],
                          r["voltage_kv"], r["capacity_mw_max"],
                          r["capacity_mw_min"], r["queued_gen_kw"],
                          r["lat"], r["lng"], r["src_updated"]))
                    out["rows"] += 1
                except Exception:
                    c.rollback()
                    continue
        c.commit()
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


def feeders_near(lat: float, lng: float, radius_km: float = 40.0) -> dict:
    """Feeder-truth block for the hosting-capacity endpoint. {} fail-soft."""
    c = _conn()
    if c is None:
        return {}
    try:
        deg = max(0.05, radius_km / 111.0)
        with c.cursor() as cur:
            cur.execute("""
                SELECT utility, feeder_id, substation, voltage_kv,
                       capacity_mw_max, capacity_mw_min, lat, lng, src_updated
                  FROM hosting_capacity_feeders
                 WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                   AND capacity_mw_max IS NOT NULL
                 ORDER BY capacity_mw_max DESC LIMIT 12
            """, (lat - deg, lat + deg, lng - deg, lng + deg))
            rows = cur.fetchall()
        if not rows:
            return {}
        top = [{"utility": r[0], "feeder_id": r[1], "substation": r[2],
                "voltage_kv": r[3], "capacity_mw_max": r[4],
                "capacity_mw_min": r[5], "lat": r[6], "lng": r[7],
                "src_updated": r[8]} for r in rows[:6]]
        return {"feeder_count_in_bbox": len(rows),
                "max_feeder_capacity_mw": max(r[4] for r in rows),
                "top_feeders": top,
                "utilities": sorted({r[0] for r in rows}),
                "basis": "utility-published feeder hosting-capacity (ingested)",
                "note": ("Utility hosting-capacity maps are informational, "
                         "not binding interconnection guidance.")}
    except Exception:
        return {}
    finally:
        try:
            c.close()
        except Exception:
            pass
