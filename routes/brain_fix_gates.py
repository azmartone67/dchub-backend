"""
routes/brain_fix_gates.py — r-escalation-ladder (2026-07-18).

THE EVIDENCE-GATED ESCALATION LADDER (spec: docs/brain-pr-escalation-gates.md,
distilled from the 2026-07-18 external design review of the model-relations →
brain_self_director → L22 draft-PR path).

Principle: **auto-open a draft PR only when evidence indicates a contract
improvement, not merely a model preference.**

    Observation → Proposal → Candidate → Draft PR

Candidate → Draft PR is the ONLY automated escalation step, and it is governed
here by:

  · four POSITIVE GATES (ALL required): stability, verdict-difference,
    deterministic-evidence, contract-locality;
  · six HARD BLOCKS (ANY one prevents auto-PR): active 5xx instability,
    conflicting recommendations, business-policy surface, backwards-compat
    break, low-confidence provenance — plus the caller-side negative-evidence
    screen (a condition fully explained by transient infra never reaches this
    module because the evaluator pipeline filters it);
  · a CONFIDENCE score that must clear the threshold (default 0.85):

        pr_confidence = 0.30*repeatability + 0.25*evaluator_agreement
                      + 0.20*deterministic_evidence + 0.15*measured_improvement
                      + 0.10*locality

This module is deliberately split in two halves:

  1. PURE GATE MATH — evaluate_escalation() and its helpers take a plain dict
     and return a plain dict. No DB, no Flask, no network, no clock: same
     input, same verdict, unit-testable to the fourth decimal.
  2. A MINIMAL VERDICT LOG — brain_gate_verdicts (a log, not a platform): one
     row per evaluation so a merged gated PR is attributable to the verdict
     that opened it, plus the feedback stub grade_prior_verdicts() (the spec's
     "merged != beneficial" loop: a condition that re-fires AFTER its gated PR
     opened marks that PR's verdict outcome='recurred_after_pr').

THE HUMAN-MERGE REQUIREMENT IS UNTOUCHED: this module gates PR *creation*
only. Everything it lets through is a doc-only DRAFT spec PR (see
brain_pr_opener.open_spec_pr — SPEC-ONLY marker convention, r-spec-honesty)
that a human merges or discards.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional
from util.json_column import json_for_column

logger = logging.getLogger(__name__)

# ── the four deterministic-evidence kinds the spec admits ────────────
# "schema-validation failures, CI failures, repeated HTTP responses,
#  reproducible regressions. 'Feels cleaner' never escalates."
DETERMINISTIC_EVIDENCE_KINDS = frozenset(
    ("schema_failure", "ci_failure", "http_repro", "benchmark"))

# ── the confidence formula (spec §Confidence score) ──────────────────
WEIGHTS = {
    "repeatability": 0.30,
    "evaluator_agreement": 0.25,
    "deterministic_evidence": 0.20,
    "measured_improvement": 0.15,
    "locality": 0.10,
}
DEFAULT_THRESHOLD = 0.85

# Stability gate: the same structural recommendation must recur this many
# times across different runs/prompts before it is more than a preference.
STABILITY_MIN_REPEATS = 3

# Hard block: active instability — when more than this fraction of recent
# eval runs observed NEW 5xx, the evaluator may be reacting to infra, not
# design (the r-429-backoff note in the spec reduces false positives here,
# but the block stays).
DEFAULT_5XX_BLOCK_RATE = 0.05


def escalation_threshold() -> float:
    """Auto-open threshold on the confidence score. Env-tunable via
    BRAIN_ESCALATION_THRESHOLD (default 0.85)."""
    try:
        return float(os.environ.get("BRAIN_ESCALATION_THRESHOLD",
                                    str(DEFAULT_THRESHOLD)))
    except Exception:
        return DEFAULT_THRESHOLD


def five_xx_block_rate() -> float:
    """Recent-5xx-rate above which the active-instability hard block fires.
    Env-tunable via BRAIN_ESCALATION_5XX_BLOCK_RATE (default 0.05)."""
    try:
        return float(os.environ.get("BRAIN_ESCALATION_5XX_BLOCK_RATE",
                                    str(DEFAULT_5XX_BLOCK_RATE)))
    except Exception:
        return DEFAULT_5XX_BLOCK_RATE


# ════════════════════════════════════════════════════════════════════
#  Contract locality (spec gate 4 + the business-policy hard block)
# ════════════════════════════════════════════════════════════════════
# Only OpenAPI/schemas/docs/recipes/CI/envelopes/orchestration metadata
# auto-escalate. Ranking, business logic, pricing, data interpretation →
# human approval. Token-based on path segments so 'auth' never matches
# 'author' by substring accident.
_LOCAL_TOKENS = frozenset((
    "docs", "doc", "readme", "md",
    "schema", "schemas", "openapi", "swagger",
    "contract", "contracts", "recipe", "recipes",
    "envelope", "envelopes", "manifest", "manifests",
    "ci", "workflows", "workflow", "github",
    "orchestration", "metadata",
))
_HUMAN_ONLY_TOKENS = frozenset((
    "pricing", "price", "prices", "billing", "payment", "payments",
    "checkout", "paywall", "stripe",
    "auth", "authn", "authz", "oauth", "authentication", "authorization",
    "quota", "quotas", "ratelimit",
    "permission", "permissions", "licensing", "license", "licence",
    "ranking", "rank", "dcpi",
    "business", "policy",
))

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT_RE.split(str(text or "").lower()) if t]


def classify_path(path: str) -> str:
    """'human_only' | 'local' | 'unknown' for one touched surface. Human-only
    wins over local (a pricing doc is still a pricing change). Anything
    unrecognized is 'unknown' — which FAILS the locality gate: an unclassified
    surface gets a human, never an auto-escalation."""
    toks = _tokens(path)
    if any(t in _HUMAN_ONLY_TOKENS for t in toks):
        return "human_only"
    if any(t in _LOCAL_TOKENS for t in toks):
        return "local"
    return "unknown"


# Breaking-change language in a recommendation text: removing/renaming/
# dropping a field/column/param/endpoint/tool/key is a backwards-compat
# break regardless of where it lands.
_BREAKING_RE = re.compile(
    r"\b(remov\w*|renam\w*|drop\w*|delet\w*)\b\s+(?:\w+\s+){0,3}"
    r"(field|column|param|parameter|endpoint|tool|key|route)s?\b")


def text_flags(text: str) -> dict:
    """Deterministic screens over a recommendation's TEXT (used when the
    concrete files a change would touch are not yet known — e.g. a structural
    ask from an external evaluator). Returns:
      human_only_surfaces — pseudo-paths ('policy:pricing', …) for every
                            business-policy keyword present, which classify_path
                            maps straight back to 'human_only';
      breaking            — True when the text asks to remove/rename a
                            field/param/endpoint (backwards-compat break)."""
    toks = set(_tokens(text))
    surfaces = sorted(f"policy:{t}" for t in (toks & _HUMAN_ONLY_TOKENS))
    return {
        "human_only_surfaces": surfaces,
        "breaking": bool(_BREAKING_RE.search(str(text or "").lower())),
    }


def surfaces_from_text(text: str) -> list[str]:
    """Just the human-only pseudo-surfaces from text_flags()."""
    return text_flags(text)["human_only_surfaces"]


def extract_evidence_kinds(verdict) -> list[str]:
    """Deterministic-evidence kinds present in an evaluator verdict dict.
    Only EXPLICIT machine signals count — a count field > 0 or an explicit
    evidence-kind list. Prose never yields evidence ('feels cleaner' never
    escalates). Sorted for determinism; [] on anything malformed."""
    v = verdict if isinstance(verdict, dict) else {}
    kinds: set = set()
    for key, kind in (("schema_failures", "schema_failure"),
                      ("contract_failures", "schema_failure"),
                      ("ci_failures", "ci_failure"),
                      ("http_repro", "http_repro"),
                      ("repeated_http_failures", "http_repro"),
                      ("benchmark_delta", "benchmark")):
        val = v.get(key)
        if val is None:
            continue
        try:
            if float(val) > 0:
                kinds.add(kind)
        except (TypeError, ValueError):
            if isinstance(val, (list, tuple)) and len(val) > 0:
                kinds.add(kind)
    ev = v.get("deterministic_evidence")
    if not isinstance(ev, (list, tuple)):
        ev = v.get("evidence") if isinstance(v.get("evidence"),
                                             (list, tuple)) else []
    for item in ev:
        k = str(item).strip().lower()
        if k in DETERMINISTIC_EVIDENCE_KINDS:
            kinds.add(k)
    return sorted(kinds)


# ════════════════════════════════════════════════════════════════════
#  THE PURE GATE — evaluate_escalation
# ════════════════════════════════════════════════════════════════════
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _int0(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def evaluate_escalation(candidate: dict,
                        threshold: Optional[float] = None) -> dict:
    """Evaluate ONE escalation candidate against the ladder. PURE and
    DETERMINISTIC: no DB, no network, no clock — same input, same verdict.

    candidate keys (all optional; missing = the conservative default):
      repeat_count               int   — same structural recommendation seen N
                                         times across runs/prompts
      distinct_sources           int   — independent evaluators/platforms that
                                         named it
      evidence                   [str] — deterministic-evidence kinds among
                                         DETERMINISTIC_EVIDENCE_KINDS
      files_touched              [str] — surfaces the change would touch
                                         (real paths or 'policy:*' pseudo-
                                         surfaces from text_flags)
      expected_improvement       float|None — measurable expected improvement
                                         in [0,1]; None = no metric named
      recent_5xx_rate            float — fraction of recent eval runs that
                                         observed NEW 5xx
      conflicting_recs           bool  — evaluators recommend conflicting
                                         changes (surface both, never
                                         synthesize)
      removes_or_renames_fields  bool  — backwards-compat break

    Returns {state, confidence, threshold, auto_escalate, gates, hard_blocks,
    components, inputs} where state ∈ {observation, proposal, candidate,
    draft_pr} and gates maps each gate name to {passed, reason}. ANY hard
    block caps the state at 'proposal'. Never raises on malformed input —
    every coercion failure degrades to the conservative default."""
    c = candidate if isinstance(candidate, dict) else {}

    repeat = _int0(c.get("repeat_count"))
    sources = _int0(c.get("distinct_sources"))
    kinds = sorted({str(k).strip().lower() for k in (c.get("evidence") or [])
                    if str(k).strip().lower() in DETERMINISTIC_EVIDENCE_KINDS})
    files = [str(f).strip() for f in (c.get("files_touched") or [])
             if str(f).strip()]
    imp = None
    if c.get("expected_improvement") is not None:
        try:
            imp = float(c.get("expected_improvement"))
        except (TypeError, ValueError):
            imp = None
    try:
        rate = max(0.0, float(c.get("recent_5xx_rate") or 0.0))
    except (TypeError, ValueError):
        rate = 0.0
    conflicting = bool(c.get("conflicting_recs"))
    breaking = bool(c.get("removes_or_renames_fields"))
    if threshold is None:
        threshold = escalation_threshold()

    # ── locality classification ──────────────────────────────────────
    classes = {f: classify_path(f) for f in files}
    human_only = sorted(f for f, k in classes.items() if k == "human_only")
    non_local = sorted(f for f, k in classes.items() if k != "local")

    # ── the four positive gates (ALL required) ───────────────────────
    gates = {}
    ok = repeat >= STABILITY_MIN_REPEATS
    gates["stability"] = {
        "passed": ok,
        "reason": (f"repeat_count {repeat} >= {STABILITY_MIN_REPEATS}" if ok
                   else f"repeat_count {repeat} < {STABILITY_MIN_REPEATS} — "
                        "the same structural recommendation must recur across "
                        "runs/prompts before it is more than a preference"),
    }
    ok = imp is not None and imp > 0
    gates["verdict_diff"] = {
        "passed": ok,
        "reason": (f"measurable expected improvement {imp}" if ok
                   else "no measurable expected improvement named — no metric "
                        "means it stays a proposal"),
    }
    ok = bool(kinds)
    gates["deterministic_evidence"] = {
        "passed": ok,
        "reason": (f"deterministic evidence present: {', '.join(kinds)}" if ok
                   else "no deterministic evidence (schema/CI/http-repro/"
                        "benchmark) — 'feels cleaner' never escalates"),
    }
    ok = bool(files) and not non_local
    gates["contract_locality"] = {
        "passed": ok,
        "reason": ("all touched surfaces are contract-local "
                   "(docs/schemas/contracts/CI/envelopes)" if ok
                   else (f"non-contract-local surfaces: "
                         f"{', '.join(non_local)}" if files
                         else "no touched surfaces listed — unclassifiable "
                              "changes get a human")),
    }

    # ── hard blocks (ANY one prevents auto-PR) ───────────────────────
    hard_blocks = []
    if rate > five_xx_block_rate():
        hard_blocks.append({
            "name": "active_5xx_instability",
            "reason": (f"recent_5xx_rate {round(rate, 4)} > "
                       f"{five_xx_block_rate()} — the evaluator may be "
                       "reacting to infra, not design"),
        })
    if conflicting:
        hard_blocks.append({
            "name": "conflicting_recommendations",
            "reason": "evaluators recommend conflicting changes — surface "
                      "both, never synthesize",
        })
    if human_only:
        hard_blocks.append({
            "name": "business_policy_surface",
            "reason": ("business-policy surface touched: "
                       f"{', '.join(human_only)} — pricing/auth/quota/"
                       "permissions/licensing/ranking are human-only"),
        })
    if breaking:
        hard_blocks.append({
            "name": "breaking_change",
            "reason": "removes or renames fields — backwards-compat break "
                      "requires a human",
        })
    if repeat <= 1 and sources <= 1:
        hard_blocks.append({
            "name": "low_confidence_provenance",
            "reason": "one model, one run — weak provenance never "
                      "auto-escalates",
        })

    # ── confidence score (the exact spec formula) ────────────────────
    components = {
        "repeatability": _clamp01(repeat / float(STABILITY_MIN_REPEATS)),
        "evaluator_agreement": _clamp01(sources / 3.0),
        "deterministic_evidence": _clamp01(0.5 * len(kinds)),
        "measured_improvement": _clamp01(imp if imp is not None else 0.0),
        "locality": 1.0 if gates["contract_locality"]["passed"] else 0.0,
    }
    confidence = round(sum(WEIGHTS[k] * components[k] for k in WEIGHTS), 4)

    # ── the ladder state ─────────────────────────────────────────────
    any_signal = repeat >= 1 or sources >= 1 or bool(kinds)
    all_gates = all(g["passed"] for g in gates.values())
    if hard_blocks:
        # A hard block caps escalation at Proposal — never Candidate, never
        # Draft PR — no matter how strong the other signals are.
        state = "proposal" if any_signal else "observation"
    elif all_gates and confidence >= threshold:
        state = "draft_pr"
    elif (gates["stability"]["passed"]
          and gates["deterministic_evidence"]["passed"] and sources >= 2):
        state = "candidate"
    elif any_signal:
        state = "proposal"
    else:
        state = "observation"

    return {
        "state": state,
        "confidence": confidence,
        "threshold": threshold,
        "auto_escalate": state == "draft_pr",
        "gates": gates,
        "hard_blocks": hard_blocks,
        "components": components,
        "inputs": {
            "repeat_count": repeat,
            "distinct_sources": sources,
            "evidence": kinds,
            "files_touched": files,
            "expected_improvement": imp,
            "recent_5xx_rate": round(rate, 4),
            "conflicting_recs": conflicting,
            "removes_or_renames_fields": breaking,
        },
    }


# ════════════════════════════════════════════════════════════════════
#  Verdict log + feedback stub (a log, not a platform)
# ════════════════════════════════════════════════════════════════════
def _conn():
    """Raw psycopg2 connection (mirrors brain_self_director._conn). None when
    no DSN / driver — every consumer degrades to a no-op."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_fix_gates: _conn failed: %s", e)
    return None


def _ensure_verdicts_table(cur) -> None:
    """Idempotent DDL for the minimal verdict log. Lazy (first write), so no
    main.py wiring is needed."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brain_gate_verdicts (
            id          BIGSERIAL PRIMARY KEY,
            fingerprint TEXT,
            agenda_id   BIGINT,
            pr_number   BIGINT,
            state       TEXT,
            confidence  DOUBLE PRECISION,
            verdict     JSONB,
            outcome     TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_brain_gate_verdicts_fp "
        "ON brain_gate_verdicts (fingerprint, created_at DESC)")


def record_gate_verdict(fingerprint, verdict: dict,
                        pr_number=None, agenda_id=None) -> Optional[int]:
    """Log ONE gate evaluation so a later merge/recurrence is attributable to
    the verdict that produced it. Best-effort: returns the new row id, or None
    on any failure (a verdict-log hiccup must never affect the tick)."""
    conn = _conn()
    if conn is None:
        return None
    try:
        v = verdict if isinstance(verdict, dict) else {}
        with conn.cursor() as cur:
            _ensure_verdicts_table(cur)
            cur.execute(
                "INSERT INTO brain_gate_verdicts "
                "(fingerprint, agenda_id, pr_number, state, confidence, "
                "verdict) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (
                    (str(fingerprint).strip() or None) if fingerprint else None,
                    int(agenda_id) if agenda_id is not None else None,
                    int(pr_number) if pr_number is not None else None,
                    str(v.get("state") or "")[:32] or None,
                    float(v.get("confidence") or 0.0),
                    json_for_column(v, 100000),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as e:
        logger.warning("brain_fix_gates: record_gate_verdict failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


def grade_prior_verdicts(fingerprint) -> int:
    """FEEDBACK STUB (spec §Feedback loop — merged != beneficial). Called when
    a condition RE-FIRES: any earlier verdict for the same fingerprint that
    actually opened a PR and has no outcome yet is marked
    outcome='recurred_after_pr' — the gated PR produced no measurable
    improvement, and the next evaluation can weigh that. (Conditions that
    improved simply never re-fire, so their outcome stays NULL = no recurrence
    observed.) MUST run BEFORE record_gate_verdict for the new evaluation so
    the fresh row never grades itself. Returns rows updated; 0 on any error —
    never raises."""
    fp = (str(fingerprint).strip() if fingerprint else "")
    if not fp:
        return 0
    conn = _conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            _ensure_verdicts_table(cur)
            cur.execute(
                "UPDATE brain_gate_verdicts SET outcome = 'recurred_after_pr' "
                "WHERE fingerprint = %s AND pr_number IS NOT NULL "
                "AND outcome IS NULL",
                (fp,),
            )
            n = cur.rowcount or 0
            conn.commit()
            return int(n)
    except Exception as e:
        logger.warning("brain_fix_gates: grade_prior_verdicts failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        try: conn.close()
        except Exception: pass
