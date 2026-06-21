"""Planned-generators ingest — EIA-860M "Planned" sheet → planned_generators.

The forward pipeline of NEW power capacity coming online: planned, permitting,
and under-construction generators NATIONWIDE — including the non-ISO regions
(TVA, Southern Co, Arizona PS, PacifiCorp, LADWP, …) that the per-ISO
interconnection-queue feed doesn't cover. Each row has coordinates, state,
county, balancing authority, technology, nameplate MW, status, and planned
operation month/year — i.e. a plottable "where + when new power lands" layer.

Source: EIA-860M (Electric Generator Inventory, monthly) Excel "Planned" sheet
(api has no planned route; the Excel does). The Excel is 13MB so the FETCH runs
on the GitHub runner (tools/infra_fetch.py downloads + parses with openpyxl and
POSTs compact rows) — this endpoint only does fast DB writes.

Safety: admin-gated, idempotent (source='eia860m_planned', deletes own rows),
empty-replace guard, transaction-wrapped, ?dry_run=1 echoes without writing.
"""
import gzip
import json
import logging
import os

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("planned_generators_ingest")
planned_gen_ingest_bp = Blueprint("planned_generators_ingest", __name__)

_SRC = "eia860m_planned"
_FIELDS = ["entity_name", "plant_id", "plant_name", "state", "county", "ba_code",
           "generator_id", "technology", "energy_source", "capacity_mw",
           "status", "planned_month", "planned_year", "lat", "lng"]


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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@planned_gen_ingest_bp.route("/api/v1/admin/ingest/planned-generators", methods=["POST"])
def ingest_planned_generators():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"

    # Rows are provided by the GitHub runner (Excel parse): gzipped {"rows":[{...}]}.
    rows = []
    raw = request.get_data() or b""
    if raw:
        try:
            enc = (request.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or request.headers.get("X-Content-Gzip"):
                raw = gzip.decompress(raw)
            j = json.loads(raw)
            if isinstance(j, dict) and isinstance(j.get("rows"), list):
                for r in j["rows"]:
                    if isinstance(r, dict) and r.get("plant_id"):
                        rows.append({
                            "entity_name":  str(r.get("entity_name") or "")[:200],
                            "plant_id":     str(r.get("plant_id") or "")[:20],
                            "plant_name":   str(r.get("plant_name") or "")[:200],
                            "state":        str(r.get("state") or "")[:2],
                            "county":       str(r.get("county") or "")[:100],
                            "ba_code":      str(r.get("ba_code") or "")[:20],
                            "generator_id": str(r.get("generator_id") or "")[:20],
                            "technology":   str(r.get("technology") or "")[:100],
                            "energy_source": str(r.get("energy_source") or "")[:20],
                            "capacity_mw":  _num(r.get("capacity_mw")),
                            "status":       str(r.get("status") or "")[:120],
                            "planned_month": _num(r.get("planned_month")),
                            "planned_year": _num(r.get("planned_year")),
                            "lat":          _num(r.get("lat")),
                            "lng":          _num(r.get("lng")),
                        })
        except Exception as e:
            return jsonify(ok=False, error=f"bad body: {str(e)[:120]}"), 400

    if dry:
        return jsonify(ok=True, dry_run=True, received=len(rows), sample=rows[:3])
    if not rows:
        return jsonify(ok=False, error="0 rows provided (runner parses the EIA-860M Planned sheet and POSTs them) — skipped to avoid wiping table"), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS planned_generators (
                        id            SERIAL PRIMARY KEY,
                        entity_name   TEXT,
                        plant_id      TEXT,
                        plant_name    TEXT,
                        state         TEXT,
                        county        TEXT,
                        ba_code       TEXT,
                        generator_id  TEXT,
                        technology    TEXT,
                        energy_source TEXT,
                        capacity_mw   NUMERIC,
                        status        TEXT,
                        planned_month NUMERIC,
                        planned_year  NUMERIC,
                        lat           DOUBLE PRECISION,
                        lng           DOUBLE PRECISION,
                        source        TEXT,
                        ingested_at   TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ix_plangen_state ON planned_generators(state)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_plangen_ba ON planned_generators(ba_code)")
                cur.execute("DELETE FROM planned_generators WHERE source = %s", (_SRC,))

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
                        cur.execute(f"INSERT INTO planned_generators ({collist}) VALUES " + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO planned_generators ({collist}) VALUES " + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


def register_planned_generators_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(planned_gen_ingest_bp)
    except Exception as e:
        log.warning(f"planned_generators_ingest registration: {e}")
