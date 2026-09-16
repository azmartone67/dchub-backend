"""LANE 3's evidence test counted an address the page refuses to print.

r-facility-facts (2026-09-16), the half PR #4624 did not reach. That change
gave the stored address a single owner — `util.facility_facts.street_address`
— and routed the three surfaces that PRINT one through it: the facts line
under the <h1>, the Address stat tile, and the Place JSON-LD's streetAddress.
routes/facility_profile_page builds ONE `_street` and hands it to all three:

    _fleet  = _is_fleet_row(power)
    _street = "" if _fleet else _street_address(address)

`util.thin_content.evidence` was not one of them, and it is not a surface — it
is the VERDICT about the surfaces. It asked `_has(fac["address"])`: "is there a
value", which is the question the three stopped asking on 2026-09-15.

★★★ THE POPULATION, from #4624's own measurement (3,000 live pages sampled
from the three facility sitemaps, 2026-09-15 — not re-derived here, and not
extrapolated into a count this file asserts):

    streetAddress stored      351 (11.7%)
      published by the rule   127  (4.2%)   "1950 N Stemmons Fwy",
                                            "Stekkenbergweg", "Calle 31"
      refused                 224           "India", "GB", "Chicago",
                                            "Mumbai, India", "Australia on",
                                            "Japan to", "MW", "it has"

So from the moment #4624 went live a page could be held `index, follow` by an
address string that no surface on it prints — Google fetching a page whose
whole content is Status + Country, which is precisely what LANE 3 exists to
noindex.

★ WHAT THIS FILE DOES NOT ASSERT. Not a delta count. The exact number of slugs
  that change verdict is a property of the corpus on the day, and the daily
  `keep_rule_dryrun` job (be#4634, .github/workflows/sitemap-selfcanon-daily)
  already reports `thin (is_contentless)` against the live artefact — it read 0
  published before this change and will read the real number after. A test that
  pinned a number would fail on a day when nothing is wrong.

★ WHAT IS PINNED: the INVARIANT. The verdict and the surfaces ask the same
  question, through the same function, and no page carrying any other fact
  leaves the index.
"""
import ast
import importlib
import pathlib
import re

import pytest

from util.facility_facts import is_fleet_row, street_address
from util.thin_content import (_has, contentless_slug_set, evidence,
                               is_contentless)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A row with nothing else to say: the class where the address decides the page.
BARE = {"city": None, "address": None, "latitude": None, "longitude": None,
        "power_mw": None, "status": "Operational", "country": "IN"}

# Every refused shape #4624 measured, plus the placeholder families.
REFUSED = ["India", "GB", "Chicago", "Mumbai, India", "Australia on",
           "Japan to", "MW", "it has", "near the Bath Road", "Unknown",
           "Unknown Street", "none", "n/a", "TBD", "100 MW campus",
           "300 jobs created", "Data Hall 3"]
# Shapes the rule publishes: a house number, or a named street, or both.
PUBLISHED = ["1950 N Stemmons Fwy", "Stekkenbergweg", "Calle 31",
             "22262 Cloud Plaza, Sterling, VA, 20166",
             "3 George-Boole-Weg, Heidelberg, 69124, DE",
             "601 NORTHWEST AVE", "Gardeners Road", "Sheppard Street"]


class TestTheVerdictAsksThePredicate:
    """`address` means "an address this page will print", not "a column with
    something in it"."""

    @pytest.mark.parametrize("addr", REFUSED)
    def test_a_refused_address_is_not_evidence(self, addr):
        assert evidence(dict(BARE, address=addr))["address"] is False

    @pytest.mark.parametrize("addr", PUBLISHED)
    def test_a_published_address_is_still_evidence(self, addr):
        assert evidence(dict(BARE, address=addr))["address"] is True

    @pytest.mark.parametrize("addr", REFUSED)
    def test_the_page_whose_only_fact_is_a_refused_address_is_contentless(
            self, addr):
        """The whole point: LANE 3's verdict now matches the rendered page."""
        assert is_contentless(dict(BARE, address=addr)) is True

    @pytest.mark.parametrize("addr", PUBLISHED)
    def test_a_page_with_a_real_street_keeps_its_index_slot(self, addr):
        assert is_contentless(dict(BARE, address=addr)) is False


class TestTheFleetGateIsPartOfTheRule:
    """The renderer suppresses EVERY fact on a fleet-sized row, the address
    included. A verdict that asked street_address alone would keep such a page
    indexable on an address the page itself refuses to show."""

    def test_a_fleet_row_with_a_real_street_prints_nothing_and_is_contentless(
            self):
        fac = dict(BARE, address="1950 N Stemmons Fwy", power_mw=63000.0)
        assert is_fleet_row(fac["power_mw"]) is True
        assert evidence(fac)["address"] is False
        assert is_contentless(fac) is True

    def test_the_same_address_under_the_cap_is_untouched(self):
        fac = dict(BARE, address="1950 N Stemmons Fwy", power_mw=350.0)
        assert evidence(fac)["address"] is True
        assert is_contentless(fac) is False

    def test_a_fleet_row_still_keeps_its_page_on_any_other_fact(self):
        """The fleet gate must not de-index a row that has real geo or a real
        city — `power` was already False for these before this change."""
        fleet = dict(BARE, address="1950 N Stemmons Fwy", power_mw=63000.0)
        assert is_contentless(dict(fleet, city="Columbus")) is False
        assert is_contentless(dict(fleet, latitude=39.96,
                                   longitude=-83.0)) is False


class TestNothingWithRealContentLeaves:
    """A page dropping out that still has content is a regression, not a win.
    One refused address plus ANY other fact keeps the page."""

    @pytest.mark.parametrize("extra", [
        {"city": "Mumbai"},
        {"latitude": 19.07, "longitude": 72.87},
        {"power_mw": 350.0},
    ])
    def test_any_other_fact_outranks_a_refused_address(self, extra):
        assert is_contentless(dict(BARE, address="India", **extra)) is False

    def test_the_other_three_evidence_fields_are_untouched(self):
        """Only `address` moved. A change that also tightened city, power or
        coordinates would de-index pages this measurement never looked at."""
        rich = {"city": "Columbus", "address": "India",
                "latitude": 39.96, "longitude": -83.0, "power_mw": 350.0}
        assert evidence(rich) == {"power": True, "coords": True,
                                  "address": False, "city": True}

    def test_the_new_predicate_can_only_REMOVE_evidence_never_ADD_it(self):
        """★ Why "no page loses a noindex it has today" is STRUCTURAL.

        `_has` is False for exactly five inputs — None, '', '0', '0.0' and
        'None' after strip — and street_address returns '' for every one of
        them too. So the new predicate is a strict SUBSET of the old:
        evidence['address'] can flip True -> False and never False -> True, and
        no page outside the refused class can change verdict on this field."""
        for v in (None, "", " ", "0", "0.0", "None", 0, 0.0):
            assert _has(v) is False or not street_address(v), v
            assert not street_address(v), v
            assert evidence(dict(BARE, address=v))["address"] is False, v

    def test_the_verdict_and_the_printed_address_cannot_disagree(self):
        """THE COHERENCE INVARIANT, checked across the boundary: evidence
        counts an address exactly when a surface prints one. street_address is
        the independent witness — it reached the rule first, for the renderer,
        and by a different route."""
        for v in REFUSED + PUBLISHED + [None, "", 0, True, False, 7,
                                        "x" * 400, "Данные 12"]:
            for mw in (None, 350.0, 63000.0):
                fac = dict(BARE, address=v, power_mw=mw)
                printed = "" if is_fleet_row(mw) else street_address(v)
                assert evidence(fac)["address"] is bool(printed), (v, mw)


class TestTheRuleHasExactlyOneSpelling:
    """The original defect was a second copy of the question. evidence must
    ASK the owner, never re-derive what an address looks like."""

    @staticmethod
    def _evidence_src() -> str:
        tree = ast.parse((ROOT / "util" / "thin_content.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "evidence":
                body = [n for n in node.body
                        if not (isinstance(n, ast.Expr)
                                and isinstance(n.value, ast.Constant))]
                return "\n".join(ast.unparse(n) for n in body)
        raise AssertionError("evidence() not found in util/thin_content.py")

    def test_evidence_asks_the_predicates(self):
        src = self._evidence_src()
        assert "street_address" in src
        assert "is_fleet_row" in src

    def test_address_is_no_longer_a_bare_presence_check(self):
        """`_has` answers 'is there a value'. The three printing surfaces
        stopped asking that; the verdict about them must stop too."""
        src = self._evidence_src()
        assert "_has(fac.get('address'))" not in src
        assert '_has(fac.get("address"))' not in src

    def test_evidence_does_not_re_spell_what_a_street_looks_like(self):
        src = self._evidence_src()
        for token in ("_STREET_LAST", "_STREET_SUFFIX", "_STREET_CJK",
                      "_NUMBER", "_PROSE", "housenumber", "addr:"):
            assert token not in src, token

    def test_the_renderer_still_builds_one_street_behind_the_fleet_gate(self):
        """The invariant above is only meaningful while the page's `_street`
        IS `"" if fleet else street_address(address)`. If that line is
        rewritten, this test is the one that says so."""
        src = (ROOT / "routes" / "facility_profile_page.py").read_text()
        assert "_street = '' if _fleet else _street_address(address)" in src \
            or '_street = "" if _fleet else _street_address(address)' in src
        assert "_fleet = _is_fleet_row(power)" in src


class _Cursor:
    """The two-column-family cursor contentless_slug_set executes against:
    (canonical_slug, city, address, latitude, longitude, power_mw)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return None

    def fetchall(self):
        return list(self._rows)


def _row(slug, *, city=None, address=None, lat=None, lon=None, mw=None):
    return (slug, city, address, lat, lon, mw)


class TestTheSitemapDelta:
    """contentless_slug_set is what the sitemap builder and the page's robots
    tag both read, so this is where the delta is actually spent."""

    def test_a_refused_address_joins_the_noindex_set(self):
        rows = [_row("junk-addr", address="India")]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(40)]
        assert "junk-addr" in contentless_slug_set(_Cursor(rows))

    def test_a_real_street_keeps_its_slug_out_of_the_set(self):
        rows = [_row("street", address="1950 N Stemmons Fwy")]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(40)]
        assert "street" not in contentless_slug_set(_Cursor(rows))

    def test_a_slug_kept_alive_by_a_SIBLING_row_is_not_dropped(self):
        """One URL, several rows: the richest row serves the page, and that
        page is not noindexed."""
        rows = [_row("twin", address="India"), _row("twin", city="Columbus")]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(40)]
        assert "twin" not in contentless_slug_set(_Cursor(rows))

    def test_the_refusal_floor_still_refuses_an_implausible_result(self):
        """Returning an empty set means 'emit everything'. A corpus where the
        evidence columns went missing must trip it, unchanged by this work."""
        rows = [_row("gone-%d" % i, address="India") for i in range(40)]
        assert contentless_slug_set(_Cursor(rows)) == set()

    def test_the_floor_is_not_tripped_by_this_change(self):
        """The floor refuses above 25%. #4624 measured 224 refused addresses
        in 3,000 pages (7.5%), and only those with no other fact change
        verdict, so the real delta is far under the floor — but a corpus that
        DID cross it must still emit everything rather than mass-noindex."""
        rows = [_row("thin-%d" % i, address="India") for i in range(30)]
        rows += [_row("real-%d" % i, city="Columbus") for i in range(970)]
        assert len(contentless_slug_set(_Cursor(rows))) == 30

    # ── the QUERY, not just the verdict ─────────────────────────────────────
    # ★★★ WHY THESE TWO EXIST. Everything above drives contentless_slug_set
    # through _Cursor, which IGNORES the query text — it answers fetchall()
    # with whatever rows the test handed it. So the SQL is unexercised, and two
    # edits to it pass this entire file. Both were applied to #4646 as merged
    # and its suite stayed GREEN at exit 0:
    #
    #   the address test pushed down into SQL   (`AND address IS NOT NULL`)
    #   `address` dropped from ONE union branch (`city, NULL, latitude`)
    #
    # The first is the defect #4646 fixed, re-introduced one layer lower:
    # street_address CANNOT RUN in SQL, so the sitemap side would silently go
    # back to "is there a value" while the page's robots tag stayed correct —
    # the exact split that left 70 noindexed URLs in both sitemaps. The second
    # reads as "no row has an address", which de-indexes real pages.

    def _sql(self):
        """contentless_slug_set's own query text, lowercased.

        ★ THE DOCSTRING IS DROPPED BY AST NODE, not by a regex over the source.
          That docstring has to describe what the query does — "it selects the
          five evidence columns" — and `selects` contains `select`, so a
          text-level version of this helper swallows the prose and reports that
          a UNION branch stopped fetching canonical_slug. It did exactly that
          before this form. Prose that explains a query is not the query."""
        src = (ROOT / "util" / "thin_content.py").read_text(encoding="utf-8")
        fn = [n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == "contentless_slug_set"]
        assert fn, "contentless_slug_set is gone or was renamed"
        body = list(fn[0].body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        assert body, "contentless_slug_set has a docstring and no code"
        sql = " ".join(
            n.value.lower() for stmt in body for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "select" in n.value.lower())
        assert sql, "contentless_slug_set no longer carries its own SQL"
        return sql

    def test_the_address_test_is_NOT_pushed_down_into_SQL(self):
        """Fetching the column is REQUIRED — is_contentless needs it. Naming it
        in a WHERE clause is the defect, because that is a second spelling of
        the rule in a language street_address cannot reach."""
        sql = self._sql()
        assert "address" in sql, (
            "the query stopped fetching `address`, so is_contentless cannot "
            "see it and every row reads as address-less")
        # A WHERE clause ENDS at the next clause keyword. `split("where", 1)[1]`
        # swallows the SECOND union branch's SELECT list, which names `address`
        # legitimately, and goes red on correct code — it did, on the first
        # draft of this guard.
        clauses = [re.split(r"\b(?:union|order by|group by|having|limit)\b",
                            part, maxsplit=1)[0]
                   for part in sql.split("where")[1:]]
        assert clauses, "the query lost its WHERE clause entirely"
        for where in clauses:
            assert "address" not in where, (
                "the address test moved into SQL, where street_address cannot "
                "run: %r" % (where,))

    def test_every_UNION_BRANCH_still_fetches_every_evidence_column(self):
        """is_contentless reads five columns off each row; one missing from a
        SELECT reads as absent evidence and de-indexes real pages.

        ★ PER BRANCH, not over the concatenated string. The query is two
          SELECTs joined by UNION ALL, and replacing `address` with `NULL` in
          the discovered_facilities branch alone leaves `address` present in
          the other — the whole-string form of this assertion stays green on
          that mutation."""
        branches = [b for b in self._sql().split("union all") if "select" in b]
        assert len(branches) == 2, (
            "expected the two column families, got %d branch(es)"
            % len(branches))
        for b in branches:
            cols = b.split("from", 1)[0]
            for col in ("canonical_slug", "city", "address", "latitude",
                        "longitude", "power_mw"):
                assert col in cols, (
                    "a UNION branch stopped fetching %s: %r" % (col, cols))


@pytest.mark.parametrize("mod,name", [
    ("util.facility_facts", "street_address"),
    ("util.facility_facts", "is_fleet_row"),
    ("util.thin_content", "evidence"),
    ("util.thin_content", "is_contentless"),
])
def test_the_predicate_is_importable_where_the_callers_look(mod, name):
    """★ evidence imports these at CALL TIME and must keep doing so:
    util.facility_facts imports util.thin_content at MODULE level
    (is_placeholder_city), so a module-level import here closes the cycle. A
    NameError there surfaces as a request-time 500 on every facility page."""
    assert callable(getattr(importlib.import_module(mod), name))


def test_the_import_stays_function_level_so_the_cycle_stays_open():
    """Pins the reason, not just the effect: importing util.thin_content on a
    cold interpreter must not require util.facility_facts to be importable
    first."""
    tree = ast.parse((ROOT / "util" / "thin_content.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            if "facility_facts" in mod:
                assert node.col_offset > 0, (
                    "util.facility_facts must be imported INSIDE a function — "
                    "it imports util.thin_content at module level")
