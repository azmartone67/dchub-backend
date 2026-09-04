"""r-burst-vs-adoption (2026-09-04) — a one-day burst is not an integration.

The public /ai hero read:

    "Claude. ChatGPT. Grok. They don't guess about data center markets —
     they call DC Hub's MCP tools."

A 30-day call COUNT cannot tell a platform that integrated from one that ran a
single test, so all three were named identically. Measured the day this
shipped, from mcp_tool_calls:

    claude-ai  742 calls / 10 active days / last call that same day
    claude     213 calls / 24 active days / last call the day before
    grok        41 calls /  1 active day  / 2026-08-30
    chatgpt     33 calls /  1 active day  / 2026-08-13

★ AND IT WAS ABOUT TO FIX ITSELF THE WRONG WAY. chatgpt's burst was EIGHT DAYS
from ageing out of the 30-day window. The sentence would then have dropped the
name silently — correcting by deletion, having been wrong the entire time it
stood, with nothing recording that it had been wrong.

Volume cannot separate these: 33 and 41 calls both landed inside one day, and
742 landed across ten. Only RECURRENCE can.

WHAT THIS GUARD PINS:
  · active_days / last_call / recurring are published per platform;
  · recurring is null — never false — when active_days was not measured, so an
    unmeasured platform is not demoted to "came once";
  · the threshold is days, not calls.
"""
import datetime as _dt
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from live_proof_platforms import _RECURRING_MIN_DAYS, shape_platforms  # noqa: E402

# The live shape on 2026-09-04: (platform, calls, gross, active_days, last_call)
_LIVE = [
    ("claude-ai", 742, 742, 10, _dt.date(2026, 9, 4)),
    ("claude", 213, 213, 24, _dt.date(2026, 9, 3)),
    ("grok", 41, 41, 1, _dt.date(2026, 8, 30)),
    ("chatgpt", 33, 33, 1, _dt.date(2026, 8, 13)),
]


def _by(rows):
    out, _ = shape_platforms(rows)
    return {r["platform"]: r for r in out}


def test_a_one_day_burst_is_not_recurring():
    g = _by(_LIVE)
    assert g["chatgpt"]["recurring"] is False, (
        "chatgpt called on ONE day (2026-08-13) and is being published as an "
        "integrating platform")
    assert g["grok"]["recurring"] is False
    assert g["claude-ai"]["recurring"] is True
    assert g["claude"]["recurring"] is True


def test_volume_cannot_buy_recurrence():
    """41 calls in one day must not outrank 3 calls across three days."""
    g = _by([("loud", 5000, 5000, 1, _dt.date(2026, 9, 4)),
             ("quiet", 3, 3, 3, _dt.date(2026, 9, 4))])
    assert g["loud"]["recurring"] is False, (
        "a single-day burst bought its way into the claim on call volume")
    assert g["quiet"]["recurring"] is True


def test_unmeasured_is_null_never_false():
    """An older payload shape must not demote every platform to 'came once'."""
    g = _by([("legacy", 500, 500)])                     # 3-column row
    assert g["legacy"]["recurring"] is None, (
        "an unmeasured platform is being published as non-recurring, which "
        "would drop a real integration out of the sentence")
    assert g["legacy"]["active_days"] is None
    assert g["legacy"]["last_call"] is None
    # …and the row still counts, exactly as before.
    assert g["legacy"]["calls"] == 500


def test_threshold_is_days_and_is_the_weakest_claim_that_means_came_back():
    assert _RECURRING_MIN_DAYS == 2, (
        "two days is the weakest claim that still means the platform RETURNED; "
        "raising it starts excluding real integrations, lowering it to 1 makes "
        "every caller recurring and restores the defect")
    g = _by([("edge", 2, 2, 2, _dt.date(2026, 9, 4))])
    assert g["edge"]["recurring"] is True


def test_last_call_is_published_as_a_date_string():
    """The basis line prints it; a date object would render as a repr."""
    g = _by(_LIVE)
    assert g["chatgpt"]["last_call"] == "2026-08-13"
    assert re.fullmatch(r"20\d\d-[01]\d-[0-3]\d", g["claude"]["last_call"])


def test_the_basis_explains_the_new_fields():
    _, exc = shape_platforms(_LIVE)
    b = exc["basis"]
    for token in ("active_days", "last_call", "recurring", "single test"):
        assert token in b, "the published basis does not explain %r" % token
    assert "null, not false" in b


def test_the_endpoint_selects_the_columns_it_publishes():
    """shape_platforms can only report what the query supplies."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "flask_mcp_endpoints.py"), encoding="utf-8").read()
    i = src.find("platforms_30d\"] = externals")
    q = src[max(0, i - 6000):i]
    assert "COUNT(DISTINCT created_at::date)" in q, (
        "active_days is not selected, so every platform publishes recurring=null")
    assert "AS last_call" in q
    # ★ Both must be filtered by the SAME self-traffic predicate as `n`, or
    # "10 active days" could be nine of ours plus one real caller's.
    a = q.find("COUNT(DISTINCT created_at::date)")
    seg = q[a:q.find("FROM mcp_calls_identity", a)]   # the FROM *after* it
    assert seg, "could not isolate the select list"
    assert seg.count("_not_self") >= 2, (
        "active_days / last_call are not filtered by the self-traffic "
        "predicate, so our own calls can manufacture recurrence")
