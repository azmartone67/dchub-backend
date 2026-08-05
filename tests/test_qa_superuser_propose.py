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

from routes.qa_superuser_dashboard import (
    INVESTIGATION_DEGRADED_MARKER, INVESTIGATION_MARKER, _parse_proposal,
    find_marked_comment, render_investigation_comment,
)
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

    @pytest.mark.parametrize("bad_find", [None, "", 0, False])
    def test_a_missing_or_empty_find_is_refused_not_crashed_on(self, bad_find):
        """★ FIX_SCHEMA only requires `fixable`, so a schema-VALID reply can omit
        `find` entirely. Without this guard `file_content.count(None)` raises
        TypeError inside a daemon thread — the proposal dies with no state
        written and the card spins forever.

        MUTATION: delete the `if not find` branch -> all four cases fail.
        (This guard had ZERO tests; the `_fix()` helper always supplies `find`,
        which is exactly how the gap survived.)
        """
        ok, why = P.validate_fix(
            {"fixable": True, "file": "routes/foo.py", "find": bad_find,
             "replace": "x"}, FILE)
        assert ok is False
        assert "'find'" in why


class TestPathNormalisation:
    """★ The prefix guard is a PATH check, not a string check.

    `./.github/workflows/ci.yml` does not start with `.github/`. Two characters
    defeated the one guard that cannot be argued down by "a human reviews the
    diff", because CI config is what decides how much a reviewer is shown.
    """

    @pytest.mark.parametrize("path", [
        ".github/workflows/ci.yml",
        "./.github/workflows/ci.yml",
        ".github/./workflows/ci.yml",
        ".//.github/workflows/ci.yml",
        "./routes/../.github/workflows/ci.yml",
        ".GitHub/workflows/ci.yml",
    ])
    def test_every_spelling_of_dot_github_is_refused(self, path):
        """MUTATION: drop posixpath.normpath from repo_path -> the ./ and
        multi-slash spellings all pass."""
        got, why = P.repo_path(path)
        assert got is None, f"{path} was allowed: {why}"

    @pytest.mark.parametrize("path,expect", [
        ("routes/foo.py", "routes/foo.py"),
        ("./routes/foo.py", "routes/foo.py"),
        ("routes//foo.py", "routes/foo.py"),
    ])
    def test_a_legitimate_path_resolves_to_one_canonical_form(self, path, expect):
        """★ ONE canonical form matters because the validator and the caller that
        writes the edit both use it. When they normalised separately, the path
        that got CHECKED and the path that got WRITTEN could differ — a guard
        inspecting one file while another is edited is not a guard.
        """
        got, why = P.repo_path(path)
        assert got == expect, why

    @pytest.mark.parametrize("path", ["", "   ", ".", "./", "/etc/passwd",
                                      "../x", "routes/../../x"])
    def test_degenerate_and_escaping_paths_are_refused(self, path):
        got, _ = P.repo_path(path)
        assert got is None


class TestProposalParsing:
    """_call_model FAILS SOFT to a legacy free-text body on a structured 400.

    So a reply can arrive fenced or prose-wrapped even though a schema was
    passed. parse_structured_json is strict and returns None for those — which
    would report "could not parse a proposal" for a perfectly good fix.
    """

    def test_plain_json_parses(self):
        assert _parse_proposal('{"fixable": true, "file": "a.py"}')["file"] == "a.py"

    def test_a_fenced_reply_parses(self):
        """MUTATION: remove the fence-strip fallback -> this fails."""
        got = _parse_proposal('```json\n{"fixable": true, "file": "a.py"}\n```')
        assert got.get("file") == "a.py"

    def test_a_prose_wrapped_reply_parses(self):
        got = _parse_proposal(
            'Here is the fix:\n{"fixable": false, "why_not": "config change"}\n'
            'Hope that helps.')
        assert got.get("why_not") == "config change"

    @pytest.mark.parametrize("junk", ["", "no json here", "{unclosed", "[1,2]"])
    def test_unparseable_replies_return_empty_not_garbage(self, junk):
        """An empty dict is refused downstream by validate_fix's `fixable`
        check; a partial dict would be acted on."""
        assert _parse_proposal(junk) == {}


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
        assert md.startswith(INVESTIGATION_MARKER)

    def test_an_unrun_refutation_is_labelled_unknown_not_no(self):
        """★ BLIND is a third state. `survived: None` means the refutation pass
        did not run — NOT that the recommendation failed it.

        Rendering that as "no" would convict a recommendation on evidence that
        was never gathered, which is the same collapse the probe refuses
        everywhere else.

        MUTATION: change the renderer's `.get(survived, "unknown")` default to
        "no" -> this fails. (Before this test, that mutation left all 32 tests
        green.)
        """
        md = render_investigation_comment(
            {"key": "k", "title": "T"},
            {"recommendation": "x", "confidence": 0.5,
             "refutation": {"attempted": False, "survived": None}})
        assert "survived refutation:** unknown" in md
        assert "survived refutation:** no" not in md
        assert "WARNING" not in md, \
            "an unrun refutation must not raise the refuted-recommendation alarm"


class TestCommentDedup:
    """Re-investigating must REWRITE one comment, not stack another.

    A second opinion is a legitimate thing to want. A thread that grows a fresh
    wall of analysis on every click is the alarm-nobody-reads failure that every
    other watcher on this platform already learned to avoid.
    """

    def test_an_existing_marked_comment_is_found(self):
        """MUTATION: return None unconditionally -> this fails, and the caller
        POSTs a duplicate instead of PATCHing."""
        got = find_marked_comment(
            [{"id": 1, "body": "a human said something"},
             {"id": 42, "body": INVESTIGATION_MARKER + "\n## 🧠 Brain"}],
            INVESTIGATION_MARKER)
        assert got == 42

    def test_a_thread_without_our_marker_returns_none(self):
        """Appending is correct when there is nothing of ours to rewrite."""
        assert find_marked_comment(
            [{"id": 1, "body": "unrelated"}], INVESTIGATION_MARKER) is None

    def test_no_comments_at_all_returns_none(self):
        assert find_marked_comment([], INVESTIGATION_MARKER) is None
        assert find_marked_comment(None, INVESTIGATION_MARKER) is None

    def test_a_human_quoting_the_marker_is_matched_not_crashed_on(self):
        """Degenerate shapes must not raise — this runs inside a daemon thread
        whose only other option is to lose the analysis entirely."""
        assert find_marked_comment(
            [None, {}, {"body": None}, {"id": None,
                                        "body": INVESTIGATION_MARKER}],
            INVESTIGATION_MARKER) is None

    def test_a_degraded_run_uses_a_DIFFERENT_marker(self):
        """★ Otherwise "did not run" OVERWRITES the analysis it should preserve.

        One shared marker means the next failed attempt PATCHes over a good
        recommendation; no marker at all means every failed attempt stacks a
        fresh note. Two markers, each deduping only against its own kind.

        MUTATION: make the renderer emit INVESTIGATION_MARKER for the degraded
        case -> this fails.
        """
        good = render_investigation_comment({"key": "k", "title": "T"},
                                            {"recommendation": "real answer"})
        bad = render_investigation_comment({"key": "k", "title": "T"},
                                           {"cannot_investigate": "no_api_key"})
        assert good.startswith(INVESTIGATION_MARKER)
        assert bad.startswith(INVESTIGATION_DEGRADED_MARKER)
        # The degraded note must not be findable under the good marker, or the
        # upsert would rewrite the good comment with it.
        assert find_marked_comment([{"id": 1, "body": bad}],
                                   INVESTIGATION_MARKER) is None
        assert find_marked_comment([{"id": 1, "body": good}],
                                   INVESTIGATION_DEGRADED_MARKER) is None

    def test_the_rendered_comment_round_trips_through_the_finder(self):
        """The marker the renderer writes is the marker the finder looks for.

        Two constants that must agree; a test that hardcodes the string in both
        places would pass while they drifted apart.
        """
        md = render_investigation_comment({"key": "k", "title": "T"},
                                          {"recommendation": "x"})
        assert find_marked_comment([{"id": 7, "body": md}],
                                   INVESTIGATION_MARKER) == 7


# ── the checks added after the 0805 sweep ───────────────────────────────────
from tools.qa_superuser.board import WITHDRAW_AFTER_GAUGE_RUNS  # noqa: E402
from tools.qa_superuser.probe_data import _granularity_too_coarse  # noqa: E402
from tools.qa_superuser.probe_mcp import _zero_arg_tools, _declared_tier  # noqa: E402


class TestGranularityGuard:
    """★ A comparison that is arithmetic rather than observation must not run.

    `deals.newest_record` is a BARE DATE, so the finest age it can express is
    ~24h. Held against a declared 5-minute cadence, `age > 2*cadence` is true by
    construction on every run, forever — the check reports the storage format,
    not the world. Half the gauge's headline number was permanent noise, which
    teaches the reader to skip the line including the half that is real.
    """

    def test_a_bare_date_cannot_be_tested_against_a_five_minute_cadence(self):
        """MUTATION: make _granularity_too_coarse return False always -> fails."""
        assert _granularity_too_coarse("2026-08-04", 5 / 60.0) is True

    def test_a_bare_date_IS_testable_against_a_daily_cadence(self):
        """The guard must not swallow real staleness — 24h resolution can test
        a 24h+ cadence, and a feed genuinely a week stale must still show."""
        assert _granularity_too_coarse("2026-08-04", 24.0) is False

    def test_a_full_timestamp_is_always_testable(self):
        assert _granularity_too_coarse(
            "Tue, 04 Aug 2026 07:35:48 GMT", 6.0) is False

    def test_a_missing_timestamp_is_not_a_granularity_problem(self):
        # No timestamp is the 'uninstrumented' finding, a different thing.
        assert _granularity_too_coarse(None, 5 / 60.0) is False


class TestToolFunctionalitySampling:
    """Registration != function. Arguments come from the server's OWN schema."""

    def test_only_tools_with_no_required_args_are_called(self):
        """★ No argument is ever invented. A tool that needs input is skipped,
        not guessed at — guessing produces a failure that says nothing about
        the tool."""
        tools = [
            {"name": "needs_args", "inputSchema": {"required": ["market"]}},
            {"name": "free_call", "inputSchema": {"properties": {}}},
            {"name": "empty_required", "inputSchema": {"required": []}},
            {"name": "no_schema"},
        ]
        assert _zero_arg_tools(tools) == ["empty_required", "free_call", "no_schema"]

    def test_a_tool_without_a_name_is_skipped(self):
        assert _zero_arg_tools([{"inputSchema": {}}]) == []


class TestTierSelfReport:
    def test_the_tier_field_is_read_from_structuredContent(self):
        """★ NOT from content[].text. Shell #49 proved an absence with a probe
        that searched the text blocks while the value lived in
        structuredContent."""
        env = {"structuredContent": {"caller_tier": "Pro"},
               "content": [{"type": "text", "text": "tier: free"}]}
        assert _declared_tier(env) == ("caller_tier", "pro")

    def test_absent_tier_field_returns_none_rather_than_a_default(self):
        """A missing claim is not a claim of 'free' — inventing one would
        manufacture a pass."""
        assert _declared_tier({"structuredContent": {"market": "ashburn"}}) is None
        assert _declared_tier({}) is None

    def test_a_non_dict_structuredContent_does_not_raise(self):
        assert _declared_tier({"structuredContent": ["not", "a", "dict"]}) is None


class TestWithdrawnIsNotFixed:
    def test_withdrawal_requires_a_SUSTAINED_retraction(self):
        """★ One quiet run is a flap, not a decision.

        The quota check flips RED<->GAUGE with the runner's trial state. If a
        single GAUGE run could withdraw an issue, that check would close its own
        issue every few hours and the board would lose a real finding.
        """
        assert WITHDRAW_AFTER_GAUGE_RUNS >= 4, (
            "a withdrawal threshold this low lets a flapping check retract its "
            "own finding")
