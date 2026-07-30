"""Guard: the first non-US hosting-capacity sources, and their forecast-year pin.

WHAT THESE SOURCES ARE
──────────────────────
Ausgrid (Sydney / Hunter / Central Coast, NSW) publishes "Unlocking Hosting
Capacity" data through the NSW Government ArcGIS portal. Probed live 2026-07-30:

    FeatureServer/0  primary_ExportFeatures    2,160 rows  216 substations x 10 yr
    FeatureServer/1  secondary_ExportFeatures  2,120 rows  212 substations x 10 yr
    fields: available_capacity__load__at_n_, available_capacity__generation_,
            voltage_level_primary (33-132 kV), voltage_level_secondary (11 kV),
            substation, year (2025-2034), extract_date (20240627), owner
    geometry: EPSG:3857 upstream; the ingest already requests outSR=4326 and a
              sample resolves to 151.066, -33.879 (Sydney), so this is correct.

THREE TRAPS, all measured, all disclosed in capacity_basis:

  T1. ONE ROW PER SUBSTATION PER FORECAST YEAR. Unfiltered, ten futures of one
      substation become ten assets. The where-clause pins ONE year, which makes
      rows == substations (216 / 212 measured at year=2026).
  T2. UNITS ARE NOT PUBLISHED. No layer description, no copyrightText, bare field
      alias. AER DAPR practice is MVA; the observed 0.0-420.1 range at 132 kV
      fits MVA and MW equally, so the numbers cannot settle it. Treated as MVA
      and NOT asserted as MW, because MVA >= MW and the other reading
      over-states deliverable real power. Same call as the PECO layer.
  T3. IT IS A STALE FORECAST, NOT A READING. extract_date=20240627 on every row,
      for years running to 2034. The pinned year is a value projected ~2 years
      before the year arrived.

WHY THE YEAR IS A LITERAL AND NOT A CLOCK READ
───────────────────────────────────────────────
Two reasons, and the first is structural:

  a. SOURCES must stay evaluable from a bare literal. The sibling contract
     harness (tests/test_hosting_capacity_source_contract.py) execs the SOURCES
     assignment with ONLY `os` in scope. A first draft computed the year from
     datetime.date.today() and referenced module constants; that broke 15 tests
     across two files at once, because the literal stopped being self-contained.
  b. A self-advancing pin silently selects whatever year the clock says. If
     Ausgrid's next extract drops 2026, or shifts the span, `year = <now>`
     returns ZERO rows — and a zero-row source publishes "no capacity here"
     where the truth is "this forecast expired". Capacity also DECLINES across
     the span (Chullora STSS load 420.1 in 2025 -> 409.5 in 2030), so rolling
     the pin changes published values; that should be a decision, not a
     side-effect of the calendar.

So the pin is fenced instead: test_ausgrid_forecast_year_pin_is_current FAILS
when the calendar year moves past the pin. The fix is to re-probe Ausgrid, then
bump the literal — deliberately.

THE CONTRACT
────────────
  A1. Exactly the four Ausgrid sources are AU, and each declares country="AU".
  A2. Every Ausgrid source pins a single forecast year in its where-clause, and
      all four pin the SAME year (they are one dataset).
  A3. The pinned year is the current calendar year — the fence.
  A4. The pinned year is inside the span Ausgrid actually publishes.
  A5. load and gen read DIFFERENT upstream fields, and each source's
      order_field matches its own mw_max field (the SCE lesson: a capacity-DESC
      crawl ordered by a different column silently crawls the wrong head).
  A6. capacity_basis carries all three trap disclosures, and does NOT claim MW.
  A7. These are the first non-US sources, so US-scoped figures must not have
      been left un-scoped: the country column and the US-only total_feeders
      scoping (#1973) must be present in the module.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ b4f53093, i.e. #1973 merged but no AU sources):
    6 failed, 1 passed, 1 xfailed
    The 1 that passes in both states is A7, which #1973 already satisfies —
    it asserts the US-only scoping is in place BEFORE non-US data arrives, so it
    is supposed to be green on both sides:
        test_us_scoping_landed_before_any_non_us_source
PATCHED (this branch):                0 failed, 7 passed, 1 xfailed

★ FIRST DRAFT WENT QUIET ON THE TREE IT EXISTS TO REJECT. Four of these tests
  called pytest.skip() when no AU source was configured, which measured as
  `2 failed, 1 passed, 4 skipped` against unpatched main. A skip is not a
  failure: those four detected nothing on exactly the tree they were written
  for. Replaced with an assertion in _au() that the sources exist — 2 failed
  became 6 failed. Vacuous-when-absent is a real failure mode in guards that
  check properties OF a thing, and "it skipped" reads as "it was fine".

`1 xfailed` in BOTH runs — strict-xfail must-fail control, so a conftest-level
abort (rc 0, 0 tests, rendered as an ordinary job) cannot pass for green.

Tests never import main.py; nothing runs at module scope.

Run:  python3 -m pytest tests/test_ausgrid_au_forecast_pin.py -v
"""
import ast
import datetime
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "hosting_capacity_ingest.py")

# Measured live 2026-07-30 against the NSW portal.
AUSGRID_SPAN = (2025, 2034)
AUSGRID_SUBSTATIONS = {0: 216, 1: 212}
AUSGRID_EXTRACT_DATE = "20240627"


def _src_text():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return src, t


def _sources():
    """Exec the SOURCES literal with ONLY `os` in scope.

    Deliberately the same restriction the sibling contract harness applies: if a
    source entry ever needs more than `os` to evaluate, it has stopped being a
    self-contained literal and this assertion is the thing that says so.
    """
    src, t = _src_text()
    node = next((n for n in t.body
                 if isinstance(n, ast.Assign)
                 and any(getattr(x, "id", None) == "SOURCES" for x in n.targets)), None)
    assert node is not None, "SOURCES not found at module scope"
    ns = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), MOD, "exec"), ns)
    val = ns["SOURCES"]
    assert val, "SOURCES evaluated EMPTY — an empty list passes every check below"
    return val


def _au():
    """The Ausgrid sources — asserted present, never skipped.

    An earlier draft called pytest.skip() when no AU source existed. Measured
    against unpatched main that produced `4 skipped`, i.e. four tests that
    detected nothing: a skip is not a failure, so the guard went quiet on
    exactly the tree it exists to reject. This file's premise is that these four
    sources exist and are correct; absence is a contract violation, not a
    reason to stand down.
    """
    au = [s for s in _sources() if s.get("country") == "AU"]
    assert au, (
        "no country='AU' sources configured. This file guards the Ausgrid "
        "sources; if they were removed on purpose, delete this file in the same "
        "change rather than leaving a guard that passes by having nothing to do.")
    return au


def _pinned_year(src):
    m = re.search(r"year\s*=\s*(\d{4})", src.get("where") or "")
    return int(m.group(1)) if m else None


# ── A1 ────────────────────────────────────────────────────────────────────────
def test_the_four_ausgrid_sources_are_declared_au():
    au = _au()
    keys = sorted(s["key"] for s in au)
    assert keys == ["ausgrid_uhc_primary_gen", "ausgrid_uhc_primary_load",
                    "ausgrid_uhc_secondary_gen", "ausgrid_uhc_secondary_load"], \
        f"unexpected AU source set: {keys}"
    for s in au:
        assert "portal.data.nsw.gov.au" in s["url"], \
            f"{s['key']} does not read the NSW portal: {s['url']}"
        assert s["capacity_type"] in ("load", "gen"), s["key"]
    # and no US source accidentally became AU
    us = [s for s in _sources() if s.get("country") == "US"]
    assert len(us) == 28, f"US source count moved to {len(us)} — expected 28"


# ── A2 ────────────────────────────────────────────────────────────────────────
def test_every_ausgrid_source_pins_one_shared_forecast_year():
    au = _au()
    years = {s["key"]: _pinned_year(s) for s in au}
    missing = [k for k, y in years.items() if y is None]
    assert not missing, (
        f"{missing} do not pin a forecast year. Unfiltered, this layer returns "
        f"one row per substation PER YEAR — ten futures of one substation "
        f"published as ten assets.")
    distinct = set(years.values())
    assert len(distinct) == 1, (
        f"the four sources pin DIFFERENT years {years} — they are one dataset "
        f"and a split pin would publish two vintages side by side")


# ── A3 — the fence ────────────────────────────────────────────────────────────
def test_ausgrid_forecast_year_pin_is_current():
    """FAILS when the calendar moves past the pin. That is the point.

    Bumping the literal is a deliberate act: re-probe Ausgrid first, because a
    year it no longer publishes returns zero rows, and a zero-row source reads
    as "no capacity here" rather than "this forecast expired". Capacity also
    declines across the span, so the bump changes published values.
    """
    au = _au()
    pinned = _pinned_year(au[0])
    now = datetime.date.today().year
    assert pinned == now, (
        f"Ausgrid forecast year is pinned at {pinned} but the current year is "
        f"{now}. Re-probe "
        f"https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
        f"Ausgrid_UHC_Data/FeatureServer/0/query?where=year%3D{now}"
        f"&returnCountOnly=true — confirm it returns ~{AUSGRID_SUBSTATIONS[0]} "
        f"rows, then bump the literal in all four sources. Do NOT make the pin "
        f"self-advancing: a year Ausgrid does not publish returns zero rows, "
        f"which this pipeline would publish as 'no capacity'.")


# ── A4 ────────────────────────────────────────────────────────────────────────
def test_pinned_year_is_inside_the_span_ausgrid_publishes():
    au = _au()
    pinned = _pinned_year(au[0])
    lo, hi = AUSGRID_SPAN
    assert lo <= pinned <= hi, (
        f"pinned year {pinned} is outside the measured published span "
        f"{lo}-{hi} — that where-clause returns ZERO rows, which this pipeline "
        f"would publish as 'no capacity available'")


# ── A5 ────────────────────────────────────────────────────────────────────────
def test_load_and_gen_read_different_fields_and_order_by_their_own():
    au = {s["key"]: s for s in _au()}
    for key, s in au.items():
        mw_field = s["fields"]["mw_max"][0]
        assert s.get("order_field") == mw_field, (
            f"{key} orders by {s.get('order_field')!r} but reads {mw_field!r} — "
            f"a capacity-DESC crawl ordered by another column crawls the wrong "
            f"head (the SCE lesson)")
        expect = ("available_capacity__generation_" if s["capacity_type"] == "gen"
                  else "available_capacity__load__at_n_")
        assert mw_field == expect, (
            f"{key} is capacity_type={s['capacity_type']} but reads {mw_field} — "
            f"load and generation headroom are different quantities and this "
            f"layer carries BOTH in the same row")
    load = {k for k, s in au.items() if s["capacity_type"] == "load"}
    gen = {k for k, s in au.items() if s["capacity_type"] == "gen"}
    assert load and gen, f"expected both load and gen sources: {sorted(au)}"


# ── A6 ────────────────────────────────────────────────────────────────────────
def test_basis_discloses_all_three_traps_and_never_claims_mw():
    au = _au()
    for s in au:
        b = s["capacity_basis"]
        assert "UNITS ARE NOT PUBLISHED" in b, \
            f"{s['key']} does not disclose that units are unpublished (T2)"
        assert "MVA" in b, f"{s['key']} does not name the MVA reading (T2)"
        assert "FORECAST" in b, f"{s['key']} does not disclose T3 (stale forecast)"
        assert AUSGRID_EXTRACT_DATE in b, \
            f"{s['key']} does not state the measured extract_date (T3)"
        assert "PER FORECAST YEAR" in b, \
            f"{s['key']} does not disclose the per-year row shape (T1)"
        assert "N-1" in b, f"{s['key']} does not state the N-1 basis"
        # ★ must NOT assert MW. The give-away phrasing is "MW of ..." which every
        # US entry legitimately uses, because those layers DO publish MW.
        assert not re.search(r"\bMW of\b", b), (
            f"{s['key']} claims 'MW of ...' — Ausgrid publishes no unit and this "
            f"figure is treated as MVA")


# ── A7 ────────────────────────────────────────────────────────────────────────
def test_us_scoping_landed_before_any_non_us_source():
    """A non-US source must not arrive before US-scoped figures were fenced.

    #1973 added the country column and held total_feeders US-only. If these
    sources ever land in a tree without it, `total_feeders` silently becomes
    US+AU under a name every consumer reads as the US.
    """
    src, _ = _src_text()
    assert "_ALLOWED_COUNTRIES" in src, \
        "no country allow-list — the country column change (#1973) is missing"
    assert '_us = [m for m in markets if m["country"] == "US"]' in src, (
        "total_feeders is not US-scoped, but non-US sources are being "
        "configured — that is the silent redefinition #1973 exists to prevent")
    assert "total_feeders_non_us" in src, \
        "non-US coverage has nowhere to be reported"


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
