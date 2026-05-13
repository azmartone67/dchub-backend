"""Shared helpers for ISO real-time extractors. Reduces per-ISO boilerplate."""

import csv
import io
import json
import os
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2 as _pg


def dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def conn():
    c = _pg.connect(dsn())
    try:
        yield c
    finally:
        c.close()


def fetch_first_working(urls, timeout=6, ua="dchub-iso/1.0", total_budget=12):
    """Try URLs in order. Returns (text, working_url) or raises.

    Phase GG+ (2026-05-13): added HTML-shell detection. Some ISOs
    serve their data feeds through JS SPAs (Dataminer 2 / iso-ne.com
    `/ws/wsclient`). A GET to these returns 200 + an HTML page that's
    the SPA shell, not actual data. Previously this counted as a
    "working URL" and the JSON/CSV parser silently returned 0 metrics.
    Now we explicitly skip responses that look like HTML when the
    URL implied JSON/CSV — we sniff the first ~200 bytes for
    `<!doctype html` or `<html>` or `<head>` patterns and treat as a
    soft-error so the loop tries the next URL.

    Some endpoints legitimately return text/html (e.g. the SPP
    /Real-time-Market HTML page is what we WANT to parse for fuel-mix
    numbers). To handle this, we only skip HTML when the URL ends in
    `.csv`/`.json` or includes `format=csv` / `download=true` —
    explicit "give me data" signals. Otherwise we accept the response
    and let the parser try.

    Phase QQ+8 (2026-05-13): TWO timeout fixes to stop 502s on the new
    ISOs (PJM/IESO/AESO/TVA/BPA), which all 502'd at exactly 15s.
      1. Per-URL `timeout` dropped 20s → 6s. With 4 URLs per ISO the
         old worst-case was 80s; Railway gunicorn killed any request
         over 15s with a 502.
      2. New `total_budget` (default 12s) — cumulative elapsed time
         across all URL attempts. Even if every URL is slow, we exit
         before the orchestrator per-future budget (12s) and Railway's
         edge (~15s). Better to fail fast (and let the orchestrator
         move on) than to 502 the whole endpoint.
    """
    import re as _re, time as _time
    last = None
    started = _time.time()
    for i, url in enumerate(urls):
        # Phase QQ+8: bail if cumulative elapsed has consumed the budget
        elapsed = _time.time() - started
        remaining = total_budget - elapsed
        if remaining <= 0.5:  # less than 500ms left — not worth trying
            raise RuntimeError(
                f"total_budget_exceeded: {elapsed:.1f}s/{total_budget}s "
                f"after {i} of {len(urls)} URLs; last_error={last}")
        # Cap per-URL timeout by remaining budget so we never overshoot
        per_url_timeout = min(timeout, max(1.0, remaining - 0.5))
        try:
            # Set Accept-Encoding: identity so we get raw bytes (no gzip)
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": "application/json, text/csv, */*;q=0.5",
                "Accept-Encoding": "identity",
            })
            with urllib.request.urlopen(req, timeout=per_url_timeout) as resp:
                if 200 <= resp.status < 300:
                    text = resp.read().decode("utf-8", errors="replace")
                    # Heuristic: if the URL signaled "give me data" but
                    # the response looks like an HTML shell, skip it.
                    looks_like_html = bool(_re.match(
                        r'^\s*(?:<!doctype\s+html|<html[\s>]|<head[\s>])',
                        text[:300], _re.I))
                    expects_data = any(s in url.lower() for s in
                                        (".csv", ".json", "format=csv",
                                         "format=json", "download=true",
                                         "/api/", "/data/"))
                    if looks_like_html and expects_data:
                        last = f"{url}: returned HTML shell (JS SPA), skipping"
                        continue
                    return text, url
        except urllib.error.HTTPError as e:
            last = f"{url}: HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            last = f"{url}: {type(e).__name__}: {str(e)[:80]}"
    raise RuntimeError(f"all URLs failed; last={last}")


def persist_metrics(iso, metrics):
    """Insert each metric to grid_data. Returns count actually inserted."""
    if not metrics:
        return 0
    rows = 0
    with conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (iso, name, data["value"], data.get("unit", "")),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                pass
        c.commit()
    return rows


def latest_for_iso(iso):
    """Return latest metric value per metric_name for an ISO."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT metric_name, metric_value, unit, timestamp
               FROM grid_data WHERE iso = %s
               ORDER BY timestamp DESC LIMIT 200""",
            (iso,),
        )
        rows = cur.fetchall()
    by = {}
    for n, v, u, ts in rows:
        if n not in by:
            by[n] = {"metric": n, "value": v, "unit": u,
                     "timestamp": ts.isoformat() if ts else None}
    return list(by.values())


def health_for_iso(iso, source_id):
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT MAX(timestamp), COUNT(*) FROM grid_data WHERE iso = %s",
            (iso,),
        )
        latest, total = cur.fetchone()
    return {
        "iso": iso,
        "latest_data_at": latest.isoformat() if latest else None,
        "total_records": int(total or 0),
        "source_id": source_id,
    }


def scrub_url(url):
    """Phase QQ+10 (2026-05-13): redact known-secret query params before
    returning a URL to a client.

    Discovered when shipping QQ+9 — EIA_API_KEY was being embedded in
    api.eia.gov/v2 URLs and then echoed back in /extract responses
    (`fetched_url` field). Any caller could read the key from a public
    endpoint. We now scrub before storing.

    Redacted params: api_key, key, token, password, auth (and
    username/password embedded as userinfo in the netloc, e.g.
    https://user:pass@host/...).
    """
    if not url or not isinstance(url, str):
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url)
        # Scrub userinfo (https://user:pass@host/...)
        netloc = parts.netloc
        if "@" in netloc:
            netloc = "***:***@" + netloc.split("@", 1)[1]
        # Scrub secret query params
        SECRET_KEYS = {"api_key", "apikey", "key", "token", "auth",
                       "password", "secret", "admin_key"}
        qs = parse_qsl(parts.query, keep_blank_values=True)
        scrubbed = [(k, ("***" if k.lower() in SECRET_KEYS else v))
                    for k, v in qs]
        new_query = urlencode(scrubbed, doseq=True)
        return urlunsplit((parts.scheme, netloc, parts.path, new_query, parts.fragment))
    except Exception:
        # Failsafe: if URL parsing fails, return host-only so we never
        # leak the full string.
        try:
            return url.split("?", 1)[0]
        except Exception:
            return "(scrubbed)"


def parse_eia_v2_fuel_mix(json_text, prefix="fuel_"):
    """Phase QQ+10 (2026-05-13): parse api.eia.gov/v2/electricity/rto/
    fuel-type-data + region-data responses.

    EIA v2 shape:
      {
        "response": {
          "data": [
            {"period": "2026-05-13T08", "respondent": "TVA",
             "fueltype": "NG", "value": 12345.6, ...},
            {"period": "2026-05-13T08", "respondent": "TVA",
             "fueltype": "NUC", "value": 6789.1, ...},
            ...
          ]
        }
      }

    region-data variant uses "type" instead of "fueltype" (NG, D for
    demand, etc). We accept either field.

    Returns a metrics dict keyed by `{prefix}{fueltype.lower()}` with the
    LATEST-period value per fuel type. Data is sorted desc by period
    upstream, so we take the first-seen value per fuel type.
    """
    try:
        d = json.loads(json_text)
    except (TypeError, ValueError):
        return {}
    rows = (d.get("response") or {}).get("data") or []
    if not rows:
        return {}
    metrics = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fuel = (row.get("fueltype") or row.get("type") or
                row.get("type-name") or "").strip()
        if not fuel:
            continue
        # First-seen wins (rows arrive sorted desc by period)
        key = f"{prefix}{fuel.lower().replace(' ', '_')}"
        if key in metrics:
            continue
        val = row.get("value")
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num == 0:
            continue
        metrics[key] = {"value": num, "unit": row.get("value-units") or "MW"}
    return metrics


def parse_csv_numeric_columns(csv_text, prefix="", skip_cols=None):
    """Take last row of CSV, emit metrics for any numeric column.

    Default skip_cols includes timestamp-like names.
    """
    skip = set(skip_cols or [])
    skip.update({"time", "timestamp", "Time", "Timestamp", "interval", "Interval",
                 "TIME", "INTERVAL", "datetime", "DateTime", "Date", "date"})

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return {}
    last = rows[-1]
    metrics = {}
    for col, val in last.items():
        if not col or col in skip:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        clean = col.strip().lower().replace(" ", "_").replace("-", "_")
        clean = "".join(ch for ch in clean if ch.isalnum() or ch == "_")
        if clean and num != 0:
            metrics[f"{prefix}{clean}_mw"] = {"value": num, "unit": "MW"}
    return metrics


def parse_json_numeric(json_text, key_path=None, prefix=""):
    """Generic JSON parser: walks structure, emits metrics for any numeric leaf.

    key_path: optional dotted path to drill into specific nested data.
    """
    try:
        data = json.loads(json_text)
    except (TypeError, ValueError):
        return {}

    # Drill into nested key_path if specified
    if key_path:
        for k in key_path.split("."):
            if isinstance(data, dict):
                data = data.get(k, {})
            else:
                return {}

    metrics = {}

    def _walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, f"{path}_{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _walk(item, f"{path}_{i}" if path else str(i))
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            clean = path.strip().lower().replace(" ", "_").replace("-", "_")
            clean = "".join(ch for ch in clean if ch.isalnum() or ch == "_")
            if clean and obj != 0:
                metrics[f"{prefix}{clean}_mw"] = {"value": float(obj), "unit": "MW"}

    _walk(data, "")
    # Cap at 50 metrics (some JSON has thousands of leaves)
    if len(metrics) > 50:
        items = list(metrics.items())[:50]
        metrics = dict(items)
    return metrics
