"""Merge-reconciler tests. FULLY MOCKED GitHub + DB. NO real token, NO
network, NO DB (CI runs with neither — and tests must never import main).

We monkeypatch the module's own primitives (list_merged_brain_prs plus the
write primitives) and assert the SAFETY INVARIANTS:
  · DISABLED (kill switch) ⇒ zero GitHub calls, zero writes.
  · GitHub list error ⇒ FAIL CLOSED: ok=False, zero writes.
  · dry ⇒ zero writes even with merged PRs to reconcile.
  · Only brain-spec/ + brain/autofix- branches ever reconcile; reverts and
    foreign branches are skipped (defense-in-depth, even if the listing
    sneaks one in).
  · Per-run cap bounds writes.
  · Outcome verdicts are HONEST: pending inside grace, still_broken=TRUE on
    recurrence after merge, FALSE only when the finding was recently live
    then went quiet, and NO outcome when the label was never tracked or was
    already dormant long before the merge.

Run:  python3 -m pytest tests/test_brain_merge_reconciler.py -v
"""
import datetime as dt
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _flask_shim():
    """Shim flask when absent (mirrors test_brain_pr_janitor)."""
    saved = sys.modules.get("flask")
    installed = False
    if "flask" not in sys.modules:
        flask = types.ModuleType("flask")

        class _BP:
            def __init__(self, *a, **k):
                pass

            def _noop(self, *a, **k):
                return lambda fn: fn

            get = post = route = _noop

        flask.Blueprint = _BP
        flask.jsonify = lambda *a, **k: (a, k)
        flask.request = types.SimpleNamespace(
            headers={}, args={}, get_json=lambda *a, **k: {})
        sys.modules["flask"] = flask
        installed = True
    yield
    if installed:
        if saved is not None:
            sys.modules["flask"] = saved
        else:
            sys.modules.pop("flask", None)


@pytest.fixture()
def rec(_flask_shim):
    import routes.brain_merge_reconciler as m
    return m


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BRAIN_MERGE_RECONCILER_DISABLE",
              "BRAIN_MERGE_RECONCILER_MAX_PER_RUN",
              "BRAIN_MERGE_RECONCILER_GRACE_HOURS",
              "BRAIN_MERGE_RECONCILER_RECENT_DAYS"):
        monkeypatch.delenv(k, raising=False)
    yield


NOW = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)


def _pr(number=1511, branch="brain-spec/agenda-76-reliability-brain-finding",
        title="[brain-spec] agenda #76: [reliability] Brain finding: "
              "mcp_tool_zero_conversion @ /admin/per-tool",
        merged_hours_ago=48):
    return {"number": number, "branch": branch, "title": title,
            "html_url": f"https://github.com/x/y/pull/{number}",
            "merged_at": NOW - dt.timedelta(hours=merged_hours_ago),
            "created_at": NOW - dt.timedelta(hours=merged_hours_ago + 24),
            "author": "azmartone67"}


class _Boom:
    """Sentinel that explodes if any write primitive is touched."""

    def __call__(self, *a, **k):
        raise AssertionError("write primitive called on a no-write path")


class _FakeCursor:
    """Answers the ledger scan + match/last-seen reads with empty results."""

    def __init__(self):
        self.rowcount = 0

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def rollback(self):
        pass

    def close(self):
        pass


# ── parsing ───────────────────────────────────────────────────────────

def test_autofix_branch_id_regex(rec):
    m = rec._AUTOFIX_BRANCH_RE.match("brain/autofix-silent_failure-482-a3f9c2d1")
    assert m and m.group(1) == "482"


def test_revert_branch_never_yields_an_id(rec):
    # brain/autofix-revert-<ORIG_PR>-<ts>: the digits are a PR number and a
    # timestamp, NOT a proposal id — matching it would credit the wrong row.
    assert rec._AUTOFIX_BRANCH_RE.match(
        "brain/autofix-revert-1234-1720512345") is None


def test_finding_label_parse(rec):
    label = rec.parse_finding_label(
        "[brain-spec] agenda #74: [reliability] Brain finding: "
        "ai_platform_crawl_drop:chatgpt @ ai_requests")
    assert label == "ai_platform_crawl_drop:chatgpt"
    assert rec.parse_finding_label("chore: bump deps") is None


# ── honest outcome verdicts ──────────────────────────────────────────

def test_outcome_pending_inside_grace(rec):
    st, broken, ev = rec.decide_outcome(
        NOW - dt.timedelta(hours=3), None, NOW, 24, 14)
    assert st == "pending_grace" and broken is None and "grace" in ev


def test_outcome_recurrence_means_still_broken(rec):
    merged = NOW - dt.timedelta(hours=48)
    st, broken, ev = rec.decide_outcome(
        merged, merged + dt.timedelta(hours=6), NOW, 24, 14)
    assert st == "outcome" and broken is True and "AFTER merge" in ev


def test_outcome_resolved_when_recently_live_then_quiet(rec):
    merged = NOW - dt.timedelta(hours=48)
    st, broken, ev = rec.decide_outcome(
        merged, merged - dt.timedelta(days=2), NOW, 24, 14)
    assert st == "outcome" and broken is False


def test_no_outcome_when_label_never_tracked(rec):
    st, broken, _ = rec.decide_outcome(
        NOW - dt.timedelta(hours=48), None, NOW, 24, 14)
    assert st == "no_evidence" and broken is None


def test_no_outcome_when_finding_already_dormant(rec):
    merged = NOW - dt.timedelta(hours=48)
    st, broken, ev = rec.decide_outcome(
        merged, merged - dt.timedelta(days=40), NOW, 24, 14)
    assert st == "no_evidence" and broken is None and "dormant" in ev


# ── safety invariants ────────────────────────────────────────────────

def test_disabled_makes_zero_github_and_db_calls(rec, monkeypatch):
    monkeypatch.setenv("BRAIN_MERGE_RECONCILER_DISABLE", "1")
    monkeypatch.setattr(rec, "list_merged_brain_prs", _Boom())
    monkeypatch.setattr(rec, "_conn", _Boom())
    rep = rec.run_reconciliation()
    assert rep["ok"] is False and rep.get("disabled") is True


def test_github_error_fails_closed(rec, monkeypatch):
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": False, "error": "500:boom", "prs": []})
    monkeypatch.setattr(rec, "_conn", _Boom())  # must not even connect
    rep = rec.run_reconciliation()
    assert rep["ok"] is False and "github_list" in rep["error"]
    assert rep["reconciled"] == []


def test_dry_run_never_writes(rec, monkeypatch):
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": [_pr()]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    for prim in ("_ensure_schema", "mark_proposal_merged",
                 "backfill_proposal_row", "record_review_decision",
                 "record_outcome", "_upsert_ledger"):
        monkeypatch.setattr(rec, prim, _Boom())
    rep = rec.run_reconciliation(dry=True)
    assert rep["ok"] is True and rep["dry"] is True
    assert len(rep["reconciled"]) == 1
    assert rep["reconciled"][0]["match"] == "unmatched"


def test_foreign_and_revert_branches_skipped_defense_in_depth(rec, monkeypatch):
    sneaked = [_pr(number=1, branch="feature/human-work"),
               _pr(number=2, branch="brain/autofix-revert-99-1720512345")]
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": sneaked})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    for prim in ("mark_proposal_merged", "backfill_proposal_row",
                 "record_review_decision", "record_outcome", "_upsert_ledger"):
        monkeypatch.setattr(rec, prim, _Boom())
    rep = rec.run_reconciliation(dry=True)
    assert rep["reconciled"] == [] and rep["pending"] == []


def test_per_run_cap_bounds_work(rec, monkeypatch):
    prs = [_pr(number=n, branch=f"brain-spec/agenda-{n}-thing")
           for n in range(1, 8)]
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": prs})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setenv("BRAIN_MERGE_RECONCILER_MAX_PER_RUN", "3")
    rep = rec.run_reconciliation(dry=True)
    assert rep["acted"] == 3
    assert sum(1 for s in rep["skipped"] if "cap" in s["why"]) == 4


def test_live_run_backfills_and_records(rec, monkeypatch):
    """The 07-09 scenario: a merged brain-spec PR with no matching row gets a
    backfill insert + review decision + ledger row; outcome honest-pending is
    NOT written when the label was never tracked."""
    calls = {"backfill": 0, "review": 0, "outcome": 0, "ledger": 0}
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": [_pr()]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "mark_proposal_merged", _Boom())  # no match ⇒ never
    monkeypatch.setattr(rec, "backfill_proposal_row",
                        lambda cur, pr: calls.__setitem__(
                            "backfill", calls["backfill"] + 1) or 4242)
    monkeypatch.setattr(rec, "record_review_decision",
                        lambda pid, label, pr: calls.__setitem__(
                            "review", calls["review"] + 1) or True)
    monkeypatch.setattr(rec, "record_outcome",
                        lambda *a, **k: calls.__setitem__(
                            "outcome", calls["outcome"] + 1) or True)
    monkeypatch.setattr(rec, "_upsert_ledger",
                        lambda *a, **k: calls.__setitem__(
                            "ledger", calls["ledger"] + 1))
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    assert calls == {"backfill": 1, "review": 1, "outcome": 0, "ledger": 1}
    e = rep["reconciled"][0]
    assert e["proposal_id"] == 4242 and e["match"] == "backfill_insert"
    # label never tracked (fake cursor returns None) ⇒ honest no_evidence
    assert e["outcome_state"] == "no_evidence" and e["still_broken"] is None


# ── R66 (2026-07-11): spec PRs are doc-only — never graded as fixes ──

def test_decide_outcome_noun_parameter(rec):
    """decide_outcome(noun=...) names the applied event honestly so the
    probe (brain_learning) can reuse the SAME discipline for autopilot
    actions without evidence text claiming a 'merge' happened."""
    applied = NOW - dt.timedelta(hours=48)
    st, broken, ev = rec.decide_outcome(
        applied, applied + dt.timedelta(hours=6), NOW, 6, 7, noun="action")
    assert st == "outcome" and broken is True and "AFTER action" in ev
    # default stays byte-identical for the reconciler's own evidence
    st, broken, ev = rec.decide_outcome(
        applied, applied + dt.timedelta(hours=6), NOW, 24, 14)
    assert "AFTER merge" in ev


def test_spec_pr_never_graded_as_fix_outcome(rec, monkeypatch):
    """A brain-spec PR adds a DOC only (zero code execution) — its merge is
    CREDITED (backfill + review decision) but a standing detector re-firing
    after a document merge is NOT a failed fix. The outcome must be labeled
    spec_doc_ungraded, still_broken=None, and record_outcome NEVER called."""
    calls = {"outcome": 0, "ledger_state": None, "ledger_broken": "sentinel"}
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": [_pr()]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "mark_proposal_merged", _Boom())
    monkeypatch.setattr(rec, "backfill_proposal_row", lambda cur, pr: 4242)
    monkeypatch.setattr(rec, "record_review_decision",
                        lambda pid, label, pr: True)
    # finding re-seen 1h ago (well AFTER the 48h-old merge) — for an autofix
    # PR this would be an honest still_broken=TRUE outcome...
    monkeypatch.setattr(rec, "_last_seen",
                        lambda cur, label: rec._now() - dt.timedelta(hours=1))
    monkeypatch.setattr(rec, "record_outcome",
                        lambda *a, **k: calls.__setitem__(
                            "outcome", calls["outcome"] + 1) or True)

    def _ledger(cur, pr, pid, method, label, state, still_broken, evidence):
        calls["ledger_state"] = state
        calls["ledger_broken"] = still_broken

    monkeypatch.setattr(rec, "_upsert_ledger", _ledger)
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    e = rep["reconciled"][0]
    # ...but a spec PR is a doc: labeled, ungraded, no outcome row.
    assert calls["outcome"] == 0
    assert e["outcome_state"] == "spec_doc_ungraded"
    assert e["still_broken"] is None
    assert "doc-only spec PR" in e["evidence"]
    assert calls["ledger_state"] == "spec_doc_ungraded"
    assert calls["ledger_broken"] is None


def test_autofix_pr_outcome_still_recorded(rec, monkeypatch):
    """The spec-PR carve-out must NOT touch real mechanical autofix PRs: a
    recurrence after an autofix merge is still an honest still_broken=TRUE
    outcome, recorded through record_outcome."""
    recorded = {}
    pr = _pr(number=1600,
             branch="brain/autofix-interval_literal-482-a3f9c2d1",
             title="[brain autofix] Brain finding: data_freshness_sla_breach"
                   " @ routes/example.py")
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": [pr]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "match_proposal",
                        lambda cur, p: (482, "autofix_branch_id", "embedded"))
    monkeypatch.setattr(rec, "mark_proposal_merged", lambda cur, pid, p: True)
    monkeypatch.setattr(rec, "record_review_decision",
                        lambda pid, label, p: True)
    monkeypatch.setattr(rec, "_last_seen",
                        lambda cur, label: rec._now() - dt.timedelta(hours=1))
    monkeypatch.setattr(rec, "record_outcome",
                        lambda pid, broken, ev, p: recorded.update(
                            pid=pid, broken=broken) or True)
    monkeypatch.setattr(rec, "_upsert_ledger", lambda *a, **k: None)
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    e = rep["reconciled"][0]
    assert e["outcome_state"] == "outcome_recorded"
    assert recorded == {"pid": 482, "broken": True}


# ── merge_outcome: the pull-side repair of the GG#4 push callback ──────
# The L5 confidence calibration self-tunes from
# COUNT(*) FILTER (WHERE merge_outcome IS NOT NULL) >= _CALIB_MIN_SAMPLES.
# Measured 2026-09-07: that count was 0 for every loop_name (NULL on 119 of
# 119 rows), because the only writer was a workflow whose job gate needs the
# `autonomous-brain-layer5` label — last seen on a merged PR 2026-08-08, so
# 12 of 12 recent runs were `skipped`. The threshold therefore sat at 0.85
# forever while the best pending proposal scored 0.83. These pin the
# reconciler writing what it already decided, and pin the doc-only carve-out
# holding on THIS path too — labelling spec PRs to fix the same symptom
# would have recorded merged_healthy for a markdown note.


def _autofix_pr():
    return _pr(number=1700,
               branch="brain/autofix-interval_literal-901-b7e1f4aa",
               title="[brain autofix] Brain finding: data_freshness_sla_breach"
                     " @ routes/example.py")


def _wire(rec, monkeypatch, pr, last_seen):
    """Common harness: one merged PR, all writes stubbed, capture the
    merge_outcome call."""
    seen = {}
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": [pr]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "match_proposal",
                        lambda cur, p: (901, "autofix_branch_id", "embedded"))
    monkeypatch.setattr(rec, "mark_proposal_merged", lambda cur, pid, p: True)
    monkeypatch.setattr(rec, "backfill_proposal_row", lambda cur, p: 901)
    monkeypatch.setattr(rec, "record_review_decision",
                        lambda pid, label, p: True)
    monkeypatch.setattr(rec, "_last_seen", lambda cur, label: last_seen)
    monkeypatch.setattr(rec, "record_outcome", lambda *a, **k: True)
    monkeypatch.setattr(rec, "_upsert_ledger", lambda *a, **k: None)
    monkeypatch.setattr(rec, "mark_merge_outcome",
                        lambda cur, pid, outcome, detail: seen.update(
                            pid=pid, outcome=outcome) or True)
    return seen


def test_recurrence_writes_merged_ineffective_not_reverted(rec, monkeypatch):
    """A finding re-seen after the merge is an honest negative — but it is
    NOT GG#4's `merged_reverted`, which means prod broke and was rolled back.
    Nothing was reverted here. The calibration divides healthy/resolved, so
    a distinct value scores identically without asserting a revert."""
    seen = _wire(rec, monkeypatch, _autofix_pr(),
                 last_seen=rec._now() - dt.timedelta(hours=1))
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    assert seen == {"pid": 901, "outcome": "merged_ineffective"}
    assert rep["reconciled"][0]["merge_outcome"] == "merged_ineffective"


def test_clean_merge_writes_merged_healthy(rec, monkeypatch):
    """Live shortly before the merge, quiet since — the fix held."""
    merged = NOW - dt.timedelta(hours=48)
    seen = _wire(rec, monkeypatch, _autofix_pr(),
                 last_seen=merged - dt.timedelta(days=2))
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    assert seen == {"pid": 901, "outcome": "merged_healthy"}


def test_spec_doc_pr_never_writes_a_merge_outcome(rec, monkeypatch):
    """THE regression this change must not cause. A brain-spec PR is a
    markdown note; crediting it as merged_healthy would teach the threshold
    to trust a producer that ships nothing, drag the bar toward the 0.70
    floor, and admit the low-confidence backlog on fabricated evidence.
    _Boom fails loudly rather than silently recording."""
    monkeypatch.setattr(rec, "mark_merge_outcome", _Boom())
    seen = _wire(rec, monkeypatch, _pr(),   # default _pr() is brain-spec/
                 last_seen=rec._now() - dt.timedelta(hours=1))
    monkeypatch.setattr(rec, "mark_merge_outcome", _Boom())
    monkeypatch.setattr(rec, "match_proposal",
                        lambda cur, p: (4242, "spec", "doc"))
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    e = rep["reconciled"][0]
    assert e["outcome_state"] == "spec_doc_ungraded"
    assert "merge_outcome" not in e
    assert seen == {}


def test_mark_merge_outcome_only_fills_a_null(rec):
    """First verdict wins: a re-reconciliation of the same PR must not
    overwrite an outcome already on the row (the GG#4 callback may have
    written it, and a later re-seen finding must not rewrite history)."""
    captured = {}

    class _Cur:
        rowcount = 1

        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params

    assert rec.mark_merge_outcome(_Cur(), 901, "merged_healthy", "ev") is True
    assert "merge_outcome IS NULL" in captured["sql"]
    assert captured["params"][0] == "merged_healthy"
    assert captured["params"][2] == 901


# ══════════════════════════════════════════════════════════════════════
#  2026-09-18 — THE REJECTION HALF OF THE REVIEW SIGNAL
#
#  Before this change brain_review_decisions had one writer and it
#  hardcoded 'approve', so check_rejection_skip() (live at
#  brain_v2_layer4.py:925) could only ever return False. These tests pin
#  the two things that make the fix real rather than merely present:
#  the pass is REACHABLE, and its hash AGREES with the reader's.
# ══════════════════════════════════════════════════════════════════════

class _ProposalCursor(_FakeCursor):
    """Fake cursor whose proposal lookup returns a real (issue_key,
    search_text) pair — the row Layer 5 wrote from the pair Layer 4 hashes."""

    def __init__(self, issue_key="funnel_step_collapse",
                 search_text="if resp.status_code == 200:"):
        super().__init__()
        self._row = (issue_key, search_text)

    def fetchone(self):
        return self._row


def _closed_pr(number=9001, branch="brain-spec/funnel_step_collapse-77-abc",
               closed=None):
    return {"number": number, "branch": branch,
            "title": "brain-spec: Brain finding: funnel_step_collapse @ x",
            "html_url": f"https://github.com/o/r/pull/{number}",
            "merged_at": None,
            "closed_at": closed or dt.datetime(2026, 9, 17,
                                               tzinfo=dt.timezone.utc),
            "created_at": dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc),
            "author": "azmartone67"}


def test_rejection_key_reads_both_halves_off_the_proposal_row(rec):
    """rejection_key must return the finding label AND the search text."""
    label, find, src = rec.rejection_key(_ProposalCursor(), 7, "title-label")
    assert label == "funnel_step_collapse"
    assert find == "if resp.status_code == 200:"
    assert src == "proposal_issue_key"


def test_rejection_hash_equals_what_layer4_looks_up(rec):
    """★ THE DECISIVE TEST. The whole change is worthless unless the hash the
    reconciler WRITES is the hash Layer 4 READS.

    Layer 4 calls check_rejection_skip(issue["issue"], find), which hashes
    issue_hash(label, find_text). We assert the writer's key is byte-identical
    to that, using the REAL issue_hash — not a re-implementation of it."""
    from routes.brain_learning import issue_hash

    label, find, _ = rec.rejection_key(_ProposalCursor(), 7, "ignored")
    written = issue_hash(label, find)
    # Exactly the call brain_v2_layer4.py:925 makes for this finding.
    looked_up = issue_hash("funnel_step_collapse", "if resp.status_code == 200:")
    assert written == looked_up

    # ── MUST-FAIL CONTROL ────────────────────────────────────────────────
    # The pre-fix keying (label only, find_text defaulted to "") is what made
    # the gate inert. If this assertion ever fails, the test above has gone
    # vacuous — it would be passing for a key that cannot match.
    label_only = issue_hash(label)
    assert label_only != looked_up, (
        "label-only hash collided with the (label, find) hash — the control "
        "no longer distinguishes the broken keying from the fixed one")


def test_layer4_still_keys_on_label_and_find(rec):
    """Pin the READER. This fix is only correct while Layer 4 keeps passing
    `find` into check_rejection_skip; if someone drops that argument the
    writer above silently stops matching again. Source-level pin because the
    disagreement is exactly what has no runtime error."""
    import re
    src = open(os.path.join(ROOT, "routes", "brain_v2_layer4.py"),
               encoding="utf-8").read()
    assert re.search(r"check_rejection_skip\(\s*issue\.get\(['\"]issue['\"]\)\s*,\s*find\s*\)",
                     src), ("brain_v2_layer4 no longer calls "
                            "check_rejection_skip(issue['issue'], find) — the "
                            "reconciler's rejection key must be updated to match")


def test_unkeyable_rejection_is_never_written(rec):
    """A rejection nothing can look up is noise, not signal."""
    assert rec.record_review_rejection(1, "", "find", _closed_pr(), "x") is False


def test_closed_unmerged_pass_runs_with_zero_merged_prs(rec, monkeypatch):
    """★ REACHABILITY. run_reconciliation used to `return` on an empty merged
    list, which would have starved this pass in exactly the window that holds
    only rejections — a lane reporting ok:true while scanning nothing."""
    seen = {"rejections": 0}
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": []})
    monkeypatch.setattr(rec, "list_closed_unmerged_brain_prs",
                        lambda d: {"ok": True, "prs": [_closed_pr()]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "_upsert_ledger", lambda *a, **k: None)
    monkeypatch.setattr(rec, "record_review_rejection",
                        lambda *a, **k: seen.__setitem__(
                            "rejections", seen["rejections"] + 1) or True)
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True
    assert rep["merged_brain_prs_in_window"] == 0
    assert seen["rejections"] == 1, "rejection pass did not run"
    assert rep["rejected"][0]["pr"] == 9001


def test_closed_unmerged_pass_credits_nothing(rec, monkeypatch):
    """A closed PR is a rejection, never fix credit: no merge mark, no merge
    outcome, no fix outcome."""
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": []})
    monkeypatch.setattr(rec, "list_closed_unmerged_brain_prs",
                        lambda d: {"ok": True, "prs": [_closed_pr()]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "_upsert_ledger", lambda *a, **k: None)
    monkeypatch.setattr(rec, "record_review_rejection", lambda *a, **k: True)
    monkeypatch.setattr(rec, "mark_proposal_merged", _Boom())
    monkeypatch.setattr(rec, "mark_merge_outcome", _Boom())
    monkeypatch.setattr(rec, "record_outcome", _Boom())
    monkeypatch.setattr(rec, "record_review_decision", _Boom())
    rep = rec.run_reconciliation(dry=False)
    assert rep["ok"] is True and len(rep["rejected"]) == 1


def test_closed_unmerged_dry_run_never_writes(rec, monkeypatch):
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": []})
    monkeypatch.setattr(rec, "list_closed_unmerged_brain_prs",
                        lambda d: {"ok": True, "prs": [_closed_pr()]})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "record_review_rejection", _Boom())
    monkeypatch.setattr(rec, "_upsert_ledger", _Boom())
    rep = rec.run_reconciliation(dry=True)
    assert rep["ok"] is True and len(rep["rejected"]) == 1


def test_closed_unmerged_github_error_does_not_read_as_no_rejections(
        rec, monkeypatch):
    """FAIL CLOSED — a GitHub error must surface, not vanish into ok:true with
    an empty rejection list."""
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": []})
    monkeypatch.setattr(rec, "list_closed_unmerged_brain_prs",
                        lambda d: {"ok": False, "error": "403:rate", "prs": []})
    monkeypatch.setattr(rec, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    rep = rec.run_reconciliation(dry=False)
    assert rep["rejections_error"] == "403:rate"
    assert "closed_unmerged_brain_prs_in_window" not in rep
    assert rep.get("rejected") is None


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _install_fake_requests(monkeypatch, payload):
    """Inject a fake `requests` so the lister's own filtering is exercised.
    Every run-loop test above monkeypatches the lister OUT, so without this
    the merged/unmerged filter is never executed by any test at all."""
    mod = types.ModuleType("requests")
    calls = []

    def get(url, **kw):
        calls.append(url)
        return _FakeResp(payload if len(calls) == 1 else [])

    mod.get = get
    monkeypatch.setitem(sys.modules, "requests", mod)
    return calls


def _gh(number, ref, merged_at, closed_at="2026-09-17T00:00:00Z"):
    return {"number": number, "head": {"ref": ref},
            "title": f"pr {number}", "html_url": f"https://x/pull/{number}",
            "merged_at": merged_at, "closed_at": closed_at,
            "updated_at": closed_at, "created_at": "2026-09-16T00:00:00Z",
            "user": {"login": "azmartone67"}}


def test_closed_unmerged_lister_excludes_merged_prs(rec, monkeypatch):
    """★ A MERGED PR MUST NEVER ENTER THE REJECTION LIST. Recording a merged
    PR as a rejection would not merely lose signal — it would invert it, and
    two rejections on one (label, find) suppress future proposals."""
    monkeypatch.setattr(rec, "_token", lambda: "tok")
    monkeypatch.setattr(rec, "_now",
                        lambda: dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc))
    _install_fake_requests(monkeypatch, [
        _gh(1, "brain-spec/a-1-aa", "2026-09-17T00:00:00Z"),   # MERGED  → out
        _gh(2, "brain-spec/b-2-bb", None),                      # closed  → in
        _gh(3, "brain/autofix-c-3-cc", None),                   # closed  → in
        _gh(4, "feature/not-brain", None),                      # foreign → out
        _gh(5, "brain/autofix-revert-d-5-dd", None),            # revert  → out
    ])
    out = rec.list_closed_unmerged_brain_prs(30)
    assert out["ok"] is True
    assert sorted(p["number"] for p in out["prs"]) == [2, 3]
    assert all(p["merged_at"] is None for p in out["prs"])


def test_closed_unmerged_lister_fails_closed_on_http_error(rec, monkeypatch):
    monkeypatch.setattr(rec, "_token", lambda: "tok")
    mod = types.ModuleType("requests")

    class _Bad:
        status_code = 503
        text = "upstream"

    mod.get = lambda url, **kw: _Bad()
    monkeypatch.setitem(sys.modules, "requests", mod)
    out = rec.list_closed_unmerged_brain_prs(30)
    assert out["ok"] is False and out["prs"] == []
