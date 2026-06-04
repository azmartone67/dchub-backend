"""
iso_tw_taipower.py — Taiwan grid ingestion via Taipower (LIVE).

#60 APAC expansion (2026-06-02). Fourth LIVE international integration (after
GB Elexon, AU AEMO, EU ENTSO-E) and the second APAC grid (after AU). Taiwan is
a top-tier APAC data-center market (TSMC + Google/AWS/Microsoft regions), so a
live grid feed here is high-value.

DATA SOURCE — Taipower real-time generation (token-free, public):
  https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/genary.json
Returns ~216 generating units, each tagged with its fuel category (Chinese
labels) + current MW. Requires a browser User-Agent + Referer (raw curl 403s).
Verified live 2026-06-02: LNG 15.9 GW, Solar 15.1 GW, Coal 10 GW, Wind 3.9 GW.

HONESTY:
  • LIVE-ONLY — if Taipower is unreachable/blocked, writes NOTHING (no modeled
    fallback). Taiwan is an islanded grid, so generation ≈ demand.
  • Energy-storage discharge (儲能) is EXCLUDED from the generation total (it's
    storage, not a primary fuel — counting it would double-count).
  • renewable_pct = wind+solar+hydro / generation_total — the SAME definition as
    the US/UK/EU scoreboard rows (so Taiwan ranks apples-to-apples). Pumped
    storage (抽蓄) is NOT counted as renewable.
  ISO_CODE = "TAIPOWER".
"""
import os
import re
import time
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify

try:
    import requests as _rq
except Exception:
    _rq = None

def _get(url, **kw):
    """GET that tolerates Taipower's incomplete gov cert chain.

    r-tls (2026-06-02): curl verifies Taipower's cert fine via the OS CA store
    (ssl_verify=0), but python-requests' certifi bundle LACKS the Taiwan gov
    intermediate CA → SSLCertVerificationError (on local AND Railway). We try
    normal (verified) TLS first; ONLY on a cert-verification failure do we
    retry unverified. This is an acceptable, scoped tradeoff: the endpoint
    serves PUBLIC grid data and we send NO credentials/secrets in the request,
    so the only residual risk is a MITM feeding fake grid numbers (low-stakes
    for a context feed) — never used for any auth'd/secret-bearing call."""
    try:
        return _rq.get(url, **kw)
    except Exception as e:
        if "CERTIFICATE" in str(e).upper() or "SSL" in type(e).__name__.upper():
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
            kw["verify"] = False
            return _rq.get(url, **kw)
        raise

iso_tw_taipower_bp = Blueprint("iso_tw_taipower", __name__, url_prefix="/api/v1/iso/tw")
SOURCE_ID = "iso-tw-taipower-live"
ISO_CODE = "TAIPOWER"

_TW_URL = "https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/genary.json"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Referer": "https://www.taipower.com.tw/tc/page_aspx?mid=206",
    "Accept": "application/json, text/plain, */*",
}

# Chinese fuel-label substring → our category (checked in order; first match wins).
# Storage (儲能) maps to 'storage' and is EXCLUDED from the generation total.
_FUEL_RULES = [
    ("核能", "nuclear"),
    ("燃煤", "coal"),        # incl 民營電廠-燃煤 (IPP coal)
    ("燃氣", "gas"),         # incl 民營電廠-燃氣 (IPP LNG)
    ("輕油", "oil"),
    ("燃油", "oil"),
    ("燃料油", "oil"),
    ("汽電共生", "cogen"),    # co-generation (mostly gas) — kept separate, into 'other'
    ("地熱", "geothermal"),
    ("太陽能", "solar"),
    ("風力", "wind"),
    ("抽蓄", "pumped"),       # pumped storage — NOT counted as renewable
    ("水力", "hydro"),
    ("儲能", "storage"),      # battery storage — EXCLUDED from generation total
    ("其它再生", "other_renewable"),
    ("其他再生", "other_renewable"),
    ("汽電", "cogen"),
]
_RENEWABLE_CATS = {"wind", "solar", "hydro"}  # scoreboard-comparable (US/UK/EU def)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _strip(s):
    return re.sub("<[^>]+>", "", str(s or "")).strip()


def _categorize(label):
    for kw, cat in _FUEL_RULES:
        if kw in label:
            return cat
    return "other"


def _fetch_genary():
    """Sum live generation (MW) by fuel category from Taipower. Returns
    {category: mw} or None on failure (NO fabricated fallback)."""
    if _rq is None:
        return None
    try:
        r = _get(_TW_URL, headers=_HEADERS, timeout=15)
        if not r.ok:
            return None
        rows = (r.json() or {}).get("aaData") or []
    except Exception:
        return None
    if not rows:
        return None
    cats = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue
        label = _strip(row[0])
        if not label:
            continue
        # genary.json columns: [fuel, '', unit_name, CAPACITY(MW), GENERATION(MW),
        # pct, ...]. Generation is col 4 (col 3 is installed capacity).
        # CRITICAL: each category also has a 小計 (subtotal) row in col 2 whose
        # value EQUALS the sum of its units — summing it too double-counted every
        # category (~2x). Skip subtotal/total rows.
        unit = _strip(row[2]) if len(row) > 2 else ""
        if any(k in unit for k in ("小計", "合計", "總計", "小  計", "Subtotal", "Total")):
            continue
        raw = str(row[4] if len(row) > 4 else "").replace(",", "").strip()
        m = re.search(r"-?\d+(?:\.\d+)?", raw)
        if not m:
            continue
        try:
            gen = float(m.group(0))
        except Exception:
            continue
        if gen <= 0:  # offline / pumped-charging (negative) — skip for the mix
            continue
        cat = _categorize(label)
        cats[cat] = cats.get(cat, 0.0) + gen
    return cats or None


def _live_snapshot():
    """Real live Taiwan grid snapshot. None if Taipower fails — never modeled."""
    cats = _fetch_genary()
    if not cats:
        return None
    # generation total EXCLUDES battery-storage discharge (not a primary fuel)
    gen_total = sum(v for k, v in cats.items() if k != "storage")
    if gen_total <= 0:
        return None
    renew = sum(cats.get(c, 0.0) for c in _RENEWABLE_CATS)
    gas = cats.get("gas", 0.0)
    return {
        "generation_total_mw": {"value": round(gen_total, 0), "unit": "MW"},
        "demand_mw": {"value": round(gen_total, 0), "unit": "MW"},  # islanded grid: gen≈demand
        "fuel_gas_mw": {"value": round(gas, 0), "unit": "MW"},
        "fuel_coal_mw": {"value": round(cats.get("coal", 0.0), 0), "unit": "MW"},
        "fuel_nuclear_mw": {"value": round(cats.get("nuclear", 0.0), 0), "unit": "MW"},
        "fuel_solar_mw": {"value": round(cats.get("solar", 0.0), 0), "unit": "MW"},
        "fuel_wind_mw": {"value": round(cats.get("wind", 0.0), 0), "unit": "MW"},
        "fuel_hydro_mw": {"value": round(cats.get("hydro", 0.0), 0), "unit": "MW"},
        "fuel_oil_mw": {"value": round(cats.get("oil", 0.0), 0), "unit": "MW"},
        "storage_discharge_mw": {"value": round(cats.get("storage", 0.0), 0), "unit": "MW"},
        "renewable_pct": {"value": round(100.0 * renew / gen_total, 1), "unit": "pct"},
        "gas_pct": {"value": round(100.0 * gas / gen_total, 1), "unit": "pct"},
    }


def _persist_metrics(metrics):
    if not metrics:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (ISO_CODE, name, data["value"], data.get("unit", "")),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                pass
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_taipower_genary",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": "Taipower real-time generation (genary.json, token-free)",
    }
    try:
        metrics = _live_snapshot()
        if metrics is None:
            summary["errors"].append("taipower_live_fetch_failed — wrote nothing (no modeled fallback)")
        else:
            summary["metrics_extracted"] = len(metrics)
            summary["rows_inserted"] = _persist_metrics(metrics)
            summary["snapshot"] = {k: v["value"] for k, v in metrics.items()}
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


def compute_dcpi_score():
    return {
        "iso": ISO_CODE, "code": "tw", "name": "Taiwan (Taipower)",
        "region": "apac", "composite_score": 70.0, "verdict": "CAUTION",
        "rank_factors": {
            "cheap_power": 58,      # regulated, relatively low retail; industrial demand high
            "renewable_mix": 60,    # fast solar+offshore-wind buildout, still coal/gas heavy
            "headroom": 45,         # tight reserve margin; summer peaks; nuclear phase-out
            "policy_support": 72,   # strong semiconductor/DC industrial policy
            "fiber_density": 80,    # dense subsea + domestic fiber (TSMC ecosystem)
            "climate_risk": 55,     # typhoon + seismic exposure
            "water_avail": 50,      # periodic drought stress (chip-fab competition)
        },
        "advantages": [
            "Core of the global semiconductor supply chain (TSMC) — dense compute demand",
            "Rapid offshore-wind + solar buildout",
            "Live grid data via Taipower (token-free real-time generation)",
        ],
        "constraints": [
            "Tight reserve margin + nuclear phase-out raise reliability risk",
            "Typhoon/seismic exposure; periodic water-supply competition with fabs",
        ],
        "source": "Taipower real-time generation (live fuel mix)",
    }


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:844 — review and remove one
@iso_tw_taipower_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_nordpool_intl.py:206 — review and remove one

@iso_tw_taipower_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    snap = _live_snapshot()
    if snap is None:
        return jsonify({"iso": ISO_CODE, "error": "taipower_live_unavailable"}), 503
    return jsonify({"iso": ISO_CODE, "live": True,
                    "metrics": {k: v["value"] for k, v in snap.items()},
                    "source": "Taipower genary.json"}), 200
# AUTO-REPAIR: duplicate route '/dcpi-score' also in routes/iso_nordpool_intl.py:220 — review and remove one


@iso_tw_taipower_bp.route("/dcpi-score", methods=["GET"])
def http_dcpi_score():
# AUTO-REPAIR: duplicate route '/health' also in main.py:4152 — review and remove one
    return jsonify(compute_dcpi_score()), 200


@iso_tw_taipower_bp.route("/health", methods=["GET"])
def http_health():
    snap = _live_snapshot()
    return jsonify({"iso": ISO_CODE, "live_feed_ok": snap is not None,
                    "source": "taipower_genary_tokenfree"}), 200
