"""
eia930.py — the ONE parameterised EIA-930 balancing-authority adapter.
======================================================================

ws2 (2026-07-29). Before this module the same api.eia.gov/v2 query string was
copy-pasted verbatim in FIVE places — routes/iso_tva.py:48, routes/iso_bpa.py:46,
routes/iso_pjm.py:59, routes/iso_miso.py:54 and routes/eia_utility_bas.py:123 —
each differing only in the respondent and `length`. Five copies means five
places to fix a URL change and five different truncation limits.

What lives here:
  resolve_respondent(region_code) — our label → EIA-930 respondent
  eia930_url(region_code, ...)    — the single URL builder
  fetch_eia930_ba(region_code, ...) — fetch + parse one BA. NEVER raises.

What deliberately does NOT live here:
  * Persistence. The caller decides which `iso` tag a reading is stored under:
    eia_utility_bas persists the AZPS reading under OUR code "APS", not under
    the EIA respondent. Folding that into the adapter would silently re-tag 43
    balancing authorities. Callers keep calling _iso_common.persist_metrics.
  * The geographic alias table (PHOENIX→AZPS, QUINCY→GCPD, SACRAMENTO→BANC …).
    That is request-routing and already lives at main.py:4341 (EIA_RTO_MAP).
    A second copy here would be one more manifest to drift.

DATASETS — /fuel-type-data/data facets on `fueltype` (fuel MW);
/region-data/data facets on `type` (D=demand, DF=day-ahead forecast, NG=net
generation, TI=interchange). They are NOT interchangeable: parse_eia_v2_fuel_mix
accepts `type` as a fuel name (routes/_iso_common.py:234), so a region-data row
type="D" is stored as a metric literally named "fuel_d" that is demand, not a
fuel. Pass dataset="region-data" only when you want exactly that.

CACHE — why, and precisely what it is worth:
  EIA-930 is an HOURLY feed. .github/workflows/data-pulse.yml:15 POSTs
  /api/v1/iso/all/extract every 15 minutes, so three of every four pulls
  re-fetch a value that cannot have changed (~47 EIA calls/tick ≈ 135k/month).
  fetch_eia930_ba memoises the parsed reading per (respondent, dataset, length)
  for EIA930_CACHE_TTL_S seconds (default 1500 = 25 min) — short enough that a
  newly published hour is never more than ~25 min late.
  ★ The cache is PER PROCESS (a plain module dict). Prod runs
    `gunicorn --workers 1` (start_web.sh:83) across numReplicas=2
    (railway.json), so the honest saving is 4 live fetches/hour/BA → about 2,
    NOT → 1. Do not quote a 4x reduction, and do not treat a cache hit measured
    on one replica as proof about the other (the per-process-cache finding from
    shell #38).
  Persistence is NOT skipped on a cache hit — the caller still writes the same
  reading to grid_data, so rows_inserted, and every success predicate built on
  it (routes/eia_utility_bas.py run_extraction, routes/iso_orchestrator.py:141-153),
  keeps exactly the meaning it had before this module existed.
  Failures are negative-cached for _NEG_TTL_S (120s) only — long enough to stop
  a burst hammering a dead upstream, short enough that the next 15-min tick
  always retries. A stale successful reading is NEVER served in place of a
  failure: that would fabricate freshness.
"""
import json
import os
import threading
import time

from routes._iso_common import (
    fetch_first_working, parse_eia_v2_fuel_mix, scrub_url,
)

EIA_V2_BASE = "https://api.eia.gov/v2/electricity/rto"
_DATASETS = ("fuel-type-data", "region-data")

# Our label → EIA-930 respondent, listed ONLY where the two differ. Anything
# absent resolves to ITSELF, which is correct for 42 of the 43 registered BAs
# (routes/eia_utility_bas.py:54-112 — code == respondent for all but APS).
# Verified divergences in-tree: routes/eia_utility_bas.py:56 (APS→AZPS),
# routes/iso_bpa.py:46 (BPA→BPAT) and main.py:4342-4348 (the ISO set). Passing
# our own label straight through returns an empty 200 for six of seven ISOs.
RESPONDENT_ALIASES = {
    "APS": "AZPS",
    "BPA": "BPAT",
    "ERCOT": "ERCO",
    "CAISO": "CISO",
    "NYISO": "NYIS",
    "SPP": "SWPP",
    "ISONE": "ISNE",
    "NEISO": "ISNE",
}

# Cache tuning. TTL is env-overridable so the cadence can be changed without a
# deploy; the negative TTL is deliberately NOT (a long negative TTL hides an
# upstream outage, which is the failure mode we care about).
_DEFAULT_TTL_S = 1500
_NEG_TTL_S = 120
_CACHE_MAX = 256
_CACHE_LOCK = threading.Lock()
_CACHE = {}   # (respondent, dataset, length) -> {stored_at, expires, result}


def _ttl_s():
    """Cache lifetime in seconds. Fails soft to the default on garbage env."""
    try:
        v = int(os.environ.get("EIA930_CACHE_TTL_S", "") or _DEFAULT_TTL_S)
    except (TypeError, ValueError):
        return _DEFAULT_TTL_S
    return v if v >= 0 else _DEFAULT_TTL_S


def resolve_respondent(region_code):
    """Return (respondent, basis) for one region code.

    basis is "alias" when the code needed translating, "identity" when our
    label IS the EIA respondent, and None when there is nothing to resolve.
    The basis travels with the value on purpose: an identity resolution is a
    GUESS that the caller's label happens to be an EIA-930 respondent, and a
    wrong guess comes back as an empty 200 from EIA, not as an error.
    """
    code = (region_code or "").strip().upper()
    if not code:
        return None, None
    if code in RESPONDENT_ALIASES:
        return RESPONDENT_ALIASES[code], "alias"
    flat = code.replace("-", "").replace("_", "").replace(" ", "")
    if flat in RESPONDENT_ALIASES:
        return RESPONDENT_ALIASES[flat], "alias"
    return code, "identity"


def eia930_url(region_code, dataset="fuel-type-data", length=12, api_key=None):
    """Build the ONE api.eia.gov/v2 EIA-930 URL.

    At the defaults this is BYTE-IDENTICAL to the five hand-written copies it
    replaces — a drifting query string returns 0 rows with no exception and no
    log, so tests/test_eia930_adapter.py freezes the exact string.
    """
    if dataset not in _DATASETS:
        raise ValueError("unknown EIA-930 dataset: %r" % (dataset,))
    respondent, _basis = resolve_respondent(region_code)
    key = api_key if api_key is not None else os.environ.get("EIA_API_KEY", "")
    return (
        f"{EIA_V2_BASE}/{dataset}/data/?api_key={key}"
        f"&frequency=hourly&data[0]=value&facets[respondent][]={respondent}"
        f"&sort[0][column]=period&sort[0][direction]=desc&length={int(length)}"
    )


def _eia_rows(json_text):
    """response.data[] out of an EIA v2 envelope, or [] — never raises."""
    try:
        d = json.loads(json_text)
    except (TypeError, ValueError):
        return []
    if not isinstance(d, dict):
        return []
    rows = (d.get("response") or {}).get("data")
    return rows if isinstance(rows, list) else []


def _latest_period(rows):
    """Newest `period` in the envelope, or None.

    parse_eia_v2_fuel_mix reads row['period'] to order rows and then throws it
    away (routes/_iso_common.py:238-241), so nothing downstream can tell a
    fresh reading from a stale one — and main.py:4071-4076 documents fuel-type
    lagging demand by up to 19h. We re-read it here rather than change the
    shared parser: that parser is pinned by tests/test_iso_common_helpers.py,
    which exec's it in a namespace holding only `json`, so giving it a new
    dependency would NameError the harness.
    """
    for row in rows or ():
        if isinstance(row, dict) and row.get("period"):
            return str(row["period"])
    return None


def _copy_result(res):
    """Shallow copy + a fresh metrics dict, so a caller mutating what it got
    back can never edit the cached copy."""
    out = dict(res)
    out["metrics"] = dict(res.get("metrics") or {})
    return out


def fetch_eia930_ba(region_code, dataset="fuel-type-data", length=12,
                    prefix="fuel_", ua="dchub-eia930/1.0", timeout=4,
                    total_budget=5, max_age_s=None):
    """Fetch + parse ONE balancing authority's latest EIA-930 reading.

    NEVER raises. Every failure comes back as status != "ok" with metrics {},
    so a caller can fall through to its own fallback URLs exactly as before.

    Returned keys:
      status            "ok" | "no_key" | "error"
      respondent        what we actually asked EIA for
      respondent_basis  "alias" | "identity" — how we got there. An identity
                        resolution is a guess; report it, do not hide it.
      metrics           {name: {value, unit}} — the shape persist_metrics wants
      metrics_count
      period            the EIA observation hour (e.g. "2026-07-29T02") or None.
                        This is the honest basis for freshness: grid_data's own
                        timestamp column is the INSERT time (its DEFAULT NOW(),
                        routes/iso_ercot.py:67), not the observation hour.
      rows_requested / rows_returned / truncated_risk
                        truncated_risk is True when EIA returned exactly as many
                        rows as we asked for, i.e. the newest hour may hold
                        fuels we never saw. length=12 against EIA's ~13 fuel
                        codes leaves a one-row margin (main.py:4026 uses 400).
      cached / cache_age_s / cache_scope
      fetched_url       ALWAYS scrub_url()'d — the api_key rides in the query
                        string and leaked through /extract once already
                        (routes/_iso_common.py:160-172).
      bytes / preview / no_metrics_reason / error
    """
    respondent, basis = resolve_respondent(region_code)
    out = {
        "status": "error",
        "region_code": (region_code or "").strip().upper() or None,
        "respondent": respondent,
        "respondent_basis": basis,
        "dataset": dataset,
        "metrics": {},
        "metrics_count": 0,
        "period": None,
        "rows_requested": int(length),
        "rows_returned": None,
        "truncated_risk": None,
        "cached": False,
        "cache_age_s": None,
        "cache_scope": "per-process",
        "fetched_url": None,
        "bytes": None,
        "preview": None,
        "no_metrics_reason": None,
        "error": None,
    }
    if not respondent:
        out["error"] = "empty region_code"
        return out

    key = os.environ.get("EIA_API_KEY", "")
    if not key:
        # api.eia.gov/v2 answers 403 without a key (routes/iso_tva.py:40-42),
        # so a keyless fan-out is 43 guaranteed round-trips to a 403. Say that
        # instead of spending them, and let the caller fall through.
        out["status"] = "no_key"
        out["error"] = ("EIA_API_KEY unset — api.eia.gov/v2 returns 403 "
                        "without it; no request attempted")
        return out

    ck = (respondent, dataset, int(length))
    ttl = _ttl_s() if max_age_s is None else max(0, int(max_age_s))
    now = time.time()
    if ttl > 0:
        with _CACHE_LOCK:
            hit = _CACHE.get(ck)
            if hit and hit["expires"] > now and (now - hit["stored_at"]) <= ttl:
                cached = _copy_result(hit["result"])
                cached["cached"] = True
                cached["cache_age_s"] = round(now - hit["stored_at"], 1)
                return cached

    try:
        url = eia930_url(respondent, dataset=dataset, length=length, api_key=key)
        text, used = fetch_first_working([url], ua=ua, timeout=timeout,
                                         total_budget=total_budget)
        out["fetched_url"] = scrub_url(used)
        out["bytes"] = len(text)
        rows = _eia_rows(text)
        out["rows_returned"] = len(rows)
        out["truncated_risk"] = bool(rows) and len(rows) >= int(length)
        out["period"] = _latest_period(rows)
        out["metrics"] = parse_eia_v2_fuel_mix(text, prefix=prefix) or {}
        out["metrics_count"] = len(out["metrics"])
        out["status"] = "ok"
        if not out["metrics"]:
            # 0 metrics is a real answer, not a crash: EIA publishes an empty
            # data[] for respondents it does not cover (non-US BAs are
            # interchange-only) and parse_eia_v2_fuel_mix drops exact zeros
            # (routes/_iso_common.py:249). Keep the reason and a snippet so a
            # zero is never reported as if it were a failure, or vice versa.
            out["preview"] = (text or "")[:240]
            out["no_metrics_reason"] = (
                "EIA returned 0 rows for this respondent"
                if not rows else
                "EIA rows present but every value was null or exactly 0 "
                "(parse_eia_v2_fuel_mix drops both)")
    except Exception as e:
        out["status"] = "error"
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"

    if ttl > 0:
        store_ttl = ttl if out["status"] == "ok" else _NEG_TTL_S
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX and ck not in _CACHE:
                # Bounded: evict the oldest rather than grow without limit if a
                # caller ever passes arbitrary region codes.
                oldest = min(_CACHE, key=lambda k: _CACHE[k]["stored_at"])
                _CACHE.pop(oldest, None)
            _CACHE[ck] = {"stored_at": now, "expires": now + store_ttl,
                          "result": _copy_result(out)}
    return out


def cache_stats():
    """Diagnostics for the memo above.

    ★ PER PROCESS. gunicorn runs 1 worker per replica (start_web.sh:83) and
    railway.json sets numReplicas=2, so this describes ONE of the processes
    serving traffic — never quote it as a fleet number.
    """
    now = time.time()
    with _CACHE_LOCK:
        ages = [now - e["stored_at"] for e in _CACHE.values()]
        return {
            "scope": "per-process (1 gunicorn worker x 2 replicas in prod)",
            "entries": len(_CACHE),
            "ttl_s": _ttl_s(),
            "negative_ttl_s": _NEG_TTL_S,
            "oldest_entry_age_s": round(max(ages), 1) if ages else None,
        }
