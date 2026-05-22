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
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error


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
                try:
                    cur.execute("""
                        INSERT INTO grid_telemetry
                            (iso, zone, observed_at, online_gen_mw, load_mw,
                             headroom_mw, reserve_margin_pct, fuel_mix, source)
                        VALUES (%s,%s,COALESCE(%s,NOW()),%s,%s,%s,%s,%s::jsonb,%s)
                    """, (
                        r.get("iso"), r.get("zone"), r.get("observed_at"),
                        r.get("online_gen_mw"), r.get("load_mw"),
                        r.get("headroom_mw"), r.get("reserve_margin_pct"),
                        json.dumps(r.get("fuel_mix") or {}), r.get("source"),
                    ))
                    n += 1
                except Exception:
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


# ─────────────────────────────────────────────────────────────────────
# Per-ISO registry — base URL, auth model, env-var prefix, status
# ─────────────────────────────────────────────────────────────────────
# auth: "oauth_key" (ERCOT — bearer + subscription key) | "key" (header key)
#       | "basic" (user/pass) | "public" (no auth)
ISO_REGISTRY = {
    "ERCOT": {"auth": "oauth_key", "base": "https://api.ercot.com/api/public-data",
              "env": "ERCOT", "impl": "fetch_ercot",
              "note": "Azure-APIM: Ocp-Apim-Subscription-Key + B2C ROPC bearer."},
    "CAISO": {"auth": "public", "base": "http://oasis.caiso.com/oasisapi/SingleZip",
              "env": "CAISO", "impl": None,
              "note": "OASIS API — public, zip/XML by queryname (e.g. ENE_SLRS)."},
    "PJM":   {"auth": "key", "base": "https://api.pjm.com/api/v1",
              "env": "PJM", "impl": None,
              "note": "Data Miner 2 — needs Ocp-Apim-Subscription-Key."},
    "MISO":  {"auth": "public", "base": "https://api.misoenergy.org/MISORTWD",
              "env": "MISO", "impl": None,
              "note": "Real-time web display JSON feeds — public."},
    "SPP":   {"auth": "public", "base": "https://portal.spp.org/file-browser-api",
              "env": "SPP", "impl": None,
              "note": "Marketplace portal CSV — public, path-based."},
    "NYISO": {"auth": "public", "base": "http://mis.nyiso.com/public/csv",
              "env": "NYISO", "impl": None,
              "note": "Public CSV by report (e.g. /pal, /rtfuelmix) — no auth."},
    "ISONE": {"auth": "basic", "base": "https://webservices.iso-ne.com/api/v1.1",
              "env": "ISONE", "impl": None,
              "note": "Web Services — HTTP basic auth (account user/pass)."},
}


def _has_creds(iso: str) -> bool:
    cfg = ISO_REGISTRY.get(iso, {})
    auth, p = cfg.get("auth"), cfg.get("env", iso)
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


def fetch_ercot() -> list[dict]:
    """Pull ERCOT generation/load → normalized records. Fail-safe.

    TODO (needs live key to confirm): pick the exact Data Product reportTypeId
    for real-time generation + system load by zone via
    `GET {base}/` (lists products), then download the artifact CSV and map
    rows → _record(...). Until ERCOT_GEN_PRODUCT_ID is set + verified, this
    lists products (proves auth works) and returns []."""
    key = _env("ERCOT_API_KEY")
    if not key:
        return []
    bearer = _ercot_bearer()
    headers = {"Ocp-Apim-Subscription-Key": key}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    base = ISO_REGISTRY["ERCOT"]["base"]
    try:
        products = _http_json(f"{base}/", headers=headers)
        # Proof-of-auth: we can list products. Real extraction is wired once
        # ERCOT_GEN_PRODUCT_ID (the gen/load reportTypeId) is confirmed.
        product_id = _env("ERCOT_GEN_PRODUCT_ID")
        if not product_id:
            print(f"[iso_grid] ERCOT auth OK — "
                  f"{len(products) if isinstance(products, list) else '?'} products "
                  f"visible. Set ERCOT_GEN_PRODUCT_ID to begin extraction.",
                  flush=True)
            return []
        # ── extraction stub (fill once product_id confirmed) ──
        # arts = _http_json(f"{base}/{product_id}", headers=headers)
        # download artifact CSV → parse → records.append(_record("ERCOT", zone, ...))
        return []
    except urllib.error.HTTPError as e:
        print(f"[iso_grid] ERCOT HTTP {e.code} (check key/bearer/scope)", flush=True)
        return []
    except Exception as e:
        print(f"[iso_grid] ERCOT fetch error: {e}", flush=True)
        return []


# Dispatch table — maps impl names to functions.
_IMPL = {"fetch_ercot": fetch_ercot}


def fetch_iso(iso: str) -> list[dict]:
    """Fetch one ISO's telemetry. Returns [] for unimplemented/credless. Safe."""
    cfg = ISO_REGISTRY.get(iso)
    if not cfg or not _has_creds(iso):
        return []
    fn = _IMPL.get(cfg.get("impl") or "")
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
    from flask import Blueprint, jsonify
    iso_grid_bp = Blueprint("iso_grid", __name__)

    _PROBE_CACHE: dict = {}

    def _ercot_product_catalog(limit: int = 400) -> dict:
        """List ERCOT public Data Products our key can see. Proves auth +
        surfaces reportTypeIds so we can pick the gen/load product. Safe:
        no key/token in the response. 5-min cache."""
        import time as _t
        now = _t.time()
        hit = _PROBE_CACHE.get("ercot")
        if hit and (now - hit[0]) < 300:
            return hit[1]
        key = _env("ERCOT_API_KEY")
        out: dict = {"auth": "unknown", "products": [], "count": 0}
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
            items = data if isinstance(data, list) else (data.get("products") or data.get("data") or [])
            prods = []
            for p in (items or [])[:limit]:
                if not isinstance(p, dict):
                    continue
                # gen/load-relevant products bubble to the top for easy picking
                prods.append({
                    "productId": p.get("productId"),
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
        return jsonify(_ercot_product_catalog()), 200
except Exception as _bp_err:  # Flask unavailable in some contexts — stay importable
    iso_grid_bp = None
    print(f"[iso_grid] blueprint skipped: {_bp_err}", flush=True)


if __name__ == "__main__":
    import pprint
    pprint.pprint(status())
