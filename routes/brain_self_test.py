"""
brain_self_test.py — Phase r55 (2026-05-25).

Single-call brain health check that exercises every layer + reports
per-layer ok / degraded / failed. The operator hits this when they
want to know "is the brain ACTUALLY alive across all its layers?"
without checking 12 different dashboards.

  GET /api/v1/brain/self-test
       Returns:
         {
           "ok": <overall>,
           "score": "<N/M layers ok>",
           "layers": {
             "L4_text_loop":          {"ok": true, "ms": 12, "verdict": "active"},
             "L11_qa_agent":          {"ok": true, ...},
             "L14_causal":            {...},
             "L15_auto_action":       {...},
             "L20_durability":        {...},
             "L21_autopilot":         {...},
             "L22_auto_code":         {...},
             "L23_lifecycle_curator": {...},
             ...
           },
           "checked_at": "..."
         }

Public — no admin gate. Read-only signal mining; no side effects.
Useful for /status dashboards + brain health graphs over time.
"""
from __future__ import annotations

import datetime
import time

from flask import Blueprint, jsonify, current_app


brain_self_test_bp = Blueprint("brain_self_test", __name__)


# Each layer is tested by hitting one canonical signal endpoint.
# We pick the cheapest/most stable one per layer so the self-test
# stays fast (<5s end-to-end) even on cold start.
_LAYER_PROBES = [
    ("L4_text_loop",          "/api/v1/brain/status",                "healthy|active"),
    ("L11_qa_agent",          "/api/v1/brain/qa-agent",              "agent|ok"),
    ("L14_causal",            "/api/v1/brain/causal/findings",       "findings"),
    ("L15_auto_action",       "/api/v1/brain/autopilot/recent",      "actions|recent"),
    ("L20_durability",        "/api/v1/brain/error-classes",         "classes"),
    ("L21_autopilot",         "/api/v1/brain/autopilot/recent",      "recent"),
    ("L22_auto_code",         "/api/v1/brain/auto-code",             "auto-code|recipes"),
    ("L23_lifecycle",         "/api/v1/brain/lifecycle/findings",    "composite_health"),
    ("ecosystem_watcher",     "/api/v1/brain/ecosystem/findings",    "by_target|summary"),
    ("value_shipped",         "/api/v1/brain/value-shipped",         "verdict"),
    ("media_organism",        "/api/v1/media/organism",              "vitality_score"),
    ("page_integrity",        "/api/v1/sentinel/page-integrity",     "site_score"),
    ("dcpi_freshness",        "/api/v1/dcpi/freshness",              "stats"),
    ("internal_bot_cb",       "/api/v1/admin/internal-bot-cb",       "enabled|limit_per_min"),
]


def _probe_one(path: str, expect_marker: str) -> dict:
    """In-process test_client GET. Returns {ok, status, ms, has_marker}."""
    t0 = time.time()
    try:
        with current_app.test_client() as tc:
            r = tc.get(path)
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 401:
                # Admin-gated; that's still a "layer is alive" signal
                return {"ok": True, "status": 401, "ms": ms,
                        "note": "admin-gated (expected)"}
            if r.status_code not in (200, 202):
                return {"ok": False, "status": r.status_code, "ms": ms}
            body = r.get_data(as_text=True) or ""
            # Marker match: substring of any one marker in expect_marker (| separated)
            markers = [m.strip() for m in expect_marker.split("|") if m.strip()]
            has = any(m in body for m in markers)
            return {"ok": has, "status": 200, "ms": ms,
                    "has_marker": has, "expect": markers}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"ok": False, "status": None, "ms": ms,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


@brain_self_test_bp.route("/api/v1/brain/self-test", methods=["GET"])
def brain_self_test():
    """Single-call health check of every brain layer."""
    t0 = time.time()
    results = {}
    ok_count = 0

    for name, path, expect_marker in _LAYER_PROBES:
        r = _probe_one(path, expect_marker)
        if r.get("ok"):
            ok_count += 1
            verdict = "active"
        elif r.get("status") in (None, 503, 504):
            verdict = "failed"
        else:
            verdict = "degraded"
        r["verdict"] = verdict
        results[name] = r

    total = len(_LAYER_PROBES)
    elapsed_ms = int((time.time() - t0) * 1000)

    return jsonify({
        "ok":         ok_count == total,
        "score":      f"{ok_count}/{total} layers active",
        "ok_count":   ok_count,
        "total":      total,
        "layers":     results,
        "elapsed_ms": elapsed_ms,
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "purpose":    ("Brain self-test. Exercises every layer in one call. "
                        "active = endpoint responsive + marker present. "
                        "degraded = responsive but unexpected shape. "
                        "failed = unresponsive or 5xx."),
    }), 200
