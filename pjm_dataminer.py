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


# ── gridstatus.io path (PRIMARY) ─────────────────────────────────────────────
# DOM-zone load + LMP are ALSO carried by gridstatus.io, for which DC Hub already
# has a provisioned key (GRIDSTATUS_API_KEY) — so Ashburn lights up with NO PJM
# Data Miner 2 registration. gridstatus is tried first; the DM2 client below is
# the fallback used only when PJM_API_KEY is set and gridstatus is unavailable.
GRIDSTATUS_BASE = "https://api.gridstatus.io/v1"


def gridstatus_key():
    return (os.environ.get("GRIDSTATUS_API_KEY") or "").strip()


def _gridstatus_get(dataset, params=None, timeout=15):
    """GET one gridstatus.io dataset query. Returns (rows_list, error_str)."""
    key = gridstatus_key()
    if not key:
        return None, "source_unavailable: GRIDSTATUS_API_KEY not set"
    p = {"api_key": key}
    if params:
        p.update(params)
    url = GRIDSTATUS_BASE + "/datasets/" + dataset + "/query?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    import time as _t
    for _attempt in range(2):  # gridstatus free tier = 1 req/sec; retry once on 429
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
            rows = payload.get("data") if isinstance(payload, dict) else payload
            return (rows or []), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and _attempt == 0:
                _t.sleep(1.1)
                continue
            return None, f"http_{e.code}"
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:120]}"
    return None, "http_429"


def _gridstatus_dom(base):
    """DOM-zone load + real-time LMP from gridstatus.io. Returns a dict or None
    (None → caller falls back to Data Miner 2 or the fail-closed marker)."""
    out = dict(base)
    got = False
    # Dominion-zone load — the `dom` column of the PJM system-load dataset (MW).
    rows, lerr = _gridstatus_get("pjm_load", {"limit": 1, "order": "desc"})
    if rows:
        out["demand_mw"] = _num(rows[0].get("dom"))
        out["demand_period"] = rows[0].get("interval_start_utc")
        got = out.get("demand_mw") is not None
    elif lerr:
        out["load_error"] = lerr
    # Dominion-zone real-time LMP (5-min), location=DOM — the Ashburn power price.
    rt, rterr = _gridstatus_get("pjm_lmp_real_time_5_min",
                                {"filter_column": "location", "filter_value": "DOM",
                                 "limit": 1, "order": "desc"})
    if rt:
        out["lmp_rt_usd_mwh"] = _num(rt[0].get("lmp"))
        out["lmp_congestion_usd_mwh"] = _num(rt[0].get("congestion"))
        out["lmp_loss_usd_mwh"] = _num(rt[0].get("loss"))
        out["lmp_period"] = rt[0].get("interval_start_utc")
        got = got or out.get("lmp_rt_usd_mwh") is not None
    elif rterr:
        out["lmp_rt_error"] = rterr
    if not got:
        return None
    out["source"] = "PJM via gridstatus.io (pjm_load.dom + pjm_lmp_real_time_5_min zone=DOM)"
    out["note"] = ("DOM-zone LOAD + real-time LMP are zone-level (Ashburn / Northern "
                   "Virginia); PJM does not publish generation per zone.")
    return out


def pjm_dom_zone():
    """Dominion (DOM) zone brief: current load + RT LMP. Fail-closed.

    Source order: gridstatus.io (PRIMARY — key already provisioned) → PJM Data
    Miner 2 (fallback, needs PJM_API_KEY) → source_unavailable marker."""
    base = {
        "region": "PJM-DOM", "iso": "PJM", "zone": "DOMINION",
        "zone_name": "PJM Dominion zone — Ashburn / Northern Virginia (world's #1 DC market)",
    }
    import time as _t
    _now0 = _t.time()
    _cache = _PJM_CACHE.get("dom")
    if _cache and _cache[0] > _now0:
        return dict(_cache[1])

    # PRIMARY: gridstatus.io (no PJM registration required)
    if gridstatus_key():
        gs = _gridstatus_dom(base)
        if gs:
            _PJM_CACHE["dom"] = (_now0 + _PJM_TTL, dict(gs))
            return gs

    # FALLBACK: PJM Data Miner 2 — only if a DM2 key is set.
    if not is_enabled():
        base.update({
            "source_unavailable": True,
            "needs": "GRIDSTATUS_API_KEY (provisioned) or PJM_API_KEY (dataminer2.pjm.com)",
            "note": ("EIA-930 has no sub-BA DOM data; DOM-zone load + LMP come from "
                     "gridstatus.io (primary) or PJM Data Miner 2 (fallback)."),
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
