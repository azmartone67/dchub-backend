"""MISO's own real-time feed is PRIMARY again; EIA-930 is the fallback.

THE DEFECT (measured 2026-09-07): MISO sat 27.5h stale in `grid_data` while
MISO itself served the current 5-minute interval. This module had led with
EIA-930 since 2026-05-31 because MISO "retired" the public RTWD Data Broker.
It was not retired, it was MOVED: on 2025-12-12 MISO republished every
real-time feed as JSON at public-api.misoenergy.org, and the dead host says so
in its own error body:

    {"error": "no data", "See": "https://www.misoenergy.org/.../rtdataapis"}

Nobody followed the pointer. Live probe on 2026-09-07: /api/FuelMix returned
"Interval 02:45 EST" read at 07:54Z — 9.0 minutes old — against EIA's newest
MISO period of 2026-09-06T04, 27.5h old.

★ THE PAYLOAD BELOW IS A REAL RESPONSE, captured 2026-09-07T07:54Z. Numbers
  are MISO's, not invented, so the shape assertions describe production.

★ WHAT THIS FILE IS REALLY GUARDING — the two ways this parse goes quietly
  wrong rather than loudly wrong:
    1. a category MISO adds later getting folded into fuel_oth, which would
       overstate "other" and hide the new category; and
    2. Battery Storage (NEGATIVE while charging) or Imports (interchange, not
       generation) leaking into the fuel_ namespace, where
       routes/state_of_power.py globs `fuel_%` and routes/iso_jp_denkiyoho.py
       sign-checks the prefix.
  Both are silent corruption, not exceptions, so they get explicit tests.

★ Stdlib only, no network, no app import — CI installs pytest/requests/flask/
  pyyaml/psycopg2/psycopg/Unidecode/Pillow/feedparser and nothing else.
"""
import datetime
import json

import pytest

from routes import iso_miso as m


# ── a real /api/FuelMix response, captured 2026-09-07T07:54Z ─────────────────
LIVE = json.dumps({
    "RefId": "07-Sep-2026 - Interval 02:45 EST",
    "TotalMW": 69757,
    "Fuel": {"Type": [
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Coal",
         "ACT": "20332", "FUEL_CATEGORY": "Coal  (20,332 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Natural Gas",
         "ACT": "20971", "FUEL_CATEGORY": "Natural Gas  (20,971 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Nuclear",
         "ACT": "11820", "FUEL_CATEGORY": "Nuclear  (11,820 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Wind",
         "ACT": "10062", "FUEL_CATEGORY": "Wind  (10,062 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Solar",
         "ACT": "0", "FUEL_CATEGORY": "Solar  (0 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Battery Storage",
         "ACT": "-77", "FUEL_CATEGORY": "Battery Storage  (-77 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Other",
         "ACT": "-1192", "FUEL_CATEGORY": "Other  (-1,192 MW)"},
        {"INTERVALEST": "2026-09-07 2:45:00 AM", "CATEGORY": "Imports",
         "ACT": "6572", "FUEL_CATEGORY": "Imports (6,572 MW)"},
    ]},
})


# ── 1 · MISO's own feed leads ────────────────────────────────────────────────

def test_the_public_api_is_tried_before_eia():
    urls = m._miso_urls()
    assert urls[0] == m.MISO_PUBLIC_FUELMIX, urls[0]
    assert "api.eia.gov" in urls[1], "EIA-930 must remain the fallback"


def test_the_retired_broker_is_last():
    """Kept for auto-recovery, but it must never outrank a working feed."""
    urls = m._miso_urls()
    broker = [i for i, u in enumerate(urls) if "MISORTWDDataBroker" in u]
    assert broker, "the legacy URLs should stay as last-resort fallbacks"
    assert min(broker) > 1, urls


# ── 2 · the parse ────────────────────────────────────────────────────────────

def test_live_payload_parses_into_the_eia_vocabulary():
    got = m.parse_miso_public_fuelmix(LIVE)
    assert got["fuel_col"] == 20332.0
    assert got["fuel_ng"] == 20971.0
    assert got["fuel_nuc"] == 11820.0
    assert got["fuel_wnd"] == 10062.0
    assert got["fuel_sun"] == 0.0
    assert got["fuel_oth"] == -1192.0


def test_act_is_coerced_from_string_including_negatives():
    """ACT arrives as a string; a lexical compare downstream would be silent."""
    got = m.parse_miso_public_fuelmix(LIVE)
    assert isinstance(got["fuel_col"], float)
    assert got["battery_storage_mw"] == -77.0


def test_absent_fuels_are_omitted_not_zeroed():
    """MISO publishes no oil/water split. Inventing 0.0 would read as a real
    measurement of zero — the same lie as a broken query reporting 0."""
    got = m.parse_miso_public_fuelmix(LIVE)
    assert "fuel_oil" not in got
    assert "fuel_wat" not in got


# ── 3 · the silent-corruption guards ─────────────────────────────────────────

def test_battery_and_imports_stay_out_of_the_fuel_namespace():
    """★ Battery goes negative and Imports is not generation. Either inside
    fuel_* corrupts every consumer that sums or sign-checks that prefix."""
    got = m.parse_miso_public_fuelmix(LIVE)
    assert got["battery_storage_mw"] == -77.0
    assert got["net_imports_mw"] == 6572.0
    for k in got:
        assert not (k.startswith("fuel_") and got[k] == -77.0), k
        assert not (k.startswith("fuel_") and got[k] == 6572.0), k
    assert "fuel_bat" not in got and "fuel_imports" not in got


@pytest.mark.parametrize("act", [None, "", "n/a", "--", {}])
def test_an_unreadable_act_is_omitted_never_written_as_a_real_zero(act):
    """★ A value that could not be READ is not a measurement of zero.

    Caught by mutation: `except: mw = 0.0` instead of `continue` survived the
    whole file. It would publish "MISO coal: 0 MW" from a malformed field —
    indistinguishable from a genuine shutdown, and the same class of lie as a
    failed COUNT reporting 0. Note fuel_sun IS legitimately 0.0 at 02:45, so
    the contract is ABSENCE, not "no zeros".
    """
    d = json.loads(LIVE)
    for t in d["Fuel"]["Type"]:
        if t["CATEGORY"] == "Coal":
            t["ACT"] = act
    got = m.parse_miso_public_fuelmix(json.dumps(d))
    assert "fuel_col" not in got, f"unreadable ACT={act!r} became {got.get('fuel_col')!r}"
    assert got["fuel_sun"] == 0.0, "a REAL zero must still be published"
    assert got["fuel_ng"] == 20971.0, "one bad field must not drop the others"


def test_an_unknown_category_is_dropped_never_folded_into_other():
    """★ If MISO adds a category, it must not silently inflate fuel_oth."""
    d = json.loads(LIVE)
    d["Fuel"]["Type"].append({"INTERVALEST": "2026-09-07 2:45:00 AM",
                              "CATEGORY": "Geothermal", "ACT": "999"})
    got = m.parse_miso_public_fuelmix(json.dumps(d))
    assert got["fuel_oth"] == -1192.0, "a new category was folded into 'other'"
    assert 999.0 not in got.values()


@pytest.mark.parametrize("bad", [
    "", "not json", "[]", "null", '{"Fuel": null}', '{"Fuel": {"Type": []}}',
    '{"Fuel": {"Type": "nope"}}', '{"RefId": "x"}',
])
def test_unrecognised_shapes_return_empty_so_the_caller_falls_through(bad):
    """{} makes fetch_first_working try the next URL; a half-parse would
    persist junk under MISO's name."""
    assert m.parse_miso_public_fuelmix(bad) == {}


# ── 4 · the timestamp ────────────────────────────────────────────────────────

def test_interval_is_fixed_est_not_dst_aware():
    """★ THE HOUR BUG. The field is INTERVALEST and the September sample reads
    02:45 EST — in September, US/Eastern local time is EDT. Treating this as a
    DST-aware zone would place every summer row an hour off."""
    ts = m.parse_miso_interval(LIVE)
    assert ts is not None
    assert ts.astimezone(datetime.timezone.utc) == datetime.datetime(
        2026, 9, 7, 7, 45, tzinfo=datetime.timezone.utc)


def test_midsummer_interval_is_still_utc_minus_5():
    d = json.loads(LIVE)
    for t in d["Fuel"]["Type"]:
        t["INTERVALEST"] = "2026-07-04 1:00:00 PM"
    ts = m.parse_miso_interval(json.dumps(d))
    assert ts.astimezone(datetime.timezone.utc) == datetime.datetime(
        2026, 7, 4, 18, 0, tzinfo=datetime.timezone.utc), (
        "July 13:00 EST must be 18:00Z; 17:00Z means a DST-aware zone crept in")


@pytest.mark.parametrize("bad", ["", "not json", '{"Fuel": {"Type": []}}',
                                 '{"Fuel": {"Type": [{"CATEGORY": "Coal"}]}}'])
def test_an_unparseable_stamp_is_none_not_a_guess(bad):
    """None lets persist_metrics fall back to the insert clock, which is
    honest. A fabricated stamp would dedup against the wrong interval."""
    assert m.parse_miso_interval(bad) is None
