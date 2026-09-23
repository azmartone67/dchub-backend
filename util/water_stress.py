"""water_stress.py — the one place that turns a stored water-stress score
into the 1-5 index every consumer scores on.

★ WHERE THE SIGNAL LIVES. `water_risk.water_stress_score`: 0-100, 100 = most
stressed, source 'wri_aqueduct', 51 rows, computed_at 2026-07-10 (measured
live 2026-09-21). `water_risk.state` is the 2-letter USPS code.

★ WHERE IT DOES NOT LIVE. `usgs_water_stress` has no stress column of any
kind. Its live columns are id, site_id, site_name, latitude, longitude,
state, county, aquifer_name, well_depth_ft, water_level_ft,
water_level_date, site_type, updated_at — 560 rows across 16 states. Four
route modules asked it for `AVG(stress_index)`, which raises UndefinedColumn
on every call; #5259 fixed the first, this module exists so the other three
share one answer instead of a fourth, fifth and sixth hand-copy. And its
`water_level_ft` groundwater proxy is NOT a substitute: it was withdrawn on
2026-07-07 for reading INVERTED, and `_UNSUPPORTED_OBJECTIVES` in
routes/interconnection_queues.py still refuses to score off it.

★ DO NOT FALL BACK TO `baseline_water_stress`. It is NULL on all 51 rows, so
a COALESCE onto it buys nothing and hides that the real column was unread.

★ WHY MIDPOINTS AND NOT WRI'S WITHDRAWAL CUT-OFFS. routes/water_aqueduct_ingest.py
normalises WRI's PUBLISHED bws_cat bucket (-1 Arid & Low Use, 0 Low, 1
Low-Medium, 2 Medium-High, 3 High, 4 Extremely High) with cat/4*100, so the
five categories land exactly on 0 / 25 / 50 / 75 / 100 and a state roll-up is
a mean of those. The stored score is a normalised CATEGORY, not a withdrawal
ratio — banding 25.0 on WRI's withdrawal-percentage cut-offs (<10%, 10-20%,
20-40%, 40-80%, >80%) would read it as "25% withdrawal, Medium-High" and
shift every state a band.
"""

from __future__ import annotations

__all__ = ["WATER_BANDS", "water_band", "STATE_WATER_STRESS_SQL"]

#: (exclusive upper edge, band). Midpoints between the five normalised
#: categories, so 0/25/50/75/100 land on 1/2/3/4/5 respectively.
WATER_BANDS = ((12.5, 1), (37.5, 2), (62.5, 3), (87.5, 4))


def water_band(score):
    """0-100 WRI score -> the 1-5 index (1 Low .. 5 Extremely High).

    None passes straight through: a stress score we could not read stays null
    and never becomes a number. That is the whole point — a failed read that
    degrades to a band is indistinguishable from a measured one.
    """
    if score is None:
        return None
    for edge, band in WATER_BANDS:
        if score < edge:
            return band
    return 5


#: Newest row per state, keyed by the 2-letter code. `DISTINCT ON` rather than
#: `AVG(...) GROUP BY` because water_risk carries one row per state per ingest
#: run: averaging would silently blend the 2026-07-10 run with any earlier one.
STATE_WATER_STRESS_SQL = """
    SELECT DISTINCT ON (UPPER(state))
           UPPER(state) AS state_code, water_stress_score
      FROM water_risk
     WHERE water_stress_score IS NOT NULL
     ORDER BY UPPER(state), computed_at DESC NULLS LAST
"""
