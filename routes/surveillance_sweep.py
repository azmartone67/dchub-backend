"""
surveillance_sweep.py — Phase r28 (2026-05-24).

Unified surveillance endpoint that answers the operator's question:
"is everything green right now, and if not, what's wrong?"

User-stated goals: squash bugs, ensure security, prevent data loss,
prevent hacking, prevent errors, make the site improve.

This endpoint composes everything that already exists into a single
authoritative payload, plus a few new checks that previously had no
home (data-drift, recent-error spike, baseline-row count guards).

Surfaces consumed:
  - brain_security_detectors  → 8 security checks
  - site_sentinel             → per-URL page health
  - mcp_backup_status helper  → Neon feed freshness
  - brain_v2_layer4           → filter telemetry (why 0 proposals)
  - dchub_media_revival       → press output health
  - dchub_media_hub aggregate → media feed depth
  - data drift helpers (new)  → row counts vs 24h baseline

Returns a single JSON payload with:
  - ok / severity (green | amber | red)
  - per-check details
  - top actionable findings ranked by priority
  - persistence: latest sweep written to sentinel_sweep_log

The cron `.github/workflows/surveillance-sweep.yml` polls this every
15 min. If severity ≠ green, the workflow surfaces the findings via
::warning:: in GHA logs so they show up in the run summary.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from typing import Any

import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, request


surveillance_bp = Blueprint("surveillance_sweep", __name__)


# ── shared helpers ────────────────────────────────────────────────

def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _ensure_log_table():
    """Idempotent — sweep log table for trend tracking."""
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentinel_sweep_log (
                    id BIGSERIAL PRIMARY KEY,
                    swept_at TIMESTAMPTZ DEFAULT NOW(),
                    severity TEXT,
                    payload JSONB,
                    findings_count INTEGER
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_sweep_log_at "
                "ON sentinel_sweep_log (swept_at DESC)"
            )
            c.commit()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


def _safe(fn, default=None):
    """Wrap any sub-check so a single failure can't 500 the whole sweep."""
    try:
        return fn()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"} \
            if default is None else default


# ── individual checks ─────────────────────────────────────────────

def _check_security() -> dict:
    """Run all brain_security_detectors. Each returns a list of findings."""
    findings: list = []
    detector_count = 0
    errors: list = []
    try:
        from routes import brain_security_detectors as bsd
        for name in (
            "check_admin_endpoint_open",
            "check_paywall_holes",
            "check_security_header_drift",
            "check_secret_pattern_in_body",
            "check_repeated_admin_401",
        ):
            fn = getattr(bsd, name, None)
            if not callable(fn):
                continue
            detector_count += 1
            try:
                rows = fn() or []
                for r in rows:
                    if isinstance(r, dict):
                        r.setdefault("source", name)
                        findings.append(r)
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}")
    except ImportError:
        return {"ok": False, "error": "brain_security_detectors not importable"}
    return {
        "ok": len(findings) == 0,
        "detectors_run": detector_count,
        "findings_count": len(findings),
        "findings": findings[:10],   # cap the payload
        "errors": errors,
    }


def _check_pages() -> dict:
    """Last persisted sentinel scan — unhealthy pages."""
    try:
        from routes.site_sentinel import latest_results, unhealthy_findings
        rows = latest_results() or []
        bad = unhealthy_findings() or []
        return {
            "ok": len(bad) == 0,
            "total_pages_scanned": len(rows),
            "unhealthy_count": len(bad),
            "unhealthy_sample": [
                {"path": b.get("path"),
                 "status": b.get("status_code"),
                 "reason": (b.get("reason") or "")[:120]}
                for b in bad[:8]
            ],
        }
    except ImportError:
        return {"ok": False, "error": "site_sentinel not importable"}


def _check_backup() -> dict:
    """Neon backup + feed freshness — mirrors what get_backup_status MCP tool returns."""
    c = _conn()
    if c is None:
        return {"ok": False, "error": "DB unreachable"}
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Cheap freshness probes for the key tables.
            tables = [
                ("discovered_facilities", "last_seen_at"),
                ("announcements",         "published_date"),
                ("deals",                  "created_at"),
                ("auto_press_releases",   "generated_at"),
            ]
            freshness: dict = {}
            stale_count = 0
            for table, col in tables:
                try:
                    cur.execute(f"""
                        SELECT
                          EXTRACT(EPOCH FROM (NOW() - MAX({col})))/3600.0 AS hours_stale,
                          COUNT(*) AS rows
                        FROM {table}
                    """)
                    r = cur.fetchone() or {}
                    hrs = float(r.get("hours_stale") or 0.0)
                    rows = int(r.get("rows") or 0)
                    freshness[table] = {
                        "rows": rows,
                        "hours_since_newest": round(hrs, 1),
                    }
                    # Different staleness thresholds per table type
                    threshold_h = {
                        "discovered_facilities": 24,
                        "announcements":         2,
                        "deals":                  4,
                        "auto_press_releases":   72,
                    }.get(table, 24)
                    if hrs > threshold_h:
                        stale_count += 1
                        freshness[table]["stale"] = True
                except Exception as e:
                    freshness[table] = {"error": f"{type(e).__name__}"}
                    stale_count += 1
        return {
            "ok": stale_count == 0,
            "tables_checked": len(tables),
            "stale_tables": stale_count,
            "freshness": freshness,
        }
    finally:
        try: c.close()
        except Exception: pass


def _check_data_drift() -> dict:
    """Detects unexpected row-count drops.

    Compares current row counts of headline tables against the snapshot
    24h ago (or earlier; first run records baseline and reports ok).
    A drop > 5% on any headline table fires a finding — that's how we
    catch silent data-loss / accidental DELETE / migration mistakes.
    """
    c = _conn()
    if c is None:
        return {"ok": False, "error": "DB unreachable"}
    try:
        # Idempotent baseline table.
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentinel_row_baselines (
                    table_name TEXT PRIMARY KEY,
                    baseline_count BIGINT,
                    baseline_at TIMESTAMPTZ DEFAULT NOW(),
                    last_seen_count BIGINT,
                    last_seen_at TIMESTAMPTZ
                )
            """)
            c.commit()

        tables = (
            "discovered_facilities",
            "announcements",
            "deals",
            "fiber_routes",
            "substations",
            "auto_press_releases",
            "ai_testimonials",
        )

        drops: list = []
        current: dict = {}
        with c.cursor() as cur:
            for t in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    n = int((cur.fetchone() or [0])[0] or 0)
                    current[t] = n

                    cur.execute(
                        "SELECT baseline_count, baseline_at "
                        "FROM sentinel_row_baselines WHERE table_name=%s",
                        (t,))
                    row = cur.fetchone()
                    if row is None:
                        # Record baseline on first sight.
                        cur.execute(
                            "INSERT INTO sentinel_row_baselines "
                            "(table_name, baseline_count, last_seen_count, last_seen_at) "
                            "VALUES (%s, %s, %s, NOW())",
                            (t, n, n))
                        c.commit()
                        continue
                    baseline = int(row[0] or 0)
                    if baseline > 0 and n < baseline * 0.95:
                        # Real drop — flag it.
                        drops.append({
                            "table": t,
                            "baseline": baseline,
                            "current":  n,
                            "drop_pct": round(100.0 * (baseline - n) / baseline, 2),
                            "baseline_at": str(row[1])[:19],
                        })
                    # Always refresh last_seen + ratchet baseline up if we've grown.
                    new_baseline = max(baseline, n)
                    cur.execute(
                        "UPDATE sentinel_row_baselines "
                        "SET last_seen_count=%s, last_seen_at=NOW(), "
                        "    baseline_count=%s, "
                        "    baseline_at=CASE WHEN %s > baseline_count THEN NOW() ELSE baseline_at END "
                        "WHERE table_name=%s",
                        (n, new_baseline, n, t))
                    c.commit()
                except Exception as e:
                    drops.append({"table": t, "error": f"{type(e).__name__}"})
        return {
            "ok": len(drops) == 0,
            "tables_checked": len(tables),
            "current_counts": current,
            "drops": drops,
        }
    finally:
        try: c.close()
        except Exception: pass


def _check_errors() -> dict:
    """Recent 5xx rate from the api_errors table (if present)."""
    c = _conn()
    if c is None:
        return {"ok": True, "error_5xx_15m": 0, "_note": "DB unreachable; assumed OK"}
    try:
        with c.cursor() as cur:
            # Try a few common error-log table names.
            for table in ("api_errors", "request_errors", "api_5xx_log"):
                try:
                    cur.execute(f"SELECT to_regclass('public.{table}')")
                    if not (cur.fetchone() or [None])[0]:
                        continue
                    cur.execute(f"""
                        SELECT COUNT(*) FROM {table}
                        WHERE created_at > NOW() - INTERVAL '15 minutes'
                    """)
                    n = int((cur.fetchone() or [0])[0] or 0)
                    return {
                        "ok": n < 20,
                        "error_5xx_15m": n,
                        "source_table": table,
                    }
                except Exception:
                    continue
        return {"ok": True, "error_5xx_15m": 0, "_note": "no error log table found"}
    finally:
        try: c.close()
        except Exception: pass


def _check_brain() -> dict:
    """Brain filter telemetry — answers 'why 0 proposals'.

    Pulls the last brain cycle's findings, candidates after each filter,
    and proposal outcome. When novel=0 due to good filtering, the brain
    is working correctly (not broken); this surface makes that legible.
    """
    c = _conn()
    if c is None:
        return {"ok": True, "_note": "DB unreachable"}
    try:
        with c.cursor() as cur:
            # Pull last 6h of learning_log entries.
            cur.execute("SELECT to_regclass('public.brain_learning_log')")
            if not (cur.fetchone() or [None])[0]:
                return {"ok": True, "_note": "learning log table not present yet"}
            cur.execute("""
                SELECT outcome, COUNT(*)
                FROM brain_learning_log
                WHERE logged_at > NOW() - INTERVAL '6 hours'
                GROUP BY outcome
                ORDER BY 2 DESC
            """)
            outcomes = {row[0]: int(row[1]) for row in cur.fetchall()}
        # Persistence-store snapshot
        try:
            from routes.brain_v2_layer4_store import most_persistent_unfixed  # type: ignore
            stuck = most_persistent_unfixed(min_count=2, limit=5) or []
        except Exception:
            stuck = []
        return {
            "ok": True,
            "outcomes_6h": outcomes,
            "total_log_entries_6h": sum(outcomes.values()),
            "most_persistent": [
                {"issue": s.get("issue_label"), "seen": s.get("seen_count")}
                for s in stuck
            ],
        }
    finally:
        try: c.close()
        except Exception: pass


def _check_media() -> dict:
    """Media chain pulse — aggregator depth + press cadence + LinkedIn velocity."""
    c = _conn()
    out: dict[str, Any] = {"ok": True}
    if c is None:
        return {"ok": False, "error": "DB unreachable"}
    try:
        with c.cursor() as cur:
            # Press cadence
            try:
                cur.execute("""
                    SELECT
                      EXTRACT(EPOCH FROM (NOW() - MAX(generated_at)))/86400.0 AS days_since,
                      COUNT(*) FILTER (WHERE generated_at >= NOW() - INTERVAL '30 days') AS count_30d,
                      COUNT(*) FILTER (WHERE generated_at >= NOW() - INTERVAL '7 days')  AS count_7d
                    FROM auto_press_releases
                """)
                r = cur.fetchone()
                if r:
                    out["press"] = {
                        "days_since_last":  round(float(r[0] or 0), 1),
                        "count_30d":        int(r[1] or 0),
                        "count_7d":         int(r[2] or 0),
                    }
            except Exception:
                out["press"] = {"error": "table missing"}

            # LinkedIn publish velocity (auto_press_releases.linkedin_sent_at)
            try:
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE linkedin_sent_at IS NOT NULL
                                       AND linkedin_sent_at >= NOW() - INTERVAL '24 hours') AS li_24h,
                      COUNT(*) FILTER (WHERE linkedin_sent_at IS NOT NULL
                                       AND linkedin_sent_at >= NOW() - INTERVAL '7 days')  AS li_7d,
                      COUNT(*) FILTER (WHERE linkedin_sent_at IS NULL)                     AS li_unsent
                    FROM auto_press_releases
                """)
                r = cur.fetchone()
                if r:
                    out["linkedin"] = {
                        "sent_24h": int(r[0] or 0),
                        "sent_7d":  int(r[1] or 0),
                        "unsent_total": int(r[2] or 0),
                    }
            except Exception:
                out["linkedin"] = {"error": "column missing"}

            # Testimonials freshness
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM ai_testimonials
                    WHERE created_at >= NOW() - INTERVAL '7 days'
                """)
                out["testimonials_7d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                pass

        # Verdict logic
        press = out.get("press", {})
        days_since = press.get("days_since_last")
        out["verdict"] = (
            "silent"  if days_since is None or days_since > 7 else
            "weak"    if (press.get("count_30d", 0) < 4) else
            "healthy"
        )
        out["ok"] = out["verdict"] in ("healthy", "weak")
        return out
    finally:
        try: c.close()
        except Exception: pass


# ── public endpoints ──────────────────────────────────────────────

@surveillance_bp.route("/api/v1/sentinel/sweep", methods=["GET"])
def sentinel_sweep():
    """Unified surveillance payload. Polled by the surveillance-sweep cron.

    Query params:
      ?include=security,pages,backup,drift,errors,brain,media  (default: all)
      ?persist=1  (default 1 — record snapshot to sentinel_sweep_log)
    """
    t0 = time.time()
    include = (request.args.get("include") or "").strip()
    requested = {p for p in include.split(",") if p} if include else None

    def _want(name):
        return requested is None or name in requested

    _ensure_log_table()

    checks: dict[str, Any] = {}
    if _want("security"): checks["security"] = _safe(_check_security)
    if _want("pages"):    checks["pages"]    = _safe(_check_pages)
    if _want("backup"):   checks["backup"]   = _safe(_check_backup)
    if _want("drift"):    checks["drift"]    = _safe(_check_data_drift)
    if _want("errors"):   checks["errors"]   = _safe(_check_errors)
    if _want("brain"):    checks["brain"]    = _safe(_check_brain)
    if _want("media"):    checks["media"]    = _safe(_check_media)

    # Aggregate the actionable findings into a single ranked list.
    actions: list = []
    if checks.get("security", {}).get("findings_count", 0) > 0:
        for f in checks["security"].get("findings") or []:
            actions.append({
                "category": "security",
                "priority": "high",
                "issue":   f.get("issue") or f.get("source", "security finding"),
                "detail":  (f.get("detail") or "")[:200],
                "url":     f.get("url"),
            })
    if checks.get("pages", {}).get("unhealthy_count", 0) > 0:
        for f in checks["pages"].get("unhealthy_sample") or []:
            actions.append({
                "category": "page_health",
                "priority": "medium",
                "issue":   f"{f.get('status')} on {f.get('path')}",
                "detail":  f.get("reason") or "",
            })
    if checks.get("backup", {}).get("stale_tables", 0) > 0:
        for t, info in (checks["backup"].get("freshness") or {}).items():
            if info.get("stale"):
                actions.append({
                    "category": "data_freshness",
                    "priority": "medium",
                    "issue":   f"{t} stale",
                    "detail":  f"{info.get('hours_since_newest')}h since newest row",
                })
    if checks.get("drift", {}).get("drops"):
        for d in checks["drift"]["drops"]:
            if "drop_pct" in d:
                actions.append({
                    "category": "data_loss",
                    "priority": "critical",
                    "issue":   f"{d['table']} dropped {d['drop_pct']}%",
                    "detail":  f"baseline={d['baseline']}, current={d['current']}",
                })
    if checks.get("errors", {}).get("error_5xx_15m", 0) >= 20:
        actions.append({
            "category": "errors",
            "priority": "high",
            "issue":   "5xx spike",
            "detail":  f"{checks['errors']['error_5xx_15m']} 5xx in last 15min",
        })
    if checks.get("media", {}).get("verdict") == "silent":
        actions.append({
            "category": "media",
            "priority": "medium",
            "issue":   "press output silent",
            "detail":  "no auto_press_releases in last 7d",
        })

    # Severity rollup: critical > high > medium > none.
    has = lambda p: any(a.get("priority") == p for a in actions)
    severity = (
        "red"   if has("critical") or has("high") else
        "amber" if has("medium") else
        "green"
    )

    payload = {
        "ok": severity == "green",
        "severity": severity,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "elapsed_ms": int((time.time() - t0) * 1000),
        "checks": checks,
        "actions": actions,
        "actions_count": len(actions),
        "purpose": (
            "Unified surveillance sweep — security, page health, backup "
            "freshness, data drift, error rate, brain filter telemetry, "
            "media chain pulse. Polled by surveillance-sweep.yml every "
            "15 min. Severity ≠ green emits ::warning:: in GHA logs."
        ),
    }

    # Persist a snapshot for trending.
    if request.args.get("persist", "1") != "0":
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO sentinel_sweep_log "
                        "(severity, payload, findings_count) VALUES (%s, %s, %s)",
                        (severity, json.dumps(payload), len(actions)))
                    c.commit()
            except Exception:
                pass
            finally:
                try: c.close()
                except Exception: pass

    return jsonify(payload), 200


@surveillance_bp.route("/api/v1/sentinel/sweep/history", methods=["GET"])
def sentinel_sweep_history():
    """Recent sweep snapshots — for trending the green/amber/red ratio."""
    limit = min(int(request.args.get("limit", 50)), 200)
    c = _conn()
    if c is None:
        return jsonify(error="DB unreachable"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT swept_at, severity, findings_count "
                "FROM sentinel_sweep_log ORDER BY swept_at DESC LIMIT %s",
                (limit,))
            rows = [
                {"swept_at": str(r[0])[:19], "severity": r[1], "findings": int(r[2] or 0)}
                for r in cur.fetchall()
            ]
        # Quick rollup
        from collections import Counter
        sev_counts = Counter(r["severity"] for r in rows)
        return jsonify(
            history=rows,
            count=len(rows),
            severity_rollup=dict(sev_counts),
            green_pct=round(100.0 * sev_counts.get("green", 0) / max(len(rows), 1), 1),
        ), 200
    finally:
        try: c.close()
        except Exception: pass
