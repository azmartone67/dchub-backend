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
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code} {e.reason}"
        _LAST_FETCH_ERROR[url] = msg
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
    source_body, _ = _http_get(_WORKER_SOURCE_URL, timeout=30)
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

    for tool, web_path in _TOOL_API_MAPPING.items():
        mcp_tier = TOOL_TIER.get(tool)
        if mcp_tier is None or mcp_tier.value > Tier.IDENTIFIED.value:
            # MCP gates at DEVELOPER+ — web API gating higher is fine.
            continue
        body, _h = _http_get(f"https://dchub.cloud{web_path}?_=radar", timeout=6)
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
        WEB_TIER_RANK = {"free": 0, "identified": 1, "developer": 2,
                          "pro": 3, "enterprise": 4}
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

def check_surface_health_critical() -> list[dict]:
    """Flag any surface whose health_score < 40. The brain learns which
    pages are dying + escalates per-surface so the right action library
    fires (markets needs a different fix than land_power)."""
    findings: list[dict] = []
    try:
        from routes.surface_brain import SURFACES
    except Exception:
        return findings
    for sid, surface in SURFACES.items():
        try:
            score = surface.health_score()
        except Exception:
            continue
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
def check_operator_profile_gap() -> list[dict]:
    """Surface top operators by facility count that lack rich
    metadata (missing markets, missing power_mw on most facilities).
    Brain flags so discovery pipeline can prioritize fills — closes
    the per-operator-profile gap vs DCHawk/dcByte."""
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
                     ORDER BY COUNT(*) DESC LIMIT 10
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
    latest_pct = float(rows[-1].get("score_pct") or 0)
    # 7-day delta if available
    week_ago = next((r for r in rows[::-1]
                     if r["date"] and rows[-1]["date"] and
                     (datetime.datetime.fromisoformat(rows[-1]["date"]) -
                      datetime.datetime.fromisoformat(r["date"])).days >= 7), None)
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
    SLAS = [
        # (table, age_column, max_hours, friendly_label)
        ("dcpi_scores",          "computed_at",  12,  "DCPI scores"),
        ("discovered_facilities","discovered_at",24,  "facility discovery"),
        ("news_items",           "published_at", 6,   "news ingest"),
        ("ai_citations",         "observed_at",  168, "AI citations (weekly)"),
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

    for api_path, page_path, max_sec, label in _PROBES:
        url = f"https://dchub.cloud{api_path}"
        import time as _t
        t0 = _t.time()
        try:
            r = _req.get(url, timeout=max_sec + 2,
                          headers={"User-Agent": "dchub-frontend-health/1.0"})
            elapsed = _t.time() - t0
        except Exception as e:
            elapsed = _t.time() - t0
            findings.append({
                "issue":  "frontend_endpoint_unreachable",
                "url":    page_path,
                "count":  1,
                "detail": (f"Public page `{page_path}` depends on API `{api_path}` "
                           f"({label}) which timed out / errored after "
                           f"{elapsed:.1f}s: {type(e).__name__}. The page renders "
                           f"empty/broken to visitors. Likely Railway upstream "
                           f"failure or endpoint regression."),
            })
            continue
        if r.status_code >= 500:
            findings.append({
                "issue":  "frontend_endpoint_5xx",
                "url":    page_path,
                "count":  r.status_code,
                "detail": (f"Public page `{page_path}` depends on API `{api_path}` "
                           f"({label}) which returned HTTP {r.status_code}. "
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
    calls, ~7 GETs total. Fails-closed: if the probe itself errors,
    return empty findings (don't false-positive on network noise)."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    for path in _BREACH_PROBE_ENDPOINTS:
        try:
            r = _req.get(f"https://dchub.cloud{path}",
                         timeout=10,
                         headers={"User-Agent": "dchub-breach-detector/1.0"})
        except Exception:
            continue  # network noise — don't flag
        if r.status_code != 200:
            continue  # 402/403/404 is the gate working; skip
        body = r.text or ""
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
]


def check_dead_internal_links() -> list[dict]:
    """Phase RRR-newsletter (2026-05-18) — probe every high-traffic
    internal URL and flag any that 404 or 5xx. Catches the dead-link
    bug class that's silent from the user's side."""
    findings: list[dict] = []
    try:
        import requests as _req
    except Exception:
        return findings
    import time as _t
    headers = {"User-Agent": "dchub-brain-deadlink-probe/1.0"}
    for path in _INTERNAL_LINK_PROBES:
        url = f"https://dchub.cloud{path}"
        t0 = _t.time()
        try:
            # HEAD is faster but some CF-served pages don't support it;
            # GET with a tight timeout is more universal.
            r = _req.get(url, timeout=5, headers=headers, allow_redirects=True)
            elapsed = _t.time() - t0
        except Exception as e:
            findings.append({
                "issue":  "internal_link_unreachable",
                "url":    path,
                "count":  1,
                "detail": (f"`{path}` failed to load: "
                           f"{type(e).__name__}: {str(e)[:120]}. "
                           f"Either CF Pages route missing OR CF Worker "
                           f"can't reach the backend handler."),
            })
            continue
        if r.status_code == 404:
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
        elif r.status_code >= 500:
            findings.append({
                "issue":  "internal_link_5xx",
                "url":    path,
                "count":  r.status_code,
                "detail": (f"`{path}` returns HTTP {r.status_code} "
                           f"(server error). Body: {r.text[:120]}"),
            })
        elif r.status_code in (401, 403):
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
    didn't grep first."""
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
        # If both endpoint names are identical, the dup was caused by
        # stacked @route decorators with the same path — usually a copy-
        # paste typo. If the names differ, two real functions are
        # competing for the path.
        same_endpoint = (len(set(endpoints)) == 1)
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
                + "grep the codebase for the path and pick one canonical handler."
            ),
        })
    return findings


def scan_all() -> list[dict]:
    """Run every detector. Return a flat list of finding dicts ready
    to merge into actionable_backend_issues."""
    out: list[dict] = []
    for fn in (check_worker_version_drift,
               check_tier_consistency,
               check_cron_coverage,
               check_cron_collisions,
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
               check_cron_endpoint_unscheduled):
        try:
            out.extend(fn() or [])
        except Exception as e:
            # Phase CCC (2026-05-16): include the detector function name
            # in the issue string itself so the heartbeat's by_issue
            # summary surfaces WHICH detector crashed without needing
            # a deep findings drill-down. The detail still carries the
            # full traceback excerpt for the engineer.
            out.append({
                "issue":  f"consistency_radar_detector_crashed:{fn.__name__}",
                "url":    fn.__name__,
                "count":  1,
                "detail": f"{type(e).__name__}: {str(e)[:200]}",
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
_SCAN_CACHE: dict = {"value": None, "expires_at": 0.0}
_SCAN_CACHE_TTL_SECONDS = 300  # 5 minutes


def scan_summary() -> dict:
    """Same as scan_all() but wrapped for the /api/v1/brain/consistency-radar
    endpoint — adds a count + as_of timestamp + grouping by issue type.

    Cached in-process for 5 min. The brain runs every detector on the
    server-side cron anyway (so findings are always fresh in the
    underlying DB); this just keeps dashboard polls from running 50+
    detectors live on every request.
    """
    now = _t_mod.time()
    if _SCAN_CACHE["value"] is not None and now < _SCAN_CACHE["expires_at"]:
        return _SCAN_CACHE["value"]

    findings = scan_all()
    by_issue: dict[str, int] = {}
    for f in findings:
        by_issue[f["issue"]] = by_issue.get(f["issue"], 0) + 1
    from datetime import datetime, timezone
    result = {
        "ok": True,
        "count": len(findings),
        "by_issue": by_issue,
        "findings": findings,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "cache_ttl_seconds": _SCAN_CACHE_TTL_SECONDS,
    }
    _SCAN_CACHE["value"] = result
    _SCAN_CACHE["expires_at"] = now + _SCAN_CACHE_TTL_SECONDS
    return result
