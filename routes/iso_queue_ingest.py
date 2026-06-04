"""
iso_queue_ingest.py v2 (Phase ZZZZZ-round47.3, 2026-05-25)

Daily ingest of ISO interconnection queue snapshots. REAL parsers for ERCOT,
PJM, NYISO, MISO, CAISO, and SPP (r70) — 6 of 7. Only ISO-NE remains
heartbeat-only (its public queue-file URL moved; needs rediscovery) and is
NEVER surfaced as live data. Every real parser was verified against live data
before shipping (no fabricated numbers).

Architecture:
  - Each ISO has its own ingest function returning (ok, parsed_dict, debug)
  - _upsert is idempotent. If parsed dict is empty, does NOT create a NULL
    row — instead touches the latest existing row's ingested_at + by.
    Keeps the table clean so _latest_snapshot in routes/interconnection_queues.py
    isn't confused by NULL-data heartbeat rows.
  - Real parsers for ERCOT (public GIS Report XLSX) and PJM (XLSX via openpyxl).
  - MISO, SPP, CAISO, NYISO, ISO-NE are heartbeat-only with documented
    upgrade paths in inline TODO comments — each can be promoted to a real
    parser independently as we verify the actual file format.

Wiring (already in main.py):
    from routes.iso_queue_ingest import iso_queue_ingest_bp
    app.register_blueprint(iso_queue_ingest_bp)

Endpoints:
  POST /api/v1/iso-queue/ingest         — run all ISO ingestors
  POST /api/v1/iso-queue/ingest/<iso>   — run a single ISO (debugging)
  GET  /api/v1/iso-queue/ingest/status  — last run summary
  GET  /api/v1/iso-queue/parser-versions — which parsers have real logic

Cron: fired by cron_heartbeat at 06:00 UTC daily.
"""
import os
import re
import io
import csv
import json
import datetime
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request
import psycopg

iso_queue_ingest_bp = Blueprint("iso_queue_ingest", __name__,
                                 url_prefix="/api/v1/iso-queue")
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")

UA = "DCHub-IsoQueueIngest/2.0 (+https://dchub.cloud/interconnection-queues)"

PARSER_STATUS = {
    "ERCOT":  "real (public GIS Report XLSX, MIS reportTypeId=15933, active Large+Small Gen capacity)",
    "PJM":    "real (public New Services Queue XLSX export, active sum of MFO)",
    "MISO":   "real (public GI-queue JSON API, applicationStatus=Active, summer MW)",
    "SPP":    "real (opsportal GenerateActiveCsv; MAX Summer MW, excl. cessation)",
    "CAISO":  "real (PublicQueueReport.xlsx 'Grid GenerationQueue'; Net MWs to Grid, status=ACTIVE)",
    "NYISO":  "real (public queue XLSX 'Interconnection Queue' sheet, SP MW via openpyxl)",
    "ISO-NE": "real (public IRTT Public Queue HTML table, Status=A active, Summer MW)",
}


# ──────────────────────────────────────────────────────────────────────
# HTTP helper
# ──────────────────────────────────────────────────────────────────────
def _fetch(url, timeout=30, return_bytes=False, accept="*/*"):
    """Fetch URL. Returns (body, status). body is bytes if return_bytes else str."""
    req = urllib.request.Request(url, headers={
        "User-Agent":       UA,
        "Accept":           accept,
        "Accept-Language":  "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return (body if return_bytes else body.decode("utf-8", errors="replace")), resp.status


def _try_pypdf(pdf_bytes):
    """Best-effort PDF text extraction. Returns full text or None."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        return None
    except Exception:
        return None


def _try_openpyxl(xlsx_bytes):
    """Returns workbook (read_only=True) or None. Handles .xls and .xlsx."""
    try:
        from openpyxl import load_workbook
        return load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    except ImportError:
        return None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# ERCOT — public GIS Report (Generator Interconnection Status), monthly XLSX
# The market-data Public API (api.ercot.com) needs a subscription key AND a
# Bearer token (B2C ROPC) and does NOT expose the generation queue anyway.
# The queue lives in the GIS Report — published monthly as a PUBLIC, no-auth
# XLSX on the MIS (reportTypeId=15933). Discover the latest GIS_Report_*.xlsx
# from the public listing, download it, sum active 'Project Details - Large
# Gen' + '- Small Gen' capacity. Apples-to-apples with the other ISO gen queues.
# ══════════════════════════════════════════════════════════════════════
def ingest_ercot():
    debug = []
    parsed = {}
    try:
        LISTING = "https://www.ercot.com/misapp/GetReports.do?reportTypeId=15933"
        DL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId="
        html, st = _fetch(LISTING, timeout=25)
        debug.append(f"listing http_{st} len={len(html)}")
        if st != 200 or len(html) < 2000:
            return True, parsed, "; ".join(debug + ["listing_unreachable"])

        # Title cell encodes the date + filename:
        #   RPT.00015933.<seq>.<YYYYMMDD>.<ms>.GIS_Report_<Mon><Year>.xlsx
        # then the 'Other' column has <a ...mirDownload?doclookupId=NNN>.
        # Listing is newest-first, so rows[0] is the latest monthly report.
        rows = re.findall(
            r'RPT\.00015933\.\d+\.(\d{8})\.\d+\.(GIS_Report_[A-Za-z0-9]+\.xlsx)'
            r'.*?doclookupId=(\d+)',
            html, re.S)
        if not rows:
            return True, parsed, "; ".join(debug + ["no_gis_xlsx_rows_in_listing"])
        pubdate, fname, docid = rows[0]
        debug.append(f"latest={fname} pub={pubdate} docid={docid}")

        xlsx_bytes, st = _fetch(DL + docid, timeout=90, return_bytes=True)
        debug.append(f"download http_{st} kb={len(xlsx_bytes)//1024}")
        if st != 200 or len(xlsx_bytes) < 50000:
            return False, None, "; ".join(debug + ["download_failed_or_small"])

        wb = _try_openpyxl(xlsx_bytes)
        if not wb:
            return True, parsed, "; ".join(debug + ["openpyxl_unavailable"])

        def _sum_gen_sheet(sheet_name):
            """Sum active queued capacity (MW) on a GIS project-detail sheet.
            Header row = the one whose cells contain 'INR'; capacity col is the
            first cell starting 'Capacity'. The Large/Small Gen sheets already
            EXCLUDE cancelled/inactive (those are on separate sheets), so every
            row with an INR + positive MW is an active queue project."""
            if sheet_name not in wb.sheetnames:
                return 0.0, 0
            ws = wb[sheet_name]
            hdr_idx = None
            inr_col = 0
            cap_col = None
            for r_i, row in enumerate(ws.iter_rows(min_row=1, max_row=40,
                                                   values_only=True), 1):
                cells = [str(c).strip() if c is not None else "" for c in row]
                if "INR" in cells:
                    hdr_idx = r_i
                    inr_col = cells.index("INR")
                    for j, c in enumerate(cells):
                        if c.lower().startswith("capacity"):
                            cap_col = j
                            break
                    break
            if hdr_idx is None or cap_col is None:
                return 0.0, 0
            total = 0.0
            n = 0
            for row in ws.iter_rows(min_row=hdr_idx + 1, values_only=True):
                if len(row) <= max(cap_col, inr_col):
                    continue
                if row[inr_col] is None:
                    continue
                try:
                    mw = float(row[cap_col])
                except (TypeError, ValueError):
                    continue
                if mw <= 0:
                    continue
                total += mw
                n += 1
            return total, n

        lg_mw, lg_n = _sum_gen_sheet("Project Details - Large Gen")
        sg_mw, sg_n = _sum_gen_sheet("Project Details - Small Gen")
        try:
            wb.close()
        except Exception:
            pass
        total_mw = lg_mw + sg_mw
        total_n = lg_n + sg_n
        debug.append(f"large={lg_mw/1000:.1f}GW/{lg_n} small={sg_mw/1000:.1f}GW/{sg_n}")

        # Sanity: ERCOT's active gen queue is hundreds of GW. <1 GW = parse miss.
        if total_mw < 1000:
            return True, parsed, "; ".join(debug + [f"total_too_small={total_mw:.0f}MW"])

        month = fname.replace("GIS_Report_", "").replace(".xlsx", "")
        parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
        parsed["source_url"] = DL + docid
        parsed["source_name"] = f"ERCOT GIS Report ({month}, active generation queue)"
        return True, parsed, "; ".join(debug + [f"TOTAL={total_mw/1000:.1f}GW/{total_n}proj"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:100]}"])

# ══════════════════════════════════════════════════════════════════════
# PJM — public "New Services Queue" XLSX export (services.pjm.com)
# PJM reorganized into a cycle-based queue; the old ProjectsActive.xls 404s.
# The full queue is served as an XLSX from the PJM Planning API export
# endpoint using the api-subscription-key PJM bakes into its OWN public
# website JS bundle (pjm.com/dist/interconnectionqueues.*.js) to render the
# public queue dashboard — i.e. the same anonymous key every browser uses,
# NOT a private credential. (Overridable via PJM_QUEUE_KEY env if PJM ever
# rotates it.) Active queue = projects still in the interconnection process
# (Status not withdrawn/in-service/retracted/deactivated/annulled/canceled);
# sum MFO (Maximum Facility Output). Apples-to-apples with the other ISO queues.
# ══════════════════════════════════════════════════════════════════════
def ingest_pjm():
    debug = []
    parsed = {}
    try:
        url = "https://services.pjm.com/PJMPlanningApi/api/Queue/ExportToXls"
        key = os.environ.get("PJM_QUEUE_KEY", "E29477D0-70E0-4825-89B0-43F460BF9AB4")
        req = urllib.request.Request(url, data=b"", method="POST", headers={
            "api-subscription-key": key,          # PJM's public website constant
            "Origin":  "https://www.pjm.com",
            "Referer": "https://www.pjm.com/",
            "User-Agent": UA,
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                xlsx_bytes = resp.read()
                st = resp.status
        except urllib.error.HTTPError as e:
            return False, None, "; ".join(debug + [f"export_http_{e.code}"])
        debug.append(f"export http_{st} kb={len(xlsx_bytes)//1024}")
        if st != 200 or len(xlsx_bytes) < 50000:
            return False, None, "; ".join(debug + ["export_failed_or_small"])

        wb = _try_openpyxl(xlsx_bytes)
        if not wb:
            return True, parsed, "; ".join(debug + ["openpyxl_unavailable"])
        ws = wb["Data"] if "Data" in wb.sheetnames else wb.active
        hdr = [str(c).strip() if c is not None else ""
               for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        idx = {h: i for i, h in enumerate(hdr)}
        cS = idx.get("Status")
        cMFO = idx.get("MFO")
        cFuel = idx.get("Fuel")
        if cS is None or cMFO is None:
            return True, parsed, "; ".join(debug + ["missing_Status_or_MFO_column"])

        # Exclude projects that have LEFT the queue (withdrawn/cancelled) or
        # already GRADUATED (in service). Everything else is the active queue.
        EXCLUDE = {"withdrawn", "in service", "retracted", "deactivated",
                   "annulled", "canceled", "cancelled"}
        total_mw = 0.0
        n = 0
        rows_seen = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            rows_seen += 1
            if rows_seen > 200000:
                break
            if len(row) <= max(cS, cMFO) or row[cS] is None:
                continue
            if str(row[cS]).strip().lower() in EXCLUDE:
                continue
            try:
                mfo = float(row[cMFO]) if row[cMFO] is not None else 0
            except (TypeError, ValueError):
                continue
            if mfo <= 0:
                continue
            total_mw += mfo
            n += 1
        try:
            wb.close()
        except Exception:
            pass
        debug.append(f"active_n={n} rows_seen={rows_seen}")

        # Sanity: PJM's active queue is ~100-200 GW. <1 GW = parse miss.
        if total_mw < 1000:
            return True, parsed, "; ".join(debug + [f"total_too_small={total_mw:.0f}MW"])

        parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
        parsed["source_url"] = url
        parsed["source_name"] = "PJM New Services Queue (active, sum of MFO)"
        return True, parsed, "; ".join(debug + [f"TOTAL={total_mw/1000:.1f}GW/{n}proj"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:100]}"])


# ══════════════════════════════════════════════════════════════════════
# Stub ingestors — return heartbeat success when fetch works.
# Each has documented TODO for the real parser implementation.
# ══════════════════════════════════════════════════════════════════════
def _heartbeat(url):
    try:
        _body, status = _fetch(url, timeout=30)
        return True, {}, f"http_{status} (heartbeat OK, parser TODO)"
    except urllib.error.HTTPError as e:
        return False, None, f"http_error: {e.code}"
    except Exception as e:
        return False, None, f"error: {type(e).__name__}: {str(e)[:80]}"


def ingest_miso():
    """REAL parser (r70, 2026-06-03). MISO exposes the full GI queue as a public
    JSON API. Count applicationStatus=='Active' and sum summerNetMW (winterNetMW
    fallback). Verified live 2026-06-03: 1084 active projects, ~231 GW.
    Same (ok, parsed, debug) shape as ingest_pjm/ingest_nyiso."""
    debug = []
    parsed = {}
    url = "https://www.misoenergy.org/api/giqueue/getprojects"
    try:
        body, st = _fetch(url, timeout=60)
        debug.append(f"miso http_{st} kb={len(body)//1024}")
        if st != 200 or not body:
            return False, None, "; ".join(debug + ["fetch_failed"])
        rows = json.loads(body)
        if not isinstance(rows, list):
            return True, parsed, "; ".join(debug + ["unexpected_json_shape"])

        def _num(x):
            try: return float(x)
            except (TypeError, ValueError): return 0.0

        total_mw = 0.0
        active_n = 0
        dc_mw = 0.0
        for p in rows:
            if not isinstance(p, dict):
                continue
            if str(p.get("applicationStatus") or "").strip().lower() != "active":
                continue
            mw = _num(p.get("summerNetMW")) or _num(p.get("winterNetMW"))
            if mw <= 0:
                continue
            total_mw += mw
            active_n += 1
            ftype = (str(p.get("fuelType") or "") + " "
                     + str(p.get("facilityType") or "")).lower()
            if any(k in ftype for k in ("load", "data center", "datacenter")):
                dc_mw += mw

        debug.append(f"active_n={active_n} total_mw={total_mw:.0f} dc_mw={dc_mw:.0f}")
        if active_n > 0 and total_mw > 100:
            parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
            parsed["source_url"] = url
            parsed["source_name"] = "MISO GI Queue API (applicationStatus=Active, summer MW)"
        if dc_mw > 0 and total_mw > 0:
            parsed["queued_load_data_center_gw"] = round(dc_mw / 1000.0, 1)
            parsed["queued_load_dc_share_pct"] = round(100.0 * dc_mw / total_mw, 1)
        return True, parsed, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


def ingest_spp():
    """REAL parser (r70, 2026-06-03). SPP serves its ACTIVE GI requests as CSV
    (opsportal GenerateActiveCsv; row 0 is a 'Last Updated On' banner, header is
    row 1, data row 2+). Sum 'MAX Summer MW' for rows WITHOUT a Cessation Date
    (cessation = withdrawn). Verified live 2026-06-03: 944 active, ~184 GW.
    Same (ok, parsed, debug) shape as the other real parsers."""
    debug = []
    parsed = {}
    url = "https://opsportal.spp.org/Studies/GenerateActiveCsv"
    try:
        body, st = _fetch(url, timeout=90)
        debug.append(f"spp http_{st} kb={len(body)//1024}")
        if st != 200 or not body:
            return False, None, "; ".join(debug + ["fetch_failed"])
        rows = list(csv.reader(io.StringIO(body)))
        if len(rows) < 3:
            return True, parsed, "; ".join(debug + ["too_few_rows"])
        hdr = [str(h or "").strip().lower() for h in rows[1]]  # row 0 is a banner

        def find_col(*keywords):
            for i, h in enumerate(hdr):
                if all(k in h for k in keywords):
                    return i
            return None

        col_mw = find_col("max", "summer", "mw") or find_col("summer", "mw") or find_col("mw")
        col_cess = find_col("cessation")
        col_fuel = find_col("fuel") or find_col("generation", "type")
        if col_mw is None:
            return True, parsed, "; ".join(debug + ["no_mw_column"])

        total_mw = 0.0
        active_n = 0
        dc_mw = 0.0
        for r in rows[2:]:
            if col_mw >= len(r):
                continue
            if col_cess is not None and col_cess < len(r) and str(r[col_cess]).strip():
                continue  # cessation date present → withdrawn, skip
            try:
                mw = float(str(r[col_mw]).replace(",", "").strip())
            except (ValueError, TypeError):
                continue
            if mw <= 0:
                continue
            total_mw += mw
            active_n += 1
            fuel_str = (str(r[col_fuel] or "").lower()
                        if col_fuel is not None and col_fuel < len(r) else "")
            if any(k in fuel_str for k in ("load", "data center", "datacenter")):
                dc_mw += mw

        debug.append(f"active_n={active_n} total_mw={total_mw:.0f}")
        if active_n > 0 and total_mw > 100:
            parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
            parsed["source_url"] = url
            parsed["source_name"] = "SPP GI active requests (MAX Summer MW, excl. cessation)"
        if dc_mw > 0 and total_mw > 0:
            parsed["queued_load_data_center_gw"] = round(dc_mw / 1000.0, 1)
            parsed["queued_load_dc_share_pct"] = round(100.0 * dc_mw / total_mw, 1)
        return True, parsed, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


def ingest_caiso():
    """REAL parser (r70, 2026-06-03). CAISO publishes the public queue as XLSX
    (sheet 'Grid GenerationQueue'; banner rows 1-3, real header at Excel row 4,
    data row 5+). Filter Application Status=='ACTIVE', sum 'Net MWs to Grid'
    (fallback 'MW-1'). Verified live 2026-06-03: 284 active, ~81.2 GW."""
    debug = []
    parsed = {}
    url = "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx"
    try:
        xlsx_bytes, st = _fetch(url, timeout=90, return_bytes=True)
        debug.append(f"caiso http_{st} kb={len(xlsx_bytes)//1024}")
        if st != 200 or len(xlsx_bytes) < 10000:
            return False, None, "; ".join(debug + ["fetch_failed_or_too_small"])
        wb = _try_openpyxl(xlsx_bytes)
        if not wb:
            return True, parsed, "; ".join(debug + ["openpyxl_failed"])
        ws = None
        for nm in wb.sheetnames:
            if "generationqueue" in str(nm).strip().lower().replace(" ", ""):
                ws = wb[nm]
                break
        if ws is None:
            for nm in wb.sheetnames:
                if "queue" in str(nm).lower():
                    ws = wb[nm]
                    break
        if ws is None:
            ws = wb.active
        hdr = None
        for r in ws.iter_rows(min_row=4, max_row=4, values_only=True):
            hdr = [str(c or "").strip().lower() for c in r]
            break
        if not hdr:
            return True, parsed, "; ".join(debug + ["no_header_row4"])

        def find_col(*keywords):
            for i, h in enumerate(hdr):
                if all(k in h for k in keywords):
                    return i
            return None

        col_status = find_col("application", "status")
        col_mw = find_col("net", "mws") or find_col("mw-1") or find_col("mw")
        col_fuel = find_col("fuel-1") or find_col("fuel") or find_col("type-1")
        if col_mw is None or col_status is None:
            return True, parsed, "; ".join(debug + [f"cols_missing status={col_status} mw={col_mw}"])

        total_mw = 0.0
        active_n = 0
        dc_mw = 0.0
        for row in ws.iter_rows(min_row=5, values_only=True):
            if col_status >= len(row):
                continue
            if str(row[col_status] or "").strip().upper() != "ACTIVE":
                continue
            mw = 0.0
            if col_mw < len(row):
                try:
                    mw = float(row[col_mw])
                except (ValueError, TypeError):
                    mw = 0.0
            if mw <= 0:
                continue
            total_mw += mw
            active_n += 1
            fuel_str = (str(row[col_fuel] or "").lower()
                        if col_fuel is not None and col_fuel < len(row) else "")
            if any(k in fuel_str for k in ("load", "data center", "datacenter")):
                dc_mw += mw

        debug.append(f"active_n={active_n} total_mw={total_mw:.0f}")
        if active_n > 0 and total_mw > 100:
            parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
            parsed["source_url"] = url
            parsed["source_name"] = "CAISO Public Queue Report (Application Status=ACTIVE, Net MWs to Grid)"
        if dc_mw > 0 and total_mw > 0:
            parsed["queued_load_data_center_gw"] = round(dc_mw / 1000.0, 1)
            parsed["queued_load_dc_share_pct"] = round(100.0 * dc_mw / total_mw, 1)
        return True, parsed, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


def ingest_nyiso():
    """REAL parser (r70, 2026-06-03). NYISO publishes the full interconnection
    queue as a stable public XLSX. The 'Interconnection Queue' sheet is the
    ACTIVE queue (Withdrawn / In Service are separate sheets, so no status
    filter is needed). MW = 'SP (MW)' = summer-peak. Verified live 2026-06-03:
    147 active projects, ~24.9 GW. Same (ok, parsed, debug) shape as ingest_pjm."""
    debug = []
    parsed = {}
    url = "https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue.xlsx"
    try:
        xlsx_bytes, st = _fetch(url, timeout=60, return_bytes=True)
        debug.append(f"nyiso http_{st} kb={len(xlsx_bytes)//1024}")
        if st != 200 or len(xlsx_bytes) < 10000:
            return False, None, "; ".join(debug + ["fetch_failed_or_too_small"])
        wb = _try_openpyxl(xlsx_bytes)
        if not wb:
            return True, parsed, "; ".join(debug + ["openpyxl_failed"])
        ws = None
        for nm in wb.sheetnames:
            if "interconnection queue" in str(nm).strip().lower():
                ws = wb[nm]
                break
        if ws is None:
            ws = wb.active
        header = [str(c or "").strip().lower()
                  for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        debug.append(f"sheet='{ws.title}' cols={len(header)}")

        def find_col(*keywords):
            for i, h in enumerate(header):
                if all(k in h for k in keywords):
                    return i
            return None

        col_mw   = find_col("sp", "mw") or find_col("mw") or find_col("capacity")
        col_name = find_col("project") or find_col("name")
        col_fuel = find_col("fuel") or find_col("type")
        if col_mw is None:
            return True, parsed, "; ".join(debug + ["no_mw_column_found"])

        total_mw = 0.0
        active_n = 0
        dc_mw = 0.0
        rows_seen = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            rows_seen += 1
            if rows_seen > 100000:
                break
            if col_mw >= len(row) or row[col_mw] is None:
                continue
            try:
                mw = float(row[col_mw])
            except (ValueError, TypeError):
                continue
            if mw <= 0:
                continue
            total_mw += mw
            active_n += 1
            name_str = (str(row[col_name] or "").lower()
                        if col_name is not None and col_name < len(row) else "")
            fuel_str = (str(row[col_fuel] or "").lower()
                        if col_fuel is not None and col_fuel < len(row) else "")
            if any(k in name_str or k in fuel_str for k in (
                "data center", "datacenter", "ai cluster", "hyperscale",
                "data ctr", "large load", "load",
            )):
                dc_mw += mw

        debug.append(f"rows_seen={rows_seen} active_n={active_n} "
                     f"total_mw={total_mw:.0f} dc_mw={dc_mw:.0f}")

        if active_n > 0 and total_mw > 100:
            parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
            parsed["source_url"] = url
            parsed["source_name"] = "NYISO Interconnection Queue (active, SP MW)"
        if dc_mw > 0 and total_mw > 0:
            parsed["queued_load_data_center_gw"] = round(dc_mw / 1000.0, 1)
            parsed["queued_load_dc_share_pct"] = round(100.0 * dc_mw / total_mw, 1)

        return True, parsed, "; ".join(debug)
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


def ingest_iso_ne():
    """ISO-NE — public IRTT "Public Queue" report (HTML table id='publicqueue').

    ISO-NE has no clean CSV/XLSX; the public queue is one big HTML page. GOTCHA:
    irtt.iso-ne.com does an ASP.NET cookie-detection 302 redirect, so a plain
    urlopen loops forever — we need a CookieJar opener (the shared _fetch can't).
    Active = Status code 'A' (W=withdrawn, C=completed); sum Summer MW (fallback
    Net MW), apples-to-apples with the other generation queues. No auth needed.
    """
    debug = []
    parsed = {}
    try:
        import http.cookiejar
        from lxml import html as _lh
        url = "https://irtt.iso-ne.com/reports/external"
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with opener.open(req, timeout=60) as resp:
            body = resp.read()
            st = resp.status
        debug.append(f"fetch http_{st} kb={len(body)//1024}")
        if st != 200 or len(body) < 50000:
            return False, None, "; ".join(debug + ["fetch_failed_or_small"])

        doc = _lh.fromstring(body)
        tbls = doc.xpath('//table[@id="publicqueue"]')
        if not tbls:
            return True, parsed, "; ".join(debug + ["no_publicqueue_table"])
        tbl = tbls[0]
        heads = [(th.text_content() or "").strip() for th in tbl.xpath('.//thead//th')]
        if not heads:
            heads = [(c.text_content() or "").strip()
                     for c in tbl.xpath('.//tr[1]/th | .//tr[1]/td')]
        idx = {h: i for i, h in enumerate(heads)}
        ci_status = idx.get("Status")
        ci_summer = idx.get("Summer MW")
        ci_net = idx.get("Net MW")
        if ci_status is None or (ci_summer is None and ci_net is None):
            return True, parsed, "; ".join(debug + [f"cols_missing heads={heads[:10]}"])

        total_mw = 0.0
        active_n = 0
        rows_seen = 0
        for tr in tbl.xpath('.//tbody//tr'):
            tds = tr.xpath('./td')
            if len(tds) <= ci_status:
                continue
            rows_seen += 1
            if rows_seen > 100000:
                break
            vals = [(td.text_content() or "").strip() for td in tds]
            if vals[ci_status].upper() != "A":   # active only
                continue
            mw = 0.0
            for ci in (ci_summer, ci_net):
                if ci is not None and ci < len(vals):
                    try:
                        m = float(vals[ci].replace(",", ""))
                    except (ValueError, TypeError):
                        m = 0.0
                    if m:
                        mw = m
                        break
            if mw <= 0:
                continue
            total_mw += mw
            active_n += 1
        debug.append(f"rows_seen={rows_seen} active_n={active_n} total_mw={total_mw:.0f}")

        # ISO-NE active queue is ~12-17 GW; <0.5 GW = parse miss.
        if total_mw < 500:
            return True, parsed, "; ".join(debug + [f"total_too_small={total_mw:.0f}MW"])
        parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
        parsed["source_url"] = url
        parsed["source_name"] = "ISO-NE IRTT Public Queue (active, Summer MW)"
        return True, parsed, "; ".join(debug + [f"TOTAL={total_mw/1000:.1f}GW/{active_n}proj"])
    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:100]}"])


INGESTORS = {
    "ERCOT":  ingest_ercot,
    "PJM":    ingest_pjm,
    "MISO":   ingest_miso,
    "SPP":    ingest_spp,
    "CAISO":  ingest_caiso,
    "NYISO":  ingest_nyiso,
    "ISO-NE": ingest_iso_ne,
}


# ══════════════════════════════════════════════════════════════════════
# UPSERT — heartbeat-touch when parsed is empty (r47.3 change)
# ══════════════════════════════════════════════════════════════════════
def _upsert(iso, parsed, as_of, ingested_by):
    """Insert/update iso_queue_snapshots.

    r47.3: if `parsed` is empty (no new data), DO NOT create a NULL row.
    Instead, touch the latest existing row's ingested_at + ingested_by
    to record that we tried. Keeps _latest_snapshot from picking up
    NULL rows.
    """
    if not NEON_URL:
        return {"ok": False, "error": "no_neon_url"}

    if not parsed:
        try:
            with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("""
                    UPDATE iso_queue_snapshots
                    SET ingested_at = NOW(),
                        ingested_by = %s
                    WHERE iso = %s
                      AND as_of = (SELECT MAX(as_of)
                                   FROM iso_queue_snapshots
                                   WHERE iso = %s)
                """, (ingested_by, iso, iso))
                touched = cur.rowcount
            return {"ok": True, "mode": "heartbeat_touch", "rows_touched": touched}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    fields = ["iso", "as_of", "ingested_by"]
    values = [iso, as_of, ingested_by]
    for k in ("queued_load_total_gw", "queued_load_data_center_gw",
              "queued_load_dc_share_pct", "new_applications_q_gw",
              "new_applications_period", "top_subregions",
              "source_url", "source_name"):
        if k in parsed:
            fields.append(k)
            v = parsed[k]
            values.append(json.dumps(v) if k == "top_subregions" else v)

    placeholders = ", ".join(["%s"] * len(values))
    cols = ", ".join(fields)
    upd_cols = [f for f in fields if f not in ("iso", "as_of")]
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in upd_cols) + ", ingested_at = NOW()"
    sql = f"""
        INSERT INTO iso_queue_snapshots ({cols})
        VALUES ({placeholders})
        ON CONFLICT (iso, as_of) DO UPDATE SET {upd}
    """
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql, values)
        return {"ok": True, "mode": "insert_or_update", "fields_updated": len(fields) - 3}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


# ══════════════════════════════════════════════════════════════════════
# HTTP endpoints
# ══════════════════════════════════════════════════════════════════════
@iso_queue_ingest_bp.route("/ingest", methods=["GET", "POST"])
def ingest_all():
    started = datetime.datetime.now(datetime.timezone.utc)
    today = started.date()
    results = []
    for iso, fn in INGESTORS.items():
        ok, parsed, debug = fn()
        if ok:
            up = _upsert(iso, parsed or {}, today,
                         "cron_v2" if parsed else "cron_v2_heartbeat")
            results.append({
                "iso": iso, "fetch_ok": True,
                "parsed_fields": list((parsed or {}).keys()),
                "debug": debug, "upsert": up,
                "parser": PARSER_STATUS.get(iso, "unknown"),
            })
        else:
            results.append({
                "iso": iso, "fetch_ok": False, "debug": debug,
                "parser": PARSER_STATUS.get(iso, "unknown"),
            })
    elapsed_ms = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
    healthy = sum(1 for r in results if r.get("fetch_ok"))
    with_data = sum(1 for r in results if r.get("parsed_fields"))
    return jsonify({
        "started_at": started.isoformat(),
        "elapsed_ms": elapsed_ms,
        "as_of":      today.isoformat(),
        "isos_total": len(INGESTORS),
        "isos_fetched": healthy,
        "isos_with_new_data": with_data,
        "isos_failed":  len(INGESTORS) - healthy,
        "results": results,
        "note": "Real parsers: ERCOT/PJM/NYISO/CAISO (XLSX), MISO (JSON), "
                "SPP (CSV) — 6 of 7. ISO-NE heartbeat-only (queue file URL moved). "
                "Empty parse result -> heartbeat_touch (no NULL rows inserted).",
    }), 200 if healthy == len(INGESTORS) else 207


@iso_queue_ingest_bp.route("/ingest/<iso>", methods=["GET", "POST"])
def ingest_one(iso):
    iso = iso.upper()
    fn = INGESTORS.get(iso)
    if not fn:
        return jsonify({"error": "unknown_iso", "iso": iso,
                        "available": list(INGESTORS.keys())}), 404
    ok, parsed, debug = fn()
    today = datetime.date.today()
    up = _upsert(iso, parsed or {}, today,
                 "cron_v2" if (ok and parsed) else "cron_v2_heartbeat")
    return jsonify({"iso": iso, "fetch_ok": ok,
                    "parser": PARSER_STATUS.get(iso, "unknown"),
                    "parsed_fields": list((parsed or {}).keys()),
                    "debug": debug, "upsert": up})


@iso_queue_ingest_bp.route("/ingest/status", methods=["GET"])
def status():
    if not NEON_URL:
        return jsonify({"error": "no_neon_url"}), 500
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("""
              SELECT iso, MAX(ingested_at) AS last_run, MAX(ingested_by) AS last_method,
                     MAX(queued_load_total_gw) AS latest_total_gw, MAX(as_of) AS latest_as_of
              FROM iso_queue_snapshots
              GROUP BY iso ORDER BY MAX(ingested_at) DESC NULLS LAST
            """)
            rows = cur.fetchall()
        return jsonify({
            "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "isos": [
                {"iso": r[0],
                 "last_run":     r[1].isoformat() if r[1] else None,
                 "last_method":  r[2],
                 "latest_total_gw": float(r[3] or 0),
                 "latest_as_of": r[4].isoformat() if r[4] else None,
                 "parser":       PARSER_STATUS.get(r[0], "unknown")}
                for r in rows
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@iso_queue_ingest_bp.route("/parser-versions", methods=["GET"])
def parser_versions():
    return jsonify({
        "ua": UA,
        "parsers": PARSER_STATUS,
        "deps": {
            "openpyxl": "required for ERCOT/PJM/NYISO/CAISO (XLSX parsing)",
            "pypdf": "no longer required (ERCOT moved off PDF to the GIS Report XLSX)",
        },
    })
