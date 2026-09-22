"""water_risk.py — the one read path for the `water_risk` table.

THE BUG THIS EXISTS TO PREVENT
------------------------------
Two published surfaces read this table and both got it wrong, in the two
different ways a read can be wrong. Measured read-only against prod Neon,
2026-09-21.

1. DEAD COLUMNS, SWALLOWED (routes/market_brief.py, Section 8 "Risk Factors").
   The query selected `drought_d2_months`, which this table has never had.
   Every call raised UndefinedColumn. The `except` branch then tried
   `SELECT stress_score, baseline_water_stress ... WHERE LOWER(market) = ...`
   — `stress_score` and `market` do not exist either — so the fallback threw
   too and a bare `except: pass` ate it. Section 8 therefore served
   `water_stress: null` on EVERY market, forever, with no error field. The
   data was there the whole time: 51 states, source `wri_aqueduct`.

2. WRONG SCALE + A NULL FALLBACK (routes/hyperscaler_brief.py, Section 5).
   That one read `water_stress_score` correctly and then classified with
   `if float(stress) >= 4.0`. That threshold is for a 1-5 index; the column
   is 0-100. Every state but the very least-stressed cleared it, so
   `stressed_state_pct` published ~100%. It also did
   `_as_float(r[0]) or _as_float(r[1])`, falling back to
   `baseline_water_stress` — NULL on all 51 rows — and, because `or` tests
   truthiness rather than None, additionally treated a genuine 0.0 (a state
   with the LEAST water stress) as missing.

Neither surface could tell "the read failed" from "the answer is null".
That is the whole failure mode, so every function here returns
`(value, error)` and never a bare fallback.

THE LIVE SCHEMA (verified 2026-09-21, 51 rows, all source='wri_aqueduct',
computed_at 2026-07-10) — exactly these columns, nothing else:

    id                     bigint
    state                  text (2-letter)
    water_stress_score     double precision   0-100, 100 = MOST stressed
    baseline_water_stress  double precision   NULL on all 51 rows
    bws_category           text
    source                 text
    computed_at            timestamptz

★ `baseline_water_stress` IS NULL ON EVERY ROW. It is declared as WRI's raw
bws_score and nothing has ever populated it. Never fall back to it — that is
how surface 2 turned a working read into a null.

★ THE SCALE IS 0-100, NOT 0-5 AND NOT 1-5. routes/water_aqueduct_ingest.py
normalises WRI's published `bws_cat` bucket (-1..4) with `cat / 4 * 100`, so
the stored values land on 0 / 25 / 50 / 75 / 100 for category-derived rows and
anywhere in 0-100 for the ratio-derived fallback. A threshold written for a
1-5 index is off by a factor of ~20 against it. Use `water_band_1_5()` if a
1-5 threshold is genuinely what a caller wants.
"""
from util.db_honesty import try_fetchall, try_fetchone

__all__ = ["water_band_1_5", "BAND_LABELS", "STRESSED_BAND",
           "read_state_stress", "read_states_stress"]


# WRI Aqueduct 4.0 category buckets, and the 0-100 score each one normalises to
# under water_aqueduct_ingest._cat_to_100 (cat / 4 * 100):
#
#   cat 0  Low            (<10%)     ->   0.0   -> band 1
#   cat 1  Low-Medium     (10-20%)   ->  25.0   -> band 2
#   cat 2  Medium-High    (20-40%)   ->  50.0   -> band 3
#   cat 3  High           (40-80%)   ->  75.0   -> band 4
#   cat 4  Extremely High (>80%)     -> 100.0   -> band 5
#   cat -1 Arid & Low Water Use      -> 100.0   -> band 5 (the ingest maps arid
#          to extreme on purpose: for a water-hungry DC, arid IS scarcity)
#
# The cut points below are the MIDPOINTS between adjacent category scores, so a
# ratio-derived score (which does not land on the 25s) falls into the band whose
# category score it is nearest. 12.5 / 37.5 / 62.5 / 87.5.
_BAND_CUTS = (12.5, 37.5, 62.5, 87.5)

BAND_LABELS = {1: "Low", 2: "Low-Medium", 3: "Medium-High",
               4: "High", 5: "Extremely High"}

# "Stressed" = WRI High or Extremely High, i.e. >=40% withdrawal/supply. This is
# what the old `>= 4.0` on a 1-5 index was reaching for; on the 0-100 column it
# is band >= 4, not score >= 4.
STRESSED_BAND = 4


def water_band_1_5(score):
    """0-100 water_stress_score -> 1-5 WRI band, or None if score is None.

    Boundaries are inclusive-below: a score exactly on a cut point belongs to
    the HIGHER band, matching WRI's own bucket edges (10-20% is Low-Medium, so
    a 12.5 lands in band 2, not band 1).
    """
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    band = 1
    for cut in _BAND_CUTS:
        if s >= cut:
            band += 1
    return band


def _shape(row):
    """(score, bws_category) tuple -> the published dict."""
    score = None
    if row is not None and row[0] is not None:
        try:
            score = float(row[0])
        except (TypeError, ValueError):
            score = None
    band = water_band_1_5(score)
    return {
        "score":     score,                       # 0-100, 100 = most stressed
        "band":      band,                        # 1-5, WRI category
        "band_label": BAND_LABELS.get(band),
        "category":  (row[1] if row is not None else None),
        "stressed":  (None if band is None else band >= STRESSED_BAND),
    }


_SQL_ONE = """
    SELECT water_stress_score, bws_category
      FROM water_risk
     WHERE UPPER(state) = UPPER(%s)
     ORDER BY computed_at DESC NULLS LAST
     LIMIT 1
"""

_SQL_MANY = """
    SELECT DISTINCT ON (UPPER(state))
           UPPER(state), water_stress_score, bws_category
      FROM water_risk
     WHERE UPPER(state) = ANY(%s)
     ORDER BY UPPER(state), computed_at DESC NULLS LAST
"""


def read_state_stress(cur, state):
    """One state -> (dict, None) | (None, "Type: msg").

    A state with no row comes back as (None, None): absent, not broken. A read
    that THREW comes back with the error text so the caller can publish null
    plus a named error instead of a silent null.
    """
    row, err = try_fetchone(cur, _SQL_ONE, ((state or "").strip(),))
    if err:
        return None, err
    if row is None:
        return None, None
    return _shape(row), None


def read_states_stress(cur, states):
    """Many states -> ({STATE: dict}, None) | ({}, "Type: msg").

    One query, not one per state. The per-state loop this replaces swallowed
    each failure with `except: continue`, so a broken read looked exactly like
    a state that simply has no row — 51 silent nulls averaged into a confident
    number.
    """
    keys = sorted({(s or "").strip().upper() for s in (states or []) if s})
    if not keys:
        return {}, None
    rows, err = try_fetchall(cur, _SQL_MANY, (keys,))
    if err:
        return {}, err
    return {r[0]: _shape((r[1], r[2])) for r in rows if r and r[0]}, None
