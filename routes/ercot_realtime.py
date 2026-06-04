"""
ERCOT real-time prices + load via the ERCOT Public API (r71, 2026-06-04)
========================================================================
Live system-wide Real-Time Settlement Point Price (NP6-905-CD) + actual system
load (NP6-345-CD) from api.ercot.com. SEPARATE from the interconnection queue
(that's the public GIS Report, already live, no auth).

AUTH (owner-activated): the ERCOT Public API needs an Ocp-Apim-Subscription-Key
header PLUS a Bearer token obtained via an Azure-B2C ROPC password grant. This
module reads three Railway env vars the OWNER sets — it never hardcodes secrets:
    ERCOT_API_KEY        (the Ocp-Apim-Subscription-Key)
    ERCOT_API_USERNAME   (the registered ERCOT account username)
    ERCOT_API_PASSWORD   (that account's password)
If any are missing the endpoint is a clean no-op ({available: false, ...}). The
client_id below is ERCOT's PUBLIC B2C app id (the same constant every public-API
user sends — not a secret).

Spec verified against gridstatus's production ercot_api connector. Column names
inside each report's `data`/`fields` envelope can vary, so parsing is defensive
(fuzzy field match); confirm the picked columns on the owner's first live run.
"""
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error

from flask import Blueprint, jsonify

ercot_rt_bp = Blueprint("ercot_rt", __name__)

_TOKEN_URL = ("https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
              "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token")
_CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"   # ERCOT PUBLIC B2C app id (not a secret)
_API_BASE = "https://api.ercot.com/api/public-reports"
_PRICE_PATH = "/np6-905-cd/spp_node_zone_hub"          # RT Settlement Point Prices (15-min)
_LOAD_PATH = "/np6-345-cd/act_sys_load_by_wzn"         # Actual System Load by weather zone

# module-level caches (per gunicorn worker; fine for a read endpoint)
_token_cache = {"id_token": None, "exp": 0.0}
_data_cache = {"payload": None, "exp": 0.0}
_DATA_TTL = 300        # 5 min — protects the single replica + the APIM 429 quota
_TOKEN_TTL = 55 * 60   # refresh the ~60-min token a little early


def _creds():
    k = os.environ.get("ERCOT_API_KEY")
    u = os.environ.get("ERCOT_API_USERNAME")
    p = os.environ.get("ERCOT_API_PASSWORD")
    return (k, u, p) if (k and u and p) else None


def _get_token(username, password):
    now = time.time()
    if _token_cache["id_token"] and _token_cache["exp"] > now:
        return _token_cache["id_token"]
    form = urllib.parse.urlencode({
        "grant_type": "password",
        "username": username,
        "password": password,
        "response_type": "id_token",
        "scope": f"openid {_CLIENT_ID} offline_access",
        "client_id": _CLIENT_ID,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=form, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read().decode())
    idt = tok.get("id_token")
    if idt:
        _token_cache["id_token"] = idt
        _token_cache["exp"] = now + _TOKEN_TTL
    return idt


def _fetch_report(path, key, token, params=None):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(_API_BASE + path + qs, headers={
        "Authorization": f"Bearer {token}",
        "Ocp-Apim-Subscription-Key": key,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _pick(fields, *needles):
    """Return the index of the first field whose name contains any needle (ci)."""
    for i, f in enumerate(fields):
        nm = str((f or {}).get("name", "")).lower()
        if any(n in nm for n in needles):
            return i
    return None


def get_ercot_realtime():
    """Live ERCOT RT price (HB_HUBAVG) + system load. Cached; env-gated."""
    now = time.time()
    if _data_cache["payload"] and _data_cache["exp"] > now:
        return _data_cache["payload"]

    creds = _creds()
    if not creds:
        return {"available": False,
                "note": "ERCOT real-time disabled — owner must set ERCOT_API_KEY, "
                        "ERCOT_API_USERNAME, ERCOT_API_PASSWORD in Railway.",
                "source": "ERCOT Public API (api.ercot.com)"}
    key, user, pw = creds
    out = {"available": True, "source": "ERCOT Public API (api.ercot.com)",
           "as_of": None, "price": None, "load": None, "errors": {}}
    try:
        token = _get_token(user, pw)
        if not token:
            return {"available": False, "note": "ERCOT token fetch returned no id_token",
                    "source": out["source"]}
    except Exception as e:
        return {"available": False, "note": f"ERCOT token error: {type(e).__name__}",
                "source": out["source"]}

    # --- RT price (system hub average) ---
    try:
        pr = _fetch_report(_PRICE_PATH, key, token,
                           {"settlementPoint": "HB_HUBAVG", "size": 60})
        fields = pr.get("fields") or []
        rows = pr.get("data") or []
        if rows:
            pi = _pick(fields, "settlementpointprice", "spp", "price")
            ti = _pick(fields, "interval", "timestamp", "delivery", "scedtimestamp")
            last = rows[-1]
            out["price"] = {
                "settlement_point": "HB_HUBAVG",
                "price_usd_mwh": (float(last[pi]) if pi is not None and last[pi] is not None else None),
                "interval": (last[ti] if ti is not None and ti < len(last) else None),
                "unit": "$/MWh",
            }
            out["as_of"] = out["price"]["interval"]
    except Exception as e:
        out["errors"]["price"] = f"{type(e).__name__}: {str(e)[:120]}"

    # --- actual system load (sum weather zones / total) ---
    try:
        ld = _fetch_report(_LOAD_PATH, key, token, {"size": 30})
        fields = ld.get("fields") or []
        rows = ld.get("data") or []
        if rows:
            last = rows[-1]
            ti = _pick(fields, "operatingday", "timestamp", "interval", "hourending")
            tot_i = _pick(fields, "total", "systemtotal", "ercot")
            if tot_i is not None and last[tot_i] is not None:
                load_mw = float(last[tot_i])
            else:
                # sum numeric weather-zone columns (skip the timestamp col)
                load_mw = 0.0
                for i, v in enumerate(last):
                    if i == ti:
                        continue
                    try:
                        load_mw += float(v)
                    except (TypeError, ValueError):
                        pass
            out["load"] = {"system_load_mw": round(load_mw, 1),
                           "interval": (last[ti] if ti is not None and ti < len(last) else None)}
    except Exception as e:
        out["errors"]["load"] = f"{type(e).__name__}: {str(e)[:120]}"

    _data_cache["payload"] = out
    _data_cache["exp"] = now + _DATA_TTL
    return out


@ercot_rt_bp.route("/api/v1/ercot/realtime", methods=["GET"])
def ercot_realtime():
    out = get_ercot_realtime()
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=120"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
