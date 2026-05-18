"""
sources.py — Source Registry + Dashboard for all data ingestion sources.

Drop-in Flask blueprint that creates/maintains:
  - source_registry table (one row per source)
  - extraction_runs table (audit log per run)

Endpoints:
  GET  /api/v1/sources                       — list all sources with freshness
  GET  /api/v1/sources/<id>                  — detail for one source
  POST /api/v1/sources                       — admin: register new source
  POST /api/v1/sources/<id>/heartbeat        — extractor pings on each run
  GET  /api/v1/sources/dashboard             — HTML dashboard
  GET  /api/v1/sources/health                — quick health check

Heartbeat usage from any extractor (Python):
    requests.post(
        f"{BASE}/api/v1/sources/{source_id}/heartbeat",
        json={"status": "success", "rows_affected": 142, "duration_ms": 1230},
        headers={"Authorization": f"Bearer {ADMIN_SECRET}"}
    )

Or status='failure' with optional 'error' field. The dashboard auto-derives
freshness based on last_run_at + cadence_seconds.
"""

import hmac
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request


sources_bp = Blueprint("sources", __name__, url_prefix="/api/v1/sources")


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("No DATABASE_URL env var set")
    c = _pg.connect(dsn)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS source_registry (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('api', 'rss', 'html', 'pdf', 'csv', 'mcp', 'cron', 'mixed')),
    url_pattern         TEXT,
    parser              TEXT,
    target_table        TEXT,
    cadence_seconds     INTEGER NOT NULL DEFAULT 86400,
    tier                TEXT NOT NULL DEFAULT 'p1' CHECK (tier IN ('p0', 'p1', 'p2', 'p3')),
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    description         TEXT,
    notes               TEXT,
    last_run_at         TIMESTAMPTZ,
    last_success_at     TIMESTAMPTZ,
    last_failure_at     TIMESTAMPTZ,
    last_error          TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_runs          BIGINT NOT NULL DEFAULT 0,
    total_rows_ingested BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_source_registry_tier_enabled
    ON source_registry (tier, enabled);
CREATE INDEX IF NOT EXISTS ix_source_registry_freshness
    ON source_registry (last_success_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES source_registry(id) ON DELETE CASCADE,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL CHECK (status IN ('success', 'failure', 'partial', 'running')),
    rows_affected       INTEGER,
    duration_ms         INTEGER,
    error               TEXT,
    metadata            JSONB
);

CREATE INDEX IF NOT EXISTS ix_extraction_runs_source_started
    ON extraction_runs (source_id, started_at DESC);
"""


def _ensure_tables() -> None:
    if getattr(_ensure_tables, "_done", False):
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_tables._done = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Auth (only required for write ops)
# ---------------------------------------------------------------------------

def _check_auth() -> Optional[tuple]:
    expected = os.environ.get("DCHUB_ADMIN_SECRET", "dchub-admin-secret-2026")
    presented = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        presented = auth[len("Bearer "):]
    elif request.headers.get("X-Admin-Key"):
        presented = request.headers["X-Admin-Key"]
    if not presented or not hmac.compare_digest(presented, expected):
        return jsonify(error="unauthorized"), 401
    return None


# ---------------------------------------------------------------------------
# Freshness derivation
# ---------------------------------------------------------------------------

def _freshness_status(last_success: Optional[datetime], cadence_s: int) -> str:
    """Return one of: fresh, stale, dead, unknown"""
    if last_success is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    age = (now - last_success).total_seconds()
    if age <= cadence_s * 1.5:
        return "fresh"
    if age <= cadence_s * 5:
        return "stale"
    return "dead"


# ---------------------------------------------------------------------------
# GET / — list sources
# ---------------------------------------------------------------------------

# AUTO-REPAIR: duplicate route '' also in cors_proxy_routes.py:114 — review and remove one
@sources_bp.route("", methods=["GET"])
def list_sources():
    _ensure_tables()
    tier = request.args.get("tier")
    enabled = request.args.get("enabled")

    where_parts = []
    args = {}
    if tier:
        where_parts.append("tier = %(tier)s")
        args["tier"] = tier
    if enabled in ("true", "false"):
        where_parts.append("enabled = %(enabled)s")
        args["enabled"] = (enabled == "true")
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT id, name, kind, url_pattern, parser, target_table,
               cadence_seconds, tier, enabled, description,
               last_run_at, last_success_at, last_failure_at, last_error,
               consecutive_failures, total_runs, total_rows_ingested,
               created_at, updated_at
        FROM source_registry
        {where}
        ORDER BY tier, last_success_at ASC NULLS FIRST;
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        for k in ("last_run_at", "last_success_at", "last_failure_at", "created_at", "updated_at"):
            if isinstance(r.get(k), datetime):
                r[k] = r[k].isoformat()
        r["freshness"] = _freshness_status(
            datetime.fromisoformat(r["last_success_at"]) if r.get("last_success_at") else None,
            r["cadence_seconds"],
        )
    return jsonify(count=len(rows), sources=rows), 200


# ---------------------------------------------------------------------------
# GET /<id> — source detail
# ---------------------------------------------------------------------------

@sources_bp.route("/<string:source_id>", methods=["GET"])
def get_source(source_id):
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT * FROM source_registry WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="source not found"), 404
        cols = [d[0] for d in cur.description]
        source = dict(zip(cols, row))

        cur.execute(
            """SELECT id, started_at, completed_at, status, rows_affected,
                      duration_ms, error
               FROM extraction_runs
               WHERE source_id = %s
               ORDER BY started_at DESC
               LIMIT 20""",
            (source_id,),
        )
        run_cols = [d[0] for d in cur.description]
        runs = [dict(zip(run_cols, r)) for r in cur.fetchall()]

    for k in ("last_run_at", "last_success_at", "last_failure_at", "created_at", "updated_at"):
        if isinstance(source.get(k), datetime):
            source[k] = source[k].isoformat()
    for run in runs:
        for k in ("started_at", "completed_at"):
            if isinstance(run.get(k), datetime):
                run[k] = run[k].isoformat()
    source["recent_runs"] = runs
    source["freshness"] = _freshness_status(
        datetime.fromisoformat(source["last_success_at"]) if source.get("last_success_at") else None,
        source["cadence_seconds"],
    )
    return jsonify(source), 200


# ---------------------------------------------------------------------------
# POST / — register / upsert source (admin)
# ---------------------------------------------------------------------------
# AUTO-REPAIR: duplicate route '' also in cors_proxy_routes.py:114 — review and remove one

@sources_bp.route("", methods=["POST"])
def upsert_source():
    err = _check_auth()
    if err is not None:
        return err
    _ensure_tables()

    p = request.get_json(silent=True) or {}
    required = ["id", "name", "kind"]
    for k in required:
        if not p.get(k):
            return jsonify(error=f"required: {k}"), 400

    kind = p["kind"]
    if kind not in ("api", "rss", "html", "pdf", "csv", "mcp", "cron", "mixed"):
        return jsonify(error="invalid kind"), 400

    sql = """
        INSERT INTO source_registry
            (id, name, kind, url_pattern, parser, target_table,
             cadence_seconds, tier, enabled, description, notes)
        VALUES
            (%(id) ON CONFLICT DO NOTHINGs, %(name)s, %(kind)s, %(url_pattern)s, %(parser)s, %(target_table)s,
             %(cadence_seconds)s, %(tier)s, %(enabled)s, %(description)s, %(notes)s)
        ON CONFLICT (id) DO UPDATE SET
            name             = EXCLUDED.name,
            kind             = EXCLUDED.kind,
            url_pattern      = EXCLUDED.url_pattern,
            parser           = EXCLUDED.parser,
            target_table     = EXCLUDED.target_table,
            cadence_seconds  = EXCLUDED.cadence_seconds,
            tier             = EXCLUDED.tier,
            enabled          = EXCLUDED.enabled,
            description      = EXCLUDED.description,
            notes            = EXCLUDED.notes,
            updated_at       = NOW()
        RETURNING id;
    """
    payload = {
        "id": p["id"],
        "name": p["name"],
        "kind": kind,
        "url_pattern": p.get("url_pattern"),
        "parser": p.get("parser"),
        "target_table": p.get("target_table"),
        "cadence_seconds": int(p.get("cadence_seconds", 86400)),
        "tier": p.get("tier", "p1"),
        "enabled": bool(p.get("enabled", True)),
        "description": p.get("description"),
        "notes": p.get("notes"),
    }
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, payload)
            row = cur.fetchone()
            c.commit()
    except Exception as e:
        return jsonify(error=f"upsert failed: {type(e).__name__}: {e}"), 500
    return jsonify(id=row[0], status="upserted"), 200


# ---------------------------------------------------------------------------
# POST /<id>/heartbeat — extractor pings on each run
# ---------------------------------------------------------------------------

@sources_bp.route("/<string:source_id>/heartbeat", methods=["POST"])
def heartbeat(source_id):
    err = _check_auth()
    if err is not None:
        return err
    _ensure_tables()

    p = request.get_json(silent=True) or {}
    status = p.get("status", "success")
    if status not in ("success", "failure", "partial", "running"):
        return jsonify(error="invalid status"), 400

    rows_affected = p.get("rows_affected")
    duration_ms = p.get("duration_ms")
    error_text = p.get("error")
    metadata = p.get("metadata")

    try:
        with _conn() as c, c.cursor() as cur:
            # Insert run record
            cur.execute(
                """INSERT INTO extraction_runs
                       (source_id, started_at, completed_at, status,
                        rows_affected, duration_ms, error, metadata)
                   VALUES (%s, NOW() ON CONFLICT DO NOTHING - INTERVAL '1 millisecond' * COALESCE(%s, 0),
                           NOW(), %s, %s, %s, %s, %s)
                   RETURNING id""",
                (source_id, duration_ms, status, rows_affected, duration_ms, error_text,
                 _pg.extras.Json(metadata) if metadata else None) if False else
                # simpler: skip metadata if psycopg2.extras not imported
                (source_id, duration_ms, status, rows_affected, duration_ms, error_text, None),
            )
            run_id = cur.fetchone()[0]

            # Update source aggregates
            if status == "success":
                cur.execute(
                    """UPDATE source_registry SET
                          last_run_at         = NOW(),
                          last_success_at     = NOW(),
                          consecutive_failures = 0,
                          total_runs          = total_runs + 1,
                          total_rows_ingested = total_rows_ingested + COALESCE(%s, 0),
                          last_error          = NULL,
                          updated_at          = NOW()
                       WHERE id = %s""",
                    (rows_affected, source_id),
                )
            elif status == "failure":
                cur.execute(
                    """UPDATE source_registry SET
                          last_run_at         = NOW(),
                          last_failure_at     = NOW(),
                          consecutive_failures = consecutive_failures + 1,
                          total_runs          = total_runs + 1,
                          last_error          = %s,
                          updated_at          = NOW()
                       WHERE id = %s""",
                    (error_text, source_id),
                )
            else:
                cur.execute(
                    "UPDATE source_registry SET last_run_at = NOW(), updated_at = NOW() WHERE id = %s",
                    (source_id,),
                )
            c.commit()
    except Exception as e:
        return jsonify(error=f"heartbeat failed: {type(e).__name__}: {e}"), 500
    return jsonify(run_id=run_id, status="recorded"), 200


# ---------------------------------------------------------------------------
# GET /dashboard — HTML view of all sources
# AUTO-REPAIR: duplicate route '/dashboard' also in main.py:12063 — review and remove one
# ---------------------------------------------------------------------------

@sources_bp.route("/dashboard", methods=["GET"])
def dashboard():
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT id, name, kind, tier, enabled,
                      cadence_seconds, last_success_at, last_failure_at,
                      consecutive_failures, total_runs, total_rows_ingested,
                      target_table, last_error
               FROM source_registry
               ORDER BY tier, last_success_at ASC NULLS FIRST"""
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Count freshness buckets
    fresh = stale = dead = unknown = 0
    for r in rows:
        f = _freshness_status(r["last_success_at"], r["cadence_seconds"])
        r["freshness"] = f
        if f == "fresh":
            fresh += 1
        elif f == "stale":
            stale += 1
        elif f == "dead":
            dead += 1
        else:
            unknown += 1

    html = ['<!doctype html><html><head><meta charset="utf-8">',
            '<title>DC Hub — Source Registry</title>',
            '<style>',
            'body{font-family:system-ui,sans-serif;max-width:1400px;margin:20px auto;padding:0 20px;color:#222}',
            'h1{margin:0 0 5px}',
            '.kpis{display:flex;gap:16px;margin:16px 0 24px}',
            '.kpi{padding:12px 18px;border-radius:8px;flex:1;text-align:center}',
            '.kpi.fresh{background:#e6f6ec;color:#0a6b22}',
            '.kpi.stale{background:#fff4cc;color:#7a5a00}',
            '.kpi.dead{background:#fde2e2;color:#900}',
            '.kpi.unknown{background:#eee;color:#555}',
            '.kpi b{display:block;font-size:32px;line-height:1}',
            'table{width:100%;border-collapse:collapse;font-size:14px}',
            'th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}',
            'th{background:#fafafa;font-weight:600;position:sticky;top:0}',
            'tr.fresh td:first-child{border-left:4px solid #0a6b22}',
            'tr.stale td:first-child{border-left:4px solid #c89800}',
            'tr.dead td:first-child{border-left:4px solid #d23}',
            'tr.unknown td:first-child{border-left:4px solid #aaa}',
            'tr.dead td{background:#fff8f8}',
            '.tier{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}',
            '.tier-p0{background:#d23;color:white}',
            '.tier-p1{background:#0a6b22;color:white}',
            '.tier-p2{background:#666;color:white}',
            '.tier-p3{background:#aaa;color:white}',
            '.dim{color:#888;font-size:12px}',
            '.err{color:#c00;font-size:11px;font-family:monospace}',
            '</style></head><body>',
            '<h1>DC Hub — Data Source Registry</h1>',
            '<div class="dim">Real-time freshness for every ingestion source. Updated by extractor heartbeat pings.</div>',
            '<div class="kpis">',
            f'<div class="kpi fresh"><b>{fresh}</b>fresh</div>',
            f'<div class="kpi stale"><b>{stale}</b>stale</div>',
            f'<div class="kpi dead"><b>{dead}</b>dead</div>',
            f'<div class="kpi unknown"><b>{unknown}</b>never run</div>',
            '</div>',
            '<table>',
            '<thead><tr>',
            '<th>Source</th><th>Tier</th><th>Cadence</th><th>Last success</th>',
            '<th>Status</th><th>Target</th><th>Total rows</th><th>Last error</th>',
            '</tr></thead><tbody>']

    now = datetime.now(timezone.utc)
    for r in rows:
        last = r.get("last_success_at")
        age_str = "never"
        if last:
            age = (now - last).total_seconds()
            if age < 60: age_str = f"{int(age)}s ago"
            elif age < 3600: age_str = f"{int(age/60)}m ago"
            elif age < 86400: age_str = f"{age/3600:.1f}h ago"
            else: age_str = f"{age/86400:.1f}d ago"
        cadence = r["cadence_seconds"]
        if cadence < 3600: cad_str = f"{cadence//60} min"
        elif cadence < 86400: cad_str = f"{cadence//3600}h"
        else: cad_str = f"{cadence//86400}d"

        err = (r.get("last_error") or "")[:80]
        target = r.get("target_table") or ""

        html.append(f'<tr class="{r["freshness"]}">')
        html.append(f'<td><b>{r["name"]}</b><div class="dim">{r["id"]} · {r["kind"]}</div></td>')
        html.append(f'<td><span class="tier tier-{r["tier"]}">{r["tier"].upper()}</span></td>')
        html.append(f'<td>{cad_str}</td>')
        html.append(f'<td>{age_str}</td>')
        html.append(f'<td>{r["freshness"]}{" · "+str(r["consecutive_failures"])+"x fails" if r["consecutive_failures"] else ""}</td>')
        html.append(f'<td><code>{target}</code></td>')
        html.append(f'<td>{r["total_rows_ingested"] or 0:,}</td>')
        html.append(f'<td><span class="err">{err}</span></td>')
        html.append('</tr>')

    html.append('</tbody></table>')
    html.append('<div class="dim" style="margin-top:24px">DC Hub Source Registry · auto-refresh: reload page</div>')
    html.append('</body></html>')
    return "".join(html), 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# AUTO-REPAIR: duplicate route '/health' also in index_api.py:516 — review and remove one
# GET /health
# ---------------------------------------------------------------------------

@sources_bp.route("/health", methods=["GET"])
def health():
    """Per-source vital signs — the "I am alive" JSON.

    Phase YY (2026-05-16): expanded from the prior 97-byte stub
    ({status, sources_registered, most_recent_success}) to a per-source
    breakdown with status colors, age, error history. Powers /alive
    and lets any monitoring tool see what's red without a deep dive.

    Query params:
        compact=1     return only the summary block (no per-source array)
        only=red      filter per-source to status_color='red'
        sort=age      sort by age_hours desc (default: status_color then age)
    """
    _ensure_tables()
    compact   = request.args.get("compact") == "1"
    only      = (request.args.get("only") or "").lower()
    sort_by   = (request.args.get("sort") or "").lower()

    rows: list[dict] = []
    summary = {"green": 0, "yellow": 0, "red": 0, "never_ran": 0, "disabled": 0}
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT id, name, kind, tier, enabled, cadence_seconds,
                   last_run_at, last_success_at, last_failure_at,
                   consecutive_failures, total_runs, total_rows_ingested,
                   target_table
              FROM source_registry
             ORDER BY tier ASC, name ASC
        """)
        for r in cur.fetchall():
            (sid, name, kind, tier, enabled, cadence,
             last_run, last_ok, last_fail, fails, runs, rows_ingested, target) = r

            sla_hours = max(1.0, (cadence or 86400) / 3600.0)
            now = datetime.now(timezone.utc)
            age_hours = None
            if last_ok:
                age_hours = round((now - last_ok).total_seconds() / 3600.0, 1)

            # Status color resolution
            if not enabled:
                color = "gray"; summary["disabled"] += 1
            elif last_ok is None:
                color = "gray"; summary["never_ran"] += 1
            elif age_hours is not None and age_hours <= sla_hours:
                color = "green"; summary["green"] += 1
            elif age_hours is not None and age_hours <= sla_hours * 2:
                color = "yellow"; summary["yellow"] += 1
            else:
                color = "red"; summary["red"] += 1

            entry = {
                "id":                 sid,
                "name":                name,
                "kind":                kind,
                "tier":                tier,
                "enabled":             enabled,
                "sla_hours":           round(sla_hours, 1),
                "age_hours":           age_hours,
                "status_color":        color,
                "consecutive_failures": int(fails or 0),
                "total_runs":          int(runs or 0),
                "total_rows_ingested": int(rows_ingested or 0),
                "target_table":        target,
                "last_success_at":     last_ok.isoformat() if last_ok else None,
                "last_failure_at":     last_fail.isoformat() if last_fail else None,
            }
            if only and color != only:
                continue
            rows.append(entry)

    # Sort: red first, then yellow, then green; ties broken by age desc
    color_rank = {"red": 0, "yellow": 1, "gray": 2, "green": 3}
    if sort_by == "age":
        rows.sort(key=lambda x: -(x.get("age_hours") or 0))
    else:
        rows.sort(key=lambda x: (color_rank.get(x["status_color"], 9),
                                 -(x.get("age_hours") or 0)))

    most_recent = None
    if rows:
        valid_ts = [r["last_success_at"] for r in rows if r["last_success_at"]]
        if valid_ts: most_recent = max(valid_ts)

    overall_status = "ok"
    if summary["red"] > 0:
        overall_status = "degraded" if summary["red"] < 5 else "critical"
    elif summary["yellow"] > 3:
        overall_status = "degraded"

    out = {
        "status":             overall_status,
        "sources_registered": sum(summary.values()),
        "summary":            summary,
        "most_recent_success": most_recent,
        "checked_at":         datetime.now(timezone.utc).isoformat(),
    }
    if not compact:
        out["sources"] = rows
    return jsonify(out), 200
