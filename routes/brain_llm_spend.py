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


def _scanned_files():
    """Every file the coverage scan looks at: routes/*.py AND the repo root's
    own *.py.

    ★2026-09-20 — THE SCAN USED TO BE routes/ ONLY while the note beside it
    said "module(s) IN THE TREE that issue a model request". That was an
    overclaim in the one place whose entire job is to state its own limits:
    main.py and content_publisher.py both build the Anthropic URL and post it,
    and neither could ever appear in the numerator OR the denominator. Adding
    the root makes the sentence true.

    Both globs are NON-recursive on purpose. `rglob` here would walk venv/,
    node_modules/ and — the one that actually bites in this repo —
    .claude/worktrees/, which holds whole stale checkouts of these same
    modules. A scan that counted those would report coverage over code that
    is not deployed and cannot be.
    """
    import pathlib
    here = pathlib.Path(__file__).resolve().parent          # routes/
    root = here.parent                                      # repo root
    return sorted(here.glob("*.py")) + sorted(root.glob("*.py"))


def instrumented_modules():
    """Modules whose Anthropic POSTs go through instrumented_post(). Scanned,
    not listed, for the same reason the denominator is: a hand-kept roster
    drifts the moment someone adds a call site."""
    if _instrumented["value"] is not None:
        return _instrumented["value"]
    out = []
    try:
        for f in _scanned_files():
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
        n = 0
        for p in _scanned_files():
            # ★THE SCANNER MATCHES ITS OWN SOURCE. This file contains the
            # literals "anthropic_messages_url()" and "instrumented_post"
            # because it SEARCHES for them, so it satisfies the predicate below
            # without issuing a single model request. instrumented_modules()
            # has always skipped it; the denominator did not, which left a
            # permanent off-by-one. Once the real gap closed that phantom was
            # the ONLY one left, and coverage would have read "26 of 27"
            # forever — one uninstrumented-looking module that does not exist.
            if p.name == "brain_llm_spend.py":
                continue
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


def uninstrumented_modules():
    """Modules that issue a model request but do NOT go through
    instrumented_post() — the gap, BY NAME.

    ★2026-09-20. Coverage was two integers, and two integers tell an operator
    that something is missing without telling them what. A name is actionable
    where "26 of 27" is only worrying. It also gives the regression guard
    (tests/test_llm_spend_coverage.py) something to assert on that fails with
    the offending module printed, instead of an arithmetic mismatch.

    Returns [] when everything is wired, and None when the scan could not run —
    which callers must render as unknown, never as "nothing is missing".
    """
    try:
        inst = set(instrumented_modules())
        out = []
        for p in _scanned_files():
            if p.name == "brain_llm_spend.py":
                continue  # see count_llm_modules: it matches its own source
            try:
                s = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "anthropic_messages_url()" not in s:
                continue
            if not ("requests.post" in s or "_llm_post(" in s
                    or "instrumented_post" in s):
                continue
            if p.stem not in inst:
                out.append(p.stem)
        return sorted(out)
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: gap scan failed: %s", str(e)[:120])
        return None


#: Callees that ARE the ledger. Everything else that posts is raw.
_WRAPPER_NAMES = ("instrumented_post", "_llm_post",
                  "instrumented_urlopen", "_llm_urlopen")
#: Raw transports we care about — the calls that actually go to the network.
#: ★`Request` is deliberately NOT here. urllib is two-step: build a Request
#: (which holds the URL), then urlopen it (which sends it). Flagging the
#: Request would flag a correctly-wired site too, because wiring replaces the
#: urlopen and leaves the Request exactly where it was. The transport is the
#: thing to check; _is_anthropic_arg() follows the variable back to the
#: Request to find the URL.
_RAW_CALLEES = ("post", "urlopen")


def _is_anthropic_arg(node, assigns) -> bool:
    """True when an argument names the Anthropic endpoint, directly or through
    a local variable."""
    import ast as _ast
    seen = 0
    while isinstance(node, _ast.Name) and node.id in assigns and seen < 5:
        node = assigns[node.id]
        seen += 1                      # bounded: assignment cycles are possible
    if isinstance(node, _ast.Call):
        f = node.func
        fname = getattr(f, "id", None) or getattr(f, "attr", None)
        if fname == "anthropic_messages_url":
            return True
        if fname == "Request":
            # urllib two-step: the URL is the Request's first argument, so
            # follow into it rather than treating the Request as opaque.
            inner = list(node.args) + [k.value for k in node.keywords
                                       if k.arg == "url"]
            return any(_is_anthropic_arg(a, assigns) for a in inner)
        return False
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        return "api.anthropic.com" in node.value
    if isinstance(node, _ast.IfExp):   # `x if cond else y` — either branch counts
        return (_is_anthropic_arg(node.body, assigns)
                or _is_anthropic_arg(node.orelse, assigns))
    return False


def uninstrumented_call_sites():
    """Every Anthropic POST that does NOT go through the ledger, as
    (module, lineno). [] when clean, None when the scan could not run.

    ★2026-09-20 — WHY THIS REPLACED A SUBSTRING CHECK. Coverage used to ask
    "does this FILE contain the string 'instrumented_post'?" That is not the
    same question as "does this CALL go through it", and the difference is not
    academic: wiring a call site leaves an `import ... instrumented_post as
    _llm_post` line behind, so un-wiring the CALL and keeping the IMPORT left
    the module still counted as instrumented. Measured — the mutation that
    reverted a wired call to `requests.post` did not fail the guard at all.
    A module with five Anthropic posts and one wrapped read as fully covered.

    So this walks the AST and resolves the URL argument, including through a
    local variable (`url = anthropic_messages_url()` … `requests.post(url,…)`,
    which is how three of these call sites are actually written).
    """
    try:
        import ast as _ast
        out = []
        for p in _scanned_files():
            if p.name == "brain_llm_spend.py":
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "anthropic_messages_url" not in src and "api.anthropic.com" not in src:
                continue
            try:
                tree = _ast.parse(src)
            except Exception:
                continue
            assigns = {}
            for n in _ast.walk(tree):
                if isinstance(n, _ast.Assign) and len(n.targets) == 1 and \
                        isinstance(n.targets[0], _ast.Name):
                    assigns.setdefault(n.targets[0].id, n.value)
            for n in _ast.walk(tree):
                if not isinstance(n, _ast.Call):
                    continue
                name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                if name in _WRAPPER_NAMES:
                    continue
                if name not in _RAW_CALLEES:
                    continue
                args = list(n.args) + [k.value for k in n.keywords
                                       if k.arg in ("url", "data")]
                if any(_is_anthropic_arg(a, assigns) for a in args):
                    out.append((p.stem, n.lineno))
        return sorted(out)
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_llm_spend: call-site scan failed: %s", str(e)[:120])
        return None


class _ReplayedResponse:
    """What instrumented_urlopen hands back: the body it already read.

    The wrapper has to consume the response to count tokens, and urllib's real
    response is a single-use stream — reading it in the ledger would hand the
    caller an empty body. So the bytes are replayed here behind the same
    surface the 26 urllib call sites actually use: `with … as r: r.read()`,
    plus .status/.getcode()/.headers for the few that check them.
    """
    __slots__ = ("_data", "status", "headers", "url")

    def __init__(self, data, status, headers, url=""):
        self._data, self.status, self.headers, self.url = data, status, headers, url

    def read(self, *_a):
        return self._data

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _model_of_request(req) -> str:
    """Model name out of a urllib Request's encoded body. "" when absent."""
    try:
        import json as _j
        raw = getattr(req, "data", None)
        if not raw:
            return ""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        d = _j.loads(raw)
        return str(d.get("model") or "") if isinstance(d, dict) else ""
    except Exception:
        return ""


def instrumented_urlopen(layer: str, req, **kwargs):
    """urllib.request.urlopen, timed and ledgered.

    ★WHY A SECOND WRAPPER RATHER THAN CONVERTING THE CALLERS. 26 of the
    uninstrumented Anthropic calls in this tree are urllib, not requests, and
    urllib RAISES HTTPError on a non-2xx while requests returns a response.
    Rewriting them to requests means rewriting 26 error branches, and any one
    that was missed would turn a 429 into a silent success path — the failure
    mode is invisible and lands in production. This wrapper changes the
    transport not at all: HTTPError still raises, the object still works as a
    context manager, .read() still returns the same bytes.

    BEHAVIOUR-PRESERVING BY CONSTRUCTION, on the same terms as
    instrumented_post: exceptions are recorded and RE-RAISED, and a failure
    inside the ledger is swallowed.
    """
    import time as _t
    import urllib.request as _ur
    t0 = _t.time()
    model = _model_of_request(req)
    try:
        with _ur.urlopen(req, **kwargs) as r:
            data = r.read()
            status = getattr(r, "status", None) or r.getcode()
            headers = getattr(r, "headers", None)
            url = getattr(r, "url", "") or ""
    except Exception as e:  # noqa: BLE001 — record, then re-raise untouched
        try:
            code = getattr(e, "code", None)
            record(layer, model=model, body=None,
                   ms=int((_t.time() - t0) * 1000), ok=False,
                   stop_reason=(f"http_{code}" if code else type(e).__name__))
        except Exception:
            pass
        raise
    ms = int((_t.time() - t0) * 1000)
    try:
        import json as _j
        body = _j.loads(data.decode("utf-8", "replace"))
    except Exception:
        body = None
    try:
        record(layer, model=model or (body or {}).get("model", ""), body=body,
               ms=ms, ok=True,
               stop_reason=str((body or {}).get("stop_reason") or ""))
    except Exception:
        pass
    return _ReplayedResponse(data, status, headers, url)


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
    _gap = uninstrumented_modules()
    _sites = uninstrumented_call_sites()
    out["coverage"] = {"layers_recording": seen,
                       "llm_modules_in_tree": total_modules,
                       "instrumented": instrumented_modules(),
                       # ★The gap BY NAME. null (not []) when the scan failed —
                       # an empty list would read as "nothing missing", which is
                       # the one thing an unmeasured scan must never say.
                       "uninstrumented": _gap,
                       # ★THE NUMBER THAT ACTUALLY MATTERS. The two above are
                       # FILE-level: a module counts as instrumented if it
                       # mentions the wrapper anywhere, so one wired call and
                       # four raw ones reads as covered. This is CALL-SITE
                       # level, resolved from the AST, and on 2026-09-20 it was
                       # 39 across 27 modules while the file-level numbers said
                       # the gap was 4. Read this one.
                       "uninstrumented_call_sites": _sites,
                       "uninstrumented_call_site_count":
                           (len(_sites) if _sites is not None else None)}
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
