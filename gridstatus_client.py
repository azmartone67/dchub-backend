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


def _budget_spend() -> bool:
    """Increment-before-request ledger: one gridstatus_call_ledger row per
    month, bumped before the HTTP attempt so every consumer is counted (a call
    that then fails still spent a provider request). Fail-OPEN on DB trouble —
    a ledger outage must not kill the feed; the vendor-side 250 cap is the
    true backstop."""
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
                    RETURNING calls
                """, (datetime.now(timezone.utc).strftime("%Y-%m"),))
                n = int(cur.fetchone()[0])
            conn.commit()
            return n <= MONTHLY_BUDGET
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


def gridstatus_get(dataset, params=None, timeout=15, caller="unknown"):
    """GET one dataset /query. Returns (rows_list, error_str) — the contract
    every ledgered consumer speaks (pjm_dataminer, grid_data_master_shell)."""
    payload, err = gs_request("/datasets/" + dataset + "/query",
                              params, timeout=timeout, caller=caller)
    if err:
        return None, err
    rows = payload.get("data") if isinstance(payload, dict) else payload
    return (rows or []), None
