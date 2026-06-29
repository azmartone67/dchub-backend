"""brain_honesty_reconciliation.py — Feature #5 (2026-06-28)

SELF-AUDIT / HONESTY-RECONCILIATION DETECTOR.

A NEW read-only sibling to the brain_consistency_radar.py detectors
(e.g. check_heartbeat_surfaces_stale). It cross-checks the brain's own
self-narrative for internal contradictions and surfaces them as
brain_findings rows so they escalate to the human digest (no autopilot
action map → finding-only → human).

Two checks, exposed as ONE detector entrypoint
`check_honesty_reconciliation()` so the radar registers a single
function:

  (a) VERDICT DISAGREEMENT — the longitudinal evolution verdict
      (brain_evolution.compute_evolution_snapshot → "verdict":
      quiet/warming/steady/ascending) and the self-model verdict
      (brain_self_model.compute_self_model → "self_assessment":
      insufficient/degraded/mixed/healthy) are mapped onto a shared
      0..3 optimism scale and compared. If they diverge by >= 2 ranks
      (e.g. evolution says "ascending" while self-model says
      "degraded") the brain is telling itself two different stories —
      file a finding. This is the exact 74-"ascending"-vs-34%-real
      split the R3-honesty re-base tried to close; this detector is the
      backstop that screams if it ever reopens.

  (b) ACTING-BUT-NEVER-LANDING — a per-pattern probe of
      brain_autopilot_actions vs autopilot_outcomes. For each pattern
      with executed_ok >= 5 AND a verified sample (succeeded TRUE/FALSE)
      >= 5, if the verified-success rate is 0% the brain is firing an
      action that NEVER produces a real effect. File one finding per
      offending pattern. Gate is intentionally conservative
      (executed_ok>=5 AND total_verified>=5) so a tiny noisy sample
      never fires.

DARK-BY-DEFAULT: this is a READ-ONLY detector. It only RETURNS finding
dicts (additive brain_findings INSERTs via the standard radar pipeline,
deduped on UNIQUE(issue,url)). It executes no behavior, fires no action,
sends no email, and writes nothing itself, so per the operator rules it
MAY be live. There is no behavior flag to gate. Every DB access and
import is wrapped so any exception degrades to "no findings" — it can
never break a radar tick.
"""

from typing import Optional

# Tolerance band (a): minimum rank gap between the two verdicts before
# we call it a disagreement. 2 = "ascending vs mixed" or worse.
_VERDICT_DISAGREE_BAND = 2

# Gates for (b).
_MIN_EXECUTED_OK = 5
_MIN_VERIFIED_SAMPLE = 5

# Optimism rank: higher = the brain claims it is doing better. Both
# ladders are normalized onto the same 0..3 scale so a numeric gap is
# meaningful regardless of which subsystem produced it.
_EVOLUTION_RANK = {
    "quiet":     0,
    "warming":   1,
    "steady":    2,
    "ascending": 3,
}
_SELF_MODEL_RANK = {
    # self_assessment is a free-text string that STARTS with one of
    # these keywords (see brain_self_model.compute_self_model).
    "insufficient": None,   # cannot grade → skip the comparison
    "degraded":     0,
    "mixed":        1,
    "healthy":      3,      # "healthy" maps to the top of the scale
}


def _db():
    """Local autocommit DB helper — mirrors brain_consistency_radar._db
    so a failed probe never poisons a follow-up query. Returns None when
    DATABASE_URL is unset or the connect fails (fail-open)."""
    try:
        import os as _os
        import psycopg2 as _pg2
    except Exception:
        return None
    db = None
    try:
        db = _os.environ.get("DATABASE_URL")
    except Exception:
        return None
    if not db:
        return None
    try:
        c = _pg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _self_model_rank(self_assessment: str) -> Optional[int]:
    """Map the self-model free-text verdict onto the 0..3 optimism
    scale by its leading keyword. None = not gradable (skip)."""
    if not self_assessment:
        return None
    head = str(self_assessment).strip().lower()
    for kw, rank in _SELF_MODEL_RANK.items():
        if head.startswith(kw):
            return rank
    return None


def _check_verdict_disagreement() -> list[dict]:
    """(a) Fire when evolution and self-model tell different stories
    about how the brain is doing, beyond _VERDICT_DISAGREE_BAND."""
    findings: list[dict] = []
    try:
        from routes.brain_evolution import compute_evolution_snapshot
        from routes.brain_self_model import compute_self_model
    except Exception:
        return findings
    try:
        evo = compute_evolution_snapshot() or {}
        sm = compute_self_model() or {}
    except Exception:
        return findings

    # Both must have produced a usable payload.
    if not evo.get("ok", False) or not sm.get("ok", False):
        return findings

    evo_verdict = str(evo.get("verdict") or "").strip().lower()
    sm_assess = sm.get("self_assessment") or ""

    evo_rank = _EVOLUTION_RANK.get(evo_verdict)
    sm_rank = _self_model_rank(sm_assess)
    if evo_rank is None or sm_rank is None:
        # Unknown verdict word or "insufficient sample" → can't compare
        # honestly. Stay silent rather than guess.
        return findings

    gap = abs(evo_rank - sm_rank)
    if gap < _VERDICT_DISAGREE_BAND:
        return findings

    try:
        evo_score = evo.get("evolution_score")
    except Exception:
        evo_score = None
    fix_rate = None
    try:
        fix_rate = (sm.get("current_state") or {}).get("fix_success_rate_30d")
    except Exception:
        fix_rate = None
    fix_pct = (f"{int(float(fix_rate) * 100)}%"
               if isinstance(fix_rate, (int, float)) else "n/a")

    sm_head = str(sm_assess).split(" ")[0].strip(",.;:").lower() or "unknown"

    findings.append({
        "issue": "brain_honesty_verdict_disagreement",
        "url":   "/api/v1/brain/evolution vs /api/v1/brain/self-model",
        "count": gap,
        "detail": (
            f"Brain self-narrative is internally inconsistent: evolution "
            f"verdict is '{evo_verdict}' (score {evo_score}) but the "
            f"self-model verdict is '{sm_head}' (verified fix rate "
            f"{fix_pct}) — a {gap}-rank optimism gap on a 0..3 scale "
            f"(band {_VERDICT_DISAGREE_BAND}). The two are supposed to "
            f"read the SAME autopilot_outcomes effect signal since the "
            f"R3-honesty re-base; a divergence this wide means one side "
            f"has drifted back onto a vanity metric. Reconcile "
            f"brain_evolution.compute_evolution_snapshot weights against "
            f"brain_self_model.compute_self_model before the optimistic "
            f"verdict gets quoted on a public surface."
        ),
    })
    return findings


def _check_acting_but_never_landing() -> list[dict]:
    """(b) Per-pattern: executed_ok>0 in brain_autopilot_actions but 0%
    verified-success in autopilot_outcomes. Gated executed_ok>=5 AND
    total_verified>=5 so a tiny sample never fires."""
    findings: list[dict] = []
    conn = _db()
    if conn is None:
        return findings
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT to_regclass('public.brain_autopilot_actions'),
                           to_regclass('public.autopilot_outcomes')
                """)
                regs = cur.fetchone() or [None, None]
            except Exception:
                return findings
            if not regs[0] or not regs[1]:
                return findings  # need both tables to compare

            # Join executed_ok fires (per pattern) against their verified
            # outcomes. autopilot_outcomes.succeeded: TRUE/FALSE = a real
            # verifier ran; NULL = no verifier for that pattern (excluded
            # from the rate so "can't tell" never reads as "failed").
            try:
                cur.execute("""
                    WITH acted AS (
                        SELECT pattern_name,
                               COUNT(*) AS executed_ok
                          FROM brain_autopilot_actions
                         WHERE outcome = 'executed_ok'
                           AND pattern_name IS NOT NULL
                           AND started_at >= NOW() - INTERVAL '30 days'
                         GROUP BY pattern_name
                    ),
                    verified AS (
                        SELECT a.pattern_name,
                               COUNT(*) FILTER (WHERE o.succeeded IS TRUE)  AS v_ok,
                               COUNT(*) FILTER (WHERE o.succeeded IS FALSE) AS v_fail
                          FROM autopilot_outcomes o
                          JOIN brain_autopilot_actions a
                            ON a.id = o.autopilot_action_id
                         WHERE o.verified_at >= NOW() - INTERVAL '30 days'
                           AND a.pattern_name IS NOT NULL
                         GROUP BY a.pattern_name
                    )
                    SELECT acted.pattern_name,
                           acted.executed_ok,
                           COALESCE(verified.v_ok, 0)   AS v_ok,
                           COALESCE(verified.v_fail, 0) AS v_fail
                      FROM acted
                      JOIN verified ON verified.pattern_name = acted.pattern_name
                """)
                rows = cur.fetchall() or []
            except Exception:
                return findings
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for r in rows:
        try:
            pattern = r[0]
            executed_ok = int(r[1] or 0)
            v_ok = int(r[2] or 0)
            v_fail = int(r[3] or 0)
        except Exception:
            continue
        total_verified = v_ok + v_fail
        # Conservative gate: enough fires AND enough verified samples.
        if executed_ok < _MIN_EXECUTED_OK:
            continue
        if total_verified < _MIN_VERIFIED_SAMPLE:
            continue
        # Acting but never landing: 0% verified success.
        if v_ok != 0:
            continue
        findings.append({
            "issue": "brain_pattern_acting_but_never_landing",
            "url":   f"autopilot_outcomes:{pattern}",
            "count": executed_ok,
            "detail": (
                f"Pattern '{pattern}' fired executed_ok {executed_ok}x in "
                f"30d but its verified-success rate is 0% "
                f"({v_ok}/{total_verified} verified effects landed). The "
                f"autopilot keeps ACTING on this finding-class but the "
                f"effect verifier never confirms a real fix — endpoint "
                f"2xx is masking a no-op. Review the verifier for this "
                f"pattern in routes/autopilot_outcomes.py (_VERIFIERS) and "
                f"consider quarantining the pattern until it lands."
            ),
        })
    return findings


def check_honesty_reconciliation() -> list[dict]:
    """Radar entrypoint. READ-ONLY. Runs both honesty checks; any
    exception in either degrades to an empty list so a single bad probe
    never breaks the radar tick. No autopilot action is mapped to these
    issue strings → findings escalate to the human digest."""
    out: list[dict] = []
    try:
        out.extend(_check_verdict_disagreement())
    except Exception:
        pass
    try:
        out.extend(_check_acting_but_never_landing())
    except Exception:
        pass
    return out
