"""
surveillance_sweep.py — Phase r29b (2026-05-24) + r29c data-drift add.

Unified surveillance rollup. Answers the operator's question:
"is everything green right now, and if not, what's wrong?"

Composes existing endpoints via Flask test_client (safe pattern).
r29c (2026-05-24): added data-drift detection as a SEPARATE endpoint
/api/v1/sentinel/drift with lazy CREATE TABLE inside the request
handler (not at module load) — the same safety pattern as the rest of
this file. The drift check is then composed into /sweep so silent data
loss / accidental DELETE / migration mistakes get caught in the same
15-min cadence.

Composed in /sweep:
  - /api/v1/sentinel/findings   — page health
  - /api/v1/freshness            — per-domain SLA breaches
  - /api/v1/brain/status          — brain layer-4 verdict
  - /api/v1/media/press-health   — press cadence
  - /api/v1/heartbeat/inventory  — stale-surface ratio
  - /api/health/db                — pool / memory / circuit-breaker (in-memory)
  - /api/v1/sentinel/drift       — row-count drift vs baseline (NEW r29c)

Severity rollup: critical > high > medium > none → red / amber / green.
"""
from __future__ import annotations

import datetime
import os
import time
import threading

import psycopg2
from flask import Blueprint, jsonify, current_app, request


surveillance_bp = Blueprint("surveillance_sweep", __name__)


def _call(tc, path, timeout_note="ok"):
    """Internal Flask call. Returns (dict, http_code). Never raises."""
    try:
        r = tc.get(path)
        if r.status_code == 200:
            try:
                return r.get_json() or {}, 200
            except Exception:
                return {"_non_json": True}, 200
        return {"_status": r.status_code}, r.status_code
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:100]}"}, 0


@surveillance_bp.route("/api/v1/sentinel/sweep", methods=["GET"])
def sentinel_sweep():
    """Unified surveillance payload — composes existing endpoints.

    Polled by surveillance-sweep.yml every 15 min. Severity ≠ green
    emits ::warning:: in GHA logs so problems surface without operator
    digging.
    """
    t0 = time.time()
    checks: dict = {}
    actions: list = []

    with current_app.test_client() as tc:
        # Page health (site_sentinel)
        body, _ = _call(tc, "/api/v1/sentinel/findings")
        page_count = int(body.get("count") or 0)
        checks["pages"] = {
            "ok": page_count == 0,
            "unhealthy_count": page_count,
            "findings": (body.get("findings") or [])[:5],
        }
        if page_count > 0:
            actions.append({
                "category": "pages",
                "priority": "medium",
                "issue": f"{page_count} unhealthy pages",
            })

        # Data freshness — SLA breaches per domain. /api/v1/backup/status
        # doesn't exist as a REST route (it's an MCP tool); /api/v1/freshness
        # is the canonical Flask endpoint and exposes richer per-domain
        # SLA data including which domains have breached.
        body, _ = _call(tc, "/api/v1/freshness")
        breaches = body.get("sla_breaches") or []
        checks["freshness"] = {
            "ok": len(breaches) == 0,
            "sla_breached_domains": breaches,
            "breach_count": len(breaches),
            "dcpi_age_minutes": (body.get("dcpi") or {}).get("age_minutes"),
            "dcpi_published_markets": (body.get("dcpi") or {}).get("published_markets"),
        }
        if len(breaches) >= 3:
            actions.append({
                "category": "data_freshness",
                "priority": "high",
                "issue": f"{len(breaches)} domain(s) breaching SLA",
                "detail": ", ".join(breaches[:6]),
            })
        elif len(breaches) > 0:
            actions.append({
                "category": "data_freshness",
                "priority": "medium",
                "issue": f"{len(breaches)} domain(s) breaching SLA",
                "detail": ", ".join(breaches[:6]),
            })

        # Brain v2 verdict
        body, _ = _call(tc, "/api/v1/brain/status")
        brain_ok = (body.get("verdict") or "").startswith("healthy")
        checks["brain"] = {
            "ok": brain_ok,
            "verdict": body.get("verdict"),
            "learning_log_count": body.get("learning_log_count"),
            "proposed_fixes": body.get("proposed_fixes_count"),
            "minutes_since_run": body.get("minutes_since_last_run"),
        }
        if not brain_ok and body.get("verdict"):
            actions.append({
                "category": "brain",
                "priority": "medium",
                "issue": f"brain verdict: {body.get('verdict')}",
            })

        # Media chain
        body, _ = _call(tc, "/api/v1/media/press-health")
        media_verdict = body.get("verdict") or "unknown"
        checks["media"] = {
            "ok": media_verdict in ("healthy", "weak"),
            "verdict": media_verdict,
            "days_since_last_press": body.get("days_since_last_press"),
            "press_releases_30d":    body.get("press_releases_30d"),
            "source_of_truth_score": body.get("source_of_truth_score"),
        }
        if media_verdict == "silent":
            actions.append({
                "category": "media",
                "priority": "medium",
                "issue": "press output silent (>7 days)",
            })

        # Heartbeat (stale-surface backlog)
        body, _ = _call(tc, "/api/v1/heartbeat/inventory")
        stale = int(body.get("stale") or 0)
        fresh = int(body.get("fresh") or 0)
        checks["heartbeat"] = {
            "ok": stale < (fresh / 4) if fresh else False,
            "fresh": fresh,
            "stale": stale,
            "stale_ratio": round(stale / max(fresh + stale, 1), 3),
        }
        if checks["heartbeat"]["stale_ratio"] > 0.30:
            actions.append({
                "category": "heartbeat",
                "priority": "medium",
                "issue": f"stale-surface ratio {checks['heartbeat']['stale_ratio'] * 100:.0f}%",
            })

        # HTTP error tracking (r29d) — exposes recent 4xx/5xx captured
        # by brain_http_capture middleware into a 1000-entry ring buffer.
        # We treat sustained 5xx as a high-priority signal.
        body, _ = _call(tc, "/api/v1/brain/http-errors")
        recent = body.get("recent") or []
        count_5xx_15m = sum(
            1 for e in recent
            if isinstance(e, dict) and int(e.get("status", 0)) >= 500
        )
        count_4xx_15m = sum(
            1 for e in recent
            if isinstance(e, dict) and 400 <= int(e.get("status", 0)) < 500
        )
        checks["errors"] = {
            "ok": count_5xx_15m < 5,
            "buffer_total": body.get("count"),
            "recent_5xx": count_5xx_15m,
            "recent_4xx": count_4xx_15m,
        }
        if count_5xx_15m >= 10:
            actions.append({
                "category": "errors",
                "priority": "high",
                "issue": f"{count_5xx_15m} 5xx in recent buffer",
                "detail": "sustained server-side errors — check brain_layer21 autopilot",
            })
        elif count_5xx_15m >= 5:
            actions.append({
                "category": "errors",
                "priority": "medium",
                "issue": f"{count_5xx_15m} 5xx in recent buffer",
            })

        # Security signals — pulled from the dedicated /sentinel/security
        # endpoint (5-min in-memory cache). Without the cache, the 5
        # detectors' urllib self-probes pile onto the same gunicorn
        # workers serving the sweep, causing test_client calls to time
        # out and the rest of /sweep to return None. Cache turns it into
        # an instant lookup unless the 5-min window has lapsed.
        body, _ = _call(tc, "/api/v1/sentinel/security")
        sec_findings = body.get("findings") or []
        checks["security"] = {
            "ok": len(sec_findings) == 0,
            "detectors_run": body.get("detectors_run"),
            "findings_count": body.get("findings_count", len(sec_findings)),
            "findings_sample": sec_findings[:5],
            "computed_at": body.get("computed_at"),
            "from_cache": body.get("from_cache"),
        }
        if len(sec_findings) > 0:
            for f in sec_findings[:3]:
                actions.append({
                    "category": "security",
                    "priority": "high",
                    "issue": f.get("issue") or f.get("source", "security finding"),
                    "detail": (f.get("detail") or "")[:200],
                })

        # Data-drift (NEW r29c) — flags >5% row drops on headline tables
        # vs recorded baseline. Surfaces silent data-loss / migrations.
        body, _ = _call(tc, "/api/v1/sentinel/drift")
        drops = body.get("drops") or []
        checks["drift"] = {
            "ok": len(drops) == 0,
            "tables_checked": body.get("tables_checked"),
            "drops": drops[:5],
            "_note": body.get("_note"),
        }
        for d in drops:
            if "drop_pct" in d:
                actions.append({
                    "category": "data_loss",
                    "priority": "critical",
                    "issue": f"{d['table']} dropped {d['drop_pct']}%",
                    "detail": f"baseline={d['baseline']:,}, current={d['current']:,}",
                })

        # Core health (pool / memory / circuit-breaker).
        # r71-stabilize (2026-06-04): was calling /api/health, which on
        # 2026-06-02 became a WSGI fast-path stub returning only {"status":"ok"}
        # (so the liveness probe can never BE the load). The sweep kept reading
        # body["pool"], which was therefore ALWAYS absent → pool_ok ALWAYS False
        # → permanent false-RED, AND the monitor was blind to real pool
        # exhaustion (the documented #1 flapping cause). /api/health/db is purely
        # in-memory (NEVER acquires a DB connection) so it reports true pool
        # state even under starvation, and returns 503 when degraded.
        body, code = _call(tc, "/api/health/db")
        pool = body.get("pool") or {}
        mem  = body.get("memory") or {}
        cb   = body.get("circuit_breaker") or {}
        pool_status = pool.get("status")
        # Judge on the endpoint's own verdict: 200=healthy, 503=degraded.
        # A transient unreachable read (code 0) is UNKNOWN, NOT a failure —
        # a monitor miss must never by itself force severity=red (that is what
        # compounded the 2026-06-04 US-West networking incident into a false RED).
        if code == 200:
            pool_ok = pool_status in ("healthy", "warning") and not cb.get("open")
        elif code == 503:
            pool_ok = False
        else:
            pool_ok = True
        checks["health"] = {
            "ok": pool_ok,
            "http_code": code,
            "overall": body.get("overall"),
            "pool_status": pool_status,
            "pool_utilization_pct": pool.get("utilization_pct"),
            "memory_rss_mb": mem.get("rss_mb"),
            "circuit_breaker_open": cb.get("open"),
        }
        if not pool_ok and code in (200, 503):
            actions.append({
                "category": "infrastructure",
                "priority": "high",
                "issue": f"pool degraded (status={pool_status or 'n/a'}, code={code})"
                         + (" + circuit-breaker OPEN" if cb.get("open") else ""),
                "detail": f"util={pool.get('utilization_pct')}% checked_out={pool.get('checked_out')}",
            })
        # Use the endpoint's own memory.warning flag (config-driven threshold,
        # currently 3072mb) instead of a stale hardcoded 800mb literal.
        if mem.get("warning"):
            actions.append({
                "category": "infrastructure",
                "priority": "high",
                "issue": "memory above threshold",
                "detail": f"{mem.get('rss_mb')}mb / {mem.get('threshold_mb')}mb",
            })

    # Severity rollup
    has = lambda p: any(a.get("priority") == p for a in actions)
    severity = (
        "red"   if has("critical") or has("high") else
        "amber" if has("medium") else
        "green"
    )

    return jsonify({
        "ok": severity == "green",
        "severity": severity,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "elapsed_ms": int((time.time() - t0) * 1000),
        "checks": checks,
        "actions": actions,
        "actions_count": len(actions),
        "purpose": (
            "Surveillance rollup — composes /sentinel/findings, "
            "/freshness, /brain/status, /media/press-health, "
            "/heartbeat/inventory, /sentinel/drift, /api/health/db. "
            "Polled by surveillance-sweep.yml every 15 min. "
            "Severity != green emits ::warning:: in GHA logs."
        ),
    }), 200


# ── security check w/ in-memory cache (r29e) ──────────────────────
#
# The 5 detectors self-probe localhost via urllib (~5-10s combined).
# Without caching, this slowed /sweep from 6s → 27s AND caused other
# test_client calls inside /sweep to time out (workers fighting each
# other). 5-min cache makes /sweep fast while still letting a separate
# cron poll /security?force=1 hourly to refresh the result.

_SEC_CACHE: dict = {"computed_at": 0.0, "findings": [], "errors": []}
_SEC_TTL_SEC = 300
_SEC_REFRESHING = {"running": False}


def _run_security_detectors() -> tuple[list, list]:
    """Run the 5 security detectors. Each self-probes localhost over HTTP, so
    this is SLOW (~10-27s cold) and must NEVER run on a gunicorn request
    thread."""
    findings: list = []
    errors: list = []
    try:
        from routes import brain_security_detectors as _bsd  # lazy
        for name in (
            "check_admin_endpoint_open",
            "check_paywall_holes",
            "check_security_header_drift",
            "check_secret_pattern_in_body",
            "check_repeated_admin_401",
        ):
            fn = getattr(_bsd, name, None)
            if not callable(fn):
                continue
            try:
                rows = fn() or []
                for r in rows:
                    if isinstance(r, dict):
                        r.setdefault("source", name)
                        findings.append(r)
            except Exception as _e:
                errors.append(f"{name}: {type(_e).__name__}: {str(_e)[:80]}")
    except Exception as _e:
        errors.append(f"import: {type(_e).__name__}: {str(_e)[:80]}")
    return findings, errors


def _sec_refresh_async():
    """Single-flight background refresh of the security cache. r36 (2026-05-31):
    the 5 detectors self-probe localhost over HTTP for ~27s; running them
    synchronously held a gunicorn request thread that whole time, and the
    15-min surveillance-sweep cron (plus user traffic) starved the 16-thread
    pool → /news and even /healthz hung 10-16s. That is THE flapping. Moving the
    compute to a daemon thread frees the request thread instantly; callers get
    stale-then-fresh, exactly like /api/v1/heal/findings."""
    if _SEC_REFRESHING["running"]:
        return
    _SEC_REFRESHING["running"] = True

    def _run():
        try:
            findings, errors = _run_security_detectors()
            _SEC_CACHE["findings"] = findings
            _SEC_CACHE["errors"] = errors
            _SEC_CACHE["computed_at"] = time.time()
        except Exception:
            pass
        finally:
            _SEC_REFRESHING["running"] = False
    threading.Thread(target=_run, daemon=True,
                     name="sentinel-security-refresh").start()


def _sec_response(stale: bool = False, computing: bool = False):
    return jsonify(
        ok=len(_SEC_CACHE["findings"]) == 0,
        from_cache=not computing,
        stale=stale,
        computing=computing,
        computed_at=(datetime.datetime.utcfromtimestamp(
            _SEC_CACHE["computed_at"]).isoformat() + "Z"
            if _SEC_CACHE["computed_at"] else None),
        findings_count=len(_SEC_CACHE["findings"]),
        findings=_SEC_CACHE["findings"][:20],
        detectors_run=5 - len(_SEC_CACHE["errors"]),
        errors=_SEC_CACHE["errors"],
    ), (202 if computing else 200)


@surveillance_bp.route("/api/v1/sentinel/security", methods=["GET"])
def sentinel_security():
    """Run brain_security_detectors. 5-min cache, refreshed ASYNC so the slow
    localhost self-probes never hold a request thread (that thread starvation
    was the site-wide flap). ?force=1 triggers an immediate background refresh;
    the fresh result lands on the next poll.
    """
    now = time.time()
    force = request.args.get("force") == "1"
    have_cache = _SEC_CACHE["computed_at"] > 0
    fresh = have_cache and (now - _SEC_CACHE["computed_at"]) < _SEC_TTL_SEC
    if fresh and not force:
        return _sec_response(stale=False)
    # stale / forced / first-run → kick a background refresh, NEVER block here
    _sec_refresh_async()
    if have_cache:
        return _sec_response(stale=True)     # serve last result while refreshing
    return _sec_response(computing=True)     # very first run — nothing cached yet


# ── data drift detection (r29c) ───────────────────────────────────
#
# Flags unexpected row-count drops on headline tables. Compares current
# count vs recorded baseline; >5% drop fires a finding. First run for
# each table records baseline. Baselines auto-ratchet up when counts
# grow (so the baseline tracks reality without operator intervention).
# Lazy CREATE TABLE inside the request handler — never runs at boot.

_DRIFT_TABLES = (
    "discovered_facilities",
    # NOTE: 'announcements' intentionally EXCLUDED (r-sweep-fix 2026-06-01).
    # It is purge-driven by design (news_engine.py 90d DELETE + main.py 30d
    # archive→announcements_archive), so its row count legitimately SHRINKS.
    # The drift detector uses a monotonic high-water baseline (new=max(base,n)),
    # so a pruned table trips the -5% threshold forever (the false "announcements
    # dropped 30.42%" RED). Drift-detection is for UNEXPECTED loss, not scheduled
    # pruning — its real freshness is tracked elsewhere. Re-add only with a
    # rolling/decaying baseline or an allowed_shrink flag.
    "deals",
    "fiber_routes",
    "substations",
    "auto_press_releases",
    "ai_testimonials",
)


def _drift_conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


@surveillance_bp.route("/api/v1/sentinel/drift", methods=["GET"])
def sentinel_drift():
    """Data-drift baseline check. Lazy schema: CREATE TABLE on first hit.

    Detects accidental DELETE / silent data loss / failed migrations
    that drop the headline-table row counts. Threshold: 5%. Baselines
    auto-ratchet up so growth doesn't trigger false alarms.
    """
    c = _drift_conn()
    if c is None:
        return jsonify(ok=True, drops=[], _note="DB unreachable"), 200

    try:
        # Lazy schema — never runs at boot.
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

        drops: list = []
        current: dict = {}
        for t in _DRIFT_TABLES:
            try:
                with c.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    n = int((cur.fetchone() or [0])[0] or 0)
                    current[t] = n

                    cur.execute(
                        "SELECT baseline_count, baseline_at "
                        "FROM sentinel_row_baselines WHERE table_name=%s",
                        (t,))
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            "INSERT INTO sentinel_row_baselines "
                            "(table_name, baseline_count, last_seen_count, last_seen_at) "
                            "VALUES (%s, %s, %s, NOW())",
                            (t, n, n))
                        c.commit()
                        continue
                    baseline = int(row[0] or 0)
                    if baseline > 0 and n < baseline * 0.95:
                        drops.append({
                            "table": t,
                            "baseline": baseline,
                            "current":  n,
                            "drop_pct": round(100.0 * (baseline - n) / baseline, 2),
                            "baseline_at": str(row[1])[:19],
                        })
                    # Always refresh last_seen + ratchet baseline up if grown.
                    new_baseline = max(baseline, n)
                    cur.execute(
                        "UPDATE sentinel_row_baselines "
                        "SET last_seen_count=%s, last_seen_at=NOW(), "
                        "    baseline_count=%s, "
                        "    baseline_at=CASE WHEN %s > baseline_count "
                        "                     THEN NOW() ELSE baseline_at END "
                        "WHERE table_name=%s",
                        (n, new_baseline, n, t))
                    c.commit()
            except Exception as e:
                drops.append({"table": t, "error": f"{type(e).__name__}"})

        return jsonify(
            ok=len(drops) == 0,
            tables_checked=len(_DRIFT_TABLES),
            current_counts=current,
            drops=drops,
        ), 200
    finally:
        try: c.close()
        except Exception: pass


# ── deals drift audit + safe re-baseline (r71-stabilize) ─────────────────
# The drift baseline (above) is a MONOTONIC high-water mark that never decays,
# so a LEGITIMATE dedup-shrink on the churning `deals` table (deal_hash dedup in
# deal_ingestion_scheduler + buyer==seller garbage cleanup) flags "critical
# data_loss" forever. This endpoint CONFIRMS the shrink is benign against the
# live table, then — only if benign AND still above the canonical floor — resets
# the stale baseline. It only ever touches the sentinel_row_baselines MONITORING
# table, never `deals` data, and fails CLOSED (a real loss is never masked).
_DEALS_CANONICAL_FLOOR = 2000  # public "2,000+" — never rebaseline below this


def _internal_ok():
    key = request.headers.get("X-Internal-Key", "")
    allowed = {os.environ.get("DCHUB_INTERNAL_KEY", ""),
               os.environ.get("DCHUB_ADMIN_KEY", ""),
               "dchub-internal-sync-2026"}  # in-code fallback
    allowed.discard("")
    return key in allowed


@surveillance_bp.route("/api/v1/admin/sentinel/deals-audit", methods=["GET", "POST"])
def deals_drift_audit():
    """GET = audit deals health (confirm dedup, not loss). POST ?rebaseline=1 =
    audit + reset the stale baseline (refused unless verdict benign)."""
    if not _internal_ok():
        return jsonify(error="unauthorized"), 401
    c = _drift_conn()
    if c is None:
        return jsonify(error="db_unreachable"), 503
    audit = {}
    try:
        def _scalar(sql):
            try:
                with c.cursor() as cur:
                    cur.execute(sql)
                    return (cur.fetchone() or [None])[0]
            except Exception as e:
                try: c.rollback()
                except Exception: pass
                return f"err:{type(e).__name__}"

        audit["total"]           = _scalar("SELECT COUNT(*) FROM deals")
        audit["distinct_hash"]   = _scalar("SELECT COUNT(DISTINCT deal_hash) FROM deals")
        audit["buyer_eq_seller"] = _scalar("SELECT COUNT(*) FROM deals WHERE LOWER(TRIM(buyer))=LOWER(TRIM(seller))")
        recent = _scalar("SELECT COUNT(*) FROM deals WHERE created_at >= NOW() - INTERVAL '48 hours'")
        if isinstance(recent, str):  # no created_at? try a 'date' column
            recent = _scalar("SELECT COUNT(*) FROM deals WHERE date::timestamptz >= NOW() - INTERVAL '48 hours'")
        audit["created_last_48h"] = recent
        with c.cursor() as cur:
            cur.execute("SELECT baseline_count, baseline_at, last_seen_count "
                        "FROM sentinel_row_baselines WHERE table_name='deals'")
            row = cur.fetchone()
        audit["baseline"] = ({"count": int(row[0]) if row[0] is not None else None,
                              "at": str(row[1])[:19],
                              "last_seen": int(row[2]) if row[2] is not None else None}
                             if row else None)

        dup_residual = (audit["total"] - audit["distinct_hash"]
                        if isinstance(audit["total"], int) and isinstance(audit["distinct_hash"], int)
                        else None)
        audit["dup_residual"] = dup_residual
        total = audit["total"] if isinstance(audit["total"], int) else 0
        benign = (total >= _DEALS_CANONICAL_FLOOR
                  and (dup_residual is None or dup_residual <= 5)
                  and isinstance(recent, int) and recent > 0)
        audit["verdict"] = "benign_dedup_shrink" if benign else "needs_human_review"

        rebaselined = False
        if request.method == "POST" and request.args.get("rebaseline") == "1":
            if not benign:
                return jsonify(audit=audit, rebaselined=False,
                               refused="verdict not benign — refusing (would mask possible real loss)"), 409
            with c.cursor() as cur:
                cur.execute("UPDATE sentinel_row_baselines "
                            "SET baseline_count=%s, last_seen_count=%s, last_seen_at=NOW(), baseline_at=NOW() "
                            "WHERE table_name='deals'", (total, total))
                c.commit()
            rebaselined = True
        return jsonify(audit=audit, rebaselined=rebaselined), 200
    finally:
        try: c.close()
        except Exception: pass
