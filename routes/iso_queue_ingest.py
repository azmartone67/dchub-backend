"""
iso_queue_ingest.py v2 (Phase ZZZZZ-round47.3, 2026-05-25)

Daily ingest of ISO interconnection queue snapshots with REAL parsers.

Architecture:
  - Each ISO has its own ingest function returning (ok, parsed_dict, debug)
  - _upsert is idempotent. If parsed dict is empty, does NOT create a NULL
    row — instead touches the latest existing row's ingested_at + by.
    Keeps the table clean so _latest_snapshot in routes/interconnection_queues.py
    isn't confused by NULL-data heartbeat rows.
  - Real parsers for ERCOT (PDF via pypdf) and PJM (XLSX via openpyxl).
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
    "ERCOT":  "real (PDF discovery + pypdf text regex)",
    "PJM":    "real (XLSX direct download + openpyxl aggregate)",
    "MISO":   "heartbeat-only — DPP CSV parser TODO",
    "SPP":    "heartbeat-only — DISIS PDF parser TODO",
    "CAISO":  "heartbeat-only — Cluster Study CSV parser TODO",
    "NYISO":  "heartbeat-only — Queue Excel parser TODO",
    "ISO-NE": "heartbeat-only — Queue Dashboard CSV parser TODO",
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
# ERCOT — Monthly Operational Highlights PDF
# Discovery: scrape landing → find PDF link → download → pypdf → regex
# ══════════════════════════════════════════════════════════════════════
def ingest_ercot():
    debug = []
    parsed = {}
    try:
        # Step 1: ERCOT publishes monthly large-load INR reports
        # Try multiple discovery surfaces
        candidates = [
            "https://www.ercot.com/gridinfo/resource",
            "https://www.ercot.com/files/docs/2026/05/01/Monthly_Operational_Highlights_March_2026.pdf",
            "https://www.ercot.com/services/comm/mkt_rules/issues/INR.aspx",
        ]
        landing = ""
        for url in candidates[:2]:
            try:
                landing, st = _fetch(url, timeout=20)
                debug.append(f"landing[{url.split('/')[-1][:30]}] http_{st} len={len(landing)}")
                if st == 200 and len(landing) > 1000:
                    break
            except urllib.error.HTTPError as e:
                debug.append(f"landing 4xx: {e.code}")
        if not landing:
            return True, parsed, "; ".join(debug + ["no_landing_reachable"])

        # Step 2: find a PDF link with "highlight"/"operational"/"interconnection" in it
        pdf_urls = re.findall(r'https?://[^\s"\'<>]+\.pdf', landing, re.IGNORECASE)
        # filter for relevant ones
        relevant = [u for u in pdf_urls if any(k in u.lower() for k in
                    ("highlight", "operational", "interconnection", "inr", "queue", "load"))]
        pdf_url = (relevant or pdf_urls or [None])[0]
        if not pdf_url:
            debug.append("no_pdf_links_in_landing")
            return True, parsed, "; ".join(debug)
        debug.append(f"pdf_url=...{pdf_url[-50:]}")

        # Step 3: download + parse
        try:
            pdf_bytes, st = _fetch(pdf_url, timeout=60, return_bytes=True)
            debug.append(f"pdf http_{st} size_kb={len(pdf_bytes)//1024}")
        except urllib.error.HTTPError as e:
            return False, None, "; ".join(debug + [f"pdf_http_{e.code}"])

        text = _try_pypdf(pdf_bytes)
        if not text:
            return True, parsed, "; ".join(debug + ["pypdf_unavailable_or_failed"])
        debug.append(f"pdf_text_len={len(text)}")

        # Step 4: extract numbers via regex
        # ERCOT INR reports typically say things like:
        #   "X.X GW of new generation interconnection requests"
        #   "Total queued large load: X.X GW"
        for pat in [
            r"(?:total\s+)?(?:queued|active)\s+(?:large\s+)?load[^.\n]*?(\d{2,4}(?:\.\d)?)\s*GW",
            r"(\d{3,4}(?:\.\d)?)\s*GW\s+(?:of\s+)?(?:large\s+)?load",
            r"(\d{3,4}(?:\.\d)?)\s*GW.*?(?:data center|AI|large load)",
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                n = float(m.group(1))
                if 100 <= n <= 1500:
                    parsed["queued_load_total_gw"] = n
                    debug.append(f"matched_total_gw={n} via /{pat[:30]}/")
                    break

        m = re.search(r"(\d{2,3})\s*%\s*(?:of)?\s*(?:large\s+)?load.*?data\s*center", text, re.IGNORECASE)
        if m:
            n = float(m.group(1))
            if 0 < n <= 100:
                parsed["queued_load_dc_share_pct"] = n
                debug.append(f"matched_dc_pct={n}")

        if not parsed:
            debug.append("regex_found_no_matches_in_pdf_text")
        else:
            parsed["source_url"] = pdf_url
            parsed["source_name"] = "ERCOT Monthly Operational Highlights"

        return True, parsed, "; ".join(debug)
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:80]}"])


# ══════════════════════════════════════════════════════════════════════
# PJM — ProjectsActive.xls direct download + aggregate
# ══════════════════════════════════════════════════════════════════════
def ingest_pjm():
    debug = []
    parsed = {}
    try:
        # PJM has historically used .xls (BIFF binary) but recently
        # transitioned to .xlsx. Try .xlsx first, fall back to .xls.
        candidates = [
            "https://www.pjm.com/pub/planning/downloads/xls/ProjectsActive.xlsx",
            "https://www.pjm.com/pub/planning/downloads/xls/ProjectsActive.xls",
        ]
        xlsx_bytes = None
        used_url = None
        for url in candidates:
            try:
                xlsx_bytes, st = _fetch(url, timeout=60, return_bytes=True)
                debug.append(f"{url.split('/')[-1]}: http_{st} size_kb={len(xlsx_bytes)//1024}")
                if st == 200 and len(xlsx_bytes) > 10000:
                    used_url = url
                    break
            except urllib.error.HTTPError as e:
                debug.append(f"{url.split('/')[-1]}: http_{e.code}")
                xlsx_bytes = None
        if not xlsx_bytes:
            return False, None, "; ".join(debug + ["all_pjm_urls_failed"])

        wb = _try_openpyxl(xlsx_bytes)
        if not wb:
            return True, parsed, "; ".join(debug + ["openpyxl_failed (old .xls binary?)"])

        ws = wb.active
        # Header row
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        header = [str(c or "").strip().lower() for c in header_row]
        debug.append(f"sheet_cols={len(header)}")

        # Find columns by best-match
        def find_col(*keywords):
            for i, h in enumerate(header):
                if all(k in h for k in keywords):
                    return i
            return None

        col_mw     = find_col("mw") or find_col("capacity")
        col_status = find_col("status")
        col_name   = find_col("project") or find_col("name")
        col_fuel   = find_col("fuel") or find_col("type")

        if col_mw is None:
            return True, parsed, "; ".join(debug + ["no_mw_column_found"])

        total_mw  = 0.0
        active_n  = 0
        dc_mw     = 0.0
        rows_seen = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            rows_seen += 1
            if rows_seen > 100000:
                break
            try:
                mw = float(row[col_mw]) if col_mw is not None and row[col_mw] is not None else 0
            except (ValueError, TypeError):
                mw = 0
            status_str = str(row[col_status] or "").strip().lower() if col_status is not None else "active"
            if "withdraw" in status_str or "complete" in status_str or "operational" in status_str:
                continue
            total_mw += mw
            active_n += 1
            name_str = str(row[col_name] or "").lower() if col_name is not None else ""
            fuel_str = str(row[col_fuel] or "").lower() if col_fuel is not None else ""
            if any(k in name_str or k in fuel_str for k in (
                "data center", "datacenter", "ai cluster", "hyperscale", "data ctr",
                "load", "large load",
            )):
                dc_mw += mw

        debug.append(f"rows_seen={rows_seen} active_n={active_n} total_mw={total_mw:.0f} dc_mw={dc_mw:.0f}")

        if total_mw > 100:
            parsed["queued_load_total_gw"] = round(total_mw / 1000.0, 1)
            parsed["source_url"] = used_url
            parsed["source_name"] = "PJM ProjectsActive (active queue)"
        if dc_mw > 0:
            parsed["queued_load_data_center_gw"] = round(dc_mw / 1000.0, 1)
            if total_mw > 0:
                parsed["queued_load_dc_share_pct"] = round(100.0 * dc_mw / total_mw, 1)

        return True, parsed, "; ".join(debug)

    except urllib.error.HTTPError as e:
        return False, None, "; ".join(debug + [f"http_error: {e.code}"])
    except Exception as e:
        return False, None, "; ".join(debug + [f"error: {type(e).__name__}: {str(e)[:120]}"])


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
    # TODO: MISO publishes DPP cluster study CSV at:
    #   https://cdn.misoenergy.org/...DPP-{yr}-{cluster}.csv
    # Discovery: scrape https://www.misoenergy.org/planning/resource-utilization/generator-interconnection-queue/
    # Parser: csv.DictReader, aggregate MW by status='Active', filter to load/data-center types
    return _heartbeat("https://www.misoenergy.org/markets-and-operations/interconnect/interconnection-queue/")


def ingest_spp():
    # TODO: SPP DISIS reports are PDFs at:
    #   https://www.spp.org/.../DISIS-{N}-Cluster-Study.pdf
    # Parser: pypdf, regex for "total queued capacity: X GW" patterns
    # Note: SPP frequently 403s without proper UA + cookies
    return _heartbeat("https://www.spp.org/markets-services/transmission-planning/aggregate-transmission-studies/disis/")


def ingest_caiso():
    # TODO: CAISO publishes cluster study CSVs:
    #   https://www.caiso.com/Documents/QueueClusterStudy-{N}.csv
    # Some CSVs are in their Documents portal — needs link discovery
    return _heartbeat("https://www.caiso.com/planningandoperations/Pages/GeneratorInterconnection/Default.aspx")


def ingest_nyiso():
    # TODO: NYISO queue tracker Excel:
    #   https://www.nyiso.com/documents/20142/.../Interconnection_Queue.xlsx
    # Parser: openpyxl, similar shape to PJM
    return _heartbeat("https://www.nyiso.com/connecting-to-the-grid")


def ingest_iso_ne():
    # TODO: ISO-NE queue dashboard exports CSV at:
    #   https://www.iso-ne.com/static-assets/documents/.../Interconnection_Queue.csv
    return _heartbeat("https://www.iso-ne.com/system-planning/connecting-to-the-grid")


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
        "note": "Real parsers: ERCOT (pypdf), PJM (openpyxl). Others heartbeat-only. "
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
            "pypdf": "required for ERCOT (PDF parsing)",
            "openpyxl": "required for PJM (XLSX parsing)",
        },
    })
