"""
data_freshness_radar.py — the site-wide staleness radar.

The problem this solves: DC Hub had no single place that knew "when did
each data domain last get fresh data." Freshness signals were scattered
(dcpi_runs, auto_press_releases.generated_at, eia_*.retrieved_at, …) and
nothing joined them — so a domain could silently rot for weeks (the
DCPI-57h silent failure was exactly this). Stale is the enemy; you can't
fight what you can't see.

This module is the single source of truth. One table —
`data_domain_freshness` — holds one row per canonical data domain
(facilities, transmission, fiber, gas, pipeline, energy_rates, dcpi,
news, transactions, substations) with: the source table it watches, the
newest record's timestamp, the row count, an SLA, and a status
(fresh / warning / breach / unknown).

`scan_domains()` walks every domain, runs `MAX(<ts>)` + `COUNT(*)` against
its source table, and upserts the row. It's deliberately defensive:
tables and timestamp columns vary across the codebase, so each domain
declares a *list* of candidate tables and candidate timestamp columns —
the scanner tries each until one works, and a missing table or unusable
column degrades that domain to `unknown` (which is itself the signal)
rather than crashing the scan.

Wiring:
  POST /api/v1/freshness/radar/scan   admin — run the scan now
  GET  /api/v1/freshness/radar        public — read the registry
  dchub_self_heal.fix_data_freshness_radar() calls scan_domains() every
    heal pull and emits `breach` / `unknown` rows into
    actionable_backend_issues, so a stale domain self-escalates through
    the Brain the same way a dead cron does.
"""

import os
import re
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request

data_freshness_radar_bp = Blueprint("data_freshness_radar", __name__)

ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()

# Canonical data domains. Each: (domain, [candidate tables], [candidate
# timestamp columns], sla_hours). The SLA is a "suspiciously quiet"
# threshold — not a contract, just "if the newest row is older than
# this, something is probably wrong." Tuned to realistic ingest cadence,
# env-overridable via DCHUB_RADAR_SLA_<DOMAIN>.
_DOMAINS = [
    ("facilities",   ["facilities"],
     ["first_seen", "created_at", "updated_at", "last_seen"],            240),
    ("transmission", ["transmission_lines", "transmission_segments", "transmission"],
     ["updated_at", "created_at", "retrieved_at"],                       720),
    ("fiber",        ["fiber_routes", "metro_dark_fiber"],
     ["updated_at", "created_at", "retrieved_at"],                       720),
    ("gas",          ["gas_pipelines"],
     ["updated_at", "created_at", "retrieved_at"],                       720),
    ("pipeline",     ["capacity_pipeline"],
     ["updated_at", "created_at", "expected_cod"],                       96),
    ("energy_rates", ["eia_electricity_rates", "eia_retail_rates"],
     ["retrieved_at", "updated_at", "created_at"],                       336),
    ("dcpi",         ["market_power_scores"],
     ["computed_at", "updated_at"],                                      48),
    ("news",         ["news"],
     ["published_date", "fetched_at", "created_at"],                     18),
    ("transactions", ["deals", "announcements"],
     ["created_at", "announced_date", "date", "updated_at"],             240),
    ("substations",  ["substations"],
     ["updated_at", "created_at", "retrieved_at"],                       720),
    # DC Hub Media: the autonomous press worker. If the daily auto-press
    # stops, the radar flags it and the Brain escalates it — the media
    # worker is now monitored the same way the data domains are, making
    # it a true peer to Brain and the ISO loops.
    ("dc_hub_media", ["auto_press_releases"],
     ["generated_at", "created_at"],                                     36),
]

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")  # defense-in-depth on table/col names


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    # Autocommit: each probe statement is independent, so a failed
    # MAX(<badcol>) can't poison the rest of the scan's transaction.
    c.autocommit = True
    return c


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key")
                    or request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return w


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS data_domain_freshness (
    domain           TEXT PRIMARY KEY,
    source_table     TEXT,
    source_ts_column TEXT,
    last_record_at   TIMESTAMPTZ,
    row_count        BIGINT,
    sla_hours        INT NOT NULL,
    age_hours        REAL,
    status           TEXT NOT NULL DEFAULT 'unknown',
    detail           TEXT,
    checked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _ensure_schema(cur):
    cur.execute(_SCHEMA_DDL)


def _sla_for(domain, default_hours):
    raw = os.environ.get(f"DCHUB_RADAR_SLA_{domain.upper()}", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return default_hours


def _first_existing_table(cur, tables):
    for t in tables:
        if not _IDENT_RE.match(t):
            continue
        try:
            cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
            if (cur.fetchone() or [None])[0]:
                return t
        except Exception:
            continue
    return None


def _max_ts_and_count(cur, table, ts_cols):
    """Return (last_record_at, ts_column_used, row_count). Tries each
    candidate timestamp column until one works; row_count is best-effort."""
    row_count = None
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")  # table from _DOMAINS, _IDENT_RE-checked
        row_count = int((cur.fetchone() or [0])[0] or 0)
    except Exception:
        pass

    for col in ts_cols:
        if not _IDENT_RE.match(col):
            continue
        try:
            # Cast to timestamptz so text-typed ISO columns work too; a
            # genuinely-incompatible column just raises and we move on.
            cur.execute(f"SELECT MAX({col}::timestamptz) FROM {table}")
            val = (cur.fetchone() or [None])[0]
            if val is not None:
                if getattr(val, "tzinfo", None) is None:
                    val = val.replace(tzinfo=timezone.utc)
                return val, col, row_count
        except Exception:
            continue
    return None, None, row_count


def _classify(age_hours, sla_hours, has_table, has_ts):
    if not has_table:
        return "unknown"
    if not has_ts or age_hours is None:
        return "unknown"
    if age_hours <= sla_hours:
        return "fresh"
    if age_hours <= sla_hours * 2:
        return "warning"
    return "breach"


def scan_domains():
    """Walk every canonical domain, measure freshness, upsert the
    registry. Returns the list of result dicts. Never raises — a domain
    that errors is recorded as `unknown` with the error in `detail`."""
    results = []
    now = datetime.now(timezone.utc)
    try:
        c = _conn()
    except Exception as e:
        return [{"domain": "_radar", "status": "unknown",
                 "detail": f"db unavailable: {str(e)[:120]}"}]
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            for domain, tables, ts_cols, default_sla in _DOMAINS:
                sla = _sla_for(domain, default_sla)
                table = _first_existing_table(cur, tables)
                last_at = ts_used = None
                row_count = None
                detail = None
                if not table:
                    detail = f"no source table (tried: {', '.join(tables)})"
                else:
                    try:
                        last_at, ts_used, row_count = _max_ts_and_count(
                            cur, table, ts_cols)
                        if ts_used is None:
                            detail = (f"table '{table}' present but no usable "
                                      f"timestamp column (tried: {', '.join(ts_cols)})")
                    except Exception as e:
                        detail = f"scan error: {str(e)[:120]}"

                age_hours = None
                if last_at is not None:
                    age_hours = round((now - last_at).total_seconds() / 3600.0, 2)

                status = _classify(age_hours, sla, bool(table), bool(ts_used))
                if status == "fresh":
                    detail = detail or f"newest row {age_hours}h old (SLA {sla}h)"
                elif status in ("warning", "breach"):
                    detail = (f"newest row {age_hours}h old — exceeds SLA {sla}h"
                              + (f" ({detail})" if detail else ""))

                row = {
                    "domain": domain, "source_table": table or tables[0],
                    "source_ts_column": ts_used, "last_record_at": last_at,
                    "row_count": row_count, "sla_hours": sla,
                    "age_hours": age_hours, "status": status, "detail": detail,
                }
                try:
                    cur.execute(
                        """INSERT INTO data_domain_freshness
                               (domain, source_table, source_ts_column,
                                last_record_at, row_count, sla_hours,
                                age_hours, status, detail, checked_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING)
                           ON CONFLICT (domain) DO UPDATE SET
                               source_table     = EXCLUDED.source_table,
                               source_ts_column = EXCLUDED.source_ts_column,
                               last_record_at   = EXCLUDED.last_record_at,
                               row_count        = EXCLUDED.row_count,
                               sla_hours        = EXCLUDED.sla_hours,
                               age_hours        = EXCLUDED.age_hours,
                               status           = EXCLUDED.status,
                               detail           = EXCLUDED.detail,
                               checked_at       = NOW()""",
                        (domain, row["source_table"], ts_used, last_at,
                         row_count, sla, age_hours, status, detail))
                except Exception as e:
                    row["detail"] = f"upsert failed: {str(e)[:100]}"
                # Serialise the timestamp for the JSON return.
                row["last_record_at"] = last_at.isoformat() if last_at else None
                results.append(row)

            # Phase GG (2026-05-15): Bundle 4 — brain heartbeat watchdog.
            # The brain learns by running hourly. If brain_meta.last_run_at
            # goes >120 min stale, the learning loop is silently broken
            # (this exact failure mode just happened — see PR #158). Flag
            # it like any other data domain so the radar surfaces it
            # automatically and self-heal can route it to a human.
            try:
                brain_row = _scan_brain_meta(cur, now)
                if brain_row:
                    results.append(brain_row)
            except Exception as e:
                results.append({"domain": "brain", "status": "unknown",
                                "detail": f"brain watchdog error: {str(e)[:120]}"})
    finally:
        try: c.close()
        except Exception: pass
    return results


def _scan_brain_meta(cur, now):
    """Brain heartbeat domain. Reads brain_meta.last_run_at and treats it
    like any other data-freshness row (upserts into data_domain_freshness).
    SLA = 120 min (2 hours), matching the hourly brain_learn cadence +
    one missed-tick grace."""
    SLA_HOURS = 2
    DOMAIN = "brain"
    try:
        cur.execute("SELECT value FROM brain_meta WHERE key = 'last_run_at'")
        row = cur.fetchone()
    except Exception as e:
        # brain_meta table missing — that's a `breach` of "the brain isn't
        # set up." Surface it loudly.
        upsert_row = {
            "domain": DOMAIN, "source_table": "brain_meta",
            "source_ts_column": "value",
            "last_record_at": None, "row_count": 0, "sla_hours": SLA_HOURS,
            "age_hours": None, "status": "unknown",
            "detail": f"brain_meta unreadable: {str(e)[:120]}",
        }
    else:
        if not row or not row[0]:
            upsert_row = {
                "domain": DOMAIN, "source_table": "brain_meta",
                "source_ts_column": "value",
                "last_record_at": None, "row_count": 0, "sla_hours": SLA_HOURS,
                "age_hours": None, "status": "unknown",
                "detail": "no last_run_at heartbeat yet — brain may be warming up",
            }
        else:
            try:
                last = datetime.fromisoformat(str(row[0]).replace('Z', '+00:00'))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age_hours = round((now - last).total_seconds() / 3600.0, 2)
                # warning = >1x SLA, breach = >2x SLA. Same shape as _classify.
                if age_hours <= SLA_HOURS:
                    status = "fresh"
                    detail = f"heartbeat {age_hours}h old (SLA {SLA_HOURS}h)"
                elif age_hours <= SLA_HOURS * 2:
                    status = "warning"
                    detail = (f"heartbeat {age_hours}h old — exceeds SLA {SLA_HOURS}h. "
                              "Brain may have missed one or two ticks.")
                else:
                    status = "breach"
                    detail = (f"heartbeat {age_hours}h old — brain is STALLED. "
                              "Check evolve-cron logs + /api/v1/brain/status.")
                upsert_row = {
                    "domain": DOMAIN, "source_table": "brain_meta",
                    "source_ts_column": "value",
                    "last_record_at": last, "row_count": 1,
                    "sla_hours": SLA_HOURS, "age_hours": age_hours,
                    "status": status, "detail": detail,
                }
            except Exception as e:
                upsert_row = {
                    "domain": DOMAIN, "source_table": "brain_meta",
                    "source_ts_column": "value",
                    "last_record_at": None, "row_count": 0,
                    "sla_hours": SLA_HOURS, "age_hours": None,
                    "status": "unknown",
                    "detail": f"could not parse heartbeat: {str(e)[:120]}",
                }

    # Upsert like other domains
    try:
        cur.execute(
            """INSERT INTO data_domain_freshness
                   (domain, source_table, source_ts_column, last_record_at,
                    row_count, sla_hours, age_hours, status, detail, checked_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING)
               ON CONFLICT (domain) DO UPDATE SET
                   source_table     = EXCLUDED.source_table,
                   source_ts_column = EXCLUDED.source_ts_column,
                   last_record_at   = EXCLUDED.last_record_at,
                   row_count        = EXCLUDED.row_count,
                   sla_hours        = EXCLUDED.sla_hours,
                   age_hours        = EXCLUDED.age_hours,
                   status           = EXCLUDED.status,
                   detail           = EXCLUDED.detail,
                   checked_at       = NOW()""",
            (upsert_row["domain"], upsert_row["source_table"],
             upsert_row["source_ts_column"], upsert_row["last_record_at"],
             upsert_row["row_count"], upsert_row["sla_hours"],
             upsert_row["age_hours"], upsert_row["status"],
             upsert_row["detail"]))
    except Exception:
        pass
    upsert_row["last_record_at"] = (
        upsert_row["last_record_at"].isoformat()
        if upsert_row["last_record_at"] else None)
    return upsert_row


def radar_snapshot():
    """Read the registry as last scanned. Returns (rows, summary)."""
    rows = []
    try:
        c = _conn()
        with c.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                """SELECT domain, source_table, source_ts_column,
                          last_record_at, row_count, sla_hours, age_hours,
                          status, detail, checked_at
                     FROM data_domain_freshness ORDER BY domain""")
            for r in cur.fetchall():
                rows.append({
                    "domain": r[0], "source_table": r[1],
                    "source_ts_column": r[2],
                    "last_record_at": r[3].isoformat() if r[3] else None,
                    "row_count": r[4], "sla_hours": r[5], "age_hours": r[6],
                    "status": r[7], "detail": r[8],
                    "checked_at": r[9].isoformat() if r[9] else None,
                })
        c.close()
    except Exception as e:
        return [], {"error": str(e)[:200]}
    summary = {
        "domains": len(rows),
        "fresh": sum(1 for r in rows if r["status"] == "fresh"),
        "warning": sum(1 for r in rows if r["status"] == "warning"),
        "breach": sum(1 for r in rows if r["status"] == "breach"),
        "unknown": sum(1 for r in rows if r["status"] == "unknown"),
    }
    summary["overall"] = (
        "all_fresh" if summary["fresh"] == summary["domains"] and summary["domains"]
        else f"{summary['breach']}_breach_{summary['warning']}_warning"
    )
    return rows, summary


@data_freshness_radar_bp.route("/api/v1/freshness/radar/scan", methods=["POST"])
@_require_admin
def radar_scan_endpoint():
    """Run the staleness scan now and return the results. Cron/admin."""
    results = scan_domains()
    breaches = [r["domain"] for r in results if r.get("status") == "breach"]
    unknowns = [r["domain"] for r in results if r.get("status") == "unknown"]
    return jsonify(
        ok=True,
        scanned=len(results),
        breaches=breaches,
        unknown=unknowns,
        results=results,
        as_of=datetime.now(timezone.utc).isoformat(),
    ), 200


@data_freshness_radar_bp.route("/api/v1/freshness/radar", methods=["GET"])
def radar_get_endpoint():
    """Public read of the freshness registry — the single source of
    truth for 'when did each data domain last get fresh data'."""
    rows, summary = radar_snapshot()
    resp = jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        summary=summary,
        domains=rows,
    )
    resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
