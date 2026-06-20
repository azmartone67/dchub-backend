"""
pjm_dataminer.py — PJM Data Miner 2 client + Dominion (DOM) zone brief.
═══════════════════════════════════════════════════════════════════════════════
WHY: Ashburn / Northern Virginia is the world's #1 data-center market but is
INVISIBLE below the PJM RTO aggregate — EIA-930 publishes NO sub-BA data for the
PJM Dominion (DOM) zone (verified: region-data + fuel-type both return 0 rows for
respondent=DOM). PJM's own Data Miner 2 DOES publish DOM-zone telemetry:
  • inst_load        area="DOM"        → Dominion-zone instantaneous load (MW)
  • rt_hrl_lmps      zone="DOMINION"   → Dominion-zone real-time LMP ($/MWh)
  • da_hrl_lmps      zone="DOMINION"   → Dominion-zone day-ahead LMP ($/MWh)
  • gen_by_fuel                        → PJM RTO fuel mix (RTO-level only; PJM
                                         does NOT publish generation per zone)
DOM-zone LMP is the actual power price in Ashburn — the number a data-center
developer screens on — and no public feed other than PJM carries it.

FAIL-CLOSED: every path needs PJM_API_KEY (a free Data Miner 2 subscription key
from dataminer2.pjm.com). Without it, calls return a clear `source_unavailable`
marker — the module is dormant and NEVER crashes. Set PJM_API_KEY on the backend
Railway service to activate.

Auth: header `Ocp-Apim-Subscription-Key`. Base: https://api.pjm.com/api/v1/.
Rate limits: non-members 6 req/min, members 600/min — keep calls light + cached.
Endpoint + field names verified against the gridstatus open-source PJM client.

NOTE: the Data Miner 2 query-param/date-range syntax is verified on the FIRST
keyed run (we can't exercise the happy path without a key). To de-risk that, we
fetch a small recent window and filter area/zone CLIENT-SIDE rather than relying
on PJM's exact server-side filter encoding.
"""
import os
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error

PJM_BASE = "https://api.pjm.com/api/v1/"
UA = "DCHub-PJM/1.0 (+https://dchub.cloud)"

# small in-process cache: PJM rate limit is tight (6/min for non-members)
_PJM_CACHE = {}
_PJM_TTL = 300  # 5 min — DOM load/LMP move on a 5-min/hourly cadence


def pjm_key():
    return (os.environ.get("PJM_API_KEY") or "").strip()


def is_enabled():
    return bool(pjm_key())


def _pjm_get(feed, extra_params=None, timeout=12):
    """GET a Data Miner 2 feed. Returns (rows_list, error_str). Fail-closed."""
    key = pjm_key()
    if not key:
        return None, "source_unavailable: PJM_API_KEY not set"
    params = {"rowCount": 100, "startRow": 1, "format": "json"}
    if extra_params:
        params.update(extra_params)
    url = PJM_BASE + feed + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Ocp-Apim-Subscription-Key": key,
        "Accept": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
        # DM2 returns {"items":[...], "totalRows":N} (format=json) or a bare list
        if isinstance(payload, dict):
            rows = payload.get("items") or payload.get("data") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        return rows, None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:160]
        except Exception:
            pass
        return None, f"http_{e.code}: {body}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:140]}"


def _ept_window(hours=4):
    """A recent datetime_beginning_ept range in PJM's 'M/D/YYYY HH:MM to ...' form."""
    now = datetime.datetime.utcnow() - datetime.timedelta(hours=4)  # rough EDT
    start = now - datetime.timedelta(hours=hours)
    fmt = "%-m/%-d/%Y %H:%M"
    try:
        return f"{start.strftime(fmt)} to {now.strftime(fmt)}"
    except ValueError:  # Windows/strftime without %-
        return f"{start.strftime('%m/%d/%Y %H:%M')} to {now.strftime('%m/%d/%Y %H:%M')}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _latest(rows, ts_keys=("datetime_beginning_utc", "datetime_beginning_ept")):
    """Most recent row by the first present timestamp key."""
    if not rows:
        return None
    key = next((k for k in ts_keys if rows and k in rows[0]), None)
    if not key:
        return rows[0]
    try:
        return max(rows, key=lambda r: str(r.get(key) or ""))
    except Exception:
        return rows[0]


def pjm_dom_zone():
    """Dominion (DOM) zone brief: current load + RT/DA LMP. Fail-closed.

    Returns a dict shaped to slot into the grid-intelligence regions, or a
    `source_unavailable` marker when PJM_API_KEY is unset."""
    base = {
        "region": "PJM-DOM", "iso": "PJM", "zone": "DOMINION",
        "zone_name": "PJM Dominion zone — Ashburn / Northern Virginia (world's #1 DC market)",
    }
    if not is_enabled():
        base.update({
            "source_unavailable": True,
            "needs": "PJM_API_KEY (free Data Miner 2 subscription key — dataminer2.pjm.com)",
            "note": ("EIA-930 has no sub-BA DOM data; DOM-zone load + LMP activate "
                     "the moment PJM_API_KEY is set on the backend Railway service."),
        })
        return base

    cache_hit = _PJM_CACHE.get("dom")
    import time as _t
    now = _t.time()
    if cache_hit and cache_hit[0] > now:
        return dict(cache_hit[1])

    window = _ept_window()
    out = dict(base)

    # DOM-zone instantaneous load (filter area client-side for robustness)
    rows, err = _pjm_get("inst_load", {"datetime_beginning_ept": window})
    if rows:
        dom = [r for r in rows if str(r.get("area") or "").upper() in ("DOM", "DOMINION")]
        latest = _latest(dom or [])
        if latest:
            out["demand_mw"] = _num(latest.get("instantaneous_load"))
            out["demand_period"] = latest.get("datetime_beginning_ept") or latest.get("datetime_beginning_utc")
    if err:
        out["load_error"] = err

    # DOM-zone real-time LMP
    rt, rterr = _pjm_get("rt_hrl_lmps", {"datetime_beginning_ept": window})
    if rt:
        dom = [r for r in rt if "DOMINION" in str(r.get("zone") or "").upper()
               or "DOM" == str(r.get("zone") or "").upper()]
        latest = _latest(dom or [])
        if latest:
            out["lmp_rt_usd_mwh"] = _num(latest.get("total_lmp_rt"))
            out["lmp_pnode"] = latest.get("pnode_name")
            out["lmp_period"] = latest.get("datetime_beginning_ept") or latest.get("datetime_beginning_utc")
    if rterr:
        out["lmp_rt_error"] = rterr

    out["source"] = "PJM Data Miner 2 (inst_load area=DOM + rt_hrl_lmps zone=DOMINION)"
    out["note"] = ("DOM-zone LOAD + LMP are zone-level; generation_mix is PJM-RTO only "
                   "(PJM does not publish generation per zone).")
    _PJM_CACHE["dom"] = (now + _PJM_TTL, dict(out))
    return out


def pjm_rto_fuel_mix():
    """PJM RTO generation by fuel (gen_by_fuel) — PJM's authoritative 10-category
    mix (richer than EIA's 7). Fail-closed. Returns (mix_dict, error)."""
    if not is_enabled():
        return None, "source_unavailable: PJM_API_KEY not set"
    rows, err = _pjm_get("gen_by_fuel", {"datetime_beginning_ept": _ept_window(hours=2)})
    if err:
        return None, err
    latest = _latest(rows or [])
    if not latest:
        return None, "no_rows"
    ts = latest.get("datetime_beginning_ept") or latest.get("datetime_beginning_utc")
    mix = {}
    for r in (rows or []):
        if (r.get("datetime_beginning_utc") == latest.get("datetime_beginning_utc")
                and r.get("fuel_type")):
            mix[r["fuel_type"]] = {"mw": _num(r.get("mw")),
                                   "is_renewable": r.get("is_renewable")}
    return {"period": ts, "mix": mix, "source": "PJM Data Miner 2 gen_by_fuel"}, None


if __name__ == "__main__":
    # Self-test: prints the fail-closed marker (no key) or the live DOM brief.
    print("PJM_API_KEY set:", is_enabled())
    print(json.dumps(pjm_dom_zone(), indent=2))
