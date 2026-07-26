"""routes/platform_funnel.py — per-platform adoption funnel (2026-07-26).

WHY: the 07-26 growth review found the "Gemini and ChatGPT blew up" signal
was crawler/read traffic, not tool adoption — the platform dashboards mix
surface reads with real calls, so a platform push could not be judged by
callers. This is the missing instrument: per platform, per week, the funnel
an iteration is supposed to move —

    arrive   distinct real external agent IPs that called ANY tool
    activate calls per arriving IP (depth, not just presence)
    claim    claim_free_key calls (identity intent)
    return   arriving IPs that were ALSO active the prior week

All from mcp_calls_identity with is_real_external — the same canonical basis
as the north star (never sessions, never raw calls, crawler-free by
construction). A platform iteration (envelope, tuner, /for/ page, connector
recipe) should show up HERE within a week; if it only moves crawler counts,
it did not move adoption.

Surface:  GET /api/v1/admin/platform-funnel        (JSON)
          GET /admin/platform-funnel               (HTML)
Read-only · admin-gated · no-store · 6 complete weeks + the partial current
week (labeled) · fail-soft {} on DB loss.
"""

from __future__ import annotations

import datetime
import logging
import os
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

platform_funnel_bp = Blueprint("platform_funnel", __name__)

_WEEKS = 6


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
        logger.debug("[platform-funnel] db unavailable: %s", e)
        return None


def _compute() -> dict:
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    try:
        with c.cursor() as cur:
            cur.execute(
                """
                WITH base AS (
                    SELECT COALESCE(NULLIF(platform, ''), '?') AS platform,
                           date_trunc('week', created_at)::date AS wk,
                           ip_address,
                           tool_name
                      FROM mcp_calls_identity
                     WHERE is_real_external IS TRUE
                       AND created_at > date_trunc('week', now())
                                        - make_interval(weeks => %s)
                )
                SELECT platform, wk,
                       COUNT(DISTINCT ip_address)                     AS arrive,
                       COUNT(*)                                      AS calls,
                       COUNT(*) FILTER (WHERE tool_name = 'claim_free_key')
                                                                     AS claims,
                       COUNT(DISTINCT ip_address) FILTER (WHERE EXISTS (
                           SELECT 1 FROM base p
                            WHERE p.platform = base.platform
                              AND p.ip_address = base.ip_address
                              AND p.wk = base.wk - 7))               AS returning
                  FROM base
                 GROUP BY 1, 2
                 ORDER BY 1, 2
                """, (_WEEKS,))
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:120]}
    finally:
        try:
            c.close()
        except Exception:
            pass
    this_wk = (datetime.datetime.now(datetime.timezone.utc).date()
               - datetime.timedelta(
                   days=datetime.datetime.now(datetime.timezone.utc)
                   .weekday()))
    platforms: dict = {}
    for platform, wk, arrive, calls, claims, returning in rows:
        platforms.setdefault(platform, []).append({
            "week": wk.isoformat(),
            "partial": wk == this_wk,
            "arrive": int(arrive),
            "calls": int(calls),
            "claims": int(claims),
            "returning": int(returning),
        })
    # Rank by latest COMPLETE week's arrivals so a partial week can't reorder.
    def _rank(p):
        complete = [w for w in platforms[p] if not w["partial"]]
        return -(complete[-1]["arrive"] if complete else 0)
    ordered = sorted(platforms, key=_rank)
    return {
        "ok": True,
        "basis": "mcp_calls_identity · is_real_external only — crawler-free "
                 "by construction; a platform push that moves only crawler "
                 "counts will NOT move these numbers",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "weeks": _WEEKS,
        "platforms": {p: platforms[p] for p in ordered},
    }


@platform_funnel_bp.route("/api/v1/admin/platform-funnel", methods=["GET"])
def funnel_json():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_compute())
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


@platform_funnel_bp.route("/admin/platform-funnel", methods=["GET"])
def funnel_html():
    from flask import make_response
    if not _admin_ok():
        return make_response("<h1>401</h1><p>admin key required</p>", 401)
    d = _compute()
    if not d.get("ok"):
        html = "<h1>Platform funnel</h1><p>error: %s</p>" % _esc(
            d.get("error"))
    else:
        blocks = []
        for p, weeks in d["platforms"].items():
            rows = "".join(
                "<tr><td>%s%s</td><td>%d</td><td>%d</td><td>%d</td>"
                "<td>%d</td></tr>"
                % (_esc(w["week"]), "*" if w["partial"] else "",
                   w["arrive"], w["calls"], w["claims"], w["returning"])
                for w in weeks)
            blocks.append(
                "<h3>%s</h3><table cellpadding='6' "
                "style='border-collapse:collapse'>"
                "<tr><th>week</th><th>arrive (real IPs)</th><th>calls</th>"
                "<th>claims</th><th>returning</th></tr>%s</table>"
                % (_esc(p), rows))
        html = ("<html><head><title>Platform funnel</title>"
                "<meta http-equiv='refresh' content='300'></head>"
                "<body style='font-family:system-ui;max-width:900px;"
                "margin:24px auto'><h1>Per-platform adoption funnel</h1>"
                "<p><small>%s · * = partial current week</small></p>%s"
                "</body></html>"
                % (_esc(d["basis"]), "".join(blocks)))
    resp = make_response(html, 200)
    resp.headers["Cache-Control"] = "no-store"
    return resp
