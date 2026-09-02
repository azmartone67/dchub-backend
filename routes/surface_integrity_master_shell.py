"""Surface Integrity Master Shell — GET /api/v1/admin/surface-integrity
tick: /api/v1/admin/surface-integrity/master-tick
kill: SURFACE_INTEGRITY_SHELL_DISABLE=1

Built 2026-08-08 out of a single afternoon in which FOUR confident diagnoses were
wrong, every one of them because a reading was trusted without checking which
LAYER produced it. This shell exists to make that specific mistake mechanical.

WHY THIS EXISTS:

  1. A CACHED HEADER IS NOT THE CURRENT POLICY. /ai was reported broken — two
     concatenated Cache-Control policies ending in stale-while-revalidate=86400,
     the exact signature of the CF Pages rule-merge bug. It was real once. It
     had already been fixed. The reading came from a response stamped
     cf-cache-status: HIT, age: 62 — an edge copy of a deployment that predated
     the fix, serving a policy that no longer existed in _headers at all.
     A HIT can describe a past that is no longer true, and nothing checked.
  2. SO THE GUARD MUST REFUSE TO JUDGE A HIT. Lane 1 renders None on any
     cf-cache-status HIT — not pass, not fail. It is the only honest verdict
     available from a cached response, and it is the rule the humans got wrong.
  3. A SELF-CALL REFUSED BY OUR OWN GATE IS INVISIBLE UNTIL SOMETHING WATCHES.
     SH52-051: the data-sync 'Energy discovery per market' step is 402'd by our
     own tier gate for a large share of its 23 markets. Two gates were cleared
     by inspection (route auth and free_tier_gate both honour X-Admin-Key); the
     emitter is still unidentified, and the leading suspect returns 429 while
     the symptom is 402. That gap is NOT resolved, so this lane is born red and
     names the unresolved contradiction rather than asserting a cause.

★ THE RULE: A READING WHOSE PROVENANCE IS UNKNOWN IS UNMEASURED. Three-valued
throughout — True (agrees), False (contradicts), None (could not read, or read
something that cannot be trusted to describe now). A cache HIT, an unreachable
host and an absent field all render None with a stated reason.

Lanes born red are work orders, not regressions.
"""
from __future__ import annotations

import datetime
import os

from flask import Blueprint, jsonify

# Imported, never copied — honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _lane_verdict, _safe_lane)

surface_integrity_master_shell_bp = Blueprint(
    "surface_integrity_master_shell", __name__)

_UA = "dchub-surface-integrity-shell/1.0 (+https://dchub.cloud)"
_TIMEOUT = 15

# Pages that must revalidate on every load. A deploy that cannot reach the user
# is indistinguishable from a deploy that never happened — the 2026-06-20
# incident had a paid user on a stale gating.js for 24h, still redacting MW
# after the fix shipped.
_STRICT_PAGES = ["/ai", "/map", "/land-power-map"]

# The directive that caused it. Any value > 0 on a strict page reopens the
# long-stale window; the check reads for its PRESENCE, not a threshold, because
# "how stale is acceptable" is exactly the argument that let it back in.
_FORBIDDEN_ON_STRICT = "stale-while-revalidate=86400"


def _disabled() -> bool:
    return os.environ.get("SURFACE_INTEGRITY_SHELL_DISABLE", "") == "1"


def _head(path: str, cache_bust: bool = True):
    """Returns (headers_dict, None) or (None, reason). NEVER raises.

    ★ Cache-busts by default. A shell that polls the same URL every tick would
    be answered from the edge every time after the first, and would then be
    grading a snapshot instead of the live policy — the precise failure this
    file was written about.
    """
    url = "https://dchub.cloud" + path
    if cache_bust:
        url += ("&" if "?" in path else "?") + "cbshell=1"
    try:
        import requests as _rq
        r = _rq.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT,
                    allow_redirects=True)
        if not 200 <= r.status_code < 300:
            return None, f"HTTP {r.status_code}"
        return {k.lower(): v for k, v in r.headers.items()}, None
    except Exception as e:  # noqa: BLE001 - any failure is UNREADABLE
        return None, f"{type(e).__name__}"


# ── lane 1 · cache policy integrity ──────────────────────────────────────────
def _lane_cache_policy() -> list[dict]:
    """Regression guard on the strict-revalidation pages.

    This defect was fixed before anyone watched it. The lane's job is to keep it
    fixed and — more importantly — to model the reading discipline that made the
    diagnosis wrong in the first place.
    """
    checks: list[dict] = []

    for path in _STRICT_PAGES:
        headers, reason = _head(path)
        cid = path.strip("/").replace("/", "_") or "root"

        if headers is None:
            checks.append(_check(
                f"cache_{cid}", f"{path} cache policy", None,
                f"UNREADABLE ({reason}) — policy NOT concluded.",
                critical=True))
            continue

        cf = (headers.get("cf-cache-status") or "").upper()
        cc = headers.get("cache-control") or ""

        # ★ THE CHECK THIS SHELL EXISTS FOR. A HIT is an edge copy that may
        # predate the current deployment; it can carry a policy that no longer
        # exists in _headers. Grading it either way asserts something about NOW
        # from evidence about THEN. The only honest verdict is None.
        if cf == "HIT":
            checks.append(_check(
                f"cache_{cid}", f"{path} cache policy", None,
                f"cf-cache-status=HIT (age={headers.get('age', '?')}) — this "
                "header is an EDGE COPY and may describe a deployment that no "
                "longer exists. NOT GRADED. Re-read on a MISS. Judging a HIT "
                "is how /ai was reported broken hours after it was fixed.",
                critical=True))
            continue

        # Concatenation signature: CF Pages merges Cache-Control across every
        # matching rule, so a per-page rule without `! Cache-Control` arrives
        # glued to the /* catch-all. Counting the directive is more robust than
        # matching the merged string, which changes whenever a rule is edited.
        merged = cc.lower().count("max-age=") > 1
        stale = _FORBIDDEN_ON_STRICT in cc.replace(" ", "")

        checks.append(_check(
            f"cache_{cid}", f"{path} serves ONE revalidating policy",
            not (merged or stale),
            f"clean single policy ({cc[:70]})" if not (merged or stale) else
            ("MERGED: " + cc[:110] + " — a per-page rule is missing its "
             "`! Cache-Control` unset and is arriving glued to the /* "
             "catch-all" if merged else
             f"stale-while-revalidate=86400 on a strict page ({cc[:80]}) — "
             "reopens the 24h stale window that served a paid user redacted "
             "MW for a day after the fix shipped"),
            critical=True))

    return checks


# ── lane 2 · self-call gate (SH52-051) ───────────────────────────────────────
def _lane_self_call_gate() -> list[dict]:
    """Born red. Our own ingestion workflow is refused by our own gate.

    Deliberately does NOT name a cause. Two gates were cleared by inspection —
    energy_auto_discovery._check_auth accepts X-Admin-Key AND X-Internal-Key,
    and free_tier_gate.enforce_free_tier carries an explicit X-Admin-Key bypass
    added 2026-05-26 for this exact class. The leading remaining suspect,
    api_monetization.monetization_before_request, has no admin bypass — but it
    returns 429 and the observed symptom is 402. Asserting it anyway would be
    the same mistake this shell was built to prevent.
    """
    return [_check(
        "sh52_051_self_call_402", "ingestion self-calls clear our own gate",
        False,
        "OPEN (SH52-051). The data-sync 'Energy discovery per market' step is "
        "402'd by our own tier gate on a large share of its 23 markets, and the "
        "affected layer has not grown in 159 days. CLEARED by inspection: "
        "energy_auto_discovery._check_auth (accepts X-Admin-Key and "
        "X-Internal-Key) and free_tier_gate.enforce_free_tier (explicit "
        "X-Admin-Key bypass, 2026-05-26). UNRESOLVED CONTRADICTION: the "
        "remaining suspect (api_monetization.monetization_before_request, no "
        "admin bypass) returns 429, but the symptom is 402 — so either a fourth "
        "gate exists or the theory is wrong. NEXT STEP: one authenticated GET "
        "to /api/energy-discovery/power-plants?market=phoenix, reading the "
        "response BODY and HEADERS — our gates stamp their identity in the "
        "payload, so the 402 will name its own emitter. Do NOT patch on the "
        "429 theory. Precedent for the fix, if it lands there: the bypass "
        "already at free_tier_gate.py ~613.",
        critical=True)]


# ── lane 3 · SEO measurement ─────────────────────────────────────────────────
# GSC finalises a day over ~72h and the ingest cron runs daily, so the newest
# site row is normally 2-3 days old: 4 is the allowance. 5 rows is the floor
# that proves a SERIES was read, not a single row; the window is 14d so that
# floor is reachable at the allowance (a 7d window at 4d lag holds 3-4 rows).
_SEO_MAX_AGE_DAYS = 4
_SEO_MIN_ROWS = 5
_SEO_WINDOW_DAYS = 14


def _as_date(v):
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _read_seo_series() -> dict:
    """In-process read of the service-account GSC site series. Never HTTP: a
    self-call through the edge grades a cache and a timeout budget, not the
    data. Raises when the series cannot be read; the lane renders that '?'."""
    from routes.gsc_performance import site_series
    return site_series(days=_SEO_WINDOW_DAYS)


def _lane_seo_measurement(today=None) -> list[dict]:
    """Was born red as a hardcoded UNMEASURED string; now reads the series.

    ★ 2026-09-02. This lane said "rank/impression truth lives in Google
    Search Console ... behind interactive auth unavailable to this process
    ... the honest state is UNMEASURED", and had said so since 2026-08-08.
    Meanwhile routes/gsc_performance.py had a service-account daily ingest
    (/api/gsc/status: verified true, permission siteFullUser; cron green
    2026-09-01T06:43Z) and the read route held 247 site-day rows, newest
    2026-08-29, on 2026-09-02 00:24Z. A lane that cannot see its own
    measurement is a false red on a health board — the mirror image of the
    fabricated-metric defect the old docstring warned about, and just as
    misleading to whoever reads the board.

    Google: PASS iff coverage.newest is <= _SEO_MAX_AGE_DAYS old AND the
    window holds >= _SEO_MIN_ROWS rows; FAIL otherwise; '?' when the series
    cannot be read at all (an unreadable series is not a stale one).
    Bing: stays UNMEASURED, separately labelled and non-critical — Bing
    Webmaster Tools has no API this process can read, and IndexNow acceptance
    is delivery, not standing. It can neither pass nor fail the lane.
    """
    checks = []
    try:
        series = _read_seo_series()
    except Exception as e:  # noqa: BLE001 - unreadable is '?', never PASS
        checks.append(_check(
            "seo_gsc_series_current",
            "Google search standing is measured (SA-backed daily series)",
            None, f"series unreadable: {type(e).__name__}: {str(e)[:100]}",
            critical=True))
    else:
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        cov = (series or {}).get("coverage") or {}
        rows = (series or {}).get("rows") or []
        newest = _as_date(cov.get("newest"))
        age = (today - newest).days if newest else None
        fresh = age is not None and age <= _SEO_MAX_AGE_DAYS
        enough = len(rows) >= _SEO_MIN_ROWS
        checks.append(_check(
            "seo_gsc_series_current",
            "Google search standing is measured (SA-backed daily series)",
            bool(fresh and enough),
            (f"gsc_daily_performance site grain: newest {cov.get('newest')} "
             f"({'unknown' if age is None else age}d old, max "
             f"{_SEO_MAX_AGE_DAYS}), {len(rows)} rows in the last "
             f"{_SEO_WINDOW_DAYS}d (min {_SEO_MIN_ROWS}), "
             f"{cov.get('rows_stored')} stored since {cov.get('oldest')}. "
             "Read in-process via routes.gsc_performance.site_series, not "
             "over HTTP. Same series as GET /api/v1/seo/performance"
             "?dimension=site."),
            critical=True))
    checks.append(_check(
        "seo_bing_standing_measured",
        "Bing search standing is measured",
        None,
        "UNMEASURED by design, and separately from Google: Bing Webmaster "
        "Tools has no API this process can read, and IndexNow acceptance is "
        "delivery, not standing. Closes only with an owner-run BWT export. "
        "Non-critical so it can neither pass nor fail the lane.",
        critical=False))
    return checks


_LANES = [
    ("cache_policy", "Cache policy integrity", _lane_cache_policy),
    ("self_call_gate", "Self-call gate (SH52-051)", _lane_self_call_gate),
    ("seo_measurement", "SEO measurement currency", _lane_seo_measurement),
]


def _build() -> dict:
    # _safe_lane returns the CHECKS list (a crash becomes one indeterminate
    # check), not a lane envelope. The envelope and its verdict are assembled
    # here from _lane_verdict, whose tokens are FAIL / ? / PASS — NOT
    # RED/GREEN. Pinning invented literals is how a sibling board shipped a
    # comparison that could never match, three times.
    lanes = []
    for lid, name, fn in _LANES:
        checks = _safe_lane(fn)
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    failing = [ln for ln in lanes if ln["verdict"] == "FAIL"]
    unknown = [ln for ln in lanes if ln["verdict"] == "?"]
    return {
        "ok": True,
        "shell": "surface_integrity_master_shell",
        "lanes": lanes,
        "failing_lanes": [ln["id"] for ln in failing],
        "indeterminate_lanes": [ln["id"] for ln in unknown],
        "verdict": ("FAIL" if failing else ("?" if unknown else "PASS")),
        "note": (
            "A reading whose provenance is unknown is UNMEASURED. A cf-cache-"
            "status HIT is never graded — it may describe a deployment that no "
            "longer exists. Lanes born red are work orders, not regressions."),
    }


@surface_integrity_master_shell_bp.route("/api/v1/admin/surface-integrity",
                                         methods=["GET"])
def surface_integrity_board():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, disabled=True,
                       reason="SURFACE_INTEGRITY_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_build()), 200


@surface_integrity_master_shell_bp.route(
    "/api/v1/admin/surface-integrity/master-tick", methods=["POST"])
def surface_integrity_tick():
    if _disabled():
        return jsonify(ok=False, disabled=True), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    built = _build()
    return jsonify(ok=True, verdict=built["verdict"],
                   failing_lanes=built["failing_lanes"],
                   indeterminate_lanes=built["indeterminate_lanes"],
                   lanes=[{"id": ln["id"], "verdict": ln["verdict"]}
                          for ln in built["lanes"]]), 200
