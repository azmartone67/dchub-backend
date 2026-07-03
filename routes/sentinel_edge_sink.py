"""sentinel_edge_sink.py — KV / R2 sinks + latency-regression detector for the
Site Sentinel master shell (r-sentinel-edge, 2026-07-03).

Three leverage points on the Cloudflare stack we already run:

  #4 KV snapshot  — kv_put_snapshot() writes the latest scan JSON to CF KV via
                    the same REST path news_publisher.kv_put() uses. Lets the
                    edge serve /api/v1/sentinel/scan (and a status page) with
                    ZERO origin/DB hit — and keeps the health surface answerable
                    even during an origin outage (a monitor that dies with the
                    app is half a monitor).

  #5 R2 archive   — r2_archive_scan() appends each sweep as JSON to R2 (reusing
                    the boto3/R2 creds the nightly pg_dump backup already uses).
                    The results table keeps only the latest row per path, so R2
                    is the only place full-sweep history lives.

  #5 regressions  — latency_regressions() reads site_sentinel_latency_history
                    (written by scan_all) and flags any path whose newest origin
                    latency is a large multiple of its own trailing median — a
                    signal class the binary up/down monitor never had.

Everything here is best-effort and FAIL-OPEN: missing env / missing boto3 /
network error → a {ok: False, skipped: ...} dict, never an exception that could
break the master tick. Nothing here is on a user request path.
"""
from __future__ import annotations

import os
import json
import datetime as _dt


# ── Cloudflare KV (#4) ────────────────────────────────────────────────
def _cf_kv_env():
    account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    # dedicated namespace for sentinel; fall back to the shared publish namespace
    ns = (os.environ.get("SENTINEL_KV_NAMESPACE_ID")
          or os.environ.get("CLOUDFLARE_KV_NAMESPACE") or "").strip()
    if account and token and ns:
        return account, token, ns
    return None


def kv_put(key: str, value: str, expiration_ttl: int = 0) -> dict:
    """Raw KV write (REST). Mirrors news_publisher.kv_put but dependency-free."""
    env = _cf_kv_env()
    if not env:
        return {"ok": False, "skipped": "CF KV env not configured "
                "(need CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN + "
                "SENTINEL_KV_NAMESPACE_ID)"}
    account, token, ns = env
    try:
        import requests
        url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
               f"/storage/kv/namespaces/{ns}/values/{key}")
        params = {}
        if expiration_ttl and expiration_ttl > 0:
            params["expiration_ttl"] = expiration_ttl
        resp = requests.put(url, headers={"Authorization": f"Bearer {token}"},
                            params=params,
                            data=value.encode("utf-8"),
                            timeout=15)
        ok = resp.status_code in (200, 204)
        return {"ok": ok, "http": resp.status_code, "key": key,
                "bytes": len(value)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}", "key": key}


def kv_put_snapshot(scan_payload: dict) -> dict:
    """Write the full scan + a findings-only view to KV for edge serving.
    Keys: sentinel:latest (full), sentinel:findings (unhealthy only).
    TTL 2 days (a fresh sweep overwrites every ~4h; TTL only bounds staleness
    if the tick itself dies)."""
    try:
        full = json.dumps(scan_payload, default=str)
    except Exception as e:
        return {"ok": False, "error": f"serialize: {str(e)[:120]}"}
    r_full = kv_put("sentinel:latest", full, expiration_ttl=172800)
    findings = [r for r in scan_payload.get("results", []) if not r.get("healthy")]
    r_find = kv_put("sentinel:findings",
                    json.dumps({"count": len(findings), "findings": findings,
                                "generated_at": scan_payload.get("generated_at")},
                               default=str),
                    expiration_ttl=172800)
    return {"ok": bool(r_full.get("ok")), "full": r_full, "findings": r_find}


# ── Cloudflare R2 (#5 archive) ────────────────────────────────────────
def _r2_client():
    endpoint = (os.environ.get("R2_ENDPOINT_URL") or "").strip()
    akey = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    skey = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
    if not (endpoint and akey and skey):
        return None, None
    try:
        import boto3
    except Exception:
        return None, None
    bucket = (os.environ.get("SENTINEL_R2_BUCKET")
              or os.environ.get("R2_BUCKET_NAME") or "dchub-backups").strip()
    try:
        client = boto3.client("s3", endpoint_url=endpoint,
                              aws_access_key_id=akey, aws_secret_access_key=skey,
                              region_name="auto")
        return client, bucket
    except Exception:
        return None, None


def r2_archive_scan(scan_payload: dict, ts_iso: str) -> dict:
    """Append this sweep to R2 as sentinel/scan_<ts>.json + overwrite
    sentinel/latest.json. ts_iso is passed in (scripts/schedulers stamp time;
    this module must not call datetime.now for resume-determinism reasons
    elsewhere, and it keeps the object key aligned with the report)."""
    client, bucket = _r2_client()
    if client is None:
        return {"ok": False, "skipped": "R2 env/boto3 not available"}
    try:
        body = json.dumps(scan_payload, default=str).encode("utf-8")
        stamp = ts_iso.replace(":", "").replace("-", "").replace("T", "_")[:15]
        key = f"sentinel/scan_{stamp}.json"
        client.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType="application/json")
        client.put_object(Bucket=bucket, Key="sentinel/latest.json", Body=body,
                          ContentType="application/json")
        return {"ok": True, "bucket": bucket, "key": key, "bytes": len(body)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ── Latency-regression detector (#5) ──────────────────────────────────
# A path is "regressed" when its newest origin latency is REGRESSION_FACTOR× its
# own trailing-window median AND above an absolute floor — so we never alarm on
# a 20ms→60ms wobble, only on real slowdowns (e.g. Surfaces API 200ms→10.6s).
REGRESSION_FACTOR   = 2.5
REGRESSION_FLOOR_MS = 1500
REGRESSION_MIN_SAMPLES = 5
REGRESSION_WINDOW_DAYS = 30


def latency_regressions(conn) -> list[dict]:
    """Return regression findings from site_sentinel_latency_history. `conn` is a
    live psycopg2 connection (caller owns its lifecycle)."""
    findings: list[dict] = []
    if conn is None:
        return findings
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH recent AS (
                    SELECT path, elapsed_ms, checked_at,
                           ROW_NUMBER() OVER (PARTITION BY path
                                              ORDER BY checked_at DESC) AS rn
                      FROM site_sentinel_latency_history
                     WHERE checked_at > NOW() - (%s || ' days')::interval
                       AND elapsed_ms IS NOT NULL
                ),
                cur_row AS (SELECT path, elapsed_ms FROM recent WHERE rn = 1),
                base AS (
                    SELECT path,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS median_ms,
                           COUNT(*) AS n
                      FROM recent WHERE rn > 1
                     GROUP BY path
                )
                SELECT c.path, c.elapsed_ms AS current_ms,
                       ROUND(b.median_ms)::int AS median_ms, b.n
                  FROM cur_row c
                  JOIN base b ON b.path = c.path
                 WHERE b.n >= %s
                   AND c.elapsed_ms >= %s
                   AND c.elapsed_ms >= b.median_ms * %s
                 ORDER BY (c.elapsed_ms::float / NULLIF(b.median_ms,0)) DESC
            """, (REGRESSION_WINDOW_DAYS, REGRESSION_MIN_SAMPLES,
                  REGRESSION_FLOOR_MS, REGRESSION_FACTOR))
            for path, current_ms, median_ms, n in cur.fetchall():
                ratio = round(current_ms / median_ms, 1) if median_ms else None
                findings.append({
                    "issue": "site_latency_regression",
                    "path": path,
                    "current_ms": current_ms,
                    "median_ms": median_ms,
                    "ratio": ratio,
                    "samples": n,
                    "detail": (f"{path} origin latency {current_ms}ms is {ratio}× "
                               f"its {REGRESSION_WINDOW_DAYS}d median ({median_ms}ms, "
                               f"n={n}) — investigate query/cache."),
                })
    except Exception:
        # Table may not exist yet on the very first tick after deploy.
        pass
    return findings


def prune_latency_history(conn, keep_days: int = 45) -> int:
    """Bound the history table. Returns rows deleted."""
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM site_sentinel_latency_history "
                        "WHERE checked_at < NOW() - (%s || ' days')::interval",
                        (keep_days,))
            return cur.rowcount or 0
    except Exception:
        return 0
