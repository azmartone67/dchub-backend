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

  3 AGENT-COUNT PARITY — the Upgrade Funnel reports 118 distinct external agents
    /7d and Source Reach reports 79 for the same window. Two dashboards, one
    top-of-funnel metric, two answers. Probably different allowlists — but
    "probably" is not good enough for the number that compounds.

★READ-ONLY. This shell does not fix anything; it makes each of the three
FAIL-VISIBLE on every tick so none of them can sit unnoticed for a month again.

GET /admin/metric-integrity · /api/v1/admin/metric-integrity/master-tick
Kill: METRIC_INTEGRITY_SHELL_DISABLE=1
"""
from __future__ import annotations

import os
import logging

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


# ── lane 3 · the two agent-count surfaces must agree ─────────────────
def _lane_agent_parity(cur) -> list:
    """★★DECLARED UNMEASURABLE — and that is the honest answer, not a cop-out.

    My first cut invented two proxy queries (broad vs strict platform exclusion)
    and they returned 261 == 261: a 0.0% gap and a PASS. But the published
    numbers are 118 (Upgrade Funnel) and 79 (Source Reach) — the proxies
    reproduced NEITHER. A check that passes without measuring the thing it claims
    to measure is worse than a failing one, because it retires the question.

    The two definitions live in different modules with different probe
    allowlists. This shell cannot reproduce either without importing both, and
    guessing at them is what produced the false pass. So: state the gap as
    OBSERVED FROM THE DASHBOARDS, and name the fix.
    """
    total7 = _one(cur, """SELECT COUNT(DISTINCT ip_address) FROM mcp_tool_calls
                           WHERE created_at >= NOW() - INTERVAL '7 days'""")
    return [_check(
        "agent_parity_single_definition",
        "ONE agent-count definition, imported by every surface", False,
        "OBSERVED 2026-07-29: Upgrade Funnel published 118 distinct external "
        "agents/7d while Source Reach published 79 — same window, same "
        f"underlying table ({int(total7 or 0)} distinct IPs total/7d before any "
        "exclusion), two different probe allowlists in two different modules. "
        "The number that compounds cannot have two values. FIX: extract one "
        "canonical `real_external_agents_7d` (as canonical_funnel already does "
        "for conversions) and have both surfaces import it. Until then this "
        "shell reports the DISAGREEMENT and does not pretend to arbitrate it.",
        critical=True)]


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
