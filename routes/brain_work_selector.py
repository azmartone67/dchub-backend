"""brain_work_selector.py — THE SELF-AWARE WORK SELECTOR (2026-06-19).

The autonomy loop (routes/brain_autonomy_loop._open_draft_prs_for_open_proposals)
processes open code-fix proposals in scan/DB order and spends a small per-run
budget (MAX_DRAFT_PRS_PER_RUN) on whichever ones come first. That is BLIND: it
does not prefer the highest-leverage work, and it does not learn from its own
track record.

This module makes the brain CHOOSE by EVIDENCE and LEARN from outcomes — the
SAME verify → track → adapt loop the media engagement-bandit uses:

  · class_success_weight(klass) — read the HISTORICAL succeeded-rate for that
    fix-class (the new routes/brain_fix_outcome_verify writes a per-`klass`
    resolve row into brain_fix_outcomes; the media/autopilot patterns write a
    per-pattern row into autopilot_outcomes). Map that rate to a soft-greedy
    multiplier with a FLOOR so a class with a poor record is DOWN-WEIGHTED but
    NEVER permanently starved (it is still explored). Untried / no-data → 1.0
    neutral. Fail-NEUTRAL on any DB error (returns 1.0 — never penalises on a
    DB hiccup).

  · leverage_score(candidate) — base = impact_weight(occurrence / severity /
    age, else 1.0) * confidence; final = base * class_success_weight(class).

  · rank_work(candidates) — re-order highest-leverage-first, annotate each item
    with `_leverage` + `_rationale`. RANK-ONLY: it NEVER drops a candidate
    (lower-ranked work is deferred to a future run, not discarded) and is STABLE
    for ties (original scan order breaks ties).

  · GET /api/v1/brain/work-plan — the SELF-MODEL surface: the current ranked
    plan + per-item rationale + the learned class weights (what the brain is
    choosing to work on next, and WHY). Admin-gated (reuses the classifier's
    _admin_ok).

SAFETY / PRINCIPLE:
  · RANK-ONLY. The wired call-site (brain_autonomy_loop, flag BRAIN_WORK_SELECTOR
    default on) re-orders the candidate list BEFORE the existing rate cap slices
    it — it never DROPS a candidate, so the worst case is identical to today's
    behaviour with a different processing order, still bounded by the same cap.
  · Every DB read is guarded; on ANY error the caller falls back to the EXISTING
    scan order. Nothing here can crash a tick.
  · FLOOR keeps soft-greedy exploration: an unproven or unlucky class is
    down-weighted, never zeroed.

Heavy imports (flask, psycopg2, the classifier's flask chain) are LAZY so this
module imports cleanly in a no-flask test env where the DB is monkeypatched.
"""
from __future__ import annotations

import os
import re


# ── Tunables (env-driven; conservative, soft-greedy) ─────────────────
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


# Soft-greedy multiplier:  weight = FLOOR + SLOPE * succeeded_rate, capped at CAP.
#   rate 0.0 → FLOOR (0.5, the exploration floor — never starved)
#   rate 1.0 → FLOOR + SLOPE = 1.3 (capped) — a proven class is boosted
#   no data  → NEUTRAL (1.0)
WORK_WEIGHT_FLOOR = _env_float("BRAIN_WORK_WEIGHT_FLOOR", 0.5)
WORK_WEIGHT_SLOPE = _env_float("BRAIN_WORK_WEIGHT_SLOPE", 0.8)
WORK_WEIGHT_CAP = _env_float("BRAIN_WORK_WEIGHT_CAP", 1.3)
WORK_NEUTRAL = 1.0

# Only trust a class's rate once we have at least this many verified outcomes;
# below it we blend toward neutral so one unlucky run can't crush a class.
WORK_MIN_SAMPLES = _env_int("BRAIN_WORK_MIN_SAMPLES", 3)

# Recent-window for the success-rate read (days).
WORK_WINDOW_DAYS = _env_int("BRAIN_WORK_WINDOW_DAYS", 45)

# ── agentic-loop #65 part C (2026-08-22): the EARNED vocabulary ──────
# The claim loop ships two outcome ledgers whose verdicts are written by a
# verifier that is not the actor: brain_action_class_runs (one row per
# executed action-class run; `verified` = the drain's pre/post verifier read)
# and the claim each run pre-registers in brain_predictions_log (outcome
# stamped at horizon by the L16 cron — writer ≠ author). _read_class_rate
# reads them as a THIRD source, so class_success_weight("facility_dedup_apply")
# is fed by real runs; learned_outcome_weights() publishes every class whose
# ledgers reached the sample floor (the bandit's learned state) WITH the raw
# counts underneath, so a lane can print "?" with the numbers when the floor
# is not met. A claim's verdict outranks the run's own read: `confirmed` is a
# success and `refuted` a failure even when the drain saw the count drop;
# open / unobserved / retracted claims fall back to `verified`.
LEARN_MIN_OUTCOMES = _env_int("BRAIN_LEARN_MIN_OUTCOMES", 5)

_RUN_OK = ("(p.outcome = 'confirmed' OR (COALESCE(p.outcome,'') NOT IN "
           "('confirmed','refuted') AND r.verified))")
_RUN_FAILED = ("(p.outcome = 'refuted' OR (COALESCE(p.outcome,'') NOT IN "
               "('confirmed','refuted') AND NOT r.verified))")
# No literal percent-sign in these strings: they are executed WITH a params
# tuple (INTERVAL '%s days' is a placeholder, as in the two sources above).
_ACTION_CLASS_RATE_SQL = (
    "SELECT COUNT(*), COUNT(*) FILTER (WHERE " + _RUN_OK + ") "
    "  FROM brain_action_class_runs r "
    "  LEFT JOIN brain_predictions_log p ON p.id = r.claim_id "
    " WHERE r.class = %s AND r.executed AND NOT r.dry_run "
    "   AND r.started_at >= NOW() - INTERVAL '%s days'")
_ACTION_CLASS_SAMPLES_SQL = (
    "SELECT r.class, COUNT(*), "
    "       COUNT(*) FILTER (WHERE " + _RUN_OK + "), "
    "       COUNT(*) FILTER (WHERE " + _RUN_FAILED + "), "
    "       COUNT(*) FILTER (WHERE p.outcome IN ('confirmed','refuted')) "
    "  FROM brain_action_class_runs r "
    "  LEFT JOIN brain_predictions_log p ON p.id = r.claim_id "
    " WHERE r.executed AND NOT r.dry_run "
    "   AND r.started_at >= NOW() - %s * INTERVAL '1 day' "
    " GROUP BY r.class")
_FIX_OUTCOME_SAMPLES_SQL = (
    "SELECT LOWER(COALESCE(klass,'')), "
    "       COUNT(*) FILTER (WHERE resolved IS NOT NULL), "
    "       COUNT(*) FILTER (WHERE resolved) "
    "  FROM brain_fix_outcomes "
    " WHERE verified_at >= NOW() - INTERVAL '%s days' "
    " GROUP BY 1")

# Severity tag → impact multiplier (parsed from rationale/detail like the
# action-queue's [CRIT]/[HIGH]/… convention).
_SEV_RE = re.compile(r"\[([A-Z]+)\]")
_SEV_IMPACT = {
    "CRIT": 2.0, "CRITICAL": 2.0,
    "HIGH": 1.5,
    "MED": 1.1, "MEDIUM": 1.1,
    "LOW": 0.9,
}


def _conn():
    """Best-effort autocommit DB connection. None on any failure (fail-neutral).
    Heavy import is lazy so the module loads in a no-flask/no-psycopg2 env."""
    try:
        import psycopg2
    except Exception:
        return None
    url = (os.environ.get("NEON_DATABASE_URL")
           or os.environ.get("DATABASE_URL"))
    if not url:
        return None
    try:
        c = psycopg2.connect(url, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        # sslmode may be rejected by some local/test DBs — retry without it.
        try:
            c = psycopg2.connect(url, connect_timeout=5)
            c.autocommit = True
            return c
        except Exception:
            return None


def _soft_greedy(rate: float, samples: int) -> float:
    """Map a historical succeeded-rate (+ sample size) to a soft-greedy
    multiplier with a FLOOR. Pure. `rate` in [0,1]; `samples` >= 0.

      weight = FLOOR + SLOPE * blended_rate   (capped at CAP)

    With few samples we BLEND the observed rate toward the neutral midpoint so
    a class with one unlucky outcome is not crushed — exploration is preserved.
    The FLOOR guarantees even a 0%-historical class keeps a non-trivial weight."""
    try:
        r = float(rate)
    except Exception:
        r = 0.5
    r = max(0.0, min(1.0, r))
    n = max(0, int(samples or 0))
    if n < WORK_MIN_SAMPLES:
        # Blend toward neutral-rate (0.625, the rate that maps to WORK_NEUTRAL
        # under the default FLOOR/SLOPE) proportional to how much data we have.
        neutral_rate = (WORK_NEUTRAL - WORK_WEIGHT_FLOOR) / max(1e-9, WORK_WEIGHT_SLOPE)
        neutral_rate = max(0.0, min(1.0, neutral_rate))
        frac = n / float(max(1, WORK_MIN_SAMPLES))
        r = neutral_rate * (1.0 - frac) + r * frac
    w = WORK_WEIGHT_FLOOR + WORK_WEIGHT_SLOPE * r
    # Clamp into [FLOOR, CAP] — never below the exploration floor.
    if w < WORK_WEIGHT_FLOOR:
        w = WORK_WEIGHT_FLOOR
    if w > WORK_WEIGHT_CAP:
        w = WORK_WEIGHT_CAP
    return round(w, 4)


def _read_class_rate(klass: str) -> tuple[float | None, int]:
    """Read the historical (succeeded-rate, sample-count) for a fix CLASS.

    PRIMARY source: brain_fix_outcomes(klass, resolved) — the per-CLASS resolve
    evidence written by routes/brain_fix_outcome_verify when a merged mechanical
    fix is verified. resolved is tri-state (NULL = indeterminate → neutral, not
    counted in the denominator).

    FALLBACK source: autopilot_outcomes(pattern_name, succeeded) — the shared
    calibration feed (media/autopilot patterns + a constant 'brain_fix_outcome'
    row). Used when the class string also names a pattern there.

    Returns (rate_or_None, samples). rate is None when there is NO usable data
    (→ caller treats as neutral). NEVER raises — returns (None, 0) on any error
    so class_success_weight stays fail-neutral."""
    if not klass:
        return None, 0
    c = _conn()
    if c is None:
        return None, 0
    try:
        with c.cursor() as cur:
            # PRIMARY: per-class mechanical-fix resolve evidence.
            try:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE resolved IS NOT NULL), "
                    "       COUNT(*) FILTER (WHERE resolved) "
                    "  FROM brain_fix_outcomes "
                    " WHERE LOWER(COALESCE(klass,'')) = LOWER(%s) "
                    "   AND verified_at >= NOW() - INTERVAL '%s days'",
                    (str(klass), WORK_WINDOW_DAYS),
                )
                row = cur.fetchone() or (0, 0)
                total = int(row[0] or 0)
                succ = int(row[1] or 0)
                if total > 0:
                    return (succ / float(total)), total
            except Exception:
                pass
            # FALLBACK: shared autopilot/media calibration feed by pattern name.
            try:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE succeeded IS NOT NULL), "
                    "       COUNT(*) FILTER (WHERE succeeded) "
                    "  FROM autopilot_outcomes "
                    " WHERE pattern_name = %s "
                    "   AND verified_at >= NOW() - INTERVAL '%s days'",
                    (str(klass), WORK_WINDOW_DAYS),
                )
                row = cur.fetchone() or (0, 0)
                total = int(row[0] or 0)
                succ = int(row[1] or 0)
                if total > 0:
                    return (succ / float(total)), total
            except Exception:
                pass
            # TERTIARY (agentic-loop #65 part C): the action-class run ledger,
            # judged by the claim verifier where a claim is linked. Same
            # contract as the two sources above: (rate, samples) or fall
            # through; never raises.
            try:
                cur.execute(_ACTION_CLASS_RATE_SQL, (str(klass), WORK_WINDOW_DAYS))
                row = cur.fetchone() or (0, 0)
                total = int(row[0] or 0)
                succ = int(row[1] or 0)
                if total > 0:
                    return (succ / float(total)), total
            except Exception:
                pass
    except Exception:
        return None, 0
    finally:
        try:
            c.close()
        except Exception:
            pass
    return None, 0


def class_success_weight(klass_or_pattern: str) -> float:
    """Soft-greedy multiplier in [FLOOR, CAP] for a fix-class / pattern based on
    its HISTORICAL succeeded-rate over a recent window.

      · low historical rate  → DOWN-WEIGHTED, but never below FLOOR (explored)
      · untried / no data     → 1.0 NEUTRAL
      · proven class          → boosted up to CAP

    Fail-NEUTRAL: returns 1.0 on missing klass or ANY DB error — a DB hiccup
    must never penalise a class. Pure aside from the guarded read."""
    try:
        if not klass_or_pattern:
            return WORK_NEUTRAL
        rate, samples = _read_class_rate(str(klass_or_pattern))
        if rate is None or samples <= 0:
            return WORK_NEUTRAL  # untried / no data → neutral
        return _soft_greedy(rate, samples)
    except Exception:
        return WORK_NEUTRAL


def _severity_impact(text: str) -> tuple[str, float]:
    """Parse a [CRIT]/[HIGH]/[MED]/[LOW] tag → (tag, impact_multiplier).
    No tag → ('NONE', 1.0)."""
    m = _SEV_RE.search(text or "")
    if m:
        tag = m.group(1).upper()
        if tag in _SEV_IMPACT:
            return tag, _SEV_IMPACT[tag]
    return "NONE", 1.0


def _coerce_float(v, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _age_days_from_ts(candidate: dict) -> float:
    """Days since first seen, derived from a timestamp when no explicit age_days
    is present. This is the ANTI-STARVATION clock (see impact_weight): the longer
    a candidate has waited, the bigger its age boost, so a down-weighted-class
    item EVENTUALLY rises above fresh high-weight work and is never permanently
    starved. Best-effort; 0.0 on anything unparseable."""
    import datetime as _dt
    for k in ("created_at", "first_seen", "first_seen_at", "reviewed_at", "ts"):
        v = candidate.get(k)
        if not v:
            continue
        try:
            d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
            return max(0.0, (_dt.datetime.now(_dt.timezone.utc) - d).total_seconds() / 86400.0)
        except Exception:
            continue
    return 0.0


# r-brain-loop (2026-06-30): some detectors emit a metric VALUE under "count"
# (frontend_endpoint_slow count=latency_ms; dedup_backlog_large count=backlog
# size). The scorer used to read that as a recurrence count, which fake-inflated
# leverage (a 21,415-row backlog or 3,094ms latency hit the occ cap 1.6x) so
# these items re-won the agenda every tick and the enhancer rendered them as
# "detected N times". These issues carry a value, not an occurrence.
# 2026-08-02 (#48 health sweep): cron_silently_dead was the third instance of
# this exact class and cost the most. brain_consistency_radar.py:7419 writes
# `"count": int(seconds_since)`, so "site-baseline (seen x477455)" is 5.5 DAYS
# of silence, not 477,455 sightings. Unlisted, it hit the occurrence cap and
# re-won the agenda every tick: the three top self-agenda items were all
# cron_silently_dead proposing a fingerprint-dedup project for a flood that
# never existed (brain_findings holds ~3k rows total, seen_count=1 on those
# rows), while the actual outage — four dead crons — went unworked.
VALUE_NOT_COUNT_ISSUES = ("frontend_endpoint_slow", "dedup_backlog_large",
                          "cron_silently_dead")


def is_value_not_count(text) -> bool:
    """True when the finding's numeric signal is a metric value, not a count.

    LEGACY / FALLBACK path: matches issue STRINGS. Kept because ~193 radar
    write-sites have not declared a type yet, and because it is the only thing
    that can speak for a finding written before count_kind existed. New
    detectors should declare count_kind instead of being added here —
    see occurrence_signal()."""
    t = str(text or "").lower()
    return any(k in t for k in VALUE_NOT_COUNT_ISSUES)


# ── #49 lane 3: the guard, DERIVED ────────────────────────────────────
# The string list above is a subscription, not a fix: it has been edited three
# times, once per recurrence of the same class, each time AFTER the misread had
# already cost an agenda cycle. A detector KNOWS at write time whether its
# integer is a tally or a magnitude, so ask it.
OCCURRENCE_KIND = "occurrence"

# Keys that are tallies by CONSTRUCTION and are never free-form: seen_count and
# friends are written by brain_findings_writer under episode semantics, so they
# stay trusted even when `count` on the same row does not.
_TALLY_KEYS = ("occurrence", "occurrences", "seen_count", "n_seen")

# Plausibility ceiling for an UNTYPED `count`. brain_findings holds ~3k rows in
# total and the highest recurrence ever observed was 1,463, so a finding
# claiming six figures of sightings is arithmetically impossible — it is a
# duration, a byte count or a backlog size wearing a tally's clothes. This is
# the part that catches detector #4 BEFORE anyone remembers to edit a tuple.
# Untyped only: a declared occurrence is a promise, and we keep it.
_UNTYPED_OCCURRENCE_CEILING = 10_000


def _untyped_ceiling() -> int:
    return int(_coerce_float(os.environ.get("BRAIN_UNTYPED_COUNT_CEILING"),
                             _UNTYPED_OCCURRENCE_CEILING))


def row_count_is_value(row) -> bool:
    """True when this row's `count`/`value` is a MAGNITUDE, not a tally.

    The rendering counterpart of occurrence_signal(), for surfaces that only
    need the yes/no (brain_enhancer renders "(value N)" instead of "(seen xN)").
    Same three layers in the same order, so a finding can never be ranked as a
    magnitude and described as a recurrence in the same breath."""
    if not isinstance(row, dict):
        return False
    kind = count_kind_of(row)
    if kind:
        return kind != OCCURRENCE_KIND
    text = " ".join(str(row.get(k) or "")
                    for k in ("issue", "claim", "rationale", "detail"))
    if is_value_not_count(text):
        return True
    for k in ("count", "value"):
        v = row.get(k)
        if v is None:
            continue
        try:
            if int(v) > _untyped_ceiling():
                return True
        except Exception:
            pass
    return False


def count_kind_of(candidate) -> str:
    """The declared meaning of `count`, or "" when the detector never said."""
    if not isinstance(candidate, dict):
        return ""
    return str(candidate.get("count_kind") or "").strip().lower()


def occurrence_signal(candidate) -> tuple:
    """(occurrences, why) — how many times this thing was actually SEEN.

    Three layers, most trustworthy first:
      1. DECLARED    count_kind says what `count` means. Anything other than
                     "occurrence" is a magnitude and contributes nothing.
      2. LEGACY      undeclared → the issue-string denylist still applies.
      3. PLAUSIBLE   undeclared and not denylisted → trusted only up to the
                     ceiling, because an untyped six-figure `count` in a 3k-row
                     table is a magnitude nobody has labelled yet.

    Tally keys (seen_count &c.) are read regardless: they are written by the
    canonical writer, not free-form by a detector."""
    if not isinstance(candidate, dict):
        return 0, {"source": "none", "trusted": False}
    occ = 0
    for k in _TALLY_KEYS:
        v = candidate.get(k)
        if v is not None:
            try:
                occ = max(occ, int(v))
            except Exception:
                pass
    raw = candidate.get("count")
    if raw is None:
        return occ, {"source": "tally_keys", "trusted": True}
    try:
        cnt = int(raw)
    except Exception:
        return occ, {"source": "tally_keys", "trusted": True,
                     "note": "count not an integer"}

    kind = count_kind_of(candidate)
    if kind:
        if kind == OCCURRENCE_KIND:
            return max(occ, cnt), {"source": "declared_occurrence",
                                   "count_kind": kind, "trusted": True}
        return occ, {"source": "declared_value", "count_kind": kind,
                     "trusted": False,
                     "note": f"count is a {kind}, not a tally"}

    text = " ".join(str(candidate.get(k) or "")
                    for k in ("issue", "claim", "rationale", "detail"))
    if is_value_not_count(text):
        return occ, {"source": "legacy_denylist", "trusted": False,
                     "note": "issue string is a known value-not-count class"}

    ceiling = _untyped_ceiling()
    if cnt > ceiling:
        return occ, {"source": "untyped_over_ceiling", "trusted": False,
                     "count": cnt, "ceiling": ceiling,
                     "note": (f"undeclared count {cnt} exceeds the plausible "
                              f"recurrence ceiling {ceiling} — treated as a "
                              f"magnitude. Declare count_kind on this "
                              f"detector to make the reading explicit.")}
    return max(occ, cnt), {"source": "untyped_within_ceiling", "trusted": True}


def impact_weight(candidate: dict) -> tuple[float, dict]:
    """How much this proposal MATTERS, independent of how likely it is to land.

    base = severity_impact * occurrence_factor * age_factor

      · severity  — parsed from rationale/detail [CRIT]/[HIGH]/… (default 1.0)
      · occurrence — log-ish bump from an occurrence/seen/count signal if present
      · age        — a small bump for an older finding (it's been deferred)

    Every signal is OPTIONAL — when absent the factor is 1.0, so a bare
    proposal scores a neutral base of 1.0. Pure; returns (weight, breakdown)."""
    if not isinstance(candidate, dict):
        return 1.0, {"severity": "NONE", "note": "non-dict candidate → neutral"}

    sev_text = " ".join(str(candidate.get(k) or "") for k in
                        ("rationale", "detail", "issue", "severity"))
    sev_tag, sev_mult = _severity_impact(sev_text)

    # Occurrence / how-many-times-seen signal (any of several common keys).
    # Skip findings whose "count" is actually a metric value (see
    # VALUE_NOT_COUNT_ISSUES) so a latency/backlog magnitude can't masquerade as
    # thousands of occurrences and pin itself to the top of the agenda forever.
    occ, occ_why = occurrence_signal(candidate)
    # Diminishing returns: each ~order of magnitude adds ~0.15, capped at +0.6.
    occ_mult = 1.0
    if occ > 1:
        import math
        occ_mult = min(1.6, 1.0 + 0.15 * math.log10(max(1, occ)) * 2)

    # Age signal (days). This is ALSO the ANTI-STARVATION guarantee: a candidate
    # in a down-weighted class would otherwise rank permanently below fresh
    # high-weight work and never be selected under the per-run cap. So the age
    # boost must be able to OVERCOME the max class-weight gap (~1.3/0.5 = 2.6x)
    # for a sufficiently-old item — a generous cap + slope, not the old +0.3 trim
    # — so a deferred item EVENTUALLY rises to the top. Derive age from a
    # timestamp when explicit day-fields are absent.
    age_days = 0.0
    for k in ("age_days", "age", "days_open"):
        v = candidate.get(k)
        if v is not None:
            age_days = max(age_days, _coerce_float(v, 0.0))
    if age_days <= 0:
        age_days = _age_days_from_ts(candidate)
    _age_slope = _coerce_float(os.environ.get("BRAIN_WORK_AGE_SLOPE"), 0.12)  # per-day boost
    _age_cap = _coerce_float(os.environ.get("BRAIN_WORK_AGE_CAP"), 5.0)       # > class-weight gap → no permanent starvation
    age_mult = 1.0
    if age_days > 0:
        age_mult = min(_age_cap, 1.0 + _age_slope * age_days)

    weight = sev_mult * occ_mult * age_mult
    return round(weight, 4), {
        "severity": sev_tag,
        "severity_mult": round(sev_mult, 3),
        "occurrence": occ,
        "occurrence_mult": round(occ_mult, 3),
        # ★Says WHY the occurrence signal is what it is. /brain/work-plan is
        # the self-model surface, and "this number was ignored because the
        # detector declared it a duration" is exactly the sentence whose
        # absence let the #48 misread run for days.
        "occurrence_source": occ_why,
        "age_days": round(age_days, 1),
        "age_mult": round(age_mult, 3),
    }


def _candidate_class(candidate: dict) -> str:
    """Best-effort fix-CLASS for a candidate. Prefer an explicit class already
    on the row; else classify via the mechanical classifier (lazy import — it
    pulls flask). Empty string when unknowable (→ neutral class weight)."""
    if not isinstance(candidate, dict):
        return ""
    for k in ("klass", "class", "_class", "finding_class"):
        v = candidate.get(k)
        if v:
            return str(v)
    try:
        from routes.brain_mechanical_classifier import classify_mechanical
        verdict = classify_mechanical(candidate)
        return str(verdict.get("klass") or "")
    except Exception:
        return ""


def _candidate_confidence(candidate: dict) -> float:
    """Confidence in [0,1]. Reads the proposal's own confidence; default 0.8
    (the classifier's MECH_MIN_CONF floor) when absent so a missing confidence
    does not zero a candidate's leverage."""
    if not isinstance(candidate, dict):
        return 0.8
    c = _coerce_float(candidate.get("confidence"), 0.8)
    return max(0.0, min(1.0, c))


def leverage_score(candidate: dict) -> tuple[float, dict]:
    """Leverage = impact * confidence * class_success_weight.

      base  = impact_weight(occurrence/severity/age, else 1.0) * confidence
      final = base * class_success_weight(candidate class)

    Pure + guarded: never raises. Returns (score, rationale_dict). The rationale
    captures every factor so the /work-plan surface can explain the ranking."""
    try:
        imp, imp_breakdown = impact_weight(candidate)
        conf = _candidate_confidence(candidate)
        klass = _candidate_class(candidate)
        cw = class_success_weight(klass)
        base = imp * conf
        final = base * cw
        return round(final, 5), {
            "class": klass or "(unknown)",
            "impact": imp,
            "impact_breakdown": imp_breakdown,
            "confidence": round(conf, 3),
            "class_success_weight": cw,
            "base": round(base, 5),
            "leverage": round(final, 5),
            "explain": (
                f"impact {imp} x confidence {round(conf, 3)} x "
                f"class_weight {cw} (class={klass or 'unknown'}) = {round(final, 5)}"
            ),
        }
    except Exception as e:
        # Fail-neutral: a candidate we cannot score gets neutral leverage so it
        # is neither boosted nor dropped.
        return 1.0, {"class": "(error)", "leverage": 1.0,
                     "error": f"{type(e).__name__}: {str(e)[:80]}"}


def collapse_to_roots(ranked: list) -> list:
    """Group an already-ranked list by ROOT CAUSE and let one member of each
    group speak for it (#49 lane 2).

    ★DEFER, NEVER DROP. The documented safety property of this module is that
    it is a permutation of its input — the caller slices the front of the list
    under MAX_DRAFT_PRS_PER_RUN, so "deferred" means "a later run, once the
    root has been tried", not "discarded". Siblings move BEHIND every
    unrelated candidate; they do not leave the list.

    Why it matters: L14 routinely finds that four open findings are one broken
    thing. Before this, a run could spend its entire PR budget opening four
    draft PRs against four symptoms, none of which fixes the cause — and then
    brain_fix_outcome_verify records four failures, which down-weights the
    fix-class for work that was never the problem.

    Fail-flat: any error, or no edges, returns the input untouched."""
    try:
        items = list(ranked or [])
    except Exception:
        return ranked
    if len(items) < 2:
        return items
    try:
        from routes.brain_finding_edges import load_root_map, root_of
        root_map = load_root_map()
    except Exception:
        return items
    if not root_map:
        return items
    try:
        seen_roots = set()
        leaders, deferred = [], []
        for cand in items:
            root = ""
            try:
                root = root_of(cand, root_map)
            except Exception:
                root = ""
            if not root:
                leaders.append(cand)
                continue
            if root not in seen_roots:
                # First (= highest-leverage, the list is already sorted)
                # member of this group speaks for it.
                seen_roots.add(root)
                if isinstance(cand, dict):
                    cand["_root_cause"] = root
                    cand["_root_role"] = "representative"
                leaders.append(cand)
            else:
                if isinstance(cand, dict):
                    cand["_root_cause"] = root
                    cand["_root_role"] = "deferred_sibling"
                    cand["_rationale"] = dict(cand.get("_rationale") or {})
                    cand["_rationale"]["deferred_because"] = (
                        f"another open finding is already representing the root "
                        f"cause '{root}' this run; working both spends the "
                        f"per-run budget twice on one problem")
                deferred.append(cand)
        out = leaders + deferred
        if len(out) != len(items):
            return items
        return out
    except Exception:
        return items


def rank_work(candidates: list) -> list:
    """Re-order `candidates` highest-leverage-first. RANK-ONLY:

      · NEVER drops an item — the returned list is a permutation of the input.
      · STABLE for ties — original scan order breaks equal-leverage ties, so a
        zero-evidence run is identical to today's order.
      · Annotates each item with `_leverage` (float) and `_rationale` (dict)
        WITHOUT mutating the caller's dicts (shallow copy per item).

    NEVER raises: on ANY error it returns the input list unchanged (the EXISTING
    scan order) so a selector failure can never crash — or reorder away — a
    tick's work."""
    try:
        items = list(candidates or [])
    except Exception:
        return list(candidates) if candidates else []
    if not items:
        return items
    try:
        annotated = []
        for idx, cand in enumerate(items):
            try:
                score, rationale = leverage_score(cand)
            except Exception:
                score, rationale = 1.0, {"error": "score_failed", "leverage": 1.0}
            # Shallow-copy so we annotate without mutating the caller's object.
            if isinstance(cand, dict):
                out = dict(cand)
            else:
                out = cand
            try:
                if isinstance(out, dict):
                    out["_leverage"] = score
                    out["_rationale"] = rationale
            except Exception:
                pass
            # (score, original_index) so Python's stable sort preserves scan
            # order within equal scores; negate score for descending.
            annotated.append((-float(score), idx, out))
        annotated.sort(key=lambda t: (t[0], t[1]))
        ranked = [t[2] for t in annotated]
        ranked = collapse_to_roots(ranked)
        # SAFETY invariant: rank-only must never drop a candidate.
        if len(ranked) != len(items):
            return items
        return ranked
    except Exception:
        # Any unexpected failure → existing scan order, untouched.
        return items


# ════════════════════════════════════════════════════════════════════
#  /work-plan SELF-MODEL surface
# ════════════════════════════════════════════════════════════════════
def _learned_class_weights(classes) -> dict:
    """For a set of classes, return {class: {weight, rate, samples}} — the
    LEARNED bandit state the selector is acting on. Guarded; partial on error."""
    out: dict = {}
    for k in sorted({(c or "") for c in classes if c}):
        try:
            rate, samples = _read_class_rate(k)
            out[k] = {
                "weight": class_success_weight(k),
                "succeeded_rate": (None if rate is None else round(rate, 3)),
                "samples": samples,
                "status": ("untried/neutral" if (rate is None or samples <= 0)
                           else ("boosted" if class_success_weight(k) > WORK_NEUTRAL
                                 else ("down-weighted" if class_success_weight(k) < WORK_NEUTRAL
                                       else "neutral"))),
            }
        except Exception:
            out[k] = {"weight": WORK_NEUTRAL, "succeeded_rate": None,
                      "samples": 0, "status": "error/neutral"}
    return out


def _read_outcome_samples(window_days: int = None) -> dict:
    """Per-class settled-outcome counts across the ledgers class_success_weight
    reads: brain_fix_outcomes (klass; the PRIMARY source) and the action-class
    run ledger joined to its claim verdicts (the TERTIARY source). Returns
    {class: {settled, ok, failed, verifier_judged, source}} — {} when both
    ledgers are readable but empty, None when the DB is unreachable (the
    caller publishes measured=False — never a flattering empty). Never
    raises."""
    days = int(window_days or WORK_WINDOW_DAYS)
    out: dict = {}
    c = _conn()
    if c is None:
        return None          # unreachable ≠ empty: the caller reports measured=False
    try:
        with c.cursor() as cur:
            try:
                cur.execute(_FIX_OUTCOME_SAMPLES_SQL, (days,))
                for klass, settled, ok in cur.fetchall() or []:
                    if not klass or not settled:
                        continue
                    out[str(klass)] = {
                        "settled": int(settled), "ok": int(ok or 0),
                        "failed": int(settled) - int(ok or 0),
                        "verifier_judged": int(settled),
                        "source": "brain_fix_outcomes"}
            except Exception:
                pass
            try:
                cur.execute(_ACTION_CLASS_SAMPLES_SQL, (days,))
                for klass, settled, ok, failed, judged in cur.fetchall() or []:
                    if not klass or not settled:
                        continue
                    out[str(klass)] = {
                        "settled": int(settled), "ok": int(ok or 0),
                        "failed": int(failed or 0),
                        "verifier_judged": int(judged or 0),
                        "source": "brain_action_class_runs+claim_verifier"}
            except Exception:
                pass
    except Exception:
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def learned_outcome_weights(min_outcomes: int = None, window_days: int = None) -> dict:
    """The effect bandit's EARNED vocabulary: every class whose outcome ledgers
    hold at least `min_outcomes` settled outcomes (default LEARN_MIN_OUTCOMES,
    5) with the soft-greedy weight class_success_weight would apply, plus the
    raw sample counts for EVERY class seen (so a lane can print "?" with the
    numbers for classes still below the floor). measured=False when the DB
    was unreadable — an unmeasured bandit is reported as such, never as an
    empty one. JSON-safe; never raises."""
    floor = int(min_outcomes or LEARN_MIN_OUTCOMES)
    days = int(window_days or WORK_WINDOW_DAYS)
    out: dict = {"floor": floor, "window_days": days, "measured": False,
                 "sample_counts": {}, "learned_class_weights": {},
                 "below_floor": {}, "non_empty": False,
                 "basis": ("settled outcomes per class from brain_fix_outcomes "
                           "(klass) and brain_action_class_runs judged by the "
                           "claim verifier; a class enters learned_class_weights "
                           "at >= floor settled outcomes")}
    try:
        samples = _read_outcome_samples(days)
        if samples is None:
            out["error"] = "db_unavailable"
            return out
        out["measured"] = True
        out["sample_counts"] = samples
        for k in sorted(samples):
            s = samples[k]
            n = int(s.get("settled") or 0)
            if n < floor:
                out["below_floor"][k] = n
                continue
            rate = s["ok"] / float(n) if n else 0.0
            w = _soft_greedy(rate, n)
            out["learned_class_weights"][k] = {
                "weight": w, "succeeded_rate": round(rate, 3), "samples": n,
                "verifier_judged": int(s.get("verifier_judged") or 0),
                "source": s.get("source"),
                "status": ("boosted" if w > WORK_NEUTRAL
                           else ("down-weighted" if w < WORK_NEUTRAL else "neutral")),
                "in_plan": False,
            }
        out["non_empty"] = bool(out["learned_class_weights"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def build_work_plan(limit: int = 50) -> dict:
    """Build the current ranked plan over the OPEN proposals (the same set the
    autonomy loop would process) + per-item rationale + learned class weights.
    Read-only; guarded. Returns a JSON-able dict (never raises)."""
    import datetime
    plan: dict = {
        "ok": True,
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "selector_enabled": (os.environ.get("BRAIN_WORK_SELECTOR", "1")
                             not in ("0", "false", "False", "off")),
        "config": {
            "floor": WORK_WEIGHT_FLOOR, "slope": WORK_WEIGHT_SLOPE,
            "cap": WORK_WEIGHT_CAP, "neutral": WORK_NEUTRAL,
            "min_samples": WORK_MIN_SAMPLES, "window_days": WORK_WINDOW_DAYS,
        },
        "ranked": [],
        "learned_class_weights": {},
        "note": ("RANK-ONLY self-model: this is the order the autonomy loop "
                 "would process open proposals in, ranked by evidence-weighted "
                 "leverage. Lower-ranked work is DEFERRED, never dropped."),
    }
    try:
        from routes.brain_mechanical_classifier import _fetch_open_proposals
        rows, err = _fetch_open_proposals(include_resolved=False, limit=200)
        if err:
            plan["ok"] = False
            plan["error"] = err
            return plan
    except Exception as e:
        plan["ok"] = False
        plan["error"] = f"fetch_failed: {type(e).__name__}: {str(e)[:120]}"
        return plan

    try:
        ranked = rank_work(rows)
    except Exception:
        ranked = rows

    classes = []
    for r in ranked[: max(1, int(limit))]:
        if not isinstance(r, dict):
            continue
        rationale = r.get("_rationale") or {}
        klass = rationale.get("class") or ""
        if klass and klass not in ("(unknown)", "(error)"):
            classes.append(klass)
        plan["ranked"].append({
            "id": r.get("id"),
            "file": r.get("file_path") or r.get("file") or "",
            "class": klass or "(unknown)",
            "leverage": r.get("_leverage"),
            "rationale": rationale,
            "status": r.get("status") or "proposed",
        })
    plan["count"] = len(plan["ranked"])
    # ── agenda #38 measurement instrument (2026-08-14) ────────────────
    # Plan-share of items the selector could NOT classify. '(unknown)' means
    # the candidate carried no class key AND the mechanical classifier
    # returned none ('(error)': scoring itself failed). Both get a NEUTRAL
    # class weight, so the learned-outcome loop can neither boost nor
    # down-weight them — the exact blind spot agenda #38 flagged when the
    # top-ranked item was class '(unknown)'. NOTE the #38 recommendation
    # mis-attributed this to a PROVIDER-attribution gap; the schema pull
    # (2026-08-14) showed it is the fix-class fallback in leverage_score.
    # share is None (UNMEASURED, never 0) on an empty plan.
    _unclassified = sum(1 for it in plan["ranked"]
                        if it.get("class") in ("(unknown)", "(error)"))
    plan["unclassified_share"] = {
        "count": _unclassified,
        "of": plan["count"],
        "share": (round(_unclassified / plan["count"], 3)
                  if plan["count"] else None),
        "basis": ("ranked items whose class is '(unknown)' or '(error)' — "
                  "no explicit class key and the mechanical classifier "
                  "returned none; class-success learning cannot act on "
                  "these items"),
    }
    plan["learned_class_weights"] = _learned_class_weights(classes)
    for _v in plan["learned_class_weights"].values():
        _v["in_plan"] = True
    # agentic-loop #65 part C: the EARNED vocabulary. Classes whose outcome
    # ledgers reached the sample floor join the learned weights flagged
    # in_plan=False — the bandit's learned state, not padding: the plan's
    # own classes keep in_plan=True, and the raw counts ride underneath.
    # Live on 2026-08-22 the plan's two classes (now_text_cast,
    # tz_naive_utcnow) had 0 samples while the ledgers held brain_spec_pr 44,
    # brain_code_pr 6 and facility_dedup_apply 6 — a vocabulary mismatch
    # this surfaces instead of reporting "untried" for everything.
    try:
        _earned = learned_outcome_weights()
        for _k, _v in (_earned.get("learned_class_weights") or {}).items():
            if _k not in plan["learned_class_weights"]:
                plan["learned_class_weights"][_k] = dict(_v, in_plan=False)
        plan["earned_vocabulary"] = {
            "measured": _earned.get("measured"), "floor": _earned.get("floor"),
            "window_days": _earned.get("window_days"),
            "sample_counts": _earned.get("sample_counts") or {},
            "below_floor": _earned.get("below_floor") or {},
            "error": _earned.get("error"),
        }
    except Exception:
        pass
    plan["learned_class_weights_basis"] = (
        "classes in the ranked plan (in_plan true) plus every class whose "
        "outcome ledgers reached the sample floor (in_plan false)")
    return plan


# ── Blueprint (lazy flask; admin-gated like other brain endpoints) ───
try:
    from flask import Blueprint, jsonify, request

    brain_work_selector_bp = Blueprint("brain_work_selector", __name__)

    @brain_work_selector_bp.route("/api/v1/brain/work-plan", methods=["GET"])
    def work_plan_endpoint():
        """SELF-MODEL surface: the brain's current ranked work plan + per-item
        rationale + learned class weights. Admin-gated (reuses the classifier's
        _admin_ok)."""
        try:
            from routes.brain_mechanical_classifier import _admin_ok
            if not _admin_ok():
                return jsonify(ok=False, error="admin only",
                               hint="X-Admin-Key / X-Internal-Key header required"), 403
        except Exception:
            # If the admin gate can't load, fail CLOSED (deny) — never expose.
            return jsonify(ok=False, error="admin gate unavailable"), 403
        try:
            limit = int(request.args.get("limit", "50"))
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))
        plan = build_work_plan(limit=limit)
        return jsonify(plan), (200 if plan.get("ok") else 200), {
            "Cache-Control": "no-store, max-age=0",
            "X-DC-Hub-Surface": "brain-work-plan",
        }
except Exception:  # pragma: no cover — no-flask test env
    brain_work_selector_bp = None
