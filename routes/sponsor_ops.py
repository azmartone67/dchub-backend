"""Producing the advertiser report, and feeding it (2026-08-28).

WHAT WAS MISSING. #3285 built `routes/sponsor_report.monthly_report()` and
`render_text()` — a good report — and stopped there. Nothing called either one.
There was no route, no command, and no scheduled job, so:

  * an operator could not produce a report for a sponsor at all, and
  * `crawls_from_snapshots()` read a table that nothing ever wrote, so the
    crawl table — the section the whole product is sold on — could only ever
    fall back to the live 8-day Cloudflare window.

/advertise promises publicly that "the first invoice goes out after the first
monthly report exists". A report generator nothing can invoke does not clear
that gate. This module is the two missing halves: an admin route that renders
the report, and an admin route that runs the daily accrual behind it.

★★★ THE ACCRUAL IS A CLOCK THAT CANNOT BE REWOUND. Cloudflare retains 8 days of
    request-level analytics for this zone; days older than that are REFUSED,
    not empty (measured, see routes/sponsor_crawl.py). Every day the snapshot
    job does not run is a day that can never appear in any advertiser's report,
    ever. That is why the job ships in the same change as the route rather than
    after it.

★ BOTH ROUTES TALK TO CLOUDFLARE AND CAN EXCEED THE EDGE TIMEOUT. The CF worker
  gives admin routes 15s (ROUTE_TIMEOUTS DEFAULT) and a slow one returns 503
  while the origin keeps working. Call these on the RAILWAY ORIGIN, not through
  dchub.cloud — the workflow that ships alongside this does exactly that.
  Once snapshots exist the report reads them from Postgres and is fast; the
  live-Cloudflare path is only the cold-start fallback.

★ SNAPSHOT WRITES ARE CORRECTIONS, NOT ADDITIONS. snapshot_crawls() upserts
  with SET, so re-running a day replaces its figure. Re-running is safe and is
  the intended way to settle the most recent day.
"""
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

sponsor_ops_bp = Blueprint("sponsor_ops", __name__)

# The report's own idea of how many days back it is worth asking for. Kept
# here so the route and the docstring cannot drift apart.
_DEFAULT_DAYS = 30
_MAX_DAYS = 400


def _admin_ok():
    """One auth path with the rest of the sponsorship surface.

    Imported rather than reimplemented: two copies of an admin check is how one
    of them ends up accepting a key the other rejects.
    """
    from routes.sponsorships import _admin_ok as _sponsor_admin_ok
    return _sponsor_admin_ok()


def _int_arg(name, default, lo, hi):
    try:
        v = int(request.args.get(name) or default)
    except Exception:
        v = default
    return max(lo, min(hi, v))


# ── GET /api/v1/admin/sponsorships/<id>/report ───────────────────────
@sponsor_ops_bp.route("/api/v1/admin/sponsorships/<int:sid>/report",
                      methods=["GET"])
def sponsorship_report(sid: int):
    """The monthly advertiser report for one sponsorship.

    `?format=text` renders the plain-text version that goes to the advertiser;
    the default JSON carries every section plus its `limits` for our own use.
    `?aliases=Acme+Cloud,AcmeCloud` widens the brand-mention scan — pass the
    names the sponsor actually trades under, because the scan is word-boundary
    exact and will not guess them.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    days = _int_arg("days", _DEFAULT_DAYS, 1, _MAX_DAYS)
    aliases = tuple(a.strip() for a in
                    (request.args.get("aliases") or "").split(",") if a.strip())
    try:
        from routes.sponsor_report import monthly_report, render_text
    except Exception as e:
        logger.warning("[sponsor_ops] report module unavailable: %s", e)
        return jsonify(ok=False, error=f"report_unavailable: {e}"), 500

    rep = monthly_report(sid, days=days, brand_aliases=aliases)
    if (request.args.get("format") or "").lower() == "text":
        body = render_text(rep)
        status = 200 if rep.get("ok") else 404
        return (body, status,
                {"Content-Type": "text/plain; charset=utf-8",
                 "Cache-Control": "no-store"})
    resp = jsonify(rep)
    resp.headers["Cache-Control"] = "no-store"
    return resp, (200 if rep.get("ok") else 404)


# ── POST /api/v1/admin/sponsor-crawl/snapshot ────────────────────────
@sponsor_ops_bp.route("/api/v1/admin/sponsor-crawl/snapshot",
                      methods=["POST", "GET"])
def sponsor_crawl_snapshot():
    """Accrue one or more days of per-engine crawl counts.

    GET is allowed on purpose: this job is idempotent and correcting, and a
    scheduled runner that can only issue a GET should not be the reason a day
    is lost forever.

    `?days=2` by default — the most recent day is still settling at Cloudflare,
    so the previous one is re-read and CORRECTED on every run.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    days = _int_arg("days", 2, 1, 8)
    try:
        from routes.sponsor_crawl import snapshot_crawls, MAX_LOOKBACK_DAYS
        from routes.sponsor_report import SPONSOR_SURFACES
    except Exception as e:
        logger.warning("[sponsor_ops] crawl module unavailable: %s", e)
        return jsonify(ok=False, error=f"crawl_unavailable: {e}"), 500

    res = snapshot_crawls(SPONSOR_SURFACES, days=days)
    res["paths"] = list(SPONSOR_SURFACES)
    res["max_lookback_days"] = MAX_LOOKBACK_DAYS
    # The whole point of the job. A run that wrote nothing is not a quiet
    # success — say which it was, in the response, where the runner logs it.
    if res.get("ok") and not res.get("days_written"):
        res.setdefault("limits", []).append(
            "Ran, but wrote no days. Coverage did not grow, and these days "
            "cannot be recovered once they age past the retention window.")
    status = 200 if res.get("ok") else 503
    return jsonify(res), status


# ── GET /api/v1/admin/sponsor-crawl/coverage ─────────────────────────
@sponsor_ops_bp.route("/api/v1/admin/sponsor-crawl/coverage", methods=["GET"])
def sponsor_crawl_coverage():
    """How much of a window the accrued snapshots actually cover.

    The one number that says whether the daily job is really running. A report
    can be produced any day; a report that covers the month it claims to cover
    only exists if this has been climbing by one per day.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    days = _int_arg("days", _DEFAULT_DAYS, 1, _MAX_DAYS)
    try:
        from routes.sponsor_crawl import crawls_from_snapshots
        from routes.sponsor_report import SPONSOR_SURFACES
    except Exception as e:
        return jsonify(ok=False, error=f"crawl_unavailable: {e}"), 500
    res = crawls_from_snapshots(SPONSOR_SURFACES, days=days)
    resp = jsonify(res)
    resp.headers["Cache-Control"] = "no-store"
    return resp, (200 if res.get("ok") else 503)
