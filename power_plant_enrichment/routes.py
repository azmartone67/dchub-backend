"""
Flask Integration - Power Plant Enrichment Routes
==================================================
Drop-in Flask blueprint for your Replit backend.
Provides endpoints to trigger enrichment and check status.

Usage in your main app.py:
    from power_plant_enrichment.routes import enrichment_bp
    app.register_blueprint(enrichment_bp, url_prefix="/api/enrichment")
"""

import logging
import threading
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request

from .eia_860m import EIA860MIngester
from .nccs_featureserver import NCCSFeatureServerClient
from .matcher import PlantMatcher

logger = logging.getLogger(__name__)

enrichment_bp = Blueprint("enrichment", __name__)

# In-memory job status tracking
_jobs = {}


def require_admin_key(f):
    """API-key auth for the enrichment admin endpoints.

    r70 (2026-06-03): previously checked ONLY os.environ['ADMIN_API_KEY'] — a key
    no cron or workflow sets, so these endpoints were effectively un-authable in
    practice (every other admin surface uses DCHUB_ADMIN_KEY). Now accepts the
    platform-standard admin keys (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY) plus the
    legacy ADMIN_API_KEY for back-compat. Header X-Admin-Key or ?admin_key=."""
    @wraps(f)
    def decorated(*args, **kwargs):
        import os
        key = request.headers.get("X-Admin-Key") or request.args.get("admin_key") or ""
        expected = {k for k in (
            os.environ.get("DCHUB_ADMIN_KEY"),
            os.environ.get("DCHUB_INTERNAL_KEY"),
            os.environ.get("ADMIN_API_KEY"),
        ) if k}
        if not expected or key not in expected:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


@enrichment_bp.route("/trigger", methods=["POST"])
@require_admin_key
def trigger_enrichment():
    """
    POST /api/enrichment/trigger
    Body (optional):
        {
            "sources": ["eia_860m", "nccs"],  // default: both
            "eia_year": 2025,
            "eia_month": 10,
            "state_filter": "VA",             // optional state filter
            "dry_run": true                   // preview without DB writes
        }
    """
    params = request.get_json(silent=True) or {}
    sources = params.get("sources", ["eia_860m", "nccs"])
    dry_run = params.get("dry_run", True)

    job_id = f"enrich_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    _jobs[job_id] = {
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "params": params,
        "progress": "Starting...",
    }

    # Run in background thread
    thread = threading.Thread(
        target=_run_enrichment,
        args=(job_id, sources, params, dry_run),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "started",
        "check_status": f"/api/enrichment/status/{job_id}",
    }), 202


@enrichment_bp.route("/status/<job_id>", methods=["GET"])
@require_admin_key
def check_status(job_id):
    """GET /api/enrichment/status/<job_id>"""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@enrichment_bp.route("/nccs/query", methods=["GET"])
@require_admin_key
def nccs_query():
    """
    GET /api/enrichment/nccs/query?state=VA&min_mw=100&fuel=NG
    Quick proxy to NCCS FeatureServer for ad-hoc queries.
    """
    client = NCCSFeatureServerClient()

    state = request.args.get("state")
    min_mw = request.args.get("min_mw", type=float)
    fuel = request.args.get("fuel")
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    radius = request.args.get("radius_miles", 50, type=float)
    limit = request.args.get("limit", 100, type=int)

    try:
        if lat and lng:
            plants = client.query_by_proximity(
                lat=lat, lng=lng,
                radius_miles=radius,
                state_filter=state,
                min_capacity_mw=min_mw,
                fuel_type=fuel,
                max_records=limit,
            )
        else:
            plants = client.query_power_plants(
                state_filter=state,
                min_capacity_mw=min_mw,
                fuel_type=fuel,
                max_records=limit,
            )

        return jsonify({
            "count": len(plants),
            "plants": plants,
        })

    except Exception as e:
        logger.error(f"NCCS query failed: {e}")
        return jsonify({"error": str(e)}), 500


@enrichment_bp.route("/nccs/layers", methods=["GET"])
@require_admin_key
def nccs_layers():
    """GET /api/enrichment/nccs/layers — Show available NCCS FeatureServer layers."""
    client = NCCSFeatureServerClient()
    try:
        info = client.get_service_info()
        layers = [
            {"id": l.get("id"), "name": l.get("name")}
            for l in info.get("layers", [])
        ]
        return jsonify({"layers": layers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _run_enrichment(job_id: str, sources: list, params: dict, dry_run: bool):
    """Background enrichment pipeline."""
    # r70b: early durable breadcrumb. The success/failure logs both fire only
    # AFTER the heavy EIA-860 ingest; if the worker is OOM-/SIGKILL-ed during
    # that load (single Railway replica), neither runs and the job is invisible.
    # Emitting a STARTED record up-front makes "thread started but died mid-run"
    # distinguishable from "deploy stale / thread never started" in the feed.
    try:
        from routes.brain_evolution import log_notification as _logn
        _logn("power_plant_enrichment",
              (f"Enrichment STARTED: sources={sources} "
               f"state={params.get('state_filter') or 'all'} dry_run={dry_run}"),
              detail={"phase": "started", "sources": sources,
                      "state_filter": params.get("state_filter") or "all",
                      "dry_run": dry_run},
              severity="info")
    except Exception:
        pass
    try:
        eia_plants = []
        nccs_plants = []

        # --- Phase 1: Ingest from sources ---

        if "eia_860m" in sources:
            _jobs[job_id]["progress"] = "Downloading EIA plant data..."
            ingester = EIA860MIngester()
            try:
                use_annual = params.get("use_annual", True)
                eia_plants = ingester.ingest(
                    year=params.get("eia_year"),
                    month=params.get("eia_month"),
                    use_annual=use_annual,
                )
                _jobs[job_id]["eia_count"] = len(eia_plants)
            except Exception as e:
                logger.error(f"EIA ingestion failed: {e}")
                _jobs[job_id]["eia_error"] = str(e)

        if "nccs" in sources:
            _jobs[job_id]["progress"] = "Querying NASA NCCS FeatureServer..."
            client = NCCSFeatureServerClient()
            try:
                nccs_plants = client.query_power_plants(
                    state_filter=params.get("state_filter"),
                )
                _jobs[job_id]["nccs_count"] = len(nccs_plants)
            except Exception as e:
                logger.error(f"NCCS query failed: {e}")
                _jobs[job_id]["nccs_error"] = str(e)

        if not eia_plants and not nccs_plants:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = "No data from any source"
            # r70b: this was a SILENT return (no brain-feed log) — the third
            # invisible exit besides the per-worker /status. Log it WITH the
            # soft per-source errors so we can see WHY a source yielded nothing
            # (e.g. NCCS FeatureServer error, or a state_filter that matched 0).
            try:
                from routes.brain_evolution import log_notification as _logn
                _logn("power_plant_enrichment",
                      (f"Enrichment NO DATA: sources={sources} "
                       f"state={params.get('state_filter') or 'all'} "
                       f"(eia={_jobs[job_id].get('eia_count', 0)} "
                       f"nccs={_jobs[job_id].get('nccs_count', 0)})"),
                      detail={"phase": "no_data",
                              "eia_error": _jobs[job_id].get("eia_error"),
                              "nccs_error": _jobs[job_id].get("nccs_error"),
                              "sources": sources,
                              "state_filter": params.get("state_filter") or "all"},
                      severity="warn")
            except Exception:
                pass
            return

        # --- Phase 2: Merge sources ---

        _jobs[job_id]["progress"] = "Merging data sources..."
        matcher = PlantMatcher()
        merged = matcher.merge_sources(eia_plants, nccs_plants)
        # r70c: the EIA-860 ingester ignores state_filter (it was only wired to
        # NCCS), so a "VA" run actually merged ALL US plants — meaning a real
        # write would dump nationwide rows, not the workflow's documented
        # "small, reversible, VA-first" set. Apply the state bound HERE,
        # post-merge (EIA dicts carry a 'state' field), so state_filter
        # genuinely bounds BOTH sources. Empty filter = all states (unchanged).
        _sf = (params.get("state_filter") or "").strip().upper()
        if _sf:
            _before = len(merged)
            merged = [p for p in merged
                      if str(p.get("state") or "").strip().upper() == _sf]
            _jobs[job_id]["state_filtered"] = f"{_before}->{len(merged)} ({_sf})"
        _jobs[job_id]["merged_count"] = len(merged)

        # --- Phase 3 + 4 (r70, 2026-06-03 — owner-approved live write) ---
        # Match merged plants against the LIVE discovered_power_plants by a
        # per-plant SQL existence check (name + ~0.02deg proximity) — NOT an
        # in-memory match against the ~96k-row table — and, ONLY when not
        # dry_run, INSERT genuinely-new plants. Safety rails:
        #   • SCHEMA-ADAPTIVE: columns read from information_schema at runtime
        #     (the live table is capacity_mw/lat/lng — repo DDL is wrong; this
        #     never trusts it). Only columns that actually exist are written.
        #   • REVERSIBLE: every inserted row is tagged source='eia_nccs_enrichment'
        #     → undo with DELETE FROM discovered_power_plants WHERE source=...
        #   • IDEMPOTENT: the existence check means re-runs don't duplicate.
        #   • dry_run (the DEFAULT) previews the REAL would-write count, no writes.
        _jobs[job_id]["progress"] = "Matching against DC Hub database..."
        results = {"updates": [], "new": [], "conflicts": []}
        would_write = 0
        inserted = 0
        existing_hits = 0
        _conn = None
        try:
            import os as _os
            import psycopg2
            _db = _os.environ.get("DATABASE_URL") or _os.environ.get("NEON_DATABASE_URL")
            if not _db:
                _jobs[job_id]["match_db_error"] = "no DATABASE_URL"
            else:
                _conn = psycopg2.connect(_db, sslmode="require", connect_timeout=10)
                with _conn.cursor() as _cur:
                    _cur.execute("""SELECT column_name FROM information_schema.columns
                                    WHERE table_name = 'discovered_power_plants'""")
                    _cols = {r[0] for r in _cur.fetchall()}
                    # The live id column is TEXT, NOT NULL, no default (repo DDL
                    # claims SERIAL/int — drift). Generate a deterministic,
                    # traceable TEXT id per plant from its EIA-860 plant id
                    # (idempotent; ON CONFLICT DO NOTHING guards id collisions).
                    _need_id = (not dry_run) and ("id" in _cols)

                    # r70h: O(n+m) IN-MEMORY proximity dedup + batched insert.
                    # The old per-plant `SELECT ... abs(lat-?)<0.02` did one DB
                    # round-trip PER merged plant — fine for a state (~300) but at
                    # NATIONWIDE scale (~16k plants vs a ~16k-row table, no spatial
                    # index) it's O(n*m) round-trips and the worker times out before
                    # posting any result (the nationwide run hung 25+ min). Instead
                    # load existing (lat,lng) ONCE, bucket on a 0.02deg grid, and
                    # check each new plant vs its 3x3 neighbour buckets in memory
                    # (exact abs<0.02 — same semantics), then ONE batched INSERT.
                    import hashlib as _hl
                    _B = 0.02
                    _buckets = {}

                    def _bkey(la, ln):
                        return (int(round(la / _B)), int(round(ln / _B)))

                    def _badd(la, ln):
                        _buckets.setdefault(_bkey(la, ln), []).append((la, ln))

                    def _near(la, ln):
                        bx, by = _bkey(la, ln)
                        for dx in (-1, 0, 1):
                            for dy in (-1, 0, 1):
                                for (ela, eln) in _buckets.get((bx + dx, by + dy), ()):
                                    if abs(la - ela) < _B and abs(ln - eln) < _B:
                                        return True
                        return False

                    _cur.execute("""SELECT lat, lng FROM discovered_power_plants
                                    WHERE lat IS NOT NULL AND lng IS NOT NULL""")
                    for _r in _cur.fetchall():
                        try:
                            _badd(float(_r[0]), float(_r[1]))
                        except (TypeError, ValueError):
                            continue

                    # Fixed schema-adaptive insert column order; NULL for missing.
                    _cand = [c for c in ("name", "lat", "lng", "capacity_mw",
                                         "state", "fuel_type", "source")
                             if c in _cols]
                    _icols = (["id"] if _need_id else []) + _cand
                    _batch = []

                    for _p in merged:
                        _lat = _p.get("latitude")
                        _lng = _p.get("longitude")
                        _nm = (_p.get("name") or "").strip()
                        if _lat is None or _lng is None or not _nm:
                            continue
                        try:
                            _laf, _lnf = float(_lat), float(_lng)
                        except (TypeError, ValueError):
                            continue
                        if _near(_laf, _lnf):
                            existing_hits += 1
                            continue
                        would_write += 1
                        results["new"].append(_p)
                        _badd(_laf, _lnf)   # intra-batch dedup: later plants see this one
                        if not dry_run:
                            _vmap = {"name": _nm, "lat": _laf, "lng": _lnf,
                                     "capacity_mw": _p.get("capacity_mw"),
                                     "state": _p.get("state"),
                                     "fuel_type": _p.get("fuel") or _p.get("prime_mover"),
                                     "source": "eia_nccs_enrichment"}
                            if _need_id:
                                _pid = _p.get("eia_plant_id") or _p.get("plant_id")
                                _vmap["id"] = (f"eia860-{_pid}" if _pid else
                                               "enr-" + _hl.md5(
                                                   f"{_nm}|{_laf:.4f}|{_lnf:.4f}".encode()
                                               ).hexdigest()[:16])
                            _batch.append(tuple(_vmap.get(c) for c in _icols))

                    if not dry_run and _batch:
                        from psycopg2.extras import execute_values
                        _res = execute_values(
                            _cur,
                            f"INSERT INTO discovered_power_plants ({','.join(_icols)}) "
                            f"VALUES %s ON CONFLICT DO NOTHING RETURNING 1",
                            _batch, page_size=500, fetch=True)
                        inserted = len(_res)
                if not dry_run:
                    _conn.commit()
        except Exception as e:
            try:
                if _conn is not None:
                    _conn.rollback()
            except Exception:
                pass
            _jobs[job_id]["match_db_error"] = f"{type(e).__name__}: {str(e)[:150]}"
        finally:
            try:
                if _conn is not None:
                    _conn.close()
            except Exception:
                pass

        _jobs[job_id]["existing_matched"] = existing_hits
        _jobs[job_id]["would_write"] = would_write
        if dry_run:
            _jobs[job_id]["write_status"] = (
                f"DRY RUN — would write {would_write} new plant(s); "
                f"{existing_hits} already present. Re-run with dry_run=false to write.")
        else:
            _jobs[job_id]["write_status"] = (
                f"wrote {inserted} new plant(s) to discovered_power_plants "
                f"(source='eia_nccs_enrichment'; reversible via "
                f"DELETE WHERE source='eia_nccs_enrichment')")

        # --- Done ---

        _jobs[job_id].update({
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat(),
            "progress": "Done",
            "dry_run": dry_run,
            "db_matching": ("live — per-plant SQL existence check (co-location "
                            "within ~0.02deg, name-independent) against "
                            "discovered_power_plants; new rows tagged "
                            "source='eia_nccs_enrichment' (reversible)"),
            "effective_mode": "preview" if dry_run else "wrote",
            "results": {
                "total_enriched": len(merged),
                "already_in_db": existing_hits,
                "new_plants": would_write,
                "written": inserted,
                "conflicts": len(results["conflicts"]),
                "stats": matcher.get_stats(),
            },
            # Include preview of first few updates/new plants
            "preview": {
                "updates": results["updates"][:10],
                "new_plants": [
                    {k: v for k, v in p.items() if not k.startswith("_")}
                    for p in results["new"][:10]
                ],
                "conflicts": results["conflicts"][:5],
            },
        })

        # r70: durable result log. The _jobs dict is in-memory + per-gunicorn-
        # worker, so /status polls (which round-robin workers) can't see a job
        # created on another worker. Mirror the outcome into the DB-backed brain
        # decision feed so the enrichment is observable + verifiable across
        # workers (read it at /api/v1/brain/notifications?kind=power_plant_enrichment).
        try:
            from routes.brain_evolution import log_notification as _logn
            _mode = "preview" if dry_run else "WROTE"
            _logn(
                "power_plant_enrichment",
                (f"Enrichment {_mode}: would_write={would_write} "
                 f"already_in_db={existing_hits} inserted={inserted} "
                 f"merged={len(merged)} state={params.get('state_filter') or 'all'}"),
                detail={"dry_run": dry_run, "merged": len(merged),
                        "would_write": would_write, "inserted": inserted,
                        "already_in_db": existing_hits,
                        "state_filter": params.get("state_filter") or "all",
                        "match_db_error": _jobs[job_id].get("match_db_error")},
                severity=("win" if (not dry_run and inserted) else "info"),
            )
        except Exception:
            pass

    except Exception as e:
        logger.exception(f"Enrichment job {job_id} failed")
        _jobs[job_id].update({
            "status": "failed",
            "error": str(e),
            "completed_at": datetime.utcnow().isoformat(),
        })
        # r70: durable FAILURE log too — the success log only fires at the end of
        # the try, so an early error (EIA ingest, DB phase) was invisible in-memory.
        # Mirror failures to the brain feed so the cause is observable cross-worker.
        try:
            from routes.brain_evolution import log_notification as _logn
            _logn("power_plant_enrichment",
                  f"Enrichment FAILED: {type(e).__name__}: {str(e)[:200]}",
                  detail={"dry_run": dry_run, "error": str(e)[:400],
                          "phase": _jobs.get(job_id, {}).get("progress")},
                  severity="warn")
        except Exception:
            pass
