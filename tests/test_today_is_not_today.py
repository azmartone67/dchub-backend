"""`total_requests_today` is a 7-day average, and wow_pct must not divide by a gap.

★ TWO PUBLISHED NUMBERS, BOTH MEANING SOMETHING OTHER THAN THEIR NAME.

1. `total_requests_today` is `round(total_7d / 7)` — a 7-day DAILY AVERAGE.
   Measured 2026-09-07 it read 8,756 = 61,293 / 7, and moved +40 while
   all-time moved +278 over the same 3.5 hours. An average barely responds to
   a few hundred requests, which is what made it look like a frozen counter.

2. `wow_pct` published 2469.9 — 61,293 this week against 2,385 prior. The
   30-day total was 115,141 with 61,293 in the last 7, leaving 53,848 across
   the other 23 days. A 341/day week in the middle of a 3,200/day month is
   missing rows, not a collapse. ai_daily_stats is a counter table: a day with
   no row reads identically to a day with no traffic.

ast-bound against the shipped source; main.py is never imported (DB pools,
~200 blueprints, keepalive threads).
"""
import ast
import pathlib
import re

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


def _src() -> str:
    return MAIN.read_text(encoding="utf-8")


def _payload_dict():
    """The response dict literal that carries total_requests_today."""
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "total_requests_today" in keys and "wow_pct" in keys:
                return node, keys
    raise AssertionError("tracking payload dict not found")


def test_the_average_is_published_under_a_name_that_says_average():
    _node, keys = _payload_dict()
    assert "requests_daily_avg_7d" in keys, (
        "the 7-day average is published only as `total_requests_today`; a "
        "reader has no field that says what the number actually is")


def test_the_misleading_key_declares_what_it_really_holds():
    _node, keys = _payload_dict()
    assert "total_requests_today_is_actually" in keys, (
        "`total_requests_today` is kept for the existing contract, so the "
        "payload must say in-band that it is not today's count")


def test_the_average_and_its_honest_alias_are_the_same_expression():
    """If they ever diverge, one of them is lying."""
    node, _keys = _payload_dict()
    vals = {}
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value in (
                "total_requests_today", "requests_daily_avg_7d"):
            vals[k.value] = ast.dump(v)
    assert len(vals) == 2, "one of the two keys is missing"
    assert vals["total_requests_today"] == vals["requests_daily_avg_7d"], (
        "the aliased average is computed differently from the field it "
        "explains — they must be the same expression")


def test_wow_requires_a_covered_prior_window():
    src = _src()
    assert "_prior_day_count" in src, (
        "wow_pct no longer counts how many days the prior window actually has")
    assert re.search(r"if\s+_prior_day_count\s*<\s*7\s*:", src), (
        "no guard refusing to compute wow_pct when the prior window is not "
        "fully covered — a gap divides into four-figure growth")


def test_wow_publishes_a_reason_instead_of_a_silent_null():
    _node, keys = _payload_dict()
    assert "wow_unavailable_reason" in keys, (
        "wow_pct can now be null, but nothing says why — a null with no "
        "reason reads as a bug rather than a refusal to divide by a gap")


def test_the_gap_branch_sets_wow_to_none_not_a_number():
    """Bind the assignment, don't grep for the word None near the guard."""
    tree = ast.parse(_src())
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = ast.dump(node.test)
        if "_prior_day_count" not in t:
            continue
        for sub in node.body:
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if getattr(tgt, "id", "") == "wow_pct":
                        assert isinstance(sub.value, ast.Constant) and sub.value.value is None, (
                            "the uncovered-window branch assigns wow_pct "
                            "something other than None")
                        found = True
    assert found, "no wow_pct = None inside the prior-window guard"
