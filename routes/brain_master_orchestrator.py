"""brain_master_orchestrator.py — the "master shell".

ONE entrypoint that ticks the whole brain discovery→action flywheel in
tiered order, so the loop runs as a single coherent cycle instead of five
disconnected crons (autopilot / L15 / L22 / L23 / verifier). Every layer
already exists and carries its own safety gates; this orchestrates them
and returns a unified report of what was auto-fixed, drafted, escalated,
and verified — the missing "fully automatic and agentic" control surface.

Tiering (matches the evolution plan):
  Tier 1 — AUTO-FIX (autonomous): /api/v1/brain/autopilot/run executes the
           whitelisted Tier-A pattern actions (redirects, recomputes,
           freshness refreshes, restarts). Rate-limited + quarantined.
  Tier 2 — AUTO-DRAFT-PR (review before merge): /api/v1/brain/auto-action/run
           (L15 causal→GH issues) + /api/v1/admin/brain/draft-prs/run
           (L22 auto-code draft PRs). NEVER auto-merges.
  Tier 3 — HUMAN-GATED (propose only): /api/v1/brain/lifecycle/audit (L23
           capability proposals) + a digest of money/pricing findings that
           must NOT auto-act (funnel leaks, addressable demand, citations).
  Verify — /api/v1/brain/autopilot/verify-pending confirms prior actions
           actually resolved their findings (feeds quarantine).

Endpoints:
  POST /api/v1/admin/brain/master-tick         — start one full cycle (202)
       ?dry=1                — preview: skip Tier-1 execution + drafting
       ?tiers=1,2,3,verify   — run a subset (default: all)
  GET  /api/v1/admin/brain/master-tick/last    — last cycle's report

r-starve (2026-07-03): the tick is FIRE-AND-FORGET. It chains 8-10 serial
self-HTTP calls (90s budget each), so running it inside a request handler
held a web thread for the whole cycle ('SLOW REQUEST: master-tick took
81.7s' during the 07-03 outage window) and fed the thread-pool starvation
that trips the health watchdog. POST now returns 202 immediately and the
cycle runs in a daemon thread guarded by a non-blocking lock (same r-async
pattern as /api/news/refresh); the report lands in brain_master_ticks and
is served by GET /master-tick/last.

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Fail-closed.
Safety: this layer only CALLS the existing run endpoints, so all their
kill-switches, rate-limits, diff-caps and forbidden-path guards still
apply. A master kill switch BRAIN_MASTER_DISABLED=1 halts the whole tick.
It NEVER merges PRs and NEVER acts on Tier-3 (money) findings.
"""
from __future__ import annotations

import os
import json
import time
import threading
import datetime as _dt
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

brain_master_orchestrator_bp = Blueprint("brain_master_orchestrator", __name__)

# r-starve: one tick at a time. acquire(blocking=False) so an overlapping
# trigger answers 202/already-running instead of queueing a second cycle.
_tick_running = threading.Lock()

_BACKEND_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _is_master_disabled() -> bool:
    return str(os.environ.get("BRAIN_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


# Finding kinds that must NEVER auto-act — money/pricing/positioning calls
# that stay human-gated. The master tick surfaces these as a digest only.
_HUMAN_GATED_PREFIXES = (
    "mcp_funnel_leak",
    "addressable_demand_unconverted",
    "zero_conversion",
    "citation_score_below",
    "billing", "pricing", "tier_",
)


def _call(method: str, path: str, use_admin: bool = True, timeout: int = 90) -> dict:
    """Self-call an existing run endpoint. Mirrors brain_autopilot._execute_action:
    identify via X-DC-Probe so the rate-limiter bypasses us, attach admin key."""
    url = _BACKEND_BASE.rstrip("/") + path
    started = time.time()
    try:
        data = b"{}" if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-DC-Probe", "master-tick")
        req.add_header("User-Agent", "dchub-master-orchestrator/1.0")
        if use_admin:
            ak = _admin_key()
            if ak:
                req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_raw": body[:300]}
            return {"ok": True, "http": resp.status, "ms": int((time.time() - started) * 1000), "data": parsed}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        return {"ok": False, "http": e.code, "error": f"HTTP {e.code}", "body": body,
                "ms": int((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:160]}",
                "ms": int((time.time() - started) * 1000)}


# ── #49 lane 5: the tick's steps, DECLARED ────────────────────────────
# The tick chained 8-10 serial self-HTTP calls at a 90s budget each, with tier
# order hardcoded in _run_master_tick. A node's prerequisites existed only as
# the order the lines happened to be written in, so nothing could tell that
# tier 3's read-only probes depend on none of each other — and a failure at the
# last step re-ran every step before it.
#
# Declaring depends_on is what lets the runner fan out what is independent and
# SKIP what is not. Tier 3's four probes each hit a different subsystem and read
# none of the others' output; tier 2 carries a real edge (see below).
ORCHESTRATOR_NODES_T1 = (
    {"step": "tier0.grid_data_master", "method": "POST",
     "path": "/api/v1/admin/grid-data/master-tick", "depends_on": ()},
)
# Tier 2 DOES have a real dependency: the draft-PR writer works from findings
# the auto-action pass has classified, so declaring it is the first edge in
# this graph that is not simply "these four are independent".
ORCHESTRATOR_NODES_T2 = (
    {"step": "tier2.l15_auto_action", "method": "POST",
     "path": "/api/v1/brain/auto-action/run", "depends_on": ()},
    {"step": "tier2.l22_draft_prs", "method": "POST",
     "path": "/api/v1/admin/brain/draft-prs/run",
     "depends_on": ("tier2.l15_auto_action",)},
)
ORCHESTRATOR_NODES_T3 = (
    {"step": "tier3.l23_lifecycle", "method": "GET",
     "path": "/api/v1/brain/lifecycle/audit", "depends_on": ()},
    {"step": "tier3.semantic_dup_findings", "method": "GET",
     "path": "/api/v1/admin/brain/rag/duplicate-findings?threshold=0.9&limit=25",
     "depends_on": ()},
    {"step": "tier3.promote_on_failure", "method": "POST",
     "path": "/api/v1/brain/autopilot/promote-on-failure", "depends_on": ()},
    {"step": "tier3.forecast_findings", "method": "GET",
     "path": "/api/v1/brain/predictions", "depends_on": ()},
)


def _parallel_enabled() -> bool:
    """★BUILT DORMANT (default "0"), and that is a considered choice, not
    caution for its own sake.

    The 2026-07-03 outage was web thread-pool STARVATION traced to master-tick
    holding a thread for its whole cycle ('SLOW REQUEST: master-tick took
    81.7s'). The tick itself is a daemon thread now, but every step is a
    self-HTTP call served by that same pool — so fanning out N steps means N
    web threads busy where there was 1. That is the same shape as the outage,
    and the dyno's worker/thread count is not knowable from inside this module.

    So: the DAG is declared, the runner is written and tested, and the fan-out
    waits for someone to flip it while watching the pool. Per-node timings are
    in the report either way, so the decision can be made on data.

    BRAIN_MASTER_PARALLEL=1 enables; BRAIN_MASTER_PARALLEL_MAX caps width (3)."""
    return (os.environ.get("BRAIN_MASTER_PARALLEL") or "").strip() == "1"


def _parallel_width() -> int:
    try:
        n = int(os.environ.get("BRAIN_MASTER_PARALLEL_MAX") or "3")
    except Exception:
        n = 3
    return max(1, min(n, 6))


def resume_nodes(step_names) -> list:
    """Re-run named nodes ALONE (#49 lane 5, resumability).

    A failure at the last step used to mean re-running every step before it, at
    90s of budget each — which is why the practical response to a flaky step
    was to wait for the next cron rather than re-fire. Dependencies are still
    honoured: asking for a node pulls in the prerequisites it needs, so a
    resume can never run a node against a stale prerequisite just because the
    operator named only the one that failed.

    Unknown names are returned as explicit skips rather than ignored — a resume
    that silently does nothing is worse than one that says the name was wrong."""
    wanted = {str(n) for n in (step_names or [])}
    if not wanted:
        return []
    by_step = {n["step"]: n for n in
               (ORCHESTRATOR_NODES_T1 + ORCHESTRATOR_NODES_T2
                + ORCHESTRATOR_NODES_T3)}
    unknown = [w for w in sorted(wanted) if w not in by_step]
    # Pull in prerequisites transitively.
    needed, frontier = set(), [w for w in wanted if w in by_step]
    while frontier:
        cur = frontier.pop()
        if cur in needed:
            continue
        needed.add(cur)
        for dep in (by_step[cur].get("depends_on") or ()):
            if dep in by_step:
                frontier.append(dep)
    plan = [n for n in (ORCHESTRATOR_NODES_T1 + ORCHESTRATOR_NODES_T2
                        + ORCHESTRATOR_NODES_T3) if n["step"] in needed]
    out = _run_nodes(plan) if plan else []
    for name in unknown:
        out.append({"step": name, "ok": False, "skipped": True,
                    "error": "unknown_node: not a declared step"})
    return out


def _run_nodes(nodes) -> list:
    """Execute declared nodes, respecting depends_on. Returns summaries in
    DECLARATION order regardless of completion order, so the report reads
    identically whether or not the fan-out is on — a diff in the report should
    mean the system changed, not that the scheduler did.

    Serial unless _parallel_enabled(). A node whose dependency failed is
    reported as skipped rather than run against a missing prerequisite."""
    results = {}
    done_ok = set()
    remaining = [dict(n) for n in nodes]
    # Dependency LEVELS: everything whose prerequisites are already satisfied
    # runs together. With all-empty depends_on this is one level, which is the
    # tier-3 case; the structure is what makes a future dependency expressible.
    guard = 0
    while remaining and guard <= len(nodes):
        guard += 1
        ready = [n for n in remaining
                 if all(d in done_ok for d in (n.get("depends_on") or ()))]
        if not ready:
            for n in remaining:
                results[n["step"]] = {
                    "step": n["step"], "ok": False, "skipped": True,
                    "error": "unmet_dependency: "
                             + ",".join(n.get("depends_on") or ())}
            break
        if _parallel_enabled() and len(ready) > 1:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=_parallel_width()) as pool:
                futs = {pool.submit(_call, n["method"], n["path"]): n
                        for n in ready}
                for fut in _cf.as_completed(futs):
                    n = futs[fut]
                    try:
                        r = fut.result()
                    except Exception as e:  # noqa: BLE001
                        r = {"ok": False, "error": f"{type(e).__name__}: "
                                                   f"{str(e)[:160]}"}
                    results[n["step"]] = _summarize(n["step"], r)
                    if r.get("ok"):
                        done_ok.add(n["step"])
        else:
            for n in ready:
                r = _call(n["method"], n["path"])
                results[n["step"]] = _summarize(n["step"], r)
                if r.get("ok"):
                    done_ok.add(n["step"])
        ready_steps = {n["step"] for n in ready}
        remaining = [n for n in remaining if n["step"] not in ready_steps]
    out = [results[n["step"]] for n in nodes if n["step"] in results]
    if out:
        out[0] = dict(out[0])
        out[0]["_parallel"] = _parallel_enabled()
    return out


def _summarize(step: str, r: dict) -> dict:
    """Pull a compact, human-meaningful summary out of each layer's response."""
    d = r.get("data") or {}
    s = {"step": step, "ok": r.get("ok"), "http": r.get("http"), "ms": r.get("ms")}
    if r.get("error"):
        s["error"] = r["error"]
    # Best-effort extraction of the fields each layer reports.
    for k in ("executed", "executed_ok", "escalated", "actions_taken", "drafted",
              "prs_drafted", "issues_opened", "verified", "succeeded", "failed",
              "scanned", "proposals", "count", "processed", "skipped"):
        if isinstance(d, dict) and k in d:
            s[k] = d[k]
    if isinstance(d, dict) and isinstance(d.get("actions"), list):
        s["actions_n"] = len(d["actions"])
    return s


def _acquisition_scoreboard() -> dict:
    """2026-07-03 — the growth north-star, read every tick. Pulls the weekly
    reach trend (distinct external agents + NEW external IPs/week) so the brain
    is ORIENTED on acquisition, not just reliability. Flags a decline vs the
    trailing 3-week average so a falling agent-acquisition number becomes a
    visible, tracked signal (it powers the human digest + future auto-actions).
    Best-effort; never raises."""
    r = _call("GET", "/api/v1/ai/reach/trend?cb=master", use_admin=False, timeout=30)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "reach_trend_unreachable")}
    d = r.get("data") or {}
    cur = d.get("current") or {}
    weeks = d.get("weeks") or d.get("trend") or []
    new_now = cur.get("new_external_ips")
    agents_now = cur.get("distinct_external_ips")
    # Decline is judged on COMPLETE weeks only — the current week is partial and
    # would always read low. Series of new_external_ips; last element is the
    # in-progress week, so compare the last-complete week to the 3 before it.
    series = [w.get("new_external_ips") for w in weeks
              if isinstance(w.get("new_external_ips"), int)]
    last_complete = series[-2] if len(series) >= 2 else None
    prior = series[-5:-2] if len(series) >= 5 else series[:-2]
    baseline = round(sum(prior) / len(prior), 1) if prior else None
    declining = (isinstance(last_complete, int) and baseline is not None
                 and last_complete < baseline * 0.8)
    top = sorted((cur.get("per_platform") or []),
                 key=lambda p: p.get("agents", 0), reverse=True)[:4]
    return {
        "ok": True,
        "new_agents_this_week": new_now,
        "active_agents_this_week": agents_now,
        "baseline_new_3wk_avg": baseline,
        "declining": bool(declining),
        "top_sources": [{"platform": p.get("platform_id"),
                         "agents": p.get("agents")} for p in top],
        "week_start": cur.get("week_start"),
    }


def _human_gated_digest() -> dict:
    """Read the public action-queue and bucket out the money/positioning findings
    that the master tick will NEVER auto-act — surfaced for the human."""
    r = _call("GET", "/api/v1/brain/action-queue?cb=master", use_admin=False, timeout=40)
    items = []
    if r.get("ok"):
        for q in (r.get("data") or {}).get("queue", []):
            issue = q.get("issue", "")
            if any(issue.startswith(p) for p in _HUMAN_GATED_PREFIXES):
                items.append({
                    "issue": issue,
                    "seen_count": q.get("seen_count"),
                    "detail": (q.get("detail") or "")[:180],
                })
    return {"count": len(items), "items": items[:12]}


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_master_ticks (
                id        BIGSERIAL PRIMARY KEY,
                ran_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dry_run   BOOLEAN,
                tiers     TEXT,
                report    JSONB
            )
        """)
    conn.commit()


def _persist(report: dict):
    try:
        import psycopg2
        du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
        if not du:
            return
        with psycopg2.connect(du, connect_timeout=8) as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO brain_master_ticks (dry_run, tiers, report) VALUES (%s,%s,%s)",
                    (bool(report.get("dry_run")), ",".join(report.get("tiers_run", [])),
                     json.dumps(report)))
            conn.commit()
    except Exception:
        note_swallowed_write("brain_master_ticks", where="brain_master_orchestrator._persist")
        pass


def _is_admin(req) -> bool:
    expected = _admin_key()
    if not expected:
        return False
    got = (req.headers.get("X-Admin-Key") or req.args.get("admin_key") or "").strip()
    return bool(got) and got == expected


def _run_master_tick(dry: bool, tiers: set) -> dict:
    """One full orchestrated cycle. Runs in a daemon thread (r-starve) —
    must not touch flask.request; everything it needs is passed in."""
    report = {
        "ok": True,
        "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "dry_run": dry,
        "tiers_run": [],
        "steps": [],
    }

    # ── Tier 0 — DATA DISCOVERY: refresh + widen the grid/power/gas data
    #    aggregation (absorb the next untapped gridstatus source + file fresh
    #    coverage gaps to brain_findings) BEFORE the brain tiers act, so the data
    #    shell's findings feed THIS same cycle. Writes → skipped on dry; own kill
    #    switch GRID_DATA_MASTER_DISABLED.
    if not dry:
        report["tiers_run"].append("0")
        report["steps"].extend(_run_nodes(ORCHESTRATOR_NODES_T1))

    # ── Tier 1 — AUTO-FIX (autonomous) ──────────────────────────────
    # Pass dry through to the autopilot so a dry master-tick previews only.
    if "1" in tiers:
        report["tiers_run"].append("1")
        path = "/api/v1/brain/autopilot/run" + ("?dry=1" if dry else "")
        report["steps"].append(_summarize("tier1.autopilot_run", _call("POST", path)))

    # ── Tier 2 — AUTO-DRAFT-PR (review before merge) ────────────────
    if "2" in tiers:
        report["tiers_run"].append("2")
        if dry:
            report["steps"].append({"step": "tier2.skipped_dry", "ok": True})
        else:
            # Declared with a REAL dependency: the draft-PR writer works from
            # findings the auto-action pass has classified, so a failed
            # auto-action must skip the writer rather than let it run against
            # last tick's classifications.
            report["steps"].extend(_run_nodes(ORCHESTRATOR_NODES_T2))
            # 2026-07-01: the CLOSE half of both open loops. L15 files issues
            # and the drafters open PRs, but nothing retired them — the open
            # lists only grew. Direct in-process calls (not HTTP) so the tick
            # works even if the blueprints failed to register. Both are
            # idempotent, capped, and kill-switchable.
            try:
                from routes.brain_issue_janitor import janitor_sweep as _issue_sweep
                _isj = _issue_sweep(dry_run=False) or {}
                report["steps"].append({"step": "tier2.issue_janitor",
                                        "ok": bool(_isj.get("ok")),
                                        "closed": _isj.get("closed_count", 0),
                                        "detail": {k: _isj.get(k) for k in
                                                   ("closed", "errors", "dry_run", "disabled")}})
            except Exception as _isj_e:
                report["steps"].append({"step": "tier2.issue_janitor", "ok": False,
                                        "error": str(_isj_e)[:160]})
            try:
                from routes.brain_pr_opener import expire_stale_draft_prs as _expire
                # days 7→5 (2026-07-02); 5→3 default (2026-07-03) to drain the
                # draft backlog faster — an untouched draft after 3 days is
                # stale and the brain re-proposes anything still worth doing.
                # Tunable without a deploy via BRAIN_DRAFT_PR_TTL_DAYS.
                _ttl_days = int(os.getenv("BRAIN_DRAFT_PR_TTL_DAYS", "3"))
                _exp = _expire(days=_ttl_days) or {}
                report["steps"].append({"step": "tier2.draft_pr_expire",
                                        "ok": bool(_exp.get("ok")),
                                        "closed": _exp.get("closed_count", 0),
                                        "detail": _exp})
            except Exception as _exp_e:
                report["steps"].append({"step": "tier2.draft_pr_expire", "ok": False,
                                        "error": str(_exp_e)[:160]})
            # 2026-09-02 (brain-agents sweep finding 7): the L22 recipe pool
            # was 14/14 ROTTEN — every tick re-skipped the same proposals
            # ("patched file fails ast.parse" ×10, "search text not in
            # main.py" ×4) and nothing ever retired them, so L22 replayed
            # instead of regenerating. draft_prs_run now counts rotten skips
            # per proposal; this expiry marks a proposal `stale` after 3.
            try:
                from routes.brain_backlog_admin import expire_rotten_proposals as _rot
                _rr = _rot() or {}
                report["steps"].append({"step": "tier2.draft_pr_expire_rotten",
                                        "ok": bool(_rr.get("ok")),
                                        "stale": _rr.get("marked_stale", 0),
                                        "detail": _rr})
            except Exception as _rot_e:
                report["steps"].append({"step": "tier2.draft_pr_expire_rotten",
                                        "ok": False, "error": str(_rot_e)[:160]})
            # 2026-09-02 (brain-agents sweep finding 2): re-drive approvals
            # whose PR draft FAILED (gateway 429, drafter exception) and that
            # still have no PR — the outcome is persisted on brain_approvals
            # now, so a lost draft is visible and retried instead of silently
            # indistinguishable from a landed one. Same can_open_pr() gate
            # (kill switch + 8/day cap) the dashboard button inherits; at most
            # 3 rows per tick, 3 redrives per row, claimed before attempted.
            try:
                from routes.brain_innovation_dashboard import (
                    redrive_approved_without_pr as _redrive)
                _rd = _redrive() or {}
                report["steps"].append({"step": "tier2.approved_without_pr_redrive",
                                        "ok": bool(_rd.get("ok")),
                                        "redriven": _rd.get("redriven", 0),
                                        "detail": _rd})
            except Exception as _rd_e:
                report["steps"].append({"step": "tier2.approved_without_pr_redrive",
                                        "ok": False, "error": str(_rd_e)[:160]})

    # ── Tier 3 — HUMAN-GATED (propose only) ─────────────────────────
    if "3" in tiers:
        report["tiers_run"].append("3")
        # #49 lane 5: these four are DECLARED nodes (ORCHESTRATOR_NODES_T3) and
        # run through the dependency-aware runner instead of four hardcoded
        # serial lines. Each hits a different subsystem and reads none of the
        # others' output, so they are one dependency level — 4 x 90s of serial
        # budget that becomes ~90s the moment BRAIN_MASTER_PARALLEL=1.
        #   · l23_lifecycle        L23 capability proposals (seeds, never auto-built).
        #   · semantic_dup_findings near-duplicate OPEN findings via RAG embeddings
        #     (the theme-dups keyword dedup misses — the L6 PR-flood cause),
        #     surfaced for the janitor to MERGE. Dark-safe: [] without embeddings.
        #   · promote_on_failure   patterns the autopilot keeps failing on, surfaced
        #     for human re-channeling instead of bounce-looping. Dark no-op unless
        #     BRAIN_PROMOTE_ON_FAILURE_ENABLED.
        #   · forecast_findings    #7 L6 forecast→finding bridge (dark unless
        #     BRAIN_FORECAST_FINDINGS_ENABLED).
        report["steps"].extend(_run_nodes(ORCHESTRATOR_NODES_T3))
        # #8 strategic-outcome ledger: re-read baselined rec metrics 14/30d later +
        # stamp moved/flat/regressed. Additive ledger UPDATE only; prompt-feedback
        # it powers is gated by BRAIN_STRATEGIC_LEDGER_FEEDBACK_ENABLED in the planner.
        try:
            from routes.brain_strategic_ledger import stamp_strategic_outcomes
            _led = stamp_strategic_outcomes(max_rows=200) or {}
            report["steps"].append({"step": "tier3.strategic_ledger_stamp",
                                    "ok": bool(_led.get("ok", True)),
                                    "stamped": (_led.get("checked_14d") or 0) + (_led.get("checked_30d") or 0),
                                    "detail": _led})
        except Exception as _led_e:
            report["steps"].append({"step": "tier3.strategic_ledger_stamp", "ok": False,
                                    "error": str(_led_e)[:160]})
        # #6 reasoning lane: top leverage-ranked findings → typed candidates
        # (endpoint→digest / code→L22 draft-PR, auto-merge OFF). Dark + ZERO cost
        # unless BRAIN_REASONING_LANE_ENABLED; spends LLM budget when on → skip dry.
        if not dry:
            report["steps"].append(_summarize("tier3.reasoning_lane_drain",
                                               _call("POST", "/api/v1/brain/reasoning-lane/drain")))
        # Spec->code promotion: re-run the L5 drafter on open [brain-spec] PRs
        # with grounded file context -> concrete code draft PRs (human merges).
        # Dark unless BRAIN_SPEC_PROMOTE_ENABLED; spends LLM budget -> skip dry.
        if not dry:
            report["steps"].append(_summarize("tier3.spec_promote",
                                               _call("POST", "/api/v1/admin/brain/spec-promote/run?dry=0")))
        # Money/positioning findings the brain must NOT auto-act on.
        report["human_decisions"] = _human_gated_digest()

    # ── Growth north-star — read every tick so the brain is oriented on
    #    acquisition, not just reliability. A declining new-agents number is
    #    surfaced as a flagged decision (the levers that fix it —
    #    agent-pitch content, GEO answers, registry depth — are human/gated).
    report["acquisition"] = _acquisition_scoreboard()

    # ── Verify — confirm prior actions resolved their findings ──────
    if "verify" in tiers:
        report["tiers_run"].append("verify")
        report["steps"].append(_summarize("verify.autopilot",
                                           _call("POST", "/api/v1/brain/autopilot/verify-pending")))

    # Roll-up headline.
    _acq = report.get("acquisition") or {}
    report["headline"] = {
        "steps_ok": sum(1 for s in report["steps"] if s.get("ok")),
        "steps_total": len(report["steps"]),
        "human_decisions_pending": (report.get("human_decisions") or {}).get("count", 0),
        "new_agents_this_week": _acq.get("new_agents_this_week"),
        "acquisition_declining": _acq.get("declining"),
    }
    _persist(report)
    return report


@brain_master_orchestrator_bp.route("/api/v1/admin/brain/master-tick", methods=["POST"])
def master_tick():
    if not _admin_key():
        return jsonify({"error": "admin_endpoint_unconfigured",
                        "hint": "Set DCHUB_ADMIN_KEY on Railway."}), 503
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if _is_master_disabled():
        return jsonify({"ok": True, "skipped": True,
                        "reason": "BRAIN_MASTER_DISABLED env set"}), 200

    dry = str(request.args.get("dry", "")).lower() in ("1", "true", "yes")
    want = request.args.get("tiers", "1,2,3,verify")
    tiers = {t.strip() for t in want.split(",") if t.strip()}

    if not _tick_running.acquire(blocking=False):
        return jsonify({"ok": True, "started": False,
                        "note": "master-tick already running; report will land at "
                                "GET /api/v1/admin/brain/master-tick/last"}), 202

    def _bg():
        try:
            _run_master_tick(dry, tiers)
        except Exception as e:
            # The cycle itself never raises (every step is try-wrapped),
            # but never let a surprise kill the lock release.
            _persist({"ok": False, "dry_run": dry, "tiers_run": sorted(tiers),
                      "error": f"{type(e).__name__}: {str(e)[:200]}",
                      "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        finally:
            try:
                _tick_running.release()
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True, name="brain-master-tick").start()
    return jsonify({"ok": True, "started": True, "dry_run": dry,
                    "tiers": sorted(tiers),
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "note": "cycle running in background; report → "
                            "GET /api/v1/admin/brain/master-tick/last"}), 202


@brain_master_orchestrator_bp.route(
    "/api/v1/admin/brain/master-tick/resume", methods=["POST"])
def master_tick_resume():
    """Re-run named nodes alone (#49 lane 5).

    POST /api/v1/admin/brain/master-tick/resume?steps=tier2.l22_draft_prs

    Synchronous ON PURPOSE, unlike the full tick: a resume is one or two nodes
    an operator is watching, not a 10-step cycle, so the 202-and-poll dance
    that exists to keep master-tick off a web thread would just make it harder
    to use. If someone resumes the whole graph they get the same thread cost
    as before, which is why the full tick keeps its own async path."""
    # Same gate and same order as master_tick() — an unconfigured admin key is
    # 503 (nothing to check against), a wrong one is 401. Diverging here would
    # give a resume weaker auth than the tick it re-runs pieces of.
    if not _admin_key():
        return jsonify({"error": "admin_endpoint_unconfigured",
                        "hint": "Set DCHUB_ADMIN_KEY on Railway."}), 503
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if _is_master_disabled():
        return jsonify({"ok": True, "skipped": True,
                        "reason": "BRAIN_MASTER_DISABLED env set"}), 200
    raw = (request.args.get("steps") or "").strip()
    steps = [s.strip() for s in raw.split(",") if s.strip()]
    if not steps:
        return jsonify(ok=False, error="no_steps",
                       hint="?steps=tier3.l23_lifecycle,tier2.l22_draft_prs",
                       declared=[n["step"] for n in
                                 (ORCHESTRATOR_NODES_T1 + ORCHESTRATOR_NODES_T2
                                  + ORCHESTRATOR_NODES_T3)]), 400
    out = resume_nodes(steps)
    return jsonify(ok=True, requested=steps, steps=out)


@brain_master_orchestrator_bp.route("/api/v1/admin/brain/master-tick/last", methods=["GET"])
def master_tick_last():
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import psycopg2
        import psycopg2.extras
        du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
        with psycopg2.connect(du, connect_timeout=8) as conn:
            _ensure_table(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, ran_at, dry_run, tiers, report "
                            "FROM brain_master_ticks ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
        if not row:
            return jsonify({"ok": True, "last": None, "note": "no master-tick has run yet"}), 200
        return jsonify({"ok": True, "last": row}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}), 200
