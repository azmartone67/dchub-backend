"""error_envelope.py — the error_version:1 contract (Gemini partnership,
LOCKED 2026-07-11).

WHY: an AI agent that hits an error should be able to RECOVER
deterministically instead of guessing. Every DC Hub error response carries
a machine-readable ``_error_mitigation`` block telling the agent (a) why it
failed, (b) how severe it is (retry now / back off / fail closed), and
(c) — when the fix is a parameter change — the EXACT corrected params to
merge and re-run with.

THE LOCKED SHAPE (structuredContent for MCP / JSON body for REST)::

    {
      "error_version": 1,
      "provenance": {
        "source":  "DC Hub Protocol Gateway",
        "as_of":   "<runtime date YYYY-MM-DD>",   # datetime.now — never hardcoded
        "license": "CC-BY-4.0",
        "cite_as": "DC Hub, dchub.cloud"
      },
      "_error_mitigation": {
        "error_code": "<machine_readable_snake_case>",
        "severity": "parameter_adjustment" | "transient_backoff" | "fatal",
        "deterministic_hint": "<one sentence: why + what unlocks it>",
        "suggested_params": { ... }   # OMITTED entirely when none
      }
    }

SEVERITY SEMANTICS (the agent-side state machine Gemini praised):
  * parameter_adjustment — agent re-runs IMMEDIATELY with the merged
    suggested_params (a deterministic corrector).
  * transient_backoff    — agent waits ~500ms and retries the SAME params
    (rate limit / pool pressure — no param change unlocks it).
  * fatal                — agent fails closed (physics violation / hard
    gate). No retry helps.

HARD RULE (the one that keeps the agent retry loop safe): every key in
``suggested_params`` MUST be a STRICT SUBSET of that tool's own declared
parameter names. An invalid key breaks the merge-and-retry loop, so the
validator DROPS (and logs) any key not in ``allowed_params`` and NEVER
emits it. If nothing survives, the key is omitted entirely.

FAIL-SOFT CONTRACT: nothing here may ever raise. Every public helper
catches everything and degrades to a minimal-but-valid envelope. Pure +
importable with no Flask / DB / network dependency
(reference_dchub_green_main_0709 — pre-merge tests never import main).
"""
from __future__ import annotations

import datetime
import logging

logger = logging.getLogger("error_envelope")

ERROR_VERSION = 1
PROVENANCE_SOURCE = "DC Hub Protocol Gateway"
LICENSE = "CC-BY-4.0"
CITE_AS = "DC Hub, dchub.cloud"

VALID_SEVERITIES = ("parameter_adjustment", "transient_backoff", "fatal")
# Unknown severity fails CLOSED — an agent must never be told to retry
# immediately (parameter_adjustment) on a severity we could not classify.
_SEVERITY_FALLBACK = "fatal"


def _runtime_as_of():
    """Runtime date, YYYY-MM-DD. datetime.now() at call time — NEVER
    hardcoded (the contract requires the true runtime date). Fail-soft:
    an empty string is still a valid JSON value if the clock ever raises."""
    try:
        return datetime.datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return ""


def _provenance_block():
    """The fixed provenance block for error envelopes. as_of is stamped at
    runtime on every call."""
    return {
        "source": PROVENANCE_SOURCE,
        "as_of": _runtime_as_of(),
        "license": LICENSE,
        "cite_as": CITE_AS,
    }


def _coerce_severity(severity):
    """Return a valid severity; unknown/garbage → fail-closed 'fatal' with a
    logged warning (never emit an out-of-contract severity)."""
    try:
        s = str(severity or "").strip()
    except Exception:
        s = ""
    if s in VALID_SEVERITIES:
        return s
    logger.warning(
        "error_envelope: invalid severity %r — coercing to %r",
        severity, _SEVERITY_FALLBACK)
    return _SEVERITY_FALLBACK


def validate_suggested_params(suggested_params, allowed_params):
    """STRICT-SUBSET filter for ``suggested_params``.

    Returns a NEW dict containing only keys that are in ``allowed_params``.
    Any key not in the allowed set is DROPPED and logged (it would break the
    agent's merge-and-retry loop). Behaviour:

      * suggested_params falsy / not a dict  -> {}
      * allowed_params is None               -> trust caller, keep all keys
                                                (still coerced to a plain dict)
      * allowed_params given                 -> keep only the intersection

    Pure; never raises. Callers treat an empty result as "omit the key".
    """
    try:
        if not suggested_params or not isinstance(suggested_params, dict):
            return {}
        if allowed_params is None:
            return dict(suggested_params)
        try:
            allowed = set(allowed_params)
        except TypeError:
            allowed = set()
        kept, dropped = {}, []
        for k, v in suggested_params.items():
            if k in allowed:
                kept[k] = v
            else:
                dropped.append(k)
        if dropped:
            logger.warning(
                "error_envelope: dropped out-of-contract suggested_params "
                "keys %s (allowed=%s) — an invalid key breaks the agent "
                "retry loop", sorted(map(str, dropped)), sorted(map(str, allowed)))
        return kept
    except Exception:
        # Fail closed: emit nothing rather than an unvalidated key.
        return {}


def error_mitigation(error_code, severity, hint,
                     suggested_params=None, allowed_params=None):
    """Build the full error_version:1 structuredContent envelope.

    error_code       — machine-readable snake_case identifier.
    severity         — one of VALID_SEVERITIES (coerced to 'fatal' if not).
    hint             — one-sentence deterministic_hint (why + what unlocks).
    suggested_params — corrective params to merge & re-run (ONLY meaningful
                       for severity 'parameter_adjustment'); OMITTED when it
                       ends up empty.
    allowed_params   — the tool's declared parameter names; when provided,
                       suggested_params is filtered to a STRICT SUBSET of it
                       (invalid keys dropped + logged).

    Always returns a valid dict; NEVER raises. Any internal error degrades to
    a minimal-but-valid envelope.
    """
    try:
        mitigation = {
            "error_code": str(error_code) if error_code is not None else "unknown_error",
            "severity": _coerce_severity(severity),
            "deterministic_hint": str(hint) if hint is not None else "",
        }
        kept = validate_suggested_params(suggested_params, allowed_params)
        if kept:
            mitigation["suggested_params"] = kept
        return {
            "error_version": ERROR_VERSION,
            "provenance": _provenance_block(),
            "_error_mitigation": mitigation,
        }
    except Exception:
        # Minimal valid envelope — the contract must survive any bug here.
        return {
            "error_version": ERROR_VERSION,
            "provenance": _provenance_block(),
            "_error_mitigation": {
                "error_code": "envelope_error",
                "severity": _SEVERITY_FALLBACK,
                "deterministic_hint": "",
            },
        }


def merge_error_mitigation(target, error_code, severity, hint,
                           suggested_params=None, allowed_params=None):
    """Merge an error_version:1 envelope INTO an existing REST/MCP error
    dict without clobbering fields the caller already set.

    ``target`` (e.g. {"error": "...", "message": "...", "valid_options": [...]})
    keeps every existing key; the three envelope keys (error_version,
    provenance, _error_mitigation) are added only if not already present.
    Returns ``target`` (mutated in place when it is a dict). NEVER raises —
    on any failure the caller's original dict is returned untouched.
    """
    try:
        env = error_mitigation(error_code, severity, hint,
                               suggested_params=suggested_params,
                               allowed_params=allowed_params)
        if not isinstance(target, dict):
            return env
        for k, v in env.items():
            if k not in target:
                target[k] = v
        return target
    except Exception:
        return target if isinstance(target, dict) else {}
