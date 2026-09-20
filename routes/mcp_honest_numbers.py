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

This is that module: a THIN bridge to ai_surface_canon (THE single
source of truth). Two doors, because its consumers want opposite things:

  as_dict()        PUBLISHERS. The floors ai_surface_canon actually
                   stands behind right now — PINNED with the live
                   overlay applied, via resolve_public_floors_cached().
  as_dict_pinned() AUDITORS. The hand-typed PINNED literals, unresolved,
                   because a detector that convicts PINNED of standing
                   above live reality needs the pin as its operand.

★2026-09-19: as_dict() used to return PINNED verbatim, so the module
whose name promises honest numbers published the cold-start literal to
every registry description no matter how well the resolver healed.

★ NEVER resolve_canon() OR resolve_public_floors() HERE. resolve_canon()
DEGRADES rather than raising (observed returning facilities="400+"
against a pinned "18,500+"), and resolve_public_floors() probes live
HTTP per call — measured 7.59s / 7.78s / 15.46s against a 15s edge
timeout. resolve_public_floors_cached() answers from cache, refreshes in
the background, only ever RAISES a floor, and never raises or blocks. It
is the only sanctioned door; see its docstring in ai_surface_canon.

No DB, and no blocking call — but as_dict() may start ONE background
refresh thread, so it is no longer free at import time. Do not call it
at module level: you would freeze the cold-start value at boot and make
it look live. Call it per request / per job. as_dict_pinned() stays
pure-import and thread-free.
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


def _build(pub: dict, tools) -> dict:
    """Shape the canon dict from one already-chosen floors mapping.

    `pub` is whatever the caller resolved — pinned or overlaid. Only the
    four public floor keys are read, so resolve_public_floors_cached()'s
    `_source` / `_rejected` / `_cold` metadata never leaks into copy.
    A key with no digits is OMITTED, never published as 0."""
    out: dict = {}
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


def as_dict() -> dict:
    """Canonical numbers for description builders / submitters.

    The RESOLVED floors: PINNED with the live overlay applied where it
    RAISES, through resolve_public_floors_cached(). Keys mirror
    mcp_presence_crawler._CANONICAL_FALLBACK so the merge
    {**fallback, **as_dict()} overrides every stale field.

    `tools` is NOT a floor — PINNED['tools_advertised'] is hand-walked
    by design and has no resolver, so it is read straight from PINNED.

    Fail-soft twice over: {} if the canon import breaks (callers keep
    their fallback rather than crashing), and the pinned floors if the
    resolver gives nothing back. A pin under-states; it never points the
    wrong way."""
    try:
        from ai_surface_canon import PINNED, resolve_public_floors_cached
    except Exception:
        return {}
    try:
        resolved = resolve_public_floors_cached() or {}
    except Exception:          # documented never to raise; belt and braces
        resolved = {}
    pub = {**(PINNED.get("public") or {}), **resolved}
    return _build(pub, PINNED.get("tools_advertised"))


def as_dict_pinned() -> dict:
    """as_dict() WITHOUT the live overlay — the hand-typed PINNED floors.

    ★ FOR AUDITORS, not publishers. brain_consistency_radar's
    check_canonical_floor_exceeds_live() exists to convict
    ai_surface_canon.PINNED['public'] of standing above live reality,
    and its finding names that literal and tells a human to lower it.
    Hand it a resolved floor and the number in the remediation stops
    being the number on the page — and the overlay only ever RAISES, so
    a resolved floor makes the detector convict a pin that is innocent.

    Pure import: no network, no DB, no thread. Safe at import time."""
    try:
        from ai_surface_canon import PINNED
    except Exception:
        return {}
    return _build(PINNED.get("public") or {}, PINNED.get("tools_advertised"))
