"""Coverage & Expansion section on /dcpi  (r-coverage, 2026-07-29).

WHY THIS SUITE EXISTS
/dcpi-v2 was a frozen launch teaser — "275+ markets across 14+ countries" and a
hardcoded "Frankfurt (1,782 MW)" — and was deleted and 301'd to /dcpi. The
expansion story moved onto /dcpi, so the section that carries it must be
incapable of repeating that failure. What is pinned here:

  1. the market count is the CANONICAL distinct-market count, not the row
     count and not the published-slug count (306 vs 317 vs 310 live);
  2. an unmeasurable figure renders the claim WITHOUT a number — never 0,
     never "None", never a frozen literal;
  3. no MW literal and no "275+" reaches the rendered section;
  4. coverage figures are computed BEFORE the anon/free tier slice, so a
     capped teaser can never shrink a coverage claim;
  5. a footprint-query failure is LOGGED at warning and is not cached.

HOUSE RULES honoured here: nothing runs at module scope (a module-scope exit
aborts collection => exit 3 with zero tests run); routes.dcpi is NEVER imported
(it opens DB pools and imports main-adjacent modules) — the shipped code is
AST-extracted from source and executed against stubs; every extraction asserts
it PARSED and that the extracted code's FREE VARIABLES RESOLVE, because a
missing global is either a NameError or, worse, a silently untested branch.
"""

import ast
import builtins
import logging
import re
import symtable
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# extraction harness
# --------------------------------------------------------------------------
def _src_path():
    return Path(__file__).resolve().parents[1] / "routes" / "dcpi.py"


def _source():
    return _src_path().read_text(encoding="utf-8")


def _parse():
    """Parse routes/dcpi.py and PROVE the parse produced real code.

    An `ast.parse` of an empty (or truncated) file yields a Module with an
    empty body, against which every subsequent assertion passes vacuously.
    """
    tree = ast.parse(_source())
    assert isinstance(tree, ast.Module), "routes/dcpi.py did not parse to a Module"
    assert tree.body, "routes/dcpi.py parsed to an EMPTY Module body"
    return tree


def _global_names(func_name):
    """Every module-level (global) name the named function loads, including
    names used only inside its comprehensions. Uses symtable so attribute
    names (`r.get`) are not mistaken for globals."""
    top = symtable.symtable(_source(), str(_src_path()), "exec")

    def find(table):
        for child in table.get_children():
            if child.get_type() == "function" and child.get_name() == func_name:
                return child
            hit = find(child)
            if hit is not None:
                return hit
        return None

    tbl = find(top)
    assert tbl is not None, f"{func_name} has no symbol table (not defined?)"

    names = set()

    def collect(t):
        for sym in t.get_symbols():
            if sym.is_global():
                names.add(sym.get_name())
        for child in t.get_children():
            collect(child)

    collect(tbl)
    return names


def _load(names, extra_globals=None):
    """Execute ONLY the named module-level defs/assignments from
    routes/dcpi.py into a fresh namespace, and assert that every global the
    loaded functions reference actually resolves in that namespace."""
    tree = _parse()
    wanted = list(names)
    picked, found = [], []
    for node in tree.body:
        nm = None
        if isinstance(node, ast.FunctionDef):
            nm = node.name
        elif isinstance(node, ast.Assign):
            ids = [t.id for t in node.targets if isinstance(t, ast.Name)]
            nm = ids[0] if ids else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            nm = node.target.id
        if nm in wanted:
            picked.append(node)
            found.append(nm)
    missing = [n for n in wanted if n not in found]
    assert not missing, f"not found at module level in routes/dcpi.py: {missing}"

    ns = dict(extra_globals or {})
    mod = ast.Module(body=picked, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), str(_src_path()), "exec"), ns)

    resolvable = set(ns) | set(dir(builtins))
    for nm in found:
        if not isinstance(ns.get(nm), type(_load)):
            continue
        unresolved = sorted(_global_names(nm) - resolvable)
        assert not unresolved, (
            f"{nm} references globals that do not resolve in the test "
            f"namespace: {unresolved} — a missing name is a NameError in "
            f"production or a silently untested path here")
    return ns


def _func_def(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _template():
    """The real DCPI_INDEX_TEMPLATE string, literal-eval'd from source."""
    tree = _parse()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "DCPI_INDEX_TEMPLATE"
                for t in node.targets):
            tpl = ast.literal_eval(node.value)
            assert isinstance(tpl, str) and len(tpl) > 10_000, (
                "DCPI_INDEX_TEMPLATE extracted but implausibly short")
            return tpl
    raise AssertionError("DCPI_INDEX_TEMPLATE not found in routes/dcpi.py")


# --------------------------------------------------------------------------
# stub DB
# --------------------------------------------------------------------------
class _Cur:
    def __init__(self, owner, row):
        self._owner = owner
        self._row = row

    def execute(self, sql, params=None):
        self._owner.executed.append(sql)
        if self._owner.raise_on_execute:
            raise self._owner.raise_on_execute

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """Minimal psycopg2-shaped connection: `with _conn() as c, c.cursor() as cur`."""

    def __init__(self, owner):
        self._owner = owner

    def cursor(self, *a, **kw):
        return _Cur(self._owner, self._owner.row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Db:
    def __init__(self, row=(178, 15363), raise_on_connect=None,
                 raise_on_execute=None):
        self.row = row
        self.raise_on_connect = raise_on_connect
        self.raise_on_execute = raise_on_execute
        self.executed = []
        self.connects = 0

    def conn(self):
        self.connects += 1
        if self.raise_on_connect:
            raise self.raise_on_connect
        return _Conn(self)


# --------------------------------------------------------------------------
# rows fixtures — shaped like the published DISTINCT ON (market_slug) set
# --------------------------------------------------------------------------
_AGG = ("pacific-nw-rural", "rural-spp", "upper-michigan")


def _rows():
    """A published-score-set shaped list with the three real complications:
    an ALIAS TWIN pair (two slugs, one market_name), the three rural
    AGGREGATE regions, and rows whose iso never tagged."""
    r = [
        {"market_slug": "ashburn", "market_name": "Ashburn", "iso": "PJM"},
        {"market_slug": "richmond", "market_name": "Richmond", "iso": "PJM"},
        {"market_slug": "dallas", "market_name": "Dallas", "iso": "ERCOT"},
        {"market_slug": "cheyenne", "market_name": "Cheyenne", "iso": "WECC"},
        {"market_slug": "cheyenne-wy", "market_name": "Cheyenne", "iso": "WECC"},
        {"market_slug": "frankfurt", "market_name": "Frankfurt", "iso": "ENTSOE-DE"},
        {"market_slug": "mumbai", "market_name": "Mumbai", "iso": "POSOCO"},
        {"market_slug": "sydney", "market_name": "Sydney", "iso": None},
        {"market_slug": "osaka", "market_name": "Osaka", "iso": "   "},
    ]
    r += [{"market_slug": s, "market_name": s.replace("-", " ").title(), "iso": "SPP"}
          for s in _AGG]
    return r


def _expected_markets():
    """Distinct market_name over non-aggregate rows: Ashburn, Richmond, Dallas,
    Cheyenne (twins collapse), Frankfurt, Mumbai, Sydney, Osaka."""
    return 8


# ==========================================================================
# _dcpi_index_coverage — the index's own coverage figures
# ==========================================================================
def test_market_count_is_distinct_markets_not_rows():
    """The published number must be DISTINCT MARKETS, strictly fewer than the
    rows on the page — the 317-rows-published-as-306-markets defect class."""
    ns = _load(["_DCPI_AGGREGATE_REGION_SLUGS", "_dcpi_index_coverage"])
    rows = _rows()
    out = ns["_dcpi_index_coverage"](rows)
    assert out["markets"] == _expected_markets()
    assert out["markets"] < len(rows), (
        "market count equalled the row count — alias twins and/or the "
        "aggregate regions were not collapsed")


def test_alias_twins_collapse_to_one_market():
    ns = _load(["_DCPI_AGGREGATE_REGION_SLUGS", "_dcpi_index_coverage"])
    fn = ns["_dcpi_index_coverage"]
    twin = [{"market_slug": "cheyenne", "market_name": "Cheyenne", "iso": "WECC"},
            {"market_slug": "cheyenne-wy", "market_name": "Cheyenne", "iso": "WECC"}]
    assert fn(twin)["markets"] == 1


def test_aggregate_regions_are_excluded_by_exactly_three():
    """Behavioural check on the canonical exclusion: adding the three rural
    aggregate rollups must not move the market count."""
    ns = _load(["_DCPI_AGGREGATE_REGION_SLUGS", "_dcpi_index_coverage"])
    fn = ns["_dcpi_index_coverage"]
    without = [r for r in _rows() if r["market_slug"] not in _AGG]
    assert len(_rows()) - len(without) == 3
    assert fn(_rows())["markets"] == fn(without)["markets"]


def test_untagged_iso_rows_are_not_counted_as_grid_regions():
    """/api/v1/dcpi/iso-comparison drops the blank/NULL-iso bucket; this figure
    is published next to that endpoint, so it must drop it identically."""
    ns = _load(["_DCPI_AGGREGATE_REGION_SLUGS", "_dcpi_index_coverage"])
    out = ns["_dcpi_index_coverage"](_rows())
    # PJM, ERCOT, WECC, ENTSOE-DE, POSOCO, SPP — None and "   " excluded
    assert out["grid_regions"] == 6


def test_unmeasurable_coverage_is_none_never_zero():
    """A count of zero is not a fact about the index, it is a failure to
    measure — and printing 0 is exactly why /dcpi-v2 was retired."""
    ns = _load(["_DCPI_AGGREGATE_REGION_SLUGS", "_dcpi_index_coverage"])
    fn = ns["_dcpi_index_coverage"]
    for empty in ([], None, [{"market_slug": "x", "market_name": "  ", "iso": ""}]):
        out = fn(empty)
        assert out["markets"] is None, out
        assert out["grid_regions"] is None, out


# ==========================================================================
# _dcpi_footprint_figures — the footprint the market list is derived from
# ==========================================================================
def _footprint_ns(db):
    return _load(["_DCPI_FOOTPRINT_TTL_S", "_dcpi_footprint_cache",
                  "_dcpi_footprint_figures"], {"_conn": db.conn})


def test_footprint_reads_both_figures_in_one_query():
    db = _Db(row=(178, 15363))
    ns = _footprint_ns(db)
    out = ns["_dcpi_footprint_figures"]()
    assert out == {"countries": 178, "facilities_distinct": 15363}
    assert len(db.executed) == 1, (
        f"expected ONE round trip on a hot public page, got {len(db.executed)}")


def test_footprint_binds_the_canonical_definitions():
    """The two figures must be the same measurements /api/v1/stats/canonical
    publishes, or the page becomes another disagreeing surface. Asserted on
    the SQL actually sent to the driver, not on a comment."""
    db = _Db(row=(178, 15363))
    ns = _footprint_ns(db)
    ns["_dcpi_footprint_figures"]()
    sql = " ".join(db.executed[0].split())
    assert "COUNT(DISTINCT country) FROM discovered_facilities" in sql
    assert "country IS NOT NULL AND country <> ''" in sql
    assert "COUNT(DISTINCT canonical_slug) FROM discovered_facilities" in sql
    assert "canonical_slug IS NOT NULL" in sql
    # ★2026-07-30: countries must be counted on the SAME fleet the facility
    # figure counts. The legacy `facilities` table (186) mixes full names with
    # ISO codes ("USA"+"US") — pairing it with a discovered_facilities count
    # is the wrong-table class this suite exists to prevent.
    assert "FROM facilities" not in sql
    # facilities_verified / facilities_tracked are documented as AMBIGUOUS
    # (routes/facilities_by_dims.py:272-288) and must not be what we cite.
    assert "duplicate_of_id" not in sql
    assert "is_duplicate" not in sql


def test_footprint_is_cached_between_renders():
    db = _Db(row=(178, 15363))
    ns = _footprint_ns(db)
    first = ns["_dcpi_footprint_figures"]()
    second = ns["_dcpi_footprint_figures"]()
    assert first == second
    assert len(db.executed) == 1, "the cache did not hold; /dcpi is a hot page"


def test_footprint_cache_cannot_be_mutated_by_a_caller():
    db = _Db(row=(178, 15363))
    ns = _footprint_ns(db)
    got = ns["_dcpi_footprint_figures"]()
    got["countries"] = 999999
    assert ns["_dcpi_footprint_figures"]()["countries"] == 178


def test_footprint_zero_reads_as_unmeasured():
    db = _Db(row=(0, 0))
    ns = _footprint_ns(db)
    assert ns["_dcpi_footprint_figures"]() == {
        "countries": None, "facilities_distinct": None}


def test_footprint_failure_is_soft_and_visible(caplog):
    """A permanently-failing measurement must be LOUD. Silent permanent
    staleness is the failure class this codebase keeps re-learning."""
    db = _Db(raise_on_connect=RuntimeError("DATABASE_URL not set"))
    ns = _footprint_ns(db)
    with caplog.at_level(logging.WARNING):
        out = ns["_dcpi_footprint_figures"]()
    assert out == {"countries": None, "facilities_distinct": None}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a failed coverage measurement was swallowed silently"
    msg = " ".join(r.getMessage() for r in warnings)
    assert "RuntimeError" in msg, f"the exception TYPE is not in the log: {msg}"


def test_footprint_failure_is_not_cached():
    """A transient failure must not pin 'unknown' for the whole TTL."""
    db = _Db(row=(178, 15363), raise_on_execute=ValueError("boom"))
    ns = _footprint_ns(db)
    assert ns["_dcpi_footprint_figures"]()["countries"] is None
    db.raise_on_execute = None
    assert ns["_dcpi_footprint_figures"]()["countries"] == 178


# ==========================================================================
# route wiring: coverage must be computed BEFORE the tier slice
# ==========================================================================
def test_coverage_is_computed_before_the_tier_slice():
    """A capped anon/free teaser must not shrink a COVERAGE claim (the bug
    that made a 310-market index read "25 MARKETS SCORED"). Proven on the AST
    of public_dashboard: the coverage call must precede every statement that
    rebinds `rows`, so it can only ever see the full published set. Comments
    cannot satisfy this — it requires real Call and Assign nodes."""
    tree = _parse()
    fn = _func_def(tree, "public_dashboard")
    cov_lines = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_dcpi_index_coverage"]
    assert cov_lines, "_dcpi_index_coverage is not called by public_dashboard"

    rebinds = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "rows" for t in n.targets)]
    # the initial fetches are rebinds too; only the gating ones matter, and
    # those are the ones inside the `if _gated_to_anon:` branch. Take every
    # rebind that happens AFTER the coverage call and require none exist.
    later = [ln for ln in rebinds if ln > max(cov_lines)]
    assert later, "no `rows` rebinding after coverage — fixture assumption broke"
    assert min(cov_lines) < min(later), (
        "coverage is computed after `rows` was rebound; a tier-capped teaser "
        "can shrink the published coverage figure")

    # and the figures must actually reach the template
    kwargs = {
        kw.arg for n in ast.walk(fn) if isinstance(n, ast.Call)
        for kw in n.keywords if kw.arg}
    for name in ("cov_markets", "cov_grid_regions", "cov_countries",
                 "cov_facilities"):
        assert name in kwargs, f"{name} is never passed to the template"


# ==========================================================================
# the rendered section
# ==========================================================================
def _render(**figures):
    jinja2 = pytest.importorskip("jinja2")
    ctx = dict(scores=[], count=25, count_actionable=0, count_low_signal=0,
               gated_to_anon=True, total_rows=310, tier_state="anon",
               all_market_links=[{"slug": "ashburn", "name": "Ashburn"}])
    ctx.update(figures)
    return jinja2.Environment().from_string(_template()).render(**ctx)


def _section(html):
    start = html.find("Coverage &amp; Expansion")
    assert start > 0, "the Coverage & Expansion section is not in the page"
    start = html.rfind('<div class="section-h">', 0, start)
    end = html.find('<div class="section-h">', start + 10)
    assert end > start, "could not find the end of the section"
    return html[start:end]


def test_section_renders_each_bound_figure():
    s = _section(_render(cov_markets=306, cov_grid_regions=49,
                         cov_countries=178, cov_facilities=15363))
    for shown in ("306", "49", "178", "15,363"):
        assert shown in s, f"{shown} was not rendered"
    assert s.count('class="num"') == 4


def test_section_never_publishes_a_frozen_or_retired_claim():
    """No MW literal (the "Frankfurt (1,782 MW)" class) and no "275+"."""
    for figures in (
            dict(cov_markets=306, cov_grid_regions=49, cov_countries=178,
                 cov_facilities=15363),
            dict(cov_markets=None, cov_grid_regions=None, cov_countries=None,
                 cov_facilities=None)):
        s = _section(_render(**figures))
        assert "MW" not in s
        assert "275" not in s
        assert "317" not in s, "the score-table ROW count must never be published"
        assert not re.search(r"\bMegawatt", s, re.I)


def test_unmeasured_figures_drop_the_number_and_keep_the_claim():
    s = _section(_render(cov_markets=None, cov_grid_regions=None,
                         cov_countries=None, cov_facilities=None))
    assert "None" not in s
    assert "nan" not in s.lower()
    assert 'class="num"' not in s, "a tile rendered with no measured figure"
    assert 'class="stats-row"' not in s
    # the claim itself survives
    assert "US-only" in s and "isn't one any more" in s


def test_partially_measured_section_renders_only_what_is_bound():
    s = _section(_render(cov_markets=306, cov_grid_regions=None,
                         cov_countries=None, cov_facilities=15363))
    assert s.count('class="num"') == 2
    assert "306" in s and "15,363" in s
    assert "None" not in s
    # the provenance sentence for a dropped figure must go with it
    assert "iso-comparison" not in s


def test_coverage_figure_is_not_the_tier_capped_count():
    """count=25 is the anon teaser; the coverage figure must be the full
    index, so the capped number must not appear as a coverage tile."""
    s = _section(_render(cov_markets=306, cov_grid_regions=49,
                         cov_countries=178, cov_facilities=15363, count=25))
    assert '<div class="num">25</div>' not in s
    assert '<div class="num">306</div>' in s


def test_section_reuses_the_pages_existing_visual_language():
    """The owner's ask was "match the look" — the section must use the page's
    own .section-h / .stats-row / .stat classes rather than a new style
    system, and must not introduce new CSS."""
    s = _section(_render(cov_markets=306, cov_grid_regions=49,
                         cov_countries=178, cov_facilities=15363))
    assert '<div class="section-h"><span class="pip"></span>' in s
    assert 'class="stats-row"' in s
    assert s.count('<div class="stat">') == 4
    assert "<style" not in s and "@media" not in s


# ==========================================================================
# MUST-FAIL control — proves this file's tests actually RAN.
# A conftest-level abort yields rc 0 with zero tests collected, which is
# indistinguishable from success without a control that must report xfail.
# ==========================================================================
@pytest.mark.xfail(strict=True, reason="control: must report xfailed, proving "
                                       "this module was collected and executed")
def test_must_fail_control():
    assert False, "this assertion is supposed to fail"
