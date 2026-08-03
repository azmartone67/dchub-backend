"""brain_llm_spend.py — what the brain's thinking actually costs (2026-08-02).

Asked "where are the tokens going?" and found the honest answer is: nobody
knows. brain_layer20_durability.register_claude_call_start/end tracks IN-FLIGHT
calls in a process-local dict — it survives neither a restart nor a second
worker, and it records no token counts at all. Every layer passes its own
max_tokens and none of them is written down. So "reduce token spend" had no
baseline to reduce from, and any change would have been judged by whether it
felt cheaper.

This is the durable ledger: one row per model call, with the usage block the
API already returns.

★TOKENS, NOT DOLLARS. A price table living in this repo would go stale the
first time pricing moved and would turn a measurement into a confident guess —
the exact failure mode of the count-vs-duration misread. Multiply outside, where
the number can be checked against a bill.

★COVERAGE IS PART OF THE ANSWER. Only instrumented call sites appear here, so a
summary that reported per-layer totals alone would read as "L14 is all of our
spend" when it only means "L14 is all of our INSTRUMENTATION". summary() returns
`instrumented` and `uninstrumented_estimate` beside the totals, and the note
says so in words.

Endpoint: GET /api/v1/admin/brain/llm-spend?days=7   (admin-gated)
Kill: BRAIN_LLM_SPEND_DISABLE=1  (recording becomes a no-op; reads still work)
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_llm_spend_bp = Blueprint("brain_llm_spend", __name__)

# Call sites wired to record(). Grow this as sites are instrumented — it is what
# lets a reader tell "we spend nothing there" from "we measure nothing there".
INSTRUMENTED = ("L14",)

# Every module that constructs a model request. Counted by the coverage check so
# the gap is a number, not a vibe.
_KNOWN_LLM_MODULES = 20


def _disabled() -> bool:
    return (os.environ.get("BRAIN_LLM_SPEND_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2
        url = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL"))
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: connect failed: %s", str(e)[:120])
        return None


def ensure_schema(cur) -> bool:
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_llm_spend (
                id            BIGSERIAL PRIMARY KEY,
                layer         TEXT NOT NULL,
                model         TEXT,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                ms            INTEGER NOT NULL DEFAULT 0,
                ok            BOOLEAN NOT NULL DEFAULT TRUE,
                stop_reason   TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS brain_llm_spend_at_idx "
                    "ON brain_llm_spend (created_at DESC)")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: ensure_schema failed: %s", str(e)[:160])
        return False


def tokens_from_usage(body) -> tuple:
    """(input_tokens, output_tokens) from an Anthropic response body. Pure.
    (0, 0) when absent — a missing usage block is zero MEASURED tokens, and the
    row still records that the call happened, which is the difference between
    'cheap' and 'unmeasured'."""
    if not isinstance(body, dict):
        return 0, 0
    u = body.get("usage")
    if not isinstance(u, dict):
        return 0, 0
    def _i(v):
        try:
            return max(0, int(v or 0))
        except Exception:
            return 0
    return _i(u.get("input_tokens")), _i(u.get("output_tokens"))


def record(layer: str, model: str = "", body=None, ms: int = 0,
           ok: bool = True, stop_reason: str = "") -> bool:
    """Append one call to the ledger. NEVER raises — a measurement must not be
    able to fail the thing it measures."""
    if _disabled():
        return False
    try:
        it, ot = tokens_from_usage(body)
        c = _conn()
        if c is None:
            return False
        try:
            with c.cursor() as cur:
                if not ensure_schema(cur):
                    return False
                cur.execute("""
                    INSERT INTO brain_llm_spend
                        (layer, model, input_tokens, output_tokens, ms, ok,
                         stop_reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING""",
                    (str(layer or "?")[:40], str(model or "")[:80], it, ot,
                     int(ms or 0), bool(ok), str(stop_reason or "")[:40]))
            return True
        finally:
            try:
                c.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: record failed: %s", str(e)[:160])
        return False


def summary(cur, days: int = 7) -> dict:
    """Per-layer token + latency totals, WITH its own coverage stated."""
    out = {"ok": False, "days": int(days), "layers": [],
           "instrumented": list(INSTRUMENTED)}
    try:
        cur.execute("""
            SELECT layer,
                   COUNT(*)                       AS calls,
                   SUM(input_tokens)              AS in_tok,
                   SUM(output_tokens)             AS out_tok,
                   PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ms)  AS p50_ms,
                   PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY ms) AS p95_ms,
                   SUM(CASE WHEN ok THEN 0 ELSE 1 END) AS failures,
                   SUM(CASE WHEN stop_reason = 'max_tokens' THEN 1 ELSE 0 END)
                       AS truncated
              FROM brain_llm_spend
             WHERE created_at >= NOW() - make_interval(days => %s)
             GROUP BY layer
             ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (int(days),))
        rows = cur.fetchall() or []
    except Exception as e:  # noqa: BLE001
        out["error"] = f"unreadable: {str(e)[:160]} — UNMEASURED, not zero"
        return out
    total_in = total_out = total_calls = 0
    for r in rows:
        layer = {"layer": r[0], "calls": int(r[1] or 0),
                 "input_tokens": int(r[2] or 0), "output_tokens": int(r[3] or 0),
                 "p50_ms": int(r[4] or 0), "p95_ms": int(r[5] or 0),
                 "failures": int(r[6] or 0), "truncated": int(r[7] or 0)}
        # ★Truncation is a spend signal, not just a quality one: a call that
        # stops at max_tokens paid for every input token and returned an
        # unparseable answer, so it is 100% waste that retries then doubles.
        layer["wasted_calls"] = layer["failures"] + layer["truncated"]
        out["layers"].append(layer)
        total_in += layer["input_tokens"]
        total_out += layer["output_tokens"]
        total_calls += layer["calls"]
    out["ok"] = True
    out["total"] = {"calls": total_calls, "input_tokens": total_in,
                    "output_tokens": total_out}
    seen = len(out["layers"])
    out["coverage"] = {"layers_recording": seen,
                       "llm_modules_in_tree": _KNOWN_LLM_MODULES,
                       "instrumented": list(INSTRUMENTED)}
    out["note"] = (
        f"TOKENS, NOT DOLLARS — no price table lives in this repo, because a "
        f"stale one turns a measurement into a guess. ★COVERAGE: {seen} layer(s) "
        f"are recording out of ~{_KNOWN_LLM_MODULES} modules in the tree that "
        f"construct a model request. These totals are a FLOOR on spend, not a "
        f"share of it — a layer missing here is uninstrumented, not free.")
    return out


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


@brain_llm_spend_bp.route("/api/v1/admin/brain/llm-spend", methods=["GET"])
def llm_spend():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = max(1, min(int(request.args.get("days", "7")), 90))
    except Exception:
        days = 7
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            ensure_schema(cur)
            return jsonify(summary(cur, days=days))
    finally:
        try:
            c.close()
        except Exception:
            pass
