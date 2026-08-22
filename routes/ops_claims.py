"""routes/ops_claims.py — the PUBLIC, keyless claims feed (Claim Loop step 5).

WHY THIS EXISTS (2026-08-22)
----------------------------
Step 1 (routes/claim_ledger.py, #3045) made every headline number, fix and
post a pre-registered CLAIM with an expectation and a horizon, and the
verifier stamps each one confirmed / refuted / unobserved at horizon — never
the author. That ledger is admin-only. A claim loop only the owner can read
is a diary. The point is that an agent, or a human, can check what DC Hub
said about itself against what happened, without a key, exactly the way
/api/v1/ops/deadman already lets them check whether the feeds ran.

So this is the dead-man feed's sibling: keyless, `Cache-Control: no-store`,
under the same /api/v1/ops/ prefix (already edge-bypassed — verified
`cf-cache-status: DYNAMIC` on /api/v1/ops/leader), and it documents its own
shape IN the response (`shape`) so nobody has to guess a field name.

THE RULES — every one is mutation-tested in tests/test_ops_claims_feed.py
  * `week` is the CURRENT ISO week, Monday 00:00Z → as_of, as a COHORT over
    shipped_at: every count in it is over the claims SHIPPED this week,
    whatever their outcome is now. A claim shipped last week and judged this
    week belongs to last week's cohort.
  * refuted_kept = outcome 'refuted' (the owner stood by it). retracted =
    outcome 'retracted' (the owner withdrew it; claim_ledger.retract()
    overwrites a refutation and keeps the prior verdict in the evidence).
    A retracted claim is therefore never counted in refuted_kept.
  * median_event_to_served_hours = median of (shipped_at − regime.as_of) in
    hours over the cohort's claims that carry a parseable as_of. NULL when
    there is no sample — null is "not measured", never 0.
  * granted_action_classes reads step 2's brain_action_classes (granted =
    TRUE). When that table is absent the value is 0 and the basis says so.
  * claims[] is the ledger, honest and complete: the statement is the
    literal claim text, refutations and retractions included, newest
    activity first. Only SHIPPED claims are public — a pre-registered post
    is a draft until it ships, and the media producer registers the post
    text BEFORE the share goes out.
  * Kill switch OPS_CLAIMS_DISABLE=1 answers 404, never 5xx: the CF worker
    reads any 5xx from Railway as a dead origin and fails the site over to
    the stale Render backend. A ledger read failure answers 200 with
    ok=false and NULL week/claims — never a fabricated zero.

Surface:
  GET /api/v1/ops/claims?limit=50&since=<ISO-8601>   keyless · no-store
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import statistics

from flask import Blueprint, jsonify, request

from routes import claim_ledger as _ledger

logger = logging.getLogger(__name__)
ops_claims_bp = Blueprint("ops_claims", __name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# The one column list, shared by the cohort read and the feed read so the
# row decoder can never disagree with the SQL about a position.
_COLS = ("id, kind, subject, statement, regime, shipped_at, outcome, "
         "outcome_at, superseded_by")

_COHORT_SQL = (
    "SELECT " + _COLS +
    "  FROM brain_predictions_log "
    " WHERE source_layer = %s AND shipped_at >= %s AND shipped_at <= %s")

SHAPE = {
    "top": ("{ok, generated_at, week{...}, claims[], count, limit, since, "
            "since_mode, shape}"),
    "week": {
        "week_start": "Monday 00:00Z of the current ISO week (inclusive)",
        "week_end": ("the following Monday 00:00Z (exclusive) — the calendar "
                     "boundary; the counts cover week_start → as_of"),
        "as_of": "when this response was computed (UTC); the cohort's upper bound",
        "shipped": ("claims with shipped_at in [week_start, as_of] — the COHORT "
                    "every other week count is over"),
        "confirmed": ("cohort claims whose outcome is 'confirmed' — judged at "
                      "horizon by the verifier, never by the author"),
        "refuted_kept": ("cohort claims whose outcome is 'refuted' and that the "
                         "owner stood by; a retraction overwrites the outcome, "
                         "so a retracted claim is never counted here"),
        "retracted": ("cohort claims whose outcome is 'retracted' — the owner "
                      "withdrew it; superseded_by names the replacement claim "
                      "when there is one"),
        "unobserved": ("cohort claims whose instrument never measured inside "
                       "2x horizon — a gap, not a verdict"),
        "open": "cohort claims with no outcome yet (horizon not reached)",
        "median_event_to_served_hours": (
            "median over the cohort of (shipped_at − regime.as_of) in hours — "
            "how long after the underlying event the claim was served; null "
            "when there is no sample (null = not measured, never 0)"),
        "median_event_to_served_samples": (
            "how many cohort claims carried a parseable regime.as_of"),
        "granted_action_classes": (
            "rows of brain_action_classes with granted = TRUE — step 2's "
            "human-granted action classes; 0 with basis 'table absent' until "
            "that table exists"),
        "granted_action_classes_basis": "what granted_action_classes was read from",
        "brain_prs_with_detector": (
            "OPTIONAL — present only when step 4's brain_pr_carries_detector "
            "is importable: {with_detector, checked, unknown, prs, basis} over "
            "this week's merged brain PRs (brain_merge_reconciliation); absent "
            "means the instrument is absent, not that the count is 0"),
    },
    "claim": ("{id, kind, subject, statement, regime{as_of,...}, shipped_at, "
              "outcome, outcome_at, superseded_by}"),
    "claim_fields": {
        "id": "ledger row id (brain_predictions_log)",
        "kind": "one of kinds",
        "subject": "what the claim is about (canon:public.facilities, finding:<url>, social_media_posts:<id>, ...)",
        "statement": "the LITERAL claim text as registered — nothing stripped",
        "regime": "what the number was relative to when it shipped; always carries as_of",
        "shipped_at": "when the artefact went out — the horizon clock starts here",
        "outcome": "confirmed | refuted | retracted | unobserved | null (open, horizon not reached)",
        "outcome_at": "when the outcome was stamped; null while open",
        "superseded_by": "id of the claim that replaced a retracted one; null otherwise",
    },
    "kinds": _ledger.KINDS,
    "outcomes": _ledger.OUTCOMES,
    "params": {
        "limit": f"claims[] length, default {DEFAULT_LIMIT}, max {MAX_LIMIT}",
        "since": ("ISO-8601; keeps claims with outcome_at >= since OR "
                  "shipped_at >= since; unparseable values are ignored and "
                  "since_mode says so"),
    },
    "order": "claims[] newest activity first: max(shipped_at, outcome_at) desc",
    "visibility": ("shipped claims only; registered-but-unshipped claims are "
                   "not public until they ship"),
    "on_failure": ("ok=false with week=null and claims=null plus an error — a "
                   "read that failed is null, never 0"),
    "kill_switch": "OPS_CLAIMS_DISABLE=1 → 404 (never 5xx)",
    "admin_view": "/api/v1/brain/claims (key required) carries the expectation and evidence",
}


# ── switches ─────────────────────────────────────────────────────────────

def _disabled() -> bool:
    return os.environ.get("OPS_CLAIMS_DISABLE", "0") == "1"


# ── pure helpers (no I/O; these are what the tests pin) ──────────────────

def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _aware(v):
    """A tz-aware datetime from a datetime or an ISO string; None otherwise."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            v = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(v, _dt.datetime):
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=_dt.timezone.utc)
    return v


def parse_since(raw):
    """-> (datetime | None, mode). ISO-8601 only; 'Z' accepted. An
    unparseable value is IGNORED (not clamped, not defaulted) and named in
    since_mode so the caller can see its filter did not apply."""
    if not (raw or "").strip():
        return None, "none"
    dt = _aware(raw)
    if dt is None:
        return None, "ignored: unparseable (use ISO-8601)"
    return dt, "iso"


def week_bounds(now: _dt.datetime):
    """(week_start, week_end): Monday 00:00Z of now's ISO week, and the
    following Monday 00:00Z (exclusive)."""
    now = _aware(now)
    start = (now - _dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start, start + _dt.timedelta(days=7)


def hours_between(later, earlier):
    """(later − earlier) in hours, or None when either side is unreadable."""
    a, b = _aware(later), _aware(earlier)
    if a is None or b is None:
        return None
    return (a - b).total_seconds() / 3600.0


def week_stats(rows, week_start, now) -> dict:
    """The week block from claim rows (dicts with datetime shipped_at /
    outcome_at and a dict regime). Filters to the cohort itself, so a row
    outside [week_start, now] is excluded even if the SQL let it through."""
    ws, now = _aware(week_start), _aware(now)
    cohort = []
    for r in rows or []:
        sa = _aware(r.get("shipped_at"))
        if sa is not None and ws <= sa <= now:
            cohort.append(r)
    by = {"confirmed": 0, "refuted": 0, "retracted": 0, "unobserved": 0}
    open_ = 0
    samples = []
    for r in cohort:
        oc = r.get("outcome")
        if oc in by:
            by[oc] += 1
        elif oc is None:
            open_ += 1
        regime = r.get("regime") if isinstance(r.get("regime"), dict) else {}
        h = hours_between(r.get("shipped_at"), regime.get("as_of"))
        if h is not None:
            samples.append(h)
    median = round(statistics.median(samples), 2) if samples else None
    _, we = week_bounds(ws)
    return {
        "week_start": ws.isoformat(),
        "week_end": we.isoformat(),
        "as_of": now.isoformat(),
        "shipped": len(cohort),
        "confirmed": by["confirmed"],
        "refuted_kept": by["refuted"],
        "retracted": by["retracted"],
        "unobserved": by["unobserved"],
        "open": open_,
        "median_event_to_served_hours": median,
        "median_event_to_served_samples": len(samples),
    }


def claims_sql(source_layer: str, since, limit: int):
    """(sql, params) for claims[]: shipped claims, newest activity first;
    `since` keeps rows whose outcome_at OR shipped_at is >= since."""
    sql = ("SELECT " + _COLS +
           "  FROM brain_predictions_log "
           " WHERE source_layer = %s AND shipped_at IS NOT NULL")
    params: list = [source_layer]
    if since is not None:
        sql += " AND (outcome_at >= %s OR shipped_at >= %s)"
        params += [since, since]
    sql += (" ORDER BY GREATEST(shipped_at, COALESCE(outcome_at, shipped_at)) DESC, "
            "id DESC LIMIT %s")
    params.append(int(limit))
    return sql, tuple(params)


def clamp_limit(raw, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    try:
        return max(1, min(cap, int(raw if raw is not None else default)))
    except (TypeError, ValueError):
        return default


def row_to_claim(r) -> dict:
    """Decode one _COLS row. Timestamps stay datetimes here (the week math
    needs them); public() renders them."""
    regime = r[4]
    if isinstance(regime, str):
        try:
            regime = json.loads(regime)
        except ValueError:
            regime = {"raw": regime}
    return {"id": r[0], "kind": r[1], "subject": r[2], "statement": r[3],
            "regime": regime if isinstance(regime, dict) else regime,
            "shipped_at": r[5], "outcome": r[6], "outcome_at": r[7],
            "superseded_by": r[8]}


def public(claim: dict) -> dict:
    out = dict(claim)
    out["shipped_at"] = _ledger._iso(claim.get("shipped_at"))
    out["outcome_at"] = _ledger._iso(claim.get("outcome_at"))
    return out


# ── reads ────────────────────────────────────────────────────────────────

def granted_action_classes(cur):
    """(count, basis). Step 2's table may not exist on this deploy: then
    0 with basis 'table absent' (the spec'd fail-soft), which is distinct
    from a read that FAILED — that is (None, 'read failed: …')."""
    try:
        cur.execute("SELECT to_regclass('public.brain_action_classes')")
        row = cur.fetchone()
        if not row or not row[0]:
            return 0, "table absent"
        cur.execute("SELECT COUNT(*) FROM brain_action_classes WHERE granted")
        row = cur.fetchone()
        return int((row or (0,))[0] or 0), "brain_action_classes WHERE granted = TRUE"
    except Exception as e:  # noqa: BLE001
        return None, f"read failed: {type(e).__name__}: {str(e)[:120]}"


# ── optional: brain PRs carrying a detector (Claim Loop step 4) ─────────
# Step 4 exposes brain_pr_carries_detector(pr_number) -> bool | None. Its
# module is not on main yet, so the import is lazy and the FIELD IS OMITTED
# when it is absent — an absent instrument is not a zero. The module path is
# an env knob so wiring it is one variable once it lands, and the predicate
# is cached per process because this is a keyless endpoint and the predicate
# may cost a GitHub read per PR.
_DETECTOR_MODULE_ENV = "OPS_CLAIMS_DETECTOR_MODULE"
_DETECTOR_MODULE_DEFAULT = "routes.brain_pr_detector_gate"
_DETECTOR_FN = "brain_pr_carries_detector"
_DETECTOR_MAX_PRS = 10
_DETECTOR_CACHE_S = 600
_DETECTOR_CACHE = {"key": None, "at": 0.0, "value": None}


def _detector_predicate():
    import importlib
    mod = os.environ.get(_DETECTOR_MODULE_ENV) or _DETECTOR_MODULE_DEFAULT
    try:
        fn = getattr(importlib.import_module(mod), _DETECTOR_FN, None)
    except Exception:  # noqa: BLE001 — absent module = absent instrument
        return None
    return fn if callable(fn) else None


def brain_prs_with_detector(cur, week_start, now, predicate=None):
    """-> dict | None. None means OMIT the field (no predicate importable)."""
    fn = predicate or _detector_predicate()
    if fn is None:
        return None
    basis = (f"brain_merge_reconciliation.merged_at in [week_start, as_of], "
             f"newest {_DETECTOR_MAX_PRS}; {_DETECTOR_FN}(pr) per PR")
    try:
        cur.execute("SELECT to_regclass('public.brain_merge_reconciliation')")
        row = cur.fetchone()
        if not row or not row[0]:
            return {"with_detector": None, "checked": 0, "unknown": 0,
                    "prs": None, "basis": "brain_merge_reconciliation absent"}
        cur.execute(
            "SELECT pr_number FROM brain_merge_reconciliation "
            " WHERE merged_at >= %s AND merged_at <= %s "
            " ORDER BY merged_at DESC LIMIT %s",
            (week_start, now, _DETECTOR_MAX_PRS))
        prs = [int(r[0]) for r in (cur.fetchall() or []) if r and r[0] is not None]
    except Exception as e:  # noqa: BLE001
        return {"with_detector": None, "checked": 0, "unknown": 0, "prs": None,
                "basis": f"read failed: {type(e).__name__}: {str(e)[:120]}"}
    with_det = checked = unknown = 0
    for pr in prs:
        try:
            v = fn(pr)
        except Exception:  # noqa: BLE001
            v = None
        if v is None:
            unknown += 1
        else:
            checked += 1
            with_det += 1 if v else 0
    return {"with_detector": with_det, "checked": checked, "unknown": unknown,
            "prs": len(prs), "basis": basis}


def _cached_detector_field(cur, week_start, now):
    import time as _t
    key = week_start.isoformat()
    c = _DETECTOR_CACHE
    if c["key"] == key and (_t.time() - c["at"]) < _DETECTOR_CACHE_S:
        return c["value"]
    value = brain_prs_with_detector(cur, week_start, now)
    c.update(key=key, at=_t.time(), value=value)
    return value


def read_feed(limit: int = DEFAULT_LIMIT, since=None, now=None) -> dict:
    """-> {ok, week, claims, error?}. One connection, reads only. Used by the
    route AND by /brain-live, which renders the same numbers."""
    if not _ledger._db_url():
        return {"ok": False, "error": "no database", "week": None, "claims": None}
    if not _ledger.ensure_schema():
        return {"ok": False, "error": "schema unavailable", "week": None,
                "claims": None}
    now = _aware(now) or utcnow()
    ws, _ = week_bounds(now)
    try:
        conn = _ledger._conn()
        try:
            # Reads only; autocommit so a failed statement can never leave
            # the next one inside an aborted transaction.
            try:
                conn.autocommit = True
            except Exception:  # noqa: BLE001
                pass
            cur = conn.cursor()
            cur.execute(_COHORT_SQL, (_ledger.SOURCE_LAYER, ws, now))
            cohort = [row_to_claim(r) for r in (cur.fetchall() or [])]
            sql, params = claims_sql(_ledger.SOURCE_LAYER, since, limit)
            cur.execute(sql, params)
            claims = [row_to_claim(r) for r in (cur.fetchall() or [])]
            granted, basis = granted_action_classes(cur)
            detector = _cached_detector_field(cur, ws, now)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[ops_claims] read failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}",
                "week": None, "claims": None}
    week = week_stats(cohort, ws, now)
    week["granted_action_classes"] = granted
    week["granted_action_classes_basis"] = basis
    if detector is not None:
        week["brain_prs_with_detector"] = detector
    return {"ok": True, "week": week, "claims": [public(c) for c in claims],
            "as_of": now.isoformat()}


# ── the route ────────────────────────────────────────────────────────────

def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


@ops_claims_bp.route("/api/v1/ops/claims", methods=["GET"])
def ops_claims():
    """PUBLIC, keyless. The shape is in the response — read `shape`."""
    if _disabled():
        return _no_store(jsonify(ok=False, error="disabled",
                                 note="OPS_CLAIMS_DISABLE=1")), 404
    limit = clamp_limit(request.args.get("limit"))
    since, since_mode = parse_since(request.args.get("since"))
    feed = read_feed(limit=limit, since=since)
    body = {
        "ok": bool(feed.get("ok")),
        "generated_at": feed.get("as_of") or utcnow().isoformat(),
        "week": feed.get("week"),
        "claims": feed.get("claims"),
        "count": len(feed["claims"]) if isinstance(feed.get("claims"), list) else None,
        "limit": limit,
        "since": since.isoformat() if since else None,
        "since_mode": since_mode,
        "shape": SHAPE,
    }
    if not feed.get("ok"):
        body["error"] = feed.get("error")
        body["basis"] = "ledger unavailable — week and claims are null, not 0"
    return _no_store(jsonify(body)), 200


def register_ops_claims(app) -> bool:
    app.register_blueprint(ops_claims_bp)
    return True
