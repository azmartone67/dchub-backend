"""routes/loop_closure_master_shell.py — the LOOP-CLOSURE Master Shell (#79,
2026-09-21).

★ WHAT THIS SHELL IS FOR
The 2026-09-21 audit named five open gaps between what the brain measures and
what it changes. This shell holds one lane per gap, and for each one it answers
the question the other shells leave implicit: WHO OWNS THE LEVER? Where nothing
was pulling an existing lever, the shell pulls it. Where the lever belongs to a
person, to another scheduler, or to a decision not yet made, the shell says so
by name instead of pretending to act.

`actuation_master_shell.py` (#39, 2026-07-28) wrote the diagnosis this answers:
"the apparatus that measures DC Hub is excellent and the apparatus that
CHANGES it is empty". 55 of 78 master shells carry a self-admission of
read-only / shadow / "fires nothing" in their own header.

★ WHAT BUILDING IT FOUND — the audit's five gaps, re-measured against main
  5. SPEC_DEBT  → ACTUATOR. brain_spec_implementer (be#5004, merged
                  2026-09-21) re-drives the code drafter with a landed spec's
                  own unchecked obligations. NOTHING SCHEDULES IT: only main.py
                  and the innovation board reference it. This lane drives it,
                  one spec per tick, oldest open obligation first.
  2. ACTIVATION → HUMAN. The automated nudge already runs daily
                  (.github/workflows/activation-nudge-daily.yml and
                  dchub-scheduler.py), and activation_desk.py already lists
                  "the ten people who paid and never called" with ESCALATE rows
                  where the nudge failed. A third caller of the same email would
                  add nothing; the remaining lever is a person.
  1. GRADIENT   → NOT AN ATTRIBUTION PROBLEM. The three bridge lanes are
                  correctly built; paid_total is 2. No join moves N. The fix
                  is to stop publishing a rate over two sales (made in
                  flask_mcp_endpoints in the same change) — this lane guards it.
  3. NEGATIVE   → ALREADY CONSUMED. brain_work_selector.class_success_weight
                  down-weights a class from its historical rate, with a FLOOR
                  that keeps a bad class explored. A second writer for the same
                  judgment would be the bug. This lane checks the negative
                  REACHES that ranker.
  4. OBJECTIVE  → A DECISION. /brain/value-shipped counts every merged PR at
                  x8 whether or not it worked. Its verdict feeds
                  brain_layer23_lifecycle and brain_self_test, so switching it
                  is a separate, deliberate change. This lane publishes the
                  verified-effect read so the gap is a number, not an argument.

★ ACTIONABLE, NOT MERELY WEAK. The weakest ACTIONABLE lane acts. A lane whose
lever is owned elsewhere is never selected: a lane whose score its own action
cannot move would otherwise stay weakest forever and starve every lane that
can act — the shell would go inert on its second tick.

★ THE INERT CHECK, ON ITSELF. `rag_master_shell` ran 78 ticks reporting
{"action":"none"} while its state endpoint said `mode: armed`. This shell
counts armed ticks where a lane was READY (actionable, and its lever's own arm
set) yet nothing acted; a full window of those emits loop_closure_shell_inert.
A tick with nothing ready is honestly idle, and is not counted against it.

★ NULL VERDICTS ARE NOT ZEROS. A rate under its sample floor is None with a
reason, never 0.0. An unmeasured lane is excluded from every contest, and the
exclusion is named.

★ NO CUSTOMER DATA IN SNAPSHOTS. Lane 2 reads the activation desk for COUNTS
only; the desk's rows carry emails and never leave this function.

Reads: loopback for the two PUBLISHED surfaces this shell grades (lanes 1, 4 —
grading a privately recomputed number could disagree with the surface it
exists to check), DB for the rest. Admin-gated. Fail-soft. One action per tick.
Kill:      LOOP_CLOSURE_DISABLED=1
Arm:       LOOP_CLOSURE_ARM=1          (shadow by default)
Per-lane:  LOOP_CLOSURE_LANE_<NAME>_OFF=1
Lane 5 additionally requires the implementer's own SPEC_IMPLEMENTER_ARM=1.

Endpoints:
  POST /api/v1/admin/loop-closure/master-tick  — measure → select → act
  GET  /api/v1/admin/loop-closure/state        — latest snapshot, attempts,
                                                  inert check
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

loop_closure_master_shell_bp = Blueprint("loop_closure_master_shell", __name__)

LANES = ("gradient", "activation", "negative", "objective", "spec_debt")

# A rate under its floor is UNMEASURED. 0.0% over N=2 and 0.0% over N=200 are
# different facts that would otherwise be published with the same digits.
GRADIENT_SAMPLE_FLOOR = int(os.environ.get("LOOP_CLOSURE_GRADIENT_FLOOR", "10"))
ACTIVATION_SAMPLE_FLOOR = int(os.environ.get("LOOP_CLOSURE_ACTIVATION_FLOOR", "20"))
NEGATIVE_SAMPLE_FLOOR = int(os.environ.get("LOOP_CLOSURE_NEGATIVE_FLOOR", "20"))
INERT_TICKS = int(os.environ.get("LOOP_CLOSURE_INERT_TICKS", "5"))
SPEC_RETRY_COOLDOWN_DAYS = int(os.environ.get("LOOP_CLOSURE_SPEC_COOLDOWN_DAYS", "7"))
_SPEC_SCAN_CAP = 400  # the corpus is ~380 specs; this bounds a runaway, not the scan


# ── auth / gates (house pattern, mirrors reliability_master_shell) ────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes")


def _disabled() -> bool:
    return _truthy(os.environ.get("LOOP_CLOSURE_DISABLED"))


def _armed() -> bool:
    """SHADOW by default — arming is a deliberate operator decision."""
    return _truthy(os.environ.get("LOOP_CLOSURE_ARM"))


def _lane_off(name: str) -> bool:
    return _truthy(os.environ.get(f"LOOP_CLOSURE_LANE_{name.upper()}_OFF"))


# ── db ────────────────────────────────────────────────────────────────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _fetchone(cur, sql, args=None, default=None):
    try:
        # psycopg2 applies %-formatting whenever a params argument is passed,
        # even an empty tuple — so pass it only when there are params.
        cur.execute(sql) if args is None else cur.execute(sql, args)
        row = cur.fetchone()
        return row if row is not None else default
    except Exception:
        return default


def _fetchall(cur, sql, args=None, default=None):
    """Rows or `default` — a table this shell creates may not exist yet on a
    first read, and that is 'nothing written yet', not an error."""
    try:
        cur.execute(sql) if args is None else cur.execute(sql, args)
        return cur.fetchall()
    except Exception:
        return default


def _close(c):
    try:
        c.close()
    except Exception:
        pass


# ── loopback read of a PUBLISHED surface ──────────────────────────────
# In-process, so it bypasses Cloudflare: the edge may hold a cached copy of the
# very number a lane is grading.
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE",
                        "https://dchub-backend-production.up.railway.app")
)


def _loopback_json(path: str, timeout: int = 30):
    """GET one of our own published payloads. None on any failure — the lane
    then scores None and is excluded, never scored 0."""
    import requests  # house rule: requests, not urllib, on Railway
    url = path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path
    headers = {"X-DC-Probe": "loop-closure-tick",  # rate-limiter bypass
               "User-Agent": "dchub-loop-closure-shell/1.0"}
    ak = _admin_key()
    if ak:
        headers["X-Admin-Key"] = ak
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


# ── rate helper: the floor is the whole point ─────────────────────────
def rate_or_none(numerator, denominator, floor: int):
    """(rate_pct, basis). None when the denominator cannot support a rate."""
    if numerator is None or denominator is None:
        return None, "unmeasured: no reading"
    try:
        n, d = int(numerator), int(denominator)
    except Exception:
        return None, "unmeasured: non-numeric reading"
    if d < floor:
        return None, f"denominator {d} below floor {floor} — no rate published"
    if d == 0:
        return None, "denominator 0 — no rate published"
    return round(100.0 * n / d, 2), f"{n} of {d}"


def _score(rate_pct):
    """0-100, or None. ★ None is NOT 0: a lane scoring 0 because its reading
    failed would win every contest."""
    if rate_pct is None:
        return None
    return max(0.0, min(100.0, round(float(rate_pct), 2)))


def _lane(name: str, **kw) -> dict:
    """Every lane reports the same shape, so no reader has to special-case one.

    owner       who holds the lever for this gap, by name
    actionable  THIS shell has a lever it can pull for the gap
    ready       actionable AND that lever's own arm is set
    """
    out = {"lane": name, "measured": False, "score": None, "rate_basis": None,
           "actionable": False, "ready": False, "owner": None,
           "why_not_actionable": None, "detail": {}}
    out.update(kw)
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 1 — GRADIENT: is the published attribution rate a measurement?
# ═══════════════════════════════════════════════════════════════════════
def lane_gradient(funnel: dict | None = None) -> dict:
    """Guards the published paid_signal_attribution_30d block.

    The bridge lanes are correctly built (handoff_definition's third lane,
    2026-09-17, reuses the same click->session builder paid_attributed joins
    on). They read 0 because paid_total is 2. So the gap is not a missing link
    — it is a rate published over two sales, which the brain's own L6 citation
    gate and scaffold reconciler read as evidence. This lane checks the surface
    withholds that rate below its floor.
    """
    out = _lane("gradient", owner="conversion volume — no attribution change "
                                  "moves paid_total",
                why_not_actionable="the lever is sales, upstream of this lane")
    if funnel is None:
        funnel = _loopback_json("/api/v1/mcp/funnel")
    psa = (funnel or {}).get("paid_signal_attribution_30d") \
        if isinstance(funnel, dict) else None
    if not isinstance(psa, dict):
        out["detail"]["error"] = "published paid_signal_attribution_30d unavailable"
        return out
    paid, bridged = psa.get("paid_total"), psa.get("bridged_to_signal")
    published = psa.get("attribution_rate_pct")
    out.update(measured=True, paid_total=paid, bridged=bridged,
               published_rate=published)
    rate, basis = rate_or_none(bridged, paid, GRADIENT_SAMPLE_FLOOR)
    out["score"], out["rate_basis"] = _score(rate), basis
    below = isinstance(paid, int) and paid < GRADIENT_SAMPLE_FLOOR
    # ★ The guard: under the floor the surface must publish None. A number
    # there is the defect this lane exists to catch, whoever reintroduces it.
    out["published_rate_honest"] = (published is None) if below else True
    if below and published is not None:
        out["detail"]["regression"] = (
            f"attribution_rate_pct={published} published over paid_total "
            f"{paid}, below floor {GRADIENT_SAMPLE_FLOOR}")
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 2 — ACTIVATION: paying accounts that never made a call
# ═══════════════════════════════════════════════════════════════════════
def lane_activation(desk: dict | None = None) -> dict:
    """Reads activation_desk — the ONE reader of this population — for counts.

    ★ The earlier draft of this lane measured free mcp_dev_keys against
    mcp_call_log while firing a nudge whose query targets PAYING `users` with
    zero calls on `api_keys`: different tables, tiers and definitions of
    "activated", so its own action could never move its own score. The nudge is
    also already scheduled twice. Reading the desk keeps one population, one
    reader, and names the actual remaining lever: a person.

    ★ COUNTS ONLY. The desk's rows carry customer emails; they never enter the
    returned dict, the tick response or the snapshot.
    """
    out = _lane("activation",
                owner="human — /activation-desk (the automated nudge already "
                      "runs daily via activation-nudge-daily.yml)",
                why_not_actionable="a third sender of the same one-time email "
                                   "adds nothing; the next rung is personal "
                                   "outreach")
    if desk is None:
        try:
            from routes.activation_desk import _payload
            desk = _payload()
        except Exception as e:
            out["detail"]["error"] = f"activation desk unavailable: {str(e)[:120]}"
            return out
    if not isinstance(desk, dict) or not desk.get("ok"):
        out["detail"]["error"] = ((desk or {}).get("note")
                                  or "activation desk returned no reading")
        return out
    try:
        open_total = int(desk.get("open_total") or 0)
        never = int(desk.get("never_contacted") or 0)
    except Exception as e:
        out["detail"]["error"] = str(e)[:160]
        return out
    seg = desk.get("by_segment") if isinstance(desk.get("by_segment"), dict) else {}
    out.update(measured=True, paid_never_called=open_total,
               never_contacted=never,
               by_segment={str(k): int(v) for k, v in seg.items()
                           if isinstance(v, (int, float))})
    rate, basis = rate_or_none(open_total - never, open_total,
                               ACTIVATION_SAMPLE_FLOOR)
    out["score"], out["rate_basis"] = _score(rate), basis
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 3 — NEGATIVE: does a verified failure reach the ranker?
# ═══════════════════════════════════════════════════════════════════════
def applied_weight_for(pattern: str):
    """The multiplier the live ranker applies to this class, or None.
    ★ Read from the consumer, never recomputed."""
    try:
        from routes.brain_work_selector import class_success_weight
        return float(class_success_weight(pattern))
    except Exception:
        return None


_WEIGHT_TOLERANCE = 0.03


def expected_weight_for(rate: float, samples: int):
    """What the ranker's OWN formula gives a class with these outcomes.

    ★ The first live tick (2026-09-21 06:55Z) showed why this exists. The
    worst class was brain_spec_pr: 8 of 44 failed, an 82% success rate, and
    the ranker applied 1.1545. An earlier verdict of `applied < 1.0` reported
    that as "the negative never reached the ranker" — but _soft_greedy maps the
    NEUTRAL rate 0.625 to 1.0, so a class above it is SUPPOSED to be boosted.
    The honest question is whether the applied weight is what the formula
    prescribes for the measured outcomes, not which side of 1.0 it sits on.
    """
    try:
        from routes.brain_work_selector import _soft_greedy
        return round(float(_soft_greedy(rate, samples)), 4)
    except Exception:
        return None


def outcomes_reached_verdict(applied, expected):
    """(reached, why). True when the ranker applies what its formula gives.

    None, never False, when the answer is unknowable: an unread ranker, an
    uncomputable expectation, or an expectation so close to NEUTRAL that a
    missed read (which falls back to NEUTRAL) would look identical.
    """
    if applied is None:
        return None, "could not read the ranker's applied weight"
    if expected is None:
        return None, "could not compute the ranker's expected weight"
    try:
        from routes.brain_work_selector import WORK_NEUTRAL as neutral
    except Exception:
        neutral = 1.0
    if abs(expected - float(neutral)) <= _WEIGHT_TOLERANCE:
        return None, ("expected weight is within tolerance of NEUTRAL, so a "
                      "missed read would look the same")
    if abs(applied - expected) <= _WEIGHT_TOLERANCE:
        return True, "applied weight matches the ranker's formula for these outcomes"
    return False, (f"applied {applied} but the formula gives {expected} for "
                   f"these outcomes — they are not reaching the ranker")


def _worst_class():
    """The fix class the ranker should down-weight hardest — read with the
    RANKER'S OWN columns and window (brain_work_selector._read_class_rate:
    brain_fix_outcomes.klass / .resolved / .verified_at, WORK_WINDOW_DAYS).

    ★ An earlier draft queried `pattern` / `succeeded` on this table. Those are
    autopilot_outcomes' columns, not this one's; the query would have failed on
    the first live tick and the lane would have gone quietly unmeasured.
    """
    try:
        from routes.brain_work_selector import WORK_WINDOW_DAYS as window
    except Exception:
        window = 30
    c = _conn()
    if c is None:
        return {"available": False, "why": "no db connection"}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT LOWER(COALESCE(klass,'')) AS k,
                       COUNT(*) FILTER (WHERE resolved IS NOT NULL) AS graded,
                       COUNT(*) FILTER (WHERE resolved IS FALSE) AS failed
                  FROM brain_fix_outcomes
                 WHERE verified_at >= NOW() - (%s || ' days')::interval
                   AND NULLIF(klass,'') IS NOT NULL
                 GROUP BY 1
                HAVING COUNT(*) FILTER (WHERE resolved IS NOT NULL) >= 5
                 ORDER BY COUNT(*) FILTER (WHERE resolved IS FALSE)::float
                          / NULLIF(COUNT(*) FILTER (WHERE resolved IS NOT NULL), 0)
                          DESC, 2 DESC
                 LIMIT 1
            """, (str(window),))
            row = cur.fetchone()
    except Exception as e:
        # Observation, not conclusion: if this is a missing column, the
        # ranker's PRIMARY read uses the same ones and falls back silently.
        return {"available": False,
                "why": f"per-class outcome read failed: {str(e)[:120]}"}
    finally:
        _close(c)
    if not row:
        return {"available": False,
                "why": "no class has 5 graded outcomes in the window"}
    graded, failed = int(row[1]), int(row[2])
    rate = (graded - failed) / float(graded) if graded else None
    applied = applied_weight_for(row[0])
    expected = None if rate is None else expected_weight_for(rate, graded)
    reached, why = outcomes_reached_verdict(applied, expected)
    return {"available": True, "klass": row[0], "graded": graded,
            "failed": failed,
            "success_rate": None if rate is None else round(rate, 4),
            "applied_weight": applied, "expected_weight": expected,
            "outcomes_reached_ranker": reached, "verdict_basis": why}


def lane_negative(effectiveness: dict | None = None) -> dict:
    """Verified fix-success from the PUBLISHED /brain/effectiveness block,
    plus whether the worst class is actually down-weighted by the ranker.

    ★ UNGRADED IS NOT FAILED. The published block separates checks_performed
    (210) from fix_succeeded + fix_failed (199); the rate is over the graded.
    """
    out = _lane("negative", owner="brain_work_selector.class_success_weight",
                why_not_actionable="the ranker already consumes outcomes; a "
                                   "second writer would compete with it")
    if effectiveness is None:
        effectiveness = _loopback_json("/api/v1/brain/effectiveness")
    ov = (effectiveness.get("outcome_verification_30d")
          if isinstance(effectiveness, dict) else None)
    if not isinstance(ov, dict):
        out["detail"]["error"] = "published outcome_verification_30d unavailable"
        return out
    try:
        succ, failed = int(ov.get("fix_succeeded")), int(ov.get("fix_failed"))
    except Exception:
        out["detail"]["error"] = "outcome_verification_30d carried no graded counts"
        return out
    graded = succ + failed
    try:
        checks = int(ov.get("checks_performed"))
    except Exception:
        checks = graded
    out.update(measured=True, graded=graded, succeeded=succ, failed=failed,
               ungraded=max(0, checks - graded))
    rate, basis = rate_or_none(succ, graded, NEGATIVE_SAMPLE_FLOOR)
    out["score"], out["rate_basis"] = _score(rate), basis
    out["success_rate"] = (None if rate is None else rate / 100.0)
    out["worst_class"] = _worst_class()
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 4 — OBJECTIVE: how much of the published value score is verified?
# ═══════════════════════════════════════════════════════════════════════
def verified_value_score(shipped_30d: dict, weights: dict, success_rate):
    """(verified_score, total_score) from the PUBLISHED counts and weights.

    ★ A merged PR is OUTPUT. value-shipped counts code_fixes = merged /pull/
    PRs at their full weight; whether each worked is recorded in
    brain_fix_outcomes. So code_fixes enter the verified score discounted by the
    verified success rate. conversions (Stripe) and autopilot_actions
    (outcome_verified IS TRUE: "a remediation whose KPI was re-checked and
    confirmed") are verified at the source. Media and discovery are output.
    Returns (None, total) when the success rate is unmeasurable — a discount
    that cannot be computed must not silently become 1.0 or 0.0.
    """
    def w(k):
        return float(weights.get(k, 0) or 0)

    def n(k):
        v = shipped_30d.get(k, 0)
        return float(v) if isinstance(v, (int, float)) else 0.0

    total = sum(w(k) * n(k) for k in shipped_30d)
    if success_rate is None:
        return None, round(total, 1)
    verified = (w("conversions") * n("conversions")
                + w("autopilot_actions") * n("autopilot_actions")
                + w("code_fixes") * n("code_fixes") * float(success_rate))
    return round(verified, 1), round(total, 1)


def lane_objective(shipped: dict | None = None, success_rate=None) -> dict:
    out = _lane("objective",
                owner="operator decision — switching /brain/value-shipped's "
                      "verdict to the verified read (it feeds "
                      "brain_layer23_lifecycle and brain_self_test)",
                why_not_actionable="changing a published verdict two brain "
                                   "layers read is a decision, not a tick")
    if shipped is None:
        shipped = _loopback_json("/api/v1/brain/value-shipped")
    data = shipped.get("shipped_30d") if isinstance(shipped, dict) else None
    weights = shipped.get("weights") if isinstance(shipped, dict) else None
    if not isinstance(data, dict) or not isinstance(weights, dict):
        out["detail"]["error"] = "value-shipped payload unavailable"
        return out
    verified, total = verified_value_score(data, weights, success_rate)
    out.update(measured=verified is not None, value_score=total,
               verified_score=verified,
               published_verdict=shipped.get("verdict"))
    if verified is None:
        out["detail"]["error"] = ("verified fix-success rate unmeasurable — "
                                  "no discount computed")
        return out
    rate, basis = rate_or_none(verified, total, 1)
    out["score"], out["rate_basis"] = _score(rate), basis
    out["detail"]["basis"] = ("same counts and weights as /brain/value-shipped; "
                              "merged PRs discounted by verified success rate; "
                              "media and discovery excluded")
    return out


# ═══════════════════════════════════════════════════════════════════════
# LANE 5 — SPEC_DEBT: the one lever nothing was pulling
# ═══════════════════════════════════════════════════════════════════════
def _attempted_recently(cur, doc: str) -> bool:
    row = _fetchone(cur, """
        SELECT 1 FROM loop_closure_spec_attempts
         WHERE doc = %s
           AND (last_acted IS TRUE
                OR last_attempt_at > NOW() - (%s || ' days')::interval)
    """, (doc, str(SPEC_RETRY_COOLDOWN_DAYS)))
    return bool(row)


def _attempted_docs(docs) -> set:
    """Docs driven recently (or successfully). Empty on any read failure — a
    missing table is 'nothing attempted yet'."""
    docs = [d for d in docs if d]
    if not docs:
        return set()
    c = _conn()
    if c is None:
        return set()
    try:
        with c.cursor() as cur:
            return {d for d in docs if _attempted_recently(cur, d)}
    except Exception:
        return set()
    finally:
        _close(c)


def lane_spec_debt(scan: dict | None = None, attempted=None) -> dict:
    """Consumes brain_spec_debt.scan_corpus(); selects the oldest open
    obligation not already driven; is READY only when the implementer's own
    arm is set.

    ★ UNMEASURED IS NOT ZERO DEBT — that module's own rule, kept: its
    `counts` is None when the corpus cannot be read.
    """
    out = _lane("spec_debt", owner="brain_spec_implementer (be#5004) — "
                                   "driven by this lane")
    if scan is None:
        try:
            from routes.brain_spec_debt import scan_corpus
            scan = scan_corpus()
        except Exception as e:
            out["detail"]["error"] = f"spec-debt scan unavailable: {str(e)[:150]}"
            return out
    if not isinstance(scan, dict) or str(scan.get("state") or "").upper() != "MEASURED":
        out["detail"]["error"] = (f"spec-debt scan state="
                                  f"{(scan or {}).get('state')!r} — unmeasured, "
                                  f"not zero debt")
        return out
    counts = scan.get("counts")
    if not isinstance(counts, dict):
        out["detail"]["error"] = "MEASURED scan carried no counts mapping"
        return out
    o, cl, u = (int(counts.get(k) or 0) for k in ("open", "closed", "unknown"))
    total = int(counts.get("total_docs") or (o + cl + u))
    if total == 0:
        out["detail"]["error"] = "empty corpus — unmeasured, not zero debt"
        return out
    out.update(measured=True, open=o, closed=cl, unknown=u, total=total)
    rate, basis = rate_or_none(cl, total, 1)
    out["score"], out["rate_basis"] = _score(rate), basis

    # Already sorted oldest-first by brain_spec_debt, undated docs LAST.
    obligations = scan.get("open_obligations")
    obligations = obligations if isinstance(obligations, list) else []
    if attempted is None:
        attempted = _attempted_docs([r.get("doc") for r in obligations
                                     if isinstance(r, dict)])
    try:
        from routes.brain_spec_implementer import _armed as _impl_armed
        from routes.brain_spec_implementer import _disabled as _impl_disabled
        from routes.brain_spec_implementer import plan_for_spec
        impl_disabled, impl_armed = _impl_disabled(), _impl_armed()
    except Exception as e:
        out["why_not_actionable"] = f"implementer unavailable: {str(e)[:120]}"
        return out
    if impl_disabled:
        out["why_not_actionable"] = "SPEC_IMPLEMENTER_DISABLE=1"
        return out

    # ★ The oldest open spec the implementer would ACT on — asked of the
    # implementer itself, never inferred. The first live tick targeted
    # agenda-41, the oldest open spec, which the implementer declines:
    # "triage recorded BLOCKED — this spec waits on an owner decision, not on
    # code" (1 of 242 open specs). Armed, the lane would have spent each day's
    # single action on it. A BLOCKED spec is the owner's lever, so it is
    # skipped AND named, never silently passed over.
    target, blocked, scanned = None, [], 0
    for r in obligations:
        if not isinstance(r, dict) or not r.get("doc") or r.get("doc") in attempted:
            continue
        if scanned >= _SPEC_SCAN_CAP:
            break
        scanned += 1
        try:
            plan = plan_for_spec(r["doc"])
        except Exception:
            plan = {}
        plan = plan if isinstance(plan, dict) else {}
        if plan.get("blocked"):
            blocked.append(r["doc"])
        elif plan.get("would_act") and target is None:
            target = r
    out.update(specs_scanned=scanned, blocked_on_owner_count=len(blocked),
               blocked_on_owner=blocked[:10])
    if target is None:
        out["why_not_actionable"] = (
            f"no open spec the implementer would act on ({scanned} scanned, "
            f"{len(blocked)} blocked on an owner decision)")
        return out
    out["target"] = {"doc": target.get("doc"), "title": target.get("title"),
                     "age_days": target.get("age_days"),
                     "unchecked_items": target.get("unchecked_items")}
    out["actionable"] = True
    out["ready"] = bool(impl_armed)
    if not impl_armed:
        out["why_not_ready"] = ("SPEC_IMPLEMENTER_ARM unset — the implementer's "
                                "own arm is the operator's decision")
    return out


def attempt_note(res: dict) -> str:
    """Why an implementer call did or did not open a PR, in one line.

    ★ The first armed tick (2026-09-23) recorded `acted=false, note=""`: the
    implementer's own `note` is set only on its dry/unarmed paths, while the
    drafter's refusal (`rationale`), gate closure (`reason`) and failures
    (`error`) live on the nested `pr` dict or on `error`. Reading `note` alone
    threw every one of them away, so a refused spec looked identical to a
    silent no-op and the next reader had nothing to act on.
    """
    res = res if isinstance(res, dict) else {}
    pr = res.get("pr") if isinstance(res.get("pr"), dict) else {}
    for label, val in (("", res.get("note")),
                       ("refused: ", pr.get("rationale")),
                       ("gate: ", pr.get("reason")),
                       ("drafter error: ", pr.get("error")),
                       ("error: ", res.get("error")),
                       ("", res.get("reason"))):
        if val:
            return f"{label}{val}"[:200]
    if pr.get("pr_url"):
        return f"opened {pr['pr_url']}"[:200]
    return ""


def act_spec_debt(measured: dict, dry: bool) -> dict:
    """Drive brain_spec_implementer for ONE landed spec.

    ★ TWO ARMS, BOTH REQUIRED. implement_spec(apply=True) itself refuses to
    open a PR unless SPEC_IMPLEMENTER_ARM=1, so an armed loop-closure tick
    cannot open one on the shell's arm alone. A DRY tick passes apply=False and
    records nothing — recording a dry "attempt" would make the first real tick
    skip the very spec it previewed.
    """
    target = measured.get("target") or {}
    doc = target.get("doc")
    if not doc:
        return {"ok": True, "acted": False, "reason": "no target"}
    try:
        from routes.brain_spec_implementer import implement_spec
    except Exception as e:
        return {"ok": False, "acted": False,
                "reason": f"implementer unavailable: {str(e)[:120]}"}
    try:
        res = implement_spec(doc, apply=not dry)
    except Exception as e:
        return {"ok": False, "acted": False, "reason": str(e)[:200]}
    res = res if isinstance(res, dict) else {"raw": str(res)[:200]}
    acted = bool(res.get("acted")) and not dry
    pr = res.get("pr") if isinstance(res.get("pr"), dict) else {}
    summary = {"ok": bool(res.get("ok", True)), "acted": acted, "doc": doc,
               "dry": dry, "note": attempt_note(res),
               "pr": pr.get("pr_url"),
               "action": "drove brain_spec_implementer for one landed spec"}
    if dry:
        summary["reason"] = "dry (LOOP_CLOSURE_ARM unset)"
        return summary
    _record_attempt(doc, acted, summary["note"])
    return summary


def _record_attempt(doc: str, acted: bool, note: str) -> None:
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loop_closure_spec_attempts (
                    doc             TEXT PRIMARY KEY,
                    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_acted      BOOLEAN NOT NULL DEFAULT FALSE,
                    last_note       TEXT,
                    attempts        INTEGER NOT NULL DEFAULT 1
                )
            """)
            # Values BOUND, never inlined: regression_lint's
            # insert-no-on-conflict regex window is [^;"']* and stops at the
            # first quote, so an inline literal would hide this ON CONFLICT.
            cur.execute("""
                INSERT INTO loop_closure_spec_attempts
                       (doc, last_attempt_at, last_acted, last_note, attempts)
                VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s, %s, 1)
                ON CONFLICT (doc) DO UPDATE
                   SET last_attempt_at = NOW(),
                       last_acted = EXCLUDED.last_acted,
                       last_note = EXCLUDED.last_note,
                       attempts = loop_closure_spec_attempts.attempts + 1
            """, (doc, acted, (note or "")[:500]))
        try:
            c.commit()
        except Exception:
            pass
    except Exception:
        try:
            from routes._swallowed_writes import note_swallowed_write
            note_swallowed_write("loop_closure_spec_attempts")
        except Exception:
            pass
    finally:
        _close(c)


ACTUATORS = {"spec_debt": act_spec_debt}


# ═══════════════════════════════════════════════════════════════════════
# selection + the inert check
# ═══════════════════════════════════════════════════════════════════════
def pick_weakest(lanes: dict):
    """(lane_name, excluded, skipped) — the lowest-scoring ACTIONABLE lane.

    excluded  unmeasured or killed — not in any contest
    skipped   measured, but its lever belongs to someone else (named)

    ★ Actionability is required, not just weakness: otherwise a lane whose
    own action cannot move its score is selected forever.
    ★ Ties break on LANES order, so the selection is deterministic.
    """
    excluded, skipped, scored = [], [], []
    for name in LANES:
        lane = lanes.get(name) or {}
        if _lane_off(name):
            excluded.append({"lane": name, "why": "lane kill switch set"})
            continue
        score = lane.get("score")
        if score is None:
            excluded.append({"lane": name,
                             "why": (lane.get("detail") or {}).get("error")
                                    or lane.get("rate_basis") or "unmeasured"})
            continue
        if not lane.get("actionable") or name not in ACTUATORS:
            skipped.append({"lane": name, "owner": lane.get("owner"),
                            "why": lane.get("why_not_actionable")
                                   or "no lever this shell can pull"})
            continue
        scored.append((float(score), LANES.index(name), name))
    if not scored:
        return None, excluded, skipped
    scored.sort()
    return scored[0][2], excluded, skipped


def inert_check(limit: int = None) -> dict:
    """Armed, a lane READY, and nothing acted — for a full window of ticks?

    ★ Only READY ticks count. A tick where no lane could act is honestly idle
    (like the review gate that is idle because no brain PR was ever closed),
    and alarming on it would make this a permanent false positive.
    """
    n = int(limit or INERT_TICKS)
    out = {"check": "loop_closure_shell_inert", "fired": False, "window": n,
           "armed_ticks": 0, "ready_unacted_ticks": 0, "measured": False}
    c = _conn()
    if c is None:
        out["reason"] = "no db connection"
        return out
    try:
        with c.cursor() as cur:
            row = _fetchone(cur, """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE ready_lanes > 0 AND acted IS NOT TRUE)
                  FROM (SELECT ready_lanes, acted FROM loop_closure_snapshots
                         WHERE armed IS TRUE
                         ORDER BY computed_at DESC LIMIT %s) t
            """, (n,))
    except Exception as e:
        out["reason"] = str(e)[:200]
        return out
    finally:
        _close(c)
    if not row:
        out["reason"] = "snapshot history unreadable"
        return out
    out["armed_ticks"], out["ready_unacted_ticks"] = int(row[0]), int(row[1])
    out["measured"] = True
    if out["armed_ticks"] < n:
        out["reason"] = (f"only {out['armed_ticks']} armed ticks on record "
                         f"(window {n}) — not yet measurable")
    elif out["ready_unacted_ticks"] == out["armed_ticks"]:
        out["fired"] = True
        out["detail"] = (f"{n} consecutive armed ticks had a READY lane and "
                         f"took zero actions — this shell is behaving as the "
                         f"read-only shells it was built to replace")
    return out


# ═══════════════════════════════════════════════════════════════════════
# tick
# ═══════════════════════════════════════════════════════════════════════
def run_tick(dry_override: bool = None) -> dict:
    started = datetime.now(timezone.utc)
    armed = _armed() if dry_override is None else (not dry_override)
    dry = not armed
    # ★ Before inert_check reads the history. On the first live tick the table
    # did not exist yet, so the check reported "snapshot history unreadable"
    # when the truth was "no history yet".
    _ensure_tables()

    negative = lane_negative()
    lanes = {
        "gradient": lane_gradient(),
        "activation": lane_activation(),
        "negative": negative,
        "objective": lane_objective(success_rate=negative.get("success_rate")),
        "spec_debt": lane_spec_debt(),
    }
    weakest, excluded, skipped = pick_weakest(lanes)
    if weakest:
        result = ACTUATORS[weakest](lanes[weakest], dry)
    else:
        result = {"ok": True, "acted": False,
                  "reason": "no actionable lane — every lever is owned elsewhere "
                            "or has nothing to do"}

    scores = [l["score"] for l in lanes.values() if l.get("score") is not None]
    ready = sum(1 for l in lanes.values() if l.get("ready"))
    snapshot = {
        "ok": True,
        "generated_at": started.isoformat(),
        "armed": armed,
        "loop_score": round(sum(scores) / len(scores), 2) if scores else None,
        "loop_score_basis": (f"mean of {len(scores)} measured lanes"
                             if scores else "no lane measurable"),
        "weakest_actionable_lane": weakest,
        "ready_lanes": ready,
        "excluded_lanes": excluded,
        "skipped_lanes": skipped,
        "action": result,
        "acted": bool(result.get("acted")),
        "lanes": lanes,
        "inert_check": inert_check(),
    }
    _persist(snapshot)
    return snapshot


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loop_closure_snapshots (
                    id            SERIAL PRIMARY KEY,
                    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    loop_score    NUMERIC(6,2),
                    armed         BOOLEAN,
                    weakest_lane  TEXT,
                    action_taken  TEXT,
                    action_ok     BOOLEAN,
                    acted         BOOLEAN NOT NULL DEFAULT FALSE,
                    ready_lanes   INTEGER NOT NULL DEFAULT 0,
                    lane_scores   JSONB,
                    detail        JSONB
                )
            """)
        try:
            c.commit()
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        _close(c)


_SNAPSHOT_KEEP = ("generated_at", "armed", "loop_score",
                  "weakest_actionable_lane", "ready_lanes", "acted")


def _detail_json(snapshot: dict) -> str:
    """The snapshot as JSON that is VALID AT ANY SIZE.

    ★ An earlier draft bound `json.dumps(snapshot)[:60000]`: past 60,000
    characters that is cut mid-string, Postgres rejects it for a jsonb column,
    the fail-soft handler swallows the error, and the snapshot is silently
    never written — which would also blind inert_check, since it reads this
    table. util.json_column.json_for_column is the house fix for exactly that
    (tests/test_json_column_binding.py).
    """
    try:
        from util.json_column import json_for_column
        return json_for_column(snapshot, max_chars=60000, keep_keys=_SNAPSHOT_KEEP)
    except Exception:
        return json.dumps({k: snapshot.get(k) for k in _SNAPSHOT_KEEP}, default=str)


def _persist(snapshot: dict) -> None:
    if not _ensure_tables():
        return
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO loop_closure_snapshots
                       (loop_score, armed, weakest_lane, action_taken, action_ok,
                        acted, ready_lanes, lane_scores, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (snapshot.get("loop_score"), snapshot.get("armed"),
                  snapshot.get("weakest_actionable_lane"),
                  (snapshot.get("action") or {}).get("action"),
                  (snapshot.get("action") or {}).get("ok"),
                  snapshot.get("acted"), snapshot.get("ready_lanes") or 0,
                  json.dumps({k: v.get("score") for k, v in
                              (snapshot.get("lanes") or {}).items()}),
                  _detail_json(snapshot)))
        try:
            c.commit()
        except Exception:
            pass
    except Exception:
        try:
            from routes._swallowed_writes import note_swallowed_write
            note_swallowed_write("loop_closure_snapshots")
        except Exception:
            pass
    finally:
        _close(c)


def _actuator_output() -> dict:
    """Read back what the actuator WROTE. An actuator whose output nothing
    reads is not actuation — it is a write-only table."""
    out = {"spec_attempts": None}
    c = _conn()
    if c is None:
        return out
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            rows = _fetchall(cur,
                             "SELECT doc, last_attempt_at, last_acted, last_note,"
                             " attempts FROM loop_closure_spec_attempts"
                             " ORDER BY last_attempt_at DESC LIMIT 10") or []
            out["spec_attempts"] = [dict(r) for r in rows]
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        _close(c)
    return out


# ── endpoints ─────────────────────────────────────────────────────────
@loop_closure_master_shell_bp.route("/api/v1/admin/loop-closure/master-tick",
                                    methods=["POST", "GET"])
def loop_closure_master_tick():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    if _disabled():
        return jsonify({"ok": False, "disabled": True,
                        "kill": "LOOP_CLOSURE_DISABLED"}), 200
    dry_override = True if request.args.get("dryrun") == "1" else None
    return jsonify(run_tick(dry_override=dry_override)), 200


@loop_closure_master_shell_bp.route("/api/v1/admin/loop-closure/state",
                                    methods=["GET"])
def loop_closure_state():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no db connection"}), 200
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT computed_at, loop_score, armed, weakest_lane,
                       action_taken, action_ok, acted, ready_lanes, lane_scores
                  FROM loop_closure_snapshots
                 ORDER BY computed_at DESC LIMIT 1
            """)
            row = cur.fetchone()
        return jsonify({"ok": True, "armed_now": _armed(),
                        "latest": dict(row) if row else None,
                        "actuator_output": _actuator_output(),
                        "inert_check": inert_check()}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        _close(c)
