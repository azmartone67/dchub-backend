"""THE single gridstatus.io HTTP client — every provider call goes through here.

2026-07-31: api.gridstatus.io returned 403 "API requests limit reached.
Usage: 375, Limit: 250" while the internal gridstatus_call_ledger showed only
45 calls for the month. The other ~330 came from clients that carried their own
HTTP path and never consulted the ledger:
  * routes/grid_data_master_shell._gs_get — driven 11-22x/day by the every-5-min
    heartbeat (GH cron-heartbeat.yml + the worker's in-process self-heartbeat)
    re-firing the daily tick across its whole hour==11 window
  * enhancements/iso_integrations.GridStatusClient — pro-gated /api/grid/* routes

Rules (shell#35 WS8; owner directive 2026-07-26 "spend the free 250 wisely"):
  * increment-before-request: the pg ledger row for the current month is bumped
    BEFORE any HTTP is attempted, and the call is REFUSED once the count
    exceeds GRIDSTATUS_MONTHLY_BUDGET (default 200; provider free tier = 250).
  * do NOT raise the budget and do NOT add a caller that talks to
    api.gridstatus.io directly — tests/test_gridstatus_single_client.py fences
    the provider-host literal to this module.
  * budget_exhausted / http_403 are surfaced loudly (stdout + the error string
    returned to the caller), never swallowed.
"""

import os
from datetime import datetime, timezone

import requests

GRIDSTATUS_BASE = "https://api.gridstatus.io/v1"
MONTHLY_BUDGET = int(os.environ.get("GRIDSTATUS_MONTHLY_BUDGET", "200"))
UA = "DCHub-GridStatus/1.0 (+https://dchub.cloud)"

BUDGET_EXHAUSTED = ("budget_exhausted: GRIDSTATUS_MONTHLY_BUDGET "
                    f"({MONTHLY_BUDGET}/mo) reached — spend the free "
                    "250 wisely (owner directive 2026-07-26)")


def gridstatus_key() -> str:
    return (os.environ.get("GRIDSTATUS_API_KEY") or "").strip()


# Month ("YYYY-MM") this process already saw refused. The budget only resets
# on the 1st, so once it is spent there is nothing to ask the ledger until then.
_EXHAUSTED_MONTH = None


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _budget_spend() -> bool:
    """Increment-before-request ledger: one gridstatus_call_ledger row per
    month, bumped before the HTTP attempt so every consumer is counted (a call
    that then fails still spent a provider request). Fail-OPEN on DB trouble —
    a ledger outage must not kill the feed; the vendor-side 250 cap is the
    true backstop.

    2026-09-25: the row counts PROVIDER REQUESTS only. It used to be bumped on
    every attempt, refused ones included, so September read 1,543 against a
    200 budget while real requests stopped at 200 (the PJM-DOM lookup kept
    retrying after the budget was spent). The bump is now conditional on being
    under budget, and a refusal is remembered in-process for the month so a
    spent budget costs no further DB round trips."""
    global _EXHAUSTED_MONTH
    month = _month()
    if _EXHAUSTED_MONTH == month:
        return False
    try:
        import psycopg2
        db = os.environ.get("DATABASE_URL")
        if not db:
            return True
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=4)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gridstatus_call_ledger (
                        month TEXT PRIMARY KEY, calls INT NOT NULL DEFAULT 0)
                """)
                cur.execute("""
                    INSERT INTO gridstatus_call_ledger (month, calls)
                    VALUES (%s, 1)
                    ON CONFLICT (month) DO UPDATE
                      SET calls = gridstatus_call_ledger.calls + 1
                      WHERE gridstatus_call_ledger.calls < %s
                    RETURNING calls
                """, (month, MONTHLY_BUDGET))
                row = cur.fetchone()
            conn.commit()
            if row is None or int(row[0]) > MONTHLY_BUDGET:
                _EXHAUSTED_MONTH = month
                return False
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return True


def gs_request(path, params=None, timeout=15, caller="unknown", api_key=None):
    """GET one api.gridstatus.io path (e.g. "/datasets/pjm_load/query").
    Returns (payload, error_str): payload is the decoded JSON on success,
    error_str is None on success or a machine-readable marker
    ("budget_exhausted: ...", "http_403", ...) on failure.

    THE chokepoint: consults the ledger before any HTTP; budget refusals and
    provider-quota 403s are printed loudly, never swallowed."""
    global _EXHAUSTED_MONTH
    key = (api_key or "").strip() or gridstatus_key()
    if not key:
        return None, "source_unavailable: GRIDSTATUS_API_KEY not set"
    if not _budget_spend():
        print(f"[gridstatus] REFUSED caller={caller} path={path} — "
              f"{BUDGET_EXHAUSTED}", flush=True)
        return None, BUDGET_EXHAUSTED
    # GridStatus reads x-api-key (verified 2026-09-06: "Missing API Key." ->
    # "Invalid API key."). A query key is logged by every proxy it crosses.
    p = {}
    if params:
        p.update(params)
    import time as _t
    for _attempt in range(2):  # free tier = 1 req/sec; retry once on 429
        try:
            r = requests.get(GRIDSTATUS_BASE + path, params=p, timeout=timeout,
                             headers={"Accept": "application/json",
                                      "User-Agent": UA,
                                      "x-api-key": key})
            if r.status_code == 429 and _attempt == 0:
                _t.sleep(1.1)
                continue
            if r.status_code == 403:
                _EXHAUSTED_MONTH = _month()   # provider quota: no point retrying until the 1st
                print(f"[gridstatus] http_403 caller={caller} path={path} — "
                      "provider monthly quota exhausted (resets on the 1st); "
                      "the internal ledger stays authoritative", flush=True)
                return None, "http_403"
            if r.status_code >= 400:
                return None, f"http_{r.status_code}"
            return r.json(), None
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:120]}"
    return None, "http_429"


def _shared_cache(key, rows=None, ttl_s=0):
    """Postgres-backed cache every worker and replica shares.

    Read (rows is None): the unexpired rows for key, or None. Write: store rows
    for ttl_s seconds. Fail-soft both ways — a cache outage means a normal
    ledgered request, never a failed answer.

    Why it exists (2026-09-25): the PJM-DOM lookup cached its answer for 6h in
    an in-process dict, so every gunicorn worker on every replica paid for its
    own copy, and the 200/month budget was gone by 2026-09-09 at the latest.
    Measured in grid_ext_metrics the same day: none of the grid-data shell's
    allowlisted PJM datasets has ingested since 2026-07-20."""
    try:
        import json
        import psycopg2
        db = os.environ.get("DATABASE_URL")
        if not db:
            return None
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=4)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gridstatus_shared_cache (
                        cache_key  TEXT PRIMARY KEY,
                        rows_json  JSONB NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL)
                """)
                if rows is None:
                    cur.execute("SELECT rows_json FROM gridstatus_shared_cache "
                                "WHERE cache_key = %s AND expires_at > NOW()", (key,))
                    hit = cur.fetchone()
                    conn.commit()
                    return hit[0] if hit else None
                cur.execute("""
                    INSERT INTO gridstatus_shared_cache (cache_key, rows_json, expires_at)
                    VALUES (%s, %s::jsonb, NOW() ON CONFLICT DO NOTHING + make_interval(secs => %s))
                    ON CONFLICT (cache_key) DO UPDATE
                      SET rows_json = EXCLUDED.rows_json, expires_at = EXCLUDED.expires_at
                """, (key, json.dumps(rows, default=str), int(ttl_s)))
            conn.commit()
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None


def gridstatus_get(dataset, params=None, timeout=15, caller="unknown", shared_ttl_s=0):
    """GET one dataset /query. Returns (rows_list, error_str) — the contract
    every ledgered consumer speaks (pjm_dataminer, grid_data_master_shell).

    shared_ttl_s > 0: answer from the cross-worker cache while it is fresh, and
    store a successful answer for that long. Only successes are stored, so an
    error is never served from cache."""
    key = None
    if shared_ttl_s and shared_ttl_s > 0:
        import json
        key = dataset + "|" + json.dumps(params or {}, sort_keys=True, default=str)
        hit = _shared_cache(key)
        if hit is not None:
            return hit, None
    payload, err = gs_request("/datasets/" + dataset + "/query",
                              params, timeout=timeout, caller=caller)
    if err:
        return None, err
    rows = payload.get("data") if isinstance(payload, dict) else payload
    rows = rows or []
    if key is not None and rows:
        _shared_cache(key, rows=rows, ttl_s=shared_ttl_s)
    return rows, None
