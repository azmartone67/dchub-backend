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

import json
import os
import re
import sys
import urllib.request
import urllib.error
from typing import Optional


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
        elif "dchub.cloud" in url or "dchub-backend-production" in url:
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
            # Also include the brain UA so rate-limit bypass kicks in
            # (separate machinery from tier-bypass).
            headers["User-Agent"] = "DCHub-BrainRadar/1.0 (+https://dchub.cloud)"
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
        print(f"[brain-radar] {url} {msg}", file=sys.stderr)
        return None, None
    except urllib.error.URLError as e:
        msg = f"URLError: {e.reason}"
        _LAST_FETCH_ERROR[url] = msg
        print(f"[brain-radar] {url} {msg}", file=sys.stderr)
        return None, None
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        _LAST_FETCH_ERROR[url] = msg
        print(f"[brain-radar] {url} {msg}", file=sys.stderr)
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
    if expected != deployed:
        findings.append({
            "issue": "worker_version_drift",
            "url": _WORKER_PROBE_URL,
            "count": 1,
            "detail": (f"_worker.js source declares WORKER_VERSION="
                       f"'{expected}' but production header reports "
                       f"'{deployed}'. Cloudflare Pages auto-deploy may "
                       f"have skipped this file. Touch _worker.js to "
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
                          "developer": 3, "pro": 4, "enterprise": 5}
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
    # Crude block split: each job starts at column 2 with a name + colon
    job_blocks = re.split(r"\n(?=  [a-z_]+:\s*\n)", text)
    for block in job_blocks:
        m_name = re.match(r"\s*([a-z_]+):", block)
        if not m_name:
            continue
        job_name = m_name.group(1)
        # Only care about the `if:` line inside this block
        if_match = re.search(r"^\s*if:\s*(.+?)(?=\n\s*runs-on|\n\s*steps:|\Z)",
                              block, re.MULTILINE | re.DOTALL)
        cond = if_match.group(1) if if_match else ""
        # Phase QA-sweep-3 (2026-05-16): treat BOTH `github.event.schedule`
        # AND `github.event_name == 'schedule'` as evidence of a scheduled
        # trigger. brain_learn + marketing_auto_press use the latter
        # ("fires on ANY scheduled tick") which is a legitimate pattern;
        # the old regex only recognized the explicit-cron-string form.
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
        if job_name in phase_options:
            scheduled.add(job_name)
    return phase_options, scheduled


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
            for table_candidate in ("request_telemetry", "http_request_log",
                                     "api_404_log", "site_sentinel_results"):
                try:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{table_candidate}",))
                    if (cur.fetchone() or [None])[0]:
                        # Found a candidate — query 404s in last hour
                        if table_candidate == "site_sentinel_results":
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
                        for r in cur.fetchall():
                            pattern = r[0] if not hasattr(r, "get") else r.get("pattern") or r.get("path")
                            n = r[1] if not hasattr(r, "get") else r.get("n")
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
        obs = d.get("observations") or d.get("recent") or []
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
        ("freshness_radar",  "/api/v1/freshness/radar"),
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
    fc = d.get("funnel_counts") or {}
    paywall = int(fc.get("paywall_hit") or 0)
    click = int(fc.get("click") or 0)
    upgrade = int(fc.get("upgrade") or 0)
    if paywall < 500:
        return findings
    click_rate = (click / paywall) if paywall else 0.0
    if click_rate >= 0.005:
        return findings
    findings.append({
        "issue": "paywall_click_leak_critical",
        "url": "/api/v1/redeem/funnel-stats",
        "count": 1,
        "detail": (f"paywall_hit={paywall:,} but click={click} "
                   f"(rate={click_rate:.4%}). The upgrade_url in the "
                   f"paywall response either isn't reaching users or "
                   f"isn't actionable. Verify the MCP server is emitting "
                   f"/upgrade?key=... (Phase FF+7) instead of bare "
                   f"/pricing. Mint endpoint: POST /api/v1/mcp/paywall-"
                   f"response. Total upgrade=={upgrade} likely came "
                   f"from non-paywall channels."),
        "paywall_hits_30d": paywall,
        "clicks_30d": click,
        "click_rate_pct": round(click_rate * 100, 4),
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
            "count": len(files),
            "detail": (f"{len(files)} workflows share cron `{expr}` — "
                       f"they fire at the EXACT same minute. Stagger by "
                       f"offsetting one or more of them. Files: "
                       f"{', '.join(files[:6])}"),
            "expr":  expr,
            "files": files,
        })
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


def check_dcpi_partial_recompute() -> list[dict]:
    """Flag when DCPI median market-age exceeds 48h. Catches the
    'load_markets_dynamic returns None → only 30 of 276 markets
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
            for col in ("created_at", "discovered_at", "first_seen_at", "inserted_at"):
                try:
                    cur.execute(f"""
                        SELECT COUNT(*) FROM discovered_facilities
                         WHERE {col} >= NOW() - INTERVAL '7 days'
                    """)
                    n_7d = int((cur.fetchone() or [0])[0] or 0)
                    break
                except Exception:
                    continue
        if n_7d is None: return findings
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
                cur.execute("SELECT to_regclass('public.mcp_pair_codes')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT COUNT(*) FROM mcp_pair_codes "
                                "WHERE redeemed_at IS NOT NULL "
                                "  AND redeemed_at >= NOW() - INTERVAL '7 days'")
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
                cur.execute("""
                    WITH paid_demand AS (
                      SELECT tool_requested AS tool, COUNT(*) AS signals
                        FROM mcp_upgrade_signals
                       WHERE created_at >= NOW() - INTERVAL '7 days'
                         AND tool_requested IS NOT NULL
                       GROUP BY tool_requested HAVING COUNT(*) >= 50
                    ),
                    converted AS (
                      SELECT tool_name AS tool, COUNT(*) AS convs
                        FROM mcp_pair_codes
                       WHERE redeemed_at IS NOT NULL
                         AND redeemed_at >= NOW() - INTERVAL '7 days'
                       GROUP BY tool_name
                    )
                    SELECT p.tool, p.signals
                      FROM paid_demand p LEFT JOIN converted c USING (tool)
                     WHERE COALESCE(c.convs, 0) = 0
                     ORDER BY p.signals DESC LIMIT 1
                """)
                r = cur.fetchone()
            except Exception as _e:
                print(f"[radar] check_mcp_demand_gap inner query: {_e}")
                return findings
            if r:
                tool, sigs = r[0], int(r[1] or 0)
                findings.append({
                    "issue":  "mcp_demand_gap_unaddressed",
                    "url":    f"mcp_upgrade_signals: tool={tool}",
                    "count":  sigs,
                    "detail": (f"Tool '{tool}' had {sigs} paywall-hit signals in "
                               f"7d but ZERO conversions. The strongest expressed "
                               f"demand on the platform; investigate the CTA, the "
                               f"tier threshold, or build a free-tier preview."),
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
    market loop (was iterating 280+ markets × per-market news query =
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
    for f in funnels:
        stages = f.get("stages") or {}
        if (stages.get("1_paywall_signals") or 0) < 50: continue
        leak = f.get("biggest_leak") or {}
        drop = leak.get("drop_pct")
        stage = leak.get("stage")
        if drop is None or drop < 95: continue
        # 95%+ drop on a tool with >50 signals = clear funnel break
        findings.append({
            "issue":  f"mcp_funnel_leak:{f['tool']}",
            "url":    f"mcp_funnel: tool={f['tool']}, stage={stage}",
            "count":  int(drop),
            "detail": (f"Tool '{f['tool']}' has a {drop}% drop at stage "
                       f"'{stage}'. {stages.get('1_paywall_signals')} paywall "
                       f"signals → {stages.get('5_converted',0)} conversions. "
                       f"Inspect /api/v1/mcp/conversion-funnel/{f['tool']} for "
                       f"the per-stage breakdown."),
        })
        if len(findings) >= 3: break  # cap — top-3 leaks is plenty
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
                           COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL),
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
    if signup_rate < 20:
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
                cur.execute("""
                    SELECT mps.market_slug, mps.market_name, mdd.generated_at
                      FROM (SELECT DISTINCT ON (market_slug) market_slug, market_name, score
                              FROM market_power_scores WHERE published = true
                             ORDER BY market_slug, computed_at DESC) mps
                      LEFT JOIN market_deep_dives mdd USING (market_slug)
                     ORDER BY mps.score DESC LIMIT 10
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
    for r in rows:
        name, deadline, starts = r[0], r[1], r[2]
        days_left = (deadline - datetime.date.today()).days if deadline else None
        findings.append({
            "issue":  f"event_submission_pending:{name[:50]}",
            "url":    "/events",
            "count":  days_left or 0,
            "detail": (f"Event '{name}' has a submission deadline in "
                       f"{days_left} days ({deadline}) and DC Hub hasn't "
                       f"submitted. Event runs {starts}. Decision needed."),
        })
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
                       WHERE merged_at IS NULL AND is_duplicate = 0
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
                       AND merged_at IS NULL AND is_duplicate = 0
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
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT path, label, bytes, prev_bytes, content_hash,
                           prev_content_hash
                      FROM site_sentinel_results
                     WHERE healthy = TRUE
                       AND content_hash IS NOT NULL
                       AND prev_content_hash IS NOT NULL
                       AND content_hash != prev_content_hash
                       AND prev_bytes > 0
                """)
                for r in cur.fetchall():
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

        # If latest commit is fresh (<6h), check Render's running
        # version. Render exposes its version via /api/v1/version.
        if latest_age_hrs < 0.5:
            return findings  # too fresh — deploy still in flight, no signal yet
        if latest_age_hrs > 168:
            return findings  # too old to be useful — must've been deployed

        # Probe Render direct
        req2 = _ur.Request(
            "https://dchub-backend-render.onrender.com/api/v1/version",
            headers={"User-Agent": "DCHub-RenderDeployCheck/1.0"},
        )
        try:
            with _ur.urlopen(req2, timeout=8) as resp:
                vbody = resp.read().decode("utf-8", errors="replace")[:500]
                # Parse JSON or HTML response
                try:
                    vjson = _json.loads(vbody)
                    render_build = vjson.get("build") or vjson.get("version") or ""
                except Exception:
                    return findings  # Render returned HTML — likely restart cycle, separate issue
        except Exception:
            return findings  # Render unreachable — different detector handles that

        # Fire if Render's build number is older AND latest commit hits between 2h-24h old
        if 2 < latest_age_hrs < 24:
            findings.append({
                "issue":  "render_pipeline_blocked",
                "url":    "https://dashboard.render.com/",
                "count":  1,
                "detail": (
                    f"Render is on build `{render_build}` but the latest "
                    f"dchub-backend commit ({latest_sha}: {latest_msg}) "
                    f"was {latest_age_hrs:.1f}h ago. Likely Render workspace "
                    f"ran out of pipeline minutes — auto-deploy blocked. "
                    f"Check Render dashboard → Events tab for 'Build blocked' "
                    f"messages. Fix: upgrade pipeline minutes OR manually "
                    f"trigger deploy from Render dashboard."
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
    candidates = [
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
                return findings
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
    if age_h > 2.0:
        findings.append({
            "issue":  "stripe_webhook_lag",
            "url":    f"table:{tbl}",
            "count":  int(age_h),
            "detail": (
                f"Last Stripe webhook landed {age_h:.1f}h ago "
                f"(table: `{tbl}`, threshold: 2h). Customer state is "
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
                    cur.execute("""
                        SELECT 1 FROM brain_findings
                         WHERE issue = 'inspector_l22_handoff_fired'
                           AND url LIKE %s
                           AND created_at > NOW() - INTERVAL '7 days'
                         LIMIT 1
                    """, (f"%/brief/{brief_id}/%",))
                    if cur.fetchone():
                        return findings  # already fired
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
    developer → pro+ ladder and were missing entries, so
    `dict.get(tier, default)` fell through to free defaults.

    Detector imports each known tier-limit dict and verifies the four
    canonical tier names are present. Flags any gap so the brain
    surfaces the regression risk BEFORE a customer hits it.

    Adding a new tier-limit table? Add it to TIER_DICTS_TO_CHECK
    below — that's the bug-class containment surface."""
    findings: list[dict] = []
    REQUIRED = {'anonymous', 'identified', 'developer', 'pro'}
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
                    SELECT COUNT(*) FROM facilities
                     WHERE LOWER(COALESCE(country,'')) IN
                          ('ca','canada','can')
                """)
                ca_count = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("""
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
    sample_pool = [
        "/", "/about", "/pricing", "/intelligence", "/ai-hub",
        "/dcpi", "/transactions", "/cited-by", "/reports/monthly",
        "/daily", "/advertise", "/markets/", "/brain/brief", "/status",
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
        if "data-dchub-brand" not in html:
            signals.append("missing-brand-mark")
        if "/js/dchub-brand.js" not in html and "dchub-brand.js" not in html:
            signals.append("missing-brand-script")
        if "Instrument Sans" not in html:
            signals.append("missing-instrument-sans")
        # Legacy color tokens — only flag when several appear (one stray
        # hex in an OG meta tag isn't drift)
        legacy = sum(html.count(c) for c in ("#10b981", "#3478f6", "#06b6d4"))
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
            "count":  len(drift_pages),
            "detail": (f"{len(drift_pages)} of {len(sample)} sampled "
                       f"pages drifted off-canonical brand: {details}. "
                       f"Fix by editing /js/dchub-nav.js (covers all "
                       f"pages that load it) or the per-page <style> "
                       f"block. Track at /status."),
        })
    return findings


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

    PAGES = [
        '/', '/pricing', '/api-docs', '/developers', '/architecture',
        '/transactions', '/transaction-comps', '/markets', '/dcpi',
        '/pockets', '/coverage', '/digest', '/news', '/press',
        '/dc-hub-media', '/tax-incentives', '/ai', '/ai-deals',
        '/ai-pipeline', '/ai-integrations', '/ai-inventory',
        '/state-of-the-data-center', '/system-status', '/grid-intelligence',
        '/platform', '/sites', '/spare-capacity', '/capacity-pipeline',
        '/mcp',
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
                findings.append({
                    "issue":  "page_brand_uniformity",
                    "url":    path,
                    "count":  html.count(needle),
                    "detail": (f"Page {path} contains off-brand pattern "
                               f"`{needle}` ({tag}) — {hint}. After "
                               f"r33-I unification this page is a "
                               f"regression."),
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
    seen = set()
    for tk, tname, outcome, ts, detail in audits:
        seen.add(tk)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        age_h = (now - ts).total_seconds() / 3600.0
        if outcome == "not_listed":
            findings.append({
                "issue":  "outbound_distribution_health",
                "url":    f"target:{tk}",
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
                "count":  int(age_h),
                "detail": (f"{tname} not audited in {age_h:.0f}h. The "
                           f"daily mcp-outreach.yml cron may be broken."),
            })
    try:
        from routes.mcp_registry_outreach import DISCOVERY_TARGETS as _TARGETS
        for t in _TARGETS:
            if t["key"] not in seen:
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
    gap = total - verified
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
            "detail": (f"Facility dedup backlog: {gap:,} raw rows not yet "
                       f"deduped (raw {total:,} vs verified {verified:,}). "
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
        "detail": (f"Facility dedup backlog: {gap:,} candidates "
                   f"awaiting dedup ({verified:,} verified of {total:,} raw). "
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
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_pair_codes
                     WHERE redeemed_at IS NOT NULL
                       AND redeemed_at >= NOW() - INTERVAL '30 days'
                """)
                conversions = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                return findings
            if signals < 1000:
                return findings  # not enough data to be meaningful
            rate = 100.0 * conversions / signals
            FLOOR = float(os.environ.get("DCHUB_MIN_CONVERSION_PCT", "0.5"))
            if rate < FLOOR:
                findings.append({
                    "issue":  "mcp_conversion_rate_below_floor",
                    "url":    "mcp_upgrade_signals + mcp_pair_codes / 30d",
                    "count":  int(rate * 100),  # basis points
                    "detail": (f"30-day MCP conversion rate is {rate:.3f}% "
                               f"({conversions} conversions / {signals} signals) "
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
        ("news_items",             "published_at", 6,    "news ingest"),
        ("press_releases",         "published_at", 36,   "press releases"),
        ("ai_citations",           "observed_at",  168,  "AI citations (weekly)"),
        ("monthly_reports",        "created_at",   744,  "monthly trend snapshot"),
        # Phase r33-D (2026-05-21) — infrastructure layer SLAs. HIFLD
        # publishes annually, EIA quarterly; we refresh aggressively
        # so the map doesn't go stale. Each pairs with a REFRESH_MAP
        # entry in brain_autopilot.py (transmission-refresh, gas-
        # refresh, substations-refresh) for autonomous recovery.
        ("transmission_lines",     "updated_at",   720,  "HIFLD transmission lines"),
        ("gas_pipelines",          "updated_at",   720,  "EIA gas pipelines"),
        ("substations",            "updated_at",   720,  "HIFLD substations"),
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
        ("/api/v1/me/tier",                  "/snapshot",          3, "snapshot tier resolution"),
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

    # r41-frontend-parallel (2026-05-25): parallelize the 24-probe loop.
    # Pre-fix this was serial — observed wall time ~37s (slowest single
    # detector in the radar scan). At 24 probes × ~1.5s avg = ~36s
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
                 WHERE expected_interval_s IS NOT NULL
                   AND expected_interval_s > 0
                ORDER BY job_name
            """)
            for row in cur.fetchall():
                job_name, last_start, expected_s, run_count, seconds_since = row
                if seconds_since is None:
                    continue
                # Flag when cron is > 2× its expected interval late.
                # The 2× buffer prevents flapping on natural jitter.
                if seconds_since > (expected_s * 2):
                    hours_late = (seconds_since - expected_s) / 3600.0
                    findings.append({
                        "issue":  "cron_silently_dead",
                        "url":    f"/api/jobs/{job_name}",
                        "count":  int(seconds_since),
                        "detail": (f"Cron `{job_name}` has not run in "
                                   f"{seconds_since}s (expected every "
                                   f"{expected_s}s, {hours_late:.1f}h late). "
                                   f"Total runs since deploy: {run_count}. "
                                   f"Likely causes: Railway crash mid-run, "
                                   f"env-var gate returned early, or scheduler "
                                   f"container died. Check the cron endpoint "
                                   f"and the scheduler service logs."),
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
            r = _req.get(url, timeout=5, headers=headers, allow_redirects=True)
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

    for path, status, body_snip, err in results:
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

    for entry in (data.get("shadowed_routes") or []):
        path = entry.get("path", "?")
        methods = entry.get("methods", [])
        endpoints = entry.get("endpoints", [])
        same_endpoint = (len(set(endpoints)) == 1)

        # Phase ZZZZ-T4 (2026-05-18): pick the loser per heuristics.
        # _override, _legacy, _v1, _old, phaseNNN_ prefixed handlers
        # are almost always the one to remove (we keep the cleanly-named
        # canonical handler).
        loser = None
        keeper = None
        for ep in endpoints:
            ep_low = ep.lower()
            if any(s in ep_low for s in ("_override", "_legacy", "_v1", "_old",
                                          "phase9", "phase8", "phase7")):
                loser = ep
            else:
                keeper = ep
        if loser and keeper and loser != keeper:
            recommendation = (
                f"REMOVE the `{loser}` handler (older/legacy pattern). "
                f"KEEP `{keeper}`. Grep the codebase for `def {loser.split('.')[-1]}` "
                f"and comment out or delete its @route decorator."
            )
        else:
            recommendation = (
                "grep the codebase for the path and pick one canonical "
                "handler (look for _override/_legacy suffixes — those are "
                "usually the ones to remove)."
            )

        findings.append({
            "issue":  "shadowed_route",
            "url":    path,
            "count":  len(endpoints),
            "detail": (
                f"Path `{path}` ({','.join(methods)}) has "
                f"{len(endpoints)} registered handlers: "
                f"{', '.join(endpoints)}. "
                + ("Same function name — likely a duplicate decorator on the "
                   "same function. " if same_endpoint else
                   "Different functions — two implementations competing for "
                   "the same URL; Flask silently picks the first registered. ")
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
        r = _req.get("https://dchub.cloud/api/v1/heartbeat",
                     timeout=8,
                     headers={"User-Agent": "dchub-brain-heartbeat/1.0"})
        if r.status_code != 200:
            return findings
        surfs = (r.json() or {}).get("surfaces") or []
    except Exception:
        return findings

    stale = [s for s in surfs if s.get("status") == "stale"]
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
                f"{calls // users:,}× this month. Get unlimited for $9/mo.'"
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

    dist = d.get("distribution") or {}
    queued = dist.get("queued_unpublished", 0)
    oldest_h = dist.get("oldest_queued_age_hours", 0)
    pub7 = dist.get("published_7d") or {}

    # Only fire if there's a backlog WORTH publishing
    if queued < 3 and oldest_h < 24:
        return findings

    for platform in ("linkedin", "twitter", "bluesky"):
        configured = dist.get(f"{platform}_configured", False)
        published = (pub7 or {}).get(platform, 0)
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


def scan_all() -> list[dict]:
    """Run every detector. Return a flat list of finding dicts ready
    to merge into actionable_backend_issues.

    Phase RRR-brain-parallel (2026-05-18): parallelized via ThreadPool
    after observability showed scan was taking 76.9s serial. Detectors
    are collected first, then run concurrently with per-detector 20s
    timeout — wall time becomes max(detector) instead of sum(detector)."""
    out: list[dict] = []
    detectors: list = []
    for fn in (check_worker_version_drift,
               check_tier_consistency,
               check_cron_coverage,
               check_cron_collisions,
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
               # Phase DDD organism detectors
               check_mcp_growth_declining,
               check_mcp_demand_gap,
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
               # Phase KKK (2026-05-17) — package install velocity drop
               check_package_install_velocity_drop,
               # Phase OOO (2026-05-17) — frontend-critical endpoint health
               check_frontend_critical_endpoints,
               # Phase QQQ (2026-05-17) — Stability Guardrails: 4 detectors
               # closing the 3 systemic blind spots inventory revealed
               check_cron_freshness,
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
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (issue, url)
);
CREATE INDEX IF NOT EXISTS brain_findings_last_seen_idx
    ON brain_findings (last_seen DESC);
CREATE INDEX IF NOT EXISTS brain_findings_issue_idx
    ON brain_findings (issue);
"""


def _persist_findings_to_db(findings: list[dict]) -> int:
    """Write findings to brain_findings. UPSERT on (issue, url) so the
    same finding rolling across scans increments seen_count + bumps
    last_seen instead of duplicating. Returns rows touched.

    Defensive — never raises; persistence failures don't fail the
    scan."""
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
                # Stale-removal: mark findings older than 2 scan cycles
                # (10 min) as gone if they didn't reappear this run.
                # First grab the current scan's unique keys, then
                # delete brain_findings rows whose last_seen is older
                # than 10 min AND not in this scan's keys.
                current_keys = set()
                for f in findings:
                    if not isinstance(f, dict): continue
                    issue = (f.get("issue") or "")[:200]
                    url   = (f.get("url") or "")[:500]
                    if not issue: continue
                    current_keys.add((issue, url))
                    cur.execute("""
                        INSERT INTO brain_findings
                            (issue, url, count, detail,
                             first_seen, last_seen, seen_count)
                        VALUES (%s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING, NOW(), 1)
                        ON CONFLICT (issue, url) DO UPDATE
                           SET count       = EXCLUDED.count,
                               detail      = EXCLUDED.detail,
                               last_seen   = NOW(),
                               seen_count  = brain_findings.seen_count + 1
                    """, (issue, url, f.get("count"),
                          (f.get("detail") or "")[:2000]))
                    rows += 1
                # Sweep stale findings (haven't reappeared in 10 min)
                cur.execute("""
                    DELETE FROM brain_findings
                     WHERE last_seen < NOW() - INTERVAL '10 minutes'
                """)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Defensive — persistence never fails the scan
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
            _persist_findings_to_db(findings or [])
        except Exception:
            pass
        return result
    finally:
        _release_scan_lock()
