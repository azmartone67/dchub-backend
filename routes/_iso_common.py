"""Shared helpers for ISO real-time extractors. Reduces per-ISO boilerplate."""

import csv
import http.client
import io
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2 as _pg
from routes._swallowed_writes import note_swallowed_write

_log = logging.getLogger("dchub.iso_common")


# ── secret scrubbing ───────────────────────────────────────────────────
# The ISO orchestrator's response body is printed verbatim by
# .github/workflows/data-pulse.yml, so ANY string that reaches an
# extractor's summary["error"] reaches a public CI log. Because these
# values are built into strings by our own code rather than passed
# through GitHub secrets, Actions' secret masking never redacts them.
#
# Env vars whose VALUES must never survive into an error string.
SECRET_ENV_KEYS = (
    "ISONE_USERNAME", "ISONE_PASSWORD",
    "ERCOT_USERNAME", "ERCOT_PASSWORD", "ERCOT_API_KEY",
    "IESO_USERNAME", "IESO_PASSWORD",
    "EIA_API_KEY", "PJM_API_KEY", "AESO_API_KEY",
)

# user/pass pairs that get sent as an HTTP Basic token — the base64 blob
# IS the credential, so redact that encoding too.
SECRET_ENV_PAIRS = (
    ("ISONE_USERNAME", "ISONE_PASSWORD"),
    ("ERCOT_USERNAME", "ERCOT_PASSWORD"),
    ("IESO_USERNAME", "IESO_PASSWORD"),
)

# Values shorter than this are not redacted by value: replacing a 1-2
# character string would shred the message without protecting anything
# real. The structural pass below still covers them inside a URL.
_MIN_REDACTABLE = 3

# Greedy up to the LAST "@" before a "/" — so a username that is itself
# an email address (user@example.com:pw@host) is fully consumed.
_RE_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/]*@")
_RE_SECRET_PARAM = re.compile(
    r"((?:api_key|apikey|key|token|auth|password|secret|admin_key)=)"
    r"[^&\s\"'>]+", re.I)


def scrub_secrets(text, environ=None):
    """Redact secret VALUES from an arbitrary string.

    Deliberately value-based rather than URL-shaped. The leak this was
    written for surfaced as:

        InvalidURL: nonnumeric port: 'user:PASSWORD@webservices.iso-ne.com'

    — an exception message with no scheme and no valid URL in it, so
    neither a URL parser nor a scheme-anchored regex would ever have
    caught it. Matching on the known secret value is what works.
    """
    if not text or not isinstance(text, str):
        return text
    from urllib.parse import quote as _q
    env = os.environ if environ is None else environ

    forms = set()

    def _add(v):
        v = (v or "").strip()
        if len(v) < _MIN_REDACTABLE:
            return
        forms.add(v)
        # urllib.request unquotes the netloc before http.client sees it,
        # so a percent-encoded credential can surface in EITHER form.
        forms.add(_q(v, safe=""))
        forms.add(_q(v))

    for k in SECRET_ENV_KEYS:
        _add(env.get(k))
    for uk, pk in SECRET_ENV_PAIRS:
        u, p = (env.get(uk) or "").strip(), (env.get(pk) or "").strip()
        if u and p:
            import base64 as _b64
            _add(_b64.b64encode(f"{u}:{p}".encode()).decode())
            _add(f"{u}:{p}")

    out = text
    # Longest first: a short value must not chop up a longer one.
    for form in sorted(forms, key=len, reverse=True):
        if form in out:
            out = out.replace(form, "***")

    # Structural pass — catches credentials that never came from the
    # env list above (e.g. a hand-built URL in a new adapter).
    out = _RE_USERINFO.sub(r"\1***:***@", out)
    out = _RE_SECRET_PARAM.sub(r"\1***", out)
    return out


def scrub_attempt(url, message):
    """Scrub `message` using the credentials carried by `url` ITSELF.

    Needed because the failure that motivated this arrives with no
    scheme and no parseable URL:

        InvalidURL: nonnumeric port: '<password>@webservices.iso-ne.com'

    There is nothing there for a URL-anchored regex to grab, and
    scrub_secrets() can only match a value it can name in the
    environment. But we know exactly which credential we just tried —
    so redact that, whatever env var it came from.
    """
    from urllib.parse import urlsplit, unquote
    out = message
    try:
        netloc = urlsplit(url).netloc
        if "@" in netloc:
            userinfo = netloc.rsplit("@", 1)[0]
            parts = [userinfo]
            if ":" in userinfo:
                parts.extend(userinfo.split(":", 1))
            # Longest first so a short username can't shred a longer
            # password that contains it.
            for p in sorted(set(parts), key=len, reverse=True):
                for form in sorted({p, unquote(p)}, key=len, reverse=True):
                    if len(form) >= _MIN_REDACTABLE:
                        out = out.replace(form, "***")
    except Exception:
        pass
    return scrub_secrets(out)


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
    for i, item in enumerate(urls):
        # An entry may be a bare URL, or (url, extra_headers) so callers
        # can send credentials as an Authorization header instead of
        # embedding them in the URL. See scrub_secrets() for why.
        if isinstance(item, (tuple, list)):
            url, extra_headers = item[0], (item[1] or {})
        else:
            url, extra_headers = item, {}
        # Phase QQ+8: bail if cumulative elapsed has consumed the budget
        elapsed = _time.time() - started
        remaining = total_budget - elapsed
        if remaining <= 0.5:  # less than 500ms left — not worth trying
            raise RuntimeError(scrub_secrets(
                f"total_budget_exceeded: {elapsed:.1f}s/{total_budget}s "
                f"after {i} of {len(urls)} URLs; last_error={last}"))
        # Cap per-URL timeout by remaining budget so we never overshoot
        per_url_timeout = min(timeout, max(1.0, remaining - 0.5))
        try:
            # Set Accept-Encoding: identity so we get raw bytes (no gzip)
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": "application/json, text/csv, */*;q=0.5",
                "Accept-Encoding": "identity",
                **extra_headers,
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
                        last = scrub_attempt(
                            url, f"{url}: returned HTML shell (JS SPA), skipping")
                        continue
                    return text, url
        except urllib.error.HTTPError as e:
            last = scrub_attempt(url, f"{url}: HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            last = scrub_attempt(
                url, f"{url}: {type(e).__name__}: {str(e)[:80]}")
        except (http.client.HTTPException, ValueError) as e:
            # A malformed URL (http.client.InvalidURL is an HTTPException,
            # NOT an OSError) used to escape this loop entirely and abort
            # the whole chain, so the working fallback URLs below it were
            # never tried. Treat it as a per-URL failure and keep going.
            last = scrub_attempt(
                url, f"{url}: {type(e).__name__}: {str(e)[:80]}")
    raise RuntimeError(scrub_secrets(f"all URLs failed; last={last}"))


# ── observation time → grid_data.timestamp ───────────────────────────
# D4 (2026-09-02). persist_metrics used to INSERT (iso, metric_name,
# metric_value, unit) and let `timestamp` take its DEFAULT NOW() — the INSERT
# clock. Its ON CONFLICT (iso, timestamp, metric_name) DO NOTHING guard could
# therefore NEVER fire: the key differed on every write by construction. So a
# frozen upstream reading was republished as a brand-new "now" row on every
# 15-minute data-pulse tick, and health_for_iso's MAX(timestamp) reported the
# insert clock as data freshness. Verified live 2026-07-29 on AEC: latest_data_at
# 2026-07-29T07:09 with 14,776 rows for a feed whose newest genuine EIA
# observation is 2021-09-01T05.
#
# The caller now DECLARES the upstream observation time. None is allowed — some
# fallback sources carry no stamp — but it is logged, so an insert-clock row is
# a visible choice rather than the silent default. UNMEASURED is None, never
# "now". Guarded by tests/test_grid_ba_surface_guard.py.
_OBSERVED_AT_UNDECLARED = object()

_RE_OBSERVED_AT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2})(?::?(\d{2}))?(?::?(\d{2}))?"
    r"(?:\.\d+)?(Z|[+-]\d{2}(?::?\d{2})?)?$")


def coerce_observed_at(value):
    """Normalise an upstream observation stamp to an aware UTC datetime.

    Accepts a datetime (naive = UTC) or the ISO-8601 shapes the extractors
    actually see: EIA-930's hour-only "2026-07-29T02" (UTC) and local-hourly
    "2026-07-29T02-04", ENTSO-E's "2026-08-07T23:00Z", and a full
    "2026-08-07T23:00:00+00:00". Returns None for None / empty / unparseable —
    a stamp we cannot read must never be replaced with the clock."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    m = _RE_OBSERVED_AT.match(s)
    if not m:
        return None
    y, mo, d, hh, mm, ss, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh), int(mm or 0), int(ss or 0))
    except ValueError:
        return None
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        dt = dt - sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    return dt.replace(tzinfo=timezone.utc)


def parse_eia_v2_latest_period(json_text):
    """The newest `period` in an api.eia.gov/v2 envelope, or None.

    Companion to parse_eia_v2_fuel_mix, which keeps the newest value per fuel
    but drops the period it came from. Rows arrive sorted desc upstream; max()
    is used rather than trusting that order. Periods are ISO-8601 prefixes of
    one shape per response ("2026-07-29T02"), so lexical max is chronological."""
    try:
        d = json.loads(json_text)
    except (TypeError, ValueError):
        return None
    rows = (d.get("response") or {}).get("data") or []
    periods = [str(r.get("period")) for r in rows
               if isinstance(r, dict) and r.get("period")]
    return max(periods) if periods else None


def persist_metrics(iso, metrics, observed_at=_OBSERVED_AT_UNDECLARED):
    """Insert each metric to grid_data. Returns count actually inserted.

    `observed_at` is the UPSTREAM observation time and becomes the row's
    `timestamp`, so the (iso, timestamp, metric_name) dedup guard fires when the
    same reading comes round again — a frozen feed stops growing instead of
    being re-stamped "now" 96 times a day. Pass None ONLY when the source
    genuinely carries no stamp; it is logged, and the row then takes the insert
    clock through COALESCE. Leaving it undeclared is logged too — every call
    site is expected to say which it is."""
    if not metrics:
        return 0
    ts = None
    if observed_at is _OBSERVED_AT_UNDECLARED:
        _log.warning("persist_metrics(%s): observed_at not declared — rows take the "
                     "INSERT clock and the dedup guard cannot fire", iso)
    elif observed_at is None:
        _log.warning("persist_metrics(%s): observed_at=None — upstream carried no "
                     "observation time; rows take the INSERT clock", iso)
    else:
        ts = coerce_observed_at(observed_at)
        if ts is None:
            _log.warning("persist_metrics(%s): unparseable observed_at %r — rows take "
                         "the INSERT clock", iso, observed_at)
    rows = 0
    with conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit, timestamp)
                       VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()))
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (iso, name, data["value"], data.get("unit", ""), ts),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                note_swallowed_write("grid_data", where="_iso_common.persist_metrics")
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
        # D4 (2026-09-02): say what the clock IS. Before persist_metrics took
        # observed_at this was always the insert clock, so a feed frozen since
        # 2021 read as "fresh this minute".
        "latest_data_at_basis": (
            "MAX(grid_data.timestamp). Extractors that know their upstream "
            "observation time write THAT (EIA-930 hour for the EIA-fed ISOs and "
            "balancing authorities); rows written before 2026-09-02, and rows "
            "from sources that carry no stamp, hold the insert clock. An old "
            "value here is a genuine last observation, not a stopped cron."),
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
            # rsplit, NOT split: ISO-NE accounts use an email address as
            # the username, so the userinfo itself contains an "@".
            # Splitting on the FIRST one kept "…:password@host" in the
            # "host" half and echoed the password straight back out.
            netloc = "***:***@" + netloc.rsplit("@", 1)[1]
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
