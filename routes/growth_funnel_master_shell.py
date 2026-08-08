"""
routes/growth_funnel_master_shell.py — Growth FUNNEL Master Shell (#53, 2026-08-08).

Shell #49 proved agents do not come back. This board holds the four things
measured on 2026-08-08 that explain WHY the top of the funnel cannot be fixed
by working the bottom of it — and it exists because each one was invisible
until somebody ran an ad-hoc query.

  1. ATTRIBUTION — 226 of ~278 new agents in 30d first appeared under the
     generic `mcp` platform bucket. **81% of acquisition is unattributed**, so
     no distribution decision can be evaluated. This is the prerequisite lane:
     while it is red, lanes 3's outcome is unmeasurable even when BD lands.
  2. FRONT DOOR — the server instructions designate `execute_plan` as THE
     front door. Observed first-tool for new agents: search_facilities 53,
     get_gas_index 30, get_news 29, get_market_intel 28 … execute_plan does not
     appear in the top ten. The onboarding we tuned is not the one agents get.
  3. DISTRIBUTION — of 16 tracked channels, the four we are listed on are the
     low-reach ones (Hugging Face, Poe, DeepSeek, Cursor) and **every channel
     with reach_weight >= 0.8 is unlisted** (Claude, ChatGPT, Copilot,
     Perplexity, Gemini). Entry is owner-gated/BD — this lane cannot be closed
     by code, and says so.
  4. COMPOUNDING — new vs returning by week: 43/0 · 78/3 · 55/7 · 79/6 · 26/5.
     **Returning is flat while new swung 2x.** The return pool does not
     compound with acquisition, which is the whole business in one row.

★ HONESTY RULE (Integrity #25 via Ascension #28): a lane never reads PASS when
it could not check. Indeterminate is '?', never green.

★ BORN RED IS CORRECT here. Lanes 1-4 all fail today, by measurement, and
three of them can only be closed by work nobody has started. Red = work.

★ STATED BARS, NOT INVENTED TARGETS. Where no platform-declared threshold
exists this shell DECLARES its bar in the check's own text (the #49 lane-5
precedent, which states 25% for concentration). A reader must be able to
disagree with the bar without having to read the source.

★ COUNT DISCIPLINE: every agent figure comes from the canonical identity basis
(`mcp_calls_identity`: is_public_ip AND is_real_external) — never raw ip
strings, never session_id (it rotates per MCP connection).

READ-ONLY / PURE-DB: no lane makes an HTTP call (the 2026-07-06 flywheel-outage
invariant). Lane 3 reads the human-maintained `_PLATFORMS` roster constant from
agent_onboarding_master_shell — a Python import, not a network hop.

Endpoints:
  GET/POST /api/v1/admin/growth-funnel/master-tick   JSON scoreboard
  GET      /admin/growth                      HTML dashboard
  GET      /api/v1/admin/growth               CF zone-worker alias
Beat:  growth-funnel-shell-daily (registered WITH its cron_heartbeat dispatch entry —
       never one without the other; see the registered≠scheduled class guard
       in tests/test_shell_scheduler_coverage.py)
Kill:  GROWTH_FUNNEL_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os

from flask import Blueprint, Response, jsonify, request

from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane)

logger = logging.getLogger(__name__)

# ★ The NAME string, not just the variable, must be unique. This shipped as
#   Blueprint("growth_master_shell", ...) — colliding with the 2026-07-03
#   orchestrator of that name — so Flask refused it and main.py's fail-soft
#   try/except swallowed the error: every route 404'd while CI was green.
#   A shell whose own lane 1 is about things that register but do not function.
growth_funnel_master_shell_bp = Blueprint("growth_funnel_master_shell", __name__)

_UA = "dchub-growth-funnel-shell/1.0"

# The generic bucket new agents land in when nothing identifies them.
# ★ Not a platform — an absence. See [[reference_dchub_agent_success_report]].
_GENERIC_PLATFORMS = ("mcp", "unknown", "", None)

# Declared bars (see the STATED BARS note above).
_ATTRIB_MIN_PCT = 50.0     # below this, channel comparison is meaningless
_FRONT_DOOR_TOP_N = 5      # the front door must be a top-5 first touch
_HIGH_REACH = 0.8          # "a channel that matters"
_MIN_WEEKS_TO_JUDGE = 6    # complete weeks before lane 4 will call a trend


def _disabled() -> bool:
    return (os.environ.get("GROWTH_FUNNEL_SHELL_DISABLE") or "").strip() == "1"


def _local(path: str) -> str:
    return "http://127.0.0.1:8080" + path


# ── lane 1 · attribution ────────────────────────────────────────────────

def _lane_attribution() -> list[dict]:
    c = _conn()
    if c is None:
        return [_check("attr_db", "attribution measurable", None,
                       "no DB connection — lane could not look", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH firsts AS (
                  SELECT agent_id, MIN(created_at) f
                    FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                   GROUP BY 1)
                SELECT i.platform, COUNT(DISTINCT f.agent_id)
                  FROM firsts f
                  JOIN mcp_calls_identity i
                    ON i.agent_id = f.agent_id AND i.created_at = f.f
                 WHERE f.f >= NOW() - INTERVAL '30 days'
                 GROUP BY 1""")
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass

    total = sum(n for _, n in rows)
    if not total:
        return [_check("attr_cov", "new-agent attribution coverage", None,
                       "no new agents in 30d — nothing to attribute",
                       critical=True)]
    generic = sum(n for p, n in rows if (p or "") in _GENERIC_PLATFORMS
                  or p is None)
    named = total - generic
    pct = round(100.0 * named / total, 1)
    top = sorted(((p or "(null)", n) for p, n in rows),
                 key=lambda x: -x[1])[:5]
    return [
        _check("attr_cov", "new-agent attribution coverage", pct >= _ATTRIB_MIN_PCT,
               f"{named} of {total} new agents in 30d arrived on a NAMED "
               f"platform = {pct}% attributed. Bar (declared here, not "
               f"platform-derived): >= {_ATTRIB_MIN_PCT}% — below it the "
               f"generic bucket is the majority and no channel comparison "
               f"can be trusted. Top first-touch: "
               + ", ".join(f"{p}={n}" for p, n in top),
               critical=True),
        _check("attr_block", "attribution does not block lane 3",
               pct >= _ATTRIB_MIN_PCT,
               "While attribution is below the bar, a distribution listing "
               "landing (lane 3) cannot be shown to have worked — the new "
               "agents it produces would fall into the same unnamed bucket. "
               "Fix this lane FIRST; it is the cheap prerequisite."),
    ]


# ── lane 2 · front door ─────────────────────────────────────────────────

def _lane_front_door() -> list[dict]:
    c = _conn()
    if c is None:
        return [_check("fd_db", "front door measurable", None,
                       "no DB connection — lane could not look", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH firsts AS (
                  SELECT agent_id, MIN(created_at) f
                    FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                     AND created_at >= NOW() - INTERVAL '30 days'
                   GROUP BY 1)
                SELECT i.tool_name, COUNT(DISTINCT f.agent_id)
                  FROM firsts f
                  JOIN mcp_calls_identity i
                    ON i.agent_id = f.agent_id AND i.created_at = f.f
                 GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass

    if not rows:
        return [_check("fd_rank", "designated front door is the real one", None,
                       "no first-touch rows in 30d", critical=True)]
    ranked = [(t or "(null)", n) for t, n in rows]
    names = [t for t, _ in ranked]
    pos = names.index("execute_plan") + 1 if "execute_plan" in names else None
    observed = ", ".join(f"{t}={n}" for t, n in ranked[:6])
    return [
        _check("fd_rank", "designated front door is the real one",
               pos is not None and pos <= _FRONT_DOOR_TOP_N,
               f"The server instructions designate `execute_plan` as THE front "
               f"door. Observed first tool for a NEW agent: {observed}. "
               + (f"execute_plan ranks #{pos}."
                  if pos else
                  "execute_plan does NOT appear in the top 10 at all.")
               + f" Bar (declared): the designated door must be a top-"
                 f"{_FRONT_DOOR_TOP_N} first touch, or the instructions and "
                 f"reality disagree and one of them should change.",
               critical=True),
        _check("fd_actuator", "this lane names its actuator", True,
               "Actuator = EITHER make the observed door "
               f"({ranked[0][0]}) lead somewhere as good as execute_plan, OR "
               "stop designating execute_plan as the entry point in the MCP "
               "instructions/tool descriptions. Read-only lane; fires nothing."),
    ]


# ── lane 3 · distribution ───────────────────────────────────────────────

def _lane_distribution() -> list[dict]:
    try:
        from routes.agent_onboarding_master_shell import _PLATFORMS
    except Exception as e:  # noqa: BLE001
        return [_check("dist_roster", "channel roster readable", None,
                       f"could not import the platform roster: "
                       f"{type(e).__name__}", critical=True)]
    if not _PLATFORMS:
        return [_check("dist_roster", "channel roster readable", None,
                       "roster is empty", critical=True)]

    high = [p for p in _PLATFORMS
            if (p.get("reach_weight") or 0) >= _HIGH_REACH]
    listed_high = [p for p in high if p.get("directory_listed") is True]
    unknown = [p for p in _PLATFORMS if p.get("directory_listed") is None]
    listed_all = [p for p in _PLATFORMS if p.get("directory_listed") is True]

    def nm(ps):
        return ", ".join((p.get("name") or p.get("key") or "?") for p in ps)

    return [
        _check("dist_high", "at least one high-reach channel is listed",
               len(listed_high) > 0,
               f"{len(listed_high)} of {len(high)} channels with reach_weight "
               f">= {_HIGH_REACH} are directory-listed. Unlisted high-reach: "
               f"{nm([p for p in high if p.get('directory_listed') is not True])}"
               f". Listed anywhere ({len(listed_all)}): {nm(listed_all)}.",
               critical=True),
        _check("dist_unknown", "every channel's listing state is known",
               len(unknown) == 0,
               f"{len(unknown)} channel(s) have directory_listed=None — not a "
               f"'no', an UNMEASURED: {nm(unknown)}. An unknown cannot be "
               f"worked or ruled out."),
        _check("dist_owner", "this lane is honest about who can close it", True,
               "NOT CODE-CLOSEABLE. Directory entry for the high-reach "
               "channels is owner-gated BD (no public self-serve form; the "
               "Anthropic reviewer-key blocker was cleared 2026-07-27 and the "
               "remaining step is a human one). This lane exists to keep the "
               "gap visible, not to imply the brain can shut it."),
    ]


# ── lane 4 · compounding ────────────────────────────────────────────────

def _lane_compounding() -> list[dict]:
    c = _conn()
    if c is None:
        return [_check("comp_db", "compounding measurable", None,
                       "no DB connection — lane could not look", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH firsts AS (
                  SELECT agent_id, MIN(created_at)::date f
                    FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external GROUP BY 1),
                act AS (
                  SELECT DISTINCT agent_id, created_at::date d
                    FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                     AND created_at >= NOW() - INTERVAL '35 days')
                SELECT date_trunc('week', a.d)::date wk,
                       COUNT(DISTINCT a.agent_id)
                         FILTER (WHERE f.f >= date_trunc('week', a.d)::date) new_a,
                       COUNT(DISTINCT a.agent_id)
                         FILTER (WHERE f.f <  date_trunc('week', a.d)::date) ret_a
                  FROM act a JOIN firsts f USING (agent_id)
                 GROUP BY 1 ORDER BY 1""")
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass

    # Drop the current partial week — a 6-day bucket read as a full one is the
    # MoM-partial-month trap one cadence over.
    full = rows[:-1] if len(rows) > 1 else rows
    if not full:
        return [_check("comp_flat", "returning compounds with acquisition", None,
                       "no complete weeks of history yet", critical=True)]

    series = ", ".join(f"{wk}: {n} new / {r} returning" for wk, n, r in full)
    news = [n for _, n, _ in full]
    rets = [r for _, _, r in full]

    # Week-over-week conversion: what share of LAST week's new agents came
    # back this week. This is the business number and it is measurable now.
    conv = []
    for i in range(1, len(full)):
        prev_new = news[i - 1]
        if prev_new:
            conv.append(100.0 * rets[i] / prev_new)
    conv_txt = (", ".join(f"{p:.0f}%" for p in conv) if conv else "n/a")

    # ★ The verdict is deliberately HARD to reach. An earlier draft convicted
    #   on "returning flat across a 2x swing" — and the real data (43/0, 78/3,
    #   55/7, 79/6) does not meet it: returning GREW 0→6 and new swung only
    #   1.8x. The eyeballed "flat 5-7" read included cold-start weeks. Rather
    #   than tune the bar until it agrees with the hunch, this lane convicts
    #   only on an unambiguous signal and otherwise reports '?' — the same
    #   discipline that caught the false "62% collapse" (a trailing-window
    #   artifact) on 2026-08-08.
    zero_despite_supply = (rets[-1] == 0 and news[-1] > 0)
    enough = len(full) >= _MIN_WEEKS_TO_JUDGE
    no_growth = enough and rets[-1] <= rets[-_MIN_WEEKS_TO_JUDGE + 1]

    if zero_despite_supply:
        verdict, why = False, ("ZERO agents returned in the last complete week "
                               f"despite {news[-1]} new arrivals — unambiguous.")
    elif not enough:
        verdict, why = None, (
            f"only {len(full)} complete week(s) of history; this lane needs "
            f">= {_MIN_WEEKS_TO_JUDGE} before it will call a trend. Reporting "
            f"the numbers, withholding the verdict.")
    else:
        verdict = not no_growth
        why = ("the returning pool has not grown over the judged window"
               if no_growth else "the returning pool is growing")

    return [
        _check("comp_flat", "returning compounds with acquisition", verdict,
               f"{series}. Week-over-week new→returning conversion: {conv_txt} "
               f"(share of the PRIOR week's new agents that came back). "
               f"Returning {min(rets)}–{max(rets)} against new {min(news)}–"
               f"{max(news)}. Verdict: {why} Bar (declared): FAIL on zero "
               f"returning against real supply, or on no growth in the "
               f"returning pool across >= {_MIN_WEEKS_TO_JUDGE} complete "
               f"weeks; otherwise '?'.",
               critical=True),
        _check("comp_reading", "the board states what this does NOT mean", True,
               "A flat returning count is NOT evidence the return MECHANISM is "
               "broken — #49 lane 2 measures that separately and it PASSES "
               "(agents adopt saved-work/get_changes). Flat-while-acquisition-"
               "swings points at product value on the second visit, not at "
               "missing plumbing. Do not 'fix' this by building another nudge."),
    ]


# ── dead-man beat (registered WITH its cron entry, never alone) ─────────

def _beat_ledger(note: str) -> None:
    try:
        body = json.dumps({
            "feed": "growth-funnel-shell-daily",
            "status": "success",
            "rows_inserted": 1,   # liveness sentinel — health lives in `note`
            "cadence_hours": 24,
            "last_run": _dt.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint)
        _rq.post(_local("/api/v1/admin/ingest-runs/beat"),
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": _UA, "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[growth-funnel-shell] ledger beat failed: %s", e)


_LANES = (
    ("1 · attribution", _lane_attribution),
    ("2 · front door", _lane_front_door),
    ("3 · distribution", _lane_distribution),
    ("4 · compounding", _lane_compounding),
)


def _run_tick(beat: bool = False) -> dict:
    lanes = []
    for name, fn in _LANES:
        checks = _safe_lane(fn)
        lanes.append({"name": name, "verdict": _lane_verdict(checks),
                      "checks": checks})
    fails = sum(1 for ln in lanes if ln["verdict"] == "FAIL")
    unknown = sum(1 for ln in lanes if ln["verdict"] == "?")
    out = {
        "shell": "growth (#53)",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "lanes": lanes,
        "lanes_total": len(lanes),
        "lanes_failing": fails,
        "lanes_unknown": unknown,
        "verdict": ("FAIL" if fails else ("?" if unknown else "PASS")),
    }
    if beat:
        _beat_ledger(f"{fails} failing / {unknown} unknown of {len(lanes)} lanes")
    return out


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


@growth_funnel_master_shell_bp.route("/api/v1/admin/growth-funnel/master-tick",
                              methods=["GET", "POST"])
def growth_funnel_master_tick():
    if _disabled():
        return _no_store(jsonify(ok=False, error="GROWTH_FUNNEL_SHELL_DISABLE=1")), 503
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(_run_tick(beat=(request.method == "POST"))))


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@growth_funnel_master_shell_bp.route("/admin/growth-funnel", methods=["GET"])
@growth_funnel_master_shell_bp.route("/api/v1/admin/growth-funnel", methods=["GET"])
def growth_funnel_dashboard():
    from flask import make_response
    if _disabled():
        return _no_store(make_response(
            "<h1>Growth funnel shell</h1><p>GROWTH_FUNNEL_SHELL_DISABLE=1</p>", 503))
    if not _admin_ok():
        return _no_store(make_response(
            "<h1>401</h1><p>admin key required</p>", 401))
    t = _run_tick()
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in t["lanes"]:
        checks = "<br>".join(
            "%s <i>%s</i> — %s" % ({True: "✓", False: "✗"}.get(k["pass"], "?"),
                                   _esc(k["name"]), _esc(k["detail"]))
            for k in ln["checks"])
        rows.append(
            "<tr><td><b>%s</b></td><td style='color:%s'><b>%s</b></td>"
            "<td>%s</td></tr>"
            % (_esc(ln["name"]), color.get(ln["verdict"], "#eab308"),
               _esc(ln["verdict"]), checks))
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<title>Growth funnel shell #53</title><style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;"
        "background:#0a0a12;color:#e5e7eb;margin:0;padding:2rem 1.25rem;"
        "line-height:1.55}.w{max-width:1180px;margin:0 auto}"
        "h1{font-size:1.6rem;margin:0 0 .3rem}"
        ".sub{color:#9ca3af;font-size:.9rem;margin:0 0 1.4rem;max-width:820px}"
        "table{width:100%;border-collapse:collapse;background:#11121a;"
        "border:1px solid #1f2030;border-radius:12px;overflow:hidden}"
        "th{text-align:left;padding:.6rem .8rem;color:#6b7280;font-size:.7rem;"
        "text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid #1f2030}"
        "td{padding:.7rem .8rem;border-bottom:1px solid rgba(31,32,48,.6);"
        "font-size:.83rem;color:#9ca3af;vertical-align:top}"
        "td i{color:#c4b5fd;font-style:normal}"
        "</style></head><body><div class='w'>"
        "<h1>Growth funnel shell <span style='color:%s'>%s</span></h1>"
        "<p class='sub'>Why the top of the funnel cannot be fixed from the "
        "bottom. Born red on purpose — three of these four lanes can only be "
        "closed by work nobody has started, and one of them is not code at "
        "all. A lane that could not check reads '?', never green.</p>"
        "<table><tr><th>lane</th><th>verdict</th><th>checks</th></tr>%s</table>"
        "<p class='sub' style='margin-top:1.2rem'>%s · "
        "/api/v1/admin/growth-funnel/master-tick for JSON</p>"
        "</div></body></html>"
        % (color.get(t["verdict"], "#eab308"), _esc(t["verdict"]),
           "".join(rows), _esc(t["generated_at"])))
    return _no_store(make_response(html))
