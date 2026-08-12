"""tests/test_radar_unmeasured_is_not_all_clear.py — a dead radar is not good
news (2026-08-12).

`brain_narrative._fetch_findings()` returned `[]` on ANY failure, and its caller
read `if not findings:` as "everything's clean" and published a narrative saying
so. The old code even admitted the ambiguity in the text it published:

    "All detectors clean (or radar cold-start in progress)."

It could not tell the two apart, so it said both. That is the most expensive
shape of this bug in the codebase, because the output is a public surface read
by humans rather than an internal metric — and the radar's own docstring records
a 20s+ cold start, so the failing path was reachable in normal operation.

`brain_mirror` had the same collapse one level up: `if not isinstance(findings,
list): findings = []` turned "could not look" into "looked, saw nothing", then
graded the brain's self-assessment against a world it never observed.

Guards:
  (1) ALL-CLEAR-ON-DEAD-RADAR — the narrative publishes a clean bill of health
      when the radar was unreachable.
  (2) COLLAPSE — _fetch_findings stops distinguishing None from [].
  (3) SILENT MIRROR — the mirror stops surfacing radar_unmeasured, so a consumer
      cannot tell an empty world from an unobserved one.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_radar_unmeasured_is_not_all_clear.py -v
"""
from __future__ import annotations

import importlib
import pathlib
import re


def _code_only(path: pathlib.Path) -> str:
    """Source with comments stripped.

    ★These guards assert that old, wrong strings are GONE — and the fixes that
    removed them quote those same strings in explanatory comments, because a
    comment naming the bug is how the next person understands the change. The
    first cut of this file therefore failed on correct code, tripping over its
    own documentation. Strip comments, then assert."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _narrative():
    return importlib.import_module("routes.brain_narrative")


def test_fetch_findings_returns_none_when_the_probe_fails(monkeypatch):
    """★(2) None means UNMEASURED. [] means measured-and-empty. Collapsing them
    is the whole bug."""
    nb = _narrative()
    import util.internal_fetch as inf
    monkeypatch.setattr(inf, "probe", lambda *a, **k: {
        "path": "/x", "ok": False, "data": {}, "reason": "HTTP 502",
        "status": 502, "empty": False})
    assert nb._fetch_findings() is None


def test_fetch_findings_returns_a_list_when_the_probe_succeeds(monkeypatch):
    nb = _narrative()
    import util.internal_fetch as inf
    monkeypatch.setattr(inf, "probe", lambda *a, **k: {
        "path": "/x", "ok": True, "data": {"findings": [{"issue": "a"}]},
        "reason": None, "status": 200, "empty": False})
    assert nb._fetch_findings() == [{"issue": "a"}]
    monkeypatch.setattr(inf, "probe", lambda *a, **k: {
        "path": "/x", "ok": True, "data": {"findings": []},
        "reason": None, "status": 200, "empty": False})
    assert nb._fetch_findings() == []


def test_the_narrative_never_publishes_an_all_clear_it_did_not_measure():
    """★(1) THE ONE THAT MATTERS. Read as source: the unmeasured branch must
    exist, must be distinguishable, and must not claim detectors are clean."""
    src = _code_only(_ROOT / "routes" / "brain_narrative.py")
    assert "if findings is None:" in src, \
        "the narrative no longer distinguishes an unreachable radar"
    assert "radar_unmeasurable" in src, \
        "the unmeasured case lost its explicit marker"
    assert "NOT an all-clear" in src, \
        "the unmeasured narrative no longer says it is not an all-clear"
    # The old ambiguous sentence must never come back.
    assert "or radar cold-start in progress" not in src, \
        "the narrative is publishing the ambiguous all-clear text again"


def test_the_clean_branch_only_fires_on_a_real_measurement():
    src = _code_only(_ROOT / "routes" / "brain_narrative.py")
    i_none = src.index("if findings is None:")
    i_elif = src.index("elif not findings:")
    assert i_none < i_elif, \
        "the empty-findings branch now precedes the unmeasured branch — an " \
        "unreachable radar would take the all-clear path again"


def test_the_mirror_surfaces_that_it_could_not_look():
    """★(3) Without this the mirror grades the brain against an empty world it
    never observed, and the consumer cannot tell."""
    src = _code_only(_ROOT / "routes" / "brain_mirror.py")
    assert "radar_unmeasured = findings is None" in src, \
        "the mirror no longer detects an unmeasured radar"
    assert '"radar_unmeasured": radar_unmeasured' in src, \
        "the mirror computes radar_unmeasured but does not publish it"


def test_layer7_treats_an_unreachable_memory_endpoint_as_unknown():
    """A non-200 used to become {}, which flowed into `top_recurring_issues or
    []` and read as 'nothing recurring to propose against'."""
    src = _code_only(_ROOT / "routes" / "brain_layer7_evolving.py")
    assert "util.internal_fetch" in src
    assert 'if not env["ok"]:' in src, \
        "L7 no longer separates an unreachable memory endpoint from an empty one"
    assert "mem = r.json() if r.ok else {}" not in src, \
        "L7 re-grew the non-200-becomes-empty collapse"
