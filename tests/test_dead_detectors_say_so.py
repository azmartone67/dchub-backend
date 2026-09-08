"""A detector whose source is missing must SAY SO, not return [].

WHY (2026-09-08). `return findings` on an absent source table produces an
empty list, and the radar cannot tell that apart from "scanned fine, found
nothing". Two registered detectors had been doing exactly that on every scan
since they shipped:

    check_404_spike              neither request_log_404 nor request_log exists
    check_signup_drop_off_step   signup_events does not exist

Both are in the detector registry (`brain_consistency_radar.py` ~12630), so
they ran on every radar pass and reported health. The signup funnel has been
unmonitored the whole time and nothing anywhere said so. They were found by
the null-signal detector, whose premise is that this shape is invisible.

The tests below are deliberately about the SHAPE of the absent-source path,
not about 404s or signups: the defect was never in the detection logic, it
was in what the code does when it cannot detect at all.

Stdlib + pytest; no DB, no network.
"""
import re

import pytest

from routes import brain_consistency_radar as r


# ── the helper's contract ─────────────────────────────────────────────
def test_source_missing_is_a_real_finding():
    f = r._source_missing("check_x", "tbl_a, tbl_b", "thing detection")
    assert f["issue"] == "detector_source_missing:check_x"
    assert f["count"] == 1
    assert "tbl_a, tbl_b" in f["detail"]


def test_issue_key_is_per_detector_so_two_dead_ones_do_not_collide():
    a = r._source_missing("check_a", "t", "x")["issue"]
    b = r._source_missing("check_b", "t", "x")["issue"]
    assert a != b, "a shared issue key would dedupe two distinct dead detectors into one"


def test_detail_names_both_honest_resolutions():
    """It will sit open until someone acts, so it must say what closing it
    looks like — otherwise it is just a permanent alarm nobody can action."""
    d = r._source_missing("check_x", "t", "x")["detail"].lower()
    assert "creating the source" in d
    assert "retiring the detector" in d


def test_detail_says_why_an_empty_result_was_the_bug():
    d = r._source_missing("check_x", "t", "x")["detail"]
    assert "indistinguishable" in d


# ── the two call sites ────────────────────────────────────────────────
def _src():
    import inspect
    return inspect.getsource(r)


@pytest.mark.parametrize("detector,table", [
    ("check_404_spike", "request_log"),
    ("check_signup_drop_off_step", "signup_events"),
])
def test_the_absent_source_path_emits_instead_of_returning_empty(detector, table):
    """THE regression. Anchored on each detector's own body so a change to
    one cannot silently restore the bare return in the other."""
    src = _src()
    start = src.index(f"def {detector}(")
    body = src[start:start + 3000]
    probe = body.index(f"to_regclass('public.{table}')")
    window = body[probe:probe + 1000]
    assert "_source_missing(" in window, (
        f"{detector} no longer reports its missing source — a bare "
        f"`return findings` there is invisible to the radar")


@pytest.mark.parametrize("detector", [
    "check_404_spike", "check_signup_drop_off_step",
])
def test_both_detectors_are_still_registered(detector):
    """If a detector is dropped from the registry this file should fail
    loudly rather than keep guarding dead code."""
    src = _src()
    reg = src[src.index("check_404_spike,"):]
    assert re.search(rf"^\s*{detector},\s*$", reg[:2000], re.M), (
        f"{detector} is no longer in the registry — retire this guard too")


def test_the_404_detector_still_prefers_the_dedicated_table():
    """The fix must not disturb the fallback order: request_log_404 first,
    request_log second, and only then the missing-source finding."""
    src = _src()
    body = src[src.index("def check_404_spike("):]
    body = body[:body.index("_source_missing(")]
    assert body.index("request_log_404") < body.index("'public.request_log'")
