#!/usr/bin/env python3
"""tests/test_spec_debt_issues.py — the problem key, the ledger walk, the plan.

NO NETWORK. Real data where it matters: the 171 spec-debt issue titles open on
2026-09-12, and real docs from docs/brain-proposals/ copied into
tests/fixtures/spec_debt/ so a later ledger sweep cannot change what is asserted.

MEASURED 2026-09-12: 171 open spec-debt issues, 0 ever closed. They were 71
problems filed once per merged PR, and 13 of the 14 whose ledger doc read
CLOSED had only been closed as a copy of a doc that was still OPEN.
"""
import importlib.util
import json
import os
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "spec_debt")
ISO = "[spec-debt] inv #{n}: iso_metric_count_zero_24h (observed at: grid_data: iso={iso}). What is"


@pytest.fixture(scope="module")
def sdi():
    spec = importlib.util.spec_from_file_location(
        "spec_debt_issues", os.path.join(ROOT, "scripts", "spec_debt_issues.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _titles():
    with open(os.path.join(FIX, "open_titles_2026-09-12.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _doc(prefix):
    names = [n for n in os.listdir(os.path.join(FIX, "docs")) if n.startswith(prefix)]
    assert len(names) == 1, (prefix, names)
    return names[0]


@pytest.fixture
def corpus(tmp_path):
    d = tmp_path / "brain-proposals"
    shutil.copytree(os.path.join(FIX, "docs"), d)
    return d


def _issue(number, title, pr, comment_prs=()):
    return {
        "number": number,
        "title": title,
        "body": ("Merged spec left its human checklist unchecked.\n\n"
                 f"<sub>spec-debt-for-pr-{pr} · brain-spec-debt-tracker</sub>\n"),
        "comments": [{"body": f"<sub>spec-debt-for-pr-{p} · brain-spec-debt-tracker</sub>"}
                     for p in comment_prs],
    }


def _open(d, name):
    (d / name).write_text("## Human checklist\n\n- [ ] Implement + verify\n")


def _closed(d, name):
    (d / name).write_text("## Human checklist\n\n- [x] Implement + verify\n")


def _docs(**by_pr):
    return {int(k[2:]): (None if v is None else [f"docs/brain-proposals/{n}" for n in v])
            for k, v in by_pr.items()}


# ── which problem ─────────────────────────────────────────────────────────

def test_the_real_backlog_is_71_problems_not_171_issues(sdi):
    rows = _titles()
    assert len(rows) == 171
    groups = {}
    for r in rows:
        groups.setdefault(sdi.class_key(r["title"]), []).append(r["number"])
    assert "" not in groups
    assert len(groups) == 71
    assert sorted((len(v) for v in groups.values()), reverse=True)[:4] == [29, 15, 12, 11]
    assert sum(1 for v in groups.values() if len(v) > 1) == 23

    key = {r["number"]: sdi.class_key(r["title"]) for r in rows}
    assert key[4376] == key[2699] == "iso_metric_count_zero_24h"
    assert key[4374] != key[4376], "_dropped is a different detector from _zero_24h"
    assert key[3788] == key[2948], "a qa_critical prefix cuts the title 12 characters earlier"
    assert key[3785] == key[3242], "a qa_major prefix must not split the story-link class"
    audits = [k for k in groups if k.startswith("prose:audit_")]
    assert len(audits) >= 10 and all(len(groups[k]) == 1 for k in audits), (
        "distinct audit findings must never fold together")


def test_the_key_is_the_detector_not_the_target(sdi):
    a = "[spec-debt] inv #100458: operator_profile_gap:Equinix (observed at: /operators/equinix). What i"
    b = "[spec-debt] agenda #100260: [reliability] Brain finding: operator_profile_gap:Digital Realty @ /op"
    assert sdi.class_key(a) == sdi.class_key(b) == "operator_profile_gap"
    assert sdi.class_key(ISO.format(n=1, iso="EPE")) != sdi.class_key(
        "[spec-debt] inv #2: iso_metric_count_dropped (observed at: grid_data: iso=EPE)")


def test_a_spec_pr_and_the_issue_filed_for_it_share_a_key(sdi):
    """The tracker titles its issue `[spec-debt] ${PR_TITLE#\\[*\\] }`, so the
    key it computes from the PR title must equal the one the reconciler
    computes from the issue title — for every producer's prefix."""
    for r in _titles():
        rest = r["title"][len("[spec-debt] "):]
        for prefix in ("[brain-spec] ", "[brain-l6 strategic-draft] ", "[brain-l5 draft] "):
            assert sdi.class_key(prefix + rest) == sdi.class_key(r["title"]), r["title"]


def test_a_title_with_nothing_in_it_never_folds(sdi, tmp_path):
    assert sdi.class_key("") == "" and sdi.class_key("[spec-debt] ") == ""
    _open(tmp_path, "a.md")
    issues = [_issue(1, "[spec-debt] ", 11), _issue(2, "[spec-debt] ", 12)]
    p = sdi.plan(issues, _docs(pr11=["a.md"], pr12=["a.md"]), str(tmp_path))
    assert p["folds"] == []


# ── the ledger walk, on real docs ─────────────────────────────────────────

def test_a_copy_closed_in_favour_of_an_open_doc_is_not_done(sdi, corpus):
    """agenda-100193 reads CLOSED — every box checked — but it was closed as a
    class member of agenda-74, which is still open. Counting it as done is
    exactly how an obligation would lose its only issue."""
    member = _doc("agenda-100193-")
    r = sdi.resolve_doc(member, str(corpus))
    assert r["state"] == "open"
    assert r["terminal"] == _doc("agenda-74-") and r["chain"] == [member, r["terminal"]]


def test_an_exact_refile_counts_as_the_doc_it_copies(sdi, corpus):
    r = sdi.resolve_doc(_doc("inv-100145-"), str(corpus))
    assert r["state"] == "open" and r["terminal"] == _doc("inv-100075-")


def test_an_adjudicated_doc_is_closed(sdi, corpus):
    r = sdi.resolve_doc(_doc("agenda-100196-"), str(corpus))
    assert r["state"] == "closed" and r["terminal"] == r["doc"]
    assert "condition already adjudicated" in r["evidence"]


def test_closing_the_target_closes_its_copies(sdi, corpus):
    canon = corpus / _doc("agenda-74-")
    canon.write_text(canon.read_text().replace("- [ ]", "- [x]"))
    assert sdi.resolve_doc(_doc("agenda-100193-"), str(corpus))["state"] == "closed"


def test_a_hand_off_to_a_doc_not_in_the_corpus_is_not_closed(sdi, corpus):
    os.remove(corpus / _doc("agenda-74-"))
    assert sdi.resolve_doc(_doc("agenda-100193-"), str(corpus))["state"] == "missing"


def test_a_hand_off_loop_is_not_closed(sdi, tmp_path):
    for me, other in (("a.md", "b.md"), ("b.md", "a.md")):
        (tmp_path / me).write_text(
            "## Triage — 2026-09-13 — CLOSED, exact re-file\n\n"
            f"- [x] Or discard this PR if superseded / not worth it — closed as an exact re-file of {other}\n")
    assert sdi.resolve_doc("a.md", str(tmp_path))["state"] == "cycle"


def test_a_file_named_in_the_recommendation_is_not_a_hand_off(sdi, tmp_path):
    (tmp_path / "a.md").write_text(
        "## The approved recommendation\n\nAct on README.md first.\n\n"
        "## Human checklist\n\n- [x] Implement + verify\n")
    r = sdi.resolve_doc("a.md", str(tmp_path))
    assert r["state"] == "closed" and r["terminal"] == "a.md"


# ── the plan ──────────────────────────────────────────────────────────────

def test_copies_fold_into_the_oldest_issue_and_carry_their_prs(sdi, tmp_path):
    for n in ("a.md", "b.md", "c.md"):
        _open(tmp_path, n)
    issues = [_issue(4376, ISO.format(n=3, iso="EU_BE"), 3),
              _issue(2699, ISO.format(n=1, iso="LGEE"), 1),
              _issue(3588, ISO.format(n=2, iso="EU_IT_SICI"), 2)]
    p = sdi.plan(issues, _docs(pr1=["a.md"], pr2=["b.md"], pr3=["c.md"]), str(tmp_path))
    assert p["closes"] == []
    [f] = p["folds"]
    assert f["canonical"] == 2699
    assert [d["number"] for d in f["duplicates"]] == [3588, 4376]
    carried = sdi.tracked_prs({"body": "", "comments": [{"body": f["canonical_comment"]}]})
    assert sorted(carried) == [2, 3], "the surviving issue must carry every copy's spec PR"
    assert "grid_data: iso=EU_BE" in f["canonical_comment"]
    assert all("Folded into #2699" in d["comment"] for d in f["duplicates"])


def test_an_issue_closes_only_when_every_spec_it_tracks_is_closed(sdi, tmp_path):
    _closed(tmp_path, "a.md")
    _open(tmp_path, "b.md")
    issue = _issue(10, ISO.format(n=1, iso="X"), 1, comment_prs=(2,))
    docs = _docs(pr1=["a.md"], pr2=["b.md"])
    p = sdi.plan([issue], docs, str(tmp_path))
    assert p["closes"] == [] and "b.md" in p["kept"][0]["reason"]
    _closed(tmp_path, "b.md")
    p = sdi.plan([issue], docs, str(tmp_path))
    assert [c["number"] for c in p["closes"]] == [10]
    assert "a.md" in p["closes"][0]["comment"] and "b.md" in p["closes"][0]["comment"]


@pytest.mark.parametrize("docs,corpus_name,body", [
    ({"pr1": None}, "", None),                      # the PR's files could not be listed
    ({"pr1": ["gone.md"]}, "", None),               # its doc is not in the corpus
    ({"pr1": []}, "", None),                        # it landed no ledger doc
    ({"pr1": ["a.md"]}, "nope", None),              # the corpus is unreadable
    ({"pr1": ["a.md"]}, "", "no marker in this body"),
])
def test_anything_unreadable_keeps_the_issue_open(sdi, tmp_path, docs, corpus_name, body):
    _closed(tmp_path, "a.md")
    issue = _issue(10, ISO.format(n=1, iso="X"), 1)
    if body is not None:
        issue["body"] = body
    p = sdi.plan([issue], _docs(**docs), str(tmp_path / corpus_name) if corpus_name else str(tmp_path))
    assert p["closes"] == [] and [k["number"] for k in p["kept"]] == [10]


def test_folding_does_not_need_the_ledger(sdi, tmp_path):
    issues = [_issue(1, ISO.format(n=1, iso="A"), 1), _issue(2, ISO.format(n=2, iso="B"), 2)]
    p = sdi.plan(issues, _docs(pr1=[], pr2=[]), str(tmp_path / "nope"))
    assert p["corpus_ok"] is False
    assert [(f["canonical"], [d["number"] for d in f["duplicates"]]) for f in p["folds"]] == [(1, [2])]


def test_the_cap_bounds_every_close_and_defers_the_rest(sdi, tmp_path):
    _closed(tmp_path, "done.md")
    for n in "abcd":
        _open(tmp_path, f"{n}.md")
    issues = [_issue(5, "[spec-debt] inv #5: stripe_webhook_lag (observed at: table:x)", 5)]
    issues += [_issue(i, ISO.format(n=i, iso=str(i)), i) for i in (1, 2, 3, 4)]
    docs = _docs(pr5=["done.md"], pr1=["a.md"], pr2=["b.md"], pr3=["c.md"], pr4=["d.md"])
    p = sdi.plan(issues, docs, str(tmp_path), max_closes=2)
    assert [c["number"] for c in p["closes"]] == [5]
    assert [d["number"] for f in p["folds"] for d in f["duplicates"]] == [2]
    assert p["deferred"] == 2


def test_a_fold_already_carried_over_is_not_announced_twice(sdi, tmp_path):
    _open(tmp_path, "a.md")
    _open(tmp_path, "b.md")
    canon = _issue(1, ISO.format(n=1, iso="A"), 1, comment_prs=(2,))
    dup = _issue(2, ISO.format(n=2, iso="B"), 2)
    [f] = sdi.plan([canon, dup], _docs(pr1=["a.md"], pr2=["b.md"]), str(tmp_path))["folds"]
    assert f["canonical_comment"] is None
    assert [d["number"] for d in f["duplicates"]] == [2]


def test_an_issue_the_ledger_closes_does_not_survive_a_fold(sdi, tmp_path):
    _closed(tmp_path, "a.md")
    _open(tmp_path, "b.md")
    _open(tmp_path, "c.md")
    issues = [_issue(1, ISO.format(n=1, iso="A"), 1), _issue(2, ISO.format(n=2, iso="B"), 2),
              _issue(3, ISO.format(n=3, iso="C"), 3)]
    p = sdi.plan(issues, _docs(pr1=["a.md"], pr2=["b.md"], pr3=["c.md"]), str(tmp_path))
    assert [c["number"] for c in p["closes"]] == [1]
    assert [(f["canonical"], [d["number"] for d in f["duplicates"]]) for f in p["folds"]] == [(2, [3])]


# ── carrying it out ───────────────────────────────────────────────────────

def _fake_gh(monkeypatch, sdi, fail_when):
    calls = []

    def fake(args, *, stdin=None):
        calls.append(list(args))
        if fail_when(list(args)):
            raise RuntimeError("simulated gh failure")
        return ""

    monkeypatch.setattr(sdi, "_gh", fake)
    return calls


def test_a_copy_is_never_closed_unless_its_prs_were_carried_over(sdi, monkeypatch):
    calls = _fake_gh(monkeypatch, sdi, lambda a: a[:3] == ["issue", "comment", "1"])
    p = {"closes": [], "folds": [{"key": "k", "canonical": 1, "canonical_comment": "carry",
                                  "duplicates": [{"number": 2, "comment": "folded"}]}]}
    done = sdi.apply(p, "o/r", pause=0)
    assert done["folded"] == [] and done["errors"]
    assert not any(a[:2] == ["api", "-X"] for a in calls), "closed a copy whose PRs were never carried"


def test_a_duplicate_close_falls_back_to_not_planned_after_commenting(sdi, monkeypatch):
    calls = _fake_gh(monkeypatch, sdi, lambda a: "state_reason=duplicate" in a)
    p = {"closes": [], "folds": [{"key": "k", "canonical": 1, "canonical_comment": None,
                                  "duplicates": [{"number": 2, "comment": "folded"}]}]}
    done = sdi.apply(p, "o/r", pause=0)
    assert done["folded"] == [2] and not done["errors"]
    assert any("state_reason=not_planned" in a for a in calls)
    first_comment = next(i for i, a in enumerate(calls) if a[:2] == ["issue", "comment"])
    first_close = next(i for i, a in enumerate(calls) if a[:2] == ["api", "-X"])
    assert first_comment < first_close, "the reason must be on the issue before it closes"
