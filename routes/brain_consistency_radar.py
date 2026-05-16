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
_WORKER_SOURCE_URL = "https://raw.githubusercontent.com/azmartone67/dchub-frontend/main/_worker.js"
_WORKER_PROBE_URL  = "https://dchub.cloud/api/v1/dcpi/scores?limit=1"


# Mutable holder for the last fetch error so detector messages can echo
# the real urllib error to the finding's detail field (otherwise we just
# get a generic "unreachable" and have to grep Railway logs).
_LAST_FETCH_ERROR: dict[str, str] = {}


def _http_get(url: str, timeout: int = 8) -> tuple[Optional[str], Optional[dict]]:
    """Returns (body, headers_dict) or (None, None) on error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dchub-brain-radar/1.0"})
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
_TOOL_API_MAPPING = {
    "get_market_intel":      "/api/v1/market-intel",
    "get_grid_intelligence": "/api/v1/grid/intelligence",
    "get_fiber_intel":       "/api/v1/fiber/intel",
    "get_water_risk":        "/api/v1/water-risk",
    "get_energy_prices":     "/api/v1/energy/summary",
    "get_pipeline":          "/api/v1/pipeline",
    "list_transactions":     "/api/v1/transactions",
    "get_grid_data":         "/api/v1/grid",
    "get_renewable_energy":  "/api/v1/energy/renewable",
    "get_tax_incentives":    "/api/v1/tax-incentives",
    "get_intelligence_index":"/api/v1/intelligence-index",
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
        # Heuristic: anonymous response shape. A `gated: true` field or
        # explicit "paid_tier_required" or HTTP 403 message in the
        # payload is the tell.
        low = body.lower()
        if ('"gated":true' in low.replace(" ", "")) or \
           ('paid_tier_required' in low) or \
           ('upgrade to' in low and 'developer' in low) or \
           ('"min_tier":"developer"' in low.replace(" ", "")) or \
           ('"min_tier":"pro"' in low.replace(" ", "")):
            findings.append({
                "issue": "tier_inconsistency_web_higher_than_mcp",
                "url": web_path,
                "count": 1,
                "detail": (f"MCP tool `{tool}` is at IDENTIFIED tier but "
                           f"the web endpoint `{web_path}` appears gated "
                           f"at DEVELOPER or higher. Agents using MCP can "
                           f"access this data with a free dev key; web "
                           f"users hit a paywall. Fix: align the web API "
                           f"decorator to match the MCP tier."),
                "tool": tool,
                "mcp_tier": mcp_tier.name,
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
    "hot_leads_preview",          # dry-run preview
    "hot_leads_send_top_5",       # safety preview before top_50
    "free_users_dryrun",          # dry-run preview
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
        if "github.event.schedule" not in cond:
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
                if "github.event.schedule" in cond:
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


def scan_all() -> list[dict]:
    """Run every detector. Return a flat list of finding dicts ready
    to merge into actionable_backend_issues."""
    out: list[dict] = []
    for fn in (check_worker_version_drift,
               check_tier_consistency,
               check_cron_coverage,
               check_cron_collisions,
               check_csp_drift):
        try:
            out.extend(fn() or [])
        except Exception as e:
            out.append({
                "issue": "consistency_radar_detector_crashed",
                "url": fn.__name__,
                "count": 1,
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


def scan_summary() -> dict:
    """Same as scan_all() but wrapped for the /api/v1/brain/consistency-radar
    endpoint — adds a count + as_of timestamp + grouping by issue type."""
    findings = scan_all()
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
    }
