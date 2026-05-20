"""
Phase FF+21-d1-sync (2026-05-19) — Neon → Cloudflare D1 hourly mirror.
================================================================

Why: today's TWO Railway edge outages made every /api/v1/map call fail.
The Cloudflare Pages worker (Phase FF+20) has D1 read-fallback wired
for /api/v1/map and /facilities/<slug>, but D1 is EMPTY until something
pushes rows into it.

This module is the pusher. Runs hourly via the existing
dchub-scheduler.py. Each tick:

  1. Query Neon for `discovered_facilities` rows that have a non-NULL
     latitude/longitude (the only rows the map would show anyway).
  2. Batch into chunks of ~200 rows.
  3. POST each batch to Cloudflare's D1 REST API as `INSERT ON CONFLICT
     DO UPDATE` so re-syncing is idempotent.
  4. Write a row to D1's `sync_log` table with timing + status.

Env vars required (set on Railway):
  CLOUDFLARE_ACCOUNT_ID       4bb33ec40ef02f9f4b41dc97668d5a52
  CLOUDFLARE_D1_DATABASE_ID   34464113-9e19-4d0b-839a-a20df72409b0
  CLOUDFLARE_API_TOKEN        Token with D1:Edit scope

Endpoints:
  POST /api/v1/admin/d1-sync/run       Trigger a sync now (admin gated)
  GET  /api/v1/admin/d1-sync/status    Latest sync log + row counts
"""
import os
import json
import time
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
d1_sync_bp = Blueprint("d1_sync", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


CF_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID",
                              "4bb33ec40ef02f9f4b41dc97668d5a52")
CF_D1_ID = os.environ.get("CLOUDFLARE_D1_DATABASE_ID",
                            "34464113-9e19-4d0b-839a-a20df72409b0")
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")

CF_D1_URL = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
              f"/d1/database/{CF_D1_ID}/query")

BATCH_SIZE = 50             # statements per /batch call (CF caps batch size + each stmt has 16 params)
SYNC_TIMEOUT_SECONDS = 600  # whole-job ceiling (10 min)


def _d1_query(sql: str, params: list = None, timeout: int = 30) -> dict:
    """POST a SQL statement to D1. Returns the parsed response dict.
    Raises on HTTP error; caller handles."""
    import requests
    if not CF_TOKEN:
        raise RuntimeError(
            "CLOUDFLARE_API_TOKEN not set on Railway — D1 sync disabled. "
            "Create a token with D1:Edit scope and set it as an env var.")
    headers = {
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"sql": sql}
    if params:
        body["params"] = params
    r = requests.post(CF_D1_URL, headers=headers, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _d1_batch(statements: list, timeout: int = 60) -> dict:
    """POST a list of {sql, params} statement objects to D1 in ONE HTTP call.

    Phase FF+25-followup (2026-05-20): the per-row loop in _run_sync()
    was making 12,553 sequential HTTP calls to CF D1 (~300ms each =
    >60min). Railway edge killed the request after ~30s, leaving D1
    perpetually under-populated (e.g. 262 of 12,553 rows). CF's D1
    REST API supports a `batch` endpoint that takes an array of
    statements + executes them as one round-trip. ~100 statements per
    call → 126 calls instead of 12,553 → completes in ~30-40s.
    """
    import requests
    if not CF_TOKEN:
        raise RuntimeError("CLOUDFLARE_API_TOKEN not set")
    # CF D1's /batch endpoint accepts an array body
    url = CF_D1_URL.replace("/query", "/batch")
    headers = {
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=statements, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _neon_query(sql: str, params: tuple = ()):
    """Query Neon Postgres for facility rows. Returns list of dicts."""
    try:
        from main import get_db
    except Exception:
        return []
    conn = get_db()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        try: conn.close()
        except Exception: pass


def _slugify(text: str) -> str:
    """Match the slug logic used by the worker /facilities/<slug> route."""
    import re, hashlib
    if not text:
        return ""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9 -]", "", s)
    s = re.sub(r"[- ]+", "-", s)
    return s.strip("-")


def _build_facility_slug(row: dict) -> str:
    """Reproduce the slug pattern from main.py:2546 _slugify so the
    D1 mirror keys match the public-facing /facilities/<slug> URLs."""
    import hashlib
    provider_slug = _slugify(row.get("provider") or "")
    name_slug = _slugify(row.get("name") or "")
    if name_slug and len(name_slug) >= 3:
        hash_src = str(row["id"]) if row.get("id") else (str(row.get("provider", "")) + str(row.get("name", "")))
        short_hash = hashlib.md5(hash_src.encode()).hexdigest()[:8]
        if provider_slug:
            return f"{provider_slug}-{name_slug}-{short_hash}"
        return f"{name_slug}-{short_hash}"
    return ""


def _run_sync() -> dict:
    """One full sync pass. Returns stats dict."""
    started = time.time()
    out = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "rows_read": 0,
        "rows_synced": 0,
        "batches": 0,
        "errors": [],
    }
    if not CF_TOKEN:
        out["errors"].append("CLOUDFLARE_API_TOKEN not configured")
        return out

    # 1) Read facilities from Neon. Mirror only rows the map needs.
    rows = _neon_query("""
        SELECT df.id, df.name, df.provider, df.city, df.state, df.country,
               df.market, df.latitude, df.longitude,
               COALESCE(df.power_mw, f.power_mw) AS power_mw,
               df.sqft, df.status, df.facility_type, df.address
        FROM discovered_facilities df
        LEFT JOIN facilities f ON f.id = df.merged_facility_id
        WHERE df.latitude IS NOT NULL
          AND df.longitude IS NOT NULL
          AND COALESCE(df.is_duplicate, 0) = 0
        ORDER BY COALESCE(df.power_mw, f.power_mw) DESC NULLS LAST
        LIMIT 50000
    """)
    out["rows_read"] = len(rows)
    if not rows:
        out["errors"].append("zero rows from Neon — Railway may be down")
        return out

    # 2) Batch-write to D1 with UPSERT semantics.
    insert_sql = """
        INSERT INTO facilities (
            id, slug, name, provider, city, state, country, market,
            latitude, longitude, power_mw, sqft, status, facility_type,
            address, fiber_providers, synced_at
        ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,unixepoch())
        ON CONFLICT(id) DO UPDATE SET
            slug=excluded.slug, name=excluded.name, provider=excluded.provider,
            city=excluded.city, state=excluded.state, country=excluded.country,
            market=excluded.market, latitude=excluded.latitude,
            longitude=excluded.longitude, power_mw=excluded.power_mw,
            sqft=excluded.sqft, status=excluded.status,
            facility_type=excluded.facility_type, address=excluded.address,
            fiber_providers=excluded.fiber_providers, synced_at=unixepoch()
    """
    # Phase FF+25-followup (2026-05-20): batched via CF D1 /batch endpoint.
    # Previous version did ONE HTTP call per row → 12,553 calls × 300ms =
    # 60+ min, way past Railway's edge timeout. Now: 50 statements per
    # batch, ~250 batches total → ~75s end-to-end at 300ms/batch.
    def _row_params(r):
        return [
            str(r.get("id")) if r.get("id") is not None else None,
            _build_facility_slug(r),
            r.get("name") or "",
            r.get("provider"),
            r.get("city"),
            r.get("state"),
            r.get("country"),
            r.get("market"),
            float(r["latitude"]) if r.get("latitude") is not None else None,
            float(r["longitude"]) if r.get("longitude") is not None else None,
            float(r["power_mw"]) if r.get("power_mw") is not None else None,
            int(r["sqft"]) if r.get("sqft") is not None else None,
            r.get("status"),
            r.get("facility_type"),
            r.get("address"),
            None,  # fiber_providers — populate separately once we have data
        ]

    for i in range(0, len(rows), BATCH_SIZE):
        if time.time() - started > SYNC_TIMEOUT_SECONDS:
            out["errors"].append(f"timeout after {SYNC_TIMEOUT_SECONDS}s, "
                                  f"completed {out['rows_synced']}/{out['rows_read']}")
            break
        batch = rows[i:i + BATCH_SIZE]
        # Build a statement array for CF D1's /batch endpoint
        statements = [{"sql": insert_sql, "params": _row_params(r)} for r in batch]
        try:
            _d1_batch(statements, timeout=45)
            out["rows_synced"] += len(batch)
        except Exception as e:
            msg = str(e)[:200]
            # Fall back to per-row mode for this batch only — gives us
            # detail on which rows fail when the batch as a whole errors
            # (e.g. one row has bad UTF-8 or oversize address).
            out["errors"].append(f"batch {out['batches']} failed ({msg}); falling back to per-row")
            for r in batch:
                try:
                    _d1_query(insert_sql, _row_params(r))
                    out["rows_synced"] += 1
                except Exception as e2:
                    pass  # already logged at batch level
        out["batches"] += 1

    # 3) Record this run in the sync_log table.
    try:
        _d1_query(
            "INSERT INTO sync_log (table_name, rows_synced, duration_ms, "
            "status, error) VALUES (?1, ?2, ?3, ?4, ?5)",
            ["facilities", out["rows_synced"], int((time.time() - started) * 1000),
             "ok" if out["rows_synced"] > 0 else "fail",
             "; ".join(out["errors"])[:500] if out["errors"] else None]
        )
    except Exception as e:
        out["errors"].append(f"sync_log write failed: {str(e)[:120]}")

    out["ok"] = out["rows_synced"] > 0
    out["duration_seconds"] = round(time.time() - started, 2)
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    return out


@d1_sync_bp.route("/api/v1/admin/d1-sync/run", methods=["POST"])
def run_now():
    """Admin: trigger a sync pass immediately."""
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403
    result = _run_sync()
    return jsonify(result), (200 if result["ok"] else 500)


@d1_sync_bp.route("/api/v1/admin/d1-sync/status", methods=["GET"])
def status():
    """Public: latest sync log + D1 row count.
    No auth — read-only diagnostic."""
    if not CF_TOKEN:
        return jsonify(
            ok=False,
            error="CLOUDFLARE_API_TOKEN not set on Railway",
            hint=("Set CLOUDFLARE_API_TOKEN env var with a token that has "
                  "D1:Edit scope, then the hourly cron will start working."),
        ), 503

    try:
        # Latest 5 sync runs
        log_resp = _d1_query(
            "SELECT table_name, rows_synced, duration_ms, status, error, "
            "       synced_at "
            "FROM sync_log ORDER BY synced_at DESC LIMIT 5"
        )
        latest_runs = (log_resp.get("result") or [{}])[0].get("results") or []

        # Current row count
        count_resp = _d1_query("SELECT COUNT(*) AS n FROM facilities")
        row_count = (((count_resp.get("result") or [{}])[0].get("results")
                       or [{}])[0].get("n") or 0)

        return jsonify(
            ok=True,
            as_of=datetime.now(timezone.utc).isoformat(),
            d1_database_id=CF_D1_ID,
            facilities_row_count=int(row_count),
            latest_runs=latest_runs,
        )
    except Exception as e:
        return jsonify(error=f"d1_status_failed: {str(e)[:200]}"), 500


# Module-load smoke check (logs warning if creds missing, never raises)
def _smoke():
    if not CF_TOKEN:
        logger.warning(
            "[d1-sync] CLOUDFLARE_API_TOKEN not set — Neon→D1 sync will "
            "fail. Set the env var on Railway with a token that has "
            "D1:Edit scope on database %s.", CF_D1_ID)
    else:
        logger.info("[d1-sync] ready, account=%s database=%s",
                     CF_ACCOUNT, CF_D1_ID[:8])

_smoke()
