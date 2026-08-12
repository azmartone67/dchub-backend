"""Freshness Master Shell — GET /api/v1/admin/freshness
tick: /api/v1/admin/freshness/master-tick
kill: FRESHNESS_SHELL_DISABLE=1

Built 2026-08-08 after a freshness chase that found the opposite of what was
reported. The owner's complaint was "radar is frozen"; the DCPI pipeline turned
out to be healthy end to end (recompute 4x/day, 315 snapshot rows/day, deltas
computing). The thing actually worth watching was never the freeze — it was a
signal that is MOVING and may be moving for the wrong reason.

WHY THIS EXISTS — three findings, none of which any existing board covered:

  1. GREEN IS NOT EVIDENCE THAT A CRON DID ANYTHING. Every job in
     facility-snapshot-daily.yml runs `curl -sS ... || true`. Without --fail,
     curl exits 0 on a 402 or a 500, and `|| true` swallows what is left. The
     workflow reported success every day for eight days; the only reason we
     know the DCPI snapshot actually inserted is that someone read
     `rows_inserted` out of the run log body. A board that watches workflow
     conclusions is watching a field that cannot fail.
  2. A CROSS-FORMULA DELTA IS WORSE THAN A STALE ONE. /api/v1/dcpi/trending
     differences today's excess_power_score against a snapshot >=7 days old.
     If method_version changed inside that window, the published "delta_7d" is
     the distance between two different formulas, rendered as a market move.
     Stale data looks stale; this looks like news. All five trending markets
     currently read NEGATIVE, which is not the shape of a genuine
     top-5-by-absolute-move, and that is the first thing to rule out.
  3. STALLED AND QUIET ARE DIFFERENT, AND ONLY ONE IS A DEFECT. /whats-new
     already separates growing / refreshed / on-cadence / measuring / unjudged
     and names its own open findings. The page is not lying — 5 of 17 layers
     grow because 5 of 17 layers grow. What nothing watched is the SHAPE of
     that distribution over time, and specifically the "the loader ran; no net
     new rows persisted" cluster: jobs succeeding while writing nothing, which
     is the failure mode that looks healthiest from the outside.

★ THE RULE THIS SHELL IS BUILT AROUND: A MEASUREMENT WE COULD NOT TAKE IS NOT
A MEASUREMENT THAT FAILED. Every check is THREE-valued: True (agrees), False
(contradicts), None (could not read). A dead endpoint, a missing column and a
DB we could not reach all render None with a stated reason. None of them may
render as a content failure, and none of them may render as a pass.

Lanes are born red where the work is real and unstarted. A born-red lane is a
work order, not a defect.
"""
from __future__ import annotations

import json
import os

from flask import Blueprint, jsonify

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane)

freshness_master_shell_bp = Blueprint("freshness_master_shell", __name__)

_UA = "dchub-freshness-shell/1.0 (+https://dchub.cloud)"
_TIMEOUT = 15

_WHATS_NEW = "https://dchub.cloud/api/v1/whats-new"
_TRENDING = "https://dchub.cloud/api/v1/dcpi/trending"

# Statuses /whats-new can emit. Only ONE of these is unambiguously a defect;
# the rest are legitimate states a healthy layer passes through. Hard-coding a
# "not growing == broken" rule is how a semiannual FCC source gets reported as
# stuck 230 days a year.
_STALL_STATUSES = {"stuck", "stalled", "overdue"}
_QUIET_OK = {"on cadence", "on_cadence", "measuring", "refreshed"}

# How old the NEWEST platform card may be before the section counts as having
# stopped receiving. Generous on purpose: shipping is lumpy and a quiet
# fortnight is normal. This is a "has the pipe closed" threshold, not a cadence
# target — tightening it turns ordinary quiet weeks into false alarms.
_PRODUCT_STALE_DAYS = 21


def _disabled() -> bool:
    return os.environ.get("FRESHNESS_SHELL_DISABLE", "") == "1"


def _get_json(url: str):
    """Returns (payload, None) or (None, reason). NEVER raises.

    A reason string means UNREADABLE — the caller must render None, not False.
    Status is checked BEFORE the body is parsed: `requests` does not raise on
    4xx, and our own API answers errors with well-formed JSON, so parsing an
    error body would hand the caller an object with none of the expected keys
    and render as a content failure on an endpoint we merely could not read.
    """
    try:
        import requests as _rq
        r = _rq.get(url, headers={"User-Agent": _UA,
                                  "Accept": "application/json"},
                    timeout=_TIMEOUT)
        if not 200 <= r.status_code < 300:
            return None, f"HTTP {r.status_code}"
        return json.loads(r.content.decode("utf-8", "replace")), None
    except Exception as e:  # noqa: BLE001 - any failure is UNREADABLE
        return None, f"{type(e).__name__}"


# ── lane 1 · ingestion liveness ──────────────────────────────────────────────
def _lane_ingestion() -> list[dict]:
    """Watches the SHAPE of the layer distribution, not any single count."""
    checks: list[dict] = []
    payload, reason = _get_json(_WHATS_NEW + "?cb=shell")

    if payload is None:
        return [_check("ingest_readable", "/whats-new readable", None,
                       f"UNREADABLE ({reason}) — no ingestion conclusion drawn. "
                       "A feed we cannot read is not a feed that is stalled.",
                       critical=True)]

    items = payload.get("items") or []
    if not items:
        return [_check("ingest_items", "layer inventory present", None,
                       "/whats-new returned no items[] — shape changed or the "
                       "feed is empty; either way UNMEASURED, not zero.",
                       critical=True)]

    growing = [i for i in items if (i.get("status") or "") == "growing"]
    stalled = [i for i in items
               if (i.get("status") or "").lower() in _STALL_STATUSES]
    flagged = [i for i in items if i.get("known_issue")]

    # "the loader ran; no net new rows persisted" — succeeding and writing
    # nothing. Detected from the STATUS field, not by string-matching prose,
    # so a copy edit to status_reason cannot silently empty this check.
    wrote_nothing = [i for i in items
                     if (i.get("status") or "") == "refreshed"
                     and not (i.get("added") or 0)]

    checks.append(_check(
        "layers_inventoried", "layer inventory readable", True,
        f"{len(items)} layers reported: {len(growing)} growing, "
        f"{len(wrote_nothing)} ran-but-wrote-nothing, {len(stalled)} stalled, "
        f"{len(flagged)} carrying a named open finding."))

    # Born red: a named finding is real, unstarted work. This does not go green
    # by the layer resuming growth — it goes green when the finding is closed.
    checks.append(_check(
        "named_findings_open", "no unresolved ingestion findings",
        len(flagged) == 0,
        ("no layer carries a known_issue" if not flagged else
         "open findings: " + "; ".join(
             f"{i.get('category')} — {str(i.get('known_issue'))[:90]}"
             for i in flagged[:4])),
        critical=True))

    # The quiet-but-succeeding cluster. Not automatically a defect (a loader
    # legitimately writes nothing when upstream published nothing), so this is
    # reported as a WATCH with the names attached rather than scored as a fail.
    checks.append(_check(
        "loader_write_effectiveness", "loaders that run also write",
        None if not wrote_nothing else False,
        ("every recently-run loader persisted rows" if not wrote_nothing else
         f"{len(wrote_nothing)} layer(s) ran and persisted 0 net rows: " +
         ", ".join(str(i.get("category")) for i in wrote_nothing[:6]) +
         " — succeeding-while-writing-nothing is the failure mode that looks "
         "healthiest from outside; confirm upstream truly published nothing")))

    # freshness_measurable is the layer's own admission that it cannot be
    # judged. Counting an unjudgeable layer as healthy is how a 159-day gap
    # goes unreported.
    unjudgeable = [i for i in items if i.get("freshness_measurable") is False]
    checks.append(_check(
        "freshness_judgeable", "every layer declares a staleness basis",
        len(unjudgeable) == 0,
        ("all layers are judgeable" if not unjudgeable else
         f"{len(unjudgeable)} layer(s) declare no staleness threshold, so they "
         "can never be flagged overdue: " +
         ", ".join(str(i.get("category")) for i in unjudgeable[:5]))))

    return checks


# ── lane 2 · product surfacing ───────────────────────────────────────────────
def _lane_product_surfacing() -> list[dict]:
    """Do brain-authored platform items actually reach the public page?

    The owner's report was that new products the brain creates never appear.
    /whats-new already publishes the answer in its own payload — platform,
    platform_pending and platform_withheld — so this lane reads the withholding
    reason rather than inferring absence from an empty list. An empty `platform`
    with a stated reason is a DIFFERENT state from an empty one without.
    """
    checks: list[dict] = []
    payload, reason = _get_json(_WHATS_NEW + "?cb=shell")

    if payload is None:
        return [_check("platform_readable", "/whats-new readable", None,
                       f"UNREADABLE ({reason}) — surfacing not concluded.",
                       critical=True)]

    published = payload.get("platform") or []
    pending = payload.get("platform_pending")
    withheld = payload.get("platform_withheld")
    why = payload.get("platform_unavailable_reason")

    checks.append(_check(
        "platform_items_published", "approved platform items are rendering",
        bool(published),
        f"{len(published)} platform item(s) published"
        if published else
        f"nothing published; reason given: {why or 'NONE STATED'}",
        critical=True))

    # A queue that never drains is the reported symptom. Pending is not a
    # defect on its own — pending WITH nothing published is.
    stuck = bool(pending) and not published
    checks.append(_check(
        "approval_queue_drains", "the approval queue reaches the page",
        None if pending is None else (not stuck),
        "platform_pending is not reported by this feed" if pending is None else
        (f"{pending} item(s) pending and {len(published)} published — queue is "
         "moving" if not stuck else
         f"{pending} item(s) pending and NOTHING published: brain-authored "
         "products are being created and never surfaced. This is the reported "
         "symptom, and it is an approval/gate defect, not an ingestion one."),
        critical=True))

    # Withholding is CORRECT behaviour when a claim cannot be substantiated —
    # but it must always be accompanied by a reason, or the page silently
    # shrinks and reads as "nothing shipped".
    checks.append(_check(
        "withholding_states_a_reason", "withheld items explain themselves",
        None if not withheld else bool(why),
        "nothing withheld" if not withheld else
        (f"{withheld} withheld, reason: {str(why)[:120]}" if why else
         f"{withheld} item(s) withheld with NO stated reason — a page that "
         "silently drops items is indistinguishable from a page with no news")))

    # ★ THE CHECK THE FIRST DRAFT OF THIS LANE MISSED. The reported symptom was
    # never "the section is empty" — it renders nine cards. It was "the NEW
    # products the brain creates never appear". Presence is not freshness: a
    # section pinned to nine months-old cards passes every count-based check
    # while being exactly the failure being reported. Age of the NEWEST item is
    # the only thing that separates a live feed from a nicely-rendered archive.
    #
    # platform_as_of is the GENERATION time (always ~now) and must never be
    # used here — it would render a frozen list as perpetually fresh.
    newest_days = None
    if published:
        import datetime as _dt
        stamps = []
        for it in published:
            raw = it.get("announced")
            if not raw:
                continue
            try:
                s = str(raw).replace("Z", "+00:00")
                d = _dt.datetime.fromisoformat(s)
                stamps.append(d.date() if hasattr(d, "date") else d)
            except Exception:  # noqa: BLE001 - unparseable stamp is UNMEASURED
                continue
        if stamps:
            newest_days = (_dt.date.today() - max(stamps)).days

    checks.append(_check(
        "new_products_still_arriving", "the newest platform item is recent",
        None if newest_days is None else (newest_days <= _PRODUCT_STALE_DAYS),
        "no parseable `announced` stamp on any published item — item age is "
        "UNMEASURED, so this section cannot be certified live"
        if newest_days is None else
        (f"newest platform item announced {newest_days}d ago "
         f"({len(published)} published)" if newest_days <= _PRODUCT_STALE_DAYS
         else
         f"newest platform item is {newest_days}d old across {len(published)} "
         f"published cards. The section RENDERS but has stopped RECEIVING: "
         "brain-authored products are not reaching the page. This is the "
         "reported symptom, and every count-based check above passes while it "
         "is true."),
        critical=True))

    return checks


# ── lane 3 · signal integrity ────────────────────────────────────────────────
def _lane_signal_integrity() -> list[dict]:
    """★ THE ONE THAT MATTERS: is delta_7d comparing two different formulas?

    /api/v1/dcpi/trending differences today's excess_power_score against the
    most recent dcpi_daily_snapshots row at least 7 days old. That query is
    correct ONLY if the scoring method is constant across the window. If
    method_version moved inside it, every published "7-day market move" is
    really the gap between two formulas — a fabricated signal that reads as
    news, which is strictly worse than a frozen one.

    Checked against the DB rather than the endpoint, because the endpoint
    cannot expose what it does not know it is mixing.
    """
    checks: list[dict] = []

    try:
        with _conn() as c, c.cursor() as cur:
            # Distinct methods present across the exact window the delta spans.
            cur.execute("""
                SELECT DISTINCT method_version
                  FROM dcpi_daily_snapshots
                 WHERE snapshot_date >= CURRENT_DATE - INTERVAL '8 days'
                   AND method_version IS NOT NULL
            """)
            methods = sorted(r[0] for r in cur.fetchall())

            cur.execute("""
                SELECT MAX(snapshot_date), COUNT(*)
                  FROM dcpi_daily_snapshots
                 WHERE snapshot_date >= CURRENT_DATE - INTERVAL '8 days'
            """)
            row = cur.fetchone()
            newest, rows_8d = (row[0], row[1]) if row else (None, 0)
    except Exception as e:  # noqa: BLE001
        return [_check("dcpi_window_readable", "snapshot window readable", None,
                       f"UNREADABLE ({type(e).__name__}) — method drift NOT "
                       "concluded either way.", critical=True)]

    # THE check. One method across the window = the delta is a real move.
    checks.append(_check(
        "method_constant_across_window",
        "delta_7d spans a single scoring method",
        None if not methods else (len(methods) == 1),
        "no method_version recorded in the window — UNMEASURED, and a delta "
        "whose formula provenance is unknown cannot be called a market move"
        if not methods else
        (f"single method across the window ({methods[0]}) — published deltas "
         "are genuine score movement"
         if len(methods) == 1 else
         f"★ {len(methods)} METHODS INSIDE THE 7-DAY WINDOW: {methods}. Every "
         "published delta_7d is the distance between two different formulas "
         "rendered as a market move. Withhold the ticker until the window "
         "clears the change, or difference within-method only."),
        critical=True))

    # The snapshot must actually be current, or week_ago silently widens from
    # "7 days" into "however long since writes stopped" while keeping its name.
    stale_days = None
    if newest is not None:
        try:
            import datetime as _dt
            stale_days = (_dt.date.today() - newest).days
        except Exception:  # noqa: BLE001
            stale_days = None

    checks.append(_check(
        "snapshot_window_current", "snapshot table is being written",
        None if stale_days is None else (stale_days <= 1),
        "newest snapshot_date unreadable — UNMEASURED" if stale_days is None
        else (f"newest snapshot {newest} ({stale_days}d old), {rows_8d} rows "
              "in window" if stale_days <= 1 else
              f"newest snapshot is {stale_days}d old: the week_ago CTE now "
              "reaches back further than 7 days while still publishing the "
              "result as delta_7d"),
        critical=True))

    # Distribution shape. All-negative movers is not proof of a bug, but it is
    # not the shape of a genuine top-5-by-absolute-move either, and it is the
    # observation that opened this investigation. Reported as a WATCH.
    payload, reason = _get_json(_TRENDING + "?cb=shell")
    if payload is None:
        checks.append(_check("mover_balance", "movers include both directions",
                             None, f"UNREADABLE ({reason})"))
    else:
        rows = payload.get("trending") or []
        deltas = [r.get("delta_7d") for r in rows
                  if isinstance(r.get("delta_7d"), (int, float))]
        ups = [d for d in deltas if d > 0]
        downs = [d for d in deltas if d < 0]
        checks.append(_check(
            "mover_balance", "movers include both directions",
            None if not deltas else (bool(ups) and bool(downs)),
            "no numeric deltas returned — UNMEASURED" if not deltas else
            (f"{len(ups)} up / {len(downs)} down — mixed, as a genuine "
             "absolute-move ranking should be"
             if ups and downs else
             f"ALL {len(deltas)} movers point the same direction "
             f"({'up' if ups else 'down'}). A top-N-by-ABSOLUTE-move that is "
             "unidirectional usually means a systematic shift — a method "
             "change, or an input that went to zero — not N independent "
             "market moves. Cross-read with method_constant_across_window.")))

    return checks


# ── lane 4 · cron honesty ────────────────────────────────────────────────────
def _lane_cron_honesty() -> list[dict]:
    """Born red until the freshness crons can actually report failure.

    Every step in facility-snapshot-daily.yml is `curl -sS ... || true`. curl
    without --fail exits 0 on 4xx/5xx, and `|| true` discards whatever survives
    that. The workflow therefore reports success whether the endpoint inserted
    315 rows or returned 402. This lane cannot be satisfied from inside the
    app — it is a work order against the workflow files, and it stays red until
    those steps use --fail-with-body.
    """
    return [_check(
        "crons_can_fail", "freshness crons surface a failing endpoint", False,
        "WORK ORDER (not an app defect): facility-snapshot-daily.yml uses "
        "`curl -sS ... || true` on every step. Without --fail, a 402 or 500 "
        "exits 0 and the run goes green. Eight consecutive green runs are "
        "therefore evidence of nothing; the only reason the DCPI snapshot is "
        "known to work is that rows_inserted was read out of the log body. "
        "Fix: --fail-with-body, and drop `|| true` where the step is load-"
        "bearing. Until then no green checkmark on that workflow is evidence.",
        critical=True)]


_LANES = [
    ("ingestion_liveness", "Ingestion liveness", _lane_ingestion),
    ("product_surfacing", "Brain product surfacing", _lane_product_surfacing),
    ("signal_integrity", "DCPI signal integrity", _lane_signal_integrity),
    ("cron_honesty", "Cron failure visibility", _lane_cron_honesty),
]


def _build() -> dict:
    # _safe_lane returns the CHECKS list (crash -> a single indeterminate
    # check), not a lane envelope. The lane dict and its verdict are assembled
    # here, from _lane_verdict, so the three-valued semantics come from the
    # canonical helper rather than from a second implementation.
    lanes = []
    for lid, name, fn in _LANES:
        checks = _safe_lane(fn)
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    # Verdict tokens come from _lane_verdict and are FAIL / ? / PASS. They are
    # NOT "RED"/"GREEN": pinning invented literals here is exactly how the
    # registry board shipped a comparison that could never match, three times.
    failing = [ln for ln in lanes if ln["verdict"] == "FAIL"]
    unknown = [ln for ln in lanes if ln["verdict"] == "?"]
    return {
        "ok": True,
        "shell": "freshness_master_shell",
        "lanes": lanes,
        "failing_lanes": [ln["id"] for ln in failing],
        "indeterminate_lanes": [ln["id"] for ln in unknown],
        "verdict": ("FAIL" if failing else ("?" if unknown else "PASS")),
        "note": (
            "Three-valued by construction: a check that could not be read is "
            "None, never False. UNREADABLE IS NOT A FINDING. Lanes born red "
            "are work orders, not regressions."),
    }


@freshness_master_shell_bp.route("/api/v1/admin/freshness", methods=["GET"])
def freshness_board():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, disabled=True,
                       reason="FRESHNESS_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_build()), 200


@freshness_master_shell_bp.route("/api/v1/admin/freshness/master-tick",
                                 methods=["POST"])
def freshness_tick():
    if _disabled():
        return jsonify(ok=False, disabled=True), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    built = _build()
    return jsonify(ok=True, verdict=built["verdict"],
                   failing_lanes=built["failing_lanes"],
                   indeterminate_lanes=built["indeterminate_lanes"],
                   lanes=[{"id": ln["id"], "verdict": ln.get("verdict")}
                          for ln in built["lanes"]]), 200
