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


def anthropic_messages_url() -> str:
    """The /v1/messages endpoint for RAW HTTP callers (requests/urllib/httpx).

    The 20+ raw call sites across the brain layers + press + market-brief
    hardcoded "https://api.anthropic.com/v1/messages", which BYPASSES the
    AI Gateway even when ANTHROPIC_BASE_URL is set (only the SDK reads that
    env var). Route them through this helper so a single env-var flip sends
    ALL of DC Hub's Claude traffic — SDK and raw — through the gateway.

    Returns the gateway messages URL when configured, else Anthropic direct.
    No behavior change until ANTHROPIC_BASE_URL / DCHUB_AI_GATEWAY_URL is set."""
    base = (get_anthropic_base_url() or "https://api.anthropic.com").rstrip("/")
    return base + "/v1/messages"


def aig_metadata_headers(component: str, *, cache_ttl: int | None = None) -> dict:
    """Cloudflare AI Gateway request headers for cost attribution + caching.

    - cf-aig-metadata: JSON tagging the request with a component (e.g.
      "brain-reasoning", "press", "market-brief") so the AI Gateway dashboard
      and spend limits can attribute cost per workload.
    - cf-aig-cache-ttl: when cache_ttl is given, lets the gateway serve
      identical prompts from cache (the brain hits the same radar/critique
      prompts repeatedly → cache hits are free).

    No-ops (returns {}) when the gateway isn't active, so adding these to
    every call site is safe whether or not the gateway is configured."""
    if not gateway_active():
        return {}
    import json as _json
    headers = {"cf-aig-metadata": _json.dumps(
        {"component": component, "app": "dchub"}, separators=(",", ":"))}
    if cache_ttl and cache_ttl > 0:
        headers["cf-aig-cache-ttl"] = str(int(cache_ttl))
    return headers


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


def get_status() -> dict:
    """Queryable AI-Gateway status — for an admin endpoint, the brain's
    self-model, or a quick health check. Never raises."""
    bu = get_anthropic_base_url()
    return {
        "active": gateway_active(),
        "base_url_host": (bu.split("/")[2] if bu and "//" in bu else None),
        "raw_calls_routed": True,   # all raw sites use anthropic_messages_url()
        "sdk_calls_routed": bool(bu),
        "attribution": "cf-aig-metadata per component (brain-reasoning tagged)",
        "how_to_enable": ("set ANTHROPIC_BASE_URL on Railway to "
                          "https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/anthropic"),
    }


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
