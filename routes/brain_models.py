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

logger = logging.getLogger(__name__)

# ── Defaults (current public Anthropic lineup, 2026-05-20) ──────────
_DEFAULT_INSPECTOR = "claude-opus-4-7-20251202"
_DEFAULT_REASONING = "claude-opus-4-7-20251202"
_DEFAULT_ROUTINE   = "claude-sonnet-4-5"
_DEFAULT_VOICE     = "claude-haiku-4-5"

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
        "inspector": _DEFAULT_INSPECTOR,
        "reasoning": _DEFAULT_REASONING,
        "routine":   _DEFAULT_ROUTINE,
        "voice":     _DEFAULT_VOICE,
    }.get(tier, _DEFAULT_ROUTINE)


def brain_model_summary() -> dict:
    """For diagnostics: what model is each tier currently using?"""
    return {
        "inspector": brain_model_for("inspector"),
        "reasoning": brain_model_for("reasoning"),
        "routine":   brain_model_for("routine"),
        "voice":     brain_model_for("voice"),
        "_global_fallback_env": _GLOBAL_FALLBACK or None,
    }


def _smoke():
    s = brain_model_summary()
    logger.info(f"[brain-models] inspector={s['inspector']} "
                f"reasoning={s['reasoning']} routine={s['routine']} "
                f"voice={s['voice']}")

_smoke()
