#!/usr/bin/env python3
"""tests/test_dataset_inventory_guard.py — the dataset inventory must be able to
FAIL, and must never report a failure it did not measure.

NO NETWORK, NO DB, NO 18-SECOND EXTRACTION. `check()` accepts an injected
`surface`, so every case here drives the real comparison logic against synthetic
inventories.

WHY THIS EXISTS. Four datasets froze within four days in March 2026 and none was
restarted for five months: discovered_transmission_lines, metro_fiber_summary,
usgs_water_stress, facility_permits. Nothing in the system could say a dataset
had stopped being written while the product kept publishing it as current.

THE PREDICATE. TIER-1 = CLAIM ∧ FOREIGN — a public surface asserts that rows we
did not mint are current. The foreign half is what makes the fence readable: a
table our own traffic writes cannot freeze the way the March cluster froze, and
without that gate the claim half alone produces mostly bookkeeping noise.

★ metro_fiber_summary is deliberately NOT foreign. Its only writer is a one-shot
seed script over a hardcoded Python literal, so no honest code signal makes it
external — and stretching "foreign" to reach it is exactly the fragile rule that
makes a fence rot. It is caught by the claimed_orphan fence instead, on the
truthful grounds that the product publishes it and nothing writes it any more.

Run standalone:   python3 tests/test_dataset_inventory_guard.py
Run under pytest: pytest tests/test_dataset_inventory_guard.py
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import dataset_inventory as DI  # noqa: E402

BASELINE = os.path.join(ROOT, "contracts", "dataset_inventory.json")
FROZEN_FOUR = ("discovered_transmission_lines", "metro_fiber_summary",
               "usgs_water_stress", "facility_permits")


# ── the foreign gate, unit level ───────────────────────────────────────────

def _foreign(tables, writes=None, texts=None, files=(), wf=()):
    return DI.foreign_tables(set(tables), list(files), texts or {}, writes or {}, list(wf))


def test_a_table_with_no_writer_can_still_be_foreign():
    """THE REGRESSION THAT SHAPED THIS. usgs_water_stress and
    discovered_transmission_lines have ZERO writers anywhere in the repo, so any
    signal read off a writer file is structurally blind to them."""
    got = _foreign(["discovered_transmission_lines"])
    assert "discovered_transmission_lines" in got, (
        "a zero-writer table was invisible to the foreign gate — that is how "
        "two of the four March datasets escape")
    assert "crawl_output" in got["discovered_transmission_lines"]


def test_a_source_host_token_in_the_name_marks_a_table_foreign():
    texts = {"loader.py": "requests.get('https://waterdata.usgs.gov/nwis')"}
    got = _foreign(["usgs_water_stress"], texts=texts, files=["loader.py"])
    assert got.get("usgs_water_stress") == ["source_token"]


def test_a_generic_host_is_not_a_source_token():
    """github/amazonaws/google are plumbing, not data sources. If they counted,
    almost every table would read as foreign and the gate would do nothing.

    ★ The host's org label must actually EQUAL a part of the table name, or this
    test passes whether or not the filter exists. The first version used
    'raw.githubusercontent.com' (org='githubusercontent') against a table named
    github_sync_state (part='github') — they never matched, so removing the
    filter left it green. Mutation caught that."""
    texts = {"x.py": "requests.get('https://api.github.com/repos/a/b')"}   # org == 'github'
    assert _foreign(["github_sync_state"], texts=texts, files=["x.py"]) == {}


def test_a_table_we_mint_ourselves_is_not_foreign():
    assert _foreign(["user_sessions"], writes={"user_sessions": {"routes/auth.py:12"}}) == {}


def test_a_fetching_writer_marks_its_table_foreign():
    texts = {"permit_scraper.py": "requests.get('https://permits.example.gov/x')"}
    got = _foreign(["facility_permits"],
                   writes={"facility_permits": {"permit_scraper.py:141"}},
                   texts=texts, files=["permit_scraper.py"])
    assert "fetching_writer" in got.get("facility_permits", [])


# ── the three-valued exit contract ─────────────────────────────────────────

def _surface(tables, tier1=None):
    n1 = tier1 if tier1 is not None else sum(1 for v in tables.values() if v.get("tier") == 1)
    return {"schema_version": 1, "stats": {"tier1_claimed": n1},
            "parse_errors": [], "tables": tables}


_T1 = {"tier": 1, "why": ["mcp_served"], "foreign": True, "foreign_signals": ["crawl_output"],
       "watched_by": ["infra_growth._LAYERS"], "live_write_paths": ["loaders/x.py"],
       "zero_writer": False, "read_sites": 3, "write_sites": 1, "serving_routes": 1,
       "mcp_tools": ["get_x"], "claim_routes": [], "claimed_but_ours": False,
       "orphan_loader_files": [], "created_in": [], "first_read": None, "first_write": None}


def _mk(**over):
    r = dict(_T1)
    r.update(over)
    return r


def test_a_clean_diff_passes(tmp_path):
    base = _surface({"a": _mk()})
    p = tmp_path / "b.json"
    p.write_text(json.dumps(base))
    assert DI.check(str(p), verbose=False, surface=base) == 0


def test_a_new_unwatched_tier1_fails(tmp_path):
    base = _surface({"a": _mk()})
    cur = _surface({"a": _mk(), "b": _mk(watched_by=[])})
    p = tmp_path / "b.json"
    p.write_text(json.dumps(base))
    assert DI.check(str(p), verbose=False, surface=cur) == 1, (
        "a newly claimed-and-foreign dataset that no registry watches must fail")


def test_a_new_tier1_that_IS_watched_passes(tmp_path):
    """Additive change is never blocked — only unwatched additions."""
    base = _surface({"a": _mk()})
    cur = _surface({"a": _mk(), "b": _mk()})
    p = tmp_path / "b.json"
    p.write_text(json.dumps(base))
    assert DI.check(str(p), verbose=False, surface=cur) == 0


def test_a_missing_baseline_is_unmeasured_not_a_pass(tmp_path):
    assert DI.check(str(tmp_path / "nope.json"), verbose=False,
                    surface=_surface({"a": _mk()})) == 2


def test_a_malformed_baseline_is_unmeasured_not_a_failure(tmp_path):
    """★ It used to raise KeyError and exit 1 — reporting "your PR broke
    something" when the truth was "the guard cannot measure". Conflating those
    is the defect this guard exists to prevent; it must not be in the guard."""
    p = tmp_path / "b.json"
    p.write_text('{"schema_version": 1}')
    assert DI.check(str(p), verbose=False, surface=_surface({"a": _mk()})) == 2


def test_an_empty_computed_inventory_is_unmeasured(tmp_path):
    """★ The baseline's tier1_claimed is forced to 0 so the COLLAPSE guard
    (`if b1 and ...`) cannot fire first. The first version left it at 1, so the
    collapse guard caught the empty inventory and this test passed with the
    empty-guard deleted — it was testing the wrong branch. Mutation caught it."""
    base = _surface({"a": _mk()}, tier1=0)
    p = tmp_path / "b.json"
    p.write_text(json.dumps(base))
    assert DI.check(str(p), verbose=False, surface=_surface({}, tier1=0)) == 2, (
        "an extractor that found nothing must never read as a clean pass")


def test_an_extractor_collapse_is_unmeasured_not_a_flood(tmp_path):
    base = _surface({f"t{i}": _mk() for i in range(100)})
    cur = _surface({"t0": _mk()})
    p = tmp_path / "b.json"
    p.write_text(json.dumps(base))
    assert DI.check(str(p), verbose=False, surface=cur) == 2, (
        "a 99% drop is a broken extractor, not 99 datasets losing their claim")


# ── the committed baseline actually covers what it was built for ───────────

@pytest.mark.skipif(not os.path.exists(BASELINE), reason="baseline not generated")
def test_the_committed_baseline_covers_the_frozen_four():
    """Each must be reachable by the fence, by whichever route is TRUTHFUL for
    it — tier-1, the claimed-orphan list, or write-only. A table that is in
    none of them is invisible to this guard."""
    d = json.load(open(BASELINE))
    tables = d["tables"]
    for t in FROZEN_FOUR:
        assert t in tables, f"{t} is absent from the inventory entirely"
        v = tables[t]
        claimed_orphan = (v.get("tier") == 1 or v.get("claimed_but_ours")) \
            and not v.get("live_write_paths")
        assert v.get("tier") == 1 or claimed_orphan or v.get("tier") == "write_only", (
            f"{t} is tier={v.get('tier')} with live_write_paths="
            f"{v.get('live_write_paths')} — no fence can reach it")


@pytest.mark.skipif(not os.path.exists(BASELINE), reason="baseline not generated")
def test_the_baseline_is_not_vacuous():
    d = json.load(open(BASELINE))
    assert d["stats"]["tier1_claimed"] >= 20, "tier-1 collapsed; the extractor is broken"
    assert len(d["tables"]) >= 300, "the inventory is far smaller than the repo"
    assert d.get("_readme", "").startswith("DERIVED FILE"), \
        "the derived file lost its do-not-hand-edit banner"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
