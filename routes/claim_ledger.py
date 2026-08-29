"""routes/claim_ledger.py — the claim ledger + pre-registration (Claim Loop step 1).

WHY THIS EXISTS (2026-08-22)
----------------------------
The brain runs all six stations of a research → propose → gate → ship → verify
→ learn loop, but on the wrong object: its own plumbing. The object that has the
properties a loop needs — small, stated, testable against a live source,
reversible, with a scalar outcome — is the CLAIM: a count, a score, a tool
answer, a post, a registry listing, a canon floor.

This module is the ledger for those claims. It is built on L16's
brain_predictions_log (routes/brain_layer16_self_critique.py), not a fifth
store: L16 already records predicted-vs-actual for the brain's self-critique,
so the ledger extends that table with the claim vocabulary and puts every
producer through ONE contract:

    register_claim(...)    BEFORE ship — REFUSED when it carries no expectation
    stamp_shipped(id)      when the artefact is actually out
    stamp_outcome(id, …)   at horizon, by the VERIFIER — never by the author

THE CONTRACT
------------
* No expectation, no row. `expected_metric` names the instrument —
      linkedin:<post_id> impressions      social_media_posts → linkedin_posts
      finding:<url> status                brain_findings (the radar's writer)
      canon:<dotted.key>                  ai_surface_canon.resolve_canon(),
                                          the LIVE override — UNOBSERVED when
                                          the resolver fell back to the pin
      get:<path> <dotted.field>           an internal GET via the envelope
  and `expected_value` is the comparator: ">= 17", "== resolved", "< 5",
  "!= 0", "absent", "present". Both are required; a malformed one is refused.
* `regime` (jsonb) carries `as_of` plus whatever the number is RELATIVE to —
  canon basis, window definition — so a refutation names the regime it failed in.
* Outcome writer ≠ author. stamp_outcome runs from the L16 cron tick
  (POST /api/v1/brain/self-critique/run → verify_due_claims); producers can
  only register and mark shipped.
* An instrument that has not measured yet is UNOBSERVED, not refuted. Inside a
  grace window (2× horizon) the claim stays open for the next tick; past it the
  row is stamped `unobserved`, so the gap is visible instead of silently pending.

★ THE SCHEMA TRAP (brain_fast_qa.py: "the repo DDL has lied"). The LIVE table
was audited over information_schema on 2026-08-22 BEFORE a column was added:
11 columns, exactly the L16 DDL, 939 rows (849 L14 + 90 QA; `confidence` is
TEXT and holds both 0.5 and high/low/medium). The claim columns are added here
with ALTER TABLE … ADD COLUMN IF NOT EXISTS through db_utils.ddl_cursor() —
the pooled cursor silently DROPS DDL (SKIP_DDL trap) — and the L16 columns are
untouched: source_layer / predicted_at / verified_at / was_correct keep their
meaning for L14, L18, the self-model and the reliability shell. Claim rows
carry source_layer='CLAIM' and verification_criterion NULL, so L16's LLM verify
path (which selects on verification_criterion) never picks them up — claims are
judged deterministically by `judge()` below, no model in the loop.

Measurement-definition note (stated so it is a marker, not a drift):
brain_self_model.predictions_30d and reliability_master_shell.predictions_30d
COUNT(*) this table; from this change those counts include claim rows. A claim
IS a pre-registered prediction — that widening is intended.

Surfaces (admin; under /api/v1/brain/ for the CF bypass rule):
  GET  /api/v1/brain/claims?limit=&kind=&outcome=   recent claims, newest first
  POST /api/v1/brain/claims                          register one (JSON body)
  POST /api/v1/brain/claims/verify                   judge the due claims now
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
claim_ledger_bp = Blueprint("claim_ledger", __name__)

KINDS = ("fact", "score", "tool_answer", "post", "listing", "canon", "fix",
         "tool_copy",
         # ★2026-08-29 lane 6: a QA check that PASSED, pre-registered so a
         # green that later stops being true is judged at horizon instead of
         # standing until someone re-runs the harness by hand. Widening KINDS
         # is additive; OUTCOMES deliberately is not widened.
         "qa")
OUTCOMES = ("confirmed", "refuted", "retracted", "unobserved")
SOURCE_LAYER = "CLAIM"

# Columns this module adds to brain_predictions_log. Every ALTER is
# IF NOT EXISTS, so the list is safe to re-run on every boot.
CLAIM_COLUMNS = (
    ("kind", "TEXT"),
    ("subject", "TEXT"),
    ("statement", "TEXT"),
    ("regime", "JSONB"),
    ("surfaces", "TEXT[]"),
    ("expected_metric", "TEXT"),
    ("expected_value", "TEXT"),
    ("horizon_hours", "INTEGER"),
    ("shipped_at", "TIMESTAMPTZ"),
    ("outcome", "TEXT"),
    ("outcome_evidence", "TEXT"),
    ("outcome_at", "TIMESTAMPTZ"),
    # ★2026-08-22 step 5: a retraction names its replacement. Additive and
    # idempotent like every column above (ADD COLUMN IF NOT EXISTS).
    ("superseded_by", "INTEGER"),
)

# A due claim whose instrument has not measured yet stays open until this
# many horizons have passed; then it is stamped `unobserved`.
GRACE_MULTIPLIER = 2

# Producer defaults — named here so a test can assert the contract each
# producer signs, not the number it happened to pick.
FINDING_HORIZON_HOURS = 168          # the squasher's own "no fix in 7d" window
CANON_HORIZON_HOURS = 24

SHAPE = {
    "claim": ("{id, registered_at, kind, subject, statement, regime{as_of,…}, "
              "surfaces[], expected_metric, expected_value, horizon_hours, "
              "shipped_at, due_at, outcome, outcome_evidence, outcome_at, "
              "superseded_by}"),
    "kinds": KINDS,
    "outcomes": OUTCOMES,
    "rule": ("register BEFORE ship with an expectation or be refused; outcome "
             "is stamped at horizon by the L16 cron (outcome writer ≠ author); "
             "unobserved = instrument gap, never a refutation"),
    "retraction": ("retract(id, reason, superseded_by=None) — the OWNER withdraws "
                   "a claim: outcome becomes 'retracted', the prior verdict (if "
                   "any) is kept in outcome_evidence.prior_outcome, and "
                   "superseded_by names the replacement claim. A retracted claim "
                   "is never a refuted_kept one. Public at /api/v1/ops/claims."),
}

_SCHEMA_STATE = {"ok": False}
_CANON_MEMO: dict = {}


# ── plumbing ────────────────────────────────────────────────────────────

def _db_url():
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def _conn():
    import psycopg2
    return psycopg2.connect(_db_url(), connect_timeout=5)


def _now_iso() -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            .replace(microsecond=0).isoformat())


def _short(v, n: int = 200) -> str:
    try:
        return json.dumps(v, default=str)[:n]
    except Exception:  # noqa: BLE001
        return str(v)[:n]


def _iso(v):
    if v is None:
        return None
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


def ensure_schema(force: bool = False) -> bool:
    """Add the claim columns to brain_predictions_log. Idempotent, memoised per
    process, fail-soft: returns False and logs. The table itself is L16's —
    this module never creates it, so a missing table is reported, not
    papered over with a second copy of someone else's DDL."""
    if _SCHEMA_STATE["ok"] and not force:
        return True
    if not _db_url():
        return False
    try:
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute("SELECT to_regclass('public.brain_predictions_log')")
            row = cur.fetchone()
            if not row or not row[0]:
                logger.warning("[claim_ledger] brain_predictions_log does not "
                               "exist yet — L16 owns it; refusing to create")
                return False
            for col, typ in CLAIM_COLUMNS:
                cur.execute(f"ALTER TABLE brain_predictions_log "
                            f"ADD COLUMN IF NOT EXISTS {col} {typ}")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_claims_due "
                        "ON brain_predictions_log (shipped_at) "
                        "WHERE outcome IS NULL AND shipped_at IS NOT NULL")
        _SCHEMA_STATE["ok"] = True
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] ensure_schema failed: %s", e)
        return False


# ── the contract, as pure functions ─────────────────────────────────────

_CMP_RE = re.compile(r"^\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$")
_METRIC_RE = re.compile(r"^\s*([a-z_]+):(\S+)(?:\s+(\S+))?\s*$")
_SCHEMES = ("get", "finding", "linkedin", "canon")


def parse_expectation(expected_value):
    """'>= 17' -> ('>=', '17'); 'absent' -> ('absent', None); None if malformed."""
    s = (expected_value or "").strip()
    if not s:
        return None
    low = s.lower()
    if low in ("absent", "present"):
        return (low, None)
    m = _CMP_RE.match(s)
    if not m:
        return None
    lit = m.group(2).strip()
    if len(lit) >= 2 and lit[0] == lit[-1] and lit[0] in ("'", '"'):
        lit = lit[1:-1]
    return (m.group(1), lit)


def parse_metric(expected_metric):
    """'linkedin:123 impressions' -> ('linkedin', '123', 'impressions')."""
    m = _METRIC_RE.match(expected_metric or "")
    if not m or m.group(1) not in _SCHEMES:
        return None
    return m.group(1), m.group(2), m.group(3)


def _num(v):
    """Best-effort numeric read: 18406, '18,406', '18,500+', 33.8, True.
    None when the value is not a number."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(",", "")
        if t.endswith("+"):
            t = t[:-1]
        try:
            return float(t)
        except ValueError:
            return None
    return None


def judge(actual, expected_value) -> str:
    """Compare a resolved value against the pre-registered expectation.

    Returns 'confirmed' | 'refuted' | 'unobserved'. `actual is None` means the
    instrument did not measure — that is UNOBSERVED for every comparator except
    absent/present, where absence is the thing being claimed."""
    parsed = parse_expectation(expected_value)
    if parsed is None:
        return "unobserved"
    op, lit = parsed
    if op == "absent":
        return "confirmed" if actual is None else "refuted"
    if op == "present":
        return "confirmed" if actual is not None else "refuted"
    if actual is None:
        return "unobserved"
    a_num, l_num = _num(actual), _num(lit)
    if op in ("<", "<=", ">", ">="):
        if a_num is None or l_num is None:
            return "unobserved"
        ok = {"<": a_num < l_num, "<=": a_num <= l_num,
              ">": a_num > l_num, ">=": a_num >= l_num}[op]
        return "confirmed" if ok else "refuted"
    if a_num is not None and l_num is not None:
        eq = math.isclose(a_num, l_num, rel_tol=1e-9, abs_tol=1e-9)
    else:
        eq = str(actual).strip().lower() == str(lit).strip().lower()
    if op == "==":
        return "confirmed" if eq else "refuted"
    return "refuted" if eq else "confirmed"


def dig(payload, path):
    """Walk a dict/list by dotted path ('public.facilities', 'rows.0.n').

    ★2026-08-29 (lane 6). A trailing '#len' returns the LENGTH of what the path
    resolves to: 'sources#len' -> len(payload['sources']).

    The natural QA assertion is a count of a list, and _num() correctly refuses
    to make a number out of a list — so without this a claim like
    `get:/api/v1/data-freshness sources >= 8` judges `unobserved` forever. A
    claim that can never be measured is worse than no claim: it looks like
    coverage and never refutes anything.

    It is an EXPLICIT suffix rather than implicit len()-on-list coercion,
    because silently turning a list into its length would change what every
    existing metric means, and 'absent'/'present' comparators genuinely care
    about the container, not its size.
    """
    want_len = False
    path = path or ""
    if path.endswith("#len"):
        want_len, path = True, path[:-4]
    cur = payload
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    if want_len:
        try:
            return len(cur)
        except TypeError:
            return None       # not sized: not measured, never a zero
    return cur


def validate_claim(kind, subject, statement, expected_metric, expected_value,
                   horizon_hours, regime) -> str | None:
    """The contract. Returns the refusal reason, or None when the claim may
    be registered. No I/O — a refusal never touches the database."""
    if kind not in KINDS:
        return f"kind must be one of {KINDS}, got {kind!r}"
    if not (subject or "").strip():
        return "subject required"
    if not (statement or "").strip():
        return "statement required (the literal claim)"
    if not (expected_metric or "").strip():
        return ("expected_metric required — a claim with no expectation "
                "cannot ship")
    if parse_metric(expected_metric) is None:
        return (f"expected_metric {expected_metric!r} is not "
                f"'<scheme>:<target> [field]' with scheme in {_SCHEMES}")
    if not (expected_value or "").strip():
        return ("expected_value required — a claim with no expectation "
                "cannot ship")
    if parse_expectation(expected_value) is None:
        return (f"expected_value {expected_value!r} is not a comparator "
                f"(== != < <= > >= absent present)")
    try:
        if int(horizon_hours) <= 0:
            return "horizon_hours must be > 0"
    except (TypeError, ValueError):
        return "horizon_hours must be an integer"
    if regime is not None and not isinstance(regime, dict):
        return "regime must be a dict"
    return None


def refusal(reason: str) -> dict:
    return {"ok": False, "refused": True, "error": f"refused: {reason}"}


# ── register / stamp ────────────────────────────────────────────────────

def register_claim(kind: str, subject: str, statement: str,
                   expected_metric: str, expected_value: str,
                   horizon_hours: int, regime: dict | None = None,
                   surfaces=None, shipped: bool = False) -> dict:
    """Pre-register a claim BEFORE it ships.

    Returns {ok: True, id} (or {ok, already: True, id} when an identical open
    claim exists), {ok: False, refused: True, error} when the contract is not
    met, {ok: False, error} on a DB problem. Refuses — never raises — and a DB
    failure is fail-soft: the producer's own job must not die with the ledger."""
    reason = validate_claim(kind, subject, statement, expected_metric,
                            expected_value, horizon_hours, regime)
    if reason:
        return refusal(reason)
    if not _db_url():
        return {"ok": False, "error": "no database"}
    if not ensure_schema():
        return {"ok": False, "error": "schema unavailable"}
    regime = dict(regime or {})
    regime.setdefault("as_of", _now_iso())
    subject = subject.strip()[:400]
    statement = statement.strip()[:4000]
    metric = expected_metric.strip()[:400]
    value = expected_value.strip()[:200]
    surfaces = [str(s)[:200] for s in (surfaces or [])][:20]
    chain_title = f"claim:{kind}:{subject}"[:400]
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            # One OPEN claim per (subject, statement, metric) — a producer that
            # re-registers the same claim on every fire must not flood the
            # ledger, and the L16 tick must judge each claim once.
            cur.execute(
                "SELECT id FROM brain_predictions_log "
                " WHERE source_layer = %s AND subject = %s AND statement = %s "
                "   AND expected_metric = %s AND outcome IS NULL LIMIT 1",
                (SOURCE_LAYER, subject, statement, metric))
            row = cur.fetchone()
            if row:
                return {"ok": True, "already": True, "id": row[0]}
            # ★ verification_criterion is deliberately NOT set: L16's LLM
            #   verify path selects on it, and claims are judged here.
            # ONE literal on purpose: the regression lint's INSERT rule reads
            # the clause from the same string as the INSERT, and a fragment
            # split hides it (the brain_llm_usage note in scripts/regression_lint.py).
            cur.execute(
                "INSERT INTO brain_predictions_log (source_layer, chain_title, prediction, kind, subject, statement, regime, surfaces, expected_metric, expected_value, horizon_hours, shipped_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN NOW() ELSE NULL END) ON CONFLICT DO NOTHING RETURNING id",  # noqa: E501
                (SOURCE_LAYER, chain_title, statement[:1000], kind, subject,
                 statement, json.dumps(regime, default=str), surfaces, metric,
                 value, int(horizon_hours), bool(shipped)))
            r = cur.fetchone()
            conn.commit()
            return {"ok": True, "id": r[0] if r else None,
                    "shipped": bool(shipped)}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] register failed for %s: %s", subject, e)
        return {"ok": False, "error": str(e)[:200]}


def stamp_shipped(claim_id) -> bool:
    """The artefact is out. Starts the horizon clock. Idempotent."""
    if not claim_id or not _db_url():
        return False
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE brain_predictions_log SET shipped_at = NOW() "
                " WHERE id = %s AND source_layer = %s AND shipped_at IS NULL",
                (claim_id, SOURCE_LAYER))
            n = cur.rowcount or 0
            conn.commit()
            return n > 0
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] stamp_shipped %s failed: %s", claim_id, e)
        return False


def _stamp_outcome_sql(cur, claim_id, outcome: str, evidence) -> bool:
    """The ONE statement that writes an outcome. Only stamps an open claim."""
    if outcome not in OUTCOMES:
        return False
    ev = evidence if isinstance(evidence, str) else json.dumps(evidence, default=str)
    cur.execute(
        "UPDATE brain_predictions_log "
        "   SET outcome = %s, outcome_evidence = %s, outcome_at = NOW() "
        " WHERE id = %s AND source_layer = %s AND outcome IS NULL",
        (outcome, (ev or "")[:4000], claim_id, SOURCE_LAYER))
    return (cur.rowcount or 0) > 0


def stamp_outcome(claim_id, outcome: str, evidence=None) -> bool:
    """Write a claim's outcome. Meant for the VERIFIER (the L16 cron tick) and
    for an operator retraction — never for the producer that registered it."""
    if not claim_id or outcome not in OUTCOMES or not _db_url():
        return False
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            ok = _stamp_outcome_sql(cur, claim_id, outcome, evidence)
            conn.commit()
            return ok
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] stamp_outcome %s failed: %s", claim_id, e)
        return False


def _json_text(payload, cap: int = 4000) -> str:
    """VALID JSON at any size — never a sliced json.dumps string (the
    r-json-truncation class; tests/test_json_column_binding.py)."""
    from util.json_column import json_for_column
    return json_for_column(payload, cap)


def _retract_sql(cur, claim_id, evidence: dict, superseded_by) -> bool:
    """The ONE statement that retracts. Unlike _stamp_outcome_sql it may
    overwrite an existing verdict — a retraction after a refutation is the
    owner's call, and the prior verdict travels in the evidence — but it
    never re-retracts: a claim already retracted is left exactly as it is.
    superseded_by is COALESCEd so a retraction without a replacement cannot
    blank one recorded earlier."""
    cur.execute(
        "UPDATE brain_predictions_log "
        "   SET outcome = 'retracted', outcome_evidence = %s, outcome_at = NOW(), "
        "       superseded_by = COALESCE(%s, superseded_by) "
        " WHERE id = %s AND source_layer = %s "
        "   AND (outcome IS NULL OR outcome <> 'retracted')",
        (_json_text(evidence), superseded_by, claim_id, SOURCE_LAYER))
    return (cur.rowcount or 0) > 0


def retract(claim_id, reason: str, superseded_by=None) -> dict:
    """The OWNER withdraws a claim (step 5, 2026-08-22).

    outcome → 'retracted' (stamp_outcome semantics for an open claim; for a
    claim already judged — refuted, unobserved, even confirmed — the prior
    verdict and its time are kept in outcome_evidence.prior_outcome /
    prior_outcome_at, so the week's refuted_kept count drops by one and the
    history does not), and superseded_by names the replacement claim when
    there is one. Returns {ok, id, prior_outcome, superseded_by} /
    {ok, already: True} when it was retracted before / {ok: False, refused,
    error} when the call is malformed / {ok: False, error} on a DB problem.
    Refuses — never raises."""
    try:
        cid = int(claim_id)
    except (TypeError, ValueError):
        return refusal("claim id must be an integer")
    if not (reason or "").strip():
        return refusal("reason required — a retraction without a reason is a "
                       "deletion")
    if superseded_by is not None:
        try:
            superseded_by = int(superseded_by)
        except (TypeError, ValueError):
            return refusal("superseded_by must be an integer claim id")
        if superseded_by == cid:
            return refusal("a claim cannot supersede itself")
    if not _db_url():
        return {"ok": False, "error": "no database"}
    if not ensure_schema():
        return {"ok": False, "error": "schema unavailable"}
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT outcome, outcome_at, superseded_by "
                "  FROM brain_predictions_log WHERE id = %s AND source_layer = %s",
                (cid, SOURCE_LAYER))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "no such claim", "id": cid}
            prior, prior_at, prior_sup = row[0], row[1], row[2]
            if prior == "retracted":
                return {"ok": True, "already": True, "id": cid,
                        "superseded_by": prior_sup}
            evidence = {"reason": reason.strip()[:2000],
                        "retracted_at": _now_iso(),
                        "prior_outcome": prior,
                        "prior_outcome_at": _iso(prior_at),
                        "superseded_by": superseded_by}
            ok = _retract_sql(cur, cid, evidence, superseded_by)
            conn.commit()
            if not ok:
                return {"ok": False, "error": "not retracted", "id": cid}
            return {"ok": True, "id": cid, "prior_outcome": prior,
                    "superseded_by": (superseded_by if superseded_by is not None
                                      else prior_sup)}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] retract %s failed: %s", claim_id, e)
        return {"ok": False, "error": str(e)[:200]}


# ── resolving an expectation against its instrument ─────────────────────

def _default_fetch(path: str) -> dict:
    """Internal GET through the envelope — a failure and an empty payload are
    distinguishable upstream; here both read as 'not observed'.

    ★ Carries X-Admin-Key when DCHUB_ADMIN_KEY is set. Without it every `get:`
    metric against an admin-gated endpoint (step 2's
    /api/v1/admin/facility-dedup/analyze, for one) 401'd on the loopback,
    read as 'not observed', and the claim judged `unobserved` forever — an
    instrument gap manufactured by our own gate. The env is read PER CALL
    (admin routes validate the live key on every request; an import-time
    snapshot would go stale on rotation), the header is merged over the
    probe's own User-Agent, and the call stays GET-only on 127.0.0.1."""
    from util.internal_fetch import data_of, probe
    key = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    headers = {"X-Admin-Key": key} if key else None
    return data_of(probe(path, 8, headers=headers))


# ── ★2026-08-29 lane 4 (second-measurer) ─────────────────────────────────
#
# The canon fix (2026-08-23) produced this loop's first genuine
# self-refutation, and the reason it worked is worth generalising: the
# expectation and the measurement stopped coming from the SAME place. Claim
# 100945 carried `pinned 1,800+ / expected == 1,900+` and was judged
# `confirmed` in production because the expectation was ALSO taken from
# resolve_canon() — actual == expected by construction. ASSERT THE PIN,
# MEASURE THE LIVE OVERRIDE.
#
# resolve_metric() is still ONE reader per scheme. A claim it confirms is
# confirmed on the word of a single instrument, and an instrument that is
# wrong the same way twice is indistinguishable from one that is right.
#
# So each scheme gets a SECOND reader on a genuinely different path — a
# different transport, a different surface, no shared helper with the first.
# Sharing a helper would make the second reading a copy of the first, which
# is the same defect one level up.
#
#   canon    1st resolve_canon() live override · 2nd the SERVED surface
#            /api/v1/canon/phrases, which is what an agent actually reads
#   finding  1st direct SQL on brain_findings  · 2nd the HTTP surface
#            /api/v1/brain/findings/db-status  (different transport entirely)
#   get      1st internal loopback envelope    · 2nd the SAME path through the
#            PUBLIC edge — catches "correct at origin, broken at the edge",
#            which is a live failure class here (edge shadow, CF route
#            timeouts), not a hypothetical
#   linkedin NO independent path. The only other reader is LinkedIn's own API
#            (external, rate-limited, credentialed). Declared unavailable
#            rather than faked — a corroboration that is really the same read
#            twice is worse than none, because it looks like agreement.
#
# THE ASYMMETRY IS DELIBERATE. A CONFIRMATION requires corroboration; a
# REFUTATION stands on one reader. Downgrading a refutation because a second
# instrument disagreed would render a failure as a non-result, which is the
# defect this entire shell exists to remove. Confirmation is the direction
# where being wrong is expensive and quiet.
CORROBORATION_UNAVAILABLE = "unavailable"


def _second_reading_canon(target, field, cur, fetch):
    """The SERVED canon surface, not the resolver. Different code path, and
    it is the value agents actually receive."""
    payload = (fetch or _default_fetch)("/api/v1/canon/phrases")
    if not isinstance(payload, dict) or not payload:
        return None, {"path": "/api/v1/canon/phrases", "status": "empty_or_failed"}
    # A canon key is a LITERAL dotted key ('facilities.count'), not a path.
    # dig() splits on dots, so it walks into a 'facilities' node that does not
    # exist and returns None — silence that would read as "no disagreement"
    # for every canon claim, i.e. a corroborator that never corroborates.
    for container in (payload, payload.get("phrases"), payload.get("canon")):
        if isinstance(container, dict) and target in container:
            val = container[target]
            return val, {"path": "/api/v1/canon/phrases", "key": target,
                         "value": _short(val)}
    val = dig(payload, target)          # nested shape, if it ever becomes one
    return val, {"path": "/api/v1/canon/phrases", "key": target,
                 "value": _short(val),
                 "status": None if val is not None else "key_not_on_surface"}


def _second_reading_finding(target, field, cur, fetch):
    """brain_findings over HTTP rather than over the cursor. A different
    transport, a different process, and it exercises the surface a consumer
    would use."""
    payload = (fetch or _default_fetch)("/api/v1/brain/findings/db-status")
    if not isinstance(payload, dict) or not payload:
        return None, {"path": "/api/v1/brain/findings/db-status",
                      "status": "empty_or_failed"}
    for row in (payload.get("recent") or []):
        if isinstance(row, dict) and row.get("url") == target:
            return row.get(field or "status"), {
                "path": "/api/v1/brain/findings/db-status", "row": _short(row)}
    # Present in the table but outside the surface's recent window is NOT a
    # disagreement — it is silence. Say so.
    return None, {"path": "/api/v1/brain/findings/db-status",
                  "status": "not_in_recent_window", "url": target}


def _second_reading_get(target, field, cur, fetch):
    """The same path through the PUBLIC edge instead of the loopback."""
    if fetch is not None:
        # An injected fetch IS the first reader; reusing it would corroborate
        # a reading with itself.
        return None, {"status": "no_independent_path_under_injected_fetch"}
    try:
        import urllib.request as _u
        base = (os.environ.get("DCHUB_PUBLIC_BASE") or "https://dchub.cloud").rstrip("/")
        url = base + target
        req = _u.Request(url, headers={
            # urllib's default UA is blocked at the edge (1010) before the
            # worker ever runs; a browser-shaped UA is required to reach it.
            "User-Agent": "Mozilla/5.0 (compatible; dchub-claim-corroborator)",
            "Cache-Control": "no-cache"})
        with _u.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return None, {"edge": target, "status": "edge_unreachable",
                      "error": str(e)[:120]}
    if not isinstance(payload, dict) or not payload:
        return None, {"edge": target, "status": "empty_or_failed"}
    val = dig(payload, field) if field else payload
    return val, {"edge": target, "field": field, "value": _short(val)}


_SECOND_READERS = {
    "canon": _second_reading_canon,
    "finding": _second_reading_finding,
    "get": _second_reading_get,
    # 'linkedin' intentionally absent — see the note above.
}


def second_reading(expected_metric: str, cur=None, fetch=None):
    """-> (actual, evidence). A reading of the SAME metric on a different
    path. `actual is None` means this reader did not measure — which is
    silence, never disagreement."""
    parsed = parse_metric(expected_metric)
    if not parsed:
        return None, {"error": "unparseable metric", "metric": expected_metric}
    scheme, target, field = parsed
    reader = _SECOND_READERS.get(scheme)
    if reader is None:
        return None, {"status": CORROBORATION_UNAVAILABLE, "scheme": scheme,
                      "why": "no independent reader exists for this scheme"}
    try:
        return reader(target, field, cur, fetch)
    except Exception as e:  # noqa: BLE001
        return None, {"status": "second_reader_failed", "error": str(e)[:140]}


def corroborate(verdict: str, expected_metric: str, expected_value: str,
                cur=None, fetch=None):
    """-> (final_verdict, corroboration_evidence).

    A confirmation that a second, independent instrument contradicts is not a
    confirmation. It is downgraded to `unobserved` — no widening of OUTCOMES,
    and honest: we do not have a measurement we can stand behind.

    A refutation is left alone. Suppressing a failure because a second reader
    disagreed would be the failure-as-benign-value bug wearing a rosette.
    """
    actual2, ev2 = second_reading(expected_metric, cur=cur, fetch=fetch)
    note = {"second_path": ev2}
    if actual2 is None:
        note["corroboration"] = CORROBORATION_UNAVAILABLE
        return verdict, note
    verdict2 = judge(actual2, expected_value)
    note["second_verdict"] = verdict2
    note["second_actual"] = _short(actual2)
    if verdict2 == verdict:
        note["corroboration"] = "agree"
        return verdict, note
    if verdict2 == "unobserved":
        note["corroboration"] = CORROBORATION_UNAVAILABLE
        return verdict, note
    note["corroboration"] = "disagree"
    if verdict == "confirmed":
        note["downgraded_from"] = "confirmed"
        note["why"] = ("a second reader on an independent path did not "
                       "confirm; a confirmation on one instrument's word is "
                       "not a confirmation")
        return "unobserved", note
    # A refutation stands, and the disagreement is on the record.
    return verdict, note


def resolve_metric(expected_metric: str, cur=None, fetch=None):
    """-> (actual, evidence). `actual is None` means the instrument did not
    measure (no row, not synced yet, endpoint failed) — never a value."""
    parsed = parse_metric(expected_metric)
    if not parsed:
        return None, {"error": "unparseable metric", "metric": expected_metric}
    scheme, target, field = parsed
    try:
        if scheme == "get":
            payload = (fetch or _default_fetch)(target)
            if not isinstance(payload, dict) or not payload:
                return None, {"endpoint": target, "status": "empty_or_failed"}
            val = dig(payload, field) if field else payload
            return val, {"endpoint": target, "field": field,
                         "value": _short(val)}
        if cur is None:
            return None, {"error": "no cursor for scheme " + scheme}
        if scheme == "finding":
            cur.execute(
                "SELECT status, count, last_seen, resolved_at FROM brain_findings "
                " WHERE url = %s ORDER BY last_seen DESC LIMIT 1", (target,))
            row = cur.fetchone()
            if not row:
                return None, {"finding": target, "status": "no_row"}
            rec = {"status": row[0], "count": row[1],
                   "last_seen": _iso(row[2]), "resolved_at": _iso(row[3])}
            return rec.get(field or "status"), {"finding": target, **rec}
        if scheme == "linkedin":
            cur.execute(
                "SELECT lp.impressions, lp.likes, lp.comments, lp.clicks, "
                "       lp.engagement_fetched_at, s.linkedin_urn "
                "  FROM social_media_posts s "
                "  LEFT JOIN linkedin_posts lp ON lp.post_urn = s.linkedin_urn "
                " WHERE s.id = %s", (int(target),))
            row = cur.fetchone()
            if not row:
                return None, {"post": target, "status": "no_row"}
            rec = {"impressions": row[0], "likes": row[1], "comments": row[2],
                   "clicks": row[3], "engagement_fetched_at": _iso(row[4]),
                   "urn": row[5]}
            if not row[5]:
                return None, {"post": target, "status": "no_urn", **rec}
            if row[4] is None:
                return None, {"post": target, "status": "not_measured_yet", **rec}
            return rec.get(field or "impressions"), {"post": target, **rec}
        if scheme == "canon":
            # ★2026-08-23. A canon claim ASSERTS the pin and MEASURES the live
            # override — the expectation is `== <pin>` (register_canon_claims)
            # and the instrument is resolve_canon(). It used to be the other
            # way round, with the expectation ALSO taken from resolve_canon(),
            # so actual == expected by construction: claim 100945 carried
            # `pinned 1,800+ / expected == 1,900+` and was judged `confirmed`
            # in production (2026-08-23T04:10Z).
            #
            # resolve_canon() is fail-soft — on a DB error the PINNED literal
            # stands (see ai_surface_canon.canon_is_live) — so an unwitnessed
            # value would confirm the pin against itself. That is an instrument
            # gap: it reads UNOBSERVED, which the verifier defers inside grace
            # and never turns into a verdict.
            from ai_surface_canon import PINNED, canon_is_live, resolve_canon
            c = resolve_canon()
            pin = dig(PINNED, target)
            if not canon_is_live(c, target):
                return None, {"canon": target, "pinned": _short(pin),
                              "status": "resolver_fell_back_to_pin",
                              "measures": "resolve_canon() live override"}
            val = dig(c, target)
            return val, {"canon": target, "value": _short(val),
                         "pinned": _short(pin),
                         "measures": "resolve_canon() live override"}
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"{type(e).__name__}: {str(e)[:160]}",
                      "metric": expected_metric}
    return None, {"error": "unknown metric scheme", "metric": expected_metric}


# ── the verifier (runs from the L16 cron tick) ──────────────────────────

_DUE_SQL = (
    "SELECT id, kind, subject, expected_metric, expected_value, horizon_hours, "
    "       shipped_at, NOW() "
    "  FROM brain_predictions_log "
    " WHERE source_layer = %s AND shipped_at IS NOT NULL AND outcome IS NULL "
    "   AND shipped_at + (horizon_hours * INTERVAL '1 hour') < NOW() "
    " ORDER BY shipped_at ASC LIMIT %s")


def verify_due_claims(limit: int = 25, fetch=None) -> dict:
    """Judge every DUE claim (shipped, past horizon, no outcome) against the
    expectation its producer pre-registered, and stamp the outcome.

    confirmed / refuted are stamped at once. unobserved (the instrument has
    not measured) is DEFERRED until GRACE_MULTIPLIER × horizon has passed, then
    stamped as unobserved — a gap stays a gap, never becomes a verdict."""
    out = {"ok": True, "due": 0, "stamped": 0, "deferred": 0,
           "outcomes": {}, "results": []}
    if not _db_url():
        out.update(ok=False, error="no database")
        return out
    if not ensure_schema():
        out.update(ok=False, error="schema unavailable")
        return out
    try:
        conn = _conn()
        try:
            # autocommit: each resolver SELECT and each stamp is its own
            # transaction, so one failing instrument cannot abort the rest.
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(_DUE_SQL, (SOURCE_LAYER, int(limit)))
            rows = cur.fetchall()
            out["due"] = len(rows)
            for r in rows:
                cid, kind, subject, metric, expected, horizon, shipped_at, now = r
                actual, evidence = resolve_metric(metric, cur=cur, fetch=fetch)
                verdict = judge(actual, expected)
                # ★lane 4: a confirmation needs a second, independent reader.
                verdict, corro = corroborate(verdict, metric, expected,
                                             cur=cur, fetch=fetch)
                evidence = dict(evidence or {})
                evidence.update(corro)
                res = {"id": cid, "kind": kind, "subject": subject,
                       "metric": metric, "expected": expected,
                       "actual": _short(actual), "outcome": verdict,
                       "corroboration": corro.get("corroboration"),
                       "stamped": False}
                if verdict == "unobserved":
                    grace_end = shipped_at + _dt.timedelta(
                        hours=int(horizon or 0) * GRACE_MULTIPLIER)
                    if now < grace_end:
                        out["deferred"] += 1
                        res["deferred_until"] = _iso(grace_end)
                        out["results"].append(res)
                        continue
                stamped = _stamp_outcome_sql(
                    cur, cid, verdict,
                    {"judged_at": _iso(now), "actual": actual,
                     "expected": expected, "evidence": evidence})
                res["stamped"] = stamped
                if stamped:
                    out["stamped"] += 1
                    out["outcomes"][verdict] = out["outcomes"].get(verdict, 0) + 1
                out["results"].append(res)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] verify_due_claims failed: %s", e)
        out.update(ok=False, error=str(e)[:200])
    return out


def list_claims(limit: int = 25, kind: str | None = None,
                outcome: str | None = None) -> list:
    """Newest first. outcome='open' selects rows with no outcome yet."""
    if not _db_url() or not ensure_schema():
        return []
    where = ["source_layer = %s"]
    params: list = [SOURCE_LAYER]
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if outcome == "open":
        where.append("outcome IS NULL")
    elif outcome:
        where.append("outcome = %s")
        params.append(outcome)
    params.append(int(limit))
    rows = []
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, predicted_at, kind, subject, statement, regime, "
                "       surfaces, expected_metric, expected_value, horizon_hours, "
                "       shipped_at, outcome, outcome_evidence, outcome_at, "
                "       shipped_at + (horizon_hours * INTERVAL '1 hour'), "
                "       superseded_by "
                "  FROM brain_predictions_log WHERE " + " AND ".join(where) +
                " ORDER BY predicted_at DESC LIMIT %s", params)
            for r in cur.fetchall():
                regime = r[5]
                if isinstance(regime, str):
                    try:
                        regime = json.loads(regime)
                    except ValueError:
                        pass
                rows.append({
                    "id": r[0], "registered_at": _iso(r[1]), "kind": r[2],
                    "subject": r[3], "statement": r[4], "regime": regime,
                    "surfaces": list(r[6] or []), "expected_metric": r[7],
                    "expected_value": r[8], "horizon_hours": r[9],
                    "shipped_at": _iso(r[10]), "due_at": _iso(r[14]),
                    "outcome": r[11], "outcome_evidence": r[12],
                    "outcome_at": _iso(r[13]),
                    "superseded_by": r[15] if len(r) > 15 else None,
                })
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] list failed: %s", e)
    return rows


# ── producers ───────────────────────────────────────────────────────────
# Each producer signs the same contract; the helpers exist so a call site is
# one line and the expectation it registers is visible (and testable) here.

# ★★★ THE LINKEDIN POST CLAIM IS RETIRED (2026-08-25) — do not re-add it.
# register_linkedin_post_claim / linkedin_expectation /
# linkedin_baseline_impressions lived here and pre-registered an IMPRESSIONS
# expectation, bar = floor(0.5 x 30d avg), before every auto-published share.
# Measured against all 141 posts of the trailing 45 days it graded the FEED,
# not the post:
#
#   * impressions are power-law — median 12, mean 34.4, max 670 — so the mean
#     sits at the 80th percentile and the bar (17) REFUTED 61% of posts. The
#     earlier reading that it "cannot fail" compared the bar to per-KIND
#     AVERAGES, but a claim is registered and graded per POST, and every kind
#     clearing its own average says nothing about the posts inside it;
#   * raising the fraction makes it worse, not falsifiable: 0.7x refutes 72%,
#     1.0x refutes 80%;
#   * it graded the metric ANTI-CORRELATED with the bandit's objective
#     (impressions vs eng_rate, Pearson r = -0.16 across the nine kinds), so a
#     post could confirm its claim while being exactly what the bandit avoids;
#   * refuted claims are recalled by brain_rag -> brain_lane_driver /
#     brain_strategic_planner, so the noise was delivered to the brain as if it
#     were a quality signal — and to the wrong consumer besides: the composer
#     never read claims at all;
#   * the desk earns 0.5-3.0 interactions per post, and the writer cannot move
#     impressions in any case.
#
# In three registrations the bar never produced a single verdict — all three
# claims were still open when it was retired.
#
# The grade that replaced it runs AFTER publication and reaches the writer:
# routes/media_published_review.py (#3178) reviews the published TEXT against
# ANALYST_VOICE and feeds the misses into the next composition.
#
# "post" stays in KINDS so the three historical rows remain readable; what is
# gone is the PRODUCER. Guarded by test_the_post_claim_producer_stays_retired.


def register_finding_claim(finding_key: str, title: str, queue_id,
                           count=None):
    """Squasher producer. The radar's findings carry no red_when, so the
    inverse of the finding is the canonical writer marking it resolved
    (brain_findings.status='resolved' once a sweep stops seeing it). Shipped
    at enqueue: the clock runs from the moment the loop took the finding on,
    so a row nobody acts on is REFUTED at 7d — the squasher's 'no fix landed'
    verdict, in claim form."""
    res = register_claim(
        kind="fix",
        subject=f"finding:{finding_key}",
        statement=(f"{title or finding_key} — resolved within "
                   f"{FINDING_HORIZON_HOURS}h of squasher queue #{queue_id}"),
        expected_metric=f"finding:{finding_key} status",
        expected_value="== resolved",
        horizon_hours=FINDING_HORIZON_HOURS,
        regime={
            "as_of": _now_iso(),
            "issue": title,
            "queue_id": queue_id,
            "count_at_enqueue": count,
            "instrument": "brain_findings.status (radar canonical writer)",
        },
        surfaces=["/api/v1/brain/squasher", "/api/v1/brain/consistency-radar"],
        shipped=True,
    )
    return res.get("id") if res.get("ok") else None


# ── ★2026-08-29 lane 7 (squasher-remit): the lesson, read back ───────────
#
# Every enqueue pre-registers a `fix` claim, and at 7d the L16 tick judges it:
# REFUTED means the finding was NOT resolved --- the loop took it on and no fix
# landed. Those refutations already flow into claim_lessons, one of the
# NEGATIVE_LESSON_CORPORA, so they come back as RECALL on a decision.
#
# But the squasher's own dedup never asked. It deduped on IDENTITY (one open
# row per finding_key) and on budget, and neither knows that this exact finding
# has been taken on and failed before. So a finding whose fix does not hold is
# re-enqueued on the next sweep, burns another ~80s model call, and is refuted
# again. That is what a recurrence rate of 0.687 with closed_with_pr=0 looks
# like from the inside: a loop that remembers in a corpus it does not consult.
def refuted_fix_attempts(finding_key: str, cur=None) -> dict:
    """How many times a `fix` claim for THIS finding has been refuted.

    -> {"known": bool, "refuted": int, "last": {...}|None}

    `known` is False when the ledger cannot be read. A caller must not treat
    that as "no prior failures" --- an unread history is not an empty one, and
    conflating them is the failure this whole shell removes.
    """
    out = {"known": False, "refuted": 0, "last": None}
    sql = ("SELECT outcome_at, outcome_evidence, horizon_hours "
           "  FROM brain_predictions_log "
           " WHERE source_layer = %s AND kind = %s AND subject = %s "
           "   AND outcome = %s "
           " ORDER BY outcome_at DESC NULLS LAST LIMIT 25")
    params = (SOURCE_LAYER, "fix", "finding:%s" % (finding_key or ""), "refuted")
    try:
        if cur is not None:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
        else:
            if not _db_url() or not ensure_schema():
                return out
            conn = _conn()
            try:
                c2 = conn.cursor()
                c2.execute(sql, params)
                rows = c2.fetchall() or []
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] refuted_fix_attempts failed: %s", e)
        return out
    out["known"] = True
    out["refuted"] = len(rows)
    if rows:
        out["last"] = {"at": _iso(rows[0][0]),
                       "evidence": _short(rows[0][1]),
                       "horizon_hours": rows[0][2]}
    return out


# ── tool_copy: the per-platform description tuner, made accountable ──────
#
# routes/ai_platform_tool_tuner rewrites a tool's description PER PLATFORM with
# Claude and upserts it into mcp_tool_descriptions_per_platform; the MCP server
# picks the row up within 30 min (_refreshPlatformDescriptions) and serves it in
# tools/list. It has read a 30d adoption signal since 2026-06-26 — but only as
# PROMPT INPUT. Nothing ever judged a rewrite after it shipped, and the table
# keeps no history, so a rewrite that REDUCED adoption was invisible and
# permanent. That is the open loop this closes.
#
# ★ WHAT THIS CLAIM DOES AND DOES NOT TEST. It is a GUARD-RAIL claim: "this
#   rewrite will not reduce adoption". It is deliberately NOT "adoption will
#   rise" — most (platform, tool) cells carry <20 calls/14d, where a rise bar
#   would be judged by noise. Refutation therefore means a REAL decline, and it
#   is what arms the revert. A `confirmed` here is the absence of harm, never
#   evidence the copy helped; do not quote it as a win.
#
# ★ THE WINDOW IS THE HORIZON, on purpose. The baseline is the trailing
#   TOOL_COPY_WINDOW_DAYS BEFORE the rewrite and the verdict reads the trailing
#   TOOL_COPY_WINDOW_DAYS at the horizon — so the two windows do not overlap and
#   the comparison is post-change vs pre-change. Reading a 30d window at a 14d
#   horizon would leave 16 days of pre-rewrite traffic inside the "after" number
#   and bias every claim toward confirmed.
TOOL_COPY_WINDOW_DAYS = 14
TOOL_COPY_HORIZON_HOURS = TOOL_COPY_WINDOW_DAYS * 24
# Refuted only on a real fall. floor(0.6 x baseline) mirrors the LinkedIn
# producer's generous bar: a cell that merely wobbles must not trigger a revert.
TOOL_COPY_BASELINE_FRACTION = 0.6
TOOL_COPY_ADOPTION_PATH = "/api/v1/admin/mcp/tool-tuner/adoption"


def tool_copy_expectation(baseline) -> int:
    """The bar a rewrite must clear at the horizon. An unreadable baseline is
    NOT zero — it becomes 0, which `>= 0` always satisfies, so the claim reads
    confirmed-by-construction. Refuse that: return -1 and let the registrar
    skip. (This is the [[canon]] green-by-construction trap, one domain over.)"""
    n = _num(baseline)
    if n is None or n < 0:
        return -1
    return max(0, int(math.floor(TOOL_COPY_BASELINE_FRACTION * n)))


def _open_tool_copy_claim(platform: str, tool: str) -> bool:
    """Is this (platform, tool) already under test? Fail-soft: an unreadable
    ledger returns True — REFUSING to open a second bet is the safe direction,
    because the failure mode of a false negative is two claims reverting each
    other's copy."""
    if not _db_url():
        return True
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM brain_predictions_log "
                " WHERE source_layer = %s AND kind = %s AND subject = %s "
                "   AND outcome IS NULL LIMIT 1",
                (SOURCE_LAYER, "tool_copy", f"tool_copy:{platform}:{tool}"))
            return cur.fetchone() is not None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] open tool_copy probe failed for "
                       "%s/%s: %s", platform, tool, e)
        return True


def register_tool_copy_claim(platform: str, tool: str, new_description: str,
                             prior_description=None, baseline_calls=None):
    """Tuner producer. Called by ai_platform_tool_tuner._upsert AFTER the row
    is written — the upsert IS the ship, so shipped=True and the clock starts
    at the write.

    `prior_description` rides in the regime because the override table carries
    UNIQUE(platform, tool_name) and overwrites in place: the claim is the ONLY
    record of what to restore, and revert_refuted_tool_copy reads it back from
    there. A claim registered without one is still judged, but names itself
    NOT revertible rather than pretending.

    Fail-soft to None: the tuner must never die with the ledger."""
    bar = tool_copy_expectation(baseline_calls)
    if bar < 0:
        return None
    # ★ ONE BET IN FLIGHT PER CELL. register_claim already dedupes an IDENTICAL
    # claim, but a SECOND rewrite of the same (platform, tool) inside the
    # horizon opens a second, different claim — and then a refuted first claim
    # reverts to v1, clobbering the v3 the second claim is still measuring.
    # Changing the treatment mid-measurement invalidates both. Skip instead:
    # the cell is already under test, and the tuner keeps the copy it has.
    if _open_tool_copy_claim(platform, tool):
        return None
    res = register_claim(
        kind="tool_copy",
        subject=f"tool_copy:{platform}:{tool}",
        statement=(new_description or "")[:4000],
        expected_metric=(f"get:{TOOL_COPY_ADOPTION_PATH}?platform={platform}"
                         f"&tool={tool}&days={TOOL_COPY_WINDOW_DAYS} calls"),
        expected_value=f">= {bar}",
        horizon_hours=TOOL_COPY_HORIZON_HOURS,
        regime={
            "as_of": _now_iso(),
            "platform": platform,
            "tool": tool,
            "baseline_calls": baseline_calls,
            "window_days": TOOL_COPY_WINDOW_DAYS,
            "rule": (f"calls over the {TOOL_COPY_WINDOW_DAYS}d AFTER the rewrite "
                     f">= floor({TOOL_COPY_BASELINE_FRACTION} x the "
                     f"{TOOL_COPY_WINDOW_DAYS}d BEFORE it) at "
                     f"{TOOL_COPY_HORIZON_HOURS}h"),
            "tests": ("that the rewrite did NOT reduce adoption. NOT that it "
                      "raised it — most cells are too small to judge a rise."),
            "instrument": ("mcp_tool_calls grouped by (platform, tool_name) — "
                           "the same read the tuner tunes on "
                           "(ai_platform_tool_tuner._outcome_signal)"),
            "prior_description": prior_description,
            "revertible": bool(prior_description),
        },
        surfaces=[f"mcp:tools/list:{platform}"],
        shipped=True,
    )
    return res.get("id") if res.get("ok") else None


def revert_refuted_tool_copy(limit: int = 5, upsert=None) -> dict:
    """Restore the prior description for every REFUTED tool_copy claim that
    still carries one, then mark the claim reverted in its own regime so a
    second pass cannot re-apply it.

    The write goes through the tuner's own `_upsert` (injectable for tests) so
    there is ONE writer for this table and the version counter keeps counting.
    Fail-soft per row: one unrevertible claim never blocks the rest."""
    out = {"ok": True, "considered": 0, "reverted": 0, "skipped": [],
           "results": []}
    if not _db_url() or not ensure_schema():
        out.update(ok=False, error="ledger unavailable")
        return out
    if upsert is None:
        try:
            from routes.ai_platform_tool_tuner import _upsert as upsert
        except Exception as e:  # noqa: BLE001
            out.update(ok=False, error=f"tuner unavailable: {type(e).__name__}")
            return out
    try:
        conn = _conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, regime FROM brain_predictions_log "
                " WHERE source_layer = %s AND kind = %s AND outcome = %s "
                " ORDER BY outcome_at DESC NULLS LAST LIMIT %s",
                (SOURCE_LAYER, "tool_copy", "refuted", int(limit)))
            rows = cur.fetchall()
            out["considered"] = len(rows)
            for cid, regime in rows:
                r = regime if isinstance(regime, dict) else {}
                try:
                    if not isinstance(regime, dict) and regime:
                        r = json.loads(regime)
                except Exception:  # noqa: BLE001
                    r = {}
                prior = r.get("prior_description")
                plat, tool = r.get("platform"), r.get("tool")
                if r.get("reverted_at"):
                    out["skipped"].append({"id": cid, "why": "already reverted"})
                    continue
                if not (prior and plat and tool):
                    out["skipped"].append({"id": cid, "why": "no prior_description"})
                    continue
                try:
                    upsert(conn, plat, tool, prior, "claim_ledger:revert")
                except Exception as e:  # noqa: BLE001
                    out["skipped"].append({"id": cid,
                                           "why": f"upsert failed: {type(e).__name__}"})
                    continue
                r["reverted_at"] = _now_iso()
                cur.execute(
                    "UPDATE brain_predictions_log SET regime = %s WHERE id = %s",
                    (json.dumps(r, default=str), cid))
                out["reverted"] += 1
                out["results"].append({"id": cid, "platform": plat, "tool": tool})
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[claim_ledger] revert_refuted_tool_copy failed: %s", e)
        out.update(ok=False, error=str(e)[:200])
    return out


_CANON_PUBLIC_KEYS = ("facilities", "deals", "markets", "countries")


def canon_claim_pairs(pinned: dict, resolved: dict) -> list:
    """(key, pinned statement, resolver value) for every headline number."""
    pairs = []
    ppub = (pinned or {}).get("public") or {}
    rpub = (resolved or {}).get("public") or {}
    for k in _CANON_PUBLIC_KEYS:
        if ppub.get(k) is not None and rpub.get(k) is not None:
            pairs.append((f"public.{k}", str(ppub[k]), str(rpub[k])))
    pt = (pinned or {}).get("tools_advertised")
    rt = (resolved or {}).get("tools_advertised")
    if pt is not None and rt is not None:
        pairs.append(("tools_advertised", str(pt), str(rt)))
    return pairs


def _canon_resolver_was_live(resolved: dict, key: str) -> bool:
    """ai_surface_canon.canon_is_live, fail-CLOSED, for the regime block.

    resolve_canon() falls back to the pin on any resolver error, so on that
    path `resolver_value` below is just the pin echoed back. A reader of a
    refuted claim has to be able to tell a real disagreement from a resolver
    that could not look, and a regime that cannot say so is the same kind of
    silently-agreeing instrument this producer exists to catch."""
    try:
        from ai_surface_canon import canon_is_live
        return bool(canon_is_live(resolved, key))
    except Exception:  # noqa: BLE001
        return False


def register_canon_claims(pinned: dict, resolved: dict) -> int:
    """Canon producer. Called from ai_surface_canon.resolve_canon(). Each
    PINNED headline number is registered as a claim that ASSERTS THE PIN —
    `expected_value` is `== <pin>` and the instrument is the live
    resolve_canon() override (horizon 24h) — so a pin that lags what the
    sources say is REFUTED on the ledger instead of found by hand. Memoised
    per process per (subject, pinned value); the DB dedup covers the other
    replica. Returns how many NEW claims this call registered.

    ★2026-08-23 — THE DIRECTION IS THE WHOLE POINT, AND IT WAS BACKWARDS.
    This used to register `expected_value = "== <resolver value>"` while
    resolve_metric("canon:…") resolved the actual through resolve_canon() as
    well, so actual == expected for every canon key by construction. Claim
    100945 shipped carrying the exact disagreement it was created to catch —
    `pinned 1,800+`, `expected == 1,900+` — and the verifier still returned
    `confirmed` (live ledger, judged 2026-08-23T04:10:09Z); the other four
    canon claims read confirmed only because their pin and the live value
    happened to agree the moment they were registered. Expecting the
    resolver's own value back from the resolver measures resolver volatility,
    never pin lag.

    Assert the PIN instead. It is what the claim's `statement` already is, it
    is what /AGENTS.md serves straight out of PINNED without ever calling the
    resolver, and it is the one side of the comparison that is not re-read
    from the instrument being measured."""
    n = 0
    for key, pin, live in canon_claim_pairs(pinned, resolved):
        subject = f"canon:{key}"
        if _CANON_MEMO.get(subject) == pin:
            continue
        res = register_claim(
            kind="canon", subject=subject, statement=pin,
            expected_metric=f"canon:{key}", expected_value=f"== {pin}",
            horizon_hours=CANON_HORIZON_HOURS,
            regime={"as_of": _now_iso(),
                    "basis": ("the PINNED floor, asserted and measured "
                              "against the resolve_canon() live override"),
                    "asserted": pin,
                    "measures": f"canon:{key} (resolve_canon live)",
                    "resolver_value": live,
                    "resolver_live_at_registration":
                        _canon_resolver_was_live(resolved, key)},
            surfaces=["/api/v1/canon/phrases", "/llms.txt",
                      "/.well-known/mcp.json", "/agent"],
            shipped=True)
        if res.get("ok"):
            _CANON_MEMO[subject] = pin
            if not res.get("already"):
                n += 1
    return n


# ── routes (admin; fail-closed via internal_auth) ───────────────────────

def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


def _authed() -> bool:
    from internal_auth import require_internal_or_admin
    return require_internal_or_admin(request)


def _limit(default: int = 25, cap: int = 200) -> int:
    try:
        return max(1, min(cap, int(request.args.get("limit", default))))
    except (TypeError, ValueError):
        return default


@claim_ledger_bp.route("/api/v1/brain/claims", methods=["GET", "POST"])
def claims():
    """GET lists; POST registers. One view per path — the regression lint's
    duplicate-route rule counts decorators, and both verbs are gated."""
    if not _authed():
        return _no_store(jsonify(ok=False, error="unauthorized")), 401
    if request.method == "GET":
        rows = list_claims(_limit(), request.args.get("kind") or None,
                           request.args.get("outcome") or None)
        return _no_store(jsonify(ok=True, count=len(rows), claims=rows,
                                 shape=SHAPE, generated_at=_now_iso()))
    b = request.get_json(silent=True) or {}
    res = register_claim(
        kind=b.get("kind"), subject=b.get("subject"),
        statement=b.get("statement"), expected_metric=b.get("expected_metric"),
        expected_value=b.get("expected_value"),
        horizon_hours=b.get("horizon_hours", 24), regime=b.get("regime"),
        surfaces=b.get("surfaces"), shipped=bool(b.get("shipped")))
    code = 200 if res.get("ok") else (422 if res.get("refused") else 503)
    return _no_store(jsonify(res)), code


@claim_ledger_bp.route("/api/v1/brain/claims/verify", methods=["POST", "GET"])
def claims_verify():
    if not _authed():
        return _no_store(jsonify(ok=False, error="unauthorized")), 401
    out = verify_due_claims(limit=_limit())
    # AUTO-REVERT. A refuted tool_copy claim means the rewrite it pre-registered
    # measurably REDUCED that tool's adoption; restoring the prior description
    # is the whole point of having registered it. Runs AFTER the judging in the
    # same tick, so a claim refuted here is reverted here. No-op (considered:0)
    # while tool_copy claims are dark, and fail-soft — a revert problem must not
    # take down the verification report that names it.
    try:
        out["tool_copy_reverts"] = revert_refuted_tool_copy()
    except Exception as e:  # noqa: BLE001
        out["tool_copy_reverts"] = {"ok": False, "error": str(e)[:200]}
    return _no_store(jsonify(out))


@claim_ledger_bp.route("/api/v1/brain/claims/retract", methods=["POST"])
def claims_retract():
    """Owner retraction. Body: {id, reason, superseded_by?}. The public feed
    (/api/v1/ops/claims) and get_changes carry the retraction on the next
    read — that is the out-in check for step 5."""
    if not _authed():
        return _no_store(jsonify(ok=False, error="unauthorized")), 401
    b = request.get_json(silent=True) or {}
    res = retract(b.get("id"), b.get("reason"), b.get("superseded_by"))
    if res.get("ok"):
        code = 200
    elif res.get("refused"):
        code = 422
    elif res.get("error") == "no such claim":
        code = 404
    else:
        code = 503
    return _no_store(jsonify(res)), code


def register_claim_ledger(app) -> bool:
    app.register_blueprint(claim_ledger_bp)
    return True
