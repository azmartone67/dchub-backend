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
def _lane_seo_measurement() -> list[dict]:
    """Born red, and honest about WHY it cannot self-satisfy.

    The last SEO read (2026-08-01: bingbot 93% = Bing Webmaster Tools, IndexNow
    healthy, 1 of 17 tracked queries in the top 10, an AI-citation cliff on
    06-24 that was NOT robots-related) is stale, and none of it can be
    re-measured from inside the app: rank and impression data live in Google
    Search Console and Bing Webmaster Tools, behind interactive auth this
    process does not hold. A lane that quietly grades itself on a proxy it CAN
    reach would manufacture a number for a question it never asked — which is
    the fabricated-metric defect, wearing a board as a disguise.
    """
    return [_check(
        "seo_measurement_current", "search standing is currently measured",
        False,
        "OPEN. Last measured 2026-08-01 and NOT re-verified since: bingbot 93% "
        "of crawl attributed to Bing Webmaster Tools, IndexNow healthy, 1 of 17 "
        "tracked queries in the top 10, AI-citation cliff on 06-24 (NOT "
        "robots-caused). Those numbers are a week old and must not be quoted as "
        "current. This lane CANNOT close itself: rank/impression truth lives in "
        "Google Search Console and Bing Webmaster Tools, behind interactive auth "
        "unavailable to this process. Closing it means either an owner-run "
        "export or a service-account credential — until then the honest state is "
        "UNMEASURED, and no in-app proxy may stand in for it.",
        critical=True)]


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
