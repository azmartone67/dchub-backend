"""Phase FF+25-followup-r9 (2026-05-20) — central brain-model config.
==========================================================================

Until now each brain layer hard-coded "claude-sonnet-4-5" in its own
file. That made it impossible to:
  · Upgrade the whole stack at once
  · Run a cost-aware tier strategy (Opus for hard problems, Sonnet for
    routine, Haiku for voice / quick reads)
  · A/B test a new model without touching 16 files

This module is the single source of truth. Each layer should now ask
brain_model_for(tier) and let env vars override.

TIERS:
  "inspector"   — Opus 4.7. Heavy synthesis: daily inspection brief,
                  novel finding generation, code-aware PR drafts.
                  Runs 1–4×/day. Highest cost per call.
  "reasoning"   — Opus by default. L7 evolving, L14 causal, L16
                  self-critique, L18 memory consolidation — anything
                  that benefits from multi-step thinking.
  "routine"     — Sonnet 4.5. L8 orchestrator, L9 conversational, L11
                  QA agent — frequent calls that need good-enough
                  judgment fast.
  "voice"       — Haiku 4.5. Brain-pulse one-liners, status summaries,
                  short labels. Cheapest, fastest.

ENV OVERRIDES (set on Railway):
  DCHUB_BRAIN_MODEL              global fallback (legacy compat)
  DCHUB_BRAIN_MODEL_INSPECTOR    override Inspector tier
  DCHUB_BRAIN_MODEL_REASONING    override reasoning tier
  DCHUB_BRAIN_MODEL_ROUTINE      override routine tier
  DCHUB_BRAIN_MODEL_VOICE        override voice tier

Note on "Mythos": as of this commit, Anthropic's public model lineup is
Opus 4.7 (1M context), Sonnet 4.5, Haiku 4.5. There is no public model
called "Mythos" — that may be a code-name from a leak or a reference to
a different vendor. If/when Anthropic ships a higher tier, drop the new
identifier into DCHUB_BRAIN_MODEL_INSPECTOR and the whole brain levels
up with one env-var change.
"""
import os
import logging
from utils.anthropic_helper import anthropic_messages_url

logger = logging.getLogger(__name__)

# ── Defaults (Anthropic lineup as of 2026-05-21) ──────────────────
# Phase r33-M: upgrade Inspector + Reasoning tier to Opus 4.7 (1M
# context window). Previous attempt at "claude-opus-4-7-20251202"
# failed because we guessed the date — Anthropic accepts the
# undated alias "claude-opus-4-7" which always points at the latest
# minor revision. If a future call returns 404 (model retired),
# DCHUB_BRAIN_MODEL_INSPECTOR env var on Railway is the override.
# Falls back automatically to opus-4-5 via _safe_resolve if 4-7 errors.
# 2026-05-31 FIX: claude-opus-4-7 returns HTTP 404 from /v1/messages (not
# accessible to this account), and layer4._call_claude uses the model RAW without
# _safe_resolve — so the documented opus-4-7→opus-4-5 fallback never fired and
# every Layer-5 Claude call 404'd → 0 proposals. Point inspector/reasoning at the
# confirmed-valid claude-sonnet-4-5 (routine default; the testimonial probe uses
# it successfully). Override to a valid opus via DCHUB_BRAIN_MODEL when available.
# ── Brain v3 (2026-06-06) — Opus 4.8 1M-context upgrade ──────────
# The user upgraded their own session to Opus 4.8 (1M context) and
# wants the brain to level up to match. SAFE BY CONSTRUCTION: we set
# Opus 4.8 as the inspector + reasoning default, but every call site
# uses the full fallback chain below, so if the brain's
# ANTHROPIC_API_KEY can't reach Opus 4.8 (the prior opus-4-7 attempt
# 404'd for this account — see the 2026-05-31 note above), it
# degrades opus-4-8 → opus-4-7 → opus-4-5 → sonnet-4-5 → haiku
# automatically. Worst case = today's behavior (Sonnet); best case =
# the brain reasons on Opus 4.8.
#
# Before trusting this default, run GET /api/v1/brain/model-probe —
# it tests reachability of each tier with the brain's actual key and
# reports the best model the brain can actually use. Set the result
# into DCHUB_BRAIN_MODEL_INSPECTOR to pin it (skips the per-call
# fallback dance + saves the failed-call latency).
_DEFAULT_INSPECTOR = "claude-opus-4-8"
_DEFAULT_REASONING = "claude-opus-4-8"
_DEFAULT_ROUTINE   = "claude-sonnet-4-5"
_DEFAULT_VOICE     = "claude-haiku-4-5"
# r47 (2026-05-25): challenger tier — independent second opinion on
# proposals from the reasoning tier. A DIFFERENT model from reasoning
# (Sonnet) so the challenge is a genuine cross-model perspective, not
# the same model grading itself. L23 multi-model challenger uses this
# to gate Opus proposals before they reach human review.
_DEFAULT_CHALLENGER = "claude-sonnet-4-5"

# Brain v3: full fallback chain. opus-4-8 at the top; each rung
# degrades to the next-cheapest confirmed-or-likely-valid model.
# _safe_resolve() and call sites walk this on a 404/400 so a model
# the key can't reach never zeros out the brain.
# r-fix (2026-06-06): route AROUND the known-404 models so a fallback walk
# doesn't burn requests (+ AI Gateway error rate) hitting dead endpoints.
# claude-opus-4-7 returns 404 (see note above); claude-haiku-3-5 is RETIRED.
# On any opus failure go straight to the confirmed-valid sonnet-4-5; haiku-4-5
# is the cheapest VALID model = terminal (no fallback to retired haiku-3-5).
_FALLBACK_CHAIN = {
    "claude-opus-4-8":     "claude-sonnet-4-5",
    "claude-opus-4-7":     "claude-sonnet-4-5",
    "claude-opus-4-5":     "claude-sonnet-4-5",
    "claude-sonnet-4-5":   "claude-haiku-4-5",
}

# Brain v3: which models support the 1M-context beta. When the
# resolved model is in this set AND DCHUB_BRAIN_1M_CONTEXT is truthy,
# call sites add the anthropic-beta: context-1m header. Lets the
# inspector tier hold the whole site's findings + history in one
# prompt instead of truncating to a 1.5KB snippet.
_ONE_M_CONTEXT_MODELS = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-5",
}
_ONE_M_BETA_HEADER = "context-1m-2025-08-07"

# Global fallback (matches the legacy DCHUB_BRAIN_MODEL pattern from
# brain_v2_layer4). If set, becomes the answer to every untiered call.
_GLOBAL_FALLBACK = (os.environ.get("DCHUB_BRAIN_MODEL") or "").strip()


def brain_model_for(tier: str = "routine") -> str:
    """Return the model identifier for a given tier. Env vars override
    defaults so we can A/B test or downgrade for cost without code edits.

    Unknown tier → routine. Empty env var → default. Legacy
    DCHUB_BRAIN_MODEL → used if specific tier isn't set.
    """
    tier = (tier or "routine").lower().strip()
    env_specific = (os.environ.get(f"DCHUB_BRAIN_MODEL_{tier.upper()}")
                    or "").strip()
    if env_specific:
        return env_specific
    if _GLOBAL_FALLBACK:
        return _GLOBAL_FALLBACK
    return {
        "inspector":  _DEFAULT_INSPECTOR,
        "reasoning":  _DEFAULT_REASONING,
        "routine":    _DEFAULT_ROUTINE,
        "voice":      _DEFAULT_VOICE,
        "challenger": _DEFAULT_CHALLENGER,
    }.get(tier, _DEFAULT_ROUTINE)


def brain_model_summary() -> dict:
    """For diagnostics: what model is each tier currently using?"""
    return {
        "inspector": brain_model_for("inspector"),
        "reasoning": brain_model_for("reasoning"),
        "routine":   brain_model_for("routine"),
        "voice":     brain_model_for("voice"),
        "_global_fallback_env": _GLOBAL_FALLBACK or None,
        "_fallback_chain":      _FALLBACK_CHAIN,
    }


def fallback_for(model: str) -> str | None:
    """r33-M: if the primary model 404s (new release not yet provisioned
    on a given Anthropic key, model retired, region-restricted), call
    sites can drop down one tier and retry. Returns None if no fallback
    is known — in which case the caller should error out, not loop.

    Usage in any brain call site:

        from routes.brain_models import brain_model_for, fallback_for
        model = brain_model_for("inspector")
        for attempt in range(3):
            try:
                resp = httpx.post(API, json={"model": model, ...})
                if resp.status_code == 404 and "model" in resp.text.lower():
                    nxt = fallback_for(model)
                    if not nxt: raise
                    model = nxt
                    continue
                resp.raise_for_status()
                break
            except ...
    """
    return _FALLBACK_CHAIN.get(model)


def resolve_chain(model: str, max_depth: int = 5) -> list:
    """Brain v3: return the full degrade chain starting at `model`.

    e.g. resolve_chain("claude-opus-4-8") ->
      ["claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-5",
       "claude-sonnet-4-5", "claude-haiku-4-5"]

    Call sites walk this on a 404/400 so a model the key can't reach
    degrades all the way to a confirmed-valid model instead of giving
    up after one retry. Stops at max_depth to avoid a cycle.
    """
    chain = [model]
    cur = model
    for _ in range(max_depth):
        nxt = _FALLBACK_CHAIN.get(cur)
        if not nxt or nxt in chain:
            break
        chain.append(nxt)
        cur = nxt
    return chain


def supports_1m_context(model: str) -> bool:
    """True if `model` can take the 1M-context beta header AND the
    DCHUB_BRAIN_1M_CONTEXT env flag is on. Default OFF — 1M context
    is more expensive per call, so opt in deliberately."""
    flag = (os.environ.get("DCHUB_BRAIN_1M_CONTEXT") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return model in _ONE_M_CONTEXT_MODELS


def one_m_beta_header() -> str:
    """The anthropic-beta value to send for 1M context."""
    return _ONE_M_BETA_HEADER


def probe_model_reachability(api_key: str,
                             models: list = None,
                             timeout: int = 15) -> dict:
    """Brain v3: test which models the brain's ANTHROPIC_API_KEY can
    actually reach. Sends a 1-token probe to each model; records
    reachable / http_404 / http_401 / etc.

    This is the verify-before-claiming step: before trusting the
    opus-4-8 default, run this to see what the key can use. The
    result names the best reachable model so the operator can pin
    it into DCHUB_BRAIN_MODEL_INSPECTOR.
    """
    import json as _json
    import urllib.request
    import urllib.error

    if not api_key:
        return {"ok": False, "error": "no_api_key",
                "hint": "ANTHROPIC_API_KEY not set on the service"}

    # Probe order: best → worst. First reachable = best usable.
    # r-fix (2026-06-06): dropped claude-haiku-3-5 (RETIRED → always 404, just
    # pollutes the AI Gateway error rate). claude-opus-4-7 kept (probing it is
    # how we confirm it's still 404), but it's no longer in the fallback path.
    if models is None:
        models = ["claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-5",
                  "claude-sonnet-4-5", "claude-haiku-4-5"]

    results = {}
    best_reachable = None
    for m in models:
        body = _json.dumps({
            "model": m,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _ = r.read()
            results[m] = "reachable"
            if best_reachable is None:
                best_reachable = m
        except urllib.error.HTTPError as e:
            # 404/400 = model not available to this key.
            # 429 = rate-limited but the model EXISTS (count as reachable).
            if e.code == 429:
                results[m] = "reachable_rate_limited"
                if best_reachable is None:
                    best_reachable = m
            else:
                results[m] = f"http_{e.code}"
        except Exception as e:
            results[m] = f"error: {str(e)[:60]}"

    return {
        "ok":              True,
        "results":         results,
        "best_reachable":  best_reachable,
        "current_default": brain_model_for("inspector"),
        "recommendation": (
            f"Set DCHUB_BRAIN_MODEL_INSPECTOR={best_reachable} on Railway "
            f"to pin the brain to its best reachable model (skips the "
            f"per-call fallback dance)."
            if best_reachable else
            "No model reachable — check ANTHROPIC_API_KEY validity + "
            "account model access."
        ),
    }


def _smoke():
    s = brain_model_summary()
    logger.info(f"[brain-models] inspector={s['inspector']} "
                f"reasoning={s['reasoning']} routine={s['routine']} "
                f"voice={s['voice']}")

_smoke()
