#!/usr/bin/env python3
"""tests/test_thin_content_lanes.py — the three thin-content lanes.

Measured on discovered_facilities 2026-08-14 (17,948 live rows): 408 carry no
power, no coordinates, no address and no real city. Those pages cannot rank and
Google already refuses them ("Crawled – currently not indexed", 3,563 pages).

WHAT THIS PINS, IN BOTH DIRECTIONS — the over-suppression side is the dangerous
one, because it fails silently by de-indexing pages that were fine:

  L3  contentless (all four absent)      -> noindex
  L3  ANY one fact present               -> still indexable
      ★ coordinates alone must NOT trigger it: _is_junk_facility's docstring
        records that an evidence test on coords would de-index 45 REAL
        coordinate-less OSM facilities. That is why is_contentless needs all
        four, and why this file tests each single-fact case separately.
  L2  context block renders only TRUE facts, and '' when there are none
  L1  infra slice stays OFF unless THIN_INFRA_SLICE=1 (a pricing decision)
      ★ 2026-08-15: LANE 1 IS NO LONGER A NO-OP. routes/substation_band_producer.py
        writes discovered_facilities.substation_band and facility_profile_page.py
        SELECTs it, so arming the flag now publishes a band. The old
        `test_lane1_is_inert_because_nothing_produces_the_band` was written to
        fail exactly when that happened; it fired and was REPLACED (not deleted)
        by the producer-exists / reaches-the-page / band-edges tests below.
  --  'California Regional' / 'Connecticut Regional' are REAL market labels
      (136 rows each) and must never be read as the 'Regional' placeholder

MUST-FAIL — executed 2026-08-15 against THIS suite, each mutation asserted to
have applied before running (PYTHONDONTWRITEBYTECODE=1; see the .pyc trap below):
    baseline                                  exit=0  14 passed
    M1  is_contentless -> any() (gate         exit=1   5 failed,  9 passed
        inverted)
    M2  placeholder test -> substring         exit=1   1 failed, 13 passed
    M3  infra slice default flipped to ON     exit=1   2 failed, 12 passed
    M4  _BANDS edges reordered (5.0 before    exit=1   1 failed, 13 passed
        1.0) — CASE arms are order-sensitive
    M5  band_for_km returns the tightest      exit=1   1 failed, 13 passed
        band for every distance
    M6  profile page stops SELECTing          exit=1   1 failed, 13 passed
        substation_band

★ M3 IS A SAME-LENGTH MUTATION ("0" -> "1") AND FIRST READ AS A CLEAN RESTORE.
Python's bytecode cache keys on size+mtime, so the stale .pyc survived the
edit and the run after "restoring" still showed 2 red. Clearing __pycache__
between mutations is what made it honest. A same-length mutation that appears
not to take is the .pyc, not the guard.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from util.thin_content import (  # noqa: E402
    context_block, evidence, infra_slice_armed, is_contentless, real_city,
)

EMPTY = {"name": "X", "city": "Regional", "country": "RO",
         "power_mw": None, "latitude": None, "longitude": None, "address": ""}


# ── LANE 3: suppress, but only the genuinely contentless ──────────────────
def test_contentless_page_is_suppressed():
    assert is_contentless(EMPTY), (
        "a facility with no power, no coords, no address and no real city is "
        "the 408-row class Google already refuses to index")


@pytest.mark.parametrize("field,value", [
    ("power_mw", 12.5),
    ("address", "1 Example Way"),
    ("city", "Iasi"),
])
def test_a_single_real_fact_keeps_the_page_indexable(field, value):
    fac = dict(EMPTY); fac[field] = value
    assert not is_contentless(fac), (
        f"{field}={value!r} is a real, citable fact — suppressing this page "
        "is over-suppression, which fails silently")


def test_coordinates_alone_keep_the_page_indexable():
    """The specific case _is_junk_facility refused to take.

    45 real OSM facilities have coordinates and little else. If coords stop
    counting as evidence, they all go noindex and nobody notices.
    """
    fac = dict(EMPTY); fac["latitude"] = 47.16; fac["longitude"] = 27.58
    assert not is_contentless(fac)
    assert evidence(fac)["coords"] is True


def test_placeholder_city_is_not_a_real_city():
    assert real_city({"city": "Regional"}) == ""
    assert real_city({"city": "  UNKNOWN "}) == ""


def test_real_regional_market_labels_survive():
    """136 rows each — equality, never substring."""
    for c in ("California Regional", "Connecticut Regional"):
        assert real_city({"city": c}) == c, (
            f"{c!r} was swallowed by the placeholder test — a substring match "
            "on 'regional' silently strips 272 REAL pages")


# ── LANE 2: context renders facts, or nothing ─────────────────────────────
def test_context_block_renders_only_true_facts():
    fac = {"city": "Iasi", "country": "RO", "power_mw": 8}
    dcpi = {"market_name": "Iasi", "iso": "Transelectrica", "verdict": "build",
            "time_to_power_months": 18}
    html = context_block(fac, dcpi)
    for want in ("Iasi", "Transelectrica", "BUILD", "18 months", "8 MW"):
        assert want in html, f"{want!r} missing from the context block"
    assert "None" not in html, "a null field leaked into the rendered block"


def test_context_block_is_empty_when_there_is_nothing_true():
    assert context_block(EMPTY, None) == "", (
        "a contentless page must not gain a 'Market & grid context' header "
        "with an empty body — that is filler with extra steps")


# ── LANE 1: pricing decision, off by default ──────────────────────────────
def test_infra_slice_is_off_unless_explicitly_armed(monkeypatch):
    monkeypatch.delenv("THIN_INFRA_SLICE", raising=False)
    assert infra_slice_armed() is False, (
        "LANE 1 is adjacent to the paid product — it must never default on")
    monkeypatch.setenv("THIN_INFRA_SLICE", "1")
    assert infra_slice_armed() is True


def test_infra_row_absent_when_disarmed(monkeypatch):
    monkeypatch.delenv("THIN_INFRA_SLICE", raising=False)
    fac = {"city": "Iasi", "country": "RO", "substation_band": "within 5 km"}
    assert "within 5 km" not in context_block(fac, None), (
        "the infra band rendered while LANE 1 was disarmed — that is a paid "
        "signal leaking onto 12,942 public pages")


def test_lane1_producer_exists_and_reaches_the_page(monkeypatch):
    """★★★ LANE 1 IS NO LONGER A NO-OP — this replaces the inert-lane guard.

    Until 2026-08-15 `_infra_rows` read fac["substation_band"] and NOTHING
    wrote it: no column, no migration, no backfill, and facility_profile_page.py
    never selected it. The predecessor of this test asserted that no-op and was
    written to FAIL the moment a producer landed. It has now fired, and it is
    replaced — not deleted — by the three things that must stay true instead:

      1. a producer writes the key,
      2. the profile page SELECTs it, so the dict reaching context_block can
         carry it,
      3. every band label the producer can write actually renders through the
         reader (producer/reader vocabulary agreement).

    (3) is the one worth having. (1) and (2) are existence checks that a
    refactor could satisfy while quietly renaming a band, and a band the reader
    cannot render is a column full of values that show up nowhere.
    """
    import pathlib
    import re

    def code_mentions_band(text: str) -> bool:
        without_docstrings = re.sub(r'"""(?:.|\n)*?"""', "", text)
        without_docstrings = re.sub(r"'''(?:.|\n)*?'''", "", without_docstrings)
        for line in without_docstrings.splitlines():
            if "substation_band" in line.split("#", 1)[0]:
                return True
        return False

    root = pathlib.Path(ROOT)
    hits = set()
    for sub in ("util", "routes"):
        for path in (root / sub).rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if code_mentions_band(text):
                hits.add(path.relative_to(root).as_posix())

    # Anti-vacuity: the reader must still match, or the walk is broken and
    # every assertion below passes for the wrong reason.
    assert "util/thin_content.py" in hits, (
        "scan matched nothing at all — the walk is broken, not the invariant")

    assert "routes/substation_band_producer.py" in hits, (
        "the LANE 1 producer is gone from the scan — if it was renamed, update "
        "this test AND routes/thin_content_master_shell.py's lane1_infra "
        "block; if it was deleted, the lane is a no-op again and "
        "renders_on_pages must go back to 0")

    assert "routes/facility_profile_page.py" in hits, (
        "facility_profile_page.py no longer references substation_band — the "
        "dict reaching context_block cannot carry a band, so LANE 1 renders "
        "nothing no matter what the producer wrote")

    # ★ Producer/reader vocabulary agreement. Every label the producer is
    # capable of writing must survive the reader when armed. A band written
    # into the column that _infra_rows drops is a silent content hole.
    monkeypatch.setenv("THIN_INFRA_SLICE", "1")
    from routes.substation_band_producer import _BANDS, _BAND_OVER
    labels = [label for _edge, label in _BANDS] + [_BAND_OVER]
    assert labels, "producer declares no bands at all"
    for label in labels:
        fac = {"name": "Ashburn DC", "city": "Ashburn", "country": "US",
               "substation_band": label}
        out = context_block(fac, None)
        assert "Nearest substation" in out and label in out, (
            f"producer can write {label!r} but the reader does not render it — "
            "producer and reader vocabularies have drifted")


def test_band_edges_are_ordered_and_assign_the_tightest_true_band():
    """★ The banding DECISION, not just the vocabulary.

    The test above can only catch a reader that stops rendering — `_infra_rows`
    echoes whatever non-empty string it is given, so it would happily publish a
    mislabelled band. This pins the label a distance actually earns.

    band_for_km is generated from the same _BANDS tuple as the SQL CASE the
    backfill runs, so an inverted or reordered arm shows up here rather than as
    17,948 quietly wrong public pages.
    """
    from routes.substation_band_producer import (
        _BANDS, _BAND_OVER, band_for_km)

    edges = [edge for edge, _label in _BANDS]
    assert edges == sorted(edges), (
        f"band edges are not ascending ({edges}) — CASE arms are evaluated in "
        "order, so an out-of-order edge makes a nearer band unreachable")
    assert len(set(edges)) == len(edges), f"duplicate band edge in {edges}"

    # No substation inside the search box, but the dataset DOES cover here ->
    # top band. This is the only case where "empty box" means "far away".
    assert band_for_km(None, True) == _BAND_OVER
    assert band_for_km(None) == _BAND_OVER, "default must stay in_coverage=True"

    # Each edge is INCLUSIVE, and a hair over it falls to the next band out.
    for i, (edge, label) in enumerate(_BANDS):
        assert band_for_km(edge) == label, (
            f"{edge} km did not land in {label!r} — edge should be inclusive")
        nxt = _BANDS[i + 1][1] if i + 1 < len(_BANDS) else _BAND_OVER
        assert band_for_km(edge + 0.001) == nxt, (
            f"just past {edge} km should band as {nxt!r}")

    # A distance beyond every edge is the top band, not the tightest one —
    # the failure mode that would publish "within 1 km" for a 40 km facility.
    assert band_for_km(edges[-1] + 100) == _BAND_OVER
    assert band_for_km(0) == _BANDS[0][1]


def test_lane1_never_bands_outside_dataset_coverage():
    """★ AN EMPTY SEARCH BOX IS NOT A DISTANCE MEASUREMENT.

    Until 2026-08-16 the producer mapped "no substation in the box" straight to
    "over 25 km". `substations` is HIFLD (US-only) plus scattered OSM rows, so
    for a facility in Frankfurt the box was empty because the dataset has no
    German substations at all — and the page published "over 25 km" as if it
    were measured. 8,911 of 12,942 banded rows (68.9%) were that artefact, vs
    4,031 with a real measurement.

    The gate: claim distance ONLY on positive evidence of coverage. Absent
    that, write '' — which `_infra_rows` renders as nothing.
    """
    from routes.substation_band_producer import (
        _BAND_OVER, _NO_COVERAGE, band_for_km, _band_case_sql)

    # The whole point: same empty box, opposite answers.
    assert band_for_km(None, False) == _NO_COVERAGE, (
        "no substation coverage must yield the absent sentinel, not a distance "
        "claim — this is the Frankfurt case that shipped 'over 25 km'")
    assert _NO_COVERAGE == "", (
        "_infra_rows tests falsiness of the stripped string; a non-empty "
        "sentinel would RENDER, which is the bug this gate exists to stop")
    assert band_for_km(None, False) != _BAND_OVER

    # Coverage must never be able to downgrade a real measurement: a distance
    # in hand is itself proof the dataset covers this place.
    for km in (0.0, 0.9, 4.2, 26.0, 500.0):
        assert band_for_km(km, False) == band_for_km(km, True), (
            f"{km} km banded differently by coverage — a measured distance "
            "must not consult the coverage probe at all")

    # And the SQL twin must gate the SAME arm. If the coverage expression drifts
    # out of the NULL arm, the Python twin above still passes while every
    # production row goes back to being banded ungated.
    case = _band_case_sql("n.km", "COV")
    null_arm = case.split("WHEN n.km <= ")[0]
    assert "COV" in null_arm, (
        "coverage expression is not in the IS NULL arm — the gate is not "
        "actually applied by the statement the backfill runs")
    assert f"ELSE '{_NO_COVERAGE}' END" in null_arm, (
        "the uncovered branch does not write the absent sentinel")
    # It must appear ONLY there — a coverage test on a measured arm would make
    # the probe run for every row, which is the cost this design avoids.
    assert case.count("COV") == 1, (
        f"coverage expression appears {case.count('COV')}x; it belongs in the "
        "NULL arm alone so Postgres short-circuits it for measured rows")


def test_lane1_still_renders_nothing_without_a_band(monkeypatch):
    """The fail-soft half, and the reason the profile-page probe exists.

    Between deploying the producer and POSTing its backfill, substation_band is
    NULL (or the column does not exist yet and the page SELECTs
    `NULL AS substation_band`). Armed, that must add nothing rather than an
    empty row — otherwise every not-yet-backfilled page grows a blank field.
    """
    monkeypatch.setenv("THIN_INFRA_SLICE", "1")
    assert infra_slice_armed() is True
    # The dict shape the profile page builds for a row with no band yet.
    for band in (None, "", "   "):
        real_shape = {"name": "Ashburn DC", "city": "Ashburn", "country": "US",
                      "power_mw": 30, "latitude": 39.0, "longitude": -77.5,
                      "substation_band": band}
        assert "Nearest substation" not in context_block(real_shape, None), (
            f"armed LANE 1 rendered a substation row for band={band!r} — "
            "un-backfilled pages would gain an empty field")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── the top band must be MEASURED, never inferred (2026-08-16, 2nd correction) ──
# The first correction gated 'over 25 km' on coverage evidence in a 1.5° box.
# 252 rows survived it, all riding on substations across a national border:
# coverage_km min 46.0 / median 73.5 / max 183.7, zero rows inside 40 km,
# 239 of 252 Canadian. Coverage evidence now comes from the SEARCH box alone.

def test_write_path_never_bands_an_empty_search_box_as_far():
    """THE PIN. An empty search box must produce '' — the only way to reach the
    top band is a measured distance. Mutation: put any truthy coverage
    expression back into the backfill's coverage_sql -> red."""
    import inspect
    from routes.substation_band_producer import (
        backfill_substation_bands, _band_case_sql, _BAND_OVER, _NO_COVERAGE,
        band_for_km)
    src = inspect.getsource(backfill_substation_bands)
    assert 'coverage_sql = "FALSE"' in src, (
        "the backfill's coverage expression is no longer FALSE — an empty "
        "search box can reach 'over 25 km' again")
    # and the generated SQL it feeds must send the NULL arm to the blank
    sql = _band_case_sql("n.km", "FALSE")
    assert f"WHEN FALSE THEN '{_BAND_OVER}' ELSE '{_NO_COVERAGE}'" in sql, sql
    # the twin in Python agrees
    assert band_for_km(None, False) == _NO_COVERAGE
    # the top band is NOT retired — a measurement past the last edge still lands
    assert band_for_km(26.0) == _BAND_OVER


def test_audit_box_is_not_wired_into_the_banding_decision():
    """_AUDIT_BOX_DEG powers ?sample= only. Mutation: reference it from
    backfill_substation_bands -> red."""
    import inspect
    from routes import substation_band_producer as sbp
    # Match the f-string INTERPOLATION, not the name: the write path names the
    # constant in a comment explaining why it is gone, and a guard that cannot
    # tell a comment from a query would fail on its own documentation.
    assert "{_AUDIT_BOX_DEG}" not in inspect.getsource(
        sbp.backfill_substation_bands), (
        "the 167 km audit box is back in the write path — that is the exact "
        "radius that banded 239 Canadian facilities off US substations")
    assert "{_AUDIT_BOX_DEG}" in inspect.getsource(sbp.top_band_sample)
