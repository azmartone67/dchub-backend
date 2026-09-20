"""Facility-record tier gate — ONE allow-list and ONE coordinate ladder, shared.

WHY THIS MODULE EXISTS
──────────────────────
The registry gate was built twice, in two places, and only one half travelled.

`api_tier_gating.FACILITY_VISIBLE_FIELDS` is the FIELD half. Every list-shaped
facility route reuses it, and it works: no `power_mw`, no `source`, no
`raw_data` reaches an anonymous caller through it.

The COORDINATE half was never extracted. It lives inline in `main.py`'s
`/api/v1/map` handler as `_MAP_ANON_COORD_DP` / `_MAP_FREE_COORD_DP` — a
deliberate, documented ladder (r-signupladder 2026-08-10):

    anonymous   MAP_ANON_COORD_DP   2dp  ~1.1 km   city block
    free/ident  MAP_FREE_COORD_DP   3dp  ~110 m    the building
    paid        exact

and `FACILITY_VISIBLE_FIELDS['anon']` contains `latitude` and `longitude` with
**no precision constraint at all**. So every route that reuses the field mask
and is not the map hands an anonymous caller survey-grade coordinates while
passing the field audit. Measured live 2026-09-19, no key and no cookie:

    /api/v1/search?q=ashburn        tier=anon   coordinates at 6 dp (~0.1 m)
    /api/v1/facility/<slug>         no tier check at all — power_mw 2300,
                                    provider, address, fiber_providers, 6 dp
    /api/facilities?limit=200       100 rows at 4 dp

★ This is the class [[reference_dchub_anon_bulk_exposure_0801]] closed on
`/api/v1/map` and on `export_facility_csv` and nowhere else: "gating the map
did not gate the corpus." A row cap cannot fix it — the 0801 audit already
established that the uncapped global render IS the bulk export. Precision is
the thing that has to be gated, and it has to be gated in one place or the
next route re-opens it.

THE POINT IS THAT THERE IS NO SECOND KNOB
─────────────────────────────────────────
`coord_dp_for_tier` reads the SAME env vars the map reads. Moving
`MAP_ANON_COORD_DP` moves every surface at once. A second env var here would
mean the map and the corpus could disagree about what anonymous means, which
is the defect this module exists to remove — cf.
[[feedback_same_name_two_token_values]].

FAIL-CLOSED, AND THE NO-OP IS OBSERVABLE
────────────────────────────────────────
`gate_records` returns the number of values it actually removed or rounded.
Callers and tests assert on it: a gate reporting 0 redactions on a payload
that carried paid fields is the no-op bug recurring, and it is now visible
instead of silent. That is the lesson `util/heatmap_gating.py` was written
for — a redaction loop aimed at a schema the payload did not have stripped
nothing for months and the response still looked gated, because it carried a
`_gated: true` flag next to the unredacted numbers.

Any exception anywhere → MINIMAL_ANON_FIELDS. A gate that throws must not
become a gate that passes.
"""
from __future__ import annotations

import os

# Last-resort allow-list. Used when tier resolution or the field-mask import
# raises — never as a normal path. Deliberately smaller than
# FACILITY_VISIBLE_FIELDS['anon']: if we cannot tell who is asking, they get
# what a search-engine result already shows and nothing else. No coordinates.
MINIMAL_ANON_FIELDS = frozenset({'name', 'city', 'state', 'country', 'status', 'slug'})

# Keys that are plumbing, not data: they describe the response or link onward,
# carry no facility fact, and must survive the mask or the caller loses its
# cite/upgrade path. Kept for every tier.
PASSTHROUGH_KEYS = frozenset({
    'slug', 'profile_url', 'id', 'v', 'confidence_badge',
    'coordinates_status', 'connectivity_note',
})

# Tiers that pay. Everything else is coarsened.
PAID_TIERS = frozenset({'developer', 'pro', 'enterprise', 'admin', 'starter'})

# Tiers that have identified themselves but do not pay — the middle rung.
IDENTIFIED_TIERS = frozenset({'free', 'identified', 'trial', 'trial_taste'})

# Coordinate keys, across every naming convention in the corpus.
_LAT_KEYS = ('latitude', 'lat')
_LON_KEYS = ('longitude', 'longitude_deg', 'lon', 'lng')


def _env_dp(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def coord_dp_for_tier(tier) -> int | None:
    """Decimal places this tier's coordinates are rounded to. None = exact.

    Reads MAP_ANON_COORD_DP / MAP_FREE_COORD_DP — the map's knobs, on purpose.
    MAP_ANON_COORD_DP=6 remains the no-deploy kill switch it already was, and
    now disables coarsening on every surface, not just the map.
    """
    t = (tier or 'anon').lower()
    if t in PAID_TIERS:
        return None
    if t in IDENTIFIED_TIERS:
        return _env_dp('MAP_FREE_COORD_DP', 3)
    # ★ ONE knob, two DEFAULTS, and the difference is load-bearing.
    #
    # The map defaults anonymous to 3 dp and that is deliberate: no caller
    # sends `bbox`, so /api/v1/map coarsens at EVERY zoom level and a tighter
    # default visibly degrades the public SEO map. tests/test_anon_bulk_
    # exposure.py pins that 3, and the 0801 audit records the reasoning —
    # tightening the MAP should wait for a frontend PR that sends bbox on zoom.
    #
    # None of that applies to a single RECORD. Nobody renders a map from
    # /api/v1/facility/<slug>, so the rendering constraint that justifies 3 dp
    # on the map buys nothing here, while 3 dp would make anonymous and free
    # IDENTICAL and collapse the signup rung the ladder exists to create.
    #
    # Setting MAP_ANON_COORD_DP still moves BOTH — production sets it to 2, so
    # the two agree there today. The defaults differ only where the surfaces
    # genuinely differ, and MAP_ANON_COORD_DP=6 remains one kill switch for one
    # ladder across every surface.
    return _env_dp('MAP_ANON_COORD_DP', 2)


def visible_fields_for_tier(tier) -> frozenset | None:
    """The tier's field allow-list. None = full record (paid).

    Delegates to api_tier_gating.FACILITY_VISIBLE_FIELDS so there is one
    vocabulary. On import failure, fail closed to MINIMAL_ANON_FIELDS rather
    than to None — None means "full record" here, and an ImportError must
    never be the thing that grants full access.
    """
    t = (tier or 'anon').lower()
    if t in PAID_TIERS:
        return None
    try:
        from api_tier_gating import FACILITY_VISIBLE_FIELDS
        visible = FACILITY_VISIBLE_FIELDS.get(t)
        if visible is None:
            # Unknown, non-paid tier string. Treat as anonymous, not as paid.
            visible = FACILITY_VISIBLE_FIELDS.get('anon') or MINIMAL_ANON_FIELDS
        return frozenset(visible)
    except Exception:
        return frozenset(MINIMAL_ANON_FIELDS)


def _round_coords(rec: dict, dp: int) -> int:
    """Round this record's coordinates in place. Returns values changed."""
    n = 0
    for key in _LAT_KEYS + _LON_KEYS:
        if key not in rec:
            continue
        v = rec[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        r = round(float(v), dp)
        # Count only a real change, so the returned tally cannot be inflated by
        # values that were already coarse. A payload of 2dp coordinates gated
        # at 2dp must report 0 roundings and that must be the truth.
        if r != v:
            rec[key] = r
            n += 1
    return n


def gate_record(rec, tier) -> tuple[dict, int]:
    """Gate ONE facility record. Returns (new_record, values_redacted)."""
    if not isinstance(rec, dict):
        return rec, 0
    try:
        visible = visible_fields_for_tier(tier)
        dp = coord_dp_for_tier(tier)
        if visible is None:
            return rec, 0          # paid: full record, exact coordinates
        keep = set(visible) | set(PASSTHROUGH_KEYS)
        out = {k: v for k, v in rec.items() if k in keep}
        # A dropped key counts as a redaction only if it carried a value. A
        # None or empty field was never the secret, and counting it would let a
        # data-poor row report a healthy tally while a rich row leaked.
        dropped = sum(1 for k, v in rec.items()
                      if k not in keep and v not in (None, '', [], {}))
        rounded = _round_coords(out, dp) if dp is not None else 0
        return out, dropped + rounded
    except Exception:
        safe = {k: v for k, v in rec.items()
                if k in (set(MINIMAL_ANON_FIELDS) | set(PASSTHROUGH_KEYS))}
        return safe, max(1, len(rec) - len(safe))


def gate_records(rows, tier) -> tuple[list, int]:
    """Gate a list of facility records. Returns (new_rows, values_redacted)."""
    if not isinstance(rows, list):
        return rows, 0
    total = 0
    out = []
    for r in rows:
        g, n = gate_record(r, tier)
        out.append(g)
        total += n
    return out, total


def coarsen_coords_deep(obj, tier) -> int:
    """Round every coordinate anywhere in `obj`, in place. Returns count.

    Defence in depth for payloads whose records this module does not otherwise
    recognise — a nested `nearby`, a GeoJSON `properties`, a market rollup.
    Rounding a number cannot break a schema, so this is safe to apply broadly
    where the field mask is not.

    Deliberately NOT applied to open-licence layers (Global Energy Monitor
    units, HIFLD transmission): the 0801 audit records those as intentionally
    public and says not to "fix" them. Callers choose; this function has no
    opinion about which payload it is handed.
    """
    dp = coord_dp_for_tier(tier)
    if dp is None:
        return 0
    n = 0
    stack = [obj]
    seen = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            n += _round_coords(cur, dp)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return n


# ── the single-record response envelope ──────────────────────────────────────
# r-slashparity (2026-09-20): three routes answered "one facility" and each
# assembled its own envelope, so they served three different free surfaces from
# the same row. Measured anonymous, cache-busted, the same minute:
#
#   /api/v1/facilities/8484    8 fields, NO coordinates, _upgrade
#   /api/v1/facilities/8484/  10 fields, lat 39.02 (2dp),  _gated
#   /api/v1/facility/<slug>   11 fields, lat 33.38 (2dp),  _gated
#
# The 8-field one was `get_facility_by_id`'s hardcoded tuple — the THIRD copy of
# the field policy, the one its own comment called out for omitting the
# coordinate ladder. Withholding coordinates entirely reads as "tighter", but it
# collapses the anon->free rung this module exists to create: under the ladder a
# free key sharpens 2dp (~1.1km) to 3dp (~110m), and a route that serves no
# coordinates at all gives a claimed key nothing to buy.
#
# So the envelope lives HERE, once, and the routes call it. A route that builds
# its own is how the drift happened; there is no second copy to keep in step.

UPGRADE_CHECKOUT_URL = 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c'
UPGRADE_PRICING_URL = 'https://dchub.cloud/pricing'
UPGRADE_PRICE = '$49/mo'


def apply_record_gate(resp, tier) -> dict:
    """Gate resp['data'] IN PLACE and attach the tier markers. Returns resp.

    Takes the response the route already built rather than returning a fresh
    one, because both facility_by_slug branches attach a provenance block
    (routes.provenance.attach_provenance) to the response BEFORE gating —
    normalize_coordinates has to see raw values and verified_flag() reads
    `is_duplicate`, which the mask drops. An earlier version of this returned a
    new dict and the call sites assigned over theirs, silently discarding that
    provenance block; the per-branch guard in
    tests/test_facility_record_envelope_is_shared.py caught it.

    Two marker vocabularies are emitted on purpose, and both are load-bearing:

      _gated / _coord_precision_dp / _redacted_values / _upgrade_cta / _pricing_url
          what the /api/v1/map surface already publishes, so a caller reading
          one surface can read the other.
      _upgrade
          what the curated OpenAPI spec tells third-party catalogues to branch
          on. Dropping it would break every generated connector.

    Paid tiers get the full record and NO markers — `_upgrade` being absent is
    the documented signal that nothing was withheld.
    """
    if not isinstance(resp, dict):
        return resp
    rec = resp.get('data')
    if not isinstance(rec, dict):
        return resp
    tier_s = (tier or 'anon').lower()
    try:
        data, n = gate_record(rec, tier_s)
        dp = coord_dp_for_tier(tier_s)
    except Exception:
        # Fail CLOSED. An import or tier-resolution raise must never be the
        # thing that serves the full record.
        #
        # PULL the allowed keys; do not ITERATE rec. The first version of this
        # block was `{k: v for k, v in rec.items() if k in allowed}`, which
        # re-enters the very call that just raised — a fail-closed path that
        # can itself raise is not one. tests/test_facility_record_envelope_is_
        # shared.py::test_the_gate_fails_closed_on_a_hostile_record caught it.
        data = {}
        for _k in (set(MINIMAL_ANON_FIELDS) | set(PASSTHROUGH_KEYS)):
            try:
                if _k in rec:
                    data[_k] = rec[_k]
            except Exception:
                continue
        resp['data'] = data
        resp['_gated'] = True
        resp['_redacted_values'] = max(1, len(data))
        resp['_pricing_url'] = UPGRADE_PRICING_URL
        return resp
    resp['data'] = data
    if dp is None and not n:
        return resp                      # paid: full record, no markers
    resp['_gated'] = True
    resp['_coord_precision_dp'] = dp
    resp['_redacted_values'] = n
    resp['_upgrade_cta'] = (
        'Power capacity, operator, on-site fiber and exact coordinates '
        'require a Developer key — dchub.cloud/pricing')
    resp['_pricing_url'] = UPGRADE_PRICING_URL
    # Published key, kept deliberately. get_facility_by_id emitted this before
    # the routes were unified, and the contract baseline recorded it. Because
    # this endpoint's response is now built by a call rather than a dict
    # literal, the static guard can no longer see its keys — so dropping this
    # would have been an invisible removal of a public key. Additive only.
    resp['_user_facing_note'] = (
        '\U0001f4a1 This is a free preview from DC Hub — coordinates are '
        'rounded to ~1.1 km for anonymous callers and ~110 m with a free key. '
        'Full data with exact coordinates, power capacity and connectivity '
        'specs is available at dchub.cloud/developers')
    resp['_upgrade'] = {
        'tier': tier_s,
        'message': (
            'Developer plan ($49/mo) unlocks exact coordinates, power capacity, '
            'source, address, and nearby infrastructure.'
            + ('' if tier_s != 'anon' else
               ' A free key first sharpens coordinates from ~1.1 km to ~110 m '
               'and adds provider, operator, market and region.')),
        'url': UPGRADE_PRICING_URL + '#developer',
        'checkout': UPGRADE_CHECKOUT_URL,
        'price': UPGRADE_PRICE,
    }
    return resp
