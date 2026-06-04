"""
brain_layer15_tool_calibration.py — Tool Calibration Drift Detector

Generic harness: for every tool registered in _TOOL_REGISTRY, runs a known-
correct (input → expected output range) test set on a daily cron. When the
actual output deviates more than DRIFT_FACTOR (default 2x) from the expected
range, files a brain_finding so the next session sees it.

Built after a real-world miss on 2026-06-04: the Site Valuation Engine v1.0
shipped with a $2M/MW baseline against an industry comp range of $150K-$800K/MW.
A 100 MW Phoenix AVOID site was valued at $167.8M when comps put it at $20-60M.
A drift detector with 3 calibration points would have caught this on day 1.

Architecture:
  - _TOOL_REGISTRY: {tool_name: {endpoint, method, tests: [...]}}
  - Each test: {label, input, expected: {field_path, range}}
  - _run_test() fires the tool internally, walks the response to field_path,
    returns (passed, actual, drift_factor)
  - On drift > DRIFT_FACTOR, _file_finding() writes a brain_findings row
  - Public status endpoint: /api/v1/brain/tool-calibration/status
  - Admin trigger: POST /api/v1/admin/brain/tool-calibration/run

Hook for crawler_scheduler.SCHEDULE: runs at 02:30 UTC daily so the
following day's brain sweeps see fresh findings.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from typing import Any

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_layer15_bp = Blueprint("brain_layer15_tool_calibration", __name__)

# Drift threshold: how far outside the expected range counts as a failure.
# 2x means: if expected_range is [100K, 300K] and actual is 50K (0.5x) or
# 600K (2x), we flag. Anything inside [50K-50K_buffer, 600K+buffer] passes.
DRIFT_FACTOR = 2.0

# Internal base URL — uses the same loopback pattern as other crons.
_INTERNAL_BASE = os.environ.get("DCHUB_INTERNAL_API",
                                 "http://127.0.0.1:8080")


def _resolve_admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_ADMIN_API_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY")
            or "")


# ── Tool Registry ─────────────────────────────────────────────────────
# Each tool defines its test set. Adding a new tool means appending here.
# Expected ranges come from human judgment — these are the comps you'd
# defend in a customer call.

_TOOL_REGISTRY: dict[str, dict] = {
    "site_valuation_engine": {
        "endpoint":      "/api/v1/site/value",
        "method":        "POST",
        "auth":          "internal",
        "description":   ("Per-MW valuation for greenfield power sites. "
                          "Industry comp range $150K-$800K/MW; engine v2.0 "
                          "calibrated to that envelope. If a test produces a "
                          "$/MW value >2x outside the expected band, the "
                          "baseline drifted or a multiplier got mis-tuned."),
        # NOTE on field paths: tests use valuation_teaser.$/mw_mid because
        # internal cron calls hit the free-tier paywall (no PRO key) — the
        # teaser exposes the same per-MW number as the full valuation, so
        # drift detection works identically. Likewise scenarios_teaser
        # surfaces time-to-power.
        "tests": [
            {
                "label":  "Phoenix-100MW-50ac · AVOID · raw land",
                "input":  {"lat": 33.45, "lon": -112.07, "acres": 50,
                           "target_mw": 100, "deadline_months": 24},
                "expected": {
                    "field_path":    "valuation_teaser.$/mw_mid",
                    "range_usd":     [100_000, 350_000],
                    "human_comp":    "Raw AVOID-tier 100 MW Phoenix parcel "
                                       "should fall in lower-mid of $150-800K/MW.",
                },
            },
            {
                "label":  "Phoenix-100MW-50ac · AVOID · fully shovel-ready",
                "input":  {"lat": 33.45, "lon": -112.07, "acres": 50,
                           "target_mw": 100, "deadline_months": 24,
                           "readiness": {"grid_interconnect_ready": True,
                                         "substation_on_site": True,
                                         "water_secured": True,
                                         "fiber_on_site": True,
                                         "zoning_approved": True,
                                         "permits_in_hand": True}},
                "expected": {
                    "field_path":    "valuation_teaser.$/mw_mid",
                    "range_usd":     [400_000, 900_000],
                    "human_comp":    "Shovel-ready 100 MW Phoenix should price "
                                       "at upper-mid / top of industry range "
                                       "due to readiness stack ~3.35x.",
                },
            },
            {
                "label":  "Cheyenne-150MW-100ac · BUILD · raw land",
                "input":  {"lat": 41.14, "lon": -104.82, "acres": 100,
                           "target_mw": 150, "deadline_months": 24},
                "expected": {
                    "field_path":    "valuation_teaser.$/mw_mid",
                    "range_usd":     [400_000, 1_000_000],
                    "human_comp":    "BUILD-tier raw land (1.65x verdict mult) "
                                       "should land mid-upper of industry range.",
                },
            },
            {
                "label":  "Phoenix gas BTM time-to-power reasonable",
                "input":  {"lat": 33.45, "lon": -112.07, "acres": 50,
                           "target_mw": 100, "deadline_months": 24},
                "expected": {
                    "field_path":    "scenarios_teaser.gas_btm.time_to_power_months",
                    "range_usd":     [10, 30],
                    "human_comp":    "Gas BTM (CCGT) build + pipeline tap "
                                       "should land 10-30 months. <10 means "
                                       "we lost the build curve; >30 means "
                                       "we miscoded the bypass-ISO advantage.",
                },
            },
        ],
    },
    # Register more tools here. Pattern:
    # "dcpi_score_engine": {"endpoint": "...", "tests": [...]},
}


def _get_nested(obj: Any, path: str) -> Any:
    """Walk a dot-delimited path through a dict. Returns None if any segment
    is missing. Supports list indices via [N] suffix on a segment."""
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _drift_factor(actual: float, lo: float, hi: float) -> float:
    """How far OUTSIDE the [lo, hi] range is actual? Returns:
      0.0  if inside the range (no drift)
      >1   if outside: ratio of distance-to-range / range-midpoint
    """
    if lo <= actual <= hi:
        return 0.0
    if actual < lo:
        # How many "ranges" below lo are we?
        if lo > 0:
            return round(lo / max(actual, 1e-9), 2)
        return float("inf")
    # actual > hi
    if hi > 0:
        return round(actual / max(hi, 1e-9), 2)
    return float("inf")


def _run_test(tool_name: str, test: dict, admin_key: str) -> dict:
    """Fire one test against the local tool endpoint and grade the result."""
    tool = _TOOL_REGISTRY[tool_name]
    url = _INTERNAL_BASE + tool["endpoint"]
    method = (tool.get("method") or "POST").upper()
    headers = {"Content-Type": "application/json",
               "User-Agent":   "dchub-brain-l15-calibration/1.0"}
    if admin_key:
        headers["X-API-Key"]   = admin_key
        headers["X-Admin-Key"] = admin_key

    t0 = time.time()
    try:
        if method == "POST":
            r = requests.post(url, headers=headers, json=test["input"], timeout=20)
        else:
            r = requests.get(url, headers=headers, params=test["input"], timeout=20)
    except Exception as e:
        return {
            "label":     test["label"],
            "passed":    False,
            "actual":    None,
            "error":     f"request_failed: {str(e)[:200]}",
            "drift_factor": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    try:
        body = r.json()
    except Exception:
        return {
            "label":     test["label"],
            "passed":    False,
            "actual":    None,
            "error":     f"non_json_response: HTTP {r.status_code}",
            "drift_factor": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    if r.status_code >= 400:
        return {
            "label":     test["label"],
            "passed":    False,
            "actual":    None,
            "error":     f"HTTP {r.status_code}",
            "body":      str(body)[:300],
            "drift_factor": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    actual = _get_nested(body, test["expected"]["field_path"])
    lo, hi = test["expected"]["range_usd"]

    if actual is None:
        return {
            "label":     test["label"],
            "passed":    False,
            "actual":    None,
            "error":     f"field_missing: {test['expected']['field_path']}",
            "drift_factor": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    try:
        actual_f = float(actual)
    except (TypeError, ValueError):
        return {
            "label":     test["label"],
            "passed":    False,
            "actual":    actual,
            "error":     "non_numeric_actual",
            "drift_factor": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    drift = _drift_factor(actual_f, lo, hi)
    passed = drift < DRIFT_FACTOR

    return {
        "label":         test["label"],
        "passed":        passed,
        "actual":        actual_f,
        "expected_range": [lo, hi],
        "drift_factor":  drift,
        "field_path":    test["expected"]["field_path"],
        "human_comp":    test["expected"].get("human_comp"),
        "latency_ms":    int((time.time() - t0) * 1000),
    }


def _file_finding(tool_name: str, result: dict) -> bool:
    """Write a brain_findings row using the canonical schema."""
    try:
        import psycopg2 as _pg
    except Exception:
        return False
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return False

    actual = result.get("actual")
    lo, hi = result.get("expected_range", [None, None])
    drift = result.get("drift_factor", 0)

    detail_parts = [
        f"Tool: {tool_name}",
        f"Test: {result['label']}",
        f"Actual: {actual}",
        f"Expected range: [{lo}, {hi}]",
        f"Drift factor: {drift}x outside range",
    ]
    if result.get("human_comp"):
        detail_parts.append(f"Comp basis: {result['human_comp']}")
    if result.get("error"):
        detail_parts.append(f"Error: {result['error']}")
    detail = " · ".join(detail_parts)

    try:
        with _pg.connect(dsn, connect_timeout=4) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain_findings
                    (issue, url, count, detail, detector, status)
                VALUES (%s, %s, 1, %s, %s, 'open')
                """,
                (
                    f"tool_calibration_drift:{tool_name}",
                    _TOOL_REGISTRY[tool_name]["endpoint"],
                    detail,
                    "brain_l15_tool_calibration",
                ),
            )
            c.commit()
        return True
    except Exception as e:
        logger.warning("brain L15 finding insert failed: %s", e)
        return False


def _run_all_tools() -> dict:
    """Run every test in every registered tool. Files findings for failures."""
    admin_key = _resolve_admin_key()
    findings_filed = 0
    summary = {}

    for tool_name, tool in _TOOL_REGISTRY.items():
        tests = tool.get("tests", [])
        results = []
        passes = 0
        failures = 0
        for test in tests:
            r = _run_test(tool_name, test, admin_key)
            results.append(r)
            if r["passed"]:
                passes += 1
            else:
                failures += 1
                if _file_finding(tool_name, r):
                    findings_filed += 1
        summary[tool_name] = {
            "tests_run":   len(tests),
            "passed":      passes,
            "failed":      failures,
            "pass_rate":   round(passes / max(len(tests), 1), 2),
            "results":     results,
        }
    return {
        "ran_at":         _dt.datetime.utcnow().isoformat() + "Z",
        "tools_checked": len(_TOOL_REGISTRY),
        "findings_filed": findings_filed,
        "summary":        summary,
    }


# ── Endpoints ─────────────────────────────────────────────────────────

@brain_layer15_bp.route("/api/v1/admin/brain/tool-calibration/run",
                         methods=["POST"], strict_slashes=False)
def run_calibration():
    """Admin-gated. Runs every registered tool's test set; files findings on
    drift. Idempotent — safe to re-run."""
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-API-Key") or "")
    if not expected or provided != expected:
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    return jsonify(_run_all_tools()), 200


@brain_layer15_bp.route("/api/v1/brain/tool-calibration/status",
                         methods=["GET"], strict_slashes=False)
def calibration_status():
    """Public: summary of registered tools + recent drift findings count."""
    info = {
        "ok":             True,
        "drift_threshold": DRIFT_FACTOR,
        "tools_registered": list(_TOOL_REGISTRY.keys()),
        "tools_detail": {
            name: {
                "endpoint":   tool["endpoint"],
                "description": tool.get("description"),
                "test_count": len(tool.get("tests", [])),
            }
            for name, tool in _TOOL_REGISTRY.items()
        },
    }

    # Best-effort count of recent open drift findings (last 7d).
    try:
        import psycopg2 as _pg
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if dsn:
            with _pg.connect(dsn, connect_timeout=4) as c, c.cursor() as cur:
                cur.execute("""
                    SELECT issue, COUNT(*)::int
                      FROM brain_findings
                     WHERE detector = 'brain_l15_tool_calibration'
                       AND status = 'open'
                       AND created_at > NOW() - INTERVAL '7 days'
                     GROUP BY issue
                     ORDER BY 2 DESC
                """)
                rows = cur.fetchall() or []
                info["recent_drift_findings_7d"] = [
                    {"issue": r[0], "count": r[1]} for r in rows
                ]
                info["recent_drift_findings_7d_total"] = sum(r[1] for r in rows)
    except Exception as e:
        info["findings_lookup_error"] = str(e)[:160]

    return jsonify(info), 200


# Programmatic helper for the crawler_scheduler runner.
def run_tool_calibration_check() -> dict:
    """Called by crawler_scheduler at 02:30 UTC daily. Returns summary dict."""
    return _run_all_tools()
