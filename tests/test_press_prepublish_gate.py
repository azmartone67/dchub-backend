"""The analyst pre-flight as a BLOCKING gate (2026-08-07).

PR #2359 shipped analyst_review as a post-hoc sweep: it could only find a
broken release AFTER it was already public. That is how
/news/2026-08-07-perplexity-dcpi-dual-score-citation got a live URL for a
release with no body and a future date. Quarantine is cleanup; this is the
gate — every composer calls it BEFORE the INSERT and binds `published` to what
it returns.

Pinned here (each is a MUST-FAIL guard — feed it the broken shape, assert it is
caught, and assert the wiring that makes the catch load-bearing):
  · a HARD failure can never be published, even when the caller asked to
  · the gate NEVER promotes: a clean release stays exactly as the caller wanted
  · the reviewer raising is a FAIL-CLOSED (a check that cannot run is not a pass)
  · no composer spells a `published` literal in its INSERT any more — the flag
    is bound from the gate, which is what makes the guard non-bypassable
  · the human one-click approve path is gated too (a held draft must not go
    live on a click), because completeness is a different question from the
    fact-check guard that already runs there
  · the post-publish sweep stays report-only until PRESS_INTEGRITY_ENFORCE=1,
    while the pre-publish block is always on (it mutates nothing — worst case a
    good release waits as a draft)

CI-SAFETY: gate_press_publish is pure (no DB, no network). The composer wiring
is asserted against SOURCE, not by executing Flask routes.
"""
import ast
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
        "title": "DC Hub's live index just crossed 17,000 facilities",
        "slug": "2026-08-07-facilities-17000",
        "body": ("The live index crossed 17,000 tracked data-center facilities "
                 "this week, across 170 countries. " * 6),
        "meta_description": "DC Hub crossed 17,000 tracked facilities.",
        "date": datetime.date.today().strftime("%Y-%m-%d"),
    }


def _blank():
    r = _good()
    r["body"] = ""
    return r


def _future():
    r = _good()
    r["date"] = (datetime.date.today()
                 + datetime.timedelta(days=45)).strftime("%Y-%m-%d")
    return r


# ── the decision itself ──────────────────────────────────────────────

def test_a_clean_release_publishes_when_the_caller_asked_to(pi):
    g = pi.gate_press_publish(_good(), want_published=True, where="t")
    assert g["publish"] is True and g["hard"] is False, g["issues"]


def test_the_gate_never_promotes_a_draft(pi):
    """It only ever WITHHOLDS. A composer that wanted a draft still gets one,
    otherwise this gate would become an accidental auto-publisher."""
    g = pi.gate_press_publish(_good(), want_published=False, where="t")
    assert g["publish"] is False and g["ok"] is True


@pytest.mark.parametrize("bad,code", [
    (_blank(), "body_blank_or_stub"),
    (_future(), "future_dated"),
])
def test_a_hard_failure_cannot_publish_however_the_caller_asked(pi, bad, code):
    g = pi.gate_press_publish(bad, want_published=True, where="t")
    assert g["publish"] is False, "a HARD failure reached published=TRUE"
    assert g["hard"] is True and code in g["codes"], g


def test_a_stub_body_cannot_publish(pi):
    r = _good()
    r["body"] = "<p>Coming soon.</p>"
    assert pi.gate_press_publish(r, want_published=True)["publish"] is False


def test_placeholder_text_cannot_publish(pi):
    r = _good()
    r["body"] = "Lorem ipsum dolor sit amet. " * 20
    assert pi.gate_press_publish(r, want_published=True)["publish"] is False


def test_a_missing_title_cannot_publish(pi):
    r = _good()
    r["title"] = ""
    assert pi.gate_press_publish(r, want_published=True)["publish"] is False


def test_a_reviewer_that_raises_fails_closed(pi, monkeypatch):
    """A check that cannot run is NOT a pass — the module's own house rule.
    Fail-open here would reopen the exact hole this gate exists to close."""
    def _boom(_release):
        raise RuntimeError("reviewer exploded")

    monkeypatch.setattr(pi, "analyst_review", _boom)
    g = pi.gate_press_publish(_good(), want_published=True, where="t")
    assert g["publish"] is False and g["hard"] is True
    assert "reviewer_error" in g["codes"]


def test_the_gate_never_raises_on_junk(pi):
    for junk in ({}, None, {"title": None, "body": 12345, "date": "nope"},
                 {"body": []}):
        g = pi.gate_press_publish(junk, want_published=True)
        assert isinstance(g, dict) and g["publish"] is False


def test_soft_issues_alone_do_not_block(pi, monkeypatch):
    """SOFT issues flag a human; they must not dark-hole the feed."""
    monkeypatch.setattr(pi, "analyst_review", lambda r: {
        "ok": True, "hard": False, "slug": "s",
        "issues": [{"code": "unverified_claims", "detail": "x", "hard": False}]})
    g = pi.gate_press_publish(_good(), want_published=True)
    assert g["publish"] is True and "unverified_claims" in g["codes"]


# ── the wiring that makes the gate load-bearing ──────────────────────

_COMPOSERS = [
    ("routes/brain_press_loop.py", "brain_press_loop"),
    ("routes/ai_citation_tracker.py", "ai_citation_tracker"),
    ("routes/marketing_engine.py", "marketing_engine"),
]


@pytest.mark.parametrize("path,name", _COMPOSERS)
def test_every_composer_calls_the_gate_before_writing(path, name):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    assert "gate_press_publish" in src, f"{name} never calls the pre-publish gate"
    i = src.index("gate_press_publish")
    j = src.index("INSERT INTO press_releases")
    assert i < j, f"{name} calls the gate AFTER its insert — that is a sweep, not a gate"


@pytest.mark.parametrize("path,name", _COMPOSERS)
def test_no_composer_hardcodes_its_published_flag(path, name):
    """★ The mutation this guard exists to catch: re-introducing a literal into
    the VALUES clause silently detaches the gate, and every behavioural test
    above still passes because gate_press_publish itself is unchanged. The flag
    must be BOUND from the gate's verdict."""
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    for m in _press_release_inserts(src):
        vals = _values_clause(m)
        assert vals is not None, f"{name}: could not read the VALUES clause"
        assert not _has_bool_literal(vals), (
            f"{name} hardcodes a published literal in VALUES — the gate's "
            f"verdict is not what lands in the row: {vals!r}")


def _press_release_inserts(src):
    out = []
    idx = 0
    while True:
        i = src.find("INSERT INTO press_releases", idx)
        if i < 0:
            return out
        out.append(src[i:i + 900])
        idx = i + 1


def _values_clause(chunk):
    up = chunk.upper()
    i = up.find("VALUES")
    if i < 0:
        return None
    j = up.find("ON CONFLICT", i)
    return chunk[i:j if j > 0 else i + 300]


def _has_bool_literal(vals):
    import re
    return bool(re.search(r"\b(TRUE|FALSE)\b", vals, re.IGNORECASE))


def test_the_human_approve_path_is_gated_too():
    """A composer holding a broken release as a draft achieves nothing if one
    click publishes it anyway. The fact-check guard already there proves the
    NUMBERS; it says nothing about a blank body or a future date."""
    src = open(os.path.join(ROOT, "routes/media_pending_digest.py"),
               encoding="utf-8").read()
    assert "gate_press_publish" in src, "one-click approve bypasses the gate"
    i = src.index("gate_press_publish")
    j = src.index("SET published = TRUE")
    assert i < j, "the gate runs after the approve UPDATE"
    assert "press_integrity_unavailable" in src, \
        "gate unavailable must fail closed at the approve path"


def test_the_review_note_is_a_side_table_not_published_copy():
    """The issue list must never be written into meta_description or body,
    where it would ship as public copy."""
    src = open(os.path.join(ROOT, "routes/press_integrity.py"),
               encoding="utf-8").read()
    assert "press_integrity_reviews" in src
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "attach_review")
    # Read the EXECUTABLE statements only — a docstring that names the table it
    # deliberately avoids must not read as a violation of the rule it explains.
    stmts = [s for s in fn.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    body = "\n".join(ast.get_source_segment(src, s) or "" for s in stmts)
    assert "press_releases" not in body, \
        "attach_review writes into press_releases — issues could reach public copy"
    assert "ON CONFLICT" in body, "review note is not idempotent"


def test_the_quarantine_sweep_is_still_report_only_by_default(pi, monkeypatch):
    """The pre-publish block is always on (it mutates nothing). The sweep, which
    flips ALREADY-LIVE rows, keeps the watch-then-arm ceremony."""
    monkeypatch.delenv("PRESS_INTEGRITY_ENFORCE", raising=False)
    assert pi._enforce() is False
    monkeypatch.setenv("PRESS_INTEGRITY_ENFORCE", "1")
    assert pi._enforce() is True


def test_the_pre_publish_gate_is_not_behind_the_enforce_flag(pi, monkeypatch):
    """If it were, shipping this PR would leave the hole open until someone
    remembered to flip a flag."""
    monkeypatch.delenv("PRESS_INTEGRITY_ENFORCE", raising=False)
    g = pi.gate_press_publish(_blank(), want_published=True)
    assert g["publish"] is False, \
        "a blank release could publish while enforcement was off"
