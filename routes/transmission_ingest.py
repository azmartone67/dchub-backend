"""Transmission line ingest — EIA Electric Power Transmission Lines → transmission_lines.

The Land & Power "electric transmission" layer FROZE: transmission_lines held a
stale HIFLD snapshot (52,244 rows, source='HIFLD') with no refresh path. Railway's
egress to ArcGIS is unreliable, so — exactly like the proven gas-pipeline ingest —
a GitHub Actions runner (tools/infra_fetch.py) fetches the live EIA service and
POSTs compact attribute rows here, which writes Neon.

Source: EIA US_Electric_Power_Transmission_Lines (FiaPA4ga0iQKduv3 org, ~94,619
features). Attributes only (returnGeometry=false — the target table stores no
geometry). Fields: ID, TYPE, STATUS, OWNER, VOLTAGE, VOLT_CLASS, SUB_1, SUB_2,
SOURCE, VAL_DATE.

Safety:
  - Admin-gated (X-Admin-Key / X-Internal-Key).
  - Idempotent FULL-REPLACE inside ONE transaction: deletes the stale HIFLD +
    prior runner rows, then batched INSERT. A schema surprise rolls back cleanly
    so the old rows survive (no partial / empty-layer state). The 94k EIA set
    supersedes the stale 52k HIFLD → clean single-source layer.
  - Dynamic column detection (only insert columns that exist live).
  - Capped + batched (500/exec via cur.mogrify) so the 1-replica backend copes.
  - ?dry_run=1 fetches + parses without writing (verify the source safely).
  - Accepts POST body {"rows":[...]} (preferred, runner-provided) OR a server-side
    _fetch fallback.
"""
import json
import logging
import os
import urllib.parse
import urllib.request

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("transmission_ingest")
transmission_ingest_bp = Blueprint("transmission_ingest", __name__)

_SRC = "eia-arcgis-runner"
# Sources superseded by this full-replace (the stale HIFLD snapshot + any prior
# runner-tagged rows). Kept in sync with tools/infra_fetch.py / the workflow.
_DELETE_SOURCES = ("eia-arcgis-runner", "HIFLD", "hifld", "hifld-runner")
# EIA Electric Power Transmission Lines (national, ~94,619 features). Same
# reliable FiaPA4ga0iQKduv3 org the working gas ingest uses. Attributes only.
_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
        "US_Electric_Power_Transmission_Lines/FeatureServer/0/query")

# Row tuple shape (matches tools/infra_fetch.fetch_transmission_lines):
#   (hifld_id, name, operator, voltage_kv, from_sub, to_sub, status, line_type)
_ROW_FIELDS = ("hifld_id", "name", "operator", "voltage_kv",
               "from_sub", "to_sub", "status", "line_type")


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


def _voltage(v):
    """VOLTAGE comes as kV int; -999999 / negatives are 'not available' sentinels."""
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _clean(s, n):
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.upper() in ("NOT AVAILABLE", "UNKNOWN", "NULL", "NONE"):
        return None
    return s[:n]


def _fetch(cap: int):
    """Paginate the EIA transmission service → list of row tuples (see _ROW_FIELDS).

    Attributes only (returnGeometry=false). Pages of 2000 (service
    maxRecordCount). Maps OWNER→operator, VOLTAGE→voltage_kv, SUB_1/2→from/to_sub,
    OWNER (or ID)→name."""
    rows = []
    offset = 0
    page = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "ID,TYPE,STATUS,OWNER,VOLTAGE,SUB_1,SUB_2",
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        with urllib.request.urlopen(_SVC + "?" + params, timeout=55) as r:
            data = json.loads(r.read().decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            a = f.get("attributes") or {}
            hid = _clean(a.get("ID"), 64)
            owner = _clean(a.get("OWNER"), 200)
            name = owner or hid
            rows.append((
                hid,
                (name or "")[:200] if name else None,
                owner,
                _voltage(a.get("VOLTAGE")),
                _clean(a.get("SUB_1"), 200),
                _clean(a.get("SUB_2"), 200),
                _clean(a.get("STATUS"), 80),
                _clean(a.get("TYPE"), 80),
            ))
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def _coerce_body_row(r):
    """Normalize a runner-provided row (list/tuple) to the _ROW_FIELDS tuple."""
    if not isinstance(r, (list, tuple)) or len(r) < 1:
        return None
    g = lambda i: r[i] if len(r) > i else None
    return (
        _clean(g(0), 64),
        _clean(g(1), 200),
        _clean(g(2), 200),
        _voltage(g(3)),
        _clean(g(4), 200),
        _clean(g(5), 200),
        _clean(g(6), 80),
        _clean(g(7), 80),
    )


@transmission_ingest_bp.route("/api/v1/admin/ingest/transmission-lines", methods=["POST"])
def ingest_transmission_lines():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"
    try:
        cap = min(int(request.args.get("cap", 100000)), 200000)
    except (TypeError, ValueError):
        cap = 100000

    # Runner-provided rows (preferred): Railway's egress to ArcGIS is unreliable,
    # so the GitHub runner fetches + POSTs. Body: {"rows":[[hifld_id,name,operator,
    # voltage_kv,from_sub,to_sub,status,line_type],...]}, optionally gzipped.
    # Falls back to the server-side _fetch when no body is sent.
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
                body_rows = []
                for r in j["rows"]:
                    cr = _coerce_body_row(r)
                    if cr is not None:
                        body_rows.append(cr)
        except Exception:
            body_rows = None

    if body_rows is not None:
        rows = body_rows[:cap] if request.args.get("cap") else body_rows
    else:
        try:
            rows = _fetch(cap)
        except Exception as e:
            return jsonify(ok=False, error=f"source fetch failed: {str(e)[:160]}"), 502

    if dry:
        return jsonify(ok=True, dry_run=True, fetched=len(rows), sample=rows[:3])

    if not rows:
        return jsonify(ok=False, error="no rows to ingest (refused empty full-replace)"), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='transmission_lines'")
                cols = {r[0] for r in cur.fetchall()}

                # Idempotent full-replace: clear stale HIFLD + prior runner rows.
                if "source" in cols:
                    cur.execute(
                        "DELETE FROM transmission_lines WHERE source IN %s",
                        (tuple(_DELETE_SOURCES),))

                use = [col for col in _ROW_FIELDS if col in cols]
                if "source" in cols:
                    use.append("source")
                have_last_updated = "last_updated" in cols
                have_created_at = "created_at" in cols
                tail = []
                if have_last_updated:
                    tail.append("last_updated")
                if have_created_at:
                    tail.append("created_at")
                collist = ",".join(use + tail)
                ph = "(" + ",".join(["%s"] * len(use)) + \
                     "".join([",NOW()"] * len(tail)) + ")"

                def _row_vals(t):
                    m = dict(zip(_ROW_FIELDS, t))
                    m["source"] = _SRC
                    return tuple(m[col] for col in use)

                batch = []
                for t in rows:
                    batch.append(_row_vals(t))
                    if len(batch) >= 500:
                        args = b",".join(cur.mogrify(ph, b) for b in batch)
                        cur.execute(f"INSERT INTO transmission_lines ({collist}) "
                                    f"VALUES " + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO transmission_lines ({collist}) "
                                f"VALUES " + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


def register_transmission_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(transmission_ingest_bp)
    except Exception as e:
        log.warning(f"transmission_ingest registration: {e}")
