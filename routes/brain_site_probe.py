"""
brain_site_probe.py — Phase ZZZZZ-round24 (2026-05-23).

User request: "is there a way for brain to probe entire site and
identify and fix errors?"

This module is the answer. A canary detector that fetches a curated
list of public URLs (pages + APIs) and surfaces:

  - 404s   (regressions from broken redirects / removed routes)
  - 500s   (handler crashes / DB errors)
  - 503s   (CF worker fallback chain exhausted)
  - empty bodies   (where the route exists but returns nothing)
  - "error" / "not found" markers in HTML responses

Findings stream into the brain's heal-findings list with class
'site_url_unhealthy', anchored to the specific URL. The brain's
existing self-healing infrastructure (Layer 4 + Layer 5) can then
match the class and propose remediation.

The list is curated — not every URL. We deliberately probe:
  - The marketing surfaces (home, pricing, /vs, /tax-incentives)
  - The data dashboards (/intelligence, /dcpi, /markets,
    /pocket-listings, /architecture)
  - The agent-facing APIs (/api/agents/*, /api/v1/dcpi/*,
    /api/v1/marketing/*)

Each probe has a min_bytes threshold + an expected status code.
A probe is a finding if status != expected OR body < min_bytes.

Lightweight: 30 GETs, parallel via ThreadPool, 25s total budget.
Default-OFF for the radar (same as the round 17 security detectors —
HTTP self-probing inside scan_all is risky). Run on-demand via
POST /api/v1/admin/brain/site-probe.
"""
from __future__ import annotations

import os
import urllib.request as _req
import urllib.error as _reqerr
from typing import Any


# Curated public URL list. (path, expected_status, min_bytes, label).
# Adding a new public surface? Append here. The probe runs every cycle
# and a new URL not yet returning 200 will surface as a finding for
# triage. Same pattern as the round 22 land_power canary.
_PROBE_LIST = [
    # ── Marketing surfaces ──
    ("/",                              200,  3000, "Homepage"),
    ("/pricing",                       200,  2000, "Pricing"),
    ("/about",                         200,  1500, "About"),
    ("/vs",                            200,   500, "Vs index"),
    ("/vs/dchawk",                     200,  2000, "Vs DC Hawk"),
    ("/vs/cbre",                       200,  2000, "Vs CBRE"),
    ("/dc-hub-media",                  200,  3000, "DC Hub Media"),
    ("/news",                          200,  2000, "News"),
    # ── Data dashboards ──
    ("/markets",                       200,  2000, "Markets"),
    ("/dcpi",                          200,  2000, "DCPI"),
    ("/intelligence",                  200,  2000, "Intelligence"),
    ("/pocket-listings",               200,  1500, "Pocket Listings"),
    ("/pockets",                       200,  1500, "Pockets"),
    ("/architecture",                  200,  2000, "Architecture"),
    ("/tax-incentives",                200,  2000, "Tax Incentives"),
    ("/powered-shell",                 200,  2000, "Powered Shell"),
    ("/operators",                     200,  1500, "Operators index"),
    ("/transactions",                  200,  1500, "Transactions"),
    ("/grid-intelligence",             200,  1500, "Grid Intelligence"),
    ("/land-power-map",                200,  3000, "Land Power Map"),
    ("/visitor-map",                   200,  2000, "Visitor Map"),
    ("/brain",                         200,  2000, "Brain dashboard"),
    ("/alive",                         200,  1500, "Operator dashboard"),
    # ── Agent-facing JSON APIs ──
    ("/api/version",                   200,    50, "Version JSON"),
    ("/api/v1/version",                200,   100, "v1 Version"),
    ("/api/v1/stats",                  200,   100, "Stats"),
    ("/api/v1/dcpi/scores",            200,   200, "DCPI scores"),
    ("/api/v1/brain/heartbeat",        200,   100, "Brain heartbeat"),
    ("/api/v1/brain/error-classes",    200,   200, "Brain error classes"),
    ("/api/v1/brain/status",           200,    50, "Brain status"),
    ("/api/v1/heal/findings",          200,   200, "Heal findings"),
    ("/api/v1/agents/health",          200,    50, "Agent health"),
    ("/api/agents/intelligence-index", 200,   200, "Intelligence index"),
    ("/api/v1/mcp/funnel",             200,   100, "MCP funnel"),
    ("/api/v1/marketing/pulse",        200,   100, "Marketing pulse"),
    ("/api/v1/marketing/distribution/health", 200, 100, "Distribution health"),
    ("/api/v1/dchub-media/feed-v3",    200,   100, "Media feed"),
    ("/api/v1/testimonials/live",      200,   100, "Testimonials"),
    ("/.well-known/ai-agents.json",    200,   200, "Agent manifest"),
    ("/api/v1/visitor-map",            200,   100, "Visitor map JSON"),
]

# Body markers that indicate a soft error even when status is 200.
# We string-match because some handlers return 200 with an error body.
_BAD_BODY_MARKERS = (
    '"error":"404',          # 404 wrapped in 200
    '"status":"error"',
    '"success":false',
    '"degraded":true',
    'Service temporarily unavailable',
    'Backend unreachable',
    'Authentication system is starting',
)


def _probe(path: str, timeout: float = 5.0) -> tuple[int, int, str]:
    """Probe a local URL; return (status, byte_count, body_text_preview)."""
    url = f"http://localhost:8080{path}"
    req = _req.Request(url, method="GET")
    # Use a non-internal UA so we see what an external caller sees.
    req.add_header("User-Agent", "dc-brain-site-probe/1.0")
    # Set Referer to dchub.cloud so the round 22 map-endpoint bypass
    # fires for the data API endpoints in the probe list.
    req.add_header("Referer", "https://dchub.cloud/")
    try:
        with _req.urlopen(req, timeout=timeout) as r:
            body = r.read(8192).decode("utf-8", "ignore")
            return r.status, len(body), body[:300]
    except _reqerr.HTTPError as he:
        try:
            body = he.read(2048).decode("utf-8", "ignore")
        except Exception:
            body = ""
        return he.code, len(body), body[:300]
    except Exception as e:
        return 0, 0, f"connection-error: {type(e).__name__}"


def _evaluate_probe(probe_entry: tuple) -> dict | None:
    """Run one probe + classify the response. Returns finding dict or
    None when the URL is healthy. Pure function — safe to call from
    a thread pool."""
    path, expected, min_bytes, label = probe_entry
    status, body_len, sample = _probe(path, timeout=4.0)
    if status != expected:
        return {
            "issue": ("site_url_unreachable" if status == 0
                       else "site_url_unhealthy"),
            "url":    path,
            "count":  status,
            "detail": (f"{label} ({path}) returned HTTP {status} "
                        f"(expected {expected}). "
                        f"Body preview: {sample[:140]!r}"),
        }
    if body_len < min_bytes:
        return {
            "issue":  "site_url_empty_body",
            "url":    path,
            "count":  body_len,
            "detail": (f"{label} ({path}) returned 200 but body is "
                        f"only {body_len} bytes (expected {min_bytes}+). "
                        f"Body preview: {sample[:140]!r}"),
        }
    lower = sample.lower()
    for marker in _BAD_BODY_MARKERS:
        if marker.lower() in lower:
            return {
                "issue":  "site_url_error_in_body",
                "url":    path,
                "count":  1,
                "detail": (f"{label} ({path}) returned 200 but body "
                            f"contains error marker {marker!r}. "
                            f"Body preview: {sample[:200]!r}"),
            }
    return None


def check_site_url_health() -> list[dict]:
    """Probe every URL in _PROBE_LIST IN PARALLEL via ThreadPoolExecutor.

    Round 24 lesson: serial probes deadlocked Railway exactly like
    round 17 did. With ~2 gunicorn workers and 40 probes each
    re-entering the same pool via localhost:8080, the worker serving
    the scan request gets stuck waiting for self-probes that can't
    be served.

    Fix: cap concurrency to 4 (well under worker count) AND cap total
    elapsed time to 15s (fail-open on remainder). Per-probe timeout
    drops to 4s. Worst-case wall time: 15s. Each probe gets its own
    request; gunicorn can multiplex 4 of them through 2 workers."""
    import concurrent.futures as _cf
    import time as _t
    findings: list[dict] = []
    deadline = _t.time() + 15.0  # hard wall-clock budget
    with _cf.ThreadPoolExecutor(max_workers=4,
                                  thread_name_prefix="site-probe") as ex:
        futs = {ex.submit(_evaluate_probe, p): p for p in _PROBE_LIST}
        for fut in _cf.as_completed(futs, timeout=15.0):
            if _t.time() > deadline:
                break
            try:
                result = fut.result(timeout=1.0)
                if result is not None:
                    findings.append(result)
            except Exception:
                # Probe internal error; not a finding (don't double-count)
                pass
    return findings
