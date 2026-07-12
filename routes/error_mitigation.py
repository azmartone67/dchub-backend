"""_error_mitigation envelope (2026-07-12) — Gemini co-design, error_version: 1.

The in-band self-correction rail: when a tool hits a KNOWN, mitigable error
state, instead of a raw JSON-RPC -32603 that shatters the agent's context loop,
the response carries a structured `_error_mitigation` block — a deterministic
path back to a successful call. The MCP layer flips isError:true so the model
treats it as an actionable error state, not a silent empty success.

THE THREE GROUNDING DELTAS (why this is a contract, not just a schema):
  1. ZERO-HALLUCINATION / STRICT BINDING — deterministic_hint and
     suggested_params are TEMPLATED SERVER-SIDE from this registry + the
     endpoint's real computed state. A hint can NEVER name a parameter that
     doesn't exist (suggested_params keys are whitelisted per code here), and
     values are the endpoint's own computed numbers, never free text or
     hardcoded guesses. (Gemini's cold-baseline example once hinted a
     nonexistent `candidate_sites` param — that class is impossible by
     construction now.)
  2. COMPUTED REALITY — a code that carries a suggested value gets it from the
     caller (e.g. min_satisfiable_max_ttp_months, computed by the TTP filter),
     never a static "36".
  3. NORMATIVE GROUNDING — error_code is a stable enum from this versioned
     registry, published at /docs/error-codes and machine-readable at
     /api/v1/error-codes, so an agent's state machine keys on a fixed taxonomy.

severity drives the agent state machine:
  parameter_adjustment — query is structurally sound but mathematically
                         impossible; fix inputs (from suggested_params) + retry.
  transient_backoff    — load/latency; wait and retry (same args).
  fatal                — upstream hard-down; do NOT retry, degrade honestly.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, send_from_directory

error_mitigation_bp = Blueprint("error_mitigation", __name__)

ERROR_VERSION = 1

# code -> {severity, hint (a .format template using ONLY computed keys),
#          params (whitelist of suggested_params keys — nothing else may appear)}
_REGISTRY = {
    "zero_row_ttp_cut": {
        "severity": "parameter_adjustment",
        "hint": ("No queued capacity matches this time-to-power constraint for "
                 "the selected ISO(s). Expand max_ttp_months to at least "
                 "{min_satisfiable} to surface the nearest queued capacity."),
        "params": ["max_ttp_months"],
        "doc": "A max_ttp_months below every in-scope ISO's average interconnection "
               "wait — the filter is a hard ISO cut, so the result is provably empty.",
    },
    "eia_upstream_timeout": {
        "severity": "transient_backoff",
        "hint": ("Upstream EIA feed latency. Retry after a short backoff, or "
                 "widen horizon_months to {suggested_horizon} to hit DC Hub's "
                 "pre-aggregated cache."),
        "params": ["horizon_months"],
        "doc": "The EIA API (eia.gov) hung or timed out; DC Hub did not crash — "
               "retry, or widen the horizon to read the cached aggregate.",
    },
    "candidate_read_unavailable": {
        "severity": "transient_backoff",
        "hint": ("Candidate store temporarily unavailable. Retry the request; "
                 "the candidate_id stays valid until its snapshot expires."),
        "params": [],
        "doc": "A transient fault reading the candidate store (replica hiccup / "
               "timeout) — distinct from an unknown candidate. Retry.",
    },
    "cold_baseline_concurrency": {
        "severity": "transient_backoff",
        "hint": ("The site-score reference baseline is warming under "
                 "concurrent load. Retry after a short backoff; results are "
                 "unaffected once warm."),
        "params": [],
        "doc": "The percentile baseline was recomputing when many concurrent "
               "queries arrived — a warm-up window, not a failure.",
    },
    "upstream_hard_down": {
        "severity": "fatal",
        "hint": ("A required upstream dependency is currently unavailable for "
                 "this query. Do not retry; report the specific data as "
                 "temporarily inaccessible rather than substituting an estimate."),
        "params": [],
        "doc": "An upstream dependency is hard-down. Fail closed — degrade the "
               "answer honestly, never synthesize a substitute metric.",
    },
}


def registry_public():
    """Machine-readable registry for an agent's state machine (served at
    /api/v1/error-codes). Templates + whitelists, no runtime state."""
    return {
        "error_version": ERROR_VERSION,
        "severities": {
            "parameter_adjustment": "query sound but impossible — fix inputs (suggested_params) and retry",
            "transient_backoff": "load/latency — wait and retry the same args",
            "fatal": "upstream hard-down — do not retry, degrade honestly",
        },
        "codes": {code: {"severity": s["severity"], "suggested_params": s["params"],
                         "meaning": s["doc"]}
                  for code, s in _REGISTRY.items()},
    }


@error_mitigation_bp.route("/api/v1/error-codes")
def api_error_codes():
    """Machine-readable error_code registry — an agent's state machine keys on
    this fixed taxonomy (the normative-grounding delta). Public, no key."""
    r = registry_public()
    r["_entity"] = "error_code_registry"
    r["_cite"] = "DC Hub (dchub.cloud)"
    r["doc"] = "https://dchub.cloud/docs/error-codes"
    return jsonify(r)


@error_mitigation_bp.route("/docs/error-codes")
def doc_error_codes():
    """The normative registry page (human + agent readable), like the candidate
    lifecycle doc — kept in lockstep with _REGISTRY."""
    return send_from_directory("static", "error-codes.html")


def build_mitigation(code, computed=None, suggested=None):
    """Build the _error_mitigation block for `code`. Returns None for an
    unknown code (never fabricates one). ENFORCES the guarantees:
      - deterministic_hint is templated from `computed` values only; if a
        template key is missing, the hint degrades to the base sentence
        (never emits an unfilled {placeholder}).
      - suggested_params contains ONLY keys whitelisted for this code in the
        registry, valued from `suggested`; any other key is dropped.
    """
    spec = _REGISTRY.get(code)
    if not spec:
        return None
    computed = computed or {}
    suggested = suggested or {}
    try:
        hint = spec["hint"].format(**computed)
    except (KeyError, IndexError):
        # a required computed value is absent — never surface a raw {placeholder}
        import re as _re
        hint = _re.sub(r"\s*[^.]*\{[^}]+\}[^.]*\.", "", spec["hint"]).strip() or \
            "This query needs adjusted parameters — see suggested_params."
    block = {
        "error_version": ERROR_VERSION,
        "error_code": code,
        "severity": spec["severity"],
        "deterministic_hint": hint,
    }
    sp = {k: suggested[k] for k in spec["params"] if k in suggested}
    if sp:
        block["suggested_params"] = sp
    return block
