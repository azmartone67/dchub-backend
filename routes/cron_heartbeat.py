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


# 2026-07-04: THE DC HUB ANALYST blueprint (routes/analyst_note.py) rides on
# this blueprint's app registration via record_once — main.py wiring is frozen
# for parallel worktree tracks, and without registration the analyst_note_weekly
# dispatch below would 404 forever. Defensive: a broken import can never break
# the heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_analyst_note(state):
    try:
        from routes.analyst_note import analyst_note_bp
        if "analyst_note" not in state.app.blueprints:
            state.app.register_blueprint(analyst_note_bp)
    except Exception as _an_e:
        print(f"[cron_heartbeat] analyst_note_bp register skipped: {_an_e}",
              flush=True)

# r78: the dispatcher runs INSIDE this Flask app — its job POSTs were going
# out to api.dchub.cloud and back through Cloudflare to reach itself (and
# api.dchub.cloud's non-/api/* routing 522s, which is what minted the
# chronic /grid/<ISO> 5xx cluster via the grid-warmer dispatch). Loopback
# reaches the same handlers directly. Jobs that must touch the public edge
# (e.g. the grid warmer's own probes) set their own BASE.
BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else "https://api.dchub.cloud"
)


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
        # 2026-06-08 FIX: the admin routes' _admin_ok() compares the provided
        # header against DCHUB_ADMIN_KEY (not the internal key), so X-Internal-Key
        # (=DCHUB_INTERNAL_KEY, a DIFFERENT value) failed hmac.compare_digest and
        # silently 403'd EVERY admin-gated dispatch job (strategic synthesis +
        # digest never ran/sent). Also send X-Admin-Key=DCHUB_ADMIN_KEY so those
        # routes authorize cron-originated calls correctly.
        _ak = os.environ.get("DCHUB_ADMIN_KEY")
        if _ak:
            _headers["X-Admin-Key"] = _ak
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

    # Phase media_no_404 (2026-06-02): URL emission registry smoke cron.
    # Every 5 min HEAD-checks every URL emitted in last 7d; auto-revokes
    # LinkedIn posts whose landing page returns 4xx and re-emits on recovery.
    ("url_smoke_5min",
     f"{BASE}/api/v1/cron/url-smoke",
     "POST",
     lambda now: True),

    # Freshness drain — re-homed 2026-07-03 off the retired off-repo Replit
    # scheduler (heartbeat_auto_drain @ :05/:35) onto the in-process
    # self-heartbeat. _hit() attaches X-Admin-Key so the refresh batch runs
    # authenticated — an unauthenticated POST returns 401 and re-stamps
    # nothing. Fires ~every 15 min; only touches stale/unknown surfaces
    # (fresh ones are skipped) and is idempotent, so overlapping fires are
    # harmless. Bounded by the handler's WALL_BUDGET_SEC=12.
    ("heartbeat_auto_drain",
     f"{BASE}/api/v1/heartbeat/auto?batch=500",
     "POST",
     lambda now: now.minute % 15 < 5),

    # Brain heartbeat warmer — once per hour at :03 (to spread load)
    ("brain_warmer_hourly",
     f"{BASE}/api/v1/brain-warming/warm",
     "POST",
     lambda now: now.minute < 5),

    # 2026-07-09: merged-PR credit reconciler — lists merged brain-spec/ +
    # brain/autofix- PRs (GitHub, read-only) and records merge outcomes in
    # the brain's own tables so /brain/effectiveness stops reading merged=0
    # the day the operator merges seven brain PRs. Wide hour windows because
    # the throttled heartbeat lands ~hourly at random minutes; the handler
    # self-throttles (MIN_INTERVAL 120min) + a per-PR ledger makes re-fires
    # no-ops. _hit() attaches X-Admin-Key (the /run endpoint is admin-gated).
    ("brain_merge_reconciler",
     f"{BASE}/api/v1/brain/merge-reconciler/run",
     "POST",
     lambda now: now.hour in (9, 21)),

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

    # 2026-07-01: gas data feeds ingest — Henry Hub daily spot (EIA v2
    # RNGWHHD) + LNG export terminals (EIA liquefaction-capacity XLSX).
    # Wide 07:xx window because the heartbeat is throttled to ~hourly at
    # random minutes; both ingestors are idempotent upserts so re-fires
    # within the hour are safe. No key -> henry_hub fails gracefully
    # (never writes synthetic rows).
    ("gas_feeds_ingest_daily",
     f"{BASE}/api/v1/gas/feeds/ingest",
     "POST",
     lambda now: now.hour == 7 and now.minute < 55),

    # r47.11 (2026-05-25): LinkedIn quad rotation — 4 posts/day at fixed
    # UTC slots 08/12/16/20. Endpoint internally filters by current UTC
    # hour + idempotency-checks `linkedin_quad_posts.UNIQUE(slot_date,slot_hour)`,
    # so calling at :00/:05 of slot hours is safe even if both fire.
    # Without this entry, the quad only ran via manual force triggers and
    # all 4 slots fired in one 4-minute burst (LinkedIn spam-throttled).
    # 2026-06-08 QUALITY OVER QUANTITY: cut LinkedIn 4x/day -> 2x/day. The 08:00
    # (3-4 AM ET) and 20:00 (3-4 PM ET) slots are disabled; keep the two peak-
    # engagement slots 12:00 (7-8 AM ET) + 16:00 (11 AM-noon ET). Fewer, sharper
    # posts. (Combined with CONTENT_QUALITY_MIN 0.5 -> 0.72.)
    ("linkedin_quad_slot_08",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: False),  # disabled — 4->2/day quality cut
    # 2026-06-20: widened minute<10 -> minute<55 so a THROTTLED heartbeat (the
    # GitHub-Actions "5-min" cron actually delivers ~26 runs/day, ~hourly at
    # random minutes) still lands inside the slot HOUR. Idempotency is enforced
    # by linkedin_quad_posts UNIQUE(slot_date,slot_hour) + _already_posted
    # (success=TRUE only), so re-fires within the hour are safe.
    ("linkedin_quad_slot_12",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 12 and now.minute < 55),
    ("linkedin_quad_slot_16",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 16 and now.minute < 55),
    # 2026-06-20: TRUE catch-up. If the throttled heartbeat misses hours 12/16
    # entirely, this fires every tick from 13:00 UTC onward and /run backfills
    # the most-recent DUE-but-unposted active slot (12/16) for today — so the
    # day's 2 posts still land even when the exact slot window was never hit.
    ("linkedin_quad_catchup",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour >= 13 and now.minute < 55),
    ("linkedin_quad_slot_20",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: False),  # disabled — 4->2/day quality cut

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

    # 2026-06-30: DC Hub Media auto-showcase — weekly VERIFIED market-pulse
    # (top deployable markets + live ISO-queue stats), the Grok-quality data
    # post the machine should write itself. Thu 14:00 UTC = 10 AM ET (peak
    # LinkedIn). Endpoint defaults kind=market_pulse, is fact-check-gated, and
    # dedups any kind within 12h → the 5-min fire window is safe.
    ("media_showcase_market_pulse_weekly",
     f"{BASE}/api/v1/admin/media/showcase/publish",
     "POST",
     lambda now: now.weekday() == 3 and now.hour == 14 and now.minute < 5),

    # 2026-07-01: AI-Surface Freshness Sentinel — audit every AI-agent surface
    # (llms.txt, manifests, AGENTS.md, /connect, /ai, robots, integration
    # configs) vs the one canon every 2h. When AI_SURFACE_SENTINEL_ENABLED=1 it
    # writes drift to brain_findings (safe, informational — no surface writes;
    # AUTOFIX is separately gated). Read-only no-op until that env flag is set.
    ("ai_surface_audit_2h",
     f"{BASE}/api/v1/admin/ai-surface/refresh",
     "POST",
     lambda now: now.hour % 2 == 0 and now.minute < 5),

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

    # Phase evolution_measured (2026-06-02): outcome-bound brain cron.
    # Three new schedules.
    # 1) Hourly: snapshot all tracked KPIs into brain_metric_observations.
    #    This is the source of truth deltas are computed from.
    ("metric_observatory_snapshot",
     f"{BASE}/api/v1/brain/metric-observatory/snapshot",
     "POST",
     lambda now: now.minute >= 25 and now.minute < 30),
    # 2) Every 6h: verify any metric_targets whose verify_at has passed.
    #    Also fires the regression spotter + auto-revert path.
    ("outcome_verifier_run",
     f"{BASE}/api/v1/brain/outcome-verifier/run",
     "POST",
     lambda now: now.hour % 6 == 0 and now.minute >= 45 and now.minute < 50),
    # 3) Daily 13:00 UTC (9 AM ET): compose + send the 'what moved' digest
    #    to azmartone@gmail.com. The operator's falsifiable scoreboard.
    ("weekly_movement_digest_daily",
     f"{BASE}/api/v1/brain/weekly-movement-digest/run?send=true",
     "POST",
     lambda now: now.hour == 13 and now.minute < 5),

    # 2026-06-08 (#2): rebuild the R2 dataset exports daily at 09:00 UTC so the
    # bulk-download set + the large-dataset failover stay fresh.
    ("r2_exports_build_daily",
     f"{BASE}/api/v1/admin/exports/build",
     "POST",
     lambda now: now.hour == 9 and now.minute < 5),

    # 2026-06-08: DELIVER the weekly strategic intelligence briefing. The brain
    # generates ~20 product-enhancement recommendations (competitor gaps + agent
    # demand + paywall pressure) but had NO cron and only ever sent a dry-run, so
    # the operator never received it. send_weekly_digest()'s _admin_ok accepts
    # X-Internal-Key (which _hit sends) and dry_run defaults to env=false → real
    # send. The endpoint is IDEMPOTENT per (week_of, dry_run), so checking hourly
    # (top of each hour) safely sends exactly ONCE per week AND catches up within
    # the hour if a week's slot was missed — the rest are cheap no-op dedupe hits.
    # UPSTREAM of the digest: run the weekly strategic SYNTHESIS (the Opus pass
    # that generates the recommendations the digest emails). It was never cron'd
    # — ran once 6-07, then recs went stale, so the digest rendered empty for new
    # weeks. run_strategic_synthesis is IDEMPOTENT per ISO week (force=False →
    # from_cache), so firing every heartbeat does the Opus work ONCE per week and
    # the rest are cheap cache reads. Populates this week's recs → the digest below
    # then has content to send.
    ("strategic_synthesis_weekly",
     f"{BASE}/api/v1/admin/brain/strategic-synthesis/run",
     "POST",
     lambda now: True),

    # Then DELIVER the briefing. send_weekly_digest dedupes per (week_of, dry_run)
    # BEFORE rendering (cheap), so firing every heartbeat sends exactly ONCE per
    # week and delivers on the next tick after the synthesis populates recs.
    # (Narrow minute-windows were unreliable under GitHub-cron latency.)
    ("strategic_digest_weekly",
     f"{BASE}/api/v1/admin/brain/strategic-digest/send",
     "POST",
     lambda now: True),

    # 2026-07-03: RE-WIRE the acquisition + measurement loops that were built
    # but never scheduled (a root cause of the flat "just us" reach). The
    # audience master-shell measures real agents, audits GEO coverage, and ACTS
    # on the single biggest broad-query discovery gap per tick; admin-gated
    # (_hit sends X-Admin-Key), killable via AUDIENCE_MASTER_DISABLED, acts on
    # one gap/day. Daily 05:xx UTC.
    ("audience_master_tick_daily",
     f"{BASE}/api/v1/admin/audience/master-tick",
     "POST",
     lambda now: now.hour == 5 and now.minute < 55),

    # reach-rollup persists the weekly external-agent trend (was dark → the
    # /ai/reach/trend line showed zeroed weeks after the March backfill).
    # Pure read/aggregate. Daily 02:xx UTC.
    ("reach_rollup_daily",
     f"{BASE}/api/cron/reach-rollup",
     "POST",
     lambda now: now.hour == 2 and now.minute < 55),

    # 2026-07-03: GROWTH master shell — scores 5 growth levers (discovery,
    # measurement, autonomy, media, distribution), finds the weakest, and pulls
    # ONE bounded, server-side-deduped action. Every 4h. Killable via
    # GROWTH_MASTER_DISABLED / GROWTH_MASTER_ACT_DISABLED (shadow) / GROWTH_LEVER_*_OFF.
    ("growth_master_tick_4h",
     f"{BASE}/api/v1/admin/growth/master-tick",
     "POST",
     lambda now: now.hour % 4 == 0 and now.minute < 5),

    # 2026-07-03: MEDIA master shell (lever #4) — daily 13:xx UTC (9am ET). If the
    # analyst feed is starved it fires a number-led evergreen. Kill: MEDIA_MASTER_DISABLED.
    ("media_master_tick_daily",
     f"{BASE}/api/v1/admin/media/master-tick",
     "POST",
     lambda now: now.hour == 13 and now.minute < 55),

    # 2026-07-03: DISTRIBUTION master shell (lever #5) — daily 09:xx UTC. Measures
    # GEO coverage + registry presence, briefs gaps + submits registries. Kill:
    # DISTRIBUTION_MASTER_DISABLED.
    ("distribution_master_tick_daily",
     f"{BASE}/api/v1/admin/distribution/master-tick",
     "POST",
     lambda now: now.hour == 9 and now.minute < 55),

    # 2026-07-03: GRID/POWER/GAS data master shell — daily 11:xx UTC. Absorbs the
    # next untapped high-value gridstatus dataset (of 523 on our key) into
    # grid_ext_metrics, self-heals stale core feeds, and files code-shaped energy-
    # data gaps to brain. Constantly widens coverage. Kill: GRID_DATA_MASTER_DISABLED.
    ("grid_data_master_tick_daily",
     f"{BASE}/api/v1/admin/grid-data/master-tick",
     "POST",
     lambda now: now.hour == 11 and now.minute < 55),

    # 2026-07-06: Depth Master Shell — ACTS on the 4 hardest grid/fiber depth gaps
    # each tick (capacity-auction price, DC-load queue beyond ERCOT, substation
    # hosting-capacity, fiber long-haul). One bounded action/tick into grid_ext_metrics.
    # Kill: DEPTH_MASTER_DISABLED. Offset one hour from the grid-data shell.
    ("depth_master_tick_daily",
     f"{BASE}/api/v1/admin/depth/master-tick",
     "POST",
     lambda now: now.hour == 10 and now.minute < 55),

    # 2026-07-03: brain RAG reindex — embed new findings/recs/news/deals into
    # brain_corpus_embeddings so the L6 planner's semantic recall stays fresh.
    # Every 4h at :20, cap 500/run (incremental backfill; catches up over runs).
    # Kill: BRAIN_RAG_DISABLED.
    ("brain_rag_reindex_4h",
     f"{BASE}/api/v1/admin/brain/rag/reindex?cap=500",
     "POST",
     lambda now: now.hour % 4 == 0 and now.minute >= 20 and now.minute < 25),

    # 2026-07-04: Gap Master Shell — the five assessed gaps (demand/conversion/
    # citations/search/retention) measured + one bounded action on the worst,
    # every 6h at 02/08/14/20 UTC. Kill: GAPS_MASTER_DISABLED.
    ("gap_master_tick_6h",
     f"{BASE}/api/v1/admin/gaps/master-tick",
     "POST",
     lambda now: now.hour % 6 == 2 and now.minute < 8),

    # 2026-07-04: Brain Lane Driver — RAG-grounded lane decisions (2 worst
    # lanes/tick, structured outputs, closed action catalog, decision ledger
    # = RAG lesson corpus). Every 6h at 04/10/16/22 UTC, offset from the gap
    # shell so its actions have hours to land before the next gap read.
    # Kills: BRAIN_LANE_DRIVER_DISABLED / _ACT_DISABLED.
    # qa-0704c: NARROW window — the heartbeat re-fires any minute<55 entry on
    # every pass within the hour (fine for idempotent endpoints; this one costs
    # 2 Fable decisions per fire — 4 fires in 12min on 07-04 until the daily
    # cap braked it, with the driver itself choosing stop/stop by fire #4).
    ("brain_lane_driver_6h",
     f"{BASE}/api/v1/admin/brain/lane-driver/tick",
     "POST",
     lambda now: now.hour % 6 == 4 and now.minute >= 5 and now.minute < 10),

    # 2026-07-04: Reliability-Recovery shell — DAILY shadow tick (measure +
    # score + persist the A/B/C auto-merge arm gate; consecutive_ge50 needs
    # ~daily snapshots to accumulate). SHADOW unless RELIABILITY_MASTER_ARM=1.
    ("reliability_master_tick_daily",
     f"{BASE}/api/v1/admin/reliability/master-tick",
     "POST",
     lambda now: now.hour == 5 and now.minute < 8),

    # 2026-07-04: RAG master shell — DAILY shadow tick at 06:xx UTC (after the
    # 04:20 reindex cycle so freshness reads warm, before the 09:xx deep-dive
    # rotation). Measures corpus freshness/coverage, runs the retrieval eval,
    # reports the reach north-star, files gaps. SHADOW unless RAG_MASTER_ARM=1
    # (reindex + deep-dive already have their own crons — arming is opt-in so the
    # supplemental nudge never double-fires). _hit() attaches X-Admin-Key.
    ("rag_master_tick_daily",
     f"{BASE}/api/v1/admin/rag/master-tick",
     "POST",
     lambda now: now.hour == 6 and now.minute < 55),

    # RAG v1 (2026-07-03): market deep-dive rotation — regenerate the 10 stalest
    # DCPI market narratives daily (317 markets → full refresh ~monthly). This
    # existing endpoint previously had NO surviving scheduler (it was only an
    # autopilot action, and the flywheel has been offline) AND its generator was
    # broken (selected the non-existent `score` column) — market_deep_dives sat
    # at 0 rows since inception. Feeds the market_narratives RAG corpus + the
    # context-pack outlook section. _hit() attaches X-Admin-Key. Daily 09:xx UTC.
    ("market_deep_dive_rotate_daily",
     f"{BASE}/api/v1/markets/deep-dive/cron?count=10",
     "POST",
     lambda now: now.hour == 9 and now.minute < 55),

    # 2026-07-03: FIX-WAVE master shell tick — the 6-lane PASS/FAIL regression
    # sentinel for the 07-03 deep-dive fix wave (routes/fixwave_master_shell.py).
    # The shell shipped registered + live but with NO driver (same orphan class
    # as the deep-dive cron), so fixwave_snapshots never accumulated and a lane
    # regressing would go unwatched. READ-ONLY probes, fail-soft, 30s in-process
    # cache + one snapshot row per tick → in-window re-fires are cheap. Odd
    # hours to offset the even-hour growth/ai-surface ticks. _hit() attaches
    # X-Admin-Key. Kill: FIXWAVE_DISABLED=1.
    ("fixwave_master_tick_2h",
     f"{BASE}/api/v1/admin/fixwave/master-tick",
     "POST",
     lambda now: now.hour % 2 == 1 and now.minute < 55),

    # 2026-07-03: brain issue-janitor — auto-closes stale / verified-fixed L15
    # GitHub issues (routes/brain_issue_janitor.py). Its ONLY driver was the
    # decommissioned off-repo Replit scheduler, so the L15 issue backlog has
    # been growing unbounded since. Sweep is bounded (max_per_run) and
    # idempotent (closed issues stay closed); admin-gated — _hit() sends
    # X-Admin-Key. Daily 10:xx UTC (free hour slot).
    # Kill: BRAIN_ISSUE_JANITOR_DISABLE=1.
    ("brain_issue_janitor_daily",
     f"{BASE}/api/v1/brain/issue-janitor/run",
     "POST",
     lambda now: now.hour == 10 and now.minute < 55),

    # 2026-07-03: AGENT-ONBOARDING master shell — daily 08:xx UTC. One per-platform
    # onboarding scoreboard (Claude/ChatGPT/Gemini/Grok/Perplexity/Copilot/Mistral/
    # HF/Poe/…): probes mcp reachability+transport+auth, A2A card, robots AI-bot
    # allowlist, and real /reach traffic → 0-100 score + ranked next-action worklist.
    # Read-only + worklist. Kill: AGENT_ONBOARDING_MASTER_DISABLED.
    ("agent_onboarding_master_tick_daily",
     f"{BASE}/api/v1/admin/agent-onboarding/master-tick",
     "POST",
     lambda now: now.hour == 8 and now.minute < 55),

    # 2026-07-04: THREE master shells shipped with live tick endpoints but NO
    # scheduler anywhere (not here, not in .github/workflows, not the retired
    # dchub-scheduler.py) — the same orphan class as heartbeat_auto_drain: a
    # working shell that silently never ticks. Wired below, one daily fire each
    # (admin-gated; _hit sends X-Admin-Key). Windows are WIDE + each tick is
    # idempotent per its own dedupe window, so sporadic/overlapping heartbeat
    # fires are harmless.

    # CONVERSION-LOOP master shell — daily 06:xx UTC. Measures the paywall→pay
    # loop and pulls one bounded action. Kill: CONVERSION_LOOP_MASTER_DISABLED.
    ("conversion_loop_master_tick_daily",
     f"{BASE}/api/v1/admin/conversion-loop/master-tick",
     "POST",
     lambda now: now.hour == 6 and now.minute < 55),

    # AGENT-USEFULNESS master shell — daily 07:xx UTC. Scores whether agents get
    # value per call + acts on the weakest tier. Kill: AGENT_USEFULNESS_MASTER_DISABLED.
    ("agent_usefulness_master_tick_daily",
     f"{BASE}/api/v1/admin/agent-usefulness/master-tick",
     "POST",
     lambda now: now.hour == 7 and now.minute < 55),

    # AGENT-PAY master shell — daily 12:xx UTC (GET-only endpoint). Real vs. all
    # pay-intent split over the MPP/x402 rail + self-driving levers. Read-mostly.
    ("agent_pay_master_tick_daily",
     f"{BASE}/api/v1/admin/agent-pay/master-tick",
     "GET",
     lambda now: now.hour == 12 and now.minute < 55),

    # 2026-07-10: the last 4 orphaned diagnostic shells, wired exactly like the
    # 07-04 batch above. All are pure-DB / read-only (flywheel + backfunnel
    # scoreboards, coverage drafts-only, registry-freshness watch-only) — without
    # a tick their snapshot/trend tables only advance when a human loads the
    # dashboard. Staggered on quiet hours. Kills: FLYWHEEL_DISABLED,
    # BACKFUNNEL_DISABLED, COVERAGE_SHELL_DISABLED, REGISTRY_FRESHNESS_DISABLED.
    ("flywheel_master_tick_daily",
     f"{BASE}/api/v1/admin/flywheel/master-tick",
     "POST",
     lambda now: now.hour == 14 and now.minute < 55),

    ("backfunnel_master_tick_daily",
     f"{BASE}/api/v1/admin/backfunnel/master-tick",
     "POST",
     lambda now: now.hour == 15 and now.minute < 55),

    ("coverage_master_tick_daily",
     f"{BASE}/api/v1/admin/coverage/master-tick",
     "POST",
     lambda now: now.hour == 16 and now.minute < 55),

    ("registry_freshness_master_tick_daily",
     f"{BASE}/api/v1/admin/registry-freshness/master-tick",
     "POST",
     lambda now: now.hour == 17 and now.minute < 55),

    # ─────────────────────────────────────────────────────────────────────
    # 2026-07-03: RE-HOMED off the retiring off-repo Replit scheduler
    # (dchub-scheduler.py). Replit is decommissioned; these five Replit jobs
    # had NO surviving driver (not in any .github/workflows/*.yml, not in
    # crawler_scheduler.py, not an in-process loop), so they would silently
    # die when the Replit project is deleted — exactly like heartbeat_auto_drain
    # did. Cadences mirror the Replit schedule; minute windows are WIDE because
    # the heartbeat is sporadic (~hourly, ~59 fires/day), and every job here is
    # idempotent, so overlapping/repeat fires within a window are harmless.
    # _hit() attaches X-Admin-Key + X-Internal-Key so admin/internal-gated jobs
    # authorize. (The other ~30 Replit jobs are already GH-cron/crawler-driven,
    # dead routes, gated off, or Replit-only — see 2026-07-03 audit; not re-homed.)

    # Housekeeping: delete expired MCP rate-limit rows + stale daily-usage rows.
    # Pure idempotent DELETE (a repeat fire in-window deletes 0). Replit: daily 03:10.
    ("mcp_rate_cleanup",
     f"{BASE}/api/jobs/mcp-rate-cleanup",
     "POST",
     lambda now: now.hour == 3 and now.minute < 55),

    # Warm the public CC-BY /api/v1/industry/pulse cache (~15-query rollup →
    # in-process _PULSE_CACHE, 30-min TTL). Read-only compute, no auth. A cold
    # cache makes the public analyst surface 503 (observed 2026-07-03). Replit:
    # every 30 min.
    ("industry_pulse_refresh",
     f"{BASE}/api/v1/industry/pulse/refresh",
     "POST",
     lambda now: now.minute % 30 < 10),

    # Land+Power dataset freshness. The crawler_scheduler patch that would have
    # folded this in was never applied, so it was Replit-only → health showed
    # land_power RED/stale. Fire-and-forget bg thread, incremental upsert
    # (idempotent). Replit: daily 04:30.
    ("land_power_sync_incremental",
     f"{BASE}/api/land-power/sync",
     "POST",
     lambda now: now.hour == 4 and now.minute < 55),

    # Failover DR: mirror Neon facilities → Cloudflare D1 so the Pages worker
    # can serve /api/v1/map when Railway is down. Last ran 2026-06-17 (16d stale,
    # D1 stuck ~6.2K/21K rows). Returns 202 immediately (bg thread + single-flight
    # guard); CF D1 INSERT ON CONFLICT is idempotent; self-no-ops if
    # CLOUDFLARE_API_TOKEN is unset. Replit: hourly :15.
    ("d1_facilities_sync",
     f"{BASE}/api/v1/admin/d1-sync/run",
     "POST",
     lambda now: now.minute >= 15 and now.minute < 25),

    # Welcome-email drip. check_and_send_drip_emails() sends only the next DUE
    # stage per user and logs to email_drip_log (dedup), so re-fires never
    # double-send. Replit: daily 16:30 (9 AM MST). admin_drip_check was patched
    # 2026-07-03 to accept the X-Admin-Key header that _hit sends.
    ("drip_emails",
     f"{BASE}/api/admin/drip-check",
     "POST",
     lambda now: now.hour == 16 and now.minute < 55),

    # ─────────────────────────────────────────────────────────────────────
    # 2026-07-04: monthly-trend CATCH-UP. The GH workflow monthly-trend-cron
    # .yml ('5 0 1 * *') is single-shot through the PUBLIC edge and has now
    # failed 2 months straight (2026-06-01 + 2026-07-01: edge worker 503
    # "Backend unreachable and no cached data available", worker 4.46.0) —
    # and the autopilot backstop (monthly_trend_unsent_3d) was dead in June
    # because the brain flywheel was offline. These loopback entries are
    # immune to edge 503s. Both endpoints default to the PRIOR month and are
    # idempotent: /archive upserts ON CONFLICT (year, month); /send-outreach
    # skips any journalist who already received that month's report
    # (media_outreach_log pitch_topic dedup) and logs to monthly_outreach_log.
    # So repeat fires in-window are no-ops after the first success, and the
    # GH cron double-firing on the 1st is harmless. Window is day<=7 (NOT
    # day<=3): the deploy landed on UTC July 4, a day<=3 window would have
    # silently skipped the whole month and orphaned June's report forever
    # (in August "prior month" becomes July) — and the sporadic ~hourly
    # heartbeat needs wide windows anyway. Archive (02:xx) fires before send
    # (03:xx) so the frozen permalink exists when journalists click, but the
    # send does not hard-depend on it (it computes its own snapshot).
    # _hit() attaches X-Admin-Key/X-Internal-Key; both routes' _admin_ok()
    # accepts either (accepted_internal_keys ∪ DCHUB_ADMIN_KEY).
    ("monthly_trend_archive_catchup",
     f"{BASE}/api/v1/reports/monthly/archive",
     "POST",
     lambda now: now.day <= 7 and now.hour == 2 and now.minute < 55),
    ("monthly_trend_outreach_catchup",
     f"{BASE}/api/v1/reports/monthly/send-outreach?triggered_by=heartbeat_catchup",
     "POST",
     lambda now: now.day <= 7 and now.hour == 3 and now.minute < 55),

    # 2026-07-04: THE DC HUB ANALYST — weekly brain-authored Analyst Note
    # ("what moved in data-center power this week"): DCPI movers + deals +
    # news + RAG recall, ONE structured-outputs compose, honest-numbers
    # fenced, persisted to analyst_notes and served at /reports/analyst-note
    # (the backend-served Reports family — /research/* is DEAD: intercepted by
    # main.py's _check_prefix_redirects AND CF-routed to dchubapiproxy).
    # Thu 14:xx UTC — the media-showcase slot family; WIDE minute window
    # because the heartbeat is sporadic (~hourly). Endpoint is IDEMPOTENT per
    # week_of (repeat fires in-window are cheap SELECT no-ops); admin-gated —
    # _hit() sends X-Admin-Key. It NEVER auto-posts: distribution is only a
    # media LEAD that the media machine's existing gates decide on.
    # Kill: ANALYST_NOTE_DISABLED=1.
    ("analyst_note_weekly",
     f"{BASE}/api/v1/analyst-note/generate",
     "POST",
     lambda now: now.weekday() == 3 and now.hour == 14 and now.minute < 55),

    # r-dedupcron (2026-07-07): DRAIN the discovered_facilities verify->merge
    # backlog on a schedule. This is the "actuator (not fired)" the flywheel
    # flagged — verified moved only +38/7d while ~21k rows sat unverified,
    # because /api/v1/admin/dedup/drain existed but nothing ever called it.
    # The endpoint is bounded (?max), idempotent (merged_at IS NULL AND
    # is_duplicate=0), savepoint-per-row, commits per batch, and is now guarded
    # by a pg SESSION advisory lock so overlapping fires can't race. _hit()
    # attaches X-Admin-Key. Wide window (~3x/hour) so a sporadic heartbeat still
    # catches it; the advisory lock makes double-fires a safe no-op; a redeploy
    # that kills a batch just resumes on the next fire. No-deploy kill switch:
    # DEDUP_DRAIN_CRON_DISABLE=1 (scoped to the cron only — manual POST still works).
    ("dedup_drain",
     f"{BASE}/api/v1/admin/dedup/drain?max=300",
     "POST",
     lambda now: (now.minute % 20 < 3)
                 and os.environ.get("DEDUP_DRAIN_CRON_DISABLE") != "1"),
]

# r-poolfix (2026-07-04): the DB/LLM-heavy ticks. When a herd of these comes
# due on the same hour-boundary heartbeat, _dispatch_all runs them at most
# 3-wide (light jobs stay 8-wide) so they can't collectively pin the pool —
# the 03:17 80/80 saturation. Most are also delegated to the worker
# (main.py _WORKER_PROXY_*), so this doubly bounds the worker's concurrency.
_HEAVY_LABELS = frozenset({
    "audience_master_tick_daily", "growth_master_tick_4h",
    "media_master_tick_daily", "distribution_master_tick_daily",
    "grid_data_master_tick_daily", "gap_master_tick_6h",
    "depth_master_tick_daily",
    "reliability_master_tick_daily", "rag_master_tick_daily",
    "fixwave_master_tick_2h", "agent_onboarding_master_tick_daily",
    "conversion_loop_master_tick_daily", "agent_usefulness_master_tick_daily",
    "agent_pay_master_tick_daily",
    "brain_detectors_daily", "brain_issue_janitor_daily",
    "brain_lane_driver_6h", "brain_rag_reindex_4h", "brain_warmer_hourly",
    "iso_queue_ingest_daily", "gas_feeds_ingest_daily",
    "dedup_drain",
    "reach_rollup_daily", "market_deep_dive_rotate_daily",
    "strategic_synthesis_weekly", "strategic_digest_weekly",
    "analyst_note_weekly",
})


@cron_heartbeat_bp.route("/heartbeat", methods=["GET", "POST"])
def heartbeat():
    """Trigger every job whose predicate is True for the current UTC minute.

    2026-06-08 FIX (the "Cron Heartbeat keeps failing" 503): jobs used to run
    SEQUENTIALLY inline and the every-5-min set (grid-warmer + mcp-sse +
    url-smoke, which HEAD-checks every URL emitted in 7d) took ~14s — right at
    the CF edge worker's ~15s proxy timeout. When it tipped over, the worker
    declared "Backend unreachable", fell through to Render (down) + KV stale
    cache (empty) → 503 → the 5-min GitHub-Actions heartbeat failed every run.

    A heartbeat only needs to TRIGGER the dispatch and confirm the dispatcher
    is alive — per-job health is tracked by cron_observability + the brain
    detectors. So fire the due jobs CONCURRENTLY in a background thread and
    return 200 immediately (sub-second). Jobs still run to completion; the HTTP
    response just no longer blocks on them, so it can never trip the worker."""
    started = datetime.datetime.utcnow()
    # Capture caller identity IN the request thread — the background dispatch
    # thread below has NO Flask request context, and reading request.* there is
    # exactly what silently broke heartbeat logging on 2026-06-08 (see
    # cron_observability.log_heartbeat). Pass these through instead.
    try:
        _ua = request.headers.get("User-Agent", "") or ""
        _ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
    except Exception:
        _ua, _ip = "", ""
    due = [(label, url, method)
           for (label, url, method, pred) in _DISPATCH if pred(started)]
    skipped = [label for (label, url, method, pred) in _DISPATCH if not pred(started)]

    # Record the heartbeat NOW, synchronously in the request thread — do NOT
    # defer logging into the background dispatch thread. The prior design logged
    # only AFTER every job finished (incl. the slow Opus strategic-synthesis),
    # so if that daemon thread didn't survive to the tail, the fire was never
    # recorded → /api/v1/cron/last-fired read empty and cried "cron dead" for
    # ~24 days while the heartbeat was firing fine. Logging up front is correct
    # (the heartbeat DID fire) and reliable — it's one fast INSERT on a DB path
    # the reader already proves works, so it can't trip the edge-worker timeout.
    try:
        from routes.cron_observability import log_heartbeat
        log_heartbeat(jobs_run=len(due), jobs_total=len(_DISPATCH),
                      elapsed_ms=0, ua=_ua, ip=_ip)
    except Exception:
        pass

    def _dispatch_all(jobs):
        # r-poolfix (2026-07-04): de-stack the pool. On an hour-boundary
        # heartbeat a herd of daily master-shell + brain ticks all come due
        # at once; firing 8 heavy DB cycles concurrently drove the 03:17
        # 80/80 pool saturation. Split the batch: light jobs (warmers,
        # keep-alives, url-smoke) stay wide-concurrent (fast); the HEAVY
        # ticks — the same set now delegated to the worker (main.py
        # _WORKER_PROXY_*), so this also bounds how many hit the worker at
        # once — run at most 3-wide. Idempotent + wide hour windows mean a
        # throttled tick still fires on a later heartbeat in its hour.
        import concurrent.futures
        heavy = [j for j in jobs if j[0] in _HEAVY_LABELS]
        light = [j for j in jobs if j[0] not in _HEAVY_LABELS]

        def _run(batch, width):
            if not batch:
                return
            try:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(width, len(batch))) as ex:
                    for (label, url, method) in batch:
                        ex.submit(_hit, url, method)  # _hit swallows errors
            except Exception:
                pass

        _run(light, 8)
        _run(heavy, 3)

    if due:
        import threading
        threading.Thread(target=_dispatch_all, args=(due,), daemon=True).start()

    return jsonify({
        "at": started.isoformat() + "Z",
        "mode": "async_background_dispatch",
        "jobs_total": len(_DISPATCH),
        "jobs_dispatched": len(due),
        "dispatched": [j[0] for j in due],
        "skipped": skipped,
        "note": ("Jobs run concurrently in the background; heartbeat returns "
                 "immediately so it never trips the edge worker proxy timeout."),
        "next_schedule_hint": ("Call this endpoint every 5 minutes from any "
                                "external cron. It decides which jobs run by UTC time."),
    }), 200


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
