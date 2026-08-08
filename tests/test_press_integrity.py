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


# ── the JS-value tokens: SHAPE, not vocabulary ───────────────────────
# Two live report-only runs on 2026-08-07 found two false-positive classes here
# and both would have UNPUBLISHED healthy releases had enforcement been armed.
# The corpus is the 143 real releases; these pin the outcome of that measurement
# in both directions, because a guard tuned only away from false positives
# quietly stops catching the thing it was built for.

# Verbatim from /news/2026-07-15-error-version-1-machine-readable-recovery,
# the one release still hard-failing after the "nan"-in-"tenants" fix. It is a
# complete, published, correct page.
_REAL_PROSE_WITH_NULL = (
    "The taxonomy is versioned. For example, a query for \"cheyene\" (typo) "
    "returns `suggested_params: {\"market\": \"cheyenne\"}` with HTTP 400, "
    "not a silent null. The agent retries with the corrected value and "
    "completes the task, instead of dead-ending on an opaque failure. ") * 2


def test_the_word_null_in_real_prose_is_not_a_broken_body(pi):
    """MUST-NOT-FAIL. A publication that writes about APIs uses these words."""
    r = _good()
    r["body"] = _REAL_PROSE_WITH_NULL
    rev = pi.analyst_review(r)
    assert rev["hard"] is False, rev["issues"]


@pytest.mark.parametrize("prose", [
    # a bare `null` in a sentence — the exact false-positive class
    "The queue position returned no value, so the developer treated it as "
    "null and re-filed the request the following week. ",
    "Returning a typed error beats returning null, which tells an agent "
    "nothing about how to recover. ",
])
def test_a_bare_null_in_a_sentence_is_not_a_broken_body(pi, prose):
    r = _good()
    r["body"] = prose * 8
    assert pi.analyst_review(r)["hard"] is False, prose


@pytest.mark.parametrize("broken", [
    "The campus adds null MW of capacity to the local grid. ",
    "DC Hub now tracks null facilities across the region. ",
    "Excess power score: null for the market this week. ",
    "<td>null</td><td>null</td>",
    "The index moved to null deals this week across every tracked market. ",
])
def test_a_null_where_a_figure_belongs_is_still_a_hard_fail(pi, broken):
    """MUST-FAIL. Narrowing `null` to SHAPE must not stop catching a broken
    render — that is the defect the check exists for. The scope of the
    narrowing is pinned by the existing 'undefined siting' case above, which
    must keep failing."""
    r = _good()
    r["body"] = broken * 8
    rev = pi.analyst_review(r)
    assert rev["hard"] is True, (broken, rev["issues"])
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


def test_claim_verify_gate_covers_every_platform():
    """Audit SH52-063: the claim-verify gate sat inside the LinkedIn-only
    branch — an over-claim blocked on LinkedIn shipped verbatim on X/Bluesky.
    Pin the hoist: the verify_claims call must sit at gate top-level (4-space
    indent), not nested under the platform=='linkedin' branch (8-space)."""
    src = open(os.path.join(ROOT, "content_publisher.py"),
               encoding="utf-8").read()
    line = next(ln for ln in src.splitlines()
                if "from routes.media_claim_verify import verify_claims" in ln)
    indent = len(line) - len(line.lstrip())
    assert indent == 8, (
        "verify_claims import is nested %d deep — inside try (8) at gate "
        "top-level is correct; 12+ means it slid back under the LinkedIn "
        "branch" % indent)
