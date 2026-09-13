#!/usr/bin/env python3
"""tests/test_spec_debt_quiet_arm.py — the reconcile script's "stopped firing" arm.

NO NETWORK: the evidence endpoint and gh are replaced. Where a heading's shape
matters it comes from a real landed doc in tests/fixtures/spec_debt/docs.

An issue may be closed as "stopped firing" only when EVERY spec it tracks maps
to a brain finding the evidence endpoint judged quiet_proven, and only when the
arm is armed. Anything unreadable — no key, no answer, a non-MEASURED answer —
is never read as quiet.
"""
import importlib.util
import json
import os
import types

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX_DOCS = os.path.join(ROOT, "tests", "fixtures", "spec_debt", "docs")
WF = os.path.join(ROOT, ".github", "workflows", "spec-debt-reconcile.yml")
EQX = ("operator_profile_gap:Equinix", "/operators/equinix")
DLR = ("operator_profile_gap:Digital Realty", "/operators/digital-realty")
REPEAT = ("repeated_404_pattern", "/js/dchub-webmcp.js")
STRIPE = ("stripe_webhook_lag", "table:stripe_webhook_events")


@pytest.fixture(scope="module")
def sdi():
    spec = importlib.util.spec_from_file_location(
        "spec_debt_issues", os.path.join(ROOT, "scripts", "spec_debt_issues.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture_doc(prefix):
    [name] = [n for n in os.listdir(FIX_DOCS) if n.startswith(prefix)]
    return name


def _doc(d, name, finding):
    issue, url = finding
    (d / name).write_text(f"# Brain proposal — {issue} (observed at: {url}). What is the root "
                          "cause?\n\n## Human checklist\n\n- [ ] Implement + verify\n")


def _issue(number, finding, pr, comment_prs=()):
    return {"number": number,
            "title": f"[spec-debt] inv #{pr}: {finding[0]} (observed at: {finding[1]}). What",
            "body": f"<sub>spec-debt-for-pr-{pr} · brain-spec-debt-tracker</sub>",
            "comments": [{"body": f"<sub>spec-debt-for-pr-{p} · brain-spec-debt-tracker</sub>"}
                         for p in comment_prs]}


def _ev(verdict):
    return {"verdict": verdict, "reason": f"{verdict} for this test",
            "detector_fn": "check_operator_profile_gap"}


def _key(sdi, finding):
    return sdi.evidence_key(*finding)


# ── which finding a spec was filed for ────────────────────────────────────

def test_a_squasher_spec_names_its_finding_exactly(sdi):
    heading = sdi.doc_heading(_fixture_doc("inv-100603-"), FIX_DOCS)
    assert sdi.spec_target(heading) == {"issue": "iso_metric_count_zero_24h",
                                        "url": "grid_data: iso=EU_BE", "url_prefix": False}


def test_an_agenda_spec_names_a_finding_whose_url_may_be_cut(sdi):
    heading = sdi.doc_heading(_fixture_doc("agenda-100193-"), FIX_DOCS)
    target = sdi.spec_target(heading)
    assert target["issue"].startswith("ai_platform_crawl_drop"), (heading, target)
    assert target["url_prefix"] is True


@pytest.mark.parametrize("heading", [
    "8 of 8 published story link(s) are dead — observed from the none seat on media",
    "qa_critical A paying key receives FEWER data fields than an anonymous caller (9 vs 10)",
    "audit_H llms.txt self-contradicts on facilities and deals — re-diverged",
    "These are the measured, currently-failing critical constraints on DC Hub's agent surface",
    "",
    None,
], ids=["qa-board", "qa-intake", "audit", "actuation-prose", "empty", "no-doc"])
def test_a_spec_not_filed_from_a_finding_has_no_target(sdi, heading):
    assert sdi.spec_target(heading) is None


# ── the verdict per issue ─────────────────────────────────────────────────

def test_an_issue_is_proven_quiet_only_when_every_spec_is(sdi, tmp_path):
    _doc(tmp_path, "a.md", EQX)
    _doc(tmp_path, "b.md", DLR)
    issue = _issue(10, EQX, 1, comment_prs=(2,))
    docs = {1: ["docs/brain-proposals/a.md"], 2: ["docs/brain-proposals/b.md"]}
    ev = {_key(sdi, EQX): _ev("quiet_proven"), _key(sdi, DLR): _ev("quiet_unproven")}
    verdict = lambda: sdi.quiet_verdicts([issue], docs, str(tmp_path), ev)[0]["verdict"]
    assert verdict() == "quiet_unproven"
    ev[_key(sdi, DLR)] = _ev("firing")
    assert verdict() == "firing"
    del ev[_key(sdi, DLR)]
    assert verdict() == "unmeasured", "a spec with no evidence is not quiet"
    ev[_key(sdi, DLR)] = _ev("quiet_proven")
    assert verdict() == "quiet_proven"


def test_a_spec_that_did_not_come_from_a_finding_keeps_its_issue_unmeasured(sdi, tmp_path):
    (tmp_path / "qa.md").write_text("# Brain proposal — 8 of 8 published story link(s) are dead "
                                    "— observed from the none seat on media\n\n- [ ] x\n")
    _doc(tmp_path, "a.md", EQX)
    issue = _issue(10, EQX, 1, comment_prs=(2,))
    docs = {1: ["docs/brain-proposals/a.md"], 2: ["docs/brain-proposals/qa.md"]}
    [q] = sdi.quiet_verdicts([issue], docs, str(tmp_path), {_key(sdi, EQX): _ev("quiet_proven")})
    assert q["verdict"] == "unmeasured"


# ── the plan ──────────────────────────────────────────────────────────────

def _one(sdi, tmp_path, verdict="quiet_proven"):
    _doc(tmp_path, "a.md", EQX)
    return ([_issue(10, EQX, 1)], {1: ["docs/brain-proposals/a.md"]},
            {_key(sdi, EQX): _ev(verdict)})


def test_shadow_only_reports_what_it_would_close(sdi, tmp_path):
    issues, docs, ev = _one(sdi, tmp_path)
    p = sdi.plan(issues, docs, str(tmp_path), evidence=ev)
    assert [c["number"] for c in p["quiet"]["would_close"]] == [10]
    assert p["quiet"]["closes"] == [] and p["closes"] == []


def test_armed_closes_with_the_evidence_on_the_issue(sdi, tmp_path):
    issues, docs, ev = _one(sdi, tmp_path)
    p = sdi.plan(issues, docs, str(tmp_path), evidence=ev, close_on_quiet=True)
    [c] = p["quiet"]["closes"]
    assert c["number"] == 10 and "provably stopped firing" in c["comment"]
    assert "quiet_proven for this test" in c["comment"]


def test_without_evidence_the_quiet_arm_does_nothing(sdi, tmp_path):
    issues, docs, _ = _one(sdi, tmp_path)
    p = sdi.plan(issues, docs, str(tmp_path), close_on_quiet=True)
    assert p["quiet"] == {"read": False, "armed": True, "classified": [], "closes": [],
                          "would_close": []}


def test_quiet_closes_share_the_cap(sdi, tmp_path):
    issues, docs, ev = [], {}, {}
    for n, finding in enumerate((EQX, REPEAT, STRIPE), start=1):
        _doc(tmp_path, f"{n}.md", finding)
        issues.append(_issue(n, finding, n))
        docs[n] = [f"docs/brain-proposals/{n}.md"]
        ev[_key(sdi, finding)] = _ev("quiet_proven")
    p = sdi.plan(issues, docs, str(tmp_path), evidence=ev, close_on_quiet=True, max_closes=2)
    assert [c["number"] for c in p["quiet"]["closes"]] == [1, 2] and p["deferred"] == 1


def test_a_surviving_issue_is_judged_on_the_copies_it_absorbs(sdi, tmp_path):
    _doc(tmp_path, "a.md", EQX)
    _doc(tmp_path, "b.md", DLR)
    issues = [_issue(1, EQX, 1), _issue(2, DLR, 2)]
    docs = {1: ["docs/brain-proposals/a.md"], 2: ["docs/brain-proposals/b.md"]}
    ev = {_key(sdi, EQX): _ev("quiet_proven"), _key(sdi, DLR): _ev("firing")}
    p = sdi.plan(issues, docs, str(tmp_path), evidence=ev, close_on_quiet=True)
    assert [(f["canonical"], [d["number"] for d in f["duplicates"]]) for f in p["folds"]] == [(1, [2])]
    assert [(c["number"], c["verdict"]) for c in p["quiet"]["classified"]] == [(1, "firing")]
    assert p["quiet"]["closes"] == []


def test_apply_comments_before_it_closes_a_quiet_issue(sdi, monkeypatch):
    calls = []
    monkeypatch.setattr(sdi, "_gh", lambda args, *, stdin=None: calls.append(list(args)) or "")
    done = sdi.apply({"closes": [], "folds": [],
                      "quiet": {"closes": [{"number": 7, "comment": "c"}]}}, "o/r", pause=0)
    assert done["quiet_closed"] == [7] and not done["errors"]
    assert calls[0][:3] == ["issue", "comment", "7"]
    assert "state_reason=completed" in calls[1]


# ── the evidence read ─────────────────────────────────────────────────────

def test_no_admin_key_means_no_evidence(sdi, monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    with pytest.raises(sdi.EvidenceUnavailable, match="not set"):
        sdi.fetch_evidence([{"issue": "i", "url": "u"}])


@pytest.mark.parametrize("status,body,match", [
    (503, {"state": "UNMEASURED", "reason": "no DATABASE_URL"}, "no DATABASE_URL"),
    (200, {"state": "UNMEASURED", "reason": "x"}, "HTTP 200"),
    (401, {"ok": False, "error": "admin key required"}, "admin key required"),
    (200, "<html>stale edge page</html>", "not JSON"),
], ids=["503-unmeasured", "200-unmeasured", "401", "not-json"])
def test_anything_but_a_measured_answer_is_unavailable(sdi, monkeypatch, status, body, match):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    text = body if isinstance(body, str) else json.dumps(body)
    monkeypatch.setattr(sdi, "_curl_post_json", lambda url, headers, b, max_time=60.0: (status, text))
    with pytest.raises(sdi.EvidenceUnavailable, match=match):
        sdi.fetch_evidence([{"issue": "i", "url": "u"}])


def test_a_measured_answer_is_keyed_by_issue_and_url(sdi, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    monkeypatch.setenv("DCHUB_BACKEND_BASE", "https://origin.example/")
    seen = {}

    def fake(url, headers, body, max_time=60.0):
        seen.update(url=url, headers=headers, body=body)
        return 200, json.dumps({"state": "MEASURED",
                                "findings": [{"issue": "i", "url": "u", "verdict": "firing"}]})

    monkeypatch.setattr(sdi, "_curl_post_json", fake)
    ev = sdi.fetch_evidence([{"issue": "i", "url": "u", "url_prefix": False}])
    assert ev == {"i|u": {"issue": "i", "url": "u", "verdict": "firing"}}
    assert seen["url"] == "https://origin.example/api/v1/brain/spec-debt/finding-evidence"
    assert seen["headers"]["X-Admin-Key"] == "k" and "dchub" in seen["headers"]["User-Agent"]
    assert seen["body"] == {"findings": [{"issue": "i", "url": "u", "url_prefix": False}]}


def test_the_admin_key_goes_to_curl_on_stdin_never_argv(sdi, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(argv=argv, input=kw.get("input"))
        return types.SimpleNamespace(returncode=0, stdout='{"state": "MEASURED"}\n200', stderr="")

    monkeypatch.setattr(sdi.subprocess, "run", fake_run)
    status, _ = sdi._curl_post_json("https://o/x", {"X-Admin-Key": "sekrit"}, {"findings": []})
    assert status == 200
    assert "sekrit" not in " ".join(captured["argv"])
    assert 'header = "X-Admin-Key: sekrit"' in captured["input"]


# ── the CLI ───────────────────────────────────────────────────────────────

def _cli(sdi, monkeypatch, tmp_path, evidence):
    _doc(tmp_path, "a.md", EQX)
    issue = _issue(10, EQX, 1)
    monkeypatch.setattr(sdi, "fetch_open_issues", lambda repo, fields=None: [issue])
    monkeypatch.setattr(sdi, "fetch_pr_docs", lambda repo, prs: {1: ["docs/brain-proposals/a.md"]})
    monkeypatch.setattr(sdi, "fetch_evidence", evidence)
    plans = []
    monkeypatch.setattr(sdi, "apply", lambda p, repo, pause=1.0: plans.append(p) or
                        {"closed": [], "folded": [], "quiet_closed": [], "errors": []})
    return ["reconcile", "--repo", "o/r", "--corpus", str(tmp_path)], plans


def test_require_evidence_fails_the_run_but_not_before_the_other_arms(sdi, monkeypatch, tmp_path):
    def unavailable(targets, meta=None):
        raise sdi.EvidenceUnavailable("HTTP 503: no DATABASE_URL")

    argv, plans = _cli(sdi, monkeypatch, tmp_path, unavailable)
    summary = tmp_path / "summary.md"
    assert sdi.main(argv + ["--apply", "--summary", str(summary), "--require-evidence"]) == 2
    assert len(plans) == 1, "the other arms must still be applied"
    assert "UNAVAILABLE" in summary.read_text() and "HTTP 503" in summary.read_text()
    assert sdi.main(argv) == 0


def test_the_summary_says_how_much_ledger_exists(sdi, monkeypatch, tmp_path, capsys):
    def measured(targets, meta=None):
        meta["ledger"] = {"sweeps": 12, "first_sweep": "2026-09-13T04:00:00+00:00"}
        return {sdi.evidence_key(*EQX): _ev("quiet_unproven")}

    argv, _ = _cli(sdi, monkeypatch, tmp_path, measured)
    assert sdi.main(argv) == 0
    assert "12 sweep(s) since 2026-09-13T04:00:00+00:00" in capsys.readouterr().out


def test_only_the_env_switch_arms_closing(sdi, monkeypatch, tmp_path):
    argv, plans = _cli(sdi, monkeypatch, tmp_path,
                       lambda targets, meta=None: {sdi.evidence_key(*EQX): _ev("quiet_proven")})
    monkeypatch.delenv("SPEC_DEBT_CLOSE_ON_QUIET", raising=False)
    assert sdi.main(argv + ["--apply"]) == 0
    assert plans[-1]["quiet"]["closes"] == []
    assert [c["number"] for c in plans[-1]["quiet"]["would_close"]] == [10]
    monkeypatch.setenv("SPEC_DEBT_CLOSE_ON_QUIET", "1")
    assert sdi.main(argv + ["--apply"]) == 0
    assert [c["number"] for c in plans[-1]["quiet"]["closes"]] == [10]


def test_the_workflow_reads_evidence_with_the_secret_and_arms_only_by_variable():
    with open(WF, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    [step] = [s for s in wf["jobs"]["reconcile"]["steps"]
              if "scripts/spec_debt_issues.py" in (s.get("run") or "")]
    assert step["env"]["DCHUB_ADMIN_KEY"] == "${{ secrets.DCHUB_ADMIN_KEY }}"
    assert step["env"]["SPEC_DEBT_CLOSE_ON_QUIET"] == "${{ vars.SPEC_DEBT_CLOSE_ON_QUIET }}"
    assert "--require-evidence" in step["run"]
