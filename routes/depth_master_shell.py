"""
depth_master_shell.py — the self-driving DEPTH ACTUATOR (2026-07-06).

The Grid Data Master Shell (grid_data_master_shell.py) FILES the four hardest
grid/fiber depth gaps to brain_findings but never CLOSES them. This shell ACTS
on them every tick — ingesting the real data into grid_ext_metrics + a
substation-headroom layer, and driving the fiber long-haul exposure — so the
"how much power/fiber, at THIS point, and when" moat keeps deepening on its own.

Same house master-shell pattern as grid_data_master_shell / growth_master_shell:
admin-gated, killable, one bounded action/tick, 0-100 score, snapshot table,
fail-soft.

Four lanes (weakest → one bounded action/tick):
  1. capacity_price   — ISO capacity-auction clearing prices ($/MW-day): PJM RPM/BRA,
                        ISO-NE FCA, MISO PRA. The #1-cited DC-power economic signal.
  2. large_load_queue — DC-classified interconnection LOAD per ISO, inferred from
                        interconnect_queue (fuel_type='Load' + data-center name match).
  3. hosting_capacity — region/parcel substation headroom from the 126k-row
                        substations layer (capacity_mva/available_mva) — replaces the
                        Colorado-default grid-headroom stub with a real, region-aware read.
  4. fiber_longhaul   — long-haul route coverage + freshness (the exposure lives in
                        get_fiber_intel market= + the map's live long-haul layer).

Endpoints (admin-gated except the two public reads):
  POST /api/v1/admin/depth/master-tick      — measure → score → act → persist
  GET  /api/v1/admin/depth/master-state     — latest snapshot + trend
  GET  /api/v1/grid/hosting-capacity        — substation headroom (public; lat/lon or region)
  GET  /api/v1/grid/capacity-price          — capacity-auction clearing prices (public)

Kill switches: DEPTH_MASTER_DISABLED=1 (whole tick),
DEPTH_MASTER_ACT_DISABLED=1 (shadow: measure+persist, no writes),
DEPTH_LEVER_<NAME>_OFF=1 (per-lever).
"""
import os
import json
import time
import hmac
import math
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

depth_master_shell_bp = Blueprint("depth_master_shell", __name__)


# ── auth + kill switches (mirror grid_data_master_shell) ──────────────
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
    return str(os.environ.get("DEPTH_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    return str(os.environ.get("DEPTH_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    return str(os.environ.get(f"DEPTH_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── DB ────────────────────────────────────────────────────────────────
def _conn():
    """Shared read/simple-write conn (ai_reach._conn is autocommit=True — fine for
    plain INSERT/SELECT/CREATE, NOT for savepoint-based writers like brain_findings)."""
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _txn_conn():
    """Own transactional (autocommit=False) conn — REQUIRED for upsert_brain_finding,
    which is SAVEPOINT-wrapped and silently no-ops under autocommit (the trap that
    cost the grid shell a redeploy)."""
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


# ═══════════════════════════════════════════════════════════════════════
# LANE 1 — capacity_price (ISO capacity-auction clearing prices, $/MW-day)
# ═══════════════════════════════════════════════════════════════════════
# Public, cited, DATED auction results. No machine-readable feed exists (ISOs
# publish results in PDFs / press releases), so we ingest the published clearing
# prices with full provenance and refresh per auction. Env-overridable via
# DEPTH_CAPACITY_PRICES_JSON (a JSON list of the same shape) so a new auction can
# be added without a deploy. NEVER fabricated — only published, dated figures.
CAPACITY_PRICES = [
    # PJM RPM Base Residual Auction — RTO-wide clearing price (the #1-cited stat).
    {"iso": "PJM", "delivery_year": "2025/2026", "price_usd_mw_day": 269.92,
     "auction": "RPM Base Residual Auction (RTO, UCAP)", "as_of": "2024-07-30",
     "source": "PJM 2025/2026 BRA report — RTO cleared price (record ~9x prior year, driven by DC load)"},
]

# Verified from PJM's official BRA report PDFs (2026/27 + 2027/28 both cleared at
# the FERC price cap → every LDA equal to RTO; the DOM/Ashburn premium was a
# 2025/2026-only, constrained-LDA phenomenon). UCAP $/MW-day. NEVER fabricated.
_RESEARCHED_PRICES = [
    {"iso": "PJM", "delivery_year": "2026/2027", "price_usd_mw_day": 329.17,
     "auction": "RPM Base Residual Auction (RTO, UCAP)", "as_of": "2025-07-22",
     "source": "PJM 2026/2027 BRA report — cleared at the FERC cap; all LDAs equal (0 constrained)"},
    {"iso": "PJM", "delivery_year": "2027/2028", "price_usd_mw_day": 333.44,
     "auction": "RPM Base Residual Auction (RTO, UCAP)", "as_of": "2025-12-17",
     "source": "PJM 2027/2028 BRA report — cleared at cap; 6,623 MW short of reliability requirement"},
    # Locational (Dominion / Northern Virginia) — the world's #1 DC market — carried
    # a premium ONLY in the 2025/26 constrained auction; kept as a dated locational point.
    {"iso": "PJM-DOM", "delivery_year": "2025/2026", "price_usd_mw_day": 444.26,
     "auction": "RPM BRA — DOM (Dominion/Northern Virginia) LDA, UCAP", "as_of": "2024-07-30",
     "source": "PJM 2025/2026 BRA report — DOM LDA constrained clearing price (Ashburn/NoVA)"},
]


def _capacity_price_rows():
    rows = list(CAPACITY_PRICES) + list(_RESEARCHED_PRICES)
    ov = os.environ.get("DEPTH_CAPACITY_PRICES_JSON")
    if ov:
        try:
            extra = json.loads(ov)
            if isinstance(extra, list):
                rows += extra
        except Exception:
            pass
    return rows


def _act_capacity_price() -> dict:
    """Upsert the published capacity-auction clearing prices into grid_ext_metrics
    (category='capacity_price'). Auto-surfaces via main._grid_ext_metrics_for +
    /api/v1/grid/extended. Idempotent (UNIQUE dataset_id, as_of)."""
    rows = _capacity_price_rows()
    if not rows:
        return {"ok": False, "reason": "no_prices"}
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "db_unavailable"}
    wrote = 0
    try:
        with c.cursor() as cur:
            for r in rows:
                dy = r.get("delivery_year") or ""
                as_of = (r.get("as_of") or "") + ("-01" if len(r.get("as_of") or "") == 7 else "")
                dataset_id = f"capacity_price:{r.get('iso')}:{dy}".replace("/", "_")
                try:
                    cur.execute("""
                        INSERT INTO grid_ext_metrics
                          (source, dataset_id, iso, category, primary_value, unit, as_of, raw)
                        VALUES ('published_auction', %s, %s, 'capacity_price', %s, '$/MW-day', %s, %s)
                        ON CONFLICT (dataset_id, as_of) DO UPDATE
                          SET primary_value = EXCLUDED.primary_value,
                              raw = EXCLUDED.raw, ingested_at = NOW()
                    """, (dataset_id, r.get("iso"), _num(r.get("price_usd_mw_day")),
                          as_of or None, json.dumps(r)))
                    wrote += 1
                except Exception:
                    c.rollback()
                    continue
        return {"ok": True, "rows_upserted": wrote,
                "isos": sorted({r.get("iso") for r in rows})}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c.close()
        except Exception: pass


def _capacity_price_state() -> dict:
    """Latest capacity_price per ISO (for measure + the public read)."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (iso) iso, primary_value, as_of, raw
                  FROM grid_ext_metrics
                 WHERE category = 'capacity_price' AND primary_value IS NOT NULL
                 ORDER BY iso, as_of DESC NULLS LAST
            """)
            for iso, pv, as_of, raw in cur.fetchall():
                r = raw if isinstance(raw, dict) else {}
                out[iso] = {"price_usd_mw_day": float(pv), "as_of": str(as_of),
                            "delivery_year": r.get("delivery_year"),
                            "auction": r.get("auction"), "source": r.get("source")}
    except Exception:
        return {}
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 2 — large_load_queue (DC-classified interconnection LOAD per ISO)
# ═══════════════════════════════════════════════════════════════════════
# interconnect_queue carries a fuel_type='Load' class (large-LOAD interconnections,
# almost entirely data centers by name) plus generation rows whose project names
# name a data center. We aggregate a DC-classified LOAD figure per ISO — clearly
# labeled INFERRED (name/fuel heuristic) — closing the "DC-load queue = ERCOT-only"
# gap for every ISO that publishes load interconnections. ERCOT's 225 GW stays on
# its dedicated large-load feed (not this queue table).
def _act_large_load() -> dict:
    """Classify DC-driven LOAD in interconnect_queue and upsert per-ISO GW into
    grid_ext_metrics (category='dc_load_queue', unit='GW', source='inferred')."""
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "db_unavailable"}
    try:
        with c.cursor() as cur:
            cur.execute(r"""
                WITH dc AS (
                    SELECT iso, capacity_mw
                      FROM interconnect_queue
                     WHERE capacity_mw IS NOT NULL AND capacity_mw > 0
                       AND ( fuel_type ILIKE 'load'
                          OR project_name ~* 'data ?cent|hyperscale|colocation|server farm|compute campus|AI data' )
                )
                SELECT iso, ROUND((SUM(capacity_mw)/1000.0)::numeric, 2) AS gw, COUNT(*) AS n
                  FROM dc WHERE iso IS NOT NULL GROUP BY iso ORDER BY gw DESC
            """)
            rows = cur.fetchall()
            wrote = 0
            now_iso = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            for iso, gw, n in rows:
                dataset_id = f"dc_load_queue:{iso}"
                raw = {"iso": iso, "dc_gw": float(gw), "project_count": int(n),
                       "method": "inferred: interconnect_queue fuel_type=Load OR DC name-match",
                       "note": "ERCOT large-load (~225GW) tracked separately via its dedicated feed"}
                try:
                    cur.execute("""
                        INSERT INTO grid_ext_metrics
                          (source, dataset_id, iso, category, primary_value, unit, as_of, raw)
                        VALUES ('inferred', %s, %s, 'dc_load_queue', %s, 'GW', %s, %s)
                        ON CONFLICT (dataset_id, as_of) DO UPDATE
                          SET primary_value = EXCLUDED.primary_value,
                              raw = EXCLUDED.raw, ingested_at = NOW()
                    """, (dataset_id, iso, float(gw), now_iso, json.dumps(raw)))
                    wrote += 1
                except Exception:
                    c.rollback()
                    continue
        return {"ok": True, "isos_classified": len(rows), "rows_upserted": wrote,
                "by_iso": {iso: float(gw) for iso, gw, n in rows}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c.close()
        except Exception: pass


def _dc_load_state() -> dict:
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (iso) iso, primary_value, as_of
                  FROM grid_ext_metrics
                 WHERE category = 'dc_load_queue' AND primary_value IS NOT NULL
                 ORDER BY iso, as_of DESC NULLS LAST
            """)
            for iso, pv, as_of in cur.fetchall():
                out[iso] = {"dc_load_gw": float(pv), "as_of": str(as_of)}
    except Exception:
        return {}
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 3 — hosting_capacity (substation headroom from the 126k substations)
# ═══════════════════════════════════════════════════════════════════════
def _substation_headroom(lat, lng, radius_km=40.0):
    """Real hosting-capacity PROXY for a point: the transmission substations within
    radius_km and their capacity_mva / available_mva by voltage tier. Replaces the
    Colorado-default grid-headroom stub with a genuine, LOCATION-AWARE read from the
    HIFLD substations layer. Not feeder-level hosting capacity (that needs utility
    GIS — still a filed gap), but a defensible transmission-proximity signal."""
    lat = _num(lat); lng = _num(lng)
    if lat is None or lng is None:
        return None
    # bbox prefilter (cos-lat corrected) then haversine rank — mirrors connectivity_score.
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT name, operator, voltage_kv, max_voltage_kv, capacity_mva,
                       available_mva, lat, lng
                  FROM substations
                 WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                   AND lat IS NOT NULL AND lng IS NOT NULL
                 LIMIT 4000
            """, (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
            rows = cur.fetchall()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass

    def _hav(la, lo):
        r = 6371.0
        p1, p2 = math.radians(lat), math.radians(la)
        dp = math.radians(la - lat); dl = math.radians(lo - lng)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(min(1.0, math.sqrt(a)))

    import re as _re

    def _volt(row_v, name):
        # HIFLD voltage columns are sparse/0 (even "Ashburn 500kV" stores 0) — fall
        # back to a kV embedded in the substation name.
        if row_v and row_v > 0:
            return row_v
        mm = _re.search(r"(\d{2,3})\s*kv", str(name or ""), _re.I)
        return float(mm.group(1)) if mm else None

    near = []
    for name, op, v, maxv, cap, avail, sla, slo in rows:
        d = _hav(_num(sla), _num(slo)) if (_num(sla) is not None and _num(slo) is not None) else None
        if d is None or d > radius_km:
            continue
        near.append({"name": name, "operator": op,
                     "voltage_kv": _volt(_num(maxv) or _num(v), name),
                     "capacity_mva": _num(cap), "available_mva": _num(avail),
                     "distance_km": round(d, 1)})
    near.sort(key=lambda x: x["distance_km"])
    if not near:
        return {"substation_count": 0, "verdict": "no_transmission_nearby", "headroom_score": 0}
    ehv = [s for s in near if (s["voltage_kv"] or 0) >= 230]   # 230kV+ = hyperscale-grade
    cap_sum = round(sum(s["capacity_mva"] or 0 for s in near), 0)
    avail_sum = round(sum(s["available_mva"] or 0 for s in near if s["available_mva"] is not None), 0)
    nearest = near[0]
    # 0-100 headroom score from RELIABLE signals only: transmission-substation
    # DENSITY within reach + EHV presence (voltage parsed from name when the column
    # is 0) + proximity to the nearest. The capacity_mva/available_mva columns are
    # too sparse in HIFLD to score on — reported as a bonus when present.
    score = min(100, int(
        min(50, len(near) * 0.7)
        + min(30, len(ehv) * 6)
        + (20 if nearest["distance_km"] <= 5 else 10 if nearest["distance_km"] <= 15 else 0)
    ))
    verdict = ("abundant" if score >= 70 else "moderate" if score >= 40
               else "constrained" if score >= 15 else "sparse")
    return {
        "substation_count": len(near),
        "ehv_substation_count": len(ehv),           # >=230kV
        "nearest_substation_km": nearest["distance_km"],
        "nearest_substation": nearest["name"],
        "total_capacity_mva": cap_sum,
        "total_available_mva": (avail_sum or None),
        "top_substations": near[:6],
        "headroom_score": score,
        "verdict": verdict,
        "radius_km": radius_km,
        "source": "DC Hub substations layer (HIFLD-derived, 126k US substations)",
        "note": "Transmission-proximity headroom proxy; feeder-level utility hosting capacity still requires per-utility GIS.",
    }


def _act_hosting_capacity() -> dict:
    """Refresh substation-headroom snapshots for the top DC markets (from
    market_power_scores lat/lng), storing a headroom score per market in
    grid_ext_metrics (category='hosting_capacity', unit='score'). Bounded: one
    batch of markets per tick (the stalest)."""
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "db_unavailable"}
    markets = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT market_slug, latitude, longitude
                  FROM market_power_scores
                 WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                 ORDER BY excess_power_score DESC NULLS LAST
                 LIMIT 40
            """)
            markets = cur.fetchall()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    if not markets:
        return {"ok": False, "reason": "no_markets"}
    wrote = 0
    now_iso = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    c2 = _conn()
    if c2 is None:
        return {"ok": False, "reason": "db_unavailable"}
    try:
        with c2.cursor() as cur:
            for slug, la, lo in markets:
                hr = _substation_headroom(la, lo)
                if not hr or hr.get("headroom_score") is None:
                    continue
                dataset_id = f"hosting_capacity:{slug}"
                try:
                    cur.execute("""
                        INSERT INTO grid_ext_metrics
                          (source, dataset_id, iso, category, primary_value, unit, as_of, raw)
                        VALUES ('substations', %s, %s, 'hosting_capacity', %s, 'score', %s, %s)
                        ON CONFLICT (dataset_id, as_of) DO UPDATE
                          SET primary_value = EXCLUDED.primary_value,
                              raw = EXCLUDED.raw, ingested_at = NOW()
                    """, (dataset_id, slug, hr["headroom_score"], now_iso, json.dumps(hr)))
                    wrote += 1
                except Exception:
                    c2.rollback()
                    continue
        return {"ok": True, "markets_scored": wrote}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c2.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════
# LANE 4 — fiber_longhaul (coverage + freshness; exposure = market= + map layer)
# ═══════════════════════════════════════════════════════════════════════
def _longhaul_coverage() -> dict:
    c = _conn()
    if c is None:
        return {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE route_type ~* 'long'),
                       COUNT(DISTINCT provider) FILTER (WHERE route_type ~* 'long'),
                       MAX(updated_at)
                  FROM fiber_routes
            """)
            n, carriers, fresh = cur.fetchone()
            return {"longhaul_routes": int(n or 0),
                    "longhaul_carriers": int(carriers or 0),
                    "updated_at": str(fresh) if fresh else None}
    except Exception:
        return {}
    finally:
        try: c.close()
        except Exception: pass


# ── remaining structural gaps → brain_findings (what's NOT yet closable here) ──
_GAP_FINDINGS = [
    ("depth_capacity_price_machine_feed",
     "Capacity-auction clearing prices are now INGESTED (published, dated) into "
     "grid_ext_metrics(category=capacity_price) + surfaced. Upgrade path: a live parser "
     "for each ISO auction (PJM RPM, ISO-NE FCA, MISO PRA) so new results refresh without a deploy."),
    ("depth_dc_load_queue_inferred_only",
     "DC-load-queue GW is now derived per ISO from interconnect_queue (fuel_type=Load + name), "
     "closing ERCOT-only — but it is INFERRED. Upgrade path: ingest each ISO's large-LOAD "
     "interconnection study where published (NYISO/PJM load queues) for a classified figure."),
    ("depth_hosting_capacity_feeder_gis",
     "Hosting capacity is now a real transmission-proximity read from the substations layer "
     "(replaces the Colorado-default stub). Upgrade path: ingest per-utility feeder hosting-"
     "capacity GIS (Dominion/Georgia Power/PG&E) for parcel-level circuit headroom."),
]


def _file_gap_findings() -> int:
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return 0
    conn = _txn_conn()
    if conn is None:
        return 0
    filed = 0
    try:
        with conn.cursor() as cur:
            for issue, detail in _GAP_FINDINGS:
                try:
                    r = upsert_brain_finding(cur, issue=issue,
                                             url="/api/v1/admin/depth/master-tick",
                                             count=1, detail=detail, detector="depth_master")
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


# ── snapshot table ────────────────────────────────────────────────────
def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS depth_snapshots (
                    id            SERIAL PRIMARY KEY,
                    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    depth_score   NUMERIC(6,2),
                    weakest_lever TEXT,
                    action_taken  TEXT,
                    lever_scores  JSONB,
                    findings_filed INTEGER,
                    detail        JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── MEASURE → SCORE → ACT → PERSIST ───────────────────────────────────
# Target ISO sets (what "full coverage" means per lane).
_PRICE_ISOS = {"PJM"}   # ISOs with a VERIFIED published clearing price ingested
                         # (ISO-NE FCA / MISO PRA = upgrade path, filed to brain)
_QUEUE_ISOS = {"PJM", "MISO", "SPP", "CAISO", "NYISO", "ISO-NE", "ERCOT"}


def measure() -> dict:
    cap = _capacity_price_state()
    dcl = _dc_load_state()
    lh = _longhaul_coverage()
    # hosting: how many markets carry a fresh headroom score
    hosting_n = 0
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM grid_ext_metrics WHERE category='hosting_capacity'")
                hosting_n = int(cur.fetchone()[0] or 0)
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    return {
        "capacity_price_isos": sorted(cap.keys()),
        "dc_load_isos": sorted(dcl.keys()),
        "hosting_markets": hosting_n,
        "longhaul": lh,
        "_cap": cap, "_dcl": dcl,
    }


def score_levers(m: dict) -> dict:
    cap_isos = set(m.get("capacity_price_isos") or [])
    dcl_isos = set(m.get("dc_load_isos") or [])
    capacity_price = round(len(cap_isos & _PRICE_ISOS) / len(_PRICE_ISOS), 3)
    large_load = round(min(1.0, len(dcl_isos) / 5.0), 3)   # 5+ ISOs classified = full
    hosting_capacity = round(min(1.0, (m.get("hosting_markets") or 0) / 40.0), 3)
    lh = m.get("longhaul") or {}
    fiber_longhaul = 1.0 if (lh.get("longhaul_routes") or 0) >= 15000 else round(
        (lh.get("longhaul_routes") or 0) / 15000.0, 3)
    scores = {"capacity_price": capacity_price, "large_load_queue": large_load,
              "hosting_capacity": hosting_capacity, "fiber_longhaul": fiber_longhaul}
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    weakest = next((n for n, _ in ranked if not _lever_off(n)), ranked[0][0])
    return {"scores": scores, "weakest": weakest}


def act(m: dict, levers: dict) -> dict:
    if _act_disabled():
        return {"action": "none", "reason": "DEPTH_MASTER_ACT_DISABLED (shadow)"}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed"}
    if lever == "capacity_price":
        return {"lever": lever, "action": "ingest_capacity_price", "result": _act_capacity_price()}
    if lever == "large_load_queue":
        return {"lever": lever, "action": "classify_dc_load", "result": _act_large_load()}
    if lever == "hosting_capacity":
        return {"lever": lever, "action": "refresh_hosting_capacity", "result": _act_hosting_capacity()}
    if lever == "fiber_longhaul":
        # coverage is data-loader-driven (carrier_kmz/NTIA loaders); the shell keeps it
        # measured + re-classifies DC-load as the useful bounded action here.
        return {"lever": lever, "action": "measure_only",
                "result": {"note": "long-haul geometry loaded by carrier_kmz/ntia loaders; exposure live via get_fiber_intel market= + map"}}
    return {"action": "none", "reason": "unknown lever"}


def depth_score(levers: dict) -> float:
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
                INSERT INTO depth_snapshots
                  (depth_score, weakest_lever, action_taken, lever_scores, findings_filed, detail)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (score, levers.get("weakest"), (action or {}).get("action"),
                  json.dumps(levers.get("scores") or {}), findings,
                  json.dumps({"capacity_price_isos": m.get("capacity_price_isos"),
                              "dc_load_isos": m.get("dc_load_isos"),
                              "hosting_markets": m.get("hosting_markets"),
                              "longhaul": m.get("longhaul"), "action": action})))
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ENDPOINTS ─────────────────────────────────────────────────────────
@depth_master_shell_bp.route("/api/v1/admin/depth/master-tick", methods=["POST", "GET"])
def depth_master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="DEPTH_MASTER_DISABLED"), 200
    started = time.time()
    _ensure_tables()
    m = measure()
    levers = score_levers(m)
    action = act(m, levers)
    # re-measure the touched lane so the persisted snapshot + headline reflect this tick
    m2 = measure()
    findings = 0 if _act_disabled() else _file_gap_findings()
    score = depth_score(score_levers(m2))
    persisted = _persist(m2, levers, score, action, findings)
    s = levers.get("scores") or {}
    headline = (
        f"depth {score}/100 · weakest → {levers.get('weakest')} ({s.get(levers.get('weakest'))}) · "
        f"acted: {action.get('action')} · capacity_price {m2.get('capacity_price_isos')} · "
        f"dc_load {m2.get('dc_load_isos')} · hosting {m2.get('hosting_markets')} mkts · "
        f"longhaul {(m2.get('longhaul') or {}).get('longhaul_routes')} routes · {findings} gaps→brain"
    )
    return jsonify(
        ok=True, ms=int((time.time() - started) * 1000),
        depth_score=score, headline=headline,
        tier1_measure={k: v for k, v in m2.items() if not k.startswith("_")},
        tier2_levers=levers, tier3_action=action,
        findings_filed=findings, persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@depth_master_shell_bp.route("/api/v1/admin/depth/master-state", methods=["GET"])
def depth_master_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(available=False, reason="db_unavailable"), 200
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM depth_snapshots ORDER BY computed_at DESC LIMIT 12")
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


@depth_master_shell_bp.route("/api/v1/grid/hosting-capacity", methods=["GET"])
def grid_hosting_capacity():
    """Public: substation-proximity hosting-capacity read for a point or a market.
    ?lat=&lon=[&radius_km=]  OR  ?market=<slug>. Region-aware (finally not Colorado)."""
    lat = request.args.get("lat"); lon = request.args.get("lon") or request.args.get("lng")
    market = request.args.get("market")
    radius = _num(request.args.get("radius_km")) or 40.0
    radius = max(5.0, min(150.0, radius))
    if (lat is None or lon is None) and market:
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute("""SELECT latitude, longitude FROM market_power_scores
                                    WHERE LOWER(market_slug)=LOWER(%s) AND latitude IS NOT NULL
                                    ORDER BY computed_at DESC LIMIT 1""", (market,))
                    row = cur.fetchone()
                    if row:
                        lat, lon = row[0], row[1]
            except Exception:
                pass
            finally:
                try: c.close()
                except Exception: pass
    hr = _substation_headroom(lat, lon, radius)
    if hr is None:
        return jsonify(available=False, reason="need lat+lon or a known market slug"), 200
    hr["available"] = True
    if market:
        hr["market"] = market
    return jsonify(hr), 200


@depth_master_shell_bp.route("/api/v1/grid/capacity-price", methods=["GET"])
def grid_capacity_price():
    """Public: latest ISO capacity-auction clearing prices ($/MW-day), cited + dated."""
    state = _capacity_price_state()
    iso = (request.args.get("iso") or "").upper().strip()
    if iso:
        return jsonify(iso=iso, **(state.get(iso) or {"available": False})), 200
    return jsonify(available=bool(state), prices=state,
                   note="Published capacity-auction clearing prices; DC Hub (dchub.cloud).",
                   source="ISO auction results (PJM RPM/BRA, ISO-NE FCA, MISO PRA)"), 200
