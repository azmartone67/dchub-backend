"""A source that does not report capacity must write NULL, never 0.

MEASURED, 2026-08-31. Before this change, on live:

    discovered_facilities   real 7,412   zero 13,901   null 6,389
    facilities              real 2,080   zero 14,119   null 7,301

Those zeros are not measurements. They are "the loader did not find a number",
written into a column that already had a way to say exactly that. The cost is
not cosmetic:

  * COUNT(power_mw) reported 68% coverage on live facilities; the real figure
    was 33%. Every "how much of the fleet do we have power for" answer was
    double the truth.
  * AVG(power_mw) — published from six call sites across api_server.py,
    main.py and dchub_mcp_server.py — was diluted toward zero by 7,002 phantom
    rows, roughly halving it.
  * SUM is the one aggregate zeros do NOT corrupt, which is exactly why this
    survived: the headline capacity number looked fine.

Four coercion points produced them, all fixed together:

    discovery_engine_v3  INSERT branch   f.get('power_mw', 0)
    discovery_engine_v3  table DDL       power_mw REAL DEFAULT 0
    discovery_engine_v3  BaxtelSource    power_mw = 0 before the regex
    routes/osm_crawler   record dict     "power_mw": 0

★ The UPDATE branch in discovery_engine_v3 was ALREADY correct
(`f.get('power_mw')`, no default) fifteen lines above the broken INSERT. The
same file was honest on update and lossy on insert, and new rows are the common
case — which is why openstreetmap ended up 82% zeros.
"""

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _code_only(text: str) -> str:
    """Source with `#` comments stripped.

    A guard that greps raw source cannot tell the defect from the note
    explaining the defect — and it fired on exactly that here, blocking a
    commit whose comment quoted the old `f.get('power_mw', 0)` while the code
    was already fixed. A guard that punishes documentation gets the
    documentation deleted, so scan CODE.

    Deliberately line-wise rather than tokenize(): a `#` inside a string
    literal would truncate that line, which is acceptable here (no scanned
    pattern lives inside a string containing `#`) and keeps the helper from
    failing on a file that does not parse."""
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


# ── the four coercion points stay fixed ──────────────────────────────

def test_insert_branch_does_not_default_power_to_zero():
    """The live bug. `f.get('power_mw', 0)` turns 'not reported' into a
    zero-megawatt data center."""
    s = _code_only(_src("discovery_engine_v3.py"))
    assert "f.get('power_mw', 0)" not in s, \
        "absence is being written as 0 again on the INSERT path"
    assert "f.get('sqft', 0)" not in s, "same defect on sqft"
    assert "f.get('power_mw')" in s


def test_update_branch_is_still_correct():
    """It was right all along — pin it so a future 'consistency' edit does not
    make BOTH branches wrong instead of both right."""
    s = _src("discovery_engine_v3.py")
    assert "power_mw = COALESCE(?, power_mw)" in s


def test_no_ddl_defaults_power_to_zero():
    s = _code_only(_src("discovery_engine_v3.py"))
    assert not re.search(r"power_mw\s+REAL\s+DEFAULT\s+0", s, re.I), \
        "a DEFAULT 0 makes an omitted column read as a measured zero"


def test_scraper_sentinel_is_none_not_zero():
    """BaxtelSource initialised power_mw = 0 before trying a regex, so a missing
    element or a non-matching string shipped a 0."""
    s = _code_only(_src("discovery_engine_v3.py"))
    assert "power_mw = None" in s
    assert not re.search(r"^\s+power_mw = 0\s*$", s, re.M)


def test_osm_crawler_record_dict_carries_none():
    s = _code_only(_src("routes/osm_crawler.py"))
    assert not re.search(r'"power_mw":\s*0\s*,', s), \
        "a 0 in the record dict is a loaded gun for the next writer"
    assert '"power_mw": None' in s


def test_osm_crawler_still_inserts_null():
    """Fixed 2026-08-11; keep it fixed."""
    s = _src("routes/osm_crawler.py")
    assert "power_mw + status NULL" in s or "power_mw and status are NULL" in s


# ── the repair script is honest about what it may touch ──────────────

REPAIR = "scripts/repair_power_mw_placeholder_zeros.py"


def test_repair_keys_on_exact_zero_not_a_range():
    """A `< 3` rule would reinterpret real sub-3 MW readings as unknown —
    inventing absence, the same error in the other direction. Live minima are
    1.0 MW (discovered_facilities) and 0.078 MW (facilities), so a range rule
    would have destroyed real data."""
    s = _src(REPAIR)
    assert "WHERE power_mw = 0" in s
    assert "SENTINEL = 0.0" in s
    assert not re.search(r"power_mw\s*<\s*[1-9]", s)


def test_repair_aborts_if_a_real_reading_could_be_a_sentinel():
    s = _src(REPAIR)
    assert 'float(before["min_real"]) <= SENTINEL' in s
    assert "Not touching this table" in s


def test_repair_rolls_back_if_real_values_changed():
    """The invariant: this reinterprets 0, it must never alter a measurement."""
    s = _src(REPAIR)
    assert 'after["real"] != before["real"]' in s
    assert "conn.rollback()" in s


def test_repair_is_dry_run_by_default():
    s = _src(REPAIR)
    assert '"--apply", action="store_true"' in s
    assert "Default is a dry run" in s


# ── nobody re-introduces the pattern in a facility writer ────────────

def test_no_facility_writer_defaults_power_to_zero():
    """Sweep the writers, not just the two files fixed here."""
    offenders = []
    for rel in ("discovery_engine_v3.py", "facility_ingestion.py",
                "routes/osm_crawler.py", "routes/discovery_routes.py",
                "news_facility_extractor.py"):
        p = ROOT / rel
        if not p.exists():
            continue
        s = _code_only(p.read_text(encoding="utf-8", errors="ignore"))
        for pat in (r"get\('power_mw',\s*0\)", r'get\("power_mw",\s*0\)',
                    r"power_mw\s+REAL\s+DEFAULT\s+0"):
            if re.search(pat, s, re.I):
                offenders.append(f"{rel}: {pat}")
    assert not offenders, (
        "absence is being coerced to 0 again: " + "; ".join(offenders))


def test_the_swept_files_still_exist():
    """Guard the guard — a rename would make the sweep above vacuous."""
    found = [rel for rel in ("discovery_engine_v3.py", "facility_ingestion.py",
                             "routes/osm_crawler.py")
             if (ROOT / rel).exists()]
    assert len(found) == 3, f"expected 3 writer files, found {found}"
