#!/usr/bin/env python3
"""The propose lane: what it refuses, and that it refuses for the right reason.

★ THIS SUITE IS MOSTLY ABOUT REFUSALS. The lane's value is not that it can write
a PR — anything can write a PR. It is that it declines to write one from a
symptom, from stale analysis, from a recommendation its own author refuted, from
an ambiguous match, or into CI config. Each of those is a test below, and each
one exists because the alternative failure has been observed on this platform.

★ Every guard is mutation-checked in the docstring of its test: what to break,
and which test must go red. A guard whose removal breaks nothing is decoration.
"""
from __future__ import annotations

import pytest

from routes.qa_superuser_dashboard import render_investigation_comment
from tools.qa_superuser import propose as P


def _fix(**kw):
    base = {"fixable": True, "file": "routes/foo.py",
            "find": "old_value = 1", "replace": "old_value = 2",
            "rationale": "because"}
    base.update(kw)
    return base


FILE = "def a():\n    old_value = 1\n    return old_value\n"


# ── the investigation gate ──────────────────────────────────────────────────
class TestInvestigationGate:
    def test_no_investigation_is_refused(self):
        """A diff written from a symptom is a guess.

        MUTATION: make gate_investigation return True for None -> this fails.
        """
        ok, why = P.gate_investigation(None)
        assert ok is False
        assert "investigation" in why.lower()

    def test_stale_investigation_is_refused(self):
        """The analysis explains evidence the finding no longer shows.

        Same three-state rule as acks: current / stale / absent. Generating code
        from `stale` fixes a problem that has already changed shape.
        """
        ok, why = P.gate_investigation(
            {"state": "stale", "survived": True, "recommendation": "do x"})
        assert ok is False
        assert "OLDER evidence" in why

    def test_refuted_recommendation_cannot_become_code(self):
        """★ The central gate.

        The brain's investigator refutes ~70% of its own drafts. Turning a
        knocked-down recommendation into a patch is exactly the
        plausible-but-wrong-fix failure this whole tool exists to avoid.

        MUTATION: drop the `survived is False` branch -> this test fails.
        """
        ok, why = P.gate_investigation(
            {"state": "current", "survived": False, "recommendation": "do x"})
        assert ok is False
        assert "refutation" in why.lower()

    def test_empty_recommendation_is_refused(self):
        ok, _ = P.gate_investigation(
            {"state": "current", "survived": True, "recommendation": "   "})
        assert ok is False

    def test_a_current_survived_investigation_passes(self):
        """The gate must actually admit the good case, or it is just a wall."""
        ok, why = P.gate_investigation(
            {"state": "current", "survived": True, "recommendation": "do x"})
        assert ok is True, why

    def test_survived_unknown_is_allowed(self):
        """`None` means the refutation pass did not run — not that it failed.

        Treating unknown as refused would silently disable the lane whenever the
        refutation step degrades, which is the BLIND-vs-RED distinction the probe
        already enforces everywhere else.
        """
        ok, _ = P.gate_investigation(
            {"state": "current", "survived": None, "recommendation": "do x"})
        assert ok is True


# ── the edit gate ───────────────────────────────────────────────────────────
class TestValidateFix:
    def test_a_clean_single_replacement_is_accepted(self):
        ok, why = P.validate_fix(_fix(), FILE)
        assert ok is True, why
        assert "routes/foo.py" in why

    def test_model_declining_is_carried_through_verbatim(self):
        """`fixable: false` is a correct and common answer — surface its reason."""
        ok, why = P.validate_fix(
            {"fixable": False, "why_not": "this is a Cloudflare zone rule"}, FILE)
        assert ok is False
        assert "Cloudflare" in why

    def test_absent_find_is_refused(self):
        """The model wrote a patch against text that is not in the file.

        MUTATION: delete the `n == 0` branch -> this fails.
        """
        ok, why = P.validate_fix(_fix(find="not_in_the_file"), FILE)
        assert ok is False
        assert "does not appear" in why

    def test_ambiguous_find_is_refused_not_resolved(self):
        """Two matches means we do not know which one the analysis meant.

        MUTATION: change `n > 1` to `n > 2` -> this fails.
        """
        twice = "x = 1\nx = 1\n"
        ok, why = P.validate_fix(_fix(find="x = 1", replace="x = 2"), twice)
        assert ok is False
        assert "2x" in why and "ambiguous" in why

    def test_unreadable_file_is_refused(self):
        """No content means no verification. Refuse rather than trust the model.

        MUTATION: treat `file_content is None` as OK -> this fails.
        """
        ok, why = P.validate_fix(_fix(), None)
        assert ok is False
        assert "could not read" in why

    def test_no_op_edit_is_refused(self):
        ok, why = P.validate_fix(
            _fix(find="old_value = 1", replace="old_value = 1"), FILE)
        assert ok is False
        assert "no-op" in why

    @pytest.mark.parametrize("path", [
        "../../etc/passwd", "/etc/passwd", "routes/../../x",
    ])
    def test_paths_outside_the_repo_are_refused(self, path):
        ok, why = P.validate_fix(_fix(file=path), FILE)
        assert ok is False
        assert "unsafe path" in why

    @pytest.mark.parametrize("path", [
        ".github/workflows/ci.yml",
        ".github/workflows/qa-superuser.yml",
    ])
    def test_ci_config_is_off_limits(self, path):
        """★ The one denial that cannot be argued down by "a human reviews it".

        Every other guard here rests on a reviewer seeing the diff. CI config is
        what decides how much a reviewer is shown, so a proposal that weakens it
        undermines the guarantee the rest of the lane depends on.

        MUTATION: empty FORBIDDEN_PREFIXES -> both cases fail.
        """
        content = "on: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
        ok, why = P.validate_fix(
            _fix(file=path, find="on: push", replace="on: workflow_dispatch"),
            content)
        assert ok is False
        assert "off-limits" in why

    def test_a_large_deletion_is_refused_as_a_stale_copy_revert(self):
        """del >> ins is the signature of pasting an older copy of a file.

        That pattern has cost this repo real work before (0728 CI wave), and it
        is indistinguishable from a "fix" at the diff level unless you measure it.

        MUTATION: raise MAX_NET_LINES_DELETED to 10_000 -> this fails.
        """
        big = "\n".join(f"line {i}" for i in range(40)) + "\n"
        ok, why = P.validate_fix(
            _fix(find=big, replace="line 0\n"), big)
        assert ok is False
        assert "stale-copy revert" in why

    def test_a_small_deletion_is_still_allowed(self):
        """The size guard must not ban ordinary edits — that would be a wall.

        A guard tuned so tight that nothing passes is the same as no lane at all,
        and it fails silently (every proposal 'refused'), which is worse.
        """
        src = "a\nb\nc\nd\n"
        ok, why = P.validate_fix(_fix(find="a\nb\nc\n", replace="a\n"), src)
        assert ok is True, why

    def test_non_dict_proposal_is_refused(self):
        ok, _ = P.validate_fix("not a dict", FILE)  # type: ignore[arg-type]
        assert ok is False


# ── titling / dedup ─────────────────────────────────────────────────────────
class TestPrTitle:
    def test_distinct_findings_get_distinct_titles(self):
        """★ Without this every QA PR is '[brain auto-fix] generic_find_replace'.

        The shared lane titles by ISSUE TYPE, and all of these share one type. A
        dedup check keyed on title would then reject the second real fix as a
        duplicate of the first, which looks like the lane working.

        MUTATION: return a constant -> this fails.
        """
        a = P.pr_title_for({"key": "web::stale-edge", "title": "Edge caches"})
        b = P.pr_title_for({"key": "mcp::quota", "title": "Quota"})
        assert a != b
        assert "web::stale-edge" in a

    def test_title_is_bounded(self):
        t = P.pr_title_for({"key": "k" * 200, "title": "t" * 200})
        assert len(t) <= 120


# ── the comment a human reads ───────────────────────────────────────────────
class TestRenderedComment:
    def test_a_degraded_run_is_never_rendered_as_a_thin_analysis(self):
        """"The brain never ran" and "the brain found little" must not look alike.

        MUTATION: drop the cannot_investigate branch -> this fails, because the
        renderer would emit an empty Recommendation section instead.
        """
        md = render_investigation_comment(
            {"key": "k", "title": "T"}, {"cannot_investigate": "call_fail:Timeout"})
        assert "did not run" in md
        assert "call_fail:Timeout" in md
        assert "### Recommendation" not in md

    def test_a_refuted_recommendation_is_flagged_before_it_is_read(self):
        """The warning must precede the recommendation, not follow it."""
        md = render_investigation_comment(
            {"key": "k", "title": "T"},
            {"recommendation": "Add a bypass rule", "confidence": 0.3,
             "refutation": {"survived": False,
                            "weaknesses_found": ["assumes the origin is right"]}})
        assert md.index("WARNING") < md.index("Add a bypass rule")
        assert "did NOT survive" in md
        assert "assumes the origin is right" in md

    def test_a_healthy_result_renders_the_decision_it_needs(self):
        md = render_investigation_comment(
            {"key": "k", "title": "T"},
            {"recommendation": "Add the header", "confidence": 0.81,
             "decision_for_human": "Choose origin vs edge",
             "caveats": ["only checked one colo"],
             "refutation": {"survived": True, "weaknesses_found": []}})
        assert "0.81" in md and "survived refutation:** yes" in md
        assert "Choose origin vs edge" in md
        assert "only checked one colo" in md
        assert "WARNING" not in md

    def test_every_comment_is_marked_recommend_only(self):
        """The comment must never read as a report of work already done."""
        md = render_investigation_comment(
            {"key": "k", "title": "T"}, {"recommendation": "x"})
        assert "Recommend-only" in md
        assert "nothing here has been applied" in md.lower()

    def test_comment_carries_the_dedup_marker(self):
        md = render_investigation_comment({"key": "k", "title": "T"}, {})
        assert md.startswith("<!-- qa-superuser:investigation -->")
