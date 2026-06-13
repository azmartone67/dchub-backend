"""FCC BDC provider fiber footprints (owner request 2026-06-13).

A customer compared the land+power map to FiberLocator near Williston ND and
our regional fiber lost. Carrier route geometry is licensed (CCMI/Rextag),
but every ISP FILES its fiber footprint with the FCC — the Broadband Data
Collection availability data is public (free API token) and rows carry
`h3_res8_id`, so we can render provider-filterable hex footprints (Midco
included) without licensed data.

Chain: admin loader (state FTTP CSV → aggregate brand × h3 hex → Neon)
  → GET /api/v1/fiber/providers   (distinct brands per state, for the UI)
  → GET /api/v1/fiber/footprint   (hex ids for one brand — client renders
                                   polygons with h3-js)

Auth to FCC: env FCC_BDC_USERNAME + FCC_BDC_TOKEN (whitespace-sanitized —
the %0a-in-env class has bitten before). Set on Railway AND Render.

NOTE on caching: /api/v1/fiber/* is covered by the owner's "Bypass cache"
zone Cache Rule (2026-06-13), so these endpoints are never edge-cached.
"""
import csv
import io
import json
import os
import time
import zipfile
import urllib.request

from flask import Blueprint, request, jsonify

fcc_bdc_fiber_bp = Blueprint("fcc_bdc_fiber", __name__)

_FCC_BASE = "https://broadbandmap.fcc.gov/api/public/map"
_DEFAULT_AS_OF = "2025-12-31"


def _creds():
    u = "".join((os.environ.get("FCC_BDC_USERNAME") or "").split())
    t = "".join((os.environ.get("FCC_BDC_TOKEN") or "").split())
    return u, t


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY", "")
                or os.environ.get("DCHUB_INTERNAL_KEY", ""))
    got = (request.headers.get("X-Admin-Key", "")
           or request.args.get("admin_key", ""))
    return bool(expected) and got == expected


def _fcc_get(path, timeout=120):
    u, t = _creds()
    req = urllib.request.Request(_FCC_BASE + path,
        headers={"username": u, "hash_value": t,
                 "User-Agent": "dchub-bdc-fiber/1.0 (+https://dchub.cloud)"})
    return urllib.request.urlopen(req, timeout=timeout)


def _ensure_schema() -> None:
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fcc_fiber_hex (
                    state_fips TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    brand_name TEXT NOT NULL,
                    provider_id TEXT,
                    h3_res8 TEXT NOT NULL,
                    locations INT DEFAULT 0,
                    max_down INT,
                    loaded_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (state_fips, as_of, brand_name, h3_res8)
                )""")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS fcc_fiber_hex_brand_idx
                    ON fcc_fiber_hex (state_fips, brand_name)""")
            c.commit()
    except Exception as e:
        print(f"[fcc-fiber] schema ensure failed: {e}", flush=True)


@fcc_bdc_fiber_bp.route("/api/v1/admin/fcc-fiber/load", methods=["POST"])
def load_state():
    """Download + aggregate one state's FTTP availability into Neon.

    Params: state_fips (e.g. 38), as_of (default 2025-12-31),
            file_id (optional — skip the listing lookup).
    Synchronous and heavy (ND ≈ a few hundred k rows) — admin/cron only.
    """
    if not _admin_ok():
        return jsonify({"error": "admin auth required"}), 403
    _ensure_schema()
    state_fips = (request.args.get("state_fips") or "").strip()
    as_of = (request.args.get("as_of") or _DEFAULT_AS_OF).strip()
    file_id = (request.args.get("file_id") or "").strip()
    if not state_fips:
        return jsonify({"error": "state_fips required"}), 400
    t0 = time.time()
    try:
        if not file_id:
            with _fcc_get(f"/downloads/listAvailabilityData/{as_of}?category=State") as r:
                files = json.loads(r.read()).get("data", [])
            match = [f for f in files
                     if str(f.get("state_fips")) == state_fips
                     and str(f.get("technology_code")) == "50"
                     and f.get("file_type") == "csv"]
            if not match:
                return jsonify({"error": f"no FTTP csv for state {state_fips} @ {as_of}"}), 404
            file_id = str(match[0]["file_id"])

        # Download the zip fully (ND ~ tens of MB), parse the CSV inside.
        with _fcc_get(f"/downloads/downloadFile/availability/{file_id}", timeout=600) as r:
            blob = r.read()
        agg = {}   # (brand, h3) -> [locations, max_down, provider_id]
        rows = 0
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next(n for n in z.namelist() if n.endswith(".csv"))
            with z.open(name) as fh:
                rdr = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
                for row in rdr:
                    rows += 1
                    brand = (row.get("brand_name") or row.get("provider_name") or "").strip()
                    h3 = (row.get("h3_res8_id") or "").strip()
                    if not brand or not h3:
                        continue
                    try:
                        down = int(float(row.get("max_advertised_download_speed") or 0))
                    except Exception:
                        down = 0
                    key = (brand, h3)
                    ent = agg.get(key)
                    if ent:
                        ent[0] += 1
                        if down > ent[1]:
                            ent[1] = down
                    else:
                        agg[key] = [1, down, (row.get("provider_id") or "")[:24]]

        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM fcc_fiber_hex WHERE state_fips=%s AND as_of=%s",
                        (state_fips, as_of))
            args = []
            for (brand, h3), (n, down, pid) in agg.items():
                args.append((state_fips, as_of, brand[:120], pid, h3, n, down))
            cur.executemany("""
                INSERT INTO fcc_fiber_hex
                    (state_fips, as_of, brand_name, provider_id, h3_res8, locations, max_down)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (state_fips, as_of, brand_name, h3_res8) DO UPDATE
                   SET locations = EXCLUDED.locations, max_down = EXCLUDED.max_down""",
                args)
            c.commit()
        brands = len({b for (b, _h) in agg.keys()})
        # autonomous-intelligence ledger (in-process, never raises)
        try:
            from routes.extractor_brain import record_extraction
            record_extraction("fcc-bdc-fiber", "success", rows_inserted=len(agg),
                              duration_ms=int((time.time() - t0) * 1000),
                              observations={"state": state_fips, "csv_rows": rows,
                                            "brands": brands})
        except Exception:
            pass
        return jsonify({"ok": True, "state_fips": state_fips, "as_of": as_of,
                        "csv_rows": rows, "hex_rows": len(agg), "brands": brands,
                        "seconds": round(time.time() - t0, 1)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 502


@fcc_bdc_fiber_bp.route("/api/v1/fiber/providers", methods=["GET"])
def list_providers():
    """Distinct fiber brands for a state (for the map's provider picker)."""
    _ensure_schema()
    state = (request.args.get("state") or request.args.get("state_fips") or "38").strip()
    out = []
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            cur.execute("""
                SELECT brand_name, SUM(locations) AS locs, COUNT(*) AS hexes
                  FROM fcc_fiber_hex WHERE state_fips=%s
                 GROUP BY brand_name ORDER BY locs DESC LIMIT 100""", (state,))
            for r in cur.fetchall():
                out.append({"brand": r[0], "locations": int(r[1] or 0), "hexes": int(r[2] or 0)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 503
    resp = jsonify({"ok": True, "state_fips": state, "providers": out,
                    "source": "FCC Broadband Data Collection (fiber-to-the-premises filings)"})
    return resp


@fcc_bdc_fiber_bp.route("/api/v1/fiber/footprint", methods=["GET"])
def footprint():
    """Hex footprint for one brand (ILIKE match). Client renders with h3-js."""
    _ensure_schema()
    state = (request.args.get("state") or "38").strip()
    brand = (request.args.get("brand") or "").strip()
    if not brand:
        return jsonify({"ok": False, "error": "brand required"}), 400
    cap = min(int(request.args.get("cap") or 25000), 60000)
    hexes, brands = [], set()
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            cur.execute("""
                SELECT h3_res8, locations, max_down, brand_name
                  FROM fcc_fiber_hex
                 WHERE state_fips=%s AND brand_name ILIKE %s
                 ORDER BY locations DESC LIMIT %s""",
                (state, "%" + brand + "%", cap))
            for r in cur.fetchall():
                hexes.append([r[0], int(r[1] or 0), int(r[2] or 0)])
                brands.add(r[3])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 503
    return jsonify({"ok": True, "state_fips": state, "brand_query": brand,
                    "brands_matched": sorted(brands), "count": len(hexes),
                    "hexes": hexes,
                    "hex_format": "[h3_res8_id, locations, max_down_mbps]",
                    "source": "FCC BDC fiber-to-the-premises filings"})
