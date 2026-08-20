"""DC Hub — STABILITY master shell (#55, 2026-08-20).

★ WHY THIS SHELL EXISTS

Owner directive 2026-08-20: "audit all errors, fix, prevent, once and for all —
no more one-off patches." The audit that followed found the drift was not a run
of bad luck but five self-reinforcing mechanisms, and — the part that matters —
**nothing anywhere measured whether any fix was working.**

The week to 2026-08-20: 374 PRs merged across three repos (backend 290, of
which 104 were prefixed `fix`), CI 351/354 GREEN, and 8,843 backend tests
passing. Volume was the only visible signal, and volume cannot tell
"we are fixing things" from "we are re-fixing the same things."

This shell is the scoreboard that was missing. Its lanes are the four steps the
audit ended on, in order:

  1. the gates must be ENFORCED, not merely present      -> lane A
  2. convergence must be MEASURED                        -> lane B
  3. the change rate must be BOUNDED                     -> lane C
  4. the two untouched root causes must stay VISIBLE     -> lanes D, E

plus the health of the two mechanisms already shipped (#2990, #2992), because
a guard nobody watches is the next thing to rot:

  F coverage floors are pinned          (RC1, be #2990)
  G the shadowed-route ratchet holds    (RC1, be #2992)

★ EACH LANE PINS AN INVARIANT, NEVER A VALUE (cf. contract healer #44). A lane
pinned to today's number goes green the moment the number drifts for an
unrelated reason — the exact failure this family of shells exists to retire.
"36 merges/day" is not the invariant; "the change rate is bounded and the bound
is declared" is.

★ THIS SHELL IS BORN RED, and that is correct (cf. #45 BORN RED). Lanes A, C, D
and E were all measured FAILING at 2026-08-20 — the gate is not yet required,
the change rate is ~36/day, the brain's code lane watches 7 loops behind a
hardcoded map, and the detector-precision lane has no routed findings yet. A
green lane on day one would mean the invariant was written to fit the defect.

★ REPORT-ONLY. It heals nothing and actions nothing. Lane C is a working-
practice decision, lane D is an autonomy-scope decision, and lane A mutates
branch protection — none of those may be auto-actioned by a diagnostic.

★ "?" IS A REAL VERDICT, NOT A SOFT PASS. Lanes A and C need a GitHub token;
without one they report `?`, never PASS. "I could not measure it" and "it is
fine" are different states, and blurring them is what this whole audit was
about.

  In production a token IS available: PR_SUBMIT_TOKEN and GITHUB_TOKEN (both
  classic, both 200) live on dchub-backend. Only GH_TOKEN — a fine-grained PAT
  that had started returning 401 — was deleted on 2026-07-25. `?` here means a
  local or tokenless run, not a broken deploy.

  ★ _gh_token() tries PR_SUBMIT_TOKEN, then GITHUB_TOKEN, then GH_TOKEN, in
  that order ON PURPOSE. The `or`-chain trap that motivated deleting GH_TOKEN
  was that every consumer read `GH_TOKEN or GITHUB_TOKEN` — so a PRESENT BUT
  BROKEN GH_TOKEN poisoned code that looked like it tried both, because there
  is no 401-retry anywhere. Preferring the known-good vars means re-adding a
  bad GH_TOKEN later cannot silently blind this shell.

Lanes
  A gate_enforcement    every gate we built must be a REQUIRED check on main
  B convergence         recurrence must be measured, with a real denominator
  C change_rate         merges/day must be under a declared ceiling
  D brain_code_reach    learn_code's targets must be DERIVED, not hardcoded
  E detector_precision  the squasher must not refuse ~100% by construction
  F coverage_floors     every repo-scanning test must be pinned
  G shadow_ratchet      shadowed routes must not exceed the pinned baseline

Routes (registered via stability_master_shell_bp in main.py):
  GET /api/v1/admin/stability-shell/master-tick  -> JSON verdicts
  GET /api/v1/admin/stability-shell              -> HTML board
  GET /admin/stability-shell                     -> HTML board
Kill: STABILITY_SHELL_DISABLE=1
  ★ returns 404, never 503 — the CF worker reads any 5xx from Railway as a dead
  origin and fails the whole site over to stale Render. See
  tests/test_shell_killswitch_never_5xx.py.
"""
from __future__ import annotations

import ast
import json
import logging
import os

import requests

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
stability_master_shell_bp = Blueprint("stability_master_shell", __name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = "azmartone67/dchub-backend"
_TIMEOUT = 6            # per-read; the whole tick must clear CF's 15s ceiling

# The gates this platform built. A gate absent from main's required contexts is
# advisory, and an advisory gate does not stop a merge.
_MUST_BE_REQUIRED = ("app-contract-gate",)

# Lane C's declared ceiling. This is a WORKING-PRACTICE bound, not a discovered
# value: the audit's finding was that ~36 backend merges/day into a
# 45,579-line main.py is itself the instability, because every merge is an
# opportunity to drift and no gate survives that volume. 12/day is the stated
# target to move toward, not a measurement of anything.
_MERGES_PER_DAY_CEILING = 12


def _disabled() -> bool:
    return (os.environ.get("STABILITY_SHELL_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    keys = {v for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY")
            for v in [os.environ.get(n)] if v}
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return bool(sent) and sent in keys


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": detail,
            "critical": critical}


def _lane_verdict(checks: list) -> str:
    """FAIL on any false; `?` when nothing was actually verified.

    Same contract as #54: a lane whose reads all failed must never render
    green, because "I could not measure it" is not "it is fine".
    """
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is None for k in crits):
        return "?"
    if any(k["pass"] is None for k in checks) and not any(k["pass"] is True for k in checks):
        return "?"
    return "PASS"


def _gh_token() -> str:
    for n in ("PR_SUBMIT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _gh(path: str):
    """GitHub read. Returns None on any failure — the caller renders `?`."""
    tok = _gh_token()
    if not tok:
        return None
    # ★ requests, not urllib. urllib is banned repo-wide (scripts/regression_
    # lint.py blocks it) because its default User-Agent is rejected at the
    # Cloudflare edge with error 1010, BEFORE the worker runs — so a urllib
    # call that works locally dies in production for reasons the traceback does
    # not explain. api.github.com is not behind that edge, but the rule is
    # repo-wide on purpose: the next person copying this helper would not know
    # which hosts are safe. The lint caught this exact line.
    try:
        r = requests.get(
            f"https://api.github.com{path}",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "dchub-stability-shell/1.0"},
            timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.info("[stability_shell] gh %s -> %s", path, r.status_code)
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.info("[stability_shell] gh %s failed: %s", path, e)
        return None


def _read(rel: str) -> str:
    """Read a repo file that ships with the deploy. '' on failure."""
    try:
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return ""


# ── lane A — the gates must be ENFORCED (audit step 1) ───────────────────

def _lane_gate_enforcement() -> list:
    """★ A gate that is not REQUIRED does not bind.

    The behavior gate (#2992) boots the real app and checks what it serves —
    the only check in CI that does. But main's required contexts on 2026-08-20
    were substance-gate, syntax-check, unit-tests, regression-lint, db-parity.
    A check absent from that list can go red while the merge proceeds, which
    makes it a dashboard, not a gate.

    INVARIANT (not a value): every gate this platform built to block a bad
    merge must appear in main's required status checks. Passes when the list
    contains them — not when the list is any particular length.

    BORN RED: app-contract-gate was not required at authoring time. It is added
    only after the workflow exists on main, because a required context that
    never reports blocks every PR forever.
    """
    prot = _gh(f"/repos/{_REPO}/branches/main/protection")
    if not isinstance(prot, dict):
        return [_check("a_read", "branch protection readable", None,
                       "no GitHub token available (prod has PR_SUBMIT_TOKEN / "
                       "GITHUB_TOKEN) — unverified, NOT assumed fine",
                       critical=True)]
    ctx = ((prot.get("required_status_checks") or {}).get("contexts") or [])
    out = [_check("a_read", "branch protection readable", True,
                  f"{len(ctx)} required context(s)", critical=True)]
    for gate in _MUST_BE_REQUIRED:
        out.append(_check(
            f"a_required_{gate}", f"{gate} is a required check",
            gate in ctx,
            f"required contexts: {sorted(ctx)}"))
    return out


# ── lane B — convergence must be MEASURED (audit step 2) ─────────────────

def _lane_convergence() -> list:
    """★ The number the platform did not have.

    104 `fix` PRs landed in the week to 2026-08-20 and nothing measured whether
    any of them stopped its finding recurring. squasher_queue.convergence()
    now computes it.

    INVARIANT: the recurrence rate must be MEASURABLE — a real denominator of
    closed findings — not merely present. A null rate is honest reporting of
    "not measured" (that is deliberate, see the null-vs-zero test), but a
    scoreboard that is permanently null is not a scoreboard.

    ★ BASELINE MEASURED 2026-08-20, live prod: closed=210 · recurred=136 ·
      recurrence_rate=0.648. Two of every three findings this lane closed came
      back. And closed_with_pr=0 — in thirty days it closed 210 findings and
      shipped ZERO code fixes, which is RC2 quantified: before #2993 the lane's
      only exit that counted as progress was a PR it never managed to open.

    Deliberately does NOT pin a target rate. What "good" looks like is unknown
    until the baseline has moved, and inventing a threshold now would be
    pinning a value — the thing lane design here forbids. The lane asks only
    that the number be MEASURABLE; driving it down is the owner's call.
    """
    try:
        from routes.squasher_queue import convergence
        c = convergence(30)
    except Exception as e:  # noqa: BLE001
        return [_check("b_read", "convergence readable", None,
                       f"could not compute: {type(e).__name__}", critical=True)]
    if not c.get("ok"):
        return [_check("b_read", "convergence readable", None,
                       str(c.get("error"))[:160], critical=True)]
    closed = int(c.get("closed") or 0)
    rate = c.get("recurrence_rate")
    return [
        _check("b_read", "convergence readable", True,
               f"closed={closed} recurred={c.get('recurred')} "
               f"rate={rate}", critical=True),
        _check("b_has_denominator", "a real denominator exists", closed > 0,
               f"{closed} finding(s) closed in 30d — a rate over 0 closed "
               f"findings measures nothing"),
        _check("b_rate_measured", "recurrence rate is measured", rate is not None,
               "null means NOT MEASURED, which is honest but is not a "
               "scoreboard"),
    ]


# ── lane C — the change rate must be BOUNDED (audit step 3) ──────────────

def _lane_change_rate() -> list:
    """★ The disease, not a symptom.

    290 backend PRs merged in the 8 days to 2026-08-20 — ~36/day — into a
    45,579-line main.py that 41 of them touched. At that rate every gate is a
    filter on a firehose: you drift slower and more visibly, you do not stop.
    288 of the 290 were authored by the owner's own account, so there is no bot
    to throttle; the rate is a working practice.

    INVARIANT: merges/day must sit under a DECLARED ceiling. The ceiling is a
    stated target (_MERGES_PER_DAY_CEILING), not a measurement — this lane
    exists to make the rate visible and bounded, and moving the ceiling is a
    deliberate act rather than a drift.

    BORN RED at ~36/day against a ceiling of 12.
    """
    prs = _gh(f"/repos/{_REPO}/pulls?state=closed&per_page=100&sort=updated"
              f"&direction=desc")
    if not isinstance(prs, list):
        return [_check("c_read", "merge history readable", None,
                       "no GitHub token available (prod has PR_SUBMIT_TOKEN / "
                       "GITHUB_TOKEN) — unverified, NOT assumed fine",
                       critical=True)]
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cut = now - timedelta(days=7)
    merged = 0
    for p in prs:
        ts = p.get("merged_at")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if when >= cut:
            merged += 1
    per_day = round(merged / 7.0, 1)
    # The page caps at 100 closed PRs; if every one of them merged inside the
    # window the true rate is HIGHER than measured, so say so rather than
    # quoting a floor as if it were the number.
    capped = merged >= 100
    return [
        _check("c_read", "merge history readable", True,
               f"{merged} merged in 7d ({per_day}/day)"
               + (" — PAGE CAP HIT, true rate is higher" if capped else ""),
               critical=True),
        _check("c_under_ceiling", "merge rate is under the declared ceiling",
               (per_day <= _MERGES_PER_DAY_CEILING) and not capped,
               f"{per_day}/day vs ceiling {_MERGES_PER_DAY_CEILING}/day"),
    ]


# ── lane D — RC3, the brain's code lane (audit step 4) ───────────────────

def _lane_brain_code_reach() -> list:
    """★ learn_code watches 7 loops behind a hardcoded 5-entry map.

    routes/brain_v2_layer5.learn_code targets loops that are stale/dead AND
    present in LOOP_SOURCE_FILES. That dict had 5 entries on 2026-08-20, and
    the whole system view has 7 loops — engagement_track and mcp_traffic are
    permanently invisible to it. So `considered=0` every run is CORRECT
    behaviour for a lane scoped to almost nothing, against a platform with 69
    tracked feeds and ~740 blueprints. It is not a bug; it is a coverage gap
    by design, which is why it never showed up as one.

    INVARIANT: the code-learning lane's target set must be DERIVED from live
    state, not a hand-maintained allowlist. A hardcoded map cannot grow when
    the platform does, so its coverage silently shrinks in relative terms every
    time anything is added elsewhere.

    Passes when LOOP_SOURCE_FILES stops being a literal dict of fixed keys.
    BORN RED. Widening what the brain watches is an autonomy-scope decision
    (see reference_dchub_autonomy_core), so this lane REPORTS and never acts.
    """
    src = _read("routes/brain_v2_layer5.py")
    if not src:
        return [_check("d_read", "brain_v2_layer5 readable", None,
                       "source not found", critical=True)]
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [_check("d_read", "brain_v2_layer5 parses", None,
                       f"SyntaxError: {e}", critical=True)]
    entries = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "LOOP_SOURCE_FILES" \
                and isinstance(node.value, ast.Dict):
            entries = len(node.value.keys)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "LOOP_SOURCE_FILES":
                    entries = len(node.value.keys)
    if entries is None:
        # Either it was removed or it is no longer a dict literal. Both are the
        # shape this lane wants, but say which rather than guessing.
        return [
            _check("d_read", "LOOP_SOURCE_FILES located", True,
                   "not a dict literal any more — the map may now be derived",
                   critical=True),
            _check("d_derived", "the target set is derived, not hardcoded",
                   True, "no hardcoded dict literal found"),
        ]
    return [
        _check("d_read", "LOOP_SOURCE_FILES located", True,
               f"hardcoded dict literal with {entries} entries", critical=True),
        _check("d_derived", "the target set is derived, not hardcoded", False,
               f"{entries} hand-maintained entries gate every code proposal; "
               f"loops outside them can never be learned from"),
    ]


# ── lane E — RC5, detector precision (audit step 4) ──────────────────────

def _lane_detector_precision() -> list:
    """★ A lane that refuses ~100% is not exercising judgement.

    On 2026-08-20 all 25 rows in squasher_work_queue were 'refused', and not
    one was a refusal in the ordinary sense: every analysis had named an
    operator action or a decision, and the lane had nowhere to put either.
    #2993 gave it awaiting_ops / awaiting_decision.

    The same file records the precedent: a broken remedy extractor once refused
    100% of the time BY CONSTRUCTION while presenting the refusals as
    judgements about the findings.

    INVARIANT: refusal must not be the lane's only terminal outcome. Once
    findings have flowed, the terminal mix must contain something other than
    'refused' — a PR, a routed hand-off, or a resolution.

    BORN RED / `?` until findings flow through the new exits.
    """
    try:
        from routes.squasher_queue import _conn, _ensure_table
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                """SELECT status, COUNT(*) FROM squasher_work_queue
                    WHERE finished_at > NOW() - INTERVAL '30 days'
                 GROUP BY status""")
            mix = {str(r[0]): int(r[1]) for r in (cur.fetchall() or [])}
    except Exception as e:  # noqa: BLE001
        return [_check("e_read", "terminal mix readable", None,
                       f"{type(e).__name__}: {str(e)[:120]}", critical=True)]
    total = sum(mix.values())
    if not total:
        return [_check("e_read", "terminal mix readable", None,
                       "no findings settled in 30d — nothing to judge",
                       critical=True)]
    refused = int(mix.get("refused") or 0)
    other = total - refused
    return [
        _check("e_read", "terminal mix readable", True,
               f"30d: {mix}", critical=True),
        _check("e_not_all_refused", "refusal is not the only outcome",
               other > 0,
               f"{refused}/{total} refused; {other} reached another terminal "
               f"state. 100% refused means the lane cannot act, not that every "
               f"finding was unactionable"),
    ]


# ── lane F — RC1, the coverage floors hold (be #2990) ────────────────────

def _lane_coverage_floors() -> list:
    """★ The guard that guards the guards.

    tests/_scan_floors.py turns "this scan found nothing" from a silent pass
    into a red build. Mutation-proved: test_brain_loggers_defined.py scans 118
    modules, and repointing its glob at a plausible refactor target dropped it
    to 1 while it still exited 0.

    INVARIANT: the floors manifest must be present and non-trivial, and every
    pinned floor must be capable of failing. A floor of 0 is an unarmed guard
    wearing a guard's uniform.
    """
    raw = _read("tests/scan_floors.json")
    if not raw:
        return [_check("f_read", "scan_floors.json present", False,
                       "the manifest is missing — every repo-scanning test is "
                       "fail-open again", critical=True)]
    try:
        floors = (json.loads(raw) or {}).get("floors") or {}
    except Exception as e:  # noqa: BLE001
        return [_check("f_read", "scan_floors.json parses", False,
                       f"{type(e).__name__}", critical=True)]
    dead = [f for f, e in floors.items()
            if not e or any(int(v) < 1 for v in e.values())]
    return [
        _check("f_read", "scan_floors.json parses", True,
               f"{len(floors)} scanning test file(s) pinned", critical=True),
        _check("f_nonempty", "the manifest pins real files", len(floors) >= 20,
               f"{len(floors)} pinned"),
        _check("f_armed", "no floor is 0 or empty", not dead,
               f"unarmed: {sorted(dead)}" if dead else "every floor can fail"),
    ]


# ── lane G — RC1, the shadowed-route ratchet (be #2992) ──────────────────

def _lane_shadow_ratchet() -> list:
    """★ 18 rule+method pairs have two handlers; Flask reaches only the first.

    Confirmed concretely: brain_layer9 registers at main.py:3817 and brain_qa at
    :41477, so routes/brain_qa.py:645 is unreachable code that still reads as
    live in source. Fixing all 18 at once would be its own risky patch wave, so
    #2992 ratchets instead — the count may fall, never rise.

    INVARIANT: the baseline is a ratchet. This lane fails if the pinned
    allowance ever INCREASES, which is the only way the ratchet can be defeated
    without anyone noticing — raising the number is a one-line change that
    looks like bookkeeping.
    """
    raw = _read("tests/app_contract.json")
    if not raw:
        return [_check("g_read", "app_contract.json present", None,
                       "not on this deploy yet (#2992)", critical=True)]
    try:
        base = json.loads(raw) or {}
    except Exception as e:  # noqa: BLE001
        return [_check("g_read", "app_contract.json parses", False,
                       f"{type(e).__name__}", critical=True)]
    allowed = base.get("max_shadowed_routes")
    return [
        _check("g_read", "app_contract.json parses", True,
               f"max_shadowed_routes={allowed} min_routes={base.get('min_routes')}",
               critical=True),
        _check("g_ratchet_held", "the shadow allowance has not been raised",
               isinstance(allowed, int) and allowed <= 18,
               f"baseline {allowed} vs the 18 measured 2026-08-20; a HIGHER "
               f"number means new dead handlers were admitted"),
    ]


_LANES = (
    ("A", "gate_enforcement", "gates are enforced, not advisory", _lane_gate_enforcement),
    ("B", "convergence", "recurrence is measured", _lane_convergence),
    ("C", "change_rate", "the change rate is bounded", _lane_change_rate),
    ("D", "brain_code_reach", "the brain's code lane is derived", _lane_brain_code_reach),
    ("E", "detector_precision", "refusal is not the only outcome", _lane_detector_precision),
    ("F", "coverage_floors", "scan floors are pinned and armed", _lane_coverage_floors),
    ("G", "shadow_ratchet", "the shadow ratchet holds", _lane_shadow_ratchet),
)


def _tick() -> dict:
    lanes = []
    for key, name, headline, fn in _LANES:
        try:
            checks = fn()
        except Exception as e:  # noqa: BLE001
            # A lane that raised is UNMEASURED, never green. Never let one
            # lane's failure 5xx the tick — see the kill-switch note.
            logger.warning("[stability_shell] lane %s raised: %s", key, e)
            checks = [_check(f"{key.lower()}_raised", "lane ran", None,
                             f"{type(e).__name__}: {str(e)[:160]}",
                             critical=True)]
        lanes.append({"lane": key, "name": name, "headline": headline,
                      "verdict": _lane_verdict(checks), "checks": checks})
    verdicts = [ln["verdict"] for ln in lanes]
    return {
        "ok": True,
        "shell": "stability",
        "number": 55,
        "report_only": True,
        "summary": {"PASS": verdicts.count("PASS"),
                    "FAIL": verdicts.count("FAIL"),
                    "?": verdicts.count("?")},
        "born_red": (
            "Lanes A, C, D and E were measured FAILING on 2026-08-20. A green "
            "lane on day one would mean the invariant was written to fit the "
            "defect."),
        "reading": (
            "'?' is NOT a soft pass — it means the read failed or no token was "
            "available, and the lane is unverified. Lanes pin invariants, not "
            "values: they go green when the property holds, not when a number "
            "matches."),
        "lanes": lanes,
    }


@stability_master_shell_bp.route(
    "/api/v1/admin/stability-shell/master-tick", methods=["GET"])
def master_tick():
    if _disabled():
        # ★ 404, never 503. The CF worker reads any 5xx from Railway as a dead
        # origin and fails the site over to the stale Render backend.
        return jsonify(ok=False, disabled=True,
                       hint="STABILITY_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_tick())
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


@stability_master_shell_bp.route("/admin/stability-shell", methods=["GET"])
@stability_master_shell_bp.route("/api/v1/admin/stability-shell", methods=["GET"])
def board():
    if _disabled():
        return jsonify(ok=False, disabled=True,
                       hint="STABILITY_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    t = _tick()
    colour = {"PASS": "#1a7f37", "FAIL": "#cf222e", "?": "#9a6700"}
    rows = []
    for ln in t["lanes"]:
        checks = "".join(
            f"<li><b>{'PASS' if c['pass'] is True else 'FAIL' if c['pass'] is False else '?'}</b> "
            f"{c['name']} — <span style='color:#57606a'>{c['detail']}</span></li>"
            for c in ln["checks"])
        rows.append(
            f"<section style='margin:18px 0;padding:12px 14px;border:1px solid #d0d7de;border-radius:8px'>"
            f"<h3 style='margin:0 0 6px'>Lane {ln['lane']} — {ln['name']} "
            f"<span style='color:{colour.get(ln['verdict'], '#57606a')}'>[{ln['verdict']}]</span></h3>"
            f"<div style='color:#57606a;margin-bottom:8px'>{ln['headline']}</div>"
            f"<ul style='margin:0'>{checks}</ul></section>")
    s = t["summary"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>DC Hub — Stability shell #55</title>"
        "<div style='font:14px/1.5 -apple-system,Segoe UI,sans-serif;"
        "max-width:900px;margin:32px auto;padding:0 16px'>"
        "<h1 style='margin:0'>Stability master shell #55</h1>"
        f"<p style='color:#57606a'>REPORT-ONLY · heals nothing · "
        f"PASS {s['PASS']} · FAIL {s['FAIL']} · ? {s['?']}</p>"
        f"<p style='color:#9a6700'>{t['born_red']}</p>"
        f"<p style='color:#57606a'>{t['reading']}</p>"
        + "".join(rows) + "</div>")
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-store"}

