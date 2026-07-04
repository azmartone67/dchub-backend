"""routes/brain_llm_structured.py — Anthropic STRUCTURED OUTPUTS helper
=========================================================================

Shared helper for the brain's fragile-JSON Claude call sites (L6 strategic
planner, investigator REASON/REFUTE, feature proposer). Instead of praying
the model replies with fenced-or-unfenced JSON and fence-stripping it, we
send the schema natively so the API *guarantees* syntactically-valid JSON.

VERIFIED API FACTS (live docs, platform.claude.com, checked 2026-07-04)
-----------------------------------------------------------------------
· Parameter:   `output_config: {"format": {"type": "json_schema",
               "schema": {...}}}` on POST /v1/messages.
               (The old top-level `output_format` param and the
               `structured-outputs-2025-11-13` beta header are DEPRECATED —
               do not use them.)
· Beta header: NONE required — structured outputs are GA.
· Models:      claude-fable-5, claude-mythos-5, claude-mythos-preview,
               claude-opus-4-8 / -4-7 / -4-6 / -4-5,
               claude-sonnet-5 / -4-6 / -4-5, claude-haiku-4-5.
               NOT supported: claude-opus-4-1, claude-opus-4-0
               (claude-opus-4-20250514), claude-sonnet-4-0
               (claude-sonnet-4-20250514), retired haiku-3.x / 3.x models.
· Thinking:    compatible with extended/adaptive thinking. On fable-5
               thinking is ALWAYS ON and thinking tokens are billed against
               max_tokens — a long think can still starve the JSON answer
               (stop_reason == "max_tokens" → truncated, unparseable JSON).
               Structured outputs guarantee syntax only when the model
               finishes; keep the existing generous max_tokens headroom and
               the existing walk-the-chain behaviour on parse failure.
               With thinking active the response carries thinking block(s)
               BEFORE the text block — callers must read the first
               type=="text" block (all three call sites already do).
· Schema:      every object needs `additionalProperties: false`; `required`
               may be a subset (optional properties are allowed and are
               emitted after required ones). Unsupported: recursive schemas,
               minimum/maximum, minLength/maxLength, minItems>1.
· Errors:      unsupported model / invalid schema → HTTP 400. That is the
               fail-soft trigger: retry the SAME model on the legacy
               free-text path, then let the caller's existing fallback
               chain do its thing.

FAIL-SOFT LADDER (per call site)
--------------------------------
1. Model supports structured outputs + kill switch off → send
   `output_config.format` with the call site's schema.
2. API rejects the request with a 400 while structured mode was on →
   memoize the model as runtime-unsupported and retry the SAME model with
   the byte-identical legacy body (free text + fence-strip parse).
3. BRAIN_STRUCTURED_OUTPUTS=0 (env kill switch; default ON) or the model
   isn't in the supported set → legacy body from the start; the legacy
   path is preserved unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model support (verified against live docs 2026-07-04) ───────────
# Prefix match so dated snapshots of a supported alias (e.g. a future
# claude-haiku-4-5-2025xxxx) stay supported. NOTE: "claude-sonnet-4-5"
# does NOT prefix-match "claude-sonnet-4-20250514" (Sonnet 4.0, which is
# unsupported), because the 6th char differs ("5" vs "2").
STRUCTURED_OUTPUT_MODEL_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)

# Models the API rejected output_config for at runtime (this process).
# Sticky per-process so a mis-listed model costs ONE wasted request, not
# one per call. Tests reset via reset_runtime_unsupported().
_RUNTIME_UNSUPPORTED: set = set()


def structured_enabled() -> bool:
    """Env kill switch. BRAIN_STRUCTURED_OUTPUTS=0/false/no/off → legacy
    everywhere. Default ON. Read at call time so a Railway env flip takes
    effect without a code change."""
    v = (os.environ.get("BRAIN_STRUCTURED_OUTPUTS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def model_supports_structured(model: str) -> bool:
    m = (model or "").strip().lower()
    if not m or m in _RUNTIME_UNSUPPORTED:
        return False
    return m.startswith(STRUCTURED_OUTPUT_MODEL_PREFIXES)


def structured_active(model: str, schema: Optional[dict]) -> bool:
    """True iff this request should carry output_config.format."""
    return bool(schema) and structured_enabled() and model_supports_structured(model)


def output_config_for(schema: dict) -> dict:
    """The verified GA parameter shape (NOT the deprecated output_format)."""
    return {"format": {"type": "json_schema", "schema": schema}}


def build_messages_body(model: str, system: str, messages: list,
                        max_tokens: int,
                        schema: Optional[dict] = None) -> tuple:
    """Build the /v1/messages JSON body for one raw-HTTP call.

    Returns (body_dict, structured_applied). When structured mode is active
    for this (model, schema), the body carries output_config.format and the
    system prompt has the now-redundant "reply ONLY with JSON" boilerplate
    stripped. Otherwise the body is byte-identical to the legacy one —
    same keys, same untouched system prompt.
    """
    applied = structured_active(model, schema)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": strip_json_only_boilerplate(system) if applied else system,
        "messages": messages,
    }
    if applied:
        body["output_config"] = output_config_for(schema)
    return body, applied


# ── Rejection detection ──────────────────────────────────────────────
_REJECTION_MARKERS = ("output_config", "output_format", "json_schema",
                      "structured output", "structured_outputs")


def looks_like_structured_rejection(status_code: int, body_text: str) -> bool:
    """True when a 400 error plausibly blames the structured-output param
    (unsupported model / invalid schema). Used only to memoize the model as
    runtime-unsupported — the fail-soft retry itself fires on ANY 400 of a
    structured attempt, so an unrecognised error message can never strand a
    call site in a broken structured mode."""
    if status_code != 400:
        return False
    t = (body_text or "").lower()
    return any(m in t for m in _REJECTION_MARKERS)


def mark_model_unsupported(model: str) -> None:
    m = (model or "").strip().lower()
    if m and m not in _RUNTIME_UNSUPPORTED:
        _RUNTIME_UNSUPPORTED.add(m)
        logger.warning(
            "brain_llm_structured: %s rejected output_config — structured "
            "outputs disabled for this model for the process lifetime", m)


def reset_runtime_unsupported() -> None:
    """Test hook."""
    _RUNTIME_UNSUPPORTED.clear()


# ── Prompt boilerplate strip (structured mode only) ──────────────────
# Exact phrases whose ONLY job was "reply with nothing but JSON". With the
# schema enforced natively they are redundant. The legacy path keeps the
# original prompt constants untouched (byte-for-byte).
_JSON_ONLY_PHRASES = (
    # L6 strategic planner, rule 7 (trailing line of the system prompt)
    "\n7. Reply with ONLY the JSON object. No prose before or after.",
    # investigator DECOMPOSE
    ", no prose outside it",
    # feature proposer
    " — no prose outside the JSON",
)


def strip_json_only_boilerplate(system: str) -> str:
    out = system or ""
    for phrase in _JSON_ONLY_PHRASES:
        out = out.replace(phrase, "")
    return out


# ── Parsing ──────────────────────────────────────────────────────────
def parse_structured_json(text: str) -> Optional[dict]:
    """Strict parse for a structured-mode response: the API guarantees the
    text block is valid JSON, so no fence-stripping and no substring
    hunting. Returns None on failure (e.g. stop_reason=="max_tokens"
    truncation) so callers keep their existing degrade path."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
