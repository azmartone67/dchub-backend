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
  --  'California Regional' / 'Connecticut Regional' are REAL market labels
      (136 rows each) and must never be read as the 'Regional' placeholder

MUST-FAIL — executed, real exit codes, each mutation confirmed applied:
    baseline                                  exit=0  11 passed
    M1  is_contentless -> any() (gate         exit=1   5 failed
        inverted)
    M2  placeholder test -> substring         exit=1   1 failed
    M3  infra slice default flipped to ON     exit=1   2 failed

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


def test_lane1_is_inert_because_nothing_produces_the_band(monkeypatch):
    """★★★ ARMING LANE 1 RENDERS 0 PAGES — the flag is currently a NO-OP.

    `_infra_rows` reads fac["substation_band"]. Nothing writes it: no column,
    no migration, no backfill, and facility_profile_page.py never selects it.
    The fixture two tests up supplies the key BY HAND, so the existing lane-1
    tests pass while production renders nothing — the board was meanwhile
    reporting 12,942 `pages_with_coords` in the lane1 block, which reads as
    impact and is wrong by 12,942.

    ★ WHEN YOU BUILD THE PRODUCER, THIS TEST SHOULD FAIL. That is the point.
    Update it together with the lane1_infra block in
    routes/thin_content_master_shell.py (`renders_on_pages` / `producer` /
    `blocked_on`) so the pricing call is made against the real page count.
    """
    import pathlib
    import re

    # Only CODE counts. The board file names substation_band in an
    # explanatory comment (that is the whole point of the comment), so a bare
    # substring scan reports it as a producer. Strip comments and docstrings
    # before deciding, or this guard cries wolf at its own documentation.
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

    # Anti-vacuity: the reader itself must match, or the scan found nothing
    # and every assertion below would pass for the wrong reason.
    assert "util/thin_content.py" in hits, (
        "scan matched nothing at all — the walk is broken, not the invariant")

    # util/thin_content.py READS the key. thin_content_master_shell.py names it
    # in the board's own `blocked_on` string — a user-facing explanation of this
    # exact no-op, not a write. Neither supplies a value; anything else would.
    producers = hits - {"util/thin_content.py",
                        "routes/thin_content_master_shell.py"}
    assert not producers, (
        "a producer for substation_band now exists in "
        f"{sorted(producers)} — LANE 1 may no longer be a no-op. Re-measure "
        "how many pages actually carry a band and update "
        "routes/thin_content_master_shell.py's lane1_infra block "
        "(renders_on_pages/producer/blocked_on) before arming "
        "THIN_INFRA_SLICE=1.")

    # The specific file a producer would have to touch to reach the page.
    profile = (root / "routes" / "facility_profile_page.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "substation_band" not in profile, (
        "facility_profile_page.py now references substation_band — the dict "
        "reaching context_block may carry a band, so LANE 1 is no longer inert")

    # And the behavioural half: armed, with the dict the profile page really
    # builds (no substation_band key), the lane still adds nothing.
    monkeypatch.setenv("THIN_INFRA_SLICE", "1")
    assert infra_slice_armed() is True
    real_shape = {"name": "Ashburn DC", "city": "Ashburn", "country": "US",
                  "power_mw": 30, "latitude": 39.0, "longitude": -77.5}
    assert "Nearest substation" not in context_block(real_shape, None), (
        "armed LANE 1 rendered a substation row for a facility dict that "
        "carries no band — the no-op claim on the board is now wrong")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
