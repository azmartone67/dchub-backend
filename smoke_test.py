"""
DC Hub Production Smoke Test v1.0
==================================
Runs a battery of health checks against the live DC Hub API and reports results.

Can be run:
  - As a standalone script: python3 smoke_test.py
  - As a Flask route: register_smoke_routes(app)  → GET /api/admin/smoke-test
  - From scheduler: POST /api/jobs/smoke-test

Checks:
  1. Health endpoint (/health)
  2. Stats API (/api/v1/stats)
  3. Search API (/api/v1/search)
  4. News API (/api/news/live)
  5. Transactions API (/api/transactions)
  6. Map API (/api/v1/map)
  7. Watchdog status (/api/health/watchdog)
  8. Pool status (/api/admin/pool-status)
  9. MCP endpoint (/mcp)
  10. Database connectivity (direct pg check)
  11. Grid Intelligence (/api/v1/grid-intelligence)
  12. Fiber routes (/api/fiber/routes)

Environment:
  DCHUB_API_BASE   — API base URL (default: https://dchub-api-production.up.railway.app)
  DCHUB_ADMIN_KEY  — Admin key for authenticated endpoints
"""

import os
import time
import json
import logging
import psycopg2
from datetime import datetime, timezone
from internal_auth import is_valid_internal_key, get_internal_key_for_client

logger = logging.getLogger("dchub.smoke")

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

API_BASE = os.environ.get('DCHUB_API_BASE', 'https://dchub-api-production.up.railway.app')
ADMIN_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')
API_KEY   = os.environ.get('DCHUB_API_KEY', '')  # for gated endpoints (Pro/Enterprise)

# Endpoints to check: (name, path, method, needs_auth, timeout_s, expected_status)
SMOKE_CHECKS = [
    ("health",          "/health",                              "GET",  False, 10, 200),
    ("stats",           "/api/v1/stats",                        "GET",  False, 15, 200),
    ("search",          "/api/v1/search?q=equinix&limit=2",     "GET",  False, 15, 200),
    ("news",            "/api/news/live?limit=2",               "GET",  False, 15, 200),
    ("transactions",    "/api/transactions?limit=2",            "GET",  False, 15, 200),
    ("map",             "/api/v1/map?limit=2",                  "GET",  False, 15, 200),
    ("watchdog",        "/api/health/watchdog",                 "GET",  False, 10, 200),
    ("pool_status",     "/api/health/db",                        "GET",  False, 10, 200),
    ("grid_intel",      "/api/v1/grid-intelligence",            "GET",  False, 15, 200),
    ("fiber",           "/api/fiber/routes?limit=2",            "GET",  False, 15, 200),
    ("substations",     "/api/infrastructure/substations?lat=33.45&lon=-112.07&limit=2", "GET", False, 15, 200),
]

# ═══════════════════════════════════════════════════════════
# GATED ENDPOINT CHECKS — exercise @require_plan paths using a real API key.
# These would have caught the 2026-04-17 `current_user` regression where
# /api/v1/markets/<market> silently returned 403 for all authenticated callers.
# Requires DCHUB_API_KEY env var pointing to a Pro or Enterprise-tier key.
# Failures here MUST block deploys — a broken auth path is a P0.
# ═══════════════════════════════════════════════════════════
GATED_CHECKS = [
    # (name, path, min_plan, timeout, expected_status)
    ("markets_detail_phoenix", "/api/v1/markets/phoenix",          "pro",        15, 200),  # ← regression guard
    ("markets_detail_ashburn", "/api/v1/markets/ashburn",          "pro",        15, 200),
    ("markets_list",           "/api/v1/markets/list",             "enterprise", 15, 200),
    ("markets_compare",        "/api/v1/markets/compare?markets=phoenix,ashburn", "pro", 15, 200),
    ("facilities",             "/api/v1/facilities?limit=2",       "pro",        15, 200),
    ("ai_query",               "/api/ai/query?type=stats&q=phoenix", "enterprise", 15, 200),
]

# Thresholds
LATENCY_WARN_MS = 2000
LATENCY_FAIL_MS = 15000


# ═══════════════════════════════════════════════════════════
# HTTP HELPER
# ═══════════════════════════════════════════════════════════

def _http_check(path, method="GET", needs_auth=False, timeout=15, api_key=None):
    """Make an HTTP request and return (status_code, latency_ms, body_preview).

    If api_key is provided, it's sent as Bearer + X-API-Key to exercise the
    @require_plan decorator path (Neon lookup via validate_api_key).
    """
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    url = API_BASE.rstrip('/') + path
    headers = {
        'User-Agent': 'DCHub-SmokeTest/1.0',
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
        headers['X-API-Key'] = api_key
    elif needs_auth and ADMIN_KEY:
        headers['X-Admin-Key'] = ADMIN_KEY
        headers['X-Internal-Key'] = get_internal_key_for_client()

    start = time.time()
    try:
        req = Request(url, method=method, headers=headers)
        if method == 'POST':
            req.data = b'{}'
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')[:500]
            latency_ms = round((time.time() - start) * 1000)
            return resp.status, latency_ms, body
    except HTTPError as e:
        latency_ms = round((time.time() - start) * 1000)
        return e.code, latency_ms, e.read().decode('utf-8', errors='replace')[:200]
    except URLError as e:
        latency_ms = round((time.time() - start) * 1000)
        return 0, latency_ms, str(e.reason)[:200]
    except Exception as e:
        latency_ms = round((time.time() - start) * 1000)
        return 0, latency_ms, str(e)[:200]


def _db_check():
    """Direct DB connectivity check — bypasses the pool entirely."""
    db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    if not db_url:
        return {"status": "skip", "reason": "no DATABASE_URL"}
    start = time.time()
    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM facilities")
        fac_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM news_articles")
        news_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
        active_conns = cur.fetchone()[0]
        cur.close()
        conn.close()
        latency_ms = round((time.time() - start) * 1000)
        return {
            "status": "pass",
            "latency_ms": latency_ms,
            "facilities": fac_count,
            "news_articles": news_count,
            "active_connections": active_conns,
        }
    except Exception as e:
        latency_ms = round((time.time() - start) * 1000)
        return {"status": "fail", "latency_ms": latency_ms, "error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════
# SMOKE TEST RUNNER
# ═══════════════════════════════════════════════════════════

def run_smoke_test():
    """Run all smoke checks and return a structured report."""
    started_at = datetime.now(timezone.utc).isoformat()
    results = []
    passed = 0
    failed = 0
    warnings = 0
    latencies = []

    # HTTP endpoint checks
    for name, path, method, needs_auth, timeout, expected_status in SMOKE_CHECKS:
        status_code, latency_ms, body = _http_check(path, method, needs_auth, timeout)
        latencies.append(latency_ms)

        check_result = {
            "name": name,
            "path": path,
            "status_code": status_code,
            "latency_ms": latency_ms,
        }

        if status_code == expected_status:
            if latency_ms > LATENCY_FAIL_MS:
                check_result["verdict"] = "warn"
                check_result["issue"] = f"Slow: {latency_ms}ms > {LATENCY_FAIL_MS}ms threshold"
                warnings += 1
            elif latency_ms > LATENCY_WARN_MS:
                check_result["verdict"] = "warn"
                check_result["issue"] = f"Elevated latency: {latency_ms}ms"
                warnings += 1
            else:
                check_result["verdict"] = "pass"
                passed += 1
        else:
            check_result["verdict"] = "fail"
            check_result["issue"] = f"Expected HTTP {expected_status}, got {status_code}"
            check_result["body_preview"] = body[:100] if body else None
            failed += 1

        results.append(check_result)

    # ── Gated endpoint checks (real API key → @require_plan path) ──
    gated_failures = []  # track these separately to escalate severity
    if API_KEY:
        for name, path, min_plan, timeout, expected_status in GATED_CHECKS:
            status_code, latency_ms, body = _http_check(
                path, method="GET", timeout=timeout, api_key=API_KEY
            )
            latencies.append(latency_ms)
            check_result = {
                "name": f"gated_{name}",
                "path": path,
                "min_plan": min_plan,
                "status_code": status_code,
                "latency_ms": latency_ms,
            }
            if status_code == expected_status:
                check_result["verdict"] = "pass"
                passed += 1
            else:
                check_result["verdict"] = "fail"
                check_result["issue"] = (
                    f"Gated endpoint returned {status_code} (expected {expected_status}). "
                    f"Likely causes: (1) @require_plan decorator missing, "
                    f"(2) API key not registered in Neon users/api_keys, "
                    f"(3) Replit vs Railway NEON_DATABASE_URL mismatch."
                )
                check_result["body_preview"] = body[:200] if body else None
                failed += 1
                gated_failures.append(name)
            results.append(check_result)
    else:
        results.append({
            "name": "gated_endpoints",
            "verdict": "skip",
            "reason": "DCHUB_API_KEY env var not set — gated endpoints not exercised",
        })
        warnings += 1

    # Direct DB check
    db_result = _db_check()
    db_verdict = db_result.get("status", "fail")
    results.append({
        "name": "database_direct",
        "path": "psycopg2.connect()",
        "verdict": db_verdict,
        **db_result,
    })
    if db_verdict == "pass":
        passed += 1
        if db_result.get("latency_ms", 0) > LATENCY_WARN_MS:
            warnings += 1
    elif db_verdict == "fail":
        failed += 1

    # Summary
    total = passed + failed + warnings
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0
    p95_latency = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

    # Overall verdict — gated-endpoint failures are ALWAYS critical (auth bugs are P0)
    if gated_failures:
        overall = "critical"
    elif failed >= 3:
        overall = "critical"
    elif failed > 0:
        overall = "degraded"
    elif warnings > 2:
        overall = "slow"
    else:
        overall = "healthy"

    report = {
        "smoke_test": "DC Hub Production Smoke Test v1.1 (with gated-endpoint coverage)",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "summary": {
            "total_checks": total,
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
            "gated_failures": gated_failures,
            "avg_latency_ms": avg_latency,
            "p95_latency_ms": p95_latency,
        },
        "checks": results,
    }

    return report


# ═══════════════════════════════════════════════════════════
# FLASK ROUTES — register on app
# ═══════════════════════════════════════════════════════════

def register_smoke_routes(app):
    """Register smoke test routes on the Flask app.

    Adds:
        GET  /api/admin/smoke-test  — run full smoke test (admin only)
        POST /api/jobs/smoke-test   — cron-triggerable smoke test
    """
    from flask import jsonify as flask_jsonify, request as flask_request

    def _check_admin():
        provided = (
            flask_request.headers.get('X-Admin-Key', '')
            or flask_request.headers.get('Authorization', '').replace('Bearer ', '')
            or flask_request.args.get('admin_key', '')
        )
        expected = os.environ.get('DCHUB_ADMIN_KEY', '')
        if not provided or provided.strip() != expected.strip():
            return flask_jsonify({'error': 'unauthorized'}), 401
        return None

    @app.route('/api/admin/smoke-test', methods=['GET'])
    def admin_smoke_test():
        auth_err = _check_admin()
        if auth_err:
            return auth_err
        report = run_smoke_test()
        status_code = 200 if report['overall'] in ('healthy', 'slow') else 503
        return flask_jsonify(report), status_code

    @app.route('/api/jobs/smoke-test', methods=['POST'])
    def job_smoke_test():
        auth_err = _check_admin()
        if auth_err:
            return auth_err
        report = run_smoke_test()

        # Log results for scheduler visibility
        logger.info("SMOKE TEST: %s — %d/%d passed, %d failed, avg %dms",
                     report['overall'],
                     report['summary']['passed'],
                     report['summary']['total_checks'],
                     report['summary']['failed'],
                     report['summary']['avg_latency_ms'])

        # Store results in Neon for historical tracking
        try:
            db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
            if db_url:
                conn = psycopg2.connect(db_url, connect_timeout=10)
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS smoke_test_history (
                        id SERIAL PRIMARY KEY,
                        run_at TIMESTAMPTZ DEFAULT NOW(),
                        overall TEXT,
                        passed INT,
                        failed INT,
                        warnings INT,
                        avg_latency_ms INT,
                        p95_latency_ms INT,
                        report JSONB
                    )
                """)
                cur.execute("""
                    INSERT INTO smoke_test_history
                    (overall, passed, failed, warnings, avg_latency_ms, p95_latency_ms, report)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    report['overall'],
                    report['summary']['passed'],
                    report['summary']['failed'],
                    report['summary']['warnings'],
                    report['summary']['avg_latency_ms'],
                    report['summary']['p95_latency_ms'],
                    json.dumps(report, default=str),
                ))
                # Keep only last 90 days
                cur.execute("DELETE FROM smoke_test_history WHERE run_at < NOW() - INTERVAL '90 days'")
                conn.commit()
                cur.close()
                conn.close()
        except Exception as e:
            logger.warning("SMOKE TEST: Failed to store results in Neon: %s", e)

        return flask_jsonify(report), 200 if report['overall'] in ('healthy', 'slow') else 503

    logger.info("✅ Smoke test routes registered: /api/admin/smoke-test, /api/jobs/smoke-test")


# ═══════════════════════════════════════════════════════════
# CLI — run standalone
# ═══════════════════════════════════════════════════════════


# === SMOKE_PRESS_RELEASE_PATCH_V1 ===
# Added 2026-04-19: frontend checks + admin-gated Flask route.
# Remove this block (between the markers) to revert.

FRONTEND_URL = os.environ.get('DCHUB_FRONTEND', 'https://dchub.cloud')

FRONTEND_CHECKS = [
    # (name, path, must_contain_any, must_not_contain_any, timeout_s)
    ("press_listing",     "/press",         ["Press", "Media"],  ["Page Not Found"], 15),
    # Phase FF+25-followup-v6 (2026-05-20): /press-release content
    # changed. Was "daily brief" page with 'Today's Headlines' +
    # 'Semantic Search Explorer' markers; now serves the static
    # press-release.html (a real announcements page with title
    # "Press Release | DC Hub"). Either marker satisfies the check.
    ("press_release_url", "/press-release", ["Press Release", "DC Hub", "press-release"], ["Page Not Found"], 15),
    ("news_listing",      "/news",          ["DC Hub"],          ["Page Not Found"], 15),
    ("homepage",          "/",              ["DC Hub"],          ["Page Not Found"], 10),
]

def _frontend_check(path, must_contain, must_not_contain, timeout):
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    url = FRONTEND_URL.rstrip('/') + path
    start = time.time()
    try:
        req = Request(url, headers={'User-Agent': 'DCHub-SmokeTest/1.0'})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            latency_ms = round((time.time() - start) * 1000)
            low = body.lower()
            issues = []
            for needle in must_not_contain:
                if needle.lower() in low:
                    issues.append(f"contains forbidden: {needle!r}")
            if must_contain and not any(n.lower() in low for n in must_contain):
                issues.append(f"missing any of: {must_contain}")
            return resp.status, latency_ms, issues
    except HTTPError as e:
        return e.code, round((time.time() - start) * 1000), [f"HTTP {e.code}"]
    except URLError as e:
        return 0, round((time.time() - start) * 1000), [str(e.reason)[:100]]
    except Exception as e:
        return 0, round((time.time() - start) * 1000), [str(e)[:100]]

_original_run_smoke_test = run_smoke_test

def run_smoke_test():
    report = _original_run_smoke_test()
    report.setdefault('summary', {})
    report.setdefault('checks', [])
    for name, path, must_contain, must_not, timeout in FRONTEND_CHECKS:
        sc, lat, issues = _frontend_check(path, must_contain, must_not, timeout)
        verdict = 'pass' if sc == 200 and not issues else 'fail'
        report['checks'].append({
            'name': f'frontend_{name}',
            'path': path,
            'status_code': sc,
            'latency_ms': lat,
            'verdict': verdict,
            'issue': '; '.join(issues) if issues else '',
        })
        if verdict == 'fail':
            report['summary']['failed'] = report['summary'].get('failed', 0) + 1
        else:
            report['summary']['passed'] = report['summary'].get('passed', 0) + 1
        report['summary']['total_checks'] = report['summary'].get('total_checks', 0) + 1

    # Recompute overall now that new checks landed
    failed = report['summary'].get('failed', 0)
    warnings = report['summary'].get('warnings', 0)
    if failed >= 2:
        report['overall'] = 'critical'
    elif failed == 1:
        report['overall'] = 'degraded'
    elif warnings > 0:
        report['overall'] = 'slow'
    else:
        report['overall'] = 'healthy'
    return report


def register_smoke_routes(app):
    """Mount admin-gated /api/admin/smoke-test on the Flask app.
    Gating: X-Admin-Key header (or Bearer) must equal DCHUB_ADMIN_KEY env var.
    Returns 200 if overall is healthy/slow, 503 if degraded/critical,
    so external monitors can alert on HTTP status alone.
    """
    try:
        from flask import jsonify, request
    except ImportError:
        logger.warning("flask not installed — register_smoke_routes skipped")
        return

    expected = os.environ.get('DCHUB_ADMIN_KEY') or os.environ.get('DAILY_ADMIN_KEY', '')

# AUTO-REPAIR: duplicate route '/api/admin/smoke-test' also in smoke_test.py:320 — review and remove one
    @app.route('/api/admin/smoke-test', methods=['GET', 'POST'])
    def _smoke_test_endpoint():
        provided = (
            request.headers.get('X-Admin-Key', '')
            or request.headers.get('Authorization', '').replace('Bearer ', '', 1)
        )
        if not expected or provided != expected:
            return jsonify({"error": "unauthorized — X-Admin-Key required"}), 401
        try:
            report = run_smoke_test()
        except Exception as e:
            logger.exception("smoke endpoint failed")
            return jsonify({"error": "smoke crashed", "detail": str(e)[:200]}), 500
        http_code = 200 if report.get('overall') in ('healthy', 'slow') else 503
        return jsonify(report), http_code

    logger.info("🧯 Smoke test endpoint registered: /api/admin/smoke-test (X-Admin-Key gated)")

# === END SMOKE_PRESS_RELEASE_PATCH_V1 ===

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    print("=" * 65)
    print("  DC Hub Production Smoke Test v1.1")
    print(f"  Target:  {API_BASE}")
    print(f"  Admin:   {'set' if ADMIN_KEY else 'MISSING DCHUB_ADMIN_KEY'}")
    if API_KEY:
        print("  Credential: set")
    else:
        print("  Credential: MISSING DCHUB_API_KEY (gated endpoints will be skipped)")
    print("=" * 65)

    report = run_smoke_test()

    # Print results
    overall = report['overall']
    emoji = {'healthy': '🟢', 'slow': '🟡', 'degraded': '🟠', 'critical': '🔴'}.get(overall, '⚪')
    print(f"\n{emoji} Overall: {overall.upper()}")
    print(f"   Passed: {report['summary']['passed']}/{report['summary']['total_checks']}")
    print(f"   Failed: {report['summary']['failed']}")
    print(f"   Warnings: {report['summary']['warnings']}")
    print(f"   Avg Latency: {report['summary']['avg_latency_ms']}ms")
    print(f"   P95 Latency: {report['summary']['p95_latency_ms']}ms")

    print(f"\n{'─' * 65}")
    for check in report['checks']:
        v = check['verdict']
        icon = {'pass': '✅', 'warn': '⚠️', 'fail': '❌', 'skip': '⏭️'}.get(v, '❓')
        latency = check.get('latency_ms', '')
        latency_str = f"{latency}ms" if latency else ''
        issue = check.get('issue', '')
        print(f"  {icon} {check['name']:<28} {check.get('status_code', ''):<5} {latency_str:<10} {issue}")
    print(f"{'─' * 65}\n")

    # Exit non-zero on critical so CI / post-deploy hooks can block on failure
    sys.exit(0 if overall in ('healthy', 'slow') else 1)
