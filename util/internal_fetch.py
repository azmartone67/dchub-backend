"""util/internal_fetch.py — the internal-probe envelope (2026-08-11).

WHY THIS EXISTS
---------------
Seventeen modules in routes/ carry a private `_internal(path)` that reads a
sibling surface over localhost HTTP and, on any problem at all, returns a bare
`{}`:

    if r.status_code != 200: return {}
    except Exception:       return {}

A timeout, a 500, a refused connection and a healthy-but-empty payload are then
INDISTINGUISHABLE downstream. The layers that assemble context out of these
calls (L14 causal, L16 self-critique, L18 consolidation, L8 orchestrator) hand
the model a `{}` it can only read as absence-of-signal, so the model answers
"cannot verify", L16 logs a null prediction, and L18 faithfully distils that
null into a lesson.

That loop is measurable and it has already consumed the brain's memory. Of the
20 lessons live on /api/v1/brain/lessons on 2026-08-11, **17 describe this one
failure**, the top two carrying evidence_count 15 and 12:

    "Predictions requiring non-empty endpoint payloads … null when endpoints
     return empty"
    "Predictions requiring granular breakdowns null when endpoints return empty"

The brain's learning loop is not broken — it works, and what it has learned is
that its own instruments are blindfolded. This module is the blindfold coming
off. It is the same failure shape as the `|| true` that made four producers exit
0 while doing nothing (see CLAUDE.md "Verifying"): a failure rendered as a
benign value.

WHAT IT DOES
------------
`probe()` returns an ENVELOPE instead of a payload:

    {"path", "ok", "data", "reason", "status", "empty"}

  ok=False   the instrument failed. We do not know the answer.
  ok=True, empty=True    the instrument worked and the answer is genuinely
                         nothing. That is a MEASUREMENT, and it is the case a
                         bare {} has been destroying.

★ `ok` is never inferred from the shape of `data`. An endpoint returning `{}`
with HTTP 200 is a working endpoint reporting nothing, and collapsing it back
into "failed" would rebuild the bug from the other side.

★ Fail-flat, never raise. These calls sit on diagnostic paths; a prober that
can throw turns a context-assembly gap into an outage.

requests, not urllib (regression_lint urllib-request-on-railway).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 8


def _base() -> str:
    return "http://127.0.0.1:" + str(os.environ.get("PORT", "8080"))


def _envelope(path: str, ok: bool, data, reason, status) -> dict:
    return {
        "path": path,
        "ok": bool(ok),
        "data": data if isinstance(data, (dict, list)) else {},
        "reason": reason,
        "status": status,
        # empty is only meaningful when ok — see the module docstring.
        "empty": bool(ok) and not data,
    }


def probe(path: str, timeout=_DEFAULT_TIMEOUT, headers=None) -> dict:
    """GET an internal surface and report WHAT HAPPENED, not just what came
    back. Never raises.

    `timeout` is forwarded to requests UNTOUCHED, so a (connect, read) tuple
    works — L8's orchestrator budget depends on that form and must keep it.

    ★`headers` MATTERS on some paths and is merged over the default UA. Callers
    hitting tier-gated surfaces must keep their own: /api/v1/grid/intelligence/*
    sits behind free_tier_gate.METERED_MAP_PREFIXES, which runs at
    before_request and privileges only keys/loopback — a loopback call without
    X-Internal-Key was 402'd by our own paywall on 2026-07-31 and every
    grid-intel field on /radar silently pinned to baseline. The default UA is
    `dchub-`-prefixed because tier-gating is UA-aware."""
    hdrs = {"User-Agent": "dchub-internal-probe/1.0"}
    if headers:
        hdrs.update(headers)
    try:
        import requests as _rq
        r = _rq.get(_base() + path, timeout=timeout, headers=hdrs)
    except Exception as e:  # noqa: BLE001
        return _envelope(path, False, {},
                         "%s: %s" % (type(e).__name__, str(e)[:110]), None)
    if r.status_code != 200:
        return _envelope(path, False, {}, "HTTP %d" % r.status_code,
                         r.status_code)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — a 200 carrying non-JSON is a failure
        return _envelope(path, False, {}, "non-JSON body", r.status_code)
    if body is None:
        body = {}
    return _envelope(path, True, body, None, r.status_code)


def data_of(env) -> dict:
    """The payload, for callers that only want the old behaviour.

    ★Use this ONLY where the distinction genuinely does not matter. Anything
    that feeds a model or a verdict should read the envelope."""
    if isinstance(env, dict) and "ok" in env and "data" in env:
        d = env.get("data")
        return d if isinstance(d, dict) else {}
    return env if isinstance(env, dict) else {}


def health_of(probes: dict) -> dict:
    """Summarise a set of named envelopes for a prompt or a lane verdict.

    Returns instrument_failed / measured_empty / ok as NAME LISTS, so a model
    reading the context can say "I could not measure X" instead of "X is zero",
    and a lane can fail on the first list being non-empty."""
    failed, empty, ok = [], [], []
    for name, env in (probes or {}).items():
        if not isinstance(env, dict):
            continue
        if not env.get("ok"):
            failed.append({"probe": name, "path": env.get("path"),
                           "reason": env.get("reason")})
        elif env.get("empty"):
            empty.append(name)
        else:
            ok.append(name)
    return {
        "instrument_failed": failed,
        "measured_empty": sorted(empty),
        "ok": sorted(ok),
        "any_instrument_failed": bool(failed),
        "note": ("Probes under instrument_failed could NOT be measured — treat "
                 "them as unknown, never as zero. Probes under measured_empty "
                 "were measured successfully and the answer is genuinely "
                 "nothing."),
    }
