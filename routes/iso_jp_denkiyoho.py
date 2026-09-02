"""
iso_jp_denkiyoho.py — Japan grid ingestion via TSO "denki-yoho" CSVs (LIVE).

Daily-content-feeds #2 (2026-07-01), APAC expansion. Fifth LIVE international
integration (after GB Elexon, AU AEMO, EU ENTSO-E, TW Taipower). Japan is the
largest APAC data-center market (Tokyo/Osaka hyperscale + AI buildout), and
until now the orchestrator's own health endpoint listed it under future_isos.

FULL FUEL MIX upgrade (2026-07-11): every one of the 10 area TSOs ALSO
publishes a standardized 30-min "エリア需給実績" CSV (eria_jukyu_YYYYMM_NN.csv,
monthly-rolling, updated intraday ~30-60 min behind real time, token-free)
with the COMPLETE fuel split: demand, nuclear, thermal split LNG/coal/oil/
other, hydro, geothermal, biomass, solar+curtailment, wind+curtailment,
pumped storage, battery, interconnector net, other. All 10 URLs were
curl-verified LIVE (same-day rows) 2026-07-11 JST — this upgrades Japan from
a demand-only partial feed to a scoreboard-RANKABLE full-mix grid, and adds
mix coverage for the 4 areas (Kansai/Tohoku/Hokkaido/Chugoku) whose legacy
juyo demand CSVs are stale/absent.

  ERIA-JUKYU VERIFIED GOTCHAS (2026-07-11):
  • Header cells differ per TSO: Kyushu uses full-width 火力（ＬＮＧ） +
    quoted cells + DATE=YYYYMMDD; Kansai 火力（LNG）; TEPCO/Tohoku ASCII
    parens. NFKC normalization + header-NAME (not position) mapping covers
    all of them — Tohoku/Chubu/etc. insert extra 出力制御量 columns.
  • TEPCO's file is UTF-8; every other TSO is Shift-JIS (cp932). The
    fetcher decodes both ways and keeps whichever contains エリア需要.
  • Future half-hours are pre-filled EMPTY rows (Hokuriku/Hokkaido pre-fill
    the whole month) — take the LAST row whose demand parses > 0.
  • Values are plain MW平均 (NO 万kW ×10 conversion — unlike the juyo files).
  • Tohoku's monthly eria file lags weeks; its daily realtime_jukyu CSV
    (same layout) is used instead. Hokkaido has no daily file and its
    monthly file lags ~1 week — the staleness guard excludes it from the
    live mix on most days (honest: mix_areas_reporting says so).

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
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2 as _pg
from flask import Blueprint, jsonify
from routes._swallowed_writes import note_swallowed_write

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

# Not ingested for 5-MIN DEMAND — kept here so the health endpoint can say
# WHY, honestly. (Since 2026-07-11 all four DO report the 30-min FULL FUEL
# MIX via _ERIA_AREAS below — only their fast juyo demand path is missing.)
_EXCLUDED_AREAS = {
    "kansai":   "legacy juyo CSVs are stale mirrors (2023/2025); live data moved to a different yamasou_* format",
    "tohoku":   "juyo_tohoku.csv frozen since 2016 (stale mirror)",
    "hokkaido": "no verifiable same-format public CSV found",
    "chugoku":  "no verifiable same-format public CSV found (page is JS-embedded)",
}

# ── FULL FUEL MIX (2026-07-11): eria_jukyu 30-min CSVs, all 10 areas ────────
# code → (grid_data iso, display name, url template: {ym}=YYYYMM, {ymd}=YYYYMMDD,
# both in JST). Every URL curl-verified live (same-day rows) 2026-07-11 JST.
# ISO codes reuse the juyo mapping where one exists; kansai joins the DCPI
# osaka market tag (KEPCO) the same way tepco joins tokyo (TEPCO).
_ERIA_AREAS = {
    "hokkaido": ("JP_HOKKAIDO", "Hokkaido (Sapporo)",
                 "https://www.hepco.co.jp/network/con_service/public_document/supply_demand_results/csv/eria_jukyu_{ym}_01.csv"),
    "tohoku":   ("JP_TOHOKU",   "Tohoku (Sendai)",
                 "https://setsuden.nw.tohoku-epco.co.jp/common/demand/realtime_jukyu/realtime_jukyu_{ymd}_02.csv"),
    "tepco":    ("TEPCO",       "TEPCO (Tokyo)",
                 "https://www.tepco.co.jp/forecast/html/images/eria_jukyu_{ym}_03.csv"),
    "chubu":    ("JP_CHUBU",    "Chubu (Nagoya)",
                 "https://powergrid.chuden.co.jp/denki_yoho_content_data/eria_jukyu_{ym}_04.csv"),
    "hokuriku": ("JP_HOKURIKU", "Hokuriku (Toyama)",
                 "https://www.rikuden.co.jp/nw/denki-yoho/csv/eria_jukyu_{ym}_05.csv"),
    "kansai":   ("KEPCO",       "Kansai (Osaka)",
                 "https://www.kansai-td.co.jp/interchange/denkiyoho/area-performance/eria_jukyu_{ym}_06.csv"),
    "chugoku":  ("JP_CHUGOKU",  "Chugoku (Hiroshima)",
                 "https://www.energia.co.jp/nw/jukyuu/sys/eria_jukyu_{ym}_07.csv"),
    "shikoku":  ("JP_SHIKOKU",  "Shikoku (Takamatsu)",
                 "https://www.yonden.co.jp/nw/supply_demand/csv/eria_jukyu_{ym}_08.csv"),
    "kyushu":   ("JP_KYUSHU",   "Kyushu (Fukuoka)",
                 "https://www.kyuden.co.jp/td_area_jukyu/csv/eria_jukyu_{ym}_09.csv"),
    "okinawa":  ("JP_OKINAWA",  "Okinawa (Naha)",
                 "https://www.okiden.co.jp/business-support/service/supply-and-demand/csv/eria_jukyu_{ym}_10.csv"),
}

# NFKC-normalized header cell → metric key. Header-NAME matching (never
# positional): TSOs insert extra 出力制御量 (curtailment) columns at will.
# NFKC folds Kyushu's 火力（ＬＮＧ） and Kansai's 火力（LNG） to 火力(LNG).
_ERIA_COLS = {
    "エリア需要": "demand_mw",
    "原子力": "fuel_nuclear_mw",
    "火力(LNG)": "fuel_gas_mw",
    "火力(石炭)": "fuel_coal_mw",
    "火力(石油)": "fuel_oil_mw",
    "火力(その他)": "fuel_thermal_other_mw",
    "水力": "fuel_hydro_mw",
    "地熱": "fuel_geothermal_mw",
    "バイオマス": "fuel_biomass_mw",
    "太陽光発電実績": "fuel_solar_mw",
    "風力発電実績": "fuel_wind_mw",
    "揚水": "pumped_storage_mw",       # storage — excluded from gen total
    "蓄電池": "battery_storage_mw",    # storage — excluded from gen total
    "連系線": "interconnector_mw",     # net imports — excluded from gen total
    "その他": "fuel_other_mw",
}
# Primary fuels — the generation_total denominator (storage + interconnector
# excluded, matching the US scoreboard convention).
_ERIA_FUEL_KEYS = ("fuel_nuclear_mw", "fuel_gas_mw", "fuel_coal_mw",
                   "fuel_oil_mw", "fuel_thermal_other_mw", "fuel_hydro_mw",
                   "fuel_geothermal_mw", "fuel_biomass_mw", "fuel_solar_mw",
                   "fuel_wind_mw", "fuel_other_mw")
_MIX_STALE_AFTER_H = 6  # 30-60min feed; older = frozen file (Hokkaido lags ~1wk)


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


def _parse_eria_jukyu(text):
    """Parse one eria_jukyu 30-min fuel-mix CSV (already decoded).

    Returns {metric_key: MW, ..., "as_of_jst": iso} for the LAST populated
    half-hour, or None if the shape is unrecognized. Pure function —
    unit-tested without network. Header cells are NFKC-normalized and
    matched by NAME (per-TSO column sets differ); values are plain MW平均
    (no 万kW conversion). Primary fuels are clamped ≥0; the storage /
    interconnector columns keep their sign (charging / exporting are real
    negatives, and they're excluded from generation_total anyway).
    """
    header_idx = None
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        cells = [unicodedata.normalize("NFKC", c).strip().strip('"').strip()
                 for c in ln.split(",")]
        if "DATE" in cells and "エリア需要" in cells:
            header_idx = {}
            for j, c in enumerate(cells):
                if c in ("DATE", "TIME"):
                    header_idx[c] = j
                elif c in _ERIA_COLS and _ERIA_COLS[c] not in header_idx:
                    header_idx[_ERIA_COLS[c]] = j
            data_start = i + 1
            break
    if not header_idx or "demand_mw" not in header_idx:
        return None  # not an eria_jukyu file — refuse to guess

    best = None
    for ln in lines[data_start:]:
        raw = [c.strip().strip('"').strip() for c in ln.split(",")]
        if len(raw) <= header_idx["demand_mw"]:
            continue
        demand = _num(raw[header_idx["demand_mw"]])
        if demand is None or demand <= 0:
            continue  # future half-hours are pre-filled empty
        best = raw
    if best is None:
        return None

    def _cell(key):
        j = header_idx.get(key)
        return _num(best[j]) if j is not None and j < len(best) else None

    out = {}
    for key in _ERIA_COLS.values():
        v = _cell(key)
        if v is None:
            continue
        if key.startswith("fuel_") and v < 0:
            v = 0.0  # clamp primary fuels; storage/interconnector keep sign
        out[key] = v
    out["demand_mw"] = _cell("demand_mw")
    total = sum(out.get(k, 0.0) for k in _ERIA_FUEL_KEYS)
    if total <= 0:
        return None
    out["generation_total_mw"] = round(total, 1)

    # DATE is 2026/7/11, 2026/07/11 or 20260711 (Kyushu); TIME is H:MM.
    date_s = best[header_idx["DATE"]] if header_idx.get("DATE") is not None else ""
    time_s = best[header_idx["TIME"]] if header_idx.get("TIME") is not None else ""
    m = (re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})$", date_s)
         or re.match(r"(\d{4})(\d{2})(\d{2})$", date_s))
    t = re.match(r"(\d{1,2}):(\d{2})$", time_s)
    if m and t:
        try:
            hh, mm = int(t.group(1)), int(t.group(2))
            base = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=_JST)
            # some TSOs label the interval END "24:00" — roll into next day
            out["as_of_jst"] = (base + timedelta(hours=hh, minutes=mm)).isoformat()
        except ValueError:
            return None
    else:
        return None  # can't stamp it → can't staleness-guard it → refuse
    return out


def _fetch_eria(code):
    """Fetch + parse one area's eria_jukyu fuel mix. Returns
    (metrics-or-None, note). Never raises. Freshness-guarded here."""
    if _rq is None:
        return None, "requests_unavailable"
    _, _, url_tpl = _ERIA_AREAS[code]
    now_jst = datetime.now(_JST)
    if "{ymd}" in url_tpl:  # Tohoku daily file
        candidates = [url_tpl.format(ymd=(now_jst - timedelta(days=d)).strftime("%Y%m%d"))
                      for d in (0, 1)]
    else:  # monthly-rolling file; fall back one month right after rollover
        months = [now_jst.strftime("%Y%m")]
        if now_jst.day <= 2:
            months.append((now_jst.replace(day=1) - timedelta(days=1)).strftime("%Y%m"))
        candidates = [url_tpl.format(ym=ym) for ym in months]
    last_note = "fetch_failed"
    for url in candidates:
        try:
            r = _rq.get(url, headers=_HEADERS, timeout=10)
            if not r.ok:
                last_note = f"http_{r.status_code}"
                continue
            # TEPCO is UTF-8, everyone else cp932 — keep whichever parses
            # to a real eria header rather than guessing by TSO.
            text = None
            for enc in ("cp932", "utf-8"):
                try:
                    cand = r.content.decode(enc)
                except UnicodeDecodeError:
                    continue
                if "エリア需要" in cand:
                    text = cand
                    break
            if text is None:
                last_note = "undecodable"
                continue
            parsed = _parse_eria_jukyu(text)
            if parsed is None:
                last_note = "unrecognized_format"
                continue
            age = datetime.now(_JST) - datetime.fromisoformat(parsed["as_of_jst"])
            if age > timedelta(hours=_MIX_STALE_AFTER_H):
                last_note = f"stale_mix as_of={parsed['as_of_jst']}"
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


# One shared fan-out for the 6 juyo demand CSVs + 10 eria mix CSVs (16
# fetches, one pool) so run_extraction/snapshot stay inside the
# orchestrator's per-slot budget. Cached 240s in-process — the TSO files
# only advance every 5-30 min and the scoreboard re-polls aggressively.
_SNAP_CACHE = {"data": None, "ts": 0.0}
_SNAP_TTL = 240


def _snapshot_all():
    """(juyo_results, juyo_notes, mix_results, mix_notes) — cached."""
    now = time.time()
    if _SNAP_CACHE["data"] is not None and (now - _SNAP_CACHE["ts"]) < _SNAP_TTL:
        return _SNAP_CACHE["data"]
    juyo, juyo_notes, mix, mix_notes = {}, {}, {}, {}
    with ThreadPoolExecutor(max_workers=len(_TSOS) + len(_ERIA_AREAS)) as pool:
        jfuts = {pool.submit(_fetch_tso, code): code for code in _TSOS}
        efuts = {pool.submit(_fetch_eria, code): code for code in _ERIA_AREAS}
        for fut, code in jfuts.items():
            try:
                parsed, note = fut.result(timeout=11)
            except Exception as e:
                parsed, note = None, type(e).__name__
            juyo_notes[code] = note
            if parsed:
                juyo[code] = parsed
        for fut, code in efuts.items():
            try:
                parsed, note = fut.result(timeout=11)
            except Exception as e:
                parsed, note = None, type(e).__name__
            mix_notes[code] = note
            if parsed:
                mix[code] = parsed
    result = (juyo, juyo_notes, mix, mix_notes)
    if juyo or mix:  # never cache a total blackout
        _SNAP_CACHE["data"] = result
        _SNAP_CACHE["ts"] = now
    return result


def _aggregate_mix(mix):
    """OCCTO-level fuel-mix rollup across the areas reporting fresh mix.
    renewable_pct = wind+solar+hydro (the US/GB/EU/TW scoreboard definition;
    geothermal + biomass are exposed separately, not counted)."""
    if not mix:
        return None
    agg = {k: round(sum(a.get(k, 0.0) for a in mix.values()), 1)
           for k in _ERIA_FUEL_KEYS}
    total = round(sum(agg.values()), 1)
    if total <= 0:
        return None
    renew = agg["fuel_wind_mw"] + agg["fuel_solar_mw"] + agg["fuel_hydro_mw"]
    return {
        "generation_total_mw": total,
        **agg,
        "renewable_pct": round(100.0 * renew / total, 1),
        "gas_pct": round(100.0 * agg["fuel_gas_mw"] / total, 1),
        "mix_demand_mw": round(sum(a.get("demand_mw", 0.0) or 0.0
                                   for a in mix.values()), 1),
        "mix_areas_reporting": float(len(mix)),
    }


_METRIC_KEYS = ("demand_mw", "peak_demand_forecast_mw", "peak_supply_capacity_mw",
                "supply_capacity_mw", "reserve_margin_pct", "usage_pct", "solar_mw")
_UNITS = {"reserve_margin_pct": "pct", "usage_pct": "pct"}


def _persist(results, mix=None, mix_agg=None):
    """Write per-TSO rows (iso=TEPCO / JP_<area>) + the OCCTO aggregate.
    Mirrors iso_tw_taipower's grid_data insert exactly. Since 2026-07-11
    also writes the eria_jukyu fuel mix: per-area fuel_*_mw rows + the
    OCCTO aggregate mix (generation_total/renewable_pct/gas_pct)."""
    if not results and not mix:
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
                note_swallowed_write("grid_data", where="iso_jp_denkiyoho._ins")
                pass
        for code, parsed in (results or {}).items():
            iso = _TSOS[code][0]
            for k in _METRIC_KEYS:
                if k in parsed and parsed[k] is not None:
                    _ins(iso, k, round(parsed[k], 1), _UNITS.get(k, "MW"))
        # OCCTO aggregate = sum of the areas that reported THIS run (coverage
        # count is persisted next to it so a partial sum is never mistaken for
        # national demand — 6/10 areas max by design, see module docstring).
        agg = sum(p["demand_mw"] for p in (results or {}).values() if p.get("demand_mw"))
        if agg > 0:
            _ins(ISO_CODE, "demand_mw", round(agg, 1), "MW")
            _ins(ISO_CODE, "tso_reporting_count", float(len(results)), "count")
        # eria_jukyu fuel mix: per-area rows (fuel split + 30-min demand
        # under the area's own iso) + the OCCTO aggregate mix.
        for code, parsed in (mix or {}).items():
            iso = _ERIA_AREAS[code][0]
            for k, v in parsed.items():
                if k in ("as_of_jst", "source_url") or v is None:
                    continue
                _ins(iso, f"mix_{k}" if k == "demand_mw" else k,
                     round(v, 1), "MW")
        if mix_agg:
            for k, v in mix_agg.items():
                unit = ("pct" if k.endswith("_pct")
                        else "count" if k == "mix_areas_reporting" else "MW")
                _ins(ISO_CODE, k, v, unit)
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_denkiyoho_csv",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": ("Japan TSO denki-yoho CSVs (6-area 5-min demand, 万kW×10→MW)"
                   " + eria_jukyu 30-min FULL fuel mix (all 10 areas, MW)"),
    }
    try:
        results, notes, mix, mix_notes = _snapshot_all()
        failed = {c: n for c, n in notes.items() if n != "ok"}
        if failed:
            summary["errors"].append(
                "; ".join(f"{c}:{n}" for c, n in sorted(failed.items()))
                + " — wrote nothing for these areas (no modeled fallback)")
        mix_failed = {c: n for c, n in mix_notes.items() if n != "ok"}
        if mix_failed:
            summary["errors"].append(
                "mix: " + "; ".join(f"{c}:{n}" for c, n in sorted(mix_failed.items()))
                + " — no fuel mix written for these areas")
        mix_agg = _aggregate_mix(mix)
        if results or mix:
            summary["metrics_extracted"] = (
                sum(1 for p in results.values() for k in _METRIC_KEYS
                    if p.get(k) is not None)
                + sum(1 for p in mix.values() for k, v in p.items()
                      if k not in ("as_of_jst", "source_url") and v is not None))
            summary["rows_inserted"] = _persist(results, mix, mix_agg)
            summary["tso_reporting"] = sorted(results.keys())
            summary["mix_areas_reporting"] = sorted(mix.keys())
            summary["snapshot"] = {
                c: {"iso": _TSOS[c][0],
                    "demand_mw": p.get("demand_mw"),
                    "as_of_jst": p.get("as_of_jst"),
                    "reserve_margin_pct": p.get("reserve_margin_pct")}
                for c, p in results.items()}
            summary["aggregate_demand_mw"] = round(
                sum(p["demand_mw"] for p in results.values() if p.get("demand_mw")), 1)
            if mix_agg:
                summary["mix_aggregate"] = mix_agg
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_jp_denkiyoho_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_jp_denkiyoho_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    results, notes, mix, mix_notes = _snapshot_all()
    if not results and not mix:
        return jsonify({"iso": ISO_CODE, "error": "denkiyoho_live_unavailable",
                        "per_tso": notes, "per_area_mix": mix_notes}), 503
    from routes.tier_gate import jsonify_gated_snapshot
    mix_agg = _aggregate_mix(mix)
    payload = {
        "iso": ISO_CODE, "live": True,
        "tsos": {c: {k: v for k, v in p.items() if k != "stale"}
                 for c, p in results.items()},
        "aggregate_demand_mw": round(
            sum(p["demand_mw"] for p in results.values() if p.get("demand_mw")), 1),
        "coverage_note": (f"{len(results)}/{len(_TSOS)} ingested areas reporting "
                          f"(of 10 Japanese TSO areas; see /health for exclusions)"),
        "per_tso_status": notes,
        "source": "Japan TSO denki-yoho CSVs (live, JST)",
    }
    if mix_agg:
        # scoreboard-shape metrics block (same keys as Taipower/Elexon) so
        # Japan ranks by renewable share apples-to-apples.
        payload["metrics"] = dict(mix_agg)
        payload["mix"] = {
            "aggregate": mix_agg,
            "areas": {c: dict(p) for c, p in mix.items()},
            "per_area_status": mix_notes,
            "coverage_note": (
                f"fuel mix from {len(mix)}/{len(_ERIA_AREAS)} areas' eria_jukyu "
                f"30-min CSVs (Hokkaido's file lags ~1wk and drops out of the "
                f"6h freshness window on most days). renewable_pct = "
                f"wind+solar+hydro (matches US/GB/EU/TW; geothermal + biomass "
                f"reported separately). Storage + interconnector excluded "
                f"from generation_total."),
        }
    return jsonify_gated_snapshot(payload, 200)
# AUTO-REPAIR: duplicate route '/health' also in main.py:7917 — review and remove one


@iso_jp_denkiyoho_bp.route("/health", methods=["GET"])
def http_health():
    results, notes, mix, mix_notes = _snapshot_all()
    return jsonify({
        "iso": ISO_CODE, "live_feed_ok": bool(results or mix),
        "tso_reporting": sorted(results.keys()),
        "per_tso_status": notes,
        "mix_areas_reporting": sorted(mix.keys()),
        "per_area_mix_status": mix_notes,
        "ingested_areas": {c: _TSOS[c][1] for c in _TSOS},
        "excluded_areas_5min_demand": _EXCLUDED_AREAS,
        "mix_areas": {c: _ERIA_AREAS[c][1] for c in _ERIA_AREAS},
        "source": "denkiyoho_csv_tokenfree + eria_jukyu_fuel_mix",
    }), 200
