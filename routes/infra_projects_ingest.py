"""Infrastructure PROJECT ingest — the forward pipeline of gas and transmission
build-out, as opposed to the as-built asset layers.

WHY THIS EXISTS (2026-09-22). Every federal ASSET layer DC Hub pulls is frozen
upstream: the EIA transmission-line service was last edited 2025-08-26, the EIA
gas-pipeline service 2025-07-01, HIFLD substations 2021-02-25. Re-ingesting
them weekly rewrites the same rows. New gas and transmission information now
only appears in PROJECT sources — lists of what is announced, applied for,
approved, under construction or cancelled. Two such sources are public,
machine-readable and reachable without a bot wall:

  gas_pipeline_projects  ← EIA "U.S. natural gas pipeline projects" workbook,
                           sheet "Natural Gas Pipeline Projects" (the active
                           list). EIA releases it quarterly and stamps every
                           row with its own Last Updated Date. Public domain
                           (U.S. Government work).
  transmission_projects  ← ERCOT Transmission Project and Information Tracking
                           (TPIT), the public "No Cost" workbook linked from
                           https://www.ercot.com/gridinfo/planning. ERCOT
                           republishes it several times a year plus ad-hoc
                           updates. ERCOT terms §5 allow public raw data to be
                           reproduced and redistributed in compilations.

Rejected with evidence (see the PR): ferc.gov's pending/approved pipeline
lists sit behind a Cloudflare JS challenge (cf-mitigated: challenge, 403 to a
non-browser client), so they are out of bounds; EIA-411 transmission files
stop at 2016.

THE FETCH RUNS ON THE GITHUB RUNNER (tools/infra_fetch.py, layers
'gas-pipeline-projects' and 'transmission-projects'), which POSTs gzipped rows
here. This module only validates, keys and writes.

IDENTITY AND "NEW". Each row carries a natural key (project_key) that is
stable across releases, and an upsert keeps first_seen_at from the FIRST run
that saw it. The first load for a source is stamped in_initial_load=TRUE, so a
"+N new" read off first_seen_at can exclude it: an initial load is a backfill
of what already existed, never news. Rows a later release no longer lists are
kept and marked in_latest_release=FALSE rather than deleted — a project that
leaves the active list has moved on (completed, withdrawn), which is itself
information. A status change records prev_status + status_changed_at.

Safety: admin-gated (header only), transaction-wrapped, DDL on a DIRECT
psycopg2 connection (DDL through db_utils.get_db() cursors is silently
dropped), a payload floor and a shrink floor so a truncated parse can neither
land nor mark hundreds of live projects as dropped, ?dry_run=1 echoes without
writing.
"""
import datetime
import gzip
import hmac
import json
import logging
import os
import re

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("infra_projects_ingest")
infra_projects_ingest_bp = Blueprint("infra_projects_ingest", __name__)

GAS_TABLE = "gas_pipeline_projects"
GAS_SOURCE = "eia_ng_pipeline_projects"
GAS_SOURCE_URL = "https://www.eia.gov/naturalgas/pipelines/EIA-NaturalGasPipelineProjects.xlsx"
GAS_LICENSE = ("Public domain (U.S. Government work, EIA) — "
               "https://www.eia.gov/about/copyrights_reuse.php")

TX_TABLE = "transmission_projects"
TX_SOURCE = "ercot_tpit"
TX_SOURCE_PAGE = "https://www.ercot.com/gridinfo/planning"
TX_LICENSE = ("ERCOT website terms §5: public raw data may be used, reproduced and "
              "redistributed in compilations — https://www.ercot.com/help/terms")

# A release smaller than this is a broken parse, not a quiet quarter. Measured
# 2026-09-22: EIA active sheet 137 projects, ERCOT TPIT 2,122 projects.
_MIN_ROWS = {GAS_SOURCE: 40, TX_SOURCE: 500}
# A release that lists fewer than this share of the projects currently marked
# in_latest_release is refused unless ?allow_shrink=1: otherwise one truncated
# workbook would flip hundreds of live projects to "no longer listed". EIA's
# yearly move of completed projects to its Historical sheet is well above it.
_SHRINK_FLOOR = 0.6

_GAS_DDL = f"""
    CREATE TABLE IF NOT EXISTS {GAS_TABLE} (
        id                   SERIAL PRIMARY KEY,
        source               TEXT NOT NULL,
        project_key          TEXT NOT NULL,
        project_name         TEXT,
        operator             TEXT,
        project_type         TEXT,
        status               TEXT,
        completed_date       TEXT,
        in_service_year      INTEGER,
        states               TEXT,
        beg_state            TEXT,
        end_state            TEXT,
        regions              TEXT,
        cost_musd_text       TEXT,
        cost_musd            NUMERIC,
        miles                NUMERIC,
        capacity_mmcfd       NUMERIC,
        diameter_in          TEXT,
        pipeline_type        TEXT,
        authority            TEXT,
        docket               TEXT,
        crosses_state_border TEXT,
        demand_served        TEXT,
        notes                TEXT,
        project_url          TEXT,
        source_row_updated   DATE,
        source_url           TEXT NOT NULL,
        license              TEXT NOT NULL,
        source_release       DATE,
        first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        in_initial_load      BOOLEAN NOT NULL DEFAULT FALSE,
        in_latest_release    BOOLEAN NOT NULL DEFAULT TRUE,
        prev_status          TEXT,
        status_changed_at    TIMESTAMPTZ,
        UNIQUE (source, project_key)
    )"""

_TX_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TX_TABLE} (
        id                   SERIAL PRIMARY KEY,
        source               TEXT NOT NULL,
        project_key          TEXT NOT NULL,
        iso                  TEXT,
        project_number       TEXT,
        title                TEXT,
        description          TEXT,
        status               TEXT,
        source_status        TEXT,
        source_list          TEXT,
        owner                TEXT,
        from_location        TEXT,
        to_location          TEXT,
        county_from          TEXT,
        county_to            TEXT,
        states               TEXT,
        kv                   NUMERIC,
        miles_new            NUMERIC,
        miles_rebuilt        NUMERIC,
        mva                  NUMERIC,
        projected_isd        DATE,
        actual_isd           DATE,
        rpg_number           TEXT,
        tier                 TEXT,
        comments             TEXT,
        source_url           TEXT NOT NULL,
        license              TEXT NOT NULL,
        source_release       DATE,
        first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        in_initial_load      BOOLEAN NOT NULL DEFAULT FALSE,
        in_latest_release    BOOLEAN NOT NULL DEFAULT TRUE,
        prev_status          TEXT,
        status_changed_at    TIMESTAMPTZ,
        UNIQUE (source, project_key)
    )"""

_INDEXES = {
    GAS_TABLE: (
        f"CREATE INDEX IF NOT EXISTS ix_gpp_first_seen ON {GAS_TABLE}(first_seen_at)",
        f"CREATE INDEX IF NOT EXISTS ix_gpp_status ON {GAS_TABLE}(status)",
    ),
    TX_TABLE: (
        f"CREATE INDEX IF NOT EXISTS ix_txp_first_seen ON {TX_TABLE}(first_seen_at)",
        f"CREATE INDEX IF NOT EXISTS ix_txp_status ON {TX_TABLE}(status)",
    ),
}

_GAS_FIELDS = ["project_key", "project_name", "operator", "project_type", "status",
               "completed_date", "in_service_year", "states", "beg_state", "end_state",
               "regions", "cost_musd_text", "cost_musd", "miles", "capacity_mmcfd",
               "diameter_in", "pipeline_type", "authority", "docket",
               "crosses_state_border", "demand_served", "notes", "project_url",
               "source_row_updated", "source_url", "license", "source_release"]

_TX_FIELDS = ["project_key", "iso", "project_number", "title", "description", "status",
              "source_status", "source_list", "owner", "from_location", "to_location",
              "county_from", "county_to", "states", "kv", "miles_new", "miles_rebuilt",
              "mva", "projected_isd", "actual_isd", "rpg_number", "tier", "comments",
              "source_url", "license", "source_release"]


# ── value coercion ─────────────────────────────────────────────────────────
def _s(v, n=500):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:n] or None


def _num(v):
    """A number, or None. A range like '13000-17000' is NOT a number — the raw
    text is kept alongside, so a range is never silently collapsed to one end."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None and n == int(n) else None


def _date(v):
    """ISO date string (YYYY-MM-DD…) or a date/datetime → date; else None."""
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    m = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})", str(v))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _http_url(v):
    s = _s(v, 1000)
    return s if s and re.match(r"^https?://", s, re.I) else None


def project_key_from_name(name):
    """The EIA active sheet has no id column. Its project names are unique
    within the sheet (measured 2026-09-22: 137 rows, 0 duplicate names, while
    5 dockets repeat across phases), so the key is the normalised name. A
    rename upstream therefore reads as a new project plus a de-listed one —
    visible, never a silent merge of two projects."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:200] or None


def ercot_project_key(v):
    """ERCOT Project Number, as text. Excel hands it back as 81354 or 81354.0."""
    if v is None:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s[:40] or None


# ── row normalisers (pure: tested without a DB) ────────────────────────────
def normalize_gas_rows(raw_rows):
    """Runner dicts → DB tuples keyed on project_key. Returns (rows, dup_count)."""
    out, seen, dups = [], set(), 0
    for r in raw_rows or []:
        if not isinstance(r, dict):
            continue
        key = project_key_from_name(r.get("project_name"))
        if not key:
            continue
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        cost_text = _s(r.get("cost_musd"), 60)
        out.append({
            "project_key": key,
            "project_name": _s(r.get("project_name"), 300),
            "operator": _s(r.get("operator"), 200),
            "project_type": _s(r.get("project_type"), 60),
            "status": _s(r.get("status"), 60),
            "completed_date": _s(r.get("completed_date"), 40),
            "in_service_year": _int(r.get("in_service_year")),
            "states": _s(r.get("states"), 120),
            "beg_state": _s(r.get("beg_state"), 20),
            "end_state": _s(r.get("end_state"), 20),
            "regions": _s(r.get("regions"), 200),
            "cost_musd_text": cost_text,
            "cost_musd": _num(r.get("cost_musd")),
            "miles": _num(r.get("miles")),
            "capacity_mmcfd": _num(r.get("capacity_mmcfd")),
            "diameter_in": _s(r.get("diameter_in"), 40),
            "pipeline_type": _s(r.get("pipeline_type"), 40),
            "authority": _s(r.get("authority"), 60),
            "docket": _s(r.get("docket"), 120),
            "crosses_state_border": _s(r.get("crosses_state_border"), 10),
            "demand_served": _s(r.get("demand_served"), 120),
            "notes": _s(r.get("notes"), 2000),
            "project_url": _http_url(r.get("project_url")),
            "source_row_updated": _date(r.get("source_row_updated")),
            "source_url": GAS_SOURCE_URL,
            "license": GAS_LICENSE,
            "source_release": _date(r.get("source_release")),
        })
    return out, dups


_TPIT_LISTS = ("future", "planned", "completed", "cancelled")


def _tx_status(source_list, source_status):
    """The sheet a project sits on outranks the per-row status cell: ERCOT's
    Completed sheet carries rows whose status cell still reads 'Planned'
    (measured 2026-09-22: 104 of 262), and the Cancelled sheet rows still read
    'Planned'/'Conceptual'. The raw cell is kept in source_status."""
    if source_list == "cancelled":
        return "Cancelled"
    if source_list == "completed":
        return "Completed"
    return source_status


def normalize_tx_rows(raw_rows):
    """Runner dicts → DB tuples keyed on the ERCOT project number."""
    out, seen, dups = [], set(), 0
    for r in raw_rows or []:
        if not isinstance(r, dict):
            continue
        key = ercot_project_key(r.get("project_number"))
        if not key:
            continue
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        lst = (_s(r.get("source_list"), 20) or "").lower()
        lst = lst if lst in _TPIT_LISTS else None
        raw_status = _s(r.get("source_status"), 60)
        if raw_status and raw_status.lower() == "none":
            raw_status = None
        out.append({
            "project_key": key,
            "iso": "ERCOT",
            "project_number": key,
            "title": _s(r.get("title"), 300),
            "description": _s(r.get("description"), 2000),
            "status": _tx_status(lst, raw_status),
            "source_status": raw_status,
            "source_list": lst,
            "owner": _s(r.get("owner"), 120),
            "from_location": _s(r.get("from_location"), 200),
            "to_location": _s(r.get("to_location"), 200),
            "county_from": _s(r.get("county_from"), 80),
            "county_to": _s(r.get("county_to"), 80),
            "states": "TX",
            "kv": _num(r.get("kv")),
            "miles_new": _num(r.get("miles_new")),
            "miles_rebuilt": _num(r.get("miles_rebuilt")),
            "mva": _num(r.get("mva")),
            "projected_isd": _date(r.get("projected_isd")),
            "actual_isd": _date(r.get("actual_isd")),
            "rpg_number": _s(r.get("rpg_number"), 60),
            "tier": _s(r.get("tier"), 40),
            "comments": _s(r.get("comments"), 2000),
            "source_url": _http_url(r.get("source_url")) or TX_SOURCE_PAGE,
            "license": TX_LICENSE,
            "source_release": _date(r.get("source_release")),
        })
    return out, dups


# ── the upsert ─────────────────────────────────────────────────────────────
def upsert_sql(table, fields):
    """ONE statement per batch. first_seen_at and in_initial_load are written on
    INSERT only — the ON CONFLICT branch never names them, which is what keeps
    "first seen" true across releases. Status bookkeeping reads the OLD row
    (`{table}.status`), because every SET expression in an upsert is evaluated
    against the pre-update row."""
    cols = fields + ["source", "first_seen_at", "last_seen_at",
                     "in_initial_load", "in_latest_release"]
    upd = [f"{f} = EXCLUDED.{f}" for f in fields if f != "project_key"]
    upd += [
        "last_seen_at = EXCLUDED.last_seen_at",
        "in_latest_release = TRUE",
        f"prev_status = CASE WHEN {table}.status IS DISTINCT FROM EXCLUDED.status "
        f"THEN {table}.status ELSE {table}.prev_status END",
        f"status_changed_at = CASE WHEN {table}.status IS DISTINCT FROM EXCLUDED.status "
        f"THEN EXCLUDED.last_seen_at ELSE {table}.status_changed_at END",
    ]
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
            f"ON CONFLICT (source, project_key) DO UPDATE SET {', '.join(upd)} "
            f"RETURNING (xmax = 0)")


def _write(dsn, table, ddl, fields, source, rows, allow_shrink):
    """Returns (http_status, body). `with conn` commits or rolls back but does
    not close, so the close is explicit."""
    c = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
    try:
        return _write_in(c, table, ddl, fields, source, rows, allow_shrink)
    finally:
        c.close()


def _write_in(c, table, ddl, fields, source, rows, allow_shrink):
    # Imported here, not at module scope: several suites stub `psycopg2` in
    # sys.modules, and a module-level `psycopg2.extras` import would make this
    # module's pure normalisers unimportable under them.
    from psycopg2.extras import execute_values
    with c:
        with c.cursor() as cur:
            cur.execute(ddl)
            for ix in _INDEXES[table]:
                cur.execute(ix)
            cur.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE in_latest_release) "
                        f"FROM {table} WHERE source = %s", (source,))
            existing, listed = cur.fetchone()
            if listed and len(rows) < _SHRINK_FLOOR * listed and not allow_shrink:
                return 409, {"ok": False, "error": (
                    f"release lists {len(rows)} projects but {listed} are currently "
                    f"listed — below the {_SHRINK_FLOOR:.0%} shrink floor; refusing "
                    f"(re-run with allow_shrink=1 only if the drop is real)")}
            initial = existing == 0
            cur.execute("SELECT NOW()")
            run_ts = cur.fetchone()[0]
            tuples = [tuple(r[f] for f in fields) + (source, run_ts, run_ts, initial, True)
                      for r in rows]
            res = execute_values(cur, upsert_sql(table, fields), tuples,
                                 page_size=500, fetch=True)
            inserted = sum(1 for (was_insert,) in res if was_insert)
            cur.execute(f"UPDATE {table} SET in_latest_release = FALSE "
                        f"WHERE source = %s AND in_latest_release AND last_seen_at < %s",
                        (source, run_ts))
            delisted = cur.rowcount
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE source = %s "
                        f"AND status_changed_at = %s", (source, run_ts))
            status_changes = cur.fetchone()[0]
    return 200, {"ok": True, "inserted": inserted, "updated": len(rows) - inserted,
                 "delisted": delisted, "status_changes": status_changes,
                 "initial_load": initial, "source": source, "table": table}


# ── HTTP ───────────────────────────────────────────────────────────────────
def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok():
    from util.admin_auth import accepted_admin_keys
    sent = (request.headers.get("X-Admin-Key") or "").strip()
    if not sent:
        return False
    return any(hmac.compare_digest(sent, k)
               for k in accepted_admin_keys(("DCHUB_ADMIN_KEY",), log))


def _body_rows():
    raw = request.get_data() or b""
    if not raw:
        return []
    if "gzip" in (request.headers.get("Content-Encoding") or "").lower():
        raw = gzip.decompress(raw)
    j = json.loads(raw)
    rows = j.get("rows") if isinstance(j, dict) else None
    return rows if isinstance(rows, list) else []


def _ingest(table, ddl, fields, source, normalize):
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    try:
        raw_rows = _body_rows()
    except Exception as e:  # noqa: BLE001 — a bad body is the caller's error
        return jsonify(ok=False, error=f"bad body: {str(e)[:120]}"), 400
    rows, dups = normalize(raw_rows)
    if request.args.get("dry_run") == "1":
        return jsonify(ok=True, dry_run=True, received=len(raw_rows), keyed=len(rows),
                       duplicate_keys=dups, sample=[
                           {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                            for k, v in r.items()} for r in rows[:3]])
    floor = _MIN_ROWS[source]
    if len(rows) < floor:
        return jsonify(ok=False, error=(
            f"{len(rows)} keyed rows is below the {floor}-row floor for {source} — "
            f"a broken parse, not a quiet release; nothing written")), 400
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    try:
        status, body = _write(dsn, table, ddl, fields, source, rows,
                              request.args.get("allow_shrink") == "1")
    except Exception as e:  # noqa: BLE001 — transaction rolled back by the context
        log.warning("%s ingest failed: %s", table, str(e)[:200])
        return jsonify(ok=False, error=str(e)[:200]), 500
    body["duplicate_keys"] = dups
    return jsonify(body), status


@infra_projects_ingest_bp.route("/api/v1/admin/ingest/gas-pipeline-projects",
                                methods=["POST"])
def ingest_gas_pipeline_projects():
    return _ingest(GAS_TABLE, _GAS_DDL, _GAS_FIELDS, GAS_SOURCE, normalize_gas_rows)


@infra_projects_ingest_bp.route("/api/v1/admin/ingest/transmission-projects",
                                methods=["POST"])
def ingest_transmission_projects():
    return _ingest(TX_TABLE, _TX_DDL, _TX_FIELDS, TX_SOURCE, normalize_tx_rows)


def register_infra_projects_ingest(app):
    try:
        app.register_blueprint(infra_projects_ingest_bp)
    except Exception as e:  # noqa: BLE001
        log.warning("infra_projects_ingest registration: %s", e)
