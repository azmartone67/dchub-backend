"""routes/context_integrity_master_shell.py — Context-Integrity Master Shell
(#63, 2026-08-11).

Every other shell asks whether a NODE is healthy or whether an EDGE exists.
This one asks whether the brain could SEE — because a reasoning layer fed an
ambiguous `{}` produces a confident-sounding null, and we have been paying
Claude to re-learn that fact roughly twice a day.

★THE MEASUREMENT THAT MOTIVATED THIS SHELL. On 2026-08-11, of the 20 lessons
live on /api/v1/brain/lessons, **17 described the same failure** — a prediction
going null because an internal endpoint returned empty. Evidence counts on the
top two were 15 and 12:

    "Predictions requiring non-empty endpoint payloads (per-agent breakdowns,
     attribution metrics, conversion funnels, weekly-series, detector findings)
     null when endpoints return empty"
    "Predictions requiring granular breakdowns null when endpoints return
     empty — per-datacolo, per-IP, or per-agent distribution data required"

The learning loop was working perfectly. It had simply been pointed at the
instruments instead of the domain, because `_internal()` collapsed a timeout, a
500 and an honest empty into one indistinguishable value. util/internal_fetch
fixes the collapse; this shell is the guard that keeps it fixed, and the meter
that shows the lessons re-pointing at the business.

FOUR LANES, each one a place where "we could not measure" has been rendering as
"the answer is nothing":

  1 ENVELOPE   — re-runs L14's own probe set through the envelope and separates
                 instrument_failed from measured_empty. Also counts how many
                 routes/ modules still carry a private bare-{} `_internal`, so
                 the migration cannot silently stall at one call site.

  2 LESSONS    — the meter. What share of active L18 lessons are the brain
                 describing its own blindness rather than the platform? This
                 was 85% (17/20) at shell birth. It should fall. If it climbs
                 back, a probe has gone dark and L18 is consolidating the noise
                 again.

  3 RETIRE     — the subtract path. 747 route files, 62 master shells and 114
                 brain modules all arrived through an additive pipeline
                 (feature_proposer → layer22_auto_code → pr_writer) with no
                 counterpart that removes. brain_capability_ledger stops the
                 brain RE-BUILDING what exists; nothing stops the inventory
                 growing. This lane names retire candidates. It is REPORT-ONLY
                 and deletes nothing — a shell that could delete its own
                 siblings is a worse problem than the one it solves.

  4 LOOP EDGES — coverage, NOT a rebuild. Shell #49 already shipped the loop
                 graph: LOOP_EDGES, apply_loop_edges(), input_status on every
                 row of /api/v1/system/loops, count_alive_on_stale_input().
                 ★It was re-proposed as missing work during the 2026-08-11
                 audit and it was not missing — which is the capability-ledger
                 failure happening to a human instead of the brain. What this
                 lane measures is the REMAINING gap: how many probed loops
                 still carry no declared input (3 of 7 on 2026-08-11), because
                 an undeclared edge is indistinguishable from an absent one.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.

Endpoints:
  GET/POST /api/v1/admin/context-integrity/master-tick   JSON scoreboard
  GET      /admin/context-integrity                       HTML dashboard
  GET      /api/v1/admin/context-integrity                CF zone-worker alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as the other master shells.
Kill: CONTEXT_INTEGRITY_SHELL_DISABLE=1
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
context_integrity_master_shell_bp = Blueprint(
    "context_integrity_master_shell", __name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Lane 2 threshold. At shell birth the share was 17/20 = 85%. Anything at or
# above this means L18 is consolidating instrument noise, not platform truth.
_BLINDNESS_FAIL_SHARE = 0.50

# When util/internal_fetch shipped (PR #2596, merged 2026-08-12T01:14:33Z).
# Lessons at or after this stamp were learned by a brain with working
# instruments; anything earlier describes the world the envelope replaced.
# Compared as a string against L18's "YYYY-MM-DD HH:MM:SS" learned_at — both
# are zero-padded UTC, so lexical order IS chronological order.
_ENVELOPE_SHIPPED_AT = "2026-08-12 01:14:33"

# Lane 2 classifier. Deliberately blunt: these are the phrases L18 actually
# emitted, not a general-purpose sentiment read. ★A lesson is instrument
# blindness only when it pairs a null/absent VERDICT with an endpoint/payload
# SUBJECT — "spike-decay predictions falsify when the post-spike week collapses"
# is a real domain lesson and must not be swept in.
#
# ★2026-08-31 — TWO DEFECTS, IN OPPOSITE DIRECTIONS, THAT CANCELLED.
# Measured against all 20 active lessons, the previous form scored 11/20. So
# does this one. That is a coincidence, not a validation:
#
#   over-matched  `_NULL_VERDICT` ran against the WHOLE lesson, so "absent"
#                 counted even when it named what the instrument SAW rather
#                 than a null verdict. "Action-queue endpoint health verified
#                 true when endpoint returns ok, populated queue data, and
#                 specific finding is absent" is a SUCCESSFUL reading — the
#                 finding cleared. Two of those, plus two "falsify when ...
#                 detector:domain pair absent — indicates reclassification,
#                 not operational fix", which is a genuine domain lesson about
#                 QA semantics. 4 false positives.
#   under-matched `payload` had no `s?` while `endpoint` was spelled twice to
#                 get one. Four lessons ending "...without segmented demand
#                 payloads" scored clean. 4 false negatives.
#
# The verdict is unchanged BECAUSE the counts happened to offset; the next
# consolidation will not be so lucky. A blindness detector that is right by
# accident is the exact failure this shell exists to name.
#
# The fix reads the VERDICT CLAUSE — the text before the condition — for the
# null word, and the whole lesson for the subject. L18 writes
# "<subject> predictions <verdict> when/based on <condition>", so a null word
# after the split is describing the condition, not the outcome.
_NULL_VERDICT = re.compile(
    r"\b(null|nulls|unavailable|insufficient|absent|blind)\b", re.I)
_EMPTY_SUBJECT = re.compile(
    r"\b(empty|non-empty|endpoints?|payloads?)\b", re.I)
# Leftmost of these ends the verdict and begins the condition.
_CONDITION_START = re.compile(r"\s+when\s+|\s+based on\s+|\u2014|--")


def _is_instrument_blindness(lesson: str) -> bool:
    """True when the lesson reports NO reading from an endpoint/payload.

    The null word must sit in the verdict clause. A lesson that reports a
    verdict of verify-true or falsify had a working instrument, whatever the
    condition clause goes on to say was absent."""
    t = str(lesson or "")
    verdict = _CONDITION_START.split(t, 1)[0]
    return bool(_NULL_VERDICT.search(verdict) and _EMPTY_SUBJECT.search(t))


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("CONTEXT_INTEGRITY_SHELL_DISABLE")
            or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, renders '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list) -> str:
    """PASS only when every critical check affirmatively passed. An
    indeterminate critical check yields '?' — never green-by-silence."""
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in crits):
        return "?"
    return "PASS"


def _routes_dir() -> str:
    return os.path.join(_REPO_ROOT, "routes")


def _read(path: str):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return None


# ── lane 1: envelope ──────────────────────────────────────────────────

def _lane_envelope() -> list:
    """Live: L14's probe set, split instrument_failed vs measured_empty.
    Repo: how many modules still swallow into a bare {}."""
    checks = []
    try:
        from util.internal_fetch import health_of, probe
        from routes.brain_layer14_causal import _CONTEXT_PROBES
    except Exception as e:  # noqa: BLE001
        return [_check("import", "envelope + probe set importable", None,
                       "import failed: %s: %s" % (type(e).__name__,
                                                  str(e)[:110]),
                       critical=True)]

    envs = {n: probe(p, t) for n, p, t in _CONTEXT_PROBES}
    h = health_of(envs)
    failed, empty = h["instrument_failed"], h["measured_empty"]
    total = len(envs)

    # ★The critical check is that we can TELL THEM APART, which is now always
    # true. What fails the lane is an instrument actually being down — that is
    # a real defect, and before the envelope it was invisible.
    checks.append(_check(
        "no_instrument_failed",
        "every L14 context probe answered",
        not failed,
        ("all %d probes answered" % total) if not failed else
        ("%d/%d could NOT be measured: %s" % (
            len(failed), total,
            "; ".join("%s (%s)" % (f["probe"], f["reason"])
                      for f in failed[:6]))),
        critical=True))

    checks.append(_check(
        "empty_is_measured",
        "empty answers are measurements, not failures",
        True,
        ("%d/%d measured empty (%s) — these are real zeros, not blind spots"
         % (len(empty), total, ", ".join(empty[:8]) or "none")),
        critical=False))

    # Repo migration coverage.
    # ★The detector lives in util/internal_fetch and is SHARED with
    # tests/test_envelope_migration.py. It used to be re-implemented here, more
    # loosely, and the two disagreed: this lane reported radar.py (correct
    # tuple form) and mcp_high_intent_claim._internal_ok (an auth helper) as
    # swallowers, while both missed phx_live's ternary form. A meter with its
    # own private definition of what it measures will drift from the guard that
    # is supposed to hold it.
    from util.internal_fetch import looks_like_swallowing_fetcher
    swallowers, migrated = [], []
    try:
        for fn in sorted(os.listdir(_routes_dir())):
            if not fn.endswith(".py"):
                continue
            src = _read(os.path.join(_routes_dir(), fn)) or ""
            if "util.internal_fetch" in src or "from util import internal_fetch" in src:
                migrated.append(fn)
            elif looks_like_swallowing_fetcher(src):
                swallowers.append(fn)
    except Exception as e:  # noqa: BLE001
        return checks + [_check("repo_scan", "routes/ scannable", None,
                                "scan failed: %s" % str(e)[:110])]

    checks.append(_check(
        "migration_coverage",
        "bare-{} internal fetchers remaining in routes/",
        None if swallowers else True,
        ("%d migrated, %d still swallow: %s"
         % (len(migrated), len(swallowers), ", ".join(swallowers[:10]))
         if swallowers else "%d migrated, none remaining" % len(migrated)),
        critical=False))
    return checks


# ── lane 2: lessons ───────────────────────────────────────────────────

def _lane_lessons() -> list:
    from util.internal_fetch import probe
    env = probe("/api/v1/brain/lessons", 10)
    if not env["ok"]:
        # ★The lane that exists to expose blind instruments must not go blind
        # silently. An unmeasurable lesson feed is '?', never PASS.
        return [_check("lessons_reachable", "L18 lesson feed answered", None,
                       "could not measure: %s" % env["reason"], critical=True)]
    lessons = (env["data"].get("active_lessons") or [])
    if not lessons:
        return [_check("lessons_present", "L18 has consolidated lessons", None,
                       "feed answered with zero active lessons — L18 may not "
                       "have run yet", critical=True)]
    blind = [l for l in lessons
             if _is_instrument_blindness((l or {}).get("lesson"))]
    share = len(blind) / float(len(lessons))

    # ★2026-08-12 — THE VERDICT NOW READS ONLY POST-FIX LESSONS, and the
    # all-time number is reported beside it as context.
    #
    # Lessons never expire. L18 deactivates one only when a NEW lesson
    # explicitly supersedes it BY EXACT TEXT, so the 17 blindness lessons
    # written before the envelope shipped stay active indefinitely and drag the
    # all-time share to 85% no matter how healthy the probes become. A critical
    # check pinned to a number that cannot move is a permanent red — and a
    # permanent red is ignored exactly as fast as a permanent green.
    #
    # What we actually want to know is: SINCE the instruments were fixed, is the
    # brain still learning about its own blindness? That is answered only by
    # lessons learned after the cutoff.
    post = [l for l in lessons
            if str((l or {}).get("learned_at") or "") >= _ENVELOPE_SHIPPED_AT]
    post_blind = [l for l in post
                  if _is_instrument_blindness((l or {}).get("lesson"))]

    if not post:
        # ★Indeterminate, not a pass: no post-fix lesson yet means the meter has
        # nothing to say, and saying "healthy" from an empty sample is the
        # confident-green this shell exists to refuse.
        checks = [_check(
            "blindness_share_since_fix",
            "post-fix lessons are about the platform, not blind probes",
            None,
            "no lesson learned since the envelope shipped (%s) — L18 "
            "consolidates every 12h, so the meter has no post-fix sample yet. "
            "All-time: %d/%d (%.0f%%), which includes the %d pre-fix lessons "
            "that motivated the shell and never expire."
            % (_ENVELOPE_SHIPPED_AT, len(blind), len(lessons), share * 100,
               len(blind)),
            critical=True)]
    else:
        post_share = len(post_blind) / float(len(post))
        checks = [_check(
            "blindness_share_since_fix",
            "post-fix lessons are about the platform, not blind probes",
            post_share < _BLINDNESS_FAIL_SHARE,
            "%d/%d (%.0f%%) of lessons learned since %s are instrument-"
            "blindness (threshold %.0f%%). All-time %d/%d (%.0f%%) — the "
            "all-time figure cannot fall on its own: lessons deactivate only "
            "on exact-text supersede."
            % (len(post_blind), len(post), post_share * 100,
               _ENVELOPE_SHIPPED_AT, _BLINDNESS_FAIL_SHARE * 100,
               len(blind), len(lessons), share * 100),
            critical=True)]
    # Weight by evidence_count: one lesson seen 15 times costs more than one
    # seen once, and the unweighted share can improve while the expensive ones
    # stay put.
    try:
        tot_w = sum(float(l.get("evidence_count") or 1) for l in lessons)
        blind_w = sum(float(l.get("evidence_count") or 1) for l in blind)
        checks.append(_check(
            "blindness_weighted",
            "evidence-weighted blindness share",
            (blind_w / tot_w) < _BLINDNESS_FAIL_SHARE if tot_w else None,
            "%.0f%% of total evidence weight (%d of %d episodes)"
            % ((blind_w / tot_w * 100) if tot_w else 0, blind_w, tot_w),
            critical=False))
    except Exception:  # noqa: BLE001
        pass
    return checks


# ── lane 3: retire ────────────────────────────────────────────────────

def _lane_retire() -> list:
    """Report-only. Names what the additive pipeline has left behind."""
    checks, rdir = [], _routes_dir()
    try:
        files = sorted(f for f in os.listdir(rdir) if f.endswith(".py"))
    except Exception as e:  # noqa: BLE001
        return [_check("scan", "routes/ scannable", None,
                       "scan failed: %s" % str(e)[:110], critical=True)]

    proposed = [f for f in files if f.startswith("_proposed_")]
    shells = [f for f in files if f.endswith("_master_shell.py")]

    checks.append(_check(
        "inventory", "routes/ inventory", True,
        "%d route modules, %d master shells, %d _proposed_ drafts"
        % (len(files), len(shells), len(proposed))))

    # ★Ask the RUNNING APP which blueprints exist, do not grep main.py.
    # The first cut text-scanned main.py alone and reported
    # webmcp_master_shell.py as "dead code carrying a dashboard". It is not:
    # cron_heartbeat._register_webmcp_shell registers it at startup (as it does
    # for analyst_note, metric_truth, dark_zones and cluster_latency), and the
    # live endpoint answers 403, not 404. A false red costs exactly what a
    # false green costs — someone stops believing the board.
    unregistered, basis = None, None
    try:
        from flask import current_app
        live = set(current_app.blueprints or ())
        if live:
            unregistered = []
            for s in shells:
                src = _read(os.path.join(rdir, s)) or ""
                m = re.search(r"Blueprint\(\s*[\"']([A-Za-z0-9_]+)[\"']", src)
                name = m.group(1) if m else s[:-3]
                if name not in live:
                    unregistered.append(s)
            basis = "live app (%d blueprints registered)" % len(live)
    except Exception as e:  # noqa: BLE001
        logger.debug("[context-integrity] blueprint introspection failed: %s", e)

    if unregistered is None:
        # ★Indeterminate, NOT a pass. Outside an app context we cannot know,
        # and answering "all registered" from ignorance is the confident-green
        # this shell exists to refuse.
        checks.append(_check(
            "unregistered_shells",
            "every master shell is registered on the live app",
            None,
            "could not read current_app.blueprints — registration unverified "
            "(this check is only meaningful inside a request context)",
            critical=False))
    else:
        checks.append(_check(
            "unregistered_shells",
            "every master shell is registered on the live app",
            not unregistered,
            ("all %d shells registered, per %s" % (len(shells), basis))
            if not unregistered else
            ("%d shells never registered — dead code carrying a dashboard "
             "(per %s): %s" % (len(unregistered), basis,
                               ", ".join(unregistered[:8]))),
            critical=False))

    checks.append(_check(
        "proposed_backlog",
        "_proposed_ drafts awaiting promote-or-delete",
        None if proposed else True,
        ("%d drafts: %s" % (len(proposed),
                            ", ".join(p[:46] for p in proposed[:6]))
         if proposed else "none"),
        critical=False))

    # Name-stem collisions: fixwave/qa_fixwave, flywheel/loop_flywheel,
    # graph/graph_spine, freshness/{ingestion,registry}_freshness. A collision
    # is not automatically duplication — it is where to LOOK for it.
    stems = {}
    for s in shells:
        stem = s[:-len("_master_shell.py")]
        for other in shells:
            if other == s:
                continue
            o = other[:-len("_master_shell.py")]
            if stem != o and (stem.endswith(o) or o.endswith(stem)):
                stems.setdefault(o if len(o) < len(stem) else stem,
                                 set()).add(stem if len(o) < len(stem) else o)
    pairs = ["%s↔%s" % (k, ",".join(sorted(v))) for k, v in sorted(stems.items())]
    checks.append(_check(
        "shell_name_overlap",
        "master shells with overlapping name stems",
        None if pairs else True,
        ("%d overlapping stems — review for duplicated lanes: %s"
         % (len(pairs), "; ".join(pairs[:8]))) if pairs else "none",
        critical=False))
    return checks


# ── lane 4: loop-edge coverage ────────────────────────────────────────

def _lane_loop_edges() -> list:
    """★NOT a rebuild — shell #49 shipped this graph. This measures the gap it
    left: loops with no declared input, where an absent edge and an undeclared
    edge look identical."""
    from util.internal_fetch import probe
    checks = []
    try:
        from routes.graph_master_shell import LOOP_EDGES
        declared = len(LOOP_EDGES or ())
    except Exception as e:  # noqa: BLE001
        declared = None
        checks.append(_check("edge_set", "LOOP_EDGES importable", None,
                             "import failed: %s" % str(e)[:110], critical=True))

    env = probe("/api/v1/system/loops", 12)
    if not env["ok"]:
        return checks + [_check("board", "loop board answered", None,
                                "could not measure: %s" % env["reason"],
                                critical=True)]
    loops = env["data"].get("loops") or []
    if not loops:
        return checks + [_check("board", "loop board carries loops", None,
                                "board answered with zero loops",
                                critical=True)]

    annotated = [l for l in loops if l.get("input_status")]
    checks.append(_check(
        "edges_applied",
        "#49 loop edges live on the board",
        len(annotated) == len(loops),
        "%d/%d loops carry input_status (%d declared edges)"
        % (len(annotated), len(loops),
           declared if declared is not None else -1),
        critical=True))

    # ★2026-08-12: a ROOT is not a GAP. This used to count every
    # no_declared_input loop as missing coverage, which held 3 of 7 permanently
    # amber — they are source nodes (external MCP clients, public HN/Reddit,
    # a GitHub Actions cron) and can never have an upstream loop. A board with
    # an amber nobody can clear teaches people to stop reading it.
    sources = [l.get("name") for l in loops
               if l.get("input_status") == "external_source"]
    undeclared = [l.get("name") for l in loops
                  if l.get("input_status") == "no_declared_input"]
    checks.append(_check(
        "edge_coverage",
        "every non-source loop has a declared input",
        None if undeclared else True,
        ("%d/%d loops have no declared input and are NOT typed as sources — an "
         "undeclared edge reads the same as no edge: %s"
         % (len(undeclared), len(loops),
            ", ".join(n for n in undeclared if n)))
        if undeclared else
        ("all %d loops accounted for: %d with declared inputs, %d typed as "
         "external sources (%s)"
         % (len(loops), len(loops) - len(sources) - len(undeclared),
            len(sources), ", ".join(n for n in sources if n) or "none")),
        critical=False))

    stale = [l.get("name") for l in loops
             if l.get("status") in ("alive", "idle")
             and l.get("input_status") == "stale"]
    checks.append(_check(
        "alive_on_stale_input",
        "no loop reports healthy on stale input",
        not stale,
        ("%d green-on-stale: %s" % (len(stale), ", ".join(n for n in stale if n)))
        if stale else "none",
        critical=True))
    return checks


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn) -> list:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s" % (type(e).__name__, str(e)[:120]),
                       critical=True)]


def _beat_ledger(note: str, failing: bool = False) -> None:
    """Best-effort beat into the SHIPPED ingest_runs ledger. NEVER raises."""
    try:
        body = json.dumps({
            "feed": "context-integrity-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint urllib-request-on-railway)
        _rq.post("http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-context-integrity-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[context-integrity] ledger beat failed: %s", e)


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    lanes = [
        {"id": "envelope", "name": "1 · probe envelope (failed vs empty)",
         "checks": _safe_lane(_lane_envelope)},
        {"id": "lessons", "name": "2 · lesson composition (the meter)",
         "checks": _safe_lane(_lane_lessons)},
        {"id": "retire", "name": "3 · retire candidates (the subtract path)",
         "checks": _safe_lane(_lane_retire)},
        {"id": "loop_edges", "name": "4 · loop-edge coverage (#49 applied)",
         "checks": _safe_lane(_lane_loop_edges)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "context-integrity-63",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


@context_integrity_master_shell_bp.route(
    "/api/v1/admin/context-integrity/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx: the CF worker's proxyWithRetry reads ANY 5xx from
        # Railway as a dead origin and fails over site-wide to the stale Render
        # backend. Two within 10s break the site for 30s.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden — X-Admin-Key or ?admin_key="), 403
    return jsonify(_run_tick(beat=(request.method == "POST")))


_MARK = {True: ("✓", "#0a7"), False: ("✗", "#c22"), None: ("?", "#b80")}


@context_integrity_master_shell_bp.route("/admin/context-integrity",
                                         methods=["GET"])
@context_integrity_master_shell_bp.route("/api/v1/admin/context-integrity",
                                         methods=["GET"])
def dashboard():
    if _disabled():
        return Response("context-integrity shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403,
                        mimetype="text/plain")
    t = _run_tick(beat=False)
    rows = []
    for ln in t["lanes"]:
        rows.append('<h2>%s <small>%s</small></h2><table>'
                    % (_esc(ln["name"]), _esc(ln["verdict"])))
        for c in ln["checks"]:
            mark, col = _MARK.get(c["pass"], ("?", "#b80"))
            rows.append(
                '<tr><td style="color:%s;font-weight:700">%s</td>'
                '<td>%s%s</td><td>%s</td></tr>'
                % (col, mark, _esc(c["name"]),
                   " <b>(critical)</b>" if c.get("critical") else "",
                   _esc(str(c["detail"]))))
        rows.append("</table>")
    html = (
        '<!doctype html><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="60">'
        "<title>Context Integrity — shell #63</title>"
        "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;"
        "margin:2rem;max-width:1100px}table{border-collapse:collapse;"
        "width:100%%;margin:.5rem 0 1.5rem}td{border-top:1px solid #e5e5e5;"
        "padding:.4rem .6rem;vertical-align:top}h2{margin:1.4rem 0 .2rem;"
        "font-size:15px}small{font-weight:400;color:#666}"
        "code{background:#f5f5f5;padding:.1rem .3rem}</style>"
        "<h1>Context Integrity <small>shell #63</small></h1>"
        "<p>Can the brain see? Lane 2 is the meter: 17 of 20 lessons were "
        "instrument-blindness at shell birth (2026-08-11).</p>"
        "<p><b>%s</b> &middot; generated %s</p>%s"
        % (_esc(t["summary"]), _esc(t["generated_at"]), "".join(rows)))
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})
