"""Capacity-heatmap paywall redaction — allow-list, not block-list.

WHY THIS MODULE EXISTS
──────────────────────
`capacity_heatmap_public()` in main.py shipped a redaction loop that nulled
these keys for non-paid callers:

    readiness.score, grid.spare_capacity_pct, grid.spare_capacity_mw,
    gas.headroom_mdth, power.local_capacity_mw, power.local_plants,
    cost.electricity_rate_cents_kwh

Every one of those is a NESTED key. The payload it was applied to —
`CAPACITY_HEATMAP_MARKETS` — is FLAT:

    {"name": ..., "lat": ..., "lng": ...,
     "capacity_mw": 4500, "utilization": 78, "growth": 12}

So `isinstance(_m.get(_sect), dict)` was False on every entry and the loop
stripped nothing. The gated response differed from the paid one by exactly
two booleans (`locked`, `_gated`); all 24 numeric values (8 markets x 3
fields) were served to non-paid callers verbatim. The nested key names are a
copy of the redaction in routes/energy_discovery_routes.py, where the payload
genuinely does have those sections — the recipe was carried across to a
surface with a different schema.

This is the same defect family as reference_dchub_capacity_paywall_gating.md
and reference_dchub_anon_bulk_exposure_0801.md ("map_tier_gating DEAD"): a
gate written against a schema the data does not have.

THE FIX IS THE DIRECTION, NOT THE KEY NAMES
───────────────────────────────────────────
Swapping the nested names for today's flat names (capacity_mw, utilization,
growth) would reproduce the same fragility the day the fixture gains a field —
a block-list only redacts what someone remembered to list. So this redacts by
ALLOW-LIST: name what a non-paid caller may keep, and null every other numeric
value, at any nesting depth. A new paid field is redacted by default.

`redact_markets` returns the number of values it actually nulled. Callers and
tests assert on it: a redaction that reports 0 on a non-empty payload is the
no-op bug recurring, and is now observable instead of silent.

The free surface matches the tier design already documented in
map_tier_gating._strip_heatmap ("Free: name + lat/lng only (no
capacity/utilization/growth)").
"""
from __future__ import annotations

# What a non-paid caller keeps: WHERE the market is, plus the traffic-light
# teaser. `lat`/`lng` are numeric and deliberately free — without them the map
# cannot render a dot, and a coordinate is not the paid product.
FREE_KEYS = frozenset({
    "name", "market", "slug", "id", "tier",
    "lat", "lng", "latitude", "longitude",
    "grade", "label", "signal", "color", "locked",
})

# The fixture is not a measurement. Stated explicitly rather than left to be
# inferred from a payload that otherwise looks live. `as_of` is None on
# purpose: the values carry no observation date, and inventing one would be
# the exact dishonesty this block exists to prevent.
CAPACITY_HEATMAP_PROVENANCE = {
    "source": "static_fixture",
    "live": False,
    "as_of": None,
    "note": (
        "Illustrative hardcoded market fixture for map rendering — NOT a live "
        "measurement and not derived from DCPI. No live per-market utilization "
        "or growth source exists in this repo. Do not cite these values."
    ),
}


def _is_number(v) -> bool:
    """bool is a subclass of int — `locked: True` must not be nulled to None."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def redact_entry(entry: dict) -> int:
    """Null every non-free numeric value in `entry`, in place, recursively.

    Returns the count of values nulled. Lists of dicts (e.g. a nested
    `plants: [...]`) are walked too, so a paid number cannot hide one level
    inside a list.
    """
    nulled = 0
    for key, value in list(entry.items()):
        if isinstance(value, dict):
            nulled += redact_entry(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nulled += redact_entry(item)
        elif key not in FREE_KEYS and _is_number(value):
            entry[key] = None
            nulled += 1
    return nulled


def redact_markets(markets):
    """Return (redacted_copy, nulled_count) for a list of market dicts.

    Never mutates the caller's objects — `CAPACITY_HEATMAP_MARKETS` is a
    module-level constant shared by every request, so mutating it in place
    would permanently blank the fixture for paid callers after the first
    non-paid request.
    """
    import copy

    out = copy.deepcopy(markets) if isinstance(markets, list) else []
    nulled = 0
    for entry in out:
        if isinstance(entry, dict):
            nulled += redact_entry(entry)
            entry["locked"] = True
    return out, nulled
