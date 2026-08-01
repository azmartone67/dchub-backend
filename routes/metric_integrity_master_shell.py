"""Metric & Automation Integrity Master Shell (#44) — 2026-07-30.

Three things the 2026-07-29/30 dashboard sweep surfaced, each of which had been
true for days or weeks while every board looked fine:

  1 AUTOMATION LANDS — brain auto-merge had been silent for **823 hours (34
    days)** against a 48h expectation. The cadence sentinel had been flagging it
    for 11.5 days. Cause is known: `main` is protected and
    `github-actions[bot]` cannot push to it (GH006), so auto-merge stopped
    LANDING anything while still reporting green runs. A green CI run is not a
    merged commit.

  2 CONVERSION PARITY — four surfaces count paid conversions and they disagreed.
    `/api/v1/mcp/funnel` published **10** while the honest number was **6**
    (10 raw → 8 after refunds → 6 after comp/seed). canonical_funnel,
    funnel_health and /health were filtered in #1885/#1888; the funnel endpoint
    was missed. Adding a filter to three of four lock-stepped surfaces does not
    fix drift, it MOVES it.

  3 AGENT-COUNT PARITY — originally: the Upgrade Funnel reported 118 distinct
    external agents/7d and Source Reach 79 for the same window (different
    identity bases + allowlists + windows). ★RESOLVED 2026-07-31 (#2038/#2036):
    ONE canonical query now lives in mcp_calls_deloop.canonical_external_
    activity_sql() and the surfaces publish it (funnel real_external_agents_7d,
    reach real_agents_7d, the /ai widget shares the builder). This lane is now
    WIRED to that field: it runs the canonical query and compares what the live
    surfaces actually publish, so a fork/revert goes red here — it no longer
    hard-fails with a narrative.

★READ-ONLY. This shell does not fix anything; it makes each of the three
FAIL-VISIBLE on every tick so none of them can sit unnoticed for a month again.

GET /admin/metric-integrity · /api/v1/admin/metric-integrity/master-tick
Kill: METRIC_INTEGRITY_SHELL_DISABLE=1
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
metric_integrity_master_shell_bp = Blueprint("metric_integrity_master_shell",
                                             __name__)

# Auto-merge is expected to land something at least this often.
_AUTOMERGE_MAX_HOURS = float(os.environ.get("METRIC_INTEGRITY_AUTOMERGE_H", "48"))
# Two surfaces may differ by at most this fraction before it is drift, not noise.
_PARITY_TOLERANCE = float(os.environ.get("METRIC_INTEGRITY_PARITY_TOL", "0.10"))


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("METRIC_INTEGRITY_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[metric-integrity] db connect failed: %s", str(e)[:120])
        return None


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:420], "critical": critical}


def _lane_verdict(checks):
    decided = [c for c in checks if c["pass"] is not None]
    return all(c["pass"] for c in decided) if decided else None


def _q(cur, sql, args=None):
    try:
        cur.execute(sql, args or ())
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("[metric-integrity] query failed: %s", str(e)[:150])
        return None


def _one(cur, sql, args=None):
    r = _q(cur, sql, args)
    return (r[0][0] if r and r[0] else None)


# ── lane 1 · does our automation actually LAND code? ──────────────────
def _lane_automation(cur) -> list:
    """★★CORRECTED 2026-07-30 — this lane was RAISING A FALSE ALARM.

    It measured `MAX(merged_at/updated_at)` on brain_automerge_log and reported
    "auto-merge DEAD 34.9 days". That was WRONG. The table only ever recorded
    MERGES, so a pipeline running correctly every 30 minutes with NOTHING
    ELIGIBLE was indistinguishable from a dead one.

    Verified live: `/api/v1/brain/automerge/run` returns
    `enabled:true · breaker clear · health green · rate_cap 3 · merged:[] ·
    skipped:[] · would_merge:[]` — healthy and correctly idle. The GitHub
    workflow `brain-autonomy.yml` fires every 30 min and succeeds. There are
    ZERO open PRs of any kind, so there is nothing to merge.

    ★IDLE IS NOT DEAD. The alarm cost a full session and produced a confident
    WRITTEN diagnosis that was false ("GH006 blocks bot pushes → convert to
    PR-merge with PR_SUBMIT_TOKEN") — this module has ALWAYS merged via
    `PUT /pulls/{n}/merge` with a PAT and never pushes. A metric that can only
    fail in one direction gets believed; this one did.

    Now reads the `kind='run'` heartbeat and separates three states.
    """
    out = []
    hb = _one(cur, """SELECT ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(updated_at)))
                             / 3600.0, 1)
                        FROM brain_automerge_log WHERE kind = 'run'""")
    if hb is None:
        out.append(_check(
            "automerge_running", "the auto-merge runner is EXECUTING", None,
            "no kind='run' heartbeat yet — ships with the heartbeat change; "
            "until the next pass this is UNMEASURED, not dead. (Merge-freshness "
            "is NOT a substitute: it conflates 'nothing eligible' with 'broken'.)",
            critical=True))
    else:
        h = float(hb)
        out.append(_check(
            "automerge_running", "the auto-merge runner is EXECUTING",
            h <= _AUTOMERGE_MAX_HOURS,
            f"last pass {h:,.1f}h ago (limit {_AUTOMERGE_MAX_HOURS:.0f}h)"
            + ("" if h <= _AUTOMERGE_MAX_HOURS else
               " — the RUNNER is not firing. Check brain-autonomy.yml (*/30) and "
               "BRAIN_AUTOMERGE_ENABLED. This is the genuinely-dead state."),
            critical=True))
    # Is the pipeline being handed anything to land? Zero eligible is the state
    # we are actually in, and it is an UPSTREAM question, not a merger fault.
    blocked = _one(cur, """SELECT COUNT(*) FROM brain_automerge_log
                            WHERE kind='run' AND status='blocked'
                              AND updated_at >= NOW() - INTERVAL '7 days'""")
    if blocked is not None:
        out.append(_check(
            "automerge_not_blocked", "eligible work is not piling up unmerged",
            int(blocked) == 0,
            f"{int(blocked)} pass(es) in 7d had eligible PRs but merged none"
            if int(blocked) else
            "no pass found eligible work it failed to merge"))
    # The REAL ceiling on autonomy: the mechanical allowlist.
    out.append(_check(
        "autofix_supply", "the brain has work it is ALLOWED to land", False,
        "UPSTREAM GAP (this is the real autonomy ceiling): the autonomy tick "
        "scans 1,170 files and finds ~43 candidates, but every draft-PR "
        "candidate is rejected `not_mechanical` — 'no allowlist transform class "
        "matched', 'adds control-flow keyword(s)', 'changed lines > "
        "MECH_MAX_LINES=8'. The three shipped classes (interval_literal, "
        "bool_is_active, now_text_cast) are EXHAUSTED — last autofix PR #1666 on "
        "2026-07-19. Auto-merge is idle because nothing is permitted to reach "
        "it. Widening the allowlist with new SAFE transform classes is what "
        "raises autonomy; it is a safety decision, not a bug fix.",
        critical=True))
    return out


# ── lane 2 · every conversion surface reports the SAME number ─────────
def _lane_conversion_parity(cur) -> list:
    raw = _one(cur, """SELECT COUNT(*) FROM mcp_conversions
                        WHERE created_at >= NOW() - INTERVAL '30 days'""")
    norefund = _one(cur, """SELECT COUNT(*) FROM mcp_conversions
                             WHERE created_at >= NOW() - INTERVAL '30 days'
                               AND refunded_at IS NULL""")
    honest = _one(cur, """SELECT COUNT(*) FROM mcp_conversions
                           WHERE created_at >= NOW() - INTERVAL '30 days'
                             AND refunded_at IS NULL
                             AND stripe_customer_id IS NOT NULL
                             AND LOWER(COALESCE(plan_to,'')) NOT IN
                                 ('comp','complimentary','research_seed_nlr','seed')
                             AND LOWER(COALESCE(source,'')) <> 'seed'""")
    out = [_check(
        "conv_refunds_excluded", "refunded sales are excluded everywhere",
        (raw is not None and norefund is not None and raw == norefund)
        if (raw is not None and norefund is not None) else None,
        f"raw {raw} · minus refunded {norefund} · honest {honest}"
        + ("" if raw == norefund else
           f" — {int(raw or 0) - int(norefund or 0)} refunded sale(s) still "
           f"inflate any surface that omits `refunded_at IS NULL`. Four "
           f"surfaces count this: canonical_funnel, funnel_health, /health and "
           f"/api/v1/mcp/funnel. All four must carry the filter."),
        critical=True)]
    out.append(_check(
        "conv_honest_vs_raw", "the honest count is what gets published",
        None,
        f"honest={honest} vs raw={raw}. Publish the honest figure; raw includes "
        f"refunds, comp keys and seed rows. A ~{(int(raw or 0) - int(honest or 0))}"
        f"-sale gap is the difference between a real KPI and a flattering one."))
    return out


# ── lane 3 · the agent-count surfaces must agree with the canonical ──

# Public edge base for the live-surface reads. Overridable so a failover or
# staging run can point the lane at the surfaces it should actually judge.
_EDGE_BASE = (os.environ.get("METRIC_INTEGRITY_EDGE_BASE")
              or "https://dchub.cloud").rstrip("/")


def _fetch_json(path, timeout=15):
    """GET a public surface through the edge. UA required — bare urllib gets
    CF-403'd. Cache-bust so a CF/edge entry can't satisfy the probe. Returns a
    dict, or None — callers must treat None as UNMEASURED, never as drift."""
    try:
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(
            f"{_EDGE_BASE}{path}{sep}cb=mi44",
            headers={"User-Agent": "dchub-metric-integrity-shell/1.0",
                     "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        logger.debug("[metric-integrity] fetch %s failed: %s",
                     path, str(e)[:150])
        return None


def _surface_vs_canonical(cid, label, payload, field, canonical, extra=""):
    """One published surface vs the canonical DB value, within tolerance.

    Three honest outcomes, in the #1858 spirit (UNMEASURED ≠ 0 ≠ drift):
      · payload is None            → passed=None (edge fetch failed — say so)
      · field ABSENT from payload  → FAIL, critical (a revert of the canonical
                                     wiring: the surface stopped publishing it)
      · field present but null     → passed=None (the surface itself could not
                                     measure — its *_error sibling says why)
      · value present              → |v−canon|/max(canon,1) ≤ tolerance
    """
    if payload is None:
        return _check(cid, label, None,
                      "UNMEASURED — edge fetch failed (timeout/403/5xx). Not "
                      "drift; retry next tick." + extra)
    if field not in payload:
        return _check(cid, label, False,
                      f"`{field}` is GONE from the payload — the canonical "
                      f"wiring (#2038) was reverted or renamed. Every reader "
                      f"of this surface is back on a non-canonical count."
                      + extra, critical=True)
    val = payload.get(field)
    if val is None:
        err = payload.get(f"{field}_error") or payload.get(
            "real_external_agents_7d_error") or "no error detail"
        return _check(cid, label, None,
                      f"`{field}` is null — the surface could not compute it "
                      f"({str(err)[:100]}). UNMEASURED at source, not drift."
                      + extra)
    v = int(val)
    rel = abs(v - canonical) / float(max(canonical, 1))
    return _check(
        cid, label, rel <= _PARITY_TOLERANCE,
        f"surface {v} vs canonical {canonical} "
        f"(Δ {rel * 100:.1f}%, tolerance {_PARITY_TOLERANCE * 100:.0f}%)"
        + extra,
        critical=rel > _PARITY_TOLERANCE)


def _lane_agent_parity(cur) -> list:
    """★WIRED TO THE CANONICAL FIELD (2026-07-31, backend #2038 + #2036).

    Lineage, kept on purpose: the first cut invented two proxy queries that
    returned 261 == 261 — a 0.0% gap and a false PASS that reproduced NEITHER
    published number (118 vs 79). It was then DECLARED UNMEASURABLE and
    hard-failed with a narrative naming the fix. That fix now exists:
    mcp_calls_deloop.canonical_external_activity_sql() is THE definition, and
    the surfaces publish it (funnel `real_external_agents_7d`, reach
    `real_agents_7d`; the /ai widget imports the same builder — pinned
    statically by tests/test_canonical_counts_drift.py).

    What this lane measures now, live, per tick:
      1. the canonical query still exists and runs (critical — its loss
         unanchors every surface),
      2. each LIVE published surface carries the canonical field and its value
         sits within _PARITY_TOLERANCE of the canonical DB count at tick time
         (covers deploy skew, stale replicas, and in-process caches: the /ai
         widget source caches 300s, reach caches up to 30 min — the tolerance
         exists precisely so cache lag reads as noise, a fork reads as drift).
    ★NEVER replace the imported canonical query with a local proxy — that is
    the exact false-pass this lane's history warns about.
    """
    checks = []
    try:
        from mcp_calls_deloop import canonical_external_activity_sql
        rows = _q(cur, canonical_external_activity_sql(7))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "agent_parity_single_definition",
            "ONE agent-count definition, imported by every surface", False,
            "mcp_calls_deloop.canonical_external_activity_sql is missing or "
            f"failing ({str(e)[:100]}) — the single definition every surface "
            "imports is gone. This is the revert this lane exists to catch.",
            critical=True))
        return checks
    if not rows:
        # _q returns None when the QUERY failed — that is UNMEASURED, not
        # "0 agents". Rendering a swallowed DB error as a passing zero is the
        # #1858 false-zero class; a genuine dead week still returns a (0, 0)
        # ROW and is handled below.
        checks.append(_check(
            "agent_parity_single_definition",
            "ONE agent-count definition, imported by every surface", None,
            "canonical query failed against the DB this tick — UNMEASURED "
            "(not zero, not drift). Surface comparisons skipped; retry next "
            "tick.", critical=True))
        return checks
    agents, calls = int(rows[0][0] or 0), int(rows[0][1] or 0)

    total7 = _one(cur, """SELECT COUNT(DISTINCT ip_address) FROM mcp_tool_calls
                           WHERE created_at >= NOW() - INTERVAL '7 days'""")
    checks.append(_check(
        "agent_parity_single_definition",
        "ONE agent-count definition, imported by every surface", True,
        f"canonical (mcp_calls_identity view, is_public_ip AND "
        f"is_real_external, rolling 7d): {agents} agents · {calls} calls "
        f"(context: {int(total7 or 0)} distinct raw IPs before exclusion).",
        critical=True))

    checks.append(_surface_vs_canonical(
        "agent_parity_funnel_canonical",
        "Upgrade Funnel publishes the canonical agent count",
        _fetch_json("/api/v1/mcp/funnel"),
        "real_external_agents_7d", agents))
    checks.append(_surface_vs_canonical(
        "agent_parity_reach_canonical",
        "Source Reach publishes the canonical agent count",
        _fetch_json("/api/v1/ai/reach"),
        "real_agents_7d", agents,
        extra=" (reach serves a ≤30-min in-process cache — lag inside the "
              "tolerance is expected; its distinct_agents_7d field is the "
              "ISO-week rollup and is deliberately NOT judged here)"))
    return checks


def _run_tick() -> dict:
    out = {"shell": "metric-integrity", "n": 44, "lanes": [],
           "note": ("Read-only. Each lane is a defect that stayed invisible for "
                    "days-to-weeks while dashboards looked fine. Fixing the "
                    "underlying issue is what turns a lane green — not editing "
                    "this shell.")}
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            for label, checks in (
                ("1 · automation LANDS code (not just goes green)",
                 _lane_automation(cur)),
                ("2 · conversion-count parity across all four surfaces",
                 _lane_conversion_parity(cur)),
                ("3 · agent-count parity (the number that compounds)",
                 _lane_agent_parity(cur)),
            ):
                out["lanes"].append({"lane": label, "checks": checks,
                                     "pass": _lane_verdict(checks)})
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass
    decided = [ln["pass"] for ln in out["lanes"] if ln["pass"] is not None]
    out["lanes_pass"] = sum(1 for p in decided if p)
    out["lanes_total"] = len(out["lanes"])
    out["ok"] = True
    return out


@metric_integrity_master_shell_bp.route(
    "/api/v1/admin/metric-integrity/master-tick", methods=["GET"])
def metric_integrity_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_run_tick())


@metric_integrity_master_shell_bp.route("/admin/metric-integrity",
                                        methods=["GET"])
def metric_integrity_dashboard():
    if _disabled():
        return Response("metric-integrity shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _run_tick()

    def chip(v):
        if v is True:
            return '<span style="color:#22c55e">PASS</span>'
        if v is False:
            return '<span style="color:#ef4444">FAIL</span>'
        return '<span style="color:#eab308">n/a</span>'

    rows = []
    for ln in p.get("lanes", []):
        rows.append(f'<h3>{ln["lane"]} — {chip(ln["pass"])}</h3><ul>')
        for ch in ln["checks"]:
            star = " ★" if ch.get("critical") else ""
            rows.append(f'<li>{chip(ch["pass"])}{star} <b>{ch["name"]}</b><br>'
                        f'<small>{ch["detail"]}</small></li>')
        rows.append("</ul>")
    return Response(
        "<html><body style='font-family:system-ui;background:#0b0b12;"
        "color:#e6e6f0;padding:24px;max-width:900px'>"
        "<h1>Metric &amp; Automation Integrity — Shell #44</h1>"
        f"<p><small>{p.get('note','')}</small></p>"
        f"<p>lanes passing {p.get('lanes_pass','?')}/{p.get('lanes_total','?')}</p>"
        + "".join(rows) + "</body></html>", mimetype="text/html")
