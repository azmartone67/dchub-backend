"""routes/planner_quality.py — planner-quality board (2026-07-26).

WHY: the 07-26 platform round converged on a question none of our boards
could answer — "is the PLANNER getting better?" ChatGPT put it precisely:
once execution is integrated, the planner owns the workflow outcome, so
completion alone is the wrong metric. Perplexity asked for the same thing
from the user side (time-to-first-result, which step failed, did a second
recipe fire), and Grok wants second-call conversion tracked over weeks.

Everything needed already lands in mcp_call_log — this board reads it:

  · `execute_plan_steps`  one row per execution, params carry
      {intent_class, status_counts{executed|gated_preview|failed|
       skipped_unresolved|not_run}, wall_ms, constraint_iso,
       constraint_rejects}
  · `recipe:<name>`       one row per prompts/get — recipe demand
  · `execute_plan`        the call itself (platform attribution)

THREE DIMENSIONS, kept separate on purpose (ChatGPT's framing — they
correlate but are not the same, and merging them hides where a regression
started):

  1. PLANNING  — did the planner pick a workable graph?
                 proxy: share of steps that resolved at all
                 (skipped_unresolved is a PLANNING miss: the graph asked
                 for an artifact nothing in it produces)
  2. EXECUTION — did the graph run without invariant violations?
                 proxy: executed+gated_preview share, constraint_rejects
  3. LATENCY   — wall_ms percentiles per intent class

INTEGRITY RATE is the headline: runs with ZERO skipped/failed steps and
zero constraint rejects. "Completed" cannot see Dallas quietly becoming
CAISO; integrity can.

Read-only, admin-gated, no-store. Fail-soft: any query error renders as a
named error row rather than a 500 — an empty board must never look green.

Surface:  GET /admin/planner-quality            (HTML)
          GET /api/v1/admin/planner-quality     (JSON)
"""

from __future__ import annotations

import datetime
import json
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

planner_quality_bp = Blueprint("planner_quality", __name__)

_DEFAULT_DAYS = 7
_GOOD = ("executed", "gated_preview")   # a gated preview is a WORKING step


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _db():
    try:
        from routes.brain_rag import _db as _rag_db
        return _rag_db()
    except Exception as e:  # noqa: BLE001
        logger.debug("[planner-quality] db unavailable: %s", e)
        return None


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def _compute(days: int = _DEFAULT_DAYS) -> dict:
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    out = {
        "ok": True,
        "window_days": days,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "basis": ("mcp_call_log · execute_plan_steps rows (one per execution) "
                  "+ recipe:<name> rows (one per prompts/get). Planning, "
                  "execution and latency are kept separate on purpose — they "
                  "correlate but a regression starts in exactly one of them."),
    }
    try:
        with c.cursor() as cur:
            # ── per-intent-class quality ──────────────────────────────
            cur.execute(
                "SELECT params FROM mcp_call_log "
                "WHERE tool = 'execute_plan_steps' "
                "  AND timestamp > now() - make_interval(days => %s)",
                (days,))
            rows = [r[0] for r in cur.fetchall()]
            by_class: dict = {}
            for p in rows:
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except Exception:
                        continue
                if not isinstance(p, dict):
                    continue
                cls = p.get("intent_class") or "?"
                b = by_class.setdefault(cls, {
                    "runs": 0, "steps": 0, "good": 0, "unresolved": 0,
                    "failed": 0, "rejects": 0, "clean_runs": 0, "ms": [],
                })
                b["runs"] += 1
                counts = p.get("status_counts") or {}
                if isinstance(counts, dict):
                    tot = sum(int(v or 0) for v in counts.values())
                    good = sum(int(counts.get(k) or 0) for k in _GOOD)
                    unres = int(counts.get("skipped_unresolved") or 0)
                    failed = int(counts.get("failed") or 0)
                    b["steps"] += tot
                    b["good"] += good
                    b["unresolved"] += unres
                    b["failed"] += failed
                    rej = int(p.get("constraint_rejects") or 0)
                    b["rejects"] += rej
                    if tot and good == tot and not rej:
                        b["clean_runs"] += 1
                ms = p.get("wall_ms")
                if isinstance(ms, (int, float)):
                    b["ms"].append(float(ms))
            classes = []
            tot_runs = tot_clean = 0
            for cls, b in sorted(by_class.items(), key=lambda kv: -kv[1]["runs"]):
                ms = sorted(b["ms"])
                p50 = ms[len(ms) // 2] if ms else None
                p95 = ms[max(0, int(len(ms) * 0.95) - 1)] if ms else None
                tot_runs += b["runs"]
                tot_clean += b["clean_runs"]
                classes.append({
                    "intent_class": cls,
                    "runs": b["runs"],
                    # 1 · PLANNING — a graph that asks for an artifact nothing
                    # in it produces is a planning miss, not an execution one.
                    "planning_resolved_pct": _pct(b["steps"] - b["unresolved"],
                                                  b["steps"]),
                    # 2 · EXECUTION — of the steps that ran, how many worked.
                    "execution_ok_pct": _pct(b["good"],
                                             max(0, b["steps"] - b["unresolved"])),
                    "integrity_pct": _pct(b["clean_runs"], b["runs"]),
                    "constraint_rejects": b["rejects"],
                    "failed_steps": b["failed"],
                    "unresolved_steps": b["unresolved"],
                    # 3 · LATENCY
                    "p50_ms": int(p50) if p50 is not None else None,
                    "p95_ms": int(p95) if p95 is not None else None,
                })
            out["classes"] = classes
            out["totals"] = {
                "runs": tot_runs,
                "integrity_pct": _pct(tot_clean, tot_runs),
                "note": ("integrity = runs with EVERY step executed (or a "
                         "tier-gated preview) AND zero constraint rejects — "
                         "completion alone cannot see a geography violation"),
            }

            # ── recipe demand + second-call conversion ────────────────
            cur.execute(
                "SELECT replace(tool, 'recipe:', '') AS recipe, count(*) "
                "FROM mcp_call_log "
                "WHERE tool LIKE 'recipe:%%' "
                "  AND timestamp > now() - make_interval(days => %s) "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 15", (days,))
            out["recipes"] = [{"recipe": r[0], "starts": int(r[1])}
                              for r in cur.fetchall()]

            # Did a session that ran execute_plan run ANOTHER planner-family
            # call afterwards? That is the next_recipe habit loop, measured.
            cur.execute(
                "WITH first AS ("
                "  SELECT session_id, min(timestamp) AS t0 FROM mcp_call_log"
                "   WHERE tool = 'execute_plan' AND session_id IS NOT NULL"
                "     AND timestamp > now() - make_interval(days => %s)"
                "   GROUP BY 1) "
                "SELECT count(*), count(*) FILTER (WHERE EXISTS ("
                "  SELECT 1 FROM mcp_call_log l WHERE l.session_id = first.session_id"
                "   AND l.timestamp > first.t0"
                "   AND (l.tool = 'execute_plan' OR l.tool LIKE 'recipe:%%'))) "
                "FROM first", (days,))
            r = cur.fetchone() or (0, 0)
            sessions, returned = int(r[0] or 0), int(r[1] or 0)
            out["second_call"] = {
                "sessions_with_execute_plan": sessions,
                "sessions_with_a_follow_up": returned,
                "conversion_pct": _pct(returned, sessions),
                "note": ("the next_recipe habit loop — a follow-up planner "
                         "call in the SAME session after the first execution"),
            }

            # ── platform attribution (callers, never crawlers) ────────
            cur.execute(
                "SELECT COALESCE(NULLIF(platform, ''), '?'), count(*) "
                "FROM mcp_call_log "
                "WHERE (tool = 'execute_plan' OR tool LIKE 'recipe:%%') "
                "  AND timestamp > now() - make_interval(days => %s) "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (days,))
            out["by_platform"] = [{"platform": r[0], "calls": int(r[1])}
                                  for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:
            pass
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def _days_arg() -> int:
    try:
        return max(1, min(90, int(request.args.get("days", _DEFAULT_DAYS))))
    except Exception:
        return _DEFAULT_DAYS


@planner_quality_bp.route("/api/v1/admin/planner-quality", methods=["GET"])
def planner_quality_json():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_compute(_days_arg()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _cell(v, suffix="%"):
    return "—" if v is None else "%s%s" % (v, suffix)


@planner_quality_bp.route("/admin/planner-quality", methods=["GET"])
def planner_quality_html():
    from flask import make_response
    if not _admin_ok():
        return make_response("<h1>401</h1><p>admin key required</p>", 401)
    d = _compute(_days_arg())
    if not d.get("ok"):
        html = ("<h1>Planner quality</h1><p style='color:#ef4444'>error: %s</p>"
                % _esc(d.get("error")))
    else:
        t = d.get("totals") or {}
        rows = "".join(
            "<tr><td><b>%s</b></td><td>%d</td><td>%s</td><td>%s</td>"
            "<td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (_esc(c["intent_class"]), c["runs"],
               _cell(c["planning_resolved_pct"]), _cell(c["execution_ok_pct"]),
               _cell(c["integrity_pct"]),
               c["constraint_rejects"] or 0,
               _cell(c["p50_ms"], "ms"), _cell(c["p95_ms"], "ms"))
            for c in d.get("classes", []))
        recipes = "".join("<li>%s — <b>%d</b></li>" % (_esc(r["recipe"]), r["starts"])
                          for r in d.get("recipes", []))
        plats = "".join("<li>%s — <b>%d</b></li>" % (_esc(p["platform"]), p["calls"])
                        for p in d.get("by_platform", []))
        sc = d.get("second_call") or {}
        html = (
            "<html><head><title>Planner quality</title>"
            "<meta http-equiv='refresh' content='300'></head>"
            "<body style='font-family:system-ui;max-width:1100px;margin:24px auto'>"
            "<h1>Planner quality <small>%dd</small></h1>"
            "<p style='color:#64748b'>%s</p>"
            "<h2>Integrity: %s <small>(%d runs)</small></h2>"
            "<p style='color:#64748b'><small>%s</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>intent class</th><th>runs</th>"
            "<th>1 · planning<br><small>steps resolvable</small></th>"
            "<th>2 · execution<br><small>ran &amp; worked</small></th>"
            "<th>integrity<br><small>clean runs</small></th>"
            "<th>geo rejects</th><th>p50</th><th>p95</th></tr>%s</table>"
            "<h3>Recipe demand</h3><ul>%s</ul>"
            "<h3>Second-call conversion</h3><p>%s of %s sessions ran a "
            "follow-up planner call — <small>%s</small></p>"
            "<h3>By platform <small>(callers, not crawlers)</small></h3><ul>%s</ul>"
            "<p><small>refreshes 300s · ?days=N to widen</small></p>"
            "</body></html>"
            % (d["window_days"], _esc(d["basis"]),
               _cell(t.get("integrity_pct")), t.get("runs", 0),
               _esc(t.get("note", "")), rows,
               recipes or "<li>none yet</li>",
               _cell(sc.get("conversion_pct")),
               sc.get("sessions_with_execute_plan", 0),
               _esc(sc.get("note", "")),
               plats or "<li>none yet</li>"))
    resp = make_response(html, 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp
