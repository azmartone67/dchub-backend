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
