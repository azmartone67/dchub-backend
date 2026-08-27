"""iso_taxonomy.py — canonical market → grid-operator resolution.

r-iso-taxonomy (2026-07-28). Single source of truth for the `iso` column
on market_power_scores. Replaces FOUR divergent copies of a state→ISO map:

    routes/dcpi.py::_state_to_iso        (governed the served data; WRONG)
    dchub_self_heal.py::US_STATE_ISO     (correct, but unreachable — see below)
    scripts/bulk_dcpi_score.py::US_STATE_ISO
    routes/brain_data_gatherer.py        (inlined, "mirrors routes/dcpi.py")

WHY THIS MODULE EXISTS
----------------------
Two independent defects compounded:

1. `_state_to_iso` mapped NC→PJM and MO→MISO. Both are wrong, and both are
   *harmful* rather than merely untidy: PJM and MISO are real RTOs, so an
   agent (or execute_plan's ISO injection) would happily pull PJM's
   interconnection queue for Charlotte — which is Duke Energy Carolinas
   territory and not in any RTO. PJM's actual Carolinas footprint is
   Dominion's northeastern-NC service area, not Charlotte.

2. The CORRECT map (dchub_self_heal.US_STATE_ISO) was dead code. It only
   fires under `WHERE iso IS NULL OR iso = '' OR iso = 'UNK'`, and the daily
   recompute unconditionally writes `iso=%s` from `_state_to_iso`, so the
   guard never matched. Observable proof: SERC had ZERO markets in
   market_power_scores while PJM had 78.

STATE-LEVEL RESOLUTION IS STRUCTURALLY INSUFFICIENT
---------------------------------------------------
Some states genuinely span RTO boundaries, so no state→ISO map can be
correct for all of them. Missouri is the proof: Kansas City is Evergy Metro
(SPP) and St. Louis is Ameren Missouri (MISO). Both are in MO. The old map
picked MISO and was therefore right for St. Louis and wrong for Kansas City
— and the contradiction was visible in the data, because the Kansas side of
the very same metro (Olathe, Overland Park, Lenexa) correctly resolved to
SPP. A metro was split across two ISOs by a state line.

Hence: MARKET_ISO_OVERRIDES (city-level) is consulted BEFORE STATE_ISO.
When you find another split-state market, add the slug there — do not
"fix" the state default, or you will break the other half of the state.

THE THREE TAXONOMIES
--------------------
The `iso` column has always mixed three different things. It still does —
the column is load-bearing for too many consumers to narrow now — but the
kind of thing each label is, is no longer implicit. `iso_type` says which:

    RTO    — organised wholesale market. HAS an interconnection queue.
             Only these are valid inputs to queue//market lookups.
    BA     — balancing authority. Operates the grid; runs no market and
             publishes no RTO-style queue. (TVA, Southern Company.)
    REGION — NERC reliability region. A footprint, not an operator.
             (WECC, SERC, FRCC.)

Callers that pull interconnection-queue data MUST gate on
`has_interconnection_queue(iso)` rather than assuming the label names an
RTO. That check — not this module's corrected values — is what actually
kills this bug class: correcting Charlotte to SERC only helps until the
next split-state market is added.
"""

from __future__ import annotations

# ── Label → taxonomy class ──────────────────────────────────────────────
ISO_TYPE = {
    # Organised wholesale markets (have interconnection queues)
    "PJM": "RTO", "MISO": "RTO", "SPP": "RTO", "ERCOT": "RTO",
    "CAISO": "RTO", "NYISO": "RTO", "ISONE": "RTO", "ISO-NE": "RTO",
    "IESO": "RTO", "AESO": "RTO",

    # Balancing authorities (grid operators, no organised market)
    "TVA": "BA", "SOCO": "BA", "HECO": "BA", "PREPA": "BA",
    "GPA": "BA", "WAPA": "BA", "MH": "BA", "HQ": "BA", "BCH": "BA",
    # shell #41 WS2 (2026-07-28): BPA (Bonneville) is a federal Power
    # Marketing Administration — the same class as WAPA, and the label
    # routes/iso_bpa.py writes to grid_data. It was absent, so
    # iso_type_of("BPA") returned "" and has_interconnection_queue("BPA")
    # returned False for the WRONG REASON: unknown label, not a typed BA
    # that genuinely has no RTO queue. Same verdict, now for a real reason.
    "BPA": "BA",
    # "HQ" above never joined anything: routes/iso_hydroquebec.py:46 sets
    # ISO_CODE = "HYDROQUEBEC", so every row that module writes was
    # unclassifiable. This is an ALIAS of the same operator, not a second
    # one — do not count it as another BA.
    "HYDROQUEBEC": "BA",

    # NERC reliability regions (footprints, not operators)
    "WECC": "REGION", "SERC": "REGION", "FRCC": "REGION", "AK": "REGION",
}

#: Labels that name an actual market with an interconnection queue.
RTO_LABELS = frozenset(k for k, v in ISO_TYPE.items() if v == "RTO")

# ── State → default grid operator ───────────────────────────────────────
# The DOMINANT operator for data-center siting purposes. For split states
# the per-market overrides below carry the truth; this is only the fallback.
STATE_ISO = {
    # Single-RTO states
    "CA": "CAISO", "TX": "ERCOT", "NY": "NYISO",
    "MA": "ISONE", "NH": "ISONE", "VT": "ISONE",
    "ME": "ISONE", "CT": "ISONE", "RI": "ISONE",
    "PA": "PJM", "NJ": "PJM", "DE": "PJM", "MD": "PJM",
    "VA": "PJM", "WV": "PJM", "DC": "PJM", "OH": "PJM",
    "MN": "MISO", "WI": "MISO", "IA": "MISO", "ND": "MISO",
    "AR": "MISO", "LA": "MISO", "MS": "MISO",
    "KS": "SPP", "OK": "SPP", "NE": "SPP",

    # ── Corrected 2026-07-28 (were wrong in _state_to_iso) ──────────────
    # NC: Duke Energy Carolinas / Progress are NOT RTO members. PJM's NC
    #     footprint is only Dominion's northeastern service area.
    "NC": "SERC",
    # SC: Dominion Energy SC / Santee Cooper / Duke — not Southern Company,
    #     so the old SOCO was wrong on top of being a BA label.
    "SC": "SERC",
    # MI: DTE Electric (Detroit) + Consumers Energy (most of the rest) are
    #     MISO. PJM's ONLY Michigan footprint is the AEP Indiana Michigan
    #     Power sliver in the far southwest (Benton Harbor / St. Joseph).
    #     All four Michigan DCPI markets — Detroit, Southfield, Grand Rapids,
    #     Lansing — sit on DTE or Consumers, and no market exists in the I&M
    #     sliver (benton-harbor and kalamazoo both 404), so no per-market
    #     override is needed. (Verified 2026-07-28 in r-split-state-iso.)
    "MI": "MISO",
    # IN: Duke Indiana, AES Indiana, NIPSCO, CenterPoint are all MISO.
    #     (I&M in the northeast is PJM — override per-market if one lands.)
    "IN": "MISO",
    # KY: LG&E/KU (Louisville, Lexington) left MISO in 2006 and are non-RTO.
    #     Kentucky Power in the east IS PJM — override per-market.
    "KY": "SERC",
    # MO: SPLIT. Default MISO (Ameren, St. Louis); Kansas City side is
    #     Evergy Metro/SPP and is handled in MARKET_ISO_OVERRIDES below.
    "MO": "MISO",
    # SD: SPLIT east(MISO)/west(SPP). Default MISO — the only SD market we
    #     score is Sioux Falls, which is Xcel Energy and genuinely MISO.
    "SD": "MISO",
    # IL: SPLIT. Default PJM — ComEd/Chicago is the material DC market.
    #     Downstate Ameren Illinois is MISO; override per-market.
    "IL": "PJM",

    # Non-RTO West (CAISO is carved out above)
    "AZ": "WECC", "NV": "WECC", "UT": "WECC", "CO": "WECC",
    "NM": "WECC", "ID": "WECC", "MT": "WECC", "WY": "WECC",
    "WA": "WECC", "OR": "WECC",

    # Balancing authorities / regions
    "TN": "TVA",    # TVA is a BA, not an ISO — see ISO_TYPE
    "AL": "SOCO",   # Alabama Power (Southern Company) — BA
    "GA": "SOCO",   # Georgia Power (Southern Company) — BA
    "FL": "FRCC",
    "AK": "AK", "HI": "HECO",
}

# ── Per-market overrides (split states) ─────────────────────────────────
# Keyed by market_slug. Consulted BEFORE STATE_ISO. Add a slug here when a
# market's serving utility disagrees with its state's dominant operator.
MARKET_ISO_OVERRIDES = {
    # Kansas City metro, Missouri side — Evergy Metro (ex-KCP&L) is SPP.
    # The Kansas side (olathe, overland-park, lenexa) already resolves to
    # SPP via STATE_ISO; without these the same metro straddles two ISOs.
    "kansas-city":       "SPP",
    "north-kansas-city": "SPP",
    "lees-summit":       "SPP",
    "blue-springs":      "SPP",
    "independence":      "SPP",   # Independence Power & Light, SPP member
    "liberty":           "SPP",
    "gladstone":         "SPP",
    "raytown":           "SPP",
    "grandview":         "SPP",
    "riverside":         "SPP",
    "belton":            "SPP",
    "raymore":           "SPP",

    # r-iso-sweep (2026-07-28): found by probing every metro in a split state
    # against its serving utility, rather than waiting for two maps to
    # disagree. Both were live with an RTO label on a grid that is not in that
    # RTO — the harmful direction, since callers act on an RTO label.
    #
    # El Paso Electric is a WECC utility. ERCOT stops well short of it: the
    # far-west Texas grid is synchronised with the Western Interconnection,
    # not the Texas one. Reported ERCOT live on 2026-07-28.
    "el-paso":           "WECC",
    #
    # SMUD is its own balancing authority inside BANC and is NOT a CAISO
    # member. Reported CAISO live on 2026-07-28. Labelled WECC (the region)
    # rather than BANC because BANC is absent from ISO_TYPE above — add it
    # there first if a BA-level label is ever wanted here.
    "sacramento":        "WECC",
}

# NOTE (r-iso-sweep 2026-07-28): these keys are bare city slugs with no state,
# so a city name that exists in two states resolves to ONE entry for both.
# "riverside" above means Riverside MISSOURI (Kansas City metro, SPP); if a
# Riverside CALIFORNIA market is ever scored it would silently inherit SPP for
# a CAISO-adjacent grid. No Riverside market is scored today, so this is
# latent, not live — but the next same-named market makes it real. The fix
# when it matters is to key overrides on (slug, state), not slug alone.


def iso_type_of(iso: str | None) -> str:
    """Taxonomy class of a label: 'RTO', 'BA', 'REGION', or '' if unknown."""
    return ISO_TYPE.get((iso or "").upper().strip(), "")


def is_registered_label(iso: str | None) -> bool:
    """True when `iso` names an operator worth showing a reader.

    r-iso-unk (2026-08-27). 'UNK' is this codebase's not-a-label sentinel:
    every SQL predicate that counts markets without an operator reads
    `iso IS NULL OR iso = '' OR iso = 'UNK'` — main.py in three places,
    dchub_self_heal.py, and the note at the top of this module. Only the
    RENDERERS treated it as a value, so four scored markets (barueri,
    bologna, midrand, osasco) printed a literal "UNK grid" into the <title>
    of every facility page that resolved to them — 5 of 500 sampled pages,
    ~91 across the sitemap.

    ★ This is NOT the same question as iso_type_of(). A market can carry a
      perfectly real operator that has no taxonomy class here — EirGrid,
      TNB, EGAT, ENTSOE-IT — and must still display. Only the sentinel and
      the empty string are hidden.

    ★★ An EMPTY iso is a DECISION, not a gap. See the midrand convention in
       tests/test_dcpi_orphan_geography.py: johannesburg and midrand are
       deliberately carried with no registered grid-operator label, and
       johannesburg already renders with no grid clause at all. This makes
       'UNK' behave the same way rather than inventing a label for it.
    """
    v = (iso or "").strip()
    return bool(v) and v.upper() != "UNK"


def has_interconnection_queue(iso: str | None) -> bool:
    """True only for labels that name a market with a real queue.

    Gate every interconnection-queue lookup on this. A BA or REGION label
    means "no RTO queue exists for this market" — which is a real answer,
    not a lookup failure, and must not be silently coerced to a neighbour.
    """
    return (iso or "").upper().strip() in RTO_LABELS


def resolve_iso(market_slug: str | None = None,
                state: str | None = None,
                default: str = "") -> str:
    """Resolve a market's grid-operator label.

    Precedence: per-market override → state default → `default`.
    `default` lets callers preserve an explicit non-US label (ENTSOE-*,
    AESO, …) that this US-centric map should never touch.
    """
    slug = (market_slug or "").lower().strip()
    if slug in MARKET_ISO_OVERRIDES:
        return MARKET_ISO_OVERRIDES[slug]
    st = (state or "").upper().strip()
    if st in STATE_ISO:
        return STATE_ISO[st]
    return default


def resolve(market_slug: str | None = None,
            state: str | None = None,
            default: str = "") -> tuple[str, str]:
    """Same as resolve_iso(), but returns (iso, iso_type)."""
    iso = resolve_iso(market_slug, state, default)
    return iso, iso_type_of(iso)
