"""One rule for "does this surface quote the right facility floor".

WHY THIS MODULE EXISTS
----------------------
Four guards asked that question and answered it four different ways. On
2026-08-31 that produced three separate false REDs on the same healthy files:

    surface_truth_master_shell   banned the RANGE 19,000-23,999. Once PINNED
                                 reached 18,500+, the live-healed floor 19,700+
                                 sat inside both the accept band AND the ban, so
                                 every surface passed "carries canon floor" and
                                 failed "free of retired floors" on the same
                                 bytes. Three lanes red.
    loop_control_master_shell    compared surfaces against the LIVE count and
                                 called anything >500 behind stale. Live moves
                                 continuously; the files carry PINNED, bumped
                                 periodically. Permanently red for no defect.
    seven_levers_master_shell    a COPY of the same rotted range regex, plus an
                                 exact `canon_floor not in body`. A card at
                                 19,900+ was flagged retired AND missing canon.
    intelligence_expansion_...   the same copy, the same exact-match.

Every one of them was individually reasonable and collectively incoherent. A
file cannot be simultaneously correct and stale, and when two guards disagree
about one byte-sequence, the one that goes red more often is the one people stop
reading.

THE RULE, stated once
---------------------
A published floor is a FLOOR: it rounds down, it is safe to hardcode, and it is
allowed to lag the live count. So:

    acceptable   PINNED itself, or any comma-formatted "N+" in
                 [PINNED, PINNED x ACCEPT_MAX_MULT]. A heal-bound surface
                 carrying the live floor is correct, not stale.
    retired      an explicit historical literal, or an over-claim in
                 (accept ceiling, PINNED x OVERCLAIM_MAX_MULT].
    neither      anything above the over-claim ceiling. Those are DIFFERENT
                 QUANTITIES — 126k substations, 182k power units, 330k mapped
                 assets — and flagging them was the 2026-08-31 regression.

Both bounds are canon-relative, so the rule cannot rot as the fleet grows. A
retired LITERAL stays wrong forever and is safe to hardcode; a retired RANGE is
not, which is what scripts/accuracy_fence.py learned when `[2-9],\\d{3} deals`
froze dchub-frontend for 19 deploys the hour deals_tracked passed 2,000.
"""

from __future__ import annotations

import re

# Any comma-formatted "N+" token, e.g. "18,500+".
FLOOR_TOKEN = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\+")

# A heal-bound surface may carry the LIVE floor while PINNED lags. 10% tracks
# realistic pin-to-live drift and still rejects the legacy over-claims.
ACCEPT_MAX_MULT = 1.10

# Above the accept ceiling but still facility-scale = an over-claim worth
# retiring. Beyond it, the number is a different quantity entirely.
OVERCLAIM_MAX_MULT = 3.0

# Permanently-wrong literals. Safe to hardcode BECAUSE they are literals:
# 12,650+ was canon itself from 2026-07-24 to 07-28 and can never be right again.
RETIRED_LITERALS = ("12,650+",)


def _base(canon) -> int | None:
    """PINNED as an int, or None when it cannot be read."""
    try:
        return int(str(canon).replace(",", "").rstrip("+")) or None
    except (TypeError, ValueError):
        return None


def acceptable_floor(body: str, canon) -> str | None:
    """The canon-family floor `body` carries, or None.

    Accepts the PINNED phrase itself, or any floor within the accept band —
    so a heal-bound page quoting the live count is not reported stale."""
    if not body or not canon:
        return None
    if str(canon) in body:
        return str(canon)
    base = _base(canon)
    if base is None:
        return None
    hi = int(base * ACCEPT_MAX_MULT)
    for m in FLOOR_TOKEN.finditer(body):
        v = int(m.group(1).replace(",", ""))
        if base <= v <= hi:
            return m.group(0)
    return None


def retired_floors(body: str, canon) -> list[str] | None:
    """Floors in `body` that are RETIRED. None when canon is unknown.

    None means INDETERMINATE, never clean: a fence that cannot resolve canon
    must not certify a page. Returning [] there would read as PASS, which is
    the fail-open direction."""
    text = body or ""
    retired = {lit for lit in RETIRED_LITERALS if lit in text}
    base = _base(canon)
    if base is None:
        return None
    hi = int(base * ACCEPT_MAX_MULT)
    over_hi = int(base * OVERCLAIM_MAX_MULT)
    for m in FLOOR_TOKEN.finditer(text):
        v = int(m.group(1).replace(",", ""))
        if hi < v <= over_hi:
            retired.add(m.group(0))
    return sorted(retired)


def floor_verdict(body: str, canon) -> dict:
    """Both halves at once, for a guard that reports them together.

    {ok, found, retired} — ok is True only when a canon-family floor is present
    AND nothing retired is. `retired is None` (canon unreadable) makes ok None,
    not False: indeterminate is its own answer."""
    found = acceptable_floor(body, canon)
    retired = retired_floors(body, canon)
    if retired is None:
        return {"ok": None, "found": found, "retired": None}
    return {"ok": bool(found) and not retired, "found": found,
            "retired": retired}
