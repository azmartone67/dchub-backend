"""
routes/brain_enhancer.py — Brain ENHANCER (2026-06-19). PROPOSE-ONLY.

The SELF-ENHANCING rung. The brain has been self-HEALING for a while: detectors
emit findings, Layer 4/5 attack them, the autopilot opens DRAFT PRs and (when
armed) auto-merges allowlisted MECHANICAL fixes. That loop only ever fixes
KNOWN bugs — it reacts.

This module makes the brain self-DIRECTING: it PROPOSES improvements nobody
filed a bug for. It SCANS the brain's own evidence (canonical_stats + recent
brain_findings + HEALTH_BASELINE + the work-plan) for high-leverage improvement
OPPORTUNITIES across {reliability, performance, data_coverage,
conversion_revenue, developer_ux}, frames each as a sharp question, runs the
REUSED verified brain_investigator.investigate() on it (decompose -> gather REAL
evidence -> reason -> adversarially REFUTE -> synthesize), RANKS the resulting
proposals by leverage x confidence via the REUSED brain_work_selector, and
SURFACES them for a human to review/decide.

LEARNING-DRIVEN scan (BRAIN_ENHANCER_LEARNED_SCAN, default on): instead of
mining the four evidence sources in a FIXED canonical->findings->baseline->
work_plan order, scan_opportunities() REORDERS the scanned opportunities so the
AREAS where the brain HISTORICALLY succeeds are mined first. The per-area weight
is aggregated from the related mechanical fix-classes' learned resolve rates via
the VETTED brain_work_selector aggregators (_read_class_rate / _soft_greedy) —
NO hand-rolled counts. It is REORDER-ONLY (never drops/invents an opportunity),
keeps an anti-starvation FLOOR (an unproven area stays NEUTRAL and is explored,
never permanently skipped), and is best-effort (any failure logs and falls back
to the prior fixed order).

This is strictly WEAKER than the self-healing loop:
  · self-HEALING auto-merges allowlisted MECHANICAL fixes (it acts).
  · self-ENHANCING only PROPOSES improvements (it NEVER acts). An improvement
    is judgement-laden and must never be auto-applied.

SAFETY (PROPOSE-ONLY is the whole point):
  · It NEVER acts — no merge, no send, no auto-draft-PR, no write beyond STORING
    proposal rows. Strictly an analysis surface.
  · BRAIN_ENHANCER_ENABLED (default OFF) — ships dark. POST /enhance returns
    {enabled: false} WITHOUT calling any model when the flag is off.
  · Degrades gracefully with NO ANTHROPIC_API_KEY — returns cannot_enhance,
    never crashes. Every LLM/DB touch is wrapped in try/except.
  · Admin-gated endpoints (reuse the classifier's _admin_ok, like the
    investigator).
  · Honest-numbers fence (reused from brain_investigator) scans every cited
    figure in each proposal against the canon — a fabrication is FLAGGED, not
    silently surfaced.
  · NO git add/commit/push/deploy. Any cron wiring may ONLY enqueue/store
    proposals — never act on them.

Endpoints (blueprint brain_enhancer_bp, admin-gated):
  POST /api/v1/brain/enhance              {max?} -> ENQUEUE an async run
                                           (status pending), return 202
                                           {run_id, status}. Flag-gated.
  GET  /api/v1/brain/enhance/<run_id>     -> the run + its stored proposals.
  GET  /api/v1/brain/enhancements         -> recent proposals, ranked.
  POST /api/v1/brain/enhancements/<id>/grade {grade} -> record a human grade
                                           for CALIBRATION.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_enhancer_bp = Blueprint("brain_enhancer", __name__)


# ── env / flags ──────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """The enhancer ships DARK. Default OFF so it can't burn API budget or
    surface half-baked proposals until an operator flips the flag."""
    return _truthy(os.environ.get("BRAIN_ENHANCER_ENABLED"))


def _learned_scan_enabled() -> bool:
    """LEARNING-DRIVEN scan reordering. Default ON — it is REORDER-ONLY (never
    drops or invents an opportunity), so the worst case is identical to the old
    fixed order. An operator can flip BRAIN_ENHANCER_LEARNED_SCAN=0 to fall back
    to the pure canonical->findings->baseline->work_plan order."""
    return os.environ.get("BRAIN_ENHANCER_LEARNED_SCAN", "1") not in (
        "0", "false", "False", "off", "no",
    )


def _has_api_key() -> bool:
    """True when an Anthropic key is configured. We read the investigator's
    live module attribute (tests monkeypatch it there) and fall back to the
    raw env so degrade-gracefully works either way."""
    try:
        from routes import brain_investigator as _bi
        if (getattr(_bi, "ANTHROPIC_API_KEY", "") or "").strip():
            return True
    except Exception:
        pass
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


# ── Admin gate (reuse the classifier's, like the investigator) ───────
def _admin_ok() -> bool:
    """Reuse brain_mechanical_classifier._admin_ok so the gate stays in one
    place. Falls back to an inline internal-key check if that import fails so
    the endpoints are never accidentally left open."""
    try:
        from routes.brain_mechanical_classifier import _admin_ok as _mech_admin_ok
        return bool(_mech_admin_ok())
    except Exception:
        keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(_n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys


# ── DB (direct psycopg2, NOT safe_db — DDL is SKIP'd under safe_db) ───
def _conn():
    """Raw psycopg2 connection. Mirrors brain_investigator._conn — the
    _iso_common contextmanager crashes on .cursor()."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_enhancer: _conn failed: %s", e)
    return None


def init_enhancer_schema() -> None:
    """Bootstrap brain_enhancement_proposals + brain_enhancement_runs via DIRECT
    psycopg2 (safe_db SKIPs DDL under SKIP_DDL=1). Idempotent; never raises."""
    conn = _conn()
    if conn is None:
        logger.warning("brain_enhancer: no DB; skipping schema init")
        return
    try:
        with conn.cursor() as cur:
            # The runs table — an async run enqueues a PENDING row, the worker
            # marks it done (PROPOSE-ONLY: 'done' means proposals were stored,
            # never that anything was acted on).
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_enhancement_runs (
                        id          BIGSERIAL PRIMARY KEY,
                        status      TEXT NOT NULL DEFAULT 'pending',
                        max_props   INTEGER,
                        result_json JSONB,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            # The proposals table — the ONLY thing the enhancer ever writes
            # beyond a run row. status defaults 'proposed' (NEVER 'applied').
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_enhancement_proposals (
                        id            BIGSERIAL PRIMARY KEY,
                        run_id        BIGINT,
                        title         TEXT NOT NULL,
                        area          TEXT,
                        proposal_json JSONB,
                        confidence    DOUBLE PRECISION,
                        leverage_rank DOUBLE PRECISION,
                        status        TEXT NOT NULL DEFAULT 'proposed',
                        grade         TEXT,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_brain_enh_props_created "
                    "ON brain_enhancement_proposals (created_at DESC)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        logger.info("brain_enhancer: schema ready")
    finally:
        try: conn.close()
        except Exception: pass


# ── Honest-numbers fence (REUSE the investigator's canonical fence) ──
def _fence(text: str) -> list[str]:
    """Reuse brain_investigator._fence_fabricated_figures so the banned-figure
    canon lives in ONE place. Returns reasons for every banned figure in `text`
    (empty = clean). Never raises."""
    try:
        from routes.brain_investigator import _fence_fabricated_figures
        return _fence_fabricated_figures(text or "")
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════
#  Step 1: SCAN evidence for improvement OPPORTUNITIES
# ════════════════════════════════════════════════════════════════════
# The five improvement areas. Each opportunity MUST be grounded in a REAL signal
# (a canonical metric, a live finding, a baseline note, or a ranked work item) —
# never invented.
AREAS = (
    "reliability", "performance", "data_coverage",
    "conversion_revenue", "developer_ux",
)


def _opportunity(area: str, signal: str, question: str) -> dict:
    return {"area": area, "signal": signal, "question": question}


# ── LEARNING-DRIVEN scan reordering (GOAL B) ─────────────────────────
# The brain learns per-fix-CLASS resolve rates (brain_fix_outcomes, read by
# brain_work_selector._read_class_rate / class_success_weight). The enhancer's
# improvement AREAS are business themes, NOT mechanical fix-classes, so we map
# each AREA to the related fix-classes and aggregate their learned success rates
# into a per-AREA weight. We then REORDER the scanned opportunities so areas
# where the brain HISTORICALLY succeeds are mined first — never DROPPING any
# opportunity (anti-starvation: the FLOOR in class_success_weight keeps an
# unproven/unlucky area explored, and reorder-only means nothing is skipped).
#
# Mapping is intentionally coarse: the six allowlisted mechanical classes are
# {interval_literal, tz_naive_utcnow, bool_is_active, sqlite_datetime_on_pg,
# now_text_cast, immutable_index}. Areas with no mechanical proxy fall through to
# NEUTRAL (1.0), preserving exploration — they are neither boosted nor starved.
_AREA_CLASS_MAP = {
    "reliability": ("tz_naive_utcnow", "now_text_cast"),
    "performance": ("sqlite_datetime_on_pg", "immutable_index"),
    "data_coverage": ("interval_literal", "bool_is_active"),
    # No mechanical fix-class proxy yet — these stay NEUTRAL (explored, never
    # boosted nor starved) until a calibration feed exists for them.
    "conversion_revenue": (),
    "developer_ux": (),
}


def _area_success_weight(area: str) -> float:
    """Learned soft-greedy weight for an AREA, aggregated from the success rates
    of its related mechanical fix-classes via the VETTED honest aggregators in
    brain_work_selector (_read_class_rate + class_success_weight). REUSE-ONLY —
    no hand-rolled SQL, no COUNT(*) (the vetted reader already reads the
    tri-state brain_fix_outcomes.resolved over a recent window).

      · area maps to N fix-classes → mean of their succeeded-rates → soft-greedy
        weight in [FLOOR, CAP].
      · no usable per-class data (untried) → NEUTRAL (1.0), so the area is
        EXPLORED, never starved (anti-starvation floor, mirrors the work
        selector's aging spirit).

    Fail-NEUTRAL: returns 1.0 on missing area or ANY error — a DB hiccup must
    never penalise (or permanently demote) an area."""
    try:
        classes = _AREA_CLASS_MAP.get(area, ())
        if not classes:
            return 1.0  # no proxy → neutral (explored, not starved)
        from routes.brain_work_selector import (
            _read_class_rate, _soft_greedy, WORK_NEUTRAL,
        )
        rates = []
        total_samples = 0
        for klass in classes:
            try:
                rate, samples = _read_class_rate(klass)
            except Exception:
                rate, samples = None, 0
            if rate is not None and samples > 0:
                rates.append(rate)
                total_samples += int(samples)
        if not rates:
            return float(WORK_NEUTRAL)  # untried area → neutral (explored)
        mean_rate = sum(rates) / float(len(rates))
        # Use the aggregate sample count so a single sparse class can't be
        # over-trusted; _soft_greedy already blends small samples toward neutral.
        return float(_soft_greedy(mean_rate, total_samples))
    except Exception as e:
        logger.warning("brain_enhancer: area success weight failed for %r: %s",
                       area, e)
        return 1.0


def _reorder_by_learned_success(opps: list[dict]) -> list[dict]:
    """REORDER opportunities so areas where the brain HISTORICALLY succeeds are
    mined first, while keeping an anti-starvation floor so NO area is permanently
    skipped. REORDER-ONLY:

      · NEVER drops or invents an opportunity — the result is a permutation of
        the input (same length, same items).
      · STABLE for ties — original scan order breaks equal-weight ties, so a
        zero-evidence run is byte-identical to the old fixed order.
      · Computes each AREA's learned weight ONCE (not per-opportunity) to avoid
        N+1 DB reads.

    Best-effort: on ANY error it returns the input UNCHANGED (the prior
    canonical->findings->baseline->work_plan order) — it can never crash a scan
    or reorder work away."""
    try:
        items = [o for o in (opps or []) if isinstance(o, dict)]
        # If filtering changed the count, something is off — fall back untouched.
        if len(items) != len(opps or []):
            return list(opps or [])
        if len(items) < 2:
            return list(items)
        # Pre-compute the learned weight per DISTINCT area ONCE (avoid N+1).
        area_weights: dict = {}
        for o in items:
            a = o.get("area")
            if a not in area_weights:
                area_weights[a] = _area_success_weight(a)
        annotated = []
        for idx, o in enumerate(items):
            w = area_weights.get(o.get("area"), 1.0)
            # (-weight, original_index) → stable descending by learned weight,
            # ties broken by the original scan order.
            annotated.append((-float(w), idx, o))
        annotated.sort(key=lambda t: (t[0], t[1]))
        ranked = [t[2] for t in annotated]
        # SAFETY invariant: reorder-only must never drop an opportunity.
        if len(ranked) != len(items):
            return list(items)
        return ranked
    except Exception as e:
        logger.warning("brain_enhancer: learned-scan reorder failed: %s", e)
        return list(opps or [])


def _scan_canonical(out: list[dict]) -> None:
    """data_coverage opportunities grounded in canonical_stats coverage gaps."""
    try:
        from canonical_stats import get_canonical_stats
        s = get_canonical_stats() or {}
    except Exception as e:
        logger.warning("brain_enhancer: canonical scan failed: %s", e)
        return
    try:
        facilities = int(s.get("facilities") or 0)
        verified = int(s.get("facilities_verified") or 0)
        if facilities and verified and verified < facilities:
            gap = facilities - verified
            out.append(_opportunity(
                "data_coverage",
                f"canonical_stats: {verified} verified vs {facilities} tracked "
                f"facilities ({gap} in the unverified discovery pile)",
                "Where is the highest-leverage place to convert tracked-but-"
                "unverified facilities into verified coverage, given the "
                f"verified/tracked gap of {gap}?",
            ))
        countries = int(s.get("countries") or 0)
        if countries:
            out.append(_opportunity(
                "data_coverage",
                f"canonical_stats: facilities span {countries} countries",
                f"Which under-covered regions (within the {countries} tracked "
                "countries) would most improve DC Hub's coverage moat per unit "
                "of ingestion effort?",
            ))
        isos = int(s.get("isos") or 0)
        markets = int(s.get("markets") or 0)
        if isos and markets:
            out.append(_opportunity(
                "data_coverage",
                f"canonical_stats: {markets} DCPI markets across {isos} live ISOs",
                f"Given {markets} DCPI markets but only {isos} ISOs with live "
                "telemetry, is grid-telemetry breadth or market-depth the "
                "higher-leverage coverage investment?",
            ))
    except Exception as e:
        logger.warning("brain_enhancer: canonical opportunity build failed: %s", e)


def _scan_findings(out: list[dict], limit: int = 12) -> None:
    """reliability/performance opportunities grounded in RECENT brain_findings —
    the live detected-issue worklist. We reuse the investigator's gatherer so
    the read path stays in one place."""
    rows = []
    try:
        from routes.brain_investigator import _gather_recent_findings
        rows = _gather_recent_findings(limit=limit) or []
    except Exception as e:
        logger.warning("brain_enhancer: findings scan failed: %s", e)
        return
    # Pick the few most-recurring findings (count desc) as the sharpest signals.
    try:
        ranked = sorted(
            [r for r in rows if isinstance(r, dict)],
            key=lambda r: (r.get("value") or 0),
            reverse=True,
        )
    except Exception:
        ranked = rows
    for r in ranked[:3]:
        claim = str(r.get("claim") or "").strip()
        if not claim:
            continue
        cnt = r.get("value")
        # r-brain-loop (2026-06-30): some findings carry a metric VALUE here
        # (latency ms / backlog size), not a recurrence count. Render those as a
        # value so the agenda stops claiming they were "detected N times" and the
        # investigator isn't handed a mislabeled scalar that forces a refutation.
        try:
            from routes.brain_work_selector import is_value_not_count
            _is_value = is_value_not_count(claim) or is_value_not_count(r.get("issue"))
        except Exception:
            _is_value = False
        if _is_value:
            out.append(_opportunity(
                "reliability",
                f"brain_findings: {claim}"
                + (f" (value {cnt:,})" if isinstance(cnt, (int, float)) and cnt else ""),
                f"This finding reports a metric VALUE of {cnt} (not an occurrence "
                f"count): '{claim[:140]}'. What underlying change moves that value "
                "into a healthy range, rather than re-detecting the same condition?",
            ))
            continue
        # A high-recurrence finding reads as reliability; everything else as a
        # developer_ux/quality opportunity. Either way it is a REAL signal.
        area = "reliability" if (isinstance(cnt, int) and cnt and cnt >= 3) else "developer_ux"
        out.append(_opportunity(
            area,
            f"brain_findings: {claim}"
            + (f" (seen x{cnt})" if isinstance(cnt, int) and cnt else ""),
            f"This recurring finding has been detected{(' ' + str(cnt) + ' times') if isinstance(cnt, int) and cnt else ''}: "
            f"'{claim[:140]}'. What underlying improvement would stop it "
            "recurring rather than just patching each instance?",
        ))


def _scan_health_baseline(out: list[dict]) -> None:
    """A reliability opportunity grounded in HEALTH_BASELINE.md (the known-good
    snapshot) — its existence is itself a signal we can reason about drift from.
    We reuse the investigator's reader."""
    try:
        from routes.brain_investigator import _gather_health_baseline
        ev = _gather_health_baseline() or []
    except Exception as e:
        logger.warning("brain_enhancer: health-baseline scan failed: %s", e)
        return
    if not ev:
        return
    out.append(_opportunity(
        "reliability",
        "HEALTH_BASELINE.md present (curated known-good config + canonical "
        "numbers)",
        "Against the documented HEALTH_BASELINE known-good state, which "
        "single reliability investment would most reduce the chance of the "
        "recurring single-replica flapping / staleness incidents?",
    ))


def _scan_work_plan(out: list[dict]) -> None:
    """conversion_revenue / performance opportunities grounded in the brain's
    own ranked work-plan (what it is choosing to work on next, and why). We
    surface the TOP-ranked open work item as a real signal worth a higher-level
    improvement question."""
    try:
        from routes.brain_work_selector import build_work_plan
        plan = build_work_plan(limit=5) or {}
    except Exception as e:
        logger.warning("brain_enhancer: work-plan scan failed: %s", e)
        return
    ranked = plan.get("ranked") or []
    if not ranked:
        return
    top = ranked[0] if isinstance(ranked[0], dict) else {}
    klass = top.get("class") or "(unknown)"
    lev = top.get("leverage")
    out.append(_opportunity(
        "performance",
        f"brain work-plan: top open item is class '{klass}' "
        f"(leverage {lev}) of {len(ranked)} ranked",
        f"The brain's top-ranked open work item is class '{klass}'. Beyond "
        "fixing that one instance, what systemic change would lift the whole "
        f"'{klass}' class so this work stops dominating the plan?",
    ))


def scan_opportunities() -> list[dict]:
    """Scan the brain's REAL evidence for high-leverage improvement
    OPPORTUNITIES across the five areas. Each item is
    {area, signal, question} and is grounded in a CONCRETE signal (a canonical
    metric, a live finding, a baseline note, or a ranked work item) — never
    invented. Best-effort: returns [] on no data; NEVER raises."""
    out: list[dict] = []
    for fn in (_scan_canonical, _scan_findings, _scan_health_baseline, _scan_work_plan):
        try:
            fn(out)
        except Exception as e:
            logger.warning("brain_enhancer: scan step %s failed: %s",
                           getattr(fn, "__name__", "?"), e)
    # LEARNING-DRIVEN reorder (GOAL B): mine areas where the brain HISTORICALLY
    # succeeds first, instead of the fixed canonical->findings->baseline->
    # work_plan order. REORDER-ONLY + anti-starvation floor + flag-gated +
    # best-effort (a failure logs and falls back to the order above).
    if _learned_scan_enabled():
        try:
            out = _reorder_by_learned_success(out)
        except Exception as e:
            logger.warning("brain_enhancer: learned scan reorder skipped: %s", e)
    return out


# ════════════════════════════════════════════════════════════════════
#  Step 2 + 3: PROPOSE (reuse investigate per-opp) + RANK (reuse selector)
# ════════════════════════════════════════════════════════════════════
def _proposal_title(area: str, signal: str) -> str:
    """A short human-readable title for the proposal chip."""
    sig = (signal or "").strip()
    if ":" in sig:
        sig = sig.split(":", 1)[1].strip()
    return f"[{area}] {sig[:90]}".strip()


def propose_enhancements(*, max_proposals: int = 3, depth: str = "default") -> dict:
    """PROPOSE-ONLY. (1) scan opportunities, (2) for the top `max_proposals` run
    the REUSED brain_investigator.investigate() to get a verified, evidence-
    backed + adversarially-refuted recommendation per opportunity, (3) RANK the
    proposals by leverage x confidence via the REUSED brain_work_selector.

    Returns {opportunities, proposals, model, cannot_enhance?}. It returns
    analysis only — it NEVER acts (no merge/send/PR/write beyond the caller
    storing proposals). Best-effort; NEVER raises; returns cannot_enhance on
    no-API-key / error.
    """
    result: dict = {
        "opportunities": [],
        "proposals": [],
        "model": None,
    }

    # Degrade gracefully with NO key — the per-proposal reasoner is the
    # investigator, which itself returns cannot_investigate without a key, but we
    # short-circuit here so we never spin up N pointless investigations.
    if not _has_api_key():
        result["cannot_enhance"] = "no_api_key"
        return result

    try:
        opportunities = scan_opportunities()
    except Exception as e:
        opportunities = []
        logger.warning("brain_enhancer: scan_opportunities failed: %s", e)
    result["opportunities"] = opportunities
    if not opportunities:
        result["cannot_enhance"] = "no_opportunities"
        return result

    try:
        from routes.brain_investigator import investigate
    except Exception as e:
        logger.warning("brain_enhancer: investigate import failed: %s", e)
        result["cannot_enhance"] = f"investigate_unavailable:{str(e)[:80]}"
        return result

    try:
        n = max(1, int(max_proposals))
    except Exception:
        n = 3

    # Wall-clock budget so a SYNCHRONOUS run stays under the gunicorn 120s worker
    # timeout: each investigate() is ~48s, so once the budget is spent (after at
    # least one proposal) we stop STARTING new ones and surface what we have.
    import time as _time
    try:
        _budget = float(os.environ.get("BRAIN_ENHANCER_TIME_BUDGET_S", "55"))
    except Exception:
        _budget = 55.0
    _start = _time.monotonic()

    candidates: list[dict] = []
    last_model = None
    for opp in opportunities[:n]:
        if candidates and (_time.monotonic() - _start) > _budget:
            logger.info("brain_enhancer: time budget %.0fs hit after %d proposal(s)",
                        _budget, len(candidates))
            break
        question = opp.get("question") or ""
        try:
            inv = investigate(question, depth=depth)
        except Exception as e:
            # PROPOSE-ONLY + best-effort: a single failed investigation must not
            # crash the batch.
            logger.warning("brain_enhancer: investigate failed: %s", e)
            continue
        if not isinstance(inv, dict) or inv.get("cannot_investigate"):
            continue
        recommendation = (inv.get("recommendation") or "").strip()
        if not recommendation:
            continue
        last_model = inv.get("model") or last_model
        confidence = inv.get("confidence")
        try:
            confidence = float(confidence or 0.0)
        except Exception:
            confidence = 0.0
        caveats = list(inv.get("caveats") or [])
        refutation = inv.get("refutation") or {}

        # Honest-numbers fence over the proposal's own surfaced text — a
        # fabrication is FLAGGED (caveat + confidence cap), never silently
        # surfaced. (investigate() already fences internally; this is a
        # belt-and-suspenders pass at the proposal boundary.)
        fab = _fence(recommendation + " " + " ".join(caveats))
        if fab:
            for f in fab:
                tag = f"FABRICATION FLAGGED: {f}"
                if tag not in caveats:
                    caveats.append(tag)
            confidence = min(confidence, 0.3)
            refutation = dict(refutation)
            refutation["fabrication_flagged"] = True

        candidates.append({
            "title": _proposal_title(opp.get("area", ""), opp.get("signal", "")),
            "area": opp.get("area"),
            "signal": opp.get("signal"),
            "recommendation": recommendation,
            "confidence": confidence,
            "caveats": caveats,
            "decision_for_human": inv.get("decision_for_human"),
            "refutation": refutation,
        })

    result["model"] = last_model
    if not candidates:
        result["cannot_enhance"] = "no_proposals_survived"
        return result

    # ── RANK by leverage x confidence (REUSE brain_work_selector) ────
    # rank_work multiplies impact_weight x confidence x class_success_weight and
    # annotates each item with _leverage. With a flat impact (no severity tag)
    # the ranking is dominated by confidence — exactly leverage x confidence —
    # which is what we want. It NEVER drops a candidate.
    try:
        from routes.brain_work_selector import rank_work
        ranked = rank_work(candidates)
    except Exception as e:
        logger.warning("brain_enhancer: rank_work failed: %s", e)
        ranked = candidates

    proposals = []
    for i, c in enumerate(ranked):
        if not isinstance(c, dict):
            continue
        c = dict(c)
        c["leverage_rank"] = c.get("_leverage")
        c.pop("_leverage", None)
        c.pop("_rationale", None)
        proposals.append(c)
    result["proposals"] = proposals
    return result


# ════════════════════════════════════════════════════════════════════
#  Storage (direct psycopg2, mirroring brain_investigator)
# ════════════════════════════════════════════════════════════════════
def _enqueue_run(max_props: int) -> Optional[int]:
    """Insert a PENDING run row so POST /enhance can return immediately and a
    background thread fills it in. The verified chain is far too slow (it calls
    investigate() several times) to run inside a request — 502 + flapping."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_enhancement_runs (status, max_props) "
                "VALUES ('pending', %s) RETURNING id",
                (int(max_props),),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as e:
        logger.warning("brain_enhancer: enqueue run failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _store_proposal(run_id: Optional[int], proposal: dict) -> Optional[int]:
    """Persist a single proposal row. The ONLY write the enhancer performs
    beyond the run row — PROPOSE-ONLY. status defaults 'proposed'. Returns the
    new id (or None)."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_enhancement_proposals "
                "(run_id, title, area, proposal_json, confidence, leverage_rank, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'proposed') RETURNING id",
                (
                    int(run_id) if run_id is not None else None,
                    str(proposal.get("title") or "")[:500],
                    str(proposal.get("area") or "")[:64],
                    json.dumps(proposal)[:200000],
                    float(proposal.get("confidence") or 0.0),
                    float(proposal.get("leverage_rank") or 0.0)
                    if proposal.get("leverage_rank") is not None else None,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as e:
        logger.warning("brain_enhancer: store proposal failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _mark_run(run_id: int, status: str, result: Optional[dict] = None) -> bool:
    """Mark a run done/failed and stash its result summary. Best-effort."""
    conn = _conn()
    if conn is None or run_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_enhancement_runs SET status=%s, result_json=%s WHERE id=%s",
                (str(status)[:32],
                 json.dumps(result)[:200000] if result is not None else None,
                 int(run_id)),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.warning("brain_enhancer: mark run failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


def _run_enhance_async(run_id: int, max_props: int) -> None:
    """Daemon-thread worker: run propose_enhancements, STORE each proposal, mark
    the run done. PROPOSE-ONLY — the worker stores analysis and NOTHING ELSE
    (no merge/send/PR). Never blocks the request, never raises out."""
    try:
        result = propose_enhancements(max_proposals=max_props)
    except Exception as e:
        try:
            _mark_run(run_id, "failed", {"cannot_enhance": f"error:{str(e)[:160]}"})
        except Exception:
            pass
        return
    stored_ids = []
    try:
        for prop in (result.get("proposals") or []):
            pid = _store_proposal(run_id, prop)
            if pid is not None:
                stored_ids.append(pid)
    except Exception as e:
        logger.warning("brain_enhancer: storing proposals failed: %s", e)
    summary = {
        "opportunities": len(result.get("opportunities") or []),
        "proposals_stored": len(stored_ids),
        "proposal_ids": stored_ids,
        "model": result.get("model"),
        "cannot_enhance": result.get("cannot_enhance"),
    }
    try:
        _mark_run(run_id, "done", summary)
    except Exception:
        pass


def _get_run(run_id: int) -> Optional[dict]:
    """Fetch a run + its stored proposals. None on not-found / error."""
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, max_props, result_json, created_at "
                "FROM brain_enhancement_runs WHERE id = %s",
                (int(run_id),),
            )
            r = cur.fetchone()
            if not r:
                return None
            result_json = r[3]
            if isinstance(result_json, str):
                try: result_json = json.loads(result_json)
                except Exception: pass
            created = r[4]
            try: created = created.isoformat()
            except Exception: created = str(created)
            run = {
                "id": r[0], "status": r[1], "max_props": r[2],
                "result": result_json, "created_at": created,
            }
            # Attach the run's stored proposals (ranked highest-leverage first).
            cur.execute(
                "SELECT id, title, area, proposal_json, confidence, leverage_rank, "
                "status, grade, created_at "
                "FROM brain_enhancement_proposals WHERE run_id = %s "
                "ORDER BY leverage_rank DESC NULLS LAST, id ASC",
                (int(run_id),),
            )
            props = [_row_to_proposal(row) for row in (cur.fetchall() or [])]
            run["proposals"] = props
            return run
    except Exception as e:
        logger.warning("brain_enhancer: get run failed: %s", e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _row_to_proposal(row) -> dict:
    pj = row[3]
    if isinstance(pj, str):
        try: pj = json.loads(pj)
        except Exception: pj = {}
    created = row[8]
    try: created = created.isoformat()
    except Exception: created = str(created)
    return {
        "id": row[0], "title": row[1], "area": row[2], "proposal": pj,
        "confidence": row[4], "leverage_rank": row[5], "status": row[6],
        "grade": row[7], "created_at": created,
    }


def _recent_proposals(limit: int = 25) -> list[dict]:
    """Recent proposals across all runs, ranked highest-leverage first. [] on
    error."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, area, proposal_json, confidence, leverage_rank, "
                "status, grade, created_at "
                "FROM brain_enhancement_proposals "
                "ORDER BY created_at DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall() or []
        props = [_row_to_proposal(r) for r in rows]
        # Surface highest-leverage first (created_at desc fetched, leverage sort).
        try:
            props.sort(key=lambda p: (p.get("leverage_rank") or 0.0), reverse=True)
        except Exception:
            pass
        return props
    except Exception as e:
        logger.warning("brain_enhancer: recent proposals failed: %s", e)
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _grade_proposal(prop_id: int, grade: str) -> bool:
    """Record a human grade for CALIBRATION (the verify->learn loop). Returns
    True if a row was updated."""
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_enhancement_proposals SET grade = %s WHERE id = %s",
                (str(grade)[:64], int(prop_id)),
            )
            n = cur.rowcount or 0
            conn.commit()
            return n > 0
    except Exception as e:
        logger.warning("brain_enhancer: grade failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ════════════════════════════════════════════════════════════════════
#  Endpoints (admin-gated)
# ════════════════════════════════════════════════════════════════════
@brain_enhancer_bp.post("/api/v1/brain/enhance")
def enhance():
    """Kick a self-enhancement run. PROPOSE-ONLY: ENQUEUES an async run, returns
    202 {run_id, status}. The worker scans opportunities, runs the verified
    investigate() per opportunity, ranks, and STORES proposals — it NEVER acts.
    Flag-gated — when BRAIN_ENHANCER_ENABLED is off this returns {enabled: false}
    WITHOUT calling any model. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    if not _enabled():
        # Short-circuit BEFORE any model call / opportunity scan.
        return jsonify(ok=True, enabled=False,
                       note="BRAIN_ENHANCER_ENABLED is off — enhancer ships "
                            "dark (propose-only). Set it to 1 to enable."), 200
    body = request.get_json(silent=True) or {}
    try:
        max_props = int(body.get("max") or 2)
    except Exception:
        max_props = 2
    max_props = max(1, min(max_props, 5))
    # SYNCHRONOUS: propose_enhancements runs investigate() per opportunity (~48s
    # each) under a wall-clock budget (BRAIN_ENHANCER_TIME_BUDGET_S, default 55s)
    # so the whole call stays under the gunicorn 120s worker timeout. Running it
    # in-request is DURABLE — no daemon thread for a redeploy/worker-recycle to
    # silently kill (which left runs stuck 'pending'). PROPOSE-ONLY: it scans,
    # reasons, ranks, and STORES proposals; it NEVER acts.
    run_id = _enqueue_run(max_props)
    result = propose_enhancements(max_proposals=max_props)
    stored_ids = []
    try:
        for prop in (result.get("proposals") or []):
            pid = _store_proposal(run_id, prop)
            if pid is not None:
                stored_ids.append(pid)
    except Exception as e:
        logger.warning("brain_enhancer: storing proposals failed: %s", e)
    summary = {
        "opportunities": len(result.get("opportunities") or []),
        "proposals_stored": len(stored_ids),
        "proposal_ids": stored_ids,
        "model": result.get("model"),
        "cannot_enhance": result.get("cannot_enhance"),
    }
    if run_id is not None:
        try:
            _mark_run(run_id, "done", summary)
        except Exception:
            pass
    return jsonify(ok=True, enabled=True, run_id=run_id, status="done",
                   proposals=result.get("proposals") or [], summary=summary,
                   note="PROPOSE-ONLY: nothing is acted on."), 200


@brain_enhancer_bp.get("/api/v1/brain/enhance/<int:run_id>")
def get_enhance_run(run_id):
    """Fetch a run + its stored proposals. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    run = _get_run(run_id)
    if run is None:
        return jsonify(ok=False, error="not_found"), 404
    return jsonify(ok=True, **run), 200


@brain_enhancer_bp.get("/api/v1/brain/enhancements")
def list_enhancements():
    """Recent proposals across runs, ranked highest-leverage first. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    try:
        limit = int(request.args.get("limit", "25"))
    except Exception:
        limit = 25
    limit = max(1, min(limit, 200))
    props = _recent_proposals(limit=limit)
    return jsonify(ok=True, count=len(props), proposals=props,
                   note="PROPOSE-ONLY — these are improvement proposals for a "
                        "human to review/decide; nothing here is acted on."), 200


@brain_enhancer_bp.post("/api/v1/brain/enhancements/<int:prop_id>/grade")
def grade_enhancement(prop_id):
    """Record a human grade (good/bad/score) for CALIBRATION — the verify->learn
    loop applied to the enhancer's own track record. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    body = request.get_json(silent=True) or {}
    grade = (str(body.get("grade") or "")).strip()
    if not grade:
        return jsonify(ok=False, error="grade required"), 400
    ok = _grade_proposal(prop_id, grade)
    if not ok:
        return jsonify(ok=False, error="not_found_or_db_error"), 404
    return jsonify(ok=True, id=prop_id, grade=grade), 200


def register_brain_enhancer(app) -> None:
    """Idempotent registration helper for main.py. Best-effort schema init."""
    try:
        init_enhancer_schema()
    except Exception as e:
        logger.warning("brain_enhancer: schema init skipped: %s", e)
    try:
        app.register_blueprint(brain_enhancer_bp)
    except Exception as e:
        logger.warning("brain_enhancer already registered: %s", e)
