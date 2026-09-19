"""GUARD — every permitting class the API advertises must be REACHABLE by the
news scanner, and the tax rule must match real program news without flooding.

FENCES _PERMIT_CLASSES / _PERMIT_SCAN in routes/agentic_master_shell.py.

──────────────────────────────────────────────────────────────────────────
★ THE BUG THIS FENCES (test_every_declared_class_has_a_scan_rule)

"tax" sat in _PERMIT_CLASSES, was accepted by the admin upsert, and was
advertised by /api/v1/permitting/intel and in llms.txt — with NO entry in
_PERMIT_SCAN. No article could ever be staged as one. The class read as
"empty" when it was actually UNREACHABLE, and it stayed at zero rows
through 2025-26 while nine states paused, repealed or narrowed their
data-center tax programs (OH AZ IL NJ WA NC MN NE OK).

A class that is valid but unscannable is a silent dead lane. "Zero rows"
and "no rule exists" look identical from outside and mean opposite things.

PROPERTIES, each with a MUST-FAIL CONTROL:

1. EVERY DECLARED CLASS IS REACHABLE ("other" is the documented catch-all
   for human upserts and is exempt). Control: the exemption list is asserted
   explicitly, so silently exempting a new class fails the test.
2. THE TAX RULE MATCHES REAL 2026 HEADLINES — the actual articles behind the
   nine corrections, not invented strings.
3. IT DOES NOT FLOOD. Ordinary tax news with no program noun must NOT match;
   a rule matching bare "tax" would stage every levy story.
"""
import re

import routes.agentic_master_shell as ams

_EXEMPT = {"other"}        # documented catch-all: human upsert only


def _rule(cls):
    return dict(ams._PERMIT_SCAN).get(cls)


# ── ★ 1. every declared class is reachable ──────────────────────────────────
def test_every_declared_class_has_a_scan_rule():
    """★ THE REGRESSION. 'tax' failed this for months."""
    missing = [c for c in ams._PERMIT_CLASSES
               if c not in _EXEMPT and not _rule(c)]
    assert not missing, (
        f"class(es) {missing} are advertised and accepted but no _PERMIT_SCAN "
        f"rule can ever stage one — a silent dead lane, not an empty class")


def test_the_exemption_list_is_explicit():
    """CONTROL for 1 — pins what may be exempt, so a future class cannot be
    quietly excused by widening _EXEMPT without a reviewer seeing it."""
    assert _EXEMPT == {"other"}
    assert set(ams._PERMIT_CLASSES) - _EXEMPT == {
        "moratorium", "zoning", "tax", "utility_pause"}


def test_every_rule_is_a_valid_regex():
    for cls, pat in ams._PERMIT_SCAN:
        re.compile(pat)          # raises if malformed
        assert pat.strip(), f"{cls} has an empty pattern"


# ── 2. the tax rule matches the REAL headlines ──────────────────────────────
REAL_2026 = [
    "Ohio pauses data center tax exemption amid growing debate over costs",
    "Gov. DeWine Announces Pause of Data Center Tax Exemption",
    "Assembly approves bill to eliminate $250 million in AI data center tax credits",
    "NC budget scales back data center tax break",
    "Data centers dominated 2026 session - lawmakers answered with 3-year tax incentive pause",
    "Ohio bill would end data center tax breaks, effective Oct. 1",
    "Washington Removes Sales Tax Exemptions for Data Center Equipment Replacement",
    "Nebraska governor signs executive order halting tax incentives for data center developers",
    "State, local data center tax breaks total billions for trillion-dollar tech companies",
]


def test_tax_rule_matches_the_real_articles_behind_the_nine_corrections():
    pat = _rule("tax")
    assert pat, "no tax rule"
    rx = re.compile(pat, re.I)
    missed = [h for h in REAL_2026 if not rx.search(h)]
    assert not missed, f"tax rule missed real headlines: {missed}"


# ── 3. it must not flood ────────────────────────────────────────────────────
NOT_PROGRAM_NEWS = [
    "County property tax rates rise 3% for homeowners",
    "Firm settles tax dispute with the state revenue department",
    "Tax season opens Monday",
    "Data center opens in Columbus",                  # DC news, no tax program
    "Utility files rate case for industrial customers",
]


def test_tax_rule_does_not_stage_ordinary_tax_news():
    """CONTROL for 2 — a rule on bare 'tax' would pass test 2 and fail here."""
    rx = re.compile(_rule("tax"), re.I)
    hits = [h for h in NOT_PROGRAM_NEWS if rx.search(h)]
    assert not hits, f"tax rule would flood candidates with: {hits}"


def test_bare_tax_would_have_failed_this_suite():
    """Proves test 3 has teeth: the naive rule passes 2 and fails 3."""
    naive = re.compile(r"tax", re.I)
    assert all(naive.search(h) for h in REAL_2026)
    assert [h for h in NOT_PROGRAM_NEWS if naive.search(h)], \
        "if bare 'tax' matched nothing ordinary, test 3 would be vacuous"
