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
import json
import logging
import datetime
import threading
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request

cron_heartbeat_bp = Blueprint("cron_heartbeat", __name__,
                               url_prefix="/api/v1/cron")
logger = logging.getLogger(__name__)


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


# 2026-07-11: METRIC GROUND-TRUTH CHECKER (routes/metric_truth_check.py)
# rides on this blueprint's registration for the same frozen-main.py reason
# as analyst_note above. Defensive: a broken import never breaks the
# heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_metric_truth(state):
    try:
        from routes.metric_truth_check import metric_truth_bp
        if "metric_truth" not in state.app.blueprints:
            state.app.register_blueprint(metric_truth_bp)
    except Exception as _mt_e:
        print(f"[cron_heartbeat] metric_truth_bp register skipped: {_mt_e}",
              flush=True)


# 2026-07-12: ERROR_CODE REGISTRY (routes/error_registry.py — the Gemini
# 'Normative Grounding' delta: a published, versioned error_code taxonomy at
# /api/v1/errors/registry) rides on this blueprint's registration for the same
# frozen-main.py reason as above. Defensive: a broken import never breaks boot.
@cron_heartbeat_bp.record_once
def _register_error_registry(state):
    try:
        from routes.error_registry import register as _reg_err_registry
        _reg_err_registry(state.app)
    except Exception as _er_e:
        print(f"[cron_heartbeat] error_registry register skipped: {_er_e}",
              flush=True)

# 2026-07-11: DARK-AVAILABILITY ZONES (routes/dark_availability_zones.py,
# Gemini dark-fiber §4.3) rides on this blueprint's registration for the
# same frozen-main.py reason as analyst_note above. Defensive: a broken
# import never breaks the heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_dark_zones(state):
    try:
        from routes.dark_availability_zones import dark_zones_bp
        if "dark_availability_zones" not in state.app.blueprints:
            state.app.register_blueprint(dark_zones_bp)
    except Exception as _dz_e:
        print(f"[cron_heartbeat] dark_zones_bp register skipped: {_dz_e}",
              flush=True)

# 2026-07-11: LATENCY CLUSTER SCREEN (routes/cluster_latency.py, Gemini
# partnership spec — /api/v1/fiber/cluster-latency) rides on this blueprint's
# registration for the same frozen-main.py reason as analyst_note above.
# Defensive: a broken import never breaks the heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_cluster_latency(state):
    try:
        from routes.cluster_latency import cluster_latency_bp
        if "cluster_latency" not in state.app.blueprints:
            state.app.register_blueprint(cluster_latency_bp)
    except Exception as _cl_e:
        print(f"[cron_heartbeat] cluster_latency_bp register skipped: {_cl_e}",
              flush=True)


# 2026-07-11: WEBMCP MASTER SHELL (routes/webmcp_master_shell.py, webmcp-lane)
# rides on this blueprint's registration for the same frozen-main.py reason
# as analyst_note above. Defensive: a broken import never breaks the
# heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_webmcp_shell(state):
    try:
        from routes.webmcp_master_shell import webmcp_master_shell_bp
        if "webmcp_master_shell" not in state.app.blueprints:
            state.app.register_blueprint(webmcp_master_shell_bp)
    except Exception as _wm_e:
        print(f"[cron_heartbeat] webmcp_master_shell_bp register skipped: {_wm_e}",
              flush=True)


# 2026-07-16: ENGINE PRE-WARM (routes/engine_prewarm.py) — the never-cold tick
# for the leadership/utilization optimization engines. Rides on this
# blueprint's registration for the same frozen-main.py reason as above.
# Defensive: a broken import never breaks the heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_engine_prewarm(state):
    try:
        from routes.engine_prewarm import engine_prewarm_bp
        if "engine_prewarm" not in state.app.blueprints:
            state.app.register_blueprint(engine_prewarm_bp)
    except Exception as _ep_e:
        print(f"[cron_heartbeat] engine_prewarm_bp register skipped: {_ep_e}",
              flush=True)


# 2026-07-18: DAILY MORNING CALLOUT (routes/brain_daily_callout.py) — the
# post-mortem of the silent June→July /press edge stall: one honest morning
# email naming every stuck pipeline + its actuator. Rides on this
# blueprint's registration for the same frozen-main.py reason as above.
# Defensive: a broken import never breaks the heartbeat (or boot).
@cron_heartbeat_bp.record_once
def _register_daily_callout(state):
    try:
        from routes.brain_daily_callout import brain_daily_callout_bp
        if "brain_daily_callout" not in state.app.blueprints:
            state.app.register_blueprint(brain_daily_callout_bp)
    except Exception as _dc_e:
        print(f"[cron_heartbeat] brain_daily_callout_bp register skipped: {_dc_e}",
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


# ── dispatch outcome classification (r-cron-outcome 2026-08-29) ───────
# Our endpoints SELF-REPORT failure at HTTP 200. brain_fix_verify_sweep
# answers {"ok":false,"disabled":true} whenever BRAIN_FIX_VERIFY!=1, the
# master shells answer {"skipped":"already_ran_today"}, and several handlers
# answer {"ok":false,"error":...} rather than a 5xx so the edge never retries
# them. Reading only resp.status therefore calls all of those a success — and
# _hit used to read 512 bytes of body and throw them away, so nothing could
# tell the difference.
#
# 512 also truncates mid-JSON on any real payload, which is why the body was
# unparseable in the first place. We read a little more and parse it.
# ★ A body we cannot parse at HTTP 200 is reported "ok", never a failure:
# plenty of dispatched endpoints answer HTML or plain text, and crying wolf on
# those would bury the real signal on day one.
_HIT_BODY_BYTES = 4096


def _classify(status, body):
    """(http status, raw body bytes) -> {status, bytes, outcome, detail}.
    outcome is one of cron_observability.CRON_OUTCOME_KINDS, or "ok"."""
    out = {"status": status, "bytes": len(body or b""), "outcome": "ok",
           "detail": ""}
    try:
        if not isinstance(status, int) or status == 0:
            out["outcome"] = "unreachable"
            return out
        if status >= 400:
            out["outcome"] = "http_error"
            out["detail"] = (body or b"")[:200].decode("utf-8", "replace")
            return out
        try:
            doc = json.loads((body or b"").decode("utf-8", "replace"))
        except Exception:
            return out          # not JSON (or truncated) — cannot judge, say ok
        if not isinstance(doc, dict):
            return out
        if doc.get("disabled"):
            out["outcome"] = "disarmed"
        elif doc.get("skipped"):
            out["outcome"] = "skipped"
        elif doc.get("ok") is False:
            out["outcome"] = "self_reported_failure"
        else:
            return out
        detail = doc.get("error") or doc.get("skipped") or doc.get("reason") or ""
        out["detail"] = str(detail)[:300]
    except Exception:
        pass
    return out


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
            body = resp.read(_HIT_BODY_BYTES)
            return _classify(resp.status, body)
    except urllib.error.HTTPError as e:
        return {"status": e.code, "bytes": 0, "outcome": "http_error",
                "detail": "http"}
    except Exception as e:
        return {"status": 0, "bytes": 0, "outcome": "unreachable",
                "detail": f"{type(e).__name__}: {str(e)[:80]}"}


def _run_batch(batch, width, hit=None):
    """Fire a batch concurrently and RETURN the outcomes that were not "ok".

    ★ This was a nested closure that did `ex.submit(_hit, url, method)` and
    never read the future — which is precisely why ~60 jobs per fire could
    fail unobserved for months. It lives at module level now so a test can
    exercise it; a load-bearing loop that no test can reach is how the silence
    happened in the first place.

    `hit` is injectable for tests. Never raises."""
    if not batch:
        return []
    import concurrent.futures
    found = []
    fn = hit or _hit
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(width, len(batch))) as ex:
            futs = {ex.submit(fn, url, method): label
                    for (label, url, method) in batch}
            for fut in concurrent.futures.as_completed(futs):
                label = futs[fut]
                try:
                    res = fut.result() or {}
                except Exception as e:
                    # _hit swallows its own errors; this is the belt.
                    res = {"status": 0, "outcome": "unreachable",
                           "detail": f"{type(e).__name__}: {str(e)[:80]}"}
                if (res.get("outcome") or "ok") != "ok":
                    found.append(dict(res, label=label))
    except Exception:
        pass
    return found


# Job schedule: (label, url, method, predicate(now) → should_run)
# Predicates take a datetime.datetime UTC and return True if the job
# should fire on THIS invocation. Keep cheap → grid every call;
# expensive → only once/hour or once/day.
_DISPATCH = [
    # Grid warmer — r-healdiet2 (2026-07-17): every invocation → ~every 15
    # min. TWO heartbeat crons dispatch this list (cron-heartbeat every 5
    # min + heartbeat-auto every 15), so "every invocation" warmed 18 edge
    # URLs every ~2-3 min — CF showed DCHub-Warmer at ~17k req/day, and the
    # /grid pages it warms are edge-cached well past 15 min anyway. The
    # minute-window predicate keeps roughly 1 dispatch in 3 (minutes 0-4,
    # 15-19, 30-34, 45-49 of each hour) regardless of which cron fires.
    ("grid_warmer",
     f"{BASE}/api/v1/grid-warmer/warm",
     "POST",
     lambda now: now.minute % 15 < 5),

    # MCP SSE event refresh — every invocation (cheap DB query)
    ("mcp_sse_refresh",
     f"{BASE}/api/v1/mcp/events/refresh",
     "POST",
     lambda now: True),

    # Growth master shell #53 (2026-08-08) — daily tick + dead-man beat.
    # Registered in the SAME change as the shell's _beat_ledger: a beat whose
    # feed nothing schedules is the registered≠scheduled class this repo has
    # paid for four times (tests/test_shell_scheduler_coverage.py enforces it).
    # Once daily at 07:1x UTC; pure-DB lanes, cheap.
    ("growth_funnel_shell_daily",
     f"{BASE}/api/v1/admin/growth-funnel/master-tick",
     "POST",
     lambda now: now.hour == 7 and now.minute < 5),

    # r-clarity-deadclicks was merged 2026-07-10 with a report route, a filer,
    # thresholds and a token — and nothing that ever CALLED it. It filed zero
    # findings in six weeks because no scheduler existed, which is the
    # registered-but-not-scheduled class this repo has paid for repeatedly.
    # ★ ONCE A DAY, DELIBERATELY. Clarity's Data Export API is capped at 10
    # requests per project per day and this tick spends one of them; a
    # tighter cadence would exhaust the quota and start failing the report
    # route a human uses interactively. 09:1x UTC keeps it clear of the 07:0x
    # and 11:0x dailies either side.
    ("clarity_dead_clicks_daily",
     f"{BASE}/api/v1/admin/clarity/dead-clicks-tick",
     "POST",
     lambda now: now.hour == 9 and now.minute < 5),

    # Squasher manual-fix queue drain (2026-08-08). The portal's "Queue fix"
    # button only ENQUEUES; this is what does the work. Runs every ~10 min so a
    # submitted finding gets picked up promptly without a human waiting on a
    # request the CF edge would kill at 15s. Normally a no-op (empty queue);
    # bounded to 2 items per drain, and the enqueue path is capped at 12/day so
    # this can never become a PR firehose.
    ("squasher_queue_drain",
     f"{BASE}/api/v1/brain/squasher/drain",
     "POST",
     lambda now: now.minute % 10 < 5),

    # r-founder-note (2026-07-17): founding-member founder-voice welcome
    # sweep — every invocation (cheap: two indexed queries, normally zero
    # candidates). The Stripe webhook schedules an in-process 5-15 min timer
    # as the fast path; this lane is the restart-proof backstop. Idempotent
    # (welcome_email_log reservation row per email). confirm=1 arms the send
    # (_hit attaches X-Admin-Key); FOUNDER_NOTE_DISABLE=1 is the kill switch.
    ("founder_note_sweep",
     f"{BASE}/api/v1/admin/founder-note/run?confirm=1",
     "POST",
     lambda now: True),

    # 2026-07-16: optimization-engine pre-warm — EVERY invocation. A COLD
    # leadership/utilization tick costs ~13.7s (6 internal prefetches), which
    # exceeds the CF worker's per-attempt timeout → failover → the stale
    # Render backend's zeros-200 poisoned the worker's KV cache for ~40 min
    # after every deploy. The endpoint warms BOTH engines IN-PROCESS (direct
    # compute call, no HTTP self-request), each honoring its _RESP_TTL (300s)
    # — a fresh cache makes a fire a cheap no-op, so every-tick dispatch is
    # safe even on the sporadic (~hourly) real heartbeat cadence. Deliberately
    # NOT in _HEAVY_LABELS: it must warm the WEB replica's in-process caches
    # (the ones user/CF-worker requests read), not the worker's.
    # Kill: ENGINE_PREWARM_DISABLE=1 (in-handler).
    ("engine_prewarm",
     f"{BASE}/api/v1/engines/prewarm",
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

    # 2026-08-16: pending-drafts digest — the operator email that surfaces every
    # human-gated media draft (press_releases, pitch drafts, media_story_queue
    # 'queued' rows from the data-story factory + expansion-stories lanes). Its
    # only scheduler was the 15:10 entry in dchub-scheduler.py, which is DEAD
    # CODE (nothing execs it; the diverged heroic-reprieve copy was
    # decommissioned 2026-08-07) — so drafts queued for review were reaching
    # nobody. Window 15:10-15:59 because GitHub drops most 5-min fires (one
    # fire/hour measured 2026-08-02); the send is an EMAIL (not idempotent), so
    # _MIN_REFIRE_S below collapses repeat fires within the window. The
    # endpoint sends nothing when the pending set is empty.
    ("media_pending_drafts_digest",
     f"{BASE}/api/v1/media/pending-drafts/digest?send=true",
     "POST",
     lambda now: now.hour == 15 and now.minute >= 10),

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

    # 2026-07-11 (brain-stall audit): the enhancer lane had NO cron caller —
    # POST /api/v1/brain/enhance ran exactly once ever (its 06-19 ship day),
    # so the daily innovation digest's enhancement section always looked
    # stalled. Async enqueue endpoint, admin-gated (_hit attaches keys),
    # idempotent per run. Kill: BRAIN_ENHANCER_ENABLED=0 (checked in-handler).
    ("brain_enhancer_daily",
     f"{BASE}/api/v1/brain/enhance",
     "POST",
     lambda now: now.hour == 18 and now.minute < 55),

    # 2026-07-11: ground-truth verifier for merged MECHANICAL brain fixes.
    # BRAIN_FIX_VERIFY was armed for weeks but its recorder INSERTed into a
    # column set that doesn't exist on the live brain_fix_outcomes table, so
    # 39 merged single-file fixes carried NO effect verdict while the
    # fix-signal trust gate (deepdive lane 1) read 0.426. The sweep verifies
    # each merged proposal's fix (search_text GONE + replace_text PRESENT in
    # the file on main) and records the tri-state verdict through the
    # canonical writer (brain_learning.record_proposal_outcome). Idempotent
    # (NOT EXISTS per proposal) + LIMIT 25/run; hours offset from
    # brain_merge_reconciler (9,21) so merge credit lands before the sweep.
    ("brain_fix_verify_sweep",
     f"{BASE}/api/v1/brain/verify-merged-fixes?limit=25",
     "POST",
     lambda now: now.hour in (10, 22)),

    # 2026-07-11 (webmcp-lane): WebMCP master shell daily tick — attribution
    # rows landing, Origin-Trial header on 3 key pages, trial-token expiry
    # countdown (<30d files a finding), and bound-API drift (every path
    # js/dchub-webmcp.js binds returns 200 keyless). Read-only diagnostic;
    # findings on breakage only. Daily 20:xx UTC (quiet hour — no other
    # daily job on 20); WIDE minute window because the throttled heartbeat
    # lands ~hourly at random minutes. ?fresh=1 bypasses the 30s tick cache
    # so the daily run always measures live. _hit() attaches X-Admin-Key.
    # Kill: WEBMCP_SHELL_DISABLE=1 (in-handler, 404).
    ("webmcp_shell_daily",
     f"{BASE}/api/v1/admin/webmcp/master-tick?fresh=1",
     "POST",
     lambda now: now.hour == 20 and now.minute < 55),

    # Press publisher cadence check — every 2h on :07
    ("press_publisher",
     f"{BASE}/api/v1/press-publisher/run",
     "POST",
     lambda now: now.minute < 10 and now.hour % 2 == 0),

    # 2026-07-18: daily morning callout email — 11-13 UTC (7-9 AM ET), a
    # THREE-hour window because the throttled heartbeat lands ~hourly at
    # random minutes; the handler's per-day claim row
    # (brain_daily_callout_log) makes re-fires within the window no-ops.
    # Kill: BRAIN_DAILY_CALLOUT_ENABLED=0. _hit() attaches X-Admin-Key.
    ("brain_daily_callout_morning",
     f"{BASE}/api/v1/admin/brain/daily-callout/send",
     "POST",
     lambda now: now.hour in (11, 12, 13) and now.minute < 55),

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
    # Cadence history: 2026-06-08 quality cut ran 2x/day (12/16 only, with
    # CONTENT_QUALITY_MIN 0.5 -> 0.72); r-media-goldmine 2026-07-14 re-enabled
    # 08/20 (diverse moat/pillar pool now feeds them); r-quad-4day 2026-07-24:
    # operator confirmed 4x/day and linkedin_quad_daily._ACTIVE_SLOT_HOURS now
    # includes 8/20 too, so all four slots post reliably (exact window OR
    # catch-up backfill) instead of only when a tick hit minute<15.
    ("linkedin_quad_slot_08",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 8 and now.minute < 55),  # r-media-goldmine 2026-07-14: re-enabled 2->4/day (diverse moat/pillar pool now feeds it)
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
    # 2026-06-20: TRUE catch-up. If the throttled heartbeat misses a slot hour
    # entirely, this fires every tick from 09:00 UTC onward and /run backfills
    # the most-recent DUE-but-unposted active slot (08/12/16/20) for today —
    # so the day's 4 posts still land even when the exact slot window was
    # never hit. (r-quad-4day 2026-07-24: was hour >= 13 when only 12/16 were
    # active; starts at 09 now so a missed 08 slot backfills promptly.)
    ("linkedin_quad_catchup",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour >= 9 and now.minute < 55),
    ("linkedin_quad_slot_20",
     f"{BASE}/api/v1/linkedin-quad/run",
     "POST",
     lambda now: now.hour == 20 and now.minute < 55),  # r-media-goldmine 2026-07-14: re-enabled 2->4/day

    # r47.11 (2026-05-25): daily cross-post email — fires after slot_20
    # so the email body contains the freshest post for the day's
    # personal-feed reshare. 21:30 UTC = 4:30 PM ET / 5:30 PM CT.
    ("cross_post_email_daily",
     f"{BASE}/api/v1/linkedin-quad/email-best",
     "POST",
     lambda now: now.hour == 21 and now.minute >= 30 and now.minute < 35),

    # 2026-07-23: PROACTIVE LinkedIn token refresh — makes the posting token
    # DURABLE so the media feed can't silently go dark. LinkedIn access tokens
    # expire in ~60d; there was NO proactive refresh, so when the token lapsed
    # _get_valid_token() returned None and the WHOLE feed went dark. This fires
    # daily 04:xx UTC (WIDE minute window — the heartbeat is sporadic ~hourly):
    # if the token is within ~10d of expiry AND a usable refresh_token + client
    # creds exist, it calls LinkedIn's refresh endpoint (via the single shared
    # linkedin_poster.refresh_access_token) and stores the new access + rotated
    # refresh_token; if it's within ~7d with NO usable refresh_token it emails a
    # LOUD alert so the owner re-seeds via OAuth. Leader-gated by a pg advisory
    # xact-lock (no double-rotate across the 2 replicas / in-hour re-fires) and
    # idempotent (post-refresh the expiry jumps ~60d out → later fires 'skip').
    # _hit() attaches X-Admin-Key. Kill: LINKEDIN_TOKEN_REFRESH_CRON_DISABLE=1
    # (checked BOTH here and in the endpoint).
    ("linkedin_token_refresh_daily",
     f"{BASE}/api/v1/linkedin/token/refresh-cron",
     "POST",
     lambda now: now.hour == 4 and now.minute < 55
                 and os.environ.get("LINKEDIN_TOKEN_REFRESH_CRON_DISABLE") != "1"),

    # 2026-07-24 growthfix wave: daily tick of the Growth-Loop Fix Master
    # Shell (#26) — read-only probe of the five fix-wave lanes; on completion
    # it beats the dead-man ledger (feed growthfix-shell-daily) itself.
    # _hit() attaches X-Admin-Key. Kill: GROWTHFIX_SHELL_DISABLE=1.
    ("growthfix_shell_daily",
     f"{BASE}/api/v1/admin/growthfix/master-tick",
     "POST",
     lambda now: now.hour == 5 and now.minute < 55
                 and os.environ.get("GROWTHFIX_SHELL_DISABLE") != "1"),

    # 2026-07-25 brain-ascension wave: daily tick of the Brain Ascension
    # Master Shell (#28) — read-only probe of the six audit-fix lanes; on
    # completion it beats the dead-man ledger (feed brain-ascension-shell-
    # daily) itself. _hit() attaches X-Admin-Key.
    # Kill: BRAIN_ASCENSION_SHELL_DISABLE=1.
    ("brain_ascension_shell_daily",
     f"{BASE}/api/v1/admin/brain-ascension/master-tick",
     "POST",
     lambda now: now.hour == 6 and now.minute < 55
                 and os.environ.get("BRAIN_ASCENSION_SHELL_DISABLE") != "1"),

    # 2026-07-25 surface-truth wave: daily tick of the Surface Truth Master
    # Shell (#30) — fetches the LIVE agent-facing surfaces through the edge and
    # compares them to ai_surface_canon. Built because the canonical-counts
    # FENCE went green while every served surface still published the retired
    # pre-dedup facility floor: the fence scans repo-root files that nothing
    # serves. Read-only; beats the dead-man ledger (feed surface-truth-shell-
    # daily) itself. _hit() attaches X-Admin-Key.
    # Kill: SURFACE_TRUTH_SHELL_DISABLE=1.
    ("surface_truth_shell_daily",
     f"{BASE}/api/v1/admin/surface-truth/master-tick",
     "POST",
     lambda now: now.hour == 8 and now.minute < 55
                 and os.environ.get("SURFACE_TRUTH_SHELL_DISABLE") != "1"),

    # 2026-08-11 shell #63: daily tick of the Context-Integrity Master Shell —
    # can the brain SEE? Lane 2 is the meter: 17 of 20 live L18 lessons were
    # the brain re-learning that its own probes go dark. Read-only; beats the
    # dead-man ledger (feed context-integrity-shell-daily) itself. _hit()
    # attaches X-Admin-Key.
    # ★09:00, AFTER surface-truth (08:00) and BEFORE the 12h L18 consolidation
    # at 16:40 — so a probe that went dark overnight is on the board before L18
    # consolidates another blindness lesson out of it.
    # Kill: CONTEXT_INTEGRITY_SHELL_DISABLE=1.
    ("context_integrity_shell_daily",
     f"{BASE}/api/v1/admin/context-integrity/master-tick",
     "POST",
     lambda now: now.hour == 9 and now.minute < 55
                 and os.environ.get("CONTEXT_INTEGRITY_SHELL_DISABLE") != "1"),

    # 2026-08-21 shell #64: daily tick of the Relay Closure Master Shell. The
    # agent→human relay has produced ZERO external human opens in its life
    # (relay_opens: 29 rows all-time, every one ours), which fires the stopping
    # rule check_relay_opens.py pre-registered in July. This shell keeps that
    # close AUDITABLE: lane A re-checks the redeem stage's published "writer is
    # off" declaration against the data, so re-enabling auto-redeem turns the
    # canon RED instead of quietly making it a lie; lane C republishes the
    # cohort sizes that make a per-platform transport experiment unrunnable, so
    # it reopens by itself if a named platform ever crosses the floor.
    # Read-only — it flips no flag, and lane C exists to say the flag must NOT
    # be flipped. Beats feed relay-closure-shell-daily itself. _hit() attaches
    # X-Admin-Key. Kill: RELAY_CLOSURE_SHELL_DISABLE=1.
    ("relay_closure_shell_daily",
     f"{BASE}/api/v1/admin/relay-closure-shell/master-tick",
     "POST",
     lambda now: now.hour == 9 and now.minute < 55
                 and os.environ.get("RELAY_CLOSURE_SHELL_DISABLE") != "1"),

    # 2026-08-22 shell #65: daily tick of the Agentic Loop Master Shell — the
    # scoreboard over the owner's 1-4 (graduate action classes on track record,
    # clear the human queues, close the learn station, measure the detector
    # merge rule), with the compounding metric row (claims confirmed ·
    # refuted-kept · retracted · granted classes · recurrence rate + 7d delta).
    # Report-only: the tick writes its own daily snapshot row (the 7d delta's
    # series) and beats feed agentic-loop-shell-daily itself; under
    # AGENTIC_LOOP_ARM=1 it may file part B's graduation proposals, bounded to
    # 3 inbox rows/day by its own ledger. Board: /admin/agentic-loop. The tick
    # route sits under /api/v1/brain/ (the Cloudflare bypass); _hit() attaches
    # X-Admin-Key. ★11:00, AFTER the 08-10 shells it reads beside (surface-
    # truth, context-integrity, relay-closure, seven-levers) so the inbox and
    # claim ledger it summarises have had the morning's detector passes.
    # Kill: AGENTIC_LOOP_SHELL_DISABLE=1.
    ("agentic_loop_shell_daily",
     f"{BASE}/api/v1/brain/agentic-loop/master-tick",
     "POST",
     lambda now: now.hour == 11 and now.minute < 55
                 and os.environ.get("AGENTIC_LOOP_SHELL_DISABLE") != "1"),

    # 2026-08-01 shell #47: daily tick of the Checkout Integrity Master Shell —
    # the four findings PR #2106 left open, each a way a checkout button is
    # wrong while looking right: the link 404s (a capital I for a lowercase l),
    # it bills the wrong amount (the $199 Pro link under a $299 label), it
    # sells a different plan than its label ("Upgrade to Pro" over the $99
    # founding link), or its capped program is sold out. The 08-01 drift proved
    # a static fence cannot see this — retired and canonical founding links
    # charged the SAME $99 for the SAME product, so only asking Stripe what a
    # link ACTUALLY charges tells them apart. Read-only: it DESCRIBES Stripe
    # objects, never creates or charges. Beats feed checkout-integrity-shell-
    # daily itself. _hit() attaches X-Admin-Key.
    # Kill: CHECKOUT_INTEGRITY_SHELL_DISABLE=1.
    ("checkout_integrity_shell_daily",
     f"{BASE}/api/v1/admin/checkout-integrity/master-tick",
     "POST",
     lambda now: now.hour == 9 and now.minute < 55
                 and os.environ.get("CHECKOUT_INTEGRITY_SHELL_DISABLE") != "1"),

    # 2026-07-25 shell #31: daily tick of the Intelligence Expansion Master
    # Shell — measures the five expansion fronts (RAG stage-2 rerank alive,
    # evidence/self-healing incl. zone-worker canon, media-growth SEE stage
    # landing, self-learning loops closing, LLM usage/cache efficiency) from
    # live state and beats the ledger (feed intelligence-expansion-shell-
    # daily) itself. Read-only. Kill: INTEL_EXPANSION_SHELL_DISABLE=1.
    ("intel_expansion_shell_daily",
     f"{BASE}/api/v1/admin/intelligence-expansion/master-tick",
     "POST",
     lambda now: now.hour == 9 and now.minute < 55
                 and os.environ.get("INTEL_EXPANSION_SHELL_DISABLE") != "1"),

    # 2026-07-25 shell #32: daily tick of the Seven Levers Master Shell —
    # one lane per lever of the 07-25 leverage ranking (zone sync,
    # recidivism, perf tail, cache, RAG anchors, loop census, media
    # followers). Read-only; beats the ledger (feed seven-levers-shell-
    # daily) itself. Kill: SEVEN_LEVERS_SHELL_DISABLE=1.
    # 2026-07-27: registry-truth scan — reads every tracked listing the way a
    # visitor does and records a four-state verdict. Runs after white-glove.
    # Kill: REGISTRY_TRUTH_DISABLE=1.
    # 2026-07-27: acquisition scan — weekly is enough; directories do not
    # appear daily and each scan is ~26 outbound fetches.
    # Kill: REGISTRY_ACQUISITION_DISABLE=1.
    ("registry_acquisition_scan_weekly",
     f"{BASE}/api/v1/admin/registry-acquisition/scan",
     "POST",
     lambda now: now.weekday() == 1 and now.hour == 21 and now.minute < 55
                 and os.environ.get("REGISTRY_ACQUISITION_DISABLE") != "1"),

    ("registry_truth_scan_daily",
     f"{BASE}/api/v1/admin/registry-truth/scan",
     "POST",
     lambda now: now.hour == 20 and 20 <= now.minute < 55
                 and os.environ.get("REGISTRY_TRUTH_DISABLE") != "1"),

    ("seven_levers_shell_daily",
     f"{BASE}/api/v1/admin/seven-levers/master-tick",
     "POST",
     lambda now: now.hour == 10 and now.minute < 55
                 and os.environ.get("SEVEN_LEVERS_SHELL_DISABLE") != "1"),

    # 2026-07-26 shell #33: daily tick of the Fix Closure Master Shell —
    # pins the 07-25/26 fix set (eia mirror, paid-key contract, envelope 79,
    # media hygiene, retention north-star, sync-source integrity) to live
    # checks and beats the ledger (feed fix-closure-shell-daily) itself.
    # Kill: FIX_CLOSURE_SHELL_DISABLE=1.
    ("fix_closure_shell_daily",
     f"{BASE}/api/v1/admin/fix-closure/master-tick",
     "POST",
     lambda now: now.hour == 11 and now.minute < 55
                 and os.environ.get("FIX_CLOSURE_SHELL_DISABLE") != "1"),

    # 2026-08-07: the analyst's pre-flight sweep — reviews every published
    # press release for completeness/accuracy and (when armed) quarantines the
    # broken ones, so a blank/future-dated/placeholder release cannot sit live
    # (the perplexity-citation incident). Report-only until
    # PRESS_INTEGRITY_ENFORCE=1. Kill: PRESS_INTEGRITY_DISABLE=1.
    ("press_integrity_daily",
     f"{BASE}/api/v1/admin/press-integrity/heal",
     "POST",
     lambda now: now.hour == 13 and now.minute < 55
                 and os.environ.get("PRESS_INTEGRITY_DISABLE") != "1"),

    # 2026-08-07 shell #52: daily tick of the Audit Closure Master Shell —
    # the closure organ for the 138-finding full-platform audit (10 lanes of
    # live checks + the finding registry); beats audit-closure-shell-daily
    # itself. Kill: AUDIT_CLOSURE_SHELL_DISABLE=1.
    ("audit_closure_shell_daily",
     f"{BASE}/api/v1/admin/audit-closure/master-tick",
     "POST",
     lambda now: now.hour == 12 and now.minute < 55
                 and os.environ.get("AUDIT_CLOSURE_SHELL_DISABLE") != "1"),

    # 2026-08-07: AUDIT INTAKE — turn shell #52's OPEN-RED registry rows into
    # brain worklist items (they were graded on a board and worked by nobody).
    # Fires an hour after the shell's own tick so the board it reads is fresh.
    # Cheap by construction: the endpoint no-ops while its brain_state
    # snapshot is younger than AUDIT_INTAKE_TTL_S (6h), so a sporadic
    # heartbeat cannot multiply the probe cost. Admin-gated (_hit sends
    # X-Admin-Key). Kill: AUDIT_INTAKE_DISABLE=1.
    ("audit_intake_refresh",
     f"{BASE}/api/v1/brain/audit-intake/refresh",
     "POST",
     lambda now: now.hour == 13 and now.minute < 55
                 and os.environ.get("AUDIT_INTAKE_DISABLE") != "1"),

    # 2026-08-30: QA SUPER-USER INTAKE — turn the outside-in board's
    # canary-verified REDs into brain worklist items. Same class of gap as the
    # audit intake above: the board was graded every 4h and worked by nobody.
    #
    # ★ REGISTERED IS NOT SCHEDULED. The blueprint + the heal-path wiring do
    # NOT make this lane run; without this entry the brain_state snapshot is
    # written once (never) and qa_superuser_findings() serves [] forever while
    # every dashboard reports the feature as shipped. That is the class called
    # out three entries up (shell #48, red for 132h).
    #
    # Cadence: qa-superuser.yml runs every 4h at :34, so this fires at
    # hour%4==2 :40-:45 — ~1h26m after each board run, enough slack for
    # GitHub's schedule throttling in this repo (measured median gap 29.7min,
    # p90 72.6) to still deliver a board before we read it. The heartbeat
    # itself beats every 5 minutes, hence the NARROW minute window: a
    # `minute < 55` predicate here would dispatch ~11x per qualifying hour.
    # Cheap regardless: the endpoint no-ops while its snapshot is younger than
    # QA_INTAKE_TTL_S (1h). Admin-gated (_hit sends X-Admin-Key).
    # Kill: QA_INTAKE_DISABLE=1.
    ("qa_superuser_intake_refresh",
     f"{BASE}/api/v1/brain/qa-superuser-intake/refresh",
     "POST",
     lambda now: now.hour % 4 == 2 and now.minute >= 40 and now.minute < 45
                 and os.environ.get("QA_INTAKE_DISABLE") != "1"),

    # 2026-08-07 (audit SH52-001): the loop-control shell (#48) shipped with a
    # declared beat and NO scheduler — 4th firing of the registered≠scheduled
    # class; it sat red 132h. Drive it like its 8 sibling shells.
    # Kill: LOOP_CONTROL_SHELL_DISABLE=1.
    ("loop_control_shell_daily",
     f"{BASE}/api/v1/admin/loop-control/master-tick",
     "POST",
     lambda now: now.hour == 5 and now.minute < 55
                 and os.environ.get("LOOP_CONTROL_SHELL_DISABLE") != "1"),

    # 2026-08-30: SITE-QA INTAKE — the website master's open alerts become
    # brain worklist items. Third instance of the same gap the audit intake
    # closed: site_qa.py has probed every public surface every 15 minutes for
    # months (measured 2,520 results/24h over 28 tests) and its alerts reached
    # a GitHub issue and an HTML dashboard, never the loop that works them.
    #
    # ★ REGISTERED IS NOT SCHEDULED — same reason spelled out on the qa
    # super-user entry: without this line the blueprint and the heal wiring
    # are both present, every dashboard calls the feature shipped, and
    # site_qa_findings() serves [] forever.
    #
    # HOURLY (not 4-hourly like the qa-superuser intake) because this board is
    # rewritten 4x an hour; the endpoint no-ops while its snapshot is younger
    # than SITE_QA_INTAKE_TTL_S (1h), so the real cost is one DB read per hour.
    # Minute window 50-55 was unused across the file, and the heartbeat beats
    # every 5 minutes, so a wider window would dispatch repeatedly per hour.
    # Kill: SITE_QA_INTAKE_DISABLE=1.
    ("site_qa_intake_refresh",
     f"{BASE}/api/v1/brain/site-qa-intake/refresh",
     "POST",
     lambda now: now.minute >= 50 and now.minute < 55
                 and os.environ.get("SITE_QA_INTAKE_DISABLE") != "1"),

    # (2026-08-07 review note: this branch briefly added GET dispatch entries
    # for the #50/#51 pull-only boards and a registry-freshness entry. All
    # three were removed pre-merge: the liveness ticks are pure reads with no
    # beat or persistence — a cron hitting them changes nothing (shell #52
    # now consumes #51's health_signal lane IN-PROCESS instead) — and
    # registry-freshness was ALREADY dispatched at hour 17 below; a second
    # row would have double-fired the same label.)

    # 2026-07-25 (#28 wave 2): daily merged-PR metric snapshot — harvest
    # brain merges, snapshot canonical KPIs (merge phase), re-stamp d14/d30.
    # Idempotent (UNIQUE pr/phase/metric). Kill: BRAIN_PR_METRICS_DISABLE=1.
    ("brain_pr_metrics_daily",
     f"{BASE}/api/v1/admin/brain/pr-metrics/tick",
     "POST",
     lambda now: now.hour == 7 and now.minute < 55
                 and os.environ.get("BRAIN_PR_METRICS_DISABLE") != "1"),

    # 2026-07-25 (#29): daily cross-domain Loop & Flywheel board — read-only
    # probe of the nine domain lanes; beats its own dead-man feed.
    # Kill: LOOP_FLYWHEEL_SHELL_DISABLE=1.
    ("loop_flywheel_shell_daily",
     f"{BASE}/api/v1/admin/loop-flywheel/master-tick",
     "POST",
     lambda now: now.hour == 8 and now.minute < 55
                 and os.environ.get("LOOP_FLYWHEEL_SHELL_DISABLE") != "1"),

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

    # 2026-08-10: PRESS PIPELINE master shell — daily 19:xx UTC, AFTER the
    # composer window (the 08-09 storm ran 15:34-17:44 UTC) so the 24h ratios
    # see a complete day of attempts rather than a half-finished one. Pure
    # read: five stage-to-stage ratios, no actuator. This is the lane that
    # would have caught a five-day publish outage on day three while every
    # single-stage dashboard still read healthy.
    # Kill: PRESS_PIPELINE_SHELL_DISABLED.
    ("press_pipeline_master_tick_daily",
     f"{BASE}/api/v1/admin/press-pipeline/master-tick",
     "POST",
     lambda now: now.hour == 19 and now.minute < 55),

    # 2026-07-15: MEDIA GROWTH master shell — daily 14:xx UTC (AFTER the 13 UTC
    # engagement sync + media master, so it manages on fresh data). SEE->GOAL->
    # MANAGE toward a follower/citation target. DARK by default (measures only
    # unless MEDIA_GROWTH_ACT_ENABLED=1). Kill: MEDIA_GROWTH_DISABLED.
    ("media_growth_master_tick_daily",
     f"{BASE}/api/v1/admin/media-growth/master-tick",
     "POST",
     lambda now: now.hour == 14 and now.minute < 55),

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

    # 2026-07-18: Precision & Gas Depth master shell (#24) — the tier past Depth:
    # point-level transmission/pipeline proximity, the gas layer (basis + pipeline),
    # and live connectivity (PeeringDB / cloud on-ramp). Also closes the scoreboard
    # dc_share_pct:0 gap. SHIPS SHADOW — ACT off unless PRECISION_DEPTH_MASTER_ACT_ENABLE=1
    # (measures + files sourced specs to brain until armed). Runs 13:xx UTC, AFTER the
    # grid family (depth=10/grid-data=11/monetize=12) so dc_load_share reads fresh
    # dc_load_queue once armed. Kill: PRECISION_DEPTH_MASTER_DISABLED.
    ("precision_depth_master_tick_daily",
     f"{BASE}/api/v1/admin/precision-depth/master-tick",
     "POST",
     lambda now: now.hour == 13 and now.minute < 55),

    # 2026-07-11: MONETIZE & RETAIN master shell — the fourth motion after the
    # three moat pillars. Meters grid/fiber paid-demand usage into leads
    # (NEVER blocks; enforcement = separate MONETIZE_METERED_ENFORCE, default
    # off), snapshots weekly retention (pillar-3 truth), and canaries the
    # structured-citation interconnect contract. One bounded action/tick.
    # Kill: MONETIZE_MASTER_DISABLED. Daily 12:xx UTC (offset from grid=11/depth=10).
    ("monetize_master_tick_daily",
     f"{BASE}/api/v1/admin/monetize/master-tick",
     "POST",
     lambda now: now.hour == 12 and now.minute < 55),

    # 2026-07-22: wire the FOUR read-only master-shell scoreboards that were
    # built but never scheduled — each ships /<slug>/master-tick + an
    # /admin/<slug> dashboard, but no driver (cron_heartbeat / dchub-scheduler /
    # GH-Action) ever hit them, so the snapshot only refreshed when a human
    # opened the pane. Same "built but never scheduled" gap as the 07-03 rewire
    # above and the crawler_scheduler _RUNNERS gaps. All four are READ-ONLY by
    # construction (aggregate / probe / draft only — never mutate, publish, or
    # email), admin-gated (_hit sends X-Admin-Key), fail-soft + timeout-bounded,
    # and individually killable. Daily, spread across the quiet 20-23 UTC block
    # so the aggregators read fresh state from the daily shells (5-14 UTC) they
    # summarize. Marked heavy in _HEAVY_LABELS (3-wide throttle).
    ("deepdive_master_tick_daily",              # kill: DEEPDIVE_DISABLED
     f"{BASE}/api/v1/admin/deepdive/master-tick",
     "POST",
     lambda now: now.hour == 20 and now.minute < 55),
    ("pillars_master_tick_daily",               # kill: PILLARS_SHELL_DISABLED
     f"{BASE}/api/v1/admin/pillars/master-tick",
     "POST",
     lambda now: now.hour == 21 and now.minute < 55),
    ("qa_fixwave_master_tick_daily",            # kill: QA_FIXWAVE_DISABLED
     f"{BASE}/api/v1/admin/qa-fixwave/master-tick",
     "POST",
     lambda now: now.hour == 22 and now.minute < 55),
    ("roadmap_master_tick_daily",               # kill: ROADMAP_SHELL_DISABLED
     f"{BASE}/api/v1/admin/roadmap/master-tick",
     "POST",
     lambda now: now.hour == 23 and now.minute < 55),

    # 2026-07-03: brain RAG reindex — embed new findings/recs/news/deals into
    # brain_corpus_embeddings so the L6 planner's semantic recall stays fresh.
    # Every 4h at :20, cap 500/run (incremental backfill; catches up over runs).
    # Kill: BRAIN_RAG_DISABLED.
    ("brain_rag_reindex_4h",
     f"{BASE}/api/v1/admin/brain/rag/reindex?cap=500",
     "POST",
     lambda now: now.hour % 4 == 0 and now.minute >= 20 and now.minute < 25),

    # 2026-07-18: Agentic Master Shell — demand-miss clustering, research-queue
    # drain, golden-check sentinels (answer + deploy, SHADOW), permitting news
    # scan, standing-intent webhook evals. Every 2h odd hours at :15.
    # Kill: AGENTIC_MASTER_DISABLED (per-cap: AGENTIC_<CAP>_DISABLED).
    ("agentic_master_tick_2h",
     f"{BASE}/api/v1/admin/agentic/master-tick",
     "POST",
     lambda now: now.hour % 2 == 1 and now.minute >= 15 and now.minute < 20),

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

    # 2026-07-14: Frontend-Reliability shell — DAILY shadow tick at 03:xx UTC.
    # Scores the 3 public-page reliability lanes (slow_path_cache / false_close_
    # refire / edge_cacheability), files ONE bounded finding on the weakest.
    # Generalizes the #100095 /api/pipeline SWR-cache fix so the class is caught
    # continuously. SHADOW unless FRONTEND_RELIABILITY_MASTER_ARM=1.
    # Kill: FRONTEND_RELIABILITY_MASTER_DISABLED.
    ("frontend_reliability_master_tick_daily",
     f"{BASE}/api/v1/admin/frontend-reliability/master-tick",
     "POST",
     lambda now: now.hour == 3 and now.minute < 8),

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
    # ★ WAS hour==14 only ("daily"), against a 30s cache on a ~10s tick — so
    #   the cache was cold for ~23h59m/day and every human visit 502'd at the
    #   CF edge (measured 2026-08-08: edge 502 in 10.2s, origin 200 in 9.8s).
    #   Now every ~10 min, which keeps the (now 600s) cache warm so a visitor
    #   is served from memory instead of paying the compute. Pure-DB and
    #   idempotent; the snapshot row is what the daily trend reads.
    ("flywheel_master_tick",
     f"{BASE}/api/v1/admin/flywheel/master-tick",
     "POST",
     lambda now: now.minute % 10 < 5),

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

    # 2026-07-11: Cadence dead-man sentinel — evaluates the lane registry
    # (publisher cadences, ingest freshness, automerge activity, citations)
    # and files cadence_stall_* brain findings on gap/queue stalls. Built
    # after 3 silent stalls (Bluesky 29h, gridstatus 7d, LinkedIn verdict
    # queue) that nothing alerted on. Pure-DB + findings-only; idempotent
    # (canonical upsert dedupe + 10-min in-process tick cache), so the wide
    # minute window's in-hour re-fires are cheap no-ops. Every 3h.
    # Deliberately NOT in _HEAVY_LABELS / _WORKER_PROXY_*: it's a light
    # aggregate-query tick, and the alarm must run on WEB — several watched
    # lanes live on the worker, so a wedged worker would kill a worker-
    # hosted sentinel along with the lanes it exists to report.
    # _hit() attaches X-Admin-Key. Kill: CADENCE_SENTINEL_DISABLE=1.
    ("cadence_sentinel_tick_3h",
     f"{BASE}/api/v1/admin/cadence-sentinel/master-tick",
     "POST",
     lambda now: now.hour % 3 == 2 and now.minute < 55),

    # 2026-07-14: Daily-page canary — fetches the public /daily page and HEADs
    # every tile (+ its onerror fallback), filing daily_page_broken_tiles only
    # when a user would actually see broken images. Complements the R2 inventory
    # /status probe, which checks the bucket, not the rendered public page —
    # added after a nightly UTC-rollover gap (page date flips at 00:00 UTC, cron
    # writes the folder ~06:00 UTC) showed broken tiles that nothing alerted on.
    # Findings-only + idempotent; light HTTP tick, kept on WEB. Every 3h at
    # hours 0/3/… so two ticks land inside the 00:00–06:00 UTC gap window,
    # offset from the cadence sentinel. Kill: DAILY_PAGE_CANARY_DISABLE=1.
    ("daily_page_canary_3h",
     f"{BASE}/api/jobs/daily-page-canary",
     "POST",
     lambda now: now.hour % 3 == 0 and now.minute < 55),

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

    # 2026-07-11 (Gemini dark-fiber §4.3): DARK-AVAILABILITY ZONES rebuild —
    # crosses dark-capable carriers (fiber_providers.dark_fiber=TRUE, alias-
    # mapped to PeeringDB carrier names) against carrier_facility_presence
    # and upserts ~800 INFERRED screening-zone rows into fiber_coverage_zones
    # (zone_type='dark_availability'). Every row is stamped v:"inferred" —
    # capability × presence inference, NEVER confirmed strand availability.
    # Daily 01:xx UTC (quiet hour — 02..17/21/22 are taken); WIDE minute
    # window because the heartbeat is sporadic; the endpoint SELF-THROTTLES
    # (skips if rebuilt <20h ago) + upserts ON CONFLICT(zone_id), so in-hour
    # re-fires are one cheap SELECT. Admin-gated — _hit() sends X-Admin-Key.
    # No-deploy kill switch checked BOTH here and in the endpoint:
    # DARK_ZONES_DISABLE=1.
    ("dark_zones_rebuild_daily",
     f"{BASE}/api/v1/admin/dark-zones/rebuild",
     "POST",
     lambda now: now.hour == 1 and now.minute < 55
                 and os.environ.get("DARK_ZONES_DISABLE") != "1"),

    # 2026-07-11: METRIC GROUND-TRUTH CHECKER — weekly recompute of 5
    # headline shell metrics straight from the raw tables (media posts/24h,
    # citation velocity 7d, verified facilities, real agents/wk, automerge
    # activity); files a brain finding via the canonical writer when a
    # shell-reported value diverges >30% from the recompute or the lane's
    # snapshot went stale. Motivated by this week's 35-vs-100 media score
    # and 5-vs-4,903 verified-facilities measurement bugs — both invisible
    # because nothing re-derived the headlines independently. Sun 15:xx UTC
    # (empty slot; analyst-note owns Thu 14); WIDE minute window because the
    # heartbeat is sporadic; endpoint is IDEMPOTENT per ISO week
    # (metric_truth_runs PK claim — repeat fires are cheap no-ops); admin-
    # gated — _hit() sends X-Admin-Key. No-deploy kill switch checked BOTH
    # here and in the endpoint: METRIC_TRUTH_CHECK_DISABLE=1.
    ("metric_truth_check_weekly",
     f"{BASE}/api/v1/admin/metric-truth/check",
     "POST",
     lambda now: now.weekday() == 6 and now.hour == 15 and now.minute < 55
                 and os.environ.get("METRIC_TRUTH_CHECK_DISABLE") != "1"),

    # 2026-07-15 (r-slug-freeze recurrence guard): FREEZE canonical_slug for
    # facilities ingested AFTER the 2026-07-03 one-shot admin freeze. That
    # freeze only ran via a manual admin POST, so every newly-discovered row
    # keeps canonical_slug NULL and can later re-hash exactly like the 6,168
    # /facilities URLs that broke. This nightly tick re-runs the SET-ONCE
    # backfill (WHERE canonical_slug IS NULL — it can NEVER overwrite a frozen
    # value; the slug is pinned once and never rewritten). Endpoint is
    # idempotent (set-once + ON CONFLICT DO NOTHING). Admin-gated via _hit().
    # Daily 23:xx UTC (empty slot); narrow minute window bounds re-fires.
    # Kill: SLUG_FREEZE_CRON_DISABLE=1.
    ("slug_freeze_backfill_daily",
     f"{BASE}/api/v1/admin/slug/freeze",
     "POST",
     lambda now: now.hour == 23 and now.minute < 8
                 and os.environ.get("SLUG_FREEZE_CRON_DISABLE") != "1"),
]

# r-poolfix (2026-07-04): the DB/LLM-heavy ticks. When a herd of these comes
# due on the same hour-boundary heartbeat, _dispatch_all runs them at most
# 3-wide (light jobs stay 8-wide) so they can't collectively pin the pool —
# the 03:17 80/80 saturation. Most are also delegated to the worker
# (main.py _WORKER_PROXY_*), so this doubly bounds the worker's concurrency.
_HEAVY_LABELS = frozenset({
    # shell #52: ~45s budget of live probes (llms/edge/MCP) — throttle-pool it.
    "audit_closure_shell_daily",
    # shell #65 (2026-08-22): in-process /ops/claims read (up to 10 GitHub
    # reads for the detector predicate), a digest render, a RAG recall and
    # ~15 DB reads per tick — same class as #52, throttle-pool it.
    "agentic_loop_shell_daily",
    # the intake runs that SAME tick to read its registry — equally heavy.
    "audit_intake_refresh",
    "audience_master_tick_daily", "growth_master_tick_4h",
    "media_master_tick_daily", "distribution_master_tick_daily",
    "grid_data_master_tick_daily", "gap_master_tick_6h",
    "depth_master_tick_daily",
    "reliability_master_tick_daily", "frontend_reliability_master_tick_daily",
    "rag_master_tick_daily",
    "fixwave_master_tick_2h", "agent_onboarding_master_tick_daily",
    "conversion_loop_master_tick_daily", "agent_usefulness_master_tick_daily",
    "agent_pay_master_tick_daily",
    "brain_detectors_daily", "brain_issue_janitor_daily",
    "brain_lane_driver_6h", "brain_rag_reindex_4h", "brain_warmer_hourly",
    "agentic_master_tick_2h",
    "iso_queue_ingest_daily", "gas_feeds_ingest_daily",
    "dedup_drain",
    "reach_rollup_daily", "market_deep_dive_rotate_daily",
    "strategic_synthesis_weekly", "strategic_digest_weekly",
    "analyst_note_weekly", "metric_truth_check_weekly",
    "dark_zones_rebuild_daily",
    "slug_freeze_backfill_daily",
    # 2026-07-22: the 4 read-only master-shell scoreboards wired into _DISPATCH.
    "deepdive_master_tick_daily", "pillars_master_tick_daily",
    "qa_fixwave_master_tick_daily", "roadmap_master_tick_daily",
})


# ── Re-fire suppression for entries that are NOT safe to overlap ─────────────
# ★★ Every OTHER entry in _DISPATCH is idempotent by construction (a DELETE, an
# upsert, a dedup-logged send), which is exactly why the minute windows above
# are deliberately WIDE: cron-heartbeat.yml is scheduled '1-59/5 * * * *' but
# GitHub drops most of those fires under load (measured 2026-08-02: ONE fire
# landed in the whole 04:00 hour), so a wide window is how a job survives at
# all. "Overlapping/repeat fires within a window are harmless" is the stated
# contract for this table.
#
# land_power_sync_incremental BREAKS that contract. It POSTs
# /api/land-power/sync, which spawns an UNGUARDED daemon thread running a
# 4-source crawl of 75k+ row ArcGIS layers — hifld-substations alone measured
# 4,740s (79 min) on 2026-08-02. NEITHER land-power entry point has a
# single-flight guard, so each re-fire inside the 55-minute window starts
# ANOTHER full crawl writing the same rows to the same tables. At the scheduled
# 5-minute cadence that is up to 11 concurrent writers racing on
# substations_name_lat_lng_uniq — and that source currently reports
# verdict=never_succeeded with 73,328 duplicate-key errors out of 75,328 rows.
#
# label -> minimum seconds between fires, enforced here in the dispatcher.
_MIN_REFIRE_S = {
    "land_power_sync_incremental": 6 * 3600,
    # the pending-drafts digest SENDS AN EMAIL — a repeat fire inside its
    # 50-minute window would mail the operator twice. Best-effort/per-process
    # (see _refire_suppressed): an occasional duplicate to the operator inbox
    # beats the alternative this replaces, which was the digest never firing.
    "media_pending_drafts_digest": 6 * 3600,
    # shell #52's tick is heavy (live probes); the <55-min window must not
    # stack it on repeat heartbeat runs within the hour.
    "audit_closure_shell_daily": 6 * 3600,
    # the intake re-runs that tick to read its registry — same shape, same
    # window, so it needs the same per-hour stacking guard.
    "audit_intake_refresh": 6 * 3600,
    # shell #65's tick is idempotent (its snapshot is an upsert, its filing is
    # ledger-bounded) but heavy (see _HEAVY_LABELS); one fire per window.
    "agentic_loop_shell_daily": 6 * 3600,
}
_LAST_FIRED = {}
_LAST_FIRED_LOCK = threading.Lock()


def _refire_suppressed(label, now):
    """True when `label` fired too recently to be fired again.

    ★ BEST EFFORT, PER PROCESS. Railway runs more than one worker, so this
    cannot promise cluster-wide single-flight — it collapses the repeat fires
    that land on the same worker, which is the common case for a heartbeat
    polled every 5 minutes. The durable fix is a single-flight guard at the
    crawl itself; this is the dispatcher declining to be the thing that stacks
    them. Recording the fire is a SIDE EFFECT, so call it exactly once per
    label per heartbeat.
    """
    window = _MIN_REFIRE_S.get(label)
    if not window:
        return False
    with _LAST_FIRED_LOCK:
        prev = _LAST_FIRED.get(label)
        if prev is not None and (now - prev).total_seconds() < window:
            return True
        _LAST_FIRED[label] = now
        return False


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
    # ★ _refire_suppressed RECORDS the fire, so it must be called at most once
    # per label per heartbeat — hence the explicit loop rather than two
    # comprehensions over _DISPATCH.
    due, skipped = [], []
    for (label, url, method, pred) in _DISPATCH:
        if not pred(started):
            skipped.append(label)
        elif _refire_suppressed(label, started):
            skipped.append(label + " (re-fire suppressed)")
        else:
            due.append((label, url, method))

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

        # r-cron-outcome (2026-08-29): READ the futures. They were submitted
        # and dropped, so _hit's result went nowhere and a job that 500'd, timed
        # out, or answered {"ok":false,"disabled":true} at HTTP 200 was
        # indistinguishable from one that did its work. Only NON-OK outcomes are
        # recorded — see cron_observability.record_job_outcomes.
        outcomes = []
        outcomes.extend(_run_batch(light, 8))
        outcomes.extend(_run_batch(heavy, 3))
        if outcomes:
            for o in outcomes:
                logger.warning("cron job did not do its work: %s -> %s (%s) %s",
                               o.get("label"), o.get("outcome"),
                               o.get("status"), (o.get("detail") or "")[:120])
            try:
                from routes.cron_observability import record_job_outcomes
                record_job_outcomes(outcomes)
            except Exception:
                pass

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
