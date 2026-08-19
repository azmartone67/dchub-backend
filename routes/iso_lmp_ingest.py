"""
iso_lmp_ingest.py v1 (2026-07-01)

Real-time per-ISO LMP (locational marginal price) ingestion — 6 ISOs.
Architecture mirrors routes/iso_queue_ingest.py: per-ISO ingestor functions
returning (ok, rows, debug), an INGESTORS registry, POST ingest endpoints
(all + per-ISO), an idempotent upsert into a snapshot table, and a public
GET snapshot endpoint that reads DISTINCT ISOs (no hardcoded ISO list).

Every parser below was verified against the LIVE source on 2026-07-01
before shipping (no fabricated numbers). Per-ISO notes:

  MISO   — public-api.misoenergy.org GetRealTimeFiveMinExPost/Rolling (JSON).
           Full nodal file (~38 MB); we regex-extract only the 8 named
           trading hubs (*.HUB) at the latest interval, so we never hold
           600k parsed rows in memory. Carries LMP + MLC (loss) + MCC
           (congestion). Timestamps are EST (UTC-5) YEAR-ROUND — MISO
           market time never observes DST — converted to UTC on ingest.
  NYISO  — mis.nyiso.com realtime_zone_lbmp.csv (15 zones). Timestamps are
           Eastern PREVAILING time (America/New_York, DST-aware) — converted
           to UTC on ingest. NYISO sign convention: LBMP = energy + losses
           − congestion (a POSITIVE congestion component LOWERS the LBMP);
           we store the component exactly as published.
  SPP    — portal.spp.org file-browser API (fsName=rtbm-lmp-by-location,
           /YYYY/MM/By_Interval/DD/RTBM-LMP-SL-*.csv, newest file). The CSV
           carries GMTIntervalEnd (already UTC) plus LMP/MLC/MCC/MEC. We
           keep only settlement locations ending in _HUB (~16 rows incl.
           SPPNORTH_HUB / SPPSOUTH_HUB).
  CAISO  — OASIS SingleZip PRC_INTVL_LMP (RTM 5-min). Returns a ZIP holding
           a long-format CSV (one row per node × component); timestamps are
           *_GMT (already UTC). We request 3 trading hubs + 4 DLAP zones.
           OASIS rate-limits aggressively (HTTP 429) — failures degrade
           gracefully, never kill the batch.
  PJM    — api.pjm.com Data Miner 2 feed rt_unverified_fivemin_lmps using
           PJM's PUBLIC anonymous subscription key (the same key PJM ships
           in dataminer2's settings.json for keyless browsing — not a
           private credential; override via PJM_DM2_KEY env). NOTE: the
           verified feed (rt_fivemin_hrl_lmps) posts DAILY with ~1-day lag,
           so real-time means the UNVERIFIED feed — labeled as such in
           source_name. datetime_beginning_utc is already UTC (interval
           ending = beginning + 5 min). Server-side filtered to the 34
           ZONE/HUB aggregate pnodes.
  ERCOT  — HTML scrape of the np6-788-cd display (content/cdr/html/hb_lz.html,
           "Real-Time LMPs for Load Zones and Trading Hubs"). LMP ONLY —
           this display carries NO congestion/energy/loss split, so those
           columns are NULL for ERCOT (labeled, never fabricated). The
           "Last Updated" stamp is Central PREVAILING time (America/Chicago)
           — converted to UTC on ingest; it is the SCED run time, which we
           store as interval_ending (SCED runs ~every 5 min).
  ISO-NE — intentionally ABSENT: its real-time LMP API is registration-gated
           (ISO Express web services need an account); no public no-auth feed.

Wiring (main.py):
    from routes.iso_lmp_ingest import iso_lmp_ingest_bp
    app.register_blueprint(iso_lmp_ingest_bp)

Endpoints:
  POST /api/v1/iso-lmp/ingest           — run all ISO ingestors (admin-gated*)
  POST /api/v1/iso-lmp/ingest/<iso>     — run a single ISO (admin-gated*)
  GET  /api/v1/iso-lmp/snapshot         — latest interval per ISO, all ISOs (public)
  GET  /api/v1/iso-lmp/snapshot?iso=PJM — single-ISO detail (public)
  GET  /api/v1/iso-lmp/parser-versions  — which parsers exist + their basis

  *admin gate: if LMP_INGEST_TOKEN env is set, POST requires it via
   X-Admin-Token header (or ?token=). If unset, ingest stays open so the
   keyless cron curl works (same posture as /api/v1/iso-queue/ingest).

Table: iso_lmp_snapshots (migrations/2026-07-01_iso_lmp_snapshots.sql; also
created lazily by _ensure_table() on first ingest — NEVER at import/boot,
to avoid the boot-DDL statement-timeout deploy trap). Bounded: hubs/zones
only (max 34 rows per ISO per interval), history pruned after 7 days.
"""
import os
import re
import io
import csv
import json
import zipfile
import datetime
import urllib.parse
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request
from internal_auth import is_valid_internal_key
import psycopg

iso_lmp_ingest_bp = Blueprint("iso_lmp_ingest", __name__,
                              url_prefix="/api/v1/iso-lmp")
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")

UA = "DCHub-IsoLmpIngest/1.0 (+https://dchub.cloud)"
UTC = datetime.timezone.utc
# MISO market time is EST (UTC-5) year-round — it does NOT observe DST.
TZ_MISO_EST = datetime.timezone(datetime.timedelta(hours=-5))

PARSER_STATUS = {
    "MISO":  "real (public-api.misoenergy.org 5-min ExPost Rolling JSON; 8 trading hubs; LMP+MLC+MCC; EST→UTC)",
    "NYISO": "real (mis.nyiso.com realtime_zone_lbmp.csv; 15 zones; LBMP+losses+congestion; Eastern prevailing→UTC)",
    "SPP":   "real (portal.spp.org file-browser RTBM-LMP-SL CSV, newest interval file; *_HUB rows; LMP+MLC+MCC+MEC; GMTIntervalEnd already UTC)",
    "CAISO": "real (OASIS SingleZip PRC_INTVL_LMP RTM; 3 TH hubs + 4 DLAPs; LMP+MCE+MCC+MCL; *_GMT already UTC)",
    "PJM":   "real (Data Miner 2 rt_unverified_fivemin_lmps, PJM public anonymous key; 34 ZONE/HUB pnodes; LMP+congestion+loss; UTC native; UNVERIFIED real-time feed)",
    "ERCOT": "real (www.ercot.com cdr hb_lz.html scrape, np6-788-cd display; 16 hubs+load zones; LMP ONLY — no congestion split published; Central prevailing→UTC)",
    # ISO-NE intentionally absent: real-time LMP API is registration-gated.
}


def _tz(name):
    """Prevailing-time zone lookup. Returns tzinfo or None (caller must
    fail-graceful — better to skip an ISO than stamp a wrong UTC time)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# HTTP helper (same shape as iso_queue_ingest._fetch)
# ──────────────────────────────────────────────────────────────────────
def _fetch(url, timeout=30, return_bytes=False, accept="*/*", headers=None):
    h = {"User-Agent": UA, "Accept": accept,
         "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return (body if return_bytes else body.decode("utf-8", errors="replace")), resp.status


def _row(location, lmp, congestion=None, energy=None, loss=None,
         interval_ending=None, location_type=None, source_url=None):
    """One snapshot row. interval_ending MUST be tz-aware UTC."""
    return {
        "location": location,
        "location_type": location_type,
        "lmp_usd_mwh": lmp,
        "congestion_usd_mwh": congestion,
        "energy_usd_mwh": energy,
        "loss_usd_mwh": loss,
        "interval_ending": interval_ending,
        "source_url": source_url,
    }


def _f(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════
# MISO — 5-min real-time ex-post LMPs, Rolling JSON (~38 MB, all CPNodes)
# ══════════════════════════════════════════════════════════════════════
def ingest_miso():
    debug = []
    url = ("https://public-api.misoenergy.org/api/MarketPricing/"
           "GetRealTimeFiveMinExPost/Rolling")
    try:
        body, st = _fetch(url, timeout=120, accept="application/json")
        debug.append(f"miso http_{st} mb={len(body)//1048576}")
        if st != 200 or len(body) < 10000:
            return False, None, "; ".join(debug + ["fetch_failed_or_small"])
        # headers verified live 2026-07-01: ["INTERVAL","CPNODE","LMP","MLC","MCC"]
        if '"INTERVAL","CPNODE","LMP","MLC","MCC"' not in body[:300]:
            return False, None, "; ".join(debug + ["unexpected_headers"])
        # Targeted regex extraction of *.HUB rows only — the full file is
        # ~600k rows and json.loads of 38 MB would spike Railway memory.
        hub_rows = re.findall(
            r'\["([^"]+)","([A-Z0-9_.]+\.HUB)","([-0-9.]*)","([-0-9.]*)","([-0-9.]*)"\]',
            body)
        debug.append(f"hub_rows={len(hub_rows)}")
        if not hub_rows:
            return False, None, "; ".join(debug + ["no_hub_rows"])
        latest = max(r[0] for r in hub_rows)
        rows = []
        for interval, node, lmp, mlc, mcc in hub_rows:
            if interval != latest:
                continue
            lmp_f = _f(lmp)
            if lmp_f is None:
                continue
            # MISO INTERVAL is the 5-min interval BEGINNING in EST (UTC-5,
            # year-round — MISO market time never observes DST). Interval
            # ending = beginning + 5 min, converted to UTC.
            beg = datetime.datetime.fromisoformat(interval).replace(tzinfo=TZ_MISO_EST)
            end_utc = (beg + datetime.timedelta(minutes=5)).astimezone(UTC)
            rows.append(_row(node, lmp_f, congestion=_f(mcc), loss=_f(mlc),
                             interval_ending=end_utc, location_type="hub",
                             source_url=url))
        debug.append(f"latest_est={latest} kept={len(rows)}")
        if not rows:
            return False, None, "; ".join(debug + ["no_rows_at_latest_interval"])
        return True, rows, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


# ══════════════════════════════════════════════════════════════════════
# NYISO — real-time zonal LBMP CSV (15 zones, ~5-min RTD)
# ══════════════════════════════════════════════════════════════════════
def ingest_nyiso():
    debug = []
    url = "https://mis.nyiso.com/public/realtime/realtime_zone_lbmp.csv"
    tz_ny = _tz("America/New_York")
    if tz_ny is None:
        return False, None, "zoneinfo_unavailable (refusing to guess Eastern offset)"
    try:
        body, st = _fetch(url, timeout=30, accept="text/csv")
        debug.append(f"nyiso http_{st} b={len(body)}")
        if st != 200 or len(body) < 100:
            return False, None, "; ".join(debug + ["fetch_failed_or_small"])
        rdr = list(csv.reader(io.StringIO(body)))
        hdr = [h.strip().lower() for h in rdr[0]]

        def col(*kw):
            for i, h in enumerate(hdr):
                if all(k in h for k in kw):
                    return i
            return None

        c_ts, c_name = col("time stamp"), col("name")
        c_lbmp = col("lbmp")
        c_loss = col("losses")
        c_cong = col("congestion")
        if None in (c_ts, c_name, c_lbmp):
            return False, None, "; ".join(debug + [f"cols_missing hdr={hdr}"])
        rows = []
        for r in rdr[1:]:
            if len(r) <= max(c_ts, c_name, c_lbmp):
                continue
            lmp = _f(r[c_lbmp])
            if lmp is None:
                continue
            # Time Stamp is Eastern PREVAILING time (EDT/EST per calendar) —
            # localize with America/New_York, then convert to UTC. It is the
            # RTD interval timestamp; stored as interval_ending.
            try:
                naive = datetime.datetime.strptime(r[c_ts].strip(), "%m/%d/%Y %H:%M:%S")
            except ValueError:
                continue
            end_utc = naive.replace(tzinfo=tz_ny).astimezone(UTC)
            # NYISO convention: LBMP = energy + losses − congestion. Components
            # stored EXACTLY as published (do not flip signs; do not derive energy).
            rows.append(_row(r[c_name].strip(), lmp,
                             congestion=_f(r[c_cong]) if c_cong is not None else None,
                             loss=_f(r[c_loss]) if c_loss is not None else None,
                             interval_ending=end_utc, location_type="zone",
                             source_url=url))
        debug.append(f"zones={len(rows)}")
        if not rows:
            return False, None, "; ".join(debug + ["no_rows"])
        return True, rows, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


# ══════════════════════════════════════════════════════════════════════
# SPP — RTBM LMP by settlement location, via the portal file-browser API
# ══════════════════════════════════════════════════════════════════════
def ingest_spp():
    debug = []
    base = "https://portal.spp.org/file-browser-api/"
    fs = "rtbm-lmp-by-location"
    try:
        # Discover the newest RTBM-LMP-SL interval CSV programmatically:
        # /YYYY/MM/By_Interval/DD/RTBM-LMP-SL-YYYYMMDDHHMM.csv (verified live
        # 2026-07-01; listing needs no token despite the UI sending one).
        # Try today then yesterday (UTC vs Central day-roll around midnight).
        now = datetime.datetime.now(UTC)
        files = []
        for day in (now, now - datetime.timedelta(days=1)):
            p = f"/{day:%Y}/{day:%m}/By_Interval/{day:%d}"
            lst_url = f"{base}?fsName={fs}&path={urllib.parse.quote(p)}&type=folder"
            body, st = _fetch(lst_url, timeout=30, accept="application/json")
            items = json.loads(body) if st == 200 else []
            files = sorted(i["path"] for i in items or []
                           if i.get("type") == "file"
                           and re.search(r"RTBM-LMP-SL-\d{12}\.csv$", i.get("name", "")))
            debug.append(f"list{p} n={len(files)}")
            if files:                       # names are timestamped → sorted = newest last
                break
        if not files:
            return False, None, "; ".join(debug + ["no_interval_files_found"])
        # GOTCHA (verified live 2026-07-01): the listing races ahead of file
        # availability — the newest listed CSV can 404 for a minute or two.
        # Walk back up to 3 files until one downloads.
        body = dl_url = None
        for latest_path in files[:-4:-1]:
            dl_url = f"{base}download/{fs}?path={urllib.parse.quote(latest_path)}"
            try:
                # NB: must be Accept: */* — the SPP download endpoint 404s on
                # "Accept: text/csv" (content-negotiation quirk, verified live).
                body, st = _fetch(dl_url, timeout=60)
            except urllib.error.HTTPError as e:
                debug.append(f"dl_{latest_path.rsplit('/', 1)[-1]}_http_{e.code}")
                body = None
                continue
            debug.append(f"dl http_{st} kb={len(body)//1024} file={latest_path.rsplit('/', 1)[-1]}")
            if st == 200 and len(body) >= 500:
                break
            body = None
        if body is None:
            return False, None, "; ".join(debug + ["download_failed"])
        rdr = list(csv.reader(io.StringIO(body)))
        hdr = [h.strip() for h in rdr[0]]
        idx = {h: i for i, h in enumerate(hdr)}
        c_gmt = idx.get("GMTIntervalEnd")
        c_loc = idx.get("Settlement Location")
        c_lmp, c_mlc, c_mcc, c_mec = (idx.get("LMP"), idx.get("MLC"),
                                      idx.get("MCC"), idx.get("MEC"))
        if None in (c_gmt, c_loc, c_lmp):
            return False, None, "; ".join(debug + [f"cols_missing hdr={hdr}"])
        rows = []
        for r in rdr[1:]:
            if len(r) <= max(c_gmt, c_loc, c_lmp):
                continue
            loc = r[c_loc].strip()
            if not loc.endswith("_HUB"):     # bounded: hubs only (~16 rows)
                continue
            lmp = _f(r[c_lmp])
            if lmp is None:
                continue
            # GMTIntervalEnd is ALREADY UTC (SPP publishes both local Central
            # and GMT interval stamps; we use the GMT one directly).
            try:
                end_utc = datetime.datetime.strptime(
                    r[c_gmt].strip(), "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                continue
            rows.append(_row(loc, lmp,
                             congestion=_f(r[c_mcc]) if c_mcc is not None else None,
                             energy=_f(r[c_mec]) if c_mec is not None else None,
                             loss=_f(r[c_mlc]) if c_mlc is not None else None,
                             interval_ending=end_utc, location_type="hub",
                             source_url=dl_url))
        debug.append(f"hub_rows={len(rows)}")
        if not rows:
            return False, None, "; ".join(debug + ["no_hub_rows"])
        return True, rows, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


# ══════════════════════════════════════════════════════════════════════
# CAISO — OASIS SingleZip PRC_INTVL_LMP (RTM 5-min), ZIP → long-format CSV
# ══════════════════════════════════════════════════════════════════════
CAISO_NODES = [
    "TH_NP15_GEN-APND", "TH_SP15_GEN-APND", "TH_ZP26_GEN-APND",   # trading hubs
    "DLAP_PGAE-APND", "DLAP_SCE-APND", "DLAP_SDGE-APND", "DLAP_VEA-APND",  # zones
]


def ingest_caiso():
    debug = []
    try:
        now = datetime.datetime.now(UTC)
        start = now - datetime.timedelta(minutes=40)
        # OASIS wants YYYYMMDDTHH:MM-0000 (already UTC — no local-time math).
        url = ("https://oasis.caiso.com/oasisapi/SingleZip"
               "?queryname=PRC_INTVL_LMP&version=1&market_run_id=RTM"
               f"&startdatetime={start:%Y%m%dT%H:%M}-0000"
               f"&enddatetime={now:%Y%m%dT%H:%M}-0000"
               f"&node={','.join(CAISO_NODES)}"
               "&resultformat=6")
        zbytes, st = _fetch(url, timeout=90, return_bytes=True)
        debug.append(f"caiso http_{st} kb={len(zbytes)//1024}")
        if st != 200 or len(zbytes) < 200:
            return False, None, "; ".join(debug + ["fetch_failed_or_small"])
        zf = zipfile.ZipFile(io.BytesIO(zbytes))
        names = zf.namelist()
        csv_names = [n for n in names if n.lower().endswith(".csv")]
        if not csv_names:
            # OASIS returns an .xml INVALID_REQUEST doc inside the zip on error
            return False, None, "; ".join(debug + [f"no_csv_in_zip names={names[:3]}"])
        body = zf.read(csv_names[0]).decode("utf-8", errors="replace")
        rdr = list(csv.reader(io.StringIO(body)))
        hdr = [h.strip() for h in rdr[0]]
        idx = {h: i for i, h in enumerate(hdr)}
        c_end, c_node = idx.get("INTERVALENDTIME_GMT"), idx.get("NODE")
        c_type, c_mw = idx.get("LMP_TYPE"), idx.get("MW")
        if None in (c_end, c_node, c_type, c_mw):
            return False, None, "; ".join(debug + [f"cols_missing hdr={hdr[:8]}"])
        # Long format: one row per node × component (LMP/MCC/MCE/MCL).
        # Pivot on (interval_end, node), then keep the latest interval.
        pivot = {}
        for r in rdr[1:]:
            if len(r) <= max(c_end, c_node, c_type, c_mw):
                continue
            key = (r[c_end].strip(), r[c_node].strip())
            pivot.setdefault(key, {})[r[c_type].strip()] = _f(r[c_mw])
        if not pivot:
            return False, None, "; ".join(debug + ["no_data_rows"])
        latest = max(k[0] for k in pivot)
        rows = []
        for (end_s, node), comp in pivot.items():
            if end_s != latest or comp.get("LMP") is None:
                continue
            # INTERVALENDTIME_GMT is ALREADY UTC (e.g. 2026-07-01T23:50:00-00:00)
            end_utc = datetime.datetime.fromisoformat(end_s).astimezone(UTC)
            rows.append(_row(node, comp["LMP"], congestion=comp.get("MCC"),
                             energy=comp.get("MCE"), loss=comp.get("MCL"),
                             interval_ending=end_utc,
                             location_type="hub" if node.startswith("TH_") else "zone",
                             source_url=url))
        debug.append(f"latest_gmt={latest} kept={len(rows)}")
        if not rows:
            return False, None, "; ".join(debug + ["no_rows_at_latest_interval"])
        return True, rows, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code} (OASIS 429=rate-limit)"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


# ══════════════════════════════════════════════════════════════════════
# PJM — Data Miner 2 rt_unverified_fivemin_lmps (real-time; posts ~5-min).
# The VERIFIED feed (rt_fivemin_hrl_lmps) posts daily with ~1-day lag —
# useless for real-time — so we use the unverified feed and SAY SO.
# Key: PJM's published anonymous subscription key (dataminer2 settings.json,
# baked into every anonymous browser session — not a private credential).
# ══════════════════════════════════════════════════════════════════════
# The 34 ZONE/HUB aggregate pnodes (verified live 2026-07-01). pnode_ids are
# PJM's stable aggregate identifiers; server-side filtering keeps the pull
# to ~34 rows instead of ~14,000 buses per interval.
PJM_AGG_PNODE_IDS = (
    "1;51217;51287;51288;51291;51292;51293;51295;51296;51297;51298;51299;"
    "51300;51301;4669664;7633629;8394954;8445784;33092311;33092313;33092315;"
    "33092371;34497125;34497127;34497151;34508503;34964545;35010337;37737283;"
    "116013751;116013753;124076095;970242670;1709725933"
)


def ingest_pjm():
    debug = []
    key = os.environ.get("PJM_DM2_KEY", "6a75d9f6d933401dbb4f36f8e70b95b3")
    try:
        now = datetime.datetime.now(UTC)
        start = now - datetime.timedelta(minutes=30)
        # datetime_beginning_utc filter is ALREADY UTC — no local-time math.
        params = urllib.parse.urlencode({
            "rowCount": "500", "startRow": "1",
            "datetime_beginning_utc": f"{start:%m/%d/%Y %H:%M}to{now:%m/%d/%Y %H:%M}",
            "pnode_id": PJM_AGG_PNODE_IDS,
            "fields": ("datetime_beginning_utc,pnode_id,pnode_name,type,"
                       "total_lmp_rt,congestion_price_rt,marginal_loss_price_rt"),
        })
        url = f"https://api.pjm.com/api/v1/rt_unverified_fivemin_lmps?{params}"
        body, st = _fetch(url, timeout=60, accept="application/json",
                          headers={"Ocp-Apim-Subscription-Key": key})
        debug.append(f"pjm http_{st} kb={len(body)//1024}")
        if st != 200:
            return False, None, "; ".join(debug + ["fetch_failed"])
        items = (json.loads(body) or {}).get("items") or []
        debug.append(f"items={len(items)}")
        if not items:
            return False, None, "; ".join(debug + ["no_items_in_window"])
        latest = max(i["datetime_beginning_utc"] for i in items)
        rows = []
        for i in items:
            if i.get("datetime_beginning_utc") != latest:
                continue
            lmp = _f(i.get("total_lmp_rt"))
            if lmp is None:
                continue
            beg = datetime.datetime.fromisoformat(i["datetime_beginning_utc"]).replace(tzinfo=UTC)
            rows.append(_row(
                str(i.get("pnode_name") or i.get("pnode_id")), lmp,
                congestion=_f(i.get("congestion_price_rt")),
                loss=_f(i.get("marginal_loss_price_rt")),
                interval_ending=beg + datetime.timedelta(minutes=5),
                location_type=str(i.get("type") or "").lower() or None,
                source_url="https://api.pjm.com/api/v1/rt_unverified_fivemin_lmps"))
        debug.append(f"latest_utc={latest} kept={len(rows)}")
        if not rows:
            return False, None, "; ".join(debug + ["no_rows_at_latest_interval"])
        return True, rows, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


# ══════════════════════════════════════════════════════════════════════
# ERCOT — np6-788-cd display scrape (hubs + load zones). LMP ONLY: this
# public display publishes no congestion/energy/loss split — those columns
# stay NULL for ERCOT rather than being fabricated.
# ══════════════════════════════════════════════════════════════════════
def ingest_ercot():
    debug = []
    url = "https://www.ercot.com/content/cdr/html/hb_lz.html"
    tz_ct = _tz("America/Chicago")
    if tz_ct is None:
        return False, None, "zoneinfo_unavailable (refusing to guess Central offset)"
    try:
        body, st = _fetch(url, timeout=30, accept="text/html")
        debug.append(f"ercot http_{st} kb={len(body)//1024}")
        if st != 200 or len(body) < 1000:
            return False, None, "; ".join(debug + ["fetch_failed_or_small"])
        # "Last Updated:&nbsp; Jul 01, 2026 19:25:26" — Central PREVAILING time
        # (America/Chicago, DST-aware). This is the SCED run stamp; SCED runs
        # ~every 5 min, so we store it (converted to UTC) as interval_ending.
        m = re.search(r"Last Updated:(?:&nbsp;|\s)*([A-Z][a-z]{2} \d{2}, \d{4} \d{2}:\d{2}:\d{2})", body)
        if not m:
            return False, None, "; ".join(debug + ["no_last_updated_stamp"])
        naive = datetime.datetime.strptime(m.group(1), "%b %d, %Y %H:%M:%S")
        end_utc = naive.replace(tzinfo=tz_ct).astimezone(UTC)
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if len(cells) < 2 or not re.match(r"^(HB|LZ)_[A-Z0-9_]+$", cells[0]):
                continue
            lmp = _f(cells[1])
            if lmp is None:
                continue
            rows.append(_row(cells[0], lmp, interval_ending=end_utc,
                             location_type="hub" if cells[0].startswith("HB_") else "zone",
                             source_url=url))
        debug.append(f"sced_ct={m.group(1)} rows={len(rows)}")
        if not rows:
            return False, None, "; ".join(debug + ["no_settlement_point_rows"])
        return True, rows, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


INGESTORS = {
    "MISO":  ingest_miso,
    "NYISO": ingest_nyiso,
    "SPP":   ingest_spp,
    "CAISO": ingest_caiso,
    "PJM":   ingest_pjm,
    "ERCOT": ingest_ercot,
    # ISO-NE intentionally absent (registration-gated real-time API).
}

SOURCE_NAMES = {
    "MISO":  "MISO Real-Time 5-min Ex-Post LMP (Rolling)",
    "NYISO": "NYISO Real-Time Zonal LBMP (RTD)",
    "SPP":   "SPP RTBM LMP by Settlement Location (5-min)",
    "CAISO": "CAISO OASIS PRC_INTVL_LMP (RTM 5-min)",
    "PJM":   "PJM Real-Time 5-min LMP (UNVERIFIED feed rt_unverified_fivemin_lmps)",
    "ERCOT": "ERCOT Real-Time LMP, hubs + load zones (np6-788-cd display; LMP only, no congestion component)",
}


# ══════════════════════════════════════════════════════════════════════
# Table + upsert. DDL runs LAZILY on first ingest — never at import/boot
# (boot-time DDL has caused statement-timeout deploy storms before).
# ══════════════════════════════════════════════════════════════════════
_DDL = """
CREATE TABLE IF NOT EXISTS iso_lmp_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    iso                 TEXT        NOT NULL,
    location            TEXT        NOT NULL,
    location_type       TEXT,
    lmp_usd_mwh         NUMERIC(10,2) NOT NULL,
    congestion_usd_mwh  NUMERIC(10,2),
    energy_usd_mwh      NUMERIC(10,2),
    loss_usd_mwh        NUMERIC(10,2),
    interval_ending     TIMESTAMPTZ NOT NULL,
    interval_minutes    INTEGER     DEFAULT 5,
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    source_url          TEXT,
    source_name         TEXT,
    UNIQUE (iso, location, interval_ending)
);
CREATE INDEX IF NOT EXISTS idx_iso_lmp_snapshots_iso_interval
    ON iso_lmp_snapshots (iso, interval_ending DESC);
"""


def _ensure_table(cur):
    cur.execute(_DDL)


def _upsert(iso, rows):
    """Idempotent upsert of one ISO's latest-interval rows + 7-day prune.
    Empty rows list = no write (per-ISO failures never poison the table)."""
    if not NEON_URL:
        return {"ok": False, "error": "no_neon_url"}
    if not rows:
        return {"ok": True, "mode": "no_rows_skipped"}
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            _ensure_table(cur)
            n = 0
            for r in rows:
                cur.execute("""
                    INSERT INTO iso_lmp_snapshots
                        (iso, location, location_type, lmp_usd_mwh,
                         congestion_usd_mwh, energy_usd_mwh, loss_usd_mwh,
                         interval_ending, fetched_at, source_url, source_name)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING,%s,%s)
                    ON CONFLICT (iso, location, interval_ending) DO UPDATE SET
                        lmp_usd_mwh        = EXCLUDED.lmp_usd_mwh,
                        congestion_usd_mwh = EXCLUDED.congestion_usd_mwh,
                        energy_usd_mwh     = EXCLUDED.energy_usd_mwh,
                        loss_usd_mwh       = EXCLUDED.loss_usd_mwh,
                        location_type      = EXCLUDED.location_type,
                        fetched_at         = NOW(),
                        source_url         = EXCLUDED.source_url,
                        source_name        = EXCLUDED.source_name
                """, (iso, r["location"], r["location_type"], r["lmp_usd_mwh"],
                      r["congestion_usd_mwh"], r["energy_usd_mwh"], r["loss_usd_mwh"],
                      r["interval_ending"], r["source_url"], SOURCE_NAMES.get(iso)))
                n += 1
            # Bounded history: keep 7 days per ISO.
            cur.execute("""
                DELETE FROM iso_lmp_snapshots
                WHERE iso = %s AND interval_ending < NOW() - INTERVAL '7 days'
            """, (iso,))
            pruned = cur.rowcount
        return {"ok": True, "mode": "insert_or_update", "rows": n, "pruned": pruned}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _admin_gate():
    """If LMP_INGEST_TOKEN is set, POSTs must carry it (X-Admin-Token header
    or ?token=). If unset, ingest stays open (keyless cron curl — same
    posture as /api/v1/iso-queue/ingest). Returns an error response or None."""
    tok = (os.environ.get("LMP_INGEST_TOKEN") or "").strip()
    if not tok:
        return None
    got = request.headers.get("X-Admin-Token") or request.args.get("token") or ""
    if got != tok:
        return jsonify({"error": "unauthorized",
                        "hint": "X-Admin-Token header required"}), 401
    return None


# ══════════════════════════════════════════════════════════════════════
# HTTP endpoints
# ══════════════════════════════════════════════════════════════════════
@iso_lmp_ingest_bp.route("/ingest", methods=["POST"])
def ingest_all():
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({"error": "unauthorized"}), 401
    gate = _admin_gate()
    if gate:
        return gate
    started = datetime.datetime.now(UTC)
    results = []
    for iso, fn in INGESTORS.items():
        try:
            ok, rows, debug = fn()
        except Exception as e:      # belt-and-braces: one ISO never kills the batch
            ok, rows, debug = False, None, f"uncaught: {type(e).__name__}: {str(e)[:120]}"
        up = _upsert(iso, rows or []) if ok else {"ok": False, "skipped": "fetch_failed"}
        results.append({
            "iso": iso, "fetch_ok": ok, "rows": len(rows or []),
            "interval_ending_utc": (max(r["interval_ending"] for r in rows).isoformat()
                                    if rows else None),
            "debug": debug, "upsert": up,
            "parser": PARSER_STATUS.get(iso, "unknown"),
        })
    elapsed_ms = int((datetime.datetime.now(UTC) - started).total_seconds() * 1000)
    healthy = sum(1 for r in results if r["fetch_ok"])
    return jsonify({
        "started_at": started.isoformat(),
        "elapsed_ms": elapsed_ms,
        "isos_total": len(INGESTORS),
        "isos_ok": healthy,
        "isos_failed": len(INGESTORS) - healthy,
        "results": results,
        "note": "Real-time hub/zone LMP snapshots. All interval_ending stamps "
                "converted to UTC on ingest. ERCOT is LMP-only (no congestion "
                "split published). PJM uses the UNVERIFIED real-time feed. "
                "ISO-NE absent (registration-gated).",
    }), 200 if healthy == len(INGESTORS) else 207


@iso_lmp_ingest_bp.route("/ingest/<iso>", methods=["POST"])
def ingest_one(iso):
    if not is_valid_internal_key(request.headers.get("X-Internal-Key") or request.headers.get("X-Admin-Key")):
        return jsonify({"error": "unauthorized"}), 401
    gate = _admin_gate()
    if gate:
        return gate
    iso = iso.upper()
    fn = INGESTORS.get(iso)
    if not fn:
        return jsonify({"error": "unknown_iso", "iso": iso,
                        "available": list(INGESTORS.keys())}), 404
    ok, rows, debug = fn()
    up = _upsert(iso, rows or []) if ok else {"ok": False, "skipped": "fetch_failed"}
    return jsonify({
        "iso": iso, "fetch_ok": ok, "rows": len(rows or []),
        "sample": ({**rows[0], "interval_ending": rows[0]["interval_ending"].isoformat()}
                   if rows else None),
        "debug": debug, "upsert": up,
        "parser": PARSER_STATUS.get(iso, "unknown"),
    })


@iso_lmp_ingest_bp.route("/snapshot", methods=["GET"])
def snapshot():
    """Public: latest interval per ISO. Reads DISTINCT ISOs from the table —
    no hardcoded ISO list, so new ingestors surface automatically."""
    if not NEON_URL:
        return jsonify({"error": "no_neon_url"}), 500
    want = (request.args.get("iso") or "").upper().strip()
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT s.iso, s.location, s.location_type, s.lmp_usd_mwh,
                       s.congestion_usd_mwh, s.energy_usd_mwh, s.loss_usd_mwh,
                       s.interval_ending, s.fetched_at, s.source_url, s.source_name
                FROM iso_lmp_snapshots s
                JOIN (SELECT iso, MAX(interval_ending) AS mx
                      FROM iso_lmp_snapshots GROUP BY iso) m
                  ON m.iso = s.iso AND m.mx = s.interval_ending
                WHERE (%s = '' OR s.iso = %s)
                ORDER BY s.iso, s.lmp_usd_mwh DESC
            """, (want, want))
            recs = cur.fetchall()
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    by_iso = {}
    for (iso, loc, ltype, lmp, cong, ene, loss, end, fetched, surl, sname) in recs:
        e = by_iso.setdefault(iso, {
            "iso": iso,
            "interval_ending_utc": end.astimezone(UTC).isoformat(),
            "fetched_at": fetched.astimezone(UTC).isoformat() if fetched else None,
            "source_name": sname, "source_url": surl,
            "interval": "5-min real-time",
            "locations": [],
        })
        e["locations"].append({
            "name": loc, "type": ltype,
            "lmp_usd_mwh": float(lmp),
            "congestion_usd_mwh": float(cong) if cong is not None else None,
            "energy_usd_mwh": float(ene) if ene is not None else None,
            "loss_usd_mwh": float(loss) if loss is not None else None,
        })
    return jsonify({
        "as_of": datetime.datetime.now(UTC).isoformat(),
        "isos": sorted(by_iso.keys()),
        "snapshots": list(by_iso.values()),
        "notes": {
            "timezones": "All interval_ending stamps are UTC (converted from "
                         "each ISO's prevailing publication time on ingest).",
            "ERCOT": "LMP only — the np6-788-cd display publishes no "
                     "congestion/energy/loss split (fields are null, not zero).",
            "NYISO": "Sign convention: LBMP = energy + losses - congestion; "
                     "components stored as published.",
            "PJM": "Real-time values come from PJM's UNVERIFIED 5-min feed; "
                   "verified LMPs post next business day.",
            "ISO-NE": "Not covered — real-time LMP API is registration-gated.",
        },
    })


@iso_lmp_ingest_bp.route("/parser-versions", methods=["GET"])
def parser_versions():
    return jsonify({"ua": UA, "parsers": PARSER_STATUS,
                    "iso_ne": "absent by design — registration-gated source"})
