"""
iso_jp_denkiyoho.py — Japan grid ingestion via TSO "denki-yoho" CSVs (LIVE).

Daily-content-feeds #2 (2026-07-01), APAC expansion. Fifth LIVE international
integration (after GB Elexon, AU AEMO, EU ENTSO-E, TW Taipower). Japan is the
largest APAC data-center market (Tokyo/Osaka hyperscale + AI buildout), and
until now the orchestrator's own health endpoint listed it under future_isos.

DATA SOURCE — the 10 Japanese transmission operators each publish a public
"denki-yoho" (electricity forecast) CSV: hourly area demand (actual +
forecast), peak supply capability and reserve margin. Same file layout across
TSOs, token-free. 6 of the 10 areas are ingested here — every URL below was
curl-verified LIVE (fresh same-day UPDATE stamp) on 2026-07-02 JST:

  TEPCO    (Tokyo)    fixed URL   juyo-d1-j.csv
  Chubu    (Nagoya)   fixed URL   juyo_cepco003.csv
  Kyushu   (Fukuoka)  dated URL   juyo-hourly-<YYYYMMDD>.csv
  Hokuriku (Toyama)   dated URL   juyo_05_<YYYYMMDD>.csv
  Shikoku  (Takamatsu)dated URL   juyo_08_<YYYYMMDD>.csv
  Okinawa  (Naha)     dated URL   juyo_10_<YYYYMMDD>.csv

  EXCLUDED (honesty > coverage): Kansai's legacy juyo CSVs are STALE mirrors
  (juyo1_kansai.csv last updated 2025-12-25, juyo2 2023) — its live data moved
  to a different "yamasou_*" format; Tohoku's juyo_tohoku.csv froze in 2016;
  Hokkaido/Chugoku expose no verifiable same-format CSV. Rather than parse a
  lookalike file and serve stale numbers as live, those 4 areas are skipped.

VERIFIED GOTCHAS (2026-07-02):
  • Values are 万kW (10,000 kW) → multiply ×10 for MW.
  • Encoding is Shift-JIS (decode cp932; TEPCO/Chubu/Kyushu/etc. all match).
  • Timestamps are JST (UTC+9). Line 1 is "YYYY/M/D H:MM UPDATE".
  • Hourly table header: DATE,TIME,当日実績(万kW),予測値(万kW),使用率(%),供給力(万kW)
    (Shikoku says 供給力想定値; Chubu prefixes 需要 on the peak blocks — the
    parser matches on stable substrings, not exact headers).
  • Future hours carry 実績=0 → take the LAST row with actual > 0.
  • A second DATE,TIME table holds 5-min demand + solar output (太陽光).

HONESTY:
  • LIVE-ONLY — an unreachable TSO writes NOTHING (no modeled fallback), and
    one TSO down never blocks the others (per-TSO isolation + thread fan-out).
  • STALENESS GUARD — a file whose UPDATE stamp is >26h old is treated as a
    dead mirror and skipped (this is exactly how Kansai/Tohoku would have
    poisoned the feed).
  • Every snapshot carries source attribution + the as-of JST timestamp of the
    demand reading actually used.
  ISO_CODE map: TEPCO (matches the existing DCPI Tokyo market tag) and
  JP_<AREA> for the rest, rolled up under the OCCTO aggregate — same
  aggregate-plus-zones convention as ENTSO-E (ENTSOE + EU_<code>).
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2 as _pg
from flask import Blueprint, jsonify

try:
    import requests as _rq
except Exception:
    _rq = None

iso_jp_denkiyoho_bp = Blueprint("iso_jp_denkiyoho", __name__, url_prefix="/api/v1/iso/jp")
SOURCE_ID = "iso-jp-denkiyoho-live"
ISO_CODE = "OCCTO"  # Japan-wide aggregate (OCCTO = the inter-area coordinator)

_JST = timezone(timedelta(hours=9))
_STALE_AFTER_H = 26  # >26h-old UPDATE stamp = dead mirror, write nothing

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "text/csv, text/plain, */*",
}

# code → (grid_data iso, display name, url or url-template with {ymd} in JST)
# Every URL curl-verified live (same-day UPDATE stamp) 2026-07-02 JST.
_TSOS = {
    "tepco":    ("TEPCO",       "TEPCO (Tokyo)",
                 "https://www.tepco.co.jp/forecast/html/images/juyo-d1-j.csv"),
    "chubu":    ("JP_CHUBU",    "Chubu (Nagoya)",
                 "https://powergrid.chuden.co.jp/denki_yoho_content_data/juyo_cepco003.csv"),
    "kyushu":   ("JP_KYUSHU",   "Kyushu (Fukuoka)",
                 "https://www.kyuden.co.jp/td_power_usages/csv/juyo-hourly-{ymd}.csv"),
    "hokuriku": ("JP_HOKURIKU", "Hokuriku (Toyama)",
                 "https://www.rikuden.co.jp/nw/denki-yoho/csv/juyo_05_{ymd}.csv"),
    "shikoku":  ("JP_SHIKOKU",  "Shikoku (Takamatsu)",
                 "https://www.yonden.co.jp/nw/denkiyoho/juyo_08_{ymd}.csv"),
    "okinawa":  ("JP_OKINAWA",  "Okinawa (Naha)",
                 "https://www.okiden.co.jp/denki2/juyo_10_{ymd}.csv"),
}

# Not ingested — kept here so the health endpoint can say WHY, honestly.
_EXCLUDED_AREAS = {
    "kansai":   "legacy juyo CSVs are stale mirrors (2023/2025); live data moved to a different yamasou_* format",
    "tohoku":   "juyo_tohoku.csv frozen since 2016 (stale mirror)",
    "hokkaido": "no verifiable same-format public CSV found",
    "chugoku":  "no verifiable same-format public CSV found (page is JS-embedded)",
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _num(s):
    """First number in a CSV cell, or None. Tolerates commas + full-width junk."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or "").replace(",", ""))
    try:
        return float(m.group(0)) if m else None
    except Exception:
        return None


def _parse_update_stamp(line):
    """'2026/7/2 9:10 UPDATE' (JST) → aware datetime, or None."""
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", line or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), tzinfo=_JST)
    except ValueError:
        return None


def _parse_denkiyoho(text):
    """Parse one denki-yoho CSV (already decoded). Returns dict or None.

    All 万kW values are converted ×10 → MW here. Never fabricates: any block
    that can't be parsed is simply absent from the result.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    if not lines:
        return None

    updated = _parse_update_stamp(lines[0])
    if updated is None:
        return None  # not a denki-yoho file — refuse to guess
    if (datetime.now(_JST) - updated) > timedelta(hours=_STALE_AFTER_H):
        return {"stale": True, "updated_jst": updated.isoformat()}

    out = {"stale": False, "updated_jst": updated.isoformat()}

    section = None  # None | 'hourly' | '5min'
    for i, ln in enumerate(lines):
        # ---- peak blocks (value is on the line AFTER the header) ----
        # ピーク時供給力(万kW) → peak supply capability; cols 5/6 (when present)
        # are 予備率(%) / 使用率(%). Chubu prefixes 需要; substring match covers it.
        if "ピーク時供給力(万kW)" in ln and "使用率ピーク" not in ln and i + 1 < len(lines):
            vals = lines[i + 1].split(",")
            v = _num(vals[0])
            if v is not None:
                out["peak_supply_capacity_mw"] = v * 10.0
            if "予備率" in ln:
                cols = ln.split(",")
                for j, h in enumerate(cols):
                    if "予備率" in h and j < len(vals):
                        rv = _num(vals[j])
                        if rv is not None:
                            out["reserve_margin_pct"] = rv
        if "予想最大電力(万kW)" in ln and "使用率ピーク" not in ln and i + 1 < len(lines):
            v = _num(lines[i + 1].split(",")[0])
            if v is not None:
                out["peak_demand_forecast_mw"] = v * 10.0

        # ---- tabular sections ----
        if ln.startswith("DATE,TIME"):
            section = "5min" if ("５分" in ln or "5分" in ln) else "hourly"
            continue
        if section and not ln:
            section = None
            continue
        if section and ln[:1].isdigit():
            cells = ln.split(",")
            if len(cells) < 3:
                continue
            actual = _num(cells[2])
            if section == "hourly" and actual and actual > 0:
                # DATE,TIME,当日実績(万kW),予測値(万kW),使用率(%),供給力(万kW)
                # future hours are 0 → keep overwriting, last positive row wins
                out["demand_mw"] = actual * 10.0
                out["as_of_jst"] = f"{cells[0]} {cells[1]} JST"
                if len(cells) > 4:
                    u = _num(cells[4])
                    if u is not None:
                        out["usage_pct"] = u
                if len(cells) > 5:
                    s = _num(cells[5])
                    if s is not None and s > 0:
                        out["supply_capacity_mw"] = s * 10.0
            elif section == "5min" and actual and actual > 0:
                # DATE,TIME,当日実績(5分間隔値)(万kW),太陽光発電実績(万kW)
                out["demand_5min_mw"] = actual * 10.0
                out["as_of_5min_jst"] = f"{cells[0]} {cells[1]} JST"
                if len(cells) > 3:
                    sol = _num(cells[3])
                    if sol is not None:
                        out["solar_mw"] = sol * 10.0  # 0 at night is REAL, keep it

    return out if ("demand_mw" in out or out.get("stale")) else None


def _fetch_tso(code):
    """Fetch + parse one TSO. Returns (metrics-or-None, note). Never raises."""
    if _rq is None:
        return None, "requests_unavailable"
    _, _, url_tpl = _TSOS[code]
    now_jst = datetime.now(_JST)
    # dated files appear at JST midnight; fall back one day just in case
    candidates = ([url_tpl] if "{ymd}" not in url_tpl else
                  [url_tpl.format(ymd=(now_jst - timedelta(days=d)).strftime("%Y%m%d"))
                   for d in (0, 1)])
    last_note = "fetch_failed"
    for url in candidates:
        try:
            r = _rq.get(url, headers=_HEADERS, timeout=10)
            if not r.ok:
                last_note = f"http_{r.status_code}"
                continue
            try:
                text = r.content.decode("cp932")  # Shift-JIS superset
            except UnicodeDecodeError:
                text = r.content.decode("utf-8", errors="replace")
            parsed = _parse_denkiyoho(text)
            if parsed is None:
                last_note = "unrecognized_format"
                continue
            if parsed.get("stale"):
                last_note = f"stale_mirror updated={parsed.get('updated_jst')}"
                continue
            parsed["source_url"] = url
            return parsed, "ok"
        except Exception as e:
            last_note = f"{type(e).__name__}"
    return None, last_note


def _live_snapshot():
    """{tso_code: parsed} for every TSO that answered fresh; {} if all down."""
    results, notes = {}, {}
    with ThreadPoolExecutor(max_workers=len(_TSOS)) as pool:
        futs = {pool.submit(_fetch_tso, code): code for code in _TSOS}
        for fut, code in futs.items():
            try:
                parsed, note = fut.result(timeout=11)
            except Exception as e:
                parsed, note = None, type(e).__name__
            notes[code] = note
            if parsed:
                results[code] = parsed
    return results, notes


_METRIC_KEYS = ("demand_mw", "peak_demand_forecast_mw", "peak_supply_capacity_mw",
                "supply_capacity_mw", "reserve_margin_pct", "usage_pct", "solar_mw")
_UNITS = {"reserve_margin_pct": "pct", "usage_pct": "pct"}


def _persist(results):
    """Write per-TSO rows (iso=TEPCO / JP_<area>) + the OCCTO aggregate.
    Mirrors iso_tw_taipower's grid_data insert exactly."""
    if not results:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        def _ins(iso, name, value, unit):
            nonlocal rows
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (iso, name, value, unit),
                )
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                pass
        for code, parsed in results.items():
            iso = _TSOS[code][0]
            for k in _METRIC_KEYS:
                if k in parsed and parsed[k] is not None:
                    _ins(iso, k, round(parsed[k], 1), _UNITS.get(k, "MW"))
        # OCCTO aggregate = sum of the areas that reported THIS run (coverage
        # count is persisted next to it so a partial sum is never mistaken for
        # national demand — 6/10 areas max by design, see module docstring).
        agg = sum(p["demand_mw"] for p in results.values() if p.get("demand_mw"))
        if agg > 0:
            _ins(ISO_CODE, "demand_mw", round(agg, 1), "MW")
            _ins(ISO_CODE, "tso_reporting_count", float(len(results)), "count")
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_denkiyoho_csv",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": ("Japan TSO denki-yoho CSVs (TEPCO/Chubu/Kyushu/Hokuriku/"
                   "Shikoku/Okinawa, token-free, Shift-JIS, 万kW×10→MW)"),
    }
    try:
        results, notes = _live_snapshot()
        failed = {c: n for c, n in notes.items() if n != "ok"}
        if failed:
            summary["errors"].append(
                "; ".join(f"{c}:{n}" for c, n in sorted(failed.items()))
                + " — wrote nothing for these areas (no modeled fallback)")
        if results:
            summary["metrics_extracted"] = sum(
                1 for p in results.values() for k in _METRIC_KEYS if p.get(k) is not None)
            summary["rows_inserted"] = _persist(results)
            summary["tso_reporting"] = sorted(results.keys())
            summary["snapshot"] = {
                c: {"iso": _TSOS[c][0],
                    "demand_mw": p.get("demand_mw"),
                    "as_of_jst": p.get("as_of_jst"),
                    "reserve_margin_pct": p.get("reserve_margin_pct")}
                for c, p in results.items()}
            summary["aggregate_demand_mw"] = round(
                sum(p["demand_mw"] for p in results.values() if p.get("demand_mw")), 1)
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


@iso_jp_denkiyoho_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200


@iso_jp_denkiyoho_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    results, notes = _live_snapshot()
    if not results:
        return jsonify({"iso": ISO_CODE, "error": "denkiyoho_live_unavailable",
                        "per_tso": notes}), 503
    from routes.tier_gate import jsonify_gated_snapshot
    return jsonify_gated_snapshot({
        "iso": ISO_CODE, "live": True,
        "tsos": {c: {k: v for k, v in p.items() if k != "stale"}
                 for c, p in results.items()},
        "aggregate_demand_mw": round(
            sum(p["demand_mw"] for p in results.values() if p.get("demand_mw")), 1),
        "coverage_note": (f"{len(results)}/{len(_TSOS)} ingested areas reporting "
                          f"(of 10 Japanese TSO areas; see /health for exclusions)"),
        "per_tso_status": notes,
        "source": "Japan TSO denki-yoho CSVs (live, JST)",
    }, 200)


@iso_jp_denkiyoho_bp.route("/health", methods=["GET"])
def http_health():
    results, notes = _live_snapshot()
    return jsonify({
        "iso": ISO_CODE, "live_feed_ok": bool(results),
        "tso_reporting": sorted(results.keys()),
        "per_tso_status": notes,
        "ingested_areas": {c: _TSOS[c][1] for c in _TSOS},
        "excluded_areas": _EXCLUDED_AREAS,
        "source": "denkiyoho_csv_tokenfree",
    }), 200
