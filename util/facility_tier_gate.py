"""Facility-record tier gate — ONE allow-list and ONE coordinate ladder, shared.

WHY THIS MODULE EXISTS
──────────────────────
The registry gate was built twice, in two places, and only one half travelled.

`api_tier_gating.FACILITY_VISIBLE_FIELDS` is the FIELD half. Every list-shaped
facility route reuses it, and it works: no `power_mw`, no `source`, no
`raw_data` reaches an anonymous caller through it.

The COORDINATE half was never extracted. It lives inline in `main.py`'s
`/api/v1/map` handler as `_MAP_ANON_COORD_DP` / `_MAP_FREE_COORD_DP` — a
deliberate, documented ladder (r-signupladder 2026-08-10). For a single
facility RECORD the ladder is now (owner decision 2026-09-21):

    anonymous       MAP_ANON_COORD_DP   2dp  ~1.1 km   city block
    free/identified MAP_FREE_COORD_DP   2dp  ~1.1 km   same as anonymous ...
                    + EXACT location (coordinates + street address) for up to
                      FREE_EXACT_LOCATIONS_PER_MONTH distinct facilities per
                      UTC month, one meter across web, REST and MCP
                      (util/location_meter.py)
    starter         full field set, location metered exactly like free
    developer+      exact everywhere (EXACT_LOCATION_TIERS)

The free rung used to be 3dp (~110 m) "the building". It is not any more: a
free account buys exact location for a few facilities a month instead of a
sharper blur for all of them. `exact_location=True` on gate_record /
apply_record_gate is how a caller spends that allowance on one record.

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
from decimal import Decimal

# Last-resort allow-list. Used when tier resolution or the field-mask import
# raises — never as a normal path. Deliberately smaller than
# FACILITY_VISIBLE_FIELDS['anon']: if we cannot tell who is asking, they get
# what a search-engine result already shows and nothing else. No coordinates.
MINIMAL_ANON_FIELDS = frozenset({'name', 'city', 'state', 'country', 'status', 'slug'})

# Keys that are plumbing, not data: they describe the response or link onward,
# carry no facility fact, and must survive the mask or the caller loses its
# cite/upgrade path. Kept for every tier.
# coordinates_status when this gate has coarsened the values. routes.provenance
# owns "known"/"unknown"; this is the third state it cannot see, because it runs
# before the rounding. Formatted, not fixed, so the number in the string can
# never disagree with the dp actually applied.
COORDS_APPROX_FMT = "approximate_{dp}dp"

PASSTHROUGH_KEYS = frozenset({
    'slug', 'profile_url', 'id', 'v', 'confidence_badge',
    'coordinates_status', 'connectivity_note',
})

# ── who gets what ────────────────────────────────────────────────────────────
# The tier strings here are whatever api_tier_gating.get_request_tier() returns:
# raw `users.plan` values (the registry's own vocabulary), 'identified' for
# trial keys, 'admin' for the role / internal key, and 'anon'. So the paid sets
# are DERIVED from tier_registry.paid_plan_names() — the house rule for a
# users.plan column (tests/test_paid_plan_lists_derived.py). The literal this
# replaced omitted founding, team and research_seed, all of which TIERS marks
# paid, and so served paying customers the free preview.
#
# ★ 'admin' is added by hand because paid_plan_names() deliberately excludes it
# (a role, not a purchasable plan) and the resolver does return it.
#
# ★ The coarse key words 'paid' and 'metered' are NOT added, on purpose.
# get_request_tier() cannot return either: validate_api_key() maps
# mcp_dev_keys.tier 'paid' -> 'pro' and anything unknown -> 'free', and
# _resolve_key_tier() maps 'paid' -> 'developer' and has no 'metered' entry.
# A $10 call-pack buyer resolves to their key's own tier ('free' for a pack-
# minted key) — the pack lives in mcp_topups, not in any tier column — so the
# pack's exact-location entitlement is decided by credit balance in
# util/location_meter.pack_active(), not by a word in these sets.
_ROLE_TIERS = frozenset({'admin'})

# Paid plans whose EXACT LOCATION is metered like a free account's while every
# other paid field stays theirs (owner decision 2026-09-21: the $9 Starter plan).
METERED_LOCATION_PAID_TIERS = frozenset({'starter'})

# What each set falls back to if the registry cannot be imported. Fail CLOSED:
# the full-record set falls back to the literal it replaced (never wider), and
# the exact-location set to NOBODY — "no exact", never "everyone exact".
_PAID_TIERS_FALLBACK = frozenset({'developer', 'pro', 'enterprise', 'admin', 'starter'})
_EXACT_LOCATION_FALLBACK = frozenset()


def _paid_plan_names():
    try:
        from tier_registry import paid_plan_names
        return frozenset(paid_plan_names())
    except Exception:
        return frozenset()


def _derive_paid_tiers():
    got = _paid_plan_names()
    return (got | _ROLE_TIERS) if got else _PAID_TIERS_FALLBACK


def _derive_exact_location_tiers():
    got = _paid_plan_names()
    if not got:
        return _EXACT_LOCATION_FALLBACK
    return (got - METERED_LOCATION_PAID_TIERS) | _ROLE_TIERS


# Tiers that pay: the FULL FIELD SET (power, operator, source, ...).
PAID_TIERS = _derive_paid_tiers()

# Tiers whose coordinates and street address are exact on EVERY record.
EXACT_LOCATION_TIERS = _derive_exact_location_tiers()

# Tiers that have identified themselves but do not pay — the middle rung.
IDENTIFIED_TIERS = frozenset({'free', 'identified', 'trial', 'trial_taste'})

# Tiers that may spend the monthly exact-location allowance: every identified
# tier, plus any paid tier that is not exact everywhere. Derived from the two
# sets above so it cannot drift from them — a tier that loses exact location
# gains the allowance by construction rather than falling to "never exact".
LOCATION_ALLOWANCE_TIERS = IDENTIFIED_TIERS | (PAID_TIERS - EXACT_LOCATION_TIERS)

# Coordinate keys, across every naming convention in the corpus.
_LAT_KEYS = ('latitude', 'lat')
_LON_KEYS = ('longitude', 'longitude_deg', 'lon', 'lng')

# The street address, across the spellings a facility record can carry. Kept
# only where the location is exact; dropped from a full-field record whose
# location is not (starter).
STREET_ADDRESS_KEYS = ('address', 'address1', 'address2', 'street',
                       'street_address', 'full_address', 'postal_code',
                       'postcode', 'zip', 'zip_code', 'zipcode')

# Raw upstream records carry the address and coordinates verbatim (PeeringDB
# address1/zipcode/latitude/longitude), so a record whose location is metered
# never carries them, even when the allowance makes this one record exact.
RAW_LOCATION_KEYS = ('raw_data',)


def _env_dp(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def norm_tier(tier) -> str:
    """Lower-cased tier string; None / '' / non-strings read as 'anon'."""
    try:
        t = (tier or 'anon')
        return t.lower() if isinstance(t, str) else 'anon'
    except Exception:
        return 'anon'


def coord_dp_for_tier(tier) -> int | None:
    """Decimal places this tier's coordinates are rounded to. None = exact.

    Exact ONLY for EXACT_LOCATION_TIERS. Every other tier that has identified
    itself — free/identified, and starter, which keeps its other paid fields —
    is rounded at MAP_FREE_COORD_DP, whose record default is now 2dp: the same
    ~1.1 km as anonymous (owner decision 2026-09-21). What a free account buys
    is exact location for a few facilities a month (gate_record's
    exact_location), not a sharper blur for every one.

    Reads MAP_ANON_COORD_DP / MAP_FREE_COORD_DP — the map's knobs, on purpose.
    MAP_ANON_COORD_DP=6 remains the no-deploy kill switch it already was, and
    now disables coarsening on every surface, not just the map.
    """
    t = norm_tier(tier)
    if t in EXACT_LOCATION_TIERS:
        return None
    if t in IDENTIFIED_TIERS or t in PAID_TIERS:
        return _env_dp('MAP_FREE_COORD_DP', 2)
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
    # on the map buys nothing here: a record is ~1.1 km (2 dp) for anonymous
    # AND for free callers. The signup rung on a record is no longer precision
    # — anonymous and free round identically by design since 2026-09-21 — it
    # is the exact-location allowance (util/location_meter.py).
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
    """Round this record's coordinates in place. Returns values changed.

    ★ Decimal is a coordinate too. A NUMERIC column comes back from psycopg2
    as decimal.Decimal, and this used to round only int/float — so a NUMERIC
    latitude passed every gate at full precision while the tally said nothing
    was there to round. It is rounded like any number and becomes a float, the
    type every JSON consumer of these routes already receives.
    """
    n = 0
    for key in _LAT_KEYS + _LON_KEYS:
        if key not in rec:
            continue
        v = rec[key]
        if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
            continue
        try:
            f = float(v)
            r = round(f, dp)
        except (OverflowError, ValueError):
            continue
        # Count only a real change, so the returned tally cannot be inflated by
        # values that were already coarse. A payload of 2dp coordinates gated
        # at 2dp must report 0 roundings and that must be the truth. Compared
        # as floats: Decimal('39.02') != 39.02 exactly, and an already-coarse
        # Decimal must not count as a redaction either.
        if r != f:
            rec[key] = r
            n += 1
        elif isinstance(v, Decimal):
            rec[key] = r        # same value, as the float the other rows carry
    return n


def location_is_exact(tier, exact_location=False) -> bool:
    """Whether this tier's record carries exact coordinates + street address.

    Always for EXACT_LOCATION_TIERS. For LOCATION_ALLOWANCE_TIERS only when the
    caller has spent (or already holds) this facility's monthly allowance and
    says so with exact_location=True. NEVER for anonymous or an unknown tier
    string, whatever the flag says — the flag is an allowance, and only a
    caller with an account can hold one.
    """
    t = norm_tier(tier)
    if t in EXACT_LOCATION_TIERS:
        return True
    return bool(exact_location) and t in LOCATION_ALLOWANCE_TIERS


def _is_value(v) -> bool:
    return v not in (None, '', [], {})


def _rounded_copy(obj, dp, memo=None):
    """(copy of obj with every coordinate at any depth rounded, values rounded).

    Containers are COPIED, never mutated, so the caller's nested objects are
    untouched; scalars are shared. Cycle-safe."""
    if memo is None:
        memo = {}
    if isinstance(obj, dict):
        if id(obj) in memo:
            return memo[id(obj)], 0
        out = {}
        memo[id(obj)] = out
        n = 0
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k], m = _rounded_copy(v, dp, memo)
                n += m
            else:
                out[k] = v
        n += _round_coords(out, dp)
        return out, n
    if isinstance(obj, list):
        if id(obj) in memo:
            return memo[id(obj)], 0
        out = []
        memo[id(obj)] = out
        n = 0
        for v in obj:
            c, m = _rounded_copy(v, dp, memo)
            out.append(c)
            n += m
        return out, n
    return obj, 0


def _gate_metered_location_record(rec, dp, exact):
    """A paid tier whose LOCATION is metered (starter): every field it pays for,
    except where the facility is.

    Drops the raw upstream record always (it carries the address and the
    coordinates verbatim) and the street address unless `exact`. Rounds the
    record's own coordinates unless `exact`, and every NESTED coordinate
    always — a nested object describes something other than this facility, and
    the allowance only ever buys this one."""
    drop = set(RAW_LOCATION_KEYS)
    if not exact:
        drop |= set(STREET_ADDRESS_KEYS)
    out = {}
    rounded = 0
    for k, v in rec.items():
        if k in drop:
            continue
        if isinstance(v, (dict, list)):
            out[k], m = _rounded_copy(v, dp)
            rounded += m
        else:
            out[k] = v
    dropped = sum(1 for k, v in rec.items() if k in drop and _is_value(v))
    own = 0 if exact else _round_coords(out, dp)
    if own:
        out['coordinates_status'] = COORDS_APPROX_FMT.format(dp=dp)
    return out, dropped + rounded + own


def gate_record(rec, tier, exact_location=False) -> tuple[dict, int]:
    """Gate ONE facility record. Returns (new_record, values_redacted).

    exact_location=True spends nothing by itself — the caller has already
    charged the monthly allowance (util/location_meter.py) — and is honoured
    only for LOCATION_ALLOWANCE_TIERS: the record then keeps its EXACT
    coordinates and street address while every other field the tier does not
    pay for stays withheld. The default, False, is today's behaviour.
    """
    if not isinstance(rec, dict):
        return rec, 0
    try:
        visible = visible_fields_for_tier(tier)
        exact = location_is_exact(tier, exact_location)
        dp = None if exact else coord_dp_for_tier(tier)
        if visible is None:
            if norm_tier(tier) in EXACT_LOCATION_TIERS:
                return rec, 0      # paid: full record, exact coordinates
            # Paid, but the location is metered (starter). The nested
            # coordinates round at the tier's own precision even when this
            # record is exact, hence coord_dp_for_tier, not `dp`.
            return _gate_metered_location_record(
                rec, coord_dp_for_tier(tier), exact)
        keep = set(visible) | set(PASSTHROUGH_KEYS)
        if exact:
            # The allowance buys the location — the coordinates are in the
            # visible set already; this adds the street address — and nothing
            # else the tier does not pay for.
            keep |= set(STREET_ADDRESS_KEYS)
        out = {k: v for k, v in rec.items() if k in keep}
        # A dropped key counts as a redaction only if it carried a value. A
        # None or empty field was never the secret, and counting it would let a
        # data-poor row report a healthy tally while a rich row leaked.
        dropped = sum(1 for k, v in rec.items()
                      if k not in keep and v not in (None, '', [], {}))
        rounded = _round_coords(out, dp) if dp is not None else 0
        # ★ r-loudgate (2026-09-20): a coarsened coordinate must not still be
        # described as "known".
        #
        # routes.provenance.normalize_coordinates sets coordinates_status from
        # the RAW values, and it has to run BEFORE this gate (verified_flag
        # reads is_duplicate, which the mask drops). So the field whose entire
        # job is to describe coordinate quality was computed before the
        # transformation that invalidates it, and shipped "known" next to a
        # latitude rounded to ~1.1km.
        #
        # Found by the partner generating a connector from our spec: "an agent
        # asks for a facility, gets a plausible record, and reports it as
        # complete." Every other gate we have announces itself with a 403; this
        # one returns 200 and looked whole. Stamping the RECORD — not only the
        # response envelope — means an agent that extracts `data` and discards
        # the envelope still carries the caveat, and every row of a gated LIST
        # carries its own.
        if rounded:
            out['coordinates_status'] = COORDS_APPROX_FMT.format(dp=dp)
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
# collapses the anon->free rung this module exists to create — then a sharper
# blur, since 2026-09-21 the exact-location allowance — and a route that serves
# no coordinates at all gives a claimed key nothing to buy.
#
# So the envelope lives HERE, once, and the routes call it. A route that builds
# its own is how the drift happened; there is no second copy to keep in step.

UPGRADE_CHECKOUT_URL = 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c'
UPGRADE_PRICING_URL = 'https://dchub.cloud/pricing'
UPGRADE_PRICE = '$49/mo'

# ── the offer, stated per caller ─────────────────────────────────────────────
# Every sentence below is assembled from the tier and the meter's own limit, so
# the copy cannot promise an allowance the meter does not grant (or a precision
# the gate did not apply): FREE_EXACT_LOCATIONS_PER_MONTH=0 removes the
# allowance sentence everywhere, and the precision phrase follows the dp.
_PRECISION_PHRASES = {0: '~110 km', 1: '~11 km', 2: '~1.1 km', 3: '~110 m',
                      4: '~11 m', 5: '~1 m'}


def _precision_phrase(dp) -> str:
    return _PRECISION_PHRASES.get(dp, f'{dp} decimal places')


def _monthly_allowance() -> int:
    """The meter's monthly limit; 0 (promise nothing) if it cannot be read."""
    try:
        from util.location_meter import monthly_limit
        return max(0, int(monthly_limit()))
    except Exception:
        return 0


def _allowance_sentence(t, n) -> str:
    if n <= 0 or t in EXACT_LOCATION_TIERS:
        return ''
    noun = 'facility' if n == 1 else 'facilities'
    if t in PAID_TIERS:
        return f'Your plan includes exact location for {n} {noun} a month.'
    if t in IDENTIFIED_TIERS:
        return f'Your account includes exact location for {n} {noun} a month.'
    return f'A free account unlocks exact location for {n} {noun} a month.'


def _developer_offer(t) -> str:
    if t in PAID_TIERS:
        # A metered-location paid plan already has power, source and the rest.
        return f'Developer ({UPGRADE_PRICE}) unlocks exact location everywhere.'
    return (f'Developer ({UPGRADE_PRICE}) unlocks exact location everywhere plus '
            'power capacity, source and nearby infrastructure.')


def location_offer(tier) -> str:
    """What exact location costs THIS caller, in one sentence. '' when their
    location is exact already. For an anonymous caller:

      "A free account unlocks exact location for 10 facilities a month;
       Developer ($49/mo) unlocks exact location everywhere plus power
       capacity, source and nearby infrastructure."
    """
    t = norm_tier(tier)
    if t in EXACT_LOCATION_TIERS:
        return ''
    allowance = _allowance_sentence(t, _monthly_allowance())
    offer = _developer_offer(t)
    return f'{allowance[:-1]}; {offer}' if allowance else offer


def _upgrade_cta(t, exact) -> str:
    if t in PAID_TIERS:
        withheld = ('Raw upstream records' if exact
                    else 'Exact coordinates and street address')
    elif t in IDENTIFIED_TIERS:
        withheld = ('Power capacity, on-site fiber and source' if exact
                    else 'Exact coordinates, street address, power capacity, '
                         'on-site fiber and source')
    else:
        withheld = ('Exact coordinates, street address, power capacity, '
                    'operator and on-site fiber')
    allowance = _allowance_sentence(t, _monthly_allowance())
    return (f'{withheld} require a Developer key — dchub.cloud/pricing.'
            + (f' {allowance}' if allowance else ''))


def _user_facing_note(t, dp, exact) -> str:
    where = ('this exact location is from your monthly allowance' if exact
             else f'coordinates are rounded to {_precision_phrase(dp)}')
    lead = ('\U0001f4a1 ' + where[0].upper() + where[1:] if t in PAID_TIERS
            else '\U0001f4a1 This is a free preview from DC Hub — ' + where)
    return f'{lead}. {location_offer(t).rstrip(".")} — dchub.cloud/pricing'


def _upgrade_message(t) -> str:
    offer = _developer_offer(t)
    n = _monthly_allowance()
    if t in PAID_TIERS or t in IDENTIFIED_TIERS:
        allowance = _allowance_sentence(t, n)
        return offer + (f' {allowance}' if allowance else '')
    noun = 'facility' if n == 1 else 'facilities'
    return offer + (' A free account first adds provider, operator, market and '
                    'region' + (f', and exact location for {n} {noun} a month.'
                                if n > 0 else '.'))


def apply_record_gate(resp, tier, exact_location=False) -> dict:
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

    exact_location=True: the caller has charged this facility to the monthly
    exact-location allowance (util/location_meter.py). Passed straight to
    gate_record, which honours it only for LOCATION_ALLOWANCE_TIERS; the
    markers then report `_coord_precision_dp: None` (exact) while naming what
    is still withheld. The caller stamps `_location_allowance` itself.
    """
    if not isinstance(resp, dict):
        return resp
    rec = resp.get('data')
    if not isinstance(rec, dict):
        return resp
    tier_s = norm_tier(tier)
    try:
        data, n = gate_record(rec, tier_s, exact_location=exact_location)
        exact = location_is_exact(tier_s, exact_location)
        dp = None if exact else coord_dp_for_tier(tier_s)
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
    # ★ r-loudgate (2026-09-20): a COUNT does not tell a caller what it lost.
    # `_redacted_values: 4` reads as noise; the field NAMES let an agent say
    # "operator and power capacity were withheld" instead of presenting a
    # partial record as whole. Names only — never the values.
    _withheld = sorted(k for k, v in rec.items()
                       if k not in data and v not in (None, '', [], {}))
    if _withheld:
        resp['_withheld_fields'] = _withheld
    resp['_upgrade_cta'] = _upgrade_cta(tier_s, exact)
    resp['_pricing_url'] = UPGRADE_PRICING_URL
    # Published key, kept deliberately. get_facility_by_id emitted this before
    # the routes were unified, and the contract baseline recorded it. Because
    # this endpoint's response is now built by a call rather than a dict
    # literal, the static guard can no longer see its keys — so dropping this
    # would have been an invisible removal of a public key. Additive only.
    resp['_user_facing_note'] = _user_facing_note(tier_s, dp, exact)
    resp['_upgrade'] = {
        'tier': tier_s,
        'message': _upgrade_message(tier_s),
        'url': UPGRADE_PRICING_URL + '#developer',
        'checkout': UPGRADE_CHECKOUT_URL,
        'price': UPGRADE_PRICE,
    }
    return resp
