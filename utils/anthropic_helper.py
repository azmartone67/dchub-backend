"""
Phase FF+21-aigateway (2026-05-19) — single source of truth for Anthropic
client construction so AI Gateway routing is one env-var flip.

The anthropic Python SDK reads `ANTHROPIC_BASE_URL` from the environment
and uses it as the base URL for all requests. So the CHEAPEST way to
route DC Hub's Claude traffic through Cloudflare AI Gateway is just:

  Railway → Variables → set:
    ANTHROPIC_BASE_URL = https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/anthropic

That alone routes ALL 11 existing `anthropic.Anthropic(...)` call sites
through the gateway WITHOUT any code change. AI Gateway then:
  - Auto-caches identical prompts (brain L8/L14/L22 hit the same
    consistency-radar prompts repeatedly → cache hits = free)
  - Surfaces per-call observability (which prompts are slow / expensive)
  - Adds rate-limit + retry policies at the edge
  - Streams stats to the CF AI Gateway dashboard

This helper exists for callers that want to be EXPLICIT about which
client they're using — useful for tests + for future migration to
prompt-version-aware routing. New callers should prefer
`get_anthropic_client()` over `anthropic.Anthropic(...)` directly so
we have one knob to turn.

Usage:
    from utils.anthropic_helper import get_anthropic_client
    client = get_anthropic_client()
    msg = client.messages.create(model="claude-haiku-4-5", ...)

Env vars consulted (in order of preference):
  ANTHROPIC_BASE_URL          official SDK override — works for ALL callers
  DCHUB_AI_GATEWAY_URL        DC Hub specific name (sugar)
  ANTHROPIC_API_KEY           required as before
"""
import os
import logging

logger = logging.getLogger(__name__)


def get_anthropic_base_url() -> str | None:
    """Resolve the active Anthropic base URL. Returns None if neither
    env var is set, in which case the SDK uses api.anthropic.com directly."""
    return (
        os.environ.get("ANTHROPIC_BASE_URL")
        or os.environ.get("DCHUB_AI_GATEWAY_URL")
        or None
    )


def gateway_active() -> bool:
    """True iff AI Gateway is actually wired (base URL is a CF gateway URL)."""
    url = get_anthropic_base_url() or ""
    return "gateway.ai.cloudflare.com" in url


def get_anthropic_client(api_key: str | None = None, **overrides):
    """Return an anthropic.Anthropic client wired to AI Gateway if configured.

    Pass `api_key=...` to override; otherwise the SDK reads
    ANTHROPIC_API_KEY from env. Any `base_url=...` in **overrides wins
    (useful for tests).
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed. `pip install anthropic`.")
    kwargs = {}
    if api_key is not None:
        kwargs["api_key"] = api_key
    base_url = overrides.pop("base_url", None) or get_anthropic_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(overrides)
    return anthropic.Anthropic(**kwargs)


# ── Module-load diagnostic ──────────────────────────────────────────
def _smoke():
    bu = get_anthropic_base_url()
    if bu and gateway_active():
        logger.info("[ai-gateway] ✅ ACTIVE — routing Anthropic via %s",
                     bu.split("/")[2])
    elif bu:
        logger.info("[ai-gateway] base URL set but not a CF gateway: %s", bu[:60])
    else:
        logger.info("[ai-gateway] ⏸ INACTIVE — set ANTHROPIC_BASE_URL on "
                     "Railway to route through CF AI Gateway. Pattern: "
                     "https://gateway.ai.cloudflare.com/v1/<account>/"
                     "<gateway>/anthropic")


_smoke()
