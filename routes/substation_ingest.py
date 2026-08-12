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

★★★ WRITES ARE DISABLED (2026-07-31) — see the block in ingest_substations().
A canary proved this layer carries UNKNOWN<id> placeholders where the held data
has validated names, so upserting it duplicates assets with worse names. The
fetch, mapping and dry_run remain live and verified.

    POST /api/v1/admin/ingest/substations?dry_run=1     # fetch + report (works)
    POST /api/v1/admin/ingest/substations               # 409, refuses to write
    POST /api/v1/admin/ingest/substations   {"rows":[[...]]}   # runner-provided
"""
import json
import logging
import os
import re

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
#
# ★ 2026-08-12 (SH52-056): `hifld_id` is FIRST and `hifld_objectid` is gone from
# this tuple. The upstream ID now goes to the column that means "upstream asset
# id" instead of the one that means "row number of the March export". See
# migrations/2026-08-12_substation_hifld_id_identity.sql for why the two must
# not share a column: hifld_objectid already holds three id-spaces at once
# (78,356 positional / 1,330 real IDs / 47,190 NULL) and writing more real IDs
# into it makes every value permanently ambiguous.
_ROW_FIELDS = ("hifld_id", "name", "city", "state", "zip", "type",
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


_PLACEHOLDER_NAME = re.compile(r"^UNKNOWN\d+$", re.I)


def _clean_name(s):
    """NAME, with upstream's `UNKNOWN<id>` placeholders treated as absent.

    ★★★ SH52-056 — THIS IS THE FUNCTION THAT MAKES THE REFRESH SAFE, and its
    absence is what made the 2026-07-31 canary destructive. _clean() rejects the
    bare string 'UNKNOWN' but passes 'UNKNOWN107655' straight through, so the
    canary compared a held validated name against a placeholder, failed to match
    on (name, lat, lng), and INSERTED a duplicate under the worse name:

        held 'HOLCOMBE'                    <-> upstream 'UNKNOWN107657'
        held 'South Bainbridge Substation' <-> upstream 'UNKNOWN107666'
        held 'CRIST'                       <-> upstream 'UNKNOWN107698'

    (identical lat/lng to the last float4 digit in every case). 668 of the 670
    rows it inserted were exact coordinate twins of a held row.

    This is not a rare shape. Measured against the live layer 2026-08-12:
    38,479 of 75,328 names — 51.1% — are `UNKNOWN` + the row's own ID. A NAME
    derived from the ID carries no independent information, so it can neither
    identify a substation nor improve one we already named. Returning None makes
    the UPSERT's COALESCE keep the held name.
    """
    v = _clean(s, 500)
    if v is None or _PLACEHOLDER_NAME.match(v):
        return None
    return v


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
        _clean_name(a.get("NAME")),
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
#
# ★★★ SH52-056, 2026-08-12 — THE CONFLICT TARGET IS NOW THE UPSTREAM ASSET ID.
# It was `(name, lat, lng)`, and that triple is not an identity:
#   · lat/lng are `real` (float4) on this table while upstream ships double
#     precision, so the match is a floating-point rounding coin flip (67.3%
#     collided, 32.7% did not, measured over 6,000 rows on 2026-07-31);
#   · and 51.1% of upstream NAMEs are `UNKNOWN<id>` placeholders, so the `name`
#     component actively DISAGREES with the held validated name and forced the
#     insert of a duplicate. That is what the 07-31 canary hit.
# `hifld_id` is the upstream's own asset id: 75,328/75,328 populated and 75,327
# distinct on the live layer (2026-08-12).
#
# ★ THE PREDICATE IS REQUIRED, NOT DECORATION. substations_hifld_id_uniq is a
# PARTIAL unique index (WHERE hifld_id IS NOT NULL). Postgres can only infer a
# partial index for ON CONFLICT if the statement repeats its predicate — omit
# the WHERE and this raises "no unique or exclusion constraint matching the ON
# CONFLICT specification" for every row.
#
# ★★ `name` USES COALESCE, NOT EXCLUDED. A matched row must never have a
# validated name ('HOLCOMBE') overwritten by a placeholder. _clean_name() maps
# `UNKNOWN<id>` to NULL, and COALESCE then keeps whichever name we already had.
# EXCLUDED.name alone would degrade ~38,000 names on the first full run.
_UPSERT_SQL = """
    INSERT INTO substations
      (hifld_id, name, city, state, zip, type, sub_type, status, county,
       county_fips, lat, lng, naics_code, naics_desc, source_date, val_method,
       val_date, lines, lines_count, max_volt, min_volt, voltage_kv,
       max_voltage_kv, min_voltage_kv, source, updated_at)
    VALUES %s
    ON CONFLICT (hifld_id) WHERE hifld_id IS NOT NULL DO UPDATE SET
      name = COALESCE(EXCLUDED.name, substations.name),
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


def _reconcile_report(dsn, rows):
    """MEASURE how far the held slice can be linked to upstream. Writes nothing.

    ★ WHY THIS IS AN ENDPOINT AND NOT A NUMBER IN A COMMENT. The blocking
    question for SH52-056 is "what does unblocking actually recover, and what
    does it duplicate?", and it has been answered three times by extrapolating
    from a 2,000-row canary. The 07-31 extrapolation ("~25,000 duplicate
    substations") was drawn from a match on (name, lat, lng) — and 51.1% of
    upstream names are `UNKNOWN<id>` placeholders, so that match was always
    going to miss. Every figure below is computed from today's upstream and
    today's table, so the decision is never made against a stale annotation.

    THE MATCH KEY IS COORDINATE ONLY, ROUNDED TO 4dp (~11 m). Name is excluded
    for the reason above. 4dp because the held lat/lng are `real` (float4) and
    upstream ships double precision — comparing them at full precision is the
    coin flip that produced the 67.3% collision rate.

    An ambiguous key — more than one asset at the same rounded point, on either
    side — is REPORTED and never linked. Co-located substations (separate
    voltage tiers on one site) are real, and a tiebreak rule would silently
    marry the wrong pair.
    """
    def key(lat, lng):
        return (round(float(lat), 4), round(float(lng), 4))

    up = {}
    for r in rows:
        hid, lat, lng = r[0], r[10], r[11]
        if hid is None or lat is None or lng is None:
            continue
        up.setdefault(key(lat, lng), []).append(str(hid))

    with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, lat, lng, hifld_id FROM substations "
                " WHERE source = %s AND lat IS NOT NULL AND lng IS NOT NULL",
                (_SRC,))
            held_rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM substations WHERE source = %s "
                        "  AND hifld_id IS NOT NULL", (_SRC,))
            already_linked = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM substations")
            table_total = cur.fetchone()[0]

    held = {}
    for rid, lat, lng, _hid in held_rows:
        held.setdefault(key(lat, lng), []).append(rid)

    amb_up = {k for k, v in up.items() if len(v) > 1}
    amb_held = {k for k, v in held.items() if len(v) > 1}
    linkable = [k for k in up
                if k in held and k not in amb_up and k not in amb_held]
    matched = [k for k in up if k in held]

    return {
        "upstream_records": len(rows),
        "upstream_distinct_ids": len({r[0] for r in rows if r[0] is not None}),
        "upstream_coord_keys": len(up),
        "held_hifld_rows": len(held_rows),
        "held_coord_keys": len(held),
        "held_already_linked": already_linked,
        "matched_coord_keys": len(matched),
        # THE RECOVERY. Not `fetched` — these are the upstream assets we do not
        # hold at all, i.e. what a refresh would genuinely add.
        "upstream_new": len(up) - len(matched),
        # Held rows this snapshot does not list. Reported, never deleted: an
        # upserting feed is a snapshot, not a floor.
        "held_not_in_upstream": len(held) - len(matched),
        "ambiguous_upstream_keys": len(amb_up),
        "ambiguous_held_keys": len(amb_held),
        "linkable_unambiguously": len(linkable),
        "table_total": table_total,
        "placeholder_names_upstream": sum(1 for r in rows if r[1] is None),
    }


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

    if request.args.get("reconcile_report") == "1":
        try:
            out["reconcile"] = _reconcile_report(dsn, rows)
            out["reconcile"]["note"] = (
                "READ-ONLY. Nothing was written. These are the numbers the "
                "unblock decision needs: `upstream_new` is the real recovery "
                "from a refresh, not `fetched`. `ambiguous_*` keys must be "
                "SKIPPED by any link pass — they are genuinely co-located "
                "assets and need a human, not a tiebreak rule.")
        except Exception as e:
            return jsonify(ok=False, error=f"reconcile failed: {str(e)[:200]}"), 500
        return jsonify(out)

    if dry:
        out["dry_run"] = True
        out["sample"] = [list(r) for r in rows[:3]]
        return jsonify(out)

    # ★★★ WRITES ARE DISABLED. 2026-07-31, after a 2,000-row canary against
    # production. This layer is NOT the same vintage as the held March data, and
    # the schema match that justified adopting it turned out to be necessary but
    # NOT sufficient evidence:
    #
    #   670 of 2,000 rows did not match on (name, lat, lng) and were inserted.
    #   668 of those 670 were EXACT coordinate twins of a held row under a
    #   DIFFERENT name — and the held name is the better one:
    #       held 'HOLCOMBE'                    <-> upstream 'UNKNOWN107657'
    #       held 'South Bainbridge Substation' <-> upstream 'UNKNOWN107666'
    #       held 'CRIST'                       <-> upstream 'UNKNOWN107698'
    #   (same lat/lng to the last float4 digit in every case)
    #
    # So this layer carries UNKNOWN<id> placeholders where the held data has
    # validated names. Extrapolated, a full run would have inserted ~25,000
    # duplicate substations with worse names. The 670 were reverted; the table
    # is back to 126,842 with all 28,319 `operator` values intact.
    #
    # ★★★ 2026-08-12 — IDENTITY IS NOW RESOLVED; THE BACKFILL IS NOT.
    # The block stays, and the reason has CHANGED. Saying "pending an identity
    # strategy" here would now be false, and a stale blocker reason is how a
    # resolved problem stays open on the board for months.
    #
    # RESOLVED: the key is upstream `ID`, written to its own `hifld_id` column
    # with a partial unique index (migrations/2026-08-12_substation_hifld_id_
    # identity.sql). Measured on the live layer 2026-08-12: 75,328/75,328
    # populated, 75,327 distinct, namespaced at 107,655+ (OBJECTID is 1..N and
    # matched ID on 0 of 2,000 sampled rows). The placeholder-name trap that
    # made the 07-31 canary destructive is closed too — _clean_name() maps
    # `UNKNOWN<id>` to NULL and the UPSERT COALESCEs, so a validated held name
    # can no longer be replaced by a placeholder.
    #
    # STILL BLOCKING, and it is a BACKFILL, not a design question: 78,356 held
    # rows have hifld_id NULL. Keyed on hifld_id, every upstream record looks
    # new, so a full run would insert ~75,000 rows beside the rows they
    # duplicate. The link pass that fixes this is deliberately not run from
    # here — it needs a live upstream fetch, it has an ambiguous residue that
    # needs a human, and an unbounded write loop against this table is what
    # disabled infra_sync in the scheduler.
    #
    # ?reconcile_report=1 measures the whole decision against today's data.
    # Measured 2026-08-12: 71,006 upstream assets link to a held row on
    # coordinate, 4,212 are genuinely new (that is the real recovery, not
    # 75,328), 8,552 held keys upstream no longer lists, and 235 coordinate
    # keys are ambiguous and must be skipped.
    if not dry:
        return jsonify(
            ok=False, fetched=fetched, usable=len(rows),
            error="writes disabled pending the hifld_id link backfill",
            identity=("RESOLVED — upstream HIFLD `ID`, stored in substations."
                      "hifld_id under a partial unique index. 75,328/75,328 "
                      "populated and 75,327 distinct on the live layer."),
            reason=("78,356 of 79,686 held HIFLD rows still have hifld_id "
                    "NULL, so keyed on the new column every upstream record "
                    "reads as new and a full run would insert ~75,000 "
                    "duplicates. The one-column link pass that closes this "
                    "must run first, skipping the ambiguous coordinate keys."),
            next_step=("POST ?reconcile_report=1 for today's measured "
                       "link coverage, then run the link pass described in "
                       "migrations/2026-08-12_substation_hifld_id_identity.sql"),
            use="?dry_run=1 exercises fetch + mapping and writes nothing",
        ), 409

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
