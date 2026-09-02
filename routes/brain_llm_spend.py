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
`coverage` beside the totals — both sides scanned, never listed — and the note
says in words that the numbers are a floor.

Endpoint: GET /api/v1/admin/brain/llm-spend?days=7   (admin-gated)
Kill: BRAIN_LLM_SPEND_DISABLE=1  (recording becomes a no-op; reads still work)
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_llm_spend_bp = Blueprint("brain_llm_spend", __name__)

# ★No hand-kept roster of instrumented call sites lives here. The first version
# had one, and a list a human must remember to update is the same shape as the
# VALUE_NOT_COUNT_ISSUES tuple this codebase has now edited three times after
# the fact. instrumented_modules() scans for the wrapper instead.

# ★COUNTED, NOT GUESSED. This was a hardcoded 20 for exactly one commit, which
# was a confident guess sitting inside the module whose entire purpose is to
# replace confident guesses with measurements. The real figure is derived by
# scanning routes/ for modules that both build the Anthropic URL and POST, and
# it degrades to None — reported as "unknown" — rather than to a number.
_llm_module_count = {"value": -1}
_instrumented = {"value": None}


def instrumented_modules():
    """Modules whose Anthropic POSTs go through instrumented_post(). Scanned,
    not listed, for the same reason the denominator is: a hand-kept roster
    drifts the moment someone adds a call site."""
    if _instrumented["value"] is not None:
        return _instrumented["value"]
    out = []
    try:
        import pathlib
        here = pathlib.Path(__file__).resolve().parent
        for f in sorted(here.glob("*.py")):
            if f.name == "brain_llm_spend.py":
                continue
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "instrumented_post" in src and "anthropic_messages_url()" in src:
                out.append(f.stem)
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: instrumented scan failed: %s", str(e)[:120])
    _instrumented["value"] = out
    return out


def count_llm_modules():
    """How many modules in routes/ actually issue a model request. None when
    the scan could not run; callers must render that as unknown, never as 0
    (which would read as "everything is instrumented")."""
    if _llm_module_count["value"] != -1:
        return _llm_module_count["value"]
    try:
        import pathlib
        here = pathlib.Path(__file__).resolve().parent
        n = 0
        for p in here.glob("*.py"):
            try:
                s = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # ★Count BOTH shapes. The denominator originally keyed on
            # `requests.post`, and the migration to the wrapper deleted that
            # string from 20 modules — so coverage briefly read "20 of 7",
            # a nonsense ratio produced by a metric that measured the thing
            # being changed. A module issues a model request if it builds the
            # URL and posts it by either route.
            if "anthropic_messages_url()" in s and (
                    "requests.post" in s or "_llm_post(" in s
                    or "instrumented_post" in s):
                n += 1
        _llm_module_count["value"] = n or None
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: module scan failed: %s", str(e)[:120])
        _llm_module_count["value"] = None
    return _llm_module_count["value"]


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


def instrumented_post(layer: str, url, **kwargs):
    """requests.post, timed and ledgered. Returns the SAME response object.

    ★BEHAVIOUR-PRESERVING BY CONSTRUCTION. This wraps 20+ call sites whose
    response handling this module knows nothing about, so it must be invisible:

      · the response object is returned untouched — no parsing, no mutation;
      · an exception from requests (timeout, connection reset) is recorded and
        then RE-RAISED, because a caller that catches requests.Timeout must go
        on catching it. Swallowing it here would silently change 20 modules'
        control flow at once;
      · a failure inside the ledger itself is swallowed. A measurement must
        never be able to fail the thing it measures.

    The layer label is the calling module's own name, not a curated map — a
    mapping table is one more thing to keep in sync, and a wrong label is worse
    than a boring one."""
    import time as _t
    import requests
    # ★2026-09-02 load-shedding governor (util/llm_spend_governor). Dark by
    # default; when armed it refuses the listed customer-facing consumers
    # before the request is made, with a 429-shaped structured `shed` reply,
    # so the reasoning layers keep headroom under the gateway's 7d rule. A
    # broken governor can never break the call (fail-open to "proceed").
    try:
        from util.llm_spend_governor import shed_response as _shed
        _refusal = _shed(layer)
    except Exception:  # noqa: BLE001
        _refusal = None
    if _refusal is not None:
        try:
            record(layer, model=_model_of(kwargs), body=None, ms=0, ok=False,
                   stop_reason="shed")
        except Exception:
            pass
        return _refusal
    t0 = _t.time()
    try:
        resp = requests.post(url, **kwargs)
    except Exception as e:  # noqa: BLE001
        try:
            record(layer, model=_model_of(kwargs), body=None,
                   ms=int((_t.time() - t0) * 1000), ok=False,
                   stop_reason=type(e).__name__[:40])
        except Exception:
            pass
        raise
    ms = int((_t.time() - t0) * 1000)
    try:
        body = None
        if getattr(resp, "status_code", None) == 200:
            try:
                body = resp.json()
            except Exception:
                body = None
        record(layer, model=_model_of(kwargs), body=body, ms=ms,
               ok=(getattr(resp, "status_code", 0) == 200),
               stop_reason=(str((body or {}).get("stop_reason") or "")
                            if isinstance(body, dict)
                            else f"http_{getattr(resp, 'status_code', '?')}"))
    except Exception:
        pass
    return resp


def _model_of(kwargs) -> str:
    """Best-effort model name out of the request payload. "" when absent — an
    unlabelled row still records that the call happened."""
    try:
        j = kwargs.get("json")
        if isinstance(j, dict):
            return str(j.get("model") or "")
    except Exception:
        pass
    return ""


def summary(cur, days: int = 7) -> dict:
    """Per-layer token + latency totals, WITH its own coverage stated."""
    out = {"ok": False, "days": int(days), "layers": [],
           "instrumented": instrumented_modules()}
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
    total_modules = count_llm_modules()
    out["coverage"] = {"layers_recording": seen,
                       "llm_modules_in_tree": total_modules,
                       "instrumented": instrumented_modules()}
    out["note"] = (
        f"TOKENS, NOT DOLLARS — no price table lives in this repo, because a "
        f"stale one turns a measurement into a guess. ★COVERAGE: {seen} "
        f"layer(s) recording out of "
        + (f"{total_modules} module(s)" if total_modules is not None
           else "an UNKNOWN number of modules (the scan failed — treat "
                "coverage as unmeasured)")
        + " in the tree that issue a model request. These totals are a FLOOR "
          "on spend, not a share of it — a layer missing here is "
          "uninstrumented, not free.")
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
