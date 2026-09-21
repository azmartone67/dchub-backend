"""
DC Hub — reVeal-Specific Endpoints
----------------------------------
Six new endpoints committed for delivery during the NLR License Initial Term:

  GET /api/v1/reveal-cell-bulk          bounding-box query, array of cells in one request
  GET /api/v1/reveal-grid-export        async bulk grid export (Parquet / GeoJSON)
  GET /api/v1/reveal-validation-feed    newly-observed facilities aligned to projection years
  GET /api/v1/social-acceptance-index   composite local-opposition score (slide-25 gap)
  GET /api/v1/climate-risk              flood + wildfire + extreme-heat risk per cell
  GET /api/v1/carbon-intensity          marginal and average grid CO2 intensity per cell

Style follows nlr_intelligence.py: Flask blueprint, soft-fail DB handling,
stateless responses, attribution baked into every payload.

Registration (in main.py, after nlr_intelligence):
    from reveal_endpoints import register_reveal_routes
    register_reveal_routes(app)
"""

import math
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request, url_for

import psycopg2

from util import status_taxonomy

logger = logging.getLogger(__name__)

reveal_ext_bp = Blueprint("reveal_endpoints", __name__)


@reveal_ext_bp.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ---------------------------------------------------------------------------
# DB helper — mirrors pattern from nlr_intelligence
# ---------------------------------------------------------------------------

def _get_db_safe():
    try:
        import __main__
        if hasattr(__main__, "get_pg_connection"):
            conn = __main__.get_pg_connection()
            if conn:
                return conn
    except Exception as exc:
        logger.debug("reveal_endpoints _get_db_safe via __main__: %s", exc)
    try:
        import os as _os, psycopg2
        url = _os.environ.get("DATABASE_URL") or _os.environ.get("NEON_DATABASE_URL")
        if url:
            return psycopg2.connect(url, connect_timeout=5)
    except Exception as exc:
        logger.warning("reveal_endpoints _get_db_safe via DATABASE_URL: %s", exc)
    return None


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ===========================================================================
# 1.  /api/v1/reveal-cell-bulk
# ===========================================================================

MAX_BULK_CELLS = 2500  # cap per request; larger uses reveal-grid-export


@reveal_ext_bp.route("/api/v1/reveal-cell-bulk")
def reveal_cell_bulk():
    """Bounding-box query; returns an array of reveal-cell feature rows."""
    try:
        min_lat = float(request.args.get("min_lat"))
        max_lat = float(request.args.get("max_lat"))
        min_lon = float(request.args.get("min_lon"))
        max_lon = float(request.args.get("max_lon"))
        cell_size_km = float(request.args.get("cell_size_km", 5))
        state = (request.args.get("state") or "").upper() or None
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": f"bbox (min_lat,max_lat,min_lon,max_lon) required as floats: {e}"}), 400

    if min_lat >= max_lat or min_lon >= max_lon:
        return jsonify({"success": False, "error": "Invalid bbox: min must be less than max"}), 400

    # Convert cell_size_km to degrees at the bbox centroid
    cell_deg_lat = cell_size_km / 111.0
    avg_lat = (min_lat + max_lat) / 2.0
    cell_deg_lon = cell_size_km / (111.0 * max(abs(math.cos(math.radians(avg_lat))), 0.1))

    # Tessellate
    n_lat = int(math.ceil((max_lat - min_lat) / cell_deg_lat))
    n_lon = int(math.ceil((max_lon - min_lon) / cell_deg_lon))
    total = n_lat * n_lon
    if total > MAX_BULK_CELLS:
        return jsonify({
            "success": False,
            "error": f"Bbox produces {total:,} cells (> {MAX_BULK_CELLS:,}). Use /api/v1/reveal-grid-export for large extents.",
            "suggested": f"/api/v1/reveal-grid-export?min_lat={min_lat}&max_lat={max_lat}&min_lon={min_lon}&max_lon={max_lon}",
        }), 413

    # Direct Python import of reveal_cell's pure-function logic.
    # We deliberately avoid HTTP self-calls (test_client OR requests.get) because
    # both deadlock under single-worker gunicorn when the outer bulk request
    # is holding the worker.
    try:
        from reveal_cell import compute_reveal_cell
    except ImportError:
        return jsonify({
            "success": True,
            "bbox": {"min_lat": min_lat, "max_lat": max_lat, "min_lon": min_lon, "max_lon": max_lon},
            "cell_size_km": cell_size_km,
            "cells_returned": 0,
            "cells_requested": total,
            "cells": [],
            "warning": (
                "reveal_cell module is not importable. Deploy reveal_cell.py (v2 or later) "
                "which exposes compute_reveal_cell() as a pure function, then restart."
            ),
            "source": "DC Hub reveal-cell-bulk  \u00B7  degraded",
        })

    cells = []
    inner_errors = 0

    for i in range(n_lat):
        for j in range(n_lon):
            lat_c = min_lat + (i + 0.5) * cell_deg_lat
            lon_c = min_lon + (j + 0.5) * cell_deg_lon
            try:
                body = compute_reveal_cell(lat_c, lon_c, cell_size_km, state, 100)
                if body.get("success"):
                    cells.append({
                        "cell_id": body.get("cell", {}).get("cell_id"),
                        "lat": round(lat_c, 4),
                        "lon": round(lon_c, 4),
                        "features": body.get("reveal_features", {}),
                        "suitability_composite": body.get("suitability_composite"),
                        "confidence": body.get("confidence"),
                        "slide25_coverage": body.get("slide25_coverage", {}),
                    })
                else:
                    inner_errors += 1
            except Exception as exc:
                logger.debug("reveal-cell-bulk inner compute failed at (%s,%s): %s", lat_c, lon_c, exc)
                inner_errors += 1

    return jsonify({
        "success": True,
        "bbox": {"min_lat": min_lat, "max_lat": max_lat, "min_lon": min_lon, "max_lon": max_lon},
        "cell_size_km": cell_size_km,
        "cells_returned": len(cells),
        "cells_requested": total,
        "inner_errors": inner_errors,
        "cells": cells,
        "source": "DC Hub reveal-cell-bulk  \u00B7  direct compute via reveal_cell.compute_reveal_cell",
    })


# ===========================================================================
# 2.  /api/v1/reveal-grid-export
# ===========================================================================
# ARTIFACT-BACKED (2026-08-01). This endpoint used to assert readiness it had
# no evidence for: a hard-coded 15-state table, all stamped 2026-04-20 and
# annotated "assumed to have nightly pre-renders", returned status:"ready"
# with a download_url on cdn.dchub.com — a host that has DNS records but does
# not serve (TLS "unrecognized name"). No pre-render job has ever existed:
# nothing under .github/workflows or scripts/ references a grid export.
# /status/<job_id> compounded it by returning "ready" for ANY job id.
#
# So a partner received HTTP 200, the word "ready", and a link that could not
# be fetched. Same false-success class as the validation-feed bug (#2073).
#
# Readiness is now DERIVED FROM STORAGE, never asserted: every response is the
# result of a HEAD against the R2 exports bucket. If the object is there the
# caller gets a working presigned URL; if not, the caller is told so. The
# endpoint starts working on its own the moment a pre-render job lands
# artifacts under the documented key — no second code change needed.
#
# NOT BUILT HERE: the pre-render pipeline itself. It is real work, not a stub —
# reveal_cell computes at roughly 0.2s/cell, so TX (~54,300 cells) is ~3 hours
# of compute. That belongs in a scheduled job writing to the keys below.

# Key layout the (future) pre-render job must write to for this to light up.
GRID_EXPORT_PREFIX = "reveal-grid-exports/"
GRID_EXPORT_FORMATS = ("parquet", "geojson", "csv")

# States the MOU contemplates pre-rendering. Membership here means "in scope",
# NOT "available" — availability is decided by the storage probe alone.
GRID_EXPORT_STATES_IN_SCOPE = (
    "VA", "TX", "CA", "OR", "WA", "AZ", "GA", "NC",
    "IL", "IA", "NE", "CO", "UT", "NV", "FL",
)

# Rough per-state cell counts, for sizing an export request only.
GRID_EXPORT_APPROX_CELLS = {"VA": 7100, "TX": 54300, "CA": 16900, "NY": 5500}


def _grid_export_key(state, fmt):
    return f"{GRID_EXPORT_PREFIX}{state}/reveal_grid_{state}_5km.{fmt}"


def _r2_export_client():
    """boto3 S3 client for the R2 exports bucket, or (None, None) if unset.

    Mirrors routes/r2_exports.py, the working precedent here — note it
    deliberately uses the WRITABLE dchub-daily bucket, not R2_BUCKET_NAME
    (dchub-backups is locked down and 401s on write).
    """
    if not (os.environ.get("R2_ENDPOINT_URL") and os.environ.get("R2_ACCESS_KEY_ID")):
        return None, None
    try:
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("R2_ENDPOINT_URL", ""),
            aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
            region_name="auto",
        )
        return client, (os.environ.get("R2_EXPORTS_BUCKET") or "dchub-daily")
    except Exception as exc:
        logger.warning("reveal-grid-export: R2 client unavailable: %s", exc)
        return None, None


def _grid_artifact(state, fmt):
    """(exists, meta) for a pre-rendered artifact. Never guesses.

    Returns exists=False on any doubt — a missing object, an unconfigured
    bucket, or a storage error. The one thing it must never do is report an
    artifact that has not been verified present.
    """
    client, bucket = _r2_export_client()
    if client is None:
        return False, {"reason": "storage_unconfigured",
                       "key": _grid_export_key(state, fmt)}
    key = _grid_export_key(state, fmt)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        # botocore raises ClientError 404/403 for absent objects; any other
        # error is equally not proof of presence, so it lands here too.
        return False, {"reason": "not_rendered", "detail": type(exc).__name__, "key": key}
    meta = {
        "key": key,
        "bytes": head.get("ContentLength"),
        "last_modified": head["LastModified"].isoformat() if head.get("LastModified") else None,
    }
    try:
        meta["download_url"] = client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
        meta["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    except Exception as exc:
        logger.warning("reveal-grid-export: presign failed for %s: %s", key, exc)
        return False, {"reason": "presign_failed", "detail": type(exc).__name__, "key": key}
    return True, meta


@reveal_ext_bp.route("/api/v1/reveal-grid-export")
def reveal_grid_export():
    """Full 5 km grid export for a state, served from pre-rendered storage.

    Returns a download URL only when the artifact is verified present in R2.
    """
    state = (request.args.get("state") or "").upper() or None
    fmt = (request.args.get("format") or "parquet").lower()
    if fmt not in GRID_EXPORT_FORMATS:
        return jsonify({"success": False,
                        "error": f"format must be {' | '.join(GRID_EXPORT_FORMATS)}"}), 400

    if not state:
        # The bbox mode used to answer status:"queued" with a job_id and an ETA.
        # There is no queue and no worker \u2014 nothing was ever enqueued, and the
        # job_id was just a hash of the request. Reporting that as accepted work
        # is the same false success as the old state path, so it is gone.
        return jsonify({
            "success": False,
            "error": "state is required",
            "detail": ("On-demand bbox rendering is not implemented. The previous "
                       "response reported status:'queued' with a job_id, but no "
                       "render queue exists and nothing was enqueued."),
            "alternatives": {
                "small_extents": ("GET /api/v1/reveal-cell-bulk with "
                                  "min_lat/max_lat/min_lon/max_lon computes live. "
                                  "Keep extents under ~0.35 deg; larger requests "
                                  "exceed the edge timeout."),
                "published_datasets": "GET /api/v1/exports",
            },
            "states_in_scope": sorted(GRID_EXPORT_STATES_IN_SCOPE),
        }), 400

    exists, meta = _grid_artifact(state, fmt)
    if exists:
        return jsonify({
            "success": True,
            "mode": "state",
            "state": state,
            "format": fmt,
            "status": "ready",
            "download_url": meta["download_url"],
            "expires_at": meta["expires_at"],
            "bytes": meta["bytes"],
            "last_refresh_utc": meta["last_modified"],
            "approx_cell_count": GRID_EXPORT_APPROX_CELLS.get(state, "varies"),
            "source": "DC Hub reveal-grid-export  \u00B7  pre-rendered artifact in object storage",
        })

    in_scope = state in GRID_EXPORT_STATES_IN_SCOPE
    return jsonify({
        "success": False,
        "mode": "state",
        "state": state,
        "format": fmt,
        "status": "not_rendered",
        "error": (f"No pre-rendered {fmt} grid export exists for {state}."
                  if in_scope else
                  f"{state} is not in the pre-render scope, and no artifact exists for it."),
        "detail": ("Availability is checked against object storage on every request. "
                   "This endpoint previously reported 'ready' with a download URL on "
                   "cdn.dchub.com, which does not serve \u2014 that response was never "
                   "backed by an artifact."),
        "reason": meta.get("reason"),
        "expected_key": meta.get("key"),
        "state_in_scope": in_scope,
        "states_in_scope": sorted(GRID_EXPORT_STATES_IN_SCOPE),
        "alternatives": {
            "small_extents": ("GET /api/v1/reveal-cell-bulk with "
                              "min_lat/max_lat/min_lon/max_lon computes live"),
            "published_datasets": "GET /api/v1/exports",
        },
        "source": "DC Hub reveal-grid-export  \u00B7  storage probe",
    }), 404


@reveal_ext_bp.route("/api/v1/reveal-grid-export/status/<job_id>")
def reveal_grid_export_status(job_id):
    """Resolve an export id ('<STATE>' or '<STATE>.<format>') against storage.

    This used to return status:"ready" plus a download URL for ANY job id \u2014 an
    invented id was confirmed "ready" on the first try. It now resolves the id
    against storage and 404s when there is nothing behind it.
    """
    raw = (job_id or "").strip()
    state, _, fmt = raw.partition(".")
    state = state.upper()
    fmt = (fmt or "parquet").lower()

    if not state.isalpha() or len(state) != 2 or fmt not in GRID_EXPORT_FORMATS:
        return jsonify({
            "success": False,
            "job_id": job_id,
            "status": "unknown",
            "error": "job_id must be '<STATE>' or '<STATE>.<format>', e.g. 'VA' or 'VA.geojson'",
            "detail": ("Opaque queue job ids are no longer issued \u2014 there is no "
                       "render queue. Export ids address a stored artifact."),
        }), 400

    exists, meta = _grid_artifact(state, fmt)
    if not exists:
        return jsonify({
            "success": False,
            "job_id": job_id,
            "state": state,
            "format": fmt,
            "status": "not_rendered",
            "error": f"No pre-rendered {fmt} grid export exists for {state}.",
            "reason": meta.get("reason"),
            "expected_key": meta.get("key"),
        }), 404

    return jsonify({
        "success": True,
        "job_id": job_id,
        "state": state,
        "format": fmt,
        "status": "ready",
        "download_url": meta["download_url"],
        "expires_at": meta["expires_at"],
        "bytes": meta["bytes"],
        "last_refresh_utc": meta["last_modified"],
    })


# ===========================================================================
# 3.  /api/v1/reveal-validation-feed
# ===========================================================================

# reVeal 5-year projection horizons.
PROJECTION_BUCKETS = [2025, 2030, 2035, 2040, 2045, 2050]

# Max rows per request. Uncapped `limit` on a partner-facing route is a
# table dump; larger pulls go through the quarterly snapshot (MOU A.4).
MAX_FEED_LIMIT = 5000

_YEAR_RE = re.compile(r"((?:19|20)\d{2})")


def _projection_bucket(year):
    """Map a completion year onto the reVeal horizon it falls inside."""
    if not year:
        return "unknown"
    for bucket in PROJECTION_BUCKETS:
        if year <= bucket:
            return str(bucket)
    return "2050+"


def _completion_year(raw):
    """First 19xx/20xx in `expected_completion`, which is free-text.

    Live values include '2027', 'Jun 2027' and 'Q3 2027', so a bare
    fromisoformat() rejects most of them. Returns None when no year is
    present \u2014 never a default. See the projection_basis note below.
    """
    if not raw:
        return None
    m = _YEAR_RE.search(str(raw))
    return int(m.group(1)) if m else None


@reveal_ext_bp.route("/api/v1/reveal-validation-feed")
def reveal_validation_feed():
    """Newly-observed facilities since a given date, aligned to reVeal projection years.

    SCHEMA NOTE (2026-07-31) \u2014 this handler shipped against a guessed column
    list that was never checked against the live table. Five of its nine
    columns do not exist on `discovered_facilities`: lat, lng, nameplate_mw,
    announcement_date and updated_at. Every call raised UndefinedColumn, the
    bare `except Exception` downgraded it to a logger.warning, and the route
    returned 200 with `"facilities": []`. It had therefore NEVER returned a
    row. Verified against the read replica on 2026-07-31.

    Real columns: latitude, longitude, power_mw, last_updated, first_seen.

    `since` now filters on `first_seen` (when DC Hub first observed the
    facility), not `last_updated`. That is what "newly-observed" means, and
    `last_updated` is dominated by bulk re-stamps \u2014 a 30-day window over it
    returns 10,596 rows against 1,292 for first_seen, most of them rows that
    were merely re-touched. Both timestamps ship on every row so the caller
    can reconstruct either reading. This is not a breaking change: the route
    has never returned a non-empty body to any caller.
    """
    since = request.args.get("since")  # ISO date
    status_filter = request.args.get("status")  # comma-separated
    projection_year = request.args.get("projection_year")  # e.g. 2028

    try:
        limit = int(request.args.get("limit", 500))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "limit must be an integer"}), 400
    limit = max(1, min(limit, MAX_FEED_LIMIT))

    try:
        since_dt = (datetime.fromisoformat(since) if since
                    else datetime.now(timezone.utc) - timedelta(days=30))
    except ValueError:
        return jsonify({"success": False, "error": "since must be ISO-8601 date"}), 400
    # first_seen is timestamptz. A naive bound would be resolved against the
    # session TimeZone (GMT on Neon today, but that is a server setting, not
    # a guarantee). Pin the comparison to UTC explicitly.
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)

    # Live status values are Title-Case ('Operational', 'Under Construction').
    # The old lowercase literal list was fed to a case-sensitive `status IN
    # (...)` and matched 12 rows of 23,315. Match on LOWER(TRIM(...)) via the
    # canonical taxonomy instead of re-inlining a vocabulary here (#2058).
    if status_filter:
        statuses = sorted({status_taxonomy.normalize(s) for s in status_filter.split(",") if s.strip()})
        if not statuses:
            return jsonify({"success": False, "error": "status must be a comma-separated list"}), 400
        status_predicate = f"{status_taxonomy.norm_expr('status')} IN ({','.join(['%s'] * len(statuses))})"
        status_args = list(statuses)
    else:
        # Default: every status the taxonomy recognises, in either bucket.
        # Unclassified (NULL/'' /unknown vocabulary) is excluded rather than
        # folded into operational, and is counted separately below.
        statuses = sorted(status_taxonomy.OPERATIONAL_STATUSES | status_taxonomy.PIPELINE_STATUSES)
        status_predicate = (f"({status_taxonomy.operational_sql('status')}"
                            f" OR {status_taxonomy.pipeline_sql('status')})")
        status_args = []

    facilities = []
    unclassified_excluded = 0
    coverage = {}
    conn = _get_db_safe()
    if conn is None:
        return jsonify({
            "success": False,
            "error": "database unavailable",
            "detail": "reveal-validation-feed could not obtain a connection",
        }), 503

    try:
        cur = conn.cursor()
        # Columns verified against the live table on 2026-07-31.
        # COALESCE(is_duplicate,0)=0 is the house fleet filter \u2014 is_duplicate
        # is INTEGER NOT NULL here (0/1), not boolean, and 7,649 of 23,315
        # rows are duplicates. A partner feed must not ship them.
        sql = f"""
            SELECT id, name, latitude, longitude, status, power_mw,
                   expected_completion, first_seen, last_updated, state, country
            FROM discovered_facilities
            WHERE first_seen >= %s
              AND COALESCE(is_duplicate, 0) = 0
              AND {status_predicate}
            ORDER BY first_seen DESC
            LIMIT %s
        """
        cur.execute(sql, [since_dt] + status_args + [limit])
        for row in cur.fetchall():
            fid, name, lat, lon, status, mw, exp_completion, seen, upd, state, country = row
            year = _completion_year(exp_completion)
            facilities.append({
                "id": fid,
                "name": name,
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                "status": status,
                "status_bucket": status_taxonomy.classify(status),
                "nameplate_mw": float(mw) if mw is not None else None,
                # No announcement date exists on this table \u2014 see date_basis.
                "announcement_date": None,
                "expected_completion": exp_completion,
                "expected_completion_year": year,
                "projection_bucket": _projection_bucket(year),
                "first_seen": seen.isoformat() if seen else None,
                "last_updated": upd.isoformat() if upd else None,
                "state": state,
                "country": country,
            })

        # How many rows the window holds that the status filter dropped for
        # being unclassified \u2014 surfaced, never silently folded into a bucket.
        cur.execute(f"""
            SELECT COUNT(*) FROM discovered_facilities
            WHERE first_seen >= %s
              AND COALESCE(is_duplicate, 0) = 0
              AND {status_taxonomy.unclassified_sql('status')}
        """, [since_dt])
        unclassified_excluded = cur.fetchone()[0]
        cur.close()
    except psycopg2.ProgrammingError:
        # Schema drift / bad SQL is OUR bug. This is the exact failure that
        # kept this route dead and unnoticed for two quarters: it was caught
        # by a bare `except Exception`, logged at warning, and reported as a
        # 200 with an empty list. Never again \u2014 fail loud, fail visibly.
        logger.exception("reveal-validation-feed: schema error against discovered_facilities")
        try:
            conn.rollback(); conn.close()
        except Exception:
            pass
        return jsonify({
            "success": False,
            "error": "query failed against discovered_facilities (schema mismatch)",
            "detail": "reported deliberately rather than returned as an empty feed",
        }), 500
    except psycopg2.Error:
        # Transient DB trouble \u2014 soft-fail is legitimate here, but it must
        # not masquerade as a successful empty result either.
        logger.exception("reveal-validation-feed: database error")
        try:
            conn.rollback(); conn.close()
        except Exception:
            pass
        return jsonify({
            "success": False,
            "error": "database error while reading discovered_facilities",
        }), 503

    try:
        conn.close()
    except Exception:
        pass

    # Bucket counts describe the window BEFORE the projection filter, so a
    # caller asking for one year can still see how the window was distributed
    # (and, in particular, how much of it is 'unknown').
    bucketed = {}
    for f in facilities:
        b = f["projection_bucket"]
        bucketed[b] = bucketed.get(b, 0) + 1

    if projection_year:
        facilities = [f for f in facilities if f["projection_bucket"] == str(projection_year)]

    # Field coverage over exactly the rows being RETURNED — computed after the
    # projection filter so that `rows` always equals `count`. Computing it
    # before gave coverage.rows=200 alongside count=0 for projection_year=2030:
    # two different denominators in one payload, which is the mismatch this
    # block exists to prevent.
    #
    # reVeal is running a validation rig against this feed; a modeller who
    # assumes lat/lon and MW are populated will silently validate against
    # mostly-empty rows. In the current 30-day window only ~10% of rows carry
    # coordinates and none carry a non-zero MW figure. Say so in the payload.
    if facilities:
        coverage = {
            "rows": len(facilities),
            "with_coordinates": sum(1 for f in facilities if f["lat"] is not None and f["lon"] is not None),
            # present-but-zero and absent are different facts: power_mw is
            # 0.0 on most Operational rows and NULL on most Announced ones.
            # Collapsing them would read as "we measured 0 MW".
            "with_nameplate_mw": sum(1 for f in facilities if f["nameplate_mw"] is not None),
            "with_nonzero_nameplate_mw": sum(1 for f in facilities if f["nameplate_mw"]),
            "with_expected_completion": sum(1 for f in facilities if f["expected_completion_year"]),
            "with_state": sum(1 for f in facilities if f["state"]),
            "note": ("counts are of returned rows; unpopulated fields are null, never "
                     "zero-filled. A nameplate_mw of 0.0 is a stored zero, not a measurement."),
        }

    return jsonify({
        "success": True,
        "since": since_dt.isoformat(),
        "since_basis": "first_seen \u2014 when DC Hub first observed the facility",
        "statuses_included": statuses,
        "status_basis": status_taxonomy.basis("reveal-validation-feed"),
        "unclassified_excluded": unclassified_excluded,
        "projection_year_filter": projection_year,
        "count": len(facilities),
        "limit": limit,
        "projection_year_buckets": bucketed,
        "field_coverage": coverage,
        "date_basis": {
            "announcement_date": (
                "always null \u2014 discovered_facilities carries no announcement date. "
                "first_seen is a DC Hub observation stamp (and is dominated by two "
                "2026-03 backfills), not an announcement; substituting it would date "
                "every facility to 2026 and collapse the whole feed into one "
                "projection bucket. Reported as null rather than guessed."
            ),
            "projection_bucket": (
                "derived from expected_completion, the only forward-looking column "
                "on this table. It is populated on 10 of 23,315 rows, so nearly "
                "every row buckets as 'unknown'. Treat the buckets as a floor."
            ),
        },
        "facilities": facilities,
        "source": "DC Hub discovered_facilities  \u00B7  aligned to reVeal 5-year projection buckets",
    })


# ===========================================================================
# 4.  /api/v1/social-acceptance-index
# ===========================================================================
# Composite 0-100 where LOWER means MORE opposition (harder to site).
#
# This header used to describe the score as "derived from: (a) local news
# sentiment, (b) zoning/ordinance litigation count, (c) community meeting
# opposition signals, (d) organized-opposition-group flags." None of those are
# measured. The score is an inverse-distance-weighted mean over the hand-placed
# jurisdiction table below, and the four "components" were a coordinate hash --
# see the note in the handler. Corrected 2026-08-01.

# Known high-friction jurisdictions (approximate — for calibration)
HIGH_FRICTION_COUNTIES = {
    # (lat, lon, radius_km, name, friction_score 0-100, drivers)
    (38.9, -77.3, 60, "Loudoun County VA",   42, "data center moratorium debate, ordinance revision 2024"),
    (39.0, -77.5, 45, "Prince William VA",   48, "PW Digital Gateway opposition"),
    (44.9, -123.0, 40, "Umatilla Morrow OR", 55, "water use concerns"),
    (45.5, -122.7, 30, "Hillsboro OR",       65, "generally accepting"),
    (33.4, -112.0, 50, "Phoenix Metro AZ",   60, "water + heat concerns"),
    (32.8, -96.9, 70, "DFW Metro TX",        78, "high acceptance"),
    (41.9, -87.7, 35, "Cook County IL",      58, "noise + visual impact concerns"),
    (40.7, -74.0, 40, "NYC Metro NJ",        46, "land use density"),
    (37.3, -121.9, 35, "Santa Clara CA",     52, "housing competition"),
    (41.6, -93.5, 45, "Polk County IA",      72, "strong acceptance"),
    (40.7, -96.0, 45, "Omaha NE",            74, "strong acceptance"),
    (25.8, -80.3, 25, "Miami-Dade FL",       54, "flood + heat concerns"),
}


@reveal_ext_bp.route("/api/v1/social-acceptance-index")
def social_acceptance_index():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        radius_km = float(request.args.get("radius_km", 50))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": f"lat, lon required as floats: {e}"}), 400

    # Weighted average of nearby known jurisdictions by 1/(dist+1)
    nearby = []
    total_w = 0
    weighted_score = 0
    for (jlat, jlon, jrad, jname, jscore, jnotes) in HIGH_FRICTION_COUNTIES:
        d = _haversine_km(lat, lon, jlat, jlon)
        if d <= max(radius_km, jrad):
            w = 1.0 / (d + 1.0)
            weighted_score += jscore * w
            total_w += w
            nearby.append({
                "name": jname, "distance_km": round(d, 1),
                "friction_score": jscore, "notes": jnotes,
            })
    nearby.sort(key=lambda x: x["distance_km"])

    # Uncovered -> None. This used to default to 70 ("most of the country is
    # moderately accepting"), which published a specific-looking score for
    # every point on earth including ones with no modelled jurisdiction within
    # range. Same false-specificity as climate-risk's old zero.
    covered = total_w > 0
    composite = int(round(weighted_score / total_w)) if covered else None

    def _rating(s):
        if s is None: return "Unknown — no modelled jurisdiction within range"
        if s >= 75: return "High acceptance — low siting friction"
        if s >= 60: return "Moderate acceptance — standard community engagement"
        if s >= 45: return "Mixed — active opposition signal in area"
        if s >= 30: return "Low acceptance — meaningful opposition risk"
        return "Minimal acceptance — high organized opposition"

    return jsonify({
        "success": True,
        "location": {"lat": lat, "lon": lon},
        "social_acceptance_index": composite,
        "rating": _rating(composite),
        # ~~ WHY THESE ARE NULL (2026-08-01) ~~
        # They used to carry integers derived from
        #     seed = abs(math.sin(lat * 12.9898 + lon * 78.233)) % 1
        # i.e. a hash of the coordinates, shaped around the composite. The
        # in-code comment said "synthetic but calibrated"; the RESPONSE said
        # litigation_count_12mo. A partner regressing against a count of
        # lawsuits was consuming a coordinate hash. DC Hub does not track
        # per-jurisdiction litigation counts or news sentiment, so the honest
        # value is null -- the keys stay for wire compatibility, exactly as
        # announcement_date does on reveal-validation-feed (#2073).
        "components": {
            "news_sentiment_score": None,
            "litigation_count_12mo": None,
            "community_opposition_signals": None,
            "organized_opposition_groups_present": None,
        },
        "components_basis": {
            "status": "not_measured",
            "detail": ("DC Hub does not currently measure per-jurisdiction news "
                       "sentiment, litigation counts or community-meeting signals. "
                       "These fields previously returned values derived from a hash "
                       "of the coordinates and have been nulled rather than "
                       "re-attributed."),
            "what_is_real": ("social_acceptance_index and nearby_jurisdictions, both "
                             "derived from the modelled jurisdiction set below."),
        },
        "nearby_jurisdictions": nearby[:5],
        "coverage": {
            "covered": covered,
            "modelled_jurisdictions": len(HIGH_FRICTION_COUNTIES),
            "matched_within_radius": len(nearby),
            "note": ("Coverage is a finite set of hand-placed jurisdictions, not a "
                     "national surface. A null index means no modelled jurisdiction "
                     "falls within range — it is NOT a finding of neutral or high "
                     "acceptance."),
        },
        "basis": {
            "method": ("inverse-distance-weighted mean of hand-placed jurisdiction "
                       "friction scores"),
            "live_feeds": False,
            "disclosure": ("Modelled priors calibrated against known case studies "
                           "(Loudoun, Umatilla, DFW). Not a live news, court or "
                           "permitting feed."),
        },
        "interpretation_note": "Higher = more accepting. Lower = more siting friction.",
        "source": "DC Hub social-acceptance-index  ·  DC Hub jurisdiction model (not a live feed)",
    })


# ===========================================================================
# 5.  /api/v1/climate-risk
# ===========================================================================
# HAZARD MODEL, NOT A LIVE FEED (corrected 2026-08-01).
#
# This endpoint's source string used to read "FEMA flood + NIFC wildfire +
# NOAA extreme heat proxies". It calls none of those services. What follows is
# a DC Hub-maintained zone model: 20 hand-placed circles with a distance decay,
# calibrated from public FEMA/NIFC/NOAA reference material. That is a legitimate
# modelling input, but it is not FEMA data and must not be attributed as such.
#
# ~~ THE BUG THIS FIXES ~~
# _zone_score() returned 0 for a point outside every circle, and 0 flowed into
# the composite and rendered as "Minimal". So Loudoun County VA -- a core reVeal
# siting market, and outside all 20 zones -- reported composite 0 / "Minimal" on
# all three components. A modeller reading that gets "we measured this and found
# no risk" when the truth is "this location is outside the model's coverage".
# Out-of-coverage is now None and surfaces as "unknown", never 0. Same class as
# the validation-feed 200-with-[] and the grid-export "ready" (#2073, above).
#
# ~~ WHY THIS IS NOT WIRED TO REAL FEMA DATA ~~
# The DB does carry `fema_risk_index` (3,232 rows, 1,924 counties, full state
# coverage). It is NOT usable here, for two independent reasons, both measured
# 2026-08-01:
#   1. Every per-hazard column -- flood_score, wildfire_score, hurricane_score,
#      tornado_score, earthquake_score, drought_score, winter_storm_score -- is
#      100% NULL. Only the COMPOSITE risk_score/risk_rating is populated, so it
#      cannot answer the flood/wildfire/heat breakdown this endpoint returns.
#   2. It is keyed on state+county, and there is no lat/lon -> county join in
#      the database. The only reverse geocoder on the codebase
#      (site_planner.reverse_geocode) calls Nominatim over the network, which
#      cannot go in this path -- reveal-cell-bulk invokes per-cell compute in a
#      loop of up to several hundred cells.
# The county-level FEMA composite is reachable via
# /api/v1/site-planner/disaster-risk. Wiring a real per-hazard feed in here
# needs the NRI per-hazard columns backfilled AND a local FIPS lookup.
#
# Scores are 0-100 where HIGHER = HIGHER RISK.

FLOOD_RISK_ZONES = [
    # (lat, lon, radius_km, name, score)
    (29.76, -95.37, 120, "Houston / Gulf Coast", 82),
    (25.76, -80.19, 80,  "Miami / SE Florida",   85),
    (30.33, -90.00, 60,  "New Orleans",           95),
    (29.95, -81.33, 50,  "NE Florida",            70),
    (40.71, -74.00, 40,  "NY Harbor",             62),
    (33.74, -84.38, 35,  "Atlanta flash flood",   52),
]
WILDFIRE_RISK_ZONES = [
    (34.05, -118.25, 100, "Southern CA WUI",   85),
    (38.58, -121.49, 120, "Northern CA WUI",   78),
    (40.00, -105.30, 90,  "CO Front Range WUI", 72),
    (45.50, -122.70, 80,  "PNW WUI",            65),
    (33.45, -112.07, 80,  "AZ Sonoran fringe", 55),
    (36.20, -115.14, 80,  "NV WUI",             58),
]
EXTREME_HEAT_ZONES = [
    (33.45, -112.07, 150, "Phoenix heat island", 92),
    (36.17, -115.14, 100, "Las Vegas",           85),
    (32.72, -117.16, 60,  "San Diego / LA",      55),
    (29.76, -95.37, 80,   "Houston",             72),
    (32.75, -96.80, 80,   "Dallas",              68),
    (30.27, -97.74, 50,   "Austin",              70),
    (25.76, -80.19, 60,   "Miami",               62),
    (35.22, -101.83, 100, "Texas Panhandle",     60),
]

CLIMATE_MODEL_VERSION = "dchub-zone-model-2026-08-01"


def _zone_score(lat, lon, zones):
    """(score, contributing) for a point, or (None, None) if UNCOVERED.

    None is load-bearing. Returning 0 for an uncovered point is what made this
    endpoint report "Minimal" risk for locations it knows nothing about.
    """
    best = None
    contrib = None
    for (zlat, zlon, zrad, zname, zscore) in zones:
        d = _haversine_km(lat, lon, zlat, zlon)
        if d <= zrad:
            decay = max(0.0, 1.0 - d / zrad * 0.6)
            sc = int(round(zscore * decay))
            if best is None or sc > best:
                best = sc
                contrib = {"zone": zname, "distance_km": round(d, 1),
                           "raw_score": zscore, "effective": sc}
    return best, contrib


@reveal_ext_bp.route("/api/v1/climate-risk")
def climate_risk():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": f"lat, lon required: {e}"}), 400

    flood_sc, flood_ctrb = _zone_score(lat, lon, FLOOD_RISK_ZONES)
    fire_sc, fire_ctrb   = _zone_score(lat, lon, WILDFIRE_RISK_ZONES)
    heat_sc, heat_ctrb   = _zone_score(lat, lon, EXTREME_HEAT_ZONES)

    covered = [s for s in (flood_sc, fire_sc, heat_sc) if s is not None]

    def _rating(s):
        if s is None: return "Unknown — outside model coverage"
        if s >= 75: return "Severe — material siting concern"
        if s >= 55: return "Elevated — engineering + insurance adjustments warranted"
        if s >= 35: return "Moderate — standard resilience design"
        if s >= 15: return "Low — minimal climate exposure"
        return "Minimal"

    if covered:
        # Weighted to emphasise the dominant hazard (risk stacking is not
        # linear). Only COVERED components take part -- an uncovered hazard
        # contributes nothing rather than dragging the composite toward 0.
        parts = sorted(covered, reverse=True)
        weights = [0.6, 0.3, 0.1][:len(parts)]
        composite = int(round(sum(p * w for p, w in zip(parts, weights)) / sum(weights)))
    else:
        composite = None

    def _component(score, contrib):
        return {
            "score": score,
            "covered": score is not None,
            "contributing": contrib,
            **({} if score is not None else
               {"note": "no modelled zone covers this location — not a finding of low risk"}),
        }

    return jsonify({
        "success": True,
        "location": {"lat": lat, "lon": lon},
        "climate_risk_composite": composite,
        "rating": _rating(composite),
        "components": {
            "flood_risk": _component(flood_sc, flood_ctrb),
            "wildfire_risk": _component(fire_sc, fire_ctrb),
            "extreme_heat_risk": _component(heat_sc, heat_ctrb),
        },
        "coverage": {
            "components_covered": len(covered),
            "components_total": 3,
            "modelled_zones": {
                "flood": len(FLOOD_RISK_ZONES),
                "wildfire": len(WILDFIRE_RISK_ZONES),
                "extreme_heat": len(EXTREME_HEAT_ZONES),
            },
            "note": ("Coverage is a finite set of modelled zones, not a national "
                     "surface. A null score means this location sits outside every "
                     "modelled zone for that hazard — it is NOT a measurement of "
                     "zero risk. Absence of coverage is not absence of hazard."),
        },
        "basis": {
            "model": CLIMATE_MODEL_VERSION,
            "method": ("distance-decayed maximum over hand-placed hazard zones, "
                       "calibrated from public FEMA / NIFC / NOAA reference material"),
            "live_feeds": False,
            "disclosure": ("DC Hub does NOT call FEMA, NIFC or NOAA at request time. "
                           "Treat these as modelled priors, not agency data."),
            "county_level_fema": ("FEMA National Risk Index composite scores are "
                                  "available per county at "
                                  "/api/v1/site-planner/disaster-risk"),
        },
        "interpretation_note": ("Higher = higher risk. The composite emphasises the "
                                "dominant hazard (0.6/0.3/0.1 over covered components) "
                                "rather than averaging it away."),
        "source": "DC Hub climate-risk  ·  DC Hub hazard-zone model (not a live agency feed)",
    })


# ===========================================================================
# 6.  /api/v1/carbon-intensity
# ===========================================================================
# Marginal and average CO2 intensity (lb/MWh), by ISO region + state adjustment.
# Values based on publicly available EIA + eGRID / EPA 2024 reference data.

ISO_REGIONS = {
    # state -> (ISO_region, avg_lb_per_mwh, marginal_lb_per_mwh, mix_summary)
    "TX": ("ERCOT",      720,   910, "44% gas, 28% wind, 15% coal, 7% solar, 6% nuclear"),
    "CA": ("CAISO",      470,   720, "44% gas, 34% solar, 10% hydro, 9% wind, 3% nuclear"),
    "VA": ("PJM-VA",     680,   860, "40% gas, 31% nuclear, 14% coal, 8% solar, 7% other"),
    "NY": ("NYISO",      490,   780, "45% gas, 33% hydro, 10% nuclear, 9% wind/solar, 3% other"),
    "WA": ("NWPP",       230,   560, "65% hydro, 13% gas, 10% wind, 8% nuclear, 4% other"),
    "OR": ("NWPP",       280,   610, "52% hydro, 20% gas, 14% wind, 9% coal, 5% solar"),
    "IL": ("PJM-ComEd",  650,   820, "52% nuclear, 23% gas, 14% coal, 9% wind, 2% solar"),
    "GA": ("SERC-GA",    790,   940, "42% gas, 28% nuclear, 18% coal, 7% solar, 5% other"),
    "NC": ("SERC-NC",    720,   870, "40% gas, 34% nuclear, 11% solar, 9% coal, 6% other"),
    "FL": ("SERC-FL",    820,   980, "72% gas, 11% nuclear, 8% solar, 6% coal, 3% other"),
    "AZ": ("WECC-AZ",    700,   880, "39% gas, 28% solar, 22% nuclear, 8% coal, 3% other"),
    "NV": ("WECC-NV",    640,   860, "62% gas, 23% solar, 8% geothermal, 5% hydro, 2% other"),
    "UT": ("WECC-UT",    1040,  1180, "56% coal, 28% gas, 13% solar, 3% other"),
    "CO": ("WECC-CO",    860,   1050, "44% gas, 24% coal, 23% wind, 7% solar, 2% other"),
    "IA": ("MISO-IA",    540,   780, "60% wind, 21% coal, 13% gas, 4% nuclear, 2% other"),
    "NE": ("SPP-NE",     710,   920, "42% coal, 31% wind, 14% nuclear, 10% gas, 3% other"),
    "OK": ("SPP-OK",     630,   840, "44% wind, 35% gas, 14% coal, 5% solar, 2% other"),
    "KS": ("SPP-KS",     650,   870, "47% wind, 32% coal, 15% gas, 4% nuclear, 2% other"),
    "OH": ("PJM-OH",     820,   960, "39% gas, 33% nuclear, 22% coal, 4% wind, 2% solar"),
    "PA": ("PJM-PA",     690,   870, "53% gas, 33% nuclear, 9% coal, 4% wind, 1% solar"),
    "WY": ("WECC-WY",    1450,  1520, "78% coal, 12% gas, 9% wind, 1% other"),
    "NM": ("WECC-NM",    870,   1080, "42% gas, 26% coal, 22% solar, 8% wind, 2% other"),
    "ID": ("NWPP-ID",    330,   580, "52% hydro, 28% gas, 13% wind, 5% geothermal, 2% other"),
    "MT": ("WECC-MT",    890,   1090, "47% coal, 26% hydro, 20% wind, 5% gas, 2% other"),
    "MN": ("MISO-MN",    660,   850, "30% wind, 27% nuclear, 22% coal, 16% gas, 5% solar"),
    "WI": ("MISO-WI",    760,   920, "40% gas, 23% coal, 17% nuclear, 12% wind, 8% other"),
    "MO": ("MISO-MO",    1080,  1220, "72% coal, 13% nuclear, 9% gas, 4% wind, 2% other"),
    "KY": ("SERC-KY",    1050,  1180, "72% coal, 20% gas, 5% hydro, 2% solar, 1% other"),
    "TN": ("TVA-TN",     620,   810, "40% nuclear, 27% gas, 18% coal, 10% hydro, 5% other"),
    "AL": ("SERC-AL",    690,   860, "40% gas, 29% nuclear, 21% coal, 8% hydro, 2% solar"),
    "MS": ("SERC-MS",    710,   870, "79% gas, 12% nuclear, 5% coal, 3% solar, 1% other"),
    "LA": ("SERC-LA",    720,   880, "70% gas, 16% nuclear, 9% coal, 3% solar, 2% other"),
    "AR": ("MISO-AR",    720,   890, "46% gas, 25% coal, 22% nuclear, 5% hydro, 2% solar"),
    "MA": ("ISO-NE",     480,   690, "54% gas, 24% nuclear, 12% renewables, 6% hydro, 4% other"),
    "CT": ("ISO-NE",     520,   720, "45% gas, 42% nuclear, 8% renewables, 3% hydro, 2% other"),
    "RI": ("ISO-NE",     540,   740, "85% gas, 12% renewables, 3% other"),
    "ME": ("ISO-NE",     290,   540, "46% hydro, 28% gas, 17% biomass, 7% wind, 2% solar"),
    "VT": ("ISO-NE",     210,   430, "47% hydro, 20% biomass, 18% solar, 13% wind, 2% other"),
    "NH": ("ISO-NE",     350,   620, "55% nuclear, 22% gas, 15% hydro, 5% wind, 3% other"),
    "NJ": ("PJM-NJ",     560,   780, "49% gas, 42% nuclear, 5% solar, 3% other, 1% wind"),
    "DE": ("PJM-DE",     720,   890, "88% gas, 7% solar, 3% coal, 2% other"),
    "MD": ("PJM-MD",     670,   860, "40% nuclear, 31% gas, 13% renewables, 9% coal, 7% other"),
    "WV": ("PJM-WV",     1610,  1640, "92% coal, 6% gas, 1% hydro, 1% wind"),
    "IN": ("MISO-IN",    1110,  1240, "53% coal, 32% gas, 9% wind, 3% solar, 3% other"),
    "MI": ("MISO-MI",    750,   920, "34% gas, 26% coal, 23% nuclear, 9% renewables, 8% other"),
    "SD": ("MISO-SD",    260,   580, "60% hydro, 26% wind, 11% gas, 3% other"),
    "ND": ("SPP-ND",     1060,  1260, "58% coal, 36% wind, 4% gas, 2% other"),
    "SC": ("SERC-SC",    660,   830, "55% nuclear, 22% gas, 11% coal, 8% solar, 4% other"),
    "AK": ("Alaska",     820,   950, "60% gas, 14% oil, 14% hydro, 8% coal, 4% other"),
    "HI": ("Hawaii",     1320,  1410, "70% oil, 16% solar, 6% wind, 4% coal, 4% other"),
    "DC": ("PJM-DC",     670,   860, "matches VA composite"),
}


def _state_from_latlon(lat, lon):
    """Rough state inference from lat/lon for when state isn't provided.
    Intentionally simple; production uses a shapefile lookup."""
    # This is a very rough bounding-box heuristic; in production we'd use a real PIP.
    ROUGH = {
        "CA": (32.5, 42.0, -124.5, -114.1), "OR": (42.0, 46.3, -124.6, -116.5),
        "WA": (45.5, 49.0, -124.8, -116.9), "NV": (35.0, 42.0, -120.0, -114.0),
        "UT": (37.0, 42.0, -114.0, -109.0), "AZ": (31.3, 37.0, -114.8, -109.0),
        "ID": (42.0, 49.0, -117.2, -111.0), "MT": (44.4, 49.0, -116.0, -104.0),
        "WY": (41.0, 45.0, -111.1, -104.0), "CO": (37.0, 41.0, -109.1, -102.0),
        "NM": (31.3, 37.0, -109.1, -103.0), "TX": (25.8, 36.5, -106.6, -93.5),
        "OK": (33.6, 37.0, -103.0, -94.4),  "KS": (37.0, 40.0, -102.1, -94.6),
        "NE": (40.0, 43.0, -104.1, -95.3),  "SD": (42.5, 46.0, -104.1, -96.4),
        "ND": (45.9, 49.0, -104.1, -96.6),  "MN": (43.5, 49.4, -97.3, -89.5),
        "IA": (40.4, 43.5, -96.7, -90.1),   "MO": (36.0, 40.6, -95.8, -89.1),
        "AR": (33.0, 36.5, -94.6, -89.6),   "LA": (28.9, 33.0, -94.0, -88.8),
        "MS": (30.2, 35.0, -91.7, -88.1),   "AL": (30.2, 35.0, -88.5, -84.9),
        "GA": (30.4, 35.0, -85.6, -80.8),   "FL": (24.5, 31.0, -87.6, -80.0),
        "SC": (32.0, 35.2, -83.4, -78.5),   "NC": (33.8, 36.6, -84.3, -75.5),
        "VA": (36.5, 39.5, -83.7, -75.2),   "WV": (37.2, 40.6, -82.6, -77.7),
        "KY": (36.5, 39.1, -89.6, -81.9),   "TN": (35.0, 36.7, -90.3, -81.6),
        "IL": (36.9, 42.5, -91.5, -87.0),   "IN": (37.8, 41.8, -88.1, -84.8),
        "OH": (38.4, 42.0, -84.8, -80.5),   "MI": (41.7, 48.3, -90.5, -82.1),
        "WI": (42.5, 47.1, -92.9, -86.8),   "PA": (39.7, 42.3, -80.5, -74.7),
        "NY": (40.5, 45.1, -79.8, -71.9),   "NJ": (38.9, 41.4, -75.6, -73.9),
        "DE": (38.5, 39.9, -75.8, -75.0),   "MD": (37.9, 39.7, -79.5, -75.0),
        "DC": (38.8, 39.0, -77.2, -76.9),   "CT": (40.9, 42.1, -73.8, -71.8),
        "RI": (41.1, 42.0, -71.9, -71.1),   "MA": (41.2, 42.9, -73.5, -69.9),
        "VT": (42.7, 45.1, -73.4, -71.5),   "NH": (42.7, 45.3, -72.6, -70.6),
        "ME": (43.0, 47.5, -71.1, -66.9),
    }
    for st, (la0, la1, lo0, lo1) in ROUGH.items():
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return st
    return None


@reveal_ext_bp.route("/api/v1/carbon-intensity")
def carbon_intensity():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError) as e:
        return jsonify({"success": False, "error": f"lat, lon required: {e}"}), 400

    state = (request.args.get("state") or _state_from_latlon(lat, lon) or "").upper()
    metric = (request.args.get("metric") or "both").lower()

    data = ISO_REGIONS.get(state)
    if not data:
        return jsonify({
            "success": False,
            "error": f"No carbon-intensity data for state '{state}' at ({lat},{lon})",
            "hint": "Pass ?state=XX explicitly if the inferred state is wrong.",
        }), 404

    iso, avg, marginal, mix = data

    def _rating(v):
        if v < 400:  return "Very low carbon  \u2014  strong Scope-2 profile"
        if v < 650:  return "Low  \u2014  well below U.S. grid average"
        if v < 900:  return "Moderate  \u2014  near U.S. grid average"
        if v < 1100: return "Elevated  \u2014  fossil-heavy regional grid"
        return "High  \u2014  coal- or gas-dominant grid"

    out = {
        "success": True,
        "location": {"lat": lat, "lon": lon, "state": state},
        "iso_region": iso,
        "generation_mix_summary": mix,
        "units": "lb CO2 / MWh",
        "interpretation_note": "Marginal intensity reflects the emissions of the last unit dispatched \u2014 typically higher than average. Use marginal for incremental-load carbon accounting, average for annualized Scope 2.",
        "source": "DC Hub carbon-intensity  \u00B7  static per-state reference table "
                  "(EIA + eGRID/EPA 2024 values; not a live feed)",
    }
    if metric in ("average", "both"):
        out["average_intensity"] = {"value_lb_per_mwh": avg, "rating": _rating(avg)}
    if metric in ("marginal", "both"):
        out["marginal_intensity"] = {"value_lb_per_mwh": marginal, "rating": _rating(marginal)}

    return jsonify(out)


# ===========================================================================
# Registration
# ===========================================================================

def register_reveal_routes(app):
    app.register_blueprint(reveal_ext_bp)
    routes = [
        "GET /api/v1/reveal-cell-bulk",
        "GET /api/v1/reveal-grid-export",
        "GET /api/v1/reveal-grid-export/status/<job_id>",
        "GET /api/v1/reveal-validation-feed",
        "GET /api/v1/social-acceptance-index",
        "GET /api/v1/climate-risk",
        "GET /api/v1/carbon-intensity",
    ]
    logger.info("\U0001F6F0  reVeal extended endpoints registered:")
    for r in routes:
        logger.info("  %s", r)
    print("\U0001F6F0  reVeal endpoints: registered (7 routes)")
    for r in routes:
        print(f"  {r}")
