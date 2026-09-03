"""
grid_data_master_shell.py — the self-driving GRID / POWER / GAS aggregation
orchestrator (2026-07-03).

Sits in the house master-shell family (mirrors growth_master_shell exactly:
admin-gated, killable, loopback self-calls, a snapshot table, tier1..3, 0-100
score, one bounded action/tick, fail-soft). Its job is to keep DC Hub's energy
data layer AHEAD OF THE CURVE — constantly widening + freshening coverage rather
than sitting on a static set of feeds.

Why this exists: get_grid_intelligence is the #1 demanded tool, yet we ingest
~a-handful of the 523 datasets our gridstatus.io key already unlocks, and several
high-value fields are structurally empty (reserve_margin, DC-load queue outside
ERCOT, capacity-auction price, feeder hosting-capacity). This shell closes that
gap in two ways every tick:

  A) MECHANICAL (armed) — a registry-driven GENERIC gridstatus ingester. Adding a
     new source = adding a row to TARGET_DATASETS (config), not writing code. Each
     tick absorbs the next untapped high-value dataset into a unified
     `grid_ext_metrics` table (reserves, capacity, load-forecast, marginal
     emissions, richer fuel mix), then maintains freshness once all are tapped.
     Purely additive — a new table, never touches the existing ISO pipelines.

  B) CODE-SHAPED (delegated) — the gaps that need real new adapters (PJM RPM
     capacity-auction clearing price, utility feeder hosting-capacity, ISO-NE
     real-time LMP, the grid_telemetry ERCOT/PJM/ISONE credential stubs) are
     filed as brain_findings so the brain's autonomy loop drafts them. Agentic,
     not static.

The four levers (weakest → one bounded action/tick):
  1. FRESHNESS  — are our core ISO / LMP / queue feeds serving fresh data?
  2. BREADTH    — how much of the target dataset registry have we absorbed?
  3. DEPTH      — do we carry the structurally-missing signals (reserves,
                  capacity, marginal emissions)?
  4. FORECAST   — do we carry forward-looking load across the ISOs?

Endpoints:
  POST /api/v1/admin/grid-data/master-tick   — measure → score → act → persist
  GET  /api/v1/admin/grid-data/master-state  — latest snapshot + trend

Kill switches: GRID_DATA_MASTER_DISABLED=1 (whole tick),
GRID_DATA_MASTER_ACT_DISABLED=1 (shadow: measure+persist, no ingest/no findings),
GRID_DATA_LEVER_<NAME>_OFF=1 (per-lever).
"""
import os
import json
import time
import hmac
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

import gridstatus_client as _gsc  # THE one budget-ledgered gridstatus.io client

grid_data_master_shell_bp = Blueprint("grid_data_master_shell", __name__)

# Loopback on Railway (mirrors growth_master_shell / cron_heartbeat.BASE).
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)


# ── auth (mirrors growth_master_shell) ────────────────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("GRID_DATA_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    """Shadow mode: measure + persist but take NO action (no ingest, no findings)."""
    return str(os.environ.get("GRID_DATA_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    return str(os.environ.get(f"GRID_DATA_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── self-call + parse helpers ─────────────────────────────────────────
def _req(path: str, method: str = "GET", timeout: int = 8) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "grid-data-tick")  # rate-limiter bypass
        req.add_header("User-Agent", "dchub-grid-data-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_raw": body[:300]}
            return {"ok": True, "http": resp.status, "data": parsed}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _fire(path: str, timeout: int = 4) -> dict:
    """Trigger a downstream action without blocking on completion."""
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "grid-data-tick")
        req.add_header("User-Agent", "dchub-grid-data-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": resp.status < 400, "http": resp.status, "dispatched": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "dispatched": True}
    except Exception:
        return {"ok": True, "dispatched": True, "note": "not awaited"}


def _num(v):
    # preserve numeric type as-is — int(3.45) would SILENTLY truncate a float
    # price ($/MMBtu, $/MWh, emissions intensity). Only stringy values get coerced.
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── gridstatus.io generic client ─────────────────────────────────────
def _gs_get(dataset: str, params: dict | None = None, timeout: int = 15):
    """GET one gridstatus dataset query via THE budget-ledgered client
    (gridstatus_client). This shell's own unledgered copy of the GET burned
    ~330 of July-2026's 375 provider calls (250 free-tier cap) — every call
    now spends the shared gridstatus_call_ledger budget first and comes back
    with a loud machine-readable error ("budget_exhausted: ...", "http_403")
    when refused. Returns (rows, err)."""
    return _gsc.gridstatus_get(dataset, params, timeout=timeout,
                               caller="grid_data_master_shell")


# ── the target-dataset REGISTRY (config-driven expansion) ─────────────
# Each entry: id (gridstatus dataset), iso, cat, value_col (primary numeric;
# best-effort fallback picks the first non-timestamp numeric), unit. Absorbing a
# new source = appending a row here — no new code. Ordered rough-priority.
TARGET_DATASETS = [
    # ── GRID (market / ops signals) ──────────────────────────────────
    # forward load per ISO (the "stay ahead" signal)
    {"id": "pjm_load_forecast",    "iso": "PJM",   "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    {"id": "ercot_load_forecast",  "iso": "ERCOT", "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    {"id": "caiso_load_forecast",  "iso": "CAISO", "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    {"id": "miso_load_forecast",   "iso": "MISO",  "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    {"id": "nyiso_load_forecast",  "iso": "NYISO", "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    {"id": "spp_load_forecast",    "iso": "SPP",   "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    # 2026-07-11 expansion: ISO-NE forward load — completes load_forecast for all
    # 7 US ISOs (was 6/7; ISONE previously carried only the real-time LMP row).
    {"id": "isone_load_forecast",  "iso": "ISONE", "dom": "grid", "cat": "load_forecast", "value_col": "load_forecast", "unit": "MW"},
    # ISO-NE real-time LMP — CLOSES the "ISO-NE LMP registration-gated" gap
    {"id": "isone_lmp_real_time_5_min", "iso": "ISONE", "dom": "grid", "cat": "lmp", "value_col": "lmp", "unit": "$/MWh"},
    # operating reserves + forward operating margin (toward the never-live reserve_margin)
    {"id": "pjm_dispatched_reserves_verified",     "iso": "PJM",   "dom": "grid", "cat": "reserves", "value_col": "total_reserve", "unit": "MW"},
    {"id": "ercot_real_time_adders_and_reserves",  "iso": "ERCOT", "dom": "grid", "cat": "reserves", "value_col": "prc", "unit": "MW"},
    {"id": "aeso_reserves",                        "iso": "AESO",  "dom": "grid", "cat": "reserves", "value_col": "dispatched_contingency_reserve_total", "unit": "MW"},
    {"id": "miso_multiday_operating_margin",       "iso": "MISO",  "dom": "grid", "cat": "margin",   "value_col": "resource_operating_margin", "unit": "MW"},
    # grid carbon intensity (the Electricity-Maps lane): marginal + consumed
    {"id": "pjm_marginal_emission_rates_5_min",  "iso": "PJM", "dom": "grid", "cat": "emissions", "value_col": "marginal_co2_rate", "unit": "lb/MWh"},
    {"id": "eia_co2_emissions",                  "iso": "US",  "dom": "grid", "cat": "emissions", "value_col": "co2_emissions_intensity_for_consumed_electricity", "unit": "lb/MWh"},
    # ── POWER (generation supply) ────────────────────────────────────
    {"id": "pjm_generation_capacity_daily", "iso": "PJM",   "dom": "power", "cat": "capacity", "value_col": "total_committed_mw", "unit": "MW"},
    {"id": "ercot_capacity_committed",      "iso": "ERCOT", "dom": "power", "cat": "capacity", "value_col": None, "unit": "MW"},
    {"id": "ercot_capacity_forecast",       "iso": "ERCOT", "dom": "power", "cat": "capacity", "value_col": None, "unit": "MW"},
    {"id": "pjm_fuel_mix",           "iso": "PJM",   "dom": "power", "cat": "fuel_mix", "value_col": None, "unit": "MW"},
    {"id": "caiso_fuel_mix",         "iso": "CAISO", "dom": "power", "cat": "fuel_mix", "value_col": None, "unit": "MW"},
    {"id": "ercot_fuel_mix_detailed","iso": "ERCOT", "dom": "power", "cat": "fuel_mix", "value_col": None, "unit": "MW"},
    # ── GAS ──────────────────────────────────────────────────────────
    {"id": "eia_henry_hub_natural_gas_spot_prices_daily", "iso": "US", "dom": "gas", "cat": "gas_price", "value_col": "price", "unit": "$/MMBtu"},
]
_REGISTRY_IDS = {t["id"] for t in TARGET_DATASETS}
_LOAD_FC_ISOS = {t["iso"] for t in TARGET_DATASETS if t["cat"] == "load_forecast"}
# depth = the structurally-missing high-value signals the weakest-depth lever pulls first
_DEPTH_CATS = ("lmp", "reserves", "margin", "capacity", "emissions", "gas_price")
_DOMAINS = ("grid", "power", "gas")
_DATASET_DOM = {t["id"]: t.get("dom") for t in TARGET_DATASETS}
_DOMAIN_TARGETS = {d: sum(1 for t in TARGET_DATASETS if t.get("dom") == d) for d in _DOMAINS}

# Capacity-MARKET clearing price ($/MW-day) — the #1-cited DC-power economic signal,
# which has NO machine-readable feed (published in ISO auction-result PDFs). Cited
# seed with provenance + delivery year (same honest pattern as ERCOT large-load).
# Update per annual auction; a live parser is the filed brain gap. Env override:
# GRID_CAPACITY_AUCTION_JSON (JSON {ISO:{...}}) so it can be refreshed without deploy.
CAPACITY_AUCTION = {
    "PJM": {"price_usd_mw_day": 269.92, "delivery_year": "2025/2026",
            "auction": "RPM Base Residual Auction",
            "source": "PJM 2025/2026 BRA cleared price (record, ~8x the prior year — driven by data-center load)",
            "as_of": "2024-07"},
}


def _capacity_auction(iso):
    """Cited capacity-auction clearing price for an ISO, env-overridable. Returns
    {} if none. NEVER fabricates — only ISOs with a published, dated figure."""
    data = dict(CAPACITY_AUCTION)
    ov = os.environ.get("GRID_CAPACITY_AUCTION_JSON")
    if ov:
        try:
            data.update(json.loads(ov))
        except Exception:
            pass
    return data.get(iso, {})


# ── DB ────────────────────────────────────────────────────────────────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS grid_data_snapshots (
                    id             SERIAL PRIMARY KEY,
                    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    grid_data_score NUMERIC(6,2),
                    breadth_tapped INTEGER,
                    breadth_target INTEGER,
                    weakest_lever  TEXT,
                    action_taken   TEXT,
                    lever_scores   JSONB,
                    findings_filed INTEGER,
                    detail         JSONB
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS grid_ext_metrics (
                    id           SERIAL PRIMARY KEY,
                    source       TEXT NOT NULL DEFAULT 'gridstatus',
                    dataset_id   TEXT NOT NULL,
                    iso          TEXT,
                    category     TEXT,
                    primary_value NUMERIC,
                    unit         TEXT,
                    as_of        TIMESTAMPTZ,
                    raw          JSONB,
                    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (dataset_id, as_of)
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _ingested_state() -> dict:
    """Return {dataset_id: {'as_of': ts, 'ingested_at': ts, 'iso': .., 'category': ..}}
    for what we already carry in grid_ext_metrics (latest per dataset)."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT dataset_id, iso, category, MAX(as_of) AS latest,
                       MAX(ingested_at) AS last_ingested
                FROM grid_ext_metrics GROUP BY dataset_id, iso, category
            """)
            for did, iso, cat, latest, last_ing in cur.fetchall():
                out[did] = {"iso": iso, "category": cat, "as_of": latest,
                            "ingested_at": last_ing}
    except Exception:
        return {}
    finally:
        try: c.close()
        except Exception: pass
    return out


_GS_ALLOWLIST = set((os.environ.get(
    "GRIDSTATUS_DATASET_ALLOWLIST",
    "pjm_load,pjm_fuel_mix,pjm_load_forecast,pjm_generation_capacity_daily"
)).split(","))


def _ingest_gridstatus_dataset(entry: dict) -> dict:
    """Pull the latest row of one gridstatus dataset into grid_ext_metrics.
    Column-agnostic + fail-soft: always captures the raw row; primary_value is
    best-effort (entry.value_col, else first non-timestamp numeric).

    shell#35 WS8 (2026-07-26): gridstatus free tier = 250 req/MONTH and July
    burned 375, mostly here (~20 datasets × every-other-day). ALLOWLIST =
    PJM-only (no free direct source without a DM2 redistribution license);
    everything else (ercot_*/caiso_*/isone_*/spp_*/miso_*/aeso_*/eia_*) has a
    free direct source we already hold creds for — skipped with an honest
    marker so the brain can repoint them (finding filed by the caller)."""
    if entry["id"] not in _GS_ALLOWLIST:
        return {"ok": False, "dataset": entry["id"],
                "error": "skipped_budget_allowlist_repoint_to_free_source"}
    rows, err = _gs_get(entry["id"], {"limit": 1, "order": "desc"})
    if err or not rows:
        return {"ok": False, "dataset": entry["id"], "error": err or "no_rows"}
    row = rows[0]
    as_of = (row.get("interval_start_utc") or row.get("publish_time_utc")
             or row.get("interval_end_utc"))
    pv = _num(row.get(entry.get("value_col"))) if entry.get("value_col") else None
    if pv is None:
        for k, v in row.items():
            if str(k).endswith("_utc"):
                continue
            n = _num(v)
            if n is not None:
                pv = n
                break
    c = _conn()
    if c is None:
        return {"ok": False, "dataset": entry["id"], "error": "db_unavailable"}
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO grid_ext_metrics
                  (source, dataset_id, iso, category, primary_value, unit, as_of, raw)
                VALUES ('gridstatus', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (dataset_id, as_of) DO UPDATE
                  SET primary_value = EXCLUDED.primary_value,
                      raw = EXCLUDED.raw, ingested_at = NOW()
            """, (entry["id"], entry.get("iso"), entry.get("cat"), pv,
                  entry.get("unit"), as_of, json.dumps(row)))
        return {"ok": True, "dataset": entry["id"], "iso": entry.get("iso"),
                "category": entry.get("cat"), "primary_value": pv, "as_of": str(as_of)}
    except Exception as e:
        return {"ok": False, "dataset": entry["id"], "error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        try: c.close()
        except Exception: pass


# ── DIRECT SOURCES — the repoint the 07-26 budget cut asked for ─────────
#
# ★★★ 2026-09-03. The allowlist cut was right (gridstatus free tier = 250
#   req/MONTH, July burned 375) and its note said the parked datasets each
#   "[have] a free direct source we already hold creds for". This is the first
#   of those repoints, and it costs ZERO new upstream calls: CAISO's Today's
#   Outlook CSVs are public, unauthenticated, and iso_grid_adapters.fetch_caiso
#   ALREADY downloads both of them every ISO pull.
#
#     demand.csv      Time, Day ahead forecast, Hour ahead forecast,
#                     Current demand, Demand response      -> caiso_load_forecast
#     fuelsource.csv  Time + 13 fuel columns               -> caiso_fuel_mix
#
#   Verified live 2026-09-03: day-ahead 26,792 MW / hour-ahead 26,355 MW, and a
#   13-column fuel row. Nothing here needs a key, a budget or a new integration
#   — the data was already on the wire and being thrown away.
#
# ★ PROVENANCE IS NOT COSMETIC. Rows land with source='caiso_todays_outlook',
#   never the table's 'gridstatus' default. A row that names the wrong upstream
#   is how a feed gets "repointed" on paper and audited as still-gridstatus.
_CAISO_BASE = "https://www.caiso.com/outlook/current"
_CAISO_TZ = "America/Los_Angeles"


def _http_csv(url: str, timeout: int = 8):
    """Fetch a CSV as a list of dicts. [] on anything unexpected (fail-soft)."""
    import csv as _csv
    import io as _io
    import urllib.request as _u
    try:
        req = _u.Request(url, headers={"User-Agent": "dchub-grid-shell/1.0"})
        with _u.urlopen(req, timeout=timeout) as r:
            body = r.read(400_000).decode("utf-8", "replace")
        return list(_csv.DictReader(_io.StringIO(body)))
    except Exception:
        return []


def _caiso_asof(hhmm: str, now=None):
    """A CAISO 'HH:MM' Pacific slot -> an aware UTC datetime, or None.

    The CSVs carry a time with NO DATE, so the date has to come from the clock.
    Anchoring on Pacific (not the server's zone) is load-bearing: this repo runs
    UTC in prod and UTC-7 locally, and a naive read would place every row up to
    seven hours off and silently write a row that dedups against the wrong slot.

    The date is simply TODAY in Pacific, with no adjustment. These are the
    "Today's Outlook" CSVs: one Pacific day per file, 00:00-23:55, reset at
    local midnight — so a row is never yesterday's.

    ★ An earlier draft subtracted a day from any slot more than 6h ahead,
      reading a future slot as a midnight rollover. That is wrong for THIS
      file: it legitimately carries forecast rows up to ~22h ahead, so the rule
      moved every afternoon slot back a day. The tests caught it. Future slots
      are handled where they belong — the load-forecast picker takes the newest
      slot at or before now, so a forecast for 14:00 is simply not chosen at
      02:05.
    """
    try:
        from datetime import datetime, timezone as _tz
        from zoneinfo import ZoneInfo
    except Exception:
        return None
    try:
        h, m = (int(x) for x in str(hhmm).strip().split(":")[:2])
    except Exception:
        return None
    try:
        pt = ZoneInfo(_CAISO_TZ)
        now_pt = (now or datetime.now(_tz.utc)).astimezone(pt)
        stamp = now_pt.replace(hour=h, minute=m, second=0, microsecond=0)
        return stamp.astimezone(_tz.utc)
    except Exception:
        return None


def _caiso_rows(name: str) -> list:
    return [r for r in _http_csv("%s/%s" % (_CAISO_BASE, name))
            if (r.get("Time") or "").strip()]


def _caiso_load_forecast(entry: dict) -> dict:
    """CAISO load forecast from the demand CSV we already pull.

    Primary is the DAY-AHEAD figure — the canonical 'load forecast' and the one
    that exists for every slot; the hour-ahead value rides in `raw` rather than
    being averaged into it, because two forecasts are two facts.
    """
    rows = _caiso_rows("demand.csv")
    if not rows:
        return {"ok": False, "error": "caiso_demand_csv_unavailable"}
    # The file spans a whole Pacific day, so its later rows are FUTURE slots
    # that already carry a forecast. Take the newest slot at or before now —
    # picking the last row outright would stamp a row hours ahead of the clock
    # and dedup against a slot that has not happened.
    now = _utcnow()
    dated = [(r, _caiso_asof(r.get("Time"))) for r in rows
             if _num(r.get("Day ahead forecast")) is not None]
    past = [(r, t) for r, t in dated if t is not None and t <= now]
    if not past:
        return {"ok": False, "error": "caiso_demand_csv_no_forecast_at_or_before_now"}
    picked, _ = max(past, key=lambda rt: rt[1])
    da = _num(picked.get("Day ahead forecast"))
    ha = _num(picked.get("Hour ahead forecast"))
    as_of = _caiso_asof(picked.get("Time"))
    if da is None or as_of is None:
        return {"ok": False, "error": "caiso_demand_row_unparseable"}
    return {"ok": True, "primary_value": da, "as_of": as_of,
            "raw": {"time_pt": picked.get("Time"),
                    "day_ahead_forecast_mw": da,
                    "hour_ahead_forecast_mw": ha,
                    "current_demand_mw": _num(picked.get("Current demand")),
                    "source_url": "%s/demand.csv" % _CAISO_BASE}}


def _caiso_fuel_mix(entry: dict) -> dict:
    """CAISO fuel mix from the fuelsource CSV we already pull.

    Primary is TOTAL generation across fuels — the one number the category is
    about; the per-fuel breakdown rides in `raw`. Negative values are kept as
    reported (solar goes negative at night on this feed) rather than clamped:
    a clamp would quietly inflate the total.
    """
    rows = _caiso_rows("fuelsource.csv")
    if not rows:
        return {"ok": False, "error": "caiso_fuelsource_csv_unavailable"}
    picked = None
    for r in rows:
        if any(_num(v) is not None for k, v in r.items() if k != "Time"):
            picked = r
    if picked is None:
        return {"ok": False, "error": "caiso_fuelsource_csv_no_numeric_row"}
    mix, total = {}, 0.0
    for k, v in picked.items():
        if k == "Time":
            continue
        n = _num(v)
        if n is not None:
            mix[k] = n
            total += n
    as_of = _caiso_asof(picked.get("Time"))
    if not mix or as_of is None:
        return {"ok": False, "error": "caiso_fuelsource_row_unparseable"}
    return {"ok": True, "primary_value": round(total, 1), "as_of": as_of,
            "raw": {"time_pt": picked.get("Time"), "fuel_mw": mix,
                    "total_generation_mw": round(total, 1),
                    "source_url": "%s/fuelsource.csv" % _CAISO_BASE}}


# dataset_id -> (source_label, fetcher). A dataset listed here is NO LONGER
# parked: parked_datasets() subtracts it, so the standing finding shrinks by
# arithmetic as repoints land rather than by anyone remembering to edit it.
_DIRECT_SOURCES = {
    "caiso_load_forecast": ("caiso_todays_outlook", _caiso_load_forecast),
    "caiso_fuel_mix":      ("caiso_todays_outlook", _caiso_fuel_mix),
}


def _utcnow():
    from datetime import datetime, timezone as _tz
    return datetime.now(_tz.utc)


def _ingest_direct(entry: dict) -> dict:
    """Ingest one dataset from its free direct source into grid_ext_metrics."""
    label, fetch = _DIRECT_SOURCES[entry["id"]]
    try:
        got = fetch(entry)
    except Exception as e:  # a bad fetcher must never break the tick
        return {"ok": False, "dataset": entry["id"],
                "error": "direct_fetch_raised:%s" % type(e).__name__}
    if not got.get("ok"):
        return {"ok": False, "dataset": entry["id"],
                "error": got.get("error") or "direct_fetch_failed"}
    c = _conn()
    if c is None:
        return {"ok": False, "dataset": entry["id"], "error": "db_unavailable"}
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO grid_ext_metrics
                  (source, dataset_id, iso, category, primary_value, unit, as_of, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (dataset_id, as_of) DO UPDATE
                  SET primary_value = EXCLUDED.primary_value,
                      source = EXCLUDED.source,
                      raw = EXCLUDED.raw, ingested_at = NOW()
            """, (label, entry["id"], entry.get("iso"), entry.get("cat"),
                  got["primary_value"], entry.get("unit"), got["as_of"],
                  json.dumps(got.get("raw") or {})))
        return {"ok": True, "dataset": entry["id"], "iso": entry.get("iso"),
                "category": entry.get("cat"), "source": label,
                "primary_value": got["primary_value"], "as_of": str(got["as_of"])}
    except Exception as e:
        return {"ok": False, "dataset": entry["id"],
                "error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    finally:
        try: c.close()
        except Exception: pass


def _ingest_dataset(entry: dict) -> dict:
    """Route a dataset to its free direct source when one exists, else to the
    budget-gated gridstatus path. Direct first, always — the whole point of the
    repoint is to stop spending the 250/month on data we can fetch free."""
    if entry["id"] in _DIRECT_SOURCES:
        return _ingest_direct(entry)
    return _ingest_gridstatus_dataset(entry)


# ── brain hand-off — file the code-shaped gaps for autonomous closure ──
# Standing structural gaps that need real new adapters (not a single-file edit,
# so these seed the brain's strategic/spec pipeline rather than the L5 code queue).
_GAP_FINDINGS = [
    ("grid_capacity_auction_no_ingest",
     "No PJM RPM / base-residual-auction (BRA) capacity clearing-price layer exists — "
     "the single most-cited data-center-power stat ($/MW-day, driven by AI load) is absent. "
     "Build a periodic ingester for the published RPM clearing price + reserve margin."),
    ("grid_hosting_capacity_no_ingest",
     "No utility feeder / hosting-capacity ingest anywhere — we cannot answer 'how much "
     "interconnectable capacity at THIS point'. Build adapters for utility hosting-capacity "
     "GIS (start Dominion/VA) and fuse with the substations layer."),
    ("grid_telemetry_headroom_stub_ercot_pjm_isone",
     "grid_telemetry live headroom is written for 5 of 7 ISOs — ERCOT went LIVE "
     "2026-07-16 (public-reports np6-625-cd gen + np6-235-cd demand). PJM/ISONE still "
     "fail closed pending owner-obtained credentials (PJM_API_KEY from Data Miner 2; "
     "ISO-NE web-services account) — do NOT fake them; DCPI keeps modeled anchors for those 2."),
    ("grid_iso_ne_realtime_lmp_absent",
     "ISO-NE real-time LMP is now CAPTURED in grid_ext_metrics via gridstatus "
     "(isone_lmp_real_time_5_min). Promote it into the canonical iso_lmp_snapshots table + "
     "get_energy_prices/get_grid_data, which still exclude ISO-NE."),
    ("grid_dc_load_queue_ercot_only",
     "The large-load / data-center interconnection-queue figure is ERCOT-only; PJM/MISO/SPP "
     "are null. Also reconcile interconnection_queues.py (says DC-load tracked:false) with the "
     "ingest layer that does derive it for ERCOT."),
    ("gas_pipelines_no_deliverability",
     "gas_pipelines carries presence/density only — NO capacity_mcf / firm deliverability, so "
     "the DCGI gas score can't reflect real pipeline capacity. Add a deliverability source "
     "(EIA-191 / pipeline tariffs)."),
    ("gas_market_pricing_synthetic_basis",
     "market_gas_pricing basis_diff_usd_mmbtu is a SYNTHETIC hardcoded seed "
     "(basis_source='synthetic_seed_basis'); delivered gas price is only as good as the basis. "
     "Wire a live regional hub-basis feed to replace the synthetic seed."),
    ("gas_eia_prices_sparse",
     "eia_gas_prices is sparse — many states NULL (EIA only publishes where available), so "
     "get_gas_intelligence returns null for those states. Backfill industrial/electric-power "
     "gas $/MMBtu for missing states via a regional-hub + basis proxy."),
    ("power_capacity_market_price_absent",
     "No capacity-MARKET clearing price for any ISO (PJM RPM/BRA, ISO-NE FCA, MISO PRA). The "
     "$/MW-day auction price — the forward economic siting signal driven by data-center load — "
     "is absent. Add a periodic capacity-auction clearing-price ingester per ISO."),
]


def parked_datasets() -> list:
    """The registry rows the budget allowlist currently refuses to ingest.

    ★★★ 2026-09-03 — THE PROMISE THAT WAS NEVER WIRED.
      _ingest_gridstatus_dataset's own docstring says the non-allowlisted
      datasets are "skipped with an honest marker so the brain can repoint them
      (finding filed by the caller)". The caller filed _GAP_FINDINGS — a static
      list of nine — and the repoint gap was not one of them. So the marker was
      returned into a dict nobody read, no finding was ever filed, the brain
      never saw it, and nobody repointed anything.

      Measured today: TARGET_DATASETS holds 21 rows and
      GRIDSTATUS_DATASET_ALLOWLIST defaults to four PJM ids — but only THREE of
      those four exist in the registry (`pjm_load` is allowlisted and never
      declared), so the shell can reach three datasets and 18 are parked. It
      has been that way since the 2026-07-26 budget cut (gridstatus free tier
      is 250 req/MONTH; July burned 375), which means the shell whose stated
      job is "constantly widening + freshening coverage" has cycled three
      datasets for five weeks while every tick reported success.

      DERIVED, never restated. A second hand-written list of parked ids would
      rot exactly the way the nine did — this reads TARGET_DATASETS and the
      live allowlist, so it is right on the day someone widens either one, and
      it reports NOTHING once the allowlist covers the registry.
    """
    return [t for t in TARGET_DATASETS
            if t["id"] not in _GS_ALLOWLIST and t["id"] not in _DIRECT_SOURCES]


def _parked_finding() -> tuple | None:
    """(issue, detail) for the parked registry rows, or None when none are."""
    parked = parked_datasets()
    if not parked:
        return None
    by_iso: dict = {}
    for t in parked:
        by_iso.setdefault(t["iso"], []).append(t["id"])
    shown = ", ".join(
        "%s×%d (%s)" % (iso, len(ids), ids[0])
        for iso, ids in sorted(by_iso.items(), key=lambda kv: -len(kv[1]))[:6])
    return (
        "grid_datasets_parked_pending_repoint",
        ("%d of %d registry datasets cannot be ingested: they are outside "
         "GRIDSTATUS_DATASET_ALLOWLIST (currently %s), which was narrowed to "
         "PJM-only on 2026-07-26 because the gridstatus free tier is 250 "
         "req/month and July burned 375. Each one has a free direct source we "
         "already hold credentials for, and each needs its adapter repointed "
         "there — that repoint is the work. Until it happens this shell can "
         "only cycle %d dataset(s), so 'widening coverage' is inert. "
         "%d already repointed to a free direct source (%s). Parked: %s."
         % (len(parked), len(TARGET_DATASETS),
            ",".join(sorted(_GS_ALLOWLIST)) or "(empty)",
            len(TARGET_DATASETS) - len(parked),
            len(_DIRECT_SOURCES),
            ",".join(sorted(_DIRECT_SOURCES)) or "none yet", shown)))


def _file_gap_findings() -> int:
    """Upsert the standing code-shaped gaps into brain_findings (idempotent) so the
    brain's autonomy loop can draft them. Returns count actually inserted/updated.

    NOTE: upsert_brain_finding is SAVEPOINT-wrapped, so it needs a NON-autocommit
    connection — ai_reach._conn() is autocommit=True, under which the savepoints
    fail-and-skip silently (no row written). So open our own transactional conn."""
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return 0
    db = os.environ.get("DATABASE_URL")
    if not db:
        return 0
    import psycopg2
    conn = None
    filed = 0
    try:
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        conn.autocommit = False
        with conn.cursor() as cur:
            # The standing nine, plus the DERIVED parked-datasets gap. Derived
            # last so a change to the allowlist is reflected on the next tick
            # without anyone editing a list.
            _gaps = list(_GAP_FINDINGS)
            _parked = _parked_finding()
            if _parked:
                _gaps.append(_parked)
            for issue, detail in _gaps:
                try:
                    r = upsert_brain_finding(
                        cur, issue=issue,
                        url="/api/v1/admin/grid-data/master-tick",
                        count=1, detail=detail, detector="grid_data_master",
                    )
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


# ── TIER 1 — MEASURE (core freshness + registry breadth) ──────────────
def tier1_measure() -> dict:
    # core feeds serving fresh data? (reachable + non-empty via our own endpoints)
    zones = (_req("/api/v1/iso/zones").get("data") or {})
    lmp = (_req("/api/v1/iso-lmp/snapshot").get("data") or {})
    queue = (_req("/api/v1/interconnection-queue/snapshot").get("data") or {})
    health = (_req("/api/health/data-freshness").get("data") or {})

    zone_count = _num(zones.get("count")) or _num(zones.get("zones_count")) or 0
    lmp_rows = lmp.get("snapshots") or lmp.get("hubs") or lmp.get("data") or lmp.get("prices")
    lmp_n = len(lmp_rows) if isinstance(lmp_rows, list) else (_num(lmp.get("count")) or 0)
    q_by_iso = queue.get("by_iso")
    q_n = len(q_by_iso) if isinstance(q_by_iso, list) else 0
    dc_share_isos = sum(1 for r in (q_by_iso or [])
                        if isinstance(r, dict) and _num(r.get("queued_load_data_center_gw")) is not None)

    # overall data-freshness green ratio (best-effort across shapes)
    green = total = 0
    srcs = health.get("sources") or health.get("datasets") or health.get("feeds")
    if isinstance(srcs, list):
        for s in srcs:
            if not isinstance(s, dict):
                continue
            total += 1
            st = str(s.get("status") or s.get("state") or "").lower()
            if st in ("green", "fresh", "ok", "healthy"):
                green += 1
    green_ratio = round(green / total, 3) if total else None

    # registry rows ONLY — grid_ext_metrics also carries the Depth shell's
    # capacity_price/dc_load_queue/hosting_capacity dataset_ids, which used to
    # inflate breadth to 66/20 and could hand _stalest_tapped a non-registry id.
    ingested = {k: v for k, v in _ingested_state().items() if k in _REGISTRY_IDS}
    dom_tapped = {}
    for did in ingested:
        d = _DATASET_DOM.get(did)
        if d:
            dom_tapped[d] = dom_tapped.get(d, 0) + 1
    # ...and how much of what we absorbed is actually being kept fresh (48h) —
    # the signal that catches the ingest lane silently stalling (07-03→07-10).
    fresh_cut = datetime.now(timezone.utc) - timedelta(hours=48)
    fresh_n = 0
    for v in ingested.values():
        ts = v.get("ingested_at")
        try:
            if ts is not None and ts >= fresh_cut:
                fresh_n += 1
        except TypeError:
            pass
    ext_fresh_ratio = round(fresh_n / len(ingested), 3) if ingested else None
    return {
        "core": {
            "iso_zone_count": zone_count,
            "lmp_locations": lmp_n,
            "queue_isos": q_n,
            "queue_dc_share_isos": dc_share_isos,
            "data_health_green_ratio": green_ratio,
            "ext_fresh_ratio_48h": ext_fresh_ratio,
        },
        "breadth_tapped": len(ingested),
        "breadth_target": len(TARGET_DATASETS),
        "domains": {d: f"{dom_tapped.get(d, 0)}/{_DOMAIN_TARGETS.get(d, 0)}" for d in _DOMAINS},
        "ingested_ids": sorted(ingested.keys()),
        "cats_tapped": sorted({v.get("category") for v in ingested.values() if v.get("category")}),
        "forecast_isos_tapped": sorted({v.get("iso") for v in ingested.values()
                                        if v.get("category") == "load_forecast" and v.get("iso")}),
        "_ingested": ingested,
    }


# ── TIER 2 — SCORE THE FOUR LEVERS (0..1; weakest = next action) ──────
def tier2_score_levers(m: dict) -> dict:
    core = m.get("core") or {}
    # 1. FRESHNESS — are the three core feeds serving + overall health green?
    fresh_bits = [
        1.0 if (core.get("iso_zone_count") or 0) > 0 else 0.0,
        1.0 if (core.get("lmp_locations") or 0) > 0 else 0.0,
        1.0 if (core.get("queue_isos") or 0) > 0 else 0.0,
    ]
    gr = core.get("data_health_green_ratio")
    if gr is not None:
        fresh_bits.append(float(gr))
    efr = core.get("ext_fresh_ratio_48h")
    if efr is not None:
        fresh_bits.append(float(efr))
    freshness = round(sum(fresh_bits) / len(fresh_bits), 3)

    # 2. BREADTH — registry coverage (how much of the firehose we've absorbed)
    tapped = m.get("breadth_tapped") or 0
    target = m.get("breadth_target") or len(TARGET_DATASETS)
    breadth = round(min(1.0, tapped / target), 3) if target else 0.0

    # 3. DEPTH — do we carry the structurally-missing signal categories?
    cats = set(m.get("cats_tapped") or [])
    depth = round(sum(1 for c in _DEPTH_CATS if c in cats) / len(_DEPTH_CATS), 3)

    # 4. FORECAST — forward load across the ISOs
    fc_isos = set(m.get("forecast_isos_tapped") or [])
    forecast = round(len(fc_isos & _LOAD_FC_ISOS) / len(_LOAD_FC_ISOS), 3) if _LOAD_FC_ISOS else 0.0

    scores = {"freshness": freshness, "breadth": breadth, "depth": depth, "forecast": forecast}
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    weakest = next((name for name, _ in ranked if not _lever_off(name)), ranked[0][0])
    return {"scores": scores, "weakest": weakest}


# ── TIER 3 — ACT (one bounded action on the weakest lever) ────────────
def _next_untapped(ingested: dict, prefer_cats: tuple | None = None,
                   prefer_isos: set | None = None) -> dict | None:
    pool = [t for t in TARGET_DATASETS if t["id"] not in ingested]
    if not pool:
        return None
    if prefer_isos:
        pref = [t for t in pool if t["cat"] == "load_forecast" and t["iso"] in prefer_isos]
        if pref:
            return pref[0]
    if prefer_cats:
        pref = [t for t in pool if t["cat"] in prefer_cats]
        if pref:
            return pref[0]
    return pool[0]


def _stalest_tapped(ingested: dict) -> dict | None:
    """When everything is tapped, re-ingest the least-recently-INGESTED dataset.
    Ranked by our own ingested_at, not the data's as_of — capacity/auction rows
    carry old as_of timestamps by nature and would otherwise always win."""
    if not ingested:
        return None
    oldest_id = min(ingested, key=lambda k: str(ingested[k].get("ingested_at") or ""))
    return next((t for t in TARGET_DATASETS if t["id"] == oldest_id), None)


def tier3_act(m: dict, levers: dict) -> dict:
    if _act_disabled():
        return {"action": "none", "reason": "GRID_DATA_MASTER_ACT_DISABLED (shadow mode)"}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed"}
    ingested = m.get("_ingested") or {}

    if lever == "freshness":
        # self-heal: re-trigger whichever core feed is EMPTY. When every core
        # feed is serving, fall through to grid_ext_metrics upkeep below — the
        # old unconditional iso/all/extract burned every tick as a no-op and
        # starved the gridstatus lane (zero re-ingests 07-03→07-10).
        core = m.get("core") or {}
        r = tgt = None
        if (core.get("lmp_locations") or 0) == 0:
            r = _fire("/api/v1/iso-lmp/ingest"); tgt = "iso-lmp/ingest"
        elif (core.get("queue_isos") or 0) == 0:
            r = _fire("/api/v1/iso-queue/ingest"); tgt = "iso-queue/ingest"
        elif (core.get("iso_zone_count") or 0) == 0:
            r = _fire("/api/v1/iso/all/extract"); tgt = "iso/all/extract"
        if tgt:
            return {"action": "self_heal_refresh", "lever": lever, "target": tgt,
                    "dispatched": r.get("dispatched")}

    # breadth / depth / forecast (+ healthy-core freshness) → absorb the next
    # relevant untapped dataset, else re-ingest the stalest to keep it fresh
    prefer_cats = _DEPTH_CATS if lever == "depth" else None
    prefer_isos = (_LOAD_FC_ISOS - set(m.get("forecast_isos_tapped") or [])) if lever == "forecast" else None
    target = _next_untapped(ingested, prefer_cats=prefer_cats, prefer_isos=prefer_isos)
    if target is None:
        target = _stalest_tapped(ingested)
        mode = "maintain_freshness"
    else:
        mode = "absorb_new_source"
    if target is None:
        return {"action": "none", "reason": "no target datasets"}
    res = _ingest_dataset(target)
    if not res.get("ok") and mode == "absorb_new_source":
        # a permanently-failing registry id must not pin the lane — keep the
        # stalest tapped dataset fresh in the same bounded tick instead
        fb = _stalest_tapped(ingested)
        if fb is not None and fb["id"] != target["id"]:
            return {"action": "ingest_gridstatus", "mode": "maintain_freshness_fallback",
                    "lever": lever, "dataset": fb["id"],
                    "failed_absorb": {"dataset": target["id"], "error": res.get("error")},
                    "result": _ingest_dataset(fb)}
    return {"action": "ingest_gridstatus", "mode": mode, "lever": lever,
            "dataset": target["id"], "result": res}


# ── SCORE, PERSIST ────────────────────────────────────────────────────
def grid_data_score(levers: dict) -> float:
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
                INSERT INTO grid_data_snapshots
                  (grid_data_score, breadth_tapped, breadth_target, weakest_lever,
                   action_taken, lever_scores, findings_filed, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                score, m.get("breadth_tapped"), m.get("breadth_target"),
                levers.get("weakest"), (action or {}).get("action"),
                json.dumps(levers.get("scores") or {}), findings,
                json.dumps({"core": m.get("core"), "cats_tapped": m.get("cats_tapped"),
                            "forecast_isos": m.get("forecast_isos_tapped"),
                            "levers": levers, "action": action}),
            ))
        return True
    except Exception:
        note_swallowed_write("grid_data_snapshots", where="grid_data_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


def _ticked_today() -> bool:
    """True when a grid_data_snapshots row already exists for the current UTC
    day. The every-5-min heartbeat (GH cron-heartbeat.yml + the worker's
    in-process self-heartbeat) re-POSTs this tick across the whole hour==11
    dispatch window — 11-22 fires/day. The DB writes are idempotent, but each
    tick's gridstatus ingest is a REAL provider call: that multiplier is how
    July 2026 burned ~330 unledgered calls against the 250 free-tier cap. One
    COMPLETED tick per day is the design (a tick that died before _persist
    still gets retried by the next heartbeat); ?force=1 overrides."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM grid_data_snapshots
                WHERE computed_at >= date_trunc('day', NOW()) LIMIT 1
            """)
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@grid_data_master_shell_bp.route("/api/v1/admin/grid-data/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="GRID_DATA_MASTER_DISABLED"), 200
    started = time.time()
    _ensure_tables()  # idempotent; must exist before measure reads / act ingests on tick #1
    if request.args.get("force") not in ("1", "true") and _ticked_today():
        return jsonify(ok=True, skipped="already_ticked_today",
                       note="one completed tick per UTC day — the 5-min heartbeat "
                            "re-fires this URL all hour; pass ?force=1 to override"), 200
    measure = tier1_measure()
    levers = tier2_score_levers(measure)
    action = tier3_act(measure, levers)
    findings = 0 if _act_disabled() else _file_gap_findings()
    score = grid_data_score(levers)
    persisted = _persist(measure, levers, score, action, findings)

    s = levers.get("scores") or {}
    doms = measure.get("domains") or {}
    # A refused/quota'd gridstatus call must be LOUD in the headline (and as a
    # top-level field), not buried in tier3_action.result — swallowing these is
    # exactly how 375/250 provider calls went unnoticed in July 2026.
    gs_alert = None
    for _e in (str(((action or {}).get("result") or {}).get("error") or ""),
               str(((action or {}).get("failed_absorb") or {}).get("error") or "")):
        if _e.startswith(("budget_exhausted", "http_403")):
            gs_alert = _e
            break
    headline = (
        f"grid-data {score}/100 · breadth {measure.get('breadth_tapped')}/{measure.get('breadth_target')} "
        f"(grid {doms.get('grid')} · power {doms.get('power')} · gas {doms.get('gas')}) · "
        f"weakest → {levers.get('weakest')} ({s.get(levers.get('weakest'))}) · "
        f"acted: {action.get('action')}"
        + (f" ({action.get('dataset')})" if action.get('dataset') else "")
        + f" · {findings} gaps→brain"
        + (f" · ⚠️ gridstatus {gs_alert.split(':')[0]}" if gs_alert else "")
    )
    return jsonify(
        ok=True,
        gridstatus_alert=gs_alert,
        ms=int((time.time() - started) * 1000),
        grid_data_score=score,
        headline=headline,
        tier1_measure={k: v for k, v in measure.items() if k != "_ingested"},
        tier2_levers=levers,
        tier3_action=action,
        findings_filed=findings,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@grid_data_master_shell_bp.route("/api/v1/grid/extended/<iso>", methods=["GET"])
def grid_extended(iso):
    """Forward-looking + supply signals for an ISO from grid_ext_metrics — the data
    the master shell absorbs from gridstatus that no serving tool exposed yet:
    forward load, committed capacity, operating reserve/margin, grid carbon
    intensity, zone LMP. Public read; complements demand+mix in get_grid_intelligence."""
    iso = (iso or "").upper().strip()
    c = _conn()
    if c is None:
        return jsonify(iso=iso, available=False, reason="db_unavailable"), 200
    out = {"iso": iso, "available": False, "source": "DC Hub grid_ext_metrics (gridstatus.io)"}
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (category) category, primary_value, unit, as_of, raw
                FROM grid_ext_metrics
                WHERE upper(iso) = %s AND primary_value IS NOT NULL
                ORDER BY category, as_of DESC NULLS LAST
            """, (iso,))
            cat = {r["category"]: r for r in cur.fetchall()}
        mapping = {
            "forward_load_mw": "load_forecast",
            "committed_capacity_mw": "capacity",
            "operating_reserve_mw": "reserves",
            "operating_margin_mw": "margin",
            "grid_carbon_intensity_lb_mwh": "emissions",
            "zone_lmp_usd_mwh": "lmp",
            "dc_load_queue_gw": "dc_load_queue",
            "dc_load_queue_measured_gw": "dc_load_queue_measured",
        }
        for field, c0 in mapping.items():
            r = cat.get(c0)
            if r and r.get("primary_value") is not None:
                out[field] = float(r["primary_value"])
                out[field + "_as_of"] = str(r.get("as_of"))
        # capacity-MARKET clearing price ($/MW-day) — prefer the Depth shell's
        # ingested published-auction rows (latest delivery year, e.g. PJM 2027/28
        # 333.44), falling back to the static seed (which alone had gone stale
        # at 2025/26 269.92).
        cp = cat.get("capacity_price")
        if cp and cp.get("primary_value") is not None:
            r0 = cp.get("raw") if isinstance(cp.get("raw"), dict) else {}
            out["capacity_auction_price_usd_mw_day"] = float(cp["primary_value"])
            out["capacity_auction_delivery_year"] = r0.get("delivery_year")
            out["capacity_auction_source"] = r0.get("source")
            out["capacity_auction_as_of"] = str(cp.get("as_of"))
        else:
            ca = _capacity_auction(iso)
            if ca.get("price_usd_mw_day") is not None:
                out["capacity_auction_price_usd_mw_day"] = ca["price_usd_mw_day"]
                out["capacity_auction_delivery_year"] = ca.get("delivery_year")
                out["capacity_auction_source"] = ca.get("source")
                out["capacity_auction_as_of"] = ca.get("as_of")
        if len(out) > 3:
            out["available"] = True
            out["note"] = ("Forward/supply signals absorbed from gridstatus.io by the grid-data "
                           "master shell — complements the live demand + fuel mix.")
        return jsonify(out), 200
    except Exception as e:
        return jsonify(iso=iso, available=False, error=f"{type(e).__name__}: {str(e)[:140]}"), 200
    finally:
        try: c.close()
        except Exception: pass


@grid_data_master_shell_bp.route("/api/v1/admin/grid-data/master-state", methods=["GET"])
def master_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM grid_data_snapshots ORDER BY id DESC LIMIT 20")
            rows = cur.fetchall()
        return jsonify(
            ok=True,
            latest=rows[0] if rows else None,
            trend=[{"at": str(r.get("computed_at")), "score": _num(r.get("grid_data_score")),
                    "breadth": f"{r.get('breadth_tapped')}/{r.get('breadth_target')}",
                    "weakest": r.get("weakest_lever"), "acted": r.get("action_taken")}
                   for r in rows],
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
