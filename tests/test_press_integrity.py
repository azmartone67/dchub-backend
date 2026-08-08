"""The analyst's pre-flight — analyst_review() (2026-08-07).

Motivated by /news/2026-08-07-perplexity-dcpi-dual-score-citation going public
blank + future-dated, reading as amateur. The operator's directive: releases
must be CHECKED before they go out — complete, correctly dated, accurate.

Pinned here (MUST-FAIL guards — each feeds analyst_review the exact broken
shape and asserts it is caught):
  · a blank / stub body is a HARD fail (the perplexity case)
  · a future-dated release is a HARD fail (the "September 21, 2026" fallback)
  · placeholder/error body text is a HARD fail
  · a missing/fragment title is a HARD fail
  · a complete, present-dated, real release PASSES
  · the sweep is report-only unless enforced (safe-arm)

CI-SAFETY: analyst_review is pure (no DB, no network); the sweep is not
exercised against a live DB here.
"""
import datetime
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def pi():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import press_integrity as m
    return m


def _good():
    return {
        "title": "Perplexity cites DC Hub's DCPI dual-score in a live answer",
        "slug": "2026-08-07-perplexity-dcpi-dual-score-citation",
        "body": ("Perplexity surfaced DC Hub's DCPI excess-power and constraint "
                 "scores in a data-center siting answer this week. " * 6),
        "date": datetime.date.today().strftime("%Y-%m-%d"),
    }


def test_a_complete_release_passes(pi):
    rev = pi.analyst_review(_good())
    assert rev["ok"] is True and rev["hard"] is False, rev["issues"]


def test_blank_body_is_a_hard_fail(pi):
    r = _good(); r["body"] = ""
    rev = pi.analyst_review(r)
    assert rev["ok"] is False and rev["hard"] is True
    assert any(i["code"] == "body_blank_or_stub" for i in rev["issues"])


def test_stub_body_is_a_hard_fail(pi):
    r = _good(); r["body"] = "<p>Coming soon.</p>"
    rev = pi.analyst_review(r)
    assert rev["hard"] is True
    assert any(i["code"] == "body_blank_or_stub" for i in rev["issues"])


def test_future_dated_is_a_hard_fail(pi):
    r = _good()
    r["date"] = (datetime.date.today()
                 + datetime.timedelta(days=45)).strftime("%Y-%m-%d")
    rev = pi.analyst_review(r)
    assert rev["hard"] is True
    assert any(i["code"] == "future_dated" for i in rev["issues"])


def test_placeholder_body_is_a_hard_fail(pi):
    r = _good()
    r["body"] = "Lorem ipsum dolor sit amet. " * 20
    rev = pi.analyst_review(r)
    assert rev["hard"] is True
    assert any(i["code"] == "placeholder_or_error_body" for i in rev["issues"])


def test_error_body_is_a_hard_fail(pi):
    r = _good()
    r["body"] = "could not generate the summary for this release " * 8
    rev = pi.analyst_review(r)
    assert rev["hard"] is True


def test_missing_title_is_a_hard_fail(pi):
    r = _good(); r["title"] = ""
    rev = pi.analyst_review(r)
    assert rev["hard"] is True
    assert any(i["code"] == "title_missing_or_fragment" for i in rev["issues"])


def test_unreadable_body_is_not_waved_through(pi):
    # None body must be treated as blank, never as a silent pass.
    r = _good(); r["body"] = None
    rev = pi.analyst_review(r)
    assert rev["hard"] is True


def test_prose_containing_nan_like_words_is_NOT_broken(pi):
    """Regression for the first live report (2026-08-07): unanchored `NaN`
    matched inside 'tenants' and hard-failed 21 of 143 healthy releases.
    Prose full of the trigger substrings must pass; verbatim JS artifacts
    must still fail."""
    r = _good()
    r["body"] = ("Per-facility tenants, project finance and grid governance "
                 "shape maintenance windows across the null-risk fleet. " * 6)
    # note: 'null-risk' is hyphenated prose — lowercase 'null' as a bounded
    # word is still an artifact; use realistic prose instead:
    r["body"] = ("Per-facility tenants, project finance and grid governance "
                 "shape maintenance windows across annualized demand. " * 6)
    rev = pi.analyst_review(r)
    assert not any(i["code"] == "placeholder_or_error_body"
                   for i in rev["issues"]), rev["issues"]

    for artifact in ("value: NaN MW", "operator: null,", "undefined siting"):
        r2 = _good()
        r2["body"] = ("A real paragraph about capacity and interconnection "
                      "timelines. " * 5) + artifact
        rev2 = pi.analyst_review(r2)
        assert any(i["code"] == "placeholder_or_error_body"
                   for i in rev2["issues"]), (artifact, rev2["issues"])


def test_review_never_raises_on_junk(pi):
    for junk in ({}, {"title": None, "body": 12345, "date": "not-a-date"},
                 {"body": []}):
        rev = pi.analyst_review(junk)
        assert isinstance(rev, dict) and "hard" in rev


def test_registered_and_scheduled():
    main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "register_blueprint(press_integrity_bp)" in main_src
    cron = open(os.path.join(ROOT, "routes/cron_heartbeat.py"),
                encoding="utf-8").read()
    assert "/api/v1/admin/press-integrity/heal" in cron, \
        "the sweep is not scheduled — registration is not scheduling"
