"""media_cta.py — one canonical reach CTA for every generated post body.

Item 8 (2026-06-30): every auto-generated press / social post should end with a
single line that tells the reader's AI agent how to connect to DC Hub's live
data — a free key in one call, and the $10 = 1,000-calls unlock. Historically
each composer ended with its own byline / hashtags and NO connect line, so a
reader who wanted the data had no next step. This module gives all composers ONE
shared string so the CTA never drifts between surfaces.

Design goals:
  * SINGLE SOURCE — every composer imports REACH_CTA / append_reach_cta from here
    so the wording is identical across LinkedIn, X, Bluesky and press bodies.
  * IDEMPOTENT — append_reach_cta() is a no-op if the CTA (or its short form) is
    already in the text, so a re-render or a composer that already appended it
    never double-stamps.
  * FAIL-SOFT — pure string helpers, no I/O, no imports that can fail;
    distribution must never block on the CTA.
  * CHAR-AWARE — X/Twitter + Bluesky are length-capped, so a short form is
    provided and append_reach_cta(short=True) only adds it when it fits.

The tracking counters (press_engagement.event_type IN ('click_out',
'stripe_click'), populated by GET /api/v1/marketing/track) already exist; the
CTA points at the same public /mcp surface those funnels measure, so click-outs
remain attributable via the existing pixel/redirect paths — no new schema.
"""
from __future__ import annotations

# Full CTA — long-form surfaces (LinkedIn long-form, press bodies). Verbatim per
# the item-8 spec so it reads identically wherever it lands. Uses an em dash +
# middot to match the house post style already used across the composers.
REACH_CTA = (
    "Connect your AI agent: dchub.cloud/mcp — free, call claim_free_key "
    "· $10 = 1,000 calls"
)

# Short form — X/Twitter (280) + Bluesky (300) are char-capped, so the full CTA
# would push an already-full tweet over the limit. Keep the connect surface +
# "free" so the ask still lands.
REACH_CTA_SHORT = "Connect your AI agent → dchub.cloud/mcp (free)"


def _has_cta(text: str) -> bool:
    """True if the reach CTA (full or short) is already present, so we never
    double-stamp on a re-render. Cheap substring test on the stable anchor."""
    if not text:
        return False
    return ("dchub.cloud/mcp" in text and
            ("claim_free_key" in text or "Connect your AI agent" in text))


def append_reach_cta(text: str, short: bool = False,
                     max_chars: int | None = None) -> str:
    """Return `text` with the reach CTA appended on its own line.

    * No-op (returns text unchanged) if the CTA is already present.
    * `short=True` uses the compact form for char-capped platforms.
    * `max_chars` (e.g. 280 for X, 300 for Bluesky): if adding the CTA would
      exceed it, the CTA is skipped rather than truncated — a half-CTA is worse
      than none, and the post still ships. Distribution never blocks on the CTA.
    """
    if text is None:
        text = ""
    if _has_cta(text):
        return text
    cta = REACH_CTA_SHORT if short else REACH_CTA
    joined = text.rstrip() + "\n\n" + cta if text.strip() else cta
    if max_chars is not None and len(joined) > max_chars:
        # Try the short form if we were about to use the long one and it fits.
        if not short:
            short_joined = text.rstrip() + "\n\n" + REACH_CTA_SHORT
            if len(short_joined) <= max_chars:
                return short_joined
        return text  # doesn't fit — ship without the CTA rather than truncate.
    return joined
