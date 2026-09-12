#!/usr/bin/env python3
"""tests/test_sitemap_snapshot_paths_match_imports.py — the rebuild trigger
must list every file that can change the sitemap.

NO NETWORK.

★ 2026-09-12 — THE FILTER NAMED ONE FILE AND THE PREDICATES HAD LEFT IT.

sitemap-snapshot.yml scoped its `push` trigger to main.py, with a comment
calling that "the file that builds the XML". The predicates deciding WHICH
URLs are emitted are moved out of main.py ON PURPOSE — util/thin_content
says so in its own docstring, and tests/test_sitemap_thin_gate::
test_the_gate_is_emission_only enforces the move. So the policy moved next to
its predicate and the trigger stayed behind.

MEASURED over the 34.1 days of run history GitHub still holds (400 runs,
2026-08-09 .. 2026-09-12): 84 commits touched a module the builder reads, 19
rebuilt only because they also touched main.py, and 65 MISSED THE REBUILD
ENTIRELY — 1.91 a day. The most recent, PR #4500, merged 3.5 minutes AFTER the
cron that would have carried it, and 2h25m later the published shards still
listed all 17 URLs it had just removed.

★ WHY A TEST AND NOT A LONGER LIST. The list is the part that goes stale. This
  derives the set from the import graph every run (scripts/
  sitemap_builder_modules.py) and compares it to the YAML, so a predicate moved
  into a NEW module fails CI instead of silently missing its rebuilds. Writing
  the derivation rather than a list immediately found three modules a
  from-memory answer had missed, two of them load-bearing — see _LOAD_BEARING
  below. Neither side is hand-editable alone.

★ WHAT THIS CANNOT DO. It cannot see a URL set that moves because the DATABASE
  changed — no deploy, no push, no trigger. That is the 4-hourly cron's job and
  test_the_cron_survives_as_the_drift_net (sibling file) is what keeps it.

Run standalone:   python3 tests/test_sitemap_snapshot_paths_match_imports.py
Run under pytest: pytest tests/test_sitemap_snapshot_paths_match_imports.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.sitemap_builder_modules import (          # noqa: E402
    ENTRY_MODULE, ENTRY_SYMBOL, BuilderEntryMissing, builder_modules,
)

WF = os.path.join(ROOT, ".github", "workflows", "sitemap-snapshot.yml")
WF_REL = ".github/workflows/sitemap-snapshot.yml"
DERIVER_REL = "scripts/sitemap_builder_modules.py"

#: Entries that are in the filter for a reason the import graph cannot state.
#: The workflow must retrigger on its own edits, and the derivation itself
#: decides the rest of the list — changing it changes which files matter, so a
#: push that edits it must rebuild too.
SELF_REFERENTIAL = frozenset({WF_REL, DERIVER_REL})

#: ★ A FLOOR, NOT AN EXPECTED ANSWER. `builder_modules()` returning {} would
#: make every "is it in the filter" assertion below vacuously true — the exact
#: shape of silent green this file exists to prevent. 21 modules derive today.
#: See [[feedback_scan_that_can_find_nothing_needs_a_floor]].
MIN_DERIVED = 15

#: ★ PINNED BECAUSE EACH ONE WAS ALREADY MISSED ONCE.
#:
#:   util/thin_content            LANE 3. Its docstring: "★ THIS QUERY LIVES
#:                                HERE, NOT IN main._build_sitemap_sections".
#:                                Changed by #4500, #4483, #4164, #4149 — four
#:                                missed rebuilds.
#:   util/facility_ner_noindex    named in that same docstring as the shape
#:                                already established for the other noindexed
#:                                class.
#:   util/facility_headline       owns MW_PLAUSIBLE_MAX. #4500 routed
#:                                thin_content.evidence THROUGH it, so the cap
#:                                now decides emission — and a from-memory
#:                                list of "the modules the builder reads"
#:                                missed it even while describing #4500.
#:   util/sitemap_redirects       redirecting_slug_set, imported directly by
#:                                the builder. Arrived in #4459 the same
#:                                morning and was missed the same way.
#:
#: If the derivation stops reaching one of these it has regressed, and a
#: subset-of-the-filter assertion would not notice.
LOAD_BEARING = (
    "main.py",
    "util/thin_content.py",
    "util/facility_ner_noindex.py",
    "util/facility_headline.py",
    "util/sitemap_redirects.py",
)


def _push_paths():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    on = wf[True] if True in wf else wf["on"]
    return list(on["push"].get("paths") or [])


def _as_regex(entry):
    """GitHub path-filter glob -> regex. `**` crosses directories, `*` does
    not. Plain entries fall through as literals, which is what the filter uses
    today; the pattern support is here so that widening one line to
    'util/**' stays legible to this guard instead of reading as a stale entry.
    """
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


def _covered(module, paths):
    return any(_as_regex(p).match(module) for p in paths)


def _negations(paths):
    """GitHub path filters accept `!pattern` to EXCLUDE. This guard has no room
    for one: an exclusion can switch a module off while the positive entry it
    contradicts is still sitting in the list, so every check here would keep
    reading it as covered. Treated as unsupported and named, rather than parsed
    into a silent green."""
    return [p for p in paths if p.startswith("!")]


# --------------------------------------------------------------------------
# the derivation must actually derive something
# --------------------------------------------------------------------------

def test_the_derivation_finds_the_sitemap_builder():
    """★ Guards every other assertion in this file. If the entry point is
    renamed, builder_modules raises instead of returning an empty set, and the
    subset checks below cannot pass by measuring nothing."""
    mods = builder_modules(ROOT)
    assert len(mods) >= MIN_DERIVED, (
        "only %d module(s) derived from %s:%s (floor %d). The slice collapsed; "
        "every 'is it in the filter' assertion in this file is now vacuous."
        % (len(mods), ENTRY_MODULE, ENTRY_SYMBOL, MIN_DERIVED)
    )
    assert ENTRY_MODULE in mods, "the entry module is not in its own slice"


def test_a_renamed_entry_point_is_loud():
    """The derivation must refuse to measure nothing rather than return {}."""
    import scripts.sitemap_builder_modules as sbm
    real = sbm.ENTRY_SYMBOL
    try:
        sbm.ENTRY_SYMBOL = "_no_such_builder_function"
        try:
            sbm.builder_modules(ROOT)
        except BuilderEntryMissing:
            pass
        else:
            raise AssertionError(
                "builder_modules() returned a set for an entry point that does "
                "not exist — a rename would silently empty the filter check")
    finally:
        sbm.ENTRY_SYMBOL = real


def test_the_load_bearing_predicate_modules_are_still_reached():
    """★ A subset check cannot see the derivation itself shrinking. Each of
    these was missed by a from-memory list once already."""
    mods = set(builder_modules(ROOT))
    missing = [m for m in LOAD_BEARING if m not in mods]
    assert not missing, (
        "the import-graph slice no longer reaches %s. These decide which URLs "
        "are emitted; if the slice cannot see them, the paths filter derived "
        "from it will drop them and the rebuilds go missing again."
        % ", ".join(missing)
    )


# --------------------------------------------------------------------------
# ★ THE SYNC. Deleting one entry from the workflow's paths: turns this red.
# --------------------------------------------------------------------------

def test_no_module_the_builder_reads_is_missing_from_the_push_filter():
    """★ THE ONE THAT WOULD HAVE CAUGHT #4500.

    Every module whose content can change the emitted URL set must be in
    `paths:`, or a push that changes it merges green and the published sitemap
    keeps serving the old URLs until the next 4-hourly cron.

    Regenerate the list with:  python3 scripts/sitemap_builder_modules.py
    """
    paths = _push_paths()
    missing = [m for m in builder_modules(ROOT) if not _covered(m, paths)]
    assert not missing, (
        "%d module(s) can change the sitemap and do NOT trigger a rebuild:\n"
        "  %s\n"
        "A push touching one of these merges green while the published shards "
        "keep the old URLs for up to four hours. Add them to paths: in %s "
        "(python3 %s prints the full list)."
        % (len(missing), "\n  ".join(missing), WF_REL, DERIVER_REL)
    )


def test_no_path_entry_the_builder_no_longer_reads():
    """The other direction. A stale entry fires rebuilds for a file that stopped
    mattering, and — worse — reads as coverage this filter does not have."""
    derived = set(builder_modules(ROOT))
    stale = [p for p in _push_paths()
             if p not in SELF_REFERENTIAL and not p.startswith("!")
             and not any(_as_regex(p).match(m) for m in derived)]
    assert not stale, (
        "paths: lists %s, which nothing in the sitemap build imports any more. "
        "Either the import graph moved and the entry should go, or the "
        "derivation stopped seeing it — check which before deleting."
        % ", ".join(stale)
    )


def test_no_negated_path_entry_quietly_switches_a_module_off():
    """★ `paths: ['util/thin_content.py', '!util/thin_content.py']` is a filter
    that excludes the file while still LISTING it. Every other assertion in
    this file would read the positive entry and pass. The derived-filter model
    has no room for an exclusion, so an exclusion has to be loud."""
    bad = _negations(_push_paths())
    assert not bad, (
        "paths: contains exclusion pattern(s) %s. An exclusion can switch off a "
        "module whose positive entry is still listed, which every check in this "
        "file would still read as covered. The filter is derived from the "
        "import graph (python3 %s) — remove the entry instead of negating it."
        % (", ".join(bad), DERIVER_REL)
    )


def test_every_path_entry_names_a_file_that_exists():
    """A path entry naming a deleted file never matches, so it is not a filter
    — it is a line that looks like one."""
    gone = [p for p in _push_paths()
            if "*" not in p and "?" not in p and not p.startswith("!")
            and not os.path.exists(os.path.join(ROOT, p))]
    assert not gone, "paths: names file(s) that do not exist: %s" % ", ".join(gone)


def test_the_filter_still_retriggers_on_its_own_edits():
    """Editing the workflow or the derivation changes which pushes rebuild; that
    edit must itself rebuild, or the first run under new rules is the next cron."""
    paths = _push_paths()
    for rel in sorted(SELF_REFERENTIAL):
        assert _covered(rel, paths), (
            "%s decides when a rebuild happens and is not in its own paths: "
            "filter" % rel
        )


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
