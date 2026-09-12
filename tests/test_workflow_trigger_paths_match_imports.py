#!/usr/bin/env python3
"""tests/test_workflow_trigger_paths_match_imports.py — a post-deploy workflow
must trigger on every file that can change what it publishes.

NO NETWORK.

★ 2026-09-12 — A FILTER NAMED ONE FILE, AND THE PREDICATES HAD LEFT IT.

sitemap-snapshot.yml scoped its push trigger to main.py, with a comment calling
that "the file that builds the XML". The predicates deciding WHICH URLs are
emitted are moved out of main.py ON PURPOSE — util/thin_content says so in its
own docstring, and tests/test_sitemap_thin_gate::test_the_gate_is_emission_only
enforces the move. So the policy moved next to its predicate and the trigger
stayed behind. 41 missed rebuilds in 34 days, each a green CI over a stale
artefact; measured by re-deriving the graph as of each commit's parent.

★ SECOND PASS — IT WAS NEVER ONE WORKFLOW.

Sweeping every push `paths:` filter in .github/workflows found the same defect
in whats-new-post-deploy-purge.yml: it purges the Cloudflare copy of
/api/v1/whats-new "when a push changes what the feed publishes", named four
modules, and routes/infra_growth.whats_new reads EIGHTEEN. Sixteen absent. Same
shape, different artefact, up to 3600s of a stale public page.

public-api-programmatic-access.yml was audited and is CORRECT: it probes the
live Cloudflare edge with stdlib urllib, so what it guards is a CF config
change that is not in this repo. Nothing can move out of its named files.

★ WHAT THIS FILE ACTUALLY DEFENDS. Not "the list is right" — lists rot. That
every guarded filter EQUALS its import graph, plus two escape hatches that are
written down and that fail when they go stale:

    extras    a real dependency no import names (data/platform_updates.json is
              read off disk through STORE_PATH). Asserted to still exist.
    excluded  derived but deliberately not worth a run. Asserted to be STILL
              DERIVED — an exclusion a refactor left behind fails here rather
              than sitting as a hole nobody can see.

★ WHAT IT CANNOT DO. It cannot see a published set that moves because the
DATABASE changed — no deploy, no push, no trigger. That is what the crons are
for, and test_the_cron_survives_as_the_drift_net (sibling file) keeps one.

Run standalone:   python3 tests/test_workflow_trigger_paths_match_imports.py
Run under pytest: pytest tests/test_workflow_trigger_paths_match_imports.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.workflow_trigger_modules import (          # noqa: E402
    GUARDED, BuilderEntryMissing, builder_modules,
)

#: ★ PINNED BECAUSE EACH WAS ALREADY MISSED ONCE BY A FROM-MEMORY LIST.
#:
#:   util/thin_content            LANE 3; its docstring says the query lives
#:                                there and not in the builder. Four missed
#:                                rebuilds (#4500, #4483, #4164, #4149).
#:   util/facility_ner_noindex    named in that same docstring.
#:   util/facility_headline       owns MW_PLAUSIBLE_MAX. #4500 routed
#:                                thin_content.evidence THROUGH it — and a
#:                                from-memory list missed it while describing
#:                                #4500.
#:   util/sitemap_redirects       redirecting_slug_set, imported directly by
#:                                the builder; arrived in #4459 and was missed
#:                                the same way.
#:   routes/facility_slug         ★ stable_hash8 is the hash8 suffix of EVERY
#:                                facility URL (main.py:33597). It has near-zero
#:                                churn because it is FROZEN on purpose, not
#:                                because it is inert — it was considered for
#:                                the exclusion list and refused. Pinned so
#:                                that reasoning cannot be quietly redone.
#:
#: A subset-of-the-filter assertion cannot see the DERIVATION itself shrinking.
#: This can.
LOAD_BEARING = {
    ".github/workflows/sitemap-snapshot.yml": (
        "main.py",
        "util/thin_content.py",
        "util/facility_ner_noindex.py",
        "util/facility_headline.py",
        "util/sitemap_redirects.py",
        "routes/facility_slug.py",
    ),
    ".github/workflows/whats-new-post-deploy-purge.yml": (
        "routes/infra_growth.py",
        "routes/platform_updates.py",
    ),
}


def _push_paths(workflow):
    import yaml
    with open(os.path.join(ROOT, workflow), encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    on = wf[True] if True in wf else wf["on"]
    return list(on["push"].get("paths") or [])


def _negations(paths):
    """GitHub path filters accept `!pattern` to EXCLUDE. This guard has no room
    for one: an exclusion can switch a module off while the positive entry it
    contradicts is still sitting in the list, so every check here would keep
    reading it as covered. Treated as unsupported and named, rather than parsed
    into a silent green."""
    return [p for p in paths if p.startswith("!")]


def _as_regex(entry):
    """GitHub path-filter glob -> regex. `**` crosses directories, `*` does
    not. Plain entries fall through as literals, which is what the filters use
    today; the pattern support is here so widening one line to 'util/**' stays
    legible to this guard instead of reading as a stale entry."""
    out, i = [], 0
    while i < len(entry):
        c = entry[i]
        if c == "*":
            if entry[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _covered(path, paths):
    return any(_as_regex(p).match(path) for p in paths)


# --------------------------------------------------------------------------
# the derivation must actually derive something
# --------------------------------------------------------------------------

def test_the_derivation_finds_every_guarded_entry_point():
    """★ Guards every other assertion here. A slice that collapses to {} makes
    'is it in the filter' vacuously true for everything."""
    bad = []
    for g in GUARDED:
        got = g.derived(ROOT)
        if len(got) < g.floor:
            bad.append("%s: only %d module(s) derived from %s:%s (floor %d)"
                       % (g.workflow, len(got), g.entry_module,
                          g.entry_symbol, g.floor))
        elif g.entry_module not in got:
            bad.append("%s: entry module %s missing from its own slice"
                       % (g.workflow, g.entry_module))
    assert not bad, "the slice collapsed:\n  " + "\n  ".join(bad)


def test_a_renamed_entry_point_is_loud():
    """The derivation must refuse to measure nothing rather than return {}."""
    for g in GUARDED:
        try:
            builder_modules(ROOT, entry_module=g.entry_module,
                            entry_symbol="_no_such_entry_point_at_all")
        except BuilderEntryMissing:
            continue
        raise AssertionError(
            "%s: builder_modules() returned a set for an entry point that does "
            "not exist — a rename would silently empty this filter check"
            % g.workflow)


def test_the_load_bearing_modules_are_still_reached_and_still_trigger():
    """★ A subset check cannot see the derivation shrinking — and it cannot see
    a module EXCLUDED out of the filter either.

    ★ 2026-09-12, found by mutation. Checking only `in derived` left a hole the
    exact size of the original bug: add util/thin_content.py to `excluded` and
    delete its line from the YAML in the same commit, and every check agreed —
    expected() dropped it, so the missing-module check passed; it was still
    derived, so the stale-exclusion check passed; it was no longer listed, so
    the stale-entry check passed. Silent green, LANE 3 no longer triggering a
    rebuild, which is #4500 all over again through the escape hatch added to
    let a genuinely inert module out.

    So these are asserted to be DERIVED *and* COVERED BY THE FILTER. An
    exclusion can never reach one of them.
    """
    bad = []
    for g in GUARDED:
        got = g.derived(ROOT)
        paths = _push_paths(g.workflow)
        for want in LOAD_BEARING.get(g.workflow, ()):
            if want not in got:
                bad.append("%s: the import graph no longer reaches %s"
                           % (g.workflow, want))
            elif want in g.excluded:
                bad.append("%s: %s is LOAD-BEARING and sits in `excluded` — an "
                           "exclusion cannot be used on one of these"
                           % (g.workflow, want))
            elif not _covered(want, paths):
                bad.append("%s: %s is derived but does not trigger the workflow"
                           % (g.workflow, want))
    assert not bad, (
        "load-bearing coverage regressed:\n  " + "\n  ".join(bad)
        + "\nThese decide what gets published. If the slice cannot see one, or "
          "an exclusion removes one, the runs go missing again exactly as they "
          "did before #4506.")


# --------------------------------------------------------------------------
# ★ THE SYNC. Deleting one entry from a workflow's paths: turns this red.
# --------------------------------------------------------------------------

def test_no_module_that_changes_the_artefact_is_missing_from_the_push_filter():
    """★ THE ONE THAT WOULD HAVE CAUGHT #4500.

    Every file whose content can change what the workflow publishes must be in
    `paths:`, or a push that changes it merges green while the published
    artefact keeps serving the old content until the next cron.

    Regenerate a filter with:
        python3 scripts/workflow_trigger_modules.py --workflow <name>
    """
    bad = []
    for g in GUARDED:
        paths = _push_paths(g.workflow)
        missing = [m for m in sorted(g.expected(ROOT)) if not _covered(m, paths)]
        if missing:
            bad.append("%s is missing %d:\n      %s"
                       % (g.workflow, len(missing), "\n      ".join(missing)))
    assert not bad, (
        "files can change a published artefact and do NOT trigger its "
        "workflow:\n  " + "\n  ".join(bad)
        + "\nA push touching one merges green while the published copy stays "
          "stale until the next cron.")


def test_no_path_entry_that_no_longer_matters():
    """The other direction. A stale entry fires runs for a file that stopped
    mattering and — worse — reads as coverage the filter does not have."""
    bad = []
    for g in GUARDED:
        want = g.expected(ROOT)
        stale = [p for p in _push_paths(g.workflow)
                 if not p.startswith("!")
                 and not any(_as_regex(p).match(m) for m in want)]
        if stale:
            bad.append("%s lists %s" % (g.workflow, ", ".join(stale)))
    assert not bad, (
        "paths: entries nothing reaches any more:\n  " + "\n  ".join(bad)
        + "\nEither the graph moved and the entry should go, or the derivation "
          "stopped seeing it — check which before deleting. A deliberate "
          "non-import dependency belongs in that workflow's `extras`.")


# --------------------------------------------------------------------------
# the escape hatches must not become places to hide things
# --------------------------------------------------------------------------

def test_every_exclusion_is_still_derived():
    """★ AN EXCLUSION THAT STOPPED BEING DERIVED IS A HOLE, NOT A CHOICE.

    `excluded` says "the graph reaches this and we chose not to run". If a
    refactor means the graph no longer reaches it, the entry is now excluding
    nothing — and would silently keep excluding whatever takes that path next.
    """
    bad = []
    for g in GUARDED:
        got = g.derived(ROOT)
        for rel in sorted(g.excluded):
            if rel not in got:
                bad.append("%s excludes %s, which the import graph no longer "
                           "reaches" % (g.workflow, rel))
    assert not bad, "stale exclusion(s):\n  " + "\n  ".join(bad)


def test_every_extra_names_a_file_that_exists():
    """`extras` are dependencies no import names. A deleted one is a line that
    looks like coverage and is not."""
    bad = []
    for g in GUARDED:
        for rel in sorted(g.extras):
            if not os.path.exists(os.path.join(ROOT, rel)):
                bad.append("%s: extra %s does not exist" % (g.workflow, rel))
    assert not bad, "\n  ".join(bad)


def test_every_escape_hatch_carries_a_written_reason():
    """An undocumented exception is indistinguishable from an oversight, which
    is how the filter this replaces came to look deliberate."""
    bad = []
    for g in GUARDED:
        for label, d in (("extras", g.extras), ("excluded", g.excluded)):
            for rel, why in sorted(d.items()):
                if not (why or "").strip() or len(why.strip()) < 30:
                    bad.append("%s %s[%r] has no real reason" % (g.workflow, label, rel))
    assert not bad, "\n  ".join(bad)


def test_no_negated_path_entry_quietly_switches_a_module_off():
    """★ `paths: ['util/thin_content.py', '!util/thin_content.py']` excludes the
    file while still LISTING it. Every other assertion here reads the positive
    entry and passes — measured, all three were green on exactly that."""
    bad = []
    for g in GUARDED:
        neg = _negations(_push_paths(g.workflow))
        if neg:
            bad.append("%s: %s" % (g.workflow, ", ".join(neg)))
    assert not bad, (
        "exclusion pattern(s) in paths:\n  " + "\n  ".join(bad)
        + "\nAn exclusion can switch off a module whose positive entry is still "
          "listed, which every check here would read as covered. Remove the "
          "entry, or put it in that workflow's `excluded` with a reason.")


def test_every_path_entry_names_a_file_that_exists():
    bad = []
    for g in GUARDED:
        for p in _push_paths(g.workflow):
            if "*" in p or "?" in p or p.startswith("!"):
                continue
            if not os.path.exists(os.path.join(ROOT, p)):
                bad.append("%s: %s" % (g.workflow, p))
    assert not bad, "paths: names file(s) that do not exist:\n  " + "\n  ".join(bad)


def test_every_filter_retriggers_on_its_own_edits():
    """Editing a workflow or the deriver changes which pushes run it; that edit
    must itself run, or the first execution under new rules is the next cron."""
    bad = []
    for g in GUARDED:
        paths = _push_paths(g.workflow)
        for rel in (g.workflow, "scripts/workflow_trigger_modules.py"):
            if not _covered(rel, paths):
                bad.append("%s does not list %s" % (g.workflow, rel))
    assert not bad, "\n  ".join(bad)


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("✓ %s" % _name)
            except AssertionError as _e:
                _failed += 1
                print("✗ %s: %s" % (_name, _e))
    print("\n%s — %d failure(s)" % ("FAILED" if _failed else "PASSED", _failed))
    sys.exit(1 if _failed else 0)
