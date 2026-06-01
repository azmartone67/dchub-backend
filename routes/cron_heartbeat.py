"""
cron_heartbeat.py — single cron endpoint that runs ALL scheduled warmers.

Phase ZZZZZ-round37.1 (2026-05-24). Railway service-level cron only
takes ONE expression and converts the whole service into a cron job —
which would break dchub-backend as a web service. This module collapses
multiple scheduled jobs (grid-warmer, brain-warming, future additions)
behind a single HTTP endpoint that dispatches by current UTC time.

User configures ONE external cron (GitHub Actions, cron-job.org, or a
separate Railway cron service) that hits /api/v1/cron/heartbeat every
5 minutes. The endpoint decides what to run based on the current minute
+ hour.

Schedule today:
  Every 5 min     → grid-warmer (keeps /grid/<ISO> CF cache warm)
  Every hour @ :03 → brain warming (lighter heartbeat refresh)
  Daily @ 14:00 UTC → full brain-warming (hot compute paths + 7 ISOs)

Add new jobs by editing _DISPATCH below.
"""
import os
import datetime
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request

cron_heartbeat_bp = Blueprint("cron_heartbeat", __name__,
                               url_prefix="/api/v1/cron")

BASE = "https://api.dchub.cloud"


def _hit(url, method="POST", timeout=30):
    try:
        data = b"" if method == "POST" else None
        _headers = {"User-Agent": "DCHub-CronHeartbeat/1.0",
                    "X-DC-Internal-Cron": "1"}
        # r47.37 (2026-05-26): include X-Internal-Key so admin-gated
        # endpoints (e.g. /api/v1/admin/enterprise/leads/sweep) can
        # authorize cron-originated calls without exposing the route
        # publicly. Falls back gracefully if env not set — non-admin
        # endpoints in the dispatch list don't need it.
        _ik = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("DCHUB_SYNC_KEY")
        if _ik:
            _headers["X-Internal-Key"] = _ik
        req = urllib.request.Request(
            url, data=data, method=method,
            headers=_headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512)
            return {"status": resp.status, "bytes": len(body)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "http"}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:80]}"}


# Job schedule: (label, url, method, predicate(now) → should_run)
# Predicates take a datetime.datetime UTC and return True if the job
# should fire on THIS invocation. Keep cheap → grid every call;
# expensive → only once/hour or once/day.
_DISPATCH = [
    # Grid warmer — every invocation (assumes cron fires every 5 min)
    ("grid_warmer",
     f"{BASE}/api/v1/grid-warmer/warm",
     "POST",
     lambda now: True),

    # MCP SSE event refresh — every invocation (cheap DB query)
    ("mcp_sse_refresh",
     f"{BASE}/api/v1/mcp/events/refresh",
     "POST",
     lambda now: True),

    # Brain heartbeat warmer — once per hour at :03 (to spread load)
    ("brain_warmer_hourly",
     f"{BASE}/api/v1/brain-warming/warm",
     "POST",
     lambda now: now.minute < 5),

    # Press publisher cadence check — every 2h on :07
    ("press_publisher",
     f"{BASE}/api/v1/press-publisher/run",
     "POST",
     lambda now: now.minute < 10 and now.hour % 2 == 0),

    # Heavy brain detectors run — once daily at 14:00 UTC
    ("brain_detectors_daily",
     f"{BASE}/api/v1/brain-warming/detectors",
     "GET",
     lambda now: now.hour == 14 and now.minute < 5),

    # r39: outreach to identified-but-unconverted leads — hourly at :17
    ("outreach_pending",
     f"{BASE}/api/v1/outreach/process-pending?limit=25",
     "POST",
     lambda now: now.minute >= 15 and now.minute < 20),

    # r47 (2026-05-25): ISO interconnection queue ingest — daily at 06:00 UTC.
    # Hits ERCOT MIS, PJM tracker, MISO GIQ, SPP, CAISO, NYISO, ISO-NE
    # public pages. UPSERTS only the fields that successfully parse, so
    # the seeded Q1-2026 data persists on scrape failure.
    ("iso_queue_ingest_daily",
     f"{BASE}/api/v1/iso-queue/ingest",
     "POST",
     lambda now: now.hour == 6 and now.minute < 5),

    # r47.11 (2026-05-25): LinkedIn quad rotation — 4 posts/day at fixed
    # UTC slots 08/12/16/20. Endpoint internally filters by current UTC
    # hour + idempotency-checks `linkedin_quad_posts.UNIQUE(slot_date,slot_hour)`,
    # so calling at :00/:05 of slot hours is safe even if both fire.
    # Without this entry, the quad only ran via manual force triggers and
    # all 4 slots fired in one 4-minute burst (LinkedIn spam-throttled).
    ("linkedin_quad_slot_08",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 8 and now.minute < 10),
    ("linkedin_quad_slot_12",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 12 and now.minute < 10),
    ("linkedin_quad_slot_16",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 16 and now.minute < 10),
    ("linkedin_quad_slot_20",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 20 and now.minute < 10),

    # r47.11 (2026-05-25): daily cross-post email — fires after slot_20
    # so the email body contains the freshest post for the day's
    # personal-feed reshare. 21:30 UTC = 4:30 PM ET / 5:30 PM CT.
    ("cross_post_email_daily",
     f"{BASE}/api/v1/linkedin-quad/email-best",
     "POST",
     lambda now: now.hour == 21 and now.minute >= 30 and now.minute < 35),

    # r47.14 (2026-05-25): weekly partnership LinkedIn post. Cycles
    # through 7 anchors (one per ISO week) targeting /partners and
    # the per-partner anchors (#dchawk, #cbre, #dcd, etc.). Wed 14:00 UTC
    # = 10 AM ET, peak LinkedIn organic engagement window. Endpoint
    # idempotency-checks by ISO year+week, so the 10-min fire window
    # is safe.
    ("linkedin_partnership_weekly",
     f"{BASE}/api/v1/linkedin-partnership/run",
     "POST",
     lambda now: now.weekday() == 2 and now.hour == 14 and now.minute < 10),

    # r47.15 (2026-05-25): weekly partnership press release. Fires
    # Tuesday 13:00 UTC (9 AM ET, ahead of Wed LinkedIn) so press
    # publishes → LinkedIn amplifies → email follows. Endpoint is
    # idempotent on the slug ("partnership-<track>-<isoyear>-w<isoweek>"),
    # so the 10-min window is safe.
    ("partnership_press_weekly",
     f"{BASE}/api/v1/partnerships/press/run",
     "POST",
     lambda now: now.weekday() == 1 and now.hour == 13 and now.minute < 10),

    # r47.26 (2026-05-26): hourly agent broadcast — re-pings MCP registries
    # + our own discovery surfaces so other agents pick up changes within
    # 1 hour. Fires every hour at :05 past the hour.
    ("agent_broadcast_hourly",
     f"{BASE}/api/v1/agents/broadcast",
     "POST",
     lambda now: now.minute >= 5 and now.minute < 10),

    # r47.37 (2026-05-26): weekly enterprise leads sweep. Identifies
    # top free-tier users by paid-tool demand (5+ hits/30d on
    # get_grid_intelligence, get_fiber_intel, analyze_site, etc.),
    # generates personalized outreach drafts into enterprise_lead_drafts.
    # Endpoint is idempotent (dedupes against any draft created in the
    # last 30d for the same email), so the 10-min window is safe.
    # Fires Monday 15:00 UTC (11 AM ET — start of week, fresh inboxes).
    # Drafts must be approved at /admin/partnerships/review before sending.
    ("enterprise_leads_sweep_weekly",
     f"{BASE}/api/v1/admin/enterprise/leads/sweep?top=10&min_hits=5",
     "POST",
     lambda now: now.weekday() == 0 and now.hour == 15 and now.minute < 10),

    # r47.38 (2026-05-26): weekly press pitch drafting. Scans the
    # platform for newsworthy story angles (DCPI verdict shifts, top
    # M&A deals, AI citation milestones, international expansions),
    # generates personalized pitch DRAFTS targeting beat reporters at
    # 20 seeded outlets (Bisnow, DCD, WSJ, Bloomberg, etc.). Drafts
    # must be approved at /admin/partnerships/review before sending —
    # same safety gate as enterprise leads + partnership press.
    # Fires Thursday 14:00 UTC (10 AM ET — journalists' Thursday pitch
    # window before Friday wind-down). Idempotent over 14d.
    ("press_outreach_drafts_weekly",
     f"{BASE}/api/v1/admin/press-outreach/generate-drafts?top=3&min_priority=6",
     "POST",
     lambda now: now.weekday() == 3 and now.hour == 14 and now.minute < 10),

    # r47.42 (2026-05-27): /dcpi/ask chat pre-warm. The Pages worker
    # variant 4.34.27-r44-gated-nocache disabled KV stale-cache fallback;
    # when a chat question misses the demo_question_cache (1h TTL) and
    # Claude takes >5s to respond, the worker times out and serves 503.
    # User hit this on first-time-of-day "where can I get 100 MW in 12
    # months". Fix: hit the 6 most-asked chat questions every 30 min
    # so the demo cache stays hot. Cost: ~12 Claude calls/hr = trivial.
    # NOTE: GET method only — the handler reads ?q= for GET, body for POST,
    # and _hit posts an empty body which would 400 before computing.
    ("dcpi_chat_prewarm_top_questions",
     f"{BASE}/api/v1/dcpi/ask?q=where+can+I+get+100+MW+in+12+months",
     "GET",
     lambda now: now.minute % 30 == 18),
    ("dcpi_chat_prewarm_build_markets",
     f"{BASE}/api/v1/dcpi/ask?q=top+BUILD+markets+this+week",
     "GET",
     lambda now: now.minute % 30 == 19),
    ("dcpi_chat_prewarm_iso_compare",
     f"{BASE}/api/v1/dcpi/ask?q=compare+ERCOT+PJM+and+CAISO+by+excess+power",
     "GET",
     lambda now: now.minute % 30 == 20),
    ("dcpi_chat_prewarm_cheyenne",
     f"{BASE}/api/v1/dcpi/ask?q=what+is+the+DCPI+for+Cheyenne",
     "GET",
     lambda now: now.minute % 30 == 21),
    ("dcpi_chat_prewarm_northern_va",
     f"{BASE}/api/v1/dcpi/ask?q=what+is+the+DCPI+for+Northern+Virginia",
     "GET",
     lambda now: now.minute % 30 == 22),
    ("dcpi_chat_prewarm_international",
     f"{BASE}/api/v1/dcpi/ask?q=which+international+markets+score+BUILD",
     "GET",
     lambda now: now.minute % 30 == 23),

    # r47.39.1 (2026-05-26): proxy heartbeat for CF Workers. The
    # dchub-selfheal / dchub-cron / arcgis-proxy workers live in CF's
    # Workers runtime, out of this repo. CF analytics confirms they're
    # firing (selfheal: 294 invocations/24h). The "right" fix is for
    # each worker to call /heartbeat directly from its scheduled handler
    # (see PATCHES/CF-WORKER-HEARTBEAT-SNIPPET.md), but until the operator
    # pastes that snippet, the backend pings on the worker's behalf
    # hourly. If a worker actually goes down, CF analytics + the brain's
    # land_power_endpoint_5xx / site_sentinel detectors will catch it
    # separately — this just keeps the source-registry honest.
    ("proxy_heartbeat_cf_selfheal",
     f"{BASE}/api/v1/sources/cf-selfheal/heartbeat",
     "POST",
     lambda now: now.minute >= 11 and now.minute < 16),
    ("proxy_heartbeat_cf_dchub_cron",
     f"{BASE}/api/v1/sources/cf-dchub-cron/heartbeat",
     "POST",
     lambda now: now.minute >= 12 and now.minute < 17),
    ("proxy_heartbeat_cf_arcgis_proxy",
     f"{BASE}/api/v1/sources/cf-arcgis-proxy/heartbeat",
     "POST",
     lambda now: now.minute >= 13 and now.minute < 18),
]


# AUTO-REPAIR: duplicate route '/heartbeat' also in routes/heartbeat.py:431 — review and remove one
@cron_heartbeat_bp.route("/heartbeat", methods=["GET", "POST"])
def heartbeat():
    """Run every job whose predicate returns True for current UTC time."""
    started = datetime.datetime.utcnow()
    results = []
    for label, url, method, predicate in _DISPATCH:
        if predicate(started):
            r = _hit(url, method=method)
            results.append({"job": label, "url": url, "method": method, **r})
        else:
            results.append({"job": label, "url": url, "method": method,
                            "skipped": True, "reason": "predicate_false"})
    elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    ran = [r for r in results if not r.get("skipped")]
    healthy = sum(1 for r in ran if 200 <= r.get("status", 0) < 400)

    # r47.18 (2026-05-26): log this heartbeat fire so /api/v1/cron/last-fired
    # can show "external scheduler is alive (last fire 4 min ago)". Best-
    # effort — never raises.
    try:
        from routes.cron_observability import log_heartbeat
        log_heartbeat(jobs_run=len(ran), jobs_total=len(_DISPATCH), elapsed_ms=elapsed_ms)
    except Exception:
        pass

    return jsonify({
        "at": started.isoformat() + "Z",
        "elapsed_ms": elapsed_ms,
        "jobs_total": len(_DISPATCH),
        "jobs_ran": len(ran),
        "jobs_skipped": len(_DISPATCH) - len(ran),
        "jobs_healthy": healthy,
        "results": results,
        "next_schedule_hint": ("Call this endpoint every 5 minutes from "
                                "any external cron. The endpoint decides "
                                "which jobs to actually run based on UTC time."),
    }), 200 if healthy == len(ran) else 207

# AUTO-REPAIR: duplicate route '/health' also in main.py:3871 — review and remove one

@cron_heartbeat_bp.route("/health", methods=["GET"])
def health():
    now = datetime.datetime.utcnow()
    return jsonify({
        "blueprint": "cron_heartbeat_bp",
        "now_utc": now.isoformat() + "Z",
        "dispatch_count": len(_DISPATCH),
        "would_run_now": [
            label for label, _, _, pred in _DISPATCH if pred(now)
        ],
        "phase": "ZZZZZ-round37.1",
    }), 200
