"""
tests/test_l6_citation_gate.py — draft-time citation validation (2026-08-31).

NO DB, NO network, NO main import.

WHAT THIS ENFORCES
──────────────────
Rule 2 of _SYSTEM_PROMPT: "Every spec MUST cite at least 1 evidence_key
drawn from the context below. Bullshit speculation gets you fired."

Nothing ever checked. Measured across the 33 scaffolds in the tree on
2026-08-31, 34 of 92 cited keys (36%) name a root that is not a context
source at all — `competitor_signal` for `competitors`, `customer_asks` for
`feedback`, `recidivist` for `recidivism`, plus `past_lessons`,
`market_news`, `news`, `now`, which name nothing. The model invents a
plausible source name and the provenance reads as real.

WHY ONLY THE ROOT
─────────────────
The valid roots are exactly the keys of the context dict the model was
handed, so "this names no source" is a fact about the schema — provable
without any value baseline. A wrong SUBPATH under a real root is a
different animal: it could be drift, it could be a bad guess, and nothing
here can tell which. be#3448 is what happens when a probe reports
indeterminate as broken, so subpaths are deliberately not judged.

The gate withholds the SCAFFOLD, never the recommendation: the idea still
persists and still reaches the digest, it just stops putting a file in the
tree that a human later deletes by hand (be#3458, be#3459).

MUTATION CONTROLS — each must FAIL if the gate is weakened:
  A. empty/absent ctx still suppresses      -> an unreadable context would
                                               become evidence about the
                                               citations. It is not.
  B. `all(bad)` relaxed to `any(bad)`       -> one invented citation beside
                                               a real one condemns the rec.
  C. no-citations treated as invented       -> absence of evidence reported
                                               as invented evidence.
  D. subpath mismatch counted as invented   -> the be#3448 failure, rebuilt:
                                               indeterminate read as proven.
  E. kill switch ignored                    -> no way to turn it off.

NOT a control, stated plainly: removing the `ctx is not None` test at the
call site kills nothing, because citations_all_invented() independently
returns False for any non-dict context. The two checks are deliberate
defence in depth — the call-site one documents intent, the inner one
enforces it — and only the inner one is load-bearing. Listing the outer one
as a control would claim a strength this suite does not have.
"""
import sys
import types

import pytest

bsp = pytest.importorskip("routes.brain_strategic_planner")

# The real context roots, from _gather_strategic_context's return dict.
CTX = {k: {} for k in (
    "funnel", "page_health", "feedback", "backlog", "competitors",
    "self_model", "recent_recs", "recidivism", "pr_outcomes",
    "self_perception", "code_inventory", "recent_arch_proposals")}

# Verbatim from scaffolds in the tree.
INVENTED = ["competitor.universe.data_center_registries[DCByte]",
            "market_news[5df23a0358e3c310]"]
REAL = ["page_health.pages[/mcp#workos-oauth-challenge].verdict",
        "funnel.now.paid_signal_attribution_30d.attribution_rate_pct"]


# ════════════════════════════════════════════════════════════════════
#  evidence_root
# ════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key,want", [
    ("page_health.pages[/x].verdict", "page_health"),
    ("page_health.pages./x.verdict=broken", "page_health"),
    ("competitor_signal.presence.x", "competitor_signal"),
    ("funnel.now.y=3.3", "funnel"),
    ("  Funnel.Now  ", "funnel"),
    ("market_news[abc]", "market_news"),
])
def test_evidence_root_extraction(key, want):
    assert bsp.evidence_root(key) == want


@pytest.mark.parametrize("bad", [None, 123, "", "   ", ".", "===", {"k": 1}])
def test_evidence_root_of_junk_is_none(bad):
    assert bsp.evidence_root(bad) is None


# ════════════════════════════════════════════════════════════════════
#  citations_all_invented — the rule
# ════════════════════════════════════════════════════════════════════
def test_all_invented_roots_are_caught():
    invented, roots = bsp.citations_all_invented(CTX, INVENTED)
    assert invented is True
    assert roots == ["competitor", "market_news"]


def test_one_real_citation_spares_the_rec():
    """Mutation control B. A rec that cites one real source is not
    fabricating its provenance, however many bad citations sit beside it."""
    invented, _ = bsp.citations_all_invented(CTX, INVENTED + REAL[:1])
    assert invented is False


def test_wrong_subpath_under_a_real_root_is_not_invented():
    """Mutation control D — the be#3448 shape. `funnel` IS a real source;
    a path under it that does not walk is indeterminate, not fabricated."""
    invented, _ = bsp.citations_all_invented(
        CTX, ["funnel.paid_signal_attribution_30d.attribution_rate_pct"])
    assert invented is False


@pytest.mark.parametrize("ctx", [{}, None, [], "nope", 0])
def test_unusable_context_never_condemns(ctx):
    """Mutation control A. A context we do not have is not evidence about
    the citations — the same unknown-is-not-a-verdict rule be#3448 put back
    into the sentinel probe."""
    invented, _ = bsp.citations_all_invented(ctx, INVENTED)
    assert invented is False


def test_no_citations_is_not_invented_provenance():
    """Mutation control C. Citing nothing violates rule 2 differently; it
    is not the same claim as citing something that does not exist."""
    assert bsp.citations_all_invented(CTX, [])[0] is False
    assert bsp.citations_all_invented(CTX, None)[0] is False


def test_junk_only_citations_never_condemn():
    assert bsp.citations_all_invented(CTX, [None, 123, "", "  "])[0] is False


def test_reported_roots_are_deduped_and_sorted():
    invented, roots = bsp.citations_all_invented(
        CTX, ["news.a", "news.b", "customer_asks.c"])
    assert invented is True
    assert roots == ["customer_asks", "news"]


# ════════════════════════════════════════════════════════════════════
#  _open_scaffold_pr — the gate in place
# ════════════════════════════════════════════════════════════════════
class _Recorder:
    def __init__(self):
        self.calls = []

    def module(self):
        m = types.ModuleType("routes.brain_pr_opener")
        m._GITHUB_TOKEN = "t0ken"
        m._GITHUB_REPO = "acme/repo"
        m.open_pr_exists = lambda *a, **k: False
        m.open_similar_pr_exists = lambda *a, **k: False
        m._get_default_branch_sha = lambda: (
            self.calls.append("sha") or "deadbeef")
        m._create_branch = lambda n, s: (
            self.calls.append(("branch", n)) or True)
        m._commit_file = lambda p, c, msg, b, s: (
            self.calls.append(("commit", p)) or True)
        m._gh = lambda meth, path, body=None: (
            self.calls.append(("gh", meth)) or types.SimpleNamespace(
                status_code=201, text="",
                json=lambda: {"html_url": "u", "number": 1}))
        return m


@pytest.fixture
def opener(monkeypatch):
    rec = _Recorder()
    monkeypatch.setitem(sys.modules, "routes.brain_pr_opener", rec.module())
    monkeypatch.delenv("BRAIN_STRATEGIC_CITATION_GATE", raising=False)
    # isolate from the evidence-subject dedup gate (be#3466)
    monkeypatch.setattr(bsp, "_scaffolded_evidence_subjects",
                        lambda *a, **k: {})
    return rec


def _rec(keys):
    return {"title": "Time-to-power estimator tool", "evidence_keys": keys,
            "week_of": "2026-08-31", "kind": "strategic_gap_4w",
            "spec_md": "spec", "confidence": 0.85}


def test_invented_citations_open_nothing(opener):
    out = bsp._open_scaffold_pr(_rec(INVENTED), ctx=CTX)
    assert out["ok"] is True
    assert out["skipped"] == "citations_all_invented"
    assert out["invented_roots"] == ["competitor", "market_news"]
    assert opener.calls == [], f"no branch, no commit, no PR: {opener.calls}"


def test_real_citations_proceed(opener):
    bsp._open_scaffold_pr(_rec(REAL), ctx=CTX)
    assert "sha" in opener.calls


def test_gate_is_inert_without_a_context(opener):
    """Mutation control E. Callers that pass no ctx — the cached path, any
    direct call — must behave exactly as before this gate existed."""
    bsp._open_scaffold_pr(_rec(INVENTED))
    assert "sha" in opener.calls


def test_kill_switch(opener, monkeypatch):
    """Mutation control F."""
    monkeypatch.setenv("BRAIN_STRATEGIC_CITATION_GATE", "0")
    bsp._open_scaffold_pr(_rec(INVENTED), ctx=CTX)
    assert "sha" in opener.calls


def test_gate_is_on_by_default(monkeypatch):
    monkeypatch.delenv("BRAIN_STRATEGIC_CITATION_GATE", raising=False)
    assert bsp._citation_gate_enabled() is True


@pytest.mark.parametrize("raw,want", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("", True), ("  ", True),
])
def test_gate_flag_parsing(monkeypatch, raw, want):
    monkeypatch.setenv("BRAIN_STRATEGIC_CITATION_GATE", raw)
    assert bsp._citation_gate_enabled() is want


def test_skip_report_names_the_context_it_judged_against(opener):
    """The skip must be auditable: a reader has to be able to see WHICH
    schema the citation was measured against, or the verdict is unfalsifiable."""
    out = bsp._open_scaffold_pr(_rec(INVENTED), ctx=CTX)
    assert out["context_roots"] == sorted(CTX)
