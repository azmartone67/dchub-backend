"""routes/growth_integrity_master_shell.py — shell #52, GROWTH INTEGRITY.

WHY THIS SHELL EXISTS (2026-08-17 growth review, five merged fixes):

Every defect that review found was a surface built to make a failure visible
that was itself lying — and in most cases lying in the OPPOSITE direction from
the bug it was created to catch:

  · /api/v1/media/pulse renamed every `degraded` feed `silent` (#2833). The
    endpoint had been rewritten on 08-15 because it read HEALTHY through a
    three-day outage; the rewrite then manufactured an outage that wasn't there.
  · the abandoned-claims counter never checked `posted_at IS NULL` — the exact
    condition its own comment defines a stranded row by (#2836).
  · `new_external_ips` exceeded `distinct_external_ips` in every week (#2834).
  · raw source ROWS were published as a facility count, on LinkedIn (#2835).
  · the retention affordance was scoped "keyed/paid only" and therefore withheld
    from the ~95% of traffic that is anonymous and does not return (mcp #200).

★★★ THE CLASS THIS SHELL IS BUILT FOR: three of those five were REACHABILITY
bugs — correct code sitting on a path real traffic never takes — and the
telemetry reported all three as healthy, because `mcp_tool_calls` / `mcp_call_log`
are structurally blind to a block that was never attached. r-prewall-anon
(07-28), r-undercap-anon (08-15) and r-return-anon (08-17) are the same bug three
times, found at 8, 18 and 40 days. **Only an OUT-IN probe can see this class**
(lane 5), which is why this shell probes the public MCP endpoint as an anonymous
caller rather than reading our own tables.

HONEST-STATE LADDER (inherited from shells #30-34, and deliberately strict):
a lane that verified NOTHING renders `?`, never PASS. `pass=None` means
UNMEASURED — never a passing zero (#1858). A check whose FIELD IS ABSENT from a
payload is a FAIL, not an unknown: absence means the emitter changed shape.

Read-only. Every network read is fail-soft. Nothing here writes.

  GET /api/v1/admin/growth-integrity-shell/master-tick     (admin-gated, JSON)
  GET /admin/growth-integrity-shell                        (admin-gated, HTML)
"""
from __future__ import annotations

import json
import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
growth_integrity_master_shell_bp = Blueprint("growth_integrity_master_shell", __name__)

_UNKNOWN = "unmeasured — the read failed; this is not a zero"
_PUBLIC = "https://dchub.cloud"
_TIMEOUT = 20

# ── admin gate (mirrors agent_pay_master_shell) ──────────────────────────────
def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("GROWTH_INTEGRITY_SHELL_DISABLE") or "").strip() == "1"


def _probe_enabled() -> bool:
    """Lane 5 makes an outbound MCP call. Killable without a deploy."""
    return (os.environ.get("GROWTH_INTEGRITY_SHELL_PROBE") or "1").strip() != "0"


# ── helpers ─────────────────────────────────────────────────────────────────
def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": detail,
            "critical": critical}


def _lane_verdict(checks: list) -> str:
    """FAIL on any false; `?` when nothing was actually verified.

    A lane whose reads all failed must never render green — this shell exists
    because surfaces flattered us, so "I could not measure it" and "it is fine"
    must be visibly different states.
    """
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is None for k in crits):
        return "?"
    if any(k["pass"] is None for k in checks) and not any(k["pass"] is True for k in checks):
        return "?"
    return "PASS"


def _get_json(path: str, base: str = _PUBLIC):
    """GET our own JSON, fail-soft. Returns None on ANY failure.

    ★ `requests`, never `urllib.request` — regression_lint has a hard rule
    (`urllib-request-on-railway`) that blocks urllib in changed lines, and the
    CF edge 1010s a bare urllib User-Agent before it ever reaches the origin.
    ★ Cache-busted: /api/v1/* is CF-cached (Cache Rule #3), and a stale payload
    would make this shell certify a state that no longer exists.
    """
    try:
        import requests
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{base}{path}{sep}_cb=shell52",
                         headers={"User-Agent": "dchub-growth-integrity-shell/1.0"},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.warning("[shell52] read failed %s: %s", path, str(e)[:120])
        return None


# ── LANE 1 — reach rollup invariants (#2834) ────────────────────────────────
def _lane_reach() -> list:
    d = _get_json("/api/v1/ai/reach/trend?weeks=20")
    if not d:
        return [_check("r_read", "reach trend readable", None, _UNKNOWN, critical=True)]
    weeks = d.get("weeks")
    if not isinstance(weeks, list):
        return [_check("r_shape", "trend carries weeks[]", False,
                       "weeks[] ABSENT — the emitter changed shape", critical=True)]
    out = []
    # THE INVARIANT (#2834): IPs never seen before cannot exceed IPs seen. This
    # held false in EVERY week until 08-18. Weeks outside BACKFILL_WEEKS keep
    # their legacy value permanently and are excluded by `computed_at` recency,
    # not by index — the window slides.
    fresh = [w for w in weeks if (w.get("computed_at") or "") >= "2026-08-18"]
    bad = [w for w in fresh
           if (w.get("new_external_ips") or 0) > (w.get("distinct_external_ips") or 0)]
    out.append(_check(
        "r_invariant", "new_external_ips <= distinct_external_ips (recomputed weeks)",
        None if not fresh else not bad,
        _UNKNOWN if not fresh else
        ("OK across %d recomputed weeks" % len(fresh) if not bad else
         "VIOLATED in %d week(s): %s" % (len(bad), ", ".join(
             "%s %s>%s" % (w.get("week_start"), w.get("new_external_ips"),
                           w.get("distinct_external_ips")) for w in bad[:4]))),
        critical=True))
    # The in-progress week must DECLARE itself, or a Monday read charts as a
    # 100% collapse — a prior session already mis-read exactly that as real.
    last = weeks[-1] if weeks else {}
    out.append(_check(
        "r_partial", "in-progress week declares partial + coverage_hours",
        ("partial" in last) and (not last.get("partial") or "coverage_hours" in last),
        "partial=%s coverage_hours=%s" % (last.get("partial"), last.get("coverage_hours")),
        critical=True))
    out.append(_check(
        "r_latest_complete", "latest_complete exposed for quoting",
        d.get("latest_complete") is not None,
        "latest_complete=%s" % ((d.get("latest_complete") or {}).get("week_start")),
        critical=False))
    return out


# ── LANE 2 — retention measured on the honest grain ─────────────────────────
def _lane_retention() -> list:
    d = _get_json("/api/v1/mcp/retention")
    if not d:
        return [_check("t_read", "retention readable", None, _UNKNOWN, critical=True)]
    cohort = d.get("agent_cohort")
    if not isinstance(cohort, list) or not cohort:
        return [_check("t_shape", "agent_cohort present", False,
                       "agent_cohort ABSENT — the canonical grain is gone",
                       critical=True)]
    out = []
    complete = cohort[:-1] if len(cohort) > 1 else cohort
    ret = [int(w.get("returning_agents") or 0) for w in complete][-6:]
    new = [int(w.get("new_agents") or 0) for w in complete][-6:]
    # NOT a threshold on a good number — a pin on the SHAPE of the leak, so the
    # shell says something true whether or not retention improves.
    out.append(_check(
        "t_returning", "returning agents (canonical agent_id grain)",
        True, "last 6 complete weeks returning=%s vs new=%s" % (ret, new)))
    # ★ THE MEASUREMENT TRAP: the ip_cohort counts raw ip_address including CF
    # POPs and crawlers — the API's own note calls it an inflated denominator.
    # On 2026-08-17 the published tile said "90 returning" while the honest
    # agent-grain figure for the same week was 8. Any surface quoting retention
    # must use agent_cohort; this check makes the divergence visible so nobody
    # re-adopts the flattering number.
    ipc = d.get("ip_cohort")
    if isinstance(ipc, list) and ipc and complete:
        a = int((complete[-1] or {}).get("returning_agents") or 0)
        b = int((ipc[-1] or {}).get("returning_agents") or 0) if isinstance(ipc[-1], dict) else 0
        out.append(_check(
            "t_grain_gap", "agent grain vs ip cohort divergence is visible",
            True, "agent_cohort=%d vs ip_cohort=%d for the latest week — quote "
                  "the FORMER; ip_cohort includes CF POPs/crawlers" % (a, b)))
    else:
        out.append(_check("t_grain_gap", "ip_cohort comparable", None, _UNKNOWN))
    return out


# ── LANE 3 — registry presence ──────────────────────────────────────────────
def _lane_registries() -> list:
    d = _get_json("/api/v1/brain/mcp-registries")
    if not d:
        return [_check("g_read", "registry sweep readable", None, _UNKNOWN, critical=True)]
    results = d.get("results") or {}
    missing = [k for k, v in results.items()
               if v.get("actionable") and v.get("verdict") not in ("present",)]
    return [
        _check("g_present", "every ACTIONABLE registry lists DC Hub",
               None if not results else not missing,
               _UNKNOWN if not results else
               ("all %d actionable registries present" % d.get("present", 0) if not missing
                else "MISSING: " + ", ".join(
                    "%s(%s)" % (k, results[k].get("verdict")) for k in missing[:5])),
               critical=False),
        _check("g_unreadable", "registries we cannot verify are named, not assumed",
               True, "%d not_actionable (unreadable ≠ absent — never report these "
                     "as drift)" % (d.get("not_actionable") or 0)),
    ]


# ── LANE 4 — conversion, measured on the right statuses ─────────────────────
def _lane_conversion() -> list:
    d = _get_json("/api/v1/mcp/funnel")
    if not d:
        return [_check("c_read", "funnel readable", None, _UNKNOWN, critical=True)]
    out = []
    sig = d.get("real_external_signals_7d")
    conv = d.get("conversions_30d")
    out.append(_check(
        "c_rate", "signal -> conversion rate is measurable",
        None if sig is None or conv is None else True,
        _UNKNOWN if sig is None or conv is None else
        "%s upgrade signals/7d -> %s conversions/30d" % (sig, conv)))
    # ★ METRIC CONTRACT (r-prewall, 07-28): the success signal for the pay
    # surface is a SETTLE (mpp_paid / mpp_verify_failed). `mpp_challenge` means
    # an agent ASKED to pay and is the real pay-intent counter — folding passive
    # offers into it turns pay-intent into "every call near the cap". This check
    # asserts the funnel still exposes the split rather than a single blended
    # number, because collapsing them is how this metric got corrupted before.
    out.append(_check(
        "c_attribution", "conversions are attributed, not blended",
        d.get("conversions_attributed_30d") is not None,
        "attributed=%s unattributed=%s" % (d.get("conversions_attributed_30d"),
                                           d.get("conversions_unattributed_30d")),
        critical=False))
    return out


# ── LANE 5 — OUT-IN envelope contract (the reachability class) ──────────────
_ENVELOPE_MUST_CARRY = ("next_session",)


def _lane_envelope() -> list:
    """The only lane that can see the reachability class.

    r-prewall-anon (07-28), r-undercap-anon (08-15) and r-return-anon (08-17)
    were all the same bug: a block wired into the KEYED branch while ~95% of
    real traffic exits the anonymous auto-mint cascade. Each shipped "verified"
    — by a probe that held a minted key and therefore took the keyed path.

    So this probe holds NO key and opens a FRESH session per call. A session
    auto-binds after its first call and silently becomes the keyed path, which
    is exactly how the 07-27 verification fooled itself.
    """
    if not _probe_enabled():
        return [_check("e_probe", "anon envelope probe", None,
                       "probe disabled (GROWTH_INTEGRITY_SHELL_PROBE=0)",
                       critical=True)]
    try:
        import requests
    except Exception:
        return [_check("e_probe", "anon envelope probe", None, _UNKNOWN, critical=True)]
    url = os.environ.get("DCHUB_MCP_PUBLIC_BASE", _PUBLIC).rstrip("/") + "/mcp"
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    try:
        init = requests.post(url, headers=hdr, timeout=_TIMEOUT, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       # ★ *verify* collapses to platform `dchub-internal` and is
                       # excluded from real-demand metrics — this probe must not
                       # pollute the very funnel lane 4 reads.
                       "clientInfo": {"name": "dchub-shell52-verify", "version": "1.0"}}})
        sid = init.headers.get("mcp-session-id")
        if not sid:
            return [_check("e_handshake", "anon MCP handshake", False,
                           "no mcp-session-id on initialize", critical=True)]
        hdr2 = dict(hdr, **{"Mcp-Session-Id": sid})
        requests.post(url, headers=hdr2, timeout=_TIMEOUT,
                      json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = requests.post(url, headers=hdr2, timeout=_TIMEOUT, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_water_risk", "arguments": {"market": "phoenix"}}})
        m = re.search(r"^data: (\{.*)$", call.text, re.M)
        if not m:
            return [_check("e_call", "anon tools/call returned a result", False,
                           "no data frame in response", critical=True)]
        res = (json.loads(m.group(1)) or {}).get("result") or {}
        sc = res.get("structuredContent") or {}
    except Exception as e:
        return [_check("e_probe", "anon envelope probe", None,
                       "%s: %s" % (type(e).__name__, str(e)[:80]), critical=True)]

    out = [_check("e_anon_path", "probe landed on the ANONYMOUS cascade",
                  bool(sc.get("trial_taste") or sc.get("preview_is_partial")),
                  "trial_taste=%s preview_is_partial=%s — if BOTH are falsy the "
                  "probe took the keyed path and proves nothing about real traffic"
                  % (sc.get("trial_taste"), sc.get("preview_is_partial")),
                  critical=True)]
    for key in _ENVELOPE_MUST_CARRY:
        buried = key in (sc.get("upgrade") or {})
        out.append(_check(
            "e_%s" % key, "anon response carries `%s` at TOP LEVEL" % key,
            bool(sc.get(key)) and not buried,
            ("present at top level" if sc.get(key) else
             "ABSENT — the block is unreachable from the anon cascade; "
             "mcp_tool_calls cannot see this and will report success")
            + (" (BURIED under `upgrade`)" if buried else ""),
            critical=True))
    return out


# ── LANE 6 — brain throughput ───────────────────────────────────────────────
def _lane_brain() -> list:
    d = _get_json("/api/v1/brain/status")
    if not d:
        return [_check("b_read", "brain status readable", None, _UNKNOWN, critical=True)]
    verdict = d.get("verdict")
    out = [_check("b_active", "brain is running",
                  bool(d.get("active")) and verdict not in ("stalled", "dormant"),
                  "verdict=%s health=%s last_run=%smin ago"
                  % (verdict, d.get("health"), d.get("minutes_since_last_run")),
                  critical=True)]
    # ★ CORRECTED 2026-08-18 — this check cried wolf on a HEALTHY brain.
    #
    # Shipped 08-17 asserting `log <= max(run*4, 120)`, which FAILED on live
    # run=2min / log=280min and reported "the brain is running but not
    # recording". That divergence is the DESIGNED signature of a quiet brain,
    # not a defect: brain_v2_layer4 stamps `last_run_at` on EVERY
    # trigger_learn() pass including no-ops (Phase RR), while `last_log_at`
    # moves only on an actual learn ATTEMPT. Measured live 08-18:
    # learning_log_count=5172, last_log_at 00:23Z, verdict=healthy_backlog —
    # the log records fine, there was simply nothing text-fixable to record.
    # The old rule fired on any quiet period past 2h, i.e. most of the time.
    #
    # The real contradiction is much narrower. `healthy_working` is only
    # returned when pf_count>0 OR stale<180, so it ASSERTS recent activity;
    # a stale log with zero proposals under that verdict is the one state
    # that cannot be true, and it is exactly what a regression in
    # compute_brain_verdict would produce.
    log = d.get("stale_minutes_since_last_log")
    pf = d.get("proposed_fixes_count")
    if verdict is None or not isinstance(log, (int, float)):
        out.append(_check("b_log_gap",
                          "verdict is consistent with log recency", None, _UNKNOWN))
    else:
        contradiction = (verdict == "healthy_working" and log >= 180 and pf == 0)
        out.append(_check(
            "b_log_gap", "verdict is consistent with log recency",
            not contradiction,
            "verdict=%s last_log=%smin proposals=%s — %s" % (
                verdict, log, pf,
                "CONTRADICTION: `healthy_working` asserts pf>0 or a log newer "
                "than 180min, and neither holds" if contradiction else
                "consistent (run>>log divergence is normal for a quiet brain)"),
            critical=False))
    # ★ The old b_backlog returned `True` whenever the field was merely
    # present and `None` otherwise — it could not fail, so it verified
    # nothing. What it should catch is r36's actual bug: the verdict
    # announcing healthy_quiet ("the healer's findings are clean") while a
    # real backlog sat open. If there IS a backlog the verdict must say so.
    # ★2026-08-30 — this only banned `healthy_quiet`, so the live state it was
    # written to catch sailed through under a DIFFERENT healthy verdict:
    # actionable=39, proposed=0, verdict=`healthy_working`. The rule now asks
    # the question it always meant to ask — when a backlog is open and nothing
    # has been proposed against it, the verdict must NAME the backlog, not just
    # avoid one particular word for it.
    ac = d.get("actionable_findings_count")
    if ac is None:
        backlog_ok = None
    elif ac > 0 and (pf or 0) == 0:
        backlog_ok = verdict == "healthy_backlog"
    elif ac > 0:
        backlog_ok = verdict != "healthy_quiet"
    else:
        backlog_ok = True
    out.append(_check(
        "b_backlog", "an open backlog is admitted by the verdict", backlog_ok,
        "actionable=%s proposed_fixes=%s verdict=%s — %s" % (
            ac, pf, verdict,
            "verdict %r does not name the %s open finding(s) with 0 proposed "
            "against them — say healthy_backlog" % (verdict, ac)
            if backlog_ok is False else
            "backlog is named by the verdict"),
        critical=False))
    # ★ b_log_gap above correctly stopped alarming on run>>log divergence. But
    # that leaves the OPPOSITE failure uncovered: if the log writer itself dies,
    # `verdict` stays healthy_backlog and stale_minutes climbs forever without
    # any lane objecting. Nothing here would notice.
    #
    # It is checkable because the writer's cadence is known and regular. The
    # two timestamps come from DIFFERENT WORKFLOWS:
    #
    #   last_run_at ← evolve-cron.yml  `0 * * * *`   HOURLY, POSTs
    #                 /api/v1/brain/learn; trigger_learn() stamps it on its
    #                 first line, before the API-key check and before any work.
    #   last_log_at ← brain-layer5.yml `8 */6 * * *` EVERY 6H, POSTs
    #                 /api/v1/brain/learn-backend-issues (the r78 log writer).
    #
    # Measured over all 200 rows of /api/v1/brain/learning-log: six bursts of
    # 24-38 rows at 00:23, 06:21, 12:17, 18:17, 00:24, 18:16 — 6.0h apart, dead
    # regular. So stale_minutes is a SAWTOOTH ramping 0→~360 by design, which is
    # why thresholding it against the hourly heartbeat was wrong. Bound it to
    # TWO of the WRITER's own cycles: one skipped run tolerated, two not.
    _L5_CADENCE_MIN = 360          # brain-layer5.yml `8 */6 * * *`
    _LOG_SILENT_MIN = 2 * _L5_CADENCE_MIN
    if isinstance(log, (int, float)):
        out.append(_check(
            "b_log_writing", "the layer-5 learning log is still being written",
            log <= _LOG_SILENT_MIN,
            "last_log=%smin vs writer cadence %smin — %s" % (
                log, _L5_CADENCE_MIN,
                "within one cycle (0-%dmin sawtooth is normal)" % _L5_CADENCE_MIN
                if log <= _L5_CADENCE_MIN else
                ("one cycle missed, inside tolerance"
                 if log <= _LOG_SILENT_MIN else
                 "SILENT: two consecutive layer-5 passes wrote nothing — the "
                 "log WRITER has stopped, which no verdict field reports")),
            critical=False))
    else:
        out.append(_check("b_log_writing",
                          "the layer-5 learning log is still being written",
                          None, _UNKNOWN))
    return out


# ── LANE 7 — platform attribution ───────────────────────────────────────────
def _lane_attribution() -> list:
    d = _get_json("/api/v1/reach")
    if not d:
        return [_check("a_read", "reach readable", None, _UNKNOWN, critical=True)]
    plats = d.get("platforms_7d")
    if not isinstance(plats, list) or not plats:
        return [_check("a_shape", "platforms_7d present", None, _UNKNOWN, critical=True)]
    total = sum(int(p.get("calls") or 0) for p in plats) or 1
    top = max(plats, key=lambda p: int(p.get("calls") or 0))
    share = 100.0 * int(top.get("calls") or 0) / total
    # ★ Measured 08-17: `mcp-generic-client` = 42 agents / 1,912 calls = 87% of
    # real volume in ONE unattributed bucket. That is not a failure — it is a
    # ceiling on every other number: you cannot tell who those agents are, so
    # you cannot tell what would retain or convert them.
    return [
        _check("a_concentration", "real call volume is attributable",
               share < 60.0,
               "top platform `%s` = %.0f%% of real calls (%s of %s). Above 60%% "
               "the demand picture is one opaque bucket."
               % (top.get("platform"), share, top.get("calls"), total),
               critical=False),
        _check("a_named", "named client platforms are visible",
               True, "%d distinct platforms in 7d" % len(plats)),
        _ci_origin_check(),
    ]


# ★ r-ci-selftag (2026-08-18) — THE OPAQUE BUCKET WAS US.
#
# a_concentration had been FAILing on "87% of real calls in one unattributed
# bucket". That bucket was not third-party demand. Every IP in the canonical
# population, tested against api.github.com/meta `actions`: 1,700 of 2,114 real
# 7d calls (80.4%) and 49 of 68 real agents (72.1%) came from GitHub Actions
# runners — dchub-mcp-server's live smoke suite, which runs against
# https://dchub.cloud/mcp on every push. It self-identifies, but the tag is
# session-scoped and was lost whenever a tools/call was served by a process
# that never saw its initialize. Runner IPs rotate and agent_id is derived from
# the forwarded IP, so every CI run minted a brand-new "distinct agent".
#
# mcp #202 moves the self-tag onto a per-request header. THIS CHECK EXISTS
# BECAUSE THAT FIX TRUSTS A TAG: it fixes the two suites we know about and
# cannot stop the next untagged harness from reading as demand. IP origin does
# not depend on anything the caller chooses to tell us.
#
# ★ This lane reads the DB directly, unlike every other lane here, and that is
# deliberate: caller IPs have no public surface and must not get one. Only
# aggregates leave this function — never an address.
#
# ★ TIME BUDGET. master-tick measured 11.9s live against Cloudflare's 15s admin
# ROUTE_TIMEOUTS ceiling, so this check must stay small: the query is one
# GROUP BY (0.31s measured), the GitHub range list is fetched with a 3s bound
# and cached 6h, and the whole result is cached 15min so repeated ticks and the
# HTML view cost nothing. A miss renders UNMEASURED rather than stalling.
_CI_ORIGIN_MAX_SHARE = 0.20
_CI_ORIGIN_TTL_S = 900
_ci_origin_cache = {"at": 0.0, "check": None}


def _ci_origin_check() -> dict:
    import time
    now = time.time()
    if (_ci_origin_cache["check"] is not None
            and now - _ci_origin_cache["at"] < _CI_ORIGIN_TTL_S):
        return _ci_origin_cache["check"]
    c = _ci_origin_check_uncached()
    # Never cache an UNMEASURED result — that would hold a transient read
    # failure for 15 minutes and hide a real reading that is one tick away.
    if c.get("pass") is not None:
        _ci_origin_cache.update({"at": now, "check": c})
    return c


def _ci_origin_check_uncached() -> dict:
    try:
        from routes.agent_success_report import _conn, measure_ci_origin_share
    except Exception as e:
        return _check("a_ci_origin", "our own CI is not counted as demand",
                      None, "%s (import failed: %s)" % (_UNKNOWN, str(e)[:60]),
                      critical=False)
    c = None
    try:
        c = _conn()
        if c is None:
            return _check("a_ci_origin", "our own CI is not counted as demand",
                          None, _UNKNOWN, critical=False)
        with c.cursor() as cur:
            m = measure_ci_origin_share(cur)
    except Exception as e:
        return _check("a_ci_origin", "our own CI is not counted as demand",
                      None, "%s (%s)" % (_UNKNOWN, str(e)[:60]), critical=False)
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    if not m:
        # Unreadable range list or an empty window. NOT a passing zero (#1858).
        return _check("a_ci_origin", "our own CI is not counted as demand",
                      None, _UNKNOWN + " — GitHub range list unreadable, or no "
                      "real calls in the window; 'unreadable' is not '0% CI'",
                      critical=False)
    return _check(
        "a_ci_origin", "our own CI is not counted as demand",
        m["share"] < _CI_ORIGIN_MAX_SHARE,
        "GitHub-Actions-origin = %.1f%% of real calls (%s of %s) and %.0f%% of "
        "real agents (%s of %s). %s"
        % (100.0 * m["share"], m["ci_calls"], m["calls"],
           100.0 * (m["agent_share"] or 0), m["ci_agents"], m["agents"],
           "within tolerance" if m["share"] < _CI_ORIGIN_MAX_SHARE else
           "OUR OWN CI IS BEING PUBLISHED AS EXTERNAL DEMAND — a harness is "
           "reaching prod without a self-tag that survives to the call row"),
        critical=False)


_LANES = [
    ("reach", "Reach rollup invariants", _lane_reach),
    ("retention", "Retention on the honest grain", _lane_retention),
    ("registries", "Registry presence", _lane_registries),
    ("conversion", "Conversion + metric contract", _lane_conversion),
    ("envelope", "OUT-IN anon envelope contract", _lane_envelope),
    ("brain", "Brain throughput", _lane_brain),
    ("attribution", "Platform attribution", _lane_attribution),
]


def _run() -> dict:
    lanes = []
    for lid, name, fn in _LANES:
        try:
            checks = fn()
        except Exception as e:                      # a lane must never 500 the shell
            checks = [_check("%s_crash" % lid, name, None,
                             "%s: %s" % (type(e).__name__, str(e)[:120]), critical=True)]
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    return {
        "shell": 52,
        "title": "Growth integrity",
        "lanes": lanes,
        "summary": " ".join("%s=%s" % (l["id"], l["verdict"]) for l in lanes),
        "any_fail": any(l["verdict"] == "FAIL" for l in lanes),
        "any_unmeasured": any(l["verdict"] == "?" for l in lanes),
        "note": ("`?` means a lane verified NOTHING — it is not a pass. Lane 5 is "
                 "the only one that can see the reachability class that produced "
                 "three of the five 2026-08-17 defects."),
    }


@growth_integrity_master_shell_bp.route(
    "/api/v1/admin/growth-integrity-shell/master-tick", methods=["GET"])
def master_tick():
    if _disabled():
        return jsonify({"ok": False, "disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_run()), 200


@growth_integrity_master_shell_bp.route("/admin/growth-integrity-shell", methods=["GET"])
@growth_integrity_master_shell_bp.route("/api/v1/admin/growth-integrity-shell",
                                        methods=["GET"])
def board():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    d = _run()
    color = {"PASS": "#16a34a", "FAIL": "#dc2626", "?": "#eab308"}
    rows = []
    for ln in d["lanes"]:
        items = "".join(
            "<li><b>%s</b> — %s <i>%s</i></li>"
            % ({True: "PASS", False: "FAIL", None: "?"}[c["pass"]],
               _esc(c["name"]), _esc(c["detail"]))
            for c in ln["checks"])
        rows.append("<h3 style='color:%s'>%s — %s</h3><ul>%s</ul>"
                    % (color.get(ln["verdict"], "#eab308"), _esc(ln["name"]),
                       ln["verdict"], items))
    return ("<html><body style='font:14px system-ui;max-width:900px;margin:2rem auto'>"
            "<h1>Shell #52 — Growth integrity</h1><p><code>%s</code></p>%s"
            "<p style='color:#666'>%s</p></body></html>"
            % (_esc(d["summary"]), "".join(rows), _esc(d["note"]))), 200


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
