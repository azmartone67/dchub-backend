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
DCPI_METHOD_VERSION = "2.0.1"

# The date the SCORING (not the labelling) last changed. Consumers comparing
# two history points from before/after this date are comparing two methods.
SCORING_UNCHANGED_SINCE = "2026-07-25"


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
QUEUE_WAIT_TRUE_CEILING_MONTHS = 89.1     # 66 * 1.35


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
    {"name": "local_dc_count", "radius_km": 25,
     "source": "discovered_facilities WHERE COALESCE(is_duplicate,0)=0",
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
     "effect": (f"queue_wait_months is clipped to 12-66 and THEN multiplied by "
                f"up to 1.35, so the published field can reach "
                f"{QUEUE_WAIT_TRUE_CEILING_MONTHS} months")},
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
    {"id": "low_signal_unreachable", "where": "derive_verdict / dchub_self_heal",
     "effect": ("LOW_SIGNAL is a documented filter value carrying a 0.35 "
                "composite multiplier, but it is only written when a score is "
                "exactly 0, which iso_defaults guarantee never happens — it is "
                "unreachable in practice")},
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
)


# ─────────────────────────────────────────────────────────────────────────
# KNOWN LIMITATIONS — published plainly, because diligence will find them
# ─────────────────────────────────────────────────────────────────────────

KNOWN_LIMITATIONS = (
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
    ("the index publishes 311 markets; the underlying table carries 317 rows. "
     "The difference is retired alias twins. 311 is the index size; 317 is a "
     "row count"),
)


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
    """Reproduce constraint_score from a published row, using ONLY the
    constants above. This is the reproducibility promise made to readers: a
    third party holding a /api/v1/dcpi/scores/<slug> payload can recompute
    the number. If this ever disagrees with routes/dcpi.py, the test fails.
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


def method_block() -> dict:
    """The whole method, as one JSON-safe dict. Emitted verbatim by
    /api/v1/dcpi/methodology and by nothing else — every other surface should
    link to that endpoint rather than restate it."""
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
            "queue_wait_true_ceiling_months": QUEUE_WAIT_TRUE_CEILING_MONTHS,
        },
        "inputs": [dict(i) for i in INPUTS],
        "local_infrastructure_terms": [dict(t) for t in LOCAL_INFRA_TERMS],
        "signal_tier": dict(SIGNAL_TIER),
        "fallbacks": [dict(f) for f in FALLBACKS],
        "cadence": dict(CADENCE),
        "revision_policy": dict(REVISION_POLICY),
        "revisions": [dict(r) for r in REVISIONS],
        "known_limitations": list(KNOWN_LIMITATIONS),
        "reproducibility": (
            "constraint_score and composite_score are reproducible from the "
            "fields published on /api/v1/dcpi/scores/<slug> using the weights "
            "above. excess_power_score is NOT fully reproducible from the "
            "published fields: two of its six inputs "
            "(queue_approval_rate_pct, and the local grid sub-index terms) "
            "are not all published per market"),
    }
