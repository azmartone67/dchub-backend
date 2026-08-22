"""routes/claim_breaker.py — the claim-breaker gate (Claim Loop step 3).

ONE gate, five lie-class guards, a must-stay-green control.
=============================================================================

Step 1 (#3045) gave the claim LEDGER a place to record what we said and whether
it came true. Step 3 is the thing that runs BEFORE we say it: a single gate that
replays the five "lie classes" that have each shipped a false public number, so
the same shape cannot ship again.

The five guards already exist, scattered — this module does not re-implement
them, it CALLS them (re-grepped 2026-08-21; line numbers drift):

  1. rows != buildings      routes/media_fact_check_guard.check_facility_count_claims
                            (the 2026-08-17 "26,000 facilities" post: raw source
                            ROWS published as distinct BUILDINGS)
  2. partial / rolling week mcp_calls_deloop.canonical_external_complete_week_sql
                            (a WoW whose window includes the in-progress week)
  3. WoW outlier baseline   routes/weekly_series._baseline_outlier_flag
                            (a -71% headline that is one spike week, not a trend;
                            this file extends that flag to EVERY metric)
  4. renamed sentinel       routes/growth_funnel_master_shell._GENERIC_PLATFORMS
                            (a renamed generic bucket walking attribution to a
                            confident FALSE 100%)
  5. self-traffic           mcp_calls_deloop.{normalize_write_platform,
                            self_traffic_session_prefixes,_SCRIPT_INTERNAL_UA}
                            + the a_ci_origin lane (our own CI counted as demand)

  (+ a sixth, cheap text check for post/listing/canon: any numeric claim vs the
   live canon, via routes/media_claim_verify.verify_claims — the banned
   over-claim class: the inflated fifty-thousand facility count and the
   unverified M&A dollar aggregate.)

POSTURE BY KIND
---------------
  post / listing / canon  -> FAIL CLOSED. `ok=False` means the caller MUST NOT
                             ship (the publisher refuses the post; the canon
                             consumer withholds the copy).
  fact / score            -> REPORT ONLY. The caller never blocks; it stamps
                             the ledger `outcome=refuted` when a claim id is
                             known, and ships. `violations` still lists what
                             was found.

MUST-STAY-GREEN CONTROL (the QA-superuser canary shape)
-------------------------------------------------------
Every call also runs a known-good control through the SAME classes. If the
control FAILS, the gate cannot trust its own verdict, so it reports
`trusted=False` and `ok=True` — UNTRUSTED, not RED. A gate that cannot pass its
own control must not block shipping on its own say-so; it must say it is broken.
Callers ship on untrusted (and log it). This is deliberately asymmetric: the
control catches a gate that FALSELY FAILS good input (over-strict / crashing);
the mutation tests catch the other direction (falsely PASSES bad input).

KILL SWITCH
-----------
  CLAIM_BREAKER_DISABLE=1 -> breaker() returns {ok:True, trusted:False,
  disabled:True}; callers ship exactly as before. The admin status endpoint
  returns 404 (never 5xx) while disabled.

Never raises. Any internal failure degrades to UNTRUSTED (ship + log), because a
gate that can crash the publisher is worse than the lies it prevents.
"""
from __future__ import annotations

import logging
import os
import re as _re
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

claim_breaker_bp = Blueprint("claim_breaker", __name__)

# ── kinds ────────────────────────────────────────────────────────────────────
# Text kinds fail CLOSED; payload kinds are REPORT ONLY.
_TEXT_KINDS = ("post", "listing", "canon")
_PAYLOAD_KINDS = ("fact", "score")
KILL_SWITCH_ENV = "CLAIM_BREAKER_DISABLE"

# CI-origin share tolerance — mirrors growth_integrity_master_shell._CI_ORIGIN_MAX_SHARE.
_CI_ORIGIN_MAX_SHARE = 0.20


def _disabled() -> bool:
    return (os.environ.get(KILL_SWITCH_ENV) or "").strip().lower() in (
        "1", "true", "yes")


# ── control fixtures (known-good; monkeypatchable) ───────────────────────────
# The corrected LinkedIn/telemetry copy from PR #1827 (routes/competitor_recon.py
# moat_live_telemetry _COPY). Deliberately count-free of any canon metric, so it
# is clean through the text classes in BOTH a DB-up and a DB-down environment —
# the point of a control is that it stays green unless the machinery is broken.
_CONTROL_POST_TEXT = (
    "7 of 7 US ISOs report live demand and full fuel mix — "
    "PJM, ERCOT, MISO, CAISO, SPP, NYISO and ISO-NE — each "
    "carrying its source and the age of the reading, right "
    "now.\n\n"
    "The useful part is what we do NOT publish. Brazil's "
    "operator bundles gas, coal and oil into a single thermal "
    "figure, so DC Hub reports no gas share for Brazil rather "
    "than inventing the split. Australia and Singapore rank "
    "nowhere on the renewable table, because their feeds carry "
    "no full fuel mix. Demand refreshes hourly but EIA "
    "publishes the mix on a slower cycle, so every layer ships "
    "stamped with its own age instead of one flattering "
    "\"live\" badge over the whole payload.\n\n"
    "36 grids ranked side by side: the 7 US ISOs, Great "
    "Britain, 24 European bidding zones, Taiwan, Japan, South "
    "Korea and Brazil. Most data-center market data is a "
    "quarterly PDF; grid conditions are not quarterly."
)

# The two sentinels that MUST stay listed in _GENERIC_PLATFORMS. A frozen good
# copy: the renamed-sentinel control checks the COMPARATOR against this, never
# the live list, so a real live drift produces a fail-closed violation while the
# control stays green (letting the gate BLOCK rather than merely go untrusted).
_CONTROL_SENTINELS = ("mcp", "mcp-generic-client", "unknown", "")
_SENTINEL_REQUIRED = ("mcp", "mcp-generic-client")

# A steady weekly series (no outlier baseline) + a correctly-excluded partial
# week — the known-good payload for the fact/score classes.
_CONTROL_STEADY_WEEKS = [
    {"week_start": "2026-05-%02d" % (i * 7 + 1), "agents": 50,
     "calls": 2000 + i * 10, "status": "measured", "partial": False}
    for i in range(7)
]
_CONTROL_PAYLOAD = {
    "weeks": _CONTROL_STEADY_WEEKS,
    "robust_wow": {"note": "the robust (median-baseline) delta is quoted"},
    "wow": {"current_week_start": "2026-06-19"},
    "current_week_partial": {"partial": True, "excluded_from_delta": True,
                             "excluded_from_series": True,
                             "week_start": "2026-06-22"},
}
_CONTROL_SAMPLE_CTX = {"sample": {
    "platform": "claude", "session": "agent-abc-1234",
    "user_agent": "Claude-User/1.0 (+external)", "origin": "external",
    "counted_external": True}}


# ── class implementations (each: input, ctx -> [violation]) ─────────────────
# Every class is a pure-ish function used for BOTH the real evaluation and the
# control (same code, different input). It must NEVER raise for a caller — a
# raising class is caught in breaker() and rendered UNTRUSTED.

def _cls_rows_ne_buildings(text, ctx):
    """rows != buildings — the 2026-08-17 post class."""
    from routes.media_fact_check_guard import check_facility_count_claims
    out = check_facility_count_claims(text or "")
    viols = []
    for c in (out.get("over") or []):
        live = out.get("live_distinct")
        viols.append({
            "cls": "rows_ne_buildings",
            "detail": (
                f"{c.get('raw')} exceeds live distinct buildings "
                f"({live if live is not None else 'unreadable'}) — raw source "
                "ROWS published as buildings (say 'source records' for the pile)"),
        })
    return viols


def _cls_canon_numbers(text, ctx):
    """any numeric claim vs canon — the banned over-claim class (inflated
    fifty-thousand facility count, unverified M&A dollar aggregate)."""
    from routes.media_claim_verify import verify_claims
    cv = verify_claims(text or "")
    return [{"cls": "canon_overclaim", "detail": b}
            for b in (cv.get("blocks") or [])]


def _cls_renamed_sentinel(text, ctx):
    """renamed sentinel — both 'mcp' and 'mcp-generic-client' must stay listed.

    Reads the LIVE _GENERIC_PLATFORMS by default; the control passes a frozen
    good set via ctx['sentinels'] so the comparator can be exercised without the
    control inheriting a real live drift.
    """
    ctx = ctx or {}
    sentinels = ctx.get("sentinels")
    if sentinels is None:
        from routes.growth_funnel_master_shell import _GENERIC_PLATFORMS
        sentinels = _GENERIC_PLATFORMS
    present = {(s or "").lower() for s in sentinels if isinstance(s, str)}
    missing = [s for s in _SENTINEL_REQUIRED if s.lower() not in present]
    if missing:
        return [{
            "cls": "renamed_sentinel",
            "detail": (
                f"attribution sentinel(s) {missing} missing from "
                "_GENERIC_PLATFORMS — a renamed generic bucket walks "
                "attribution toward a confident FALSE 100%"),
        }]
    return []


def _cls_partial_week(payload, ctx):
    """partial / rolling week in a delta — the class complete-week SQL prevents."""
    from mcp_calls_deloop import canonical_external_complete_week_sql
    viols = []
    if not isinstance(payload, dict):
        return viols

    partial_starts = set()
    for w in (payload.get("weeks") or []):
        if isinstance(w, dict) and w.get("partial"):
            partial_starts.add(w.get("week_start"))
            if not w.get("excluded_from_delta"):
                viols.append({
                    "cls": "partial_week",
                    "detail": (f"partial week {w.get('week_start')} is not "
                               "excluded_from_delta — a partial week inside a "
                               "delta is arithmetic, not a business event"),
                })
    cwp = payload.get("current_week_partial")
    if isinstance(cwp, dict) and cwp.get("partial"):
        partial_starts.add(cwp.get("week_start"))
        if not cwp.get("excluded_from_delta"):
            viols.append({
                "cls": "partial_week",
                "detail": ("current_week_partial is not excluded_from_delta — "
                           "the in-progress week must never headline a WoW"),
            })

    wow = payload.get("wow") or {}
    cur_start = wow.get("current_week_start")
    if cur_start is not None and cur_start in partial_starts:
        viols.append({
            "cls": "partial_week",
            "detail": (f"WoW current_week_start {cur_start} is a partial week — "
                       "compare complete weeks (canonical_external_complete_"
                       "week_sql excludes the partial week by construction)"),
        })

    # Cross-check any declared basis SQL against the canonical builder (this is
    # the guard function this class exists to reuse).
    declared = payload.get("complete_week_sql")
    if declared is not None:
        try:
            canon_sql = canonical_external_complete_week_sql(
                int(payload.get("weeks_back") or 0))
        except Exception:
            canon_sql = None
        if canon_sql is not None and declared != canon_sql:
            viols.append({
                "cls": "partial_week",
                "detail": ("declared complete_week_sql does not match "
                           "canonical_external_complete_week_sql — a bespoke "
                           "window is how the partial week creeps back in"),
            })
    return viols


def _cls_wow(payload, ctx):
    """WoW headlined against an outlier baseline without the robust correction."""
    if not isinstance(payload, dict):
        return []
    weeks = payload.get("weeks")
    if not weeks:
        return []
    from routes.weekly_series import _baseline_outlier_flag
    flag = _baseline_outlier_flag(weeks)
    per_metric = flag.get("per_metric") or {}
    outliers = [m for m, info in per_metric.items()
                if info.get("checked") and info.get("is_outlier")]
    if not per_metric and flag.get("checked") and flag.get("is_outlier"):
        outliers = [flag.get("metric") or "calls"]
    if not outliers:
        return []
    # An honest payload publishes robust_wow alongside; only a payload that
    # headlines the naive WoW off an outlier baseline is the lie.
    if payload.get("robust_wow"):
        return []
    return [{
        "cls": "wow_outlier",
        "detail": (f"WoW headlined against an outlier baseline for {outliers} "
                   "with no robust_wow (trailing-median) correction — the delta "
                   "describes the spike week, not the trend"),
    }]


def _cls_self_traffic(payload, ctx):
    """self-traffic counted as external demand — all three predicates + a_ci_origin."""
    ctx = ctx or {}
    viols = []
    sample = ctx.get("sample")
    if not sample and isinstance(payload, dict):
        sample = payload.get("sample")

    if isinstance(sample, dict):
        counted_external = sample.get("counted_external", True)
        reasons = []
        # external_platform predicate (write-time twin: normalize -> dchub-internal)
        plat = sample.get("platform")
        if plat is not None:
            try:
                from mcp_calls_deloop import normalize_write_platform
                if normalize_write_platform(plat) == "dchub-internal":
                    reasons.append(f"platform {plat!r} normalizes to dchub-internal")
            except Exception:
                pass
        # external_session predicate (known self-traffic session prefixes)
        sess = sample.get("session") or sample.get("mcp_session_id")
        if sess:
            try:
                from mcp_calls_deloop import self_traffic_session_prefixes
                low = str(sess).lower()
                for p in self_traffic_session_prefixes():
                    if p and low.startswith(p.lower()):
                        reasons.append(f"session prefix {p!r} is known self-traffic")
                        break
            except Exception:
                pass
        # real_ua predicate (internal / raw-scripting UA)
        ua = sample.get("user_agent")
        if ua:
            try:
                from mcp_calls_deloop import _SCRIPT_INTERNAL_UA
                if _re.search("(%s)" % _SCRIPT_INTERNAL_UA, str(ua), _re.I):
                    reasons.append("user_agent matches an internal/script UA family")
            except Exception:
                pass
        # a_ci_origin lane (our own GitHub-Actions CI is not demand)
        origin = (sample.get("origin") or "").lower()
        if origin in ("github-actions", "github_actions", "ci") or sample.get("ip_is_ci"):
            reasons.append("originates from GitHub-Actions CI egress (a_ci_origin)")
        # a_ci_origin, live: measure the real share when a DB cursor is supplied.
        cur = ctx.get("cur")
        if cur is not None:
            try:
                from routes.agent_success_report import measure_ci_origin_share
                m = measure_ci_origin_share(cur)
                if m and (m.get("share") or 0) >= _CI_ORIGIN_MAX_SHARE:
                    reasons.append(
                        "CI-origin is %.0f%% of real calls (>= %.0f%%) — our own "
                        "CI is being counted as external demand"
                        % (100.0 * m["share"], 100.0 * _CI_ORIGIN_MAX_SHARE))
            except Exception:
                pass
        if reasons and counted_external:
            viols.append({"cls": "self_traffic", "detail": "; ".join(reasons)})

    # Always-on (weak) basis audit: a declared population/basis must name all
    # three predicate lanes AND the CI-origin exclusion.
    basis = None
    if isinstance(payload, dict):
        basis = payload.get("basis") or payload.get("population_filters")
    if basis is not None:
        text = basis if isinstance(basis, str) else " ".join(map(str, basis))
        low = text.lower()
        need = {
            "real_ua": ("real_ua", "user_agent", "is_real_external"),
            "external_session": ("external_session", "session"),
            "external_platform": ("external_platform", "platform", "is_real_external"),
        }
        missing = [k for k, keys in need.items() if not any(kk in low for kk in keys)]
        if not any(k in low for k in ("a_ci_origin", "ci-origin", "ci_origin",
                                      "github-actions", "github_actions")):
            missing.append("a_ci_origin")
        if missing:
            viols.append({
                "cls": "self_traffic",
                "detail": f"declared basis omits self-traffic exclusions: {missing}",
            })
    return viols


# name -> {kinds, fn, control()->(input, ctx)}
_CLASSES = {
    "rows_ne_buildings": {"kinds": _TEXT_KINDS, "fn": _cls_rows_ne_buildings,
                          "control": lambda: (_CONTROL_POST_TEXT, {})},
    "canon_overclaim":   {"kinds": _TEXT_KINDS, "fn": _cls_canon_numbers,
                          "control": lambda: (_CONTROL_POST_TEXT, {})},
    "renamed_sentinel":  {"kinds": _TEXT_KINDS, "fn": _cls_renamed_sentinel,
                          "control": lambda: (None, {"sentinels": _CONTROL_SENTINELS})},
    "partial_week":      {"kinds": _PAYLOAD_KINDS, "fn": _cls_partial_week,
                          "control": lambda: (_CONTROL_PAYLOAD, {})},
    "wow_outlier":       {"kinds": _PAYLOAD_KINDS, "fn": _cls_wow,
                          "control": lambda: (_CONTROL_PAYLOAD, {})},
    "self_traffic":      {"kinds": _PAYLOAD_KINDS, "fn": _cls_self_traffic,
                          "control": lambda: (_CONTROL_PAYLOAD, _CONTROL_SAMPLE_CTX)},
}


def classes_for(kind: str) -> list[str]:
    return [name for name, c in _CLASSES.items() if kind in c["kinds"]]


# ── in-process decision log (for breaker_summary / Step 5) ──────────────────
_LOCK = threading.Lock()
_LOG: deque = deque(maxlen=50)
_COUNTS = {"calls": 0, "blocked": 0, "untrusted": 0, "disabled": 0,
           "clean": 0, "by_class": {}}


def _record(decision: dict) -> None:
    with _LOCK:
        _LOG.append(decision)
        _COUNTS["calls"] += 1
        if decision.get("disabled"):
            _COUNTS["disabled"] += 1
        elif not decision.get("trusted"):
            _COUNTS["untrusted"] += 1
        elif decision.get("violations"):
            _COUNTS["blocked"] += 1
        else:
            _COUNTS["clean"] += 1
        for v in decision.get("violations") or []:
            cls = v.get("cls", "?")
            _COUNTS["by_class"][cls] = _COUNTS["by_class"].get(cls, 0) + 1


# ── the gate ─────────────────────────────────────────────────────────────────

def breaker(text_or_payload, kind: str, context: dict | None = None) -> dict:
    """Run the applicable lie-class guards for `kind`.

    Returns:
      {
        ok:          bool,   # True = safe to ship. For post/listing/canon a
                             #   False MUST stop the ship (fail closed). For
                             #   fact/score ok is advisory: never block, stamp
                             #   the ledger 'refuted' when violations exist.
        kind:        str,
        fail_closed: bool,   # True for post/listing/canon
        report_only: bool,   # True for fact/score
        trusted:     bool,   # False = control failed / class raised / disabled;
                             #   callers ship on our say-so no further
        disabled:    bool,   # kill switch
        violations:  [ {cls, detail} ],
        control:     { ok, violations, classes },
        classes_run: [ str ],
      }

    Never raises. `ok` is True whenever trusted is False (UNTRUSTED, not RED).
    """
    ctx = dict(context or {})
    kind = (kind or "").strip().lower()
    fail_closed = kind in _TEXT_KINDS
    report_only = kind in _PAYLOAD_KINDS

    if _disabled():
        d = {"ok": True, "kind": kind, "fail_closed": fail_closed,
             "report_only": report_only, "trusted": False, "disabled": True,
             "violations": [], "control": {"ok": None, "violations": [],
                                           "classes": []},
             "classes_run": []}
        _record(d)
        return d

    names = classes_for(kind)
    violations: list[dict] = []
    class_error = False
    for name in names:
        try:
            violations.extend(_CLASSES[name]["fn"](text_or_payload, ctx) or [])
        except Exception as e:  # a broken class -> UNTRUSTED, never a caller crash
            class_error = True
            logger.warning("[claim_breaker] class %s raised on %s: %s",
                           name, kind, str(e)[:160])

    # must-stay-green control: known-good input through the same classes.
    control_viols: list[dict] = []
    control_error = False
    for name in names:
        try:
            c_in, c_ctx = _CLASSES[name]["control"]()
            control_viols.extend(_CLASSES[name]["fn"](c_in, c_ctx) or [])
        except Exception as e:
            control_error = True
            logger.warning("[claim_breaker] CONTROL class %s raised: %s",
                           name, str(e)[:160])

    control_ok = (not control_viols) and (not control_error)
    trusted = control_ok and (not class_error)

    if not trusted:
        # UNTRUSTED: a gate that cannot pass its own control (or that crashed)
        # must not block shipping on its own say-so. Report, do not RED.
        ok = True
    else:
        ok = not violations

    decision = {
        "ok": ok, "kind": kind, "fail_closed": fail_closed,
        "report_only": report_only, "trusted": trusted, "disabled": False,
        "violations": violations,
        "control": {"ok": control_ok, "violations": control_viols,
                    "classes": names},
        "classes_run": names,
    }
    _record(decision)
    return decision


def breaker_summary(limit: int = 50) -> dict:
    """Last N in-process decisions + counts, for Step 5's /brain-live page.

    In-process only (no DB) — the decision log is per worker and resets on
    deploy, which is the honest shape for a request-time gate.
    """
    with _LOCK:
        recent = list(_LOG)[-max(1, min(int(limit or 50), _LOG.maxlen)):]
        counts = {"calls": _COUNTS["calls"], "blocked": _COUNTS["blocked"],
                  "untrusted": _COUNTS["untrusted"],
                  "disabled": _COUNTS["disabled"], "clean": _COUNTS["clean"],
                  "by_class": dict(_COUNTS["by_class"])}
    # Trim each recorded decision to a summary-safe shape.
    trimmed = [{
        "kind": d.get("kind"), "ok": d.get("ok"), "trusted": d.get("trusted"),
        "disabled": d.get("disabled"),
        "violations": [v.get("cls") for v in (d.get("violations") or [])],
        "control_ok": (d.get("control") or {}).get("ok"),
    } for d in recent]
    return {"disabled": _disabled(), "counts": counts, "recent": trimmed,
            "control_text_len": len(_CONTROL_POST_TEXT),
            "classes": sorted(_CLASSES.keys())}


# ── admin status endpoint (optional; 404 when disabled, never 5xx) ───────────

def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


@claim_breaker_bp.route("/api/v1/brain/claim-breaker/status", methods=["GET"])
def claim_breaker_status():
    # Disabled -> 404 (never 5xx): the surface is simply not present.
    if _disabled():
        return _no_store(jsonify(ok=False, disabled=True,
                                 error="claim-breaker disabled")), 404
    try:
        from internal_auth import require_internal_or_admin
        if not require_internal_or_admin(request):
            return _no_store(jsonify(ok=False, error="unauthorized")), 401
    except Exception:
        # Auth module unavailable -> fail closed (401), never 5xx.
        return _no_store(jsonify(ok=False, error="unauthorized")), 401
    try:
        return _no_store(jsonify(ok=True, summary=breaker_summary()))
    except Exception as e:  # never 5xx
        logger.warning("[claim_breaker] status raised: %s", str(e)[:160])
        return _no_store(jsonify(ok=True, summary={"error": "summary_unavailable"}))


def register_claim_breaker(app) -> bool:
    app.register_blueprint(claim_breaker_bp)
    return True
