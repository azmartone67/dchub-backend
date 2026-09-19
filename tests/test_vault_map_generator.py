"""tests/test_vault_map_generator.py — the map cannot quietly go stale (2026-08-12).

An audit on 2026-08-11 re-proposed THREE already-shipped capabilities because
758 route modules were navigable only by grep. scripts/generate_vault_map.py is
the map. It is GENERATED rather than written, because a hand-maintained map is
accurate for one day and then lies — the same "failure rendered as a benign
value" this whole series of fixes has been removing.

Ways the map starts lying:
  (1) PHANTOM LOOP — the first cut scraped every `"name":` in system_loops.py
      and reported EIGHT loops, inventing `iso_metrics` out of a docstring that
      quotes heartbeat's registry as an example.
  (2) WRONG KEY — keying loops by function name gives `auto_press`, while the
      board reports `auto_press_daily`. A map keyed differently from the thing
      it maps sends you hunting for something that does not exist.
  (3) EATS HAND-WRITTEN NOTES — the generator overwrites a note a human owns.
  (4) SILENT DRIFT — --check stops detecting a stale vault, so CI can no longer
      prove the map matches the tree.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_vault_map_generator.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "generate_vault_map.py"


def _mod():
    spec = importlib.util.spec_from_file_location("vault_map", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _probed_loop_names() -> list:
    return _mod().collect_loops()["probes"]


def test_every_owned_note_is_produced():
    notes = _mod().build()
    assert set(notes) == set(_mod()._OWNED)
    for name, text in notes.items():
        assert text.strip(), "%s rendered empty" % name
        assert "generated: true" in text, \
            "%s lacks the generated marker that stops hand edits" % name


def test_no_phantom_loop_from_a_docstring():
    """★REGRESSION (1). `iso_metrics` is a heartbeat SURFACE quoted inside a
    docstring in system_loops.py — it is not a probed loop, and a file-wide
    `"name":` scrape reported it as one."""
    assert "iso_metrics" not in _probed_loop_names(), (
        "the map invented a loop out of a docstring example — read probe "
        "FUNCTION BODIES, not every quoted name in the file")


def test_loop_names_match_the_probe_functions_one_for_one():
    """Every probed loop, exactly once, no more and no fewer."""
    src = (_ROOT / "routes" / "system_loops.py").read_text(encoding="utf-8")
    n_funcs = len(re.findall(r"^def _probe_[a-z_]+\s*\(", src, re.M))
    names = _probed_loop_names()
    assert len(names) == n_funcs, \
        "%d probe functions but %d mapped loops" % (n_funcs, len(names))
    assert len(set(names)) == len(names), "duplicate loop names in the map"


def test_the_reported_name_is_used_not_the_function_name():
    """★REGRESSION (2). _probe_auto_press returns name 'auto_press_daily'; the
    map must carry what the board shows."""
    names = _probed_loop_names()
    assert "auto_press_daily" in names
    assert "auto_press" not in names


def test_source_nodes_and_edges_are_read_from_the_canonical_graph():
    g = _mod().collect_loops()
    assert g["sources"], "source nodes missing from the map"
    assert g["edges"], "declared edges missing from the map"
    typed = {s["loop"] for s in g["sources"]}
    assert typed <= set(g["probes"]), \
        "a typed source names a loop that is not probed: %s" % (
            typed - set(g["probes"]))


def test_hand_written_notes_are_never_owned():
    """★REGRESSION (3). These carry findings a generator cannot reconstruct."""
    owned = set(_mod()._OWNED)
    for hand in ("Context Integrity.md", "Admin Cache Leak.md", "Traps.md",
                 "Home.md", "DCHUB.md"):
        assert hand not in owned, \
            "the generator would overwrite the hand-written note %s" % hand


def test_check_mode_detects_a_stale_vault(tmp_path):
    """★REGRESSION (4). --check is how CI proves the map still matches."""
    m = _mod()
    notes = m.build()
    for name, text in notes.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    import sys
    argv = sys.argv[:]
    try:
        sys.argv = ["gen", "--check", "--check-target", str(tmp_path)]
        assert m.main() == 0, "--check called a fresh vault stale"
        (tmp_path / "Loop Graph.md").write_text("drifted", encoding="utf-8")
        assert m.main() == 1, "--check passed a vault that no longer matches"
    finally:
        sys.argv = argv


@pytest.mark.parametrize("note", ["Architecture Map.md", "Master Shells.md"])
def test_key_notes_carry_the_fix_history_pointer_or_links(note):
    """The map's whole job is to stop the next audit grepping its way to a
    wrong conclusion, so the entry points must point somewhere better."""
    text = _mod().build()[note]
    assert "[[" in text, "%s has no wikilinks — it is a dead end" % note


def test_unregistered_brain_drafts_are_not_counted_as_route_modules():
    """★ 2026-08-24. `routes/_proposed_*.py` are draft proposals the brain
    attaches to a strategic-draft PR. Not one is registered as a blueprint, yet
    all 27 counted toward `route modules`.

    Wrong on this map's own terms — it exists because "758 route modules were
    navigable only by grep" let an audit re-propose three shipped capabilities,
    so padding that number with the very drafts that caused the confusion makes
    it less navigable. And it made every brain-l6 draft PR born red: the PR adds
    one _proposed_ file, the count moves, the committed map goes stale, and
    test_the_in_repo_copy_is_current fails on a file the bot never knew to
    regenerate."""
    m = _mod()
    live = m.live_route_modules()
    assert live, "route module list came back empty"
    assert not [f for f in live if f.startswith("_proposed_")], \
        "an unregistered brain draft is being counted as a route module"
    # and the count actually rendered in the map is that list, not len(listdir)
    import os as _os
    on_disk = [f for f in _os.listdir(m._ROUTES) if f.endswith(".py")]
    drafts = [f for f in on_disk if f.startswith("_proposed_")]
    if drafts:                       # guard is only meaningful while any exist
        assert len(live) == len(on_disk) - len(drafts)
        assert f"| route modules | {len(live)} |" in \
            m.build()["Architecture Map.md"], \
            "the map still renders the padded count"


def test_a_promoted_draft_starts_counting(tmp_path, monkeypatch):
    """★ THE PAIRED CONTROL. Excluding a PREFIX must not exclude a real module.
    Promotion drops the `_proposed_` prefix, and the moment it does the module
    must appear — otherwise this fix hides live routes instead of drafts."""
    m = _mod()
    fake = tmp_path / "routes"
    fake.mkdir()
    for n in ("_proposed_thing.py", "thing.py", "notes.md"):
        (fake / n).write_text("", encoding="utf-8")
    monkeypatch.setattr(m, "_ROUTES", str(fake))
    live = m.live_route_modules()
    assert "thing.py" in live, "a real route module was excluded"
    assert "_proposed_thing.py" not in live
    assert "notes.md" not in live, "a non-.py file was counted"


# ── why this gate is advisory on PRs, and only on PRs (2026-09-04) ──────
#
# It stays the gate on main. On a PULL REQUEST it reports drift without failing,
# because requiring every PR to COMMIT the regenerated map is what turns one
# generated file into a repo-wide merge-conflict generator.
#
# The race is already documented, in .github/workflows/refresh-architecture-map.yml:
#
#     "IT IS A RACE, NOT CARELESSNESS. main's protection has strict:false, so a
#      PR need not be up to date to merge. PR A regenerates the map against its
#      own tree, PR B lands a new route module first, and A merges a map that
#      was true when written and is stale by the time it arrives."
#
# That workflow heals MAIN after every merge and demonstrably works. What it
# cannot heal is PR-vs-PR: two open PRs that each add a route module both commit
# a different version of the SAME generated file and conflict with each other,
# every time, by construction. Measured 2026-09-04: main takes ~47 commits/24h
# and the unit suite runs 25 minutes, so every PR is open long enough for this
# to be a certainty rather than bad luck. Three of four PRs open that evening
# hit it; two needed a hand-resolved rebase whose entire content was "re-run the
# generator".
#
# So the file no longer has to travel in the PR. Drift is reported on PRs and
# healed on main by the workflow above. The assertion is UNCHANGED for push,
# schedule and local runs — a developer still sees it, and main is still gated.
#
# ★ The skip is deliberately narrow: exactly GITHUB_EVENT_NAME == "pull_request".
# test_the_pr_exemption_cannot_widen below pins that, because a guard that
# learns to skip everywhere is the failure this file's own docstring is about.
# The healer's own branch is the ONE pull request that must still be gated.
# refresh-architecture-map.yml mints it at :110 as "chore/arch-map-<sha>" and
# matches on that prefix at :104; tests/test_architecture_map_autoheal.py:73
# already pins the string, so this reuses a constant rather than inventing one.
#
# ★ Advisory is not the same as invisible, and it was invisible until 2026-09-19.
# The skip below carried a full explanation that nobody could read: pytest shows
# a skip as one `s`, and the unit-tests step runs ~22,000 tests, so a PR author
# saw a green check and learned about the drift when MAIN went red after their
# merge. Drift is now ANNOUNCED on the PR — a ::warning annotation and a job
# summary — while staying advisory, so the author sees it without the generated
# file having to travel in the branch.
_HEALER_BRANCH_PREFIX = "chore/arch-map-"

_DRIFT_TITLE = "architecture map drifted"


def _workflow_escape(value) -> str:
    """Workflow-command escaping, so a filename cannot end the annotation early."""
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _drift_message(stale) -> str:
    """The one-line version, for the annotation."""
    return (
        "%s: %s. NOT failing this PR — refresh-architecture-map.yml regenerates it "
        "on main after the merge. Run `python3 scripts/generate_vault_map.py` if "
        "you want the diff in your branch." % (_DRIFT_TITLE, ", ".join(stale))
    )


def _announce_drift(stale, request) -> None:
    """Put PR drift where the author will actually see it. Never raises.

    ★ The annotation goes to STDERR, and that is not a style choice.
    test_inspector_failure_diagnostics_reach_the_log records the 2026-08-12
    failure where a block's stdout was redirected into $GITHUB_STEP_SUMMARY and
    swallowed the ::warning whole. The unit-tests step pipes pytest's stdout
    through `tee`. stderr is left alone by both.

    ★ pytest captures fd 1 AND fd 2, so a plain write would surface only in the
    captured-output section of a SKIPPED test, which is exactly where nobody
    looked. Global capture is suspended for the write.

    The job summary is a file, so it needs neither of those workarounds — and it
    is the half that renders on the PR's own checks page.
    """
    import os
    import sys
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(
                    "### %s\n\n"
                    "`docs/architecture/` no longer matches the tree: %s\n\n"
                    "This does **not** block the PR — `refresh-architecture-map.yml` "
                    "regenerates it on `main` after the merge. Commit it here only if "
                    "you want the diff in your branch:\n\n"
                    "```\npython3 scripts/generate_vault_map.py\n```\n"
                    % (_DRIFT_TITLE, ", ".join(stale)))
        except OSError:
            pass
    if os.environ.get("GITHUB_ACTIONS") == "true":
        line = "::warning title=%s::%s" % (
            _workflow_escape(_DRIFT_TITLE), _workflow_escape(_drift_message(stale)))
        capture = request.config.pluginmanager.getplugin("capturemanager")
        if capture is None:
            print(line, file=sys.stderr, flush=True)
        else:
            with capture.global_and_fixture_disabled():
                print(line, file=sys.stderr, flush=True)


def _is_pull_request() -> bool:
    """True on a PR that may defer regeneration to the healer.

    ★ The healer's OWN PR is excluded, and that carve-out is the whole point.
    Its entire purpose is to make the committed map match the tree, so it is the
    one PR that must not be allowed to merge a stale one. main's protection has
    strict:false, so a healing PR is never required to be up to date: during its
    ~25-minute check cycle more merges can land and move the count again, and
    because the checkout is the MERGE ref its tree already contains those new
    modules while its committed map does not. Exempting it would disarm the only
    check that ever validated the healer's output — the healer would merge a map
    that was already stale, and the next push would just open another one.
    """
    import os
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return False
    head = os.environ.get("GITHUB_HEAD_REF") or ""
    return not head.startswith(_HEALER_BRANCH_PREFIX)


def test_the_in_repo_copy_is_current(request):
    """★THE ACTUAL CI GATE, and the reason an in-repo copy exists at all.

    The vault is a local Obsidian directory outside the repo, so a runner has no
    copy of it — `--check --vault ~/Documents/DCHUB` can never execute in CI.
    Shipping --check while saying "CI can prove the map matches" would have been
    a guard that reads as wired and enforces nothing, which is the failure this
    codebase keeps rediscovering. docs/architecture/ is committed precisely so
    THIS test can fail when someone adds a shell and does not regenerate."""
    import pathlib as _p
    m = _mod()
    target = _p.Path(m._REPO_DOCS)
    assert target.is_dir(), (
        "docs/architecture/ is missing — run "
        "`python3 scripts/generate_vault_map.py` and commit it")
    stale = []
    for name, text in m.build().items():
        cur = target / name
        if not cur.exists() or cur.read_text(encoding="utf-8").strip() != text.strip():
            stale.append(name)
    if stale and _is_pull_request():
        # Reported, not enforced — refresh-architecture-map.yml regenerates this
        # on main after the merge. Failing here would force the PR to carry the
        # file, which is the conflict generator described above.
        # ANNOUNCE FIRST: the skip reason below is one `s` in a 22,000-test run.
        _announce_drift(stale, request)
        import pytest as _pt
        _pt.skip(
            "architecture map drifted (%s) — NOT failing the PR: "
            "refresh-architecture-map.yml regenerates it on main after merge. "
            "Requiring every PR to commit this generated file is what makes two "
            "PRs conflict on it. Run `python3 scripts/generate_vault_map.py` "
            "locally if you want the diff in your branch." % ", ".join(stale))
    assert not stale, (
        "the committed architecture map no longer matches the tree: %s\n"
        "Re-run `python3 scripts/generate_vault_map.py` and commit "
        "docs/architecture/." % ", ".join(stale))


def test_the_pr_exemption_cannot_widen(monkeypatch):
    """The exemption must be EXACTLY pull_request. A guard that learns to skip
    on push, schedule or a bare local run stops gating main — which is the only
    thing this gate was ever for."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    for ev, expected in (("pull_request", True), ("push", False),
                         ("schedule", False), ("workflow_dispatch", False),
                         ("", False)):
        monkeypatch.setenv("GITHUB_EVENT_NAME", ev)
        assert _is_pull_request() is expected, f"event {ev!r} misclassified"
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    assert _is_pull_request() is False, "unset env (local run) must still enforce"


def test_the_healers_own_pr_is_never_exempt(monkeypatch):
    """The healer exists to make the committed map match the tree, so it is the
    one PR that must not merge a stale one.

    main's protection has strict:false, so a healing PR is never required to be
    up to date. During its ~25-minute check cycle more merges land, and its
    checkout is the MERGE ref — tree already updated, committed map not. Before
    the PR exemption, this check failing there is exactly what blocked auto-merge
    and forced a fresh regeneration. Exempting it would let the healer merge a
    map that is already stale again."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    for branch, exempt in (
        ("chore/arch-map-abc1234", False),   # the healer itself — MUST enforce
        ("chore/arch-map-", False),
        ("feat/some-feature", True),         # an ordinary PR — may defer
        ("fix/whatever", True),
        ("", True),
    ):
        monkeypatch.setenv("GITHUB_HEAD_REF", branch)
        assert _is_pull_request() is exempt, (
            f"branch {branch!r}: expected exempt={exempt}")


def test_the_healer_branch_prefix_matches_the_workflow_that_mints_it():
    """If the workflow renames its branch, the carve-out above silently stops
    matching and the healer becomes exempt again — the failure this whole file
    is about."""
    wf = (_ROOT / ".github" / "workflows" / "refresh-architecture-map.yml"
          ).read_text(encoding="utf-8")
    assert _HEALER_BRANCH_PREFIX in wf, (
        f"{_HEALER_BRANCH_PREFIX!r} no longer appears in "
        "refresh-architecture-map.yml — the healer branch was renamed and the "
        "carve-out in _is_pull_request() now matches nothing.")


def test_the_push_side_backstop_still_runs_the_suite():
    """★ THE NET THAT DOES NOT DEPEND ON THE HEALER.

    Adversarial review of this exemption found that
    test_the_healer_that_justifies_the_exemption_exists (below) is GREP, NOT
    BEHAVIOUR: setting the repo variable ARCH_MAP_AUTOHEAL_DISABLE=1 turns the
    healer into a no-op that still `exit 0`s as SUCCESS
    (refresh-architecture-map.yml:73-75, 84-86), and every assertion in that
    test still passes. A guard that cannot see its own kill switch is the exact
    failure this file's docstring is about, so the exemption must not rest on it
    alone.

    It does not. pre-merge-gauntlet runs the FULL suite on `push: branches:
    [main]` with no `concurrency:` block, so a stale main reds independently of
    the healer — and this gate is not exempt there, because the event is `push`.
    Measured by the review: main commits aa8693350 and 405224687 were genuinely
    stale (routes/ count 793, committed map 792) and BOTH push runs failed
    (33789265981, 33789288736) while the commits either side succeeded.

    So: the healer makes staleness SHORT (~28 min); this makes it VISIBLE even
    if the healer is off. Pin the second net, because it is the load-bearing one.
    """
    wf = (_ROOT / ".github" / "workflows" / "pre-merge.yml").read_text(encoding="utf-8")
    import re as _re
    on_block = wf.split("jobs:", 1)[0]
    assert _re.search(r"push:\s*\n\s*branches:\s*\[\s*main\s*\]", on_block), (
        "pre-merge-gauntlet no longer runs on push to main — that is the backstop "
        "that catches a stale map when the healer is disabled. Without it the PR "
        "exemption in test_the_in_repo_copy_is_current has no independent net.")
    assert "pytest tests/" in wf, (
        "pre-merge-gauntlet no longer runs the full suite, so it would not run "
        "this gate on push to main.")
    # A concurrency block is ALLOWED, but only if a push to main can never
    # share a group with another run. Evaluated, not grepped — see
    # _assert_main_push_is_never_queued below.
    _assert_main_push_is_never_queued(wf)


# ── the concurrency invariant, evaluated ─────────────────────────────────────
# ★ 2026-09-05. The check above used to be `"\nconcurrency:" not in wf`, which
# is a DRIFT check, not a correctness one: it fails any concurrency block,
# including a correct one, and would equally have passed a block that kept main
# safe by accident. It also could not express the actual danger, which is
# subtler than "main gets cancelled":
#
#   cancel-in-progress: false is NOT enough. It protects a run that has already
#   STARTED. GitHub still cancels a PENDING run when a newer run joins the same
#   group — so grouping main pushes together loses the MIDDLE commit of any two
#   close merges, which is exactly the stale-main blind spot this file exists
#   to prevent. That defect shipped in the first draft of the queue fix and is
#   what this replacement catches.


def _truthy(v) -> bool:
    """GitHub's truthiness: empty string and 0 are false, everything else true."""
    return not (v is False or v == "" or v == 0)


def _eval_gha(expr: str, ctx: dict):
    """Evaluate the subset of GitHub expression syntax a concurrency group uses.

    ★ `&&` and `||` return an OPERAND, not a boolean, exactly as GitHub does —
    that is the whole reason the `a == b && x || y` ternary idiom works, and
    the reason it silently falls through when x is falsy. Modelling that is the
    point; a boolean-returning version would not catch the fall-through bug.
    """
    e = expr.strip()
    if "||" in e:
        lhs, rhs = e.split("||", 1)
        v = _eval_gha(lhs, ctx)
        return v if _truthy(v) else _eval_gha(rhs, ctx)
    if "&&" in e:
        lhs, rhs = e.split("&&", 1)
        v = _eval_gha(lhs, ctx)
        return _eval_gha(rhs, ctx) if _truthy(v) else v
    for op in ("==", "!="):
        if op in e:
            lhs, rhs = e.split(op, 1)
            eq = _eval_gha(lhs, ctx) == _eval_gha(rhs, ctx)
            return eq if op == "==" else not eq
    if e.startswith("'") and e.endswith("'"):
        return e[1:-1]
    if e not in ctx:
        raise AssertionError(
            f"concurrency group references {e!r}, which this guard cannot "
            "evaluate — teach _eval_gha about it rather than deleting the check")
    return ctx[e]


def _group_for(wf: str, ref: str, run_id: str) -> str:
    """The concurrency group pre-merge-gauntlet would compute for one run."""
    import re as _re
    line = _re.search(r"^concurrency:\n(?:.*\n)*?[ \t]+group:[ \t]*(.+)$",
                      wf, _re.M)
    assert line, "concurrency block present but has no group: line"
    ctx = {"github.ref": ref, "github.run_id": run_id}
    out, rest = "", line.group(1).strip()
    while "${{" in rest:
        head, rest = rest.split("${{", 1)
        expr, rest = rest.split("}}", 1)
        out += head + str(_eval_gha(expr, ctx))
    return out + rest


def _assert_main_push_is_never_queued(wf: str) -> None:
    if "\nconcurrency:" not in wf:
        return                                   # no grouping at all is safe
    a = _group_for(wf, "refs/heads/main", "111")
    b = _group_for(wf, "refs/heads/main", "222")
    assert a != b, (
        f"two pushes to main share the concurrency group {a!r}. "
        "cancel-in-progress: false does NOT save this — GitHub cancels a "
        "PENDING run when a newer one joins the group, so the middle commit "
        "of two close merges never runs its gauntlet and a stale main goes "
        "unseen. Give main a per-run group (github.run_id).")
    # Positive control: without this, a group that is unique for EVERY run
    # would pass the assertion above while doing nothing about the queue the
    # block was added to fix.
    p = _group_for(wf, "refs/pull/7/merge", "111")
    q = _group_for(wf, "refs/pull/7/merge", "222")
    assert p == q, (
        f"two runs on the same PR ref got different groups ({p!r} vs {q!r}), "
        "so no superseded run is ever cancelled and the concurrency block is "
        "decorative — the queue it exists to drain would still be there.")


def test_a_push_to_main_never_shares_a_concurrency_group():
    """The invariant above, run against the shipped workflow."""
    _assert_main_push_is_never_queued(
        (_ROOT / ".github" / "workflows" / "pre-merge.yml").read_text(encoding="utf-8"))


def test_the_healer_that_justifies_the_exemption_exists():
    """The healer keeps main's staleness SHORT. It is wiring-level only.

    ★ This asserts the workflow is present and wired — NOT that it heals. Setting
    ARCH_MAP_AUTOHEAL_DISABLE=1 makes it a no-op that reports SUCCESS and every
    assertion below still passes. That limit is why
    test_the_push_side_backstop_still_runs_the_suite exists above; do not treat
    this test as proof the healer works."""
    import pathlib as _p
    wf = _ROOT / ".github" / "workflows" / "refresh-architecture-map.yml"
    assert wf.is_file(), (
        "refresh-architecture-map.yml is gone — the PR exemption in "
        "test_the_in_repo_copy_is_current assumes it heals main after merge. "
        "Restore it, or make that assertion fail on PRs again.")
    text = wf.read_text(encoding="utf-8")
    assert "branches: [main]" in text, "the healer no longer runs on main pushes"
    assert "generate_vault_map" in text, "the healer no longer runs the generator"


# ── the advisory PR path has to be LOUD, or it is the same as silent ────────
#
# These exist because the failure they guard against already happened: the skip
# above carried a careful explanation and a PR author never saw it. A guard that
# reads as wired and reports nothing is the exact shape this file's docstring is
# about, so the announcement is tested end to end rather than by reading it.
def _pr_env(monkeypatch, tmp_path):
    """Env of a PR running in Actions, with a job summary to write to."""
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feat/some-branch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    return summary


def test_drift_reaches_the_job_summary(tmp_path, monkeypatch, request):
    """The half that renders on the PR's own checks page."""
    summary = _pr_env(monkeypatch, tmp_path)
    _announce_drift(["Architecture Map.md"], request)
    body = summary.read_text(encoding="utf-8")
    assert "Architecture Map.md" in body, "the summary does not name what drifted"
    assert "python3 scripts/generate_vault_map.py" in body, \
        "the summary does not tell the author how to regenerate"
    assert "not** block" in body, \
        "the summary does not say the PR is unblocked, so it reads as a failure"


def test_the_annotation_goes_to_stderr_not_stdout():
    """★Mirrors test_inspector_failure_diagnostics_reach_the_log. On 2026-08-12 a
    ::warning on stdout was eaten whole by a $GITHUB_STEP_SUMMARY redirect; the
    unit-tests step pipes pytest's stdout through `tee`. stderr survives both."""
    import inspect
    src = inspect.getsource(_announce_drift)
    assert "file=sys.stderr" in src, \
        "the drift annotation left stderr — a redirect or a pipe will eat it again"
    # ★ The positive alone is VACUOUS and was measured so: switching the
    # capture-suspended branch to stdout left this test green, because the
    # `capture is None` fallback still contained the string it looked for. A
    # substring check over source passes on the wrong occurrence.
    assert "sys.stdout" not in src, \
        "an annotation print went to stdout; the unit-tests step pipes stdout " \
        "through `tee` and the 2026-08-12 failure had it redirected away entirely"
    assert "global_and_fixture_disabled" in src, \
        "pytest capture is no longer suspended, so the annotation lands in the " \
        "captured output of a skipped test, which is where nobody looked"


def test_the_annotation_escapes_a_percent_so_it_cannot_be_truncated():
    line = _drift_message(["odd%name.md"])
    assert "%" in line and _workflow_escape(line).count("%25") == 1


def test_the_pr_skip_announces_before_it_skips(tmp_path, monkeypatch, request):
    """★THE WIRING TEST. Everything above tests _announce_drift in isolation; this
    one proves the gate actually calls it. Deleting the call would leave every
    other test here green and put the PR author back where they started."""
    summary = _pr_env(monkeypatch, tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Architecture Map.md").write_text("what is committed", encoding="utf-8")

    class _Fake:
        _REPO_DOCS = str(docs)

        @staticmethod
        def build():
            return {"Architecture Map.md": "what the tree says now"}

    monkeypatch.setitem(globals(), "_mod", lambda: _Fake)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        test_the_in_repo_copy_is_current(request)
    assert "Architecture Map.md" in str(excinfo.value)
    assert "Architecture Map.md" in summary.read_text(encoding="utf-8"), \
        "the gate skipped without announcing — the drift is invisible again"


def test_a_clean_tree_announces_nothing(tmp_path, monkeypatch, request):
    """The announcement must be drift-only; a summary that always says something
    is noise the author learns to scroll past."""
    summary = _pr_env(monkeypatch, tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Architecture Map.md").write_text("identical", encoding="utf-8")

    class _Fake:
        _REPO_DOCS = str(docs)

        @staticmethod
        def build():
            return {"Architecture Map.md": "identical"}

    monkeypatch.setitem(globals(), "_mod", lambda: _Fake)
    test_the_in_repo_copy_is_current(request)          # passes, does not skip
    assert summary.read_text(encoding="utf-8") == "", \
        "a matching map still wrote to the job summary"


def test_the_annotation_really_lands_on_stderr_under_pytest(tmp_path):
    """★THE BEHAVIOURAL ONE. The source checks above are a backstop; this runs
    the real function inside a real pytest process with fd 1 and fd 2 pointed at
    different files, which is the only way to see which one it came out of.

    A subprocess because _announce_drift suspends pytest's global capture: while
    it is suspended, sys.stderr is the ORIGINAL object, so rebinding it from
    inside this process would be undone before the write. Separate fds are the
    only honest observation point.
    """
    import os
    import subprocess
    import sys

    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import importlib.util\n"
        "_s = importlib.util.spec_from_file_location('vmg', %r)\n"
        "_m = importlib.util.module_from_spec(_s)\n"
        "_s.loader.exec_module(_m)\n"
        "def test_probe(request):\n"
        "    _m._announce_drift(['PROBE-MARKER.md'], request)\n" % str(_ROOT / "tests" / "test_vault_map_generator.py"),
        encoding="utf-8")
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    env = {**os.environ, "GITHUB_ACTIONS": "true"}
    env.pop("GITHUB_STEP_SUMMARY", None)
    with open(out, "w") as fo, open(err, "w") as fe:
        subprocess.run([sys.executable, "-m", "pytest", str(probe), "-q",
                        "-p", "no:cacheprovider"],
                       cwd=str(tmp_path), stdout=fo, stderr=fe, env=env, timeout=300)
    stdout, stderr = out.read_text(encoding="utf-8"), err.read_text(encoding="utf-8")
    assert "PROBE-MARKER.md" in stderr, (
        "the ::warning did not reach stderr — pytest capture swallowed it, or it "
        "was printed somewhere a redirect will eat.\nstdout=%r" % stdout[-400:])
    assert "PROBE-MARKER.md" not in stdout, \
        "the ::warning came out on stdout, which the unit-tests step pipes through `tee`"
