"""
iso_grid_adapters.py — Phase FF (2026-05-22)
============================================
Live ISO grid-telemetry adapters that feed the DCPI excess-power score with
REAL per-zone headroom (online generation vs. load) instead of the hardcoded
`reserve_margin_pct or 12` default in compute_excess_power_score(). One
normalized schema across all 7 US ISOs.

STATUS: SKELETON / FRAMEWORK — intentionally inert until activated.
  • Every adapter is FAIL-SAFE: with no creds it no-ops and returns []. So
    importing/running this module changes nothing until env vars are set.
  • NOT yet wired into compute_excess_power_score() — that's the verified
    next step, once a live pull is confirmed for an ISO.
  • NOT yet on an active cron — staged disabled in dchub-scheduler. Real-time
    pulls MUST run in the scheduler service, NEVER in-process (the in-process
    bulk-loaders are what caused the Neon pool-exhaustion → SIGTERM loops).

Normalized record (one per ISO zone per pull):
  {iso, zone, observed_at, online_gen_mw, load_mw, headroom_mw,
   reserve_margin_pct, fuel_mix(dict), source}

Activate one ISO end-to-end first (ERCOT — Dallas/TX), verify live, wire ONE
scoring input, THEN template the rest. run_all() pulls every ISO whose creds
are present.
"""

from __future__ import annotations

import os
import io
import csv
import json
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error
from routes._swallowed_writes import note_swallowed_write

# Spacing between ERCOT fuel-mix fetches (seconds) — keeps the tail of the
# request burst under APIM throttling. Tests set 0 via env.
_ERCOT_FUEL_SPACING_S = float(os.environ.get("ERCOT_FUEL_SPACING_S", "1.5") or 0)


# ─────────────────────────────────────────────────────────────────────
# Storage — grid_telemetry snapshots
# ─────────────────────────────────────────────────────────────────────
def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS grid_telemetry (
    id                 BIGSERIAL PRIMARY KEY,
    iso                TEXT NOT NULL,
    zone               TEXT,
    observed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    online_gen_mw      REAL,
    load_mw            REAL,
    headroom_mw        REAL,
    reserve_margin_pct REAL,
    fuel_mix           JSONB DEFAULT '{}'::jsonb,
    source             TEXT
);
CREATE INDEX IF NOT EXISTS ix_grid_telemetry_iso_zone_ts
    ON grid_telemetry (iso, zone, observed_at DESC);
"""


def ensure_schema() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[iso_grid] schema ensure skipped: {e}", flush=True)
        return False
    finally:
        try: c.close()
        except Exception: pass


def store_records(records: list[dict]) -> int:
    """Insert normalized telemetry records. Returns count stored. Never raises."""
    if not records:
        return 0
    c = _conn()
    if c is None:
        return 0
    n = 0
    try:
        with c, c.cursor() as cur:
            for r in records:
                # Honest fail-closed markers carry no telemetry — never persist
                # them as data rows (would write a misleading all-NULL record).
                if r.get("source_unavailable"):
                    continue
                try:
                    cur.execute("""
                        INSERT INTO grid_telemetry
                            (iso, zone, observed_at, online_gen_mw, load_mw,
                             headroom_mw, reserve_margin_pct, fuel_mix, source)
                        VALUES (%s,%s,COALESCE(%s,NOW() ON CONFLICT DO NOTHING),%s,%s,%s,%s,%s::jsonb,%s)
                    """, (
                        r.get("iso"), r.get("zone"), r.get("observed_at"),
                        r.get("online_gen_mw"), r.get("load_mw"),
                        r.get("headroom_mw"), r.get("reserve_margin_pct"),
                        json.dumps(r.get("fuel_mix") or {}), r.get("source"),
                    ))
                    n += 1
                except Exception:
                    note_swallowed_write("grid_telemetry", where="iso_grid_adapters.store_records")
                    continue
    except Exception as e:
        print(f"[iso_grid] store skipped: {e}", flush=True)
    finally:
        try: c.close()
        except Exception: pass
    return n


def _record(iso, zone, online_gen_mw=None, load_mw=None,
            reserve_margin_pct=None, fuel_mix=None, source=None) -> dict:
    """Build a normalized record; derives headroom when both gen + load present."""
    headroom = None
    if online_gen_mw is not None and load_mw is not None:
        headroom = float(online_gen_mw) - float(load_mw)
    return {
        "iso": iso, "zone": zone,
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "online_gen_mw": online_gen_mw, "load_mw": load_mw,
        "headroom_mw": headroom, "reserve_margin_pct": reserve_margin_pct,
        "fuel_mix": fuel_mix or {}, "source": source,
    }


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _http_json(url: str, headers: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_text(url: str, headers: dict | None = None, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "dchub-iso/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _to_float(v) -> float | None:
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s not in ("", "-", "N/A") else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# Per-ISO registry — base URL, auth model, env-var prefix, status
# ─────────────────────────────────────────────────────────────────────
# auth: "oauth_key" (ERCOT — bearer + subscription key) | "key" (header key)
#       | "basic" (user/pass) | "public" (no auth)
ISO_REGISTRY = {
    "ERCOT": {"auth": "oauth_key", "base": "https://api.ercot.com/api/public-reports",
              "env": "ERCOT", "impl": "fetch_ercot",
              "note": "Azure-APIM: Ocp-Apim-Subscription-Key + B2C ROPC bearer. "
                      "Products: np6-625-cd gen + np6-235-cd demand "
                      "(+ np4-733/738-cd wind/solar mix)."},
    "CAISO": {"auth": "public", "base": "https://www.caiso.com/outlook/current",
              "env": "CAISO", "impl": "fetch_caiso",
              "note": "Today's Outlook CSVs (fuelsource + demand) — public, no auth."},
    "PJM":   {"auth": "key", "base": "https://api.pjm.com/api/v1",
              "env": "PJM", "impl": "fetch_pjm",
              "note": "Data Miner 2 — needs Ocp-Apim-Subscription-Key. "
                      "Fails closed (source_unavailable) without PJM_API_KEY."},
    "MISO":  {"auth": "public", "base": "https://public-api.misoenergy.org/api",
              "env": "MISO", "impl": "fetch_miso",
              "note": "Public RT API (FuelMix + RealTimeTotalLoad) — no auth. "
                      "Legacy MISORTWDDataBroker host now returns no data."},
    "SPP":   {"auth": "public", "base": "https://portal.spp.org/chart-api",
              "env": "SPP", "impl": "fetch_spp",
              "note": "Public portal chart API (gen-mix + load-forecast "
                      "'Actual Load') — no auth."},
    "NYISO": {"auth": "public", "base": "http://mis.nyiso.com/public/csv",
              "env": "NYISO", "impl": "fetch_nyiso",
              "note": "Public CSV by report (rtfuelmix + pal) — no auth."},
    "ISONE": {"auth": "basic", "base": "https://webservices.iso-ne.com/api/v1.1",
              "env": "ISONE", "impl": "fetch_isone",
              "note": "Web Services — HTTP basic auth (account user/pass). "
                      "Fails closed without ISONE_USERNAME/PASSWORD."},
}


def _has_creds(iso: str) -> bool:
    cfg = ISO_REGISTRY.get(iso, {})
    auth, p = cfg.get("auth"), cfg.get("env", iso)
    if iso == "PJM" and _env("EIA_API_KEY"):
        # shell#35 WS8: fetch_pjm's PRIMARY is EIA-930 (no PJM creds needed);
        # PJM_API_KEY remains only the optional DM2 fallback.
        return True
    if auth == "public":
        return True
    if auth == "key":
        return bool(_env(f"{p}_API_KEY"))
    if auth == "basic":
        return bool(_env(f"{p}_USERNAME") and _env(f"{p}_PASSWORD"))
    if auth == "oauth_key":
        return bool(_env(f"{p}_API_KEY") and _env(f"{p}_USERNAME")
                    and _env(f"{p}_PASSWORD"))
    return False


# ─────────────────────────────────────────────────────────────────────
# ERCOT — implemented against the real api.ercot.com contract.
# Auth = Azure AD B2C ROPC bearer + Ocp-Apim-Subscription-Key. The token URL
# and client_id are ENV-OVERRIDABLE so we never ship a value we can't verify;
# the documented ERCOT defaults are placeholders to confirm on first live run.
# ─────────────────────────────────────────────────────────────────────
_LAST_BEARER_ERR: str | None = None


def _ercot_bearer() -> str | None:
    """ROPC token. Returns access_token or None (fail-safe). Records the
    failure reason in _LAST_BEARER_ERR so the probe can report it precisely."""
    global _LAST_BEARER_ERR
    _LAST_BEARER_ERR = None
    user, pw = _env("ERCOT_USERNAME"), _env("ERCOT_PASSWORD")
    if not (user and pw):
        _LAST_BEARER_ERR = "missing ERCOT_USERNAME and/or ERCOT_PASSWORD"
        return None
    client_id = _env("ERCOT_CLIENT_ID", "")           # set from your ERCOT app
    if not client_id:
        _LAST_BEARER_ERR = ("missing ERCOT_CLIENT_ID — Azure B2C rejects the "
                            "ROPC token request without it. Find it in ERCOT's "
                            "API authorization docs (public client ID).")
        return None
    token_url = _env("ERCOT_TOKEN_URL",
        "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
        "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token")   # VERIFY on first live run
    scope = _env("ERCOT_SCOPE", f"openid {client_id} offline_access")
    body = urllib.parse.urlencode({
        "grant_type": "password", "username": user, "password": pw,
        "client_id": client_id, "scope": scope, "response_type": "token",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(token_url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read().decode("utf-8")).get("access_token")
        if not tok:
            _LAST_BEARER_ERR = "token endpoint returned no access_token"
        return tok
    except urllib.error.HTTPError as e:
        detail = ""
        try: detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception: pass
        _LAST_BEARER_ERR = (f"token HTTP {e.code} — likely wrong client_id, "
                            f"token URL, scope, or credentials. {detail}")
        print(f"[iso_grid] ERCOT bearer HTTP {e.code}: {detail}", flush=True)
        return None
    except Exception as e:
        _LAST_BEARER_ERR = f"token request error: {str(e)[:160]}"
        print(f"[iso_grid] ERCOT bearer failed: {e}", flush=True)
        return None


# Confirmed live 2026-07-16 against https://api.ercot.com/api/public-reports
# (the old /api/public-data base 302s to a loading page and then 401s — it was
# never the product catalog):
#   GET {base}                → {"_embedded":{"products":[{emilId,...}]}}
#   GET {base}/{emilId}       → product detail; artifacts[]._links.endpoint.href
#   GET <artifact endpoint>   → {"_meta":{"sortedBy":...},"fields":[{"name":..}],
#                                "data":[[...]]} with ?size=N paging
# Products used (EMIL ids, env-overridable):
#   ERCOT_GEN_PRODUCT_ID   np6-625-cd  "State Estimator Load Report - Total
#       ERCOT Generation" → se_ld_rpt_ercot_gen, hourly, sorted seExeTime DESC;
#       seMW = total system generation output.
#   ERCOT_LOAD_PRODUCT_ID  np6-235-cd  "System-Wide Demand" →
#       system_wide_demand, 15-min actuals posted hourly, sorted
#       deliveryDate DESC + timeEnding ASC → latest is resolved client-side.
# Fuel mix (best-effort, never fatal): NP4-733-CD wind + NP4-738-CD solar
# actual 5-min system-wide values. The public-reports catalog exposes no full
# fuel-mix product, so the mix carries only the two renewables ERCOT publishes
# here — consumers must not treat it as exhaustive.
# KNOWN SEMANTICS: seMW is gross generation (includes private-use-network
# units and storage-charging offset), so headroom_mw = gen − demand runs
# persistently positive for ERCOT (~+13%) — the same class of structural
# offset as the other system adapters (MISO runs ~−14% because its online gen
# excludes imports). The DCPI blend clamps this (±LIVE_DELTA_CAP_PCT).
def _ercot_headers() -> dict | None:
    """Auth headers for api.ercot.com, or None when no subscription key."""
    key = _env("ERCOT_API_KEY")
    if not key:
        return None
    headers = {"Ocp-Apim-Subscription-Key": key}
    bearer = _ercot_bearer()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _ercot_table(payload) -> list[dict]:
    """ERCOT public-reports {fields,data} arrays → list of row dicts. Pure."""
    if not isinstance(payload, dict):
        return []
    names = [f.get("name") for f in (payload.get("fields") or [])
             if isinstance(f, dict) and f.get("name")]
    if not names:
        return []
    return [dict(zip(names, row)) for row in (payload.get("data") or [])
            if isinstance(row, (list, tuple))]


def _ercot_latest_gen_mw(rows: list[dict]) -> float | None:
    """Latest seMW by seExeTime (ISO-8601 strings — lexical max is temporal
    max). Pure; does not rely on server-side sort order."""
    best_ts, best = "", None
    for r in rows:
        ts = str(r.get("seExeTime") or "")
        mw = _to_float(r.get("seMW"))
        if mw is not None and ts > best_ts:
            best_ts, best = ts, mw
    return best


def _ercot_latest_demand_mw(rows: list[dict], max_age_days: int = 2) -> float | None:
    """Latest 15-min demand by (deliveryDate, timeEnding). Returns None when
    the newest row is older than max_age_days — a frozen upstream feed must
    degrade to the modeled fallback, never masquerade as live. Pure."""
    best_key, best = ("", ""), None
    for r in rows:
        key = (str(r.get("deliveryDate") or ""), str(r.get("timeEnding") or ""))
        mw = _to_float(r.get("demand"))
        if mw is not None and key > best_key:
            best_key, best = key, mw
    if best is None:
        return None
    try:
        latest = datetime.date.fromisoformat(best_key[0])
        today = datetime.datetime.now(datetime.timezone.utc).date()
        if (today - latest).days > max_age_days:
            return None
    except Exception:
        return None
    return best


def _ercot_latest_syswide_mw(rows: list[dict]) -> float | None:
    """Latest genSystemWide by intervalEnding (wind/solar 5-min actuals). Pure."""
    best_ts, best = "", None
    for r in rows:
        ts = str(r.get("intervalEnding") or "")
        mw = _to_float(r.get("genSystemWide"))
        if mw is not None and ts > best_ts:
            best_ts, best = ts, mw
    return best


def _ercot_artifact_rows(headers: dict, product_id: str, size: int = 200) -> list[dict]:
    """Resolve a product's data-artifact endpoint from its detail record and
    fetch the most recent page as row dicts. Errors propagate to the caller."""
    base = ISO_REGISTRY["ERCOT"]["base"]
    pid = urllib.parse.quote((product_id or "").strip().lower())
    detail = _http_json(f"{base}/{pid}", headers=headers)
    for art in (detail or {}).get("artifacts") or []:
        href = (((art.get("_links") or {}).get("endpoint") or {}).get("href")) or ""
        if href:
            return _ercot_table(_http_json(f"{href}?size={int(size)}", headers=headers))
    return []


def fetch_ercot() -> list[dict]:
    """Pull ERCOT real-time system generation/load (+ wind/solar mix) → one
    normalized record. Fail-safe → [] (never a fabricated number)."""
    headers = _ercot_headers()
    if headers is None:
        return []
    gen_pid = _env("ERCOT_GEN_PRODUCT_ID", "np6-625-cd")
    load_pid = _env("ERCOT_LOAD_PRODUCT_ID", "np6-235-cd")
    try:
        # size: gen posts 1 SE row/hour → 30 covers a day; demand posts 96
        # 15-min rows/day sorted date-DESC-but-time-ASC → 200 spans today fully.
        gen = _ercot_latest_gen_mw(_ercot_artifact_rows(headers, gen_pid, size=30))
        load = _ercot_latest_demand_mw(_ercot_artifact_rows(headers, load_pid, size=200))
        if gen is None or gen <= 0 or load is None or load <= 0:
            print(f"[iso_grid] ERCOT extraction incomplete (gen={gen}, "
                  f"load={load}) — check {gen_pid}/{load_pid} shapes/freshness.",
                  flush=True)
            return []
        # Mix is contextual — never fail the record over it. But do NOT fail
        # silently either: the first prod deploy stored empty fuel_mix on every
        # pull with zero log evidence. Prod→ERCOT latency is low enough that
        # the 8-GET burst trips APIM tail throttling on these last requests
        # (gen/load, first in the burst, always pass) — so space the fuel
        # fetches out and retry once on 429.
        fuel_mix = {}
        for label, pid in (("Wind", "np4-733-cd"), ("Solar", "np4-738-cd")):
            for attempt in (1, 2):
                try:
                    if _ERCOT_FUEL_SPACING_S > 0:
                        time.sleep(_ERCOT_FUEL_SPACING_S * attempt)
                    mw = _ercot_latest_syswide_mw(
                        _ercot_artifact_rows(headers, pid, size=5))
                    if mw is not None:
                        fuel_mix[label] = round(mw, 1)
                    break
                except Exception as fe:
                    code = getattr(fe, "code", None)
                    if attempt == 1 and code == 429:
                        continue
                    print(f"[iso_grid] ERCOT fuel-mix {label} ({pid}) skipped: "
                          f"{type(fe).__name__} {code or str(fe)[:80]}", flush=True)
                    break
        return [_record("ERCOT", "ERCOT",
                        online_gen_mw=round(gen, 1),
                        load_mw=round(load, 1),
                        fuel_mix=fuel_mix,
                        source=f"ercot:{gen_pid}(seMW)+{load_pid}(demand)")]
    except urllib.error.HTTPError as e:
        print(f"[iso_grid] ERCOT HTTP {e.code} (check key/bearer/scope)", flush=True)
        return []
    except Exception as e:
        print(f"[iso_grid] ERCOT fetch error: {e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────────────────
# NYISO — public dated CSVs, no auth. Confirmed live shapes (2026-05-22):
#   rtfuelmix:  Time Stamp, Time Zone, Fuel Category, Gen MW   (row per fuel)
#   pal (load): "Time Stamp","Time Zone","Name","PTID","Load"  (row per zone)
# We build ONE system record: total online gen (Σ latest fuel mix) vs total
# load (Σ latest zone loads), with the fuel mix carried for context.
# ─────────────────────────────────────────────────────────────────────
def _nyiso_csv(report: str) -> str:
    """Fetch today's NYISO CSV for a report (e.g. 'rtfuelmix','pal'); falls
    back to yesterday near midnight ET when today's file is empty."""
    base = ISO_REGISTRY["NYISO"]["base"]
    for delta in (0, 1):
        d = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=delta)).strftime("%Y%m%d")
        try:
            txt = _http_text(f"{base}/{report}/{d}{report}.csv")
            if txt and txt.count("\n") > 1:
                return txt
        except Exception:
            continue
    return ""


def fetch_nyiso() -> list[dict]:
    """NYISO system telemetry from public real-time CSVs. Fail-safe → []."""
    try:
        fuel_txt = _nyiso_csv("rtfuelmix")
        load_txt = _nyiso_csv("pal")
        if not fuel_txt or not load_txt:
            return []
        # --- fuel mix: keep only rows at the latest timestamp ---
        frows = list(csv.DictReader(io.StringIO(fuel_txt)))
        if not frows:
            return []
        latest_fts = max(r["Time Stamp"] for r in frows if r.get("Time Stamp"))
        fuel_mix, total_gen = {}, 0.0
        for r in frows:
            if r.get("Time Stamp") != latest_fts:
                continue
            mw = _to_float(r.get("Gen MW"))
            cat = (r.get("Fuel Category") or "").strip()
            if mw is not None and cat:
                fuel_mix[cat] = round(mw, 1)
                total_gen += mw
        # --- load: sum zone loads at the latest timestamp ---
        lrows = list(csv.DictReader(io.StringIO(load_txt)))
        if not lrows:
            return []
        latest_lts = max(r["Time Stamp"] for r in lrows if r.get("Time Stamp"))
        total_load = 0.0
        for r in lrows:
            if r.get("Time Stamp") != latest_lts:
                continue
            v = _to_float(r.get("Load"))
            if v is not None:
                total_load += v
        if total_gen <= 0 or total_load <= 0:
            return []
        return [_record("NYISO", "NYCA",
                        online_gen_mw=round(total_gen, 1),
                        load_mw=round(total_load, 1),
                        fuel_mix=fuel_mix,
                        source="nyiso:rtfuelmix+pal")]
    except Exception as e:
        print(f"[iso_grid] NYISO fetch error: {e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────────────────
# CAISO — public "Today's Outlook" CSVs, no auth. Confirmed live (2026-05-22):
#   outlook/current/fuelsource.csv  Time, Solar, Wind, ... Imports, Other
#   outlook/current/demand.csv      Time, Day ahead fc, Hour ahead fc,
#                                    Current demand, Demand response
# One system record: Σ latest fuel-source row = supply, Current demand = load.
# ─────────────────────────────────────────────────────────────────────
def fetch_caiso() -> list[dict]:
    """CAISO system telemetry from public Today's Outlook CSVs. Fail-safe → []."""
    try:
        fs = _http_text("https://www.caiso.com/outlook/current/fuelsource.csv")
        dm = _http_text("https://www.caiso.com/outlook/current/demand.csv")
        if not fs or not dm:
            return []
        frows = [r for r in csv.DictReader(io.StringIO(fs))
                 if any((v or "").strip() for k, v in r.items() if k != "Time")]
        if not frows:
            return []
        last = frows[-1]
        fuel_mix, total_gen = {}, 0.0
        for k, v in last.items():
            if k == "Time":
                continue
            mw = _to_float(v)
            if mw is not None:
                fuel_mix[k] = round(mw, 1)
                total_gen += mw
        # demand: latest row with a non-empty Current demand
        load = None
        for r in csv.DictReader(io.StringIO(dm)):
            v = _to_float(r.get("Current demand"))
            if v is not None:
                load = v
        if total_gen <= 0 or load is None:
            return []
        return [_record("CAISO", "CAISO",
                        online_gen_mw=round(total_gen, 1),
                        load_mw=round(load, 1),
                        fuel_mix=fuel_mix,
                        source="caiso:outlook")]
    except Exception as e:
        print(f"[iso_grid] CAISO fetch error: {e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────────────────
# MISO — public, no auth. The legacy MISORTWDDataBroker host now returns
# {"error":"no data"} for every messageType (migrated 2025-12). The CURRENT
# canonical public real-time feed is public-api.misoenergy.org (same feed
# gridstatus/pyiso use). Verified live 2026-06-02:
#   GET /api/FuelMix            → {"TotalMW","Fuel":{"Type":[{CATEGORY,ACT,
#                                  INTERVALEST}]}}  (online gen + fuel mix)
#   GET /api/RealTimeTotalLoad  → {"LoadInfo":{"FiveMinTotalLoad":
#                                  [{"Load":{"Time","Value"}}]}}  (actual RT load)
# We use ONLY the actual 5-min load (FiveMinTotalLoad), never the forecast
# (MediumTermLoadForecast) or cleared (ClearedMW) series. If MISO returns no
# data we return [] — never a fabricated number.
# ─────────────────────────────────────────────────────────────────────
_MISO_BASE = "https://public-api.misoenergy.org/api"


def fetch_miso() -> list[dict]:
    """MISO system telemetry from the public real-time API. Fail-safe → []."""
    hdrs = {"User-Agent": "dchub-iso/1.0", "Accept": "application/json"}
    try:
        # --- online generation + fuel mix ---
        fm = _http_json(f"{_MISO_BASE}/FuelMix", headers=hdrs)
        if not isinstance(fm, dict):
            return []
        types = (((fm.get("Fuel") or {}).get("Type")) or [])
        fuel_mix, sum_act = {}, 0.0
        for t in types:
            if not isinstance(t, dict):
                continue
            mw = _to_float(t.get("ACT"))
            cat = (t.get("CATEGORY") or "").strip()
            if mw is not None and cat:
                fuel_mix[cat] = round(mw, 1)
                sum_act += mw
        # Prefer MISO's own authoritative TotalMW; fall back to Σ ACT.
        total_gen = _to_float(fm.get("TotalMW"))
        if total_gen is None:
            total_gen = sum_act if sum_act > 0 else None

        # --- actual real-time load (latest 5-min point) ---
        ld = _http_json(f"{_MISO_BASE}/RealTimeTotalLoad", headers=hdrs)
        load = None
        five = (((ld or {}).get("LoadInfo") or {}).get("FiveMinTotalLoad")) or []
        for entry in five:
            v = _to_float(((entry or {}).get("Load") or {}).get("Value"))
            if v is not None:
                load = v   # keep walking → last non-null is the most recent
        if not total_gen or total_gen <= 0 or load is None or load <= 0:
            return []
        return [_record("MISO", "MISO",
                        online_gen_mw=round(total_gen, 1),
                        load_mw=round(load, 1),
                        fuel_mix=fuel_mix,
                        source="miso:public-api/FuelMix+RealTimeTotalLoad")]
    except urllib.error.HTTPError as e:
        print(f"[iso_grid] MISO HTTP {e.code}", flush=True)
        return []
    except Exception as e:
        print(f"[iso_grid] MISO fetch error: {e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────────────────
# SPP — public, no auth. Real-time chart feeds behind the public SPP portal.
# Verified live 2026-06-02:
#   GET /chart-api/gen-mix/asChart       → {"response":{"labels":[ts...],
#       "datasets":[{"label":<fuel>,"data":[mw...]}]}}  (per-fuel, 5-min,
#       rolling 2h — latest column = online gen by fuel)
#   GET /chart-api/load-forecast/asChart → datasets include "Actual Load"
#       alongside forecasts; we take ONLY the "Actual Load" series' last
#       non-null point. Forecast/Mid-Term series are never used as load.
# Returns [] (never a fabricated number) if either feed is unavailable.
# ─────────────────────────────────────────────────────────────────────
_SPP_BASE = "https://portal.spp.org/chart-api"


def _spp_last_point(datasets: list, want_label: str | None = None):
    """From a SPP chart dataset list, return the latest non-null value.
    If want_label is given, restrict to that single series; otherwise the
    caller is summing — see fetch_spp. Returns float | None."""
    series = None
    if want_label is not None:
        for ds in datasets or []:
            if isinstance(ds, dict) and (ds.get("label") or "").strip().lower() == want_label.lower():
                series = ds.get("data") or []
                break
        if series is None:
            return None
    else:
        series = []
    last = None
    for v in series:
        fv = _to_float(v)
        if fv is not None:
            last = fv
    return last


def fetch_spp() -> list[dict]:
    """SPP system telemetry from the public portal chart API. Fail-safe → []."""
    hdrs = {"User-Agent": "Mozilla/5.0 dchub-iso/1.0", "Accept": "application/json"}
    try:
        # --- generation mix: latest column across all fuel datasets ---
        gm = _http_json(f"{_SPP_BASE}/gen-mix/asChart", headers=hdrs)
        gresp = (gm or {}).get("response") or {}
        gdatasets = gresp.get("datasets") or []
        glabels = gresp.get("labels") or []
        if not gdatasets or not glabels:
            return []
        # Find the latest time index for which at least one fuel has a value,
        # then read every fuel at that same index → one consistent snapshot.
        n = min(len(glabels), max((len(ds.get("data") or []) for ds in gdatasets), default=0))
        if n <= 0:
            return []
        snap_idx = None
        for i in range(n - 1, -1, -1):
            if any(_to_float((ds.get("data") or [None] * n)[i]) is not None
                   for ds in gdatasets if isinstance(ds, dict)):
                snap_idx = i
                break
        if snap_idx is None:
            return []
        fuel_mix, total_gen = {}, 0.0
        for ds in gdatasets:
            if not isinstance(ds, dict):
                continue
            data = ds.get("data") or []
            if snap_idx >= len(data):
                continue
            mw = _to_float(data[snap_idx])
            label = (ds.get("label") or "").strip()
            if mw is not None and label:
                fuel_mix[label] = round(mw, 1)
                total_gen += mw

        # --- actual load: ONLY the "Actual Load" series' latest non-null ---
        lf = _http_json(f"{_SPP_BASE}/load-forecast/asChart", headers=hdrs)
        ldatasets = ((lf or {}).get("response") or {}).get("datasets") or []
        load = _spp_last_point(ldatasets, want_label="Actual Load")

        if total_gen <= 0 or load is None or load <= 0:
            return []
        return [_record("SPP", "SPP",
                        online_gen_mw=round(total_gen, 1),
                        load_mw=round(load, 1),
                        fuel_mix=fuel_mix,
                        source="spp:portal/gen-mix+load-forecast(Actual Load)")]
    except urllib.error.HTTPError as e:
        print(f"[iso_grid] SPP HTTP {e.code}", flush=True)
        return []
    except Exception as e:
        print(f"[iso_grid] SPP fetch error: {e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────────────────
# Credentialed ISOs we do NOT have keys for — PJM (key) & ISO-NE (basic).
# These FAIL CLOSED: they read their env var and, when absent, return an
# explicit {"source_unavailable": True, "needs": <ENV_VAR>} marker record.
# They NEVER fabricate a number. (ERCOT already has its own real auth path in
# fetch_ercot; these two are pure honest stubs until creds exist.)
# ─────────────────────────────────────────────────────────────────────
def _unavailable(iso: str, needs: str, base: str, note: str) -> dict:
    """Honest fail-closed marker — no numbers, names the real source + the
    exact env var required to enable it. Carries no telemetry fields."""
    return {
        "iso": iso, "zone": None,
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "online_gen_mw": None, "load_mw": None, "headroom_mw": None,
        "reserve_margin_pct": None, "fuel_mix": {},
        "source_unavailable": True, "needs": needs,
        "source": f"{iso.lower()}:credential-required",
        "endpoint": base, "note": note,
    }


def fetch_pjm() -> list[dict]:
    """PJM Data Miner 2 — requires PJM_API_KEY (Ocp-Apim-Subscription-Key).
    We have no key, so this FAILS CLOSED with an honest marker (never a number).
    Real source: https://api.pjm.com/api/v1 (instantaneous_dispatch_rates /
    gen_by_fuel + inst_load). Wire extraction here once PJM_API_KEY is set."""
    key = _env("PJM_API_KEY")
    base = ISO_REGISTRY["PJM"]["base"]
    # Owner decisions 2026-07-26: (1) NO direct DM2 serving without a PJM
    # Redistribution License; (2) gridstatus free tier = 250 req/MONTH —
    # a 15-min pull would burn ~5,760/mo, so gridstatus is BANNED from this
    # cadence path (it serves only the 6h-cached PJM-DOM depth in
    # pjm_dataminer). PRIMARY here = EIA-930 hourly (public domain, free,
    # redistribution-safe): net generation + demand for the PJM BA.
    # HONEST: hourly cadence, and EIA NG excludes imports (same documented
    # bias class as ISO-NE/MISO) — labeled in source, no invented offset.
    eia_key = _env("EIA_API_KEY")
    if eia_key:
        try:
            def _eia_latest(series_type):
                js = _http_json(
                    "https://api.eia.gov/v2/electricity/rto/region-data/data/"
                    f"?api_key={eia_key}&frequency=hourly&data[0]=value"
                    f"&facets[respondent][]=PJM&facets[type][]={series_type}"
                    "&sort[0][column]=period&sort[0][direction]=desc&length=1")
                rows = (((js or {}).get("response") or {}).get("data")) or []
                if rows:
                    try:
                        return float(rows[0].get("value")), rows[0].get("period")
                    except (TypeError, ValueError):
                        return None, None
                return None, None

            gen, _gp = _eia_latest("NG")
            load, _lp = _eia_latest("D")
            if gen and load and gen > 0 and load > 0:
                return [_record("PJM", "PJM", online_gen_mw=round(gen, 1),
                                load_mw=round(load, 1),
                                source="eia930_hourly")]
            print(f"[iso_grid] PJM EIA-930 parse empty (gen={gen} load={load});"
                  " trying DM2 fallback if keyed.", flush=True)
        except Exception as e:
            print(f"[iso_grid] PJM EIA-930 path failed: {str(e)[:120]}",
                  flush=True)
    if not key:
        return [_unavailable("PJM", "EIA_API_KEY|PJM_API_KEY", base,
                             "EIA-930 PJM feed unavailable and no DM2 key set "
                             "(DM2 also needs a redistribution license).")]
    # shell#35 follow-up (2026-07-26): extraction wired so the key going into
    # Railway env = instant live telemetry. Tolerant parse, FAIL-CLOSED.
    _hdr = {"Ocp-Apim-Subscription-Key": key, "Accept": "application/json"}
    try:
        gen_js = _http_json(
            base + "/gen_by_fuel?rowCount=50&startRow=1&format=json",
            headers=_hdr)
        load_js = _http_json(
            base + "/inst_load?rowCount=25&startRow=1&format=json",
            headers=_hdr)

        def _items(js):
            if isinstance(js, dict):
                return js.get("items") or js.get("Items") or []
            return js if isinstance(js, list) else []

        fuel_mix, gen = {}, 0.0
        for r in _items(gen_js):
            if not isinstance(r, dict):
                continue
            mw = None
            for k, v in r.items():
                if k.lower() in ("mw", "market_generation_mw", "gen_mw"):
                    try:
                        mw = float(v)
                    except (TypeError, ValueError):
                        mw = None
                    break
            if mw is None:
                continue
            cat = str(r.get("fuel_type") or r.get("fuel") or "other").lower()
            fuel_mix[cat] = round(fuel_mix.get(cat, 0.0) + mw, 1)
            gen += mw
        load = None
        for r in _items(load_js):
            if not isinstance(r, dict):
                continue
            area = str(r.get("area") or r.get("area_name") or "")
            for k, v in r.items():
                if "load" in k.lower() and k.lower().endswith("_mw"):
                    try:
                        cand = float(v)
                    except (TypeError, ValueError):
                        continue
                    if "RTO" in area.upper() or load is None:
                        load = cand
                    break
            if load is not None and "RTO" in area.upper():
                break
        if gen > 0 and load and load > 0:
            return [_record("PJM", "PJM", online_gen_mw=round(gen, 1),
                            load_mw=round(load, 1), fuel_mix=fuel_mix,
                            source="pjm_dataminer2")]
        return [_unavailable("PJM", "PJM_API_KEY", base,
                             "PJM responded but gen/load parse was empty "
                             "(no fabricated data).")]
    except Exception as e:
        print(f"[iso_grid] PJM extraction failed: {str(e)[:120]}", flush=True)
        return [_unavailable("PJM", "PJM_API_KEY", base,
                             f"PJM fetch error: {type(e).__name__}")]


def fetch_isone() -> list[dict]:
    """ISO-NE Web Services — requires ISONE_USERNAME + ISONE_PASSWORD (HTTP
    basic). We have no credentials, so this FAILS CLOSED with an honest marker
    (never a number). Real source: https://webservices.iso-ne.com/api/v1.1
    (/genfuelmix/current + /fiveminutesystemload/current)."""
    user, pw = _env("ISONE_USERNAME"), _env("ISONE_PASSWORD")
    base = ISO_REGISTRY["ISONE"]["base"]
    if not (user and pw):
        return [_unavailable("ISONE", "ISONE_USERNAME+ISONE_PASSWORD", base,
                             "ISO-NE Web Services requires HTTP basic auth.")]
    # shell#35 follow-up (2026-07-26): owner registered ISO Express creds —
    # real extraction. HTTP basic; JSON via .json suffix. Tolerant parse,
    # FAIL-CLOSED to the honest marker if either feed can't be read.
    import base64 as _b64
    _auth = {"Authorization": "Basic " + _b64.b64encode(
        f"{user}:{pw}".encode()).decode(), "Accept": "application/json"}

    def _find_rows(obj, key):
        """Depth-first: first list of dicts whose members carry `key`."""
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and key in obj[0]:
                return obj
            for it in obj:
                r = _find_rows(it, key)
                if r:
                    return r
        elif isinstance(obj, dict):
            if key in obj:
                return [obj]
            for v in obj.values():
                r = _find_rows(v, key)
                if r:
                    return r
        return None

    try:
        mix_js = _http_json(base + "/genfuelmix/current.json", headers=_auth)
        load_js = _http_json(base + "/fiveminutesystemload/current.json",
                             headers=_auth)
        mix_rows = _find_rows(mix_js, "GenMw") or []
        load_rows = _find_rows(load_js, "LoadMw") or []
        fuel_mix, gen = {}, 0.0
        for r in mix_rows:
            try:
                mw = float(r.get("GenMw"))
            except (TypeError, ValueError):
                continue
            cat = str(r.get("FuelCategory") or r.get("FuelCategoryRollup")
                      or "other").strip().lower()
            fuel_mix[cat] = round(fuel_mix.get(cat, 0.0) + mw, 1)
            gen += mw
        load = None
        if load_rows:
            try:
                load = float(load_rows[0].get("LoadMw"))
            except (TypeError, ValueError):
                load = None
        if gen > 0 and load and load > 0:
            return [_record("ISONE", "ISONE", online_gen_mw=round(gen, 1),
                            load_mw=round(load, 1), fuel_mix=fuel_mix,
                            source="isone_webservices")]
        return [_unavailable("ISONE", "ISONE_USERNAME+ISONE_PASSWORD", base,
                             "ISO-NE responded but gen/load parse was empty "
                             "(no fabricated data).")]
    except Exception as e:
        print(f"[iso_grid] ISO-NE extraction failed: {str(e)[:120]}",
              flush=True)
        return [_unavailable("ISONE", "ISONE_USERNAME+ISONE_PASSWORD", base,
                             f"ISO-NE fetch error: {type(e).__name__}")]


# Dispatch table — maps impl names to functions.
_IMPL = {"fetch_ercot": fetch_ercot,
         "fetch_nyiso": fetch_nyiso,
         "fetch_caiso": fetch_caiso,
         "fetch_miso": fetch_miso,
         "fetch_spp": fetch_spp,
         "fetch_pjm": fetch_pjm,
         "fetch_isone": fetch_isone}


# Adapters that FAIL CLOSED with an honest marker even when creds are absent
# (so the missing-credential signal is visible to probes, never silent).
_FAIL_CLOSED_IMPLS = {"fetch_pjm", "fetch_isone"}


def fetch_iso(iso: str) -> list[dict]:
    """Fetch one ISO's telemetry. Returns [] for unimplemented/credless. Safe.
    Fail-closed adapters still run without creds so they can surface their
    honest {"source_unavailable": True, "needs": ...} marker."""
    cfg = ISO_REGISTRY.get(iso)
    if not cfg:
        return []
    impl = cfg.get("impl") or ""
    if not _has_creds(iso) and impl not in _FAIL_CLOSED_IMPLS:
        return []
    fn = _IMPL.get(impl)
    if not fn:
        return []   # registered but not yet implemented
    try:
        return fn() or []
    except Exception as e:
        print(f"[iso_grid] {iso} fetch_iso error: {e}", flush=True)
        return []


def run_all() -> dict:
    """Cron entrypoint: pull every ISO whose creds are present, store snapshots.
    Returns a summary. Never raises."""
    ensure_schema()
    summary = {"ran": [], "stored": 0, "skipped": []}
    for iso in ISO_REGISTRY:
        if not _has_creds(iso):
            summary["skipped"].append(iso)
            continue
        recs = fetch_iso(iso)
        if recs:
            summary["stored"] += store_records(recs)
            summary["ran"].append({"iso": iso, "records": len(recs)})
        else:
            summary["ran"].append({"iso": iso, "records": 0})
    return summary


def status() -> dict:
    """Which ISOs are configured/implemented — for an admin probe."""
    return {iso: {"implemented": bool(_IMPL.get(cfg.get("impl") or "")),
                  "creds_present": _has_creds(iso),
                  "auth": cfg.get("auth"), "note": cfg.get("note")}
            for iso, cfg in ISO_REGISTRY.items()}


# ─────────────────────────────────────────────────────────────────────
# Probe blueprint — lets us confirm auth + discover the right Data Product
# WITHOUT shipping secrets. Returns only public ERCOT catalog metadata
# (names/ids/descriptions). Cached so repeated hits don't hammer ERCOT.
# ─────────────────────────────────────────────────────────────────────
try:
    from flask import Blueprint, jsonify, request
    iso_grid_bp = Blueprint("iso_grid", __name__)

    _PROBE_CACHE: dict = {}

    def _ercot_product_catalog(limit: int = 400, fresh: bool = False) -> dict:
        """List ERCOT public Data Products our key can see. Proves auth +
        surfaces reportTypeIds so we can pick the gen/load product. Safe:
        no key/token in the response. 5-min cache (bypass with fresh=True)."""
        import time as _t
        now = _t.time()
        if not fresh:
            hit = _PROBE_CACHE.get("ercot")
            if hit and (now - hit[0]) < 300:
                return hit[1]
        # Raw vs stripped length lets us detect trailing whitespace in the
        # Railway env var WITHOUT ever revealing the key itself.
        raw = os.environ.get("ERCOT_API_KEY") or ""
        key = raw.strip()
        out: dict = {"auth": "unknown", "products": [], "count": 0,
                     "key_present": bool(key),
                     "key_len": len(key),
                     "key_had_whitespace": (len(raw) != len(key))}
        if not key:
            out["auth"] = "no_api_key"
            return out
        bearer = _ercot_bearer()
        out["bearer_obtained"] = bool(bearer)
        out["bearer_error"] = _LAST_BEARER_ERR
        headers = {"Ocp-Apim-Subscription-Key": key}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            data = _http_json(f"{ISO_REGISTRY['ERCOT']['base']}/", headers=headers)
            # public-reports catalog shape: {"_embedded":{"products":[...]}}
            items = data if isinstance(data, list) else (
                ((data.get("_embedded") or {}).get("products"))
                or data.get("products") or data.get("data") or [])
            prods = []
            for p in (items or [])[:limit]:
                if not isinstance(p, dict):
                    continue
                # gen/load-relevant products bubble to the top for easy picking
                prods.append({
                    "emilId": p.get("emilId"),
                    "reportTypeId": p.get("reportTypeId"),
                    "name": p.get("name"),
                    "generationFrequency": p.get("generationFrequency"),
                    "description": (p.get("description") or "")[:160],
                })
            kw = ("gen", "load", "system condition", "fuel", "capacity", "reserve")
            prods.sort(key=lambda x: 0 if any(
                k in ((x.get("name") or "") + (x.get("description") or "")).lower()
                for k in kw) else 1)
            out.update(auth="ok", products=prods, count=len(prods))
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode("utf-8", "ignore")[:240]
            except Exception: pass
            out.update(auth=f"http_{e.code}", api_error=body,
                       hint="401 'invalid subscription key' → ERCOT_API_KEY wrong; "
                            "401 token/audience → ERCOT_SCOPE wrong; "
                            "403 → not subscribed to the product")
        except Exception as e:
            out.update(auth="error", detail=str(e)[:160])
        _PROBE_CACHE["ercot"] = (now, out)
        return out

    @iso_grid_bp.get("/api/v1/iso/status")
    def _iso_status():
        return jsonify(as_of=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       isos=status()), 200

    @iso_grid_bp.get("/api/v1/iso/probe/ercot")
    def _iso_probe_ercot():
        fresh = request.args.get("fresh") in ("1", "true", "yes")
        return jsonify(_ercot_product_catalog(fresh=fresh)), 200

    @iso_grid_bp.post("/api/v1/iso/pull")
    def _iso_pull():
        """Pull + STORE telemetry for every credentialed ISO. Admin-gated
        (X-Admin-Key == BRAIN_ADMIN_KEY). Meant to be driven by a scheduler
        cron, not hot paths. Uses its own short-lived psycopg2 connection
        (isolated from the app pool), and the public ISOs return ~1 row each,
        so it's nowhere near the bulk-loader pattern that caused pool issues."""
        import hmac as _hmac
        # Accept the system-standard admin key (what every other cron uses)
        # OR the operator BRAIN_ADMIN_KEY — whichever is configured.
        candidates = [os.environ.get(k, "") for k in
                      ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "BRAIN_ADMIN_KEY")]
        candidates = [c for c in candidates if c]
        provided = request.headers.get("X-Admin-Key", "")
        if not candidates:
            return jsonify(ok=False, error="no admin key configured"), 503
        if not (provided and any(_hmac.compare_digest(provided, c) for c in candidates)):
            return jsonify(ok=False, error="admin auth required"), 401
        try:
            summary = run_all()
            return jsonify(ok=True, **summary), 200
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:200]), 500

    @iso_grid_bp.get("/api/v1/iso/sample/<iso>")
    def _iso_sample(iso):
        """Read-only live pull for one ISO — proves an adapter works WITHOUT
        writing to the DB (no store_records). 60s cache to avoid hammering the
        ISO. Public ISOs (NYISO, CAISO) need no creds."""
        import time as _t
        iso = (iso or "").upper()
        if iso not in ISO_REGISTRY:
            return jsonify(ok=False, error="unknown ISO",
                           known=list(ISO_REGISTRY)), 404
        ck = f"sample:{iso}"
        now = _t.time()
        hit = _PROBE_CACHE.get(ck)
        if hit and (now - hit[0]) < 60:
            return jsonify(hit[1]), 200
        recs = fetch_iso(iso)
        out = {
            "ok": True, "iso": iso,
            "implemented": bool(_IMPL.get((ISO_REGISTRY[iso].get("impl") or ""))),
            "creds_present": _has_creds(iso),
            "records": recs, "count": len(recs),
            "note": ISO_REGISTRY[iso].get("note"),
        }
        _PROBE_CACHE[ck] = (now, out)
        return jsonify(out), 200
except Exception as _bp_err:  # Flask unavailable in some contexts — stay importable
    iso_grid_bp = None
    print(f"[iso_grid] blueprint skipped: {_bp_err}", flush=True)


if __name__ == "__main__":
    import pprint
    pprint.pprint(status())
