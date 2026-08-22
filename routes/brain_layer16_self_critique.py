"""
Brain L16 — Self-Critique (2026-05-19).

The feedback loop the brain has been missing. L14 produces causal chains
with confidence labels (high/medium/low). L15 opens GitHub issues for
high-confidence ones. L8 emits action plans. But until now, NOTHING
checked whether those predictions came true.

L16 closes that loop:

  1. Reads every L14 chain L15 acted on, every L8 action, every
     L7 detector proposal in the last 7 days.
  2. Pulls the "verification" criterion from each (the curl that
     should flip, the metric that should move, the row count that
     should change).
  3. Re-evaluates them now and records the outcome.
  4. Calibrates confidence: if "high confidence" chains have been
     wrong 30% of the time, that's a calibration error worth knowing.
  5. Exposes the calibration data so L14's next prompt reads it
     ("Your last 10 high-confidence chains were correct 7/10 times;
     adjust your priors accordingly.")

This is the difference between a brain that lists symptoms and a
brain that *knows what it's good and bad at*.

Schema:
  brain_predictions_log (id, predicted_at, source_layer,
                         chain_title, confidence,
                         verification_criterion, prediction,
                         verified_at, actual_outcome, was_correct,
                         calibration_bucket)

Endpoints:
  GET  /api/v1/brain/self-critique           — recent verifications
  GET  /api/v1/brain/self-critique/calibration — confidence-vs-correctness table
  POST /api/v1/brain/self-critique/run       — admin: re-verify pending

Cron: every 6h at :35, after L8 (:45 of previous cycle) and L14/L15.
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request
from utils.anthropic_helper import anthropic_messages_url
from routes.brain_llm_spend import instrumented_post as _llm_post

logger = logging.getLogger(__name__)
brain_layer16_bp = Blueprint("brain_layer16", __name__)

_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
# 2026-08-15: the module-level _ADMIN_KEY snapshot is GONE on purpose.
# It was an import-time read, so a process that started before the env
# was set held "" forever, and the gate that tested `and _ADMIN_KEY`
# silently disabled itself. Auth now goes through
# internal_auth.require_internal_or_admin, which reads os.environ at
# request time. Do not reintroduce this name.


def _ensure_table():
    try:
        from main import get_db
        conn = get_db()
        if not conn: return False
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_predictions_log (
                    id                      SERIAL PRIMARY KEY,
                    predicted_at            TIMESTAMPTZ DEFAULT NOW(),
                    source_layer            TEXT NOT NULL,
                    chain_title             TEXT NOT NULL,
                    confidence              TEXT,
                    verification_criterion  TEXT,
                    prediction              TEXT,
                    verified_at             TIMESTAMPTZ,
                    actual_outcome          TEXT,
                    was_correct             BOOLEAN,
                    calibration_bucket      TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_layer_time "
                        "ON brain_predictions_log(source_layer, predicted_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_pending "
                        "ON brain_predictions_log(verified_at) "
                        "WHERE verified_at IS NULL")
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"L16 table create failed: {e}")
        return False


def _internal(path: str, timeout: int = 8) -> dict:
    # ★2026-08-12 — was a bare-{} swallow: a timeout, a 500 and an honest
    # empty were one indistinguishable value, and the brain spent 17 of its
    # 20 L18 lessons re-learning that. Envelope: util/internal_fetch.
    from util.internal_fetch import data_of, probe
    return data_of(probe(path, timeout))


def _capture_pending_predictions() -> int:
    """Walk L14's recent chains + L15's recent issues; record each as
    a pending prediction. Idempotent: chain_title is the dedup key."""
    captured = 0
    causal = _internal("/api/v1/brain/causal", 6)
    chains = (causal.get("analysis") or {}).get("causal_chains") or []
    if not chains:
        return 0
    try:
        from main import get_db
        conn = get_db()
        if not conn: return 0
        try:
            cur = conn.cursor()
            for c in chains:
                title = c.get("title", "")
                if not title: continue
                # Have we already logged this chain in the last 7d?
                cur.execute(
                    "SELECT 1 FROM brain_predictions_log "
                    "WHERE chain_title = %s AND source_layer = 'L14' "
                    "AND predicted_at > NOW() - INTERVAL '7 days' LIMIT 1",
                    (title,),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    "INSERT INTO brain_predictions_log "
                    "(source_layer, chain_title, confidence, "
                    " verification_criterion, prediction) "
                    "VALUES ('L14', %s, %s, %s, %s)",
                    (title,
                     c.get("confidence"),
                     (c.get("verification") or "")[:1000],
                     (c.get("root_cause_hypothesis") or "")[:1000]),
                )
                captured += 1
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"L16 capture failed: {e}")
    return captured


def _resolve_criterion(criterion_text: str) -> list:
    """r89f (2026-06-15): resolve a prediction's verification_criterion to its
    ACTUAL current value so L16 can score was_correct true/false instead of the
    null that kept calibration permanently empty. Extract the internal GET
    path(s) the criterion names and fetch them via the EXISTING _internal()
    helper (GET-only, localhost:8080, own origin, 8s). Returns a compact
    evidence list [{endpoint, status, snippet}].
    SAFETY: read-only — never POST, never an external URL; denylist any
    admin/keys/secret path. Capped at 2 GETs per prediction by the caller's
    max_to_verify budget."""
    import re as _re
    out = []
    try:
        paths = _re.findall(r'(/api/v1/[\w/\-]+|/[\w/\-]{2,})', criterion_text or '')
        # snake_case field names the criterion references — used to surface the
        # SPECIFIC value Claude needs even when it sits deep in a large response
        # (a blind prefix snippet truncates it; e.g. conversions_30d is at char
        # 1763 of the 8KB funnel JSON). Field-aware beats bigger-snippet.
        tokens = set(_re.findall(r'[a-z][a-z0-9_]{3,}', (criterion_text or '').lower()))
        seen = set()
        for p in paths:
            if len(out) >= 2:
                break
            if p in seen:
                continue
            seen.add(p)
            if any(b in p.lower() for b in ('admin', 'keys', 'secret', 'internal-key', 'cron')):
                continue
            try:
                resp = _internal(p, timeout=8)
                ev = {"endpoint": p, "status": "ok" if resp else "empty"}
                if isinstance(resp, dict):
                    hits = {k: json.dumps(resp[k], default=str)[:200]
                            for k in resp if k.lower() in tokens}
                    if hits:
                        ev["named_fields"] = hits   # the exact value(s) the criterion cited
                ev["snippet"] = json.dumps(resp, default=str)[:800]
                out.append(ev)
            except Exception as _e:
                out.append({"endpoint": p, "status": "err", "snippet": str(_e)[:120]})
    except Exception:
        pass
    return out


def _verify_due_claims() -> dict:
    """★2026-08-22 Claim Loop step 1. The same cron tick that scores L14's
    predictions judges the claim ledger's DUE rows (shipped_at + horizon <
    now, outcome NULL) against the expectation each producer PRE-REGISTERED
    (routes/claim_ledger). Outcome writer ≠ author: producers only register
    and mark shipped — the stamp happens here, from the cron. Deterministic
    comparator, no model call; `get:` metrics resolve through the same
    envelope _resolve_criterion uses (_internal). Fail-soft."""
    try:
        from routes.claim_ledger import verify_due_claims
        return verify_due_claims(limit=25, fetch=lambda p: _internal(p, 8))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"L16 claim verify failed: {e}")
        return {"ok": False, "error": str(e)[:200]}


def _verify_pending(max_to_verify: int = 10) -> dict:
    """For each pending prediction with a verifiable criterion, ask
    Claude (if available) to compare predicted vs current state and
    label was_correct + actual_outcome."""
    if not _ANTHROPIC_KEY:
        return {"verified": 0, "error": "no anthropic key"}
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return {"verified": 0, "error": "no db"}
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, chain_title, confidence, verification_criterion, "
                "       prediction, predicted_at FROM brain_predictions_log "
                "WHERE verified_at IS NULL "
                "  AND predicted_at < NOW() - INTERVAL '6 hours' "
                "  AND predicted_at > NOW() - INTERVAL '14 days' "
                "  AND verification_criterion IS NOT NULL "
                "  AND verification_criterion != '' "
                "ORDER BY predicted_at ASC LIMIT %s",
                (max_to_verify,),
            )
            pending = [dict(r) if hasattr(r, "get") else
                       {"id": r[0], "chain_title": r[1], "confidence": r[2],
                        "verification_criterion": r[3], "prediction": r[4],
                        "predicted_at": r[5]} for r in cur.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass

        if not pending:
            return {"verified": 0, "note": "no pending predictions ready"}

        # Pull current brain state for Claude to compare against (supplementary)
        ctx = {
            "freshness":  _internal("/api/v1/freshness/radar"),
            "funnel":     _internal("/api/v1/mcp/funnel"),
            "redeem":     _internal("/api/v1/redeem/funnel-stats"),
            "findings":   (_internal("/api/v1/brain/consistency-radar")
                            .get("findings") or [])[:15],
            "publisher":  _internal("/api/v1/marketing/worker-status"),
        }

        # r89f (2026-06-15): when BRAIN_VERIFY_RESOLVE_ENABLE is set, resolve each
        # prediction's verification_criterion to its LIVE value (GET-only, own
        # origin) and hand Claude PREDICTED-vs-ACTUAL pairs — so was_correct comes
        # back true/false instead of null (the null is what kept calibration empty,
        # starving L14 meta-cognition / awareness / self-model). Env unset = today's
        # exact generic-ctx path (fully reversible).
        import os as _os16
        _resolve_on = _os16.environ.get("BRAIN_VERIFY_RESOLVE_ENABLE", "") in ("1", "true", "True", "yes")
        _pending_json = [{
            "id": p["id"],
            "title": p["chain_title"],
            "confidence": p["confidence"],
            "verification": p["verification_criterion"],
            "predicted_root_cause": p["prediction"][:400],
            "predicted_at": str(p["predicted_at"]),
            **({"resolved_evidence": _resolve_criterion(p["verification_criterion"])}
               if _resolve_on else {}),
        } for p in pending]
        _resolve_instr = ("\nEACH prediction includes resolved_evidence — the LIVE value of the "
                          "endpoint(s) its verification criterion names. COMPARE predicted_root_cause "
                          "against resolved_evidence and set was_correct true/false accordingly. "
                          "Return null ONLY when resolved_evidence is genuinely inconclusive "
                          "(empty/err on every endpoint).\n" if _resolve_on else "")

        prompt = f"""You are the DC Hub Brain L16 — the Self-Critique Layer.

Your job: look at predictions the brain made 6+ hours ago, and check
whether each prediction was CORRECT, WRONG, or UNCERTAIN given the
current system state.
{_resolve_instr}
For each pending prediction, return:
  - id: the prediction id (echo it back)
  - was_correct: true | false | null  (null = can't verify yet)
  - actual_outcome: 1-2 sentence factual summary of current state
                    relevant to the prediction
  - calibration_bucket: "well-calibrated" | "over-confident" |
                        "under-confident"

Calibration rule: a "high" confidence prediction that turned out
WRONG = over-confident. A "low" confidence prediction that turned
out CORRECT = under-confident. Anything else = well-calibrated.

Pending predictions ({len(pending)}):
{json.dumps(_pending_json, indent=2, default=str)[:8000]}

Current system state:
{json.dumps(ctx, indent=2, default=str)[:4000]}

Return a JSON array (no markdown fences) of {{id, was_correct,
actual_outcome, calibration_bucket}} objects. One entry per pending
prediction. Reply with ONLY the JSON."""

        import requests
        r = _llm_post("brain_layer16_self_critique",
            anthropic_messages_url(),
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "User-Agent": "dchub-brain/1.0",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5",
                  "max_tokens": 2500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.status_code != 200:
            return {"verified": 0, "error": f"claude_{r.status_code}: {r.text[:200]}"}
        body = r.json() or {}
        text = "".join(b.get("text", "") for b in (body.get("content") or [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"): text = text[4:].lstrip("\n")
        verifications = json.loads(text)
        if not isinstance(verifications, list):
            return {"verified": 0, "error": "claude returned non-list"}

        # Write verifications back to DB
        conn = get_db()
        if not conn:
            return {"verified": 0, "error": "no db on writeback"}
        try:
            cur = conn.cursor()
            for v in verifications:
                _id = v.get("id")
                if not _id: continue
                cur.execute(
                    "UPDATE brain_predictions_log SET "
                    " verified_at = NOW(), "
                    " actual_outcome = %s, "
                    " was_correct = %s, "
                    " calibration_bucket = %s "
                    "WHERE id = %s",
                    ((v.get("actual_outcome") or "")[:1000],
                     v.get("was_correct"),
                     v.get("calibration_bucket") or "well-calibrated",
                     _id),
                )
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
        return {"verified": len(verifications), "predictions": verifications}
    except Exception as e:
        logger.warning(f"L16 verify failed: {e}")
        return {"verified": 0, "error": str(e)[:200]}


@brain_layer16_bp.route("/api/v1/brain/self-critique", methods=["GET"])
def self_critique_list():
    """Recent verifications, most recent first."""
    _ensure_table()
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return jsonify(ok=False, error="db unavailable"), 503
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT predicted_at, source_layer, chain_title, "
                "       confidence, verified_at, was_correct, "
                "       actual_outcome, calibration_bucket "
                "FROM brain_predictions_log "
                "ORDER BY COALESCE(verified_at, predicted_at) DESC LIMIT 25"
            )
            rows = []
            for r in cur.fetchall():
                if hasattr(r, "get"):
                    rows.append({
                        "predicted_at":   str(r.get("predicted_at") or ""),
                        "source":         r.get("source_layer"),
                        "title":          r.get("chain_title"),
                        "confidence":     r.get("confidence"),
                        "verified_at":    str(r.get("verified_at") or "") or None,
                        "was_correct":    r.get("was_correct"),
                        "outcome":        r.get("actual_outcome"),
                        "calibration":    r.get("calibration_bucket"),
                    })
                else:
                    rows.append({"predicted_at": str(r[0]), "source": r[1],
                                 "title": r[2], "confidence": r[3],
                                 "verified_at": str(r[4]) if r[4] else None,
                                 "was_correct": r[5], "outcome": r[6],
                                 "calibration": r[7]})
        finally:
            try: conn.close()
            except Exception: pass
        return jsonify(ok=True, count=len(rows), recent=rows)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503


@brain_layer16_bp.route("/api/v1/brain/self-critique/calibration",
                        methods=["GET"])
def calibration():
    """Confidence-vs-correctness table — the brain's track record."""
    _ensure_table()
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return jsonify(ok=False, error="db unavailable"), 503
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT confidence, "
                "       COUNT(*) AS total, "
                "       COUNT(*) FILTER (WHERE was_correct = TRUE) AS correct, "
                "       COUNT(*) FILTER (WHERE was_correct = FALSE) AS wrong, "
                "       COUNT(*) FILTER (WHERE was_correct IS NULL) AS uncertain "
                "FROM brain_predictions_log "
                "WHERE verified_at IS NOT NULL "
                "GROUP BY confidence"
            )
            buckets = {}
            for r in cur.fetchall():
                if hasattr(r, "get"):
                    conf = r.get("confidence") or "unknown"
                    total = r.get("total") or 0
                    correct = r.get("correct") or 0
                    wrong = r.get("wrong") or 0
                    uncertain = r.get("uncertain") or 0
                else:
                    conf, total, correct, wrong, uncertain = r[0] or "unknown", r[1], r[2], r[3], r[4]
                rate = round((correct / max(total - uncertain, 1)) * 100, 1) if (total - uncertain) > 0 else None
                buckets[conf] = {"total": total, "correct": correct,
                                  "wrong": wrong, "uncertain": uncertain,
                                  "correctness_rate_pct": rate}

            # Compute overall calibration verdict
            cur.execute(
                "SELECT calibration_bucket, COUNT(*) FROM brain_predictions_log "
                "WHERE calibration_bucket IS NOT NULL "
                "GROUP BY calibration_bucket"
            )
            cal_dist = {}
            for r in cur.fetchall():
                k = (r.get("calibration_bucket") if hasattr(r, "get")
                     else r[0]) or "unknown"
                v = (r.get("count") if hasattr(r, "get") else r[1]) or 0
                cal_dist[k] = v
        finally:
            try: conn.close()
            except Exception: pass

        return jsonify(
            ok=True,
            by_confidence=buckets,
            calibration_distribution=cal_dist,
            note=("Brain is self-aware. These numbers are its track record. "
                  "L14 reads them at every causal-analysis call so its "
                  "next prediction is informed by its actual hit rate."),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503


@brain_layer16_bp.route("/api/v1/brain/self-critique/run",
                        methods=["POST", "GET"])
def self_critique_run():
    # 2026-08-15: this gate used to read
    #     if request.method == "POST" and _ADMIN_KEY:
    # which failed OPEN two ways. (1) `and _ADMIN_KEY` made the whole auth
    # block VANISH on any process whose env lacks DCHUB_ADMIN_KEY — which is
    # what dchub-worker looked like on 2026-08-08 — and this route is
    # worker-delegated (main.py _WORKER_PROXY_POST_PATHS), so it was reachable
    # UNAUTHENTICATED from the public internet. (2) the gate tested for POST
    # while the route accepts GET, so GET ran this body ungated on every
    # process regardless of the env. A gate that disables itself when
    # misconfigured is not a gate. require_internal_or_admin is fail-CLOSED,
    # reads os.environ at CALL time (not an import-time snapshot) and compares
    # constant-time with _clean_key normalisation on both sides.
    from internal_auth import require_internal_or_admin
    if not require_internal_or_admin(request):
        return jsonify(error="unauthorized"), 401
    _ensure_table()
    captured = _capture_pending_predictions()
    claims = _verify_due_claims()
    verified = _verify_pending(max_to_verify=10)
    return jsonify(
        ok=True,
        captured_count=captured,
        claims=claims,
        verified=verified,
        ran_at=_dt.datetime.utcnow().isoformat() + "Z",
    )
