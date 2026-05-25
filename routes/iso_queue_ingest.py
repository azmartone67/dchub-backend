"""
iso_queue_ingest.py — daily ingest of ISO interconnection queue snapshots.

Phase ZZZZZ-round47.2 (2026-05-25). Companion to round47's
interconnection-queues landing page. Pulls the headline queue numbers
(total queued GW, DC share %, top BUILD subregions) from each ISO's
public surface so the seeded Q1-2026 data doesn't go stale.

Fired by cron_heartbeat at 06:00 UTC daily. Each ISO has its own
ingest function. The framework is robust to scrape failures:
  - One ISO fails -> others still update
  - All scrape fails -> existing data preserved, ingested_at bumped to "we tried"
  - Real numbers found -> UPSERT into iso_queue_snapshots

Wiring (main.py):
    from routes.iso_queue_ingest import iso_queue_ingest_bp
    app.register_blueprint(iso_queue_ingest_bp)

Endpoints:
  POST /api/v1/iso-queue/ingest         -- run all ISO ingestors
  POST /api/v1/iso-queue/ingest/<iso>   -- run a single ISO (debugging)
  GET  /api/v1/iso-queue/ingest/status  -- last run summary

THE SCRAPING LOGIC for each ISO is currently SCAFFOLDING. The framework
hits the real source URL with a real UA so it counts as a heartbeat
even if the parser doesn't find new numbers. As ERCOT/PJM/MISO publish
machine-readable data (or as we add per-ISO parsers), each function
upgrades from scaffold to real ingestion.
"""
import os
import re
import json
import datetime
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request
import psycopg

iso_queue_ingest_bp = Blueprint("iso_queue_ingest", __name__,
                                 url_prefix="/api/v1/iso-queue")
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")

UA = "DCHub-IsoQueueIngest/1.0 (+https://dchub.cloud/interconnection-queues)"

# ════════════════════════════════════════════════════════════════════
# Per-ISO ingestors. Each returns (ok, parsed_dict_or_none, debug_msg).
# parsed_dict shape — only set fields that we successfully parsed:
#   {
#     "queued_load_total_gw":       410.0,
#     "queued_load_data_center_gw": 357.0,
#     "queued_load_dc_share_pct":   87.0,
#     "new_applications_q_gw":      198.0,
#     "new_applications_period":    "Q1 2026",
#     "top_subregions":             [{name, queued_gw, ttp_months, dcpi_verdict}, ...]
#   }
# Anything not parsed -> field omitted -> UPSERT preserves existing.
# ════════════════════════════════════════════════════════════════════

def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace"), resp.status


def ingest_ercot():
    """ERCOT large-load interconnection requests.
    Public landing: https://www.ercot.com/gridinfo/resource
    Future: ERCOT MIS API for structured GIS reports.
    """
    parsed = {}
    debug = []
    try:
        body, status = _fetch("https://www.ercot.com/gridinfo/resource")
        debug.append(f"http_{status} len={len(body)}")
        # Regex for "XXX GW" patterns; ERCOT publishes headline GW values
        # in the report-listing table. This is a best-effort extraction;
        # validate against current known minimum (>=380 GW for 2026).
        m = re.search(r"(\d{3,4}(?:\.\d)?)\s*GW", body)
        if m:
            n = float(m.group(1))
            if 380 <= n <= 800:
                parsed["queued_load_total_gw"] = n
                debug.append(f"matched_total_gw={n}")
        if not parsed:
            debug.append("no_headline_match")
        return True, parsed, "; ".join(debug)
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


def ingest_pjm():
    """PJM active queue summary.
    Public landing: https://www.pjm.com/planning/services-requests/interconnection-queues
    Future: parse the ProjectsActive.xls export.
    """
    parsed = {}
    debug = []
    try:
        body, status = _fetch("https://www.pjm.com/planning/services-requests/interconnection-queues")
        debug.append(f"http_{status} len={len(body)}")
        m = re.search(r"(\d{2,3}(?:\.\d)?)\s*GW.*?(?:data center|large load)", body, re.IGNORECASE)
        if m:
            parsed["queued_load_data_center_gw"] = float(m.group(1))
            debug.append(f"matched_dc_gw={m.group(1)}")
        if not parsed:
            debug.append("no_dc_match")
        return True, parsed, "; ".join(debug)
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


def ingest_miso():
    """MISO Generator Interconnection Queue (DPP reports)."""
    try:
        _body, status = _fetch("https://www.misoenergy.org/planning/resource-utilization/generator-interconnection-queue/")
        return True, {}, f"http_{status} (no parser yet, heartbeat only)"
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


def ingest_spp():
    try:
        _body, status = _fetch("https://www.spp.org/engineering/transmission-planning/generator-interconnection/")
        return True, {}, f"http_{status} (no parser yet, heartbeat only)"
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


def ingest_caiso():
    try:
        _body, status = _fetch("https://www.caiso.com/planning/generator-interconnection-process")
        return True, {}, f"http_{status} (no parser yet, heartbeat only)"
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


def ingest_nyiso():
    try:
        _body, status = _fetch("https://www.nyiso.com/connecting-to-the-grid")
        return True, {}, f"http_{status} (no parser yet, heartbeat only)"
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


def ingest_iso_ne():
    try:
        _body, status = _fetch("https://www.iso-ne.com/system-planning/interconnection-process")
        return True, {}, f"http_{status} (no parser yet, heartbeat only)"
    except Exception as e:
        return False, None, f"fetch_failed: {type(e).__name__}: {str(e)[:80]}"


INGESTORS = {
    "ERCOT":  ingest_ercot,
    "PJM":    ingest_pjm,
    "MISO":   ingest_miso,
    "SPP":    ingest_spp,
    "CAISO":  ingest_caiso,
    "NYISO":  ingest_nyiso,
    "ISO-NE": ingest_iso_ne,
}


def _upsert(iso, parsed, as_of, ingested_by):
    """UPSERT into iso_queue_snapshots. Only updates fields that parsed
    successfully — preserves existing values for unparsed columns."""
    if not NEON_URL:
        return {"ok": False, "error": "no_neon_url"}
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
    # Build dynamic INSERT/UPDATE. ON CONFLICT updates only the parsed fields
    # plus always-update ingested_at + ingested_by.
    placeholders = ", ".join(["%s"] * len(values))
    cols = ", ".join(fields)
    upd_cols = [f for f in fields if f not in ("iso", "as_of")]
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in upd_cols)
    upd += ", ingested_at = NOW()"
    sql = f"""
        INSERT INTO iso_queue_snapshots ({cols})
        VALUES ({placeholders})
        ON CONFLICT (iso, as_of) DO UPDATE SET {upd}
    """
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql, values)
            cur.execute("SELECT count(*) FROM iso_queue_snapshots WHERE iso=%s", (iso,))
            count = cur.fetchone()[0]
        return {"ok": True, "fields_updated": len(fields) - 3, "iso_row_count": count}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


@iso_queue_ingest_bp.route("/ingest", methods=["GET", "POST"])
def ingest_all():
    """Run every ISO ingestor sequentially. Idempotent. Logs per-ISO status."""
    started = datetime.datetime.now(datetime.timezone.utc)
    today = started.date()
    results = []
    for iso, fn in INGESTORS.items():
        ok, parsed, debug = fn()
        if ok and parsed is not None:
            up = _upsert(iso, parsed, today, "cron_v1")
            results.append({"iso": iso, "fetch_ok": True,
                            "parsed_fields": list(parsed.keys()),
                            "debug": debug, "upsert": up})
        elif ok:
            # Fetch succeeded but parser found nothing -- heartbeat only
            up = _upsert(iso, {}, today, "cron_v1_heartbeat")
            results.append({"iso": iso, "fetch_ok": True,
                            "parsed_fields": [],
                            "debug": debug, "upsert": up})
        else:
            results.append({"iso": iso, "fetch_ok": False, "debug": debug})
    elapsed_ms = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
    healthy = sum(1 for r in results if r.get("fetch_ok"))
    return jsonify({
        "started_at": started.isoformat(),
        "elapsed_ms": elapsed_ms,
        "as_of":      today.isoformat(),
        "isos_total": len(INGESTORS),
        "isos_fetched": healthy,
        "isos_failed":  len(INGESTORS) - healthy,
        "results": results,
        "note": "Per-ISO parsers are scaffolding. ERCOT+PJM attempt regex extraction; "
                "others do heartbeat fetches only (existing seed data preserved via UPSERT).",
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
                 "cron_v1" if (ok and parsed) else "cron_v1_heartbeat")
    return jsonify({"iso": iso, "fetch_ok": ok,
                    "parsed_fields": list((parsed or {}).keys()),
                    "debug": debug, "upsert": up})


@iso_queue_ingest_bp.route("/ingest/status", methods=["GET"])
def status():
    """Last-run summary per ISO. Useful for monitoring."""
    if not NEON_URL:
        return jsonify({"error": "no_neon_url"}), 500
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("""
              SELECT iso, MAX(ingested_at) AS last_run, MAX(ingested_by) AS last_method,
                     MAX(queued_load_total_gw) AS latest_total_gw
              FROM iso_queue_snapshots
              GROUP BY iso ORDER BY MAX(ingested_at) DESC NULLS LAST
            """)
            rows = cur.fetchall()
        return jsonify({
            "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "isos": [
                {"iso": r[0], "last_run": r[1].isoformat() if r[1] else None,
                 "last_method": r[2], "latest_total_gw": float(r[3] or 0)}
                for r in rows
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
