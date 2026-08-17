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


# ── the landed scan must never fail SILENTLY (2026-08-16) ───────────────────
# It is 0-for-2 in production and nobody could tell, because both excepts
# swallowed the reason and a miss was indistinguishable from an honest "no
# match". Verified NOT the cause, against the worker's running commit
# 4cb4738f: the function was present, the call was wired, the stamped doc was
# in the tree. The silence is the defect these pin.
# ★ Deliberately here, not in test_brain_spec_lifecycle.py — that file's
# autouse _seal_landed_spec_scan stubs this very function, so a guard written
# there would assert against a lambda. (It did, until this comment existed.)

def test_unreadable_corpus_warns_instead_of_returning_a_quiet_none(monkeypatch, caplog):
    """Mutation: restore `except Exception: pass` -> red."""
    import logging
    from routes import brain_pr_opener as opener
    monkeypatch.setattr(opener.os, "listdir",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no such dir")))
    caplog.set_level(logging.WARNING, logger="routes.brain_pr_opener")
    assert opener.landed_spec_with_fingerprint("deadbeef" * 4) is None
    assert any("LANDED SCAN FAILED" in r.getMessage() for r in caplog.records), \
        "an unreadable docs corpus must warn — a silent miss reads as 'no match'"


def test_empty_corpus_warns_because_every_condition_then_looks_novel(monkeypatch, caplog):
    """Mutation: drop the scanned==0 branch -> red."""
    import logging
    from routes import brain_pr_opener as opener
    monkeypatch.setattr(opener.os, "listdir", lambda *_a, **_k: [])
    caplog.set_level(logging.WARNING, logger="routes.brain_pr_opener")
    assert opener.landed_spec_with_fingerprint("deadbeef" * 4) is None
    assert any("READ 0 DOCS" in r.getMessage() for r in caplog.records)


# ── every landed spec must carry a fingerprint stamp (2026-08-17) ───────────
# 65 docs filed before 2026-07-15 had none, so no stamp-based dedup could ever
# match them and an old condition re-filed today looked novel. 44 of the 65
# turned out to share a condition with a LATER doc that was filed anyway —
# including agenda-41 "311 DCPI markets" vs agenda-100198 "320 DCPI markets".
# Backfilled from each doc's own H1, the derivation that reproduces 100% of
# agenda (77/77) and prop (8/8) stamps. NOT applied to inv, where the filer
# hashes a DB `signal` that never reaches the markdown (54% reproducible) —
# and no inv doc was unstamped, so none needed it.

def test_every_landed_spec_carries_a_fingerprint_stamp():
    """Mutation: strip the stamp from any doc in docs/brain-proposals -> red.
    An unstamped landed spec is invisible to the dedup forever."""
    import os, re
    d = os.path.join(ROOT, "docs", "brain-proposals")
    if not os.path.isdir(d):
        import pytest
        pytest.skip("docs corpus absent in this environment")
    stamp = re.compile(r"<!--\s*fingerprint:[0-9a-f]{8,64}\s*-->")
    missing = [n for n in sorted(os.listdir(d)) if n.endswith(".md")
               and not stamp.search(open(os.path.join(d, n),
                                        encoding="utf-8",
                                        errors="replace").read(600))]
    assert not missing, (
        f"{len(missing)} landed spec(s) carry no fingerprint stamp and are "
        f"invisible to both dedup checks: {missing[:5]}")


# ── 5: deep merged-PR dedup (2026-08-17) ─────────────────────────────

class _GhResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        return self._payload


def test_merged_pr_dedup_deep_search_covers_the_100_pr_horizon(monkeypatch):
    """agenda-41's spec merged in July — outside the recently-updated-100
    closed-PR list — and its condition re-filed as agenda-100198 on 08-16.
    The deep issue-search pass exists to answer for that horizon. Pins:
    (a) it runs when the shallow list misses and returns the PR number,
    (b) it skips non-PR items and re-verifies the stamp instead of trusting
    the phrase match, (c) it fails OPEN on a search error."""
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import routes.brain_pr_opener as bpo

    fp = "e" * 32
    seen = []

    def gh_hit(method, path, body=None):
        seen.append(path)
        if path.startswith("/repos/") and "/pulls" in path:
            return _GhResp(200, [])   # the horizon: recent-100 list is empty
        assert path.startswith("/search/issues"), path
        return _GhResp(200, {"items": [
            {"number": 775, "body": f"<!-- fingerprint:{fp} -->"},  # no pull_request key
            {"number": 777, "pull_request": {"url": "https://x"},
             "body": f"<!-- fingerprint:{fp} -->\nrest"},
        ]})

    monkeypatch.setattr(bpo, "_gh", gh_hit)
    assert bpo.merged_spec_pr_with_fingerprint(fp) == 777
    assert any(p.startswith("/search/issues") for p in seen)

    def gh_phrase_only(method, path, body=None):
        if path.startswith("/repos/") and "/pulls" in path:
            return _GhResp(200, [])
        return _GhResp(200, {"items": [
            {"number": 778, "pull_request": {}, "body": "no stamp here"}]})

    monkeypatch.setattr(bpo, "_gh", gh_phrase_only)
    assert bpo.merged_spec_pr_with_fingerprint(fp) is None

    def gh_rate_limited(method, path, body=None):
        if path.startswith("/repos/") and "/pulls" in path:
            return _GhResp(200, [])
        return _GhResp(403, {})

    monkeypatch.setattr(bpo, "_gh", gh_rate_limited)
    assert bpo.merged_spec_pr_with_fingerprint(fp) is None  # fail-open
