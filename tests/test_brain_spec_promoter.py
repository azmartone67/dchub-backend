"""Spec→code promotion (2026-07-01): grounded-path extraction + safe-run rails."""
import routes.brain_spec_promoter as sp


def test_extract_candidate_paths_orders_and_dedupes():
    text = ("The fix belongs in routes/brain_autopilot.py near _execute_action; "
            "see also routes/brain_autopilot.py (again) and tests/test_x.py. "
            "Ignore ../etc/passwd and main.py too.")
    got = sp._extract_candidate_paths(text)
    assert got[0] == "routes/brain_autopilot.py"
    assert "tests/test_x.py" in got
    assert "main.py" in got
    assert len([p for p in got if p == "routes/brain_autopilot.py"]) == 1
    assert all(".." not in p for p in got)


def test_promote_skips_marked_and_respects_cap(monkeypatch):
    prs = [
        {"number": 10, "title": "[brain-spec] a", "draft": True,
         "head": {"ref": "b10"}, "body": "edit routes/brain_models.py"},
        {"number": 11, "title": "[brain-spec] b", "draft": True,
         "head": {"ref": "b11"}, "body": "edit routes/brain_models.py"},
        {"number": 12, "title": "unrelated", "draft": True, "head": {"ref": "x"}},
    ]

    class _R:
        status_code = 200
        def json(self):
            return prs

    import routes.brain_pr_opener as po
    monkeypatch.setattr(po, "_gh", lambda m, p, body=None: _R())
    monkeypatch.setattr(po, "_get_file",
                        lambda path, ref="main": ("content", "sha"))
    # PR 10 already promoted; PR 11 fresh.
    monkeypatch.setattr(sp, "_marker_get",
                        lambda n: "promoted:url" if n == 10 else None)
    monkeypatch.setattr(sp, "_spec_body_for_pr",
                        lambda pr: (pr.get("body") or "", None))

    out = sp.promote_spec_prs(dry_run=True, cap=2)
    assert out["ok"] is True and out["dry_run"] is True
    promoted = {p["pr"] for p in out["promoted"]}
    assert promoted == {11}, out
    skipped = {s["pr"]: s["reason"] for s in out["skipped"]}
    assert 10 in skipped and skipped[10].startswith("marker:")


def test_promote_refusal_sets_marker_not_retried(monkeypatch):
    prs = [{"number": 20, "title": "[brain-spec] c", "draft": True,
            "head": {"ref": "b20"}, "body": "edit routes/brain_models.py"}]

    class _R:
        status_code = 200
        def json(self):
            return prs

    import routes.brain_pr_opener as po
    monkeypatch.setattr(po, "_gh", lambda m, p, body=None: _R())
    monkeypatch.setattr(po, "_get_file",
                        lambda path, ref="main": ("content", "sha"))
    monkeypatch.setattr(sp, "_marker_get", lambda n: None)
    monkeypatch.setattr(sp, "_spec_body_for_pr",
                        lambda pr: (pr.get("body") or "", None))
    markers = {}
    monkeypatch.setattr(sp, "_marker_set",
                        lambda n, v: markers.__setitem__(n, v))

    import routes.brain_guardrails as bg
    monkeypatch.setattr(bg, "draft_and_open_pr",
                        lambda **kw: {"ok": True, "acted": False,
                                      "refused": True, "rationale": "too broad"})
    out = sp.promote_spec_prs(dry_run=False, cap=2)
    assert out["refused"] and out["refused"][0]["pr"] == 20
    assert markers.get(20) == "refused"
    assert out["promoted"] == []


def test_extract_falls_back_to_module_tokens():
    text = ("Approve investigating and fixing the shared mcp_funnel "
            "response/lifecycle handler plus a fence in HEALTH_BASELINE.")
    got = sp._extract_candidate_paths(text)
    assert "routes/mcp_funnel.py" in got, got


def test_explicit_paths_rank_before_module_tokens():
    text = "Fix routes/brain_models.py — the mcp_funnel handler drops keys."
    got = sp._extract_candidate_paths(text)
    assert got[0] == "routes/brain_models.py"
    assert "routes/mcp_funnel.py" in got


def test_distill_strips_self_defeating_boilerplate():
    doc = ("# Brain proposal — x\n\n> filed here as a spec for a human to "
           "implement (or close). **Draft PR — a human merges.**\n\n"
           "## The approved recommendation\n\nSwap the cache key to include "
           "the tool name in routes/mcp_funnel.py.\n\n## Human checklist\n\n- [ ] x\n")
    got = sp._distill_directive(doc)
    assert got.startswith("Swap the cache key")
    assert "human to implement" not in got
    assert "checklist" not in got.lower()
