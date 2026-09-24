"""media_editorial_jev_shadow.py — shadow-price Jev against the editorial desk.

WHY THIS EXISTS
===============
TypeSafe's Jev is a "System One" model: you hand it state plus predefined
questions and it returns TYPED answers with probabilities — no prose. Priced at
$0.042/M input tokens with no output-token charge, it is pitched as the cheap
replacement for the small judgment calls an agent makes between pieces of real
work. media_editorial_gate is our clearest instance of exactly that shape: one
Claude call whose entire consumed output is `publish` or `draft`.

The question "should we move that decision to Jev?" cannot be answered by
reasoning about it. Two measured facts say so:

  · 60 modules in this repo POST to the Anthropic API; 21 files reference
    brain_llm_spend.instrumented_post (one of which is the module defining it).
    So roughly two thirds of our model spend is not in the ledger at all, and
    brain_llm_spend.summary() already says in words that its numbers are a
    FLOOR. A post-hoc "we saved X" would be a confident guess of precisely the
    kind that module exists to replace.
  · The incumbent call returns `reasons[]` as well as a verdict, and those
    strings are what the pending-drafts digest shows a human to explain why a
    story was held. Jev returns no prose. A swap is therefore not a drop-in and
    the agreement rate is only half the decision.

So this module BUYS THE MEASUREMENT FIRST. It asks Jev the same question the
editor was just asked, records both answers side by side, and changes nothing.

SHADOW ONLY — the safety properties, in order of importance:

  1. It runs AFTER the incumbent verdict is already computed, and its return
     value is discarded by the caller. There is no code path on which a Jev
     answer, a Jev error, or a Jev timeout can change what the gate decides.
  2. It is OFF unless BOTH JEV_SHADOW_ENABLED=1 and an API key are present.
     This module ships inert.
  3. It writes to its OWN table. media_editorial_reviews is load-bearing for
     the composer's DO-NOT-REPEAT memory (see _record_review's note about the
     Midland-Odessa story proposed 16 times in two hours) and is not touched.
  4. It never raises. record_shadow() returns a dict; on any failure it returns
     a dict describing the failure.

★ EVERY ATTEMPT WRITES A ROW, INCLUDING THE FAILURES. A shadow lane that is
armed but silently never answers produces an empty table, and an empty table
read as "no disagreements found" is the exact false-negative shape this repo
has been bitten by before (a monitor whose probe could not succeed looks
identical to the watched thing not happening). Every row carries an explicit
`outcome`, and summarize() divides agreements by ANSWERED, never by attempted —
an unanswered probe is never counted as agreement.

★ THE REQUEST SHAPE IS WRITTEN FROM DOCUMENTATION, NOT FROM A LIVE RESPONSE.
Jev is waitlist-gated early access and we hold no key, so neither the request
body nor the response parsing below has been executed against api.typesafe.ai.
That is why _parse_answer() is defensive across several plausible shapes and
why a shape mismatch records `parse_error` WITH the keys it actually saw
(`raw_keys`) rather than returning None. The first armed run is a probe of the
contract as much as of the model; read raw_keys before reading agreement.

Arming:  JEV_SHADOW_ENABLED=1  +  TYPESAFE_API_KEY (or JEV_API_KEY)
Tunables: JEV_SHADOW_MODEL (default jev-latest)
          JEV_SHADOW_TIMEOUT_S (default 6)
          JEV_SHADOW_ENDPOINT (default https://api.typesafe.ai/v1/systemone)
Read:    GET /api/v1/admin/media/jev-shadow?days=14   (admin-gated)
"""
from __future__ import annotations

import os
import json
import time
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
media_jev_shadow_bp = Blueprint("media_jev_shadow", __name__)

_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_BODY_CHARS = 3500      # mirrors _ask_editor's truncation so tokens compare
_HEADLINE_LINES = 25    # mirrors _RECENT_HEADLINES


# ── Arming ───────────────────────────────────────────────────────────────
# Two independent switches. The env flag alone does nothing without a key,
# and a key alone does nothing without the flag — so neither a stray key in
# the environment nor a flag flipped during unrelated work can start billing.

def _enabled() -> bool:
    return os.environ.get("JEV_SHADOW_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _api_key() -> str:
    return (os.environ.get("TYPESAFE_API_KEY")
            or os.environ.get("JEV_API_KEY") or "").strip()


def _endpoint() -> str:
    return (os.environ.get("JEV_SHADOW_ENDPOINT") or _DEFAULT_ENDPOINT).strip()


def _model() -> str:
    return (os.environ.get("JEV_SHADOW_MODEL") or "jev-latest").strip()


def _timeout_s() -> float:
    """Deliberately tight. The shadow call sits on the gate's request path, so
    although it cannot change the verdict it CAN slow a publish. 6s is well
    past Jev's advertised sub-second latency; anything beyond it is a fault we
    want recorded as `timeout`, not waited out."""
    try:
        return max(1.0, min(30.0, float(os.environ.get("JEV_SHADOW_TIMEOUT_S", "6"))))
    except Exception:
        return 6.0


# ── Pure comparison logic ────────────────────────────────────────────────
# These three functions are free of Flask, psycopg2 and requests ON PURPOSE:
# the CI unit-tests job installs pytest ONLY (not requirements.txt), so a test
# that imported this module would crash collection. tests/test_media_editorial_
# jev_shadow.py AST-extracts exactly these and execs them, the same pattern
# test_media_editorial_classify.py uses. Keep them importing nothing.

#: Outcomes that mean Jev actually answered. Anything else is UNMEASURED and
#: must never land in an agreement numerator or denominator.
ANSWERED_OUTCOMES = ("ok",)


def compare_verdicts(incumbent: str, shadow: str) -> str:
    """Compare the two desks' verdicts.

    Returns "agree", "disagree", or "unknown". "unknown" is returned whenever
    either side is missing or is not one of the two verdicts the gate consumes
    — it is NOT folded into "disagree", because an unparseable shadow answer is
    a fact about our request, not a fact about the model's judgement.
    """
    a = str(incumbent or "").strip().lower()
    b = str(shadow or "").strip().lower()
    valid = ("publish", "draft")
    if a not in valid or b not in valid:
        return "unknown"
    return "agree" if a == b else "disagree"


def summarize(rows) -> dict:
    """Agreement stats over shadow rows.

    ★ THE DENOMINATOR IS `answered`, NOT `attempts`. Counting an unanswered
    probe as agreement would make a totally broken lane — bad key, wrong
    request shape, API down — report 100% agreement and read as a green light
    to swap. `coverage` is returned beside the rate for the same reason
    brain_llm_spend returns coverage beside its totals: the rate is only
    meaningful against the share of attempts that produced an answer.
    """
    rows = list(rows or [])
    attempts = len(rows)
    answered = agreed = disagreed = unknown = 0
    outcomes: dict = {}
    lat = []
    for r in rows:
        outcome = str((r or {}).get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome not in ANSWERED_OUTCOMES:
            continue
        answered += 1
        ms = (r or {}).get("latency_ms")
        if isinstance(ms, (int, float)) and ms >= 0:
            lat.append(float(ms))
        cmp_ = compare_verdicts((r or {}).get("incumbent_verdict"),
                                (r or {}).get("shadow_verdict"))
        if cmp_ == "agree":
            agreed += 1
        elif cmp_ == "disagree":
            disagreed += 1
        else:
            unknown += 1

    rate = round(agreed / answered, 4) if answered else None
    lat_sorted = sorted(lat)
    median = lat_sorted[len(lat_sorted) // 2] if lat_sorted else None
    return {
        "attempts": attempts,
        "answered": answered,
        "agreed": agreed,
        "disagreed": disagreed,
        "unknown_verdict": unknown,
        "agreement_rate": rate,
        "coverage": {
            "answered": answered,
            "attempts": attempts,
            "ratio": round(answered / attempts, 4) if attempts else None,
            "note": ("agreement_rate is agreed/answered. Unanswered attempts are "
                     "excluded from BOTH sides — they are not agreement. Read "
                     "coverage.ratio before reading the rate: a low ratio means "
                     "the lane is mostly failing, whatever the rate says."),
        },
        "outcomes": outcomes,
        "median_latency_ms": median,
        "note": ("SHADOW ONLY — no row here affected a published story. "
                 if attempts else
                 "No attempts recorded. This is NOT agreement — it means the "
                 "lane never ran (unarmed, or the gate never reached Layer 2)."),
    }


def build_questions() -> dict:
    """The typed questions put to Jev.

    ONE Choice, mirroring the decision the gate actually consumes. The
    incumbent also emits a 0-10 score, but the gate only uses it as
    `score >= MEDIA_EDITORIAL_MIN_SCORE` on the way to publish/draft, so the
    verdict is the comparable unit. A Score primitive with eleven bands would
    add cost and a second thing to disagree about without changing what we
    learn.

    ★ Jev does not see the question's KEY, so "verdict" carries no meaning to
    it — every instruction has to live in `instructions` and `criteria`. The
    criteria text below is lifted from the incumbent prompt deliberately: a
    comparison against a differently-worded question would measure our
    rewording, not the model.
    """
    return {
        "verdict": {
            "type": "choice",
            "instructions": (
                "Judge one candidate press release for novelty against what "
                "was already published. A story is worth publishing only if it "
                "reports a concrete change — a verdict flip, a score move, a "
                "new deal, a new record, a new capability — that is not a "
                "re-worded repeat of a recent headline. A fresh angle on stale "
                "numbers is not a concrete change. When torn, choose draft."
            ),
            "criteria": {
                "publish": ("The candidate reports a concrete, specific change "
                            "that does not appear in the recently published "
                            "headlines."),
                "draft": ("The candidate repeats, re-words or re-angles a "
                          "recently published headline, or reports no concrete "
                          "change. This is the safe choice when uncertain."),
            },
        },
    }


def build_state(title: str, body: str, category: str, recent_lines: str) -> dict:
    """The evidence Jev judges, kept field-separated.

    Documentation is explicit that "the researcher finished" tells Jev less
    than the findings themselves, and that the original request belongs in a
    different field from the work done. Same truncations as _ask_editor so the
    input token counts are comparable between the two desks.
    """
    return {
        "recently_published_headlines": (recent_lines or "")[:8000],
        "candidate_category": (category or "")[:80],
        "candidate_title": (title or "")[:300],
        "candidate_body": (body or "")[:_BODY_CHARS],
    }


def _parse_answer(payload) -> tuple:
    """Pull (verdict, confidence, raw_keys) out of a Jev response.

    Defensive across shapes because we have never seen a live one (see the
    module docstring). Returns verdict=None when nothing recognisable is
    present — the caller then records `parse_error` WITH raw_keys, so the first
    armed run tells us the real contract instead of failing silently.
    """
    if not isinstance(payload, dict):
        return None, None, []
    raw_keys = sorted(payload.keys())[:12]
    holder = None
    for key in ("choices", "answers", "results", "questions"):
        v = payload.get(key)
        if isinstance(v, dict) and v:
            holder = v
            break
    if holder is None:
        return None, None, raw_keys
    ans = holder.get("verdict")
    if ans is None and len(holder) == 1:
        ans = list(holder.values())[0]
    if isinstance(ans, str):
        return ans, None, raw_keys
    if not isinstance(ans, dict):
        return None, None, raw_keys
    verdict = None
    for key in ("choice", "value", "answer", "selected"):
        if isinstance(ans.get(key), str):
            verdict = ans[key]
            break
    conf = ans.get("confidence")
    if not isinstance(conf, (int, float)):
        conf = None
    return verdict, conf, raw_keys


# ── Persistence ──────────────────────────────────────────────────────────

def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=6)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_editorial_jev_shadow (
    id                 BIGSERIAL PRIMARY KEY,
    press_slug         TEXT NOT NULL,
    category           TEXT,
    incumbent_verdict  TEXT,
    incumbent_score    REAL,
    incumbent_model    TEXT,
    shadow_verdict     TEXT,
    shadow_confidence  REAL,
    shadow_model       TEXT,
    outcome            TEXT NOT NULL,
    latency_ms         INTEGER,
    input_tokens       INTEGER,
    raw_keys           JSONB DEFAULT '[]'::jsonb,
    detail             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mejs_created ON media_editorial_jev_shadow(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_mejs_outcome ON media_editorial_jev_shadow(outcome);
"""


def _record(row: dict) -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute("""
                INSERT INTO media_editorial_jev_shadow
                    (press_slug, category, incumbent_verdict, incumbent_score,
                     incumbent_model, shadow_verdict, shadow_confidence,
                     shadow_model, outcome, latency_ms, input_tokens,
                     raw_keys, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING
            """, (
                str(row.get("press_slug") or "")[:200],
                str(row.get("category") or "")[:80] or None,
                row.get("incumbent_verdict"), row.get("incumbent_score"),
                str(row.get("incumbent_model") or "")[:120] or None,
                row.get("shadow_verdict"), row.get("shadow_confidence"),
                str(row.get("shadow_model") or "")[:120] or None,
                str(row.get("outcome") or "unknown")[:40],
                row.get("latency_ms"), row.get("input_tokens"),
                json.dumps(list(row.get("raw_keys") or [])[:12]),
                str(row.get("detail") or "")[:300] or None,
            ))
        return True
    except Exception as e:
        logger.warning("[jev_shadow] record failed: %s", str(e)[:140])
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── The shadow call ──────────────────────────────────────────────────────

def _ask_jev(state: dict) -> dict:
    """One typed decision from Jev. Returns an outcome dict; never raises."""
    t0 = time.time()
    try:
        import requests
    except Exception:
        return {"outcome": "no_requests", "latency_ms": 0}
    try:
        r = requests.post(
            _endpoint(),
            headers={"Authorization": f"Bearer {_api_key()}",
                     "content-type": "application/json",
                     "User-Agent": "dchub-brain-jev-shadow/1.0"},
            json={"model": _model(), "state": state,
                  "questions": build_questions()},
            timeout=_timeout_s())
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        name = type(e).__name__.lower()
        outcome = "timeout" if "timeout" in name else "transport_error"
        return {"outcome": outcome, "latency_ms": ms, "detail": str(e)[:200]}

    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        # 401 = key rejected, 422 = our request shape is wrong, 429 = throttled.
        # All three are recorded distinctly so a misconfigured lane is legible
        # at a glance instead of looking like a model that never disagrees.
        return {"outcome": f"http_{r.status_code}", "latency_ms": ms,
                "detail": (r.text or "")[:200]}
    try:
        payload = r.json()
    except Exception:
        return {"outcome": "parse_error", "latency_ms": ms,
                "detail": "response was not JSON"}

    verdict, conf, raw_keys = _parse_answer(payload)
    usage = payload.get("usage") if isinstance(payload, dict) else None
    tokens = None
    if isinstance(usage, dict):
        for k in ("input_tokens", "prompt_tokens", "total_input_tokens"):
            if isinstance(usage.get(k), int):
                tokens = usage[k]
                break
    if verdict is None:
        return {"outcome": "parse_error", "latency_ms": ms, "raw_keys": raw_keys,
                "input_tokens": tokens,
                "detail": "no recognisable answer in response"}
    return {"outcome": "ok", "latency_ms": ms, "shadow_verdict": verdict,
            "shadow_confidence": conf, "raw_keys": raw_keys,
            "input_tokens": tokens}


def record_shadow(title: str, body: str, category: str, recent_lines: str,
                  incumbent_verdict: str, incumbent_score=None,
                  incumbent_model: str | None = None,
                  press_slug: str | None = None) -> dict:
    """Ask Jev the editor's question and record both answers. NEVER raises.

    The caller discards the return value — it exists for tests and for the log
    line. Nothing here can change a publish decision.
    """
    try:
        if not _enabled():
            return {"outcome": "disabled"}
        if not _api_key():
            # ★ Armed but keyless is a MISCONFIGURATION, and it gets a row.
            # Returning silently here would leave an empty table that reads
            # exactly like a lane that ran and found nothing to disagree with.
            row = {"press_slug": press_slug or (title or "")[:80],
                   "category": category, "incumbent_verdict": incumbent_verdict,
                   "incumbent_score": incumbent_score,
                   "incumbent_model": incumbent_model,
                   "shadow_model": _model(), "outcome": "no_key",
                   "detail": "JEV_SHADOW_ENABLED=1 but no TYPESAFE_API_KEY/JEV_API_KEY"}
            _record(row)
            logger.warning("[jev_shadow] armed without an API key — recorded no_key")
            return {"outcome": "no_key"}

        res = _ask_jev(build_state(title, body, category, recent_lines))
        row = {
            "press_slug": press_slug or (title or "")[:80],
            "category": category,
            "incumbent_verdict": incumbent_verdict,
            "incumbent_score": incumbent_score,
            "incumbent_model": incumbent_model,
            "shadow_model": _model(),
            "shadow_verdict": res.get("shadow_verdict"),
            "shadow_confidence": res.get("shadow_confidence"),
            "outcome": res.get("outcome"),
            "latency_ms": res.get("latency_ms"),
            "input_tokens": res.get("input_tokens"),
            "raw_keys": res.get("raw_keys"),
            "detail": res.get("detail"),
        }
        _record(row)
        if res.get("outcome") == "ok":
            logger.info("[jev_shadow] %s incumbent=%s shadow=%s (%s) %sms",
                        compare_verdicts(incumbent_verdict, res.get("shadow_verdict")),
                        incumbent_verdict, res.get("shadow_verdict"),
                        res.get("shadow_confidence"), res.get("latency_ms"))
        else:
            logger.warning("[jev_shadow] outcome=%s detail=%s",
                           res.get("outcome"), str(res.get("detail"))[:120])
        return res
    except Exception as e:  # pragma: no cover — the gate must never see this
        logger.warning("[jev_shadow] swallowed: %s", str(e)[:140])
        return {"outcome": "shadow_raised", "detail": str(e)[:200]}


# ── Read surface ─────────────────────────────────────────────────────────

@media_jev_shadow_bp.route("/api/v1/admin/media/jev-shadow", methods=["GET"])
def jev_shadow_summary():
    """Agreement + cost between the editorial desk and its Jev shadow."""
    try:
        from routes.brain_mechanical_classifier import _admin_ok
    except Exception:
        return jsonify({"error": "admin gate unavailable"}), 503
    if not _admin_ok():
        return jsonify({"error": "forbidden"}), 403

    try:
        days = max(1, min(90, int(request.args.get("days", "14"))))
    except Exception:
        days = 14

    c = _conn()
    if c is None:
        return jsonify({"error": "no database"}), 503
    rows = []
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute("""
                SELECT press_slug, category, incumbent_verdict, incumbent_score,
                       shadow_verdict, shadow_confidence, outcome, latency_ms,
                       input_tokens, raw_keys, detail, created_at
                  FROM media_editorial_jev_shadow
                 WHERE created_at > NOW() - (%s || ' days')::interval
                 ORDER BY created_at DESC
                 LIMIT 2000
            """, (str(days),))
            for r in cur.fetchall():
                rows.append({
                    "press_slug": r[0], "category": r[1],
                    "incumbent_verdict": r[2], "incumbent_score": r[3],
                    "shadow_verdict": r[4], "shadow_confidence": r[5],
                    "outcome": r[6], "latency_ms": r[7], "input_tokens": r[8],
                    "raw_keys": r[9], "detail": r[10],
                    "created_at": r[11].isoformat() if r[11] else None,
                })
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try: c.close()
        except Exception: pass

    summary = summarize(rows)
    disagreements = [r for r in rows
                     if compare_verdicts(r.get("incumbent_verdict"),
                                         r.get("shadow_verdict")) == "disagree"]
    return jsonify({
        "days": days,
        "armed": bool(_enabled() and _api_key()),
        "shadow_model": _model(),
        "summary": summary,
        "disagreements": disagreements[:50],
        "recent": rows[:50],
    })
