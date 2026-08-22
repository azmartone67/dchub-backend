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
      canon:<dotted.key>                  ai_surface_canon.resolve_canon()
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

KINDS = ("fact", "score", "tool_answer", "post", "listing", "canon", "fix")
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
)

# A due claim whose instrument has not measured yet stays open until this
# many horizons have passed; then it is stamped `unobserved`.
GRACE_MULTIPLIER = 2

# Producer defaults — named here so a test can assert the contract each
# producer signs, not the number it happened to pick.
LINKEDIN_HORIZON_HOURS = 72          # ≥ 2 daily engagement syncs (14:40Z)
LINKEDIN_BASELINE_FRACTION = 0.5     # bar = floor(0.5 × 30d avg impressions)
FINDING_HORIZON_HOURS = 168          # the squasher's own "no fix in 7d" window
CANON_HORIZON_HOURS = 24

SHAPE = {
    "claim": ("{id, registered_at, kind, subject, statement, regime{as_of,…}, "
              "surfaces[], expected_metric, expected_value, horizon_hours, "
              "shipped_at, due_at, outcome, outcome_evidence, outcome_at}"),
    "kinds": KINDS,
    "outcomes": OUTCOMES,
    "rule": ("register BEFORE ship with an expectation or be refused; outcome "
             "is stamped at horizon by the L16 cron (outcome writer ≠ author); "
             "unobserved = instrument gap, never a refutation"),
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
    """Walk a dict/list by dotted path ('public.facilities', 'rows.0.n')."""
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


# ── resolving an expectation against its instrument ─────────────────────

def _default_fetch(path: str) -> dict:
    """Internal GET through the envelope — a failure and an empty payload are
    distinguishable upstream; here both read as 'not observed'."""
    from util.internal_fetch import data_of, probe
    return data_of(probe(path, 8))


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
            from ai_surface_canon import resolve_canon
            c = resolve_canon()
            val = dig(c, target)
            return val, {"canon": target, "value": _short(val)}
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
                res = {"id": cid, "kind": kind, "subject": subject,
                       "metric": metric, "expected": expected,
                       "actual": _short(actual), "outcome": verdict,
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
                "       shipped_at + (horizon_hours * INTERVAL '1 hour') "
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

def linkedin_baseline_impressions(cur):
    """30-day average impressions across measured LinkedIn posts — the same
    query routes/dchub_media_accelerator._compute_baseline uses. None when
    the instrument has nothing to say (no measured posts, table missing)."""
    try:
        cur.execute(
            "SELECT AVG(impressions)::float FROM linkedin_posts "
            " WHERE posted_at > NOW() - INTERVAL '30 days' "
            "   AND impressions IS NOT NULL AND impressions > 0")
        row = cur.fetchone()
        if not row:
            return None
        v = row[0] if not hasattr(row, "get") else row.get("avg")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def linkedin_expectation(baseline) -> int:
    """The bar a post is pre-registered against: impressions >= max(1,
    floor(LINKEDIN_BASELINE_FRACTION × 30d avg)). No baseline -> 1 (the post
    must at least be measured as seen)."""
    if baseline is None or baseline <= 0:
        return 1
    return max(1, int(math.floor(float(baseline) * LINKEDIN_BASELINE_FRACTION)))


def register_linkedin_post_claim(post_id, content_text, article_url=None):
    """Media producer. Called by content_publisher BEFORE _post_to_linkedin;
    the caller stamps shipped on a successful share. Uses the ledger's OWN
    connection for the baseline read — never the publisher's cursor, so a
    failing read cannot abort the publisher's transaction after the share went
    out (that would un-mark a published post and re-publish it)."""
    baseline = None
    if _db_url():
        try:
            conn = _conn()
            try:
                baseline = linkedin_baseline_impressions(conn.cursor())
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            baseline = None
    bar = linkedin_expectation(baseline)
    res = register_claim(
        kind="post",
        subject=f"social_media_posts:{post_id}",
        statement=(content_text or "")[:4000],
        expected_metric=f"linkedin:{post_id} impressions",
        expected_value=f">= {bar}",
        horizon_hours=LINKEDIN_HORIZON_HOURS,
        regime={
            "as_of": _now_iso(),
            "baseline_30d_avg_impressions": baseline,
            "rule": (f"impressions >= max(1, floor({LINKEDIN_BASELINE_FRACTION} "
                     f"x 30d avg)) at {LINKEDIN_HORIZON_HOURS}h"),
            "instrument": ("linkedin_posts.impressions via "
                           "linkedin-engagement-sync (daily 14:40Z)"),
            "article_url": article_url,
        },
        surfaces=["linkedin"],
        shipped=False,
    )
    return res.get("id") if res.get("ok") else None


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


def register_canon_claims(pinned: dict, resolved: dict) -> int:
    """Canon producer. Called from ai_surface_canon.resolve_canon(). Each
    PINNED headline number is registered as a claim whose expectation is the
    resolver's live value (horizon 24h) — a pin that lags the resolver is then
    REFUTED on the ledger instead of found by hand. Memoised per process per
    (subject, pinned value); the DB dedup covers the other replica. Returns
    how many NEW claims this call registered."""
    n = 0
    for key, pin, live in canon_claim_pairs(pinned, resolved):
        subject = f"canon:{key}"
        if _CANON_MEMO.get(subject) == pin:
            continue
        res = register_claim(
            kind="canon", subject=subject, statement=pin,
            expected_metric=f"canon:{key}", expected_value=f"== {live}",
            horizon_hours=CANON_HORIZON_HOURS,
            regime={"as_of": _now_iso(),
                    "basis": "PINNED floor vs resolve_canon() live override",
                    "resolver_value": live},
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
    return _no_store(jsonify(verify_due_claims(limit=_limit())))


def register_claim_ledger(app) -> bool:
    app.register_blueprint(claim_ledger_bp)
    return True
