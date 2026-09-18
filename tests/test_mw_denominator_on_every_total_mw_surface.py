"""Every surface that publishes a fleet "Total MW" must publish its denominator.

#4710 fixed /markets/<slug>. Measured 2026-09-18, four more surfaces rendered
the same bare COALESCE(SUM(power_mw),0) with nothing beside it:

    routes/market_brief.py       Operational / Pipeline KPI tiles (x3 painters)
    routes/operator_brief.py     hero "Total MW" tile
    routes/operators.py          operator page "Total MW" card
    routes/hyperscaler_brief.py  "Total Announced" KPI

power_mw is NULL on 91.8% of `facilities` and 94.9% of `discovered_facilities`
(measured live 2026-09-18). SUM COALESCEs that NULL to 0, so a surface whose
rows mostly do not record capacity still renders a confident-looking number.
Austin publishes 107 MW; 4 of its 91 facilities reported anything.

These assertions anchor on the SQL and on the tile f-string, never on a bare
substring that a comment could satisfy.
"""
from __future__ import annotations

import ast
import os
import re

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (module, how many aggregates in it must carry the denominator)
_SURFACES = {
    "routes/market_brief.py":      1,
    "routes/operator_brief.py":    1,
    "routes/operators.py":         1,
    "routes/hyperscaler_brief.py": 1,
}


def _src(rel: str) -> str:
    return open(os.path.join(_REPO, rel), encoding="utf-8").read()


def _strip_sql_comments(sql: str) -> str:
    """Drop `-- ...` tails. The rationale comments added beside these
    aggregates NAME the thing being asserted, so an assertion run over raw
    text would be satisfied by the prose explaining the fix rather than by
    the fix. Measured: without this, deleting the COUNT(*) FILTER line still
    passed, because the comment above it says "COUNT(*) FILTER".
    """
    return re.sub(r"--[^\n]*", "", sql)


def _sql_literals(src: str) -> list[str]:
    """Every string literal, with an f-string returned WHOLE.

    ★ An f-string is a JoinedStr whose literal chunks are separate Constant
    nodes. Walking Constants therefore CUTS one SQL statement into pieces at
    each `{...}`, and a tail chunk carrying `SUM(power_mw)` but not the
    SELECT's denominator looks exactly like an unfixed aggregate. Measured:
    hyperscaler_brief's state split reported as a violation after it had
    already been fixed, because `WHERE {where}` splits it in two.
    """
    tree = ast.parse(src)
    inner = {id(c) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
             for c in ast.walk(n) if c is not n}
    out = []
    for n in ast.walk(tree):
        if id(n) in inner:
            continue
        if isinstance(n, ast.JoinedStr):
            out.append(ast.get_source_segment(src, n) or "")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def test_every_total_mw_aggregate_selects_its_denominator():
    for rel, want in _SURFACES.items():
        src = _src(rel)
        found = 0
        for node in _sql_literals(src):
            sql = _strip_sql_comments(node)
            if "SUM(power_mw)" not in sql:
                continue
            if "COUNT(*) FILTER (WHERE power_mw > 0)" in sql:
                found += 1
            else:
                raise AssertionError(
                    f"{rel}: an aggregate SUMs power_mw with no denominator "
                    f"beside it:\n{node[:400]}")
        assert found >= want, (
            f"{rel}: expected >={want} aggregate(s) carrying "
            f"COUNT(*) FILTER (WHERE power_mw > 0), found {found}")


def test_every_surface_renders_the_note_next_to_its_total():
    """The SQL is useless if no painter prints it. Anchor on the tile markup."""
    checks = {
        # module: (regex for the tile/value markup, fragment it must contain)
        # NOT `.*?</div>` — non-greedy stops at the FIRST </div>, which is
        # the card-metric's, i.e. BEFORE the note. That regex passed while the
        # note was absent. Anchor on the end of the whole card instead.
        "routes/operators.py": (
            r'<div class="card-label">Total MW</div>.*?</div>\}?[^\n]*', "_mw_cov_html"),
        "routes/operator_brief.py": (
            r'\("Total MW",\s+_fmt_mw\(total_mw\).*?\)\),', "_mwcov"),
        "routes/hyperscaler_brief.py": (
            r'<div class="kpi-l">Total Announced.*?</div>', "_mwcov_l"),
    }
    for rel, (pat, frag) in checks.items():
        src = _src(rel)
        m = re.search(pat, src, re.S)
        assert m, f"{rel}: could not locate the Total MW tile ({pat})"
        assert frag in m.group(0), (
            f"{rel}: the Total MW tile no longer renders its denominator:\n"
            + m.group(0))


def test_market_briefs_three_painters_all_carry_it():
    """market_brief.py paints the Operational tile three times (full brief,
    embed, SEO shell). #4710's lesson was that fixing two of three painters
    ships a page that still lies."""
    src = _src("routes/market_brief.py")
    painters = re.findall(
        r'\("Operational",\s*_fmt_mw\(kpis\.get\("operational_mw"\)\)[^\n]*\n?[^\n]*',
        src)
    painters += re.findall(
        r'kpi_tiles\.append\(\("Operational",[^\n]*\n?[^\n]*', src)
    assert len(painters) >= 3, (
        f"expected 3 Operational painters, found {len(painters)}")
    for p in painters:
        assert "_mw_cov_small" in p, f"painter without the denominator:\n{p}"


def test_the_note_is_styled_wherever_it_is_emitted():
    """A <small> dropped into an uppercase mono label renders as a shouted
    fragment; one with no rule at all inherits whatever the tile sets. Every
    template that emits the note carries a rule scoping it."""
    for rel, sel in (
        ("routes/market_brief.py",      ".kpi-v small{{"),
        ("routes/operator_brief.py",    ".kpi-v small{{"),
        ("routes/hyperscaler_brief.py", ".kpi-l small{{"),
        ("routes/operators.py",         ".card small{{"),
    ):
        src = _src(rel)
        assert sel in src, f"{rel}: no CSS rule scoping the note ({sel})"


def test_only_measured_fabricating_sources_are_refused():
    """The capacity-source predicate must stay narrow and must actually
    refuse. A blanket denial would drop the curated feeds that are the only
    real capacity the fleet has."""
    import sys
    sys.path.insert(0, _REPO)
    from util.facility_count_basis import (NON_CAPACITY_SOURCES,
                                           source_publishes_capacity)
    assert NON_CAPACITY_SOURCES == frozenset({"openstreetmap"})
    assert source_publishes_capacity("OpenStreetMap") is False
    assert source_publishes_capacity("openstreetmap") is False
    for keep in ("seed", "datacentermap", "operator_website", "news_extraction",
                 "cloudscene", None, ""):
        assert source_publishes_capacity(keep) is True, keep


def test_the_drain_carries_capacity_onto_an_existing_facility():
    """Case B of the dedup drain linked a discovered row to an existing
    facility and threw its power_mw away (535 rows, measured 2026-09-18).
    It must now fill a NULL — and only a NULL, and only from a source that
    publishes capacity."""
    src = _src("main.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_admin_dedup_drain")
    seg = ast.get_source_segment(src, fn) or ""
    body = _strip_sql_comments(seg)
    assert "source_publishes_capacity(src)" in body, (
        "the drain copies capacity without checking the source publishes any")
    assert "UPDATE facilities SET power_mw" in body, (
        "Case B still drops power_mw on link")
    m = re.search(r'"UPDATE facilities SET power_mw = %s "\s*\n?\s*"?([^"]*)"', body)
    assert m and "power_mw IS NULL" in m.group(0), (
        "the drain's UPDATE is not restricted to rows with no reading — it "
        "can overwrite a published value:\n" + (m.group(0) if m else body[:300]))
