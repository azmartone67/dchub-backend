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
               (claude-opus-4-8), claude-sonnet-4-0
               (claude-sonnet-5), retired haiku-3.x / 3.x models.
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

from utils.anthropic_helper import cached_system

logger = logging.getLogger(__name__)

# ── Model support (verified against live docs 2026-07-04) ───────────
# Prefix match so dated snapshots of a supported alias (e.g. a future
# claude-haiku-4-5-2025xxxx) stay supported. NOTE: "claude-sonnet-4-5"
# does NOT prefix-match "claude-sonnet-4-0" (Sonnet 4.0, which is
# unsupported), because the 6th char differs ("5" vs "0").
# ★2026-07-25: this comment had been rewritten to say "claude-sonnet-5" by a
# blanket model-ID migration, which inverted its meaning — Sonnet 5 IS in the
# supported tuple below. The tuple itself was correct; only the prose broke.
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
                        schema: Optional[dict] = None,
                        cache_system: bool = True) -> tuple:
    """Build the /v1/messages JSON body for one raw-HTTP call.

    Returns (body_dict, structured_applied). When structured mode is active
    for this (model, schema), the body carries output_config.format and the
    system prompt has the now-redundant "reply ONLY with JSON" boilerplate
    stripped.

    Prompt caching (2026-07-23): the (static) brain system prompt is wrapped in
    a `cache_control: ephemeral` block via cached_system() so its prefix is cached
    (0.1x input price on reads) instead of reprocessed at full price on every
    call. The brain layers hit the same charter/critique prompts repeatedly, so
    this is a large share of the low-cache-hit-rate spend. Pass
    cache_system=False for a caller whose system prompt genuinely changes every
    request (a changing prefix pays a wasted cache WRITE each call — see
    CACHING.md). cached_system() is a no-op on non-str / already-cached input, so
    callers that pre-wrap their own system (e.g. brain_lane_driver) are unaffected.
    """
    applied = structured_active(model, schema)
    sys_val = strip_json_only_boilerplate(system) if applied else system
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": cached_system(sys_val) if cache_system else sys_val,
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


# ── Usage / cache telemetry (shell #31, 2026-07-25) ──────────────────
# cached_system() shipped 2026-07-23 on the assumption it would lift the
# cache hit-rate, but nothing MEASURES whether the cache actually hits — the
# API reports it on every response (usage.cache_read_input_tokens) and the
# call sites just drop it. This recorder lands that evidence in
# brain_llm_usage so the Intelligence Expansion shell can report a real
# hit-rate instead of an assumption. Best-effort by contract: no DB, no
# usage block, any error → silent no-op. NEVER raises into a brain call.
_USAGE_DDL_DONE = False


def record_llm_usage(component: str, model: str, resp_json) -> None:
    global _USAGE_DDL_DONE
    try:
        usage = (resp_json or {}).get("usage") or {}
        if not usage:
            return
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "").strip()
        if not url:
            return
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                if not _USAGE_DDL_DONE:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS brain_llm_usage ("
                        " id BIGSERIAL PRIMARY KEY,"
                        " ts TIMESTAMPTZ DEFAULT NOW(),"
                        " component TEXT,"
                        " model TEXT,"
                        " input_tokens BIGINT,"
                        " output_tokens BIGINT,"
                        " cache_read_tokens BIGINT,"
                        " cache_write_tokens BIGINT)")
                    _USAGE_DDL_DONE = True
                cur.execute(
                    "INSERT INTO brain_llm_usage (component, model,"
                    " input_tokens, output_tokens, cache_read_tokens,"
                    " cache_write_tokens) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING"
                    " ON CONFLICT DO NOTHING",
                    (str(component)[:80], str(model)[:80],
                     usage.get("input_tokens"),
                     usage.get("output_tokens"),
                     usage.get("cache_read_input_tokens"),
                     usage.get("cache_creation_input_tokens")))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001 — telemetry must never break a call
        logger.debug("record_llm_usage swallowed: %s", e)
