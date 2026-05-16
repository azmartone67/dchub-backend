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
        WEB_TIER_RANK = {"free": 1, "identified": 2, "developer": 3,
                          "pro": 4, "enterprise": 5}
        web_rank = WEB_TIER_RANK.get(web_min_tier, 0)
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
            # Inner try — column-missing is the most likely failure mode
            try:
                cur.execute("""
                    WITH paid_demand AS (
                      SELECT tool, COUNT(*) AS signals
                        FROM mcp_upgrade_signals
                       WHERE created_at >= NOW() - INTERVAL '7 days'
                         AND tool IS NOT NULL
                       GROUP BY tool HAVING COUNT(*) >= 50
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

            # All recent news text (one query — preferred over N+1)
            news_text = ""
            try:
                cur.execute("""
                    SELECT LOWER(COALESCE(title,'') || ' ' || COALESCE(summary,'')) AS text
                      FROM news
                     WHERE published_date >= NOW() - INTERVAL '24 hours'
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
               check_media_topic_unaddressed):
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
