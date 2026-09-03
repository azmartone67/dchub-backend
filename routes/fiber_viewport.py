"""Pure helpers for the fiber-routes viewport and carrier filters (2026-09-03).

Extracted from main.py so they can be tested without a database, a network or
an import of main.py (tests/ house rules). main.py holds the SQL and the
connection; everything here is a total function over its arguments.

Background — two measured defects behind "FiberLocator shows carriers at this
address and we don't", neither of which was a shortage of fiber:

  1. ?bbox= filtered on the route's START POINT, so a route crossing the
     viewport without beginning in it was dropped. At 2675 Olthoff Dr,
     Muskegon MI: 21 routes' geometry passes through the viewport, the
     start-point test kept 16, and the 5 dropped included the only Zayo route
     reaching Muskegon.  (Fixed in SQL + migrations/2026-09-03_*.sql.)

  2. ?carrier= was an exact `provider = %s` against a column holding the same
     carrier under several spellings, so it answered for one of them.
     (Fixed by match_carriers below.)
"""
from __future__ import annotations

import re

__all__ = ["norm_carrier", "match_carriers", "thin_coords", "thinning_stride"]

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# A query shorter than this is never allowed to match by PREFIX — only
# exactly. Two characters ("US", "GT") would otherwise sweep in unrelated
# carriers wholesale.
MIN_PREFIX_LEN = 3


def norm_carrier(s) -> str:
    """Fold a carrier name to its comparable core: lowercase, alphanumerics
    only.

    >>> norm_carrier("123NET") == norm_carrier("123Net")
    True
    >>> norm_carrier("GTT Communications (AS3257)")
    'gttcommunicationsas3257'
    """
    return _NON_ALNUM.sub("", str(s or "").lower())


def match_carriers(wanted, known):
    """Every name in `known` that means the carrier `wanted`.

    MEASURED in fiber_routes 2026-09-03 — one carrier, two spellings:
        '123Net' (8 routes)  vs '123NET' (7)
        'Cogent' (115)       vs 'Cogent Communications, Inc.' (11)
        'GTT' (74)           vs 'GTT Communications (AS3257)' (14)
        'Windstream' (74)    vs 'Windstream Wholesale' (14)
    `provider = '123Net'` returned 8 of 15 with no signal to the caller.

    Rule: normalized equality, OR the stored name normalizes to something
    STARTING with the normalized query (query >= MIN_PREFIX_LEN).

    Prefix, deliberately, NOT substring: 'uniti' must not pull in
    'Unitas Global'. Normalized prefix keeps them apart because 'uniti' is not
    a prefix of 'unitasglobal', whereas a substring test on 'unit' would fold
    two unrelated carriers into one answer.

    Returns [] when nothing matches — the caller treats that as "unknown
    carrier" and returns an empty result rather than an unfiltered one.
    Order follows `known` so the output is stable for a stable input.
    """
    key = norm_carrier(wanted)
    if not key:
        return []
    out = []
    for name in known:
        n = norm_carrier(name)
        if not n:
            continue
        if n == key or (len(key) >= MIN_PREFIX_LEN and n.startswith(key)):
            out.append(name)
    return out


def thinning_stride(total_vertices: int, budget: int) -> int:
    """Stride that brings `total_vertices` under `budget`. 1 = no thinning.

    Guards budget <= 0 rather than dividing by it: a misconfigured budget must
    not raise inside a response builder.
    """
    if budget <= 0 or total_vertices <= budget:
        return 1
    return int(total_vertices // budget) + 1


def thin_coords(coords, stride: int):
    """Keep every `stride`-th vertex, ALWAYS preserving the first and last.

    Used when a response exceeds its vertex budget. The budget spends its
    bytes on route PRESENCE, not detail: a thinned route still answers "does
    this carrier reach this site", whereas a dropped route asserts — falsely —
    that it does not. So this thins geometry and never removes a route.

    A 2-point line is returned unchanged (there is nothing to thin), and the
    result is never shorter than 2 points.
    """
    if stride <= 1 or not coords or len(coords) <= 2:
        return coords
    out = coords[::stride]
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    if len(out) < 2:
        out = [coords[0], coords[-1]]
    return out
