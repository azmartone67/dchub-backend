"""Payload shape for GET /api/v1/ai-tracking/stats.

★ WHY THIS MODULE EXISTS (2026-08-10).

This route is the FALLBACK arm of the /ai dashboard's `fetchTrackingData`: the
page reaches it whenever /api/ai/tracking is slow or 503s (measured 2026-08-10
from the outside: p50 2.3s, 1-in-20 responses 503, 2-in-20 over the frontend's
abort budget). Every field the page needed lived ONLY under the `stats` wrapper,
so `data.platforms_active`, `data.total_requests_all_time` and
`data.total_requests_today` each evaluated to `undefined` in the browser. The
page turned those undefineds into a confident "0 AI PLATFORMS CONNECTED" and
"0 active / 14 tracked", plus three em-dash tiles — while this very payload held
16 platforms and 312,928 requests. That is contract-audit mismatch #2.

The shape is built here, as a pure function over already-fetched rows, so the
contract can be tested without importing the Flask app or touching a database.
The test harness (tests/conftest.py) deliberately avoids importing main.

Two rules this module encodes:

  1. ADDITIVE compatibility. `stats` keeps its canonical names; the top-level
     keys are aliases of the same values. A public shape does not change without
     an alias.

  2. ABSENT MEANS UNKNOWN. This route measures no "today" window and holds no
     per-platform census. It therefore emits neither — not even as 0. A zero
     synthesised from a value nobody measured is the flattering-zero defect, and
     it is worse than a dash because it is quotable.
"""

SHAPE_NOTE = (
    "Canonical fields are under `stats`. The top-level keys platforms_active / "
    "total_requests_all_time / total_platforms / requests_7d / last_activity are "
    "compatibility aliases of the same values. This route has NO per-platform "
    "census and NO today figure - use /api/ai/tracking for those. Absent means "
    "unknown, not zero."
)

TOTAL_REQUESTS_LABEL = (
    "external AI-platform requests (excludes Direct/Mcp/Internal transport + "
    "probe/scanner traffic)"
)


def build_ai_tracking_stats_payload(rows, is_real_platform):
    """Build the /api/v1/ai-tracking/stats body.

    rows: iterable of (platform, total_requests, requests_7d, last_seen).
    is_real_platform: predicate excluding transport/probe/internal buckets.
    """
    rows = list(rows)

    total_platforms = sum(
        1 for r in rows if is_real_platform(r[0]) and int(r[1] or 0) > 0
    )
    total_requests = sum(int(r[1] or 0) for r in rows if is_real_platform(r[0]))
    requests_7d = sum(int(r[2] or 0) for r in rows if is_real_platform(r[0]))
    total_requests_all = sum(int(r[1] or 0) for r in rows)
    requests_7d_all = sum(int(r[2] or 0) for r in rows)
    last_activity = max((r[3] for r in rows if r[3]), default=None)
    last_activity = str(last_activity) if last_activity else None

    return {
        "success": True,
        "source": "railway",
        "stats": {
            "total_platforms": total_platforms,
            "total_requests": int(total_requests),
            "requests_7d": int(requests_7d),
            "total_requests_including_infrastructure": int(total_requests_all),
            "requests_7d_including_infrastructure": int(requests_7d_all),
            "total_requests_label": TOTAL_REQUESTS_LABEL,
            "last_activity": last_activity,
        },
        # ── Top-level compatibility aliases; same values, names consumers read.
        "platforms_active": total_platforms,
        "total_platforms": total_platforms,
        "total_requests_all_time": int(total_requests),
        "requests_7d": int(requests_7d),
        "last_activity": last_activity,
        # ── Honest absence, declared rather than implied.
        # NOTE the deliberate omissions: no `total_requests_today`, no
        # `platforms`. This route computes neither. Emitting 0 for either would
        # rebuild the exact bug this module was written to kill.
        "has_platform_census": False,
        "shape_note": SHAPE_NOTE,
    }
