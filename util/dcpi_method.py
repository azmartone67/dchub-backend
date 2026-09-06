"""dcpi_method.py — the ONE source of truth for how DCPI is scored.

r-ws3-methodology (2026-07-29).

WHY THIS MODULE EXISTS
----------------------
On 2026-07-29 the only DCPI methodology DC Hub published was fiction.
https://dchub.cloud/dcpi/methodology/ (a Cloudflare Pages static file — see
routes/dcpi.py, which documents that CF intercepts /dcpi/* so a backend route
there is dead code) described:

    excess = 40*grid_headroom_mw + 25*generator_queue_velocity
           + 15*(1 - utility_subscription_ratio) + 10*local_renewable_surplus_mw
           + 10*(1 - tier1_market_overlap_score)

None of those five terms exist anywhere in this repo. It also published a
NEUTRAL verdict band that derive_verdict cannot emit, and a
/data/dcpi-history.csv download that 404s. Measured the same morning: 209 of
311 published markets (67%) carried a verdict the published bands could not
produce — e.g. north-kansas-city (excess 73.6, constraint 36.8) is UNDEFINED
under the published table and BUILD in the live index.

★ THAT 67% IS NOW EXPLAINED AND CLOSED (r-verdict-one-band, 2026-08-08). It
was NOT the fabricated page alone. dchub_self_heal.py held FOUR more
hand-copied verdict band tables, no two alike and none matching
VERDICT_BANDS, and two of them were armed and rewriting `verdict` on
PUBLISHED rows on every heal cycle. Re-measured against live Neon on
2026-08-08 the figure was 221 of 324 (68.2%), and the rewrites were the
mechanism: at 15:36 UTC the band table in fix_repair_verdict_matrix
reproduced 324 of 324 stored verdicts exactly, while these bands reproduced
103. All four are retired; derive_verdict is the only producer. See
REVISIONS 2.3.0, and dchub_self_heal.py's retirement block for the full
measurement.

The root cause is not carelessness, it is HAND-COPYING. The same pattern is
visible in routes/dcpi_explain.py, which re-types the verdict multiplier under
a comment reading "If that changes, update here too". Every consumer that
retypes a weight is a page that can silently disagree with the scorer.

So: the constants live HERE, routes/dcpi.py imports them, and
routes/dcpi_methodology.py *emits* them. A published weight and a scoring
weight are then the same Python object, not two strings that happen to match.
Same house pattern as util/iso_taxonomy.py and util/status_taxonomy.py — both
fixes for this identical bug class the same week.

WHAT IS NOT IN HERE
-------------------
Nothing that requires a DB or a network read. This module is pure constants +
pure helpers so the methodology endpoint can never 500 and so tests can assert
reproducibility without a database.

VERSIONING RULE (committed to publicly)
---------------------------------------
  MAJOR/MINOR bump  = a weight, threshold, normalisation ceiling, verdict band,
                      composite multiplier or input SOURCE changed, i.e. the
                      same market can move without the world moving.
  PATCH bump        = labels/metadata/provenance only; every published score
                      and verdict is byte-identical.
Anything that moves a score gets an entry in REVISIONS, with the markets that
moved most, because DCPI scores are UPDATE-in-place and a methodology change
therefore restates the whole back series implicitly.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────
# Version
# ─────────────────────────────────────────────────────────────────────────

# 2.0   = the scoring method in force since 2026-07-25 (r-local-granularity).
# 2.0.1 = 2026-07-29 provenance/label corrections only — scores byte-identical.
# 2.0.2 = 2026-08-08 (r-repro) published-accuracy corrections only: the
#         reproducibility claim, the index-size limitation and the queue-wait
#         ceiling label. No weight, threshold, ceiling, band or multiplier
#         moved; every published score and verdict is byte-identical.
# 2.1.0 = 2026-08-08 (r-universe-dedup). No weight, ceiling or band moved. An
#         input SOURCE did: the market list and the market CENTROID are now
#         built from de-duplicated discovered_facilities rows. Per the
#         versioning rule that is a MINOR, because the same market can move
#         without the world moving — and 36 markets' centroids did move,
#         re-pointing the local-infrastructure boxes that feed both scorers.
# 2.1.1 = 2026-08-08 precision correction to the 2.1.0 REVISIONS entry only.
#         It said "22 markets enter the scored set"; 14 of those 22 already had
#         rows and were being carried by the scored-orphan path, so only 8 are
#         new to the table. Scores and verdicts are byte-identical to 2.1.0.
# 2.2.0 = 2026-08-08 (r-radius-dedup) the local_dc_count INPUT SOURCE was
#         corrected to key duplicate visibility on duplicate_of_id instead of
#         the is_duplicate suppression flag. A MINOR bump, not a patch: the
#         versioning rule counts an input SOURCE change that moves scores, and
#         this moves constraint_score. No weight, ceiling or band moved. This
#         is the THIRD dedup-key correction dated 2026-08-08 (see also 2.1.0
#         and REVISIONS ref r-sat-dedup) — a back series diffed across this
#         date carries all of them, not this one alone.
# 2.2.1 = 2026-08-08 (r-provenance-writer) provenance only; every published
#         score and verdict is byte-identical to 2.2.0. The stamp this module
#         promises on "every score row" was missing from 8 published markets
#         because five of the six writers for market_power_scores carried
#         hand-copied column lists that never picked up data_basis_json,
#         signal_tier or method_version. The four full-scorer writes now share
#         ONE generated statement in util/dcpi_score_row.py, which also
#         refuses to write a score it cannot attribute.
# 2.3.0 = 2026-08-08 (r-verdict-one-band) no weight, ceiling or band moved —
#         VERDICT_BANDS is byte-identical to 2.2.1. What changed is that the
#         bands are now the only ones in force. dchub_self_heal.py carried
#         four more hand-copied band tables and two were armed, rewriting
#         `verdict` on published rows every heal cycle from thresholds this
#         module never published. Retiring them MOVES 220 of 324 published
#         verdicts back onto the published rule, so by the versioning rule
#         below it is a MINOR, not a patch: the same market moves without the
#         world moving. This is the verdict-side twin of 2.2.1's provenance
#         fix — same bug class (a hand-copy nothing connected to the source),
#         same remedy (one definition, imported).
DCPI_METHOD_VERSION = "2.3.0"

# The date the SCORING (not the labelling) last changed. Consumers comparing
# two history points from before/after this date are comparing two methods.
SCORING_UNCHANGED_SINCE = "2026-08-08"


# ─────────────────────────────────────────────────────────────────────────
# CONSTRAINT SCORE — high = more constrained = avoid. 0..100.
# ─────────────────────────────────────────────────────────────────────────

# Value the scorer substitutes when an input is None AT SCORING TIME. These are
# deliberately published: they are a FOURTH tier of constant, distinct from
# iso_defaults and slug_overrides, and documenting only iso_defaults would
# understate the modelled surface.
CONSTRAINT_INPUT_DEFAULTS = {
    "queue_wait_months": 18,
    "reserve_margin_pct": 12,
    "emergency_count_30d": 0,
    "demand_growth_yoy_pct": 3,
}

# Every normalisation is the same shape: linear ratio-to-ceiling, then clip to
# [0, 100]. The ceiling is "the value that scores 100".
CONSTRAINT_CEILINGS = {
    "queue_wait_months": 36.0,      # >36 months = critical
    "reserve_margin_pct": 25.0,     # inverted: <13% = critical (NERC)
    "demand_growth_yoy_pct": 12.0,
    "local_dc_count": 40.0,
}
# emergency_count_30d is not a ratio — each event is worth this many points.
CONSTRAINT_EMERGENCY_POINTS_PER_EVENT = 20

CONSTRAINT_WEIGHTS = {
    "queue_wait": 0.40,
    "reserve_margin": 0.25,
    "emergencies": 0.20,
    "demand_growth": 0.15,
}
# Bounded additive term ON TOP of a weights-sum-to-1.0 base, so the pre-clip
# maximum is 106, not 100. Zero when the key is absent — callers that never
# gathered local terms score byte-identically.
CONSTRAINT_LOCAL_COMPETITION_BONUS = 0.06


# ─────────────────────────────────────────────────────────────────────────
# EXCESS POWER SCORE — high = more buildable headroom. 0..100.
# ─────────────────────────────────────────────────────────────────────────

EXCESS_INPUT_DEFAULTS = {
    "reserve_margin_pct": 12,
    "gen_additions_12mo_mw": 0,
    "curtailment_pct": 0,
    "queue_approval_rate_pct": 50,
    "stranded_capacity_mw": 0,
    "btm_headroom_mw": 0,
}

# reserve is scored as a BONUS above the floor, spread over the span.
EXCESS_RESERVE_FLOOR_PCT = 12.0
EXCESS_RESERVE_SPAN_PCT = 13.0
EXCESS_CEILINGS = {
    "gen_additions_12mo_mw": 5000.0,
    "curtailment_pct": 10.0,
    "stranded_capacity_mw": 1000.0,
    "btm_headroom_mw": 500.0,
    # queue_approval_rate_pct is already a percentage — clipped, not scaled.
}

EXCESS_WEIGHTS = {
    "reserve_margin": 0.20,
    "gen_additions": 0.20,
    "curtailment": 0.20,
    "queue_approval": 0.15,
    "stranded": 0.15,
    "btm_headroom": 0.10,
}
EXCESS_LOCAL_GRID_BONUS = 0.08

# Local grid-access sub-index (0..100 before the bonus weight above).
LOCAL_GRID_SUBSTATION_CEILING = 300.0
LOCAL_GRID_SUBSTATION_POINTS = 55.0
LOCAL_GRID_KV_POINTS = ((345.0, 25.0), (230.0, 12.0))   # first match wins
LOCAL_GRID_GEN_CEILING = 3000.0
LOCAL_GRID_GEN_POINTS = 20.0


# ─────────────────────────────────────────────────────────────────────────
# VERDICT + COMPOSITE
# ─────────────────────────────────────────────────────────────────────────

# Ordered: first row whose BOTH conditions hold wins. There is no NEUTRAL
# band — the fabricated page invented one.
VERDICT_BANDS = (
    ("BUILD",   {"excess_min": 65.0, "constraint_max": 50.0}),
    ("CAUTION", {"excess_min": 50.0, "constraint_max": 70.0}),
)
VERDICT_FALLBACK = "AVOID"


# ─────────────────────────────────────────────────────────────────────────
# The verdict, as a publishable measurement
# ─────────────────────────────────────────────────────────────────────────
#
# r-publish-the-verdict (2026-09-06). BUILD/CAUTION/AVOID is displayed on
# /dcpi, /markets and /pockets and was published as structured data by none of
# them — the single most citable fact about a market reached an agent only as
# prose.
#
# The sentence is GENERATED from VERDICT_BANDS and VERDICT_FALLBACK, never
# typed, and it lives here because this module already owns them: it is what
# /api/v1/dcpi/methodology serves. A second hand-written account of the bands
# is not hypothetical — the static methodology page published a NEUTRAL band
# for months that derive_verdict could not produce, and 67% of published
# markets carried a verdict that page could not explain.
#
# ★ WHAT THE SENTENCE MUST SAY, and why each part is load-bearing:
#
#   * THE DOMAIN, from the bands themselves. LOW_SIGNAL is deliberately NOT in
#     it: it is documented, carries a 0.35 composite multiplier, is accepted by
#     ?verdict= and counted by iso_snapshot — and has NO WRITER AT ALL
#     ("unreachable in practice", fallbacks list). Publishing it as a value the
#     scorer emits would re-create the NEUTRAL defect exactly.
#
#   * TWO INPUTS, NOT THREE. derive_verdict(constraint, excess) does not take
#     time-to-power. That matters here more than anywhere else, because the
#     Dataset these measures ship in publishes Time to Power immediately
#     beside the verdict, and an agent reading them as one group would
#     conclude a faster interconnect could move a market out of AVOID. It
#     cannot.
#
#   * AVOID IS THE FALLBACK, so it means "did not clear a band" — including
#     for a market whose inputs are missing — not "actively bad". An agent
#     that reads every AVOID as a judgement will overstate what DCPI claims.

def verdict_domain() -> tuple:
    """The verdicts derive_verdict can actually return, banded first."""
    return tuple(v for v, _b in VERDICT_BANDS) + (VERDICT_FALLBACK,)


def verdict_basis() -> str:
    """The measurementTechnique for a published verdict.

    ★ NO apostrophe, and NO ASCII >= or <=. This is interpolated into ld+json,
    where Jinja autoescape rewrites & < > \' " into entities a JSON parser will
    not decode — and a band description is almost all comparison operators, so
    the naive spelling would have shipped "excess-power &gt;= 65" to every
    agent. The unicode forms are not escaped.
    """
    bands = "; ".join(
        f"{v} when excess-power ≥ {b['excess_min']:g} and grid-constraint "
        f"≤ {b['constraint_max']:g}"
        for v, b in VERDICT_BANDS)
    return (
        f"DC Hub Power Index verdict. One of "
        f"{', '.join(verdict_domain())}. Derived from exactly two inputs, "
        f"the excess-power and grid-constraint scores: {bands}; otherwise "
        f"{VERDICT_FALLBACK}. Time-to-power is NOT an input — a faster "
        f"interconnect cannot move a market out of {VERDICT_FALLBACK}. "
        f"{VERDICT_FALLBACK} is the fallback, so it means the market did not "
        f"clear a band, including when an input is missing; it is not a claim "
        f"that the market is actively bad. A label, not a score: not "
        f"orderable and not comparable with a number."
    )

COMPOSITE_WEIGHTS = {
    "excess": 0.60,
    "inverse_constraint": 0.30,     # applied to (100 - constraint)
    "time_to_power": 0.10,
}
COMPOSITE_TTP_CAP_MONTHS = 60.0
COMPOSITE_VERDICT_MULTIPLIERS = {
    "BUILD": 1.00,
    "CAUTION": 0.85,
    "AVOID": 0.60,
    "LOW_SIGNAL": 0.35,
}
COMPOSITE_DEFAULT_MULTIPLIER = 1.00


# ─────────────────────────────────────────────────────────────────────────
# TIME TO POWER
# ─────────────────────────────────────────────────────────────────────────

TTP_INPUT_DEFAULTS = {"queue_wait_months": 24, "reserve_margin_pct": 12}
# (reserve_margin_pct threshold, multiplier applied to queue_wait_months),
# evaluated in order; TTP_ADJ_SCARCE applies below TTP_SCARCE_RESERVE_PCT.
TTP_ADJUSTMENTS = ((20.0, 0.6), (16.0, 0.8))
TTP_SCARCE_RESERVE_PCT = 10.0
TTP_ADJ_SCARCE = 1.4
TTP_ADJ_DEFAULT = 1.0


# ─────────────────────────────────────────────────────────────────────────
# QUEUE-WAIT PROXY + LOCAL SATURATION (both rewrite inputs before scoring)
# ─────────────────────────────────────────────────────────────────────────

# queue_wait_months is queue DEPTH, not a measured wait: queue_date stores a
# projected COD in several ISOs and goes negative (TX ~ -16 months), so it is
# not a submission-to-energisation interval. Published as a proxy, with its
# calibration and its band, because it saturates.
QUEUE_WAIT_PROXY = {
    "formula": "clip(12.0 + active_queue_GW * 0.6, 12, 66) months",
    "base_months": 12.0,
    "months_per_gw": 0.6,
    "clip_months": [12.0, 66.0],
    "saturates_at_gw": 90.0,
    "saturation_note": (
        "any state with >=90 GW of active queue pins at the 66-month cap. "
        "Texas is ~462 GW, so every Texas market shares one identical value: "
        "a saturated live input is functionally a constant."
    ),
}

SATURATION_CEILINGS = {
    "local_facility_count": 400.0,   # Ashburn-class
    "local_operational_mw": 8000.0,
    "local_pipeline_mw": 5000.0,
    "local_provider_count": 40.0,
}
SATURATION_WEIGHTS = {
    "local_facility_count": 0.40,
    "local_operational_mw": 0.25,
    "local_pipeline_mw": 0.15,
    "local_provider_count": 0.20,
}
SATURATION_SCALE = "log: ln(1+v) / ln(1+ceiling)"
# The three inputs the saturation index REWRITES before they reach a scorer.
SATURATION_REWRITES = {
    "queue_wait_months": "multiplied by (0.90 + 0.45 * saturation)",
    "demand_growth_yoy_pct": "increased by (4.0 * saturation) percentage points",
    "btm_headroom_mw": "multiplied by (1 - 0.40 * saturation)",
}
# Applied AFTER the 12-66 clip, so the published field can exceed the band.
SATURATION_BREACHES_QUEUE_BAND = True

# 66 * 1.35. This bounds ONE of the three paths that can fill
# queue_wait_months — the queue-DEPTH PROXY path, which is the only one that
# is clipped to [12, 66] before the saturation multiplier is applied.
#
# r-repro (2026-08-08): this number was published as
# `queue_wait_true_ceiling_months`, i.e. as the ceiling of the PUBLISHED
# FIELD. It is not. Measured against all 315 published markets on 2026-08-08,
# 6 exceed it:
#
#     london     144.0  slug_override literal (saturation SKIPPED entirely)
#     amsterdam  120.0  slug_override literal
#     rotterdam  107.1  iso_defaults[ENTSOE-NL]=96 x 1.116 saturation
#     manchester  96.0  slug_override literal
#     milan       94.9  iso_defaults[ENTSOE-IT]=78 x 1.217 saturation
#     edinburgh   90.9  iso_defaults[NGESO]=84    x 1.082 saturation
#
# The TRANSFORM is correct and needs no change: the highest proxy-path market
# measured 87.6 months, inside 89.1. What was wrong is the CEILING — it
# generalised a proxy-path bound to a field that two constant-fed paths also
# write, and neither of those is clipped to 66 first:
#
#   1. proxy       clip(12 + GW*0.6, 12, 66) then x[0.90..1.35]  -> <= 89.1
#   2. iso_default iso_defaults[iso] (UNCLIPPED) then x[0.90..1.35]
#   3. override    slug_overrides[slug] (UNCLIPPED); saturation is skipped
#                  for override markets, so the literal publishes as-is
#
# Paths 2 and 3 read constants that live in routes/dcpi.py. Restating their
# maxima here is exactly the hand-copy bug this module exists to kill, so the
# honest published ceiling is MEASURED from the live index and injected
# (see method_block(live_counts=...)), never retyped.
QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS = 89.1

# Deprecated alias. Kept so an existing importer does not break, but it names
# the proxy path now — there is no single analytic "true ceiling" for the
# published field, which is the whole point of the correction above.
QUEUE_WAIT_TRUE_CEILING_MONTHS = QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS

QUEUE_WAIT_FILL_PATHS = (
    {"path": "queue_depth_proxy", "clipped_to_66_first": True,
     "saturation_applies": True,
     "ceiling_months": QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS,
     "note": "the only path this module can bound analytically"},
    {"path": "iso_default_constant", "clipped_to_66_first": False,
     "saturation_applies": True, "ceiling_months": None,
     "note": ("iso_defaults[iso] in routes/dcpi.py is an UNCLIPPED per-ISO "
              "constant; the saturation multiplier is applied on top of it")},
    {"path": "slug_override_constant", "clipped_to_66_first": False,
     "saturation_applies": False, "ceiling_months": None,
     "note": ("a hand-calibrated constant publishes verbatim — the saturation "
              "rewrite is skipped for override markets")},
)


# ─────────────────────────────────────────────────────────────────────────
# INPUT REGISTRY — what each score input is, and what happens when it is absent
# ─────────────────────────────────────────────────────────────────────────

# live_capable: is there ANY code path that can fill this from a real table?
INPUTS = (
    {
        "name": "queue_wait_months", "units": "months", "live_capable": True,
        "source": "interconnect_queue (active rows per state) via _state_queue_depth",
        "derivation": QUEUE_WAIT_PROXY["formula"],
        "missing_behaviour": (
            "falls back to iso_defaults[iso]; on a DB error it also falls "
            "back, because the adapter cannot distinguish an empty result "
            "from a failed query"),
    },
    {
        "name": "queue_capacity_mw", "units": "MW", "live_capable": True,
        "source": "interconnect_queue SUM(capacity_mw) of active rows",
        "derivation": "raw sum; reported, not scored",
        "missing_behaviour": "stays null — never modelled",
    },
    {
        "name": "gen_additions_12mo_mw", "units": "MW", "live_capable": True,
        "source": "planned_generators (EIA-860M), statuses (U)/(V)/(T)/(TS) "
                  "with a planned COD inside the next 12 months",
        "derivation": "SUM(capacity_mw); regulatory-pending (P)/(L) excluded",
        "missing_behaviour": (
            "the scorer reads `or 0`, i.e. it is treated as ZERO additions. "
            "That is a penalty, not neutrality"),
    },
    {
        "name": "reserve_margin_pct", "units": "%", "live_capable": True,
        "source": "modelled ISO planning anchor BLENDED with grid_telemetry",
        "derivation": ("effective = anchor + clamp(live_operating_reserve, "
                       "+/-6.0), floored at 0; telemetry older than 180 "
                       "minutes is ignored"),
        "missing_behaviour": "anchor used unchanged and labelled modelled",
    },
    {
        "name": "curtailment_pct", "units": "%", "live_capable": False,
        "source": None,
        "derivation": "iso_defaults / slug_overrides constant",
        "missing_behaviour": "always modelled — there is no live source",
    },
    {
        "name": "queue_approval_rate_pct", "units": "%", "live_capable": False,
        "source": None,
        "derivation": "iso_defaults / slug_overrides constant",
        "missing_behaviour": "always modelled — there is no live source",
    },
    {
        "name": "btm_headroom_mw", "units": "MW", "live_capable": False,
        "source": None,
        "derivation": "iso_defaults / slug_overrides constant, then "
                      "down-weighted by local saturation",
        "missing_behaviour": "always modelled — there is no live source",
    },
    {
        "name": "stranded_capacity_mw", "units": "MW", "live_capable": False,
        "source": None,
        "derivation": "present only in a handful of hand-written slug_overrides",
        "missing_behaviour": "null -> the scorer reads 0",
    },
    {
        "name": "demand_growth_yoy_pct", "units": "% YoY", "live_capable": False,
        "source": None,
        "derivation": "global default 4.0, then increased by local saturation",
        "missing_behaviour": "always modelled — there is no live source",
    },
    {
        "name": "emergency_count_30d", "units": "count", "live_capable": False,
        "source": None,
        "derivation": None,
        "missing_behaviour": (
            "NEVER ASSIGNED ANYWHERE. The scorer reads `or 0`, so "
            f"{CONSTRAINT_WEIGHTS['emergencies']:.0%} of every constraint "
            "score is a permanent zero for every market at every signal tier"),
    },
)

# Bounded local-infrastructure terms. Any failure, missing coordinates, or a
# (0,0) centroid yields a zero-dict — deliberately never a penalty.
LOCAL_INFRA_TERMS = (
    {"name": "local_substation_count", "radius_km": 40,
     "source": "substations (HIFLD, US-only)", "feeds": "excess"},
    {"name": "local_max_kv", "radius_km": 40,
     "source": "substations MAX(voltage_kv)", "feeds": "excess"},
    {"name": "local_gen_mw", "radius_km": 60,
     "source": "gem_power WHERE status='operating' (GEM, global)",
     "feeds": "excess"},
    # r-radius-dedup (2026-08-08): this string is PUBLISHED at
    # /api/v1/dcpi/methodology, so it must name the predicate the scorer really
    # runs. It said COALESCE(is_duplicate,0)=0 while routes/dcpi.py ran the same
    # thing — both were the wrong key, and correcting only the SQL would have
    # left the published methodology describing a query that no longer exists.
    {"name": "local_dc_count", "radius_km": 25,
     "source": "discovered_facilities WHERE duplicate_of_id IS NULL",
     "feeds": "constraint"},
)


# ─────────────────────────────────────────────────────────────────────────
# SIGNAL TIER
# ─────────────────────────────────────────────────────────────────────────

SIGNAL_TIER = {
    "tracked_adapters": ["interconnect_queue", "planned_generators",
                         "grid_telemetry"],
    "rule": ("full = all 3 live-capable adapters returned data; "
             "partial = 1 or 2; low = 0, OR the ISO fell through to the "
             "WECC default"),
    "null_semantics": ("NULL means the writer of that row did not record a "
                       "tier. Readers MUST surface it as unknown, never "
                       "coerce it to 'low' — that would invent a measurement"),
    "scope_note": ("'full' means every adapter that CAN be live was live — "
                   "NOT that every score input is measured"),
    "affects_scores": False,
    "always_modeled_inputs": [i["name"] for i in INPUTS if not i["live_capable"]],
    "never_populated_inputs": ["emergency_count_30d"],
    "adapter_null_semantics": ("the queue and generator adapters cannot "
                               "distinguish an empty result from a failed "
                               "query, so silent_adapters is NOT an error count"),
}


# ─────────────────────────────────────────────────────────────────────────
# FALLBACKS THAT CAN SILENTLY MOVE A SCORE
# ─────────────────────────────────────────────────────────────────────────

FALLBACKS = (
    {"id": "iso_default_fail_open", "where": "routes/dcpi.py gather_metrics_for_market",
     "effect": ("iso_defaults.get(iso, iso_defaults['WECC']) — an unrecognised "
                "ISO silently scores on Western-grid parameters. Cost a real "
                "published defect: ~22 Southeast markets published '500 MW "
                "behind-the-meter headroom' that was WECC's constant. Now "
                "DETECTED (signal_tier -> low) but still not PREVENTED")},
    {"id": "scorer_or_literals", "where": "compute_constraint_score / compute_excess_power_score",
     "effect": ("inputs that are still None at scoring time take the "
                "CONSTRAINT_INPUT_DEFAULTS / EXCESS_INPUT_DEFAULTS values, "
                "which are DIFFERENT numbers from iso_defaults — a fourth "
                "tier of constant")},
    {"id": "slug_override_replaces_live", "where": "the slug_overrides metrics.update()",
     "effect": ("a hand-calibrated constant overwrites a live-read value. "
                "Until 2026-07-29 the field kept its 'live' provenance label; "
                "the override now revokes it")},
    {"id": "saturation_breaches_queue_band", "where": "the local-saturation rewrite",
     "effect": (f"on the queue-DEPTH PROXY path queue_wait_months is clipped "
                f"to 12-66 and THEN multiplied by up to 1.35, so the published "
                f"field can reach "
                f"{QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS} months")},
    {"id": "queue_wait_constant_paths_are_unclipped",
     "where": "iso_defaults / slug_overrides in routes/dcpi.py",
     "effect": ("the 12-66 clip belongs to the queue-depth PROXY, not to the "
                "field. When queue_wait_months comes from a per-ISO default or "
                "a slug_override it is NEVER clipped to 66 first, so the "
                f"published value can exceed the {QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS}-month "
                "proxy ceiling. Three slug_override markets (london, "
                "amsterdam, manchester) are STRUCTURALLY above it — their "
                "literals are 144, 120 and 96 months and the saturation "
                "rewrite is skipped for them entirely. A few per-ISO-default "
                "markets (rotterdam, milan, edinburgh, rome) sit just either "
                "side of it and cross in both directions as the saturation "
                "multiplier moves on each recompute, so the breach COUNT is "
                "not a constant: read "
                "local_saturation.queue_wait_markets_over_proxy_ceiling, "
                "which is measured per request. These are deliberate "
                "calibrations — London's interconnection queue really is "
                "~12 years — the DEFECT was publishing 89.1 as the field's "
                "ceiling. See queue_wait_fill_paths")},
    {"id": "queue_proxy_saturation", "where": QUEUE_WAIT_PROXY["formula"],
     "effect": QUEUE_WAIT_PROXY["saturation_note"]},
    {"id": "no_international_queue", "where": "interconnect_queue",
     "effect": ("US/CA only, and international state spellings do not match, "
                "so intl markets score modeled_estimate. Domestic coverage is "
                "partial too — some states return no queue rows at all")},
    {"id": "adapters_swallow_exceptions", "where": "every adapter + the saturation block",
     "effect": ("a DB outage degrades the whole index to constants without a "
                "single error surfacing")},
    {"id": "per_process_caches", "where": "_TELEMETRY_CACHE / _QUEUE_STATE_CACHE / "
                                          "_GEN_ADD_STATE_CACHE / _LOCAL_INFRA_CACHE",
     "effect": ("per-process, 60s TTL (_LOCAL_INFRA_CACHE unbounded), so two "
                "workers can score the same market from different cache states "
                "within one run")},
    {"id": "lite_recompute_second_writer", "where": "POST /api/v1/dcpi/lite-recompute",
     "effect": ("a second writer to market_power_scores with a completely "
                "different formula and NULL time_to_power / data_basis / "
                "signal_tier / method_version. It currently scores ZERO "
                "markets (it raises per market inside a swallow-all), so it "
                "cannot be the cause of any unrecorded field today")},
    {"id": "low_signal_unreachable", "where": "derive_verdict",
     "effect": ("LOW_SIGNAL is a documented filter value carrying a 0.35 "
                "composite multiplier, and it has NO WRITER AT ALL. It is "
                "accepted by the ?verdict= filter, excluded by default from "
                "the leaderboard, counted by iso_snapshot's low_signal_count "
                "and rendered as the /dcpi 'Monitoring' tab — all of which "
                "are permanently empty. Until 2026-08-08 the only thing that "
                "could emit it was a band table in dchub_self_heal.py, which "
                "required a score of exactly 0; iso_defaults guarantee that "
                "never happens, so it never fired either. That table is "
                "retired (2.3.0) and derive_verdict returns only BUILD, "
                "CAUTION or AVOID, so the value is now unreachable BY "
                "CONSTRUCTION rather than by arithmetic accident")},
)


# ─────────────────────────────────────────────────────────────────────────
# CADENCE + REVISION POLICY
# ─────────────────────────────────────────────────────────────────────────

CADENCE = {
    "schedule": "approximately 4x/day",
    "cron": "5 6,12,18,0 * * * (UTC), .github/workflows/dcpi-daily.yml",
    "observed_drift_minutes": "30-45, routinely",
    "authoritative_timestamp": "computed_at on the row — NOT the cron time",
    "note": ("Do not read this as 'every 6 hours'. Runs start 30-45 minutes "
             "after nominal often enough that the as-of stamp is the only "
             "trustworthy answer to 'how fresh is this'"),
    "other_triggers": [
        "POST /api/v1/dcpi/recompute (admin key)",
        "the heartbeat refresh",
        "a cold-start recompute when the dashboard renders zero rows",
    ],
}

REVISION_POLICY = {
    "scores_are_revised_in_place": True,
    "current_state_table": ("market_power_scores holds exactly ONE row per "
                            "market, forever, with computed_at = NOW(). It is "
                            "current-state, not an as-of archive"),
    "official_series": ("dcpi_daily_snapshots — one point per market per day. "
                        "A same-day rerun OVERWRITES that day's point"),
    "restatement": ("a methodology change restates the entire back series "
                    "implicitly, because there is no as-of archive to restate "
                    "against. Every such change is enumerated in revisions[]"),
    "version_bump_commitment": (
        "method_version bumps on any change to a weight, threshold, "
        "normalisation ceiling, verdict band, composite multiplier or input "
        "SOURCE. A PATCH bump means labels/metadata only and every score is "
        "byte-identical. Normalisation ceilings are otherwise left alone, so "
        "a market moving means the market moved"),
    "history_endpoint": "/api/v1/dcpi/history (single market is free to cite)",
}

REVISIONS = (
    {"date": "2026-07-17", "version": "1.8", "ref": "r-declone-2",
     "scores_changed": True, "restated_back_series": True,
     "what": ("per-market de-cloning of the ISO-inherited inputs, so markets "
              "in one ISO stop sharing identical scores"),
     "observed_moves": "midland-tx excess 55.9 -> 60.5, constraint 25.8 -> 22.8"},
    {"date": "2026-07-24", "version": "1.9", "ref": "queue+gen adapter rewrite",
     "scores_changed": True, "restated_back_series": True,
     "what": ("queue_wait_months re-derived from real per-state active queue "
              "DEPTH, and gen_additions_12mo_mw repointed from a table that "
              "had neither of the columns it queried (so it had been 0 for "
              "every market) to planned_generators"),
     "observed_moves": "UNMEASURED — no per-market before/after was captured"},
    {"date": "2026-07-25", "version": "2.0", "ref": "r-local-granularity",
     "scores_changed": True, "restated_back_series": True,
     "what": ("added the bounded local-infrastructure terms: local DC density "
              "into constraint (<= +6) and local substation/HV/generation "
              "access into excess (<= +8)"),
     "observed_moves": ("excess: atlanta 35.6 -> 49.2, chicago 17.6 -> 34.0, "
                        "phoenix 34.8 -> 62.8, midland-tx 60.5 -> 85.7. The "
                        "grid did not change on that date; the method did")},
    {"date": "2026-07-28", "version": "2.0", "ref": "r-ws3-signal-tier",
     "scores_changed": False, "restated_back_series": False,
     "what": ("signal_tier persisted per row. Non-numeric and invisible to "
              "both scorers, so every score and verdict is byte-identical"),
     "observed_moves": "none — metadata only"},
    {"date": "2026-07-29", "version": "2.0.1", "ref": "r-ws3-methodology",
     "scores_changed": False, "restated_back_series": False,
     "what": ("this document, emitted from the scorer's own constants; "
              "method_version stamped on every score row and daily snapshot; "
              "and a provenance correction — a slug_override that overwrites "
              "a live-read value now REVOKES that field's 'live' label "
              "instead of keeping it"),
     "observed_moves": ("no score moves. Some override markets flip their "
                        "published data_basis label from 'mixed' to "
                        "'modeled_estimate', which is the correction")},
    {"date": "2026-08-08", "version": "2.0.2", "ref": "r-repro",
     "scores_changed": False, "restated_back_series": False,
     "what": ("three published-accuracy corrections, no scoring change. "
              "(1) The reproducibility claim said constraint_score was "
              "reproducible from the published fields; measured across all "
              "315 markets, 0 reproduce, because demand_growth_yoy_pct "
              "(weight 0.15) and local_dc_count (bonus 0.06) are not columns "
              "on market_power_scores and so are published for no market. "
              "The claim now names what is reproducible (composite_score and "
              "verdict — 315/315 exact) and what is not. "
              "(2) known_limitations' index-size entry was a hardcoded "
              "'311 markets / 317 rows'; live it is 315 / 322. It is now "
              "generated from injected live counts. "
              "(3) queue_wait_true_ceiling_months published 89.1 as the "
              "ceiling of the published field; several markets exceed it "
              "because the per-ISO-default and slug_override paths are not "
              "clipped to 66 first. The transform was correct; the ceiling "
              "was wrong, and is renamed to the proxy path it actually "
              "bounds. The breach count and the field maximum are now "
              "measured per request rather than written down, because the "
              "per-ISO-default markets cross the line on each recompute"),
     "observed_moves": ("none — every score, verdict and composite is "
                        "byte-identical. Documentation only")},
    {"date": "2026-08-08", "version": "2.1.0", "ref": "r-universe-dedup",
     "scores_changed": True, "restated_back_series": True,
     "what": ("the market list and the market CENTROID are now built from "
              "de-duplicated discovered_facilities rows. 38% of that table "
              "(9,459 of 24,859) is twin rows carrying a duplicate_of_id, and "
              "the loader counted them: it admitted cities on the >=3-facility "
              "bar that did not have 3 real buildings, ranked the fixed "
              "200-market cap on padded counts, and took the coordinate median "
              "over duplicated points. No weight, ceiling or verdict band "
              "moved — only which markets exist and where they sit"),
     "observed_moves": ("22 markets enter the live-sourced 200, displacing 22 "
                        "that were ranked on twins. Of those 22, only 8 are NEW "
                        "to the scored table (laurel, lenoir, luckey, maiden, "
                        "modesto, monroe, salem, west-chester); the other 14 — "
                        "including mount-pleasant (3,600 MW) and abilene "
                        "(3,100 MW) — already had rows and were being carried "
                        "by the scored-orphan path with frozen coordinates, so "
                        "what changes for them is that they are refreshed from "
                        "the facility table again rather than newly published. "
                        "None LEAVE the universe, because every displaced slug "
                        "is already in market_power_scores and is re-adopted by "
                        "that same orphan path. Facility counts fall for 177 of "
                        "178 retained markets (ashburn 308 -> 163, boardman "
                        "51 -> 5). 36 centroids move >0.5 km, 6 move >5 km: "
                        "chattanooga 21.5 km, west-texas 14.2 km, fort-worth "
                        "8.2 km, council-bluffs 7.0 km, douglasville 6.2 km, "
                        "boise 5.9 km. Those shifts re-point the local-infra "
                        "boxes: west-texas reads local_max_kv 345 -> 138 and "
                        "local_gen_mw 584 -> 145; fort-worth local_gen_mw "
                        "3,059 -> 5,512. The isolated constraint local-density "
                        "term moves at most 0.8 pts (west-texas -0.8, "
                        "council-bluffs +0.4, chattanooga +0.3); the excess "
                        "side moves more, but is not reproducible from the "
                        "published fields so it is not restated numerically "
                        "here")},
    {"date": "2026-08-08", "version": "2.2.0", "ref": "r-radius-dedup",
     "scores_changed": True, "restated_back_series": True,
     "what": ("local_dc_count (the 25 km local-competition input) now scopes "
              "duplicate visibility on duplicate_of_id, the pointer, instead "
              "of the is_duplicate suppression flag. The flag was wrong in "
              "both directions: 3,286 rows carry a pointer while UNflagged and "
              "were counted twice as competition against themselves, while "
              "1,510 rows are flagged with no pointer — keeperless "
              "suppressions, i.e. real facilities that were dropped from the "
              "count. Same predicate the facility lists and the canonical "
              "already use. No weight, ceiling or verdict band moved"),
     "observed_moves": ("direction is asymmetric by construction: markets whose "
                        "25 km box holds pointed-but-unflagged twins score "
                        "DOWN (competition removed), markets holding "
                        "flagged-but-unpointed rows score UP (real facilities "
                        "restored), and a market holding both nets out. The "
                        "term is bounded, so no market can move more than the "
                        "0.06 local-density weight allows. MEASUREMENT BASIS — "
                        "the per-market figures for this change were taken on "
                        "2026-08-08 against the PRE-2.1.0 market universe: 147 "
                        "of 316 markets moved (132 down, 15 up, mean |delta| "
                        "0.47 points, max 1.5; largest miami 52.8 -> 51.3 and "
                        "doral 48.2 -> 46.7 on 40 -> 30 facilities, manchester "
                        "71.7 -> 70.2 on 38 -> 28; largest "
                        "rise spokane 44.4 -> 44.9 on 4 -> 7), with ZERO "
                        "verdicts flipped. Version 2.1.0 then re-pointed the "
                        "local-infrastructure boxes by moving 36 centroids and "
                        "swapping 22 markets, and the index now publishes 315 "
                        "markets, not 316 — so those per-market deltas are "
                        "SUPERSEDED as exact values and are recorded here for "
                        "the shape of the change, not as the current diff. The "
                        "mechanism, the direction and the bound are unaffected "
                        "by that re-pointing. NOTE: ref r-sat-dedup applied "
                        "the same dedup rule to the market SATURATION "
                        "footprint on this same date and also moved published "
                        "scores without its own entry here — a back series "
                        "diffed across 2026-08-08 carries that change, this "
                        "one and 2.1.0 together, not any one alone")},
    {"date": "2026-08-08", "version": "2.2.1", "ref": "r-provenance-writer",
     "scores_changed": False, "restated_back_series": False,
     "what": ("the 2.0.1 entry below claims method_version is stamped on "
              "every score row. Measured 2026-08-08 09:24 UTC that was FALSE "
              "for 8 published markets — laurel, lenoir, luckey, maiden, "
              "modesto, monroe, salem and west-chester, the 8 rows 2.1.1 "
              "describes as new to the table, inserted 08:51 UTC with "
              "constraint_score, excess_power_score and verdict but "
              "method_version, signal_tier and data_basis_json all NULL. The "
              "cause was six hand-written INSERT column lists for one table: "
              "only the daily recompute's was updated when the three "
              "provenance columns were added, and the gap-filling writer that "
              "admits new markets was not. All four full-scorer writes now "
              "share one generated statement, so the claim is true by "
              "construction rather than by four people remembering"),
     "observed_moves": ("none — no weight, ceiling, band or input source "
                        "moved, and every published score and verdict is "
                        "byte-identical. The 8 rows needed no backfill: the "
                        "14:34-14:49 UTC recompute re-scored and stamped them "
                        "2.2.0 the same day, which is the method that actually "
                        "produced their current numbers. Two data-integrity "
                        "fixes ride along and are NOT score restatements: the "
                        "two watchdog writers now preserve a stored centroid "
                        "instead of overwriting it with a dynamic market's "
                        "NULL, and now write iso_type. The lite two-input "
                        "approximation is additionally fenced out of rows the "
                        "full method owns, so it can no longer overwrite a "
                        "full score while inheriting its version stamp")},
    {"date": "2026-08-08", "version": "2.3.0", "ref": "r-verdict-one-band",
     "scores_changed": False, "restated_back_series": True,
     "what": ("VERDICT_BANDS is byte-identical to 2.2.1 and no weight, "
              "ceiling or multiplier moved. What changed is that these bands "
              "are now the ONLY ones in force. dchub_self_heal.py carried "
              "four more verdict band tables, hand-typed into that file, no "
              "two alike and none matching this module — and two were armed, "
              "rewriting `verdict` on rows matching `published = true OR "
              "tier_required IS NULL OR tier_required != 'lite-pro'`, i.e. on "
              "the public index, on every heal cycle. All four are retired, "
              "along with a fifth job that wrote the label NODATA, which was "
              "never in the published alphabet. routes/dcpi.py's "
              "derive_verdict — which imports VERDICT_BANDS — is now the only "
              "producer of a stored verdict. This is the verdict-side twin of "
              "2.2.1: same bug class, same remedy"),
     "observed_moves": (
         "MEASURED against live Neon on 2026-08-08 over 324 published rows. "
         "The published verdict was OSCILLATING, so the size of the defect "
         "depends on which writer ran last: at 15:36 UTC, with a healer last, "
         "221 of 324 (68.2%) carried a verdict these bands cannot produce; at "
         "17:26 UTC the 162 rows the scorer sweep had reached were 162/162 "
         "canonical while the 106 it had not were ~33%. That oscillation is "
         "the finding — the scorer wrote the published rule ~4x/day and the "
         "healers overwrote it, each undoing the other, and the two armed "
         "healers disagreed with EACH OTHER minutes apart (15:36:09 "
         "CAUTION=256 AVOID=41 BUILD=27, then 15:36:44 CAUTION=241 BUILD=57 "
         "AVOID=32). RETIRING THEM MOVES 220 of 324 markets, and the moves "
         "are DIRECTIONAL, not noise: every one is downward toward the "
         "published rule. 187 markets served as CAUTION are canonically "
         "AVOID (spokane, madison, copenhagen, quincy, helsinki, akron, "
         "albany, alpharetta, ashburn, atlanta, aurora, baltimore, bangkok, "
         "barcelona, barueri, batam, baton-rouge, altoona, anchorage, "
         "andover, asheville, auckland and 165 more), and 33 served as BUILD "
         "are canonically CAUTION (appalachia-coal, aurora-co, stockholm, "
         "centennial, englewood, los-lunas, enterprise, west-jordan, "
         "south-west-jordan, the-dalles, pacific-nw-rural, winnipeg and 21 "
         "more). So the index had been reading as more buildable than the "
         "scorer found it to be. NO BACKFILL IS NEEDED and none was run: the "
         "17:26 sweep above is direct evidence that the scorer restores every "
         "published row to the canonical label on its own, so removing the "
         "overwriters is self-correcting within one recompute. A back series "
         "diffed across 2026-08-08 carries this alongside 2.1.0, 2.2.0 and "
         "ref r-sat-dedup, not any one alone")},
)


# ─────────────────────────────────────────────────────────────────────────
# KNOWN LIMITATIONS — published plainly, because diligence will find them
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# REPRODUCIBILITY — which published score a third party can actually recompute
# ─────────────────────────────────────────────────────────────────────────
#
# r-repro (2026-08-08). Until today this module published, verbatim:
#
#   "constraint_score and composite_score are reproducible from the fields
#    published on /api/v1/dcpi/scores/<slug> using the weights above."
#
# Half of that was true and half of it was false, and the false half was the
# one an analyst would try first. Measured against ALL 315 published markets:
#
#   composite_score   315/315 reproduce EXACTLY (to the published decimal)
#   verdict           315/315 reproduce EXACTLY
#   constraint_score    0/315 reproduce. Residual 1.22 .. 21.00, mean 10.74
#
# The cause is not a wrong weight — it is that two of constraint_score's five
# inputs are not published on the scores endpoint for ANY market:
#
#   demand_growth_yoy_pct   weight 0.15  -> up to 15.0 points
#   local_dc_count          bonus  0.06  -> up to  6.0 points
#
# Neither is a column on market_power_scores, so the endpoint's `SELECT *`
# cannot emit them. Johor is the worst case and lands on exactly the
# theoretical maximum: 21.0 of its 41.0 constraint points are underivable.
#
# (demand_growth_yoy_pct sometimes LEAKS into top_risks_json as prose —
# "18.0% YoY demand growth" — but only when it is a top risk. It is absent for
# tokyo, chicago and singapore among others, so it is not a field a consumer
# can rely on. A string that is present only when the number is alarming is
# not a published input.)
#
# The honest fix available inside this module is (b): CORRECT THE CLAIM.
# Option (a) — publish the two fields so the claim becomes true — is strictly
# better and is written up in the PR body, but it needs an ALTER TABLE plus a
# writer change in routes/dcpi.py, which this change does not own.

CONSTRAINT_INPUTS_PUBLISHED = (
    "queue_wait_months", "reserve_margin_pct", "emergency_count_30d")
CONSTRAINT_INPUTS_NOT_PUBLISHED = ("demand_growth_yoy_pct", "local_dc_count")

# Weight carried by each unpublished input, DERIVED from the weight table
# rather than retyped — if a weight moves, the published gap moves with it.
CONSTRAINT_UNPUBLISHED_WEIGHTS = {
    "demand_growth_yoy_pct": CONSTRAINT_WEIGHTS["demand_growth"],
    "local_dc_count": CONSTRAINT_LOCAL_COMPETITION_BONUS,
}
# Worst-case points of constraint_score that no published field can explain.
MAX_UNDERIVABLE_CONSTRAINT_POINTS = round(
    100.0 * sum(CONSTRAINT_UNPUBLISHED_WEIGHTS.values()), 1)


def _queue_wait_ceiling_limitation(queue_wait_max=None, queue_wait_over=None,
                                   index_size=None) -> str:
    """known_limitations' queue-wait-ceiling entry, GENERATED from live counts.

    r-repro-2 (2026-08-08): shipped a few hours earlier with the breach count
    and the field maximum frozen into the sentence as literals. The 06:53
    recompute moved edinburgh from 90.9 to 87.3 and the true count dropped by
    one — inside the same day, in a document that ALSO carries the live
    figure, so the two disagreed on the same page.

    That is the same defect as the index-size literal this change set already
    fixed, and it is worth being precise about why it recurred: only the three
    slug_override markets (london, amsterdam, manchester) are structurally
    above the proxy ceiling. The iso_default markets (rotterdam, milan,
    edinburgh, rome) sit near it and cross back and forth as the local
    saturation multiplier moves on every recompute. A count over a moving set
    cannot be a literal.
    """
    tail = ("Values fed by a per-ISO default or a slug_override are never "
            "clipped to 66 first, so the published field legitimately exceeds "
            "it; the three slug_override markets sit well above it and the "
            "per-ISO-default markets cross it in both directions as local "
            "saturation moves on each recompute")
    head = (f"queue_wait_months is bounded at "
            f"{QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS} months only on the "
            f"queue-depth PROXY path")
    if not isinstance(queue_wait_over, int) or queue_wait_over < 0 \
            or queue_wait_max is None:
        return (f"{head}. {tail}. The current breach count and field maximum "
                f"are UNMEASURED in this process — read "
                f"local_saturation.queue_wait_markets_over_proxy_ceiling and "
                f".queue_wait_published_max_months, which are measured per "
                f"request")
    scope = f" of {index_size}" if isinstance(index_size, int) and index_size else ""
    return (f"{head} — as of this response {queue_wait_over} market"
            f"{'' if queue_wait_over == 1 else 's'}{scope} exceed it, up to "
            f"{queue_wait_max} months. {tail}")


def _index_size_limitation(index_size=None, table_rows=None) -> str:
    """known_limitations' index-size entry, GENERATED from live counts.

    r-repro (2026-08-08): this entry used to be a hardcoded string literal
    naming an index size and a row count. BOTH numbers had drifted from live
    data — the pair in the source was 4 and 5 short of the measured values
    respectively. A limitation section that is itself stale is worse than no
    limitation section, so the numbers are now injected by the caller and the
    prose is generated around them. When the counts are unavailable the entry
    says so and names where to read them, rather than restating a pair that
    may have drifted again. The measured values as of that date are recorded
    once, in REVISIONS, where a dated observation belongs.
    """
    if not isinstance(index_size, int) or not isinstance(table_rows, int) \
            or index_size <= 0 or table_rows < index_size:
        return ("the index size and the underlying market_power_scores row "
                "count differ; the difference is retired alias twins. Both "
                "counts are UNMEASURED in this process and are deliberately "
                "not restated from memory — read them live from "
                "/api/v1/dcpi/total. The hardcoded pair published here until "
                "2026-08-08 had drifted from live data on both numbers")
    return (f"the index publishes {index_size} markets; the underlying table "
            f"carries {table_rows} rows. The difference is "
            f"{table_rows - index_size} retired alias twins (unpublished, not "
            f"deleted). {index_size} is the index size; {table_rows} is a row "
            f"count — cite the first")


KNOWN_LIMITATIONS_STATIC = (
    ("emergency_count_30d is never populated, so "
     f"{CONSTRAINT_WEIGHTS['emergencies']:.0%} of every constraint score is a "
     "structural zero"),
    ("five of the nine score inputs have no live source and are per-ISO "
     "constants; demand growth additionally defaults to a single global 4.0"),
    ("queue_wait_months is queue DEPTH, not a measured wait — a defensible "
     "proxy, published as one, with its 0.6 months/GW calibration stated"),
    ("that proxy SATURATES: every state at or above 90 GW pins at the "
     "66-month cap, so all Texas markets share one identical value"),
    ("interconnection-queue coverage is incomplete domestically and absent "
     "internationally"),
    ("roughly 40 curated markets carry hand-calibrated slug_overrides that "
     "REPLACE live values"),
    ("the local saturation index's operational-MW term is under repair "
     "(r-status-taxonomy): it is currently an unfiltered SUM over all "
     "statuses, so it contains the pipeline it separately reports. No "
     "operational-MW figure from DCPI is citeable until that lands"),
    ("LOW_SIGNAL is documented and multiplier-weighted but unreachable in "
     "practice"),
    (f"constraint_score is NOT reproducible from the published fields: "
     f"{' and '.join(CONSTRAINT_INPUTS_NOT_PUBLISHED)} are not emitted on "
     f"/api/v1/dcpi/scores/<slug> for any market, so up to "
     f"{MAX_UNDERIVABLE_CONSTRAINT_POINTS} of its 100 points cannot be "
     f"derived by a third party. composite_score and verdict ARE exactly "
     f"reproducible"),
)


def known_limitations(live_counts=None) -> list:
    """The limitations list, with the index-size entry generated from live
    counts supplied by the caller. `live_counts` is the dict shape emitted by
    routes/dcpi_methodology.py; anything missing degrades to the honest
    "unmeasured" wording rather than a stale literal."""
    lc = live_counts if isinstance(live_counts, dict) else {}
    return list(KNOWN_LIMITATIONS_STATIC) + [
        _queue_wait_ceiling_limitation(lc.get("queue_wait_max"),
                                       lc.get("queue_wait_over_proxy_ceiling"),
                                       lc.get("index_size")),
        _index_size_limitation(lc.get("index_size"), lc.get("table_rows"))]


# Backwards-compatible module constant. Carries the UNMEASURED wording,
# because a module-level constant has no access to live counts by
# construction — which is precisely why the old literal went stale.
KNOWN_LIMITATIONS = tuple(known_limitations())


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers — used by the endpoint and by the reproducibility test.
# NOT used by the scorer (which keeps its own inlined arithmetic for speed);
# tests/test_dcpi_methodology.py asserts the two agree.
# ─────────────────────────────────────────────────────────────────────────

def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def constraint_from_published_fields(queue_wait_months=None,
                                     reserve_margin_pct=None,
                                     emergency_count_30d=None,
                                     demand_growth_yoy_pct=None,
                                     local_dc_count=None) -> float:
    """Reproduce constraint_score from the SCORER's inputs. If this ever
    disagrees with routes/dcpi.py, the test fails.

    r-repro (2026-08-08) — read the argument list before trusting the name.
    This docstring used to say "a third party holding a
    /api/v1/dcpi/scores/<slug> payload can recompute the number". They cannot:
    `demand_growth_yoy_pct` and `local_dc_count` are not published on that
    endpoint for any market, so a caller working from a real payload has to
    guess them, and a guess of None silently substitutes
    CONSTRAINT_INPUT_DEFAULTS — which returns a plausible number that is not
    the published one. Use constraint_derivable_from_published_fields() to see
    what a published payload genuinely supports.
    """
    d = CONSTRAINT_INPUT_DEFAULTS
    c = CONSTRAINT_CEILINGS
    w = CONSTRAINT_WEIGHTS
    qw = float(queue_wait_months if queue_wait_months is not None else d["queue_wait_months"])
    rm = float(reserve_margin_pct if reserve_margin_pct is not None else d["reserve_margin_pct"])
    em = int(emergency_count_30d if emergency_count_30d is not None else d["emergency_count_30d"])
    dg = float(demand_growth_yoy_pct if demand_growth_yoy_pct is not None else d["demand_growth_yoy_pct"])

    s_wait = _clip((qw / c["queue_wait_months"]) * 100)
    s_reserve = _clip((1 - (rm / c["reserve_margin_pct"])) * 100)
    s_emerg = _clip(em * CONSTRAINT_EMERGENCY_POINTS_PER_EVENT)
    s_demand = _clip((dg / c["demand_growth_yoy_pct"]) * 100)
    base = (w["queue_wait"] * s_wait + w["reserve_margin"] * s_reserve
            + w["emergencies"] * s_emerg + w["demand_growth"] * s_demand)
    s_local = _clip((float(local_dc_count or 0) / c["local_dc_count"]) * 100)
    return round(_clip(base + CONSTRAINT_LOCAL_COMPETITION_BONUS * s_local), 1)


def constraint_derivable_from_published_fields(queue_wait_months=None,
                                               reserve_margin_pct=None,
                                               emergency_count_30d=None) -> float:
    """The part of constraint_score a third party CAN derive.

    Deliberately takes ONLY the three inputs that /api/v1/dcpi/scores/<slug>
    actually emits. The gap between this and the published constraint_score is
    the honest measure of the reproducibility shortfall, and it is what
    tests/test_dcpi_methodology.py recomputes per market: if that gap is ever
    non-zero while the payload claims constraint_score is reproducible, the
    published claim is false and the test fails.

    NOT the same function as constraint_from_published_fields, which also
    takes demand_growth_yoy_pct and local_dc_count — two inputs that are not
    published anywhere, and whose absence is the entire defect.
    """
    d = CONSTRAINT_INPUT_DEFAULTS
    c = CONSTRAINT_CEILINGS
    w = CONSTRAINT_WEIGHTS
    qw = float(queue_wait_months if queue_wait_months is not None else d["queue_wait_months"])
    rm = float(reserve_margin_pct if reserve_margin_pct is not None else d["reserve_margin_pct"])
    em = int(emergency_count_30d if emergency_count_30d is not None else d["emergency_count_30d"])
    s_wait = _clip((qw / c["queue_wait_months"]) * 100)
    s_reserve = _clip((1 - (rm / c["reserve_margin_pct"])) * 100)
    s_emerg = _clip(em * CONSTRAINT_EMERGENCY_POINTS_PER_EVENT)
    return round(w["queue_wait"] * s_wait + w["reserve_margin"] * s_reserve
                 + w["emergencies"] * s_emerg, 4)


def composite_from_published_fields(excess, constraint, ttp_months,
                                    verdict=None) -> float:
    """Reproduce composite_score from a published row."""
    e = float(excess or 0)
    c = float(constraint or 0)
    t = min(float(ttp_months or 0), COMPOSITE_TTP_CAP_MONTHS)
    w = COMPOSITE_WEIGHTS
    raw = (e * w["excess"]
           + (100 - c) * w["inverse_constraint"]
           + (1 - t / COMPOSITE_TTP_CAP_MONTHS) * 100 * w["time_to_power"])
    mult = COMPOSITE_VERDICT_MULTIPLIERS.get((verdict or "").upper(),
                                             COMPOSITE_DEFAULT_MULTIPLIER)
    return round(max(0.0, min(100.0, raw * mult)), 1)


def verdict_from_scores(constraint: float, excess: float) -> str:
    """Reproduce the verdict. Note the absence of a NEUTRAL band."""
    for label, band in VERDICT_BANDS:
        if excess >= band["excess_min"] and constraint <= band["constraint_max"]:
            return label
    return VERDICT_FALLBACK


def verdict_case_sql(excess_expr: str, constraint_expr: str) -> str:
    """verdict_from_scores as a SQL CASE, GENERATED from VERDICT_BANDS.

    r-restatement-marker (2026-08-08). A consumer that needs to ask "was
    this stored verdict produced by the rule in force?" has to evaluate the
    bands in SQL, against a table of stored scores. Retyping the thresholds
    to do that is how this file came to have five band tables in the first
    place — see the module docstring and tests/test_dcpi_verdict_bands.py.
    So the CASE is built from the same tuple verdict_from_scores loops over,
    and a band added there appears here with no second edit.

    Emits a bare expression, parenthesised, so it drops into a SELECT list
    or a WHERE clause unchanged. The caller passes the column expressions,
    which is what lets one definition serve both sides of a self-join.

    ONE DELIBERATE DIVERGENCE from verdict_from_scores: both score columns
    are nullable, and verdict_from_scores has no NULL branch at all (it
    would raise). Here a NULL is COALESCEd to a value that satisfies no
    band — below every excess_min, above every constraint_max — so a
    NULL-scored row resolves to VERDICT_FALLBACK rather than to whatever
    three-valued logic would produce. That is the conservative direction
    for the only caller: a row whose stored verdict then fails to match is
    treated as unattributable and SUPPRESSED, not published. Measured on
    the live table 2026-08-08 the branch is unreachable — 21,666 snapshot
    rows, zero NULL in either score column — so it is defence, not policy.

    NO %-FORMATTING, and callers must not add any: the result is spliced
    into statements that carry %s placeholders for psycopg2, and mixing
    Python % into such a string is how a literal % reaches the driver (the
    psycopg2 percent trap — same note as util/dcpi_score_row.py).
    """
    whens = "".join(
        f" WHEN COALESCE({excess_expr}, -1) >= {band['excess_min']}"
        f" AND COALESCE({constraint_expr}, 999) <= {band['constraint_max']}"
        f" THEN '{label}'"
        for label, band in VERDICT_BANDS
    )
    return f"(CASE{whens} ELSE '{VERDICT_FALLBACK}' END)"


def method_block(live_counts=None) -> dict:
    """The whole method, as one JSON-safe dict. Emitted verbatim by
    /api/v1/dcpi/methodology and by nothing else — every other surface should
    link to that endpoint rather than restate it.

    `live_counts` (optional) carries figures this module must NOT hardcode,
    because every one of them has drifted while written down as a literal:

        index_size            published market count
        table_rows            market_power_scores row count
        queue_wait_max        MAX(queue_wait_months) over published markets
        queue_wait_over_proxy_ceiling  how many exceed the proxy ceiling

    The module stays pure — no DB, no network, no import beyond stdlib — so
    the endpoint can never 500 and tests can assert reproducibility without a
    database. Omitted or malformed counts degrade to explicit "unmeasured"
    wording, never to a stale number.
    """
    lc = live_counts if isinstance(live_counts, dict) else {}
    return {
        "method_version": DCPI_METHOD_VERSION,
        "scoring_unchanged_since": SCORING_UNCHANGED_SINCE,
        "scale": "both component scores are 0..100",
        "normalisation": ("every term is a linear ratio to a stated ceiling, "
                          "clipped to [0,100], except the log-scaled local "
                          "saturation index"),
        "constraint_score": {
            "direction": "high = MORE constrained = avoid",
            "weights": dict(CONSTRAINT_WEIGHTS),
            "ceilings": dict(CONSTRAINT_CEILINGS),
            "emergency_points_per_event": CONSTRAINT_EMERGENCY_POINTS_PER_EVENT,
            "scoring_time_defaults": dict(CONSTRAINT_INPUT_DEFAULTS),
            "local_competition_bonus_weight": CONSTRAINT_LOCAL_COMPETITION_BONUS,
            "max_before_outer_clip": round(
                100 * (1 + CONSTRAINT_LOCAL_COMPETITION_BONUS), 1),
        },
        "excess_power_score": {
            "direction": "high = MORE buildable headroom",
            "weights": dict(EXCESS_WEIGHTS),
            "ceilings": dict(EXCESS_CEILINGS),
            "reserve_floor_pct": EXCESS_RESERVE_FLOOR_PCT,
            "reserve_span_pct": EXCESS_RESERVE_SPAN_PCT,
            "scoring_time_defaults": dict(EXCESS_INPUT_DEFAULTS),
            "local_grid_bonus_weight": EXCESS_LOCAL_GRID_BONUS,
            "local_grid_subindex": {
                "substation_ceiling": LOCAL_GRID_SUBSTATION_CEILING,
                "substation_points": LOCAL_GRID_SUBSTATION_POINTS,
                "kv_points": [list(p) for p in LOCAL_GRID_KV_POINTS],
                "generation_ceiling_mw": LOCAL_GRID_GEN_CEILING,
                "generation_points": LOCAL_GRID_GEN_POINTS,
            },
            "max_before_outer_clip": round(
                100 * (1 + EXCESS_LOCAL_GRID_BONUS), 1),
        },
        "verdict": {
            "bands": [{"verdict": v, **b} for v, b in VERDICT_BANDS],
            "fallback": VERDICT_FALLBACK,
            "has_neutral_band": False,
            "low_signal_note": ("LOW_SIGNAL exists as a filter value and "
                                "carries a composite multiplier, but is "
                                "unreachable in practice — see fallbacks"),
        },
        "composite_score": {
            "formula": ("excess*0.60 + (100-constraint)*0.30 + "
                        "(1 - min(ttp,60)/60)*100*0.10, then multiplied by "
                        "the verdict quality multiplier"),
            "weights": dict(COMPOSITE_WEIGHTS),
            "ttp_cap_months": COMPOSITE_TTP_CAP_MONTHS,
            "verdict_multipliers": dict(COMPOSITE_VERDICT_MULTIPLIERS),
            "default_multiplier": COMPOSITE_DEFAULT_MULTIPLIER,
        },
        "time_to_power": {
            "formula": "queue_wait_months * reserve-margin adjustment",
            "adjustments": [list(a) for a in TTP_ADJUSTMENTS],
            "scarce_reserve_pct": TTP_SCARCE_RESERVE_PCT,
            "scarce_adjustment": TTP_ADJ_SCARCE,
            "default_adjustment": TTP_ADJ_DEFAULT,
            "scoring_time_defaults": dict(TTP_INPUT_DEFAULTS),
        },
        "queue_wait_proxy": dict(QUEUE_WAIT_PROXY),
        "local_saturation": {
            "scale": SATURATION_SCALE,
            "weights": dict(SATURATION_WEIGHTS),
            "ceilings": dict(SATURATION_CEILINGS),
            "rewrites_before_scoring": dict(SATURATION_REWRITES),
            "applies_to": "markets WITHOUT a hand-calibrated slug_override",
            "breaches_queue_wait_band": SATURATION_BREACHES_QUEUE_BAND,
            # r-repro (2026-08-08): this key used to be
            # "queue_wait_true_ceiling_months": 89.1, which claimed to bound
            # the PUBLISHED FIELD and did not — several markets exceeded it.
            # 89.1 bounds the queue-depth PROXY path only; the two
            # constant-fed paths are not clipped to 66 first. Renamed to say
            # what it actually bounds.
            "queue_wait_proxy_path_ceiling_months":
                QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS,
            "queue_wait_ceiling_note": (
                f"{QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS} = 66 x 1.35 bounds "
                "the queue-depth PROXY path only. queue_wait_months is also "
                "filled from unclipped per-ISO defaults and from "
                "slug_overrides, and neither is clipped to 66 before "
                "publication, so the published field legitimately exceeds "
                "this number. See queue_wait_fill_paths and the "
                "queue_wait_constant_paths_are_unclipped fallback"),
            "queue_wait_fill_paths": [dict(p) for p in QUEUE_WAIT_FILL_PATHS],
            # MEASURED from the live index, never retyped — the maxima of the
            # constant-fed paths live in routes/dcpi.py.
            "queue_wait_published_max_months": lc.get("queue_wait_max"),
            "queue_wait_markets_over_proxy_ceiling":
                lc.get("queue_wait_over_proxy_ceiling"),
        },
        "inputs": [dict(i) for i in INPUTS],
        "local_infrastructure_terms": [dict(t) for t in LOCAL_INFRA_TERMS],
        "signal_tier": dict(SIGNAL_TIER),
        "fallbacks": [dict(f) for f in FALLBACKS],
        "cadence": dict(CADENCE),
        "revision_policy": dict(REVISION_POLICY),
        "revisions": [dict(r) for r in REVISIONS],
        "known_limitations": known_limitations(lc),
        # r-repro-3 (2026-08-08): the market count is the LIVE index size, not
        # a literal — the same figure was hardcoded in three other places in
        # this module and had drifted in every one of them. "no market" and
        # "every market" are structural and safe to state absolutely; a COUNT
        # is not.
        "reproducibility": (
            "composite_score and verdict ARE reproducible from the fields "
            "published on /api/v1/dcpi/scores/<slug> using the weights above "
            "— measured exact on every published market"
            f"{' (%d as of this response)' % lc['index_size'] if lc.get('index_size') else ''}. "
            "constraint_score is NOT: two of its five inputs "
            "(demand_growth_yoy_pct, weight "
            f"{CONSTRAINT_WEIGHTS['demand_growth']}, and local_dc_count, "
            f"bonus weight {CONSTRAINT_LOCAL_COMPETITION_BONUS}) are not "
            "columns on market_power_scores and are therefore emitted for no "
            "market, so up to "
            f"{MAX_UNDERIVABLE_CONSTRAINT_POINTS} of its 100 points cannot be "
            "derived by a third party — NO market reproduces exactly, and that "
            "is by construction rather than by measurement: the inputs are "
            "absent from the schema, not merely missing from some rows. "
            "excess_power_score is NOT fully reproducible either: "
            "queue_approval_rate_pct and the local grid sub-index terms are "
            "not all published per market. See reproducibility_detail"),
        "reproducibility_detail": {
            "first_measured_on": "2026-08-08, all published markets",
            "index_size_at_this_response": lc.get("index_size"),
            "scores": {
                "composite_score": {
                    "reproducible": True,
                    "exact_on": "every published market, at every measurement",
                    "from": ["excess_power_score", "constraint_score",
                             "time_to_power_months", "verdict"],
                },
                "verdict": {
                    "reproducible": True,
                    "exact_on": "every published market, at every measurement",
                    "from": ["excess_power_score", "constraint_score"],
                },
                "constraint_score": {
                    "reproducible": False,
                    "exact_on": "no market — by construction, see why",
                    "published_inputs": list(CONSTRAINT_INPUTS_PUBLISHED),
                    "unpublished_inputs": list(CONSTRAINT_INPUTS_NOT_PUBLISHED),
                    "unpublished_input_weights":
                        dict(CONSTRAINT_UNPUBLISHED_WEIGHTS),
                    "max_underivable_points":
                        MAX_UNDERIVABLE_CONSTRAINT_POINTS,
                    "observed_residual_points": {
                        # r-repro-3 (2026-08-08): a MEAN over 315 markets moves
                        # on every recompute — r-universe-dedup rescored
                        # local_dc_count the same day and took it 10.74 ->
                        # 10.55 — so it is not published. min/max are stated as
                        # a DATED observation, not a standing property: the max
                        # is pinned by the weight-derived cap below (johor sits
                        # exactly on it), the min is not pinned by anything.
                        # Recompute your own with the helper named below.
                        "as_of": "2026-08-08",
                        "min": 1.22, "max": 21.0,
                        "at_the_cap": "johor (21.0 of 41.0 underivable)",
                        "note": ("a dated snapshot. Per-market residuals move "
                                 "whenever local_dc_count or demand growth is "
                                 "rescored, which is up to 4x/day; only the "
                                 "max_underivable_points cap is a standing "
                                 "property, because it is derived from the "
                                 "weights rather than measured")},
                    "why": ("neither demand_growth_yoy_pct nor local_dc_count "
                            "is a column on market_power_scores, so the "
                            "endpoint's SELECT * cannot emit them. "
                            "demand_growth_yoy_pct sometimes appears as PROSE "
                            "inside top_risks_json, but only when it is a top "
                            "risk, so it is not a field a consumer can rely "
                            "on"),
                    "remedy": ("publish both fields on the scores endpoint — "
                               "tracked separately; it needs a schema and "
                               "writer change, not a documentation change"),
                },
                "excess_power_score": {
                    "reproducible": False,
                    "unpublished_inputs": ["queue_approval_rate_pct",
                                           "local_substation_count",
                                           "local_max_kv", "local_gen_mw"],
                },
            },
            "recompute_helpers": {
                "module": "util/dcpi_method.py",
                "constraint_full": "constraint_from_published_fields(...)",
                "constraint_from_published_only":
                    "constraint_derivable_from_published_fields(...)",
                "composite": "composite_from_published_fields(...)",
                "verdict": "verdict_from_scores(...)",
                "note": ("constraint_from_published_fields is named for the "
                         "promise, not the reality: two of its arguments are "
                         "NOT published. Use "
                         "constraint_derivable_from_published_fields to see "
                         "exactly how far a published payload gets you"),
            },
        },
    }
