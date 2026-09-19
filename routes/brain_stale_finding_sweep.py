"""brain_stale_finding_sweep — close findings the detector stopped claiming.

WHY
A detector that stops emitting a finding does not close it. brain_findings rows
are UPSERTed on re-detection (last_seen bumps) and nothing ever transitions a
row that simply stops arriving. So every detector precision fix strands its own
accepted entries: #4756 taught js_field_fallback_missing to stop filing
predicate reads, and those four rows would have sat `open` forever — re-detected
never, resolved never — inflating the queue with claims the detector itself had
withdrawn.

★★★ WHY THIS IS DANGEROUS, AND WHAT THE FLOORS ARE FOR
"Resolve what hasn't been seen lately" is one bad night away from resolving the
ENTIRE queue. If the nightly scan fails, is misconfigured, or checks out the
wrong root, every row goes stale simultaneously — and an unfloored sweep reads
that as "everything got fixed" and closes the lot. The sweep would report a
triumphant number and destroy the queue, and the failure would be invisible
because an empty queue looks exactly like a healthy one.

So staleness is NEVER sufficient evidence on its own. Three floors, each
refusing a different way for the scan to be lying:

  1. LIVENESS      the detector must have written INSIDE the fresh window.
                   Proves the scanner ran at all. Without it, a dead scanner
                   sweeps its own queue to zero.
  2. REFRESH VOLUME enough rows must have been refreshed in that window.
                   A scanner that ran but matched almost nothing (stale glob,
                   wrong root, renamed repo) passes LIVENESS on a single row.
                   cf. a scan that can find nothing needs a floor.
  3. PROPORTION    never close more than a fraction of the open queue in one
                   run. When a detector's URL/key SHAPE changes, every old row
                   stops matching at once. That is ONE regression, not N fixes,
                   and it presents identically to N fixes. Refusing is correct
                   even though it leaves real staleness uncollected.

A refusal always reports every floor's computed value, because a sweep that
declines silently is indistinguishable from one that found nothing to do.

★★ WHY status='resolved' AND NOT A TRUER NAME
These rows were WITHDRAWN, not fixed, and 'dismissed' says that better. It
cannot be used. The seven consumers that read brain_findings disagree about
what counts as closed:

    NOT IN ('resolved', 'closed')                  x1
    NOT IN ('resolved', 'wont_fix')                x2
    NOT IN ('resolved', 'wont_fix', 'dismissed')   x4  (plus status='open' readers)

'dismissed' would leave the row OPEN to three of them and closed to the rest —
a finding that is closed on one surface and open on another is the split-brain
defect, and it is worse than an imprecise label. 'resolved' is the only value
every consumer honours.

The cost is paid explicitly instead of hidden: 'resolved' feeds
findings_resolved_7d/30d on /brain/evolution, so a sweep INFLATES a metric the
operator reads as fixes. Every swept row therefore carries the marker below in
its detail, so the inflation is greppable and subtractable after the fact
rather than silently baked in. Reconciling those seven vocabularies is the real
fix and is not attempted here.
"""

import os
import logging
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_stale_finding_sweep_bp = Blueprint("brain_stale_finding_sweep", __name__)

MARKER = "[stale-sweep]"

# Floors. Deliberately conservative: the cost of refusing is a queue that stays
# a little too long, and the cost of over-firing is a queue that silently
# empties. Those are not symmetric.
_FRESH_WINDOW_H = 36        # the nightly scan runs 05:13Z; 36h survives one miss
_MIN_REFRESHED = 5          # rows the scan must have touched in that window
_STALE_DAYS = 3             # not re-detected for this long => candidate
_MAX_SWEEP_FRACTION = 0.30  # of the open queue, per run
_MIN_ABS_SWEEP = 3          # small queues would otherwise never clear


def _dsn():
    return os.environ.get("DATABASE_URL")


def _admin_ok() -> bool:
    """Same env precedence as brain_bug_squash — reading a different admin key
    name than the sibling shipped an endpoint gated SHUT (#4762)."""
    import hmac
    want = (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.headers.get("X-Internal-Key") or "").strip()
    return bool(want) and hmac.compare_digest(want, got)


def evaluate_floors(newest_age_h, refreshed_in_window, open_total, stale_total):
    """Pure decision function — the whole safety argument, testable without a DB.

    Returns (allowed, refusals, cap). `refusals` is a list so a caller sees
    EVERY reason it declined, not just the first: a scan can be both dead and
    shape-changed, and fixing one would otherwise reveal the next as a surprise.
    """
    refusals = []
    if newest_age_h is None:
        refusals.append({
            "floor": "liveness", "value": None, "limit": _FRESH_WINDOW_H,
            "why": "this detector has never written a finding — there is no "
                   "scan to trust, so staleness means nothing"})
    elif newest_age_h > _FRESH_WINDOW_H:
        refusals.append({
            "floor": "liveness", "value": round(newest_age_h, 1),
            "limit": _FRESH_WINDOW_H,
            "why": "the scanner has not written for longer than the fresh "
                   "window, so every row is stale because the SCANNER is dead, "
                   "not because the findings were fixed"})
    if refreshed_in_window < _MIN_REFRESHED:
        refusals.append({
            "floor": "refresh_volume", "value": refreshed_in_window,
            "limit": _MIN_REFRESHED,
            "why": "the scan wrote almost nothing — a stale glob or wrong "
                   "checkout root passes the liveness floor on a single row"})

    cap = max(_MIN_ABS_SWEEP, int(open_total * _MAX_SWEEP_FRACTION))
    if stale_total > cap:
        refusals.append({
            "floor": "proportion", "value": stale_total, "limit": cap,
            "why": f"{stale_total} of {open_total} open rows went stale at "
                   "once — that is the signature of a detector key/URL shape "
                   "change (ONE regression), not that many separate fixes"})
    return (not refusals), refusals, cap


def _gather(cur, detector):
    cur.execute(
        """SELECT
             EXTRACT(EPOCH FROM (NOW() - MAX(last_seen))) / 3600.0,
             COUNT(*) FILTER (WHERE last_seen > NOW() - %s * INTERVAL '1 hour'),
             COUNT(*) FILTER (WHERE COALESCE(status,'open') = 'open')
           FROM brain_findings WHERE detector = %s""",
        (_FRESH_WINDOW_H, detector))
    newest_age_h, refreshed, open_total = cur.fetchone()
    cur.execute(
        """SELECT id, issue, url,
                  EXTRACT(EPOCH FROM (NOW() - last_seen)) / 86400.0
             FROM brain_findings
            WHERE detector = %s
              AND COALESCE(status,'open') = 'open'
              AND last_seen < NOW() - %s * INTERVAL '1 day'
            ORDER BY last_seen ASC""",
        (detector, _STALE_DAYS))
    stale = [{"id": r[0], "issue": r[1], "url": r[2],
              "stale_days": round(float(r[3]), 1)} for r in cur.fetchall()]
    return (float(newest_age_h) if newest_age_h is not None else None,
            int(refreshed or 0), int(open_total or 0), stale)


@brain_stale_finding_sweep_bp.route(
    "/api/v1/admin/brain/findings/stale-sweep", methods=["POST", "GET"])
def stale_sweep():
    """DRY RUN unless dry_run=0. `detector` is REQUIRED and never defaults:
    liveness is a per-detector fact, and a global sweep would let one healthy
    scanner vouch for another that is dead."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    detector = (request.args.get("detector") or "").strip()
    if not detector:
        return jsonify(ok=False, error="detector is required",
                       why="liveness is per-detector; a global sweep lets one "
                           "healthy scanner vouch for a dead one"), 400
    dry = request.args.get("dry_run", "1") not in ("0", "false", "no")
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="DATABASE_URL not set"), 200

    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                newest_h, refreshed, open_total, stale = _gather(cur, detector)
                allowed, refusals, cap = evaluate_floors(
                    newest_h, refreshed, open_total, len(stale))
                floors = {
                    "liveness": {"newest_write_age_h":
                                 round(newest_h, 1) if newest_h is not None else None,
                                 "limit_h": _FRESH_WINDOW_H},
                    "refresh_volume": {"refreshed_in_window": refreshed,
                                       "limit": _MIN_REFRESHED},
                    "proportion": {"stale": len(stale), "open_total": open_total,
                                   "cap": cap,
                                   "fraction": _MAX_SWEEP_FRACTION},
                    "stale_after_days": _STALE_DAYS,
                }
                if not allowed:
                    return jsonify(ok=True, acted=False, detector=detector,
                                   refused=True, refusals=refusals,
                                   floors=floors, candidates=stale[:50]), 200
                if dry or not stale:
                    return jsonify(ok=True, acted=False, dry_run=dry,
                                   detector=detector, refused=False,
                                   floors=floors, would_close=len(stale),
                                   candidates=stale[:50]), 200

                note = (f"\n\n{MARKER} {datetime.now(timezone.utc).isoformat()} "
                        f"— closed by the stale sweep: not re-emitted by "
                        f"{detector} for >{_STALE_DAYS}d, with the scan proven "
                        f"live ({refreshed} rows refreshed in {_FRESH_WINDOW_H}h). "
                        f"WITHDRAWN by its detector, not fixed — subtract this "
                        f"marker from findings_resolved before reading it as work.")
                ids = [s["id"] for s in stale]
                cur.execute(
                    """UPDATE brain_findings
                          SET status = 'resolved', resolved_at = NOW(),
                              detail = COALESCE(detail,'') || %s
                        WHERE id = ANY(%s)
                          AND COALESCE(status,'open') = 'open'""",
                    (note, ids))
                closed = cur.rowcount
            c.commit()
    except Exception as e:
        return jsonify(ok=False,
                       error=f"{type(e).__name__}: {str(e)[:200]}"), 200

    return jsonify(ok=True, acted=True, detector=detector, refused=False,
                   closed=closed, requested=len(stale), floors=floors,
                   closed_ids=ids[:50],
                   note=("status='resolved' is the only value all seven "
                         "brain_findings consumers honour; the marker in "
                         "`detail` is what keeps the metric inflation "
                         "auditable")), 200
