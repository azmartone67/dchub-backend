"""Heal→fix→learn rewire (2026-08-07) — pins the four loop-closure changes.

Context: the 08-07 audit found the brain SEES and does not ACT — 55
actionable findings, 0 proposals, 0 landed code since ~07-19, the same
condition specced-and-merged SIX times, red deadman feeds sitting 5-11 days
in a 62-comment monologue, and the detector-supply scout shipped dark.

What these tests pin:
  1. Landed-spec dedup finds fingerprints in MERGED docs/brain-proposals —
     the open-PR-only dedup was the 6x-duplicate treadmill (a REAL landed
     fingerprint from the tree must be found; garbage must not).
  2. The propose-stage recorder is three-state and fail-soft: outcome
     histogram counts 'proposed' as generated, and no DB → recorded=False
     without raising (instrumentation never breaks the run it measures).
  3. The deadman triage router exists, is capped, auto-closes on green, and
     the watch's step-5 calls it (source assertions — the router shells out
     to gh, so behavior tests live in the watcher's own dry-run).
  4. The detector-scout workflow is a REAL scheduler (cron, not
     dispatch-only), loud-fails on a missing secret AND on a disarmed
     scout — green-while-dark is the exact state it exists to end.

CI-SAFETY: no DB (env stripped per-test), no network, no gh calls.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def no_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)


# ── 1: landed-spec dedup ──────────────────────────────────────────────

def _some_landed_fingerprint():
    d = os.path.join(ROOT, "docs", "brain-proposals")
    pat = re.compile(r"<!--\s*fingerprint:([0-9a-f]{8,64})\s*-->")
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        try:
            head = open(os.path.join(d, name), encoding="utf-8",
                        errors="replace").read(600)
        except OSError:
            continue
        m = pat.search(head)
        if m:
            return m.group(1), name
    return None, None


def test_landed_spec_dedup_finds_a_real_merged_fingerprint():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes.brain_pr_opener import landed_spec_with_fingerprint
    fp, expected = _some_landed_fingerprint()
    assert fp, "no fingerprinted landed spec in docs/brain-proposals — " \
               "if the stamp format changed, update the dedup too"
    assert landed_spec_with_fingerprint(fp) == expected
    assert landed_spec_with_fingerprint("f" * 32) is None
    assert landed_spec_with_fingerprint("") is None
    assert landed_spec_with_fingerprint(None) is None


def test_filer_consults_landed_specs_not_just_open_prs():
    src = open(os.path.join(ROOT, "routes/brain_pr_opener.py"),
               encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    # ★Call-site assertion, not a bare name grep: the function DEFINITION
    # contains the same substring, so grepping the name passed even with the
    # caller mutated away (proven by mutation while writing this test).
    assert re.search(r"landed\s*=\s*landed_spec_with_fingerprint\(", code), \
        "open_spec_pr filer no longer consults landed specs — the 6x " \
        "duplicate-spec treadmill (inv-100025..100039) reopens"
    assert "awaiting_implementation" in code


# ── 2: propose-stage recorder ─────────────────────────────────────────

def test_recorder_counts_proposed_and_fails_soft_without_db(no_db):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes.brain_v2_layer5 import _record_propose_run
    row = _record_propose_run("learn_code", 3, [
        {"outcome": "proposed"},
        {"outcome": "search_not_found", "file": "x.py"},
        {"outcome": "claude_error: boom"},
    ])
    assert row["generated"] == 1
    assert row["considered"] == 3
    assert row["outcomes"]["proposed"] == 1
    assert row["outcomes"]["search_not_found"] == 1
    assert row["outcomes"]["claude_error"] == 1   # split(':') normalization
    assert row["recorded"] is False               # no DB → soft, not raised


def test_both_learn_endpoints_record_runs():
    src = open(os.path.join(ROOT, "routes/brain_v2_layer5.py"),
               encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert code.count('_record_propose_run("learn_code"') == 2, \
        "learn_code must record both the no-work and the processed path"
    assert code.count('_record_propose_run("learn_backend_issues"') == 2
    assert '"/api/v1/brain/propose-stage/status"' in code


# ── 3: deadman triage router ──────────────────────────────────────────

def test_triage_router_wired_capped_and_autoclosing():
    src = open(os.path.join(ROOT, "tools/deadman/watch.py"),
               encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "triage_red_feeds(overdue)" in code, \
        "watch step 5 no longer routes red feeds into work items"
    assert "TRIAGE_MAX_NEW_PER_RUN" in code
    assert '"close"' in code or "issue\", \"close" in code, \
        "triage issues must auto-close on green or they become a graveyard"


# ── 4: detector-scout workflow is a real, loud scheduler ──────────────

def test_scout_workflow_has_cron_and_loud_failures():
    wf = os.path.join(ROOT, ".github/workflows/detector-scout-daily.yml")
    assert os.path.exists(wf), "no workflow schedules the detector scout"
    src = open(wf, encoding="utf-8").read()
    assert "schedule:" in src and "cron:" in src, \
        "dispatch-only is not a scheduler (#2027)"
    assert "/api/v1/admin/detector-scout/tick" in src
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert re.search(r"if \[ -z .*ADMIN_KEY.*\][\s\S]{0,200}?exit 1", body), \
        "missing secret must exit 1 (the gas-pipeline green-no-op class)"
    assert '"skipped"\\s*:\\s*"disabled"' in src or "disabled" in body, \
        "a disarmed scout must read RED, not green-while-dark"


def test_mirror_penalties_feed_the_findings_table():
    src = open(os.path.join(ROOT, "routes/brain_mirror.py"),
               encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "upsert_brain_finding" in code, \
        "mirror penalties no longer reach brain_findings — the brain " \
        "stops prioritizing its own pipeline stalls"
    assert "mirror_penalty_" in code
