"""
routes/mcp_honest_numbers.py — canonical numbers bridge (2026-07-18).
=====================================================================

routes/mcp_presence_crawler._canonical_numbers() was DESIGNED to read
this module ("the honest-numbers module is the source of truth … so
when it ships, the submitter automatically picks up updates") but it
never shipped — so every auto-submitted registry description rendered
the frozen in-file fallback (tools=33, a number that was stale the day
it was written). The white-glove propagation job (r-white-glove BUILD 1)
would then have PASTED stale copy into the very listings it exists to
fix.

This is that module: a THIN, pure-import bridge to ai_surface_canon
(THE single source of truth). No network, no DB — safe for every
caller including import-time paths. Values are the pinned floors;
consumers that need live-resolved numbers should use
ai_surface_canon.resolve_canon() directly.
"""
from __future__ import annotations

import re


def _floor(public_str) -> int | None:
    """'1,400+' → 1400 · '21,000+' → 21000 · '300+' → 300."""
    try:
        digits = re.sub(r"[^\d]", "", str(public_str or ""))
        return int(digits) if digits else None
    except Exception:
        return None


def as_dict() -> dict:
    """Canonical numbers for description builders / submitters.

    Keys mirror mcp_presence_crawler._CANONICAL_FALLBACK so the merge
    {**fallback, **as_dict()} overrides every stale field. Fail-soft:
    returns {} if the canon import breaks (callers keep their fallback
    rather than crashing)."""
    try:
        from ai_surface_canon import PINNED
    except Exception:
        return {}
    pub = PINNED.get("public") or {}
    out: dict = {}
    tools = PINNED.get("tools_advertised")
    if isinstance(tools, int) and tools > 0:
        out["tools"] = tools
    facilities = _floor(pub.get("facilities"))
    if facilities:
        out["facilities"] = facilities
    markets = _floor(pub.get("markets"))
    if markets:
        out["markets"] = markets
    deals = _floor(pub.get("deals"))
    if deals:
        out["deals"] = deals
        out["deals_phrase"] = f"{pub.get('deals')} tracked deals"
    countries = _floor(pub.get("countries"))
    if countries:
        out["countries"] = countries
        out["countries_phrase"] = f"{pub.get('countries')} countries"
    return out
