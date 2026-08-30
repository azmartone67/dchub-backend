"""
iso_kr_kpx.py — South Korea grid ingestion via KPX real-time page (LIVE).

Global-expansion (2026-07-11). Seventh LIVE international integration and the
fifth APAC grid (after AU/TW/JP/SG). Korea is a top-tier APAC DC market
(Seoul + Busan DCPI markets, both tagged KEPCO-KR) that the orchestrator's
health note carried as "future (KPX is API-key-gated)" — the data.go.kr API
route is key-gated, but KPX's own real-time dashboard is NOT: it embeds the
full 5-minute fuel mix + demand in the page as plain JS arrays.

DATA SOURCE — KPX 실시간 전력수급현황 (real-time supply-demand, token-free):
  https://new.kpx.or.kr/powerinfoSubmain.es?mid=a10606030000
The HTML embeds `var ictArr = [...]` — 288 five-minute slots for today, each
{coal, localCoal, gas, oil, nuclearPower, waterPower, windPower, sunlight,
 newRenewable, raisingWater, ppa, btm, regDate, ...} in MW — and the demand
chart arrays `var x` (total demand 총수요, MW) + `var t_time` (KST stamps).
Verified live 2026-07-11: gas 24.3 GW, coal 26.1 GW, nuclear 20.4 GW, demand
78.3 GW, freshest slot <10 min old. Same page electricitymaps' KPX parser
scrapes (they map coal+localCoal→coal, sunlight→solar, newRenewable→unknown
and ignore ppa/btm — mirrored here).

VERIFIED GOTCHAS (2026-07-11):
  • The page is SLOW from US IPs (14-18s observed) — GET timeout is 25s, so
    this extractor can outlive the orchestrator's per-slot budget; the thread
    still completes and persists (the run just reports late).
  • FUTURE 5-min slots are pre-filled with regDate:"0" + seq:"99999" and
    all-zero values — take the LAST slot with a real regDate and nonzero
    generation, never the array tail.
  • This is HTML scraping (regex over embedded JS), not an API contract, and
    electricitymaps routes their KPX calls through a KR proxy — treat
    US-reachability as working-today-not-guaranteed. LIVE-only handling
    below means a future geo-fence degrades to "writes nothing" + a red
    /health, never to fake data.

HONESTY:
  • LIVE-ONLY — if KPX is unreachable/blocked, writes NOTHING (no modeled
    fallback). Slots older than 2h are treated as stale and skipped.
  • SANITY BOUNDS — generation outside 30-130 GW is refused.
  • renewable_pct = wind+solar+hydro / generation_total — the SAME definition
    as the US/GB/EU/TW rows, so Korea ranks apples-to-apples. newRenewable
    (fuel cell / hydrogen / IGCC etc.) stays in the denominator but NOT in
    the renewable numerator — same treatment as Taipower's other_renewable.
  • Pumped storage (raisingWater) is storage, not primary generation —
    EXCLUDED from generation_total (matches the US scoreboard convention),
    exposed separately. ppa/btm are excluded entirely (fuel-unattributed /
    behind-the-meter estimate) — same as electricitymaps.
  ISO_CODE = "KEPCO-KR" (matches the DCPI seoul/busan market tags).
"""
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._swallowed_writes import note_swallowed_write

try:
    import requests as _rq
except Exception:
    _rq = None

iso_kr_kpx_bp = Blueprint("iso_kr_kpx", __name__, url_prefix="/api/v1/iso/kr")
SOURCE_ID = "iso-kr-kpx-live"
ISO_CODE = "KEPCO-KR"

_KPX_URL = "https://new.kpx.or.kr/powerinfoSubmain.es?mid=a10606030000"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*",
}
_KST = timezone(timedelta(hours=9))
_STALE_AFTER_H = 2                      # 5-min feed; >2h old slot = stale page
_GEN_SANE_MW = (30_000, 130_000)        # KR generation plausibility bounds


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return n if n == n else 0.0  # NaN guard


def _parse_kpx_page(html):
    """Extract the freshest populated 5-min slot + demand from the KPX page.

    Returns {"as_of_kst": iso, "cats": {...}, "demand_mw": float|None,
    "demand_as_of_kst": str|None} or None if the page shape is unrecognized.
    Pure function — unit-tested without network. Staleness/sanity are the
    caller's job (this just refuses to guess on shape drift).
    """
    if not html:
        return None
    m = re.search(r"var\s+ictArr\s*=\s*(\[.*?\])\s*;", html, re.S)
    if not m:
        return None
    try:
        slots = json.loads(m.group(1))
    except ValueError:
        return None
    best = None
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        reg = str(slot.get("regDate") or "").strip()
        if not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", reg):
            continue  # future slots carry regDate:"0"
        gen = sum(_num(slot.get(k)) for k in
                  ("coal", "localCoal", "gas", "oil", "nuclearPower",
                   "waterPower", "windPower", "sunlight", "newRenewable"))
        if gen <= 0:
            continue
        best = slot  # slots are chronological — last populated wins
    if best is None:
        return None
    try:
        as_of = datetime.strptime(
            str(best["regDate"])[:16], "%Y-%m-%d %H:%M").replace(tzinfo=_KST)
    except (KeyError, ValueError):
        return None
    cats = {
        "gas": _num(best.get("gas")),
        "coal": _num(best.get("coal")) + _num(best.get("localCoal")),
        "oil": _num(best.get("oil")),
        "nuclear": _num(best.get("nuclearPower")),
        "hydro": _num(best.get("waterPower")),
        "wind": _num(best.get("windPower")),
        "solar": _num(best.get("sunlight")),
        "other_renewable": _num(best.get("newRenewable")),
        "pumped": _num(best.get("raisingWater")),
    }

    # Demand: chart arrays `var x` (총수요 MW) + `var t_time` (KST stamps).
    # Best-effort — a redesign of the chart never kills the fuel mix.
    demand, demand_as_of = None, None
    mx = re.search(r"var\s+x\s*=\s*\[(.*?)\]", html, re.S)
    mt = re.search(r"var\s+t_time\s*=\s*\[(.*?)\]", html, re.S)
    if mx:
        xs = [v.strip().strip("'\"") for v in mx.group(1).split(",")]
        xs = [v for v in xs if re.fullmatch(r"\d+(?:\.\d+)?", v)]
        if xs:
            demand = float(xs[-1])
        if mt:
            ts = [v.strip().strip("'\"") for v in mt.group(1).split(",")]
            ts = [v for v in ts if re.fullmatch(r"\d{14}", v)]
            if ts:
                demand_as_of = ts[-1]
    return {"as_of_kst": as_of.isoformat(), "cats": cats,
            "demand_mw": demand, "demand_as_of_kst": demand_as_of}


def _live_snapshot():
    """Real live KR snapshot in scoreboard shape, or None — never modeled."""
    if _rq is None:
        return None
    try:
        r = _rq.get(_KPX_URL, headers=_HEADERS, timeout=25)
        if not r.ok:
            return None
        parsed = _parse_kpx_page(r.text)
    except Exception:
        return None
    if not parsed:
        return None
    try:
        as_of = datetime.fromisoformat(parsed["as_of_kst"])
    except (KeyError, ValueError):
        return None
    if (datetime.now(timezone.utc) - as_of) > timedelta(hours=_STALE_AFTER_H):
        return None  # frozen page — write nothing
    cats = parsed["cats"]
    # generation total EXCLUDES pumped storage (storage, not primary fuel)
    gen_total = sum(v for k, v in cats.items() if k != "pumped")
    if not (_GEN_SANE_MW[0] <= gen_total <= _GEN_SANE_MW[1]):
        return None
    renew = cats["wind"] + cats["solar"] + cats["hydro"]
    snap = {
        "generation_total_mw": {"value": round(gen_total, 0), "unit": "MW"},
        "fuel_gas_mw": {"value": round(cats["gas"], 0), "unit": "MW"},
        "fuel_coal_mw": {"value": round(cats["coal"], 0), "unit": "MW"},
        "fuel_oil_mw": {"value": round(cats["oil"], 0), "unit": "MW"},
        "fuel_nuclear_mw": {"value": round(cats["nuclear"], 0), "unit": "MW"},
        "fuel_hydro_mw": {"value": round(cats["hydro"], 0), "unit": "MW"},
        "fuel_wind_mw": {"value": round(cats["wind"], 0), "unit": "MW"},
        "fuel_solar_mw": {"value": round(cats["solar"], 0), "unit": "MW"},
        "fuel_other_renewable_mw": {"value": round(cats["other_renewable"], 0), "unit": "MW"},
        "pumped_storage_mw": {"value": round(cats["pumped"], 0), "unit": "MW"},
        "renewable_pct": {"value": round(100.0 * renew / gen_total, 1), "unit": "pct"},
        "gas_pct": {"value": round(100.0 * cats["gas"] / gen_total, 1), "unit": "pct"},
    }
    if parsed.get("demand_mw"):
        snap["demand_mw"] = {"value": round(parsed["demand_mw"], 0), "unit": "MW"}
    snap["_as_of_kst"] = parsed["as_of_kst"]
    return snap


def _persist_metrics(metrics):
    if not metrics:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        for name, data in metrics.items():
            if name.startswith("_"):
                continue
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
                note_swallowed_write("grid_data", where="iso_kr_kpx._persist_metrics")
                pass
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_kpx_realtime_page",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": ("KPX 실시간 전력수급현황 (new.kpx.or.kr, token-free, "
                   "5-min fuel mix + demand)"),
    }
    try:
        metrics = _live_snapshot()
        if metrics is None:
            summary["errors"].append(
                "kpx_live_fetch_failed_or_stale — wrote nothing (no modeled fallback)")
        else:
            summary["metrics_extracted"] = sum(
                1 for k in metrics if not k.startswith("_"))
            summary["rows_inserted"] = _persist_metrics(metrics)
            summary["as_of_kst"] = metrics["_as_of_kst"]
            summary["snapshot"] = {k: v["value"] for k, v in metrics.items()
                                   if not k.startswith("_")}
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_kr_kpx_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_kr_kpx_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    snap = _live_snapshot()
    if snap is None:
        return jsonify({"iso": ISO_CODE, "error": "kpx_live_unavailable_or_stale"}), 503
    from routes.tier_gate import jsonify_gated_snapshot
    return jsonify_gated_snapshot({
        "iso": ISO_CODE, "live": True,
        "metrics": {k: v["value"] for k, v in snap.items()
                    if not k.startswith("_")},
        "as_of_kst": snap["_as_of_kst"],
        "note": ("renewable_pct = wind+solar+hydro (matches US/GB/EU/TW). "
                 "newRenewable (fuel cell etc.) counts in the total but not "
                 "as renewable; pumped storage excluded from the total. "
                 "demand_mw is KPX 총수요 (total incl. behind-the-meter est)."),
        "source": "KPX real-time supply-demand (live, 5-min)",
    }, 200)
# AUTO-REPAIR: duplicate route '/health' also in main.py:7791 — review and remove one


@iso_kr_kpx_bp.route("/health", methods=["GET"])
def http_health():
    snap = _live_snapshot()
    return jsonify({"iso": ISO_CODE, "live_feed_ok": snap is not None,
                    "source": "kpx_realtime_page_tokenfree"}), 200
