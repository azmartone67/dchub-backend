"""Guards for r-status-taxonomy (2026-07-29): op_mw must mean OPERATIONAL.

The DCPI saturation index published `local_operational_mw` from an unfiltered
`SUM(power_mw)` over discovered_facilities — every status, including the
pipeline it separately reported as pipeline_mw. So the published figure was a
total wearing the word "operational", and the index charged pipeline twice
(0.25 inside op_mw + 0.15 as pipe_mw).

Pure source/AST + pure-function asserts. No DB, no network, and — per
tests/conftest.py and CLAUDE.md — no `import main`: main.py is read as TEXT.
Nothing here runs at module scope.
"""
import ast
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _func_source(rel_path, func_name):
    """Source of a function, located via a real AST parse.

    Deliberately NOT an isinstance-filter that can quietly yield nothing: an
    empty candidate set would make every downstream `not in` assertion pass
    against zero characters. The parse is asserted, the function is asserted
    found, and the extracted segment is asserted non-trivial.
    """
    src = _read(rel_path)
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module) and tree.body, \
        f"{rel_path} produced an empty parse — assertions below would be vacuous"
    found = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            found = node
            break
    assert found is not None, f"{func_name} not found in {rel_path} (renamed?)"
    segment = ast.get_source_segment(src, found)
    assert segment and len(segment) > 200, \
        f"{func_name} source segment came back empty/tiny — extraction broken"
    return segment


def _text_window(rel_path, start_marker, end_marker):
    """TEXT slice between two markers — for code inside main.py, which this
    suite must never import (it opens DB pools and registers ~200 blueprints).
    Both markers are asserted present so a rename fails loudly."""
    src = _read(rel_path)
    i = src.find(start_marker)
    assert i != -1, f"start marker {start_marker!r} gone from {rel_path}"
    j = src.find(end_marker, i)
    assert j != -1, f"end marker {end_marker!r} gone from {rel_path}"
    window = src[i:j]
    assert len(window) > 500, "window too small — markers collapsed"
    return window


# ── the taxonomy module itself ─────────────────────────────────────────────

# Every status value observed live on 2026-07-29, in BOTH tables. The prompt's
# by_status list came from /api/v1/stats, which reads `facilities`; the DCPI
# query reads `discovered_facilities`. The vocabularies differ, which is why
# the filter is an allow-list over both rather than a deny-list over one.
_OBSERVED = {
    # discovered_facilities (the table the DCPI query actually scans)
    "Operational": "operational",
    "active": "operational",
    "operational": "operational",
    "Expanding": "operational",
    "Announced": "pipeline",
    "Under Construction": "pipeline",
    "Planned": "pipeline",
    "Under Development": "pipeline",
    "Approved": "pipeline",
    # facilities-only additions
    "announced": "pipeline",
    "planned": "pipeline",
    "under_construction": "pipeline",
    "Planning": "pipeline",
}


def test_status_taxonomy_buckets_every_observed_value():
    """No observed status may land in UNCLASSIFIED, and 'Announced' must be
    pipeline — under the old 5-string filter it was neither, so it was summed
    into the figure published as operational."""
    from util.status_taxonomy import classify
    for raw, expected in _OBSERVED.items():
        assert classify(raw) == expected, \
            f"{raw!r} classified {classify(raw)!r}, expected {expected!r}"
    # Case and whitespace variants normalise, not fall through to unclassified.
    for raw in ("  UNDER CONSTRUCTION ", "PLANNED", "Operational "):
        assert classify(raw) != "unclassified", f"{raw!r} fell through"
    # An unknown value is unknown — never silently operational.
    assert classify("mothballed") == "unclassified"
    assert classify(None) == "unclassified"
    assert classify("") == "unclassified"


def test_status_buckets_are_disjoint_and_normalised():
    """Disjoint buckets are what makes the 0.25 and 0.15 index terms measure
    different megawatts. An overlap silently restores the double-count."""
    from util import status_taxonomy as st
    assert st.OVERLAP == frozenset(), f"buckets overlap: {sorted(st.OVERLAP)}"
    assert st.OPERATIONAL_STATUSES and st.PIPELINE_STATUSES
    for value in (st.OPERATIONAL_STATUSES | st.PIPELINE_STATUSES):
        assert value == value.strip().lower(), \
            f"{value!r} is not in normalised form — SQL LOWER(TRIM()) can never match it"
    assert "announced" in st.PIPELINE_STATUSES
    assert "active" in st.OPERATIONAL_STATUSES   # 46% of rows, ~0 MW: a count, not capacity


def test_status_sql_is_case_insensitive_and_three_bucketed():
    """The emitted SQL must normalise (the old filter's three lowercase
    entries matched zero rows) and must expose an explicit third bucket."""
    from util.status_taxonomy import (operational_sql, pipeline_sql,
                                      unclassified_sql, basis)
    op, pipe, unk = operational_sql(), pipeline_sql(), unclassified_sql()
    for frag in (op, pipe, unk):
        assert "LOWER(TRIM(COALESCE(status,'')))" in frag, frag
        assert "%" not in frag, "a literal % in a query string is a psycopg2 500"
    assert " IN (" in op and " IN (" in pipe
    assert " NOT IN (" in unk, "unclassified must be the complement, not a guess"
    assert "'announced'" in pipe and "'announced'" not in op
    b = basis()
    assert b["taxonomy_version"] and b["operational_statuses"] and b["pipeline_statuses"]


# ── the call sites ─────────────────────────────────────────────────────────

def test_dcpi_op_mw_is_status_filtered_in_both_branches():
    """FAILS on pre-fix code: gather_metrics_for_market had
    `COALESCE(SUM(power_mw), 0) AS op_mw` in the US branch AND, 14 lines
    later, in the international branch."""
    src = _func_source("routes/dcpi.py", "gather_metrics_for_market")
    assert "COALESCE(SUM(power_mw), 0) AS op_mw" not in src, \
        "op_mw is an UNFILTERED SUM again — it contains pipeline"
    assert src.count("AS op_mw") == 2, \
        "expected exactly two footprint queries (US + intl)"
    # Both branches must be filtered, and both must carry the third bucket.
    assert src.count("_SQL_OP_STATUS") == 2, "one branch left unfiltered"
    assert src.count("_SQL_PIPE_STATUS") == 2
    assert src.count("AS unclassified_mw") == 2, \
        "unmapped statuses are being silently dropped or folded in"
    # The intl branch itself must survive (r-declone-2 guard). r-namesake
    # (2026-08-07) moved the predicate into the shared _market_country_scope,
    # so follow it there — and check both branches are actually scoped, which
    # is strictly more than the old single-literal match proved.
    from routes.dcpi import _market_country_scope
    # "{_ctry_sql}", not "_ctry_sql" — the bare name also matches the single
    # assignment above the branches, so counting it would pass with one branch
    # scoped and the other bare.
    assert src.count("{_ctry_sql}") == 2, \
        "a footprint branch lost its country scope"
    assert "NOT IN ('US', 'USA')" in _market_country_scope("NGESO", "UK")[0]
    assert "IN ('US', 'USA')" in _market_country_scope("PJM", "VA")[0]


def test_dcpi_publishes_the_status_basis_and_the_unclassified_bucket():
    """A number called operational must be auditable: which table, which
    filter, which values. Same house pattern as reserve_margin_basis."""
    src = _func_source("routes/dcpi.py", "gather_metrics_for_market")
    assert '"status_basis": _status_basis()' in src, \
        "published operational_mw is no longer auditable"
    assert '"source_table": "discovered_facilities"' in src
    assert '"unclassified_mw"' in src
    assert "local_unclassified_mw" in src
    # The ceiling is deliberately unchanged — see the comment block. Pinning
    # it here so a later "rebase it now op_mw is smaller" is a conscious edit.
    assert "_log_sat(_op_mw, 8000.0)" in src, \
        "op_mw ceiling moved — that re-clones markets r-declone-2 separated"


def test_lite_writers_do_not_divide_by_the_corrected_operational_mw():
    """Both lite writers upsert market_power_scores with
    ON CONFLICT (market_slug) DO UPDATE, so they clobber the full path's
    scores. There op_mw is a DENOMINATOR: shrinking it without changing the
    ratio's basis multiplies pipe_ratio ~2.6x and pins constraint at 100
    (verdict AVOID). Guards both copies — routes/dcpi.py and the same-URL
    duplicate in main.py."""
    blueprint = _func_source("routes/dcpi.py", "lite_recompute")
    inline = _text_window("main.py",
                          "=== Phase 216: DCPI lite-recompute",
                          "=== Phase 217:")
    for label, src, taxonomy_ref in (("routes/dcpi.py", blueprint, "_SQL_OP_STATUS"),
                                     ("main.py", inline, "status_taxonomy")):
        assert "pipe_ratio = (pipe_mw / op_mw)" not in src, \
            f"{label}: corrected op_mw still used as the ratio denominator"
        assert "_footprint_mw" in src, \
            f"{label}: ratio no longer taken against the total footprint"
        assert "COALESCE(SUM(power_mw), 0)" not in src, \
            f"{label}: unfiltered SUM(power_mw) is back"
        assert "'Under Construction','Planned'" not in src, \
            f"{label}: the hand-copied 5-string status literal is back"
        assert taxonomy_ref in src, \
            f"{label}: no longer sourcing statuses from the shared taxonomy"
