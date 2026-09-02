"""QA REDs mint their own fix claim; a merged doc does not act on one (2026-09-02).

Measured: `media::item-links` RED for 142h (failing_since 2026-08-27T02:08Z)
while #3444 (08-31) and #3494 (09-01) — doc-only [brain-spec] PRs drafted from
QA-derived investigations — were each credited as its fix. Three guards:
  (a) claim_ledger `qa:` scheme reads the board key itself (RED -> refuted)
  (b) brain_qa_superuser_intake mints ONE fix claim per seeded RED
  (c) brain_merge_reconciler records a QA-origin spec PR acted=False, no credit
DB-free: every reader is injected. Never imports main.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cl = pytest.importorskip("routes.claim_ledger")  # noqa: E402
qi = pytest.importorskip("routes.brain_qa_superuser_intake")  # noqa: E402
rec = pytest.importorskip("routes.brain_merge_reconciler")  # noqa: E402

KEY = "media::item-links#9e086c"


def _f(key=KEY, verdict="RED", severity="major", fault=False):
    return {"key": key, "verdict": verdict, "severity": severity,
            "instrument_fault": fault, "title": "6 of 8 story links dead",
            "surface": "media", "failing_since": "2026-08-27T02:08:00+00:00"}


def _board(findings=None, canary=True):
    return {"canary_fired": canary, "generated_at": "2026-09-02T00:20:00+00:00",
            "findings": findings if findings is not None else [_f()]}


# ── (a) the qa: scheme ───────────────────────────────────────────────────

def test_qa_metric_parses_and_validates():
    # Kills: dropping "qa" from _SCHEMES (the intake's claims would be REFUSED
    # by validate_claim and nothing would ever judge a RED).
    assert cl.parse_metric(f"qa:{KEY} verdict") == ("qa", KEY, "verdict")
    assert cl.validate_claim("fix", f"qa:{KEY}", "reads PASS",
                             f"qa:{KEY} verdict", "== PASS", 168, None) is None


def test_a_red_key_on_the_board_is_measured_as_red():
    # Kills: returning None for a present key (the claim would sit
    # `unobserved` forever — exactly the silence that let a doc "fix" it).
    val, ev = cl.resolve_qa_board(KEY, "verdict", _board())
    assert val == "RED" and ev["failing_since"] == "2026-08-27T02:08:00+00:00"
    assert cl.judge(val, "== PASS") == "refuted"


def test_a_pass_confirms_and_absence_is_unmeasured():
    val, _ = cl.resolve_qa_board(KEY, None, _board([_f(verdict="PASS")]))
    assert cl.judge(val, "== PASS") == "confirmed"
    # key not on the board / no run / canary silent -> None, never a verdict
    assert cl.resolve_qa_board("other", None, _board())[0] is None
    assert cl.resolve_qa_board(KEY, None, {})[0] is None
    val, ev = cl.resolve_qa_board(KEY, None, _board(canary=False))
    assert val is None and ev["status"] == "canary_not_fired"


def test_resolve_metric_routes_qa_without_a_cursor(monkeypatch):
    # Kills: placing the qa branch after the `cur is None` guard — L16 would
    # get "no cursor for scheme qa" and defer every fix claim.
    monkeypatch.setattr(cl, "_qa_board_latest", lambda: _board())
    val, ev = cl.resolve_metric(f"qa:{KEY} verdict", cur=None)
    assert val == "RED" and ev["qa"] == KEY


# ── (b) the intake mints the claim ───────────────────────────────────────

def test_to_findings_carries_the_board_key_and_its_claim_metric():
    out = qi.to_findings([_f()])[0]
    assert out["finding_key"] == KEY
    assert out["fix_claim_metric"] == f"qa:{KEY} verdict"


def test_fix_claim_names_the_red_as_its_own_instrument():
    # Kills: any drift between the metric the claim carries and the scheme
    # the ledger resolves.
    spec = qi.fix_claim_for(_f(), "2026-09-02T00:20:00+00:00")
    assert spec["kind"] == "fix" and spec["subject"] == f"qa:{KEY}"
    assert spec["expected_metric"] == f"qa:{KEY} verdict"
    assert spec["expected_value"] == "== PASS"
    assert spec["horizon_hours"] == 168 and spec["shipped"] is True
    assert cl.validate_claim(spec["kind"], spec["subject"], spec["statement"],
                             spec["expected_metric"], spec["expected_value"],
                             spec["horizon_hours"], spec["regime"]) is None


def test_an_instrument_fault_mints_no_fix_claim():
    # Our probe being broken is not a platform defect; "PASS" would only mean
    # we fixed the probe.
    assert qi.fix_claim_for(_f(verdict="BLIND", fault=True)) is None
    assert qi.fix_claim_for(_f(verdict="PASS")) is None


def test_mint_registers_once_per_red_and_counts_dedup():
    calls = []

    def _reg(**kw):
        calls.append(kw)
        return {"ok": True, "id": 1} if len(calls) == 1 else \
            {"ok": True, "already": True, "id": 1}
    out = qi.mint_fix_claims([_f(), _f(), _f(verdict="BLIND", fault=True)],
                             "b", register_fn=_reg)
    assert out["minted"] == 1 and out["already"] == 1 and out["ids"] == [1]
    assert len(calls) == 2 and calls[0]["kind"] == "fix"


def test_a_refused_or_broken_ledger_never_blocks_the_seed():
    out = qi.mint_fix_claims([_f()], register_fn=lambda **k: {"ok": False, "refused": True})
    assert out["refused"] == 1 and out["minted"] == 0

    def _boom(**k):
        raise RuntimeError("ledger down")
    assert qi.mint_fix_claims([_f()], register_fn=_boom)["errors"] == 1


def test_refresh_mints_the_claims_for_what_it_seeds(monkeypatch):
    # Kills: seeding a RED without registering its claim (the 142h shape).
    saved, minted = {}, []
    monkeypatch.setattr(qi, "_state_get", lambda k: None)
    monkeypatch.setattr(qi, "_state_set", lambda k, v: saved.update(v) or True)
    monkeypatch.setattr(qi, "mint_fix_claims",
                        lambda rows, as_of, register_fn=None: minted.append(
                            [r["key"] for r in rows]) or {"minted": len(rows)})
    latest = dict(_board(), generated_at=(dt.datetime.now(dt.timezone.utc)
                                          - dt.timedelta(hours=1)).isoformat())
    out = qi.refresh_snapshot(force=True, load_fn=lambda: latest)
    assert out["ok"] and out["rows"] == 1
    assert minted == [[KEY]] and saved["fix_claims"] == {"minted": 1}


# ── (c) the reconciler withholds credit from a QA-origin doc ────────────

class _Cur:
    """Answers the brain_investigations read; everything else empty."""

    def __init__(self, question):
        self.rowcount = 0
        self.q = question
        self._last = ""

    def execute(self, sql, *a, **k):
        self._last = sql

    def fetchone(self):
        return (self.q, "{}") if "brain_investigations" in self._last else None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


NOW = dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.timezone.utc)
QA_Q = ("6 of 8 published story link(s) are dead — observed from the analyst "
        "seat on media: 404 x6. What is the root cause and the smallest correct fix?")
CODE_Q = "Why does the L7 step 503 — json.loads fails on a non-JSON reply?"


def _pr(title, number=3494):
    return {"number": number, "branch": "brain-spec/inv-100405-story-links",
            "title": title, "html_url": f"https://github.com/x/y/pull/{number}",
            "merged_at": NOW - dt.timedelta(hours=48),
            "created_at": NOW - dt.timedelta(hours=72), "author": "bot"}


def test_inv_ref_and_qa_markers_are_pure():
    assert rec.investigation_ref("[brain-spec] inv #100405: 6 of 8 dead") == 100405
    assert rec.investigation_ref("chore: bump") is None
    assert rec.text_derives_from_qa_red(QA_Q) is True
    assert rec.text_derives_from_qa_red("x", "dchub://qa-superuser/k") is True
    assert rec.text_derives_from_qa_red(CODE_Q) is False


def _run(monkeypatch, question, title):
    credited = []
    monkeypatch.setattr(rec, "list_merged_brain_prs",
                        lambda d: {"ok": True, "prs": [_pr(title)]})
    monkeypatch.setattr(rec, "_conn", lambda: _Conn(_Cur(question)))
    monkeypatch.setattr(rec, "_now", lambda: NOW)
    monkeypatch.setattr(rec, "_ensure_schema", lambda cur: None)
    monkeypatch.setattr(rec, "mark_proposal_merged", lambda *a: True)
    monkeypatch.setattr(rec, "backfill_proposal_row", lambda cur, pr: 7)
    monkeypatch.setattr(rec, "record_review_decision",
                        lambda pid, label, pr: credited.append(pid) or True)
    monkeypatch.setattr(rec, "record_outcome", lambda *a: True)
    monkeypatch.setattr(rec, "_upsert_ledger", lambda *a: None)
    rep = rec.run_reconciliation()
    return rep, credited


def test_qa_origin_spec_pr_is_acted_false_and_uncredited(monkeypatch):
    # Kills: crediting the doc (record_review_decision) or grading it.
    rep, credited = _run(monkeypatch, QA_Q, "[brain-spec] inv #100405: 6 of 8 dead")
    e = rep["reconciled"][0]
    assert e["acted"] is False and e["qa_red_origin"] is True
    assert e["outcome_state"] == "spec_doc_qa_red_ungraded"
    assert credited == [] and rep["qa_red_origin_uncredited"] == 1


def test_control_a_code_derived_spec_pr_is_still_credited(monkeypatch):
    # The rule is scoped to QA-board origins; every other spec doc keeps the
    # merge credit it always had (human approval, human_reviews_30d).
    rep, credited = _run(monkeypatch, CODE_Q, "[brain-spec] inv #100416: L7 503")
    e = rep["reconciled"][0]
    assert e["qa_red_origin"] is False and e["acted"] is False
    assert e["outcome_state"] != "spec_doc_qa_red_ungraded"
    assert credited == [7] and rep["qa_red_origin_uncredited"] == 0
