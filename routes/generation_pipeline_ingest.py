"""Generator-inventory ingest — EIA-860M operable generators by balancing authority.

A grid-capacity signal for data-center siting: the full inventory of operable
generators (operating + standby/reserve + returning-to-service) keyed to their
balancing authority (→ ISO), state, technology, and nameplate MW. The standby
(SB) and out-of-service-but-returning (OA) statuses are a latent-headroom signal
(reserve capacity that can come online). Generator-level + BA-keyed, so it's
additive to our plant-level power_plants_eia.

Source: EIA operating-generator-capacity v2 API (EIA-860M, monthly), latest
reported period. (EIA's v2 API has NO planned/under-construction route — the
forward queue lives in interconnect_queue — so this is the operable-inventory
snapshot, not a build pipeline.)

Pattern mirrors gas_pipeline_ingest / transmission_ingest:
  - Admin-gated (X-Admin-Key / cron secret).
  - Idempotent + NON-DESTRUCTIVE: tags rows source='eia860m' and deletes only
    its own prior rows each run; empty-replace guard (0 rows never wipes).
  - Transaction-wrapped; ?dry_run=1 fetches without writing.
  - Server-side fetch (Railway reaches api.eia.gov fine; the operable set is a
    fast paginated pull); also accepts runner-POSTed rows ({"rows":[{...}]}).
"""
import gzip
import json
import logging
import os
import urllib.parse
import urllib.request

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("generation_pipeline_ingest")
gen_pipeline_ingest_bp = Blueprint("generation_pipeline_ingest", __name__)

_SRC = "eia860m"
_EIA = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"
# Operable statuses worth tracking: OP=operating, SB=standby/reserve,
# OA=out of service but expected back within a year. (OS=gone-for-good excluded.)
_INVENTORY_STATUS = ["OP", "SB", "OA"]

_FIELDS = ["period", "plant_id", "generator_id", "plant_name", "state",
           "ba_code", "entity_name", "technology", "energy_source",
           "capacity_mw", "status", "status_desc"]


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


def _norm(r: dict) -> dict:
    """Coerce a raw EIA generator record into our row shape (width-bounded)."""
    try:
        mw = float(r.get("nameplate-capacity-mw") or 0)
    except (TypeError, ValueError):
        mw = 0.0
    return {
        "period":        (r.get("period") or "")[:7],
        "plant_id":      str(r.get("plantid") or "")[:20],
        "generator_id":  str(r.get("generatorid") or "")[:20],
        "plant_name":    (r.get("plantName") or "")[:200],
        "state":         (r.get("stateid") or "")[:2],
        "ba_code":       (r.get("balancing_authority_code") or "")[:20],
        "entity_name":   (r.get("entityName") or "")[:200],
        "technology":    (r.get("technology") or "")[:100],
        "energy_source": (r.get("energy_source_code") or "")[:20],
        "capacity_mw":   mw,
        "status":        (r.get("status") or "")[:8],
        "status_desc":   (r.get("statusDescription") or "")[:200],
    }


def _fetch(cap: int):
    """Pull the latest-period operable generators from EIA (server-side)."""
    key = os.environ.get("EIA_API_KEY") or os.environ.get("EIA_KEY") or ""
    if not key:
        return []
    rows, offset, page, latest = [], 0, 5000, None
    while len(rows) < cap and offset < 80000:
        params = [("api_key", key), ("frequency", "monthly"),
                  ("data[]", "nameplate-capacity-mw"),
                  ("sort[0][column]", "period"), ("sort[0][direction]", "desc"),
                  ("offset", str(offset)), ("length", str(page))]
        for s in _INVENTORY_STATUS:
            params.append(("facets[status][]", s))
        url = _EIA + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "dchub/1.0"}),
                timeout=60) as r:
            data = (json.loads(r.read()).get("response") or {}).get("data") or []
        if not data:
            break
        if latest is None:
            latest = data[0].get("period")
        for raw in data:
            if raw.get("period") != latest:   # sorted desc → past the snapshot
                return rows[:cap]
            rows.append(_norm(raw))
            if len(rows) >= cap:
                break
        offset += page
        if len(data) < page:
            break
    return rows[:cap]


@gen_pipeline_ingest_bp.route("/api/v1/admin/ingest/generator-inventory", methods=["POST"])
def ingest_generator_inventory():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"
    try:
        cap = min(int(request.args.get("cap", 40000)), 80000)
    except (TypeError, ValueError):
        cap = 40000

    # Runner-provided rows preferred (gzipped {"rows":[{...}]}); else server fetch.
    body_rows = None
    raw = request.get_data() or b""
    if raw:
        try:
            enc = (request.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or request.headers.get("X-Content-Gzip"):
                raw = gzip.decompress(raw)
            j = json.loads(raw)
            if isinstance(j, dict) and isinstance(j.get("rows"), list):
                body_rows = [r if _is_norm(r) else _norm(r) for r in j["rows"]]
        except Exception:
            body_rows = None

    rows = body_rows if body_rows is not None else _fetch(cap)
    if request.args.get("cap"):
        rows = rows[:cap]

    if dry:
        return jsonify(ok=True, dry_run=True, fetched=len(rows), sample=rows[:3])
    if not rows:
        # empty-replace guard: never wipe the table on a 0-row fetch
        return jsonify(ok=False, error="0 rows (source returned nothing) — skipped to avoid wiping table"), 502

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS generator_inventory (
                        id            SERIAL PRIMARY KEY,
                        period        TEXT,
                        plant_id      TEXT,
                        generator_id  TEXT,
                        plant_name    TEXT,
                        state         TEXT,
                        ba_code       TEXT,
                        entity_name   TEXT,
                        technology    TEXT,
                        energy_source TEXT,
                        capacity_mw   NUMERIC,
                        status        TEXT,
                        status_desc   TEXT,
                        source        TEXT,
                        ingested_at   TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ix_geninv_state ON generator_inventory(state)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_geninv_ba ON generator_inventory(ba_code)")
                cur.execute("DELETE FROM generator_inventory WHERE source = %s", (_SRC,))

                cols = _FIELDS + ["source"]
                collist = ",".join(cols)
                ph = "(" + ",".join(["%s"] * len(cols)) + ")"

                def _vals(r):
                    return tuple(r.get(f) for f in _FIELDS) + (_SRC,)

                batch = []
                for r in rows:
                    batch.append(_vals(r))
                    if len(batch) >= 500:
                        args = b",".join(cur.mogrify(ph, b) for b in batch)
                        cur.execute(f"INSERT INTO generator_inventory ({collist}) VALUES " + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO generator_inventory ({collist}) VALUES " + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


def _is_norm(r) -> bool:
    return isinstance(r, dict) and "plant_id" in r and "nameplate-capacity-mw" not in r




@gen_pipeline_ingest_bp.route("/api/v1/generator-inventory", methods=["GET"])
def get_generator_inventory_public():
    """Public read API for operable US generator inventory by balancing authority (EIA-860M).
    Auto-generated by brain_codegen_expose (self-tested against the live DB).
    Filters: state, ba, technology, status, min_mw, limit (max 5000)."""
    import os as _os
    import psycopg2 as _pg
    dsn = _os.environ.get("DATABASE_URL") or _os.environ.get("NEON_DATABASE_URL") or ""
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    args = request.args
    where = ["source = 'eia860m'"]
    params = []
    if args.get("state"):
        where.append("UPPER(state) = %s"); params.append(args["state"].upper()[:2])
    if args.get("ba"):
        where.append("UPPER(ba_code) = %s"); params.append(args["ba"].upper()[:20])
    if args.get("technology"):
        where.append("technology ILIKE %s"); params.append("%" + args["technology"] + "%")
    if args.get("status"):
        where.append("status ILIKE %s"); params.append("%" + args["status"] + "%")
    if args.get("min_mw"):
        try:
            where.append("capacity_mw >= %s"); params.append(float(args["min_mw"]))
        except ValueError:
            pass
    try:
        limit = min(int(args.get("limit", 1000)), 5000)
    except (TypeError, ValueError):
        limit = 1000
    sql = ("SELECT plant_name, entity_name, state, ba_code, technology, energy_source, capacity_mw, status FROM generator_inventory WHERE " + " AND ".join(where) +
           " ORDER BY capacity_mw DESC NULLS LAST LIMIT %s")
    params.append(limit)
    try:
        with _pg.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                cnames = [d[0] for d in cur.description]
                rows = [dict(zip(cnames, r)) for r in cur.fetchall()]
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    for r in rows:
        for k, v in list(r.items()):
            if v is not None and not isinstance(v, (int, float, bool, str)):
                try:
                    r[k] = float(v)
                except (TypeError, ValueError):
                    r[k] = str(v)
    resp = jsonify(ok=True, count=len(rows),
                   source="operable US generator inventory by balancing authority (EIA-860M) (DC Hub, dchub.cloud)", rows=rows)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def register_generation_pipeline_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(gen_pipeline_ingest_bp)
    except Exception as e:
        log.warning(f"generation_pipeline_ingest registration: {e}")
