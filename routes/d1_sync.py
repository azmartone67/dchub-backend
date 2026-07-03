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
from internal_auth import accepted_internal_keys
import json
import time
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
d1_sync_bp = Blueprint("d1_sync", __name__)


_INTERNAL_KEYS = accepted_internal_keys()
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

BATCH_SIZE = 100            # statements per CF D1 batch call. Each statement
                            # binds 16 params — well under D1's 100-param/query
                            # cap (that cap is PER STATEMENT, not per batch). A
                            # batch that fails wholesale falls back to per-row,
                            # so an over-large batch degrades safely, never wedges.
SYNC_TIMEOUT_SECONDS = 600  # whole-job ceiling (10 min)


def _d1_headers() -> dict:
    return {
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    }


def _check_d1(r) -> dict:
    """Parse a CF D1 REST response and raise a DESCRIPTIVE error on failure.

    CF returns the real reason (bad SQL, missing field, param mismatch) in the
    JSON body's `errors`, even on an HTTP 400. requests' raise_for_status()
    discards the body — which is exactly why sync_log only ever showed a bare
    "400 Client Error: Bad Request" and the true cause was invisible. Surface
    the body detail instead so failures are diagnosable from /status."""
    try:
        data = r.json()
    except Exception:
        r.raise_for_status()          # non-JSON body → fall back to HTTP status
        raise
    if not r.ok or not data.get("success", False):
        errs = data.get("errors") or data.get("messages") or []
        detail = "; ".join(
            (e.get("message") if isinstance(e, dict) else str(e)) for e in errs
        ) or f"HTTP {r.status_code}"
        raise RuntimeError(f"D1 error: {detail}")
    return data


def _d1_query(sql: str, params: list = None, timeout: int = 30) -> dict:
    """POST a single SQL statement to D1. Returns the parsed response dict.
    Raises a descriptive error on failure; caller handles."""
    import requests
    if not CF_TOKEN:
        raise RuntimeError(
            "CLOUDFLARE_API_TOKEN not set on Railway — D1 sync disabled. "
            "Create a token with D1:Edit scope and set it as an env var.")
    body = {"sql": sql}
    if params:
        body["params"] = params
    r = requests.post(CF_D1_URL, headers=_d1_headers(), json=body, timeout=timeout)
    return _check_d1(r)


def _d1_batch(statements: list, timeout: int = 60) -> dict:
    """Execute many {sql, params} statements in ONE HTTP call via CF D1's
    documented batch envelope.

    THE FIX (2026-07-03): the CF D1 REST `/query` endpoint accepts a JSON
    OBJECT only — either {"sql","params"} or {"batch":[{"sql","params"},...]}.
    It does NOT accept a bare top-level array. The prior version (git e0037c60)
    POSTed `json=statements` (a bare array), so CF couldn't find a `sql` field
    and 400'd EVERY batch — forcing the per-row fallback that took ~8.5 min for
    ~4k rows and left D1 frozen at ~6.2k of ~21k facilities. Wrapping the same
    statements in {"batch": [...]} is the documented batch mode:
      • ~21k rows / 100 per batch ≈ 210 calls → ~1–2 min end-to-end.
      • The batch runs in an implicit transaction: if one statement errors the
        whole batch rolls back, and the caller's per-row fallback re-applies it.

    History of wrong turns kept as a warning: v1 used a `/batch` URL (404 — CF
    has no such path); v2 used array-body-to-/query (400 — this function's bug).
    """
    import requests
    if not CF_TOKEN:
        raise RuntimeError("CLOUDFLARE_API_TOKEN not set")
    # CF D1 batch: same /query URL, body is an OBJECT with a "batch" array.
    r = requests.post(CF_D1_URL, headers=_d1_headers(),
                      json={"batch": statements}, timeout=timeout)
    return _check_d1(r)


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
    # r-stable-slug (2026-06-16): hash on provider|name (stable), NOT id (churns
    # every re-ingestion) — must match the sitemap/lookup so D1 mirror keys align.
    from routes.facility_slug import stable_hash8
    provider_slug = _slugify(row.get("provider") or "")
    name_slug = _slugify(row.get("name") or "")
    if name_slug and len(name_slug) >= 3:
        short_hash = stable_hash8(row.get("provider"), row.get("name"))
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
    # Batched via CF D1's {"batch":[...]} envelope (_d1_batch). 100 upserts per
    # HTTP call → ~210 calls for ~21k rows → ~1–2 min. A per-row fallback covers
    # any single batch that CF rejects (bad UTF-8, oversize address, etc.).
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
                    # Capture ONE representative per-row failure so we can tell a
                    # transient batch reject from a genuinely bad row.
                    if not any("per-row row" in x for x in out["errors"]):
                        out["errors"].append(
                            f"per-row row id={r.get('id')} failed: {str(e2)[:160]}")
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


_D1_SYNC_RUNNING = False


@d1_sync_bp.route("/api/v1/admin/d1-sync/run", methods=["POST"])
def run_now():
    """Admin: trigger a sync pass in a BACKGROUND THREAD + return 202 immediately.
    r-dr (2026-06-17): _run_sync() takes ~75s (250 CF D1 batches) which EXCEEDS the
    gunicorn worker timeout — running it inline 502'd ('Application failed to
    respond') on every hourly cron call, so the sync never completed and D1 froze
    (stuck ~4.5K rows, sync_log empty). Background thread + a single-flight guard
    fixes it. CF D1 uses INSERT ON CONFLICT (idempotent), so a worker recycled
    mid-sync is harmless — the next hourly run just retries."""
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403
    global _D1_SYNC_RUNNING
    if _D1_SYNC_RUNNING:
        return jsonify(ok=True, started=False, mode="background",
                       note="a D1 sync is already running"), 202
    import threading

    def _bg():
        global _D1_SYNC_RUNNING
        _D1_SYNC_RUNNING = True
        try:
            _run_sync()
        except Exception as e:
            logging.warning("[d1_sync] background sync failed: %s", e)
        finally:
            _D1_SYNC_RUNNING = False

    threading.Thread(target=_bg, daemon=True, name="d1-sync").start()
    return jsonify(ok=True, started=True, mode="background",
                   note="sync running in background (~75s); poll /api/v1/admin/d1-sync/status"), 202


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
