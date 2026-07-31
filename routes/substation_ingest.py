"""HIFLD Electric Substations ingest — the refresh path that did not exist.

WHY THIS MODULE EXISTS
──────────────────────
The `substations` table's HIFLD slice is a ONE-SHOT SNAPSHOT. Measured
2026-07-30: all 79,686 rows carry created_at = 2026-03-17 and have never been
re-pulled. (MAX(updated_at) reads 2026-07-15, which is a single unrelated touch —
35,858 rows moved that day; created_at is what shows the real load, and it is a
single date.) The table looks maintained and is not.

It could not be refreshed, because EVERY code path to do so was dead — measured,
not assumed:

    land_power_crawler.crawl_substations
        HIFLD_SUBSTATIONS_URL (opendata.arcgis.com download API)
        -> HTTP 500 {"errors":{"message":"Item does not exist or is inaccessible."}}
    hifld_substation_loader.py — BOTH of its endpoints
        opendata.arcgis.com/.../f48d61b8...      -> HTTP 500
        services1.arcgis.com/Hp6G80Pky0om7QvQ/Electric_Substations_1
                                                 -> {"error":{"code":400,"message":"Invalid URL"}}

The opendata.arcgis.com *download* API is gone generally; the data moved to
FeatureServer *query* endpoints. That is the same shape routes/transmission_ingest.py
already uses successfully for the sibling layer, so this module mirrors it rather
than inventing a pattern.

THE LIVE SOURCE (verified 2026-07-30)
─────────────────────────────────────
    services5.arcgis.com/HDRa0B57OVrv2E1q/.../Electric_Substations/FeatureServer/0
    count = 75,328   extent -165.43 .. 145.71 lon (CONUS + AK + HI + Pacific)
    maxRecordCount = 2000, exceededTransferLimit honoured -> paginate

Its field list is the HIFLD Electric Substations schema exactly — ID, NAME, CITY,
STATE, ZIP, TYPE, STATUS, COUNTY, COUNTYFIPS, COUNTRY, LATITUDE, LONGITUDE,
NAICS_CODE, NAICS_DESC, SOURCE, SOURCEDATE, VAL_METHOD, VAL_DATE, LINES,
MAX_VOLT, MIN_VOLT — and that is 1:1 with the live table's own column set
(zip, county_fips, naics_*, source_date, val_method, val_date, min_volt,
max_volt). That correspondence is the evidence this is the same layer the March
load came from, not a lookalike.

★★★ THE TABLE'S HIFLD KEY IS A POSITIONAL ARTIFACT, NOT AN IDENTIFIER.
Found by running this ingest for real: the write failed on
`substations_name_lat_lng_uniq`, and the blocking row was

    id=2599  hifld_objectid=1  name='UNKNOWN107655'  (45.768425, -91.864746)

hifld_objectid = 1 for the substation whose real HIFLD ID is 107655. Measured:
held hifld_objectid runs 1..79,687 over 79,686 rows, and the first five rows in
that order are exactly the first five the FeatureServer returns. So the March
load stored the ArcGIS **OBJECTID** — the row number of that particular export —
where the stable HIFLD **ID** belongs. It re-identifies nothing: re-export in a
different order and OBJECTID 1 is a different substation.

That is why ON CONFLICT (hifld_objectid) matched nothing (107655 != 1), tried a
fresh INSERT, and collided on (name, lat, lng) instead.

★ CONFLICT TARGET IS (name, lat, lng) — the constraint that actually identifies a
substation in this table today: 79,686 distinct triples over 79,686 HIFLD rows,
zero duplicate groups. And `hifld_objectid` is now in the UPDATE SET, so every
matched row is CORRECTED to its real HIFLD ID as the ingest passes over it.
Safe because the two ranges do not overlap — upstream IDs are 107,655..311,000,
held positional values 1..79,687 — so writing a real ID can never collide with a
positional one. Once a full run completes, hifld_objectid becomes a stable key.

★ ON CONFLICT ONLY HANDLES THE CONSTRAINT YOU NAME. This table carries three
unique indexes on (name, lat, lng) plus three on hifld_objectid. A violation of
any OTHER unique constraint still raises — which is exactly how the positional
key was discovered, and is the reason the first live run failed loudly rather
than writing something wrong.

★★ UPSERT, NEVER FULL-REPLACE — AND THAT IS NOT A STYLE CHOICE.
transmission_ingest does a full replace, which is right for THAT layer. Doing it
here would destroy data, measured two ways:

  1. THIS LAYER HAS NO OWNER/OPERATOR FIELD. The live table has `operator`
     populated on 28,319 HIFLD rows from elsewhere. A full replace would null
     every one of them. So `operator` is explicitly NOT in the UPDATE SET below —
     upstream cannot speak to it, so upstream must not overwrite it.
  2. Upstream now has 75,328 rows against 79,686 held. A delete-then-insert would
     drop the published substation total 126,842 -> 122,484, and "126k
     substations" is a published headline figure. Rows upstream no longer lists
     are REPORTED as `upstream_missing`, not deleted: an upserting feed is a
     SNAPSHOT, not a floor, and pruning against one snapshot is how a real
     asset silently disappears. Deleting is a separate, deliberate decision.

★ GAPS THIS ALSO FILLS. On the 79,686 held rows, `type`, `sub_type` and
`max_volt` are populated on ZERO. The March loader never mapped them. They are
mapped here, so this is a genuine enrichment and not only a currency refresh.

★ NOT SCHEDULED, BY DESIGN. Admin-triggered only. It changes a published
headline count, so a human decides when it runs. Registering a cron for it is a
separate decision with a separate blast radius.

★ EGRESS. routes/transmission_ingest.py records that Railway's egress to ArcGIS
is unreliable, and its runner POSTs rows instead. The same body protocol is
supported here for the same reason; the server-side fetch is the fallback, not
the assumption.

    POST /api/v1/admin/ingest/substations?dry_run=1     # fetch + report, no write
    POST /api/v1/admin/ingest/substations               # upsert
    POST /api/v1/admin/ingest/substations   {"rows":[[...]]}   # runner-provided
"""
import json
import logging
import os

import psycopg2
import requests
from flask import Blueprint, jsonify, request

log = logging.getLogger("substation_ingest")
substation_ingest_bp = Blueprint("substation_ingest", __name__)

_SRC = "HIFLD"

# Verified live 2026-07-30: 75,328 features, national extent, HIFLD schema.
_SVC = ("https://services5.arcgis.com/HDRa0B57OVrv2E1q/ArcGIS/rest/services/"
        "Electric_Substations/FeatureServer/0/query")

_OUT_FIELDS = ("ID,NAME,CITY,STATE,ZIP,TYPE,STATUS,COUNTY,COUNTYFIPS,"
               "LATITUDE,LONGITUDE,NAICS_CODE,NAICS_DESC,SOURCE,SOURCEDATE,"
               "VAL_METHOD,VAL_DATE,LINES,MAX_VOLT,MIN_VOLT")

# Row tuple shape. Keep in step with _UPSERT_SQL's column list.
_ROW_FIELDS = ("hifld_objectid", "name", "city", "state", "zip", "type",
               "sub_type", "status", "county", "county_fips", "lat", "lng",
               "naics_code", "naics_desc", "source_date", "val_method",
               "val_date", "lines", "lines_count", "max_volt", "min_volt",
               "voltage_kv", "max_voltage_kv", "min_voltage_kv")

_PAGE = 2000          # the service's own maxRecordCount
_DEFAULT_CAP = 120000


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(expected) and provided == expected


def _volt(v):
    """HIFLD uses -999999 as 'not available'. Measured on live rows, e.g.
    TAP161924 carries MAX_VOLT = MIN_VOLT = -999999. Storing that verbatim would
    publish a negative kV; storing 0 would publish a measured zero. Both are
    wrong in the same direction — return None, which is 'unknown'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _clean(s, n):
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.upper() in ("NOT AVAILABLE", "UNKNOWN", "NULL", "NONE", "N/A"):
        return None
    return s[:n]


def _num(v):
    """LATITUDE/LONGITUDE are esriFieldTypeDouble — coerce numerically, not via
    the string cleaner (which would stringify a float and mis-handle 0.0)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if -180.0 <= f <= 180.0 else None


def _epoch_ms_to_date(v):
    """SOURCEDATE / VAL_DATE arrive as epoch MILLISECONDS (e.g. 1473984000000).
    Passing that to a DATE column would either error or land in the year 48000."""
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _row_from_attrs(a):
    hid = _clean(a.get("ID"), 50)
    if not hid:
        return None          # no conflict key -> cannot upsert; count it, drop it
    max_v = _volt(a.get("MAX_VOLT"))
    min_v = _volt(a.get("MIN_VOLT"))
    typ = _clean(a.get("TYPE"), 100)
    lines = a.get("LINES")
    try:
        lines = int(float(lines)) if lines is not None else None
    except (TypeError, ValueError):
        lines = None
    return (
        hid,
        _clean(a.get("NAME"), 500),
        _clean(a.get("CITY"), 200),
        _clean(a.get("STATE"), 10),
        _clean(a.get("ZIP"), 20),
        typ,
        typ,                                   # sub_type — 0 populated today
        _clean(a.get("STATUS"), 50),
        _clean(a.get("COUNTY"), 200),
        _clean(a.get("COUNTYFIPS"), 20),
        _num(a.get("LATITUDE")),
        _num(a.get("LONGITUDE")),
        _clean(a.get("NAICS_CODE"), 20),
        _clean(a.get("NAICS_DESC"), 200),
        _epoch_ms_to_date(a.get("SOURCEDATE")),
        _clean(a.get("VAL_METHOD"), 100),
        _epoch_ms_to_date(a.get("VAL_DATE")),
        lines,
        lines,                                 # lines_count
        max_v,                                 # max_volt — 0 populated today
        min_v,
        max_v,                                 # voltage_kv: the app-facing column
        max_v,                                 # max_voltage_kv
        min_v,
    )


def _fetch(cap: int):
    """Paginate the service -> (rows, fetched, dropped_no_id).

    returnGeometry=false: LATITUDE/LONGITUDE are attributes on this layer, so the
    geometry payload would be dead weight at ~38 pages.
    """
    rows, offset, fetched, dropped = [], 0, 0, 0
    while len(rows) < cap:
        params = {
            "where": "1=1",
            "outFields": _OUT_FIELDS,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": _PAGE,
            "f": "json",
        }
        # requests, not urllib — scripts/regression_lint.py enforces this
        # (`urllib-request-on-railway`). urllib's default User-Agent gets
        # bot-filtered at the edge, and its timeout does not cover the read.
        resp = requests.get(
            _SVC, params=params, timeout=55,
            headers={"User-Agent": "DCHub-GridData/1.0 (+https://dchub.cloud)"})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"service error: {str(data['error'])[:160]}")
        feats = data.get("features") or []
        if not feats:
            break
        fetched += len(feats)
        for f in feats:
            row = _row_from_attrs(f.get("attributes") or {})
            if row is None:
                dropped += 1
            else:
                rows.append(row)
        offset += _PAGE
        if len(feats) < _PAGE:
            break
    return rows[:cap], fetched, dropped


def _coerce_body_row(r):
    if not isinstance(r, (list, tuple)) or not r:
        return None
    g = lambda i: r[i] if len(r) > i else None
    if not g(0):
        return None
    return tuple(g(i) for i in range(len(_ROW_FIELDS)))


# `operator` is deliberately absent from the UPDATE SET: this upstream carries no
# OWNER field, and 28,319 held rows have `operator` populated from elsewhere.
# Adding it here would null them. Same for `source_id` and `capacity_mva`.
_UPSERT_SQL = """
    INSERT INTO substations
      (hifld_objectid, name, city, state, zip, type, sub_type, status, county,
       county_fips, lat, lng, naics_code, naics_desc, source_date, val_method,
       val_date, lines, lines_count, max_volt, min_volt, voltage_kv,
       max_voltage_kv, min_voltage_kv, source, updated_at)
    VALUES %s
    ON CONFLICT (name, lat, lng) DO UPDATE SET
      hifld_objectid = EXCLUDED.hifld_objectid,
      city = EXCLUDED.city,
      state = EXCLUDED.state,
      zip = EXCLUDED.zip,
      type = EXCLUDED.type,
      sub_type = EXCLUDED.sub_type,
      status = EXCLUDED.status,
      county = EXCLUDED.county,
      county_fips = EXCLUDED.county_fips,
      naics_code = EXCLUDED.naics_code,
      naics_desc = EXCLUDED.naics_desc,
      source_date = EXCLUDED.source_date,
      val_method = EXCLUDED.val_method,
      val_date = EXCLUDED.val_date,
      lines = EXCLUDED.lines,
      lines_count = EXCLUDED.lines_count,
      max_volt = EXCLUDED.max_volt,
      min_volt = EXCLUDED.min_volt,
      voltage_kv = EXCLUDED.voltage_kv,
      max_voltage_kv = EXCLUDED.max_voltage_kv,
      min_voltage_kv = EXCLUDED.min_voltage_kv,
      updated_at = NOW()
"""


@substation_ingest_bp.route("/api/v1/admin/ingest/substations", methods=["POST"])
def ingest_substations():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"
    try:
        cap = min(int(request.args.get("cap", _DEFAULT_CAP)), 200000)
    except (TypeError, ValueError):
        cap = _DEFAULT_CAP

    body_rows = None
    raw = request.get_data() or b""
    if raw:
        try:
            enc = (request.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or request.headers.get("X-Content-Gzip"):
                import gzip as _gz
                raw = _gz.decompress(raw)
            j = json.loads(raw)
            if isinstance(j, dict) and isinstance(j.get("rows"), list):
                body_rows = [x for x in (_coerce_body_row(r) for r in j["rows"])
                             if x is not None]
        except Exception:
            body_rows = None

    fetched = dropped = 0
    if body_rows is not None:
        rows = body_rows[:cap]
        fetched = len(body_rows)
    else:
        try:
            rows, fetched, dropped = _fetch(cap)
        except Exception as e:
            return jsonify(ok=False,
                           error=f"source fetch failed: {str(e)[:200]}",
                           service=_SVC), 502

    out = {"ok": True, "fetched": fetched, "usable": len(rows),
           "dropped_no_id": dropped, "service": _SVC}

    if dry:
        out["dry_run"] = True
        out["sample"] = [list(r) for r in rows[:3]]
        return jsonify(out)

    # Refuse an empty write. A feed that returns nothing is a broken feed, and
    # upserting zero rows would silently report success.
    if not rows:
        return jsonify(ok=False, fetched=fetched,
                       error="no usable rows — refused to report a no-op as a "
                             "successful ingest"), 400

    try:
        from psycopg2.extras import execute_values
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM substations WHERE source = %s",
                            (_SRC,))
                before = cur.fetchone()[0]
                # template supplies NOW() for updated_at, so the row tuples
                # carry exactly _ROW_FIELDS + source.
                execute_values(
                    cur,
                    _UPSERT_SQL,
                    [r + (_SRC,) for r in rows],
                    template="(" + ",".join(["%s"] * (len(_ROW_FIELDS) + 1)) + ", NOW())",
                    page_size=500,
                )
                cur.execute("SELECT COUNT(*) FROM substations WHERE source = %s",
                            (_SRC,))
                after = cur.fetchone()[0]
                # Held rows this snapshot did NOT list. Reported, never deleted —
                # an upserting feed is a snapshot, not a floor.
                cur.execute(
                    "SELECT COUNT(*) FROM substations "
                    " WHERE source = %s AND updated_at < NOW() - INTERVAL '1 hour'",
                    (_SRC,))
                stale = cur.fetchone()[0]
            c.commit()
    except Exception as e:
        log.error("substation ingest failed: %s", e)
        return jsonify(ok=False, fetched=fetched, usable=len(rows),
                       error=f"write failed: {str(e)[:200]}"), 500

    out.update({
        "rows_before": before,
        "rows_after": after,
        "inserted": max(0, after - before),
        "updated": len(rows) - max(0, after - before),
        "upstream_missing": stale,
        "note": ("upstream_missing rows are held, NOT deleted — this feed is a "
                 "snapshot, not a floor. `operator` is never overwritten: this "
                 "upstream carries no OWNER field and 28,319 held rows have it "
                 "populated from elsewhere."),
    })
    return jsonify(out)
