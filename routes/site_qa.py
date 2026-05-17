"""
site_qa.py — synthetic monitoring + self-healing layer for dchub.cloud.

Tests every public surface (pages + APIs) on a regular cadence. Stores
results. Surfaces regressions. Triggers auto-fix candidates when the
issue is something the code can detect and propose a fix for.

Endpoints:
  GET  /api/v1/qa/run                trigger a full test run
  GET  /api/v1/qa/report             latest results
  GET  /api/v1/qa/regressions        tests that started failing recently
  GET  /api/v1/qa/dashboard          HTML dashboard
  GET  /api/v1/qa/health             quick check
"""

import json
import os
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*args, **kwargs): pass


site_qa_bp = Blueprint("site_qa", __name__, url_prefix="/api/v1/qa")
SOURCE_ID = "site-qa-self-healing"

# Base URL for self-tests — defaults to public hostname, override via env
BASE_URL = os.environ.get("DCHUB_QA_BASE", "https://dchub.cloud")


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS site_qa_results (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    test_name       TEXT NOT NULL,
    test_category   TEXT NOT NULL,
    url             TEXT,
    expected        TEXT,
    actual          TEXT,
    status          TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'warn', 'skip')),
    severity        TEXT NOT NULL DEFAULT 'p1' CHECK (severity IN ('p0', 'p1', 'p2', 'p3')),
    response_ms     INTEGER,
    http_code       INTEGER,
    error_detail    TEXT,
    proposed_fix    TEXT,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS ix_site_qa_results_run ON site_qa_results (run_at DESC);
CREATE INDEX IF NOT EXISTS ix_site_qa_results_test ON site_qa_results (test_name, run_at DESC);
CREATE INDEX IF NOT EXISTS ix_site_qa_results_status ON site_qa_results (status, run_at DESC) WHERE status != 'pass';

CREATE TABLE IF NOT EXISTS site_qa_alerts (
    id              BIGSERIAL PRIMARY KEY,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    test_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    first_failed_at TIMESTAMPTZ,
    consecutive_failures INTEGER DEFAULT 1,
    proposed_fix    TEXT,
    resolved_at     TIMESTAMPTZ,
    metadata        JSONB
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_site_qa_alerts_test_open
    ON site_qa_alerts (test_name)
    WHERE resolved_at IS NULL;
"""


def _ensure_tables():
    if getattr(_ensure_tables, "_done", False): return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_tables._done = True


# ---------------------------------------------------------------------------
# Test definitions — what to check, how often
# ---------------------------------------------------------------------------

# A test is a tuple: (name, category, url, expected_check, severity, proposed_fix_template)
# expected_check: "200_html", "200_json", "200_json_nonempty", "200_no_paywall",
#                 "200_contains:<text>", "404_expected"

# These are the public surfaces that should ALWAYS work for unauthenticated users
PUBLIC_PAGES = [
    ("home",          "page", "/",                              "200_html",            "p0"),
    ("ai_page",       "page", "/ai",                            "200_html",            "p0"),
    ("pricing",       "page", "/pricing",                       "200_html",            "p1"),
    ("markets_index", "page", "/markets",                       "200_html",            "p1"),
    ("markets_chicago","page","/markets/chicago",               "200_html",            "p1"),
    ("markets_dallas","page", "/markets/dallas",                "200_html",            "p1"),
    ("markets_nova",  "page", "/markets/northern-virginia",                  "200_html",            "p1"),
    ("ai_deals",      "page", "/ai-deals",                      "200_html",            "p1"),
    ("press",         "page", "/press",                         "200_html",            "p2"),
    ("news",          "page", "/news",                          "200_html",            "p2"),
    ("daily",         "page", "/daily",                         "200_html",            "p2"),
    ("about",         "page", "/about",                         "200_html",            "p3"),
    ("api_docs",      "page", "/api-docs",                      "200_html",            "p3"),
]

# These are public APIs that should return 200 + JSON (no auth required)
PUBLIC_APIS = [
    ("api_health",    "api", "/api/health",                     "200_json",            "p0"),
    ("api_version",   "api", "/api/v1/version",                 "200_json",            "p0"),
    ("api_stats",     "api", "/api/v1/stats",                   "200_json_nonempty",   "p0"),
    ("api_markets_list", "api", "/api/v1/markets/list",         "200_no_paywall",      "p1"),
    ("api_news_feed", "api", "/api/v1/news?limit=5",            "200_no_paywall",      "p1"),
    ("api_press",     "api", "/api/press-releases",             "200_no_paywall",      "p2"),
    ("api_well_known","api", "/.well-known/mcp.json",           "200_json",            "p1"),
    ("api_openapi",   "api", "/api/v1/openapi.json",            "200_json",            "p2"),
]

# Source registry endpoints (from earlier phases — should all be live)
INTERNAL_APIS = [
    ("intelligence_health", "intelligence", "/api/v1/intelligence/health",  "200_json", "p1"),
    ("sources_health",      "intelligence", "/api/v1/sources/health",       "200_json", "p1"),
    ("grid_snapshot",       "intelligence", "/api/v1/grid/snapshot",        "200_json", "p1"),
    ("grid_totals",         "intelligence", "/api/v1/grid/totals",          "200_json", "p1"),
    ("redeem_diagnostic",   "conversion",   "/api/v1/redeem/diagnostic/health", "200_json", "p1"),
    ("redeem_funnel",       "conversion",   "/api/v1/redeem/funnel-stats",  "200_json", "p1"),
    ("mcp_funnel",          "conversion",   "/api/v1/mcp/funnel",           "200_json", "p1"),
]

ALL_TESTS = PUBLIC_PAGES + PUBLIC_APIS + INTERNAL_APIS


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _fetch(url, timeout=15):
    """Fetch and return (http_code, text, response_ms)."""
    started = time.time()
    full_url = BASE_URL + url if not url.startswith("http") else url
    req = urllib.request.Request(full_url, headers={"User-Agent": "dchub-site-qa/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = int((time.time() - started) * 1000)
            return resp.status, text, elapsed_ms
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        elapsed_ms = int((time.time() - started) * 1000)
        return e.code, text, elapsed_ms
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        return 0, f"{type(e).__name__}: {e}", elapsed_ms


def _evaluate_check(check_kind, http_code, text):
    """Return (status, error_detail, proposed_fix)."""
    if check_kind == "200_html":
        if http_code != 200:
            return "fail", f"HTTP {http_code}", _suggest_for_status(http_code)
        if "<html" not in text.lower() and "<!doctype" not in text.lower():
            return "fail", "Not HTML response", "Check that the route returns text/html, not JSON or empty"
        return "pass", None, None

    if check_kind in ("200_json", "200_json_nonempty"):
        if http_code != 200:
            return "fail", f"HTTP {http_code}", _suggest_for_status(http_code)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return "fail", f"Not JSON: {e}", "Check that the route returns application/json"
        if check_kind == "200_json_nonempty":
            if not parsed or (isinstance(parsed, (list, dict)) and len(parsed) == 0):
                return "fail", "JSON returned empty", "Check the underlying data source — empty result usually = stale data"
        return "pass", None, None

    if check_kind == "200_no_paywall":
        if http_code != 200:
            return "fail", f"HTTP {http_code}", _suggest_for_status(http_code)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return "fail", "Not JSON", "Check route returns JSON"
        # Check for paywall indicators
        if isinstance(parsed, dict):
            err = (parsed.get("error") or "").lower()
            msg = (parsed.get("message") or "").lower()
            if "plan_required" in err or "requires a" in msg or "upgrade" in msg.lower():
                return "fail", f"Returns paywall: {err or msg[:80]}", \
                       "This endpoint should be free/public. Check tier_gating decorator on the route — likely @require_plan('pro') was added incorrectly. Find route handler and remove or adjust gating."
            if parsed.get("trial_preview") is not None or parsed.get("paid_only"):
                return "fail", "Returns trial_preview", \
                       "Endpoint marked as paid-only but should be public. Check is_public_route() or remove from PAID_ONLY_TOOLS list."
        return "pass", None, None

    if check_kind.startswith("200_contains:"):
        expected_text = check_kind.split(":", 1)[1]
        if http_code != 200:
            return "fail", f"HTTP {http_code}", _suggest_for_status(http_code)
        if expected_text.lower() not in text.lower():
            return "fail", f"Missing expected text: {expected_text!r}", \
                   "Page rendering issue — text expected but absent"
        return "pass", None, None

    return "skip", f"Unknown check kind: {check_kind}", None


def _suggest_for_status(code):
    """Auto-suggest fixes based on HTTP code."""
    if code == 404:
        return "Route not registered. Check (a) blueprint import succeeded, (b) register_blueprint() called, (c) url_prefix matches expected path."
    if code == 401:
        return "Auth required. If endpoint should be public, remove auth middleware. If gated, the test URL needs a key."
    if code == 500:
        return "Server error. Check Railway deploy logs for the exception. Most common: DB query failed or missing env var."
    if code == 502 or code == 503:
        return "Backend down or CF Worker can't reach Railway. Check Railway service status."
    if code == 0:
        return "Network/DNS error. Check whether dchub.cloud DNS resolves."
    return f"HTTP {code} — investigate manually"


def _run_test(name, category, url, check_kind, severity):
    """Run a single test, return result dict."""
    http_code, text, response_ms = _fetch(url)
    status, error_detail, proposed_fix = _evaluate_check(check_kind, http_code, text)
    return {
        "test_name": name,
        "test_category": category,
        "url": url,
        "expected": check_kind,
        "actual": text[:200] if status != "pass" else None,
        "status": status,
        "severity": severity,
        "response_ms": response_ms,
        "http_code": http_code,
        "error_detail": error_detail,
        "proposed_fix": proposed_fix,
    }


def run_full_qa_suite():
    """Run ALL tests and persist results. Returns summary dict."""
    _ensure_tables()
    started = time.time()
    results = []

    for name, category, url, check_kind, severity in ALL_TESTS:
        try:
            r = _run_test(name, category, url, check_kind, severity)
            results.append(r)
        except Exception as e:
            results.append({
                "test_name": name, "test_category": category, "url": url,
                "expected": check_kind, "status": "fail",
                "severity": severity, "error_detail": f"runner exception: {e}",
                "proposed_fix": None,
                "actual": None, "response_ms": 0, "http_code": 0,
            })

    # Persist all results
    with _conn() as c, c.cursor() as cur:
        for r in results:
            cur.execute(
                """INSERT INTO site_qa_results
                       (test_name, test_category, url, expected, actual, status,
                        severity, response_ms, http_code, error_detail, proposed_fix)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (r["test_name"], r["test_category"], r["url"], r["expected"],
                 r["actual"], r["status"], r["severity"], r["response_ms"],
                 r["http_code"], r["error_detail"], r["proposed_fix"]),
            )
        c.commit()

    # Update alerts: open new alert for each new failure, resolve cleared ones
    _update_alerts(results)

    summary = {
        "run_duration_ms": int((time.time() - started) * 1000),
        "total": len(results),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "failed": sum(1 for r in results if r["status"] == "fail"),
        "warned": sum(1 for r in results if r["status"] == "warn"),
        "skipped": sum(1 for r in results if r["status"] == "skip"),
        "p0_failures": [r for r in results if r["status"] == "fail" and r["severity"] == "p0"],
        "p1_failures": [r for r in results if r["status"] == "fail" and r["severity"] == "p1"],
        "results": results,
    }

    _heartbeat(
        SOURCE_ID,
        status="success" if summary["failed"] == 0 else "partial",
        rows_affected=summary["passed"],
        duration_ms=summary["run_duration_ms"],
        metadata={"failed": summary["failed"], "p0_failures": len(summary["p0_failures"])},
    )
    return summary


def _update_alerts(results):
    """Open alerts for new failures, resolve fixed ones."""
    now = datetime.now(timezone.utc)
    with _conn() as c, c.cursor() as cur:
        for r in results:
            if r["status"] == "fail":
                # Upsert open alert
                cur.execute(
                    """INSERT INTO site_qa_alerts
                           (test_name, severity, message, first_failed_at,
                            consecutive_failures, proposed_fix, metadata)
                       VALUES (%s, %s, %s, NOW() ON CONFLICT DO NOTHING, 1, %s, %s::jsonb)
                       ON CONFLICT (test_name) WHERE resolved_at IS NULL
                       DO UPDATE SET
                           consecutive_failures = site_qa_alerts.consecutive_failures + 1,
                           message = EXCLUDED.message,
                           proposed_fix = EXCLUDED.proposed_fix,
                           metadata = EXCLUDED.metadata""",
                    (r["test_name"], r["severity"], r["error_detail"] or "fail",
                     r["proposed_fix"], json.dumps({"url": r["url"], "http_code": r["http_code"]})),
                )
            elif r["status"] == "pass":
                # Resolve any open alert for this test
                cur.execute(
                    """UPDATE site_qa_alerts
                       SET resolved_at = NOW()
                       WHERE test_name = %s AND resolved_at IS NULL""",
                    (r["test_name"],),
                )
        c.commit()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# AUTO-REPAIR: duplicate route '/run' also in ai_orchestrator.py:916 — review and remove one
@site_qa_bp.route("/run", methods=["GET", "POST"])
def trigger_run():
    """Run the full QA suite now and return summary."""
    summary = run_full_qa_suite()
    # Strip large 'actual' field from response
    summary["results"] = [
        {k: v for k, v in r.items() if k != "actual"} for r in summary["results"]
    ]
    return jsonify(summary), (200 if summary["failed"] == 0 else 207)

# AUTO-REPAIR: duplicate route '/report' also in ai_agent.py:335 — review and remove one

@site_qa_bp.route("/report", methods=["GET"])
def latest_report():
    """Latest test results — most recent run only."""
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT test_name, test_category, url, status, severity,
                      response_ms, http_code, error_detail, proposed_fix, run_at
               FROM site_qa_results
               WHERE run_at = (SELECT MAX(run_at) FROM site_qa_results)
               ORDER BY severity, test_name"""
        )
        rows = cur.fetchall()
    cols = ["test_name", "test_category", "url", "status", "severity",
            "response_ms", "http_code", "error_detail", "proposed_fix", "run_at"]
    results = [dict(zip(cols, r)) for r in rows]
    for r in results:
        if isinstance(r.get("run_at"), datetime):
            r["run_at"] = r["run_at"].isoformat()
    return jsonify(
        count=len(results),
        results=results,
        passed=sum(1 for r in results if r["status"] == "pass"),
        failed=sum(1 for r in results if r["status"] == "fail"),
    ), 200


@site_qa_bp.route("/regressions", methods=["GET"])
def regressions():
    """Tests that recently changed status — alerts."""
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT id, detected_at, test_name, severity, message,
                      first_failed_at, consecutive_failures, proposed_fix, resolved_at
               FROM site_qa_alerts
               WHERE resolved_at IS NULL OR resolved_at > NOW() - INTERVAL '24 hours'
               ORDER BY (resolved_at IS NULL) DESC, severity, detected_at DESC
               LIMIT 50"""
        )
        rows = cur.fetchall()
    cols = ["id", "detected_at", "test_name", "severity", "message",
            "first_failed_at", "consecutive_failures", "proposed_fix", "resolved_at"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        for k in ("detected_at", "first_failed_at", "resolved_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        out.append(d)
    return jsonify(count=len(out), alerts=out), 200
# AUTO-REPAIR: duplicate route '/dashboard' also in main.py:11423 — review and remove one


@site_qa_bp.route("/dashboard", methods=["GET"])
def dashboard():
    """HTML dashboard."""
    _ensure_tables()
    # Get latest results
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT test_name, test_category, url, status, severity,
                      response_ms, http_code, error_detail, proposed_fix, run_at
               FROM site_qa_results
               WHERE run_at = (SELECT MAX(run_at) FROM site_qa_results)
               ORDER BY
                  CASE severity WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,
                  status, test_name"""
        )
        rows = cur.fetchall()

        cur.execute(
            """SELECT COUNT(*) FROM site_qa_alerts
               WHERE resolved_at IS NULL"""
        )
        open_alerts = cur.fetchone()[0]

        cur.execute("SELECT MAX(run_at) FROM site_qa_results")
        last_run = cur.fetchone()[0]

    cols = ["test_name", "test_category", "url", "status", "severity",
            "response_ms", "http_code", "error_detail", "proposed_fix", "run_at"]
    results = [dict(zip(cols, r)) for r in rows]

    pass_count = sum(1 for r in results if r["status"] == "pass")
    fail_count = sum(1 for r in results if r["status"] == "fail")

    html = ['<!doctype html><html><head><meta charset="utf-8">',
            '<title>DC Hub — Site QA</title>',
            '<style>',
            'body{font-family:system-ui;max-width:1400px;margin:20px auto;padding:0 20px;color:#222;background:#fafafa}',
            'h1{margin:0 0 5px}',
            '.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:16px 0}',
            '.card{background:white;padding:16px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}',
            '.kpi-pass{border-left:4px solid #0a6b22}',
            '.kpi-fail{border-left:4px solid #d23}',
            '.kpi-num{font-size:32px;font-weight:600}',
            'table{width:100%;border-collapse:collapse;font-size:13px;background:white;margin-top:12px}',
            'th,td{padding:8px;border-bottom:1px solid #eee;text-align:left}',
            'th{background:#f5f5f5;font-weight:600;position:sticky;top:0}',
            '.s-pass{color:#0a6b22;font-weight:600}',
            '.s-fail{color:#d23;font-weight:600}',
            '.s-warn{color:#c89800}',
            '.sev{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;color:white}',
            '.sev-p0{background:#d23}',
            '.sev-p1{background:#0a6b22}',
            '.sev-p2{background:#666}',
            '.sev-p3{background:#aaa}',
            '.fix{font-family:monospace;font-size:11px;color:#444;background:#f0f0f0;padding:4px 6px;border-radius:3px;display:inline-block}',
            '</style></head><body>',
            '<h1>🩺 DC Hub — Site QA Self-Healing</h1>',
            f'<div style="color:#888;font-size:13px">Last run: {last_run.isoformat() if last_run else "never"} · Open alerts: {open_alerts}</div>',
            '<div class="kpis">',
            f'<div class="card kpi-pass"><div class="kpi-num">{pass_count}</div>passing</div>',
            f'<div class="card kpi-fail"><div class="kpi-num">{fail_count}</div>failing</div>',
            f'<div class="card"><div class="kpi-num">{len(results)}</div>total tests</div>',
            f'<div class="card"><div class="kpi-num">{open_alerts}</div>open alerts</div>',
            '</div>']

    if fail_count > 0:
        html.append('<h2>⚠️ Failing tests + suggested fixes</h2><table>')
        html.append('<tr><th>Test</th><th>Severity</th><th>URL</th><th>HTTP</th><th>Error</th><th>Suggested Fix</th></tr>')
        for r in results:
            if r["status"] == "fail":
                html.append(f'<tr>')
                html.append(f'<td><b>{r["test_name"]}</b><br><small>{r["test_category"]}</small></td>')
                html.append(f'<td><span class="sev sev-{r["severity"]}">{r["severity"].upper()}</span></td>')
                html.append(f'<td><code>{r["url"]}</code></td>')
                html.append(f'<td>{r["http_code"]}</td>')
                html.append(f'<td>{(r["error_detail"] or "")[:120]}</td>')
                html.append(f'<td><span class="fix">{(r["proposed_fix"] or "")[:200]}</span></td>')
                html.append('</tr>')
        html.append('</table>')

    # All tests table
    html.append('<h2>All tests (latest run)</h2><table>')
    html.append('<tr><th>Test</th><th>Severity</th><th>Status</th><th>URL</th><th>HTTP</th><th>Latency</th></tr>')
    for r in results:
        s_class = f's-{r["status"]}'
        html.append(f'<tr>')
        html.append(f'<td>{r["test_name"]}</td>')
        html.append(f'<td><span class="sev sev-{r["severity"]}">{r["severity"].upper()}</span></td>')
        html.append(f'<td class="{s_class}">{r["status"].upper()}</td>')
        html.append(f'<td><code>{r["url"]}</code></td>')
        html.append(f'<td>{r["http_code"]}</td>')
        html.append(f'<td>{r["response_ms"]}ms</td>')
        html.append('</tr>')
    html.append('</table>')

    html.append('<div style="color:#888;font-size:12px;margin-top:24px">')
    html.append('<a href="/api/v1/qa/run">trigger run</a> · ')
    html.append('<a href="/api/v1/qa/report">JSON report</a> · ')
    html.append('<a href="/api/v1/qa/regressions">regressions</a>')
    html.append('</div></body></html>')

# AUTO-REPAIR: duplicate route '/health' also in index_api.py:516 — review and remove one
    return "".join(html), 200, {"Content-Type": "text/html; charset=utf-8"}


@site_qa_bp.route("/health", methods=["GET"])
def health():
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT MAX(run_at), COUNT(*) FROM site_qa_results
               WHERE run_at > NOW() - INTERVAL '24 hours'"""
        )
        last_run, count_24h = cur.fetchone()
        cur.execute(
            """SELECT COUNT(*) FROM site_qa_alerts WHERE resolved_at IS NULL"""
        )
        open_alerts = cur.fetchone()[0]
    return jsonify(
        status="ok",
        last_run_at=last_run.isoformat() if last_run else None,
        results_24h=int(count_24h or 0),
        open_alerts=int(open_alerts or 0),
        tests_configured=len(ALL_TESTS),
    ), 200
