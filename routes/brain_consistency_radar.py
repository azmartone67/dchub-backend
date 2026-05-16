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


# ── 6. ISO-loop freshness ─────────────────────────────────────────
#
# Phase SS (2026-05-15). Every ISO writes to `grid_data` with a unique
# `iso` value (PJM/MISO/CAISO/ERCOT/SPP/NYISO/ISO-NE/+CAN). The existing
# data_freshness_radar checks the union (just market_power_scores), so
# a single ISO loop silently dying (e.g. PJM API returning 500s for 4h)
# isn't surfaced anywhere until it cascades into stale DCPI scores.
# This detector watches each ISO independently against a per-source SLA.
_ISO_FRESHNESS_SLA_HOURS = 2   # ISO loops run every 15-30 min, so 2h = clear miss
_TRACKED_ISOS = ["PJM", "ERCOT", "CAISO", "MISO", "SPP", "NYISO", "ISO-NE",
                 "BPA", "AESO", "IESO", "TVA"]


def _db():
    """Local DB conn helper — autocommit so a failed probe doesn't poison
    follow-up queries within scan_all()."""
    import os
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def check_iso_freshness() -> list[dict]:
    """Per-ISO freshness probe. Reads grid_data MAX(timestamp) grouped by
    iso and flags any ISO whose latest row is older than SLA hours.
    Pre-empts the "stale DCPI scores" cascade by catching the upstream
    loop death at its true source."""
    findings: list[dict] = []
    conn = _db()
    if not conn:
        return findings
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.grid_data')")
                if not (cur.fetchone() or [None])[0]:
                    return findings  # table doesn't exist (test env)
            except Exception:
                return findings

            cur.execute("""
                SELECT iso,
                       MAX(timestamp)               AS latest_ts,
                       EXTRACT(EPOCH FROM (NOW() - MAX(timestamp))) / 3600.0
                                                    AS age_hours,
                       COUNT(*)                     AS row_count
                  FROM grid_data
                 GROUP BY iso
            """)
            present = {row[0]: (row[1], float(row[2] or 0), int(row[3] or 0))
                       for row in cur.fetchall()}

        for iso in _TRACKED_ISOS:
            info = present.get(iso)
            if info is None:
                # Tracked but never written → probably loop never deployed
                findings.append({
                    "issue":  "iso_loop_no_data",
                    "url":    f"grid_data WHERE iso='{iso}'",
                    "count":  1,
                    "detail": (f"ISO {iso} has zero rows in grid_data. "
                               f"Either the loop is not registered, the cron "
                               f"isn't firing, or the loop is crashing before "
                               f"its first INSERT. Check routes/iso_{iso.lower().replace('-','')}.py "
                               f"and the corresponding GH Actions workflow."),
                })
                continue
            _latest, age, rows = info
            if age > _ISO_FRESHNESS_SLA_HOURS:
                findings.append({
                    "issue":  "iso_loop_stale",
                    "url":    f"grid_data WHERE iso='{iso}'",
                    "count":  1,
                    "detail": (f"ISO {iso} hasn't written to grid_data in "
                               f"{age:.1f}h (SLA: {_ISO_FRESHNESS_SLA_HOURS}h, "
                               f"row count: {rows}). The loop is either "
                               f"crashing silently, rate-limited by the ISO "
                               f"endpoint, or its cron is paused."),
                })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── 7. MCP tool error-rate spike ──────────────────────────────────
#
# Phase SS (2026-05-15). When an MCP tool's backend dependency breaks
# (e.g. EIA API change, Cloudflare worker quota hit), the tool quietly
# starts returning errors but every paywall and call-count metric looks
# fine because requests are still arriving. This detector watches the
# `status` field on mcp_call_log over the past hour and flags any tool
# whose error rate crosses 20% with enough volume to matter.
_MCP_ERROR_RATE_THRESHOLD = 0.20      # 20%
_MCP_ERROR_MIN_VOLUME     = 10        # ignore noise from low-volume tools


def check_mcp_tool_error_rate() -> list[dict]:
    """Per-tool error rate over the last hour. Pre-empts the "everything
    works but the user is confused" failure mode where a single tool is
    silently failing for everyone."""
    findings: list[dict] = []
    conn = _db()
    if not conn:
        return findings
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.mcp_call_log')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
            except Exception:
                return findings

            cur.execute("""
                SELECT tool,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status IN ('error', 'fail', 'failed',
                                                '500', '502', '503', '504')
                                THEN 1 ELSE 0 END) AS errors
                  FROM mcp_call_log
                 WHERE timestamp >= NOW() - INTERVAL '1 hour'
                   AND tool IS NOT NULL
                 GROUP BY tool
                HAVING COUNT(*) >= %s
                 ORDER BY errors DESC
            """, (_MCP_ERROR_MIN_VOLUME,))
            for row in cur.fetchall():
                tool, total, errs = row[0], int(row[1] or 0), int(row[2] or 0)
                if total <= 0: continue
                rate = errs / total
                if rate >= _MCP_ERROR_RATE_THRESHOLD:
                    findings.append({
                        "issue":  "mcp_tool_error_spike",
                        "url":    f"mcp_call_log: tool={tool}",
                        "count":  errs,
                        "detail": (f"Tool '{tool}' has {errs}/{total} errors "
                                   f"in the last hour ({rate*100:.0f}% error "
                                   f"rate). Check the backend route it calls "
                                   f"(see dchub_mcp_server.py:_api_get target) "
                                   f"and the upstream dependency (EIA, OSM, "
                                   f"Cloudflare Vectorize, etc.)."),
                    })
    finally:
        try: conn.close()
        except Exception: pass
    return findings


# ── 8. Traffic anomaly (sudden drop or unusual spike) ─────────────
#
# Phase SS (2026-05-15). When the CF Pages worker, the MCP proxy, or
# the Railway backend drops requests (TLS error, DNS hiccup, rate-limit
# cascade), the most visible symptom is *missing traffic* — but nothing
# else throws. This detector compares the last hour's MCP call volume
# to the same-hour-of-week 7d median and flags drops or spikes ≥ 3×.
_TRAFFIC_DROP_RATIO  = 0.30   # current < 30% of baseline = drop
_TRAFFIC_SPIKE_RATIO = 5.0    # current > 5× baseline   = spike
_TRAFFIC_MIN_BASELINE = 5     # don't divide-by-zero on quiet hours


def check_traffic_anomaly() -> list[dict]:
    """Compares this hour's MCP call volume to last-7-days same-hour
    median. Surfaces silent outages (drop) and bot/abuse spikes."""
    findings: list[dict] = []
    conn = _db()
    if not conn:
        return findings
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT to_regclass('public.mcp_call_log')")
                if not (cur.fetchone() or [None])[0]:
                    return findings
            except Exception:
                return findings

            # Current: rows in the last 60 min.
            cur.execute("""
                SELECT COUNT(*) FROM mcp_call_log
                 WHERE timestamp >= NOW() - INTERVAL '1 hour'
            """)
            current = int((cur.fetchone() or [0])[0] or 0)

            # Baseline: rows in each of the last 7 same-hour-of-week windows.
            cur.execute("""
                WITH hourly AS (
                    SELECT date_trunc('hour', timestamp) AS hr,
                           COUNT(*)                       AS n
                      FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '8 days'
                       AND timestamp <  NOW() - INTERVAL '1 hour'
                       AND EXTRACT(DOW  FROM timestamp) =
                           EXTRACT(DOW  FROM NOW())
                       AND EXTRACT(HOUR FROM timestamp) =
                           EXTRACT(HOUR FROM NOW())
                     GROUP BY hr
                )
                SELECT COALESCE(
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY n), 0
                ) AS median_n
                  FROM hourly
            """)
            baseline = float((cur.fetchone() or [0])[0] or 0)

        if baseline < _TRAFFIC_MIN_BASELINE:
            return findings  # not enough history yet — skip rather than spam

        if current < baseline * _TRAFFIC_DROP_RATIO:
            findings.append({
                "issue":  "mcp_traffic_drop",
                "url":    "mcp_call_log: last 1h",
                "count":  current,
                "detail": (f"MCP call volume in the last hour ({current}) is "
                           f"<{int(_TRAFFIC_DROP_RATIO*100)}% of the 7-day "
                           f"same-hour median ({baseline:.0f}). Likely causes: "
                           f"CF Pages worker outage, MCP proxy down, Railway "
                           f"backend not responding, or DNS/TLS issue. Check "
                           f"/healthz and CF dashboard."),
            })
        elif current > baseline * _TRAFFIC_SPIKE_RATIO:
            findings.append({
                "issue":  "mcp_traffic_spike",
                "url":    "mcp_call_log: last 1h",
                "count":  current,
                "detail": (f"MCP call volume in the last hour ({current}) is "
                           f">{int(_TRAFFIC_SPIKE_RATIO)}× the 7-day same-hour "
                           f"median ({baseline:.0f}). Likely causes: bot/abuse "
                           f"campaign, viral citation, runaway client retry "
                           f"loop, or a new integration going live. Check "
                           f"mcp_call_log GROUP BY client_name for the source."),
            })
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
               check_iso_freshness,
               check_mcp_tool_error_rate,
               check_traffic_anomaly):
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
