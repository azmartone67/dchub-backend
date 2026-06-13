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
import tempfile
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


_SCHEMA_DONE = False


def _raw(cur):
    """Unwrap db_utils' PGCursorWrapper.

    Two wrapper behaviors break this module: execute() silently SKIPS
    DDL when SKIP_DDL=1 (the env default — CREATE TABLE would no-op),
    and every INSERT is followed by a SELECT lastval() probe that
    ERRORS on tables with no serial column, aborting the transaction
    mid-bulk-load. The raw psycopg2 cursor has neither problem and our
    SQL is already PG-native.
    """
    return getattr(cur, "_cur", cur)


def _try(cur, sql) -> None:
    """Run one DDL statement; never let a failure poison the rest."""
    try:
        cur.execute(sql)
        cur.connection.commit()
    except Exception as e:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        print(f"[fcc-fiber] ddl skipped ({str(e)[:80]}): {sql[:60]}", flush=True)


def _ensure_schema() -> None:
    global _SCHEMA_DONE
    if _SCHEMA_DONE:
        return
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = _raw(c.cursor())
            _try(cur, """
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
            _try(cur, """
                CREATE INDEX IF NOT EXISTS fcc_fiber_hex_brand_idx
                    ON fcc_fiber_hex (state_fips, brand_name)""")
            # Viewport queries: hex centers via Neon's h3 extension
            # (verified available 2026-06-13), gist point index for
            # bbox, pg_trgm gin for brand ILIKE at national scale.
            _try(cur, "CREATE EXTENSION IF NOT EXISTS h3")
            _try(cur, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
            _try(cur, "ALTER TABLE fcc_fiber_hex ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION")
            _try(cur, "ALTER TABLE fcc_fiber_hex ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION")
            _try(cur, """
                CREATE INDEX IF NOT EXISTS fcc_fiber_hex_pt_gist
                    ON fcc_fiber_hex USING gist ((point(lng, lat)))""")
            _try(cur, """
                CREATE INDEX IF NOT EXISTS fcc_fiber_hex_brand_trgm
                    ON fcc_fiber_hex USING gin (brand_name gin_trgm_ops)""")
        _SCHEMA_DONE = True
    except Exception as e:
        print(f"[fcc-fiber] schema ensure failed: {e}", flush=True)


def _fill_latlng(cur, state_fips: str) -> int:
    """Set hex-center lat/lng on rows that lack it (pure SQL via h3 ext)."""
    cur.execute("""
        UPDATE fcc_fiber_hex
           SET lat = (h3_cell_to_lat_lng(h3_res8::h3index))[1],
               lng = (h3_cell_to_lat_lng(h3_res8::h3index))[0]
         WHERE state_fips=%s AND lat IS NULL""", (state_fips,))
    return cur.rowcount


def _flush(cur, state_fips, as_of, agg) -> None:
    """Chunked multi-row upsert of (brand, hex) aggregates.

    ACCUMULATE semantics (+= / GREATEST), not overwrite: a key can span
    flush boundaries within one load. Safe because the loader DELETEs
    the state's rows first. db_utils executemany is a per-row loop —
    never use it here (50k+ rows would take minutes).
    """
    if not agg:
        return
    args = []
    for (brand, h3), (n, down, pid) in agg.items():
        args.append((state_fips, as_of, brand[:120], pid, h3, n, down))
    CHUNK = 1000
    base = """
        INSERT INTO fcc_fiber_hex
            (state_fips, as_of, brand_name, provider_id, h3_res8, locations, max_down)
        VALUES {}
        ON CONFLICT (state_fips, as_of, brand_name, h3_res8) DO UPDATE
           SET locations = fcc_fiber_hex.locations + EXCLUDED.locations,
               max_down = GREATEST(COALESCE(fcc_fiber_hex.max_down, 0),
                                   COALESCE(EXCLUDED.max_down, 0))"""
    for i in range(0, len(args), CHUNK):
        chunk = args[i:i + CHUNK]
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(chunk))
        flat = [v for row in chunk for v in row]
        cur.execute(base.format(ph), flat)


def _bbox_param():
    """Parse ?bbox=w,s,e,n → normalized float tuple, or None."""
    raw = (request.args.get("bbox") or "").strip()
    if not raw:
        return None
    try:
        w, s, e, n = [float(x) for x in raw.split(",")[:4]]
    except Exception:
        return None
    if w > e:
        w, e = e, w
    if s > n:
        s, n = n, s
    if not (-180.0 <= w <= 180.0 and -90.0 <= s <= 90.0):
        return None
    return (w, s, e, n)


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

        # Stream the zip to a temp file (CA-class states are hundreds of
        # MB — never hold the blob in RAM on the web worker), then parse,
        # flushing aggregates to Neon every ~250k keys so the dict stays
        # bounded. One transaction per state: DELETE + flushes + latlng
        # fill commit together (atomic re-load).
        tmp = tempfile.NamedTemporaryFile(prefix="fcc_bdc_", suffix=".zip", delete=False)
        try:
            # FCC's CDN drops long streams sporadically ("SSL connection
            # has been closed unexpectedly") — TX failed twice on this.
            # Retry the whole download up to 3× on a fresh connection.
            last_err = None
            for attempt in range(3):
                try:
                    tmp.seek(0)
                    tmp.truncate()
                    with _fcc_get(f"/downloads/downloadFile/availability/{file_id}", timeout=900) as r:
                        while True:
                            piece = r.read(1 << 20)
                            if not piece:
                                break
                            tmp.write(piece)
                    last_err = None
                    break
                except Exception as de:
                    last_err = de
                    print(f"[fcc-fiber] download attempt {attempt + 1} failed: {str(de)[:120]}", flush=True)
                    time.sleep(4 * (attempt + 1))
            if last_err is not None:
                raise last_err
            tmp.close()

            from db_utils import safe_db
            agg = {}   # (brand, h3) -> [locations, max_down, provider_id]
            rows = 0
            with safe_db() as c:
                cur = _raw(c.cursor())
                # Fiber-dense states (MN tripped this) push the set-based
                # lat/lng UPDATE past the connection's statement_timeout.
                # LOCAL = this transaction only.
                cur.execute("SET LOCAL statement_timeout = '540s'")
                cur.execute("DELETE FROM fcc_fiber_hex WHERE state_fips=%s AND as_of=%s",
                            (state_fips, as_of))
                with zipfile.ZipFile(tmp.name) as z:
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
                            if len(agg) >= 250_000:
                                _flush(cur, state_fips, as_of, agg)
                                agg.clear()
                _flush(cur, state_fips, as_of, agg)
                agg.clear()
                filled = _fill_latlng(cur, state_fips)
                cur.execute("""SELECT COUNT(*), COUNT(DISTINCT brand_name)
                                 FROM fcc_fiber_hex
                                WHERE state_fips=%s AND as_of=%s""", (state_fips, as_of))
                hex_total, brands = cur.fetchone()
                c.commit()
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        # autonomous-intelligence ledger (in-process, never raises)
        try:
            from routes.extractor_brain import record_extraction
            record_extraction("fcc-bdc-fiber", "success", rows_inserted=int(hex_total or 0),
                              duration_ms=int((time.time() - t0) * 1000),
                              observations={"state": state_fips, "csv_rows": rows,
                                            "brands": int(brands or 0)})
        except Exception:
            pass
        return jsonify({"ok": True, "state_fips": state_fips, "as_of": as_of,
                        "csv_rows": rows, "hex_rows": int(hex_total or 0),
                        "brands": int(brands or 0), "latlng_filled": filled,
                        "seconds": round(time.time() - t0, 1)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 502


@fcc_bdc_fiber_bp.route("/api/v1/fiber/providers", methods=["GET"])
def list_providers():
    """Fiber brands for a state OR a ?bbox=w,s,e,n viewport (map picker)."""
    _ensure_schema()
    bbox = _bbox_param()
    state = (request.args.get("state") or request.args.get("state_fips") or "38").strip()
    out = []
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            if bbox:
                w, s, e, n = bbox
                cur.execute("""
                    SELECT brand_name, SUM(locations) AS locs, COUNT(*) AS hexes
                      FROM fcc_fiber_hex
                     WHERE lat IS NOT NULL
                       AND point(lng, lat) <@ box(point(%s,%s), point(%s,%s))
                     GROUP BY brand_name ORDER BY locs DESC LIMIT 100""",
                    (w, s, e, n))
            else:
                cur.execute("""
                    SELECT brand_name, SUM(locations) AS locs, COUNT(*) AS hexes
                      FROM fcc_fiber_hex WHERE state_fips=%s
                     GROUP BY brand_name ORDER BY locs DESC LIMIT 100""", (state,))
            for r in cur.fetchall():
                out.append({"brand": r[0], "locations": int(r[1] or 0), "hexes": int(r[2] or 0)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 503
    resp = jsonify({"ok": True, "providers": out,
                    **({"bbox": list(bbox)} if bbox else {"state_fips": state}),
                    "source": "FCC Broadband Data Collection (fiber-to-the-premises filings)"})
    return resp


@fcc_bdc_fiber_bp.route("/api/v1/fiber/footprint", methods=["GET"])
def footprint():
    """Hex footprint for one brand (ILIKE), by ?bbox=w,s,e,n viewport or
    by state (legacy default 38). Client renders with h3-js."""
    _ensure_schema()
    bbox = _bbox_param()
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
            if bbox:
                w, s, e, n = bbox
                cur.execute("""
                    SELECT h3_res8, locations, max_down, brand_name
                      FROM fcc_fiber_hex
                     WHERE brand_name ILIKE %s AND lat IS NOT NULL
                       AND point(lng, lat) <@ box(point(%s,%s), point(%s,%s))
                     ORDER BY locations DESC LIMIT %s""",
                    ("%" + brand + "%", w, s, e, n, cap))
            else:
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
    return jsonify({"ok": True, "brand_query": brand,
                    **({"bbox": list(bbox)} if bbox else {"state_fips": state}),
                    "brands_matched": sorted(brands), "count": len(hexes),
                    "capped": len(hexes) >= cap, "hexes": hexes,
                    "hex_format": "[h3_res8_id, locations, max_down_mbps]",
                    "source": "FCC BDC fiber-to-the-premises filings"})
