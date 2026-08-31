"""Guards for scripts/quarantine_peeringdb_power.py.

WHAT IT DID, 2026-08-31
-----------------------
5,855 facilities sourced `peeringdb` carried a power_mw the upstream does not
publish. Verified against the live PeeringDB API: a fac object has no power,
capacity, MW or kW field — the closest are available_voltage_services and
diverse_serving_substations, neither of which is a capacity.

The values were degenerate the way synthesized data is: 22 distinct values
across 5,855 rows, 4,973 of them (85%) at exactly 3.0 MW, all written on one
day. Genuinely sourced power looks nothing like it — operator_website 46
distinct at 9% mode, datacentermap 28 distinct at 11%.

Quarantining them moved three published numbers:

    coverage        33.2%  ->   6.9%   (1,375 facilities, all sourceable)
    average power    24.1  ->   96.9 MW
    total capacity 160,612 -> 133,264 MW

That last line is the one that matters: ~27,348 MW of capacity was being
published that did not exist, and ~4,445 facility pages said "it carries a
REPORTED power capacity of 3.0 MW" plus a meta description Google could show.

WHY THIS FILE EXISTS
--------------------
The script stays in the repo rather than being a hand-run UPDATE, so its safety
argument is reviewable and re-runnable. These tests pin that argument. The
important one is `test_it_aborts_when_the_distribution_looks_real`: if someone
later ingests genuine peeringdb power, this script must refuse to wipe it.
"""

import ast
import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "scripts" / "quarantine_peeringdb_power.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _const(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


# ── the safety argument ──────────────────────────────────────────────

def test_it_aborts_when_the_distribution_looks_real():
    """THE guard. Real power spreads out; the synthesized block did not. If
    genuine peeringdb capacity ever lands, this must refuse rather than wipe
    it."""
    assert "ABORT" in TEXT
    assert "MAX_DISTINCT_VALUES" in TEXT and "MIN_MODE_SHARE_PCT" in TEXT
    main_src = TEXT[TEXT.find("def main("):]
    assert "before[\"distinct\"] > MAX_DISTINCT_VALUES" in main_src
    assert "before[\"mode_pct\"] < MIN_MODE_SHARE_PCT" in main_src

    # ★ EACH abort must return, checked per-branch. Counting `return 1`
    # occurrences was too weak: deleting one still left two elsewhere (the other
    # guard and the post-update rollback), so a branch that printed ABORT and
    # then fell straight through to the UPDATE passed the count test. Walk the
    # ast and require a return inside every `if` whose body prints an ABORT.
    tree = ast.parse(TEXT)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    aborts = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        body_src = "\n".join(ast.get_source_segment(TEXT, st) or ""
                             for st in node.body)
        if "ABORT" not in body_src:
            continue
        aborts += 1
        assert any(isinstance(st, ast.Return) for st in node.body), (
            "an ABORT branch falls through to the UPDATE instead of returning:\n"
            + body_src[:200])
    assert aborts >= 3, f"expected 3 abort branches, walked {aborts}"


def test_the_thresholds_separate_synthetic_from_real():
    """Pin the VALUES, not their presence. Measured on live:

        peeringdb (synthetic)   22 distinct,  85% mode  -> must be caught
        operator_website (real) 46 distinct,   9% mode  -> must be spared
        datacentermap    (real) 28 distinct,  11% mode  -> must be spared

    datacentermap at 28 distinct is the tight one: the distinct-count threshold
    alone cannot separate it from the synthetic block, which is exactly why the
    mode-share test exists alongside it. Both must pass for a wipe."""
    max_distinct = _const("MAX_DISTINCT_VALUES")
    min_mode = _const("MIN_MODE_SHARE_PCT")

    # the synthetic block trips neither guard (so it IS quarantined)
    assert 22 <= max_distinct and 85.0 >= min_mode

    # ★ Defence in depth: the distinct-count guard must do REAL work on its own,
    # not lean entirely on mode share. Loosening it to 60 left every real source
    # protected only by the mode test — safe today, one edit from unsafe.
    assert max_distinct <= 50, (
        f"MAX_DISTINCT_VALUES={max_distinct} no longer separates the synthetic "
        f"block (22) from operator_website (46) — the distinct guard has "
        f"stopped contributing")

    # every real source is spared by at least one guard
    for name, distinct, mode in (("operator_website", 46, 9.0),
                                 ("datacentermap", 28, 11.0),
                                 ("cloudscene", 34, 8.0)):
        spared = distinct > max_distinct or mode < min_mode
        assert spared, (
            f"{name} ({distinct} distinct, {mode}% mode) would be wiped — "
            f"loosen nothing until this passes")


def test_dry_run_is_the_default():
    assert '"--apply", action="store_true"' in TEXT
    assert "Default is a dry run" in TEXT


def test_it_rolls_back_if_values_survive_the_update():
    assert 'after["n"] != 0' in TEXT
    assert "conn.rollback()" in TEXT


def test_it_nulls_rather_than_deletes():
    """The row, its identity and its provenance stay. Only the unsourceable
    number goes — NULL is what the schema already means by 'unknown'."""
    assert "SET power_mw = NULL" in TEXT
    # Match a STATEMENT, not the word. An earlier version asserted "DELETE" was
    # absent from the file and tripped on this script's own docstring heading,
    # "WHY NULL RATHER THAN DELETE" — the same crude-substring trap that made a
    # sibling guard fire on "interconnection" for containing "conn".
    assert not re.search(r"\bDELETE\s+FROM\b", TEXT, re.I), \
        "this must never delete a row — only the unsourceable number goes"
    assert not re.search(r"\bDROP\s+(TABLE|COLUMN)\b", TEXT, re.I)


def test_it_records_why_in_notes_without_clobbering():
    """A quarantine nobody can explain later is indistinguishable from data
    loss."""
    assert "notes = CASE" in TEXT
    assert "notes || ' | ' ||" in TEXT, "existing notes must be preserved"
    note = _const("NOTE")
    assert "not sourced" in note and "PeeringDB API" in note
    assert "2026-08-31" in note


def test_it_is_case_insensitive_on_source():
    """Both `peeringdb` and `PeeringDB` exist in this table — the casing split
    is how 5,849 duplicate rows arrived under a second id namespace."""
    assert "LOWER(source) = %s" in TEXT
    assert _const("SOURCE_MATCH") == "peeringdb"


def test_it_is_idempotent():
    assert "nothing to quarantine" in TEXT
    assert 'if not before["n"]' in TEXT


def test_the_evidence_is_recorded_in_the_docstring():
    """The claim 'PeeringDB publishes no power field' is the whole basis for
    this script. It must travel with the code, not live in a chat log."""
    doc = ast.get_docstring(TREE) or ""
    assert "available_voltage_services" in doc, \
        "the actual API field list is the evidence — keep it verbatim"
    for tok in ("no power", "22 distinct", "3.0 MW", "2026-03-18"):
        assert tok in doc, f"missing evidence: {tok}"
    assert re.search(r"33\.2%.*6\.9%|6\.9%", doc), \
        "the cost of running this must be stated, not discovered later"
