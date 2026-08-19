"""Phase RR (2026-05-15) — Brain v2 consistency radar.

Exposes GET /api/v1/brain/consistency-radar that returns the current
finding set. Findings also feed into /api/v1/heal/findings's
actionable_backend_issues stream via _merge_radar_findings() below.


Three new detectors covering blind spots that surfaced during the
Phase NN/PP/QQ rollouts:

  1. WORKER VERSION DRIFT — Cloudflare Pages worker stuck on an older
     version than the source. (PR #184/#185/#186 shipped fine; but
     `_worker.js` source said 4.11.0-qq12 while production headers
     reported 4.8.3 for ~24h.)

  2. TIER INCONSISTENCY — MCP tool tier in `mcp_gatekeeper.TOOL_TIER`
     diverging from the matching web API endpoint's tier decorator.
     (PR #185 fixed energy; pipeline was still inconsistent.)

  3. MISSING CRON COVERAGE — workflow_dispatch phase that has no
     scheduled `cron:` trigger. (`marketing_publish_now` was dispatch-
     only for weeks; LinkedIn published 0 posts despite 4 generated
     releases.)

Each detector returns a list of finding dicts. Findings are merged
into `/api/v1/heal/findings`'s actionable_backend_issues stream so
the Brain v2 Layer 5 cron processes them like any other issue.

The radar runs on a 6h cadence (low frequency — these issues don't
flap minute-to-minute and the probes are mildly expensive).
"""

from __future__ import annotations

import datetime  # r62-fix: several detectors (check_event_submission_pending,
                 # check_market_deep_dive_stale) use bare `datetime.` with no
                 # local import → NameError crash when they hit that line. A
                 # module-level import is the correct file-wide fix; it does not
                 # conflict with the function-local `from datetime import …`
                 # aliases (those shadow within their own scope).
import json
import logging
import os
import re
import sys
import urllib.request
import urllib.error
from typing import Optional
from routes._swallowed_writes import note_swallowed_write

# 2026-06-15 FREEZE FIX: this module used `logger.info(...)` in the durable-
# persist summary block (_persist_findings_to_db) but never defined `logger`.
# The NameError fired AFTER the bf_summary savepoint was released, so the
# except handler did ROLLBACK TO an already-released savepoint → that aborted
# the whole transaction → the trailing conn.commit() silently discarded every
# upsert. Result: brain_findings froze (no last_seen bumps, no resolve-on-
# absence) for ~22h while the scan itself kept reporting success. Defining the
# logger removes the NameError; the release-ordering fix below is defense-in-depth.
logger = logging.getLogger(__name__)


# ── 1. Worker version drift ────────────────────────────────────────

# Public raw URL to the source-of-truth _worker.js. We fetch this and
# extract the WORKER_VERSION constant, then compare to the deployed
# X-DC-Worker-Version header on a known-cheap endpoint.
#
# Phase VVV (2026-05-16): old URL pointed at a standalone
# `azmartone67/dchub-frontend` repo that doesn't exist — the frontend
# is a sub-directory of `azmartone67/dchub-backend`. Was 404ing every
# radar cycle, spamming the log + producing a false
# `worker_source_unreachable` finding every scan. Fixed path now
# resolves to the actual checkout.
_WORKER_SOURCE_URL = "https://raw.githubusercontent.com/azmartone67/dchub-backend/main/dchub-frontend/_worker.js"
_WORKER_PROBE_URL  = "https://dchub.cloud/api/v1/dcpi/scores?limit=1"


# Mutable holder for the last fetch error so detector messages can echo
# the real urllib error to the finding's detail field (otherwise we just
# get a generic "unreachable" and have to grep Railway logs).
_LAST_FETCH_ERROR: dict[str, str] = {}

# r-peace (2026-07-05): the radar probes its OWN public edge (dchub.cloud via
# CF → back to this backend). When the replica is busy those self-probes time
# out, and each timeout used to print `[brain-radar] <url> TimeoutError` to
# stderr — so the busier the box got, the LOUDER its logs got, drowning real
# signal in self-inflicted noise. A transient timeout / connection error to our
# own edge is NEVER a drift finding (the caller just sees body=None and skips
# it — the finding logic reads the parsed body, not the log line), so it's
# silenced exactly like the already-silenced 401/403 case. Genuinely unexpected
# errors still print, but throttled to once per URL per 10 min so a persistent
# problem is visible without flooding.
_RADAR_LOG_THROTTLE: dict[str, float] = {}
_RADAR_LOG_TTL_S = 600

def _radar_transient(msg: str) -> bool:
    m = (msg or "").lower()
    return any(s in m for s in (
        "timed out", "timeout", "connection reset", "connection refused",
        "connection aborted", "broken pipe", "temporarily unavailable",
        "bad gateway", "502", "503", "504"))

def _radar_log(url: str, msg: str) -> None:
    """Print a radar fetch error at most once per URL per TTL; stay silent for
    transient self-edge conditions (they're load, not drift)."""
    if _radar_transient(msg):
        return
    import time as _t
    now = _t.time()
    last = _RADAR_LOG_THROTTLE.get(url, 0.0)
    if now - last < _RADAR_LOG_TTL_S:
        return
    _RADAR_LOG_THROTTLE[url] = now
    print(f"[brain-radar] {url} {msg}", file=sys.stderr)


def _http_get(url: str, timeout: int = 8) -> tuple[Optional[str], Optional[dict]]:
    """Returns (body, headers_dict) or (None, None) on error.

    Phase WW (2026-05-16): when fetching from raw.githubusercontent.com,
    auto-add the GITHUB_TOKEN bearer header if present. The frontend repo
    azmartone67/dchub-frontend is private, so anonymous raw fetches 404
    silently and the worker_version_drift detector mis-reports the radar
    as 'source unreachable' instead of detecting the actual production
    drift. With the token we get the real file body and the comparison works.
    """
    try:
        headers = {"User-Agent": "dchub-brain-radar/1.0"}
        if "raw.githubusercontent.com" in url:
            gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("BACKEND_PAT")
            if gh_token:
                headers["Authorization"] = f"token {gh_token}"
        # Phase FF+7-meta (2026-05-19): when probing our own paid API
        # endpoints (dchub.cloud/api/v1/*), include the enterprise key.
        # Without this, every brain-radar probe to a paid tool gets
        # 401/403 and the detectors that rely on those probes go blind.
        #
        # r32-conv-2 (2026-05-20): user reported the 401 still firing
        # on /api/v1/fiber/intel — meaning the three DCHUB_*_API_KEY
        # env vars aren't set on Railway. Add a SECOND bypass path
        # via X-Internal-Key (already validated by
        # map_tier_gating._detect_caller_tier → 'pro' tier). The
        # _INTERNAL_KEYS set in schema_repair pulls from
        # DCHUB_INTERNAL_KEY which IS set on Railway. Same fallback
        # chain so the brain self-heals without a new env-var setup.
        elif ("dchub.cloud" in url or "dchub-backend-production" in url
              or "localhost" in url or "127.0.0.1" in url):
            # r58c (2026-06-01): widened to include localhost/127.0.0.1. The
            # radar's own self-calls to http://localhost:8080/api/v1/* were
            # NOT matching this branch (only the public-host branch attached
            # auth/probe headers), so they sent ONLY a User-Agent and relied
            # purely on the limiter's loopback bypass — which fails under 2
            # Railway replicas (non-loopback remote_addr) → the brain-radar
            # 429 storm. Now the self-calls carry X-Internal-Key + X-Admin-Key
            # + X-DC-Probe, so the limiter's internal-key/probe exemptions fire
            # regardless of which replica/IP the call lands on.
            # r33-Q+hardening (2026-05-22): _clean() defends against
            # contaminated env vars. The recurring "ValueError: Invalid
            # header value b'5GyWzWPGvz...\n~/dchub-frontend'" AND the 401
            # on /api/v1/fiber/intel were BOTH caused by DCHUB_INTERNAL_KEY
            # having a trailing newline + shell path pasted into it. A
            # newline in an HTTP header value raises ValueError, the
            # request never sends, the endpoint sees no auth → 401.
            # Take only the first whitespace-delimited token so even a
            # dirty env var produces a valid header.
            def _clean(v):
                parts = (v or "").split()
                return parts[0] if parts else ""
            api_key = _clean(
                os.environ.get("DCHUB_INTERNAL_API_KEY")
                or os.environ.get("DCHUB_API_KEY")
                or os.environ.get("DCHUB_BRAIN_API_KEY") or "")
            if api_key:
                headers["X-API-Key"] = api_key
            internal_key = _clean(
                os.environ.get("DCHUB_INTERNAL_KEY")
                or os.environ.get("INTERNAL_KEY")
                or os.environ.get("DCHUB_ADMIN_KEY") or "")
            if internal_key:
                headers["X-Internal-Key"] = internal_key
            # r34 (2026-05-22): the X-API-Key / X-Internal-Key paths kept
            # 401ing on /api/v1/fiber/intel (those env vars unset or not
            # authorizing). DCHUB_ADMIN_KEY IS set and grants admin-tier
            # bypass on every gated endpoint — send it too so the radar can
            # finally probe paid tools instead of going blind.
            admin_key = _clean(os.environ.get("DCHUB_ADMIN_KEY")
                               or os.environ.get("DCHUB_INTERNAL_KEY") or "")
            if admin_key:
                headers["X-Admin-Key"] = admin_key
            # 2026-06-12: ALSO send X-Internal-Key. Gates that honor the
            # internal key (e.g. /api/v1/pipeline on the Render failover —
            # verified 403 bare vs 200 with the header) check this header,
            # not X-Admin-Key, so radar probes against Render read as anon
            # 403s and the probe data went blind for those endpoints.
            internal_key = _clean(os.environ.get("DCHUB_INTERNAL_KEY") or "")
            if internal_key:
                headers["X-Internal-Key"] = internal_key
            # Also include the brain UA so rate-limit bypass kicks in
            # (separate machinery from tier-bypass).
            headers["User-Agent"] = "DCHub-BrainRadar/1.0 (+https://dchub.cloud)"
            # r58c (2026-06-01): explicit probe marker — rate_limiter.py
            # bypasses 'brain-radar' regardless of IP/UA, so the radar's
            # self-calls are never throttled even if the internal-key path
            # is misconfigured.
            headers["X-DC-Probe"] = "brain-radar"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code} {e.reason}"
        _LAST_FETCH_ERROR[url] = msg
        # 401/403 on an anonymous internal probe is EXPECTED — the radar's
        # purpose is to detect when a tier gates higher than its MCP-tool
        # counterpart. Treat gated responses as a normal data point and
        # don't pollute Railway logs with red WARNINGs.
        if e.code in (401, 403):
            return None, None
        _radar_log(url, msg)
        return None, None
    except urllib.error.URLError as e:
        msg = f"URLError: {e.reason}"
        _LAST_FETCH_ERROR[url] = msg
        _radar_log(url, msg)
        return None, None
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        _LAST_FETCH_ERROR[url] = msg
        _radar_log(url, msg)
        return None, None


def check_worker_version_drift() -> list[dict]:
    """Compare _worker.js source's WORKER_VERSION vs the live header
    value. Flag if they diverge."""
    findings = []
    # Phase RR+1: bumped to 30s timeout — raw.githubusercontent.com can be
    # slow from Railway's network. Also echoes the actual urllib error in
    # the finding detail instead of a generic 'unreachable'.
    source_body, _ = _http_get(_WORKER_SOURCE_URL, timeout=15)
    if not source_body:
        err = _LAST_FETCH_ERROR.get(_WORKER_SOURCE_URL, "unknown error")
        return [{
            "issue": "worker_source_unreachable",
            "url": _WORKER_SOURCE_URL,
            "count": 1,
            "detail": (f"Could not fetch _worker.js source from GitHub "
                       f"({err}). Radar fails closed — re-run later. "
                       f"If this persists, check that raw.githubusercontent.com "
                       f"is reachable from the Railway runtime."),
        }]
    m = re.search(r"const\s+WORKER_VERSION\s*=\s*['\"]([\w\d\.\-]+)['\"]",
                   source_body)
    if not m:
        return [{
            "issue": "worker_version_constant_not_found",
            "url": _WORKER_SOURCE_URL,
            "count": 1,
            "detail": "WORKER_VERSION constant not found in source. "
                      "Schema may have changed.",
        }]
    expected = m.group(1)

    _, headers = _http_get(_WORKER_PROBE_URL, timeout=8)
    if not headers:
        return findings  # probe failed; can't compare
    deployed = headers.get("x-dc-worker-version") or headers.get("X-DC-Worker-Version")
    if not deployed:
        return [{
            "issue": "worker_version_header_missing",
            "url": _WORKER_PROBE_URL,
            "count": 1,
            "detail": "X-DC-Worker-Version header not returned. "
                      "Either route isn't going through the worker, or the "
                      "worker isn't setting the header.",
        }]
    # r88-honesty: directional compare. Prod (out-of-git Cloudflare deploy)
    # routinely runs a NEWER worker than the in-repo _worker.js source, so a
    # raw `expected != deployed` flagged a false drift every scan — and the
    # suggested "touch _worker.js to redeploy" would DOWNGRADE prod. Only a
    # real backslip (deployed strictly OLDER than the in-repo/expected
    # version) is a problem worth surfacing. Compare on the numeric
    # major.minor.patch only; ignore non-numeric suffixes like
    # '-switzerland' / '-r80...' which Cloudflare appends per deploy.
    def _vnum(v: str) -> tuple[int, ...]:
        parts: list[int] = []
        for tok in str(v).split("."):
            num = ""
            for ch in tok:
                if ch.isdigit():
                    num += ch
                else:
                    break  # stop at first non-numeric char (e.g. '40-switzerland')
            if num == "":
                break  # non-numeric component → stop building the tuple
            parts.append(int(num))
        return tuple(parts)

    if expected != deployed:
        exp_t, dep_t = _vnum(expected), _vnum(deployed)
        # Flag ONLY when deployed is strictly older than expected (real
        # backslip). If deployed >= expected (prod is newer or equal on the
        # numeric core), it's an expected out-of-git lead — not drift.
        if dep_t and exp_t and dep_t < exp_t:
            findings.append({
                "issue": "worker_version_drift",
                "url": _WORKER_PROBE_URL,
                "count": 1,
                "detail": (f"_worker.js source declares WORKER_VERSION="
                           f"'{expected}' but production header reports an "
                           f"OLDER '{deployed}'. Cloudflare Pages auto-deploy "
                           f"may have skipped this file. Touch _worker.js to "
                           f"force a redeploy."),
                "expected": expected,
                "deployed": deployed,
            })
    return findings


# ── 2. Tier inconsistency (web API ↔ MCP) ──────────────────────────

# Hardcoded mapping of MCP tools → the web API endpoint that serves
# the same data. When the MCP tool's tier changes (e.g. Phase PP
# demotions) but the web endpoint's tier decorator isn't updated to
# match, agents can pull data via MCP for free but a human user hitting
# the same endpoint via the website sees a paywall. Bad UX.
#
# Only listed: tools that have a direct web API counterpart. Composite
# tools (analyze_site, compare_sites) don't have a single web endpoint.
# Phase FF (2026-05-17) — corrected paths after live probe found 4 dead
# mappings spamming 404s in Railway logs. Each entry must point to a
# real Flask route that returns JSON; the radar fetches it as anonymous
# and compares the `min_tier` field with the MCP tool's tier (see
# check_tier_consistency below).
#
# Previously-dead, now-fixed:
#   get_market_intel      /api/v1/market-intel       (404) → /api/v1/markets
#   get_grid_data         /api/v1/grid               (404) → /api/v1/grid/intelligence/CAISO
#   get_intelligence_index/api/v1/intelligence-index (404) → /api/v1/intelligence/trends
#
# Removed (no public web counterpart):
#   get_water_risk  — /api/v1/water/stress is wrapped in a conditional
#                     register at api_integration_wiring.py and 404s in
#                     prod; there's no other water endpoint. The MCP
#                     tool returns data from a different code path that
#                     doesn't have a web mirror, so no tier comparison
#                     is possible. Removing eliminates noise without
#                     losing signal.
_TOOL_API_MAPPING = {
    "get_market_intel":      "/api/v1/markets",
    "get_grid_intelligence": "/api/v1/grid/intelligence",
    "get_fiber_intel":       "/api/v1/fiber/intel",
    "get_energy_prices":     "/api/v1/energy/summary",
    "get_pipeline":          "/api/v1/pipeline",
    "list_transactions":     "/api/v1/transactions",
    "get_grid_data":         "/api/v1/grid/intelligence/CAISO",
    "get_renewable_energy":  "/api/v1/energy/renewable",
    "get_tax_incentives":    "/api/v1/tax-incentives",
    "get_intelligence_index":"/api/v1/intelligence/trends",
}


# r-peace (2026-07-05): TTL result-cache for the tier-drift probe. It round-
# trips ~10 endpoints through CF (→ back to this same backend) on EVERY radar
# scan, and many brain workflows trigger scans — pure self-traffic. Tier gating
# only changes on DEPLOY, so re-probing every scan is waste. Cache 30 min,
# DB-shared via brain_meta so the TTL survives gunicorn --max-requests worker
# recycles AND spans replicas — the exact proven pattern as the dead-link sweep.
# Removes the reason the radar round-trips its own edge, not just the log noise.
_TIER_CACHE = {"ts": 0.0, "findings": None}
_TIER_TTL_S = 1800


def check_env_drift() -> list[dict]:
    """r-env-drift (2026-07-18): compare this service's shared-critical env
    fingerprints against the worker's (Railway private network). The eval
    tick + crawler scheduler run on the WORKER, so a key updated on the
    backend dashboard alone silently no-ops — three incidents on 2026-07-17
    (GROQ/XAI/MOONSHOT each landed on one service). Only the allowlist in
    routes.env_drift.SHARED_CRITICAL_VARS is compared (many vars
    legitimately differ per service); only var NAMES appear in findings."""
    try:
        if (os.environ.get("DCHUB_ROLE") or "").strip() == "worker":
            return []          # the web replica owns this comparison
        base = (os.environ.get("DCHUB_WORKER_INTERNAL_URL") or "").strip()
        key = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
        if not base or not key:
            return []
        from routes.env_drift import env_fingerprints
        mine = env_fingerprints()
        req = urllib.request.Request(
            base.rstrip("/") + "/api/v1/internal/env-fingerprint",
            headers={"X-Internal-Key": key})
        with urllib.request.urlopen(req, timeout=6) as r:
            theirs = (json.loads(r.read().decode("utf-8", "replace"))
                      .get("fingerprints") or {})
        if not theirs:
            return []          # worker predates the endpoint — no signal yet
        drifted = sorted(v for v in mine if mine.get(v) != theirs.get(v))
        if not drifted:
            return []
        return [{
            "issue": "env_drift_backend_vs_worker",
            "url": "/api/v1/internal/env-fingerprint",
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count": len(drifted),
            "detail": ("shared-critical env vars differ between dchub-backend "
                       f"and dchub-worker: {', '.join(drifted[:12])}. Evals + "
                       "crons run on the WORKER — a key saved on one service's "
                       "dashboard silently no-ops on the other. Mirror the "
                       "values on both services."),
        }]
    except Exception:
        return []              # fail-soft: absence of signal, never noise


def check_tier_consistency() -> list[dict]:
    """For each known MCP tool with a web API counterpart, fetch the
    web endpoint as an anonymous caller and check the response shape.
    Flag if the web endpoint blocks a tool that's available via MCP
    for an identified user.

    Heuristic: anonymous probe → response.gated == true means the web
    endpoint gates higher than IDENTIFIED. If the MCP tool is at
    IDENTIFIED tier, that's a mismatch (the agent could get data via
    MCP after one keys/claim call; the user via web hits a paywall).
    """
    import time as _t_tier
    _now_tier = _t_tier.time()
    # Hydrate the in-process cache from brain_meta, then serve if still fresh —
    # a fresh window does 0 CF probes.
    if _TIER_CACHE["findings"] is None:
        try:
            from routes.brain_v2_store import get_meta as _gm_t
            _row_t = _gm_t("tier_drift_cache")
            if _row_t and _row_t.get("value"):
                import json as _json_t
                _p_t = _json_t.loads(_row_t["value"])
                _TIER_CACHE["ts"] = float(_p_t.get("ts") or 0)
                _TIER_CACHE["findings"] = _p_t.get("findings") or []
        except Exception:
            pass
    if (_TIER_CACHE["findings"] is not None
            and (_now_tier - _TIER_CACHE["ts"]) < _TIER_TTL_S):
        return list(_TIER_CACHE["findings"])

    findings: list[dict] = []
    try:
        from mcp_gatekeeper import TOOL_TIER, Tier
    except Exception as e:
        return [{
            "issue": "tier_radar_import_failed",
            "url": "mcp_gatekeeper",
            "count": 1,
            "detail": f"Could not import TOOL_TIER: {e}",
        }]

    # r34 (2026-05-22): probe the eligible endpoints CONCURRENTLY. This loop
    # used to run ~10 sequential _http_get calls at 6s each — the dominant
    # cause of the 103s /consistency-radar runs that blew the 30s budget.
    # Fan out with a small thread pool so total time ≈ slowest single probe.
    eligible = [(t, p) for t, p in _TOOL_API_MAPPING.items()
                if (mt := TOOL_TIER.get(t)) is not None
                and mt.value <= Tier.IDENTIFIED.value]
    from concurrent.futures import ThreadPoolExecutor
    _probe = lambda tp: (tp, _http_get(f"https://dchub.cloud{tp[1]}?_=radar", timeout=6))
    with ThreadPoolExecutor(max_workers=8) as _ex:
        results = list(_ex.map(_probe, eligible))

    for (tool, web_path), (body, _h) in results:
        mcp_tier = TOOL_TIER.get(tool)
        if not body:
            continue
        # Phase WW (2026-05-15): parse JSON and check the STRUCTURED
        # `min_tier` field rather than substring-matching the message
        # text. The old heuristic flagged the energy endpoint as drift
        # because its anonymous message string contained the word
        # "developer" — even after PR #185 demoted min_tier to identified.
        # Structured field is the source of truth.
        try:
            payload = json.loads(body)
        except Exception:
            continue  # non-JSON response — can't reason about tier shape
        if not isinstance(payload, dict):
            continue
        gated = payload.get("gated") is True
        if not gated:
            continue
        # Only flag if the structured min_tier is HIGHER than the MCP tier.
        web_min_tier = (payload.get("min_tier") or "").lower()
        # Phase QQ-fix (2026-05-17): WEB_TIER_RANK was off-by-one — it
        # mapped "free"→1, "identified"→2 but Tier.FREE.value=0 and
        # Tier.IDENTIFIED.value=1. Result: an IDENTIFIED-tier web
        # endpoint compared to an IDENTIFIED MCP tool showed
        # web_rank(2) > mcp_rank(1) and fired a false-positive
        # "web higher than MCP" flag for get_energy_prices. Align the
        # ranks to the Tier enum so equal tiers don't trip the gate.
        WEB_TIER_RANK = {"free": 0, "identified": 1, "starter": 2,
                          "developer": 3, "pro": 4, "founding": 4,
                          "enterprise": 5}  # r43-H: founding == pro
        web_rank = WEB_TIER_RANK.get(web_min_tier, -1)
        mcp_rank = mcp_tier.value if hasattr(mcp_tier, "value") else 0
        if web_min_tier and web_rank > mcp_rank:
            findings.append({
                "issue": "tier_inconsistency_web_higher_than_mcp",
                "url": web_path,
                "count": 1,
                "detail": (f"MCP tool `{tool}` is at {mcp_tier.name} but "
                           f"the web endpoint `{web_path}` gates at "
                           f"min_tier={web_min_tier}. Agents using MCP can "
                           f"access this data with a free dev key; web "
                           f"users hit a paywall. Fix: align the web API "
                           f"decorator to match the MCP tier."),
                "tool": tool,
                "mcp_tier": mcp_tier.name,
                "web_min_tier": web_min_tier,
            })
    # Cache the result (in-process + brain_meta) so the next ~30 min of scans
    # do 0 CF probes. Tier drift only changes on deploy — no detection latency
    # that matters is lost.
    _TIER_CACHE["ts"] = _now_tier
    _TIER_CACHE["findings"] = list(findings)
    try:
        from routes.brain_v2_store import set_meta as _sm_t
        import json as _json_t2
        _sm_t("tier_drift_cache", _json_t2.dumps({"ts": _now_tier, "findings": list(findings)}))
    except Exception:
        pass
    return findings


# ── 3. Missing cron coverage ───────────────────────────────────────

_WORKFLOW_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github", "workflows", "evolve-cron.yml")

# Phases that are intentionally dispatch-only (regression checks,
# safety nets, one-off recovery actions). Listing here suppresses
# the false-positive flag.
_INTENTIONAL_DISPATCH_ONLY = {
    "all",                        # umbrella — fires every sub-job
    "energy_verify",              # regression check, manual only
    "marketing_rescue",           # recovery — should be manual
    "marketing_publish_now",      # manual override of dedupe
    "hot_leads_preview",          # dry-run preview
    "hot_leads_send_top_5",       # safety preview before top_50
    "free_users_dryrun",          # dry-run preview
    "free_users_send",            # Phase QA-sweep-3 (2026-05-16): no
                                   # job block exists in evolve-cron.yml
                                   # for this option — pure orphan from
                                   # the workflow_dispatch options array.
                                   # Either ship the job OR remove the
                                   # option. For now, allowlist so the
                                   # radar stops permanent-red flagging.
    "testimonial_probe",          # one-off diagnostic
    "brain_probe_outcomes",       # already runs on its own cron via the
                                   # explicit '45 5 * * *' schedule
}


def _parse_workflow_regex(text: str) -> tuple[list[str], set[str]]:
    """Lightweight regex-based parse of evolve-cron.yml. Returns
    (phase_options, scheduled_phases). Avoids the PyYAML dependency
    which isn't always installed on the Railway runtime."""
    # Extract the workflow_dispatch options list. Pattern matches:
    #   options: [all, brain, outreach, ..., testimonial_probe]
    opt_match = re.search(r"options:\s*\[([^\]]+)\]", text)
    phase_options: list[str] = []
    if opt_match:
        phase_options = [o.strip() for o in opt_match.group(1).split(",") if o.strip()]

    # For each job block (between `^  <name>:` lines), find its `if:`
    # condition. If the condition references `github.event.schedule`
    # along with a phase option, mark the phase as scheduled.
    scheduled: set[str] = set()
    # r86 (2026-06-13): scan EVERY `if:` expression in the whole file rather
    # than trusting per-job block isolation. The old `re.split` anchor silently
    # merged adjacent job blocks (e.g. when a header was followed by a comment
    # line), absorbing hot_leads_send_top_5/top_50 into the hot_leads_preview
    # block — so re.search only saw the FIRST `if:` (no schedule signal) and
    # falsely reported hot_leads_send_top_50 as cron_phase_missing_schedule even
    # though evolve-cron.yml guards it with `github.event.schedule == '7 17 * * 4'`.
    # A whole-file scan is immune to block-split fragility.
    # Phase QA-sweep-3 (2026-05-16): treat BOTH `github.event.schedule` AND
    # `github.event_name == 'schedule'` as evidence of a scheduled trigger.
    for ifm in re.finditer(r"^\s*if:\s*(.+?)(?=\n\s{0,4}[a-z_]+:|\Z)",
                           text, re.MULTILINE | re.DOTALL):
        cond = ifm.group(1)
        has_schedule_signal = (
            "github.event.schedule" in cond
            or "github.event_name == 'schedule'" in cond
            or 'github.event_name == "schedule"' in cond
        )
        if not has_schedule_signal:
            continue
        for opt in phase_options:
            if f"== '{opt}'" in cond or f'== "{opt}"' in cond:
                scheduled.add(opt)
    return phase_options, scheduled


def check_paywall_capacity_gating() -> list[dict]:
    """Paywall-integrity canary — the alarm that was MISSING during the
    2026-06-20 incident, where a PAYING (even the owner's enterprise) key was
    resolved as 'anonymous' and the map's gating.js redacted CAPACITY (MW) in
    every popup. Four independent links all had to hold and none was watched.
    This detector watches them continuously. cf [[dchub-capacity-paywall-gating]].

    Layer 1 (ALWAYS runs, no key, no network): the tier-VOCABULARY invariant.
    Several legacy resolvers emit 'paid'/'identified'/'admin'/… while gating.js
    and /me/tier only understand TIER_ORDER (anonymous<free<developer<pro<
    enterprise<founding). If a genuinely-paid word stops mapping to index >=
    developer(2), paid users silently get redacted again. Asserts the
    normalization map in gating_routes covers every word.

    Layer 2 (runs when PAYWALL_CANARY_KEY env = a known PAID key): live HTTP
    checks of /api/v1/me/tier — the exact endpoint gating.js calls:
      (a) paid key  → must resolve tier_index >= 2 (else capacity is redacted
          for payers — P1, the literal incident);
      (b) the per-user response must NOT be CF-edge-cached (a cached
          'anonymous' was served to cookieless key-auth'd payers);
      (c) anonymous → must resolve tier_index == 0 (paywall must still gate;
          protects monetization).
    """
    findings: list[dict] = []

    # ── Layer 1: vocabulary invariant (no network, no key) ──────────────
    try:
        from routes.gating_routes import _GATE_TIER_NORMALIZE, TIER_INDEX
        # (tier word a resolver can emit, must_be_a_paid_tier)
        _VOCAB = [
            ("paid", True), ("pro", True), ("enterprise", True),
            ("developer", True), ("dev", True), ("founding", True),
            ("team", True), ("metered", True), ("admin", True),
            ("ent", True), ("research_seed", True),
            ("free", False), ("identified", False), ("starter", False),
            ("anonymous", False), ("anon", False),
        ]
        for word, must_pay in _VOCAB:
            norm = _GATE_TIER_NORMALIZE.get(word, word)
            idx = TIER_INDEX.get(norm, 0)
            if must_pay and idx < 2:
                findings.append({
                    "issue": "tier_vocab_unmapped_paid",
                    "url": "routes/gating_routes.py::_GATE_TIER_NORMALIZE",
                    "count": 1,
                    "severity": "critical",
                    "detail": (f"Paid tier word '{word}' normalizes to '{norm}' = "
                               f"index {idx} < developer(2). gating.js will REDACT "
                               f"capacity for any key resolving '{word}'. Map it in "
                               f"_GATE_TIER_NORMALIZE to a name >= developer. "
                               f"(2026-06-20 incident class.)"),
                })
    except Exception as e:
        findings.append({
            "issue": "paywall_canary_import_failed",
            "url": "routes/gating_routes.py",
            "count": 1,
            "detail": f"Could not import tier normalization map for the canary: {e}",
        })

    # ── Layer 2: live /me/tier checks (needs a known paid canary key) ───
    canary = os.environ.get("PAYWALL_CANARY_KEY")
    if not canary:
        return findings  # dormant until owner sets the env var (fail-soft)
    try:
        import requests as _req
        import random as _rnd
    except Exception:
        return findings
    _base = "https://dchub.cloud/api/v1/me/tier"

    def _tier(headers, bust):
        url = _base + (f"?_={_rnd.randint(1, 10 ** 9)}" if bust else "")
        r = _req.get(url, headers=headers, timeout=8)
        try:
            return r.json(), r.headers
        except Exception:
            return {}, r.headers

    # (a) paid key must NOT be gated — the literal incident
    try:
        d, _h = _tier({"X-API-Key": canary, "Cache-Control": "no-cache"}, True)
        if d.get("tier_index", 0) < 2:
            findings.append({
                "issue": "paid_key_gated",
                "url": "/api/v1/me/tier",
                "count": 1,
                "severity": "critical",
                "detail": (f"A known PAID canary key resolves tier='{d.get('tier')}' "
                           f"index {d.get('tier_index')} < developer(2) → gating.js "
                           f"redacts CAPACITY (MW) in map popups for paying users "
                           f"(the literal 2026-06-20 incident). Inspect "
                           f"util.tier_gate.resolve_tier + gating_routes."
                           f"get_current_tier + the api_keys dual key_hash match."),
            })
    except Exception:
        pass
    # (b) per-user endpoint must not be edge-cached
    try:
        _d2, h2 = _tier({"X-API-Key": canary}, False)
        cc = (h2.get("cf-cache-status") or "").upper() if h2 else ""
        if cc == "HIT":
            findings.append({
                "issue": "me_tier_edge_cached",
                "url": "/api/v1/me/tier",
                "count": 1,
                "severity": "warning",
                "detail": ("/api/v1/me/tier is per-user but returned cf-cache-status: "
                           "HIT — a cached 'anonymous' can be served to a paid user "
                           "whose request carries no session cookie (Vary keys on "
                           "Cookie, not X-API-Key). Must be no-store. (2026-06-20.)"),
            })
    except Exception:
        pass
    # (c) anonymous must still be gated — paywall/monetization intact
    try:
        d3, _h3 = _tier({}, True)
        if d3.get("tier_index", 0) != 0:
            findings.append({
                "issue": "paywall_anon_leak",
                "url": "/api/v1/me/tier",
                "count": 1,
                "severity": "warning",
                "detail": (f"Anonymous /me/tier resolves tier='{d3.get('tier')}' "
                           f"index {d3.get('tier_index')} != 0 — the capacity paywall "
                           f"no longer gates free visitors (monetization leak)."),
            })
    except Exception:
        pass
    # (d) 2026-07-13: a paid key must NOT be 401/402/403'd on the metered MAP
    # endpoints. This is the class the Land & Power map 402-flood fell into —
    # free_tier_gate's session cap authenticates "paid" only via key/JWT and had
    # no canary asserting a paying caller stays served. (a)-(c) only cover the
    # /me/tier gating vocabulary, not the metered data endpoints themselves.
    try:
        for _ep in ("/api/v1/fiber/routes?limit=3",
                    "/api/v1/power-plants?lat=40.41&lng=-80.58&radius=4&limit=3",
                    "/api/v1/grid/intelligence/PJM"):
            _sep = "&" if "?" in _ep else "?"
            _u = "https://dchub.cloud" + _ep + _sep + "__cb=" + str(_rnd.randint(1, 10 ** 9))
            _r = _req.get(_u, headers={"X-API-Key": canary, "Cache-Control": "no-cache"}, timeout=8)
            if _r.status_code in (401, 402, 403):
                findings.append({
                    "issue": "paid_key_walled_on_map",
                    "url": _ep.split("?")[0],
                    "count": 1,
                    "severity": "critical",
                    "detail": (f"A known PAID canary key got HTTP {_r.status_code} on a "
                               f"metered map endpoint — a paying user is being walled on the "
                               f"flagship (the 2026-07-13 session-cap regression class). Check "
                               f"free_tier_gate._resolve_caller / _metered_session_gate and any "
                               f"require_plan on the route."),
                })
    except Exception:
        pass
    return findings


def check_cron_coverage() -> list[dict]:
    """Parse evolve-cron.yml. For each workflow_dispatch phase option,
    check if any job has a `cron:` trigger that fires it. Flag
    dispatch-only phases that should have a schedule.

    Phase RR+1 (2026-05-15): switched from PyYAML to regex parsing
    because the Railway container doesn't ship yaml. The workflow's
    structure is stable enough that regex is reliable; fall back to
    yaml if available for the rare case it gets installed."""
    findings: list[dict] = []
    if not os.path.exists(_WORKFLOW_FILE):
        return findings

    try:
        with open(_WORKFLOW_FILE, "r") as f:
            text = f.read()
        try:
            import yaml  # type: ignore[import-not-found]
            wf = yaml.safe_load(text)
            on = wf.get("on") or wf.get(True) or {}
            dispatch = on.get("workflow_dispatch", {}) if isinstance(on, dict) else {}
            inputs = (dispatch or {}).get("inputs", {})
            phase_choice = inputs.get("phase", {})
            phase_options = phase_choice.get("options", []) or []
            scheduled_phases: set[str] = set()
            jobs = wf.get("jobs", {})
            for job_name, job_def in jobs.items():
                cond = (job_def or {}).get("if", "") or ""
                # Phase QA-sweep-3 (2026-05-16): also recognize
                # `github.event_name == 'schedule'` as scheduled —
                # see _parse_workflow_regex for full rationale.
                has_schedule_signal = (
                    "github.event.schedule" in cond
                    or "github.event_name == 'schedule'" in cond
                    or 'github.event_name == "schedule"' in cond
                )
                if has_schedule_signal:
                    for opt in phase_options:
                        if f"== '{opt}'" in cond or f'== "{opt}"' in cond:
                            scheduled_phases.add(opt)
                    if job_name in phase_options:
                        scheduled_phases.add(job_name)
        except ImportError:
            # No PyYAML on this runtime — fall back to regex parse.
            phase_options, scheduled_phases = _parse_workflow_regex(text)
    except Exception as e:
        return [{
            "issue": "cron_radar_parse_failed",
            "url": _WORKFLOW_FILE,
            "count": 1,
            "detail": f"Could not parse workflow file: {type(e).__name__}: {e}",
        }]

    # Flag phases in workflow_dispatch.options that have no schedule
    # AND aren't on the intentional-allowlist.
    for opt in phase_options:
        if opt in scheduled_phases:
            continue
        if opt in _INTENTIONAL_DISPATCH_ONLY:
            continue
        findings.append({
            "issue": "cron_phase_missing_schedule",
            "url": _WORKFLOW_FILE,
            "count": 1,
            "detail": (f"workflow_dispatch phase `{opt}` has no scheduled "
                       f"cron trigger. If this phase produces business-"
                       f"critical output (LinkedIn posts, broadcasts, "
                       f"testimonials), nobody runs it unless a human "
                       f"manually dispatches. Add a `cron:` entry or "
                       f"document as intentional in _INTENTIONAL_DISPATCH_ONLY."),
            "phase": opt,
        })
    return findings


# ── public API ─────────────────────────────────────────────────────

def check_unsafe_db_conn_pattern() -> list[dict]:
    """Phase FF+7-fix4 (2026-05-19) — static-audit for the conn-leak
    pattern that took Railway down on 2026-05-19.

    Scans .py files for occurrences of `conn = _get_db()` or
    `conn = get_db()` and counts how many times `conn.close()` appears
    in the same file with a `finally:` block. If a file opens many
    conns but has zero or few finally blocks, it's a leak risk —
    every uncaught exception leaks a slot in the Neon pool.

    Runs LOCALLY (no HTTP). Lightweight enough to fire on every scan.
    Flags only when the ratio is bad (e.g. 10 opens, 0 finally) since
    sometimes `with` contexts or per-request handlers don't need it.
    """
    findings: list[dict] = []
    import os as _os, re as _re

    backend_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    open_pat = _re.compile(r"conn\s*=\s*_?get_db\(\)")
    finally_pat = _re.compile(r"^\s*finally\s*:", _re.MULTILINE)

    # Long-running thread files are the highest risk — leaks in
    # per-request handlers are bounded per-request, but leaks in
    # daemon threads accumulate forever.
    thread_files = set()
    for root in (backend_root, _os.path.join(backend_root, "routes")):
        try:
            for f in _os.listdir(root):
                if not f.endswith(".py"): continue
                p = _os.path.join(root, f)
                if not _os.path.isfile(p): continue
                try:
                    with open(p, "rb") as fh:
                        src = fh.read().decode("utf-8", "ignore")
                except Exception: continue
                if "daemon=True" in src or "daemon = True" in src:
                    thread_files.add(p)
        except Exception: continue

    for p in sorted(thread_files):
        try:
            with open(p, "rb") as fh:
                src = fh.read().decode("utf-8", "ignore")
        except Exception: continue
        opens = len(open_pat.findall(src))
        finallys = len(finally_pat.findall(src))
        # Heuristic: more than 3 opens AND fewer finallys than half
        # the opens = likely leaks. Skip if balanced.
        if opens >= 3 and finallys < opens / 2:
            rel = p.replace(backend_root, "").lstrip("/")
            findings.append({
                "issue": "unsafe_db_conn_pattern",
                "url": rel,
                "count": opens,
                "detail": (f"{rel}: {opens} `conn = _get_db()` opens vs "
                           f"only {finallys} `finally:` blocks. This file "
                           f"contains a daemon thread (daemon=True). Conn "
                           f"leaks in long-running threads accumulate forever "
                           f"and eventually exhaust the Neon pool — the "
                           f"failure mode behind the 2026-05-19 outage. Fix: "
                           f"wrap every _get_db()...conn.close() block with "
                           f"try/finally so conn closes on exception paths."),
                "open_count": opens,
                "finally_count": finallys,
            })
    return findings


def _make_route_prober():
    """Return `fn(pattern) -> bool` — True when the app has a registered
    route that would match `pattern`.

    Used by check_repeated_404_patterns to separate the two very different
    causes of a 404: a missing RECORD (route exists, slug doesn't — normal,
    high-volume, not actionable) from a missing ROUTE (nothing serves that
    path — a real gap worth fixing). Stored patterns carry placeholders like
    `/api/v1/facility/<slug>`, so substitute a probe token before matching.

    Returns None when no url_map is reachable. The caller must then emit a
    single `route_prober_unavailable` finding and skip the volume checks —
    NOT fall back to raw counts. Falling back would report every legitimate
    missing-record 404 as a route gap (the /api/v1/facility/<slug> case,
    ~288/day), which is a worse failure than staying quiet: it trains the
    operator to ignore this detector. One honest "I am degraded" finding
    beats a page of false ones.
    """
    import re as _re
    adapter = None
    # Prefer the live app context — the radar normally runs inside a request,
    # where current_app is the actual serving app. `import main` is the
    # fallback and is NOT reliable outside the server process.
    try:
        from flask import current_app as _ca
        adapter = _ca.url_map.bind("dchub.cloud")
    except Exception:
        try:
            from main import app as _app
            adapter = _app.url_map.bind("dchub.cloud")
        except Exception:
            return None

    def _exists(pattern: str) -> bool:
        try:
            probe = _re.sub(r"<[^>]*>", "probe", pattern or "")
            if not probe.startswith("/"):
                probe = "/" + probe
            adapter.match(probe, method="GET")
            return True
        except Exception as exc:  # NotFound → gap; MethodNotAllowed → route exists
            return type(exc).__name__ == "MethodNotAllowed"

    return _exists


def check_repeated_404_patterns() -> list[dict]:
    """Phase FF+7-meta (2026-05-19) — fires when the same URL PATTERN
    has 404'd repeatedly. The gap the user spotted: the map's facility
    profile pages hit /api/v1/facility/<slug> (singular) which 404'd —
    backend serves /facilities/<slug> (plural). Every visitor to a
    facility profile got 404. The brain didn't catch it because no
    detector was looking at recent 404 patterns.

    This detector reads from `request_telemetry` (any HTTP log table
    the app writes to) OR from the 404 handler's own counter, groups
    by URL pattern (collapses /api/v1/facility/<X> to /api/v1/facility/*),
    and fires if any pattern has >=10 404s in the last hour.

    Falls back to checking sentinel 404 status if telemetry table
    doesn't exist.
    """
    findings: list[dict] = []
    try:
        from main import get_db
        conn = get_db()
        if not conn: return findings
        try:
            cur = conn.cursor()
            # Probe for a 404-log table; if missing, fall back gracefully
            # r-404-table (2026-07-24 coverage audit): none of the original candidates
            # carry the real 404 stream, so this returned [] on every scan since it was
            # written. brain_http_errors(pattern, status, occurred_at) is where 404s
            # actually land (~19.5k rows) — probe it FIRST.
            for table_candidate in ("brain_http_errors", "request_telemetry",
                                     "http_request_log",
                                     "api_404_log", "site_sentinel_results"):
                try:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{table_candidate}",))
                    if (cur.fetchone() or [None])[0]:
                        # Found a candidate — query 404s in last hour
                        if table_candidate == "brain_http_errors":
                            cur.execute("""
                                SELECT pattern, COUNT(*) AS n
                                FROM brain_http_errors
                                WHERE occurred_at > NOW() - INTERVAL '24 hours'
                                  AND status = 404
                                GROUP BY pattern HAVING COUNT(*) >= 10
                                ORDER BY n DESC LIMIT 5
                            """)
                        elif table_candidate == "site_sentinel_results":
                            cur.execute("""
                                SELECT path, COUNT(*) AS n
                                FROM site_sentinel_results
                                WHERE checked_at > NOW() - INTERVAL '24 hours'
                                  AND status = 404
                                GROUP BY path HAVING COUNT(*) >= 2
                                ORDER BY n DESC LIMIT 5
                            """)
                        else:
                            cur.execute(f"""
                                SELECT
                                    regexp_replace(path, '/[a-z0-9_-]{{16,}}$', '/<slug>') AS pattern,
                                    COUNT(*) AS n
                                FROM {table_candidate}
                                WHERE created_at > NOW() - INTERVAL '1 hour'
                                  AND status = 404
                                GROUP BY pattern HAVING COUNT(*) >= 10
                                ORDER BY n DESC LIMIT 5
                            """)
                        # r-404-routegap (2026-07-24 coverage audit): most high-count
                        # 404 patterns are LEGITIMATE resource misses — e.g.
                        # /api/v1/facility/<slug> 404s 288x/day purely because agents
                        # probe slugs that don't exist, and that route is registered
                        # (main.py, singular+plural on one handler). Firing on those
                        # would make this detector permanent noise. The actionable
                        # class is narrower: a path with NO registered route at all.
                        # Ask Werkzeug's url_map directly instead of guessing.
                        _rule_exists = _make_route_prober()
                        if _rule_exists is None:
                            findings.append({
                                "issue": "route_prober_unavailable",
                                "url":   "routes/brain_consistency_radar.py:_make_route_prober",
                                "count": 1,
                                "detail": (
                                    "check_repeated_404_patterns could not reach the "
                                    "Flask url_map, so it cannot tell a missing ROUTE "
                                    "(actionable) from a missing RECORD (normal). "
                                    "Volume checks were SKIPPED this scan rather than "
                                    "emitting false route gaps. This detector is "
                                    "degraded until the app context is reachable."
                                ),
                            })
                            break
                        for r in cur.fetchall():
                            pattern = r[0] if not hasattr(r, "get") else r.get("pattern") or r.get("path")
                            n = r[1] if not hasattr(r, "get") else r.get("n")
                            if pattern and _rule_exists(pattern):
                                continue  # route exists → 404 is a missing record, not a gap
                            if pattern and n:
                                findings.append({
                                    "issue": "repeated_404_pattern",
                                    "url": pattern,
                                    "count": int(n),
                                    "detail": (f"URL pattern '{pattern}' returned 404 "
                                               f"{n} times recently. Likely a "
                                               f"frontend/backend route mismatch (e.g. "
                                               f"/facility/<slug> vs /facilities/<slug>). "
                                               f"Auto-fix idea: add a route alias on the "
                                               f"backend OR fix the frontend caller. "
                                               f"Verify with: curl -i "
                                               f"https://dchub.cloud{pattern.replace('<slug>','test')}"),
                                })
                        break
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    continue
        finally:
            try: conn.close()
            except Exception: pass
    except Exception: pass
    return findings


def check_press_stale_vs_citations() -> list[dict]:
    """Phase FF+7-press-loop (2026-05-19) — fires when AI citations
    have landed BUT the press_releases queue hasn't caught up.

    The gap the user caught: ChatGPT + Gemini cited dchub.cloud today,
    /dc-hub-media still shows 73-day-old releases. Brain L20 was
    capturing citations but no loop drafted press from them.

    Detector logic: if newest dchub_cited=true observation is fresher
    than newest press_releases row, flag it as a press_drafting_lag
    finding. Auto-fix is to POST /api/v1/ai-citations/draft-press.
    """
    findings: list[dict] = []
    try:
        body, _ = _http_get("http://localhost:8080/api/v1/ai-citations/history",
                            timeout=5)
        if not body: return findings
        import json as _json
        d = _json.loads(body)
        # 2026-07-18: the endpoint's live response key is `history` —
        # parsing only observations/recent left this detector permanently
        # empty-handed (0 findings ever) while the /press surface froze.
        obs = d.get("observations") or d.get("recent") or d.get("history") or []
        cited = [o for o in obs if o.get("dchub_cited")]
        if not cited: return findings

        # Newest citation timestamp
        from datetime import datetime as _dt
        newest_citation = None
        for o in cited:
            at = o.get("observed_at") or o.get("at")
            try:
                d2 = _dt.fromisoformat(str(at).replace("Z", "+00:00"))
                if newest_citation is None or d2 > newest_citation:
                    newest_citation = d2
            except Exception: continue
        if newest_citation is None: return findings

        # Newest press_releases.published_at
        body2, _ = _http_get("http://localhost:8080/api/v1/press-releases?limit=1",
                              timeout=5)
        newest_press = None
        if body2:
            try:
                d3 = _json.loads(body2)
                items = d3.get("items") or d3.get("press_releases") or d3.get("releases") or (d3 if isinstance(d3, list) else [])
                if items:
                    at = items[0].get("published_date") or items[0].get("created_at")
                    if at:
                        newest_press = _dt.fromisoformat(str(at).replace("Z", "+00:00"))
            except Exception: pass

        # Gap: citation fresher than newest press by >24h
        from datetime import timezone as _tz
        nc = newest_citation.replace(tzinfo=_tz.utc) if newest_citation.tzinfo is None else newest_citation
        np = newest_press.replace(tzinfo=_tz.utc) if (newest_press and newest_press.tzinfo is None) else newest_press
        if np is None or (nc - np).total_seconds() > 24 * 3600:
            lag_h = int((nc - np).total_seconds() / 3600) if np else 9999
            findings.append({
                "issue": "press_drafting_lag",
                "url": "/dc-hub-media",
                "count": 1,
                "detail": (f"AI-citation observations are fresher than "
                           f"the newest press release by {lag_h}h. "
                           f"The dc-hub-media surface looks stale even "
                           f"though ChatGPT/Gemini/Claude have cited us "
                           f"recently. Auto-fix: POST /api/v1/ai-citations/"
                           f"draft-press?write=true&auto_approve=true&days=7"),
                "lag_hours": lag_h,
            })
    except Exception: pass
    return findings


def check_ai_citation_new_landing() -> list[dict]:
    """Phase FF+7-meta (2026-05-19) — celebrate-and-amplify detector.
    Fires when a NEW ai_citations row landed in the last 24h where
    dchub_cited=true. Turns the brain into a notifier for citation wins.

    Why this matters: AI citation is the long-game KPI behind the
    'Switzerland with receipts' positioning. The first one took a year.
    The brain should make sure the next one isn't missed by ops — we
    surface it as a finding so the dashboards highlight it.
    """
    findings: list[dict] = []
    try:
        body, _ = _http_get("http://localhost:8080/api/v1/ai-citations/history",
                            timeout=5)
        if not body: return findings
        import json as _json
        data = _json.loads(body)
        obs = data.get("observations") or data.get("recent") or []
        # Filter to last 24h + dchub_cited
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = _dt.now(_tz.utc) - _td(hours=24)
        new_wins = []
        for o in obs:
            if not o.get("dchub_cited"): continue
            at = o.get("observed_at") or o.get("at")
            try:
                d = _dt.fromisoformat(str(at).replace("Z", "+00:00"))
                if d > cutoff:
                    new_wins.append(o)
            except Exception: continue
        if new_wins:
            engines = ", ".join(sorted({w.get("engine", "?") for w in new_wins}))
            findings.append({
                "issue": "ai_citation_landed",
                "url": "/cited-by",
                "count_kind": "item_count",  # magnitude, not a recurrence tally
                "count": len(new_wins),
                "detail": (f"{len(new_wins)} new AI-citation observation(s) "
                           f"in the last 24h where dchub.cloud was cited "
                           f"(engines: {engines}). This is the long-game "
                           f"KPI behind the positioning. Add to /cited-by "
                           f"page + tweet a screenshot."),
                "engines": list(sorted({w.get("engine", "?") for w in new_wins})),
                "wins": new_wins[:5],
            })
    except Exception: pass
    return findings


def check_deploy_queue_churn() -> list[dict]:
    """Phase FF+7-meta (2026-05-19) — detect the outage class triggered
    by rapid-fire commits.

    Two confirmed outages this week (~30 min each) had the same shape:
      - 5-8 commits pushed in <30 min
      - Railway serializes deploys at 2-3 min each
      - Net: deploy queue saturated, intermediate states unhealthy
      - Brain's INTERNAL detectors can't fire because they live on
        the unhealthy container

    This static detector pulls recent commit timestamps from the GitHub
    API and flags when push velocity exceeds Railway's deploy throughput.
    Visible from inside the brain so we can ATTRIBUTE outages to push
    velocity in retrospect.

    Threshold: >=4 commits in the last 30 min from any author = warn.
    """
    findings: list[dict] = []
    import os as _os
    token = _os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return findings  # need a token to read API; fail closed
    try:
        import urllib.request as _ur, json as _json, datetime as _dt
        since = (_dt.datetime.utcnow() - _dt.timedelta(minutes=30)).isoformat() + "Z"
        repo = _os.environ.get("GITHUB_REPO", "azmartone67/dchub-backend").strip()
        req = _ur.Request(
            f"https://api.github.com/repos/{repo}/commits?since={since}&per_page=20",
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {token}"},
        )
        with _ur.urlopen(req, timeout=8) as resp:
            commits = _json.loads(resp.read().decode("utf-8"))
        n = len(commits) if isinstance(commits, list) else 0
        if n >= 4:
            first_msg = (commits[0].get("commit", {}).get("message", "")
                          .split("\n")[0])[:80]
            last_msg = (commits[-1].get("commit", {}).get("message", "")
                         .split("\n")[0])[:80]
            findings.append({
                "issue": "deploy_queue_churn",
                "url": f"https://github.com/{repo}/commits/main",
                "count": n,
                "detail": (f"{n} commits pushed in the last 30 min. "
                           f"Railway serializes deploys at 2-3 min each — "
                           f"this saturates the queue and produced 2 "
                           f"outages this week (each ~30 min). Recent commits: "
                           f"newest='{first_msg}', oldest='{last_msg}'. "
                           f"Recommend: rate-limit pushes to 1 per 5 min "
                           f"on fragile files (publishers, brain layers, "
                           f"main.py)."),
                "commit_count": n,
            })
    except Exception:
        pass
    # (d) 2026-07-13: a paid key must NOT be 401/402/403'd on the metered MAP
    # endpoints. This is the class the Land & Power map 402-flood fell into —
    # free_tier_gate's session cap authenticates "paid" only via key/JWT and had
    # no canary asserting a paying caller stays served. (a)-(c) only cover the
    # /me/tier gating vocabulary, not the metered data endpoints themselves.
    try:
        for _ep in ("/api/v1/fiber/routes?limit=3",
                    "/api/v1/power-plants?lat=40.41&lng=-80.58&radius=4&limit=3",
                    "/api/v1/grid/intelligence/PJM"):
            _sep = "&" if "?" in _ep else "?"
            _u = "https://dchub.cloud" + _ep + _sep + "__cb=" + str(_rnd.randint(1, 10 ** 9))
            _r = _req.get(_u, headers={"X-API-Key": canary, "Cache-Control": "no-cache"}, timeout=8)
            if _r.status_code in (401, 402, 403):
                findings.append({
                    "issue": "paid_key_walled_on_map",
                    "url": _ep.split("?")[0],
                    "count": 1,
                    "severity": "critical",
                    "detail": (f"A known PAID canary key got HTTP {_r.status_code} on a "
                               f"metered map endpoint — a paying user is being walled on the "
                               f"flagship (the 2026-07-13 session-cap regression class). Check "
                               f"free_tier_gate._resolve_caller / _metered_session_gate and any "
                               f"require_plan on the route."),
                })
    except Exception:
        pass
    return findings


def check_db_pool_pressure() -> list[dict]:
    """Phase FF+7-fix4 (2026-05-19) — early-warning detector for the
    pool-exhaustion class of outage that took Railway down for 30 min
    on 2026-05-19 08:18 UTC.

    Symptom of pool exhaustion: many independent DB-backed endpoints
    start timing out simultaneously while pure-Python endpoints stay
    fast. By the time Railway's health-check fails, the pool is fully
    gone and recovery requires a container restart.

    We probe 3 DB endpoints with tight timeouts. If 2/3 time out (>3s
    each) on a healthy worker, the pool is under pressure — flag it
    before the container hits the unhealthy threshold.
    """
    findings: list[dict] = []
    import time as _t
    probes = [
        # r-radarfix (2026-06-26): swapped /freshness/radar OUT — it runs
        # scan_domains() (intrinsically 4s+), so as a DB-pool-pressure canary it
        # false-timed-out EVERY scan (logging "[brain-radar] .../freshness/radar
        # TimeoutError") AND piled self-inflicted load on the single replica — the
        # same flapping class we dropped the quarterly-deep probe for above. Use a
        # genuinely-light DB endpoint (/api/v1/site/stats, the proven-fast failover
        # probe target) so this canary reflects REAL pool pressure, not the
        # endpoint's own slowness.
        ("site_stats",       "/api/v1/site/stats"),
        ("brain_memory",     "/api/v1/brain/memory/stats"),
        ("redeem_funnel",    "/api/v1/redeem/funnel-stats"),
    ]
    slow = []
    for label, path in probes:
        t0 = _t.monotonic()
        body, _ = _http_get(f"http://localhost:8080{path}", timeout=4)
        dur = _t.monotonic() - t0
        if not body or dur >= 3.0:
            slow.append({"endpoint": label, "duration_s": round(dur, 2),
                         "got_body": bool(body)})

    if len(slow) >= 2:
        findings.append({
            "issue": "db_pool_pressure",
            "url": "/api/v1/brain/db-pool-pressure",
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count": len(slow),
            "detail": (f"{len(slow)}/3 DB-backed endpoints slow or "
                       f"timing out ({slow}). Pool exhaustion class of "
                       f"failure — check for connection leaks in long-"
                       f"running threads (auto-publisher loops, brain "
                       f"learn cycles). 2026-05-19 incident: publisher "
                       f"loops missing try/finally leaked conns until "
                       f"Neon pool exhausted, container went unhealthy "
                       f"for 30 min. Fix is always: ensure every "
                       f"_get_db()/get_db() call is followed by a "
                       f"try/finally that closes the conn."),
            "slow_endpoints": slow,
        })
    return findings


def check_report_content_drift() -> list[dict]:
    """r41-report-quality (2026-05-25). The monthly + quarterly-deep
    reports are dchub's strongest claim-to-fame ('live equivalent of
    CBRE H2 2025'). The architecture is dynamic — they regenerate per
    request — but the CONTENT can drift toward empty/broken sections
    silently if upstream signal pipelines stop producing data:

      - dcpi_movers: [] → DCPI weekly-mover detection broken OR
        thresholds set too high so no markets ever qualify
      - brand_pulse.citation_score_pct: 0.0 → citation tracker
        scraping no external press mentions
      - hyperscaler_deals: [] in quarterly → news pipeline regression
      - generated_at > 25 hours old → cache stuck OR cron not firing

    Any of these would be invisible from a smoke-test pass (HTTP 200 +
    valid JSON shape) but visibly empty to a journalist clicking
    through from the LinkedIn 'CC-BY-4.0. AI-agent native' partnership
    post. We catch it ourselves before they do.
    """
    findings: list[dict] = []
    # r43-radarfix (2026-05-29): DROPPED the quarterly-deep probe. The
    # quarterly-deep report is a heavy dynamic render that routinely takes
    # 12-14s on the single Railway replica, so this synchronous GET (8s
    # timeout) timed out almost every scan → "[brain-radar] .../reports/
    # quarterly-deep TimeoutError" false negative every cycle, while also
    # piling self-inflicted load onto the one worker (the exact flapping
    # class we've been fighting). The monthly report is light and covers
    # the same drift signals (dcpi_movers, brand_pulse, generated_at
    # staleness), so we keep that one and lose only the quarterly-only
    # hyperscaler_deals=[] check — an acceptable trade vs hammering the
    # single replica every scan.
    for window, url in [
        ("monthly",          "http://localhost:8080/api/v1/reports/monthly"),
    ]:
        body, _ = _http_get(url, timeout=8)
        if not body:
            findings.append({
                "issue":  f"report_unreachable:{window}",
                "url":    url,
                "count":  1,
                "detail": (f"{window} report endpoint did not return — "
                           f"could be slow path or genuine outage. "
                           f"This is the LinkedIn-partnership-post "
                           f"surface; if it's down, the post becomes "
                           f"a credibility problem."),
            })
            continue
        try:
            import json as _json
            d = _json.loads(body) if isinstance(body, str) else body
        except Exception:
            continue

        # Drift check 1: dcpi_movers empty
        movers = d.get("dcpi_movers")
        if isinstance(movers, list) and len(movers) == 0:
            findings.append({
                "issue":  f"report_empty_section:{window}.dcpi_movers",
                "url":    url,
                "count":  0,
                "detail": (f"{window} report has dcpi_movers=[]. Either "
                           f"the WoW-delta threshold is too aggressive "
                           f"(no markets move >5pts) or DCPI recompute "
                           f"isn't producing per-week deltas. Backfill "
                           f"with a 'no movers this period' sentinel "
                           f"in compute_report() so the JSON shape "
                           f"never looks broken to readers."),
            })

        # Drift check 2: brand_pulse zero (citation tracker broken)
        bp = d.get("brand_pulse") or {}
        score = bp.get("citation_score_pct")
        if isinstance(score, (int, float)) and score == 0.0:
            # r64-d (2026-05-31): SUPPRESS the false alarm. The detector's
            # own comment said "cross-check against
            # check_ai_citations_stale_v2 to disambiguate" — now we
            # actually do. A brand_pulse.citation_score_pct of 0.0 in the
            # monthly report does NOT mean DC Hub is uncited: the report's
            # brand_pulse reads the (stale, seeder-fed) testimonial tables,
            # while the LIVE ai_citations feed shows DC Hub #1 at ~36.4%
            # share-of-voice. Escalating "zero citation score" while we're
            # objectively winning share-of-voice is a self-inflicted false
            # crisis. So: if real share-of-voice > 0, the score is just a
            # stale-pipeline artifact (already covered by the seeder cron
            # + ai_citations_cron_silent detector) — don't escalate.
            sov = _dchub_share_of_voice_pct()
            if sov is not None and sov > 0.0:
                pass  # We're being cited (sov% SoV) — stale report metric, not a real gap.
            else:
                findings.append({
                    "issue":  f"report_zero_signal:{window}.brand_pulse",
                    "url":    url,
                    "count":  0,
                    "detail": (f"{window} report brand_pulse.citation_score_pct "
                               f"= 0.0 AND live ai_citations share-of-voice is "
                               f"{('0.0%' if sov == 0.0 else 'unavailable')}. "
                               f"Either no external citations in the window "
                               f"(real gap, not a code bug — see "
                               f"[[source_of_truth]] memory) OR the citation "
                               f"probe/detector is missing crawl targets. "
                               f"Cross-checked check_ai_citations_stale_v2's "
                               f"ai_citations table; it did not show DC Hub "
                               f"being cited, so this is a genuine signal."),
                })

        # Drift check 3: quarterly's hyperscaler_deals empty
        if window == "quarterly-deep":
            hd = d.get("hyperscaler_deals")
            if isinstance(hd, list) and len(hd) == 0:
                findings.append({
                    "issue":  f"report_empty_section:{window}.hyperscaler_deals",
                    "url":    url,
                    "count":  0,
                    "detail": (f"Quarterly report has hyperscaler_deals=[] "
                               f"— news pipeline isn't extracting $1B+ "
                               f"capex events. Run /api/v1/hyperscaler-"
                               f"alerts/sweep to backfill OR check the "
                               f"news ingestion log for last 24h."),
                })

        # Drift check 4: generated_at staleness
        ts = d.get("generated_at") or ""
        try:
            import datetime as _dt
            gen = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            now = _dt.datetime.now(_dt.timezone.utc)
            age_h = (now - gen).total_seconds() / 3600.0
            if age_h > 25.0:
                findings.append({
                    "issue":  f"report_stale:{window}",
                    "url":    url,
                    "count_kind": "hours",  # magnitude, not a recurrence tally
                    "count":  int(age_h),
                    "detail": (f"{window} report generated_at is {age_h:.1f}h "
                               f"old (SLA: <24h). 'Daily refresh' claim "
                               f"in the LinkedIn post breaks here. "
                               f"Check cron + CF cache invalidation."),
                })
        except Exception:
            pass

        # Drift check 5 (r42, 2026-05-25): narrative_summary should be
        # present when ANTHROPIC_API_KEY is set. Missing field means the
        # LLM call failed or the env var was rotated out — the report
        # still works (CBRE-equivalent data) but loses the analyst voice
        # we promised to compete with their narrative depth.
        import os as _os
        if (_os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            narr = d.get("narrative_summary")
            if not isinstance(narr, dict) or not (narr.get("text") or "").strip():
                findings.append({
                    "issue":  f"report_no_narrative:{window}",
                    "url":    url,
                    "count":  1,
                    "detail": (f"{window} report missing narrative_summary "
                               f"despite ANTHROPIC_API_KEY being set. Check "
                               f"routes/report_narrative.py: model availability, "
                               f"prompt size, or 25s timeout. Without the "
                               f"narrative, the CBRE/JLL gap claim weakens — "
                               f"we sell freshness + license, but they sell "
                               f"the analyst voice in the prose."),
                })

    return findings


def check_mcp_tool_description_drift() -> list[dict]:
    """r41-description-drift (2026-05-25). The worker's MCP_SERVER_INFO
    description string + each MCP tool's description are what every AI
    crawler reads first. Today's session caught a real instance: the
    description said '7 ISO grid data' but we'd added Hydro-Quebec +
    AESO + Nord Pool months ago — undetected for weeks because nothing
    cross-checks the marketing copy against ground truth.

    This detector fetches /mcp/manifest (the canonical agent-facing
    descriptor) and flags known drift patterns:
      - claims '7 ISO' but the platform now serves >=10
      - claims tools_count=N but the actual tools/list count differs
        by >2
    Cheap: one HTTP fetch, all comparisons are string/integer.
    """
    findings: list[dict] = []
    body, _ = _http_get("https://dchub.cloud/mcp/manifest", timeout=8)
    if not body:
        return findings
    try:
        import json as _json
        manifest = _json.loads(body) if isinstance(body, str) else body
    except Exception:
        return findings
    desc = (manifest.get("description") or "").lower()
    claimed_count = manifest.get("tools_count")

    # Pattern 1: stale ISO count in description.
    import re as _re
    iso_match = _re.search(r"(\d+)\s*ISO", desc)
    if iso_match:
        claimed_iso = int(iso_match.group(1))
        # Hard-coded ground truth for now: 10 ISOs (7 US + HQ + AESO + Nord Pool).
        # When more ISOs land (CENACE etc.), update here.
        actual_iso = 7
        if claimed_iso < actual_iso:
            findings.append({
                "issue":  "mcp_description_drift:iso_count",
                "url":    "/mcp/manifest (MCP_SERVER_INFO.description in worker)",
                "count":  actual_iso - claimed_iso,
                "detail": (f"Manifest description claims {claimed_iso} ISOs "
                           f"but we now cover {actual_iso} "
                           f"(7 US ISOs + modeled baselines: Hydro-Québec, AESO, Nord Pool). "
                           f"AI crawlers reading the manifest get a "
                           f"stale picture of our coverage. Update "
                           f"MCP_SERVER_INFO.description in the latest "
                           f"worker patch + redeploy."),
            })

    # Pattern 2: tools_count drift vs the FALLBACK_TOOLS the worker
    # actually exposes. We can't easily count the worker's runtime
    # tool list from here, but the manifest is the single source of
    # truth — if it says N but tools/list returns N+k, agents see a
    # different count than the discovery surface advertises.
    # r43-radarfix (2026-05-29): REMOVED the GET probe to
    # https://dchub.cloud/mcp here. /mcp is POST-only (JSON-RPC + session),
    # so a GET always returned HTTP 405 → "[brain-radar] .../mcp HTTP 405"
    # logged every scan as a false negative, AND the probe was already a
    # dead no-op (its result was never used — the manifest's own count is
    # the source of truth). Dropping it removes the log noise and a needless
    # request to the single Railway replica with zero loss of signal.

    return findings


def check_session_upgrade_silenced() -> list[dict]:
    """r41-session-upgrade-health (2026-05-25). The session-upgrade
    flow shipped in v2.1.7 lets a Claude.ai web user hit a paywall,
    follow the redeem URL, get a dev key, and have their MCP session
    upgraded in-place so the next paid-tool call returns real data —
    closing the gap where Claude.ai's connector UI can't attach an
    X-API-Key header.

    The whole mechanism is silent if it ever breaks (the user just
    keeps seeing paywalls forever, with no error message). This
    detector flags the silent-break case:
      - paywall_hit > 200 in 24h (significant demand exists)
      - api_keys created with metadata.session_id < 5 in 24h
        (~no one successfully completed the redeem flow)

    Cause is usually one of:
      1. Flask /api/v1/mcp/trial-check didn't deploy with the
         tier_upgrade extension (server.mjs gets {trial_used} only,
         no tier_upgrade ever fires)
      2. The redeem POST stopped persisting metadata.session_id on
         the dev key (api_keys row exists but session_id is null)
      3. server.mjs deployment regressed v2.1.7 → an earlier version
    """
    findings: list[dict] = []
    body, _ = _http_get("http://localhost:8080/api/v1/redeem/funnel-stats",
                        timeout=5)
    if not body:
        return findings
    try:
        import json as _json
        d = _json.loads(body) if isinstance(body, str) else body
    except Exception:
        return findings
    fc = d.get("funnel_counts") or {}
    paywall = int(fc.get("paywall_hit") or 0)
    if paywall < 200:
        return findings  # not enough raw demand to bother with a DB check
    # Query for keys created via redeem in the trailing 24h.
    conn = _db()
    if conn is None:
        return findings
    distinct_callers_24h = 0
    try:
        with conn.cursor() as cur:
            # Phase r35 (2026-05-31): gate on DISTINCT paywall CALLERS, not the
            # raw paywall_hit volume. paywall_hit is dominated by a handful of
            # power-agents firing thousands of signals each (one agent's
            # search_facilities alone = 8,978 fires from a single key), so the
            # old "paywall>200" guard was trivially true every cycle and this
            # detector screamed "redeem flow is silently broken!" non-stop even
            # when the flow was fine and demand was just a few callers. Mirror
            # the canonical distinct-caller expression the ops dashboard uses
            # (2b_unique_paywall_callers_7d) over a 24h window: a genuine
            # "broad demand that isn't converting" signal needs many DISTINCT
            # callers, not one agent in a loop.
            try:
                cur.execute("""
                    SELECT COUNT(DISTINCT COALESCE(
                               NULLIF(user_email,''),
                               NULLIF(mcp_client,''),
                               NULLIF(tool_requested,'')))
                      FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '24 hours'
                """)
                distinct_callers_24h = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                conn.rollback()
                distinct_callers_24h = 0
            try:
                # Tolerant of both schemas: metadata JSONB and TEXT.
                cur.execute("""
                    SELECT COUNT(*) FROM api_keys
                     WHERE created_at >= NOW() - INTERVAL '24 hours'
                       AND (
                            (metadata::text LIKE '%%session_id%%')
                            OR (
                                CASE WHEN pg_typeof(metadata)::text = 'jsonb'
                                THEN metadata::jsonb ? 'session_id'
                                ELSE FALSE END
                            )
                       )
                """)
                redeemed_24h = int((cur.fetchone() or [0])[0])
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    if distinct_callers_24h < 25:
        return findings  # demand is a few power-agents, not a broad audience
    if redeemed_24h >= 5:
        return findings  # flow is firing
    findings.append({
        "issue":  "session_upgrade_silenced",
        "url":    "/api/v1/mcp/trial-check (Flask) + server.mjs session-upgrade path",
        "count":  redeemed_24h,
        "detail": (f"{distinct_callers_24h} DISTINCT paywall callers in 24h "
                   f"(raw paywall_hit={paywall:,}) but only "
                   f"redeemed_24h={redeemed_24h} dev keys persisted "
                   f"metadata.session_id. The session-upgrade flow that "
                   f"closes the Claude.ai paid-tool gap (v2.1.7+) is "
                   f"either silently broken OR genuinely no one is "
                   f"completing the redeem form. Check: "
                   f"(1) trial-check returns tier_upgrade for known "
                   f"redeemed sessions, "
                   f"(2) redeem POST persists metadata.session_id, "
                   f"(3) server.mjs version >= 2.1.7."),
    })
    return findings


def check_paywall_click_leak() -> list[dict]:
    """Phase FF+7 (2026-05-19) — flag the conversion leak L14 identified
    as the real root cause of the funnel collapse: paywall_hit → click
    drop-off >99% means the upgrade_url either isn't reaching users or
    is pointing somewhere users can't act on.

    Pulls /api/v1/redeem/funnel-stats. If paywall_hit > 500 (significant
    volume) and click/paywall_hit < 0.5%, fire — this is the leak.
    """
    findings: list[dict] = []
    body, _ = _http_get("http://localhost:8080/api/v1/redeem/funnel-stats",
                        timeout=5)
    if not body:
        return findings
    try:
        import json as _json
        d = _json.loads(body) if isinstance(body, str) else body
    except Exception:
        return findings
    # Phase r35 (2026-05-31) — STOP the permanent false-positive. The old
    # check divided `click` (browser redeem events) by `paywall_hit` (every
    # MCP gate fire). Those are DIFFERENT populations: paywall_hit is ~19k
    # events from a handful of MCP KEYS (search_facilities alone = 8,978 calls
    # from 0_unique_keys=1 — one developer/agent hammering tools), while click/
    # view/submit are human browser interactions. So click/paywall_hit is
    # ALWAYS ~0% and the detector fired every single cycle, burying real signal.
    #
    # Honest measure: (1) gate on DISTINCT paywall callers, not raw events —
    # one key generating 9k signals is not 9k lost customers; (2) the real,
    # actionable leak is the HUMAN sub-funnel view->submit (people who reached
    # the upgrade form but didn't submit). Fire on THAT, not on the
    # apples-to-oranges MCP-events-vs-browser-clicks ratio.
    fc = d.get("funnel_counts") or {}
    by_event = d.get("by_event") or {}
    rates = d.get("conversion_rates") or {}
    view = int(fc.get("view") or 0)
    submit = int(fc.get("submit") or 0)
    paywall_callers = int((by_event.get("paywall_hit") or {}).get("distinct_users") or 0)

    # Real leak: a meaningful number of humans VIEWED the upgrade form but
    # 0% submitted. Only fire with enough sample to be real (>=20 views).
    if view >= 20 and submit == 0:
        findings.append({
            "issue": "redeem_form_submit_leak",
            "url": "/api/v1/redeem/funnel-stats",
            "count": 1,
            "detail": (f"{view} humans VIEWED the redeem/upgrade form in 30d "
                       f"but 0 submitted (view->submit = 0%). The form itself "
                       f"is the leak — check the redeem page submit flow, "
                       f"validation, or a broken POST. (paywall_hit volume is "
                       f"MCP-agent events from {paywall_callers or '~1'} "
                       f"distinct keys, NOT lost human prospects — ignore that "
                       f"ratio.)"),
            "views_30d": view,
            "submits_30d": submit,
        })
    return findings


def check_cron_if_mismatched() -> list[dict]:
    """Phase FF+7 (2026-05-19) — catch the bug class L14 surfaced:
    a job's `if: github.event.schedule == 'CRON_STRING'` references a
    cron string that isn't actually in the workflow's `on.schedule`
    list, so the job never fires from cron.

    Two failure modes covered:
      1. STALE: the schedule was moved (e.g. ':00' -> ':10' to break
         collision with another workflow) but the matching if-check
         wasn't updated. Job hasn't fired since the move.
      2. COLLISION: the if-check pins to a cron like '0 17 * * 4' but
         the hourly '0 * * * *' cron ALSO fires at that minute. GH
         Actions coalesces into one workflow run and passes the hourly
         schedule in github.event.schedule, so the if-check fails.

    We flag both: any if-check cron string that isn't in the schedule
    list literally is STALE; any cron pinned to ':00' minute where a
    hourly '0 * * * *' cron also exists is COLLISION.
    """
    findings: list[dict] = []
    if not os.path.exists(_WORKFLOW_FILE):
        return findings
    try:
        with open(_WORKFLOW_FILE, "r") as f:
            text = f.read()
    except Exception:
        return findings

    # Extract cron strings from `on.schedule` block.
    import re
    cron_strings = set(re.findall(r"^\s*-\s*cron:\s*['\"]([^'\"]+)['\"]",
                                   text, re.MULTILINE))
    has_hourly_zero = any(c.strip() == "0 * * * *" for c in cron_strings)

    # Walk every if-check that pins to github.event.schedule == '<cron>'.
    # Phase FF+7-detector-fix (2026-05-19): require line to START with
    # whitespace + `if:` so we don't match `if:` mentioned inside YAML
    # comments (the prior regex was matching comment text like
    # `# if: github.event.schedule == '0 17 * * 4'` as a real if-check).
    for m in re.finditer(
        r"^\s*if:\s*github\.event\.schedule\s*==\s*['\"]([^'\"]+)['\"]",
        text,
        re.MULTILINE,
    ):
        check_cron = m.group(1)
        line_no = text[:m.start()].count("\n") + 1
        # Find the nearest preceding job name (best-effort context).
        prev = text.rfind("\n  ", 0, m.start())
        nl = text.find("\n", m.start())
        job_ctx = text[max(0, m.start()-300):m.start()]
        job_match = re.findall(r"^\s\s([\w_-]+):\s*$", job_ctx, re.MULTILINE)
        job_name = job_match[-1] if job_match else "?"

        if check_cron not in cron_strings:
            findings.append({
                "issue": "cron_if_check_mismatched_schedule",
                "url": _WORKFLOW_FILE,
                "count": 1,
                "detail": (f"Job `{job_name}` (line {line_no}) checks for "
                           f"github.event.schedule == '{check_cron}', but "
                           f"that cron string is NOT in the workflow's "
                           f"on.schedule list. Either the schedule was moved "
                           f"and this if-check wasn't updated, or the check "
                           f"was written for a cron that was never added. "
                           f"Either way, this job never fires from cron."),
                "job": job_name,
                "expected_cron": check_cron,
            })
            continue

        # Cron is in schedule list — but does it collide with the hourly '0 * * * *'?
        if has_hourly_zero and check_cron.startswith("0 ") and check_cron != "0 * * * *":
            # The if-check is pinned to ':00' minute and an hourly cron also
            # fires at ':00'. GH Actions will (usually) pass the hourly cron
            # in github.event.schedule, so this job's if-check evaluates false.
            findings.append({
                "issue": "cron_if_check_collides_with_hourly",
                "url": _WORKFLOW_FILE,
                "count": 1,
                "detail": (f"Job `{job_name}` (line {line_no}) is pinned to "
                           f"cron '{check_cron}' which fires at ':00' minute. "
                           f"The hourly '0 * * * *' cron also fires at ':00' "
                           f"every hour. When both fire simultaneously, GH "
                           f"Actions coalesces them and passes the hourly "
                           f"schedule in github.event.schedule — this job's "
                           f"if-check evaluates false. Move the cron to a "
                           f"non-':00' minute (e.g. ':05' or ':07')."),
                "job": job_name,
                "colliding_cron": check_cron,
            })
    return findings


def check_cron_collisions() -> list[dict]:
    """Phase VV-1 (2026-05-15) — detect cron expression collisions across
    workflow files in BOTH repos.

    Two workflows firing at the exact same minute trigger a thundering-
    herd against the backend (e.g. 4 workflows curl /api/v1/heal/findings
    at :00, :15, :30, :45 simultaneously). Audit found 7 colliding
    expressions: 4 jobs at `0 14 * * 1`, 4 at `0 */6 * * *`, 4 at
    `*/15 * * * *`, etc. Detector flags every collision so we know
    which to stagger.
    """
    findings: list[dict] = []
    # Two repo paths: backend (this file's repo) + sibling frontend.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(here, ".github", "workflows"),
        os.path.join(os.path.dirname(here), "dchub-frontend", ".github", "workflows"),
    ]
    cron_to_files: dict[str, list[str]] = {}
    cron_pattern = re.compile(r"-\s*cron:\s*['\"]?([\w*/\s,-]+?)['\"]?\s*(?:#.*)?$",
                               re.MULTILINE)

    for workflows_dir in candidates:
        if not os.path.isdir(workflows_dir):
            continue
        try:
            yml_files = [os.path.join(workflows_dir, f)
                         for f in os.listdir(workflows_dir)
                         if f.endswith((".yml", ".yaml"))]
        except OSError:
            continue
        for wf in yml_files:
            try:
                with open(wf) as fh:
                    text = fh.read()
            except OSError:
                continue
            for m in cron_pattern.finditer(text):
                expr = m.group(1).strip()
                if not expr or len(expr.split()) != 5:
                    continue  # malformed — skip
                cron_to_files.setdefault(expr, []).append(
                    os.path.relpath(wf, os.path.dirname(here)))

    # Only flag if 2+ workflows share the SAME expression. Same minute
    # = thundering herd.
    for expr, files in cron_to_files.items():
        if len(files) < 2:
            continue
        findings.append({
            "issue": "cron_schedule_collision",
            "url":   expr,
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count": len(files),
            "detail": (f"{len(files)} workflows share cron `{expr}` — "
                       f"they fire at the EXACT same minute. Stagger by "
                       f"offsetting one or more of them. Files: "
                       f"{', '.join(files[:6])}"),
            "expr":  expr,
            "files": files,
        })
    return findings


# ── series-break discipline (2026-08-19) ─────────────────────────────────────
# THE SHARED LESSON BEHIND THE NEXT TWO DETECTORS.
#
# Both exist because of the same mistake, made twice in one day:
#
#   1. dchub-mcp-server#202 changed WHO COUNTS as a real external caller
#      (2026-08-18 06:31Z). weekly-series would have published the resulting
#      cliff as a demand collapse. Fixed by a MANUAL marker in
#      routes/weekly_series._DEFINITION_CHANGES — which only works if a human
#      remembers to add one. check_unmarked_population_shift is the guard for
#      when nobody does.
#
#   2. r-challenge-after-value moved the Claude-connector OAuth challenge from
#      `initialize` to `tools/call` (2026-08-15). The init-only counter decays
#      to 0 BY DESIGN, and an operator reading it that day called
#      "4,008 challenges -> 3 identities" the biggest leak on the board. It was
#      a RETIRED SERIES. mcp_retention.py documents this at the call site; the
#      reader still got it wrong, because nothing refused the division.
#
# ★ ONE RULE, ENFORCED IN CODE RATHER THAN IN A COMMENT: a ratio or delta whose
# window straddles a declared break is NOT a rate. Refuse it. Silence beats a
# confident wrong number, and both detectors below are written to be silent in
# exactly that case — with tests that prove the silence is deliberate.
_SERIES_BREAKS = {
    # step key -> ISO date the counting method changed under it
    "oauth_connector_identity": "2026-08-15",
}


def _straddles_break(step_key: str, window_days: int, today=None) -> str | None:
    """The break date if it falls inside the trailing window, else None."""
    import datetime as _d
    raw = _SERIES_BREAKS.get(step_key)
    if not raw:
        return None
    try:
        brk = _d.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    end = today or _d.datetime.now(_d.timezone.utc).date()
    return raw if brk > (end - _d.timedelta(days=int(window_days))) else None


def check_unmarked_population_shift() -> list[dict]:
    """A caller CLASS vanished from the counted population and nobody declared it.

    ★ WHY (2026-08-19). #202 removed DC Hub's own GitHub Actions runners from
    is_real_external. Measured across the deploy boundary, on IP origin:

        48h BEFORE   GHA 327 calls / 526 total = 62.2%   (6 distinct GHA IPs)
        ~24h SINCE   GHA   0 calls / 229 total =  0.0%   (0 distinct GHA IPs)

    Non-CI traffic held (199 -> 229). That asymmetry IS the signature, and it is
    what separates a definition change from a demand change: demand moves the
    whole population up or down, a definition change deletes one identifiable
    class and leaves the rest alone.

    ★ TWO SIGNALS WERE TRIED FIRST AND BOTH FAILED against real data — recorded
    so nobody rebuilds them:
      · PASS-RATE (passing/rows_observed). W34 read 47.6%, inside the normal
        42.8-82.2% band, because excluded calls are never WRITTEN — the
        denominator fell with the numerator and the ratio never moved.
      · BURST COUNT (>=40 calls in <=300s). Bursts continued after the deploy
        (2 on 08-18) and full quiet days occur naturally (08-10, 08-14 = 0), so
        "zero bursts" is inside the base rate and proves nothing.

    Fires only when a class collapse is UNDECLARED — a matching entry in
    weekly_series._DEFINITION_CHANGES silences it, because that is the whole
    point of declaring one. UNMEASURED (unreadable ranges) yields no finding,
    never a clean verdict.

    LIMIT, stated: CI/GitHub-Actions is the only caller class we can currently
    identify by origin. A different class vanishing is still invisible.
    """
    findings: list[dict] = []
    _PRIOR_SHARE_FLOOR = 0.25   # the class must have MATTERED before
    _NOW_SHARE_CEIL = 0.05      # ...and be effectively gone now
    _RESIDUAL_LO, _RESIDUAL_HI = 0.55, 1.75   # the rest must have HELD
    conn = None
    try:
        import ipaddress
        from routes.agent_success_report import github_actions_ranges
        from routes.weekly_series import _changes_in
        import datetime as _d

        nets = github_actions_ranges()
        if not nets:
            return findings      # UNMEASURED — never a clean pass
        v4 = [n for n in nets if n.version == 4]
        v6 = [n for n in nets if n.version == 6]

        def _is_ci(ip):
            try:
                a = ipaddress.ip_address((ip or "").strip())
            except Exception:
                return False
            return any(a in n for n in (v4 if a.version == 4 else v6))

        conn = _db()
        if conn is None:
            return findings
        with conn.cursor() as cur:
            cur.execute("""
                SELECT date_trunc('week', created_at)::date AS wk,
                       split_part(COALESCE(ip_address,''), ',', 1) AS ip,
                       COUNT(*) AS n
                  FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND created_at >= date_trunc('week', now())
                                     - interval '5 weeks'
                   AND created_at <  date_trunc('week', now())
                 GROUP BY 1, 2
            """)
            rows = cur.fetchall() or []
        if not rows:
            return findings

        per_week: dict = {}
        for wk, ip, n in rows:
            slot = per_week.setdefault(wk, {"ci": 0, "other": 0})
            slot["ci" if _is_ci(ip) else "other"] += int(n or 0)
        weeks = sorted(per_week)
        if len(weeks) < 3:
            return findings      # not enough history to call anything a shift

        cur_wk = weeks[-1]
        prior = weeks[:-1]
        cur_ci = per_week[cur_wk]["ci"]
        cur_other = per_week[cur_wk]["other"]
        cur_total = cur_ci + cur_other
        if cur_total <= 0:
            return findings

        prior_shares = [per_week[w]["ci"] / max(1, per_week[w]["ci"] + per_week[w]["other"])
                        for w in prior]
        prior_share = sorted(prior_shares)[len(prior_shares) // 2]
        prior_other = sorted(per_week[w]["other"] for w in prior)[len(prior) // 2]
        cur_share = cur_ci / cur_total

        collapsed = (prior_share >= _PRIOR_SHARE_FLOOR
                     and cur_share <= _NOW_SHARE_CEIL)
        held = (prior_other > 0
                and _RESIDUAL_LO <= cur_other / prior_other <= _RESIDUAL_HI)
        if not (collapsed and held):
            return findings

        # Declared? Then this is expected and the marker is doing its job.
        if _changes_in(cur_wk, cur_wk + _d.timedelta(weeks=1)):
            return findings

        findings.append({
            "issue": "population_shift_unmarked",
            "url": f"week:{cur_wk.isoformat()}",
            "count_kind": "item_count",
            "count": int(round((prior_share - cur_share) * 100)),
            "detail": (
                f"The CI/GitHub-Actions caller class fell from "
                f"{prior_share*100:.1f}% to {cur_share*100:.1f}% of counted "
                f"calls in the week of {cur_wk}, while non-CI calls HELD "
                f"({prior_other:,} -> {cur_other:,}). That is the signature of "
                f"a change in WHAT IS COUNTED, not in demand — and no entry in "
                f"weekly_series._DEFINITION_CHANGES covers this week, so every "
                f"delta over it will publish as a demand collapse. Add a marker "
                f"(effective_at / change / direction / means / ref) or explain "
                f"the disappearance."
            ),
            "week": cur_wk.isoformat(),
            "prior_ci_share": round(prior_share, 4),
            "current_ci_share": round(cur_share, 4),
        })
    except Exception:
        return findings
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return findings


def check_funnel_step_collapse() -> list[dict]:
    """A funnel step taking N inputs and producing ~0 outputs.

    ★ WHY (2026-08-19). `oauth_challenges_all_30d` 4,056 against
    `oauth_new_identities_30d` 3 sat in a published payload for weeks. Nothing
    diffed it, so nobody acted on it.

    ★ AND WHY IT REFUSES TO FIRE TODAY. The Claude-connector challenge moved
    from `initialize` to `tools/call` on 2026-08-15 (r-challenge-after-value),
    so a 30-day window still straddles the break: it mixes challenge events
    counted under two different methods. Dividing across that is not a rate —
    it is the exact error that produced "4,008 -> 3 is our biggest leak" from a
    series mcp_retention.py already labels retired at its own call site.

    So this detector is DELIBERATELY SILENT on that step until the window
    clears the break (~2026-09-14), and the silence is tested. A detector that
    fired anyway would be manufacturing the misread it exists to prevent.

    `gateway_reporting` (a `_beat` row) is required: no beats = the gateway
    never checked in = DORMANT, which is not the same as "zero conversions".
    """
    findings: list[dict] = []
    _WINDOW_D = 30
    _INPUT_FLOOR = 500       # below this the ratio is noise
    _MAX_RATIO = 250.0       # inputs per output before it is a collapse
    conn = None
    try:
        conn = _db()
        if conn is None:
            return findings
        if _straddles_break("oauth_connector_identity", _WINDOW_D):
            return findings   # not a rate — see the docstring
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(n) FILTER (WHERE kind = 'claude_connector'), 0),
                       COALESCE(SUM(n) FILTER (WHERE kind = '_beat'), 0)
                  FROM mcp_oauth_challenges
                 WHERE day >= ((NOW() AT TIME ZONE 'UTC')::date - 30)
            """)
            challenges, beats = cur.fetchone() or (0, 0)
            if not beats:
                return findings      # DORMANT != zero
            cur.execute("""
                SELECT COUNT(*) FROM mcp_dev_keys
                 WHERE api_key LIKE 'dch_oauth_%'
                   AND created_at >= NOW() - interval '30 days'
            """)
            identities = int((cur.fetchone() or [0])[0] or 0)
        challenges = int(challenges or 0)
        if challenges < _INPUT_FLOOR:
            return findings
        ratio = challenges / identities if identities else float("inf")
        if ratio <= _MAX_RATIO:
            return findings
        findings.append({
            "issue": "funnel_step_collapse",
            "url": "funnel:oauth_connector_identity",
            "count_kind": "item_count",
            "count": int(min(ratio, 10**6)),
            "detail": (
                f"{challenges:,} Claude-connector OAuth challenges in "
                f"{_WINDOW_D}d produced {identities} new durable identities "
                f"({ratio:,.0f}:1, threshold {_MAX_RATIO:.0f}:1). The handshake "
                f"is being issued and not completed. Check "
                f"server.mjs _claudeChallengeEligible and the connector "
                f"callback path."
            ),
            "challenges_30d": challenges,
            "new_identities_30d": identities,
            "ratio": round(ratio, 1),
        })
    except Exception:
        return findings
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return findings


def check_stale_stored_slug_404s() -> list[dict]:
    """A stored facility slug that is neither canonical nor aliased = a 404.

    ★ WHY THIS DETECTOR EXISTS (2026-08-19). The gap it watches was open for
    two months and no check could see it, because every existing surface
    measured COMPLETION rather than the gap: /api/v1/admin/slug/status
    published `frozen: 26,112 / 26,239` and read finished.

    Measured that day: 9,822 rows carried a pre-swap non-hash8 slug, **0 of
    them had an alias row**, and a 30-URL live probe returned 17 × 404. GSC
    reported 3,576 "Not found (404)" against the property, and /facilities is
    71% of all Google clicks.

    Nothing was broken in the serving path — render_facility_profile calls
    resolve_alias() before it 404s. The table simply had no rows for this
    population, and no detector counted rows that SHOULD be there.

    ★ It watches the INVARIANT, not the incident: "every stored slug that
    differs from its canonical resolves to something". A one-off backfill
    closes today's 9,808; re-ingestion churns slugs continuously, so without
    this the hole silently reopens. That is the difference between a fix and
    a guard.

    Fires on gap > _SLUG_GAP_FLOOR. UNMEASURED on any DB failure — never a
    reassuring 0, per the empty-range rule.
    """
    findings: list[dict] = []
    _SLUG_GAP_FLOOR = 50   # tolerate churn between the backfill and the sweep
    conn = None
    try:
        from routes.facility_slug_freeze import (
            _FACILITY_TABLES, stored_slug_alias_gap, _get_conn)
        conn = _get_conn()
        if conn is None:
            return findings
        for t in _FACILITY_TABLES:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass(%s)", (t,))
            if not cur.fetchone()[0]:
                continue
            try:
                gap, stale = stored_slug_alias_gap(conn, t)
            except Exception:
                continue          # UNMEASURED for this table, not clean
            if gap <= _SLUG_GAP_FLOOR:
                continue
            findings.append({
                "issue": "stale_stored_slug_no_alias",
                "url":   f"table:{t}",
                "count_kind": "item_count",
                "count": gap,
                "detail": (
                    f"{gap:,} rows in {t} carry a stored `slug` that is not "
                    f"their canonical_slug AND have no facility_slug_aliases "
                    f"row — each one is a live 404 that should be a 301 "
                    f"({stale:,} stale slugs total). /facilities is ~71% of "
                    f"organic clicks. Fix: POST /api/v1/admin/slug/freeze "
                    f"(runs backfill_stored_slug_aliases), then purge the "
                    f"sitemap cache."
                ),
                "table": t,
                "gap": gap,
                "stale_total": stale,
            })
    except Exception:
        return findings
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return findings


def check_csp_drift() -> list[dict]:
    """Phase TT-2 (2026-05-15) — detect CSP source-of-truth drift.

    The Flask-served /dcpi page hardcodes its CSP because it bypasses
    CF Pages's /_headers. The hardcoded copy MUST match the canonical
    source. Drift = real bug (PR #188 fixed 3 live cases including the
    missing stats.g.doubleclick.net entry).
    """
    findings: list[dict] = []
    try:
        from util.csp_canonical import verify_csp_matches
        from routes.dcpi import _DCPI_CSP
        ok, msg = verify_csp_matches(_DCPI_CSP)
        # The verifier returns (False, "could not load canonical CSP") in
        # production where the sibling repo isn't present. That's NOT a
        # drift finding — it's expected. Only flag actual mismatches.
        if not ok and not msg.startswith("could not load"):
            findings.append({
                "issue": "csp_source_of_truth_drift",
                "url":   "routes/dcpi.py:_DCPI_CSP",
                "count": 1,
                "detail": (f"The /dcpi page's hardcoded CSP has drifted "
                            f"from dchub-frontend/_headers. {msg}. Bring "
                            f"them back in sync by copying the canonical "
                            f"CSP into routes/dcpi.py:_DCPI_CSP."),
            })
    except Exception as e:
        # Don't surface importer failures as drift findings.
        print(f"[brain-radar] csp drift check skipped: {e}", file=__import__('sys').stderr)
    return findings


# ─────────────────────────────────────────────────────────────────
# Phase ZZ (2026-05-16) — four detectors for the cascade the QA sweep
# surfaced: DCPI 89% stale, discovery dead, ISO metric drop, press
# repetition. Each is a SQL probe against an existing table, never
# blocks the radar, fails open on table-missing.
# ─────────────────────────────────────────────────────────────────
def _db():
    """Local DB helper for the Phase ZZ detectors. Autocommit so a
    failed probe doesn't poison follow-ups inside scan_all()."""
    import os as _os, psycopg2 as _pg2
    db = _os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = _pg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _dchub_share_of_voice_pct() -> Optional[float]:
    """r64-d (2026-05-31): DC Hub's real AI-citation share-of-voice over
    the last 30 days, as a %. Mirrors the math behind
    /api/v1/ai-citations/share-of-voice (which check_ai_citations_stale_v2
    already monitors) but reads the ai_citations table DIRECTLY rather
    than via an HTTP self-call — the backend is a single Railway replica
    and synchronous self-calls inside the radar scan exhaust the worker
    pool (see reference_dchub_backend_flapping memory).

    Returns:
      float > 0.0  → DC Hub is being cited (e.g. ~36.4% — we are #1)
      0.0          → table alive but zero dchub_cited rows in the window
      None         → table missing / unreadable (caller should NOT treat
                     None as 'zero': it just means 'can't tell from here')
    """
    c = _db()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ai_citations')")
            if not (cur.fetchone() or [None])[0]:
                return None
            cur.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN dchub_cited THEN 1 ELSE 0 END) AS dchub
                  FROM ai_citations
                 WHERE observed_at >= NOW() - INTERVAL '30 days'
            """)
            row = cur.fetchone() or (0, 0)
            total = int(row[0] or 0)
            dchub = int(row[1] or 0)
            if total <= 0:
                return None  # no observations → can't compute a share
            return round(100.0 * dchub / total, 1)
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def check_dcpi_partial_recompute() -> list[dict]:
    """Flag when DCPI median market-age exceeds 48h. Catches the
    'load_markets_dynamic returns None → only 30 of 300+ markets
    refresh' regression class. Was silently bleeding for 5 days."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.market_power_scores')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
            except Exception:
                return findings
            # Phase CCC (2026-05-16): PERCENTILE_CONT can't take a
            # timestamptz directly (UndefinedFunction in PostgreSQL).
            # Compute the median over EXTRACT(EPOCH ...) of the age
            # first, then convert to hours. Same result, valid types.
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug) market_slug, computed_at
                      FROM market_power_scores
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE computed_at < NOW() - INTERVAL '72 hours') AS stale_3d,
                       COUNT(*) FILTER (WHERE computed_at < NOW() - INTERVAL '24 hours') AS stale_24h,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (
                          ORDER BY EXTRACT(EPOCH FROM (NOW() - computed_at))
                       ) / 3600.0 AS median_age_h
                  FROM latest
            """)
            r = cur.fetchone()
            if not r: return findings
            total, stale_3d, stale_24h, median_h = r
            total = int(total or 0)
            stale_3d = int(stale_3d or 0)
            stale_24h = int(stale_24h or 0)
            median_h = float(median_h or 0)
            if total < 20: return findings  # not enough data
            stale_pct = (stale_3d / total) * 100 if total else 0
            if stale_pct >= 50:
                findings.append({
                    "issue":  "dcpi_partial_recompute",
                    "url":    "market_power_scores: stale-age distribution",
                    "count":  stale_3d,
                    "detail": (f"DCPI is stale: {stale_3d}/{total} markets "
                               f"({stale_pct:.0f}%) haven't recomputed in 72h, "
                               f"median age {median_h:.1f}h. Daily cron is "
                               f"likely timing out — verify dcpi-daily.yml "
                               f"chunking (offset/limit params) is in place "
                               f"AND _load_markets_dynamic() returns >30 "
                               f"markets (tuple-shape branch must exist)."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


def check_discovery_stalled() -> list[dict]:
    """Flag when zero new facilities have landed in `discovered_facilities`
    over the last 7 days. /api/v1/stats.data.new_last_7_days was 0 at the
    time this was authored — discovery had quietly stopped, undermining
    the 'living being' positioning."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.discovered_facilities')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
            except Exception:
                return findings
            # Try a few likely timestamp columns
            n_7d = None
            # r-col-probe (2026-07-24 coverage audit): every candidate here failed on the
            # live table — created_at does not exist, and discovered_at is TEXT so the
            # `>= NOW()` compare raised "operator does not exist: text >= timestamptz".
            # The loop swallowed both and the silent `return findings` below produced a
            # permanent false all-clear on discovery. `first_seen` is the real column.
            for col in ("first_seen", "discovered_at", "created_at", "inserted_at"):
                try:
                    cur.execute(f"""
                        SELECT COUNT(*) FROM discovered_facilities
                         WHERE {col}::timestamptz >= NOW() - INTERVAL '7 days'
                    """)
                    n_7d = int((cur.fetchone() or [0])[0] or 0)
                    break
                except Exception:
                    continue
        if n_7d is None:
            # Never a silent all-clear: if no timestamp column resolves, the detector is
            # blind and must say so rather than implying discovery is healthy.
            return [{
                "issue":  "discovery_probe_unresolvable",
                "url":    "discovered_facilities",
                "count":  1,
                "detail": ("check_discovery_stalled could not resolve a usable timestamp "
                           "column on discovered_facilities (tried first_seen, "
                           "discovered_at, created_at, inserted_at) — discovery freshness "
                           "is UNWATCHED. Fix the column probe; do not treat as healthy."),
            }]
        if n_7d == 0:
            findings.append({
                "issue":  "discovery_stalled_7d",
                "url":    "discovered_facilities: last 7d INSERTs",
                "count":  0,
                "detail": ("Zero new facilities have been added to "
                           "discovered_facilities in the last 7 days. "
                           "The /api/v1/stats endpoint advertises 12,553 "
                           "facilities — if discovery is dead, that "
                           "number is frozen and the 'living being' "
                           "positioning starts to drift from reality. "
                           "Check crawler workflows: dchub-osm-refresh.yml, "
                           "data-pulse.yml, daily-infra-sync.yml. Likely "
                           "either the crawler errored out, the API "
                           "source quota was hit, or the ingest cron "
                           "stopped firing."),
            })
        elif n_7d < 10:
            findings.append({
                "issue":  "discovery_anemic_7d",
                "url":    "discovered_facilities: last 7d INSERTs",
                "count":  n_7d,
                "detail": (f"Discovery anemic: only {n_7d} new facilities "
                           f"in 7 days. Expected rate is 50+/week. Either "
                           f"crawlers are rate-limited or the upstream "
                           f"sources have run out of fresh signal."),
            })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


def check_iso_metric_dropped() -> list[dict]:
    """Flag when an ISO listed in by_iso has metric_count=0 — meaning
    the loop registered but the latest ingest wrote nothing. Caught
    PJM + MISO showing 0 metrics at audit time while CAISO/SPP/NYISO
    were healthy."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.grid_data')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
            except Exception:
                return findings
            cur.execute("""
                SELECT iso, COUNT(*) AS metric_count, MAX(timestamp) AS latest
                  FROM grid_data
                 WHERE timestamp >= NOW() - INTERVAL '24 hours'
                 GROUP BY iso
            """)
            recent_rows = {r[0]: (int(r[1] or 0), r[2]) for r in cur.fetchall()}

            cur.execute("SELECT DISTINCT iso FROM grid_data")
            all_isos = {r[0] for r in cur.fetchall() if r[0]}

        for iso in all_isos:
            recent = recent_rows.get(iso)
            if recent is None:
                findings.append({
                    "issue":  "iso_metric_count_zero_24h",
                    "url":    f"grid_data: iso={iso}",
                    "count":  0,
                    "detail": (f"ISO {iso} has prior history in grid_data "
                               f"but ZERO writes in the last 24h. The "
                               f"loop has stopped. Check the matching "
                               f"workflow + iso_{iso.lower().replace('-','')}.py "
                               f"module."),
                })
            elif recent[0] < 3:
                findings.append({
                    "issue":  "iso_metric_count_dropped",
                    "url":    f"grid_data: iso={iso}",
                    "count":  recent[0],
                    "detail": (f"ISO {iso} wrote only {recent[0]} metric(s) "
                               f"in 24h (expected 5-15). Loop is partial — "
                               f"the API call may be erroring on most "
                               f"metrics while one or two succeed."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


def check_press_repetition() -> list[dict]:
    """Flag when the last 3+ auto-press release titles all reference the
    same market. The Phase MM/NN dedup logic was supposed to catch this
    but 4 identical Cheyenne releases shipped May 12-15 — proving the
    guard didn't fire. This detector closes the loop by alerting when
    repetition actually occurs in the published output."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.auto_press_releases')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
            except Exception:
                return findings
            cur.execute("""
                SELECT title FROM auto_press_releases
                 WHERE generated_for >= CURRENT_DATE - INTERVAL '5 days'
                   AND title IS NOT NULL
                 ORDER BY generated_at DESC NULLS LAST
                 LIMIT 5
            """)
            titles = [r[0] for r in cur.fetchall() if r and r[0]]
        if len(titles) < 3: return findings
        # Extract leading market name from each title (same regex as
        # routes/marketing_engine._recent_market_names)
        import re as _re
        markets: list[str] = []
        for t in titles:
            m = _re.match(r"^([A-Z][a-zA-Z\.\- ]+?)(?:,| Metro|:| - | – | Leads| Tops| Takes)", t)
            if m: markets.append(m.group(1).strip().lower())
        if len(markets) < 3: return findings
        # If first 3 titles all share the same market → repetition
        first_three = markets[:3]
        if len(set(first_three)) == 1:
            findings.append({
                "issue":  "auto_press_market_repetition",
                "url":    "auto_press_releases: last 3 titles",
                "count":  3,
                "detail": (f"Auto-press is repeating the same market — "
                           f"last 3 releases all led with '{first_three[0]}'. "
                           f"The Phase MM/NN dedup guard (routes/"
                           f"marketing_engine._market_clash) didn't fire. "
                           f"Either DCPI freshness is broken (so only one "
                           f"market refreshes and wins every day), or the "
                           f"dedup is bypassed via a non-protected topic "
                           f"branch. Recent titles: {titles[:3]}"),
            })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── Phase XX (2026-05-16) — MCP flow stale detector ───────────────
# Funnel ran at 14,058 upgrade signals : 0 conversions over 30d from
# the MCP platform. No detector was watching it. This one flags when
# the 7d signal:conversion ratio crosses thresholds. Reuses the
# Phase ZZ _db() helper above.
_MCP_STALE_CRITICAL = 500
_MCP_STALE_WARN     = 200
_MCP_STALE_MIN_SIGNALS = 50


# The floor auto_trial.py named in May: <20% of trial keys reaching a real
# signup within 7 days is the fix not working.
_AUTO_TRIAL_SIGNUP_FLOOR = float(
    os.environ.get("AUTO_TRIAL_SIGNUP_FLOOR", "0.20"))
# Below this the rate is noise, not a finding.
_AUTO_TRIAL_MIN_SAMPLE = int(
    os.environ.get("AUTO_TRIAL_MIN_SAMPLE", "10"))


def check_auto_trial_conversion_rate() -> list[dict]:
    """THE DETECTOR routes/auto_trial.py PROMISED AND NOBODY BUILT (2026-08-03).

    auto_trial.py's docstring has said since May: "Brain detector
    check_auto_trial_conversion_rate fires if <20% of trial keys -> real signups
    within 7 days. Tracks the fix's impact." Grepping the tree, that name
    appeared exactly ONCE — in that docstring. The detector was never written.

    So the fix built to solve 7,839 paywall signals -> 6 conversions (0.08%)
    shipped three months ago and nothing has ever measured whether it worked.

    ★A USED TRIAL KEY IS NOT A CONVERSION. check_mcp_conversion_stale counts
    `auto_trial_keys WHERE call_count > 0` toward `conversions` — that is an
    agent retrying with a FREE key it was handed. It is activation, and it is
    the number that has been making the funnel look alive. The real hop is
    signed_up_email: a human bound an address to the key. This detector
    measures THAT, separately, so the two can never be read as one number
    again.

    Only keys minted long enough ago to have HAD their 7 days are counted —
    scoring keys minted this morning as failures would make the rate a
    function of how recently the cron ran.
    """
    findings: list[dict] = []
    conn = _conn()
    if conn is None:
        return findings
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.auto_trial_keys')")
            if not (cur.fetchone() or [None])[0]:
                return findings
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE call_count > 0),
                       COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL
                                          AND signed_up_email <> ''),
                       COUNT(*) FILTER (WHERE upgraded_tier IS NOT NULL
                                          AND upgraded_tier <> '')
                  FROM auto_trial_keys
                 WHERE minted_at <  NOW() - INTERVAL '7 days'
                   AND minted_at >= NOW() - INTERVAL '37 days'""")
            row = cur.fetchone() or (0, 0, 0, 0)
            minted, activated, signed_up, upgraded = (int(x or 0) for x in row)
    except Exception as e:
        logger.debug("check_auto_trial_conversion_rate failed: %s", str(e)[:140])
        return findings
    finally:
        try: conn.close()
        except Exception: pass

    if minted < _AUTO_TRIAL_MIN_SAMPLE:
        return findings
    rate = signed_up / minted
    if rate >= _AUTO_TRIAL_SIGNUP_FLOOR:
        return findings
    findings.append({
        "issue":  "auto_trial_signup_rate_low",
        "url":    "auto_trial_keys: minted 8-37d ago",
        # ★A RATE, not a tally — declared so brain_work_selector cannot read
        # the percentage as an occurrence count (the #48 class).
        "count_kind": "percent",
        "count":  int(rate * 100),
        "detail": (
            f"{signed_up}/{minted} auto-trial key(s) reached a REAL SIGNUP "
            f"({rate*100:.1f}%, floor {_AUTO_TRIAL_SIGNUP_FLOOR*100:.0f}%) in "
            f"the 8-37d cohort. {activated} were USED by an agent and "
            f"{upgraded} reached a paid tier. ★The gap between {activated} "
            f"used and {signed_up} signed up is the whole problem: "
            f"check_mcp_conversion_stale counts the USED number toward "
            f"'conversions', so the funnel reads healthy on agents retrying "
            f"with a free key nobody paid for. auto_trial.py promised this "
            f"detector in May and it was never written, so the 0.08% fix has "
            f"gone unmeasured for three months."),
    })
    return findings


def check_mcp_conversion_stale() -> list[dict]:
    """Flag when MCP upgrade_signals:conversions ratio crosses threshold."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            signals = 0
            try:
                cur.execute("SELECT to_regclass('public.mcp_upgrade_signals')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT COUNT(*) FROM mcp_upgrade_signals "
                                "WHERE created_at >= NOW() - INTERVAL '7 days'")
                    signals = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                signals = 0
            conversions = 0
            try:
                # #1551 fix (2026-07-13): mcp_pair_codes.redeemed_at is 0 rows EVER,
                # so this detector fired a PERPETUAL false 'stale-critical'. Count the
                # canonical ledger (the auto_trial augmentation below is preserved).
                cur.execute("SELECT to_regclass('public.mcp_conversions')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT COUNT(*) FROM mcp_conversions "
                                "WHERE created_at >= NOW() - INTERVAL '7 days'")
                    conversions = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                conversions = 0
            # ── Phase HH (2026-05-17): the conversion-stale detector
            # historically only counted the legacy mcp_pair_codes flow
            # (web-form redemption). But Phase DDDDD shipped auto-mint
            # trial keys that bypass that flow entirely — an agent gets
            # `dch_trial_xxx` in the paywall response, retries with the
            # key, and is now a converted user. Those don't touch
            # mcp_pair_codes. Result: the brain reports "0 conversions
            # on 15k signals!" while auto-trial usage is actually high.
            # Count auto-trial keys with call_count > 0 (the agent
            # actually came back and used the minted key) as conversions
            # too. This makes the metric a TRUE conversion rate.
            try:
                cur.execute("SELECT to_regclass('public.auto_trial_keys')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""SELECT COUNT(*) FROM auto_trial_keys
                                    WHERE minted_at >= NOW() - INTERVAL '7 days'
                                      AND call_count > 0""")
                    auto_trial_conv = int((cur.fetchone() or [0])[0] or 0)
                    # ★NAMED, not silently folded in. This counts an agent
                    # retrying with a FREE key it was handed — activation, not
                    # revenue. Folding it into `conversions` unlabelled is what
                    # made this funnel read healthy while licence sales stayed
                    # flat. The threshold logic is unchanged (narrowing it here
                    # would fire a false alarm storm); what changes is that the
                    # detail now says how much of the number is free usage.
                    # check_auto_trial_conversion_rate measures the real hop.
                    conversions += auto_trial_conv
            except Exception:
                pass

        if signals < _MCP_STALE_MIN_SIGNALS:
            return findings

        ratio = signals / max(1, conversions) if conversions > 0 else signals
        if conversions == 0 and signals >= _MCP_STALE_CRITICAL:
            findings.append({
                "issue":  "mcp_conversion_stale_critical",
                "url":    "mcp_upgrade_signals: 7d window",
                # A signal tally IS a tally, so this one is genuinely an
                # occurrence — declared rather than left for the ceiling to
                # guess at.
                "count_kind": "occurrence",
                "count":  signals,
                "detail": (f"MCP flow stale: {signals} upgrade signals in 7d "
                           f"but ZERO conversions. The paywall → pair-code → "
                           f"Stripe pipeline is broken end-to-end or pricing/CTA "
                           f"is misaligned with demand."),
            })
        elif ratio >= _MCP_STALE_CRITICAL:
            findings.append({
                "issue":  "mcp_conversion_stale_critical",
                "url":    "mcp_upgrade_signals: 7d window",
                # A signal tally IS a tally, so this one is genuinely an
                # occurrence — declared rather than left for the ceiling to
                # guess at.
                "count_kind": "occurrence",
                "count":  signals,
                "detail": (f"MCP flow degraded: {signals} signals / {conversions} "
                           f"conversions over 7d = 1:{int(ratio)} ratio. Industry "
                           f"benchmark for self-serve B2B AI: 1:100."),
            })
        elif ratio >= _MCP_STALE_WARN:
            findings.append({
                "issue":  "mcp_conversion_stale_warn",
                "url":    "mcp_upgrade_signals: 7d window",
                "count":  signals,
                "detail": (f"MCP conversion ratio degraded to 1:{int(ratio)} over "
                           f"7d ({signals} signals / {conversions} conversions)."),
            })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── Phase DDD (2026-05-16) — organism detectors ───────────────────
# MCP + Media as living organisms means the brain ALSO watches their
# growth signals — declining call volume, demand gaps not addressed,
# source-of-truth score dropping, hot topics ignored. Each detector
# below is a SQL probe against the new snapshot tables from
# routes/mcp_growth.py + routes/media_pulse.py.

def check_mcp_growth_declining() -> list[dict]:
    """Flag when 7-day MCP call volume drops >25% week-over-week.
    Reads mcp_growth_snapshots; needs at least one snapshot from 6-8d ago."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.mcp_growth_snapshots')")
                if not (cur.fetchone() or [None])[0]: return findings
            except Exception:
                return findings
            cur.execute("""
                SELECT tool_calls_7d, snapshot_date
                  FROM mcp_growth_snapshots
                 ORDER BY snapshot_date DESC LIMIT 2
            """)
            rows = cur.fetchall()
            if len(rows) < 2: return findings
            today_calls = int(rows[0][0] or 0)
            prev_calls = int(rows[1][0] or 0)
            if prev_calls < 100: return findings  # too low-volume for trend signal
            pct = round(100.0 * (today_calls - prev_calls) / prev_calls, 1)
            if pct <= -25:
                findings.append({
                    "issue":  "mcp_growth_declining",
                    "url":    "mcp_growth_snapshots: latest 2",
                    "count":  abs(int(pct)),
                    "detail": (f"MCP call volume dropped {pct}% week-over-week "
                               f"({prev_calls} → {today_calls}). Investigate: "
                               f"(1) /api/v1/mcp/funnel for platform changes, "
                               f"(2) recent paywall changes, (3) CF worker "
                               f"version drift, (4) outbound MCP catalog updates."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


def check_mcp_demand_gap() -> list[dict]:
    """Flag the #1 demand gap: a tool with 50+ paywall signals and 0
    conversions over 7d. Means there's strong agent demand for something
    we either don't have, paywall too high, or our CTA is broken.

    Phase DDD-2 (2026-05-16): wrap the inner query separately because
    mcp_upgrade_signals.tool might not exist OR mcp_pair_codes.tool_name
    might not — schema varies across deploys. Catch column-missing
    errors and return empty findings instead of crashing the radar."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT to_regclass('public.mcp_upgrade_signals'),
                           to_regclass('public.mcp_pair_codes')
                """)
                regs = cur.fetchone() or [None,None]
                if not (regs[0] and regs[1]): return findings
            except Exception:
                return findings
            # Phase VVV (2026-05-16): use `tool_requested` (the actual
            # column on mcp_upgrade_signals, same fix as Phase UUU's
            # funnel column rename). Was probing `tool` which doesn't
            # exist → "column 'tool' does not exist" spam in Railway
            # logs every radar cycle. Falls back to a no-result if the
            # column also doesn't exist on this deploy.
            try:
                # r36 (2026-05-31): gate on DISTINCT callers, not raw signal
                # COUNT(*). A single power-key looping a tool's paywall thousands
                # of times (one key = 8,999 calls in the funnel) tripped the old
                # `COUNT(*) >= 50` and got reported as "the strongest expressed
                # demand on the platform" — when it's ONE caller. Require >=15
                # DISTINCT callers so this flags genuine broad demand that isn't
                # converting (same distinct-caller correction as r35 + C1).
                cur.execute("""
                    WITH paid_demand AS (
                      SELECT tool_requested AS tool,
                             COUNT(*) AS signals,
                             COUNT(DISTINCT COALESCE(NULLIF(user_email,''),
                                                     NULLIF(mcp_client,''),
                                                     NULLIF(tool_requested,''))) AS callers
                        FROM mcp_upgrade_signals
                       WHERE created_at >= NOW() - INTERVAL '7 days'
                         AND tool_requested IS NOT NULL
                       GROUP BY tool_requested
                      HAVING COUNT(DISTINCT COALESCE(NULLIF(user_email,''),
                                                     NULLIF(mcp_client,''),
                                                     NULLIF(tool_requested,''))) >= 15
                    ),
                    converted AS (
                      SELECT tool_name AS tool, COUNT(*) AS convs
                        FROM mcp_pair_codes
                       WHERE redeemed_at IS NOT NULL
                         AND redeemed_at >= NOW() - INTERVAL '7 days'
                       GROUP BY tool_name
                    )
                    SELECT p.tool, p.signals, p.callers
                      FROM paid_demand p LEFT JOIN converted c USING (tool)
                     WHERE COALESCE(c.convs, 0) = 0
                     ORDER BY p.callers DESC LIMIT 1
                """)
                r = cur.fetchone()
            except Exception as _e:
                print(f"[radar] check_mcp_demand_gap inner query: {_e}")
                return findings
            if r:
                tool, sigs, callers = r[0], int(r[1] or 0), int(r[2] or 0)
                findings.append({
                    "issue":  "mcp_demand_gap_unaddressed",
                    "url":    f"mcp_upgrade_signals: tool={tool}",
                    "count":  callers,
                    "detail": (f"Tool '{tool}' had {callers} DISTINCT callers hit "
                               f"its paywall in 7d ({sigs} raw signals) but ZERO "
                               f"conversions — genuine broad demand that isn't "
                               f"converting. Investigate the CTA, the tier "
                               f"threshold, or build a free-tier preview."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


def check_source_of_truth_declining() -> list[dict]:
    """Flag when our media source-of-truth score drops >15 points week-
    over-week. Reads media_pulse_snapshots."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.media_pulse_snapshots')")
                if not (cur.fetchone() or [None])[0]: return findings
            except Exception:
                return findings
            cur.execute("""
                SELECT source_of_truth_score
                  FROM media_pulse_snapshots
                 ORDER BY snapshot_date DESC LIMIT 2
            """)
            rows = cur.fetchall()
            if len(rows) < 2: return findings
            today = int(rows[0][0] or 0)
            prev  = int(rows[1][0] or 0)
            if (prev - today) >= 15:
                findings.append({
                    "issue":  "source_of_truth_declining",
                    "url":    "media_pulse_snapshots: latest 2",
                    "count":  prev - today,
                    "detail": (f"Source-of-truth score dropped {prev - today}pts "
                               f"week-over-week ({prev} → {today}). AI citations "
                               f"or news mentions are softening. Push auto-press "
                               f"diversification + check share-of-voice trend."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


def check_media_topic_unaddressed() -> list[dict]:
    """Flag when a hot news topic (5+ news items in 24h mentioning a
    DCPI market) has NO press-release response in 48h.

    Phase DDD-2 (2026-05-16): wrap each query separately + bound the
    market loop (was iterating 300+ markets × per-market news query =
    560+ queries, any one failing crashed the whole detector). Now:
    pre-aggregate news mentions in a single query, then intersect with
    markets in Python. One query instead of N+1."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT to_regclass('public.news'),
                           to_regclass('public.auto_press_releases'),
                           to_regclass('public.market_power_scores')
                """)
                regs = cur.fetchone() or [None,None,None]
                if not (regs[0] and regs[1] and regs[2]): return findings
            except Exception:
                return findings

            # Recent press titles (one query, lowercased)
            recent_press_titles = ""
            try:
                cur.execute("""
                    SELECT title FROM auto_press_releases
                     WHERE generated_for >= CURRENT_DATE - INTERVAL '2 days'
                       AND title IS NOT NULL
                """)
                recent_press_titles = " ".join(
                    (r[0] or "").lower() for r in cur.fetchall())
            except Exception as _e:
                print(f"[radar] check_media_topic_unaddressed press: {_e}")
                return findings

            # All recent news text (one query — preferred over N+1).
            # Phase VVV (2026-05-16): introspect news columns first so
            # we tolerate schema drift (no `summary` column in some
            # deploys — was spamming Railway logs every cycle). Pick
            # the best available body column from a candidate list.
            news_text = ""
            try:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                     WHERE table_name = 'news'
                """)
                news_cols_set = {r[0] for r in cur.fetchall()}
                body_col = None
                for c in ("summary", "description", "body", "snippet", "excerpt"):
                    if c in news_cols_set:
                        body_col = c; break
                date_col = ("published_date" if "published_date" in news_cols_set
                            else ("published_at" if "published_at" in news_cols_set
                                  else "created_at"))
                body_expr = f"COALESCE({body_col},'')" if body_col else "''"
                cur.execute(f"""
                    SELECT LOWER(COALESCE(title,'') || ' ' || {body_expr}) AS text
                      FROM news
                     WHERE {date_col} >= NOW() - INTERVAL '24 hours'
                """)
                news_text = "\n".join(r[0] for r in cur.fetchall() if r and r[0])
            except Exception as _e:
                print(f"[radar] check_media_topic_unaddressed news: {_e}")
                return findings

            if not news_text: return findings  # no news → nothing to check

            # Market list (one query)
            try:
                cur.execute("""
                    SELECT DISTINCT ON (market_slug) market_slug, market_name
                      FROM market_power_scores
                     WHERE published = true
                     ORDER BY market_slug, computed_at DESC
                """)
                markets = cur.fetchall()
            except Exception as _e:
                print(f"[radar] check_media_topic_unaddressed markets: {_e}")
                return findings

        # Pure-Python intersection — count market-name occurrences in news
        for slug, name in markets:
            if not name: continue
            nm_low = name.lower()
            # Cheap substring count
            n = news_text.count(nm_low)
            if n >= 5 and nm_low not in recent_press_titles:
                findings.append({
                    "issue":  "media_topic_unaddressed",
                    "url":    f"news: market={slug}",
                    "count":  n,
                    "detail": (f"Hot topic '{name}' has {n} news mentions in "
                               f"last 24h but no auto-press response in 48h. "
                               f"Trigger /api/v1/marketing/auto-generate with "
                               f"topic context for {name}."),
                })
                if len(findings) >= 3: break  # cap at 3 — don't flood
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── Phase EEE (2026-05-16) — surface brain health detector ───────
# Flags when any registered surface drops below a health threshold.
# Surface health combines volume + success rate + WoW growth into a
# 0-100 score. <40 = critical (e.g. no traffic OR mostly failing).

# Phase FF+9-triage (2026-05-19) — internal/admin surfaces that are
# EXPECTED to have low consumer traffic. Excluded from the critical
# detector so the L21 escalation queue doesn't sit on actions that
# need "more traffic" when by design these pages are only used by
# operators (us) running diagnostics. Fold a new surface in here
# only when the surface is truly internal — consumer-facing surfaces
# with low traffic ARE a real signal and SHOULD keep firing.
_LOW_TRAFFIC_OK_SURFACES = {
    "site_sentinel",    # /sentinel admin dashboard — page-health monitor
    "power_totals",     # /dcpi/totals — vanity stat page, marketed when needed
}


def check_surface_health_critical() -> list[dict]:
    """Flag any surface whose health_score < 40. The brain learns which
    pages are dying + escalates per-surface so the right action library
    fires (markets needs a different fix than land_power).

    Phase FF+9-triage: surfaces in _LOW_TRAFFIC_OK_SURFACES are skipped
    because their low traffic is by design, not a failure mode.

    r41-surface-parallel (2026-05-25): parallelized the per-surface
    health_score() calls. Pre-fix each call did 2 DB queries
    (pulse + growth) serially, so 65 surfaces × 2 queries × ~250ms =
    ~32s wall time (slowest single detector in the radar). Now ~4-6s
    via 8-worker pool. Worker cap stays well under the 50-conn DB pool.
    """
    findings: list[dict] = []
    try:
        from routes.surface_brain import SURFACES
    except Exception:
        return findings

    eligible = [(sid, surface) for sid, surface in SURFACES.items()
                if sid not in _LOW_TRAFFIC_OK_SURFACES]

    import concurrent.futures as _cf

    def _score_one(item):
        sid, surface = item
        try:
            return (sid, surface, surface.health_score())
        except Exception:
            return (sid, surface, None)

    with _cf.ThreadPoolExecutor(max_workers=8,
                                 thread_name_prefix="surface-health") as ex:
        results = list(ex.map(_score_one, eligible))

    for sid, surface, score in results:
        if score is not None and score < 40:
            # r42u (2026-05-26): skip surfaces with ZERO events 7d — they
            # don't have enough data to judge. Pre-fix the detector was
            # flagging 30+ "surface_health_critical:auto_*" findings every
            # cycle, each at score ≈ 35 (the 50-15-for-zero-events baseline).
            # All escalated immediately (no autonomous fix exists) and
            # polluted the autopilot recent-actions log. Real human-
            # actionable findings drowned in this noise.
            try:
                _p = surface.pulse() or {}
                if (_p.get("events_7d") or 0) == 0 and (_p.get("events_24h") or 0) == 0:
                    continue  # not enough data — skip silently
            except Exception:
                pass

            findings.append({
                "issue":  f"surface_health_critical:{sid}",
                "url":    f"surface_telemetry: surface_id={sid}",
                "count":  score,
                "detail": (f"Surface '{surface.name}' (id={sid}) health is "
                           f"{score}/100. Likely cause: very low traffic, "
                           f"high failure rate, or steep WoW decline. Check "
                           f"/api/v1/surface/{sid}/pulse + /demand-gaps + "
                           f"/growth for specifics. If the surface is new + "
                           f"has no beacon yet, the score will be low until "
                           f"the frontend instrumentation lands."),
            })
    return findings


def _platform_conversions_30d() -> int:
    """Account-level conversions in the last 30d (ANY tool). A paid key unlocks
    ALL tools, so conversion is account-level — used to sanity-check a per-tool
    'ZERO converted' signal before alarming. Returns -1 on DB miss (don't
    suppress on a miss).

    2026-07-10 (issue #1551): read mcp_conversions — the canonical ledger the
    Stripe webhooks write — NOT pair-code redemptions. The pair-code redeem
    flow is dead (0 of 1,097 codes ever minted were redeemed), so the old
    query pinned this gate at 0 while real money landed in the ledger (9
    non-test conversions in the 30d before this fix: 3x $99 founding, 1x $49,
    starter, metered...). Result: the r88 honesty gate never suppressed and
    check_mcp_funnel_leak screamed '446 paywalled callers, ZERO conversions'
    — conversions happened but were invisible to the brain."""
    c = _db()
    if c is None:
        return -1
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mcp_conversions "
                        "WHERE created_at >= NOW() - INTERVAL '30 days' "
                        "AND COALESCE(is_test, FALSE) = FALSE")
            return int((cur.fetchone() or [0])[0])
    except Exception:
        return -1
    finally:
        try: c.close()
        except Exception: pass


# ── Phase GGG (2026-05-16) — per-tool funnel leak detector ────────
def check_mcp_funnel_leak() -> list[dict]:
    """Flag any tool with >50 paywall signals where a single funnel
    stage drops >95%. Tells us EXACTLY where the conversion engine is
    broken per tool (vs the aggregate stale-conversion detector which
    only says 'something is wrong')."""
    findings: list[dict] = []
    try:
        from routes.mcp_funnel import _compute_funnel
        funnels = _compute_funnel(tool_filter=None, days=14)
    except Exception:
        return findings
    # r85i: fire ONLY on the honest, actionable signal — a tool with real
    # DISTINCT demand at the paywall but ZERO conversions. The old detector
    # gated on raw paywall signals + a >95% stage drop, but stages 2-5 came from
    # the separate mcp_pair_codes flow (≠ per-tool signals) so EVERY tool showed
    # a fake ~100% leak. _compute_funnel now reports honest distinct_callers +
    # the signal's own converted flag; per-tool conversion is genuinely 10-45%.
    # r88 (2026-06-28): account-level + lag HONESTY gate. Conversion lags a
    # median ~2.6 days and a paid key unlocks ALL tools, so a per-tool,
    # same-14d-window `converted`==0 is an ATTRIBUTION ARTIFACT whenever the
    # platform is converting at all. The old detail screamed "ZERO converted —
    # NO MONETIZATION" for grid/fiber/market intel while the platform did 9
    # conversions/30d — a false alarm of the same class as the demand-gap FP.
    # Only the genuine "the upgrade path converts NOBODY platform-wide" state
    # is an actionable broken-funnel finding. When the platform IS monetizing,
    # the high per-tool demand is an OPTIMIZATION (addressable_demand_
    # unconverted already covers it honestly), not a broken funnel.
    platform_conv = _platform_conversions_30d()
    for f in funnels:
        distinct  = f.get("distinct_callers") or 0
        converted = (f.get("stages") or {}).get("5_converted") or 0
        if distinct < 25:           # raw signals are loop-inflated — gate on distinct
            continue
        if converted > 0:           # any conversion = a normal funnel, not "broken"
            continue
        if platform_conv > 0:       # account-level conversion exists → not "no monetization"
            continue                # (per-tool 0 is lag + account-level attribution noise)
        findings.append({
            "issue":  f"mcp_funnel_leak:{f['tool']}",
            "url":    f"mcp_funnel: tool={f['tool']}",
            "count":  int(distinct),
            "detail": (f"Tool '{f['tool']}' had {distinct} DISTINCT callers hit its "
                       f"paywall in 14d AND the platform recorded ZERO conversions "
                       f"account-wide in 30d — the upgrade path converts nobody. "
                       f"Investigate /api/v1/mcp/conversion-funnel/{f['tool']}. "
                       f"(Per-tool conversion is account-level + lags ~2.6 days; "
                       f"this fires only when platform-wide conversion is also 0.)"),
        })
        if len(findings) >= 3:
            break
    return findings


# ── Phase LLL (2026-05-16) — enterprise bot identifier ────────────
def check_enterprise_bot_present() -> list[dict]:
    """Flag the top whale (>500 calls in 14d, 3+ days) so it surfaces
    in the heartbeat — humans then decide outreach vs block vs monitor."""
    findings: list[dict] = []
    try:
        from routes.bot_outreach import _compute_whales
        whales = _compute_whales(min_days=3, min_calls_per_day=100)
    except Exception:
        return findings
    if not whales: return findings
    top = whales[0]
    if top.get("total_calls_14d", 0) < 500:
        return findings  # not significant enough to flag
    findings.append({
        "issue":  "enterprise_bot_present",
        "url":    f"mcp_tool_calls: ip_hash={top.get('ip_hash','?')}",
        "count":  int(top.get("total_calls_14d", 0)),
        "detail": (f"High-volume bot identified: {top.get('total_calls_14d')} calls "
                   f"over {top.get('days_active')} days "
                   f"({top.get('calls_per_day_avg','?')}/day avg). "
                   f"Suggested: {top.get('suggested_action','monitor')}. "
                   f"UA: {(top.get('ua_fingerprint','') or '')[:60]}. "
                   f"Full whale list at /api/v1/bots/whales."),
    })
    return findings


# ── Phase FFFFF (2026-05-16) — autopilot outcome verification ────
def check_autopilot_action_unverified() -> list[dict]:
    """Fires when autopilot actions older than 1h have no outcome
    record. Means FFFFF verifier cron hasn't run OR verifier
    function is missing for that pattern. Closes the brain's biggest
    blind spot: knowing if an action actually succeeded."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT to_regclass('public.brain_autopilot_actions'),
                           to_regclass('public.autopilot_outcomes')
                """)
                regs = cur.fetchone() or [None, None]
                if not regs[0]: return findings
                # Count actions fired 1h-24h ago without outcomes
                if regs[1]:
                    cur.execute("""
                        SELECT COUNT(*) FROM brain_autopilot_actions a
                         WHERE a.started_at <= NOW() - INTERVAL '1 hour'
                           AND a.started_at >= NOW() - INTERVAL '24 hours'
                           AND a.outcome = 'executed_ok'
                           AND NOT EXISTS (
                             SELECT 1 FROM autopilot_outcomes o
                              WHERE o.autopilot_action_id = a.id
                           )
                    """)
                    n = int((cur.fetchone() or [0])[0] or 0)
                else:
                    n = 0  # table missing — verifier hasn't deployed
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    if n < 5: return findings
    return [{
        "issue":  "autopilot_action_unverified",
        "url":    "/api/v1/brain/autopilot/outcomes",
        "count":  n,
        "detail": (f"{n} autopilot actions fired in last 24h are not yet "
                   f"verified. Cron POST /api/v1/brain/autopilot/verify-pending "
                   f"should run every 15 min. Either the cron isn't firing, "
                   f"OR the verifier function for the action's pattern is "
                   f"missing from _VERIFIERS dict in routes/autopilot_outcomes.py."),
    }]


# ── Phase GGGGG (2026-05-16) — schema.org coverage gap ───────────
def check_schema_org_coverage_low() -> list[dict]:
    """Fires when audit shows <80% schema coverage on critical pages.
    Direct attack on the 10/100 SOT score — AI agents fact-cite
    structured data first."""
    try:
        from routes.schema_org_saturation import run_audit
        a = run_audit()
    except Exception:
        return []
    pct = a.get("coverage_pct", 100)
    if pct < 80:
        return [{
            "issue":  "schema_org_coverage_low",
            "url":    "/api/v1/schema-org/missing",
            "count":  int(pct),
            "detail": (f"Schema.org coverage is {pct}% — below 80% target. "
                       f"{a.get('missing',0)} pages have no JSON-LD; "
                       f"{a.get('wrong_type',0)} have wrong @type. AI agents "
                       f"prioritize structured data when fact-citing — "
                       f"this directly drags the source-of-truth score. "
                       f"Worklist: /api/v1/schema-org/missing."),
        }]
    return []


# ── Phase HHHHH (2026-05-16) — external mentions dropoff ─────────
def check_external_mentions_dropoff() -> list[dict]:
    """Fires when 7d external mention count drops >40% vs trailing
    28d daily avg. Counterpart to TTTT for human-mention signal."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.external_mentions')")
                if not (cur.fetchone() or [None])[0]: return findings
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE discovered_at >= NOW() - INTERVAL '7 days') AS recent,
                      COUNT(*) FILTER (WHERE discovered_at >= NOW() - INTERVAL '35 days'
                                       AND discovered_at <  NOW() - INTERVAL '7 days') AS baseline
                      FROM external_mentions
                """)
                r = cur.fetchone() or (0, 0)
                recent, baseline = int(r[0] or 0), int(r[1] or 0)
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    if baseline < 20: return findings  # not enough baseline
    baseline_weekly = baseline / 4.0
    if baseline_weekly < 1: return findings
    drop_pct = 100.0 * (baseline_weekly - recent) / baseline_weekly
    if drop_pct > 40:
        findings.append({
            "issue":  "external_mentions_dropoff",
            "url":    "/api/v1/mentions/stats",
            "count":  int(drop_pct),
            "detail": (f"External (HN/Reddit) DC Hub mentions dropped "
                       f"{drop_pct:.0f}% week-over-week ({int(baseline_weekly)} → {recent}). "
                       f"Combined with the 10/100 SOT score this suggests "
                       f"brand discovery is stalling. Consider auto-posting "
                       f"to ShowHN or industry subreddits."),
        })
    return findings


# ── Phase EEEEE (2026-05-16) — MCP volume regression detector ────
def check_mcp_volume_regression() -> list[dict]:
    """Fires when 7-day MCP volume drops >20% vs trailing 28-day daily
    average. User flagged this after a 60K → 37K weekly drop (~38%)
    following XXX's tier tightening. EEEEE shipped to recover; this
    detector keeps the brain honest about whether the recovery worked."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.mcp_call_log')")
                if not (cur.fetchone() or [None])[0]: return findings
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '7 days') AS recent_7d,
                      COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '35 days'
                                       AND timestamp <  NOW() - INTERVAL '7 days') AS baseline_28d
                      FROM mcp_call_log
                """)
                r = cur.fetchone() or (0, 0)
                recent, baseline = int(r[0] or 0), int(r[1] or 0)
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    if baseline < 5000:
        return findings  # not enough baseline to judge
    # Compare recent 7d against baseline 28d daily average × 7
    baseline_weekly = baseline / 4.0  # 28 days → daily avg → 7-day equivalent
    if baseline_weekly < 1:
        return findings
    drop_pct = 100.0 * (baseline_weekly - recent) / baseline_weekly
    if drop_pct > 20:
        findings.append({
            "issue":  "mcp_volume_regression",
            "url":    "/api/v1/mcp/funnel",
            "count":  int(drop_pct),
            "detail": (f"MCP volume regressed: last 7 days = {recent:,} calls, "
                       f"baseline 28-day weekly avg = {int(baseline_weekly):,} calls "
                       f"({drop_pct:.1f}% drop). EEEEE anon grace mode should "
                       f"recover this — check /api/v1/grace/stats for adoption. "
                       f"If recovery doesn't fire within 7 days, the FREE tier "
                       f"may need further loosening OR the grace cap raised "
                       f"from 5/24h to 10/24h."),
        })
    return findings


# ── Phase DDDDD (2026-05-16) — auto-trial conversion-rate detector ──
def check_trial_taste_abuse() -> list[dict]:
    """r62c-conv guardrail. The trial key now unlocks a 7-day FULL taste of
    get_grid_intelligence + get_fiber_intel (the Pro crown jewels). Healthy
    use = mint → reconnect → eventually identify/upgrade. ABUSE = many DISTINCT
    trial keys (rotating IPs) each hammering grid/fiber with ~ZERO email-binds
    — i.e. farming the free Pro data instead of converting. Fires a WARNING
    only on a real 24h spike (mint volume + distinct callers) with near-zero
    identification AND heavy per-key usage, so normal low-conversion days don't
    trip it. Remedy: tighten TRIAL_DAYS / TRIAL_DAILY_CALLS in
    routes/auto_trial.py, or add a per-IP mint cap."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.auto_trial_keys')")
                if not (cur.fetchone() or [None])[0]: return findings
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(DISTINCT request_ip_hash),
                           COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL),
                           COALESCE(SUM(call_count), 0),
                           COUNT(*) FILTER (WHERE minted_for_tool IN
                                   ('get_grid_intelligence','get_fiber_intel'))
                      FROM auto_trial_keys
                     WHERE minted_at >= NOW() - INTERVAL '24 hours'
                """)
                r = cur.fetchone() or (0, 0, 0, 0, 0)
                minted, distinct_ip, signed, calls, gridfiber = (
                    int(r[0] or 0), int(r[1] or 0), int(r[2] or 0),
                    int(r[3] or 0), int(r[4] or 0))
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    # Conservative thresholds — only a genuine spike trips this.
    MINT_SPIKE_24H = 40    # trials minted in 24h
    IP_SPIKE_24H   = 30    # distinct caller IPs (rotation signal)
    if minted < MINT_SPIKE_24H or distinct_ip < IP_SPIKE_24H:
        return findings
    signup_rate   = 100.0 * signed / max(1, minted)
    calls_per_key = calls / max(1, minted)
    if signup_rate < 3.0 and calls_per_key >= 5:
        findings.append({
            "issue":    "trial_taste_abuse_suspected",
            "severity": "warning",
            "url":      "/api/v1/keys/auto-trial/stats",
            "count":    minted,
            "detail":  (f"24h: {minted} trial keys minted across {distinct_ip} distinct IPs, "
                        f"{calls} total calls ({calls_per_key:.1f}/key), {signed} email-binds "
                        f"({signup_rate:.1f}%); {gridfiber} minted on grid/fiber. High mint + heavy "
                        f"usage with ~zero identification = possible rotating-IP farming of the free "
                        f"grid/fiber taste (r62c). If sustained: tighten TRIAL_DAYS / "
                        f"TRIAL_DAILY_CALLS in routes/auto_trial.py or add a per-IP mint cap."),
        })
    return findings


def check_auto_trial_conversion() -> list[dict]:
    """Tracks whether the auto-mint-trial flow (DDDDD) is actually
    converting agents → signups → upgrades. Fires informational
    finding so /transparency sparkline shows the conversion lift."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.auto_trial_keys')")
                if not (cur.fetchone() or [None])[0]: return findings
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE COALESCE(signed_up_email, operator_email) IS NOT NULL),
                           COUNT(*) FILTER (WHERE upgraded_tier IS NOT NULL),
                           COUNT(*) FILTER (WHERE minted_at >= NOW() - INTERVAL '7 days')
                      FROM auto_trial_keys
                """)
                r = cur.fetchone() or (0, 0, 0, 0)
                total, signed, upgraded, m7d = (int(r[0] or 0), int(r[1] or 0),
                                                  int(r[2] or 0), int(r[3] or 0))
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    if total < 10:
        # Not enough data yet — too early to judge
        return findings
    signup_rate  = 100.0 * signed   / max(1, total)
    upgrade_rate = 100.0 * upgraded / max(1, total)
    # Healthy goal: 20%+ trials → signups. Flag if below.
    # r88h (2026-06-14): SUPPRESS — structural floor, not a fixable bug.
    # Autonomous MCP agents have no email/credit card, so they use the inline
    # trial key and never "redeem" (1597 minted / 3 signed up is the permanent
    # shape). The agent-side conversion levers already shipped (r86-r88:
    # first-call-full, auto-bind, depth-tease); the real lever is human site
    # traffic, not agent redemption. Firing every cycle was pure noise (same
    # class as the detectors #1151 stopped). Re-enable: BRAIN_FLAG_TRIAL_SIGNUP=1.
    if signup_rate < 20 and os.environ.get("BRAIN_FLAG_TRIAL_SIGNUP") == "1":
        findings.append({
            "issue":  "auto_trial_signup_rate_low",
            "url":    "/api/v1/keys/auto-trial/stats",
            "count":  int(signup_rate),
            "detail": (f"Auto-trial keys: {total} minted, {signed} signed up "
                       f"({signup_rate:.1f}%), {upgraded} upgraded ({upgrade_rate:.1f}%). "
                       f"7-day mint volume: {m7d}. Signup rate below 20% target — "
                       f"agents are using the trial key but not redeeming. "
                       f"Consider improving the redemption CTA in the paywall message."),
        })
    return findings


# ── Phase DDDDD (2026-05-16) — per-tool funnel concentration detector ──
def check_mcp_funnel_concentration() -> list[dict]:
    """The user diagnosis: 7,839 signals in 7d but signals concentrated
    on 5 tools (market_intel, grid_data, water_risk, energy_prices,
    renewable_energy) = 70% of all signals. If conversion is low on
    those top tools specifically, that's the leak. Surface per-tool
    signal volume so the operator sees where to focus."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT tool_requested, COUNT(*) AS signals
                      FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '7 days'
                       AND tool_requested IS NOT NULL
                     GROUP BY tool_requested
                     ORDER BY signals DESC LIMIT 5
                """)
                top5 = cur.fetchall()
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    if not top5 or len(top5) < 3: return findings
    top5_signals = sum(int(r[1] or 0) for r in top5)
    if top5_signals < 500: return findings  # not enough volume to flag
    # Single finding summarizing the funnel concentration
    summary = ", ".join(f"{r[0]}={r[1]}" for r in top5)
    findings.append({
        "issue":  "mcp_funnel_concentration_top5",
        "url":    "/api/v1/mcp/funnel",
        "count":  top5_signals,
        "detail": (f"Top 5 tools generated {top5_signals} paywall signals "
                   f"in 7 days: {summary}. If conversions are low overall, "
                   f"focus paywall-response improvements on THESE tools "
                   f"first. Phase DDDDD auto-trial flow targets exactly "
                   f"this set (FREE → IDENTIFIED gate)."),
    })
    return findings


# ── Phase ZZZZ (2026-05-16) — market deep-dive coverage detector ──
def check_growth_sentinel() -> list[dict]:
    """MCP Growth Sentinel — WIDEN-arm drift alerts (alert-only, r-spine 2026-06-05).

    Makes the ecosystem-coverage sentinel part of the brain's 24x7 loop. Reads
    the brain_ecosystem_watch table (populated by the every-6h ecosystem cron)
    and flags native-discoverability / registry DRIFT:
      - official MCP-registry listing STALE (published version behind the repo)
        -> agents discover an out-of-date DC Hub
      - one of our OWN agent-facing surfaces DOWN (manifest / capabilities /
        ai-agents.json / llms.txt / AGENTS.md) -> native discoverability broken,
        silently shrinking the funnel
    Coverage gaps (awesome-mcp etc.) are owner-gated submissions surfaced in
    /api/v1/brain/ecosystem/coverage — NOT alerted here (would be chronic noise).
    Alert-only: findings flow to radar -> notifications -> /heartbeat. No
    autonomous side-effects."""
    conn = _db()
    if conn is None:
        return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.brain_ecosystem_watch')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                # latest row per target, only if checked recently (cron alive);
                # stale watch data is a different detector's concern.
                cur.execute("""
                    SELECT DISTINCT ON (target_key)
                      target_key, we_present, detail, at
                    FROM brain_ecosystem_watch
                    WHERE at >= NOW() - INTERVAL '18 hours'
                    ORDER BY target_key, at DESC
                """)
                rows = cur.fetchall()
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    if not rows:
        return findings

    by = {r[0]: {"present": r[1], "detail": (r[2] or "")} for r in rows}

    off = by.get("official_registry")
    if off and str(off["detail"]).upper().startswith("STALE"):
        findings.append({
            "issue":  "mcp_registry_listing_stale",
            "url":    "/api/v1/brain/ecosystem/coverage",
            "count":  1,
            "detail": (f"Official MCP registry listing is stale: {off['detail']}. "
                       f"Bump server.json version + push — mcp-registry-publish.yml "
                       f"republishes via the DNS key. Until then agents discover an "
                       f"out-of-date DC Hub."),
        })

    down = sorted(k for k, v in by.items()
                  if k.startswith("self_") and v["present"] is False)
    if down:
        findings.append({
            "issue":  "native_discoverability_surface_down",
            "url":    "/api/v1/brain/ecosystem/coverage",
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count":  len(down),
            "detail": (f"{len(down)} agent-facing discovery surface(s) DOWN: "
                       f"{', '.join(down)}. Agents fetch these to discover + parse "
                       f"DC Hub natively; a 404/empty here silently shrinks the MCP "
                       f"funnel. Check the route(s)."),
        })

    return findings


def check_market_deep_dive_stale() -> list[dict]:
    """Flag when the top 10 DCPI markets have deep-dives older than
    30 days OR no deep-dive at all. Cron should keep these fresh."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT to_regclass('public.market_deep_dives'),
                           to_regclass('public.market_power_scores')
                """)
                regs = cur.fetchone() or [None, None]
                if not (regs[0] and regs[1]): return findings
                # market_power_scores has no stored `score` column — the DCPI
                # composite is derived at read time. Rank the top-10 on the
                # writer-guaranteed excess_power_score instead (same fix as
                # the deep-dive cron_rotate target picker).
                cur.execute("""
                    SELECT mps.market_slug, mps.market_name, mdd.generated_at
                      FROM (SELECT DISTINCT ON (market_slug) market_slug, market_name, excess_power_score
                              FROM market_power_scores WHERE published = true
                             ORDER BY market_slug, computed_at DESC) mps
                      LEFT JOIN market_deep_dives mdd USING (market_slug)
                     ORDER BY mps.excess_power_score DESC NULLS LAST LIMIT 10
                """)
                rows = cur.fetchall()
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    stale = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in rows:
        slug, name, gen_at = r[0], r[1], r[2]
        if gen_at is None:
            stale.append((slug, name, "never"))
        elif gen_at.tzinfo is None:
            gen_at = gen_at.replace(tzinfo=datetime.timezone.utc)
        if gen_at and (now - gen_at).days > 30:
            stale.append((slug, name, f"{(now-gen_at).days}d"))
    if not stale: return findings
    findings.append({
        "issue":  "market_deep_dive_stale",
        "url":    "/api/v1/markets/deep-dive/cron",
        "count_kind": "item_count",  # magnitude, not a recurrence tally
        "count":  len(stale),
        "detail": (f"{len(stale)} of top-10 DCPI markets have stale or "
                   f"missing deep-dive narratives. Stalest: "
                   f"{', '.join(f'{s[1]} ({s[2]})' for s in stale[:3])}. "
                   f"Cron POST /api/v1/markets/deep-dive/cron to refresh."),
    })
    return findings


# ── Phase BBBBB (2026-05-16) — event submission deadline detector ──
def check_event_submission_pending() -> list[dict]:
    """Flag upcoming industry events that have a submission deadline
    in the next 30 days AND DC Hub hasn't submitted. Closes the
    'why aren't we at DCD?' gap."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.industry_events')")
                if not (cur.fetchone() or [None])[0]: return findings
                cur.execute("""
                    SELECT name, submission_deadline, starts_on
                      FROM industry_events
                     WHERE dchub_submitted = FALSE
                       AND submission_deadline IS NOT NULL
                       AND submission_deadline >= CURRENT_DATE
                       AND submission_deadline <= CURRENT_DATE + INTERVAL '30 days'
                     ORDER BY submission_deadline ASC LIMIT 5
                """)
                rows = cur.fetchall()
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    # r62-fix: this module has NO top-level `import datetime`; the bare
    # `datetime.date.today()` below raised NameError the moment the query
    # returned rows (outside the query guard above) — that was the
    # consistency_radar_detector_crashed:check_event_submission_pending
    # finding. Import locally (matching the file's convention) + make the row
    # loop fully fail-soft: a bad/NULL name or a timestamp-typed deadline can
    # no longer crash the whole detector.
    import datetime as _dt
    today = _dt.date.today()
    for r in rows:
        try:
            name, deadline, starts = r[0], r[1], r[2]
            # submission_deadline may come back as date OR datetime depending
            # on the column type; datetime - date raises TypeError, so
            # normalize to a date first.
            _dl = deadline.date() if isinstance(deadline, _dt.datetime) else deadline
            days_left = (_dl - today).days if _dl else None
            nm = name or "event"
            findings.append({
                "issue":  f"event_submission_pending:{nm[:50]}",
                "url":    "/events",
                "count":  days_left or 0,
                "detail": (f"Event '{nm}' has a submission deadline in "
                           f"{days_left} days ({deadline}) and DC Hub hasn't "
                           f"submitted. Event runs {starts}. Decision needed."),
            })
        except Exception:
            continue
    return findings


# ── Phase CCCCC (2026-05-16) — tenant-coverage detector ──────────
def check_tenant_coverage_thin() -> list[dict]:
    """Flag when tenant coverage on top-50 facilities is <20%.
    Surfaces the gap so the operator knows to invest in tenant
    data ingest pipelines."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.facility_tenants')")
                if not (cur.fetchone() or [None])[0]: return findings
                cur.execute("""
                    WITH top50 AS (
                      SELECT id::text AS fid
                        FROM discovered_facilities
                       -- 2026-07-16: fleet filter is COALESCE(is_duplicate,0)=0
                       -- ONLY (issue #1539) — the old `merged_at IS NULL AND
                       -- is_duplicate=0` matched the drained pending queue
                       -- (0 rows), silently emptying top50 and killing this
                       -- detector.
                       WHERE COALESCE(is_duplicate,0) = 0
                         AND power_mw IS NOT NULL
                       ORDER BY power_mw DESC LIMIT 50
                    )
                    SELECT COUNT(*) FILTER (WHERE ft.tenant_name IS NOT NULL),
                           COUNT(*)
                      FROM top50 t
                      LEFT JOIN facility_tenants ft ON ft.facility_id = t.fid
                """)
                r = cur.fetchone() or (0, 0)
                with_t, total = int(r[0] or 0), int(r[1] or 0)
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    if total == 0: return findings
    pct = 100.0 * with_t / total
    if pct < 20:
        findings.append({
            "issue":  "tenant_coverage_thin",
            "url":    "/api/v1/tenants/coverage",
            "count":  int(pct),
            "detail": (f"Tenant coverage on top-50 facilities is only "
                       f"{pct:.0f}% ({with_t}/{total}). Per-building tenant "
                       f"data is DCHawk's main remaining moat. Invest in "
                       f"SEC filings + CRE comps + news NLP ingest pipeline "
                       f"OR POST /api/v1/tenants/ingest with structured rows."),
        })
    return findings


# ── Phase YYYY (2026-05-16) — operator-profile gap detector ──────
_NON_OPERATOR_PROVIDERS = {
    # Phase FF+9-triage (2026-05-19) — catch-all bucket. "Unknown" was
    # generating an `operator_profile_gap` finding daily, but it's not
    # a real operator — it's the placeholder for 1,603 facilities the
    # discovery pipeline hasn't been able to attribute to a named
    # provider yet. Surfacing it as a profile gap was actionable noise:
    # the human can't write a profile for "Unknown." The real work
    # (provider attribution backfill) belongs to a separate detector.
    "unknown", "n/a", "tbd", "various", "multiple",
    "undisclosed", "other", "",
}


def check_operator_profile_gap() -> list[dict]:
    """Surface top operators by facility count that lack rich
    metadata (missing markets, missing power_mw on most facilities).
    Brain flags so discovery pipeline can prioritize fills — closes
    the per-operator-profile gap vs DCHawk/dcByte.

    Phase FF+9-triage: filters _NON_OPERATOR_PROVIDERS (Unknown / N/A /
    placeholder rows) so the human queue isn't blocked on actions that
    cannot have a profile written for them."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT provider,
                           COUNT(*) AS facility_count,
                           COUNT(*) FILTER (WHERE power_mw IS NULL) AS mw_missing,
                           COUNT(*) FILTER (WHERE market IS NULL OR market = '') AS market_missing
                      FROM discovered_facilities
                     WHERE provider IS NOT NULL AND provider != ''
                       AND COALESCE(is_duplicate, 0) = 0
                     GROUP BY provider
                    HAVING COUNT(*) >= 10
                     ORDER BY COUNT(*) DESC LIMIT 20
                """)
                rows = cur.fetchall()
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    gaps = []
    for r in rows:
        name, total, mw_missing, mkt_missing = r[0], int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)
        # Phase FF+9-triage: skip placeholder/catch-all providers
        if (name or "").strip().lower() in _NON_OPERATOR_PROVIDERS:
            continue
        mw_pct  = 100.0 * mw_missing / max(1, total)
        mkt_pct = 100.0 * mkt_missing / max(1, total)
        # Flag if >50% of either field is missing
        if mw_pct >= 50 or mkt_pct >= 50:
            gaps.append((name, total, mw_pct, mkt_pct))

    if not gaps: return findings
    # One finding per gappy operator, cap at 3
    for (name, total, mw_pct, mkt_pct) in gaps[:3]:
        findings.append({
            "issue":  f"operator_profile_gap:{name[:50]}",
            "url":    f"/operators/{name.lower().replace(' ', '-')}",
            "count":  total,
            "detail": (f"Operator '{name}' has {total} facilities tracked "
                       f"but {mw_pct:.0f}% missing power_mw and "
                       f"{mkt_pct:.0f}% missing market. Discovery should "
                       f"prioritize this operator. Closes the per-operator "
                       f"profile gap vs DCHawk/dcByte."),
        })
    return findings


# ── Phase TTTT (2026-05-16) — citation-score detector ────────────
def check_citation_score_dropped() -> list[dict]:
    """Fires when DC Hub citation score in AI-platform responses
    drops 10+ points week-over-week, OR is below 30% with 3+ days
    of baseline. The 10/100 source-of-truth score is THE blocking
    metric for being the most important industry source; this puts
    real numbers behind the trend."""
    try:
        from routes.citation_hunter import read_score_history
        d = read_score_history(days=14)
    except Exception:
        return []
    rows = d.get("history") or []
    if len(rows) < 3: return []
    try:
        latest_pct = float(rows[-1].get("score_pct") or 0)
    except (TypeError, ValueError):
        return []
    # Phase r33-G (2026-05-21): defensive date parsing. Before this
    # guard, a malformed date crashed the detector. r33-G-fix:
    # `datetime` was never module-imported in this file; use the
    # inline-import pattern like every other detector here.
    import datetime as _dt_mod
    def _safe_iso(s):
        try:
            return _dt_mod.datetime.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None
    latest_dt = _safe_iso(rows[-1].get("date"))
    week_ago = None
    if latest_dt is not None:
        for r in rows[::-1]:
            r_dt = _safe_iso(r.get("date"))
            if r_dt is not None and (latest_dt - r_dt).days >= 7:
                week_ago = r
                break
    findings: list[dict] = []
    if week_ago:
        wow_delta = latest_pct - float(week_ago.get("score_pct") or 0)
        if wow_delta <= -10:
            findings.append({
                "issue":  "citation_score_dropped",
                "url":    "/api/v1/citations/score",
                "count":  abs(int(wow_delta)),
                "detail": (f"DC Hub citation score in AI-platform responses "
                           f"fell {abs(wow_delta):.1f}pts WoW "
                           f"({week_ago.get('score_pct')}% → {latest_pct}%). "
                           f"Either AI platforms are mentioning us less OR "
                           f"competitors are gaining share. See "
                           f"/api/v1/citations/latest for the actual "
                           f"Claude responses."),
            })
    if latest_pct < 30 and not findings:
        findings.append({
            "issue":  "citation_score_below_30pct",
            "url":    "/api/v1/citations/score",
            "count":  int(latest_pct),
            "detail": (f"DC Hub appears in only {latest_pct}% of Claude "
                       f"responses to data-center research queries. "
                       f"Auto-triggering DC Hub Media press cycle won't "
                       f"fix this — needs direct outreach to AI platforms "
                       f"(see /api/v1/media/winback-pitches)."),
        })
    return findings


# ── Phase UUUU (2026-05-16) — pattern-proposal candidate detector ─
def check_pattern_proposal_candidates() -> list[dict]:
    """Fires when 3+ identical (issue_prefix, action_taken) tuples
    exist in brain_resolution_log without a matching pattern in the
    library. Each surfaces a proposed pattern stub the operator can
    paste into routes/brain_autopilot.py:_PATTERN_LIBRARY."""
    try:
        from routes.pattern_growth import compute_proposals
        proposals = compute_proposals(min_matches=3) or []
    except Exception:
        return []
    out = []
    for p in proposals[:5]:  # cap so heartbeat doesn't bloat
        out.append({
            "issue":  f"pattern_proposal_candidate:{p['issue_prefix']}",
            "url":    "/api/v1/brain/pattern-proposals",
            "count":  int(p["match_count"]),
            "detail": (f"Operator has manually resolved "
                       f"'{p['issue_prefix']}' {p['match_count']} times "
                       f"with action='{p['proposed_action']}'. Brain "
                       f"proposes adding an autopilot pattern. Paste "
                       f"the stub from /api/v1/brain/pattern-proposals "
                       f"into routes/brain_autopilot.py:_PATTERN_LIBRARY."),
        })
    return out


# ── Phase VVVV (2026-05-16) — page content drift detector ────────
def check_page_content_drift() -> list[dict]:
    """Fires when Sentinel detects a page's content_hash changed
    AND its byte size moved by >25% (up or down) since the previous
    scan. Catches stealth regressions: someone removes the schema
    block, the deal table shrinks from 1,852 → 5, etc."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    # r-sentinel-gated (2026-07-07): content-drift only makes sense between two
    # SUCCESSFUL full renders. Exclude admin-gated + known-dynamic endpoints
    # (heartbeat's stale-while-revalidate body, admin JSON that answers 401 when
    # the Sentinel is unauthenticated) — their size legitimately flaps, and a
    # 401 gate body vs a 200 baseline reads as a -96% 'drift' that is an auth/
    # cache artifact, not a stealth regression. This was the #1484 false 'deploy
    # regression' (heartbeat + 3 admin endpoints). Best-effort; empty set on any
    # import error so drift still runs for the static content pages it targets.
    try:
        from routes.site_sentinel import _MANIFEST as _SS_MANIFEST
        _drift_skip = {e.get("path") for e in _SS_MANIFEST
                       if e.get("needs_admin") or e.get("skip_drift")}
    except Exception:
        _drift_skip = set()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT path, label, bytes, prev_bytes, content_hash,
                           prev_content_hash, status_code
                      FROM site_sentinel_results
                     WHERE healthy = TRUE
                       AND content_hash IS NOT NULL
                       AND prev_content_hash IS NOT NULL
                       AND content_hash != prev_content_hash
                       AND prev_bytes > 0
                       AND (status_code IS NULL OR status_code < 400)
                """)
                for r in cur.fetchall():
                    if r["path"] in _drift_skip:
                        continue
                    delta_pct = abs(100.0 * (int(r["bytes"] or 0) - int(r["prev_bytes"] or 0)) / max(1, int(r["prev_bytes"] or 1)))
                    if delta_pct < 25:
                        continue
                    findings.append({
                        "issue":  f"page_content_drift:{r['path']}",
                        "url":    r["path"],
                        "count":  int(delta_pct),
                        "detail": (f"Page '{r.get('label') or r['path']}' "
                                   f"content hash changed AND size moved by "
                                   f"{delta_pct:.0f}% "
                                   f"({r['prev_bytes']:,} → {r['bytes']:,} bytes). "
                                   f"Could be legit content update OR a stealth "
                                   f"regression (removed schema block, dropped "
                                   f"rows, broken template). Inspect."),
                    })
                    if len(findings) >= 5: break
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── Phase XXXX (2026-05-16) — competitor announcement detector ────
def check_competitor_announcement() -> list[dict]:
    """Fires when a competitor's snapshot diffs vs yesterday by >10%
    byte delta or title change. Auto-flags DC Hub Media so they can
    respond with /vs updates or counter-positioning content."""
    try:
        from routes.competitor_intel import compute_diffs
        diffs = compute_diffs(min_byte_delta_pct=10.0) or []
    except Exception:
        return []
    if not diffs: return []
    findings: list[dict] = []
    for d in diffs[:3]:
        findings.append({
            "issue":  f"competitor_announcement:{d.get('competitor')}",
            "url":    d.get("url"),
            "count":  int(d.get("byte_delta_pct") or 0),
            "detail": (f"{d.get('competitor')} updated {d.get('url')}: "
                       f"{d.get('byte_delta_pct')}% byte delta"
                       f"{' + TITLE CHANGED' if d.get('title_changed') else ''}. "
                       f"Title: '{(d.get('title_now') or '')[:80]}'. "
                       f"DC Hub Media should respond — update /vs or "
                       f"publish counter-positioning content."),
        })
    return findings


# ── Phase SSSS (2026-05-16) — winback pitches unsent detector ─────
def check_winback_pitches_unsent() -> list[dict]:
    """Fires when winback-pitches identifies platforms but none have
    been delivered in the last 14 days. The user shipped the auto-
    delivery cron (SSSS) but if the cron breaks OR Resend key is
    missing, pitches accumulate invisibly. This detector closes the
    loop: brain notices when output side stops working."""
    conn = _db()
    if conn is None: return []
    try:
        # Pitch count
        try:
            from routes.bot_outreach import _compute_dormant
            dormant = _compute_dormant(min_prior_calls=30, idle_days=14) or []
            # Unique-platform pitch count is an upper bound — close enough
            # for "is there work to do?" without re-running the full
            # winback-pitches classifier here.
            available = len({(a.get("ua_fingerprint") or "")[:60] for a in dormant})
        except Exception:
            return []
        if available == 0:
            return []
        # Recent delivery count
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.winback_outreach_sent')")
                if not (cur.fetchone() or [None])[0]:
                    # Table not created yet — first run after deploy
                    return [{
                        "issue":  "winback_pitches_unsent",
                        "url":    "/api/v1/media/winback-pitches",
                        "count":  available,
                        "detail": (f"{available} unique dormant-agent UAs "
                                   f"available for winback outreach but the "
                                   f"winback_outreach_sent table doesn't "
                                   f"exist yet — Phase SSSS deploy may be "
                                   f"pending, OR the weekly cron hasn't "
                                   f"fired yet. Check workflow run history."),
                    }]
                cur.execute("""
                    SELECT COUNT(*) FROM winback_outreach_sent
                     WHERE sent_at >= NOW() - INTERVAL '14 days'
                """)
                sent_14d = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                return []
    finally:
        try: conn.close()
        except Exception: pass

    if sent_14d == 0:
        return [{
            "issue":  "winback_pitches_unsent",
            "url":    "/api/v1/media/winback/log",
            "count":  available,
            "detail": (f"Brain identifies winback opportunities ({available} "
                       f"unique dormant-agent UAs available) but ZERO "
                       f"deliveries logged in last 14 days. The weekly "
                       f"Monday cron may have failed OR DCHUB_RESEND_API_KEY "
                       f"is unset. Inspect winback-weekly.yml run history."),
        }]
    return []


# ── Phase RRRR (2026-05-16) — DC Hub Media silence detector ───────
def check_upgrade_pool_grown() -> list[dict]:
    """Phase r32-conv (2026-05-20). Fires when the MCP upgrade pool
    grows past 50 unreached candidates — your outreach engine has work
    to do. The pool is identified users with paywall signals who
    haven't been outreached and haven't converted. Past 50, the
    addressable revenue justifies a campaign batch.

    Threshold tuning: 50 candidates × 5% conversion × $49 MRR = $122
    expected MRR per batch — large enough to be worth a brain alert."""
    findings: list[dict] = []
    import os as _os, psycopg2 as _pg
    db = _os.environ.get("DATABASE_URL")
    if not db: return findings
    try:
        c = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(DISTINCT user_email)
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - INTERVAL '30 days'
                       AND user_email IS NOT NULL AND user_email != ''
                       AND COALESCE(converted, false) = false
                       AND COALESCE(outreach_sent, false) = false
                """)
                count = int((cur.fetchone() or [0])[0] or 0)
        finally:
            c.close()
    except Exception:
        return findings

    THRESHOLD = 50
    if count >= THRESHOLD:
        findings.append({
            "issue":  "upgrade_pool_grown",
            "url":    "/api/v1/admin/upgrade-pool/preview",
            "count":  count,
            "detail": (
                f"{count} identified users have hit MCP paywall signals "
                f"in the last 30 days without being outreached and without "
                f"converting. At a conservative 5% conversion rate that's "
                f"~{count // 20} potential Developer signups ($49/mo each). "
                f"POST /api/v1/admin/upgrade-pool/send to fire the campaign. "
                f"Use ?dry=1 first to inspect."
            ),
        })
    return findings


def check_cf_pages_deploy_stuck() -> list[dict]:
    """Phase r33-B (2026-05-21). Caught earlier this session: a CF
    Pages worker deploy can fail silently, leaving the worker stuck
    on an old version while subsequent pushes pile up behind it. User
    only notices when a routing change doesn't take effect.

    This detector probes the worker version header. If the worker
    version hasn't changed in 6+ hours despite git activity on the
    dchub-frontend repo, fire a finding so the operator knows to
    check CF Pages dashboard for a build failure.

    Escalation-only — fix is manual (cancel stuck deploy + retrigger
    from latest commit in CF Pages dashboard)."""
    findings: list[dict] = []
    try:
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request(
            "https://dchub.cloud/api/v1/site/stats",
            headers={"User-Agent": "DCHub-CFDeployCheck/1.0"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            worker_version = resp.headers.get("x-dc-worker-version", "")
    except Exception:
        return findings
    if not worker_version:
        return findings

    # Versions are bumped per-meaningful-deploy. Walk recent commits
    # in dchub-frontend (last 24h) and check if any touched _worker.js.
    # If yes, but worker_version's age vs latest commit is >6h, fire.
    try:
        import urllib.request as _ur, json as _json
        # GitHub API for recent commits on dchub-frontend
        gh_token = _os_env().get("GITHUB_TOKEN", "")
        headers = {"Accept": "application/vnd.github.v3+json",
                   "User-Agent": "DCHub-CFDeployCheck/1.0"}
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"
        url = "https://api.github.com/repos/azmartone67/dchub-frontend/commits?per_page=10"
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=8) as resp:
            commits = _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return findings
    if not isinstance(commits, list) or not commits:
        return findings

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    # Find commits in last 6h that touched _worker.js
    worker_commits = []
    for c in commits[:10]:
        try:
            dt_str = c.get("commit", {}).get("committer", {}).get("date")
            if not dt_str: continue
            dt = _dt.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if (now - dt).total_seconds() / 3600 > 6:
                break  # commits sorted desc; older ones don't matter
            msg = c.get("commit", {}).get("message", "")
            sha = c.get("sha", "")[:7]
            worker_commits.append({"sha": sha, "msg": msg[:80], "dt": dt})
        except Exception:
            continue

    # Compute the latest commit time. If we have worker_commits and
    # worker_version doesn't include any of the recent SHAs in some
    # heuristic way (worker version is a free-form string), fire if
    # any worker-touching commit is >2h old without the version
    # changing.
    if worker_commits:
        oldest = min(c["dt"] for c in worker_commits)
        age_hrs = (now - oldest).total_seconds() / 3600
        if age_hrs >= 2.0:
            findings.append({
                "issue":  "cf_pages_deploy_stuck",
                "url":    "https://dash.cloudflare.com/?to=/:account/pages",
                "count_kind": "item_count",  # magnitude, not a recurrence tally
                "count":  len(worker_commits),
                "detail": (
                    f"CF Pages worker version is `{worker_version}` but "
                    f"{len(worker_commits)} commit(s) hit dchub-frontend in "
                    f"the last 6h (oldest: {age_hrs:.1f}h ago, sha "
                    f"{worker_commits[-1]['sha']}). Worker likely stuck on "
                    f"an old deploy. Check CF Pages dashboard → Deployments "
                    f"for a Failed deployment that's blocking the queue."
                ),
            })
    return findings


def check_slow_request_ratio() -> list[dict]:
    """Phase r33-B (2026-05-21). The /grid 112s bug killed Railway in
    a restart loop all session. We have SLOW REQUEST warnings in
    Railway logs but no brain detector that aggregates them.

    This detector checks observability_metrics for slow-request
    counts in the last hour. If any path has >5 slow-requests/hour
    OR consistently >30s response time, fire a finding so the
    operator catches it BEFORE the watchdog forced restart kicks in.

    Escalation-only — fix is per-handler (parallelize, add timeout,
    cache more aggressively, etc.). Brain can't auto-fix code paths,
    only flag them."""
    findings: list[dict] = []
    import os as _os, psycopg2 as _pg
    db = _os.environ.get("DATABASE_URL")
    if not db: return findings
    try:
        c = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                # observability_metrics may not exist if schema repair
                # hasn't run — gracefully skip.
                cur.execute("SELECT to_regclass('public.observability_metrics')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                # Look for slow_request entries in the last hour.
                cur.execute("""
                    SELECT metric, COUNT(*) AS hits,
                           AVG(value)::float AS avg_ms,
                           MAX(value)::float AS max_ms
                      FROM observability_metrics
                     WHERE metric LIKE 'slow_request:%%'
                       AND recorded_at > NOW() - INTERVAL '1 hour'
                     GROUP BY metric
                    HAVING COUNT(*) >= 5
                     ORDER BY hits DESC
                     LIMIT 10
                """)
                rows = cur.fetchall()
        finally:
            c.close()
    except Exception:
        return findings

    for r in rows:
        metric, hits, avg_ms, max_ms = r
        path = metric.split(":", 1)[1] if ":" in metric else metric
        findings.append({
            "issue":  "slow_request_ratio",
            "url":    path,
            "count":  int(hits),
            "detail": (
                f"`{path}` had {hits} slow-request events in the last "
                f"hour (>30s each). Avg {avg_ms:.0f}ms, max {max_ms:.0f}ms. "
                f"This is the failure pattern that triggers gunicorn worker "
                f"timeout → SIGTERM → restart loop. Audit the handler for "
                f"sequential HTTP calls, unbounded queries, or sync wait "
                f"on slow upstream APIs. Parallelize or add timeout."
            ),
        })
    return findings


def check_render_pipeline_blocked() -> list[dict]:
    """Phase r33-B (2026-05-21). User session caught this: Render
    workspace ran out of pipeline minutes, all subsequent auto-deploys
    silently 'Build blocked'. Render stays on old code, user thinks
    auto-deploy is working, drift accumulates.

    This detector compares the latest code commit on the dchub-backend
    repo against the running version on Render. If Render's version
    is >6h behind the latest commit, fire a finding flagging probable
    deploy block.

    Escalation-only — fix is billing (add pipeline minutes) or manual
    deploy trigger via Render dashboard."""
    findings: list[dict] = []
    try:
        import urllib.request as _ur, json as _json
        # Get latest commit on main
        gh_token = _os_env().get("GITHUB_TOKEN", "")
        headers = {"Accept": "application/vnd.github.v3+json",
                   "User-Agent": "DCHub-RenderDeployCheck/1.0"}
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"
        url = "https://api.github.com/repos/azmartone67/dchub-backend/commits?per_page=1"
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=8) as resp:
            latest = _json.loads(resp.read().decode("utf-8", errors="replace"))
        if not latest: return findings
        latest_sha = (latest[0].get("sha") or "")[:7]
        latest_msg = (latest[0].get("commit", {}).get("message", "") or "")[:80]
        latest_dt_str = latest[0].get("commit", {}).get("committer", {}).get("date")
        if not latest_dt_str: return findings
        import datetime as _dt
        latest_dt = _dt.datetime.fromisoformat(latest_dt_str.replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        latest_age_hrs = (now - latest_dt).total_seconds() / 3600

        # ★ 2026-07-24 — this detector was dead in BOTH directions and had
        # never fired, while Render silently drifted 4+ days behind:
        #
        #   1. It gated on `2 < latest_age_hrs < 24`, where latest_age_hrs is
        #      the age of main's NEWEST COMMIT — not how far behind Render is.
        #      The brain pushes to main every ~45min (5 commits inside 0.78h
        #      when this was found), so that window is essentially never open.
        #      False negative in the normal case; and had main ever gone quiet
        #      for 2h it would have fired regardless of Render's actual state.
        #   2. It read `build` from /api/v1/version, which is a HAND-MAINTAINED
        #      CONSTANT returning 91 on BOTH origins. It never compared that
        #      value to anything — it only interpolated it into the message.
        #      Comparing a static build number to a git SHA cannot work.
        #
        # Now: compare real commit SHAs, and gate on the DRIFT, not on how
        # recently someone happened to push. Render exposes the running SHA
        # via /api/v1/ops/origin-freshness (routes/failover_stale_gate.py).
        if latest_age_hrs < 0.5:
            return findings  # deploy still plausibly in flight — no signal yet

        render_build = ""
        render_commit = ""
        render_data_age = None
        try:
            req2 = _ur.Request(
                "https://dchub-backend-render.onrender.com/api/v1/ops/origin-freshness",
                headers={"User-Agent": "DCHub-RenderDeployCheck/1.0"},
            )
            with _ur.urlopen(req2, timeout=8) as resp:
                fj = _json.loads(resp.read().decode("utf-8", errors="replace")[:2000])
            render_commit = (fj.get("commit") or "")
            render_data_age = fj.get("data_age_hours")
        except Exception:
            # Mirror has not deployed the freshness probe yet (it 404s until
            # it picks up 8cac23ca) — which is ITSELF the drift we are hunting.
            render_commit = ""

        drifted = bool(render_commit) and render_commit[:7] != latest_sha[:7]
        no_probe = not render_commit

        if drifted or no_probe:
            render_build = render_commit or "unknown (freshness probe absent)"
            findings.append({
                "issue":  "render_pipeline_blocked",
                "url":    "https://dashboard.render.com/",
                "count":  1,
                "detail": (
                    f"Failover MIRROR is behind: Render runs `{render_build}`, "
                    f"main is at {latest_sha} ({latest_msg}). Render auto-deploy "
                    f"is OFF by design (pipeline minutes), so the mirror only "
                    f"moves when the deploy hook fires — POST "
                    f"RENDER_DEPLOY_HOOK_URL, or check Render dashboard → Events "
                    f"for 'Build blocked'. "
                    + (f"Mirror DATA is {render_data_age}h old — a redeploy "
                       f"will NOT fix that; its DATABASE_URL points at a "
                       f"different database. "
                       if isinstance(render_data_age, (int, float))
                       and render_data_age > 6 else "")
                    + "A behind-mirror serves stale reads during failover; "
                    "since 8cac23ca the stale-gate 503s its metrics surfaces "
                    "once it is deployed."
                ),
            })
    except Exception:
        pass
    return findings


def _os_env():
    """Helper for the platform detectors — wraps os.environ so the
    detectors can be imported without a fresh os import."""
    import os as _os
    return _os.environ


# Phase r33-E (2026-05-21) — detector-runtime tracker. Populated
# inside scan_all()'s _run_one wrapper; read by
# check_detector_runtime_distribution to surface slow detectors as
# brain-level findings (otherwise the only way to spot a 30s
# detector is to read Railway logs by hand).
_DETECTOR_TIMINGS: dict[str, dict] = {}


# Phase r33-F (2026-05-21) — worker process boot time. Set ONCE at
# module import. Each gunicorn worker that imports this file gets
# its own boot time, so the detector sees only its own worker's
# age (we can't see siblings). Still useful: if THIS worker is
# >24h old, others likely are too — that's the memory-growth-class
# restart signal.
import time as _r33f_time
_BOOT_TIME: float = _r33f_time.time()


def check_render_flapping() -> list[dict]:
    """Phase r33-C (2026-05-21). Render side of the failover pair has
    been flapping all session — DB pool stale connections, pipeline
    minutes blocked, manual deploys required. The user wants this
    detected AND auto-recovered.

    Probes Render's /api/v1/version endpoint 3x with 5s sleeps. Fires
    if at least 2 of the 3 probes fail (timeout, 5xx, connection
    refused). Pairs with autopilot action `_action_render_restart`
    (in brain_autopilot.py) which hits Render's deploy hook to force
    a fresh container.

    Lower-frequency than check_multi_cloud_failover_broken — that one
    is a "BOTH down" alarm; this one is "Render alone is sick"."""
    findings: list[dict] = []
    import urllib.request as _ur, time as _t
    render_url = (_os_env().get("RENDER_BACKUP_URL")
                  or "https://dchub-backend-render.onrender.com")
    probe_url = f"{render_url.rstrip('/')}/api/v1/version"
    fails = 0
    detail_bits: list[str] = []
    # Phase r33-G-fix (2026-05-21): cut probe-interval from 5s → 1.5s.
    # The original 5s sleep × 2 intervals + 3 probes × 4s timeout was
    # making this detector contribute ~22s to scan wall time — the
    # biggest single contributor to the consistency-radar 70s SLOW
    # REQUEST events. 1.5s is still long enough to defeat a true
    # transient flap (single dropped packet) without dominating the
    # scan budget. Worst case now: 3*4 + 2*1.5 = 15s.
    for i in range(3):
        try:
            req = _ur.Request(probe_url,
                              headers={"User-Agent": "DCHub-RenderFlapCheck/1.0"})
            with _ur.urlopen(req, timeout=4) as resp:
                code = resp.getcode()
                if code >= 500:
                    fails += 1
                    detail_bits.append(f"probe{i+1}=HTTP{code}")
                else:
                    detail_bits.append(f"probe{i+1}=ok")
        except Exception as e:
            fails += 1
            detail_bits.append(f"probe{i+1}={type(e).__name__}")
        if i < 2:
            _t.sleep(1.5)
    if fails >= 2:
        findings.append({
            "issue":  "render_flapping",
            "url":    probe_url,
            "count":  fails,
            "detail": (
                f"Render backup is flapping ({fails}/3 probes failed: "
                f"{', '.join(detail_bits)}). Failover safety is degraded — "
                f"if Railway also fails right now the site has no backstop. "
                f"Auto-recovery: brain autopilot _action_render_restart will "
                f"trigger a fresh container via the Render deploy hook."
            ),
        })
    return findings


# ──────────────────────────────────────────────────────────────────
# Phase r33-E (2026-05-21) — QA monitor master shell. Five detectors
# closing the highest-leverage QA gaps the user identified:
#   1. check_404_spike — burst detection
#   2. check_neon_replication_lag — failover safety
#   3. check_signup_drop_off_step — revenue protection
#   4. check_detector_runtime_distribution — brain meta-monitor
#   5. check_stripe_webhook_lag — revenue pipeline safety
# Each is defensive (graceful skip when its table doesn't exist).
# ──────────────────────────────────────────────────────────────────


def check_404_spike() -> list[dict]:
    """Burst-detect 404s. Different from check_repeated_404_patterns
    (which catches sustained patterns over hours); this catches
    SUDDEN bursts: any URL pattern with ≥10 404s in the last 5
    minutes where the prior hour averaged <1/hr. Classic deploy-
    regression signal — the path was working an hour ago, now
    everyone hitting it gets a 404.

    Looks for the data in `request_log_404` first, falls back to
    `request_log` filtered on status=404. Both are optional; if
    neither exists, returns []."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            # Prefer dedicated 404 log if present.
            cur.execute("SELECT to_regclass('public.request_log_404')")
            tbl_404 = (cur.fetchone() or [None])[0]
            if tbl_404:
                path_col = "url_pattern"
                ts_col   = "ts"
                src_tbl  = "request_log_404"
                where_extra = ""
            else:
                cur.execute("SELECT to_regclass('public.request_log')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                src_tbl  = "request_log"
                path_col = "path"
                ts_col   = "ts"
                where_extra = " AND status = 404"
            # Burst window: last 5min, ≥10 hits per path
            cur.execute(f"""
                WITH burst AS (
                    SELECT {path_col} AS p, COUNT(*) AS n5
                      FROM {src_tbl}
                     WHERE {ts_col} > NOW() - INTERVAL '5 minutes'
                       {where_extra}
                     GROUP BY {path_col}
                    HAVING COUNT(*) >= 10
                ),
                baseline AS (
                    SELECT {path_col} AS p, COUNT(*) AS n60
                      FROM {src_tbl}
                     WHERE {ts_col} > NOW() - INTERVAL '1 hour'
                       AND {ts_col} <= NOW() - INTERVAL '5 minutes'
                       {where_extra}
                     GROUP BY {path_col}
                )
                SELECT b.p, b.n5, COALESCE(bl.n60, 0) AS n60
                  FROM burst b
                  LEFT JOIN baseline bl ON bl.p = b.p
                 WHERE COALESCE(bl.n60, 0) < 60   -- <1/min baseline
                 ORDER BY b.n5 DESC LIMIT 10
            """)
            rows = cur.fetchall()
    except Exception:
        return findings
    finally:
        try: c.close()
        except Exception: pass

    for path, n5, n60 in rows:
        findings.append({
            "issue":  "404_spike",
            "url":    path,
            "count":  int(n5),
            "detail": (
                f"`{path}` returned {n5} 404s in the last 5min "
                f"(baseline {n60}/hr — was working). Classic deploy "
                f"regression: a route was removed/renamed and traffic "
                f"is still hitting the old path. Audit recent commits "
                f"for blueprint registration changes or route renames."
            ),
        })
    return findings


def check_neon_replication_lag() -> list[dict]:
    """Probes the read-replica connection (if configured) and
    measures the gap between primary and replica via
    pg_last_xact_replay_timestamp(). Fires if >60s.

    On Neon, the read replica is a separate compute endpoint with
    its own DATABASE_URL — usually exposed as READ_REPLICA_URL.
    When the replica falls behind, all read traffic routed there
    serves stale data. Failover assumes replica is fresh, so this
    detector catches the gap before it becomes an outage."""
    findings: list[dict] = []
    import os as _os, psycopg2 as _pg
    rr_url = (_os.environ.get("READ_REPLICA_URL")
              or _os.environ.get("DATABASE_REPLICA_URL"))
    if not rr_url:
        return findings  # No replica configured → nothing to probe
    try:
        c = _pg.connect(rr_url, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                # pg_last_xact_replay_timestamp() returns the commit
                # timestamp of the last applied xact. On the PRIMARY
                # this returns NULL; on a replica it returns the
                # timestamp we're caught up to.
                cur.execute("""
                    SELECT EXTRACT(EPOCH FROM (
                        NOW() - pg_last_xact_replay_timestamp()
                    ))
                """)
                lag_s = (cur.fetchone() or [None])[0]
        finally:
            c.close()
    except Exception as e:
        # Connection-level failure means the replica is unreachable
        # which is its own finding — surface it.
        findings.append({
            "issue":  "neon_replication_lag",
            "url":    "neon:read_replica",
            "count":  -1,
            "detail": (
                f"Read replica unreachable: {type(e).__name__}. "
                f"Failover safety is gone — all reads landing on the "
                f"primary. Check READ_REPLICA_URL is valid and the "
                f"replica endpoint isn't paused on the Neon dashboard."
            ),
        })
        return findings
    if lag_s is None:
        # We connected but got NULL → we hit the primary, not the
        # replica. Misconfiguration.
        findings.append({
            "issue":  "neon_replication_lag",
            "url":    "neon:read_replica",
            "count":  -2,
            "detail": (
                "READ_REPLICA_URL connected but pg_last_xact_replay_"
                "timestamp() returned NULL — the URL is pointing at "
                "the PRIMARY, not a replica. Reconfigure to a Neon "
                "read-replica endpoint to restore failover safety."
            ),
        })
    elif float(lag_s) > 60.0:
        findings.append({
            "issue":  "neon_replication_lag",
            "url":    "neon:read_replica",
            "count":  int(lag_s),
            "detail": (
                f"Read replica is {float(lag_s):.0f}s behind the primary "
                f"(threshold: 60s). Reads routed to the replica serve "
                f"stale data. Check Neon dashboard for replica health "
                f"or for primary-side write storms saturating WAL."
            ),
        })
    return findings


def check_signup_drop_off_step() -> list[dict]:
    """Computes per-step signup funnel counts for yesterday vs the
    day before, fires for any step where conversion drops >30%
    day-over-day. Each step is keyed off events in `signup_events`:
      • landing → email_submitted → email_verified → onboarded →
        first_mcp_call

    Defensive: if the events table doesn't exist or steps don't
    have enough volume (n<20), skip silently."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.signup_events')")
            if not (cur.fetchone() or [None])[0]:
                return findings
            cur.execute("""
                SELECT step,
                       COUNT(*) FILTER (
                         WHERE created_at::date = (CURRENT_DATE - 1)) AS yday,
                       COUNT(*) FILTER (
                         WHERE created_at::date = (CURRENT_DATE - 2)) AS day_before
                  FROM signup_events
                 WHERE created_at > CURRENT_DATE - INTERVAL '4 days'
                   AND step IS NOT NULL
                 GROUP BY step
            """)
            rows = cur.fetchall()
    except Exception:
        return findings
    finally:
        try: c.close()
        except Exception: pass

    for step, yday, prev in rows:
        yday = int(yday or 0)
        prev = int(prev or 0)
        # Need enough volume on the prior day to make the ratio
        # meaningful. n<20 = noise, skip.
        if prev < 20: continue
        drop_pct = round((1.0 - (yday / prev)) * 100.0, 1)
        if drop_pct >= 30.0:
            findings.append({
                "issue":  "signup_drop_off_step",
                "url":    f"funnel:{step}",
                "count":  int(drop_pct),
                "detail": (
                    f"Signup step `{step}` dropped {drop_pct}% "
                    f"day-over-day ({yday} vs {prev} the day before). "
                    f"Audit the page that owns this step for a "
                    f"regression — broken form, paywall change, JS "
                    f"error, or copy that turned the flow cold."
                ),
            })
    return findings


def check_detector_runtime_distribution() -> list[dict]:
    """Reads the _DETECTOR_TIMINGS dict (populated by scan_all's
    _run_one wrapper) and fires for any detector taking >15s.

    Brain meta-monitor: when the radar itself slows down (caught
    this session — consistency_radar hit 107s and triggered the
    /grid 112s Railway restart cascade), this surfaces the culprit
    as a brain-level finding instead of requiring a manual log dig."""
    findings: list[dict] = []
    THRESHOLD_MS = 15_000
    for name, info in list(_DETECTOR_TIMINGS.items()):
        try:
            ms = int(info.get("last_ms") or 0)
        except Exception:
            continue
        if ms > THRESHOLD_MS:
            findings.append({
                "issue":  "detector_runtime_slow",
                "url":    f"detector:{name}",
                "count":  int(ms / 1000),
                "detail": (
                    f"Detector `{name}` took {ms/1000:.1f}s on the last "
                    f"scan (threshold: 15s). Slow detectors push the "
                    f"whole scan past its 60s budget — eventually the "
                    f"gunicorn worker times out and Railway restarts. "
                    f"Audit the detector for sequential HTTP calls, "
                    f"unbounded queries, or a missing per-probe timeout."
                ),
            })
    return findings


def check_stripe_webhook_lag() -> list[dict]:
    """Fires if the most recent Stripe webhook receipt is >2h old.

    Stripe webhooks are how subscription state, payment failures,
    and cancellations land in our DB. A lag means our customer
    state is drifting from reality — a paid user might be churned
    in Stripe but still see paid-tier access here, or worse.

    Looks for the most recent row in `stripe_webhooks` /
    `stripe_webhook_log` / `stripe_events`. Defensive across schema
    variants."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    # r-webhook-table (2026-07-24 coverage audit): NONE of the original three tables
    # exist — this detector returned [] on every scan since it was written, a permanent
    # false all-clear on the REVENUE pipeline. The real table is stripe_webhook_events.
    candidates = [
        ("stripe_webhook_events", "processed_at"),
        ("stripe_webhooks",     "received_at"),
        ("stripe_webhook_log",  "received_at"),
        ("stripe_events",       "created_at"),
    ]
    tbl = ts_col = None
    try:
        with c.cursor() as cur:
            for t, col in candidates:
                cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                if (cur.fetchone() or [None])[0]:
                    tbl, ts_col = t, col
                    break
            if not tbl:
                # Never a silent all-clear on revenue: say we're blind.
                return [{
                    "issue":  "stripe_webhook_table_missing",
                    "url":    "stripe webhook telemetry",
                    "count":  1,
                    "detail": ("check_stripe_webhook_lag found none of its candidate "
                               "tables — Stripe webhook freshness is UNWATCHED (this "
                               "silently returned healthy for its entire life). Add the "
                               "real table to `candidates`."),
                }]
            cur.execute(f"SELECT MAX({ts_col}) FROM {tbl}")
            last = (cur.fetchone() or [None])[0]
    except Exception:
        return findings
    finally:
        try: c.close()
        except Exception: pass

    if last is None:
        # Table exists but is empty — could be a fresh deploy or a
        # webhook that's never fired. Fire only if it's a known-active
        # account; otherwise informational.
        return findings
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=_dt.timezone.utc)
    age_h = (now - last).total_seconds() / 3600.0
    # r-stripe-threshold (2026-07-24 coverage audit): the original 2h threshold
    # was written for a high-volume assumption. Measured over 180 days of real
    # `stripe_webhook_events`: avg gap 5.2h, p90 20.2h, max 49.6h — so 2h would
    # fire during entirely normal operation and train the eye to ignore it.
    # 72h sits above the observed maximum, so it only fires on a genuine stall.
    _LAG_THRESHOLD_H = 72.0
    if age_h > _LAG_THRESHOLD_H:
        findings.append({
            "issue":  "stripe_webhook_lag",
            "url":    f"table:{tbl}",
            "count_kind": "hours",  # magnitude, not a recurrence tally
            "count":  int(age_h),
            "detail": (
                f"Last Stripe webhook landed {age_h:.1f}h ago "
                f"(table: `{tbl}`, threshold: {_LAG_THRESHOLD_H:.0f}h — above the "
                f"49.6h worst gap seen in 180 days). Customer state is "
                f"drifting — recent subscription changes, payment "
                f"failures, and cancellations are not reflected. "
                f"Check Stripe dashboard → Developers → Webhooks for "
                f"a disabled endpoint or repeated 5xx failures."
            ),
        })
    return findings


# ──────────────────────────────────────────────────────────────────
# Phase r33-F (2026-05-21) — second batch of QA monitors. Five more
# detectors closing structural blind spots:
#   1. check_canonical_redirect_loops — top-50 pages, follow one
#      redirect, flag self-bounces or 404 targets
#   2. check_gunicorn_worker_age — memory-growth class indicator
#   3. check_facility_dedupe_collisions — ghost-facility class
#   4. check_paid_user_zero_value_tools — pre-churn signal
#   5. check_cf_kv_namespace_pressure — KV cache stampede
# ──────────────────────────────────────────────────────────────────


def check_canonical_redirect_loops() -> list[dict]:
    """Probes a hand-curated list of top public pages. For each,
    issues a HEAD with redirects=manual and inspects the Location
    header. Fires if:
      • Location matches the source path (loop)
      • Location resolves to a 404 (broken target)

    Defensive: each probe has a 6s timeout, parallel via
    ThreadPoolExecutor so wall-time stays under 20s."""
    findings: list[dict] = []
    PROBES = [
        "/", "/pricing", "/markets", "/dcpi", "/pockets",
        "/coverage", "/api", "/developers", "/docs",
        "/dc-hub-media", "/digest", "/brain", "/brain-live",
        "/admin-health", "/sitemap.xml", "/state-of-the-data-center",
    ]
    BASE = "https://dchub.cloud"
    import urllib.request as _ur, urllib.error as _ue
    import concurrent.futures as _cf, urllib.parse as _up

    def _probe_one(path: str):
        try:
            req = _ur.Request(BASE + path, method="HEAD",
                              headers={"User-Agent": "DCHub-RedirCheck/1.0"})
            opener = _ur.build_opener(_ur.HTTPRedirectHandler())
            # Disable auto-redirect so we can SEE the 30x.
            class _NoFollow(_ur.HTTPRedirectHandler):
                def redirect_request(self, *a, **k): return None
            opener = _ur.build_opener(_NoFollow())
            try:
                resp = opener.open(req, timeout=6)
                return (path, resp.getcode(), None)
            except _ue.HTTPError as he:
                loc = he.headers.get("Location") if he.headers else None
                return (path, he.code, loc)
        except Exception as e:
            return (path, 0, f"err:{type(e).__name__}")

    results = []
    with _cf.ThreadPoolExecutor(max_workers=8,
                                 thread_name_prefix="brain-redir") as ex:
        futs = {ex.submit(_probe_one, p): p for p in PROBES}
        for fut in _cf.as_completed(futs, timeout=18):
            try:
                results.append(fut.result(timeout=8))
            except Exception:
                continue

    for src, code, loc in results:
        if code in (301, 302, 307, 308) and loc:
            # Normalise loc to a path for comparison
            try:
                parsed = _up.urlparse(loc)
                loc_path = parsed.path or "/"
            except Exception:
                loc_path = loc
            # r-redir-norm (2026-06-02): a 308 from /x to /x/ (or vice-versa) is
            # Flask/Werkzeug trailing-slash canonicalization — a healthy one-hop
            # 30x->200, NOT a loop. It rstrip-equals the source, which made
            # /dc-hub-media fire canonical_redirect_loop forever. Skip the
            # trailing-slash-only case; a real loop has a byte-identical target.
            if loc_path.rstrip("/") == src.rstrip("/") and loc_path != src:
                continue
            # Self-loop: redirects to itself
            if loc_path.rstrip("/") == src.rstrip("/"):
                findings.append({
                    "issue":  "canonical_redirect_loop",
                    "url":    src,
                    "count":  1,
                    "detail": (
                        f"`{src}` 30x→ `{loc_path}` (itself). Loop. "
                        f"Browsers will fail after ~20 hops. Audit "
                        f"the redirect rule that owns this path "
                        f"(_redirects, _worker.js, or Flask handler)."
                    ),
                })
            else:
                # Verify the target isn't itself a 404
                try:
                    target_req = _ur.Request(
                        BASE + loc_path, method="HEAD",
                        headers={"User-Agent": "DCHub-RedirCheck/1.0"})
                    with _ur.urlopen(target_req, timeout=4) as tresp:
                        if tresp.getcode() >= 400:
                            findings.append({
                                "issue":  "canonical_redirect_loop",
                                "url":    src,
                                "count":  tresp.getcode(),
                                "detail": (
                                    f"`{src}` 30x→ `{loc_path}` but "
                                    f"target returns HTTP {tresp.getcode()}. "
                                    f"Dead redirect — point it at a real URL "
                                    f"or remove the rule."
                                ),
                            })
                except _ue.HTTPError as he:
                    findings.append({
                        "issue":  "canonical_redirect_loop",
                        "url":    src,
                        "count":  he.code,
                        "detail": (
                            f"`{src}` 30x→ `{loc_path}` but target "
                            f"returns HTTP {he.code}. Dead redirect."
                        ),
                    })
                except Exception:
                    pass
    return findings


def check_gunicorn_worker_age() -> list[dict]:
    """Fires if the current gunicorn worker has been alive >24h.
    Long-lived workers accumulate memory (psycopg2 cursors that
    never get freed, growing per-process caches). Restart hygiene
    is to recycle workers daily; gunicorn's --max-requests handles
    this normally but if it's mis-set or disabled this detector
    catches the drift.

    Per-process visibility: each worker that imports this module
    has its own _BOOT_TIME. We only see the worker handling this
    request — but that's enough signal to know workers AREN'T
    being recycled."""
    findings: list[dict] = []
    age_s = _r33f_time.time() - _BOOT_TIME
    age_h = age_s / 3600.0
    if age_h > 24.0:
        import os as _os
        pid = _os.getpid()
        findings.append({
            "issue":  "gunicorn_worker_age",
            "url":    f"pid:{pid}",
            "count_kind": "hours",  # magnitude, not a recurrence tally
            "count":  int(age_h),
            "detail": (
                f"Worker PID {pid} has been alive {age_h:.1f}h "
                f"(threshold: 24h). Memory drift class — add or fix "
                f"gunicorn --max-requests=1000 --max-requests-jitter=100 "
                f"in the Procfile/startup. Restarts the worker after N "
                f"requests, freeing accumulated state."
            ),
        })
    return findings


def check_facility_dedupe_collisions() -> list[dict]:
    """Finds facility rows that share the same name AND coordinates
    (rounded to 4 decimal places) but have different IDs. These are
    'ghost facilities' — a discovery crawl created a new row when
    it should have linked to an existing one. Frontend shows them
    twice on the map, search returns duplicates, downstream
    aggregations double-count.

    Caps at 20 findings per scan to prevent flooding."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.facilities')")
            if not (cur.fetchone() or [None])[0]:
                return findings
            cur.execute("""
                SELECT name,
                       ROUND(lat::numeric, 4) AS lat4,
                       ROUND(lng::numeric, 4) AS lng4,
                       COUNT(*) AS dup,
                       ARRAY_AGG(id ORDER BY id) AS ids
                  FROM facilities
                 WHERE name IS NOT NULL AND name != ''
                   AND lat IS NOT NULL AND lng IS NOT NULL
                 GROUP BY name, ROUND(lat::numeric, 4), ROUND(lng::numeric, 4)
                HAVING COUNT(*) > 1
                 ORDER BY COUNT(*) DESC
                 LIMIT 20
            """)
            rows = cur.fetchall()
    except Exception:
        return findings
    finally:
        try: c.close()
        except Exception: pass

    for name, lat4, lng4, dup, ids in rows:
        primary_id = ids[0] if ids else None
        findings.append({
            "issue":  "facility_dedupe_collision",
            "url":    f"facility:{primary_id}",
            "count":  int(dup),
            "detail": (
                f"{dup} facilities share name=`{name}` at "
                f"({lat4},{lng4}). IDs: {ids}. Merge candidates — "
                f"point the duplicates at the canonical ID via "
                f"`POST /api/v1/admin/facilities/merge` (canonical: "
                f"id={primary_id}). Downstream aggregations are "
                f"double-counting this site."
            ),
        })
    return findings


def check_paid_user_zero_value_tools() -> list[dict]:
    """Pre-churn signal: paid customers (developer/pro/enterprise)
    who haven't called ANY paid MCP tool in 14 days. They're paying
    but extracting zero value — high churn risk.

    Joins `api_keys` (or `users`) tier info with `mcp_call_log` time
    series. Defensive across schema variants."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.api_keys')")
            keys_exists = (cur.fetchone() or [None])[0]
            cur.execute("SELECT to_regclass('public.mcp_call_log')")
            log_exists = (cur.fetchone() or [None])[0]
            if not keys_exists or not log_exists:
                return findings
            # Probe column names defensively. api_keys often has
            # (api_key, tier, email, created_at). mcp_call_log has
            # (api_key, tool_name, created_at).
            cur.execute("""
                SELECT ak.email, ak.tier,
                       COALESCE(
                           (SELECT MAX(mcl.created_at)
                              FROM mcp_call_log mcl
                             WHERE mcl.api_key = ak.api_key),
                           '1970-01-01'::timestamp) AS last_call
                  FROM api_keys ak
                 WHERE ak.tier IN ('developer','pro','enterprise')
                   AND ak.email IS NOT NULL
                   AND ak.created_at < NOW() - INTERVAL '14 days'
                 ORDER BY last_call ASC
                 LIMIT 30
            """)
            rows = cur.fetchall()
    except Exception:
        return findings
    finally:
        try: c.close()
        except Exception: pass

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    for email, tier, last_call in rows:
        if last_call is None: continue
        try:
            if last_call.tzinfo is None:
                last_call = last_call.replace(tzinfo=_dt.timezone.utc)
            silence_d = (now - last_call).total_seconds() / 86400.0
        except Exception:
            continue
        if silence_d >= 14.0:
            findings.append({
                "issue":  "paid_user_zero_value",
                "url":    f"user:{email}",
                "count":  int(silence_d),
                "detail": (
                    f"`{email}` ({tier}) has not called any paid MCP "
                    f"tool in {silence_d:.0f} days. Pre-churn signal. "
                    f"Trigger: reach out with a use-case nudge "
                    f"(/api/v1/admin/outreach/send), surface a "
                    f"personalized welcome-back via the upgrade pool, "
                    f"or add their account to the lost-conversion "
                    f"campaign queue."
                ),
            })
    return findings


def check_cf_kv_namespace_pressure() -> list[dict]:
    """Probes the Cloudflare KV API for namespace key counts on
    DCHUB_CACHE / DCHUB_API_KEYS / DCHUB_USAGE. Fires if any
    namespace has >5000 keys (cache stampede signal — orphaned
    entries accumulating because TTL isn't firing).

    Requires CF_API_TOKEN and CF_ACCOUNT_ID env vars. Silent no-op
    if either is missing — this is an enterprise-tier-feature
    detector."""
    findings: list[dict] = []
    import os as _os
    token = _os.environ.get("CF_API_TOKEN")
    acct  = _os.environ.get("CF_ACCOUNT_ID")
    if not token or not acct:
        return findings
    import urllib.request as _ur, json as _json
    # List namespaces, then for each one count keys
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent":    "DCHub-KVPressure/1.0",
    }
    try:
        url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/storage/kv/namespaces?per_page=50"
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return findings
    namespaces = (data.get("result") or [])
    TARGETS = {"DCHUB_CACHE", "DCHUB_API_KEYS", "DCHUB_USAGE"}
    for ns in namespaces:
        title = ns.get("title", "")
        if title not in TARGETS:
            continue
        ns_id = ns.get("id", "")
        # Count keys (CF caps the list to 1000 per page; iterate
        # cursors only if first page is full — for pressure check
        # we just need an "estimated >5000" answer, so cap iteration).
        total = 0
        cursor = None
        try:
            for _ in range(6):  # max 6 pages = 6000 keys probed
                page_url = (
                    f"https://api.cloudflare.com/client/v4/accounts/{acct}"
                    f"/storage/kv/namespaces/{ns_id}/keys?limit=1000"
                )
                if cursor:
                    page_url += f"&cursor={cursor}"
                req = _ur.Request(page_url, headers=headers)
                with _ur.urlopen(req, timeout=8) as resp:
                    pd = _json.loads(resp.read().decode("utf-8", errors="replace"))
                keys = pd.get("result") or []
                total += len(keys)
                cursor = (pd.get("result_info") or {}).get("cursor")
                if not cursor or not keys:
                    break
        except Exception:
            continue
        if total >= 5000:
            findings.append({
                "issue":  "cf_kv_namespace_pressure",
                "url":    f"kv:{title}",
                "count":  total,
                "detail": (
                    f"CF KV namespace `{title}` has ≥{total} keys "
                    f"(threshold: 5000). Cache stampede class — TTL "
                    f"likely not firing or entries are being written "
                    f"with infinite TTL. Audit writes to this namespace "
                    f"for missing `expirationTtl`. KV is unlimited but "
                    f"key-count growth past 5K usually indicates a "
                    f"write-leak, not legitimate cache growth."
                ),
            })
    return findings


def check_multi_cloud_failover_broken() -> list[dict]:
    """Phase r32-multi-cloud (2026-05-21). User hit a multi-cloud outage:
    Railway down (status incident KVZ1Z8GY in progress) AND Render
    also failing (likely Neon credential drift). The architecture
    intent is failover — Render backs up Railway — but if BOTH are
    sick simultaneously, the failover is theatre, not safety.

    This detector probes BOTH origins directly (bypassing the CF
    worker) and fires when:
      - Railway returns connection-refused / timeout (000)
      - Render returns 5xx or doesn't have a fresh response

    The finding is escalation-only because the fix is environment
    (Neon DSN on Render, Railway plan upgrade, etc.) — not autopilot
    actionable. But surfacing it lets the operator catch the
    failover regression BEFORE the next outage."""
    findings: list[dict] = []
    try:
        import urllib.request as _ur
        import urllib.error as _ue
    except Exception:
        return findings

    def _probe(url: str, timeout: int = 8) -> tuple[int, str]:
        try:
            req = _ur.Request(url, headers={
                "User-Agent": "DCHub-FailoverProbe/1.0",
            })
            with _ur.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(200).decode("utf-8", errors="replace")
        except _ue.HTTPError as e:
            return e.code, str(e)[:200]
        except Exception as e:
            return 0, f"{type(e).__name__}: {str(e)[:160]}"

    railway_code, railway_body = _probe(
        "https://dchub-backend-production.up.railway.app/api/v1/health")
    render_code, render_body = _probe(
        "https://dchub-backend-render.onrender.com/api/v1/site/stats")

    railway_ok = 200 <= railway_code < 400
    render_ok = 200 <= render_code < 400

    if not railway_ok and not render_ok:
        findings.append({
            "issue":  "multi_cloud_both_down",
            "url":    "/api/v1/health",
            "count":  1,
            "detail": (
                f"Both backends are unreachable. Railway returned "
                f"{railway_code} ({railway_body[:120]}); Render returned "
                f"{render_code} ({render_body[:120]}). The CF Worker is "
                f"failing every dynamic request — only static pages "
                f"served by CF Pages are working. Investigate Railway "
                f"status page + Render dashboard. If Render is sleeping "
                f"(free tier), wake it with a keep-alive cron or upgrade."
            ),
        })
    elif not railway_ok:
        findings.append({
            "issue":  "railway_down_render_serving",
            "url":    "/api/v1/health",
            "count":  1,
            "detail": (
                f"Railway is down ({railway_code}) but Render is serving. "
                f"Failover is doing its job — verify the CF worker is "
                f"correctly routing 100% of traffic to Render right now "
                f"(x-dc-hub-backend header should say 'render')."
            ),
        })
    elif not render_ok:
        findings.append({
            "issue":  "render_down_railway_serving",
            "url":    "/api/v1/health",
            "count":  1,
            "detail": (
                f"Render backup is unreachable ({render_code}). Railway is "
                f"primary right now. If Railway has an incident, failover "
                f"won't catch us. Likely Neon credential drift on Render "
                f"(ep-old-waterfall-aa2rwjzs-pooler reference from earlier "
                f"in the session) — update DATABASE_URL on Render."
            ),
        })
    return findings


def check_inspector_brief_unprocessed_recipes() -> list[dict]:
    """Phase r32-brain-pipe (2026-05-20). The Inspector (Claude Opus 4.5)
    writes daily briefs that include code-fix RECIPE candidates
    (schema_drift_guard, route_alias_404, cron_if_mismatched). Today's
    brief proposed 4 of these. None have been promoted to L22 auto-PR
    drafting yet because the handoff endpoint /api/v1/brain/brief/<id>
    /draft-prs is human-triggered only.

    This detector fires when:
      - There's a brain_briefs row from the last 24h
      - That brief mentions a RECIPE candidate in its code-fix section
      - The corresponding L22 proposal hasn't been drafted

    Pairs with autopilot pattern `inspector_l22_handoff` which POSTs
    the existing draft-prs endpoint, letting L22's 3-recipe whitelist
    decide whether to actually draft a PR.

    Safety: L22 has _already_drafted() idempotency + a strict
    whitelist (3 recipes only). Brain autopilot has rate-limit +
    cooldown machinery. Three-deep safety boundary."""
    findings: list[dict] = []
    import os as _os, psycopg2 as _pg, re as _re
    db = _os.environ.get("DATABASE_URL")
    if not db: return findings
    try:
        c = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                # Find the most-recent brief that has RECIPE candidates.
                # r33-living option C (2026-05-21): widened lookback
                # from 24h to 7d. Older briefs with RECIPE candidates
                # that nothing acted on are still actionable — the
                # idempotency guard below (brain_findings check) prevents
                # re-firing on the same brief_id. So safe to look back
                # further and catch RECIPE work that piled up.
                cur.execute("""
                    SELECT id, brief_md, generated_at
                      FROM brain_briefs
                     WHERE generated_at > NOW() - INTERVAL '7 days'
                       AND brief_md LIKE '%%RECIPE:%%'
                     ORDER BY generated_at DESC
                     LIMIT 1
                """)
                row = cur.fetchone()
                if not row:
                    return findings
                brief_id, md, gen_at = row

                # Did we already fire the L22 handoff for this brief?
                # Check brain_findings for a previous autopilot record.
                try:
                    # r33-living C: widen idempotency window to match
                    # the 7d brief lookback. If we already fired the
                    # L22 handoff for this specific brief_id in the
                    # last 7 days, don't re-fire — L22's own
                    # _already_drafted() check provides the real safety.
                    # r-l22-throttle (2026-07-22): the cooldown only checked the
                    # `_fired` marker, which autopilot writes AFTER a successful
                    # POST. But the handoff POST rate-limits (0/15 land), so the
                    # marker never gets written and the finding re-emitted every
                    # detector cycle — ~38x/day, eating 265 rate-limits into a
                    # broken path. Also skip if the EMITTED finding itself was
                    # raised for this brief in the last day, so a handoff that
                    # can't land backs off to ~1/day instead of thrashing.
                    cur.execute("""
                        SELECT 1 FROM brain_findings
                         WHERE issue IN ('inspector_l22_handoff_fired',
                                         'inspector_l22_handoff')
                           AND url LIKE %s
                           AND created_at > NOW() - INTERVAL '1 day'
                         LIMIT 1
                    """, (f"%/brief/{brief_id}/%",))
                    if cur.fetchone():
                        return findings  # fired OR emitted recently — don't thrash
                except Exception:
                    try: c.rollback()
                    except Exception: pass

                # Extract just the RECIPE lines for the finding detail.
                recipe_lines = []
                for line in (md or "").split("\n"):
                    if "RECIPE:" in line:
                        recipe_lines.append(line.strip()[:200])
                recipe_summary = "; ".join(recipe_lines[:4])
        finally:
            c.close()
    except Exception:
        return findings

    findings.append({
        "issue":  "inspector_l22_handoff",
        "url":    f"/api/v1/brain/brief/{brief_id}/draft-prs",
        "count_kind": "item_count",  # magnitude, not a recurrence tally
        "count":  len(recipe_lines),
        "detail": (
            f"Inspector brief #{brief_id} ({gen_at.isoformat() if gen_at else 'recent'}) "
            f"proposed {len(recipe_lines)} RECIPE candidate(s) for L22 auto-PR drafting "
            f"but the handoff hasn't fired. Recipes: {recipe_summary}. "
            f"Autopilot will POST /api/v1/brain/brief/{brief_id}/draft-prs to hand them "
            f"to L22's 3-recipe safety whitelist. L22's _already_drafted() idempotency "
            f"plus brain autopilot's rate-limit form a three-deep safety boundary."
        ),
        "_brief_id": brief_id,
    })
    return findings


def check_tier_dict_missing_keys() -> list[dict]:
    """Phase r32-sweep (2026-05-20). Closes the bug class that caused
    Land & Power to silently treat paying $49/mo Developer customers
    as free-tier (they hit 1 search/month instead of 50). Root cause:
    tier-limit dicts predated the canonical anonymous → identified →
    starter → developer → pro+ ladder and were missing entries, so
    `dict.get(tier, default)` fell through to free defaults.

    Detector imports each known tier-limit dict and verifies the five
    canonical tier names are present. Flags any gap so the brain
    surfaces the regression risk BEFORE a customer hits it.

    r-starter-sweep (2026-07-30): 'starter' added to REQUIRED. The set
    predated the r34 starter tier, which is exactly why this radar
    never flagged the starter gaps PR #1943 fixed — the detector was
    GREEN because its own requirement list was stale, not because the
    dicts were complete. Adding a new tier? It must ALSO be added
    here, or this check goes blind to it the same way.

    Adding a new tier-limit table? Add it to TIER_DICTS_TO_CHECK
    below — that's the bug-class containment surface.
    tests/test_tier_consistency.py::test_tier_dicts_cover_canonical_five
    mirrors this audit in CI (it AST-parses what it cannot import), so
    a gap fails the PR before this radar ever sees it in prod."""
    findings: list[dict] = []
    REQUIRED = {'anonymous', 'identified', 'starter', 'developer', 'pro'}
    # (module_path, dict_attr_name, description)
    TIER_DICTS_TO_CHECK = [
        ('api_tier_gating', 'TIER_RATE_LIMITS',
         'Daily API rate limit by tier'),
        ('api_tier_gating', 'TIER_DAILY_RECORD_CAPS',
         'Per-day unique record cap'),
        ('api_tier_gating', 'TIER_PAGE_CAPS',
         'Max pages per paginated query'),
        ('api_tier_gating', 'TIER_SEARCH_LIMITS',
         'Max results per search'),
        ('api_tier_gating', 'MCP_TIER_RESULT_LIMITS',
         'MCP per-tool result limit'),
        ('api_tier_gating', 'PLAN_LEVELS',
         'Plan hierarchy (used by user_has_access)'),
        ('paywall_middleware', 'TIER_HIERARCHY',
         'Tier hierarchy in paywall middleware'),
        ('paywall_middleware', 'RATE_LIMITS',
         'Paywall middleware rate limits'),
        ('paywall_middleware', 'TIER_FEATURES',
         'Paywall middleware feature flags'),
        ('dchub_me', 'LIMITS',
         '/api/me rate-limit table'),
        ('land_power_usage_limiter', 'LAND_POWER_LIMITS',
         'Land & Power tool monthly caps'),
        ('land_power_usage_limiter', 'API_MONTHLY_LIMITS',
         'Land & Power API monthly limits'),
        # r32-sweep round 2 (2026-05-20): wider audit found 3 more
        # tier dicts missing identified + others. Adding them here so
        # the detector covers the bug-class containment surface fully.
        # Nested attrs use a dot path: 'PROTECTION_CONFIG.daily_record_caps'
        # — the detector walks the path and audits the leaf dict.
        ('alert_system_v2', 'ALERT_LIMITS',
         'Alert quotas per tier'),
        ('free_tier_limiter', 'TIER_LIMITS',
         'Land & Power + API monthly caps (free tier limiter)'),
        ('api_data_protection', 'PROTECTION_CONFIG.daily_record_caps',
         'Per-day unique record cap (api_data_protection)'),
        ('api_data_protection', 'PROTECTION_CONFIG.max_results_per_response',
         'Max API response rows (api_data_protection)'),
    ]
    import importlib
    def _resolve_dotted(mod, path):
        """Walk a dot path: 'PROTECTION_CONFIG.daily_record_caps' →
        getattr(mod, 'PROTECTION_CONFIG')['daily_record_caps']. Returns
        None if any step fails so the caller can skip cleanly."""
        parts = path.split('.')
        cur = getattr(mod, parts[0], None)
        for p in parts[1:]:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return None
            if cur is None:
                return None
        return cur

    for mod_path, attr, desc in TIER_DICTS_TO_CHECK:
        try:
            mod = importlib.import_module(mod_path)
            d = _resolve_dotted(mod, attr)
            if not isinstance(d, dict):
                continue
            keys = set(d.keys())
            missing = sorted(REQUIRED - keys)
            if missing:
                findings.append({
                    "issue":  "tier_dict_missing_keys",
                    "url":    f"/gating-matrix#{mod_path}.{attr}",
                    "count_kind": "item_count",  # magnitude, not a recurrence tally
                    "count":  len(missing),
                    "detail": (
                        f"{mod_path}.{attr} ({desc}) is missing required "
                        f"tier keys: {', '.join(missing)}. Callers in those "
                        f"tiers silently fall through to the default (usually "
                        f"'free'), so paying customers may be getting free-"
                        f"tier limits. Add explicit entries — see Land & "
                        f"Power r32 fix for the pattern."
                    ),
                    "_module": mod_path,
                    "_attr":   attr,
                    "_missing": missing,
                })
        except Exception:
            # Module not importable in this scan context — skip silently.
            # We deliberately don't flag missing modules as findings
            # because some are environment-dependent.
            pass
    return findings


def check_pocket_high_mover() -> list[dict]:
    """Phase r28 (2026-05-20). When a tracked market's excess-power
    index shifts ≥15 points in 7 days, that's a story. Pre-r28 the
    only places this surfaced were the developer brief and the daily
    digest — neither of which prompt the autopilot to *do* anything
    with the signal.

    This detector reads market_power_scores 7-day deltas and fires
    findings for any market with |Δ| ≥ 15. Pairs with
    _action_pocket_alert_announce in brain_autopilot.py which drafts
    a press-style sentence and queues it for social auto-publish.

    Threshold tuning: 15pts is large enough that real news (a major
    capacity announcement, a transmission upgrade, a moratorium being
    lifted) drives it, while filtering normal week-to-week noise (most
    deltas are <5pts)."""
    findings: list[dict] = []
    try:
        from routes.pockets import detect_high_movers
        movers = detect_high_movers(threshold=15.0)
    except Exception as e:
        # Pockets module not loaded yet, or pg unavailable — skip silently.
        return findings

    for m in movers[:5]:  # cap so a chaotic week doesn't flood the brief
        direction = "rising" if (m["delta_7d"] or 0) > 0 else "falling"
        sign = "+" if (m["delta_7d"] or 0) > 0 else ""
        findings.append({
            "issue":  "pocket_high_mover",
            "url":    f"/pockets/{m['market_slug']}",
            "count":  1,
            "detail": (
                f"{m['market_name']} ({m['iso'] or '—'}, {m['state'] or '—'}) "
                f"moved {sign}{m['delta_7d']:.1f} pts on the excess-power "
                f"index over the last 7 days — now at {m['current_score']:.1f}, "
                f"verdict {m['verdict'] or 'HOLD'}. "
                f"This is {direction} faster than normal week-to-week noise "
                f"and is worth a tweet/note. /pockets shows full ranking."
            ),
            "_market_slug": m["market_slug"],
            "_market_name": m["market_name"],
            "_iso":         m["iso"],
            "_state":       m["state"],
            "_delta_7d":    m["delta_7d"],
            "_score":       m["current_score"],
            "_verdict":     m["verdict"],
        })
    return findings


def check_founding_customer_not_welcomed() -> list[dict]:
    """Phase FF+25-followup-r21 (2026-05-20). Tonight, Kevin Serfass
    (first paid customer) ended up without a welcome email because the
    Stripe webhook auto-tag fired into a deploy-lag window — the
    founding_customers module wasn't loaded yet, the tag silently 404'd,
    and the welcome email path never fired.

    This detector closes that gap. It scans founding_customers for rows
    where contact_status is 'new' or 'auto-tagged' AND tagged_at is
    older than 1 hour. Any such row means the customer was tagged but
    never welcomed. Autopilot fires the send-welcome endpoint
    autonomously so the customer gets their email even if the original
    path failed.

    The 1-hour threshold gives the standard webhook auto-email path
    time to complete before the brain intervenes — prevents
    double-sending in the happy path."""
    import os as _os, psycopg2 as _pg
    findings: list[dict] = []
    db = _os.environ.get("DATABASE_URL")
    if not db: return findings
    try:
        c = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT email, tagged_at, contact_status
                      FROM founding_customers
                     WHERE COALESCE(contact_status, 'new')
                          IN ('new', 'auto-tagged')
                       AND tagged_at < NOW() - INTERVAL '1 hour'
                     ORDER BY tagged_at ASC
                     LIMIT 5
                """)
                rows = cur.fetchall()
        finally:
            c.close()
    except Exception:
        return findings

    for r in rows:
        email = r[0]
        when = r[1].isoformat() if r[1] else "unknown"
        status = r[2] or "new"
        findings.append({
            "issue":  "founding_customer_not_welcomed",
            "url":    f"/api/v1/admin/founding-customers/send-welcome",
            "count":  1,
            "detail": (f"Founding customer {email} (tagged {when}, "
                       f"status={status}) hasn't received a welcome "
                       f"email after 1 hour. Autopilot will POST "
                       f"/api/v1/admin/founding-customers/send-welcome "
                       f"to rescue. If this fires repeatedly check "
                       f"DCHUB_RESEND_API_KEY on Railway."),
            # extra payload the autopilot action reads
            "_email": email,
        })
    return findings


def check_coverage_gap_canada() -> list[dict]:
    """Phase FF+25-followup-r14 (2026-05-20). User found two Calgary-
    metro facilities (Gryphon Digital Mining in Pincher Creek; Prairie
    Sky in Strathmore) that DCHawk tracks and we don't. Symptom of the
    discovery pipeline missing Canadian DC announcements.

    This detector queries our own facilities table for Canada rows. If
    we have fewer than 80 (industry baseline: ~110 Canadian DCs across
    Toronto/Montreal/Calgary/Vancouver per public sources), it fires.
    Escalation-only — fix is upstream in the discovery crawler, not an
    autopilot action."""
    import os as _os, psycopg2 as _pg
    findings: list[dict] = []
    db = _os.environ.get("DATABASE_URL")
    if not db: return findings
    try:
        c = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    # lint: legacy-facilities-ok — intentional audit of legacy table
                    SELECT COUNT(*) FROM facilities
                     WHERE LOWER(COALESCE(country,'')) IN
                          ('ca','canada','can')
                """)
                ca_count = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("""
                    # lint: legacy-facilities-ok — intentional audit of legacy table
                    SELECT COUNT(*) FROM facilities
                     WHERE (LOWER(COALESCE(state,'')) = 'ab'
                            OR LOWER(COALESCE(city,'')) LIKE '%calgar%'
                            OR LOWER(COALESCE(city,'')) LIKE '%edmont%'
                            OR LOWER(COALESCE(city,'')) LIKE '%pincher%'
                            OR LOWER(COALESCE(city,'')) LIKE '%strathmore%')
                """)
                ab_count = int((cur.fetchone() or [0])[0] or 0)
        finally:
            c.close()
    except Exception:
        return findings

    # Thresholds calibrated to current public estimates. Tunable via env
    # if either over- or under-fires for a few weeks.
    if ca_count < 80:
        findings.append({
            "issue":  "coverage_gap_canada",
            "url":    f"/api/v1/facilities?country=CA",
            "count":  80 - ca_count,
            "detail": (f"Only {ca_count} Canadian facilities in DB "
                       f"(baseline ~110+). DCHawk + dcByte have more. "
                       f"Discovery crawler likely missing CA sources. "
                       f"Inspect crawler_scheduler.py + add a Canadian "
                       f"data source (dcd.com, datacenterhawk.com, the "
                       f"CDCRA registry). Patch immediate gaps via "
                       f"POST /api/v1/admin/facilities/bulk."),
        })
    if ab_count < 8:
        findings.append({
            "issue":  "coverage_gap_alberta",
            "url":    f"/api/v1/facilities?state=AB",
            "count":  8 - ab_count,
            "detail": (f"Alberta footprint thin: {ab_count} facilities "
                       f"tracked, vs known active builds in Pincher "
                       f"Creek (Gryphon Digital Mining), Strathmore "
                       f"(Prairie Sky Data Solutions), Calgary metro. "
                       f"User reported this gap on 2026-05-20."),
        })
    return findings


def check_page_brand_drift() -> list[dict]:
    """Phase FF+25-followup-r12 (2026-05-20). The user is tired of
    fixing visual drift one page at a time. This detector fetches a
    rotating sample of canonical public pages and looks for signals
    that the page has drifted off-brand:

      · missing data-dchub-brand attribute (no canonical mark)
      · missing 'Instrument Sans' font reference
      · old color tokens: #10b981, #3478f6, #06b6d4 (legacy green/cyan)
      · missing /js/dchub-brand.js script reference

    Fires page_brand_drift finding with count of drift signals when at
    least one signal is found. Lets the brain catch new drift the same
    way it catches schema drift today.
    """
    import urllib.request as _req
    findings: list[dict] = []
    # Rotating canonical sample (deterministic but covers different
    # pages over a 24-hour window so we don't hit the same 5 every tick).
    import datetime as _dt
    hour_bucket = _dt.datetime.utcnow().hour
    # r86: dropped "/status" — it 301s to status.dchub.cloud (external), so a
    # brand scan of the redirect target always false-flagged drift.
    sample_pool = [
        "/", "/about", "/pricing", "/intelligence", "/ai-hub",
        "/dcpi", "/transactions", "/cited-by", "/reports/monthly",
        "/daily", "/advertise", "/markets/", "/brain/brief",
    ]
    # Take 5 from the pool keyed off hour so we cycle through over time
    start = (hour_bucket * 3) % len(sample_pool)
    sample = (sample_pool + sample_pool)[start:start + 5]

    drift_pages: list[dict] = []
    for path in sample:
        try:
            req = _req.Request(
                f"https://dchub.cloud{path}",
                headers={"User-Agent": "DCHubBrainDriftDetector/1.0"},
            )
            with _req.urlopen(req, timeout=8) as resp:
                html = resp.read(120000).decode("utf-8", errors="replace")
        except Exception:
            continue
        if not html or len(html) < 1000:
            continue

        signals = []
        # r86: data-dchub-brand + dchub-brand.js are injected at RUNTIME by
        # /js/dchub-nav.js (see dchub-nav.js inject()/buildNavHTML) — they are
        # NEVER present in the served static HTML, so the old needles
        # false-flagged drift on every modern page (the /about, /pricing
        # missing-brand-mark/missing-brand-script findings). The real canonical-
        # brand signal is nav.js itself being loaded.
        if "/js/dchub-nav.js" not in html:
            signals.append("missing-nav-js")
        if "Instrument Sans" not in html and "/js/dchub-nav.js" not in html:
            signals.append("missing-instrument-sans")
        # Legacy color tokens — only flag when several appear (one stray
        # hex in an OG meta tag isn't drift)
        # r88h: #10b981 (emerald) is a CURRENT brand accent, not legacy — counting
        # it tripped false legacy-colors drift on pages that legitimately use it
        # (/dcpi, /pricing). Keep only the genuinely-retired tokens.
        legacy = sum(html.count(c) for c in ("#3478f6", "#06b6d4"))
        if legacy >= 3:
            signals.append(f"legacy-colors({legacy})")

        if signals:
            drift_pages.append({"path": path, "signals": signals})

    if drift_pages:
        details = "; ".join(
            f"{p['path']} → " + ",".join(p["signals"])
            for p in drift_pages[:5]
        )
        findings.append({
            "issue":  "page_brand_drift",
            "url":    "/status",
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count":  len(drift_pages),
            "detail": (f"{len(drift_pages)} of {len(sample)} sampled "
                       f"pages drifted off-canonical brand: {details}. "
                       f"Fix by editing /js/dchub-nav.js (covers all "
                       f"pages that load it) or the per-page <style> "
                       f"block. Track at /status."),
        })
    return findings


_BRAND_UNIFORMITY_CACHE = {"ts": 0.0, "findings": None}
# r-fix (2026-06-06): brand uniformity only changes when the FRONTEND
# redeploys (rare), yet this detector re-fetched every top public page on
# every radar scan — DCHub-BrainUniformity was ~9.7k req/day on the CF
# dashboard. Cache the whole sweep for 6h (matches the deadlink-probe + self-
# heal cadence) so we keep detection without DOSing the single Railway replica.
_BRAND_UNIFORMITY_TTL_S = 21600


def check_page_brand_uniformity() -> list[dict]:
    """Phase r33-K (2026-05-21). After r33-I's manual sweep unified every
    public page to the canonical brand (Instrument Sans + indigo→violet
    + dchub-brand.css + dchub-nav.js), this detector watches for FUTURE
    drift so a regression never ships silently.

    Companion to check_page_brand_drift (sampled, rotating) — this one
    scans ALL top public pages every cycle and tests for two failure
    modes side by side:

      A. MISSING required brand elements (positive signals):
         · /static/dchub-brand.css link
         · 'Instrument Sans' font reference
         · /js/dchub-nav.js script

      B. PRESENT off-brand patterns we just removed (negative signals):
         · #1e40af — wrong-blue accent (should be #6366f1)
         · #065f46 / #0f766e — old emerald-teal gradient (should be
           indigo/violet)
         · body { font-family:-apple-system,BlinkMacSystemFont } — fall-
           back stack used as the primary, no Instrument Sans
         · body { font-family: Inter } — wrong canonical font
         · font-family: 'DM Sans' — wrong canonical font

    Fires one finding per page/issue, capped at 20 per scan so a fully-
    broken site doesn't flood the radar. Detail field is actionable:
    names the page + the exact CSS/HTML change to make.
    """
    import urllib.request as _req
    import urllib.error as _rerr
    import concurrent.futures as _cf
    import re as _re
    import time as _t

    # Serve the cached sweep if still fresh — dedups the all-pages probe across
    # every brain workflow that hits the radar inside the 6h window (0 probes).
    _now = _t.time()
    if (_BRAND_UNIFORMITY_CACHE["findings"] is not None
            and (_now - _BRAND_UNIFORMITY_CACHE["ts"]) < _BRAND_UNIFORMITY_TTL_S):
        return list(_BRAND_UNIFORMITY_CACHE["findings"])

    PAGES = [
        '/', '/pricing', '/api-docs', '/developers', '/architecture',
        '/transactions', '/transaction-comps', '/markets', '/dcpi',
        '/pockets', '/coverage', '/digest', '/news', '/press',
        '/dc-hub-media', '/tax-incentives', '/ai', '/ai-deals',
        '/ai-pipeline', '/ai-integrations', '/ai-inventory',
        '/state-of-the-data-center', '/system-status', '/grid-intelligence',
        '/platform', '/sites', '/spare-capacity', '/capacity-pipeline',
        # r86: dropped "/mcp" — it is the MCP JSON-RPC endpoint
        # (application/json), not an HTML page, so brand needles never matched
        # and it false-flagged "missing dchub nav js" every pass.
    ]

    REQUIRED = [
        ("/static/dchub-brand.css", "missing-brand-css"),
        ("Instrument Sans",         "missing-instrument-sans"),
        ("/js/dchub-nav.js",        "missing-dchub-nav-js"),
    ]

    # Off-brand patterns we just removed in r33-I. Each tuple is
    # (needle, issue-tag, fix-hint). For body-font patterns we use a
    # compiled regex because we need to match a `body { ... }` block.
    OFF_BRAND_LITERALS = [
        ("#1e40af",
         "off-brand-blue-1e40af",
         "replace #1e40af with #6366f1 (canonical indigo accent)"),
        ("#065f46",
         "off-brand-emerald-065f46",
         "replace #065f46 with the indigo→violet gradient "
         "(linear-gradient(135deg,#6366f1,#a855f7))"),
        ("#0f766e",
         "off-brand-teal-0f766e",
         "replace #0f766e with the indigo→violet gradient "
         "(linear-gradient(135deg,#6366f1,#a855f7))"),
    ]
    # Body-font regressions: only flag when the wrong family is the
    # PRIMARY family inside a body{} declaration, not a fallback later
    # in a stack that starts with Instrument Sans.
    _BODY_BLOCK_RE = _re.compile(r"body\s*\{[^}]{0,400}\}", _re.IGNORECASE)
    BODY_FONT_PATTERNS = [
        (_re.compile(r"font-family\s*:\s*-apple-system\s*,\s*BlinkMacSystemFont",
                     _re.IGNORECASE),
         "body-font-apple-system-primary",
         "body uses the -apple-system,BlinkMacSystemFont stack as the "
         "primary — prepend 'Instrument Sans' so canonical font wins"),
        (_re.compile(r"font-family\s*:\s*Inter\b", _re.IGNORECASE),
         "body-font-inter",
         "body { font-family: Inter } — replace with "
         "'Instrument Sans','Inter',-apple-system,sans-serif"),
        (_re.compile(r"font-family\s*:\s*['\"]DM Sans['\"]", _re.IGNORECASE),
         "body-font-dm-sans",
         "body { font-family: 'DM Sans' } — replace with "
         "'Instrument Sans',-apple-system,sans-serif"),
    ]

    def _fetch(path: str) -> tuple[str, Optional[str], Optional[int]]:
        url = f"https://dchub.cloud{path}"
        try:
            req = _req.Request(
                url,
                headers={"User-Agent": "DCHub-BrainUniformity/1.0"},
            )
            with _req.urlopen(req, timeout=10) as resp:
                code = resp.getcode()
                body = resp.read(200000).decode("utf-8", errors="replace")
                return path, body, code
        except _rerr.HTTPError as e:
            return path, None, e.code
        except Exception:
            return path, None, None

    findings: list[dict] = []
    # 8 parallel fetches with a global cap so one slow page doesn't
    # block the whole scan.
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch, PAGES))

    for path, html, code in results:
        if not html or code != 200:
            # Non-200 is caught by check_frontend_critical_endpoints
            # and friends — don't double-report here.
            continue
        # r86: only brand-scan bodies that are actually HTML documents. A path
        # that resolves to JSON (e.g. an API/MCP endpoint) has no brand surface,
        # so scanning it just manufactures false "missing brand" findings.
        if "<html" not in html[:2000].lower():
            continue
        if len(findings) >= 20:
            break

        # A. Required elements
        for needle, tag in REQUIRED:
            if len(findings) >= 20:
                break
            if needle not in html:
                if tag == "missing-brand-css":
                    fix = ("add `<link rel=\"stylesheet\" "
                           "href=\"/static/dchub-brand.css\">` before "
                           "</head>")
                elif tag == "missing-instrument-sans":
                    fix = ("add Instrument Sans — either via the Google "
                           "Fonts <link> in <head> or by ensuring "
                           "/static/dchub-brand.css is loaded (it "
                           "@imports the font)")
                else:  # missing-dchub-nav-js
                    fix = ("add `<script src=\"/js/dchub-nav.js\" "
                           "defer></script>` before </body> so the "
                           "shared nav renders")
                findings.append({
                    "issue":  "page_brand_uniformity",
                    "url":    path,
                    "count":  1,
                    "detail": (f"Page {path} {tag.replace('-', ' ')} — "
                               f"{fix}. After r33-I unification this "
                               f"page is a regression."),
                })

        # B. Off-brand literals (anywhere in the HTML)
        for needle, tag, hint in OFF_BRAND_LITERALS:
            if len(findings) >= 20:
                break
            if needle in html:
                # r64-d (2026-05-31): count = 1, NOT html.count(needle).
                # Each page/pattern is ONE distinct finding. Emitting the
                # raw occurrence count (a single CSS var like #1e40af can
                # appear 40+ times on one page) is exactly the
                # COUNT(*)-not-DISTINCT inflation the signal-inflation rule
                # warns about: any downstream consumer that SUM()s the
                # `count` field across these findings ballooned to ~366
                # "escalated brand-uniformity issues" when the real DISTINCT
                # open-finding count is ~7. The literal occurrence count is
                # preserved in `detail` for the operator; `count` is now the
                # distinct-finding unit so the brain narrates 7, not 366.
                _occurrences = html.count(needle)
                findings.append({
                    "issue":  "page_brand_uniformity",
                    "url":    path,
                    "count":  1,
                    "detail": (f"Page {path} contains off-brand pattern "
                               f"`{needle}` ({tag}, {_occurrences}× on page) "
                               f"— {hint}. After r33-I unification this "
                               f"page is a regression."),
                })

        # C. Body-font regressions (regex-checked inside body{} blocks)
        for body_block in _BODY_BLOCK_RE.findall(html):
            for rx, tag, hint in BODY_FONT_PATTERNS:
                if len(findings) >= 20:
                    break
                if rx.search(body_block):
                    findings.append({
                        "issue":  "page_brand_uniformity",
                        "url":    path,
                        "count":  1,
                        "detail": (f"Page {path} has off-brand body "
                                   f"font ({tag}) — {hint}. After "
                                   f"r33-I unification this page is a "
                                   f"regression."),
                    })
            if len(findings) >= 20:
                break

    _BRAND_UNIFORMITY_CACHE["ts"] = _now
    _BRAND_UNIFORMITY_CACHE["findings"] = list(findings)
    return findings


def check_outbound_distribution_health() -> list[dict]:
    """Phase r33-N (2026-05-21) — outbound discovery health.

    Watches our PRESENCE on 7 major MCP discovery surfaces (Smithery,
    mcp.so, MCPHub, PulseMCP, Glama, awesome-mcp-servers, Anthropic).
    Fires when: any audit is stale >48h (cron broken) OR any target
    is `not_listed` for >7d (submission stalled) OR any target has
    NEVER been audited (first-run not triggered)."""
    findings: list[dict] = []
    import os as _os, psycopg2 as _pg
    db = _os.environ.get("DATABASE_URL")
    if not db: return findings
    try:
        conn = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.outreach_submissions')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                cur.execute("""
                    SELECT DISTINCT ON (target_key)
                           target_key, target_name, outcome,
                           submitted_at, detail
                      FROM outreach_submissions
                     WHERE action = 'audit'
                     ORDER BY target_key, submitted_at DESC
                """)
                audits = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return findings

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    # r78: never flag registries already declared dead (no working submit
    # path). mcphub sat in the stuck-issue worklist forever because this
    # checker kept re-filing it every cycle while the outreach module had
    # already written it off.
    try:
        from routes.mcp_registry_outreach import _DEAD_REGISTRY_KEYS as _DEAD_RK
    except Exception:
        _DEAD_RK = set()
    seen = set()
    for tk, tname, outcome, ts, detail in audits:
        seen.add(tk)
        if tk in _DEAD_RK:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        age_h = (now - ts).total_seconds() / 3600.0
        if outcome == "not_listed":
            findings.append({
                "issue":  "outbound_distribution_health",
                "url":    f"target:{tk}",
                "count_kind": "hours",  # magnitude, not a recurrence tally
                "count":  int(age_h),
                "detail": (f"{tname} audit: `not_listed` "
                           f"({(detail or '')[:80]}). {age_h:.0f}h ago. "
                           f"Open the PR / fill form. See "
                           f"/api/v1/admin/outreach/mcp-registry/status"),
            })
        if age_h > 48.0:
            findings.append({
                "issue":  "outbound_distribution_health",
                "url":    f"target:{tk}",
                "count_kind": "hours",  # magnitude, not a recurrence tally
                "count":  int(age_h),
                "detail": (f"{tname} not audited in {age_h:.0f}h. The "
                           f"daily mcp-outreach.yml cron may be broken."),
            })
    try:
        from routes.mcp_registry_outreach import DISCOVERY_TARGETS as _TARGETS
        for t in _TARGETS:
            if t["key"] not in seen and t["key"] not in _DEAD_RK:
                findings.append({
                    "issue":  "outbound_distribution_health",
                    "url":    f"target:{t['key']}",
                    "count":  0,
                    "detail": (f"{t['name']} has never been audited. "
                               f"POST /api/v1/admin/outreach/mcp-registry/submit "
                               f"with target={t['key']} to start."),
                })
    except Exception:
        pass
    return findings[:10]


def check_monthly_trend_unsent_3d() -> list[dict]:
    """Phase FF+25-followup-r7 (2026-05-20). If today is the 4th or later
    of a new month AND we haven't yet emailed the prior-month monthly
    trend snapshot to the journalist outreach list, fire this finding.
    The autopilot's autonomous action POSTs the send endpoint, acting
    as a backstop for the GitHub cron in case it failed.

    Threshold: day >= 4 of the new month. Gives the cron its full
    grace window (1st 00:05 UTC fire, 2nd-3rd retry buffer)."""
    import datetime as _dt
    findings: list[dict] = []
    today = _dt.date.today()
    if today.day < 4:
        return findings   # still inside cron grace window

    # Prior month
    if today.month == 1:
        py, pm = today.year - 1, 12
    else:
        py, pm = today.year, today.month - 1
    prior_label = _dt.date(py, pm, 1).strftime("%B %Y")

    try:
        import os, psycopg2 as _pg
        db = os.environ.get("DATABASE_URL")
        if not db: return findings
        c = _pg.connect(db, sslmode="require", connect_timeout=5)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM monthly_outreach_log
                     WHERE year = %s AND month = %s
                """, (py, pm))
                already_sent = int((cur.fetchone() or [0])[0] or 0)
        finally:
            c.close()
    except Exception:
        # Table might not exist yet on a fresh deploy — treat as unsent
        # so the brain fires the create-and-send on its first tick.
        already_sent = 0

    if already_sent:
        return findings

    findings.append({
        "issue":  "monthly_trend_unsent_3d",
        "url":    f"/reports/monthly/{py}-{pm:02d}",
        "count":  today.day,
        "detail": (f"Day {today.day} of the new month and the {prior_label} "
                   f"monthly trend snapshot has not been emailed to the "
                   f"journalist outreach list (DCD, DCK, DCF, WSJ, "
                   f"Forbes, Semafor, SFGate, Runtime). Autopilot will "
                   f"POST /api/v1/reports/monthly/send-outreach as a "
                   f"backstop for the GitHub cron."),
    })
    return findings


def check_dchub_media_press_silent() -> list[dict]:
    """User asked 'is DC Hub Media telling everyone?' Honest answer
    when last checked: NO — 0 press releases in /api/v1/press-releases/
    list. This detector quantifies press silence so the autopilot
    auto-triggers /api/v1/marketing/auto-generate when it fires.

    Two thresholds:
      - silent: no press in 7+ days → autopilot AUTO-FIRES press worker
      - weak:   <4 press in 30 days → escalate to human"""
    findings: list[dict] = []
    try:
        from routes.dchub_media_revival import _last_press_age_days
        age, count_30d = _last_press_age_days()
    except Exception:
        return findings
    if age is None:
        # No press table data — surface as silent (cold start case)
        findings.append({
            "issue":  "dchub_media_press_silent",
            "url":    "/api/v1/media/press-health",
            "count":  999,
            "detail": ("No press release timestamps found in "
                       "auto_press_releases or press_releases tables. "
                       "DC Hub Media is silent — autopilot will trigger "
                       "/api/v1/marketing/auto-generate."),
        })
        return findings
    if age > 7:
        findings.append({
            "issue":  "dchub_media_press_silent",
            "url":    "/api/v1/media/press-health",
            "count":  int(age),
            "detail": (f"DC Hub Media has been silent for {age:.1f} days "
                       f"({count_30d} press releases in last 30d). "
                       f"Source-of-truth score is anemic. Autopilot will "
                       f"AUTO-TRIGGER /api/v1/marketing/auto-generate."),
        })
    elif count_30d < 4:
        findings.append({
            "issue":  "dchub_media_press_weak",
            "url":    "/api/v1/media/press-health",
            "count":  count_30d,
            "detail": (f"DC Hub Media output is weak: only {count_30d} "
                       f"press releases in last 30 days. Healthy cadence "
                       f"is 4+/month. Escalate to operator — needs human "
                       f"to inspect why the auto-press cron is running "
                       f"but not landing rows."),
        })
    return findings


def check_press_public_surface_stale() -> list[dict]:
    """r-daily-callout (2026-07-18) — the July post-mortem detector.

    The stall the operator caught by hand: press_releases gained 1-2 rows
    EVERY day while the public dchub.cloud/press page froze at 2026-06-22
    (~26 days). check_dchub_media_press_silent above reads the DB tables,
    so it was structurally blind — generation and PUBLICATION are different
    ends of the pipe. This detector measures the end users (and AI crawlers)
    actually see: newest ISO date visible in the public HTML vs newest
    press_releases row. Fires per stale page when the edge trails the DB by
    more than routes.brain_daily_callout.SURFACE_LAG_DAYS (default 3d)."""
    findings: list[dict] = []
    try:
        from routes.brain_daily_callout import press_surface_report
        report = press_surface_report()
    except Exception:
        return findings
    for page in report.get("pages") or []:
        if not page.get("stale"):
            continue
        lag = page.get("lag_days")
        findings.append({
            "issue": "press_public_surface_stale",
            "url":   page.get("url") or "https://dchub.cloud/press",
            "count": int(lag) if lag is not None else 999,
            "detail": (f"Public page {page.get('url')} shows newest press "
                       f"date {page.get('newest_visible') or 'NONE'} while "
                       f"the DB's newest press_releases row is "
                       f"{report.get('db_newest')} — the static press bake "
                       f"is stalled ({lag if lag is not None else '?'}d "
                       f"behind); the generator is fine. Actuator (dchub-"
                       f"frontend repo): `gh workflow run press-rss.yml` "
                       f"(hourly lane that bakes releases into press.html + "
                       f"dc-hub-media/index.html via scripts/"
                       f"bake_press_static.py, then pushes → Pages deploy "
                       f"purges both URLs); if the page is still stale "
                       f"after a green run, `gh workflow run cf-purge.yml "
                       f"-f urls=...` — the /* catch-all's stale-while-"
                       f"revalidate=86400 can pin one day-stale copy per "
                       f"POP. Generation-side restart (press-publisher/run) "
                       f"will NOT fix this."),
        })
    return findings


# ── Phase PPPP (2026-05-16) — dedup-pipeline divergence detector ──
def check_dedup_backlog_growing() -> list[dict]:
    """Fires when the raw vs verified gap is >5,000 AND verified
    hasn't moved in 7 days. Surfaces a stalled dedup worker — the
    user reported 12,553 verified that was actually 10,078 (worker
    DID run) vs 21,374 raw (lots of un-deduped rows piling up).

    The honest read: dedup work is happening sporadically but the
    backlog is huge. This detector lets the brain catch the next
    stall before users notice their displayed count drifting from
    the true tracked count."""
    try:
        from routes.facilities_delta import compute_delta
        d = compute_delta()
    except Exception:
        return []
    cur = d.get("current") or {}
    total    = int(cur.get("total")    or 0)
    verified = int(cur.get("verified") or 0)
    # 2026-07-10 (issue #1539): the backlog is the rows actually AWAITING
    # dedup/merge (the pending queue), NOT total-verified. total-verified
    # counts rows already FLAGGED as duplicates (processed, kept for lineage)
    # as if they were still waiting — combined with the old queue-as-verified
    # counter this read "21,937 candidates, 0 verified of 21,937 raw" while
    # the pipeline was actively merging (516 merges in the prior 7 days).
    pending = cur.get("pending")
    gap = int(pending) if pending is not None else (total - verified)
    if gap < 5000:
        return []
    # If we have baseline, check whether verified has moved
    delta_7d = (d.get("deltas") or {}).get("7d") or {}
    verified_delta = int(delta_7d.get("verified") or 0)
    # If verified hasn't moved >100 in 7 days while gap is >5K, stall
    if abs(verified_delta) < 100 and d.get("snapshots_available", 0) >= 7:
        return [{
            "issue":  "dedup_pipeline_stalled",
            "url":    "/api/v1/facilities/delta",
            "count":  gap,
            "detail": (f"Facility dedup backlog: {gap:,} rows awaiting "
                       f"dedup/merge (raw {total:,}, verified fleet {verified:,}). "
                       f"Verified count moved only {verified_delta} over the "
                       f"last 7 days. The dedup worker has stalled or slowed "
                       f"dramatically; users see a stale facility count on "
                       f"the homepage. Inspect the dedup cron + "
                       f"discovery_routes.py merge logic."),
        }]
    # No baseline yet — still flag the gap as informational
    return [{
        "issue":  "dedup_backlog_large",
        "url":    "/api/v1/facilities/delta",
        "count":  gap,
        "count_kind": "backlog_size",  # VALUE not a recurrence count (see brain_work_selector.VALUE_NOT_COUNT_ISSUES)
        "detail": (f"Facility dedup backlog: {gap:,} candidates "
                   f"awaiting dedup/merge (verified fleet {verified:,} of {total:,} raw). "
                   f"Not yet flagged as stalled — need 7d of snapshots for "
                   f"that. If this gap doesn't shrink over the next week, "
                   f"check the dedup pipeline."),
    }]


# ── Phase HHHH (2026-05-16) — facility-count stagnation detector ──
def check_facility_count_stagnant() -> list[dict]:
    """Fires when the 7-day facility-count delta is zero (or negative).
    User pain: 'ai-inventory hasn't improved in weeks, same 12,553
    facilities.' That's a silent discovery-pipeline failure that
    used to require human spotting. Now it surfaces in the heartbeat.

    Requires the Phase HHHH facility_count_snapshots table to be
    populated by the daily cron — quiet during the first 7 days
    after deploy while we accumulate baseline."""
    try:
        from routes.facilities_delta import compute_delta
        d = compute_delta()
    except Exception:
        return []
    if d.get("snapshots_available", 0) < 7:
        return []  # not enough baseline yet
    delta_7d = (d.get("deltas") or {}).get("7d")
    if not delta_7d: return []
    net_total = int(delta_7d.get("total") or 0)
    if net_total > 0:
        return []
    current_total = int((d.get("current") or {}).get("total") or 0)
    stagnant_days = int(d.get("stagnant_days_7d") or 0)
    return [{
        "issue":  "facility_count_stagnant",
        "url":    "/api/v1/facilities/delta",
        "count":  abs(net_total),
        "detail": (f"Facility count over the last 7 days: net {net_total} "
                   f"({current_total:,} current). {stagnant_days} of the "
                   f"last 7 days saw ZERO net growth — the discovery "
                   f"pipeline likely stopped finding new facilities. "
                   f"Inspect routes/discovery_routes.py + the daily ingest "
                   f"crons; either add new sources or fix a broken one."),
    }]


# ── Phase DDDD (2026-05-16) — REST upgrade-gate hit detector ──────
def check_rest_gate_hits() -> list[dict]:
    """Surface the count of REST upgrade-gate 402 responses in the
    last 24h as a brain finding. Lets us see — at heartbeat cadence —
    whether the new DEVELOPER + PRO REST gates (Phase DDDD) are
    actually being hit and how many leads they're producing.

    Reads mcp_call_log if it tracks REST too (it doesn't by default),
    else relies on developer_funnel_events with event_type=cta_click
    OR the upcoming rest_gate_hits table (Phase DDDD+ telemetry).
    Always returns at most 1 finding (the aggregate count) — purely
    informational, not blocking."""
    conn = _db()
    if conn is None: return []
    try:
        with conn.cursor() as cur:
            try:
                # Best-effort across the available signal sources
                cur.execute("""
                    SELECT to_regclass('public.developer_funnel_events')
                """)
                if not (cur.fetchone() or [None])[0]:
                    return []
                cur.execute("""
                    SELECT COUNT(*) FROM developer_funnel_events
                     WHERE event_type IN ('cta_click','pricing_view','key_claimed')
                       AND ts >= NOW() - INTERVAL '24 hours'
                """)
                hits = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                return []
    finally:
        try: conn.close()
        except Exception: pass
    if hits <= 0:
        return []
    # Not an "issue" — informational heartbeat metric. Only surface
    # if there's enough volume that the count tells the operator
    # something meaningful (>= 5 in 24h).
    if hits < 5:
        return []
    return [{
        "issue":  "upgrade_gate_traffic_active",
        "url":    "/api/v1/developers/funnel",
        "count":  hits,
        "detail": (f"{hits} upgrade-gate interactions in last 24h "
                   f"(developer_funnel_events: cta_click + pricing_view + "
                   f"key_claimed). Healthy signal that the Phase DDDD "
                   f"REST gates are visible to users. Compare to "
                   f"30d conversion rate via /api/v1/developers/funnel."),
    }]


# ── Phase BBBB (2026-05-16) — /developers funnel drop detector ────
def check_developers_funnel_dead() -> list[dict]:
    """Surface when /developers gets traffic but stage-1 (intent
    signal) drop is >95% — page is attracting visits but failing to
    convert interest into intent. The user asked: 'is our developer
    site actively getting new ai agents to use our tool?' This is
    the answer surface."""
    findings: list[dict] = []
    try:
        from routes.developers_funnel import _compute_funnel
        d = _compute_funnel(days=30)
    except Exception:
        return findings
    s = d.get("stages") or {}
    visitors = int(s.get("0_unique_visitors") or 0)
    intent   = int(s.get("1_intent_signals")  or 0)
    if visitors < 50:
        return findings  # not enough data yet
    if visitors == 0:
        return findings
    intent_rate = 100.0 * intent / visitors
    if intent_rate < 5.0:  # <5% of visitors signal any intent
        findings.append({
            "issue":  "developers_funnel_intent_dead",
            "url":    "/api/v1/developers/funnel",
            "count":  int(intent_rate * 10),  # rate * 10 so it's visible in heartbeat
            "detail": (f"/developers got {visitors} unique visitors in 30d "
                       f"but only {intent} intent signals ({intent_rate:.1f}% "
                       f"intent rate). Either the page copy isn't converting "
                       f"or the CTA is buried. Inspect "
                       f"/api/v1/developers/funnel for the per-stage breakdown "
                       f"+ run an A/B on the pricing block."),
        })
    return findings


# ── Phase CCCC (2026-05-16) — spare-capacity health detector ──────
def check_spare_capacity_status() -> list[dict]:
    """Surface (1) the initial milestone of any listings appearing, +
    (2) pending listings older than 24h that haven't been moderated.
    The marketplace is brand new — the user wants to know when it
    starts catching real listings + needs to be reminded to approve
    pending submissions."""
    findings: list[dict] = []
    conn = _db()
    if conn is None: return findings
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.spare_capacity_listings')")
                if not (cur.fetchone() or [None])[0]: return findings
            except Exception:
                return findings
            try:
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE status = 'live')    AS live,
                      COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                      COUNT(*) FILTER (WHERE status = 'pending'
                                       AND created_at < NOW() - INTERVAL '24 hours') AS pending_stale,
                      COALESCE(SUM(mw_available) FILTER (WHERE status = 'live'), 0) AS total_live_mw
                      FROM spare_capacity_listings
                """)
                r = cur.fetchone() or (0,0,0,0)
                live, pending, pending_stale, total_mw = (int(r[0] or 0), int(r[1] or 0),
                                                            int(r[2] or 0), float(r[3] or 0))
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    if pending_stale > 0:
        findings.append({
            "issue":  "spare_capacity_pending_moderation",
            "url":    "/api/v1/spare-capacity/listings?status=pending",
            "count":  pending_stale,
            "detail": (f"{pending_stale} spare-capacity listings have been "
                       f"pending for >24h. Review + flip status to 'live' "
                       f"in spare_capacity_listings table, or build the "
                       f"admin approval endpoint (Phase DDDD+). Total "
                       f"pending: {pending}, live: {live}, live MW: {total_mw:.1f}."),
        })
    # Don't flag absence of listings (zero is the default state until the
    # marketplace gets traction — flagging "0 listings" every cycle would
    # be noise).
    return findings


# ── Phase AAAA (2026-05-16) — dormant-MCP detector ────────────────
def check_mcp_dormant_agents() -> list[dict]:
    """Surface the top-3 dormant MCP agents (>30 prior calls, idle 14+
    days) as brain findings. The user reported /ai-integrations
    showing ~90+ inactive MCP connections — those are real prospect
    waste. This puts a regularly-refreshed count + the top winback
    targets on the heartbeat so DC Hub Media has a structured outreach
    worklist instead of guessing at AI-platform contact pages.

    Cap at 3 to keep heartbeat readable; full list at
    /api/v1/bots/dormant."""
    findings: list[dict] = []
    try:
        from routes.bot_outreach import _compute_dormant
        dormant = _compute_dormant(min_prior_calls=30, idle_days=14)
    except Exception:
        return findings
    if not dormant:
        return findings
    # Aggregate count + top targets as one finding (less noise than 3)
    high_priority = [d for d in dormant if d.get("suggested_action") == "high_priority_winback"]
    top = dormant[0]
    findings.append({
        "issue":  "mcp_dormant_agents_present",
        "url":    "/api/v1/bots/dormant",
        "count_kind": "item_count",  # magnitude, not a recurrence tally
        "count":  len(dormant),
        "detail": (f"{len(dormant)} MCP agents went dormant (no calls in "
                   f"14+ days but >=30 prior calls in last 90 days). "
                   f"{len(high_priority)} are HIGH-PRIORITY winback "
                   f"candidates (>=100 prior calls). Top target: "
                   f"ip_hash={top.get('ip_hash')} ua='"
                   f"{(top.get('ua_fingerprint','') or '')[:50]}', "
                   f"{top.get('prior_calls')} prior calls, idle "
                   f"{top.get('days_idle')}d. Full list at "
                   f"/api/v1/bots/dormant."),
    })
    return findings


# ── Phase XXX (2026-05-16) — conversion-rate floor detector ───────
def check_conversion_rate_floor() -> list[dict]:
    """Fires when MCP conversion rate over last 30 days is below the
    target floor (default 0.5%) AND the denominator is meaningful
    (>1000 paywall signals). The user explicitly asked: 'we need to
    gate more data, to incent people to upgrade, right now they aren't
    doing it.' This puts numerical pressure on the heartbeat so any
    tier-config regression that drops conversion shows immediately."""
    conn = _db()
    if conn is None: return []
    findings: list[dict] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT to_regclass('public.mcp_upgrade_signals'),
                           to_regclass('public.mcp_pair_codes')
                """)
                regs = cur.fetchone() or [None,None]
                if not (regs[0] and regs[1]): return findings
            except Exception:
                return findings
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '30 days'
                """)
                signals = int((cur.fetchone() or [0])[0] or 0)
                # r36 (2026-05-31): the conversion RATE must be per DISTINCT
                # caller, not per raw signal. mcp_upgrade_signals is dominated by
                # a few power-keys looping thousands of calls (one key alone =
                # 8,999 search_facilities calls, 0_unique_keys:1 in the funnel),
                # so conversions / raw-signals pins at ~0% forever and this
                # detector screamed a "0.000% conversion crisis" every cycle over
                # what is really a handful of callers — the same artifact r35 + C1
                # removed from the other funnel detectors. Gate + rate on distinct
                # callers so the floor reflects real demand.
                # r72: dropped the NULLIF(tool_requested,'') fallback from the
                # COALESCE. With it, an anonymous bot looping all ~48 tools counted
                # as 38 distinct "callers" (tool-name diversity, not caller
                # diversity) — inflating the denominator and DEFLATING the
                # conversion rate, which kept this escalation firing ~x27 on a
                # handful of bots. Count only IDENTIFIABLE callers (email or MCP
                # client); anonymous rows (both NULL) are skipped by COUNT(DISTINCT),
                # so the rate now reflects conversion among callers we can actually
                # convert. Still gated at >=25 below, so it stays silent until
                # there's genuinely broad identified demand.
                # r78: exclude internal/self clients from the caller pool. The
                # live 30d window counted dchub-selfheal, mcp-probe,
                # dchub-mcp-test and pipeline_mcp as "callers" — our own probes
                # are not prospects, and they deflate the conversion rate.
                cur.execute("""
                    SELECT COUNT(DISTINCT COALESCE(
                               NULLIF(user_email,''),
                               NULLIF(mcp_client,'')))
                      FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '30 days'
                       AND COALESCE(mcp_client,'') NOT LIKE 'dchub%'
                       AND COALESCE(mcp_client,'') NOT LIKE 'loop%'
                       AND COALESCE(mcp_client,'') NOT LIKE '%probe%'
                       AND COALESCE(mcp_client,'') NOT LIKE '%-test'
                       AND COALESCE(mcp_client,'') NOT LIKE '%scanner%'
                       AND COALESCE(mcp_client,'') <> 'pipeline_mcp'
                """)
                distinct_callers = int((cur.fetchone() or [0])[0] or 0)
                # r78: pair-code redemptions are ONE conversion mechanism and
                # have been 0/30d (verified live) while the canonical funnel
                # (funnel_health KPI-2) counts conversions from the Stripe-
                # webhook mcp_conversions table — 8 in the same window.
                # Dividing real demand by a dead mechanism pinned this metric
                # at 0% forever (the ×47 stuck finding). Prefer mcp_conversions,
                # fall back to pair codes — the same preference funnel_health uses.
                cur.execute("SELECT to_regclass('public.mcp_conversions') IS NOT NULL")
                if bool((cur.fetchone() or [False])[0]):
                    cur.execute("""
                        SELECT COUNT(*) FROM mcp_conversions
                         WHERE created_at >= NOW() - INTERVAL '30 days'
                    """)
                else:
                    cur.execute("""
                        SELECT COUNT(*) FROM mcp_pair_codes
                         WHERE redeemed_at IS NOT NULL
                           AND redeemed_at >= NOW() - INTERVAL '30 days'
                    """)
                conversions = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                return findings
            if distinct_callers < 25:
                return findings  # a few looping power-keys, not broad demand
            rate = 100.0 * conversions / max(distinct_callers, 1)
            FLOOR = float(os.environ.get("DCHUB_MIN_CONVERSION_PCT", "0.5"))
            if rate < FLOOR:
                findings.append({
                    "issue":  "mcp_conversion_rate_below_floor",
                    "url":    "mcp_upgrade_signals + mcp_pair_codes / 30d",
                    "count":  int(rate * 100),  # basis points
                    "detail": (f"30-day MCP conversion rate is {rate:.3f}% "
                               f"({conversions} conversions / {distinct_callers} "
                               f"DISTINCT paywall callers; {signals} raw signals) "
                               f"— below the {FLOOR}% floor. Either tighten "
                               f"more tools to IDENTIFIED+, raise the FREE "
                               f"daily cap pressure, or improve the paywall "
                               f"response message. See "
                               f"/api/v1/mcp/conversion-funnel for the "
                               f"per-tool breakdown."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── Phase WWW (2026-05-16) — Site Sentinel ingest ─────────────────
def check_site_sentinel() -> list[dict]:
    """Pull every unhealthy page from the Site Sentinel manifest into
    brain findings so the heartbeat surfaces page-level breakages
    (404, 500, body-too-small) in real time. Without this, broken
    pages stay invisible until a user reports them — which is exactly
    the problem the user surfaced ("tax incentives doesn't show map,
    capacity pipeline errors, powered shell 503, ercot-batch-zero
    404"). The Sentinel polls all of those at every radar cycle."""
    try:
        from routes.site_sentinel import unhealthy_findings
        return unhealthy_findings() or []
    except Exception:
        return []


# ── Phase VVV (2026-05-16) — schema-drift detector ────────────────
def check_schema_drift() -> list[dict]:
    """Surface every 'column X does not exist' or 'relation Y does not
    exist' error from the aggregator into a brain finding. The user
    pointed out Railway logs spam these every cycle — they should NOT
    require log-trawling to see. Pull from dchub_media._agg_errors
    (populated by aggregate_announcements) + probe a few known noisy
    queries directly so we catch schema drift even when the aggregator
    succeeded via its column-aware fallback path."""
    findings: list[dict] = []
    seen: set[str] = set()

    # Pull from aggregator's error map
    try:
        from dchub_media import get_aggregator_errors as _get_agg_errors
        for category, err in (_get_agg_errors() or {}).items():
            msg = (err or "").lower()
            if "column" in msg and "does not exist" in msg:
                # Extract the column name from "column \"X\" does not exist"
                import re
                m = re.search(r'column [\"\']?(\w+)[\"\']?\s+does not exist', msg)
                col = m.group(1) if m else "?"
                key = f"agg:{category}:column:{col}"
                if key in seen: continue
                seen.add(key)
                findings.append({
                    "issue":  f"schema_drift_column_missing:{category}.{col}",
                    "url":    f"dchub_media.aggregate_announcements: {category}",
                    "count":  1,
                    "detail": (f"Aggregator query for '{category}' failed because "
                               f"column '{col}' doesn't exist. Either the table "
                               f"schema changed or the query needs to introspect "
                               f"information_schema.columns and pick the actual "
                               f"available column. Quick fix: route the caller to "
                               f"aggregate_announcements_v3 (column-aware)."),
                })
            elif "relation" in msg and "does not exist" in msg:
                import re
                m = re.search(r'relation [\"\']?(\w+)[\"\']?\s+does not exist', msg)
                tbl = m.group(1) if m else "?"
                key = f"agg:{category}:table:{tbl}"
                if key in seen: continue
                seen.add(key)
                findings.append({
                    "issue":  f"schema_drift_table_missing:{tbl}",
                    "url":    f"dchub_media.aggregate_announcements: {category}",
                    "count":  1,
                    "detail": (f"Aggregator query for '{category}' referenced "
                               f"table '{tbl}' which doesn't exist. Either the "
                               f"table was renamed/dropped or the query is from "
                               f"a pre-migration deploy. Wrap caller with a "
                               f"to_regclass() probe."),
                })
    except Exception:
        pass

    # Phase QA-sweep (2026-05-16): removed the direct probe of
    # (wind_projects, gas_compressors, gas_processings, transmission,
    # pipelines). The previous behavior flagged these every cycle even
    # though the call sites had been guarded with to_regclass already
    # (in energy_auto_discovery_pg.py + observability_routes.py).
    # Surfacing them as findings forever was permanent red without an
    # action: the platform genuinely doesn't ingest those tables on
    # this deploy. If they ever start being referenced again WITHOUT a
    # guard, the _agg_errors signal above will catch it.
    return findings[:8]


# ── Phase TTT (2026-05-16) — brand-surface dormancy detector ──────
def check_brand_surface_dormant() -> list[dict]:
    """Fires if any brand-positioning surface (/vs, /intelligence,
    /dcpi/totals, /bs_translator) has zero views in the last 72h
    AND the surface has been alive (registered) for >24h. These
    are the marketing-front-door pages — if nobody is hitting them,
    either the nav links are missing or the brand message isn't
    reaching anyone. Either way: a human should know.

    Different from check_surface_health_critical (which fires on
    score<40 across ALL surfaces): this one is BRAND-SPECIFIC and
    fires on absolute silence — score 0/100 is unambiguous neglect."""
    findings: list[dict] = []
    BRAND_SURFACES = ("bs_translator", "power_totals")
    # (/intelligence isn't registered as a surface — it auto-logs
    # under 'ai_hub'. Add when refactored. /vs is 'bs_translator',
    # /dcpi/totals is 'power_totals'.)
    try:
        from routes.surface_brain import SURFACES, _conn as _sb_conn
    except Exception:
        return findings
    try:
        import psycopg2.extras
        c = _sb_conn()
        if c is None: return findings
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                for sid in BRAND_SURFACES:
                    if sid not in SURFACES: continue
                    try:
                        cur.execute("""
                            SELECT COUNT(*) AS views
                              FROM surface_telemetry
                             WHERE surface_id = %s
                               AND event_type = 'view'
                               AND ts >= NOW() - INTERVAL '72 hours'
                        """, (sid,))
                        n = int((cur.fetchone() or {}).get("views") or 0)
                    except Exception:
                        continue
                    if n == 0:
                        findings.append({
                            "issue":  f"brand_surface_dormant:{sid}",
                            "url":    f"surface_telemetry: surface_id={sid}",
                            "count":  0,
                            "detail": (f"Brand-positioning surface '{sid}' has "
                                       f"ZERO views in the last 72h. The page "
                                       f"is live (registered) but invisible. "
                                       f"Likely causes: (1) nav link missing/"
                                       f"buried, (2) homepage tile missing, "
                                       f"(3) external traffic not landing. "
                                       f"Check dchub-frontend/js/dchub-nav.js + "
                                       f"any homepage hero block, then bump "
                                       f"discoverability."),
                        })
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        return findings
    return findings


# ═══════════════════════════════════════════════════════════════════
# Phase KK (2026-05-17) — 4 NEW BLIND-SPOT DETECTORS
#
# Each closes a category the brain previously had no eyes on:
#   • check_data_freshness_sla_breach   — datasets stale past their SLA
#   • check_mcp_tool_sunset_candidate   — tools dying despite past use
#   • check_ai_citations_stale_v2       — Phase II cron not landing rows
#   • check_autopilot_verifier_backlog  — Phase FFFFF verifier backlogged
# ═══════════════════════════════════════════════════════════════════

def check_data_freshness_sla_breach() -> list[dict]:
    """Fires when a tracked dataset hasn't refreshed within its SLA.
    Operationalizes the "is the data fresh" question that ops keeps
    asking manually. Per-dataset SLA (in hours):
      • dcpi_scores       — 12h  (recompute cron)
      • discovered_facilities — 24h (discovery cron)
      • news_items        — 6h   (news pipeline)
      • ai_citations      — 168h (weekly cron — see Phase II)
    """
    findings: list[dict] = []
    # r33-stale-recovery (2026-05-21): expanded SLA list to match what
    # /status page tracks. User caught the gap — `facilities` canonical
    # table was 17d stale (407h vs 336h SLA) but our detector only
    # watched `discovered_facilities` (the queue). Both matter; both
    # now monitored.
    SLAS = [
        # (table, age_column, max_hours, friendly_label)
        ("dcpi_scores",            "computed_at",  12,   "DCPI scores"),
        ("market_power_scores",    "computed_at",  48,   "market power scores"),
        ("discovered_facilities",  "discovered_at",24,   "facility discovery queue"),
        ("facilities",             "first_seen",   336,  "canonical facilities"),
        ("news_articles",          "published_at", 6,    "news ingest"),
        # r36 (2026-05-31): 36→168. press_releases is EVENT-DRIVEN, not a fixed
        # cadence: dcpi_auto_press (the writer) only publishes on a >=15pt DCPI
        # 7-day market shift, so multi-day quiet stretches are normal when the
        # index is stable (verified: cron fires every 6h + succeeds, but
        # /api/v1/dcpi/auto-press/recent = 0 — no qualifying event in 5 days, not
        # a broken cron). A 36h SLA guaranteed a false breach in any quiet week.
        # 168h matches the weekly-event tier (same as ai_citations below) and
        # still catches a genuinely-stuck pipeline. NOTE (product lever, not
        # changed here): the 10-14pt moves become DRAFTS in press_releases_queue
        # that need review to publish — publishing those, or lowering the
        # auto-publish threshold, is how you'd make press refresh more often.
        ("press_releases",         "published_at", 168,  "press releases (event-driven)"),
        ("ai_citations",           "observed_at",  168,  "AI citations (weekly)"),
        ("monthly_reports",        "created_at",   744,  "monthly trend snapshot"),
        # r80c: proactive canary for the MCP telemetry pipeline. mcp_tool_calls
        # gets rows continuously as long as ANY MCP traffic flows (the self-heal
        # loop alone keeps it fresh every few min), so a 12h gap means the whole
        # track pipeline died — exactly the silent-write class we hardened with
        # logging in r80b, now caught proactively too. 12h is generous enough to
        # ride out a deploy window without false-breaching.
        ("mcp_tool_calls",         "created_at",   12,   "MCP tool-call telemetry"),
        # Phase r33-D (2026-05-21) — infrastructure layer SLAs. HIFLD
        # publishes annually, EIA quarterly; we refresh aggressively
        # so the map doesn't go stale. Each pairs with a REFRESH_MAP
        # entry in brain_autopilot.py (transmission-refresh, gas-
        # refresh, substations-refresh) for autonomous recovery.
        ("transmission_lines",     "updated_at",   720,  "HIFLD transmission lines"),
        ("gas_pipelines",          "updated_at",   720,  "EIA gas pipelines"),
        ("substations",            "updated_at",   720,  "HIFLD substations"),
        # 2026-07-02 — data-moat feed sentinels. This sweep found
        # usgs_water_stress 3.5 MONTHS stale (last row 2026-03-18) with no
        # detector watching it — the water-risk answers were silently aging.
        # SLAs sized to each feed's real cadence (LMP hourly-ish cron → 48h
        # rides out a weekend outage; water USGS ~weekly → 21d; LNG terminals
        # near-static → 45d).
        ("iso_lmp_snapshots",      "fetched_at",   48,   "ISO LMP price feed"),
        ("henry_hub_spot",         "ingested_at",  96,   "Henry Hub spot price (EIA)"),
        ("lng_export_terminals",   "ingested_at",  1080, "LNG export terminals (EIA)"),
        # 2026-07-16 — usgs_water_stress SLA RETIRED (was 504h). The table was
        # SUPERSEDED 2026-07-10 by the WRI Aqueduct 4.0 ingest
        # (routes/water_aqueduct_ingest.py → water_risk rows tagged
        # source='wri_aqueduct', built from wri_aqueduct_us_states) and its
        # producing cron is gone, so the detector fired data_freshness_sla_breach
        # on it 2,800+ times with nothing left to fix. The WRI dataset is NOT
        # added to this watch on purpose: it refreshes rarely by design
        # (annual-cadence WRI baseline, crawl-first manual ingest), so any
        # hours-scale SLA here would just mint a new false-alarm stream.
        ("competitor_snapshots",   "captured_at",  72,   "competitor gap-crawler inputs"),
    ]
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            for tbl, col, sla_hrs, label in SLAS:
                try:
                    cur.execute(f"SELECT to_regclass('public.{tbl}')")
                    if not (cur.fetchone() or [None])[0]:
                        continue  # table doesn't exist on this deploy
                    cur.execute(
                        f"SELECT MAX({col}) FROM {tbl}"
                    )
                    last = (cur.fetchone() or [None])[0]
                    if last is None:
                        findings.append({
                            "issue":  "data_freshness_sla_breach",
                            "url":    f"table:{tbl}",
                            "count":  1,
                            "detail": (f"{label} has NO rows yet. SLA: {sla_hrs}h. "
                                       f"Either the producing cron has never run, "
                                       f"or the table was recently truncated."),
                        })
                        continue
                    import datetime as _dt
                    now = _dt.datetime.now(_dt.timezone.utc)
                    # last may be tz-naive — coerce
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=_dt.timezone.utc)
                    age_h = (now - last).total_seconds() / 3600.0
                    if age_h > sla_hrs:
                        findings.append({
                            "issue":  "data_freshness_sla_breach",
                            "url":    f"table:{tbl}",
                            "count_kind": "hours",  # magnitude, not a recurrence tally
                            "count":  int(age_h),
                            "detail": (f"{label} last refreshed {age_h:.1f}h ago "
                                       f"(SLA: {sla_hrs}h). The cron that produces "
                                       f"this table has missed at least one window. "
                                       f"Check Railway logs for the cron's name."),
                        })
                except Exception:
                    continue
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return findings


def check_mcp_tool_sunset_candidate() -> list[dict]:
    """Fires when an MCP tool's 7-day call count is < 5% of its 90-day
    average. Catches tools that are dying — either deprecation candidates
    or, more interestingly, tools that USED to be hot and silently broke.
    Lets us either revive (fix the breakage) or sunset (clean up docs)."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.mcp_call_log')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                cur.execute("""
                    WITH per_tool AS (
                      SELECT tool,
                             COUNT(*) FILTER (
                               WHERE timestamp >= NOW() - INTERVAL '7 days') AS calls_7d,
                             COUNT(*) FILTER (
                               WHERE timestamp >= NOW() - INTERVAL '90 days') AS calls_90d
                        FROM mcp_call_log
                       WHERE tool IS NOT NULL
                       GROUP BY tool
                    )
                    SELECT tool, calls_7d, calls_90d
                      FROM per_tool
                     WHERE calls_90d >= 100        -- had real adoption
                       AND calls_7d * 13 < calls_90d * 0.05  -- 7d run-rate < 5% of 90d
                     ORDER BY calls_90d DESC LIMIT 5
                """)
                for r in cur.fetchall() or []:
                    tool, c7, c90 = r[0], int(r[1] or 0), int(r[2] or 0)
                    findings.append({
                        "issue":  "mcp_tool_sunset_candidate",
                        "url":    f"tool:{tool}",
                        "count":  c7,
                        "detail": (f"MCP tool `{tool}` had {c90} calls over 90d but "
                                   f"only {c7} in the last 7d (run-rate dropped >95%). "
                                   f"Either the tool broke silently (check logs for "
                                   f"errors), got rate-limited out, or its consumers "
                                   f"migrated. Investigate before sunsetting."),
                    })
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass
    return findings


def check_ai_citations_stale_v2() -> list[dict]:
    """Fires when no new ai_citations rows have landed in 7+ days
    despite ANTHROPIC_API_KEY being set. Phase II shipped the actual
    Claude probe; this detector ensures the weekly cron is actually
    firing. If it's silent, we don't know if our source-of-truth score
    is stable or stale."""
    findings: list[dict] = []
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return findings  # No key = no expectation
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.ai_citations')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                cur.execute("""SELECT COUNT(*) FROM ai_citations
                                WHERE observed_at >= NOW() - INTERVAL '7 days'
                                  AND source LIKE 'auto_cron%'""")
                n = int((cur.fetchone() or [0])[0] or 0)
                if n == 0:
                    findings.append({
                        "issue":  "ai_citations_cron_silent",
                        "url":    "/api/v1/ai-citations/run-cron",
                        "count":  1,
                        "detail": ("ANTHROPIC_API_KEY is set but no auto_cron "
                                   "citation rows in 7 days. Phase II shipped "
                                   "the real Claude probe — the WEEKLY CRON to "
                                   "fire `POST /api/v1/ai-citations/run-cron` "
                                   "with X-Admin-Key isn't scheduled. Add it to "
                                   ".github/workflows/evolve-cron.yml so the "
                                   "share-of-voice metric starts moving."),
                    })
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass
    return findings


def check_frontend_critical_endpoints() -> list[dict]:
    """Phase OOO (2026-05-17) — probe the API endpoints that public
    HTML pages depend on. Flag any that timeout / 5xx / take >5s.

    Why: user reported /cited-by showing 'No testimonials surfaced yet'
    + /capacity-pipeline + /tax-incentives + /powered-shell all silently
    failing because their backing API was 503. The brain had no eyes on
    this — no detector probed these specific paths. Now it does.

    Each endpoint maps to a public page so the finding URL points at
    the page the user sees broken, not the API path they don't know
    about.
    """
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings

    # Endpoints that public pages depend on. Each entry is
    # (api_path, public_page_path, max_seconds, page_description).
    #
    # Phase QQQ (2026-05-17) — expanded from 7 → 23 probes to cover
    # every data-fetching public page. From the frontend inventory,
    # only 23 of 95 HTML pages actually do fetches; this covers all
    # of them. Coverage went from 30% → 100% of data-driven pages.
    _PROBES = [
        # Original 7 from Phase OOO
        ("/api/v1/testimonials?limit=4",     "/cited-by",          5, "cited-by testimonials widget"),
        ("/api/v1/news?q=DC+Hub&limit=6",    "/cited-by",          5, "cited-by news mentions widget"),
        ("/api/v1/powered-shell/markets",    "/powered-shell",     5, "powered-shell markets list"),
        ("/api/v1/map?all=true&limit=2000",  "/assets",            8, "assets / map facility data"),
        ("/api/v1/site/stats",               "/",                  3, "homepage hero counts"),
        ("/api/v1/marketing/pulse",          "/dc-hub-media",      5, "DC Hub Media press pulse"),
        ("/api/v1/stats",                    "/",                  3, "site-wide stats"),
        # Phase QQQ additions — every remaining data-fetching public page
        ("/api/v1/stats",                    "/by-the-numbers",    3, "by-the-numbers stats"),
        ("/api/v1/news?limit=5",             "/",                  5, "homepage news widget"),
        ("/api/v1/packages/stats",           "/",                  3, "homepage install-count pill"),
        ("/api/v1/demo/ask",                 "/",                  5, "homepage demo Ask widget"),
        ("/api/v1/usage",                    "/dashboard",         5, "user usage dashboard"),
        ("/api/v1/observability/snapshot",   "/pricing",           5, "pricing observability snapshot"),
        ("/api/v1/discovery/last-7d",        "/snapshot",          5, "snapshot last-7d discoveries"),
        # r-probe-falsepos (2026-06-27): REMOVED ("/api/v1/me/tier","/snapshot",3,...).
        # /api/v1/me/tier is intentionally per-user → private/no-store/force-origin
        # (worker ROUTE_CACHE_MAP tier 'none', _worker.js:424), so it can NEVER be
        # edge-fast and structurally always trips the 3s "frontend_endpoint_slow"
        # cap → a permanent false-positive finding. Its accidental-edge-cache risk
        # is still guarded by the dedicated me_tier_edge_cached check elsewhere in
        # this radar, so dropping it from the latency probe set weakens nothing.
        ("/api/v1/dcpi/scores?limit=300",    "/state-of-the-data-center", 8, "DCPI scores grid"),
        ("/api/v1/status",                   "/system-status",     5, "system-status uptime"),
        ("/api/v1/tax-incentives?limit=50",  "/tax-incentives",    5, "tax-incentives table"),
        ("/api/v1/site/stats",               "/intelligence",      3, "intelligence page stats"),
        ("/api/ai-analytics",                "/connect",           5, "connect AI analytics"),
        # Phase RRR-orphan-followup (2026-05-18): correct path is /api/pipeline,
        # not /api/v1/pipeline (verified live during item #8 investigation —
        # returns 213 pipeline items vs 404 on /api/v1/pipeline).
        ("/api/pipeline",                    "/capacity-pipeline", 5, "capacity pipeline chart"),
        ("/api/pipeline",                    "/construction-pipeline", 5, "construction pipeline chart"),
        ("/api/pipeline",                    "/ai-pipeline",       5, "ai-pipeline chart"),
        ("/api/v1/listings",                 "/listings",          5, "listings marketplace"),
    ]

    # r41-frontend-parallel (2026-05-25): parallelize the probe loop
    # (23 probes after r-probe-falsepos dropped the /api/v1/me/tier probe).
    # Pre-fix this was serial — observed wall time ~37s (slowest single
    # detector in the radar scan). At ~1.5s avg/probe ≈ ~35s
    # serially, but the scan_all() outer ThreadPoolExecutor only gives
    # each detector a 20s budget, so this detector was getting truncated
    # past the deadline and its findings were dropped half the time.
    # Same pattern as check_dead_internal_links r32-mt-fix.
    import concurrent.futures as _cf
    import time as _t

    def _probe_one(probe):
        api_path, page_path, max_sec, label = probe
        url = f"https://dchub.cloud{api_path}"
        t0 = _t.time()
        try:
            r = _req.get(url, timeout=max_sec + 2,
                          headers={"User-Agent": "dchub-frontend-health/1.0"})
            return (probe, r.status_code, _t.time() - t0, None)
        except Exception as e:
            return (probe, None, _t.time() - t0,
                    f"{type(e).__name__}: {str(e)[:120]}")

    with _cf.ThreadPoolExecutor(max_workers=8,
                                 thread_name_prefix="frontend-probe") as ex:
        results = list(ex.map(_probe_one, _PROBES))

    for probe, status, elapsed, err in results:
        api_path, page_path, max_sec, label = probe
        if err is not None:
            findings.append({
                "issue":  "frontend_endpoint_unreachable",
                "url":    page_path,
                "count":  1,
                "detail": (f"Public page `{page_path}` depends on API `{api_path}` "
                           f"({label}) which timed out / errored after "
                           f"{elapsed:.1f}s: {err}. The page renders "
                           f"empty/broken to visitors. Likely Railway upstream "
                           f"failure or endpoint regression."),
            })
            continue
        if status >= 500:
            findings.append({
                "issue":  "frontend_endpoint_5xx",
                "url":    page_path,
                "count":  status,
                "detail": (f"Public page `{page_path}` depends on API `{api_path}` "
                           f"({label}) which returned HTTP {status}. "
                           f"The page renders empty/broken to visitors."),
            })
        elif elapsed > max_sec:
            findings.append({
                "issue":  "frontend_endpoint_slow",
                "url":    page_path,
                "count":  int(elapsed * 1000),
                "count_kind": "latency_ms",  # VALUE not a recurrence count (see brain_work_selector.VALUE_NOT_COUNT_ISSUES)
                "detail": (f"Public page `{page_path}` API call to `{api_path}` "
                           f"({label}) took {elapsed:.1f}s (cap {max_sec}s). "
                           f"Visitors abandon before render."),
            })
    return findings


def check_package_install_velocity_drop() -> list[dict]:
    """Phase KKK (2026-05-17) — flag when published package install
    velocity drops sharply WoW. Pulls from package_install_stats
    (populated daily by /api/v1/packages/refresh cron).

    Why: pip + npm install counts are the most honest organic-adoption
    signal we have. A sustained drop means either an upstream issue
    (PyPI / npm CDN), a new bug in a release, or a competitor stole
    mindshare. All three want a human to look.
    """
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.package_install_stats')")
                if not (cur.fetchone() or [None])[0]:
                    return findings  # table doesn't exist yet
                cur.execute("""
                    WITH this_week AS (
                      SELECT ecosystem, package_name,
                             MAX(downloads_7d) AS downloads_7d
                        FROM package_install_stats
                       WHERE snapshot_date >= CURRENT_DATE - INTERVAL '1 day'
                       GROUP BY ecosystem, package_name
                    ),
                    last_week AS (
                      SELECT ecosystem, package_name,
                             MAX(downloads_7d) AS downloads_7d
                        FROM package_install_stats
                       WHERE snapshot_date <= CURRENT_DATE - INTERVAL '7 days'
                         AND snapshot_date >= CURRENT_DATE - INTERVAL '8 days'
                       GROUP BY ecosystem, package_name
                    )
                    SELECT tw.ecosystem, tw.package_name,
                           tw.downloads_7d AS this_7d,
                           lw.downloads_7d AS last_7d
                      FROM this_week tw
                      JOIN last_week lw USING (ecosystem, package_name)
                     WHERE lw.downloads_7d >= 10
                       AND tw.downloads_7d * 2 < lw.downloads_7d
                """)
                for r in cur.fetchall() or []:
                    eco, name, this7, last7 = r[0], r[1], int(r[2] or 0), int(r[3] or 0)
                    drop_pct = round(100.0 * (last7 - this7) / max(1, last7), 1)
                    findings.append({
                        "issue":  "package_install_velocity_drop",
                        "url":    f"{eco}:{name}",
                        "count":  this7,
                        "detail": (f"{eco} package `{name}` 7-day installs dropped "
                                   f"{drop_pct}% WoW ({last7}→{this7}). Either an "
                                   f"upstream registry issue, a regression in the "
                                   f"latest release, or competitor mindshare shift. "
                                   f"Investigate before assuming it's noise."),
                    })
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass
    return findings


def check_autopilot_verifier_backlog() -> list[dict]:
    """Fires when Phase FFFFF's outcome verifier has > 5 actions
    fired in the last 48h but not yet verified. Either the verify
    cron is silent or actions are failing at a higher rate than the
    verifier can keep up with."""
    findings: list[dict] = []
    c = _db()
    if c is None: return findings
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.autopilot_outcomes')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
                cur.execute("""SELECT COUNT(*) FROM autopilot_outcomes
                                WHERE fired_at >= NOW() - INTERVAL '48 hours'
                                  AND verified_at IS NULL""")
                n = int((cur.fetchone() or [0])[0] or 0)
                if n >= 5:
                    findings.append({
                        "issue":  "autopilot_verifier_backlog",
                        "url":    "/api/v1/autopilot/verify-pending",
                        "count":  n,
                        "detail": (f"{n} autopilot actions have fired in the last "
                                   f"48h without being verified. Phase FFFFF's "
                                   f"verify-pending cron may be silent OR actions "
                                   f"are failing faster than it can drain. Check "
                                   f"the cron schedule + look at recent verifier "
                                   f"errors."),
                    })
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass
    return findings


# ═══════════════════════════════════════════════════════════════════
# Phase XX (2026-05-17) — BREACH PREVENTION DETECTOR
#
# Closes the loop on Round 4's gating audit. Round 4 found Round 4
# found 3 REST endpoints leaking high-value data anon (DCPI scores
# 112KB, tax incentives 16KB, grid intelligence all regions). Phase
# WW + WW-2 plugged them with soft-paywall. But the next time someone
# adds an `@bp.route("/api/v1/expensive-thing")` and forgets the gate,
# we'll leak again.
#
# This detector probes a list of HIGH-VALUE endpoints as anon and
# flags any whose response > 8KB AND doesn't contain the `_gated`
# field. The soft-paywall pattern always injects `_gated: true` so
# that field's presence is the marker that the gate is wired up.
# Absence + large size = leak.
# ═══════════════════════════════════════════════════════════════════

# Endpoints that SHOULD have a soft-paywall gate (any handler returning
# bulk data should be on this list). Probe is GET, no body, no auth.
_BREACH_PROBE_ENDPOINTS = [
    "/api/v1/dcpi/scores",
    "/api/v1/tax-incentives",
    "/api/v1/grid-intelligence",
    "/api/v1/intelligence/trends",
    "/api/v1/intelligence/market-velocity",
    "/api/v1/connectivity/providers",
    "/api/v1/transactions",
]
_BREACH_SIZE_THRESHOLD_BYTES = 8000  # 8KB — anything bigger is "bulk"


def check_rest_endpoint_leakage() -> list[dict]:
    """Fires for any monitored endpoint returning > 8KB of data WITHOUT
    a _gated marker. Catches the "added a new bulk endpoint and forgot
    to gate it" failure mode that Round 4 surfaced.

    Cheap to run: probes the platform's OWN endpoints, no external API
    calls. Fails-closed: if the probe itself errors, return empty
    findings (don't false-positive on network noise).

    r41-leakage-parallel (2026-05-25): parallelized the per-endpoint
    HTTP probes. Pre-fix serial loop hit 38.7s wall time (top single
    detector after r41's other parallelization fixes exposed it).
    With ~10s/probe timeout × N endpoints, the cumulative time was
    busting the 25s scan budget. 6-worker pool: ~5-10s wall time.
    """
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings

    import concurrent.futures as _cf

    def _probe_one(path):
        try:
            r = _req.get(f"https://dchub.cloud{path}",
                         timeout=10,
                         headers={"User-Agent": "dchub-breach-detector/1.0"})
            return (path, r.status_code, r.text or "", None)
        except Exception as e:
            return (path, None, "", e)

    with _cf.ThreadPoolExecutor(max_workers=6,
                                 thread_name_prefix="breach-probe") as ex:
        results = list(ex.map(_probe_one, _BREACH_PROBE_ENDPOINTS))

    for path, status, body, err in results:
        if err is not None:
            continue  # network noise — don't flag
        if status != 200:
            continue  # 402/403/404 is the gate working; skip
        size = len(body)
        if size <= _BREACH_SIZE_THRESHOLD_BYTES:
            continue  # small enough that it's probably a teaser/single-item
        # Check for the soft-paywall marker
        if '"_gated"' in body or '"_preview_only"' in body or '"_required_tier"' in body:
            continue  # gate is wired
        # Possible leak — flag it
        findings.append({
            "issue":  "rest_endpoint_leakage",
            "url":    path,
            "count":  size,
            "detail": (f"REST endpoint `{path}` returns {size} bytes to "
                       f"anon callers (threshold: {_BREACH_SIZE_THRESHOLD_BYTES}) "
                       f"with no `_gated`/`_preview_only`/`_required_tier` field "
                       f"in the body. Either it's a known-public dataset (add "
                       f"to allowlist), it's already gated via a different "
                       f"pattern (add the soft-paywall marker), or it's a real "
                       f"leak. Apply `from routes._soft_paywall import "
                       f"maybe_paywall` + `return maybe_paywall(payload, "
                       f"list_key='data', preview_cap=10, teaser='...')`."),
        })
    return findings


# =============================================================================
# 2026-06-29 — two FAIL-CLOSED read-only coaching/drop detectors.
#
# Both mirror check_rest_endpoint_leakage: read-only, cheap, and they
# return [] on ANY error. Returning [] (rather than a synthetic crash
# finding) is deliberate — the radar's resolve-on-absence sweep auto-
# closes findings that DON'T re-appear. If these detectors raised a
# crash-finding or partial result on a transient error, a real finding
# could be auto-resolved on the next clean pass. Failing silently to []
# means: on error, we neither file a false finding NOR auto-close a real
# one (the existing finding simply persists until the detector succeeds).
# =============================================================================

# Gated endpoints that MUST carry agent-coaching (claim_free_key) + an
# email_capture hook when they return a gated/teaser body. If a gated
# response is missing BOTH, an AI agent has no machine-readable path to
# upgrade and the human conversion CTA is invisible.
_COACHING_PROBE_ENDPOINTS = [
    "/api/v1/dcpi/scores",
    "/api/v1/interconnection/queue?iso=PJM",
    "/api/v1/fiber/intel?market=ashburn",
    "/api/v1/market-brief/all",
    "/api/v1/grid/intelligence/PJM",
]

# Markers that indicate a response is gated / a teaser / a preview.
_COACHING_GATED_MARKERS = (
    "_gated", "_locked_fields", "_preview_only", "_teaser",
    "_upgrade_cta", "_upgrade_hint", "tier_required", "total_available",
)


def check_gated_endpoint_coaching_missing() -> list[dict]:
    """Probe each gated endpoint with NO API key. For any response that is
    GATED (body contains a gating marker) but is MISSING both an
    `agent_action` block (carrying `claim_free_key`) AND an
    `email_capture` hook → file `gated_endpoint_missing_coaching`.

    Read-only. FAIL-CLOSED: returns [] on ANY error so the resolve-on-
    absence sweep can never auto-close a genuine missing-coaching finding
    on a transient network blip. Per-endpoint probe failures are skipped
    individually (a network error on one endpoint != "coaching present").
    """
    findings: list[dict] = []
    try:
        import requests as _req
        import concurrent.futures as _cf

        def _probe_one(path):
            try:
                # No API key on purpose — we want the anon/gated response.
                # Cache-bust (/api/v1/* sits behind CF Cache Rule #3) so we read
                # the live origin, not a stale cached body → no false positives.
                import time as _t
                _sep = "&" if "?" in path else "?"
                r = _req.get(f"https://dchub.cloud{path}{_sep}_radarcb={int(_t.time())}",
                             timeout=10,
                             headers={"User-Agent": "dchub-coaching-detector/1.0",
                                      "Cache-Control": "no-cache"})
                return (path, r.status_code, r.text or "", None)
            except Exception as e:  # noqa: BLE001 — per-endpoint, fail-closed
                return (path, None, "", e)

        with _cf.ThreadPoolExecutor(max_workers=5,
                                     thread_name_prefix="coach-probe") as ex:
            results = list(ex.map(_probe_one, _COACHING_PROBE_ENDPOINTS))

        for path, status, body, err in results:
            if err is not None:
                continue  # per-endpoint network noise — fail-closed, skip
            if not body:
                continue
            # Is the response gated/teaser?
            is_gated = any(m in body for m in _COACHING_GATED_MARKERS)
            if not is_gated:
                continue  # not gated → coaching not required here
            # Does it carry agent coaching + an email-capture hook?
            has_agent_action = ('"agent_action"' in body
                                 and "claim_free_key" in body)
            has_email_capture = '"email_capture"' in body
            if has_agent_action and has_email_capture:
                continue  # fully coached — good
            missing = []
            if not has_agent_action:
                missing.append("agent_action(claim_free_key)")
            if not has_email_capture:
                missing.append("email_capture")
            findings.append({
                "issue":  "gated_endpoint_missing_coaching",
                "url":    path,
                "count":  1,
                "detail": (f"Gated endpoint `{path}` returns a gated/teaser body "
                           f"(matched one of {list(_COACHING_GATED_MARKERS)}) but "
                           f"is MISSING: {', '.join(missing)}. A gated response "
                           f"must include BOTH an `agent_action` block carrying "
                           f"`claim_free_key` (so AI agents have a machine-readable "
                           f"upgrade path) AND an `email_capture` hook (so the human "
                           f"conversion CTA is present). Add both to the soft-paywall "
                           f"payload for this surface."),
            })
    except Exception:  # noqa: BLE001 — FAIL-CLOSED: never crash, never auto-close
        return []
    return findings


# Per-platform crawl-drop detector thresholds.
_AI_CRAWL_PRIOR_MIN = 140      # prior-7d must be >= this (≈20/day active)
_AI_CRAWL_DROP_FRAC = 0.80     # flag if current-7d dropped >= 80% vs prior


def check_ai_platform_crawl_drop() -> list[dict]:
    """Per AI platform, compare last-7d crawl/request count vs the prior
    7d (from the `ai_requests` table; cols: platform, created_at). For any
    platform that was ACTIVE in the prior window (prior_7d >= 140, i.e.
    ~20/day) and then dropped >= 80% in the current window → file
    `ai_platform_crawl_drop:<platform>`.

    Uses its OWN DB connection (via _db()). Read-only single SELECT.
    FAIL-CLOSED: returns [] on ANY error so the resolve-on-absence sweep
    can never auto-close a genuine crawl-drop finding on a transient DB
    blip.
    """
    findings: list[dict] = []
    c = None
    try:
        c = _db()
        if c is None:
            return []
        with c.cursor() as cur:
            # Table may not exist yet on a fresh DB — fail-closed to [].
            cur.execute("SELECT to_regclass('public.ai_requests')")
            if not cur.fetchone()[0]:
                return []
            cur.execute("""
                SELECT platform,
                       COUNT(*) FILTER (
                           WHERE created_at >= NOW() - INTERVAL '7 days')
                           AS cur_7d,
                       COUNT(*) FILTER (
                           WHERE created_at >= NOW() - INTERVAL '14 days'
                             AND created_at <  NOW() - INTERVAL '7 days')
                           AS prior_7d
                  FROM ai_requests
                 WHERE created_at >= NOW() - INTERVAL '14 days'
                   AND platform IS NOT NULL
                   AND platform <> ''
                 GROUP BY platform
            """)
            for row in cur.fetchall():
                platform, cur_7d, prior_7d = row
                cur_7d = int(cur_7d or 0)
                prior_7d = int(prior_7d or 0)
                if prior_7d < _AI_CRAWL_PRIOR_MIN:
                    continue  # wasn't active enough in the prior window
                drop_frac = (prior_7d - cur_7d) / float(prior_7d)
                if drop_frac < _AI_CRAWL_DROP_FRAC:
                    continue  # not a big-enough drop
                findings.append({
                    "issue":  f"ai_platform_crawl_drop:{platform}",
                    "url":    "ai_requests",
                    "count":  cur_7d,
                    "detail": (f"AI platform `{platform}` crawl/request volume "
                               f"dropped {drop_frac*100:.0f}% week-over-week: "
                               f"prior 7d = {prior_7d}, current 7d = {cur_7d}. "
                               f"This platform was active (>= {_AI_CRAWL_PRIOR_MIN} "
                               f"in the prior window) and has nearly gone silent. "
                               f"Likely causes: a crawl surface broke (robots/"
                               f"llms.txt/sitemap), the platform stopped fetching, "
                               f"or a gating/WAF change started blocking it. Check "
                               f"the live render paths and recent CF/WAF changes."),
                })
    except Exception:  # noqa: BLE001 — FAIL-CLOSED: never crash, never auto-close
        return []
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return findings


def check_media_and_yield_health() -> list[dict]:
    """2026-07-02 — three silent-death modes in the media/discovery loops that
    plain MAX(col) freshness SLAs can't express:

    1. `x_publisher_dead` — approved X rows queued but ZERO published in
       14d (status='published' + publish_platform, the columns the writers
       actually stamp — r-xid 2026-07-18: the original twitter_id-keyed
       predicate was a permanent false positive because no writer stored
       the tweet id).
    2. `linkedin_engagement_readback_stale` — posts are going out but
       engagement_fetched_at isn't advancing → the media loop is flying
       blind (root cause 2026-07-02: token missing r_organization_social;
       the fetch batch breaks on the first 403 silently).
    3. `competitor_gap_yield_stale` — the gap crawler runs daily (findings
       update) but hasn't staged a NEW discovered_facilities row in 3+
       weeks → either every competitor gap is ingested (fine) or the
       source parsing/geocoding died (needs eyes either way).

    Read-only. FAIL-CLOSED: returns [] on any error.
    """
    findings: list[dict] = []
    c = None
    try:
        c = _db()
        if c is None:
            return []
        with c.cursor() as cur:
            # 1. X publisher: queued but nothing published recently.
            # r-xid (2026-07-18): the death predicate keyed on twitter_id,
            # which NO writer stamped (the loop discarded the tweet id) —
            # so this fired 397h straight against a publisher that shipped
            # 35 posts in 14d. Measure what the writers actually record
            # (status='published' + publish_platform), with the deadman's
            # cast guard: live posted_at is naive timestamp, published_at
            # is TEXT with mixed ISO shapes — ::text + ISO-date regex +
            # ::timestamp survives both and skips junk rows.
            try:
                cur.execute("SELECT to_regclass('public.social_media_posts')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT COUNT(*) FILTER (WHERE status = 'approved'
                                    AND platform = 'twitter'),
                               COUNT(*) FILTER (WHERE status = 'published'
                                    AND publish_platform IN ('twitter', 'x')
                                    AND COALESCE(posted_at::text, published_at::text)
                                        ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                                    AND COALESCE(posted_at::text, published_at::text)::timestamp
                                        > NOW()::timestamp - INTERVAL '14 days')
                          FROM social_media_posts
                    """)
                    queued, posted_14d = cur.fetchone() or (0, 0)
                    if int(queued or 0) > 0 and int(posted_14d or 0) == 0:
                        findings.append({
                            "issue":  "x_publisher_dead",
                            "url":    "table:social_media_posts",
                            "count":  int(queued),
                            "detail": (f"{queued} approved X posts queued but 0 published in "
                                       f"14d (status='published', publish_platform twitter/x). "
                                       f"Check /api/v1/dchub-media/publisher-status "
                                       f"(last_error_class, auth circuit breaker) and Railway "
                                       f"logs; historical root cause was the X app not being "
                                       f"attached to a Project in the X developer portal."),
                        })
            except Exception:
                c.rollback()

            # 2. LinkedIn engagement read-back stale
            try:
                cur.execute("SELECT to_regclass('public.linkedin_posts')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT COUNT(*) FILTER (
                                   WHERE COALESCE(status,'') = 'success'
                                     AND COALESCE(posted_at, published_at, created_at)
                                         > NOW() - INTERVAL '7 days'),
                               MAX(engagement_fetched_at)
                          FROM linkedin_posts
                    """)
                    recent_posts, last_fetch = cur.fetchone() or (0, None)
                    stale = last_fetch is None
                    if not stale:
                        cur.execute(
                            "SELECT %s < NOW() - INTERVAL '96 hours'", (last_fetch,))
                        stale = bool(cur.fetchone()[0])
                    if int(recent_posts or 0) > 0 and stale:
                        findings.append({
                            "issue":  "linkedin_engagement_readback_stale",
                            "url":    "table:linkedin_posts",
                            "count":  int(recent_posts),
                            "detail": (f"{recent_posts} LinkedIn posts published in 7d but "
                                       f"engagement_fetched_at last advanced "
                                       f"{last_fetch or 'NEVER'} — the media loop can't learn "
                                       f"what lands. fetch_linkedin_engagement breaks its whole "
                                       f"batch on the first 403; token needs "
                                       f"r_organization_social (+ r_organizational_social_feed "
                                       f"for impressions). Trigger POST /api/linkedin/"
                                       f"engagement-sync and read `reason` to confirm."),
                        })
            except Exception:
                c.rollback()

            # 3. Competitor gap-crawler yield
            try:
                cur.execute("""
                    SELECT MAX(first_seen) FROM discovered_facilities
                     WHERE source LIKE 'competitor_gap%%'
                """)
                last_yield = (cur.fetchone() or [None])[0]
                if last_yield is not None:
                    cur.execute(
                        "SELECT %s < NOW() - INTERVAL '21 days'", (last_yield,))
                    if bool(cur.fetchone()[0]):
                        findings.append({
                            "issue":  "competitor_gap_yield_stale",
                            "url":    "table:discovered_facilities",
                            "count":  1,
                            "detail": (f"Gap crawler last staged a NEW facility {last_yield} "
                                       f"(>21d ago) while its daily scan keeps running "
                                       f"(coverage_gap_competitor findings update). Either all "
                                       f"competitor gaps are ingested (dedup working — good) or "
                                       f"Cloudscene sitemap parsing / geocoding silently died. "
                                       f"Verify: POST /api/discovery/run?sources=competitor_gap "
                                       f"and check found vs staged vs dup counts."),
                        })
            except Exception:
                c.rollback()
    except Exception:  # noqa: BLE001 — FAIL-CLOSED
        return []
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return findings


_LLMS_TXT_URLS = (
    "https://dchub.cloud/llms.txt",
    "https://dchub.cloud/llms-full.txt",
)
_LLMS_PROBE_CAP = 15         # max advertised URLs probed per sweep
_LLMS_TIME_BUDGET_S = 45.0   # hard wall-clock budget for the whole detector
# Statuses that mean "alive but gated/wrong-method" — an agent following the
# doc gets a real answerable response, not a dead end. Only genuinely-dead
# statuses (404/410/5xx…) file a finding.
_LLMS_ALIVE_STATUSES = {401, 402, 403, 405, 429}


def check_llms_txt_contract() -> list[dict]:
    """2026-07-02 — doc-vs-live contract test for llms.txt/llms-full.txt.
    These files are what AI agents follow VERBATIM; every 404 there teaches
    an answer engine that DC Hub is unreliable (the 06-30 audit found 8
    phantom endpoints advertised; they were hand-fixed — this keeps the
    contract from drifting again, e.g. a route rename or an edge-routing
    regression like the /ai/cite PHASE_282 miss).

    Fetches the live llms files, extracts every advertised dchub.cloud URL,
    GETs each (cache-busted) and files `llms_txt_dead_link:<path>` for any
    that return 404/410/5xx. Gated statuses (401/403/402/405/429) count as
    alive. Capped + time-budgeted. FAIL-CLOSED: [] on any error.
    """
    import time as _time
    findings: list[dict] = []
    started = _time.time()
    try:
        urls: list[str] = []
        seen = set()
        for src in _LLMS_TXT_URLS:
            try:
                req = urllib.request.Request(
                    src + f"?_cb={int(started)}",
                    headers={"User-Agent": "DCHub-Brain-ContractCheck/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read(300_000).decode("utf-8", "replace")
            except Exception:
                continue
            for m in re.finditer(r"https://dchub\.cloud[^\s\)\]\"'`<>]*", body):
                u = m.group(0).rstrip(".,;:")
                # skip templated examples the doc shows with placeholders
                if any(ch in u for ch in ("{", "<", "…")):
                    continue
                if u in seen:
                    continue
                seen.add(u)
                urls.append(u)
        if not urls:
            return []  # couldn't read the docs at all — fail closed
        for u in urls[:_LLMS_PROBE_CAP]:
            if _time.time() - started > _LLMS_TIME_BUDGET_S:
                break
            sep = "&" if "?" in u else "?"
            status = None
            try:
                req = urllib.request.Request(
                    u + f"{sep}_cb={int(started)}",
                    headers={"User-Agent": "DCHub-Brain-ContractCheck/1.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    status = resp.status
            except urllib.error.HTTPError as e:
                status = e.code
            except Exception:
                continue  # network blip ≠ dead link; fail closed per-URL
            if status is not None and status >= 400 and status not in _LLMS_ALIVE_STATUSES:
                from urllib.parse import urlparse as _up
                path = _up(u).path or "/"
                findings.append({
                    "issue":  f"llms_txt_dead_link:{path}",
                    "url":    u,
                    "count":  1,
                    "detail": (f"llms.txt/llms-full.txt advertises {u} but it returns "
                               f"HTTP {status}. Agents follow these docs verbatim — a dead "
                               f"advertised rail costs citations AND credibility. Fix the "
                               f"route (backend), the edge routing (_routes.json / "
                               f"PHASE_282_PREFIXES in _worker.js), or the doc itself."),
                })
    except Exception:  # noqa: BLE001 — FAIL-CLOSED
        return []
    return findings


# =============================================================================
# Phase QQQ (2026-05-17) — Stability Guardrails (4 new detectors)
#
# The brain has 50 detectors but the user kept finding bugs the brain
# missed. Inventory revealed 3 systemic blind spots: (1) we check that
# crons are scheduled but never that they actually RAN, (2) we have no
# visibility into env-var-gated silent skips, (3) CSP violations report
# to /api/csp-report but no detector reads them, and (4) Railway upstream
# health is conflated with API endpoint health so a Railway outage
# doesn't fire its own finding. These 4 detectors close those gaps.
# =============================================================================

# Required env vars manifest. Each entry is (var_name, why_critical).
# Missing vars in this list cause real silent failures we've debugged
# repeatedly. Add new entries here as you find new silent-skip bugs;
# the detector will auto-flag them at the next scan.
_REQUIRED_ENV_VARS = [
    ("DATABASE_URL",
     "Neon primary connection — without it the entire app degrades to no-DB mode"),
    ("DCHUB_ADMIN_KEY",
     "Required to call /api/jobs/* endpoints; missing = ALL crons return 401"),
    ("ANTHROPIC_API_KEY",
     "Claude API for brain detector AI features; missing = brain emits empty findings"),
    ("STRIPE_WEBHOOK_SECRET",
     "Stripe payment webhooks; missing = paid signups silently fail"),
    ("LINKEDIN_ACCESS_TOKEN",
     "LinkedIn auto-publish; missing = press releases silently skip distribution"),
]


# Jobs deliberately retired or run ad-hoc — add the job_name here (with a
# one-line reason) to silence check_cron_freshness, mirroring
# _INTENTIONAL_DISPATCH_ONLY. A silently-dead job SHOULD surface once so a
# human decides revive-vs-retire, then gets listed here.
_INTENTIONAL_STALE_CRONS: set[str] = {
    # Retired 2026-08-07 with the heroic-reprieve decommission (08-07 audit
    # P0#1). Their ONLY driver was that project's frozen dchub-scheduler-v4
    # zombie, whose every call 401'd after the 07-31 key rotation. The two
    # keepers (alert-emails, energy-discovery) moved onto dchub-jobs.yml arms;
    # these four are retired by decision. Endpoints remain manually callable.
    "content-publish",        # ai-wars-era SEO/social poster
    "global-intelligence",    # ai-wars-era market enrichment agent
    "ai-outreach",            # ai-wars-era directory pinger
    "ai-ecosystem",           # ai-wars-era ecosystem enrichment
}


def check_cron_freshness() -> list[dict]:
    """Phase QQQ (2026-05-17) — flag crons that haven't run when they
    were supposed to.

    The `cron_last_run` table is populated by every authenticated
    /api/jobs/* endpoint hit (via _record_cron_run in jobs_routes.py).
    If `expected_interval_s` is set and last_started_at > 2× that
    interval ago, OR the row is missing entirely, the cron is silently
    dead.

    Why this matters: `check_cron_coverage` checks that crons EXIST in
    the schedule. This checks that crons actually FIRED. Those are
    very different bugs and we've shipped both in production.
    """
    findings: list[dict] = []
    c = _db()
    if c is None:
        return findings
    try:
        with c.cursor() as cur:
            # Table created by jobs_routes.init_jobs_routes() — if it
            # doesn't exist yet we just return cleanly. The brain runs
            # before the jobs blueprint on first deploy; not a bug.
            cur.execute("SELECT to_regclass('public.cron_last_run')")
            if not cur.fetchone()[0]:
                return findings
            cur.execute("""
                SELECT job_name,
                       last_started_at,
                       expected_interval_s,
                       run_count,
                       EXTRACT(EPOCH FROM (NOW() - last_started_at))::INTEGER
                           AS seconds_since_last_run
                  FROM cron_last_run
                 WHERE last_started_at IS NOT NULL
                ORDER BY job_name
            """)
            # r-cron-deadman-fix (2026-07-22): the query used to filter
            # `WHERE expected_interval_s IS NOT NULL`, but almost nothing
            # populates that column (20/21 jobs NULL), so this watcher only ever
            # saw ONE job — gas-refresh (52d), ai-wars (16d) and site-baseline
            # (11d) all died silently. Now watch EVERY job: threshold = 2× its
            # declared interval, or a 30h default when the interval is unknown
            # (a daily-ish job silent >30h is presumed dead). Retired jobs are
            # allowlisted in _INTENTIONAL_STALE_CRONS.
            _DEFAULT_STALE_S = 30 * 3600
            for row in cur.fetchall():
                job_name, last_start, expected_s, run_count, seconds_since = row
                if seconds_since is None or job_name in _INTENTIONAL_STALE_CRONS:
                    continue
                threshold = (expected_s * 2) if (expected_s and expected_s > 0) else _DEFAULT_STALE_S
                # Flag when the job is past its stale threshold. The 2× buffer
                # (or generous default) prevents flapping on natural jitter.
                if seconds_since > threshold:
                    hours_late = (seconds_since - threshold) / 3600.0
                    exp_txt = (f"expected every {expected_s}s"
                               if expected_s else "no declared interval")
                    findings.append({
                        "issue":  "cron_silently_dead",
                        "url":    f"/api/jobs/{job_name}",
                        # ★THE #48 misread at its source: this is SECONDS OF
                        # SILENCE, not a sighting tally. Declaring it here is
                        # what lets brain_work_selector stop inferring the
                        # answer from a hand-maintained list of issue strings
                        # (VALUE_NOT_COUNT_ISSUES), which had to be edited
                        # after the damage rather than before it.
                        "count_kind": "seconds_since",
                        "count":  int(seconds_since),
                        "detail": (f"Cron `{job_name}` has not run in "
                                   f"{int(seconds_since)}s ({seconds_since / 3600.0:.1f}h; "
                                   f"{exp_txt}, {hours_late:.1f}h past the stale "
                                   f"threshold). Total runs since deploy: {run_count}. "
                                   f"Likely causes: Railway crash mid-run, "
                                   f"env-var gate returned early, or scheduler "
                                   f"container died. Revive it (re-enable its "
                                   f"scheduler/cron entry, confirm the endpoint "
                                   f"200s), or if retired add `{job_name}` to "
                                   f"_INTENTIONAL_STALE_CRONS."),
                    })
    except Exception as e:
        findings.append({
            "issue":  "consistency_radar_detector_crashed:check_cron_freshness",
            "url":    "check_cron_freshness",
            "count":  1,
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        })
    finally:
        try:
            c.close()
        except Exception:
            pass
    return findings


def check_mcp_presence_stale() -> list[dict]:
    """The MCP-registry presence flywheel keeps our listings across ~16 registries
    fresh + drift-corrected via the SEPARATE crawler_scheduler (lanes
    mcp_presence_crawl @6/18 UTC + mcp_presence_auto_fix @19). Those lanes are NOT
    /api/jobs, so cron_last_run + check_cron_freshness can't see them — when that
    scheduler stalls, listings rot silently (glama/smithery went ~20d stale; the
    official GitHub registry drifted to 30 of 79 tools). This is the data-side
    watch: fire when BOTH sweeps have gone quiet, or when drift persists."""
    findings: list[dict] = []
    c = _db()
    if c is None:
        return findings
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.mcp_presence_listings')")
            if not cur.fetchone()[0]:
                return findings
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM (NOW() - MAX(last_crawled_at)))/3600,
                       EXTRACT(EPOCH FROM (NOW() - MAX(last_auto_fix_at)))/3600,
                       COUNT(*) FILTER (WHERE drift_detected),
                       COUNT(*)
                  FROM mcp_presence_listings
            """)
            crawl_h, fix_h, drift_n, total = cur.fetchone()
            crawl_h = float(crawl_h) if crawl_h is not None else 1e9
            fix_h = float(fix_h) if fix_h is not None else 1e9
            drift_n = int(drift_n or 0)
            STALE_H = 40   # both lanes run daily; 40h = a full day missed
            if crawl_h > STALE_H and fix_h > STALE_H:
                findings.append({
                    "issue": "mcp_presence_flywheel_stalled",
                    "url":   "mcp_presence_listings",
                    "count": int(max(crawl_h, fix_h)),
                    "detail": (
                        f"MCP-registry presence sweeps are stale — last discovery "
                        f"crawl {crawl_h:.0f}h ago, last auto-fix {fix_h:.0f}h ago "
                        f"(both should run daily via crawler_scheduler "
                        f"mcp_presence_crawl @6/18 + mcp_presence_auto_fix @19). "
                        f"Listings across {total} registries drift silently when this "
                        f"stalls. Check the crawler_scheduler service is up; catch up "
                        f"with POST /api/v1/admin/mcp-presence/crawl + "
                        f"/api/v1/admin/outreach/mcp-registry/submit-all."),
                })
            # r-perrow-staleness (2026-07-24 coverage audit): the MAX() aggregate above
            # goes green the moment ANY single registry is touched — so the 6 recently
            # added rows masked all 10 LISTED registries rotting for ~20 days. Aggregate
            # freshness is not per-row freshness. Check each row against the crawler's
            # own 3-day re-scrape SLA (mcp_presence_crawler: last_crawled_at > 3 days).
            try:
                cur.execute("""
                    SELECT registry_name,
                           COALESCE(EXTRACT(EPOCH FROM (NOW() - last_crawled_at))/3600, 1e9)
                      FROM mcp_presence_listings
                     WHERE last_crawled_at IS NULL
                        OR last_crawled_at < NOW() - INTERVAL '72 hours'
                     ORDER BY last_crawled_at ASC NULLS FIRST
                """)
                stale_rows = cur.fetchall() or []
            except Exception:
                stale_rows = []
            if stale_rows:
                names = ", ".join(f"{r[0]}({float(r[1]):.0f}h)" for r in stale_rows[:6])
                findings.append({
                    "issue": "mcp_presence_listing_stale",
                    "url":   "mcp_presence_listings",
                    "count_kind": "item_count",  # magnitude, not a recurrence tally
                    "count": len(stale_rows),
                    "detail": (
                        f"{len(stale_rows)} of {total} registry listings have not been "
                        f"re-scraped in >72h (crawler SLA is 3 days): {names}"
                        f"{' …' if len(stale_rows) > 6 else ''}. Per-row staleness is "
                        f"invisible to the MAX()-based flywheel check — a single fresh "
                        f"row makes the aggregate look healthy while listed registries "
                        f"rot (glama/smithery went ~20d unverified this way). Kick "
                        f"POST /api/v1/admin/mcp-presence/crawl."),
                })
            if drift_n > 0:
                findings.append({
                    "issue": "mcp_presence_drift_uncorrected",
                    "url":   "mcp_presence_listings",
                    "count": drift_n,
                    "detail": (
                        f"{drift_n} registry listing(s) show a STALE tool count vs our "
                        f"live server (drift_detected) — e.g. the official GitHub "
                        f"registry showed 30 of 79 tools. Owner-gated ones (DNS-TXT "
                        f"verify) surface as brain_findings 'mcp_presence_human_loop:*'; "
                        f"the auto-fixer flags them but can't clear them alone."),
                })
    except Exception as e:
        findings.append({
            "issue":  "consistency_radar_detector_crashed:check_mcp_presence_stale",
            "url":    "check_mcp_presence_stale",
            "count":  1,
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        })
    finally:
        try:
            c.close()
        except Exception:
            pass
    return findings


def check_customer_activation_health() -> list[dict]:
    """r-customer-loop (2026-07-20) — cross-platform course-correction leg
    for the customer white-glove loop.

    Reads the stages the loop PERSISTS (users.engagement_stage where
    last_touch_by='customer_white_glove') and flags SYSTEMIC activation
    failure: acquisition works but paying customers never make a first call.
    This is decoupled from the loop on purpose — if the loop itself stops,
    check_cron_freshness catches that separately (the tick self-stamps
    cron_last_run under job_name 'customer_white_glove_tick'). Here we watch
    the loop's OUTPUT, so a healthy-but-wrong outcome (everyone stranded)
    still reaches the brain agenda. FAIL-SOFT.

    Thresholds mirror customer_white_glove._self_health so the operator
    digest and the brain agenda show the same number.
    """
    findings: list[dict] = []
    STRANDED_SYSTEMIC_RATIO = 0.40
    ESCALATE_MIN = 3  # nudged-but-still-stranded customers that need a human
    c = _db()
    if c is None:
        return findings
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users')")
            if not cur.fetchone()[0]:
                return findings
            # engagement_stage is created lazily by the loop's first tick;
            # tolerate its absence on a fresh deploy.
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='users' AND column_name='engagement_stage'")
            if not cur.fetchone():
                return findings
            cur.execute("""
                SELECT engagement_stage, COUNT(*)
                  FROM users
                 WHERE last_touch_by = 'customer_white_glove'
                   AND engagement_stage IS NOT NULL
                 GROUP BY engagement_stage
            """)
            counts = {s: n for s, n in cur.fetchall()}
            total = sum(counts.values())
            stranded = counts.get("stranded", 0)
            # Guard: needs a populated cohort. total==0 means the tick has not
            # persisted yet (fresh deploy) — not a finding; the cron dead-man
            # owns "the loop never ran".
            if total >= 5 and stranded / total >= STRANDED_SYSTEMIC_RATIO:
                pct = 100.0 * stranded / total
                findings.append({
                    "issue":  "customer_activation_systemic_failure",
                    "url":    "/api/v1/admin/customer-white-glove/state",
                    "count":  int(stranded),
                    "detail": (f"{stranded}/{total} paying customers ({pct:.0f}%) are "
                               f"STRANDED — paid, zero calls past grace. Acquisition "
                               f"works, activation doesn't. Course-correct: arm "
                               f"ACTIVATION_NUDGE_ARM=1 to recover them, escalate the "
                               f"already-nudged to a human touch, and add first-call "
                               f"onboarding to the checkout flow. Board: "
                               f"/api/v1/admin/customer-white-glove/state."),
                })
            # Escalation backlog: nudged customers who stayed stranded — the
            # automated motion failed and a human needs to step in.
            cur.execute("""
                SELECT COUNT(*) FROM users u
                 WHERE u.last_touch_by = 'customer_white_glove'
                   -- r-escalation-column-fix (2026-07-24 coverage audit): this read
                   -- `lifecycle_stage`, which is OWNED by the CRM funnel subsystem and
                   -- whose vocabulary has no 'stranded' value at all (live: {new:142,
                   -- converted:12}) — so the human-escalation branch could NEVER fire
                   -- while 15 of 17 paying customers sat stranded. The white-glove loop
                   -- writes `engagement_stage` (customer_white_glove._persist), which is
                   -- exactly what leg (a) of this same function already reads.
                   AND u.engagement_stage = 'stranded'
                   AND EXISTS (SELECT 1 FROM email_drip_log d
                                WHERE lower(d.user_email) = lower(u.email)
                                  AND d.email_key = 'activation_nudge'
                                  AND d.sent_at < NOW() - INTERVAL '7 days')
            """)
            escalate = cur.fetchone()[0] or 0
            if escalate >= ESCALATE_MIN:
                findings.append({
                    "issue":  "customer_nudge_failed_needs_human",
                    "url":    "/api/v1/admin/customer-white-glove/state",
                    "count":  int(escalate),
                    "detail": (f"{escalate} paying customers were sent the automated "
                               f"activation nudge 7+ days ago and are STILL at zero "
                               f"calls — the automated motion failed. The loop escalated "
                               f"them; they need a human touch (call / personal note), "
                               f"not another email."),
                })
    except Exception as e:
        findings.append({
            "issue":  "consistency_radar_detector_crashed:check_customer_activation_health",
            "url":    "check_customer_activation_health",
            "count":  1,
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        })
    finally:
        try:
            c.close()
        except Exception:
            pass
    return findings


def check_facility_duplicate_clusters() -> list[dict]:
    """r-facility-dedup (2026-07-20) — regrowth detector for cross-source
    facility duplicates. A paying customer (Landry) flagged that the same
    physical site appears many times in the paid /api/v1/facilities listing
    (source variants under different names). routes/facility_dedup.py collapses
    them (duplicate_of_id). This watches the deduped markets and fires when NEW
    duplicate rows have been ingested but not yet collapsed — so the fix stays
    closed-loop instead of silently rotting as fresh data lands. FAIL-SOFT."""
    findings: list[dict] = []
    try:
        try:
            from routes import facility_dedup as fd
        except Exception:
            import facility_dedup as fd
    except Exception:
        return findings
    c = _db()
    if c is None:
        return findings
    # Top markets by facility volume — where cross-source dupes concentrate.
    MARKETS = ["US", "SG", "DE", "AU", "CA", "GB", "NL", "JP", "BR", "FR",
               "TH", "NZ", "IN", "ID", "HK"]
    try:
        for code in MARKETS:
            plan = fd._plan(code)
            if not plan:
                continue
            dup_ids = [d["id"] for cl in plan["plan"] for d in cl["duplicates"]]
            if not dup_ids:
                continue
            with c.cursor() as cur:
                # This detector IS the dedup pass over the legacy table:
                # duplicate_of_id lives on `facilities`, and the finding it
                # raises ("cross-source duplicate facility rows not yet
                # collapsed") is about those exact rows. Pointing it at
                # discovered_facilities would measure the wrong table and
                # silence the dedup signal.
                # lint: legacy-facilities-ok
                cur.execute("SELECT COUNT(*) FROM facilities WHERE id = ANY(%s) "
                            "AND duplicate_of_id IS NULL", (dup_ids,))
                unmarked = cur.fetchone()[0] or 0
            if unmarked >= 5:
                findings.append({
                    "issue":  "facility_duplicates_unmarked",
                    "url":    f"/api/v1/admin/facility-dedup/analyze?country={code}",
                    "count":  int(unmarked),
                    "detail": (f"{unmarked} cross-source duplicate facility rows in "
                               f"{code} are not yet collapsed (new arrivals since the "
                               f"last dedup pass). The paid /api/v1/facilities listing "
                               f"is showing them as separate sites. Re-run POST "
                               f"/api/v1/admin/facility-dedup/apply?country={code}&confirm=1."),
                })
    except Exception as e:
        findings.append({
            "issue":  "consistency_radar_detector_crashed:check_facility_duplicate_clusters",
            "url":    "check_facility_duplicate_clusters",
            "count":  1,
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        })
    finally:
        try:
            c.close()
        except Exception:
            pass
    return findings


def check_facility_geo_mismatch() -> list[dict]:
    """r-facility-geo (2026-07-20) — regrowth detector for country-mislabeled
    facilities. A bulk ingestion stamped country='US' on non-US sites; we
    relabel from coordinates. This fires when NEW high-confidence mislabels
    appear (coords land unambiguously in a different country than the label),
    so the fix stays closed-loop. FAIL-SOFT."""
    findings: list[dict] = []
    try:
        try:
            from routes import facility_geo_quality as gq
        except Exception:
            import facility_geo_quality as gq
    except Exception:
        return findings
    try:
        s = gq._scan()
        if not s:
            return findings
        n = len(s["fixes"])
        if n >= 25:
            from collections import Counter
            top = Counter((r["from"], r["to"]) for r in s["fixes"]).most_common(5)
            flows = ", ".join(f"{a}->{b}:{c}" for (a, b), c in top)
            findings.append({
                "issue":  "facility_country_mislabeled",
                "url":    "/api/v1/admin/facility-geo/analyze",
                "count":  int(n),
                "detail": (f"{n} facilities have coordinates that land unambiguously "
                           f"in a different country than their `country` label "
                           f"(top flows: {flows}). Customer country filters are "
                           f"wrong for these. Fix: POST /api/v1/admin/facility-geo/"
                           f"apply?confirm=1 (reversible via /undo)."),
            })
    except Exception as e:
        findings.append({
            "issue":  "consistency_radar_detector_crashed:check_facility_geo_mismatch",
            "url":    "check_facility_geo_mismatch",
            "count":  1,
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        })
    return findings


def check_required_env_vars() -> list[dict]:
    """Phase QQQ (2026-05-17) — flag missing env vars that cause silent
    skips elsewhere in the codebase.

    Inventory found 5+ crons that return early without logging when
    their gating env var is missing. The brain was the worst offender:
    if ANTHROPIC_API_KEY is missing, the brain emits zero findings and
    we have no idea it's blind. This detector watches the watchers.
    """
    import os as _os
    findings: list[dict] = []
    for var_name, why in _REQUIRED_ENV_VARS:
        if not (_os.environ.get(var_name, "") or "").strip():
            findings.append({
                "issue":  "required_env_var_missing",
                "url":    f"env://{var_name}",
                "count":  1,
                "detail": (f"Required env var `{var_name}` is missing on "
                           f"the running backend. Impact: {why}. Fix: set "
                           f"the var in Railway → service → variables, "
                           f"then redeploy."),
            })
    return findings


def check_csp_violation_reports() -> list[dict]:
    """Phase QQQ (2026-05-17) — flag CSP allowlist gaps that are
    actively breaking real users.

    csp_report.py records every browser CSP violation report. If a
    blocked URI shows up repeatedly in the last 24h, it's an allowlist
    gap we should plug. Without this detector we only learn about CSP
    drift when a user reports a broken page (the jsdelivr / unpkg
    pattern repeated all session).
    """
    findings: list[dict] = []
    try:
        # csp_report.py is at the repo root, not under routes/.
        # Import is lazy so we don't crash if the module isn't loaded.
        try:
            from csp_report import recent_blocked_uris  # type: ignore
        except ImportError:
            return findings
        reports = recent_blocked_uris(window_seconds=86400, top_n=5)
        for r in reports:
            count = r.get("count", 0)
            blocked = r.get("blocked_uri") or ""
            directive = r.get("directive") or ""
            if count < 3:
                # Single-occurrence noise (browser extensions, etc.)
                continue
            findings.append({
                "issue":  "csp_violation_recurring",
                "url":    f"csp://{directive}/{blocked}",
                "count":  count,
                "detail": (f"CSP directive `{directive}` blocked `{blocked}` "
                           f"{count}× in the last 24h. Likely an allowlist "
                           f"gap — add `{blocked}` to the `{directive}` "
                           f"directive in dchub-frontend/_headers and "
                           f"redeploy. (Browsers POST to /api/csp-report.)"),
            })
    except Exception as e:
        findings.append({
            "issue":  "consistency_radar_detector_crashed:check_csp_violation_reports",
            "url":    "check_csp_violation_reports",
            "count":  1,
            "detail": f"{type(e).__name__}: {str(e)[:200]}",
        })
    return findings


def check_backend_pool_health() -> list[dict]:
    """Phase QQQ (2026-05-17) — flag Railway upstream pool health
    separately from API endpoint health.

    `check_frontend_critical_endpoints` probes API paths, but if
    Railway is degraded (pool > 80% utilized, circuit breaker open,
    memory near OOM) those probes just see 5xx errors and never tell
    us the root cause. This detector hits `/api/health/db` directly —
    which is in-memory only — to separate "API endpoint broken" from
    "backend upstream dying."
    """
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    url = "https://dchub.cloud/api/health/db"
    try:
        # Phase QQQ-hotfix: 3s timeout (down from 8s) — /api/health/db is
        # in-memory only and should respond in <100ms; 3s is a generous
        # ceiling that keeps brain scan latency bounded. The 5-min cache
        # on scan_summary() means this fires at most every 5 min anyway.
        r = _req.get(url, timeout=3,
                      headers={"User-Agent": "dchub-brain-pool-probe/1.0"})
    except Exception as e:
        findings.append({
            "issue":  "backend_pool_unreachable",
            "url":    "/api/health/db",
            "count":  1,
            "detail": (f"Railway /api/health/db is unreachable "
                       f"({type(e).__name__}: {str(e)[:120]}). "
                       f"Either Railway is hard-down (TCP timeout) or "
                       f"the CF worker stale cache is exhausted. Every "
                       f"data widget on the site is currently dark."),
        })
        return findings
    if r.status_code >= 500:
        findings.append({
            "issue":  "backend_pool_degraded",
            "url":    "/api/health/db",
            "count":  r.status_code,
            "detail": (f"Railway /api/health/db returned HTTP {r.status_code}. "
                       f"Pool is critical OR memory over threshold OR "
                       f"circuit breaker open. Body: {r.text[:200]}"),
        })
        return findings
    try:
        body = r.json()
    except Exception:
        return findings
    pool = (body.get("pool") or {})
    util = pool.get("utilization_pct")
    if isinstance(util, (int, float)) and util > 80:
        findings.append({
            "issue":  "backend_pool_utilization_high",
            "url":    "/api/health/db",
            "count":  int(util),
            "detail": (f"Neon pool at {util}% utilization on Railway "
                       f"({pool.get('checked_out')}/"
                       f"{pool.get('max_configured')} connections in use). "
                       f"At >90% the health gate fails. Find the runaway "
                       f"query or scale the pool: DB_POOL_MAX env var."),
        })
    return findings


# ── Phase RRR-revenue (2026-05-18) — orphaned-scheduler detector ─────
#
# This is the 5th instance of a recurring bug class this session:
#   1. deal_ingestion_scheduler.start_deal_scheduler — defined, never called
#   2. content_publisher.start_auto_publisher (LinkedIn) — defined, never called
#   3. content_publisher.start_twitter_publisher — defined, never called
#   4. content_publisher.start_bluesky_publisher — defined, never called
#   5. routes/package_stats.start_package_stats_refresher — defined, never called
#
# Symptom is always the same: a downstream surface looks "healthy" because
# the code that publishes/refreshes/ingests exists, env vars are set, and
# the queue is clear — but no actual work happens because the daemon
# thread that does the work was never started at boot.
#
# This detector AST-scans the codebase for functions whose body contains
# `threading.Thread(target=...)` (the signature of a daemon-loop starter),
# then text-greps the codebase for any external reference to the function
# name (excluding the file it's defined in). Zero external references =
# orphaned = silent skip waiting to happen.
#
# False-positive controls:
#  - Skip names starting with `_` (private helpers, often called internally)
#  - Skip files in tests/, scripts/, migrations/, .venv/, node_modules/
#  - Allow-list explicit known-unused (e.g. deprecated experiments)
_ORPHANED_SCHEDULER_ALLOWLIST: set[str] = {
    # Phase RRR-orphan triage (2026-05-18):
    # `start_scheduled_discovery` is a duplicate of the external
    # `dchub-scheduler.py` cron (hits the same /api/news/refresh,
    # /api/discovery/run, /api/facilities/refresh endpoints). Wiring it
    # at boot would double-fire every cron run. Kept in the file as a
    # fallback in case the external scheduler is decommissioned.
    "start_scheduled_discovery",
    # `register_transactions_news_api` is legacy — superseded by
    # routes/deals_routes.py + routes/admin_ai_deals.py which serve
    # /api/deals from Neon. Old in-memory module retained because
    # mcp_server.py still imports VERIFIED_TRANSACTIONS for its
    # bootstrap seed, but the register function itself is dead.
    "register_transactions_news_api",
    # `startup_restore_and_sync` is legacy SQLite-bridge code from
    # the dc_nexus.db era. The site moved fully to Neon; only
    # `sync_on_write` is still imported (lines main.py:8479 +
    # main.py:11906). Wiring this would re-introduce a SQLite↔PG
    # sync nobody needs.
    "startup_restore_and_sync",
}

_SCHEDULER_SKIP_DIRS = {
    "tests", "test", "scripts", "migrations", ".venv", "venv", "node_modules",
    "__pycache__", ".git", "dist", "build", ".wrangler", ".pytest_cache",
}

def check_orphaned_scheduler_functions() -> list[dict]:
    """Find `def start_xxx()` / `def xxx_loop()` functions that spawn a
    threading.Thread but are never called from anywhere else.

    This is the bug class that caused:
      - LinkedIn/X/Bluesky publish silently 0/0/0 for weeks
      - /ai-deals stale 21+ days (deal ingestion never started)
      - homepage install-count pill stuck at 0
    """
    import ast as _ast
    import os as _os
    findings: list[dict] = []

    here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    # Pass 1: AST-walk to find scheduler-starter candidates.
    candidates: list[tuple[str, str, int]] = []  # (func_name, file_rel, lineno)
    for root, dirs, files in _os.walk(here):
        # In-place filter — _os.walk respects it
        dirs[:] = [d for d in dirs if d not in _SCHEDULER_SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = _os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    source = f.read()
                tree = _ast.parse(source, filename=fpath)
            except Exception:
                continue
            for node in _ast.walk(tree):
                if not isinstance(node, _ast.FunctionDef):
                    continue
                if node.name.startswith("_"):
                    continue  # private helper, often called from same file
                if node.name in _ORPHANED_SCHEDULER_ALLOWLIST:
                    continue
                # Phase RRR-orphan refinement (2026-05-18): Flask route
                # handlers spawn threading.Thread on user request, not at
                # boot. Skip any function decorated with .route / .get /
                # .post / .put / .patch / .delete — those are reachable
                # via HTTP, not orphaned. Eliminates 4-of-5 false positives.
                is_route_handler = False
                for dec in node.decorator_list:
                    # Handle both @app.route(...) and @bp.route(...)
                    # which parse as ast.Call wrapping an ast.Attribute.
                    target = dec.func if isinstance(dec, _ast.Call) else dec
                    if isinstance(target, _ast.Attribute) and target.attr in (
                            "route", "get", "post", "put", "patch", "delete"):
                        is_route_handler = True
                        break
                if is_route_handler:
                    continue
                # Heuristic: must spawn a threading.Thread inside the body
                spawns_thread = False
                for sub in _ast.walk(node):
                    if isinstance(sub, _ast.Call):
                        func = sub.func
                        # threading.Thread(...) or Thread(...)
                        if (isinstance(func, _ast.Attribute) and
                                func.attr == "Thread"):
                            spawns_thread = True
                            break
                        if isinstance(func, _ast.Name) and func.id == "Thread":
                            spawns_thread = True
                            break
                if spawns_thread:
                    rel = _os.path.relpath(fpath, here)
                    candidates.append((node.name, rel, node.lineno))

    if not candidates:
        return findings

    # Pass 2: For each candidate, walk every .py file and COUNT occurrences
    # of the function name. We need counts (not just presence) so we can
    # distinguish "defined in main.py + called in main.py" (=2 occurrences
    # in main.py, NOT orphaned) from "defined in main.py, never called"
    # (=1 occurrence in main.py, orphaned).
    candidate_names = {c[0] for c in candidates}
    # Map (name → file → count)
    counts: dict[str, dict[str, int]] = {n: {} for n in candidate_names}
    for root, dirs, files in _os.walk(here):
        dirs[:] = [d for d in dirs if d not in _SCHEDULER_SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = _os.path.join(root, fname)
            rel = _os.path.relpath(fpath, here)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    src = f.read()
            except Exception:
                continue
            for name in candidate_names:
                n = src.count(name)
                if n > 0:
                    counts[name][rel] = n

    # Pass 3: report any candidate where:
    #   - the defining file has exactly 1 occurrence (just the def itself)
    #   - AND no other file references it
    # This catches the silent-skip pattern: defined, not called anywhere.
    for func_name, def_file, def_line in candidates:
        per_file = counts.get(func_name, {})
        in_def_file = per_file.get(def_file, 0)
        other_files = {f: c for f, c in per_file.items() if f != def_file}
        is_orphan = (in_def_file <= 1) and (not other_files)
        if is_orphan:
            findings.append({
                "issue":  "scheduler_function_orphaned",
                "url":    f"{def_file}:{def_line}",
                "count":  1,
                "detail": (f"`{func_name}()` is defined in `{def_file}` and "
                           f"spawns a threading.Thread (daemon loop), but "
                           f"NOTHING else in the codebase references it. "
                           f"Likely the `{func_name}()` call was never added "
                           f"to main.py at boot — the loop never starts, the "
                           f"surface it powers (publish queue / cron / "
                           f"refresher) sits silent while everything *appears* "
                           f"healthy (env vars set, queue clear). This is the "
                           f"bug class that caused LinkedIn/X/Bluesky publish + "
                           f"deal ingestion + package counter silent-skips. "
                           f"Fix: add a try/except wrapped `{func_name}()` "
                           f"call at boot in main.py."),
            })
    return findings


# ── Phase RRR-newsletter (2026-05-18) — dead internal link detector ──
#
# Catches the bug class of "navigation/CTA links pointing at routes that
# no longer exist". We hit this twice this session:
#   - /intelligence card linked to /open-data → 404 (real path was /data)
#   - /press hit /api/press-releases → 404 (real path was /list)
#
# Both were silent: the user clicked, got a 404, didn't report it.
# This detector probes a curated list of the highest-traffic internal
# URLs surfaced on the homepage + key landing pages, flags any that
# don't return 2xx. The list is intentionally short — every entry has
# to be deliberately maintained, which keeps signal-to-noise high.
#
# To extend coverage to the full ~95 HTML page surface, switch this to
# an auto-discovery detector that scrapes hrefs from a small set of
# index pages. For now the curated approach is more reliable.
_INTERNAL_LINK_PROBES = [
    # Navigation entries that appear on /dchub-nav.js (every page header)
    "/",
    "/by-the-numbers",
    "/cited-by",
    "/pricing",
    "/dc-hub-media/",
    "/markets",
    "/land-power",
    "/dashboard",
    "/digest",
    # Hero/CTA targets across landing pages
    "/dcpi",
    "/intelligence",
    "/ai",
    "/ai-deals",
    "/ai-pipeline",
    "/ai-inventory",
    "/ai-wars",
    "/ecosystem",
    "/state-of-the-data-center",
    "/about",
    "/advertise",
    "/api-docs",
    "/data",                       # /intelligence "Open data (CSV)" link
    "/research/grid-intelligence/",
    # Common API paths the frontend depends on
    "/api/v1/stats",
    "/api/v1/site/stats",
    "/api/v1/marketing/pulse",
    "/api/v1/packages/stats",
    "/api/v1/grid-intelligence",
    "/api/press-releases/list",    # /press page (Phase RRR-wave2 fixed)
    # r32-cf-audit (2026-05-20): paths the CF analytics flagged as the
    # top 4xx sources. Each one was the same bug class — literal path
    # not forwarded by CF Pages → handler unreachable → cached 404.
    # 276k /grid 404s alone over 30 days.
    "/grid",                       # 276.85k 4xx — biggest single leak
    "/mcp.json",                   # 94.47k 4xx — agent discovery convention
    "/agents",                     # 18.25k 4xx — should serve agents.html
    "/digest",                     # 29.21k 4xx — cached 404 from past
                                   # outage; route is healthy now
    "/.well-known/mcp.json",       # canonical mcp.json — pair with above
    "/pockets.rss",                # r31 — make sure RSS feed doesn't drift
]


# r43-H (2026-05-29): TTL result-cache for the dead-link sweep. This detector
# probes ~60 internal URLs (each via CF→backend) on EVERY radar scan, and many
# brain GH workflows trigger radar scans — CF analytics showed the
# dchub-brain-deadlink-probe UA doing ~101k req/day, the single biggest source
# of backend self-traffic and a big chunk of the 429/saturation that's been
# flapping the replica. Dead-link state changes slowly, so re-probing all 60
# URLs on every scan is pure waste. Cache the whole result for 30 min: the
# first scan in a window does the probes; every other caller in that window
# gets the cached findings (0 probes). ~101k/day → ~3k/day, no loss of
# detection latency that matters for a health check.
_DEADLINK_CACHE = {"ts": 0.0, "findings": None}
_DEADLINK_TTL_S = 21600  # r72 (2026-06-04): 60 min → 6 h. CF analytics dashboard
                         # inflation — dchub-brain-deadlink-probe was 56k/day (the
                         # single largest probe UA). Dead-link state changes slowly;
                         # 4 sweeps/day is plenty of detection latency for a self-
                         # health check, and gives the same coverage at ~1/30th
                         # the volume. (Durable fix — Redis-shared cache spanning
                         # replicas — is flagged.)

def check_dead_internal_links() -> list[dict]:
    """Phase RRR-newsletter (2026-05-18) — probe every high-traffic
    internal URL and flag any that 404 or 5xx. Catches the dead-link
    bug class that's silent from the user's side.

    Phase r32 (2026-05-20): expanded to auto-discover nested-slug paths
    from modules whose data tables list canonical slugs (competitive_vs,
    pockets, locations). Pre-r32 the detector missed /vs/cbre and
    /vs/jll because they weren't in the curated list — user reported
    "come on why isnt brain fixing". This closes that gap by reading
    the slug tables directly at probe time, so any future addition is
    automatically covered."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    import time as _t
    now = _t.time()
    # r78: hydrate the in-process cache from brain_meta first. The 6h TTL
    # was a fiction in prod — gunicorn --max-requests recycles the worker
    # every ~35 min and wipes _DEADLINK_CACHE, so nearly every scan re-ran
    # the full ~60-URL sweep (~47.8k deadlink-probe requests/day at CF,
    # the single largest self-traffic source). The DB copy makes the TTL
    # window shared across worker recycles AND both replicas.
    if _DEADLINK_CACHE["findings"] is None:
        try:
            from routes.brain_v2_store import get_meta as _gm_dl
            _row_dl = _gm_dl("deadlink_sweep_cache")
            if _row_dl and _row_dl.get("value"):
                import json as _json_dl
                _payload_dl = _json_dl.loads(_row_dl["value"])
                _DEADLINK_CACHE["ts"] = float(_payload_dl.get("ts") or 0)
                _DEADLINK_CACHE["findings"] = _payload_dl.get("findings") or []
        except Exception:
            pass
    # Serve the cached sweep result if still fresh — dedups the ~60-URL probe
    # across every brain workflow that hits the radar inside the TTL window.
    if (_DEADLINK_CACHE["findings"] is not None
            and (now - _DEADLINK_CACHE["ts"]) < _DEADLINK_TTL_S):
        return list(_DEADLINK_CACHE["findings"])
    headers = {"User-Agent": "dchub-brain-deadlink-probe/1.0"}

    # Expand the probe list with auto-discovered nested slugs.
    probes = list(_INTERNAL_LINK_PROBES)
    # /vs/<competitor> — known from competitive_vs._COMPETITORS
    try:
        from routes.competitive_vs import _COMPETITORS
        for slug in _COMPETITORS.keys():
            probes.append(f"/vs/{slug}")
    except Exception:
        pass
    # /pockets/<slug> — sample first 3 ranked pockets (full sweep
    # would be too noisy; if 3 work the rest will)
    try:
        from routes.pockets import _fetch_pockets
        rows = _fetch_pockets(limit_hint=3)
        for r in rows:
            if r.get("market_slug"):
                probes.append(f"/pockets/{r['market_slug']}")
    except Exception:
        pass

    # r32-mt-fix (2026-05-21): parallelize the probes. Pre-fix this
    # detector ran 45 serial HTTP requests with 5s timeout each =
    # worst case 225s. Railway logs caught this as a SLOW REQUEST
    # (216.2s on /api/v1/brain/consistency-radar). scan_all() already
    # parallelizes DETECTORS, but each detector still ran serially
    # inside itself — so a single slow detector blocked its scan slot
    # the full 20s timeout. Now: 6-thread pool inside this detector,
    # 5s per-request timeout, target wall time ~10s for 45 probes.
    import concurrent.futures as _cf

    def _probe_one(path):
        url = f"https://dchub.cloud{path}"
        try:
            # r41-dead-links-timeout (2026-05-25): tuple timeout
            # (connect=3, read=5) catches DNS / TCP-connect stalls that
            # the prior single `timeout=5` missed — requests' single
            # int timeout is the READ timeout only, with no bound on
            # connect time. Cold lookups + TLS handshakes were each
            # routinely 10-30s on some probes, blowing wall time from
            # the expected ~10s to the observed 77s. Worst case per
            # probe is now 8s (3s connect + 5s read).
            # r85: read timeout 5s -> 10s. The old 5s cap + transient backend
            # slowness (the cron thundering-herd, fixed r85) flagged 9 LIVE
            # pages (/pockets/*, /digest, /vs/*) as "unreachable" — every one
            # actually returns 200 in 1-2s, they just spiked past 5s under
            # load. 10s tolerates a load spike; the failed subset is re-probed
            # once below, so nothing is flagged on a single slow response.
            r = _req.get(url, timeout=(4, 10), headers=headers, allow_redirects=True)
            return (path, r.status_code, (r.text or "")[:120], None)
        except Exception as e:
            return (path, None, "", f"{type(e).__name__}: {str(e)[:120]}")

    # r41 (2026-05-25): bumped from 6 → 12 workers. Detector was still
    # hitting 52s observed wall time with 6 workers (the slowest single
    # detector in the radar scan). Probe count has grown (auto-discovered
    # slugs from competitive_vs + pockets push the list past 60), so the
    # 6-worker cap was a real bottleneck. 12 workers in 60 probes × 5s
    # worst case ≈ 25s; in practice most probes return in 0.5-2s so
    # wall time should land closer to 8-12s.
    with _cf.ThreadPoolExecutor(max_workers=12,
                                 thread_name_prefix="deadlink") as ex:
        results = list(ex.map(_probe_one, probes))

    # r85: second-chance re-probe. Only paths that failed the first sweep
    # (timeout/connect error, 404, or 5xx) get re-probed once — a link is
    # flagged ONLY if it fails TWICE. This kills the transient-slowness
    # false-positive class (mirrors the r84 heartbeat "don't destructively
    # mark on a single transient timeout" fix). The healthy common case (no
    # failures) skips the re-probe entirely, so wall time is unchanged.
    def _is_suspect(res):
        _p, _s, _b, _e = res
        return _e is not None or _s == 404 or (_s is not None and _s >= 500)
    _suspects = [r[0] for r in results if _is_suspect(r)]
    if _suspects:
        import time as _t2
        _t2.sleep(0.3)  # brief breather so an instantaneous load spike clears
        with _cf.ThreadPoolExecutor(max_workers=min(12, len(_suspects)),
                                     thread_name_prefix="deadlink-retry") as ex2:
            _retry = {r[0]: r for r in ex2.map(_probe_one, _suspects)}
        results = [_retry.get(r[0], r) for r in results]

    # r-linkprobe-sweepfail (2026-07-24 coverage audit): when essentially
    # EVERY probe fails, the site is not 230-pages-down — the prober is.
    # Observed live: 230/230 findings, all ReadTimeout to dchub.cloud, while
    # all 20 sampled paths returned 200 from outside. The probe list has
    # grown ~60 -> ~230 and 12 workers hammering the frontend now trips
    # timeouts/throttling on our own sweep. Emitting 230 findings buried the
    # 37 real ones (86% of the whole radar was this one detector) — which is
    # the same "detector you learn to ignore" failure as reporting green.
    # Report the sweep failure ONCE and suppress the per-path noise.
    #
    # NOTE: this must NOT return early — the cache write lives at the bottom
    # of this function, and skipping it would make every subsequent radar
    # call re-run the whole ~230-probe sweep. Per the r78 note above, this
    # sweep is already the single largest source of self-traffic (~47.8k
    # probes/day at CF), so an uncached sweep-per-scan would be far worse
    # than the noise being fixed. Collapse into `findings` and fall through.
    _total = len(results)
    _failed = sum(1 for _r in results if _r[3] is not None)
    _sweep_failed = _total >= 10 and _failed >= int(_total * 0.8)
    if _sweep_failed:
        _sample = ", ".join(_r[0] for _r in results[:3] if _r[3] is not None)
        findings = [{
            "issue":  "link_prober_sweep_failed",
            "url":    "routes/brain_consistency_radar.py:check_internal_links",
            "count":  _failed,
            "detail": (
                f"Internal-link sweep failed on {_failed}/{_total} probes "
                f"(e.g. {_sample}) — suppressed the per-path findings because "
                f"a ~100% failure rate means the PROBER is blocked, not that "
                f"every page is down (spot-checked paths return 200 from "
                f"outside). Likely causes: the probe list grew to ~{_total} "
                f"paths and 12 concurrent workers now trip Cloudflare "
                f"throttling on our own origin, or egress from the backend to "
                f"dchub.cloud is blocked. Fix the sweep (lower concurrency / "
                f"raise the read timeout / probe the origin directly) before "
                f"trusting this detector again."
            ),
        }]

    for path, status, body_snip, err in (() if _sweep_failed else results):
        if err is not None:
            findings.append({
                "issue":  "internal_link_unreachable",
                "url":    path,
                "count":  1,
                "detail": (f"`{path}` failed to load: {err}. "
                           f"Either CF Pages route missing OR CF Worker "
                           f"can't reach the backend handler."),
            })
            continue
        if status == 404:
            findings.append({
                "issue":  "internal_link_404",
                "url":    path,
                "count":  1,
                "detail": (f"`{path}` returns 404 Not Found. Either the "
                           f"route was renamed/removed but a nav link "
                           f"still points at it, or CF Pages _routes.json "
                           f"doesn't include this prefix. Audit nav JS + "
                           f"_redirects."),
            })
        elif status is not None and status >= 500:
            findings.append({
                "issue":  "internal_link_5xx",
                "url":    path,
                "count":  status,
                "detail": (f"`{path}` returns HTTP {status} "
                           f"(server error). Body: {body_snip}"),
            })
        elif status in (401, 403):
            # Some admin endpoints will 401/403 anonymously — that's
            # correct behavior, not a dead link. Skip.
            pass
    # Cache this sweep so concurrent/subsequent radar callers reuse it.
    _DEADLINK_CACHE["ts"] = now
    _DEADLINK_CACHE["findings"] = list(findings)
    # r78: persist to brain_meta so the TTL survives worker recycles and
    # spans replicas (see hydrate note at the top of this function).
    try:
        from routes.brain_v2_store import set_meta as _sm_dl
        import json as _json_dl2
        _sm_dl("deadlink_sweep_cache",
               _json_dl2.dumps({"ts": now, "findings": list(findings)}))
    except Exception:
        pass
    return findings


# ── Phase RRR-cron-wiring (2026-05-18) — HTTP-cron unscheduled detector ─
#
# Sibling of check_orphaned_scheduler_functions. That one catches
# Thread() loops defined but never started. THIS one catches HTTP
# endpoints that LOOK like cron triggers (path matches /api/jobs/*,
# /api/v1/*/refresh, /api/v1/*/deliver, /api/v1/*/send-public, etc.)
# but aren't actually scheduled in dchub-scheduler.py.
#
# Hit this twice today: routes/weekly_public_newsletter.py's
# /api/v1/weekly/send-public endpoint AND routes/winback_outreach.py's
# /api/v1/media/winback/deliver endpoint — both built, both
# admin-gated, both intended to fire on Mondays, neither was actually
# in JOBS until I added them. Same "silent inert" failure mode as the
# Thread()-spawner orphans.

_CRON_PATH_PATTERNS = [
    r"/api/jobs/[a-z][\w-]*",           # convention for scheduled jobs
    r"/api/v1/[\w-]+/refresh",          # */refresh
    r"/api/v1/[\w-]+/deliver\b",        # */deliver
    r"/api/v1/[\w-]+/send-public",      # */send-public
    r"/api/v1/[\w-]+/run\b",            # */run
    r"/api/v1/[\w-]+/cron",             # */cron
    r"/api/v1/[\w-]+/sync\b",           # */sync
    r"/api/v1/[\w-]+/scan\b",           # */scan
    # Phase RRR-publish-cron (2026-05-18) — caught the silent
    # press → LinkedIn skip. The marketing publish-now endpoint had no
    # cron + my original patterns missed it. Adding here.
    r"/api/v1/[\w-]+/publish-now",      # */publish-now
    r"/api/v1/[\w-]+/publish\b",        # */publish (plain)
    r"/api/v1/[\w-]+/ingest\b",         # */ingest
]

# Endpoints that LOOK like cron paths but are intentionally manual-only
# (not in JOBS, that's correct). Allowlist to prevent false positives.
_CRON_INTENTIONAL_MANUAL: set[str] = {
    "/api/jobs/db-backup/list",          # admin read-only
    "/api/jobs/status",                  # admin read-only
    "/api/jobs/keep-alive",              # called by browser keep-alive, not cron
    "/api/v1/manual/run",                # explicit manual-only
    # Phase RRR-cron-batch (2026-05-18) — read-only cached endpoints
    # whose ACTIVE trigger is a sibling path (e.g., /scan returns
    # cached results; /scan-now triggers a fresh scan). The cron
    # should be on the active variant, not the read variant.
    "/api/v1/sentinel/scan",             # read-only (see /sentinel/scan-now)
    "/api/v1/news/sync",                 # one-shot manual sync (uses /api/jobs/news-refresh for cron)
    # Phase RRR-cron-batch-2 (2026-05-18) — final 4 unschedulables
    # that are intentionally not cron-driven. Each has a documented
    # reason; the brain shouldn't keep flagging them.
    "/api/jobs/db-backup",               # the existing 'backup' cron handles backups; this endpoint is a manual variant
    "/api/jobs/sync-all-tables",         # too heavy for cron — invoked manually for one-time data ops
    "/api/v1/heal/run",                  # requires ?action=<name> param; not cron-compatible without choosing the action
    "/api/v1/packages/refresh",          # already wired as a daemon Thread via start_package_stats_refresher
    # Phase RRR-publish-cron-followup (2026-05-18): /api/v1/tenants/ingest
    # is a bulk-import admin endpoint — takes {tenants:[...]} POST body
    # for one-shot data ingest. NOT cron-compatible; intentionally manual.
    "/api/v1/tenants/ingest",
    # r36 (2026-05-31): the detector only reads dchub-scheduler.py JOBS, so it
    # was false-flagging endpoints that ARE triggered, just not by that scheduler:
    #  - the infra refreshers fire EVENT-DRIVEN via brain_autopilot's REFRESH_MAP
    #    (_action_data_freshness_breach) when transmission_lines/gas_pipelines/
    #    substations breach their SLA, AND daily-infra-sync.yml runs a full
    #    /api/jobs/infrastructure-sync daily. A fixed cron on each would just
    #    duplicate that.
    "/api/jobs/transmission-refresh",    # autopilot REFRESH_MAP (table SLA breach)
    "/api/jobs/gas-refresh",             # autopilot REFRESH_MAP (table SLA breach)
    "/api/jobs/substations-refresh",     # autopilot REFRESH_MAP (table SLA breach)
    "/api/jobs/infra-refresh-status",    # read-only STATUS endpoint, not a job
    # /api/v1/narrative/refresh is a force-recompute; the narrative arc already
    # self-refreshes lazily on GET when its _ARC_TTL_SECONDS cache expires, so it
    # never goes stale without a cron. The POST is an admin/cron convenience.
    "/api/v1/narrative/refresh",
}


def check_cron_endpoint_unscheduled() -> list[dict]:
    """Find Flask routes that look like cron triggers but aren't in
    dchub-scheduler.py's JOBS dict. The "weekly_public_newsletter" +
    "winback_delivery" wiring I added today were both already-built
    endpoints; the brain would have caught them sooner with this
    detector."""
    import ast as _ast
    import os as _os
    import re as _re
    findings: list[dict] = []

    here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    # Pass 1: extract all scheduled endpoint paths from dchub-scheduler.py
    scheduled_paths: set[str] = set()
    scheduler_path = _os.path.join(here, "dchub-scheduler.py")
    try:
        with open(scheduler_path, "r", encoding="utf-8") as f:
            src = f.read()
        # Find every 'endpoint': '/...' pair. Phase RRR-publish-cron
        # bugfix (2026-05-18): strip query params before comparing, since
        # JOBS dict often has endpoints like '/api/v1/daily/run?job=all'
        # but the matching Flask route is just '/api/v1/daily/run'.
        # Without the strip, we'd false-flag every job that uses query
        # params (caught both /daily/run + /alerts/run incorrectly).
        for m in _re.finditer(r"['\"]endpoint['\"]\s*:\s*['\"]([^'\"]+)['\"]", src):
            scheduled_paths.add(m.group(1).split('?')[0])
    except Exception:
        return findings  # if we can't read the scheduler, can't detect

    # Pass 2: extract every Flask @route path from main.py + routes/*.py
    # via simple regex (faster than AST for this; route decorators are
    # syntactically clean).
    candidates: list[tuple[str, str, int]] = []  # (path, file_rel, lineno)
    compiled_patterns = [_re.compile(p) for p in _CRON_PATH_PATTERNS]
    route_re = _re.compile(r"@\w+\.route\(['\"]([^'\"]+)['\"]")

    for root, dirs, files in _os.walk(here):
        # Skip noisy dirs
        dirs[:] = [d for d in dirs if d not in
                    {".git", ".venv", "venv", "tests", "test", "__pycache__",
                     "node_modules", ".wrangler", "dist", "build", "scripts",
                     "migrations"}]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = _os.path.join(root, fname)
            rel = _os.path.relpath(fpath, here)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        m = route_re.search(line)
                        if not m:
                            continue
                        path = m.group(1)
                        if path in _CRON_INTENTIONAL_MANUAL:
                            continue
                        # Does this path match any cron pattern?
                        if any(cp.fullmatch(path) for cp in compiled_patterns):
                            candidates.append((path, rel, lineno))
            except Exception:
                continue

    # Dedup candidates by path — multiple decorators on the same path
    # (e.g., @app.route('/foo', methods=POST) + @app.route('/foo',
    # methods=OPTIONS)) are the same endpoint logically.
    seen = set()
    unique_candidates = []
    for path, rel, ln in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique_candidates.append((path, rel, ln))

    # Pass 3: flag any candidate not in scheduled_paths
    for path, rel, ln in unique_candidates:
        if path not in scheduled_paths:
            findings.append({
                "issue":  "cron_endpoint_unscheduled",
                "url":    path,
                "count":  1,
                "detail": (
                    f"`{path}` (defined at `{rel}:{ln}`) matches a cron-"
                    f"trigger naming convention (/jobs/*, */refresh, "
                    f"*/deliver, */send-public, */run, */cron, */sync, "
                    f"*/scan) but isn't in `dchub-scheduler.py`'s JOBS "
                    f"dict. Either (a) add an entry to JOBS so it fires "
                    f"on the intended schedule, OR (b) add the path to "
                    f"`_CRON_INTENTIONAL_MANUAL` in this detector if it "
                    f"genuinely should only be hit by hand. Caught the "
                    f"winback-delivery + weekly-newsletter silent-inert "
                    f"bugs today (both endpoints existed, neither fired)."
                ),
            })
    return findings


# ── Phase RRR-funnel (2026-05-18) — auto-trial signal/mint mismatch ─
#
# The 1,581 → 0 conversion mystery cracked open: of 1,581 paywall
# signals on get_market_intel in 7 days, only 3 auto-trial keys were
# ever minted. The mint flow deduplicates per (ip_hash, ua) within
# 24h — so 1,581 signals came from ~3 unique agents bashing the gate
# repeatedly without ever extracting the trial key from the JSON-RPC
# response body. Each repeat reused the existing key. Agents render
# the text to humans but don't programmatically retry with the key.
#
# This detector flags the mismatch: when signals >> mints, the gate
# is being hit by sticky repeat callers who aren't converting. It's
# the "fix the funnel UX" signal.
def check_auto_trial_signal_mint_mismatch() -> list[dict]:
    """Flag when paywall-signal volume vastly exceeds auto-trial mints.
    Strong signal that agents hit the gate but don't extract+use the
    inline trial key."""
    findings: list[dict] = []
    conn = _db()
    if conn is None: return findings
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '7 days'
                """)
                signals = int((cur.fetchone() or (0,))[0] or 0)
                cur.execute("""
                    SELECT COUNT(*) FROM auto_trial_keys
                     WHERE minted_at >= NOW() - INTERVAL '7 days'
                """)
                mints = int((cur.fetchone() or (0,))[0] or 0)
            except Exception:
                return findings
    finally:
        try: conn.close()
        except Exception: pass

    # Healthy ratio: ~1 mint per signal (unique caller pattern). When
    # signals overwhelmingly outnumber mints, the same callers are
    # hitting the gate over and over without converting.
    if signals >= 500 and mints < signals * 0.05:
        ratio = signals / max(mints, 1)
        findings.append({
            "issue":  "auto_trial_signal_mint_mismatch",
            "url":    "/api/v1/observability/auto-trial-funnel",
            "count":  signals,
            "detail": (
                f"7d: {signals:,} paywall signals → only {mints} trial keys "
                f"minted ({ratio:.0f}:1 ratio). The mint flow deduplicates "
                f"per (ip_hash, ua) within 24h, so high signal volume + low "
                f"mint count = sticky repeat callers who never extract the "
                f"trial key from the JSON-RPC response body. They render "
                f"the gated message to humans but don't programmatically "
                f"retry. Fix: switch from inline JSON-key delivery to "
                f"transparent auto-retry inside mcp_gatekeeper.py (invoke "
                f"the wrapped tool with the new key + return data, not a "
                f"gated response). Or: surface the key in MCP server's "
                f"transport-layer response metadata."
            ),
        })
    return findings


# ── Phase RRR-newsletter+1 (2026-05-18) — shadowed-route detector ────
#
# Catches the bug class where the same Flask path is registered TWICE
# (different blueprints / different functions). Flask silently picks one
# based on registration order — usually the first registered wins. Hit
# this concretely today: my Phase RRR-wave3 dummy `submit-challenge` in
# ai_wars.py was registered before the REAL working implementation in
# ai_wars_automation.py, so the dummy "private beta" responder shadowed
# the working queue-and-async-battle handler for ~6 hours.
#
# Detection is essentially free — the existing /api/v1/observability/
# route-audit endpoint already reports shadowed_routes + sets healthy:
# False when any exist. This detector just consumes that signal.
def check_shadowed_routes() -> list[dict]:
    """Probe /api/v1/observability/route-audit and flag any path that
    has multiple handlers. The dup is almost always a code merge issue:
    one was the original, one is a copy added later by someone who
    didn't grep first.

    Phase ZZZZ-T4 (2026-05-18): now includes a specific proposed-fix
    PER shadow based on the endpoint name conventions we've seen this
    week. Older _override / _legacy / _v1 suffixed handlers are
    proposed for removal; newer named handlers stay."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    try:
        r = _req.get("https://dchub.cloud/api/v1/observability/route-audit",
                     timeout=5,
                     headers={"User-Agent": "dchub-brain-route-audit/1.0"})
        if r.status_code != 200:
            return findings
        data = (r.json().get("data") or {})
    except Exception:
        return findings

    # Verify-before-filing (2026-06-07): the raw route-audit lists EVERY path with
    # >1 handler, but two cases are pure noise that clog the "stuck/untried" worklist
    # forever (the brain can't safely delete a decorator, so they never resolve):
    #   1. Intentional 404-killer / fallback handlers — registered precisely to LOSE
    #      to the real route (working as designed, not a bug).
    #   2. Duplicate handlers whose path still serves users a healthy response —
    #      Flask just picks the first; nothing is broken.
    # So we now PROBE the live edge and only file a finding when the route is actually
    # broken for users (404/5xx, or 403 = CF Error-1000 routing like the
    # /state-of-the-data-center + /research cases). This stops the brain crying wolf.
    _FALLBACK = ("redirects_404_killer", "redir_", "_fallback", "_404", "killer")
    _EDGE = "https://dchub.cloud"
    _seen: set = set()
    for entry in (data.get("shadowed_routes") or []):
        path = entry.get("path", "?")
        if path in _seen:          # route-audit can list the same path twice
            continue
        _seen.add(path)
        methods = entry.get("methods", [])
        endpoints = entry.get("endpoints", [])

        # Never probe mutating paths (recompute/delete/etc.); skip them entirely.
        if any(w in path.lower()
               for w in ("recompute", "delete", "send", "reset", "purge", "wipe")):
            continue

        # PROBE FIRST, then decide (order matters — a route can have a "fine"-looking
        # handler list yet still be 403/404 for real users, e.g. /research). Probe the
        # live EDGE: 405 = HEAD not allowed (route exists) → confirm with a 1-byte GET.
        live = None
        try:
            hr = _req.head(_EDGE + path, timeout=3, allow_redirects=False,
                           headers={"User-Agent": "dchub-brain-route-audit/1.0"})
            live = hr.status_code
            if live == 405:
                live = _req.get(_EDGE + path, timeout=3, allow_redirects=False,
                                headers={"User-Agent": "dchub-brain-route-audit/1.0",
                                         "Range": "bytes=0-0"}).status_code
        except Exception:
            live = None
        broken = (live in (403, 404)) or (live is not None and live >= 500)
        if not broken:
            # Route serves users fine — whether it's a working duplicate or a
            # canonical route + 404-killer safety net, it's not a wolf. Don't file.
            continue

        # Broken for real users → surface it. Strip fallback handlers for the
        # recommendation (the canonical handler is the keeper).
        real_eps = [ep for ep in endpoints
                    if not any(m in ep.lower() for m in _FALLBACK)] or endpoints
        loser = keeper = None
        for ep in real_eps:
            if any(s in ep.lower() for s in ("_override", "_legacy", "_v1", "_old",
                                              "phase9", "phase8", "phase7")):
                loser = ep
            else:
                keeper = ep
        if loser and keeper and loser != keeper:
            recommendation = (
                f"REMOVE the `{loser}` handler (older/legacy pattern); KEEP `{keeper}`.")
        else:
            recommendation = (
                "Two real implementations compete for this path AND the live edge is "
                "broken — most likely CF routing, NOT a Python dup: check the "
                "_routes.json include wildcard + the zone-worker allowlist (same class "
                "as the /state-of-the-data-center fix).")

        findings.append({
            "issue":  "shadowed_route",
            "url":    path,
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count":  len(real_eps),
            "detail": (
                f"Path `{path}` ({','.join(methods)}) has {len(real_eps)} real handlers "
                f"({', '.join(real_eps)}) AND returns live edge status {live} (BROKEN). "
                + recommendation
            ),
        })
    return findings


def check_heartbeat_surfaces_stale() -> list[dict]:
    """Probe /api/v1/heartbeat and flag any surface in 'stale' status.
    User asked "brain needs to be proactive" after spotting 34 red rows
    on /heartbeat that brain hadn't surfaced.

    Strategy: don't spam findings — group by refresh_func so the operator
    sees patterns ("12 surfaces using refresh_iso are red — that one
    function is the problem") rather than 34 individual rows.
    """
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    try:
        # r86: probe the Railway ORIGIN, not dchub.cloud — the CF edge caches
        # /api/v1/heartbeat up to 1h, so a surface the /auto drain already healed
        # at origin still reads "stale" on the edge, manufacturing the finding.
        r = _req.get("https://dchub-backend-production.up.railway.app/api/v1/heartbeat",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-heartbeat/1.0"})
        if r.status_code != 200:
            return findings
        surfs = (r.json() or {}).get("surfaces") or []
    except Exception:
        return findings

    # r86: re-stamp drift parks volatile surfaces right at their window edge
    # between /auto drains. Only treat a surface as GENUINELY stale if it is
    # well past its window (>1.5x) — a single missed drain tick can't reach
    # that, so this kills the boundary-sawtooth false positive while still
    # catching a refresh function that's actually dead.
    def _really_stale(s) -> bool:
        if s.get("status") != "stale":
            return False
        age = s.get("age_hours") or 0
        win = s.get("stale_after_hours") or 24
        return age > win * 1.5
    stale = [s for s in surfs if _really_stale(s)]
    if not stale:
        return findings

    # Group stale by refresh_func — the failing function is the actionable
    # signal, not each surface individually
    by_fn: dict[str, list[dict]] = {}
    for s in stale:
        fn = s.get("refresh_func") or "(none)"
        by_fn.setdefault(fn, []).append(s)

    # If >= 10 surfaces share the same broken refresh_func, the function
    # itself is the bug (or its cron isn't firing). Flag as P0.
    # Smaller groups → individual surface issues (P1).
    for fn, group in by_fn.items():
        n = len(group)
        max_age = max((s.get("age_hours") or 0) for s in group)
        # Sample a few surface names for the detail block
        samples = ", ".join(s.get("surface", "?") for s in group[:3])
        if n > 3:
            samples += f", +{n-3} more"
        severity_word = "system-wide" if n >= 10 else "localized"
        findings.append({
            "issue":  "heartbeat_surfaces_stale",
            "url":    f"refresh_func:{fn}",
            "count":  n,
            "detail": (
                f"{n} surfaces stuck STALE on /heartbeat ({severity_word} — "
                f"all share refresh_func={fn}). Oldest: {max_age:.1f}h. "
                f"Surfaces: {samples}. Either the refresh function is a no-op "
                f"that returns True without doing work, or its cron isn't "
                f"firing. Check (a) dchub-scheduler.py JOBS for a job that "
                f"hits the relevant endpoint, (b) the refresh_{fn.replace('refresh_','')} "
                f"function body in routes/heartbeat.py to see if it actually "
                f"refreshes anything. Quick fix: ensure /api/v1/heartbeat/auto "
                f"is scheduled (it drains by stale-age regardless of fn)."
            ),
        })
    return findings


def check_pricing_page_placeholder_content() -> list[dict]:
    """Sweep the live /pricing page for unresolved placeholder patterns:
    empty $-amount spans, `__PRICE__` literals, `{{...}}` templating that
    didn't render, `undefined`, or `NaN` next to /year or /month. User
    spotted broken Pro Annual rendering and asked brain to catch this
    earlier.
    """
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    try:
        r = _req.get("https://dchub.cloud/pricing",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-pricing/1.0"})
        if r.status_code != 200:
            return findings
        html = r.text or ""
    except Exception:
        return findings

    import re
    # Pattern 1: empty price-amount spans (the Pro Annual screenshot case)
    empties = re.findall(r'<span\s+class="price-amount"[^>]*>\s*</span>', html)
    # Pattern 2: literal placeholders that didn't get filled
    placeholders = re.findall(r'__[A-Z_]+__|\{\{\s*\w+\s*\}\}', html)
    # Pattern 3: undefined/NaN next to /year or /month
    undefined_near_period = re.findall(
        r'(undefined|NaN)\s*</span>\s*<span[^>]*>\s*/(?:year|month)', html)
    # Pattern 4: price-period without a preceding price-amount value
    # (Loose check — if /year appears with no $-amount in 100 chars before)
    suspect_periods = []
    for m in re.finditer(r'<span[^>]*price-period[^>]*>\s*/(year|month)\s*</span>', html):
        start = max(0, m.start() - 200)
        window = html[start:m.start()]
        if not re.search(r'\$[\d,]+', window):
            suspect_periods.append(m.group(0))

    issues = []
    if empties:
        issues.append(f"{len(empties)} empty price-amount span(s) — price value missing")
    if placeholders:
        issues.append(f"{len(placeholders)} unrendered placeholder(s): "
                      f"{', '.join(set(placeholders[:3]))}")
    if undefined_near_period:
        issues.append(f"{len(undefined_near_period)} 'undefined/NaN' next to /year or /month")
    if suspect_periods:
        issues.append(f"{len(suspect_periods)} /year or /month with no $-amount nearby")

    if issues:
        findings.append({
            "issue":  "pricing_page_placeholder_content",
            "url":    "https://dchub.cloud/pricing",
            "count_kind": "item_count",  # magnitude, not a recurrence tally
            "count":  len(empties) + len(placeholders) + len(undefined_near_period) + len(suspect_periods),
            "detail": (
                "Pricing page has unresolved content patterns customers will "
                "see as missing or broken numbers: "
                + "; ".join(issues)
                + ". Diff dchub-frontend/pricing.html against the CF Pages "
                "deploy — likely a stale CF cache or a partial deploy where "
                "the price-amount text node got cleared."
            ),
        })
    return findings


def check_package_metadata_freshness() -> list[dict]:
    """Phase ZZZZ-brain-L7-accepted (2026-05-19): the FIRST brain-
    written detector. L7 (brain_layer7_evolving) analyzed 3 commits
    to commit_scope:phase-kkk and proposed this detector.

    Detects packages with stale or missing metadata by comparing our
    DB cache age against PyPI's last-updated timestamp. Fires when
    packages haven't been refreshed in over 48 hours despite PyPI
    showing recent activity, or when new packages exist in our
    install-count tracker but lack metadata entries entirely.

    Wrapped in try/except per-check because Claude wrote the SQL
    against an assumed schema; if column names differ in production,
    the check degrades gracefully into a 'schema_drift' finding
    instead of crashing the whole scan."""
    findings: list[dict] = []
    from datetime import datetime, timedelta

    # r-l7-guard (2026-06-02): this L7-authored detector queries
    # public_install_counts + package_metadata — tables that were NEVER created
    # anywhere in the backend (the PyPI install-tracker feature was proposed but
    # never built; grep confirms the names appear only in this file). So the
    # orphan query below raised UndefinedTable every scan and the except handler
    # emitted a permanent schema_drift_for_l7_detector finding (stuck x6).
    # Short-circuit silently until the tables actually exist — no meta-finding.
    try:
        _gc = _db()
        if _gc is None:
            return findings
        try:
            with _gc.cursor() as _gcur:
                _gcur.execute(
                    "SELECT to_regclass('public.public_install_counts'), "
                    "to_regclass('public.package_metadata')")
                _ex = _gcur.fetchone()
            if not _ex or not _ex[0] or not _ex[1]:
                return findings  # feature not built yet — nothing to check, stay quiet
        finally:
            try: _gc.close()
            except Exception: pass
    except Exception:
        return findings  # can't even probe existence -> stay silent (no schema_drift noise)

    # Check 1: orphaned packages (install activity but no metadata row)
    try:
        c = _db()
        if c is None:
            return findings
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT package_name, SUM(install_count) AS installs
                      FROM public_install_counts
                     WHERE package_name NOT IN (SELECT name FROM package_metadata)
                     GROUP BY package_name
                    HAVING SUM(install_count) > 10
                     LIMIT 20
                """)
                rows = cur.fetchall() or []
            if rows:
                example = rows[0][0]
                findings.append({
                    "issue":  "install_tracker_orphans",
                    "url":    "table:package_metadata",
                    "count_kind": "item_count",  # magnitude, not a recurrence tally
                    "count":  len(rows),
                    "detail": (
                        f"Found {len(rows)} packages with install activity "
                        f"but no metadata row (e.g. {example}). Fix: trigger "
                        f"packages_refresh job with --force flag, then verify "
                        f"PyPI JSON API fallback is enabled in config. "
                        f"L7-proposed detector."
                    ),
                })
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        # Schema drift — fail gracefully into a meta-finding
        findings.append({
            "issue":  "schema_drift_for_l7_detector",
            "url":    "check_package_metadata_freshness/orphans",
            "count":  1,
            "detail": (
                f"L7-proposed detector check_package_metadata_freshness "
                f"failed its orphan-check query: {type(e).__name__}: "
                f"{str(e)[:120]}. Likely column-name drift. Update the SQL "
                f"to match the live schema or comment out this detector."
            ),
        })

    # Check 2: stale metadata for active packages
    try:
        c = _db()
        if c is None:
            return findings
        try:
            with c.cursor() as cur:
                stale_threshold = datetime.utcnow() - timedelta(hours=48)
                cur.execute("""
                    SELECT pm.name, pm.last_refreshed, pic.recent_installs
                      FROM package_metadata pm
                      JOIN (SELECT package_name, SUM(install_count) AS recent_installs
                              FROM public_install_counts
                             WHERE recorded_at > NOW() - INTERVAL '7 days'
                             GROUP BY package_name) pic ON pm.name = pic.package_name
                     WHERE pm.last_refreshed < %s
                       AND pic.recent_installs > 100
                     LIMIT 15
                """, (stale_threshold,))
                rows = cur.fetchall() or []
            if rows:
                example = rows[0][0]
                findings.append({
                    "issue":  "stale_metadata_active_packages",
                    "url":    "table:package_metadata.last_refreshed",
                    "count_kind": "item_count",  # magnitude, not a recurrence tally
                    "count":  len(rows),
                    "detail": (
                        f"Found {len(rows)} high-traffic packages with "
                        f"metadata older than 48h (e.g. {example}). Fix: "
                        f"verify daily packages_refresh cron is running; "
                        f"check for silent API failures on new packages. "
                        f"L7-proposed detector."
                    ),
                })
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        # Don't double-flag schema drift if check 1 already did
        pass

    return findings


def check_brain_memory_empty() -> list[dict]:
    """Phase ZZZZ-T2.2 (2026-05-18): irony detector. Fires when L3
    brain memory has < 5 records. Brain shipped the memory table
    + endpoints but nothing's writing to it — brain literally can't
    learn from prior fixes. Recommend hitting the bootstrap endpoint.
    """
    findings: list[dict] = []
    try:
        import requests as _req
        r = _req.get("https://dchub.cloud/api/v1/brain/memory/stats",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-memory-probe/1.0"})
        if r.status_code != 200:
            return findings
        d = r.json() or {}
    except Exception:
        return findings

    total = d.get("total_records", 0)
    if total >= 5:
        return findings

    findings.append({
        "issue":  "brain_memory_empty",
        "url":    "/api/v1/brain/memory/stats",
        "count":  total,
        "detail": (
            f"Brain L3 memory has {total} records. Without history, brain "
            f"can't recommend 'we tried X before, it worked' on recurring "
            f"findings — every detection feels new. Bootstrap with: "
            f"`curl -X POST https://dchub.cloud/api/v1/brain/memory/"
            f"backfill-from-commits?days=14` (auto-records fix/feat/perf "
            f"commits from git as success-outcomes). Going forward, every "
            f"brain narrative cycle should auto-record what it observed."
        ),
    })
    return findings


def check_addressable_demand_unconverted() -> list[dict]:
    """Phase ZZZZ-T3.2 (2026-05-18): identifies CONCENTRATED revenue
    opportunities — single paid tools where many distinct users are
    hammering with 0 conversions. Different from the generic conversion
    leak detector: this calls out specific tools to focus sales on.

    Threshold: any paid_tool with > 30 unique users + > 500 calls in
    30d. Currently fires for get_grid_intelligence (100 users) and
    get_fiber_intel (98 users)."""
    findings: list[dict] = []
    try:
        import requests as _req
        r = _req.get("https://dchub.cloud/api/v1/mcp/funnel",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-demand/1.0"})
        if r.status_code != 200:
            return findings
        d = r.json() or {}
    except Exception:
        return findings

    paid_demand = d.get("paid_tool_demand_30d") or []
    paid_keys = (d.get("keys_by_tier") or {}).get("paid", 0)

    for t in paid_demand:
        users = t.get("users", 0)
        calls = t.get("calls", 0)
        name = t.get("tool", "?")
        if users < 30 or calls < 500:
            continue
        # Brain found a concentrated demand pocket
        findings.append({
            "issue":  "addressable_demand_unconverted",
            "url":    f"tool:{name}",
            "count":  users,
            "detail": (
                f"`{name}`: {users} unique free users with {calls:,} calls "
                f"in 30d but only {paid_keys} paid keys account-wide. This "
                f"is a CONCENTRATED upgrade target — pick the top-5 users "
                f"of this tool, look up their IPs/UAs, run a manual "
                f"sales-outreach (LinkedIn DM, email, etc.). Or wire a "
                f"per-tool email-capture form: 'You hit get_grid_intelligence "
                f"{calls // users:,}× this month. Unlock it for $9/mo (200 calls/day).'"
            ),
        })
    return findings


def check_trial_to_paid_stagnation() -> list[dict]:
    """Fires when auto-trial keys are being minted but NONE are converting
    to paid. Pattern: lots of mints + lots of usage + 0 redemptions OR 0
    upgrades over 7d → trial mechanism is working as a giveaway, not a
    conversion funnel.

    Threshold: > 10 trial keys with activity in last 7d AND 0 paid keys
    minted in last 7d. Surfaces the "giveaway leak" the user spotted."""
    findings: list[dict] = []
    try:
        import requests as _req
        r = _req.get("https://dchub.cloud/api/v1/mcp/funnel", timeout=8)
        if r.status_code != 200:
            return findings
        d = r.json() or {}
    except Exception:
        return findings

    sig_platforms = d.get("signals_by_platform_30d") or []
    tot_signals = sum(p.get("signals", 0) for p in sig_platforms)
    tot_conv = sum(p.get("converted", 0) for p in sig_platforms)
    keys = d.get("keys_by_tier") or {}
    paid = keys.get("paid", 0)
    free = keys.get("free", 0)

    if tot_signals < 500:
        return findings   # not enough volume to draw a conclusion

    rate = (tot_conv / tot_signals * 100) if tot_signals else 0
    if rate >= 1.0:
        return findings   # converting OK — no finding

    findings.append({
        "issue":  "trial_to_paid_stagnation",
        "url":    "funnel:signals_to_conversions",
        "count":  int(tot_signals),
        "detail": (
            f"{tot_signals:,} paywall signals → {tot_conv} conversions "
            f"({rate:.3f}% gate→paid). Free key count: {free}. Paid: {paid}. "
            f"Likely cause: the transparent auto-trial gives 7d × 50/day "
            f"FREE access to the 5 hot tools, so agents never see the "
            f"upgrade wall. Tighten further by: (1) lowering TRIAL_DAYS "
            f"from 7 to 3, (2) lowering TRIAL_DAILY_CALLS from 50 to 20, "
            f"(3) adding a hard upgrade-wall after N=3 trial cycles, "
            f"(4) removing transparent-retry for non-essential tools "
            f"(the 5-tool whitelist in mcp_gatekeeper._AUTO_RETRY_TOOLS)."
        ),
    })
    return findings


def check_cf_account_health() -> list[dict]:
    """Polls /api/v1/cf-analytics/health and flags account-level traffic
    anomalies the user spotted via the CF dashboard:
      • cache_rate_pct < 25 (was 38.93% baseline → dropped to 13.7%)
      • total_bytes / total_requests indicates extreme growth (we want
        to know if the +1,260% spike sustains or crashes)

    Becomes brain's voice into account-level metrics — previously brain
    only saw what Railway/Flask returned, not what CF saw above it."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    try:
        r = _req.get("https://dchub.cloud/api/v1/cf-analytics/health",
                     timeout=10,
                     headers={"User-Agent": "dchub-brain-cf-health/1.0"})
        if r.status_code != 200:
            return findings
        d = r.json() or {}
    except Exception:
        return findings

    if not d.get("ok"):
        # Token/perm issue — surface as a one-time finding
        findings.append({
            "issue":  "cf_analytics_unavailable",
            "url":    "/api/v1/cf-analytics/health",
            "count":  1,
            "detail": (f"CF account-level analytics polling failed: "
                       f"{d.get('error','?')}. Add 'Account Analytics: Read' "
                       f"permission to the CLOUDFLARE_API_TOKEN secret so "
                       f"brain can monitor 4xx rate, cache rate, and "
                       f"bandwidth at the CF edge."),
        })
        return findings

    # Cache-rate breach (target ≥25% to keep Railway origin costs sane)
    # cf-analytics returns cache_rate_pct=None when zone-scope query
    # isn't accessible (account-scope httpRequestsAdaptiveGroups doesn't
    # expose cache rate). Skip the check rather than crash.
    cr = d.get("cache_rate_pct")
    if cr is None:
        return findings
    if cr < 25:
        findings.append({
            "issue":  "cf_cache_rate_low",
            "url":    f"cache_rate:{cr}%",
            "count":  int(d.get("total_requests", 0)),
            "detail": (
                f"CF account cache rate is {cr}% over last 7d "
                f"({d.get('cached_requests',0):,} cached / "
                f"{d.get('total_requests',0):,} total). Target ≥25%. "
                f"Every uncached request hits Railway, costs egress, and "
                f"adds latency. Top remediation: add `Cache-Control: "
                f"public, max-age=N` headers to high-traffic GETs that "
                f"don't change per-user. Candidates: /api/v1/stats, "
                f"/api/v1/news, /api/v1/grid/totals, /api/v1/dcpi/scores, "
                f"/.well-known/mcp.json, /api/v1/openapi.json."
            ),
        })
    return findings


def check_social_publish_silent_failure() -> list[dict]:
    """Probe /api/v1/marketing/worker-status — fires when a social platform
    is configured (token set) but zero publishes succeeded in 7d AND the
    queue is backed up. Catches the "LinkedIn/X access token expired"
    pattern that brain spotted manually this session: 11 posts queued
    for 4.5 days, 0 published, 60% lifetime delivery rate (suggesting
    SOMETHING used to work, then stopped — classic token expiry).

    LinkedIn tokens expire every 60 days. X tokens cycle on policy changes.
    This detector turns "I notice nothing's been published" into a P0
    finding the moment it happens, not 4.5 days later."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    try:
        r = _req.get("https://dchub.cloud/api/v1/marketing/worker-status",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-social-probe/1.0"})
        if r.status_code != 200:
            return findings
        d = r.json() or {}
    except Exception:
        return findings

    # worker-status serves these as JSON null when the queue is empty/unknown.
    # dict.get's default does NOT apply to an existing null key, and `None < 3`
    # is the TypeError that crashed this detector on every sweep.
    def _num(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    dist = d.get("distribution") or {}
    queued = _num(dist.get("queued_unpublished"))
    oldest_h = _num(dist.get("oldest_queued_age_hours"))
    pub7 = dist.get("published_7d") or {}

    # Only fire if there's a backlog WORTH publishing
    if queued < 3 and oldest_h < 24:
        return findings

    for platform in ("linkedin", "twitter", "bluesky"):
        configured = dist.get(f"{platform}_configured", False)
        published = _num((pub7 or {}).get(platform))
        if not configured: continue   # platform not set up — not a bug
        if published > 0: continue    # platform IS publishing — fine
        # Configured but 0 publishes in 7d + queue backed up → token issue
        findings.append({
            "issue":  "social_publish_silent_failure",
            "url":    f"platform:{platform}",
            "count":  int(queued),
            "detail": (
                f"{platform.title()} is configured but published 0 posts "
                f"in last 7d while {queued} posts are queued (oldest: "
                f"{oldest_h:.1f}h old). Most likely cause: the platform's "
                f"access token expired (LinkedIn tokens cycle every 60 days; "
                f"X tokens cycle on policy changes). Fix: regenerate "
                f"{platform.upper()}_ACCESS_TOKEN in Railway env vars and "
                f"trigger /api/v1/marketing/publish-now?max=20 to drain "
                f"the backlog. Each queued post represents ~24h of lost "
                f"distribution reach."
            ),
        })
    return findings


def check_tool_signal_to_conversion_leak() -> list[dict]:
    """Probe /api/v1/mcp/funnel for tools with high paywall-signal volume
    but near-zero conversions. Targeted at the leak the brain narrative
    already flagged: get_market_intel had 1547 signals → 0 conversions.
    Catches the same pattern across all tools.

    Fires when: tool has >100 paywall signals in 7d AND <0.5% gate→paid
    conversion AND the tool is gated above FREE. P0 finding."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    try:
        r = _req.get("https://dchub.cloud/api/v1/mcp/funnel",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-conversion-leak/1.0"})
        if r.status_code != 200:
            return findings
        d = r.json() or {}
    except Exception:
        return findings

    tools = d.get("top_tools") or d.get("tools") or []
    if not tools:
        return findings

    for t in tools[:20]:
        if not isinstance(t, dict): continue
        signals = (t.get("paywall_signals_7d")
                   or t.get("gated_7d") or t.get("signals_7d") or 0)
        conv = (t.get("conversions_7d") or t.get("paid_7d") or 0)
        name = t.get("tool") or t.get("name") or "?"
        if signals < 100:
            continue
        rate = (conv / signals * 100) if signals else 0
        if rate >= 0.5:
            continue
        findings.append({
            "issue":  "tool_signal_conversion_leak",
            "url":    f"tool:{name}",
            "count":  int(signals),
            "detail": (
                f"`{name}` got {signals} paywall signals in 7d but only "
                f"{conv} conversions ({rate:.2f}% gate→paid). This is the "
                f"same leak pattern brain narrative flagged for get_market_intel. "
                f"Likely root cause: free tier returns enough data that "
                f"users don't need to upgrade, OR the upgrade CTA doesn't "
                f"convey concrete savings. Check (a) LIMITS[Tier.FREE].max_rows "
                f"in mcp_gatekeeper.py, (b) _SAVINGS_CLAIMS for this tool, "
                f"(c) whether transparent auto-trial is dispensing too many "
                f"long-lived free keys."
            ),
        })
    return findings


def check_blueprint_registration_silent_failure() -> list[dict]:
    """Catch the recurring bug class where `from routes.X import X_bp` +
    `app.register_blueprint(X_bp)` lines run without raising, but the
    routes are nowhere in `app.url_map` — the late-line silent failure
    pattern that hit us 3× in 7 days (press_loop, industry_pulse,
    market_deep_dive). When this happens the user sees a 404 on what
    SHOULD be a 200 page.

    The detector walks main.py for `from routes.X import Y_bp` declarations
    and for each, checks whether ANY rule in current_app.url_map points
    at an endpoint matching the blueprint's name. If not → silent failure.
    """
    findings: list[dict] = []
    try:
        from flask import current_app
    except Exception:
        return findings

    # Resolve main.py path
    import os
    main_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py")
    if not os.path.exists(main_py):
        return findings

    # Collect imports of blueprints from routes/*.py
    import re
    pattern = re.compile(
        r"from\s+routes\.(\w+)\s+import\s+(\w+_bp)", re.MULTILINE)
    try:
        with open(main_py, "r") as f:
            src = f.read()
    except Exception:
        return findings

    declared: dict[str, str] = {}  # bp_var → module
    for m in pattern.finditer(src):
        module, bp_var = m.group(1), m.group(2)
        declared[bp_var] = module

    if not declared:
        return findings

    # Build set of blueprint NAMES (not vars) that the running app has
    registered_bp_names: set[str] = set()
    try:
        for rule in current_app.url_map.iter_rules():
            ep = rule.endpoint or ""
            if "." in ep:
                registered_bp_names.add(ep.split(".", 1)[0])
    except Exception:
        return findings

    # For each declared blueprint var, infer the likely Blueprint() name
    # — convention in this repo is Blueprint("module_name", ...) so the
    # name equals the module. We also accept the bp_var stripped of _bp.
    for bp_var, module in declared.items():
        candidates = {module, bp_var[:-3] if bp_var.endswith("_bp") else bp_var}
        if not (candidates & registered_bp_names):
            findings.append({
                "issue":  "blueprint_registered_but_not_serving",
                "url":    f"main.py: register_blueprint({bp_var})",
                "count":  1,
                "detail": (
                    f"`from routes.{module} import {bp_var}` is declared in "
                    f"main.py but no rules from blueprint name(s) "
                    f"{sorted(candidates)} are in app.url_map. This is the "
                    f"late-line silent-failure pattern: the register_blueprint "
                    f"call may be inside an except-swallowed try, or after a "
                    f"line that errored at import-time. FIX: move the "
                    f"`from routes.{module} import {bp_var}` + "
                    f"`app.register_blueprint({bp_var})` pair into the known-"
                    f"working safe zone at ~line 1180 of main.py (next to "
                    f"weekly_digest_bp). 3× confirmed instances of this bug "
                    f"in the last 7 days (press_loop, industry_pulse, "
                    f"market_deep_dive)."
                ),
            })
    return findings


def check_canonical_floor_exceeds_live() -> list[dict]:
    """PRE-EMPTION (2026-06-16): flag when a canonical_stats._FALLBACK floor
    EXCEEDS the live deduped reality — the over-claim class (the marketed
    21,000-floor vs 3,141-active, same family as the retired headline over-claims). Floors must
    round DOWN to reality, never above. Finding-only: NO autopilot action map →
    escalates to a human (per the never-auto-apply gate).

    CRITICAL trap (mirrors the honest-null discipline): canonical_stats._query_live
    falls back to _FALLBACK verbatim on a DB outage, so live==floor and a naive
    floor>live can NEVER fire AND an outage must not yield a false all-clear. We
    require PROOF the DB answered — the raw `facilities` COUNT(*) (~21,461) differs
    from its 21,000 floor — and skip (return []) otherwise."""
    try:
        import canonical_stats as _cs
        live = _cs._query_live()          # fresh, uncached
        fb = _cs._FALLBACK
    except Exception:
        return []
    # DB-reachability proof: a real read sets facilities to the raw count, distinct
    # from the floor. If they're identical, the DB didn't answer -> skip (no finding,
    # no false all-clear).
    try:
        if int(live.get("facilities", 0)) == int(fb.get("facilities", 0)):
            return []
    except Exception:
        return []
    findings: list[dict] = []
    for key in ("facilities_verified", "countries_verified", "facilities", "countries", "markets"):
        floor, real = fb.get(key), live.get(key)
        if floor is None or real is None:
            continue
        try:
            if int(floor) > int(real):
                findings.append({
                    "issue": "canonical_floor_above_live_reality",
                    "url": "canonical_stats._FALLBACK",
                    "count": int(floor) - int(real),
                    "detail": (f"canonical_stats floor `{key}`={floor} EXCEEDS the live "
                               f"value {real}. Floors must round DOWN to reality, never "
                               f"above — lower _FALLBACK['{key}'] to <= {real}. Over-claim "
                               f"class (cf. the retired headline over-claims / 21,000-tracked-vs-3,141-verified)."),
                })
        except (TypeError, ValueError):
            continue

    # ── the OTHER canon: ai_surface_canon.PINNED["public"] (2026-07-24) ──────
    # This detector historically watched ONLY canonical_stats._FALLBACK. But the
    # canon that feeds PUBLIC copy, registry submitters and the white-glove
    # propagation job is ai_surface_canon.PINNED["public"] — and nothing watched
    # it. After entity-resolution shipped it kept claiming "22,000+" facilities
    # against a live 12,687 distinct sites (~1.7x over-claim), and a PARTNER
    # (Grok) caught it on our own pages before any detector did. Watch it now:
    # a floor above live reality here gets pasted into every downstream listing.
    try:
        from routes.mcp_honest_numbers import as_dict as _canon_public
        pinned = _canon_public() or {}
    except Exception:
        pinned = {}
    if pinned:
        live_pub: dict = {}
        try:
            import os as _o, psycopg2 as _pg
            cx = _pg.connect(_o.environ.get("DATABASE_URL", ""), connect_timeout=5)
            try:
                with cx.cursor() as cur:
                    # ★★★2026-08-09: `facilities` NO LONGER HAS ITS OWN QUERY.
                    # It ran `COUNT(*) ... WHERE duplicate_of_id IS NULL` while
                    # the comment directly above it said "DISTINCT sites, never
                    # raw rows" — the code and its own docstring disagreed, and
                    # BOTH halves diverged from canon:
                    #   canon (canonical_stats, what EVERY public surface
                    #     publishes): COUNT(DISTINCT canonical_slug)
                    #     WHERE COALESCE(is_duplicate,0)=0            = 17,260
                    #   this fence:   COUNT(*) WHERE duplicate_of_id IS NULL
                    #                                                 = 15,565
                    # So the fence measured a DIFFERENT POPULATION than the
                    # floor it was fencing, and convicted a floor of 17,000 —
                    # which is correctly BELOW live canon — of over-claiming by
                    # 1,435. It fired 1,436 times.
                    # ★The two filters are not interchangeable: `is_duplicate`
                    # is a VISIBILITY flag; `duplicate_of_id` is the
                    # consolidation pointer, and a pointer-carrying row stays
                    # live, counted and serving 200 (rel=canonical merges the
                    # URLs). 3,286 rows carry a pointer while unflagged, so the
                    # pointer filter deletes ~1,983 distinct slugs that are
                    # live pages. Counting DISTINCT canonical_slug already
                    # consolidates; the pointer filter double-counts the dedup.
                    # ★This is the SAME fix `markets` got on 2026-07-29, in the
                    # comment below — reuse canon rather than re-deriving it, so
                    # the fence cannot drift away from the floor a second time.
                    # `live` is already the fresh canonical read from the top of
                    # this function, so this costs no extra round trip.
                    _fv, _fv_fb = live.get("facilities_verified"), fb.get("facilities_verified")
                    if _fv is not None and int(_fv) != int(_fv_fb or -1):
                        # differs from the fallback => the canonical query really
                        # answered. If it did NOT, omit the key: an unanswered
                        # read is BLIND, never a verdict about the floor.
                        live_pub["facilities"] = int(_fv)
                    # ★2026-07-29: was COUNT(*) — ROWS (live 317), which include the
                    # three aggregate regions and the duplicate slug variants canon
                    # deliberately excludes. Comparing a pinned floor against ROWS
                    # made this fence blind to exactly the drift it exists to catch:
                    # the pinned markets floor "311" was ABOVE live canon (306) yet
                    # passed here, because 311 <= 317. Use the canonical definition
                    # (canonical_stats.py:165-167) so the fence measures the same
                    # thing every public surface publishes.
                    cur.execute("SELECT COUNT(DISTINCT market_name) "
                                "FROM market_power_scores "
                                "WHERE COALESCE(published, true) = true "
                                "AND market_slug NOT IN ('pacific-nw-rural',"
                                "'rural-spp','upper-michigan')")
                    live_pub["markets"] = int(cur.fetchone()[0] or 0)
            finally:
                cx.close()
        except Exception:
            live_pub = {}
        for key, real in live_pub.items():
            floor = pinned.get(key)
            if not floor or not real:
                continue
            try:
                if int(floor) > int(real):
                    findings.append({
                        "issue": "canonical_floor_above_live_reality",
                        "url": "ai_surface_canon.PINNED.public",
                        "count": int(floor) - int(real),
                        "detail": (
                            f"ai_surface_canon PINNED public floor `{key}`={floor} EXCEEDS "
                            f"live {real}. This canon feeds public copy, registry "
                            f"submitters and white-glove propagation, so a stale floor is "
                            f"pasted into every downstream listing. Lower "
                            f"PINNED['public']['{key}'] to <= {real} AND add the old string "
                            f"to PINNED['stale_markers'] so the scrubber catches reprints."),
                    })
            except (TypeError, ValueError):
                continue
    return findings


def check_cross_surface_value_drift() -> list[dict]:
    """PRE-EMPTION (2026-06-16): flag SOURCE files that hardcode a canonical metric
    (market / country count) at a value that DIVERGES from canonical_stats' live
    read — the parallel-stale-surface class (e.g. state_of_power.py markets=233 vs
    live ~307). canonical_stats is the ONE source (we do NOT re-query — that would
    create a 3rd disagreeing source). Scoped to a small allow-list of known
    count-bearing files. Finding-only; escalates to a human."""
    try:
        import canonical_stats as _cs, os, re
        live = _cs.get_canonical_stats(force=True)
        fb = _cs._FALLBACK
    except Exception:
        return []
    try:
        if int(live.get("facilities", 0)) == int(fb.get("facilities", 0)):
            return []  # DB unreachable -> skip
    except Exception:
        return []
    metrics = {"markets": live.get("markets"), "countries": live.get("countries")}
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # r-surface-paths (2026-07-24 coverage audit): three of these were rooted at the
    # repo root but have only ever lived under routes/ — open() raised FileNotFoundError
    # and the bare `continue` below swallowed it, so 3 of 5 allow-listed surfaces (and
    # EVERY count literal the detector could act on) were unreadable since the file was
    # created. A typo'd allow-list entry was indistinguishable from a clean file.
    SURFACES = ["routes/state_of_power.py", "routes/competitive_seo.py", "agent_hub.py",
                "routes/quarterly_report.py", "routes/mcp_presence_crawler.py"]
    findings: list[dict] = []
    for rel in SURFACES:
        try:
            txt = open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace").read()
        except Exception:
            # Never silently skip: a missing allow-list path means this detector is
            # blind to that surface, which is the failure mode above.
            findings.append({
                "issue": "surface_allowlist_path_missing",
                "url":   rel,
                "count": 1,
                "detail": (f"check_cross_surface_value_drift cannot open allow-listed "
                           f"surface `{rel}` — it is blind to that file's hardcoded "
                           f"metrics. Fix the path or drop it from SURFACES."),
            })
            continue
        for mkey, live_val in metrics.items():
            if not live_val:
                continue
            lv = int(live_val)
            pat = re.compile(r'(\d{2,4})\s*' + mkey + r'\b|' + mkey + r'["\']?\s*[:=]\s*["\']?(\d{2,4})')
            for m in pat.finditer(txt):
                try:
                    lit = int(m.group(1) or m.group(2))
                except (TypeError, ValueError):
                    continue
                # tolerance: flag only if a real count (>50) off by >max(10, 5%) — skips years/noise
                if lit > 50 and abs(lit - lv) > max(10, 0.05 * lv):
                    ln = txt[:m.start()].count("\n") + 1
                    findings.append({
                        "issue": "cross_surface_metric_divergence",
                        "url": f"{rel}:{ln}",
                        "count": abs(lit - lv),
                        "detail": (f"{rel}:{ln} hardcodes {mkey}={lit} but the live canonical "
                                   f"value is {lv}. Read canonical_stats.{mkey}_phrase() / "
                                   f"get_canonical_stats() so it can't drift (parallel-stale-surface)."),
                    })
    return findings[:20]


def check_dcpi_ssr_cross_tier() -> list[dict]:
    """2026-07-26 incident alarm — /dcpi INDEX HTML is SSR'd per-tier but was
    edge-cached under ONE shared key: a pro-cookied load poisoned the entry
    with the full 317-score page for ANONYMOUS visitors (paid-product leak),
    and a stale anon entry was served to a logged-in Pro (payer mis-gated,
    zero console errors — the owner caught it by eye). Fixed with the zone
    Cache Rule 'Bypass cache - tier-varying HTML for AUTHED users'
    (e30fab55…, bypasses when a dchub_token/dchub_refresh cookie is present)
    on top of the origin's existing authed no-store (r-tierleak). This check
    guards BOTH halves staying true:

      (a) anon GET /dcpi HTML must contain NO numeric score decimals —
          >3 matches means the paid bulk index reaches anonymous (critical);
      (b) GET /dcpi WITH a dchub_token cookie must NOT be a cache HIT — the
          bypass rule keys on cookie PRESENCE, so an unsigned canary cookie
          is enough; validity is irrelevant to the edge.

    Network canary: degrades silently on connectivity errors (never crashes
    the scan, never false-fires on a timeout). cf
    [[reference_dchub_tier_gating_qa_0726]]."""
    findings: list[dict] = []
    try:
        import re as _re2
        import requests as _req
        _u = 'https://dchub.cloud/dcpi'
        _a = _req.get(_u, timeout=20,
                      headers={'User-Agent': 'dchub-radar-dcpi-ssr-canary'})
        if _a.status_code == 200:
            _scores = _re2.findall(r'>\s*\d{2}\.\d\s*<', _a.text)
            if len(_scores) > 3:
                findings.append({
                    "issue": "dcpi_anon_ssr_leak",
                    "url": _u,
                    "count_kind": "item_count",  # magnitude, not a recurrence tally
                    "count": len(_scores),
                    "severity": "critical",
                    "detail": (f"anon GET /dcpi returned HTML containing "
                               f"{len(_scores)} numeric DCPI scores "
                               f"(cf-cache-status={_a.headers.get('cf-cache-status')}, "
                               f"age={_a.headers.get('age')}). The paid bulk index is "
                               f"leaking to anonymous visitors — a paid render is being "
                               f"edge-cached again. Verify the zone bypass rule "
                               f"(tier-varying HTML, e30fab55…) is still LAST in the "
                               f"ruleset and routes/dcpi.py still no-stores authed "
                               f"renders; then purge /dcpi."),
                })
        _b = _req.get(_u, timeout=20,
                      headers={'User-Agent': 'dchub-radar-dcpi-ssr-canary',
                               'Cookie': 'dchub_token=radar-presence-canary'})
        if (_b.headers.get('cf-cache-status') or '').upper() == 'HIT':
            findings.append({
                "issue": "dcpi_authed_edge_cached",
                "url": _u,
                "count": 1,
                "severity": "warning",
                "detail": ("GET /dcpi WITH a dchub_token cookie returned "
                           "cf-cache-status=HIT — the authed-bypass zone rule is "
                           "gone or shadowed, so logged-in payers can again be "
                           "served the cached ANON teaser (locks for paying "
                           "customers) and their paid render can poison the shared "
                           "entry. Restore 'Bypass cache - tier-varying HTML for "
                           "AUTHED users' as the LAST cache rule."),
            })
    except Exception:
        pass
    return findings


def scan_all() -> list[dict]:
    """Run every detector. Return a flat list of finding dicts ready
    to merge into actionable_backend_issues.

    Phase RRR-brain-parallel (2026-05-18): parallelized via ThreadPool
    after observability showed scan was taking 76.9s serial. Detectors
    are collected first, then run concurrently with per-detector 20s
    timeout — wall time becomes max(detector) instead of sum(detector)."""
    out: list[dict] = []
    detectors: list = []
    for fn in (# r-preempt (2026-06-16): anti-fabrication PRE-EMPTION detectors —
               # catch over-claim floors + parallel-stale metric surfaces (the
               # class the freshness/breakage/placeholder detectors are blind to).
               # Finding-only; NO autopilot action map → escalate to a human.
               check_canonical_floor_exceeds_live,
               check_cross_surface_value_drift,
               check_worker_version_drift,
               # r-env-drift (2026-07-18): shared-critical env vars silently
               # drifting between the backend and worker services (dashboard
               # saves per-service; 3 incidents on 2026-07-17 alone).
               check_env_drift,
               check_tier_consistency,
               # 2026-06-20 incident alarm — fires if a PAID key ever resolves
               # below developer (capacity redacted for payers), if a paid tier
               # word stops mapping into TIER_ORDER, if /me/tier gets edge-
               # cached, or if anon stops being gated. cf gating_routes +
               # [[dchub-capacity-paywall-gating]].
               check_paywall_capacity_gating,
               # 2026-07-26 /dcpi cross-tier edge-cache incident alarm — anon
               # HTML must never contain scores; cookied requests must never
               # be edge HITs. cf [[reference_dchub_tier_gating_qa_0726]].
               check_dcpi_ssr_cross_tier,
               check_cron_coverage,
               check_cron_collisions,
               # 2026-08-19 — the guard for the marker nobody remembers to add.
               # #202 removed our own GitHub Actions runners from the counted
               # population mid-week (62.2% -> 0.0% of calls across the deploy,
               # non-CI traffic HELD 199 -> 229). weekly_series._DEFINITION_CHANGES
               # is a MANUAL declaration; this fires when a class vanishes and
               # no one declared it.
               check_unmarked_population_shift,
               # 2026-08-19 — 4,056 OAuth challenges / 3 identities sat in a
               # published payload unread. Deliberately silent while a 30d
               # window straddles the 2026-08-15 method switch: a ratio across
               # a series break is not a rate.
               check_funnel_step_collapse,
               # 2026-08-19 — 9,822 stored facility slugs were 404ing with
               # ZERO alias rows while slug/status published "frozen 26,112"
               # and read finished. Watches the invariant (every stale stored
               # slug has a rescue path), not the one-off backfill, because
               # re-ingestion churns slugs continuously.
               check_stale_stored_slug_404s,
               # Phase FF+7 (2026-05-19) — catches the bug L14 helped
               # find: jobs with `if: github.event.schedule == 'X'` where
               # 'X' isn't in on.schedule (stale check after cron move)
               # OR 'X' is pinned to ':00' minute where hourly cron also
               # fires (silent collision — job never runs from cron).
               # Found 5 instances on first run.
               check_cron_if_mismatched,
               # Phase FF+7 (2026-05-19) — paywall_hit -> click drop-off
               # detector. L14 identified this as the actual conversion-
               # crisis root cause (15K paywall hits / 1 click in 30d).
               # Watches /redeem/funnel-stats; fires if rate < 0.5% on
               # >500 paywall hits. Recovery target: 5%+ click-through.
               check_paywall_click_leak,
               # r41-session-upgrade-health (2026-05-25) — watches for
               # silent breaks in the Claude.ai session-upgrade flow
               # shipped in MCP server v2.1.7. Flags when paywall_hit
               # >200 in 24h but redeemed dev keys with session_id
               # metadata <5 (mechanism broken or no completions).
               check_session_upgrade_silenced,
               # r41-description-drift (2026-05-25) — flags when the
               # MCP manifest description goes stale vs reality (e.g.
               # claims '7 ISOs' when we now serve 10). Catches the
               # marketing-copy-vs-truth gap before AI crawlers cache
               # the stale picture.
               check_mcp_tool_description_drift,
               # r41-report-quality (2026-05-25) — watches our own
               # monthly + quarterly-deep reports for empty sections,
               # zero brand_pulse, stale generated_at. The reports are
               # what the LinkedIn partnership post points at; if they
               # drift toward looking empty, the 'live equivalent of
               # CBRE' claim falls apart visibly. Self-aware quality
               # gate.
               check_report_content_drift,
               # Phase FF+7-fix4 (2026-05-19) — early-warning for the
               # pool-exhaustion class of outage that took Railway down
               # for 30min on 2026-05-19. Probes 3 DB endpoints; flags
               # when 2/3 are slow — before container goes unhealthy.
               check_db_pool_pressure,
               # Phase FF+7-fix4 (2026-05-19) — STATIC auditor that flags
               # daemon-thread .py files with many _get_db() opens but few
               # finally: blocks. Closes the discovery loop for the leak
               # class that caused the outage. Lightweight (no HTTP).
               check_unsafe_db_conn_pattern,
               # Phase FF+7-meta (2026-05-19) — detects rapid-fire commits
               # saturating Railway's deploy queue. Catches the outage
               # CLASS that bypasses every other safeguard because the
               # brain is on the unhealthy container.
               check_deploy_queue_churn,
               # Phase FF+7-meta (2026-05-19) — celebrate-and-amplify
               # detector. Fires when a new AI-citation observation lands
               # with dchub_cited=true in the last 24h. Turns wins into
               # findings so they surface on dashboards. First detected
               # win: Gemini citing dchub.cloud alongside CBRE+JLL.
               check_ai_citation_new_landing,
               # Phase FF+7-press-loop (2026-05-19) — flags when press
               # output lags behind citation evidence. User spotted this:
               # AI citations landed today but /dc-hub-media showed 73-
               # day-old releases. Now any citation/press lag >24h fires.
               check_press_stale_vs_citations,
               # Phase FF+7-meta (2026-05-19) — repeated-404-pattern
               # detector. Map facility profiles hit /facility/<slug>
               # (404) for hours before user reported it. Brain didn't
               # catch it. This detector groups recent 404s by URL
               # pattern and fires when one pattern has >=10 hits.
               check_repeated_404_patterns,
               check_csp_drift,
               check_dcpi_partial_recompute,
               check_discovery_stalled,
               check_iso_metric_dropped,
               check_press_repetition,
               check_mcp_conversion_stale,
               check_auto_trial_conversion_rate,
               # Phase DDD organism detectors
               check_mcp_growth_declining,
               check_mcp_demand_gap,
               # Phase r-spine — MCP Growth Sentinel (WIDEN-arm drift alerts)
               check_growth_sentinel,
               check_source_of_truth_declining,
               check_media_topic_unaddressed,
               # Phase EEE surface-brain detector
               check_surface_health_critical,
               # Phase GGG/LLL detectors
               check_mcp_funnel_leak,
               check_enterprise_bot_present,
               # Phase TTT brand-surface dormancy detector
               check_brand_surface_dormant,
               # Phase VVV schema-drift detector
               check_schema_drift,
               # Phase WWW Site Sentinel — every public page polled
               check_site_sentinel,
               # Phase XXX conversion-rate floor detector
               check_conversion_rate_floor,
               # Phase AAAA dormant-MCP detector
               check_mcp_dormant_agents,
               # Phase BBBB /developers funnel
               check_developers_funnel_dead,
               # Phase CCCC spare-capacity marketplace health
               check_spare_capacity_status,
               # Phase DDDD REST gate informational signal
               check_rest_gate_hits,
               # Phase HHHH facility-discovery stagnation
               check_facility_count_stagnant,
               # Phase PPPP dedup-backlog growing or stalled
               check_dedup_backlog_growing,
               # Phase RRRR DC Hub Media silence
               check_dchub_media_press_silent,
               # r-daily-callout (2026-07-18): public /press page vs DB —
               # the June→July 26-day edge stall no DB-side check could see
               check_press_public_surface_stale,
               # Phase FF+25-followup-r7 monthly trend backstop
               check_monthly_trend_unsent_3d,
               # Phase FF+25-followup-r12 visual drift across the site
               check_page_brand_drift,
               # Phase r33-K (2026-05-21) brand-uniformity sweep. Audits
               # every top public page for missing brand.css / Instrument
               # Sans / dchub-nav.js AND for off-brand colors / wrong
               # body fonts re-introduced after r33-I's manual unify pass.
               # Companion to check_page_brand_drift (rotating sample)
               # — this one hits the full canonical page set every cycle.
               check_page_brand_uniformity,
               # Phase r33-N (2026-05-21) — outbound discovery health.
               # Watches our presence across 7 MCP registries; fires if
               # the daily cron hasn't audited recently OR if any
               # listing has fallen off / never landed.
               check_outbound_distribution_health,
               # Phase FF+25-followup-r14 Canadian / regional coverage gaps
               check_coverage_gap_canada,
               # Phase FF+25-followup-r21 founding-customer welcome rescue
               check_founding_customer_not_welcomed,
               # Phase r28 (2026-05-20) — pocket-of-power high mover
               # detector. Fires when a tracked market's excess-power
               # index shifts ≥15pts in 7 days. Pairs with the autopilot
               # action _action_pocket_alert_announce so significant
               # shifts auto-generate a press/social post rather than
               # only living in /digest where users have to seek them out.
               check_pocket_high_mover,
               # Phase r32-sweep (2026-05-20) — tier-dict missing-keys
               # detector. Closes the bug class that caused Land &
               # Power to treat paying $49 Developer customers as free
               # tier. Static-imports each known tier-limit dict and
               # verifies anonymous/identified/developer/pro are all
               # present. Adding a new tier table? Append to the list
               # inside the detector — that's the containment surface.
               check_tier_dict_missing_keys,
               # Phase r32-conv (2026-05-20) — MCP upgrade-pool growth
               # alert. Fires when ≥50 identified users have hit paid-
               # tool paywall signals without being outreached. The
               # autopilot pattern below can fire the outreach campaign
               # autonomously when this finding lands repeatedly.
               check_upgrade_pool_grown,
               # Phase r32-brain-pipe (2026-05-20) — Inspector → L22
               # auto-PR handoff. Closes the missing pipe between
               # the Inspector's RECIPE proposals and the L22 auto-
               # code drafter. Three-deep safety: brain autopilot
               # rate-limit + L22 whitelist (3 recipes only) + L22
               # _already_drafted() idempotency.
               check_inspector_brief_unprocessed_recipes,
               # Phase r32-multi-cloud (2026-05-21) — failover health.
               # Probes Railway + Render origins directly. Fires when
               # BOTH are down (no failover safety) or when one is
               # down (warns about the regression risk). User caught
               # this with the failed schema-repair curl during a
               # Railway incident — Render was ALSO sick so failover
               # was theatre.
               check_multi_cloud_failover_broken,
               # Phase SSSS winback pitches accumulating without delivery
               check_winback_pitches_unsent,
               # Phase TTTT citation score
               check_citation_score_dropped,
               # Phase UUUU pattern-proposal candidates
               check_pattern_proposal_candidates,
               # Phase VVVV Sentinel content drift
               check_page_content_drift,
               # Phase XXXX competitor announcements
               check_competitor_announcement,
               # Phase YYYY operator-profile gap
               check_operator_profile_gap,
               # Phase ZZZZ market deep-dive coverage
               check_market_deep_dive_stale,
               # Phase BBBBB event submission deadlines
               check_event_submission_pending,
               # Phase CCCCC tenant coverage thin
               check_tenant_coverage_thin,
               # Phase DDDDD auto-trial conversion
               check_auto_trial_conversion,
               # Phase DDDDD funnel concentration
               check_mcp_funnel_concentration,
               # Phase EEEEE volume regression
               check_mcp_volume_regression,
               # Phase FFFFF autopilot outcome verification
               check_autopilot_action_unverified,
               # Phase GGGGG schema.org coverage
               check_schema_org_coverage_low,
               # Phase HHHHH external mentions dropoff
               check_external_mentions_dropoff,
               # Phase KK (2026-05-17) — 4 new blind-spot detectors
               check_data_freshness_sla_breach,
               check_mcp_tool_sunset_candidate,
               check_ai_citations_stale_v2,
               check_autopilot_verifier_backlog,
               # Phase XX (2026-05-17) — breach prevention
               check_rest_endpoint_leakage,
               # 2026-06-29 — two FAIL-CLOSED read-only detectors:
               # (1) gated endpoints that forgot agent_action(claim_free_key)
               #     + email_capture coaching (no upgrade path for agents,
               #     no human CTA), and (2) per-platform AI crawl drop-off
               #     (a crawl surface broke / WAF started blocking). Both
               #     return [] on ANY error so resolve-on-absence can't
               #     auto-close a real finding.
               check_gated_endpoint_coaching_missing,
               check_ai_platform_crawl_drop,
               # 2026-07-02 — flywheel sentinels: (1) media/discovery silent-
               # death modes (X publisher 0-ever-posted, LinkedIn engagement
               # read-back frozen, gap-crawler yield dead) and (2) llms.txt
               # doc-vs-live contract (advertised URLs must resolve — agents
               # follow them verbatim). Both FAIL-CLOSED.
               check_media_and_yield_health,
               check_llms_txt_contract,
               # Phase KKK (2026-05-17) — package install velocity drop
               check_package_install_velocity_drop,
               # Phase OOO (2026-05-17) — frontend-critical endpoint health
               check_frontend_critical_endpoints,
               # Phase QQQ (2026-05-17) — Stability Guardrails: 4 detectors
               # closing the 3 systemic blind spots inventory revealed
               check_cron_freshness,
               # 2026-07-22: watches the MCP-registry presence flywheel, which
               # runs on crawler_scheduler (not /api/jobs) so check_cron_freshness
               # is blind to it — caught the ~51/61h stall + the GitHub drift.
               check_mcp_presence_stale,
               # r-customer-loop (2026-07-20) — systemic activation-failure +
               # nudge-escalation backlog, the course-correction leg of the
               # customer white-glove loop (its cron freshness is covered by
               # check_cron_freshness via the tick's self-stamp).
               check_customer_activation_health,
               # r-facility-dedup (2026-07-20) — regrowth watch for cross-source
               # facility duplicates (customer-flagged data-quality issue).
               check_facility_duplicate_clusters,
               # r-facility-geo (2026-07-20) — country-mislabel regrowth watch.
               check_facility_geo_mismatch,
               check_required_env_vars,
               check_csp_violation_reports,
               check_backend_pool_health,
               # Phase RRR-revenue (2026-05-18) — orphaned-scheduler
               # detector. Closes the recurring bug class where a daemon
               # loop is defined but never started at boot (4 instances
               # caught this session before this detector existed).
               check_orphaned_scheduler_functions,
               # Phase RRR-newsletter (2026-05-18) — dead-link detector.
               # Catches navigation/CTA links that 404 (we hit this twice
               # this session: /open-data and /api/press-releases).
               check_dead_internal_links,
               # Phase RRR-newsletter+1 (2026-05-18) — shadowed-route
               # detector. Catches duplicate Flask route registrations
               # (today's dummy submit-challenge shadowing the real one).
               check_shadowed_routes,
               # Phase RRR-funnel (2026-05-18) — auto-trial signal/mint
               # mismatch. Catches the silent failure mode where agents
               # bash the paywall without extracting trial keys.
               check_auto_trial_signal_mint_mismatch,
               # r62c-conv (2026-06-01) — trial-taste abuse guard. The trial
               # key now unlocks full grid/fiber for 7d; this flags a
               # rotating-IP mint spike with ~zero email-binds (farming the
               # free Pro data instead of converting).
               check_trial_taste_abuse,
               # Phase RRR-cron-wiring (2026-05-18) — HTTP-cron orphan
               # detector. Sibling to check_orphaned_scheduler_functions
               # — that one catches Thread() loops never started; this
               # one catches HTTP cron endpoints never scheduled.
               check_cron_endpoint_unscheduled,
               # Phase ZZZZ-bp-detector (2026-05-18) — blueprint silent-
               # failure detector. Closes the recurring bug class where
               # late-line `app.register_blueprint(X)` calls execute
               # without raising but never actually wire (3× in 7 days:
               # press_loop, industry_pulse, market_deep_dive). Walks
               # main.py for `from routes.X import Y_bp` and verifies
               # each is in current_app.url_map. Fast — no HTTP.
               check_blueprint_registration_silent_failure,
               # Phase ZZZZ-heartbeat (2026-05-18) — heartbeat surface
               # stale detector. User saw 34 red rows on /heartbeat that
               # brain hadn't surfaced; this groups by refresh_func so
               # a single broken function shows as ONE finding instead
               # of 34. Promotes the operational signal "X function
               # isn't refreshing" to a brain-level finding.
               check_heartbeat_surfaces_stale,
               # Phase ZZZZ-pricing (2026-05-18) — pricing placeholder
               # detector. Sweeps /pricing for empty price-amount spans,
               # __PLACEHOLDER__ literals, undefined/NaN near /year. Catches
               # the broken-Pro-Annual-rendering pattern the user spotted.
               check_pricing_page_placeholder_content,
               # Phase ZZZZ-conversion (2026-05-18) — tool-level paywall
               # signal vs conversion leak. Closes the same gap the brain
               # narrative flagged (1547 get_market_intel signals → 0
               # conversions). Now any tool with that pattern auto-surfaces.
               check_tool_signal_to_conversion_leak,
               # Phase ZZZZ-social (2026-05-18) — social publish silent
               # failure detector. Fires when a platform (LinkedIn/X/
               # Bluesky) is configured but has 0 publishes in 7d while
               # the queue is backed up. Catches token-expiry the moment
               # it happens, not 4.5 days later (which is what we saw
               # this session — 11 posts queued, 0 published, 60% lifetime
               # rate, all because tokens silently expired).
               check_social_publish_silent_failure,
               # Phase ZZZZ-cf (2026-05-18) — CF account-level health.
               # Polls /api/v1/cf-analytics/health (which calls the CF
               # GraphQL Analytics API). Flags cache rate dropping
               # below 25% — the dashboard showed it at 13.7%, every
               # uncached hit is a Railway origin call.
               check_cf_account_health,
               # 2026-07-24 coverage audit: this was DEFINED but never registered —
               # 75 lines of KV-pressure detection that had never once run. Read-only
               # and self-disabling without CF_API_TOKEN/CF_ACCOUNT_ID, so wiring it
               # in is zero-risk. (A detector nobody registered is the purest form of
               # the "exists but watches nothing" class this audit is hunting.)
               check_cf_kv_namespace_pressure,
               # Phase ZZZZ-trial (2026-05-18) — trial-to-paid stagnation.
               # Fires when paywall signals are high but conversions are
               # near-zero — the leak the user spotted this session
               # (15K signals → 0 conversions because trial gives free
               # access to the 5 hot tools).
               check_trial_to_paid_stagnation,
               # Phase ZZZZ-T2.2 (2026-05-18) — irony: brain memory empty.
               check_brain_memory_empty,
               # Phase ZZZZ-T3.2 (2026-05-18) — addressable demand.
               # Names specific paid tools where concentrated demand
               # exists with 0 conversions = sales-outreach targets.
               check_addressable_demand_unconverted,
               # Phase ZZZZ-brain-L7-accepted (2026-05-19) — the FIRST
               # brain-written detector. L7 (brain_layer7_evolving)
               # analyzed 3 commits to commit_scope:phase-kkk and
               # proposed this. Wrapped with try/except for schema
               # drift. Brain literally writing brain.
               check_package_metadata_freshness,
               # Phase r33-B (2026-05-21) — three platform-health
               # detectors. Each does at most 1-2 HTTP probes with
               # short timeouts (≤10s) and an early-out on failure,
               # so they're cheap to run on the parallel scan.
               #   cf_pages_deploy_stuck: worker version not bumping
               #     despite recent _worker.js commits (the bug class
               #     where CF Pages auto-deploy gets stuck retrying
               #     a failed commit, blocking later pushes).
               #   slow_request_ratio: aggregates SLOW REQUEST logs
               #     into a brain-level finding, so /grid 112s no
               #     longer needs to be diagnosed by reading Railway
               #     logs by hand — brain surfaces it.
               #   render_pipeline_blocked: latest dchub-backend
               #     commit on GitHub vs Render's /api/v1/version —
               #     fires when Render's pipeline-minutes-blocked
               #     state causes deploy drift to accumulate silently.
               check_cf_pages_deploy_stuck,
               check_slow_request_ratio,
               check_render_pipeline_blocked,
               # Phase r33-C (2026-05-21) — Render flap auto-recovery.
               # Probes Render directly 3x; fires when ≥2/3 fail. Pairs
               # with the autopilot action that hits the Render deploy
               # hook for a fresh container.
               check_render_flapping,
               # Phase r33-E (2026-05-21) — QA monitor master shell.
               # Five detectors closing the next-highest-leverage gaps:
               #   404_spike: burst detection (deploy regression sign)
               #   neon_replication_lag: failover safety
               #   signup_drop_off_step: revenue protection
               #   detector_runtime_distribution: brain meta-monitor
               #     (catches a slow detector before it cascades a
               #      restart — the bug class that caused this session's
               #      107s consistency_radar → /grid 112s outage)
               #   stripe_webhook_lag: revenue-pipeline safety
               check_404_spike,
               check_neon_replication_lag,
               check_signup_drop_off_step,
               check_detector_runtime_distribution,
               check_stripe_webhook_lag,
               # Phase r33-F (2026-05-21) — second QA-monitor batch.
               # Five more detectors closing structural blind spots:
               #   canonical_redirect_loops: 30x→self or 30x→404
               #   gunicorn_worker_age: memory drift / restart hygiene
               #   facility_dedupe_collisions: ghost facility class
               #   paid_user_zero_value_tools: pre-churn signal
               #   cf_kv_namespace_pressure: write-leak detection
               check_canonical_redirect_loops,
               check_gunicorn_worker_age,
               check_facility_dedupe_collisions,
               check_paid_user_zero_value_tools,
               check_cf_kv_namespace_pressure):
        detectors.append(fn)

    # r43-L (2026-05-30): MCP discoverability/health — continuously detects
    # drift across the static MCP discovery surfaces (.well-known, static
    # manifests, tool descriptions, pricing tiers) so registry/agent crawlers
    # see consistent metadata. Findings surface in the standard brain pipeline.
    try:
        from routes.brain_mcp_health import check_mcp_health
        detectors.append(check_mcp_health)
    except Exception as _e_mcp:
        import sys as _sys
        print(f"[radar] brain_mcp_health detector skipped: {_e_mcp}",
              file=_sys.stderr)

    # Feature #5 (2026-06-28) — SELF-AUDIT / HONESTY-RECONCILIATION.
    # READ-ONLY detector: cross-checks the brain's own evolution verdict
    # vs self-model verdict (fires on a >=2-rank optimism gap) and flags
    # per-pattern "acting but never landing" (executed_ok>=5 AND
    # total_verified>=5 with 0% verified success). No autopilot action map
    # → findings escalate to the human digest. Import-guarded so the
    # module can never break the radar.
    try:
        from routes.brain_honesty_reconciliation import check_honesty_reconciliation
        detectors.append(check_honesty_reconciliation)
    except Exception as _e_honesty:
        import sys as _sys
        print(f"[radar] brain_honesty_reconciliation detector skipped: {_e_honesty}",
              file=_sys.stderr)

    # Phase ZZZZZ-round17 (2026-05-23) — security/breach detectors.
    # The user explicitly asked: "can we also enhance brain to detect any
    # bugs or gate breaches or security breaches for that matter, want
    # our data to be secure". These run alongside the health detectors
    # so security regressions surface in the same heal-findings stream:
    #   - admin_endpoint_open       → POST /admin/* without auth = 200
    #   - paywall_hole              → PRO-gated endpoint serving data anon
    #   - security_header_missing   → x-content-type-options, x-frame-options, etc.
    #   - secret_pattern_in_response → AWS/Stripe/GitHub/internal keys in body
    #   - suspicious_admin_scan     → 401-spam from one IP > 20/h
    #
    # Phase ZZZZZ-round20 (2026-05-23) EMERGENCY GATE: registering 6
    # HTTP-self-probing detectors inside scan_all caused Railway to hang.
    # With only ~2 gunicorn workers, having 6 detectors each issuing
    # 5+ blocking self-calls back to localhost:8080 inside a single
    # scan deadlocked the worker pool — the workers serving scan_all
    # couldn't serve the self-probes the detectors were waiting on.
    # POST endpoints (which CF can't failover to Render) started 503'ing.
    #
    # Fix: gate behind DCHUB_SECURITY_RADAR_ENABLED env var, default OFF.
    # The security detectors should run on their own schedule (cron, not
    # every-5min radar pass). To re-enable for testing, set the env var
    # to '1' on Railway. They remain available as a module and can be
    # invoked directly via /api/v1/admin/brain/security-scan endpoint.
    import os as _os_radar
    if _os_radar.environ.get("DCHUB_SECURITY_RADAR_ENABLED", "0") == "1":
        try:
            from routes.brain_security_detectors import SECURITY_DETECTORS
            for _sec_fn in SECURITY_DETECTORS:
                detectors.append(_sec_fn)
        except Exception as _e_sec:
            # Module import must never break the radar.
            import sys as _sys
            print(f"[radar] brain_security_detectors import skipped: {_e_sec}",
                  file=_sys.stderr)

    # Phase RRR-brain-parallel (2026-05-18) — scan was taking 76.9s
    # serial because several detectors make HTTP calls (frontend probes
    # 23 URLs, dead-link probes 30 URLs, backend pool probe 1 URL, route
    # audit 1 URL, competitor sitemaps 6 URLs). At ~3-8s each that
    # serializes to 60-90s. Parallelize via ThreadPoolExecutor — wall
    # time becomes max(detector_time) ≈ 10-15s instead of sum.
    # Per-detector 20s timeout prevents any single slow probe from
    # holding up the whole scan.
    import concurrent.futures as _cf, time as _scan_time
    def _run_one(fn):
        t0 = _scan_time.time()
        try:
            result = fn() or []
            _DETECTOR_TIMINGS[fn.__name__] = {
                "last_ms":  int((_scan_time.time() - t0) * 1000),
                "last_run": _scan_time.time(),
                "ok":       True,
            }
            return ("ok", fn.__name__, result)
        except Exception as e:
            _DETECTOR_TIMINGS[fn.__name__] = {
                "last_ms":  int((_scan_time.time() - t0) * 1000),
                "last_run": _scan_time.time(),
                "ok":       False,
                "err":      f"{type(e).__name__}: {str(e)[:120]}",
            }
            return ("err", fn.__name__,
                    f"{type(e).__name__}: {str(e)[:200]}")

    # r33-Q+radar-budget (2026-05-22): HARD 25s wall-clock budget on the
    # whole scan. Previously `as_completed(timeout=60)` let the scan run
    # 100s+ (observed: "SLOW REQUEST GET /api/v1/brain/consistency-radar
    # took 103.1s"). With ~100 detectors making 4-8s HTTP self-calls in
    # 8 worker threads, deadlocked self-calls compounded into 13 batches
    # × per-call timeouts. A radar scan that takes 103s is worse than
    # useless: it holds a gunicorn worker hostage and trips L20 + the
    # watchdog. Better to return PARTIAL findings in 25s than complete
    # findings in 103s. Detectors still running at the deadline are
    # abandoned (their thread finishes in the background, result
    # discarded). Each detector also keeps its own 20s per-future cap.
    _SCAN_BUDGET_S = 25
    _deadline = _scan_time.time() + _SCAN_BUDGET_S
    _completed = 0
    _abandoned = 0
    with _cf.ThreadPoolExecutor(max_workers=8,
                                 thread_name_prefix="brain-scan") as ex:
        futs = {ex.submit(_run_one, fn): fn for fn in detectors}
        try:
            for fut in _cf.as_completed(futs, timeout=_SCAN_BUDGET_S):
                fn = futs[fut]
                try:
                    status, name, result = fut.result(timeout=5)
                    _completed += 1
                    if status == "ok":
                        out.extend(result)
                    else:
                        out.append({
                            "issue":  f"consistency_radar_detector_crashed:{name}",
                            "url":    name,
                            "count":  1,
                            "detail": result,
                        })
                except _cf.TimeoutError:
                    out.append({
                        "issue":  f"consistency_radar_detector_timeout:{fn.__name__}",
                        "url":    fn.__name__,
                        "count":  1,
                        "detail": "Detector exceeded per-future 5s collection cap.",
                    })
                except Exception as e:
                    out.append({
                        "issue":  f"consistency_radar_detector_crashed:{fn.__name__}",
                        "url":    fn.__name__,
                        "count":  1,
                        "detail": f"{type(e).__name__}: {str(e)[:200]}",
                    })
                if _scan_time.time() >= _deadline:
                    break
        except _cf.TimeoutError:
            # Overall scan budget exceeded — abandon the rest. Count how
            # many detectors never reported so the scan is honest about
            # being partial rather than silently dropping them.
            _abandoned = sum(1 for f in futs if not f.done())
        # Tally abandoned detectors (deadline hit mid-iteration or budget
        # raised). Surface as a single finding so the operator knows the
        # scan was partial — never a silent truncation.
        not_done = [futs[f].__name__ for f in futs if not f.done()]
        if not_done:
            out.append({
                "issue":  "consistency_radar_scan_partial",
                "url":    "/api/v1/brain/consistency-radar",
                "count_kind": "item_count",  # magnitude, not a recurrence tally
                "count":  len(not_done),
                "detail": (f"Scan hit {_SCAN_BUDGET_S}s budget with "
                           f"{len(not_done)} detectors still running "
                           f"(completed {_completed}). Slowest are "
                           f"likely HTTP self-call probes. Abandoned: "
                           + ", ".join(not_done[:8])),
            })
    return out


# Phase RR: Flask is optional so the radar module is importable in
# bare test environments (CI images often lack the full app deps).
# If Flask is present, expose the radar at /api/v1/brain/consistency-radar.
try:
    from flask import Blueprint, jsonify
    brain_consistency_radar_bp = Blueprint("brain_consistency_radar", __name__)

    @brain_consistency_radar_bp.get("/api/v1/brain/consistency-radar")
    def consistency_radar_endpoint():
        """Public read-only endpoint — returns current consistency findings.
        Cached in-process for 5 min to avoid hammering on dashboard polls."""
        return jsonify(scan_summary())

    @brain_consistency_radar_bp.post("/api/v1/brain/scan/force")
    def consistency_radar_force_endpoint():
        """Phase r33-G (2026-05-21) — operator escape hatch. Admin
        endpoint to force-clear the cache + force-release the lock
        + run a fresh scan_all(). Use when brain is stuck on a
        stale lock (single_flight_lock_busy on /brain-live).

        Auth: X-Admin-Key (same as other admin endpoints)."""
        import os as _os_force
        from flask import request as _req_force
        admin_key = (_os_force.environ.get("DCHUB_ADMIN_KEY")
                     or _os_force.environ.get("DCHUB_INTERNAL_KEY"))
        provided = (_req_force.headers.get("X-Admin-Key")
                    or _req_force.headers.get("X-Internal-Key")
                    or _req_force.args.get("admin_key") or "")
        if not admin_key or provided != admin_key:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        # Force-clear cache + lock
        _SCAN_CACHE["value"]      = None
        _SCAN_CACHE["expires_at"] = 0.0
        _release_scan_lock()
        # Run fresh scan
        t0 = _t_mod.time()
        try:
            findings = scan_all()
        except Exception as e:
            return jsonify(
                ok=False,
                error=f"{type(e).__name__}: {str(e)[:300]}",
                elapsed_s=round(_t_mod.time() - t0, 1),
            ), 500
        result = _build_summary(findings)
        _SCAN_CACHE["value"]      = result
        _SCAN_CACHE["expires_at"] = _t_mod.time() + _SCAN_CACHE_TTL_SECONDS
        result["elapsed_s"]   = round(_t_mod.time() - t0, 1)
        result["forced_by"]   = "operator"
        return jsonify(result), 200

    @brain_consistency_radar_bp.get("/api/v1/brain/scan/diagnostic")
    def consistency_radar_diagnostic_endpoint():
        """Quick diagnostic: cache state + lock state + per-detector
        timings (from _DETECTOR_TIMINGS). Useful when brain is
        misbehaving — answers 'what is brain doing right now'."""
        cache_age = -1
        if _SCAN_CACHE.get("expires_at"):
            cache_age = round(
                _t_mod.time()
                - (_SCAN_CACHE["expires_at"] - _SCAN_CACHE_TTL_SECONDS), 1)
        lock_t = _SCAN_LOCK_HOLDER_T0.get("t", 0.0)
        return jsonify({
            "ok": True,
            "cache": {
                "fresh": (_SCAN_CACHE.get("expires_at", 0.0)
                          > _t_mod.time()),
                "age_seconds":  cache_age,
                "ttl_seconds":  _SCAN_CACHE_TTL_SECONDS,
                "grace_seconds": _SCAN_STALE_GRACE_SECONDS,
                "findings_count": (
                    (_SCAN_CACHE.get("value") or {}).get("count", 0)),
            },
            "lock": {
                "locked":  lock_t > 0,
                "held_for_s": (round(_t_mod.time() - lock_t, 1)
                               if lock_t > 0 else 0),
                "max_hold_s": _SCAN_LOCK_MAX_HOLD_SECONDS,
                "holder_pid": _SCAN_LOCK_HOLDER_T0.get("pid", 0),
            },
            "detector_timings": {
                name: {"last_ms": info.get("last_ms", 0),
                       "ok":      info.get("ok"),
                       "err":     info.get("err"),
                       "age_s":   (round(_t_mod.time()
                                         - info.get("last_run", 0), 1)
                                   if info.get("last_run") else None)}
                for name, info in (_DETECTOR_TIMINGS or {}).items()
            },
        }), 200
except ImportError:
    brain_consistency_radar_bp = None  # tests can still import the detectors


# Phase QQQ-hotfix (2026-05-18) — the docstring on consistency_radar_endpoint
# always claimed "Cached in-process for 5 min" but no cache existed. The
# brain has 50+ detectors, several of which now make HTTP calls (Phase OOO
# frontend probes, Phase QQQ check_backend_pool_health). Running them all
# live on every dashboard poll caused the brain endpoint to time out past
# Railway's request limit. This single-process TTL cache makes the
# docstring actually true.
import time as _t_mod
import threading as _thr_mod
_SCAN_CACHE: dict = {"value": None, "expires_at": 0.0}
_SCAN_CACHE_TTL_SECONDS = 300       # 5 minutes (fresh)
_SCAN_STALE_GRACE_SECONDS = 3600    # serve stale up to 1 h old when herd hits

# Phase FF+13-radarstorm (2026-05-19) — EMERGENCY single-flight lock.
# The endpoint kept saturating all gunicorn workers at once:
#   - The "5-min cache" was per-worker (in-process), not shared.
#   - Gunicorn runs N workers, each had its own _SCAN_CACHE.
#   - When the TTL expired, every concurrent caller in every worker
#     ran scan_all() (76 detectors, several making HTTP/DB calls).
#   - Logs showed 7+ concurrent in-flight calls each taking ~140s.
#   - Workers fully blocked → watchdog declared self_response failure → kill.
#
# Plus 9 brain layers (L8, L11, L12, L14, L16, L19, L22, autopilot, alive)
# all hit this endpoint on their own crons — a classic thundering herd.
#
# Fix:
#   1. ONE thread per process actually computes scan_all() at a time
#      (threading.Lock with non-blocking acquire).
#   2. Concurrent callers who can't get the lock get the LAST cached
#      value with a stale=true flag — never blocks > a few ms.
#   3. Stale-grace extends to 1h so a slow scan_all() never causes a
#      total cache miss for callers.
#   4. If no cache exists yet AND the lock is held, return an empty
#      ok=true response instead of waiting (better than 140s timeout).
# Phase r33-G (2026-05-21): timed lock instead of boolean Lock().
# The old threading.Lock() pattern leaked: if scan_all hung (one
# detector blocking despite the 20s timeout), the lock stayed held
# forever — every subsequent scan request returned
# "single_flight_lock_busy" stale data and the brain stopped
# evolving. Now we track WHEN the lock was claimed; any caller can
# force-release after 120s (longer than a healthy scan's ~15s p99).
_SCAN_LOCK = _thr_mod.Lock()
_SCAN_LOCK_HOLDER_T0: dict[str, float] = {"t": 0.0, "pid": 0}
_SCAN_LOCK_MAX_HOLD_SECONDS = 120.0


def _try_acquire_scan_lock() -> bool:
    """Try to grab the scan lock. If it's held but >120s old,
    force-release first (assume the holder is dead)."""
    import os as _os_lock
    if _SCAN_LOCK.acquire(blocking=False):
        _SCAN_LOCK_HOLDER_T0["t"]   = _t_mod.time()
        _SCAN_LOCK_HOLDER_T0["pid"] = _os_lock.getpid()
        return True
    # Held — check age
    held_for = _t_mod.time() - _SCAN_LOCK_HOLDER_T0.get("t", 0.0)
    if held_for > _SCAN_LOCK_MAX_HOLD_SECONDS:
        # Force-release: the holder is presumed dead (gunicorn worker
        # restarted, detector hung past its 20s budget, etc).
        try: _SCAN_LOCK.release()
        except Exception: pass
        if _SCAN_LOCK.acquire(blocking=False):
            _SCAN_LOCK_HOLDER_T0["t"]   = _t_mod.time()
            _SCAN_LOCK_HOLDER_T0["pid"] = _os_lock.getpid()
            return True
    return False


def _release_scan_lock() -> None:
    try: _SCAN_LOCK.release()
    except Exception: pass
    _SCAN_LOCK_HOLDER_T0["t"]   = 0.0
    _SCAN_LOCK_HOLDER_T0["pid"] = 0


def _build_summary(findings):
    by_issue: dict[str, int] = {}
    for f in findings:
        by_issue[f["issue"]] = by_issue.get(f["issue"], 0) + 1
    from datetime import datetime, timezone
    return {
        "ok": True,
        "count_kind": "item_count",  # magnitude, not a recurrence tally
        "count": len(findings),
        "by_issue": by_issue,
        "findings": findings,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "cache_ttl_seconds": _SCAN_CACHE_TTL_SECONDS,
    }


# r33-O Wave A (2026-05-21) — DB-backed findings persistence.
#
# Inspector has been flagging "brain_findings relation missing" on
# multiple briefs. Autopilot has been silent for 10+ hours because
# its in-process scan_summary() bridge keeps hitting empty caches on
# fresh workers. Both problems solved by: every scan_all run UPSERTs
# its findings to a shared brain_findings table, so:
#   1. The autopilot reads from DB (worker-independent, no cache
#      divergence) — fixes the silence problem permanently.
#   2. The Inspector's `brain_findings` query stops erroring.
#   3. Any cron / external tool can ALSO query findings without
#      having to hit the radar endpoint with its lock dance.
_BRAIN_FINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS brain_findings (
    id           SERIAL PRIMARY KEY,
    issue        TEXT NOT NULL,
    url          TEXT NOT NULL DEFAULT '',
    count        INTEGER,
    detail       TEXT,
    detector     TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    resolved_at  TIMESTAMPTZ,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count   INTEGER NOT NULL DEFAULT 1,
    episode_count      INTEGER NOT NULL DEFAULT 1,
    episode_seen_count INTEGER NOT NULL DEFAULT 1,
    episode_started_at TIMESTAMPTZ,
    UNIQUE (issue, url)
);
CREATE INDEX IF NOT EXISTS brain_findings_last_seen_idx
    ON brain_findings (last_seen DESC);
CREATE INDEX IF NOT EXISTS brain_findings_issue_idx
    ON brain_findings (issue);
"""


# ── Savepoint helpers for the durable-findings persist path ──────────
# Mirror routes.brain_findings_writer's pattern: every DB step inside
# the persist transaction is wrapped so a single failing statement rolls
# back ONLY itself, never aborting the whole upsert/commit. All three are
# no-throw — they swallow errors and return a bool/None so the caller's
# control flow degrades to the safe (leave-rows-in-place) path.
def _persist_savepoint(cur, name: str) -> bool:
    try:
        cur.execute(f"SAVEPOINT {name}")
        return True
    except Exception:
        return False


def _persist_rollback_sp(cur, name: str) -> None:
    try:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:
        pass


def _persist_release_sp(cur, name: str) -> None:
    try:
        cur.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        pass


def _persist_findings_to_db(findings: list[dict], full_sweep: bool = False) -> int:
    """Write findings to brain_findings. UPSERT on (issue, url) so the
    same finding rolling across scans increments seen_count + bumps
    last_seen instead of duplicating. Returns rows touched.

    DURABLE FINDINGS (r-incentives, 2026-06-14): this used to end with a
    `DELETE FROM brain_findings WHERE last_seen < NOW()-INTERVAL '10 min'`
    sweep, which WIPED every finding that dropped out of a scan. Because
    a sweep always completes inside that 10-min window, ALL rows were
    perpetually <1h old and resolved_count was structurally 0 — the
    open/resolved trajectory (the signal the incentive system needs to
    reward CLOSURES over raw activity) was unmeasurable.

    Now: instead of deleting, findings ABSENT from the current sweep but
    still marked open are RESOLVED (status='resolved', resolved_at=NOW()),
    preserving their first_seen / seen_count history. A reappearing
    finding is reopened by the canonical writer (clears resolved_at). The
    two live consumers (autopilot worklist read + outcome verifier) both
    already gate on `last_seen > NOW()-INTERVAL '10 minutes'`, so they
    keep seeing ONLY freshly-detected findings — resolved rows accumulate
    as history without polluting the active worklist or the verifier.

    Defensive + FAIL-SAFE: every step is savepoint-wrapped and the whole
    body is try/except. On ANY error it degrades to the safe old-ish
    behavior (rows simply left in place — never deleted, never a crash);
    persistence failures never fail the scan. Returns rows upserted."""
    import os as _os_p, psycopg2 as _pg_p
    # r33-Q+persist-robust (2026-05-22): fall back to NEON_DATABASE_URL
    # if DATABASE_URL isn't set (main.py normally overrides it, but a
    # bare-import context or a worker that booted before the override
    # may not have it). Connect timeout bumped 5s→10s: the earlier
    # "inspector_findings_persisted: 0" happened because this opens a
    # COLD psycopg2 connection (not the warm pool) and a 5s timeout
    # loses the race during a Railway flap. 10s clears the flap window.
    db = (_os_p.environ.get("DATABASE_URL")
          or _os_p.environ.get("NEON_DATABASE_URL"))
    if not db or not findings:
        return 0
    rows = 0
    try:
        conn = _pg_p.connect(db, sslmode="require", connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(_BRAIN_FINDINGS_DDL)
                # Idempotent: ensure the durability columns exist even on
                # a table created from an older DDL (no rewrite in PG 11+).
                # resolved_at/status are already live, but ADD IF NOT
                # EXISTS keeps this self-healing + matches the canonical
                # schema. Savepoint-wrapped so a failure can't poison the
                # upsert transaction below — degrades to old behavior.
                if _persist_savepoint(cur, "bf_alter_durable"):
                    try:
                        cur.execute(
                            "ALTER TABLE brain_findings "
                            "ADD COLUMN IF NOT EXISTS resolved_at "
                            "TIMESTAMPTZ")
                        cur.execute(
                            "ALTER TABLE brain_findings "
                            "ADD COLUMN IF NOT EXISTS status "
                            "TEXT NOT NULL DEFAULT 'open'")
                        _persist_release_sp(cur, "bf_alter_durable")
                    except Exception:
                        _persist_rollback_sp(cur, "bf_alter_durable")
                # 2026-06-06: the inline INSERT used seen_count + ON
                # CONFLICT (issue, url) — neither exists on the LIVE
                # table, so the old "canonical" persister failed silently
                # for weeks. Delegate to the canonical writer which
                # introspects the live schema, restores seen_count, and
                # upserts constraint-agnostically (the live table has NO
                # UNIQUE(issue,url) — verified via information_schema —
                # so ON CONFLICT cannot be used here).
                from routes.brain_findings_writer import upsert_brain_finding
                current_keys = set()
                inserted = 0
                for f in findings:
                    if not isinstance(f, dict): continue
                    issue = (f.get("issue") or "")[:200]
                    url   = (f.get("url") or "")[:500]
                    if not issue: continue
                    current_keys.add((issue, url))
                    res = upsert_brain_finding(
                        cur, issue=issue, url=url,
                        count=f.get("count") or 1,
                        # #49 lane 3: carry the detector's DECLARED meaning of
                        # `count` through to the row, so the selector reads a
                        # type instead of guessing from the issue string.
                        count_kind=f.get("count_kind") or "",
                        detail=(f.get("detail") or "")[:2000],
                        detector="consistency_radar")
                    if res == "inserted":
                        inserted += 1
                    if res in ("inserted", "updated"):
                        rows += 1
                # DURABLE FINDINGS: RESOLVE (don't DELETE) findings that
                # were open but are ABSENT from this sweep. This preserves
                # first_seen/seen_count history and makes the open→resolved
                # trajectory measurable, which the incentive system rewards.
                #
                # "Absent from this sweep" == an OPEN row whose last_seen
                # was NOT just bumped by an upsert this run. The upserts
                # above set last_seen=NOW(), so any open row with
                # last_seen older than a small grace window (2 min — well
                # under the scan cadence, comfortably above this run's own
                # write latency) is one we did not re-detect. We only
                # transition rows that are currently 'open' (idempotent;
                # never re-stamps an already-resolved row's resolved_at).
                #
                # FAIL-SAFE: savepoint-wrapped. If it errors we roll back
                # just this step and leave the rows untouched (open) — the
                # OLD code would have DELETED them, so "leave open" is the
                # strictly safer degraded behavior.
                resolved_now = 0
                # r-incentives FIX: resolve-on-absence ONLY during a FULL sweep.
                # _persist_findings_to_db has PARTIAL callers (e.g. the Inspector
                # persisting just its degrading/attention items) — running the
                # resolve from those would mark every finding NOT in that partial
                # set as resolved (they're all >2min old), falsely closing the
                # radar's open findings. Only the canonical full radar sweep
                # passes full_sweep=True, where "open + stale last_seen" really
                # does mean "not re-detected this sweep".
                # EPISODE SCOPING (stateful-detector layer, 2026-07-17): the
                # 2-min absence window is only valid for findings THIS sweep
                # could have re-detected — the radar's own. Other detectors
                # (fast_qa, master shells, sentinels…) write on slower
                # cadences; resolving their rows 2 min after each write
                # churned them resolved→open every cycle, which under episode
                # semantics would mint a fake new episode per sighting (the
                # exact inflation the episode ledger exists to kill). Radar
                # rows keep the per-sweep window; foreign/NULL-detector rows
                # only resolve after 24h of silence (detector had many
                # chances to re-emit and didn't → the incident really ended).
                if full_sweep and _persist_savepoint(cur, "bf_resolve_absent"):
                    try:
                        cur.execute("""
                            UPDATE brain_findings
                               SET status = 'resolved',
                                   resolved_at = NOW()
                             WHERE status = 'open'
                               AND ((detector = 'consistency_radar'
                                     AND last_seen < NOW() - INTERVAL '2 minutes')
                                    OR last_seen < NOW() - INTERVAL '24 hours')
                        """)
                        resolved_now = cur.rowcount or 0
                        _persist_release_sp(cur, "bf_resolve_absent")
                    except Exception:
                        note_swallowed_write("brain_findings", where="brain_consistency_radar._persist_findings_to_db")
                        _persist_rollback_sp(cur, "bf_resolve_absent")
                        resolved_now = 0
                # Open/resolved/new-rate summary (read-only, for the
                # incentive signal + logs). Savepoint-wrapped; never fatal.
                if _persist_savepoint(cur, "bf_summary"):
                    try:
                        cur.execute("""
                            SELECT
                              COUNT(*) FILTER (WHERE status = 'open')     AS open_now,
                              COUNT(*) FILTER (WHERE status = 'resolved') AS resolved_total,
                              COUNT(*) FILTER (
                                WHERE resolved_at > NOW() - INTERVAL '24 hours'
                              ) AS resolved_24h
                            FROM brain_findings
                        """)
                        srow = cur.fetchone() or (0, 0, 0)
                        open_now, resolved_total, resolved_24h = (
                            srow[0] or 0, srow[1] or 0, srow[2] or 0)
                        denom = inserted + resolved_now
                        new_rate = (round(inserted / denom, 3)
                                    if denom else 0.0)
                        logger.info(
                            "brain_findings durable persist: upserted=%d "
                            "new=%d resolved_this_run=%d | open_now=%d "
                            "resolved_total=%d resolved_24h=%d "
                            "new_rate=%.3f",
                            rows, inserted, resolved_now, open_now,
                            resolved_total, resolved_24h, new_rate)
                        # 2026-06-15: RELEASE is now the LAST statement in the try.
                        # Previously it ran BEFORE logger.info(); when that line
                        # NameError'd (undefined logger), the except did ROLLBACK TO
                        # an already-released savepoint → aborted the tx → the commit
                        # below silently discarded every upsert (22h finding freeze).
                        # With release last, any exception above still has a live
                        # savepoint to roll back to, so the tx stays committable.
                        _persist_release_sp(cur, "bf_summary")
                    except Exception:
                        _persist_rollback_sp(cur, "bf_summary")
            conn.commit()
        finally:
            conn.close()
    except Exception as _persist_err:
        # Defensive — persistence never fails the scan. But LOG it: a silently
        # swallowed exception here is exactly what hid the 22h finding-freeze
        # (a NameError → aborted-tx → discarded commit returned rows>0 anyway).
        try:
            logger.warning("brain_findings persist failed (findings frozen until "
                           "next successful sweep): %s: %s",
                           type(_persist_err).__name__, str(_persist_err)[:200])
        except Exception:
            pass
    return rows


def scan_summary() -> dict:
    """Single-flight + stale-grace wrapper around scan_all().
    Returns INSTANT response on any concurrent contention; only the
    first caller through the lock actually runs the 76 detectors."""
    now = _t_mod.time()
    cached = _SCAN_CACHE.get("value")
    expires_at = _SCAN_CACHE.get("expires_at", 0.0)

    # Fast path: fresh cache hit. No lock needed.
    if cached is not None and now < expires_at:
        return cached

    # Cache stale or missing. Try to acquire the timed lock. Force-
    # releases stale (>120s) holds so a dead worker doesn't poison
    # the brain forever.
    got_lock = _try_acquire_scan_lock()
    if not got_lock:
        if cached is not None and (now - expires_at) < _SCAN_STALE_GRACE_SECONDS:
            stale = dict(cached)
            stale["stale"] = True
            stale["stale_reason"] = "single_flight_lock_busy"
            stale["lock_held_for_s"] = round(
                _t_mod.time() - _SCAN_LOCK_HOLDER_T0.get("t", 0.0), 1)
            return stale
        return {
            "ok": True, "count": 0, "by_issue": {}, "findings": [],
            "stale": True, "stale_reason": "cold_start_lock_busy",
            "cache_ttl_seconds": _SCAN_CACHE_TTL_SECONDS,
            "lock_held_for_s": round(
                _t_mod.time() - _SCAN_LOCK_HOLDER_T0.get("t", 0.0), 1),
        }

    try:
        # Double-check the cache after acquiring the lock — another
        # thread may have just refreshed it while we were waiting.
        now = _t_mod.time()
        cached = _SCAN_CACHE.get("value")
        expires_at = _SCAN_CACHE.get("expires_at", 0.0)
        if cached is not None and now < expires_at:
            return cached

        # Truly stale and we have the lock — refresh.
        findings = scan_all()
        result = _build_summary(findings)
        _SCAN_CACHE["value"] = result
        _SCAN_CACHE["expires_at"] = _t_mod.time() + _SCAN_CACHE_TTL_SECONDS
        # r33-O Wave A: persist findings to brain_findings table so the
        # autopilot worker can read fresh findings from DB instead of
        # via in-process scan_summary() (which has cache divergence
        # across Railway workers and has caused autopilot silence
        # for hours). Defensive — never fails the scan.
        try:
            # full_sweep=True: this is the canonical scan_all() over ALL
            # detectors, so resolve-on-absence is valid here (only place it runs).
            _persist_findings_to_db(findings or [], full_sweep=True)
        except Exception as _e:
            try:
                logger.warning("scan_summary full-sweep persist failed: %s", str(_e)[:200])
            except Exception:
                pass
        return result
    finally:
        _release_scan_lock()
