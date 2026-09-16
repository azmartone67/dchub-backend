#!/usr/bin/env python3
"""tests/test_csp_violation_detector.py — a CSP finding may not recommend a
remedy that cannot apply.

THE DEFECT, AS MEASURED (GET /api/v1/brain/innovation/digest, 2026-09-15):
check_csp_violation_reports built its remediation hint unconditionally —

    f"Likely an allowlist gap — add `{blocked}` to the `{directive}` "
    f"directive in dchub-frontend/_headers and redeploy."

— for every blocked URI it saw, having first reduced that URI to a bare HOST
and thrown away the document that reported it. So it advised the same edit
whether the block was cross-origin (where the edit is right), same-origin
(where `'self'` already permits it and the edit is a NO-OP), or a source
keyword like 'inline' (which is not a host and cannot go in an allowlist).

Measured the same day on origin/main dchub-frontend/_headers and on the live
header dchub.cloud serves: `script-src-elem 'self' …`, `connect-src 'self' …`.

It did not stay cosmetic. Three awaiting_decision board rows each carried a
~48s investigation asserting _headers content the investigator had never been
shown, because title + finding_key were all it got and both said "allowlist
gap":

    inv #100362  csp://script-src-elem/appassets.androidplatform.net (08-24)
    inv #100661  csp://script-src-elem/dchub.cloud                   (09-15)
    inv #100662  csp://connect-src/dchub.cloud                       (09-15)

#100661 and #100662 were refuted for exactly that. The investigator was
repeating the detector — the invented-path shape of
tests/test_radar_no_invented_paths.py, one directory over.

What is proved here:
  · the same-origin case can no longer produce the _headers remedy;
  · a source keyword is never described as something to add to an allowlist;
  · the cross-origin case KEEPS the remedy, so the fix did not just mute the
    detector;
  · ★ THE GENERAL GUARD: the document the verdict turns on reaches the
    finding KEY, not only the detail — routes/squasher_queue.
    investigation_question() builds the investigator's prompt from title +
    finding_key alone, so a correction left in `detail` would change nothing
    about the loop that produced those three rows;
  · recent_blocked_uris carries the evidence the verdict needs, additively.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_csp_violation_detector.py -v
"""
from __future__ import annotations

import time

import pytest

from routes import brain_consistency_radar as r

# The remedy sentence the defect emitted for everything. Pinned verbatim so
# it cannot come back wearing a different f-string.
_HEADERS_EDIT = "dchub-frontend/_headers"
_ADD_TO = "add `"


@pytest.fixture
def store(monkeypatch):
    """The REAL in-process store, emptied, so the detector under test runs the
    real recent_blocked_uris() rather than a stub that agrees with it."""
    import csp_report
    from collections import deque
    events: deque = deque(maxlen=5000)
    monkeypatch.setattr(csp_report, "_recent_events", events)

    def add(directive: str, blocked: str, document: str, n: int = 3,
            age_s: float = 10.0):
        now = time.time()
        for i in range(n):
            events.append((now - age_s - i, directive, blocked, document))
    return add


def _one(findings):
    assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
    return findings[0]


# ── the three board rows, replayed ────────────────────────────────────────

def test_the_two_live_same_origin_rows_no_longer_advise_the_headers_edit(store):
    """inv #100661 and #100662. dchub.cloud blocked on a dchub.cloud page:
    `'self'` already covers it, so the edit the old detector recommended
    changes nothing."""
    for directive in ("script-src-elem", "connect-src"):
        store(directive, "dchub.cloud", "https://dchub.cloud/ai", n=4)
        f = _one(r.check_csp_violation_reports())
        assert f["issue"] == "csp_violation_same_origin", f
        assert _ADD_TO not in f["detail"], (
            "the allowlist-top-up remedy survived for a same-origin block:\n  "
            + f["detail"])
        assert "changes NOTHING" in f["detail"]
        import csp_report
        csp_report._recent_events.clear()


def test_the_android_webview_row_keeps_the_remedy_but_warns_who_it_grants(store):
    """inv #100362. appassets.androidplatform.net IS cross-origin to the page,
    so the allowlist edit is the real candidate — the finding must still say
    so, and must also say that allowlisting an injected host grants a third
    party the rights of that directive."""
    store("script-src-elem", "appassets.androidplatform.net",
          "https://dchub.cloud/", n=5)
    f = _one(r.check_csp_violation_reports())
    assert f["issue"] == "csp_violation_recurring"
    assert _HEADERS_EDIT in f["detail"], "the fix must not simply mute the detector"
    assert "ONLY if" in f["detail"]
    assert "appassets.androidplatform.net" in f["detail"]


# ── ★ the general guard ───────────────────────────────────────────────────

_CASES = [
    # (directive, blocked, document, expected issue)
    ("connect-src",     "dchub.cloud",   "https://dchub.cloud/ai",   "csp_violation_same_origin"),
    ("script-src-elem", "dchub.cloud",   "https://dchub.cloud/",     "csp_violation_same_origin"),
    ("script-src-elem", "cdn.evil.test", "https://dchub.cloud/",     "csp_violation_recurring"),
    ("script-src-elem", "inline",        "https://dchub.cloud/",     "csp_violation_inline_or_eval"),
    ("script-src",      "eval",          "https://dchub.cloud/",     "csp_violation_inline_or_eval"),
    ("img-src",         "data",          "https://dchub.cloud/map",  "csp_violation_inline_or_eval"),
    ("worker-src",      "blob",          "https://dchub.cloud/map",  "csp_violation_inline_or_eval"),
    ("connect-src",     "api.other.test", "",                        "csp_violation_unattributed"),
]


@pytest.mark.parametrize("directive,blocked,document,expected", _CASES)
def test_the_verdict_matches_the_origin_relation(directive, blocked, document,
                                                  expected):
    issue, remedy = r.csp_violation_verdict(blocked, directive, document)
    assert issue == expected, f"{blocked!r} on {document!r} -> {issue}\n{remedy}"


@pytest.mark.parametrize("directive,blocked,document,expected", _CASES)
def test_only_a_cross_origin_block_may_recommend_an_allowlist_edit(
        directive, blocked, document, expected):
    """★ The general guard. Walks the rendered sentence, not the classifier.

    The _headers allowlist edit is a correct remedy in exactly one of these
    four situations. Any other case emitting it is the original defect."""
    _, remedy = r.csp_violation_verdict(blocked, directive, document)
    recommends_edit = _ADD_TO in remedy and _HEADERS_EDIT in remedy
    if expected == "csp_violation_recurring":
        assert recommends_edit, (
            "the cross-origin case lost its remedy — this detector must still "
            "name the fix it can name:\n  " + remedy)
    else:
        assert not recommends_edit, (
            "%s (%s on %s) still recommends the allowlist edit:\n  %s"
            % (expected, blocked, document, remedy))


@pytest.mark.parametrize("directive,blocked,document,expected", _CASES)
def test_the_document_reaches_the_finding_key_not_only_the_detail(
        store, directive, blocked, document, expected):
    """★ The mechanism, pinned. routes/squasher_queue.investigation_question()
    prompts the investigator with title + finding_key ONLY. If the document
    stays in `detail`, the investigator sees the same string it saw when it
    filed #100661/#100662 and nothing about the loop has changed."""
    import csp_report
    csp_report._recent_events.clear()
    store(directive, blocked, document, n=3)
    f = _one(r.check_csp_violation_reports())
    assert f["issue"] == expected
    if document:
        host = r._csp_host(document)
        # ★ Assert the EXPLICIT suffix, not `host in url`. For the same-origin
        #   rows the blocked host and the document host are the same string,
        #   so a substring test is satisfied by `csp://connect-src/dchub.cloud`
        #   alone and passes with the document still missing — measured, when
        #   mutation M2 (drop the suffix) left those two params green.
        assert host, "fixture bug: %r has no host" % document
        assert f["url"].endswith(" on %s" % host), (
            "the page the verdict turns on is absent from the finding key %r "
            "— the investigator will never see it" % f["url"])
    else:
        assert " on " not in f["url"], (
            "a document was invented for a report that carried none: %r"
            % f["url"])
    assert f["url"].startswith("csp://%s/" % directive)


# ── the count is a report count, and says so ──────────────────────────────

def test_the_detail_does_not_call_a_deduplicated_report_count_a_violation_count(store):
    """csp_report() drops an identical (directive, resource, page) within 60s
    BEFORE appending, so `count` counts surviving reports. The old detail said
    'blocked X {count}× in the last 24h', which is a different measurement."""
    store("connect-src", "cdn.other.test", "https://dchub.cloud/", n=4)
    f = _one(r.check_csp_violation_reports())
    assert "deduplicated reports" in f["detail"]
    assert "not a violation count" in f["detail"]
    assert "%d×" % f["count"] not in f["detail"], (
        "the detail is back to rendering the report count as a violation count")


def test_below_threshold_is_still_silent(store):
    store("connect-src", "cdn.other.test", "https://dchub.cloud/", n=2)
    assert r.check_csp_violation_reports() == []


# ── the store carries the evidence, additively ────────────────────────────

def test_recent_blocked_uris_carries_the_document_and_a_full_uri(store):
    from csp_report import recent_blocked_uris
    store("script-src-elem", "https://dchub.cloud/static/gating.js?v=3",
          "https://dchub.cloud/ai", n=3)
    rows = recent_blocked_uris(window_seconds=86400, top_n=5)
    assert len(rows) == 1
    row = rows[0]
    # Old keys, unchanged meaning — the host-grouped key is still the identity.
    assert row["blocked_uri"] == "dchub.cloud"
    assert row["directive"] == "script-src-elem"
    assert row["count"] == 3
    # New evidence.
    assert row["document_uri"] == "https://dchub.cloud/ai"
    assert row["sample_blocked_uri"] == "https://dchub.cloud/static/gating.js?v=3", (
        "the full URI must survive — the host-strip is what hid which resource "
        "was blocked")


def test_the_most_frequent_document_wins_deterministically(store):
    from csp_report import recent_blocked_uris
    store("connect-src", "cdn.other.test", "https://dchub.cloud/b", n=1)
    store("connect-src", "cdn.other.test", "https://dchub.cloud/a", n=3)
    rows = recent_blocked_uris(window_seconds=86400, top_n=5)
    assert rows[0]["document_uri"] == "https://dchub.cloud/a"


def test_a_group_with_no_document_reports_an_empty_string_not_a_guess(store):
    from csp_report import recent_blocked_uris
    store("connect-src", "cdn.other.test", "", n=3)
    rows = recent_blocked_uris(window_seconds=86400, top_n=5)
    assert rows[0]["document_uri"] == ""


def test_events_outside_the_window_are_excluded(store):
    from csp_report import recent_blocked_uris
    store("connect-src", "cdn.other.test", "https://dchub.cloud/", n=3,
          age_s=90000.0)
    assert recent_blocked_uris(window_seconds=86400, top_n=5) == []


# ── host parsing, and fail-soft ───────────────────────────────────────────

@pytest.mark.parametrize("uri,host", [
    ("https://dchub.cloud/ai",            "dchub.cloud"),
    ("dchub.cloud",                       "dchub.cloud"),
    ("https://DCHub.Cloud:443/x?y=1",     "dchub.cloud"),
    ("https://user:pw@dchub.cloud/x",     "dchub.cloud"),
    ("http://[::1]:8080/x",               "[::1]"),
    ("",                                  ""),
    ("-",                                 ""),
])
def test_csp_host_reads_the_host_and_nothing_else(uri, host):
    assert r._csp_host(uri) == host


def test_a_www_variant_is_not_treated_as_same_origin(store):
    """The same-origin verdict is what suppresses the _headers remedy, so it
    must not fire on a host that merely looks related — a report from
    www.dchub.cloud about dchub.cloud IS a real cross-origin block."""
    issue, remedy = r.csp_violation_verdict(
        "dchub.cloud", "connect-src", "https://www.dchub.cloud/ai")
    assert issue == "csp_violation_recurring"
    assert _HEADERS_EDIT in remedy


def test_the_detector_is_fail_soft_when_the_store_is_unavailable(monkeypatch):
    """It runs every 6h inside a sweep; an exception here must become a
    finding, never an escape."""
    import csp_report
    def boom(**kw):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(csp_report, "recent_blocked_uris", boom)
    out = r.check_csp_violation_reports()
    assert len(out) == 1
    assert out[0]["issue"].startswith("consistency_radar_detector_crashed:")
    assert "store exploded" in out[0]["detail"]
