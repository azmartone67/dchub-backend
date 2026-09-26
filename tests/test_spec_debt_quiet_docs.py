"""spec_debt_issues quiet-docs: tick a spec doc only when EVERY target it stands
for provably stopped firing.

NO NETWORK. Evidence is handed in; the corpus is a tmp dir.

2026-09-26: the quiet arm closed spec-debt ISSUES but nothing ticked a DOC, so
/api/v1/brain/spec-debt counted the same open docs whatever the ledger proved.
A class-collapse canonical stands for its whole roster, so one member still
firing must hold it open.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "spec_debt_issues", os.path.join(ROOT, "scripts", "spec_debt_issues.py"))
s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s)

BOXES = ("- [ ] Confirm this is still worth doing\n"
         "- [ ] Scope it to a concrete change (file(s) + approach)\n"
         "- [ ] Implement + verify\n"
         "- [ ] Or close this PR if superseded / not worth it\n")
ISSUE = "iso_metric_count_dropped"


def _doc(region, extra="", boxes=BOXES):
    return (f"# Brain proposal — {ISSUE} (observed at: grid_data: iso={region}). "
            f"What is the root cause, and is there a single fix?\n\n_Filed 2026-09-03_\n\n"
            f"{extra}## Human checklist\n\n{boxes}")


def _roster(*members):
    lines = "".join(f"- `grid_data: iso={r}` — was `{f}` (filed 2026-09-03)\n" for r, f in members)
    return f"## Rolled-up targets — class `{ISSUE}` (class collapse, 2026-09-26)\n\n{lines}\n"


def _ev(*regions, verdict="quiet_proven"):
    return {s.evidence_key(ISSUE, f"grid_data: iso={r}"):
            {"verdict": verdict, "reason": f"{r} reason", "detector_fn": "check_iso_metric_dropped"}
            for r in regions}


@pytest.fixture
def corpus(tmp_path):
    def write(name, text):
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path, write


def test_a_single_quiet_doc_is_ticked_and_then_reads_CLOSED(corpus):
    d, w = corpus
    w("a.md", _doc("A"))
    p = s.quiet_doc_plan(str(d), _ev("A"))
    assert [t["doc"] for t in p["ticks"]] == ["a.md"]
    text = s.tick_doc_text((d / "a.md").read_text(), p["ticks"][0]["targets"], "2026-09-27")
    mod = s._ledger()
    assert mod.classify_doc_text(text) == mod.CLOSED
    (d / "a.md").write_text(text)
    r = s.resolve_doc("a.md", str(d))
    assert r["state"] == "closed" and r["terminal"] == "a.md", "the tick must not read as a hand-off"
    assert "quiet_proven" in text and "A reason" in text


def test_CONTROL_a_firing_doc_is_held(corpus):
    d, w = corpus
    w("a.md", _doc("A"))
    p = s.quiet_doc_plan(str(d), _ev("A", verdict="firing"))
    assert p["ticks"] == [] and "firing" in p["held"][0]["reason"]


@pytest.mark.parametrize("verdict", ["quiet_unproven", "unmeasured"])
def test_short_of_proof_is_held(corpus, verdict):
    d, w = corpus
    w("a.md", _doc("A"))
    assert s.quiet_doc_plan(str(d), _ev("A", verdict=verdict))["ticks"] == []


def test_a_canonical_is_held_while_ONE_roster_member_fires(corpus):
    d, w = corpus
    w("canon.md", _doc("A", _roster(("A", "canon.md"), ("B", "b.md"), ("C", "c.md"))))
    w("b.md", _doc("B").replace("- [ ]", "- [x]"))
    w("c.md", _doc("C").replace("- [ ]", "- [x]"))
    ev = {**_ev("A", "B"), **_ev("C", verdict="firing")}
    p = s.quiet_doc_plan(str(d), ev)
    assert p["ticks"] == [] and p["held"][0]["doc"] == "canon.md"
    assert "1 of 3" in p["held"][0]["reason"]
    p = s.quiet_doc_plan(str(d), _ev("A", "B", "C"))
    assert [(t["doc"], len(t["targets"])) for t in p["ticks"]] == [("canon.md", 3)]


def test_a_roster_member_with_no_nameable_target_blocks_the_canonical(corpus):
    d, w = corpus
    w("canon.md", _doc("A", _roster(("A", "canon.md"), ("B", "prose.md"))))
    w("prose.md", "# Brain proposal — 5 of 8 published story link(s) are dead — observed "
                  "from the none seat on media: x\n\n" + BOXES.replace("- [ ]", "- [x]"))
    assert s.doc_targets("canon.md", str(d)) is None
    assert s.quiet_doc_plan(str(d), _ev("A"))["ticks"] == []


def test_a_non_template_checklist_is_held_not_rewritten(corpus):
    d, w = corpus
    w("a.md", _doc("A", boxes=BOXES + "- [ ] Owner signs off on the migration\n"))
    p = s.quiet_doc_plan(str(d), _ev("A"))
    assert p["ticks"] == [] and "not the 4-line template" in p["held"][0]["reason"]


def test_the_cap_defers_the_rest(corpus):
    d, w = corpus
    for r in "ABCDE":
        w(f"{r}.md", _doc(r))
    p = s.quiet_doc_plan(str(d), _ev(*"ABCDE"), max_ticks=2)
    assert len(p["ticks"]) == 2 and p["deferred"] == 3


def test_only_restricts_the_plan(corpus):
    d, w = corpus
    w("a.md", _doc("A"))
    w("b.md", _doc("B"))
    p = s.quiet_doc_plan(str(d), _ev("A", "B"), only={"b.md"})
    assert [t["doc"] for t in p["ticks"]] == ["b.md"]
