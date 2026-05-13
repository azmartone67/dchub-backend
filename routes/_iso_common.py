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


def fetch_first_working(urls, timeout=20, ua="dchub-iso/1.0"):
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
    """
    import re as _re
    last = None
    for url in urls:
        try:
            # Set Accept-Encoding: identity so we get raw bytes (no gzip)
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": "application/json, text/csv, */*;q=0.5",
                "Accept-Encoding": "identity",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            last = f"{url}: {type(e).__name__}: {e}"
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
