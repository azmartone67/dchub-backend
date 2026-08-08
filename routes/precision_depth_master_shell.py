"""
precision_depth_master_shell.py — the "next tier" grid/gas/fiber depth actuator
(shell #24, 2026-07-18).

WHERE THIS SITS.  [[reference_dchub_depth_master_shell]] (shipped 07-06) already
closed the first tier of depth gaps — PJM capacity-auction prices, DC-classified
interconnection LOAD, substation-proximity hosting capacity, and fiber long-haul
coverage — all ingesting into grid_ext_metrics. This shell takes the tier BEYOND
that, the three things a siting decision actually needs and no competitor
machine-serves:

  (1) POINT-LEVEL PROXIMITY — distance-to-nearest HV transmission LINE and gas
      PIPELINE (with parsed voltage / diameter), the site-selection holy grail.
      Depth scored substation *points*; lines + pipes are the missing geometry.
  (2) THE GAS LAYER — Depth touched grid + fiber only. get_gas_intelligence today
      ships pipeline *presence* + a SYNTHETIC-labelled basis and OMITS storage,
      LNG, and firm capacity. This shell fills real hub basis + pipeline proximity
      honestly (dated, cited, never fabricated).
  (3) LIVE CONNECTIVITY — PeeringDB IX/participant density (free API, no key) and
      cloud on-ramp proximity (Equinix Fabric / Megaport / AWS DX / Azure ER).

Plus it closes a SURFACING gap Depth left: get_grid_scoreboard still returns
`interconnection_queue.dc_share_pct: 0` for every ISO even though Depth ingests
`dc_load_queue_gw`. Lane `dc_load_share` derives the share from EXISTING verified
data and ingests it so the scoreboard/grid tools can surface it.

HOUSE PATTERN (mirrors depth_master_shell.py exactly): admin-gated, killable, ONE
bounded action per tick, 0-100 score, weakest-lane-first, snapshot table
`precision_depth_snapshots`, fail-soft, ingest into grid_ext_metrics so
main._grid_ext_metrics_for auto-surfaces every lane.

★ SHIPS SHADOW.  Unlike Depth (which acts by default), this is a brand-new shell
touching a NEW sink (gas) — so ACT is OFF unless PRECISION_DEPTH_MASTER_ACT_ENABLE=1.
Default behaviour = measure + file precise, sourced upgrade specs to brain_findings
(the proven "grid_data files → depth acts" progression). Arming is a reviewed,
one-env-flag step per the ARM PATH at the bottom.

★ NO FABRICATION.  gas_basis and large_load_tariff ship with EMPTY seed arrays —
they ingest ONLY dated, cited figures supplied via env JSON (or a future loader).
The lane is built and armable; it never invents a number. This is the same
contract capacity_price honours via DEPTH_CAPACITY_PRICES_JSON.

LANES (weakest → one bounded action/tick):
  1. dc_load_share          — power  — ARMABLE NOW (pure derivation from existing DB)
  2. gas_basis              — gas    — built; populate PRECISION_GAS_BASIS_JSON to arm
  3. large_load_tariff      — power  — built; populate PRECISION_LARGE_LOAD_TARIFF_JSON
  4. transmission_proximity — power  — measure + file loader spec (HIFLD lines)
  5. gas_pipeline_proximity — gas    — measure + file loader spec (HIFLD/EIA pipes)
  6. peering_density        — fiber  — measure + file loader spec (PeeringDB API)
  7. cloud_onramp           — fiber  — measure + file loader spec (Equinix/Megaport)

Endpoints:
  POST /api/v1/admin/precision-depth/master-tick   — measure → score → act → persist (admin)
  GET  /api/v1/admin/precision-depth/master-state   — latest snapshot + trend (admin)
  GET  /api/v1/grid/dc-load-share[?iso=]            — PUBLIC: DC share of the queue per ISO
  GET  /api/v1/gas/basis[?hub=]                     — PUBLIC: hub basis differentials, dated+cited
  GET  /api/v1/grid/large-load-tariff[?utility=|state=] — PUBLIC: large-load/industrial tariffs

Kill switches:
  PRECISION_DEPTH_MASTER_DISABLED=1        — skip the whole tick
  PRECISION_DEPTH_MASTER_ACT_ENABLE=1      — ARM writes (absent/0 = SHADOW: measure+file only)
  PRECISION_DEPTH_LEVER_<NAME>_OFF=1       — disable one lane
"""
import os
import json
import time
import hmac
import math
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

precision_depth_master_shell_bp = Blueprint("precision_depth_master_shell", __name__)


# ── auth + kill switches (mirror depth_master_shell) ──────────────────────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.headers.get("X-Internal-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("PRECISION_DEPTH_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_armed() -> bool:
    # SHADOW BY DEFAULT — a brand-new shell touching a new sink (gas) does not write
    # until explicitly armed. (Inverts depth's act-by-default posture on purpose.)
    return str(os.environ.get("PRECISION_DEPTH_MASTER_ACT_ENABLE", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    return str(os.environ.get(f"PRECISION_DEPTH_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── DB ────────────────────────────────────────────────────────────────────
def _conn():
    """Shared read/simple-write conn (autocommit — fine for INSERT/SELECT/CREATE)."""
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _txn_conn():
    """Own autocommit=False conn — REQUIRED for upsert_brain_finding (SAVEPOINT-wrapped,
    silent no-op under autocommit — the trap that cost the grid + depth shells a redeploy)."""
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = False
        return c
    except Exception:
        return None


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _now_hour():
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _ingest_ext(cur, *, source, dataset_id, iso, category, value, unit, raw):
    """Upsert one grid_ext_metrics row using the proven Depth shape + UNIQUE(dataset_id, as_of).
    Auto-surfaces via main._grid_ext_metrics_for. Caller owns the txn/rollback."""
    cur.execute("""
        INSERT INTO grid_ext_metrics
          (source, dataset_id, iso, category, primary_value, unit, as_of, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset_id, as_of) DO UPDATE
          SET source = EXCLUDED.source,
              primary_value = EXCLUDED.primary_value,
              raw = EXCLUDED.raw, ingested_at = NOW()
    """, (source, dataset_id, iso, category, value, unit, _now_hour(), json.dumps(raw)))


# ── geo helpers (bbox prefilter + haversine — mirrors depth._substation_headroom) ──
def _haversine_km(lat, lng, la, lo):
    r = 6371.0
    p1, p2 = math.radians(lat), math.radians(la)
    dp = math.radians(la - lat); dl = math.radians(lo - lng)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _bbox(lat, lng, radius_km):
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def _market_latlng(market):
    """Resolve a market slug → centroid via market_power_scores (same source depth uses)."""
    c = _conn()
    if c is None:
        return (None, None)
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT latitude, longitude FROM market_power_scores
                            WHERE LOWER(market_slug)=LOWER(%s) AND latitude IS NOT NULL
                            ORDER BY computed_at DESC LIMIT 1""", (market,))
            r = cur.fetchone()
            return (r[0], r[1]) if r else (None, None)
    except Exception:
        return (None, None)
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# LANE 1 — dc_load_share  (POWER · ARMABLE NOW)
# Closes the get_grid_scoreboard `dc_share_pct: 0` surfacing gap. Depth ingests
# dc_load_queue GW; this derives DC share of the total queue per ISO from EXISTING
# verified data (interconnect_queue totals + grid_ext_metrics dc_load_queue) and
# ingests category='dc_load_share' (unit='pct'). No new feed, no fabrication.
# ═══════════════════════════════════════════════════════════════════════════
def _dc_load_share_measure() -> dict:
    """Per-ISO: dc_load_queue GW (Depth), total queued GW (interconnect_queue), share."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            # total queued GW per ISO
            cur.execute("""
                SELECT iso, ROUND((SUM(capacity_mw)/1000.0)::numeric, 2)
                  FROM interconnect_queue
                 WHERE capacity_mw IS NOT NULL AND capacity_mw > 0 AND iso IS NOT NULL
                 GROUP BY iso
            """)
            totals = {iso: float(gw) for iso, gw in cur.fetchall() if gw}
            # DC load GW per ISO (Depth's output)
            cur.execute("""
                SELECT DISTINCT ON (iso) iso, primary_value
                  FROM grid_ext_metrics
                 WHERE category = 'dc_load_queue' AND primary_value IS NOT NULL
                 ORDER BY iso, as_of DESC NULLS LAST
            """)
            dc = {iso: float(pv) for iso, pv in cur.fetchall() if pv}
        for iso, dc_gw in dc.items():
            tot = totals.get(iso)
            share = round(100.0 * dc_gw / tot, 1) if tot and tot > 0 else None
            out[iso] = {"dc_gw": dc_gw, "total_queue_gw": tot, "dc_share_pct": share}
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _act_dc_load_share() -> dict:
    state = _dc_load_share_measure()
    if not state:
        return {"ok": False, "reason": "no_dc_load_or_queue_rows"}
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "db_unavailable"}
    wrote = 0
    try:
        with c.cursor() as cur:
            for iso, s in state.items():
                if s.get("dc_share_pct") is None:
                    continue
                try:
                    _ingest_ext(cur, source="derived", dataset_id=f"dc_load_share:{iso}",
                                iso=iso, category="dc_load_share", value=s["dc_share_pct"],
                                unit="pct", raw={"iso": iso, **s,
                                "method": "dc_load_queue GW / interconnect_queue total GW"})
                    wrote += 1
                except Exception:
                    note_swallowed_write("grid_ext_metrics", where="precision_depth._act_dc_load_share")
                    c.rollback()
                    continue
        return {"ok": True, "isos_scored": wrote, "by_iso": {k: v.get("dc_share_pct") for k, v in state.items()}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# LANE 2 — gas_basis  (GAS · built; zero-fabrication seed)
# Replaces the synthetic-labelled basis in get_gas_intelligence with REAL, dated,
# cited hub differentials. Ships EMPTY — ingests only rows supplied via
# PRECISION_GAS_BASIS_JSON (or a future EIA/ICE daily-index loader). Target hubs
# below define "full coverage" for the score; nothing is invented.
# ═══════════════════════════════════════════════════════════════════════════
_GAS_BASIS_TARGET_HUBS = ["WAHA", "DOM-SOUTH", "ALGONQUIN", "CHICAGO", "SOCAL"]  # vs Henry Hub


def _gas_basis_rows() -> list:
    """Real, dated, cited basis rows from two honest sources — NEVER fabricated:
      1. PRECISION_GAS_BASIS_JSON  — inline rows (no deploy needed).
      2. PRECISION_GAS_BASIS_FEED_URL — a licensed source (NGI/ICE/Platts) or an
         owner-run shim the shell GETs each tick; accepts a JSON list, or an object
         with a `rows`/`data` list. This is the auto-ingest hook — point it at a
         real feed and gas_basis fills itself (then drop PRECISION_DEPTH_LEVER_GAS_BASIS_OFF)."""
    rows = []
    ov = os.environ.get("PRECISION_GAS_BASIS_JSON")
    if ov:
        try:
            j = json.loads(ov)
            if isinstance(j, list):
                rows += j
        except Exception:
            pass
    feed = os.environ.get("PRECISION_GAS_BASIS_FEED_URL")
    if feed:
        try:
            req = urllib.request.Request(feed, headers={"User-Agent": "dchub-precision-depth/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(j, dict):
                j = j.get("rows") or j.get("data") or []
            if isinstance(j, list):
                rows += j
        except Exception:
            pass  # feed down / malformed → ingest nothing (no fabrication)
    return rows


def _act_gas_basis() -> dict:
    rows = _gas_basis_rows()
    if not rows:
        return {"ok": True, "rows_upserted": 0,
                "reason": "no verified basis rows — set PRECISION_GAS_BASIS_JSON "
                          "(shape: [{hub, basis_usd_mmbtu, as_of, source}]). No fabrication."}
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "db_unavailable"}
    wrote = 0
    try:
        with c.cursor() as cur:
            for r in rows:
                hub = (r.get("hub") or "").upper().strip()
                basis = _num(r.get("basis_usd_mmbtu"))
                if not hub or basis is None or not r.get("source"):
                    continue  # require hub + value + citation
                try:
                    _ingest_ext(cur, source="published_index", dataset_id=f"gas_basis:{hub}",
                                iso=None, category="gas_basis", value=basis, unit="usd_mmbtu",
                                raw={"hub": hub, "basis_usd_mmbtu": basis,
                                     "as_of": r.get("as_of"), "source": r.get("source")})
                    wrote += 1
                except Exception:
                    note_swallowed_write("grid_ext_metrics", where="precision_depth._act_gas_basis")
                    c.rollback()
                    continue
        return {"ok": True, "rows_upserted": wrote}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# LANE 3 — large_load_tariff  (POWER · built; zero-fabrication seed)
# Industrial + named large-load tariffs (EIA-861 industrial $/kWh; Dominion GS-4 /
# large-load rider, AEP-Ohio, Georgia Power dockets). Ships EMPTY — ingests only
# dated, cited rows via PRECISION_LARGE_LOAD_TARIFF_JSON. Makes gas-vs-grid $/MWh
# compare against ACTUAL retail, not a modelled grid price.
# ═══════════════════════════════════════════════════════════════════════════
_TARIFF_TARGET_UTILITIES = ["DOMINION", "AEP-OHIO", "GEORGIA-POWER", "AEP-TX", "APS"]


def _tariff_rows() -> list:
    ov = os.environ.get("PRECISION_LARGE_LOAD_TARIFF_JSON")
    if not ov:
        return []
    try:
        rows = json.loads(ov)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _act_large_load_tariff() -> dict:
    rows = _tariff_rows()
    if not rows:
        return {"ok": True, "rows_upserted": 0,
                "reason": "no verified tariff rows — set PRECISION_LARGE_LOAD_TARIFF_JSON "
                          "(shape: [{utility, state, tariff, usd_per_kwh, demand_charge, as_of, source, docket}])."}
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "db_unavailable"}
    wrote = 0
    try:
        with c.cursor() as cur:
            for r in rows:
                util = (r.get("utility") or "").upper().strip()
                rate = _num(r.get("usd_per_kwh"))
                if not util or rate is None or not r.get("source"):
                    continue
                try:
                    _ingest_ext(cur, source="tariff_filing", dataset_id=f"large_load_tariff:{util}",
                                iso=None, category="large_load_tariff", value=rate, unit="usd_kwh",
                                raw={"utility": util, "state": r.get("state"), "tariff": r.get("tariff"),
                                     "usd_per_kwh": rate, "demand_charge": r.get("demand_charge"),
                                     "as_of": r.get("as_of"), "docket": r.get("docket"),
                                     "source": r.get("source")})
                    wrote += 1
                except Exception:
                    note_swallowed_write("grid_ext_metrics", where="precision_depth._act_large_load_tariff")
                    c.rollback()
                    continue
        return {"ok": True, "rows_upserted": wrote}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# LANES 4-7 — loader-dependent (measure + file precise loader spec to brain).
# These need a new geo layer / live pull that a data-loader owns; the shell's
# bounded action until then is to KEEP THE SPEC LIVE in brain_findings (exactly
# how Depth filed ISO-NE FCA / feeder-GIS). Each carries the exact public source.
# ═══════════════════════════════════════════════════════════════════════════
def _layer_present(table: str) -> int:
    """Best-effort row count for a candidate geo layer (0 if absent/unreadable)."""
    c = _conn()
    if c is None:
        return 0
    n = 0
    try:
        with c.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # table names are hardcoded, not user input
            n = int(cur.fetchone()[0] or 0)
    except Exception:
        n = 0
    finally:
        try: c.close()
        except Exception: pass
    return n


# lane_name -> (candidate table, gap issue, gap detail w/ source + touchpoint)
_LOADER_LANES = {
    "transmission_proximity": (
        "transmission_lines",
        "precision_transmission_line_proximity",
        "SHIPPED point-level: GET /api/v1/grid/transmission-proximity?lat=&lon= — nearest HV lines "
        "(voltage/operator/endpoints/distance). transmission_lines has NO geometry AND state is NULL "
        "for all 94k rows in prod, so each line is geolocated via its from_sub substation "
        "(~76% match substations.name, which has coords; voltage_kv populated on 80k). UPGRADE: parse "
        "infrastructure_layers.coordinates for the ~24% unmatched + true line paths."),
    "gas_pipeline_proximity": (
        "gas_pipelines",
        "precision_gas_pipeline_proximity",
        "SHIPPED: GET /api/v1/gas/pipeline-proximity?lat=&lon= — nearest active gas pipelines "
        "(operator, diameter_inches, type, distance_km) via bbox+haversine on the 31k gas_pipelines "
        "table. Closes the get_gas_intelligence 'pipeline PRESENCE only' gap with point-level distance. "
        "UPGRADE: diameter_inches/capacity_mcf are sparse in the EIA load — backfill firm-capacity."),
    "peering_density": (
        "peeringdb_ix",
        "precision_peering_density_live",
        "SHIPPED metro-level: GET /api/v1/grid/peering?city= (public namespace — /api/v1/fiber is "
        "paywalled behind the L&P map). IX count + total participants + top exchanges by metro, off "
        "the 1.3k peeringdb_ix table. peeringdb coords are 100% NULL in prod (both peeringdb_ix and "
        "peeringdb_ix_facilities) so this is city/ILIKE-scoped, not lat/lon. UPGRADE: backfill IX "
        "coordinates from the free PeeringDB API for true point-radius density + Equinix/Megaport fabrics."),
    "cloud_onramp": (
        "cloud_onramps",
        "precision_cloud_onramp_proximity",
        "Nearest cloud on-ramp (Equinix Fabric / Megaport / AWS Direct Connect / Azure ExpressRoute) "
        "+ distance is a top-5 connectivity question and is the ONLY loader lane with no data yet "
        "(cloud_onramps table empty). Locations are published per provider; load into `cloud_onramps` "
        "(lat/lng/provider/type), then expose GET /api/v1/fiber/cloud-onramp?lat=&lon=."),
}


def _loader_lane_measure() -> dict:
    return {name: _layer_present(tbl) for name, (tbl, _iss, _d) in _LOADER_LANES.items()}


# ── file the full lane roster (built + upgrade-path) to brain ──────────────
_GAP_FINDINGS = [
    ("precision_dc_load_share_surface",
     "dc_load_share ingests DC share-of-queue % per ISO (grid_ext_metrics category=dc_load_share) "
     "from existing verified data — closing get_grid_scoreboard's dc_share_pct:0. WIRE: point the "
     "scoreboard's interconnection_queue.dc_share_pct at grid_ext_metrics(category=dc_load_share)."),
    ("precision_gas_basis_index_feed",
     "gas_basis lane replaces get_gas_intelligence's SYNTHETIC basis with real, dated, cited hub "
     "differentials (Waha/Dom-South/Algonquin/Chicago/SoCal vs Henry Hub). Populate via "
     "PRECISION_GAS_BASIS_JSON or an EIA/ICE daily-index loader; then read grid_ext_metrics"
     "(category=gas_basis) in get_gas_intelligence's basis field (drops the 'synthetic' label)."),
    ("precision_large_load_tariff_feed",
     "large_load_tariff lane ingests industrial + named large-load tariffs (EIA-861 industrial "
     "$/kWh; Dominion large-load rider, AEP-Ohio, Georgia Power dockets) so gas-vs-grid compares "
     "against ACTUAL retail. Populate via PRECISION_LARGE_LOAD_TARIFF_JSON; surface in get_grid_intelligence."),
]


def _file_gap_findings() -> int:
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return 0
    findings = list(_GAP_FINDINGS)
    # add the loader-dependent lanes only while their layer is absent
    present = _loader_lane_measure()
    for name, (tbl, issue, detail) in _LOADER_LANES.items():
        if present.get(name, 0) <= 0:
            findings.append((issue, detail))
    conn = _txn_conn()
    if conn is None:
        return 0
    filed = 0
    try:
        with conn.cursor() as cur:
            for issue, detail in findings:
                try:
                    r = upsert_brain_finding(cur, issue=issue,
                                             url="/api/v1/admin/precision-depth/master-tick",
                                             count=1, detail=detail, detector="precision_depth")
                    if r in ("inserted", "updated"):
                        filed += 1
                except Exception:
                    continue
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    return filed


# ── snapshot table ─────────────────────────────────────────────────────────
def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS precision_depth_snapshots (
                    id             SERIAL PRIMARY KEY,
                    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    precision_score NUMERIC(6,2),
                    weakest_lever  TEXT,
                    action_taken   TEXT,
                    armed          BOOLEAN,
                    lever_scores   JSONB,
                    findings_filed INTEGER,
                    detail         JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── coverage reads (for scoring) ───────────────────────────────────────────
def _category_isos(category: str) -> set:
    c = _conn()
    if c is None:
        return set()
    out = set()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT DISTINCT iso FROM grid_ext_metrics WHERE category=%s AND iso IS NOT NULL", (category,))
            out = {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _category_datasets(category: str) -> int:
    c = _conn()
    if c is None:
        return 0
    n = 0
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT dataset_id) FROM grid_ext_metrics WHERE category=%s", (category,))
            n = int(cur.fetchone()[0] or 0)
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return n


# ── MEASURE → SCORE → ACT → PERSIST ────────────────────────────────────────
def measure() -> dict:
    dc_load_isos = _category_isos("dc_load_queue")           # ISOs Depth classified
    dc_share_isos = _category_isos("dc_load_share")          # ISOs this shell has scored
    return {
        "dc_load_classified_isos": sorted(dc_load_isos),
        "dc_share_scored_isos": sorted(dc_share_isos),
        "gas_basis_hubs": _category_datasets("gas_basis"),
        "large_load_tariff_utils": _category_datasets("large_load_tariff"),
        "loader_layers": _loader_lane_measure(),
    }


def score_levers(m: dict) -> dict:
    dc_load = set(m.get("dc_load_classified_isos") or [])
    dc_scored = set(m.get("dc_share_scored_isos") or [])
    # dc_load_share: fraction of Depth-classified ISOs that now carry a share
    dc_load_share = round(len(dc_scored & dc_load) / len(dc_load), 3) if dc_load else 0.0
    gas_basis = round(min(1.0, (m.get("gas_basis_hubs") or 0) / len(_GAS_BASIS_TARGET_HUBS)), 3)
    large_load_tariff = round(min(1.0, (m.get("large_load_tariff_utils") or 0) / len(_TARIFF_TARGET_UTILITIES)), 3)
    layers = m.get("loader_layers") or {}
    transmission_proximity = 1.0 if (layers.get("transmission_proximity") or 0) > 0 else 0.0
    gas_pipeline_proximity = 1.0 if (layers.get("gas_pipeline_proximity") or 0) > 0 else 0.0
    peering_density = 1.0 if (layers.get("peering_density") or 0) > 0 else 0.0
    cloud_onramp = 1.0 if (layers.get("cloud_onramp") or 0) > 0 else 0.0
    scores = {
        "dc_load_share": dc_load_share,
        "gas_basis": gas_basis,
        "large_load_tariff": large_load_tariff,
        "transmission_proximity": transmission_proximity,
        "gas_pipeline_proximity": gas_pipeline_proximity,
        "peering_density": peering_density,
        "cloud_onramp": cloud_onramp,
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    # weakest ACTABLE lane first: the 4 loader lanes act via file-to-brain only, so
    # prefer a weak lane the shell can actually write; fall back to weakest overall.
    actable = {"dc_load_share", "gas_basis", "large_load_tariff"}
    weakest = next((n for n, _ in ranked if n in actable and not _lever_off(n)), None)
    if weakest is None:
        weakest = next((n for n, _ in ranked if not _lever_off(n)), ranked[0][0])
    return {"scores": scores, "weakest": weakest}


def act(m: dict, levers: dict) -> dict:
    if not _act_armed():
        return {"action": "none", "reason": "SHADOW — set PRECISION_DEPTH_MASTER_ACT_ENABLE=1 to arm"}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed"}
    if lever == "dc_load_share":
        return {"lever": lever, "action": "ingest_dc_load_share", "result": _act_dc_load_share()}
    if lever == "gas_basis":
        return {"lever": lever, "action": "ingest_gas_basis", "result": _act_gas_basis()}
    if lever == "large_load_tariff":
        return {"lever": lever, "action": "ingest_large_load_tariff", "result": _act_large_load_tariff()}
    # loader-dependent lanes: the bounded action is keeping the loader spec filed to brain
    if lever in _LOADER_LANES:
        return {"lever": lever, "action": "file_loader_spec",
                "result": {"note": _LOADER_LANES[lever][2], "candidate_table": _LOADER_LANES[lever][0]}}
    return {"action": "none", "reason": "unknown lever"}


def precision_score(levers: dict) -> float:
    s = levers.get("scores") or {}
    return round(100.0 * (sum(s.values()) / len(s)), 2) if s else 0.0


def _persist(m: dict, levers: dict, score: float, action: dict, findings: int) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO precision_depth_snapshots
                  (precision_score, weakest_lever, action_taken, armed, lever_scores, findings_filed, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (score, levers.get("weakest"), (action or {}).get("action"), _act_armed(),
                  json.dumps(levers.get("scores") or {}), findings, json.dumps({**m, "action": action})))
        return True
    except Exception:
        note_swallowed_write("precision_depth_snapshots", where="precision_depth._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ENDPOINTS ──────────────────────────────────────────────────────────────
@precision_depth_master_shell_bp.route("/api/v1/admin/precision-depth/master-tick", methods=["POST", "GET"])
def precision_depth_master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="PRECISION_DEPTH_MASTER_DISABLED"), 200
    started = time.time()
    _ensure_tables()
    m = measure()
    levers = score_levers(m)
    action = act(m, levers)
    m2 = measure()  # re-measure so the persisted snapshot reflects this tick's write
    findings = _file_gap_findings()
    score = precision_score(score_levers(m2))
    persisted = _persist(m2, levers, score, action, findings)
    s = levers.get("scores") or {}
    headline = (
        f"precision {score}/100 · weakest → {levers.get('weakest')} ({s.get(levers.get('weakest'))}) · "
        f"acted: {action.get('action')} · armed={_act_armed()} · "
        f"dc_share {len(m2.get('dc_share_scored_isos') or [])}/{len(m2.get('dc_load_classified_isos') or [])} isos · "
        f"gas_basis {m2.get('gas_basis_hubs')} hubs · tariffs {m2.get('large_load_tariff_utils')} · "
        f"{findings} specs→brain"
    )
    return jsonify(
        ok=True, ms=int((time.time() - started) * 1000),
        precision_score=score, headline=headline, armed=_act_armed(),
        tier1_measure=m2, tier2_levers=levers, tier3_action=action,
        findings_filed=findings, persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@precision_depth_master_shell_bp.route("/api/v1/admin/precision-depth/master-state", methods=["GET"])
def precision_depth_master_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM precision_depth_snapshots ORDER BY computed_at DESC LIMIT 12")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("computed_at"):
                r["computed_at"] = r["computed_at"].isoformat()
        return jsonify(available=bool(rows), latest=(rows[0] if rows else None), trend=rows), 200
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass


@precision_depth_master_shell_bp.route("/api/v1/grid/dc-load-share", methods=["GET"])
def grid_dc_load_share():
    """Public: DC share of the interconnection queue per ISO (%), derived + cited."""
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    iso = (request.args.get("iso") or "").upper().strip()
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if iso:
                cur.execute("""SELECT DISTINCT ON (iso) iso, primary_value AS dc_share_pct, as_of, raw
                                 FROM grid_ext_metrics WHERE category='dc_load_share' AND iso=%s
                                ORDER BY iso, as_of DESC NULLS LAST""", (iso,))
            else:
                cur.execute("""SELECT DISTINCT ON (iso) iso, primary_value AS dc_share_pct, as_of, raw
                                 FROM grid_ext_metrics WHERE category='dc_load_share'
                                ORDER BY iso, as_of DESC NULLS LAST""")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("as_of"):
                r["as_of"] = r["as_of"].isoformat() if hasattr(r["as_of"], "isoformat") else r["as_of"]
        return jsonify(available=bool(rows), count=len(rows), by_iso=rows,
                       note="DC share of interconnection queue; DC Hub (dchub.cloud), CC-BY-4.0."), 200
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass


@precision_depth_master_shell_bp.route("/api/v1/gas/basis", methods=["GET"])
def gas_basis():
    """Public: hub basis differentials vs Henry Hub ($/MMBtu), dated + cited."""
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    hub = (request.args.get("hub") or "").upper().strip()
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if hub:
                cur.execute("""SELECT DISTINCT ON (dataset_id) dataset_id, primary_value AS basis_usd_mmbtu, as_of, raw
                                 FROM grid_ext_metrics WHERE category='gas_basis' AND dataset_id=%s
                                ORDER BY dataset_id, as_of DESC NULLS LAST""", (f"gas_basis:{hub}",))
            else:
                cur.execute("""SELECT DISTINCT ON (dataset_id) dataset_id, primary_value AS basis_usd_mmbtu, as_of, raw
                                 FROM grid_ext_metrics WHERE category='gas_basis'
                                ORDER BY dataset_id, as_of DESC NULLS LAST""")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("as_of"):
                r["as_of"] = r["as_of"].isoformat() if hasattr(r["as_of"], "isoformat") else r["as_of"]
        return jsonify(available=bool(rows), count=len(rows), hubs=rows,
                       target_hubs=_GAS_BASIS_TARGET_HUBS,
                       note="Gas hub basis vs Henry Hub; published index, dated. DC Hub (dchub.cloud)."), 200
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass


@precision_depth_master_shell_bp.route("/api/v1/grid/large-load-tariff", methods=["GET"])
def grid_large_load_tariff():
    """Public: large-load / industrial electricity tariffs ($/kWh), dated + cited."""
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    util = (request.args.get("utility") or "").upper().strip()
    state = (request.args.get("state") or "").upper().strip()
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT DISTINCT ON (dataset_id) dataset_id, primary_value AS usd_per_kwh, as_of, raw
                             FROM grid_ext_metrics WHERE category='large_load_tariff'
                            ORDER BY dataset_id, as_of DESC NULLS LAST""")
            rows = [dict(r) for r in cur.fetchall()]
        out = []
        for r in rows:
            raw = r.get("raw") or {}
            if util and (raw.get("utility") or "").upper() != util:
                continue
            if state and (raw.get("state") or "").upper() != state:
                continue
            if r.get("as_of"):
                r["as_of"] = r["as_of"].isoformat() if hasattr(r["as_of"], "isoformat") else r["as_of"]
            out.append(r)
        return jsonify(available=bool(out), count=len(out), tariffs=out,
                       target_utilities=_TARIFF_TARGET_UTILITIES,
                       note="Large-load/industrial tariffs; utility filings + EIA-861. DC Hub (dchub.cloud)."), 200
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# POINT-LEVEL PROXIMITY ENDPOINTS (off data already in the DB — no new loader)
# ═══════════════════════════════════════════════════════════════════════════
@precision_depth_master_shell_bp.route("/api/v1/gas/pipeline-proximity", methods=["GET"])
def gas_pipeline_proximity():
    """Public: nearest active gas pipelines to a point (operator, diameter, type, distance).
    ?lat=&lon=[&radius_km=]. bbox prefilter + haversine on the gas_pipelines layer."""
    lat = _num(request.args.get("lat")); lon = _num(request.args.get("lon") or request.args.get("lng"))
    radius = _num(request.args.get("radius_km")) or 40.0
    radius = max(5.0, min(200.0, radius))
    if lat is None or lon is None:
        return jsonify(available=False, reason="need lat & lon"), 200
    la0, la1, lo0, lo1 = _bbox(lat, lon, radius)
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT name, operator, pipeline_type, diameter_inches, lat, lng
                  FROM gas_pipelines
                 WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                   AND lat IS NOT NULL AND lng IS NOT NULL
                   AND COALESCE(status, 'active') = 'active'
                 LIMIT 5000
            """, (la0, la1, lo0, lo1))
            rows = cur.fetchall()
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass
    near = []
    for name, op, ptype, dia, pla, plo in rows:
        if pla is None or plo is None:
            continue
        d = _haversine_km(lat, lon, _num(pla), _num(plo))
        if d > radius:
            continue
        near.append({"operator": op, "pipeline": name, "type": ptype,
                     "diameter_inches": _num(dia), "distance_km": round(d, 1)})
    near.sort(key=lambda x: x["distance_km"])
    if not near:
        return jsonify(available=True, pipeline_count=0, nearest_km=None,
                       verdict="no_pipeline_nearby",
                       note="No active gas pipeline within radius. DC Hub (dchub.cloud), CC-BY-4.0."), 200
    operators = sorted({n["operator"] for n in near if n["operator"]})
    return jsonify(available=True, pipeline_count=len(near),
                   nearest_km=near[0]["distance_km"], nearest=near[0],
                   operators=operators[:12], top=near[:8],
                   note="Nearest active gas pipelines (HIFLD/EIA). DC Hub (dchub.cloud), CC-BY-4.0."), 200


@precision_depth_master_shell_bp.route("/api/v1/grid/peering", methods=["GET"])
def grid_peering():
    """Public: internet-exchange (IX) depth for a metro — IX count + total peering
    participants + top exchanges. ?city=<name>[&country=<cc>]. NOTE peeringdb carries
    city + participant counts but its coordinates are unpopulated, so this is
    metro/city-scoped (ILIKE), not lat/lon proximity. Public namespace (/api/v1/fiber
    is paywalled behind the Land & Power map)."""
    city = (request.args.get("city") or request.args.get("market") or "").strip()
    country = (request.args.get("country") or "").strip().upper()
    if not city and not country:
        return jsonify(available=False, reason="need ?city=<metro> (optional &country=<cc>)"), 200
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            q = "SELECT name, city, country, participants FROM peeringdb_ix WHERE 1=1"
            args = []
            if city:
                q += " AND city ILIKE %s"; args.append("%" + city + "%")
            if country:
                q += " AND UPPER(country) = %s"; args.append(country)
            q += " ORDER BY participants DESC NULLS LAST LIMIT 200"
            cur.execute(q, tuple(args))
            rows = cur.fetchall()
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass
    ixes = [{"ix": n, "city": ct, "country": co, "participants": _num(p)} for n, ct, co, p in rows]
    if not ixes:
        return jsonify(available=True, ix_count=0, total_participants=0,
                       query={"city": city, "country": country}, verdict="no_ix",
                       note="No matching internet exchange (PeeringDB). DC Hub (dchub.cloud)."), 200
    total = int(sum((x["participants"] or 0) for x in ixes))
    score = min(100, int(min(60, len(ixes) * 10) + min(40, total / 20.0)))
    verdict = ("dense" if score >= 70 else "moderate" if score >= 40
               else "sparse" if score >= 15 else "thin")
    return jsonify(available=True, ix_count=len(ixes), total_participants=total,
                   peering_score=score, verdict=verdict, top_ix=ixes[:10],
                   query={"city": city, "country": country},
                   note="Internet-exchange depth by metro (PeeringDB). DC Hub (dchub.cloud), CC-BY-4.0."), 200


@precision_depth_master_shell_bp.route("/api/v1/grid/transmission-proximity", methods=["GET"])
def grid_transmission_proximity():
    """Public: nearest HV transmission LINES to a point — voltage, operator, endpoints,
    distance. ?lat=&lon=[&radius_km=][&min_kv=]. transmission_lines has no geometry of its
    own, so each line is geolocated via its from_sub substation (substations layer, which
    HAS coords; ~76% of lines match). Complements depth's substation hosting-capacity with
    LINE voltage + operator."""
    lat = _num(request.args.get("lat")); lon = _num(request.args.get("lon") or request.args.get("lng"))
    radius = _num(request.args.get("radius_km")) or 40.0
    radius = max(5.0, min(200.0, radius))
    min_kv = _num(request.args.get("min_kv")) or 0.0
    if lat is None or lon is None:
        return jsonify(available=False, reason="need lat & lon"), 200
    la0, la1, lo0, lo1 = _bbox(lat, lon, radius)
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            # 1) substations in the bbox → name→coords (bounded, indexed lat/lng)
            cur.execute("""
                SELECT name, lat, lng FROM substations
                 WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                   AND name IS NOT NULL AND name <> '' AND lat IS NOT NULL AND lng IS NOT NULL
                 LIMIT 1500
            """, (la0, la1, lo0, lo1))
            subs = {}
            for nm, sla, slo in cur.fetchall():
                subs.setdefault(nm, (_num(sla), _num(slo)))
            if not subs:
                return jsonify(available=True, line_count=0, nearest_km=None, verdict="no_line_nearby",
                               note="No substation-anchored transmission line within radius. DC Hub (dchub.cloud)."), 200
            # 2) lines anchored at those substations (single scan via from_sub = ANY)
            cur.execute("""
                SELECT name, operator, voltage_kv, from_sub, to_sub FROM transmission_lines
                 WHERE from_sub = ANY(%s) AND COALESCE(voltage_kv, 0) >= %s
                 LIMIT 8000
            """, (list(subs.keys()), min_kv))
            rows = cur.fetchall()
    except Exception as e:
        return jsonify(available=False, error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass
    near = []
    for name, op, kv, fsub, tsub in rows:
        sla, slo = subs.get(fsub, (None, None))
        if sla is None or slo is None:
            continue
        d = _haversine_km(lat, lon, sla, slo)
        if d > radius:
            continue
        near.append({"line": name, "operator": op, "voltage_kv": _num(kv),
                     "from_sub": fsub, "to_sub": tsub, "distance_km": round(d, 1)})
    near.sort(key=lambda x: x["distance_km"])
    if not near:
        return jsonify(available=True, line_count=0, nearest_km=None, verdict="no_line_nearby",
                       note="No transmission line (via substation match) within radius. DC Hub (dchub.cloud)."), 200
    ehv = [n for n in near if (n["voltage_kv"] or 0) >= 230]
    maxkv = max((n["voltage_kv"] or 0) for n in near)
    return jsonify(available=True, line_count=len(near), ehv_230kv_plus=len(ehv),
                   max_voltage_kv=maxkv, nearest_km=near[0]["distance_km"], nearest=near[0],
                   top=near[:8],
                   note="Nearest HV transmission lines, geolocated via from_sub substation "
                        "(HIFLD; ~76% coverage). DC Hub (dchub.cloud), CC-BY-4.0."), 200
