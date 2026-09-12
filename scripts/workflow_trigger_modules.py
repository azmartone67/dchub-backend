#!/usr/bin/env python3
"""scripts/workflow_trigger_modules.py — which files can change what a
post-deploy workflow publishes.

★ 2026-09-12 (second pass) — GENERALISED, BECAUSE IT WAS NOT ONE WORKFLOW.

This shipped as sitemap_builder_modules.py, for sitemap-snapshot.yml alone. A
sweep of every push `paths:` filter in .github/workflows found the same defect
in whats-new-post-deploy-purge.yml: it purges the Cloudflare copy of
/api/v1/whats-new "when a push changes what the feed publishes", named four
modules, and the feed's handler reads EIGHTEEN — sixteen of them absent. Same
failure: green CI, stale published page, up to the zone TTL (3600s).

(public-api-programmatic-access.yml was audited and is CORRECT as written: it
probes the live Cloudflare edge with stdlib urllib, so the thing it guards is a
CF configuration change that is not in this repo at all. Nothing can move out
of its named files, and it carries a 6-hourly cron besides.)

So the slice is now generic and the per-workflow facts live in GUARDED below.

★ TWO ESCAPE HATCHES, BOTH REQUIRING A WRITTEN REASON, BOTH SELF-INVALIDATING.
  An import graph is not the whole truth in either direction:

    extras   — a real dependency the graph CANNOT see. /api/v1/whats-new reads
               data/platform_updates.json off disk through STORE_PATH; no
               import names it. The guard asserts each extra still exists.
    excluded — derived, but deliberately not worth a run. The guard asserts
               each exclusion is STILL IN the derived set, so one left behind
               by a refactor fails rather than lingering as a silent hole.

  Both are visible in one place and reviewed. That is the point: the previous
  filter's exceptions were invisible because the filter WAS the exception.

--- the original finding, unchanged ---

Which files can change the emitted sitemap URL set.

★ 2026-09-12 — THE PUSH FILTER NAMED ONE FILE, AND THE PREDICATES LEFT IT.

.github/workflows/sitemap-snapshot.yml scoped its `push` trigger to `main.py`,
with a comment calling that "the file that builds the XML". That premise went
stale the first time a predicate deciding WHICH URLs get emitted was moved out
of main.py on purpose — and the repo has been moving them out deliberately ever
since. util/thin_content.contentless_slug_set says so in its own docstring:

    ★ THIS QUERY LIVES HERE, NOT IN main._build_sitemap_sections, and that is
      not cosmetic

tests/test_sitemap_thin_gate::test_the_gate_is_emission_only enforces the move
by refusing `power_mw` inside a builder query. So the policy moved next to its
predicate, by design, and the rebuild trigger stayed behind.

MEASURED 2026-09-12 over the 34.1 days of run history GitHub still holds
(2026-08-09 .. 2026-09-12, 400 runs). The graph was re-derived AS OF EACH
COMMIT'S PARENT — applying today's graph backwards over-counts, because
util/sitemap_redirects only landed on 2026-09-12 and pulls
routes/facility_profile_page in with it:

    commits on main in the window                 1,461
    evaluated against their own-era graph           984
    MISSED THE REBUILD ENTIRELY                      41   (1.20/day)

(Today's graph over the same window gives 65. That is the number of extra runs
this filter ADDS, not a count of misses — see the concurrency note in the
workflow.)

The most recent is PR #4500 (4df6ccce4, merged 12:38:17Z), which changed
util/thin_content.evidence so 17 URLs leave both sitemap families. The last
rebuild before it was the 12:34:45Z cron on 69619bb9c — 3.5 minutes EARLIER.
At 15:03Z the published shards still served 6,846 / 10,000 / 8,886, i.e. all
17 still published, with the correction waiting on the 16:19Z cron. The origin
was already serving the fix: those pages render noindex, and the sitemap
listing them did not.

★ WHY THIS IS DERIVED AND NOT A LIST. A remembered list is how the filter got
  here. Writing this analysis rather than a list immediately found three
  modules a from-memory answer missed — util/sitemap_redirects (arrived in
  #4459 the same morning), util/facility_headline (the module #4500 ITSELF
  routes evidence() through), and routes/facility_profile_page (reached via
  sitemap_redirects.served_slugs). Two of the three are the load-bearing ones.
  See [[feedback_fix_scoped_from_a_truncated_list]].

★ WHAT "READS" MEANS HERE — a symbol-level slice, not a module closure.
  _rebuild_sitemap_snapshot imports one or two names out of large route
  modules; those modules' own router wiring is not read by the build. A whole-
  module import closure over this repo returns 303 modules and a name-keyed one
  returns 978 — i.e. "every file", which is not a filter. Recursion here
  follows only bindings the sliced code actually references:

    * a function symbol pulls in what its body references,
    * a module pulls in its top-level DATA statements (SEED_MARKETS and
      friends are the URL set for three families),
    * binding lookup is scope-aware, so a local named `conn` does not resolve
      to some other function's `from routes._iso_common import conn`.

★ RECURSION STOPS AT THE ENTRY MODULE. db_utils does `from main import
  _record_circuit_failure` for the circuit breaker, so following edges back
  into main.py re-enters main's whole namespace and drags in the application.
  main.py is the entry and is always in the returned set; re-entering it adds
  no path and costs precision.

★ THIS CANNOT SEE DATA. The URL set also moves when facilities appear in the
  database with no deploy at all. That is what the 4-hourly cron is for, and
  widening the push filter does not replace it.

Used by tests/test_sitemap_snapshot_paths_match_imports.py, which fails CI when
a module in this set is not in the workflow's `paths:` filter.

Run standalone:   python3 scripts/sitemap_builder_modules.py
"""
import argparse
import ast
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Kept as the sitemap's entry point and as the default, so the original
#: call shape still works. main._rebuild_sitemap_snapshot calls
#: _build_sitemap_sections for the gated families and again, through
#: _build_sitemap_facilities_ungated, for the AI family — so one entry covers
#: both, and naming the outer one also covers the shard/index rendering.
ENTRY_MODULE = "main.py"
ENTRY_SYMBOL = "_rebuild_sitemap_snapshot"

#: Vendored or mirrored trees ONLY. A copy of the app under github-repo/ is not
#: the app; a path entry there would fire rebuilds for edits that deploy
#: nothing. Nothing that Railway actually runs belongs in here — app/db.py is
#: real code this repo deploys, so it stays derivable even though nothing in
#: today's slice reaches it. A skip that masks nothing today is still the place
#: a future predicate would hide.
VENDORED = (
    "node_modules/", "PATCHES/", "github-repo/", "dchub-mcp-v2.1/",
    "mcp-directory/", "venv/", ".venv/",
)

_MODULE_CONSTANTS = "<module constants>"


class BuilderEntryMissing(RuntimeError):
    """The entry point named above is not in main.py under that name.

    Raised rather than returned as an empty set: a derivation that quietly
    finds nothing makes every "is it in the filter" assertion vacuously true,
    which is the failure mode this whole file exists to prevent.
    See [[feedback_scan_that_can_find_nothing_needs_a_floor]].
    """


def _resolve(root, dotted):
    """Dotted module name -> repo-relative .py path, or None if not ours."""
    base = dotted.replace(".", os.sep)
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(os.path.join(root, cand)):
            return None if cand.startswith(VENDORED) else cand
    return None


def _index(root, rel, _cache):
    """(top-level defs, module-level import bindings, module-level data)."""
    if rel in _cache:
        return _cache[rel]
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        _cache[rel] = None
        return None
    defs = {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    data = ast.Module(
        body=[n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))],
        type_ignores=[],
    )
    binds = {}
    for node in tree.body:                      # module level only, on purpose
        _bind(node, binds)
    _cache[rel] = (defs, binds, data)
    return _cache[rel]


def _bind(node, out):
    if isinstance(node, ast.Import):
        for a in node.names:
            out[a.asname or a.name.split(".")[0]] = (a.name, None)
    elif isinstance(node, ast.ImportFrom):
        if node.level or not node.module:       # relative imports stay local
            return
        for a in node.names:
            out[a.asname or a.name] = (node.module, a.name)


def _local_binds(node):
    """Imports written anywhere inside one symbol — function-level imports are
    the norm here, because several of these modules import each other back and
    a module-level import would close the cycle (see util/thin_content)."""
    out = {}
    for sub in ast.walk(node):
        _bind(sub, out)
    return out


def _referenced(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def builder_modules(root=REPO_ROOT, with_provenance=False,
                    entry_module=None, entry_symbol=None):
    """Repo-relative paths whose CONTENT can change what the entry point emits.

    Always includes the entry module itself. Deterministic: sorted, and it
    reads the tree rather than importing anything, so it needs no database,
    no network and none of the app's runtime dependencies.

    entry_module/entry_symbol default to the sitemap builder. They are
    PARAMETERS rather than the module-level constants they used to be: the
    guard needs to slice several entry points in one run, and rebinding a
    global between calls is how a test ends up measuring the wrong thing.
    """
    entry_module = entry_module or ENTRY_MODULE
    entry_symbol = entry_symbol or ENTRY_SYMBOL
    cache = {}
    if _index(root, entry_module, cache) is None:
        raise BuilderEntryMissing("cannot parse %s" % entry_module)
    if entry_symbol not in _index(root, entry_module, cache)[0]:
        raise BuilderEntryMissing(
            "%s has no top-level %s(). The entry point was renamed or moved; "
            "this derivation is measuring nothing until it is updated."
            % (entry_module, entry_symbol))

    seen, queue, found = set(), [(entry_module, entry_symbol)], {}
    while queue:
        rel, sym = queue.pop()
        if (rel, sym) in seen:
            continue
        seen.add((rel, sym))
        ix = _index(root, rel, cache)
        if ix is None:
            continue
        defs, binds, data = ix
        node = data if sym == _MODULE_CONSTANTS else defs.get(sym)
        if node is None:
            continue
        scope = dict(binds)
        local = _local_binds(node)
        scope.update(local)
        for name in _referenced(node) | set(local):
            if name in defs:
                queue.append((rel, name))
                continue
            if name not in scope:
                continue
            dotted, orig = scope[name]
            target = _resolve(root, dotted)
            # Stop at the entry module: db_utils imports back into main for the
            # circuit breaker, and following that edge re-enters the whole app.
            if not target or target == rel or target == entry_module:
                continue
            found.setdefault(target, set()).add(
                "%s:%s -> %s" % (rel, sym, orig or dotted))
            queue.append((target, _MODULE_CONSTANTS))
            if orig:
                queue.append((target, orig))

    found.setdefault(entry_module, set()).add("the entry point itself")
    if with_provenance:
        return {k: sorted(v) for k, v in sorted(found.items())}
    return sorted(found)


class Guarded(object):
    """One workflow whose push `paths:` filter is derived from an import graph.

    workflow      repo-relative path of the .yml
    entry_module  the module holding the function that produces the artefact
    entry_symbol  that function
    extras        {path: reason} — real dependencies the import graph CANNOT
                  see (data files read off disk, the workflow's own files).
                  The guard asserts each still exists on disk.
    excluded      {path: reason} — derived, but deliberately not worth a run.
                  The guard asserts each is STILL DERIVED, so an exclusion left
                  behind by a refactor fails instead of quietly widening.
    floor         minimum plausible derived count; below it the slice has
                  collapsed and every subset assertion is vacuous.
    """

    def __init__(self, workflow, entry_module, entry_symbol,
                 extras=None, excluded=None, floor=1):
        self.workflow = workflow
        self.entry_module = entry_module
        self.entry_symbol = entry_symbol
        self.extras = dict(extras or {})
        self.excluded = dict(excluded or {})
        self.floor = floor

    def derived(self, root=REPO_ROOT):
        return set(builder_modules(root, entry_module=self.entry_module,
                                   entry_symbol=self.entry_symbol))

    def expected(self, root=REPO_ROOT):
        """Exactly what `paths:` should contain."""
        return (self.derived(root) - set(self.excluded)) | set(self.extras)


#: The workflows whose triggers are derived rather than remembered.
#:
#: ★ public-api-programmatic-access.yml is deliberately NOT here. It probes the
#:   live Cloudflare edge from outside with stdlib urllib; what it guards is a
#:   CF configuration change that does not live in this repo, so there is no
#:   import graph to drift from. Audited 2026-09-12.
GUARDED = (
    Guarded(
        workflow=".github/workflows/sitemap-snapshot.yml",
        entry_module="main.py",
        entry_symbol="_rebuild_sitemap_snapshot",
        floor=15,
        extras={
            ".github/workflows/sitemap-snapshot.yml":
                "the workflow decides when a rebuild happens; an edit to it "
                "must rebuild, or the first run under new rules is the cron",
            "scripts/workflow_trigger_modules.py":
                "this file derives the filter, so changing it changes which "
                "pushes rebuild",
        },
        excluded={
            "utils/cache.py":
                "BoundedCache only. market_brief and hyperscaler_brief build "
                "_BRIEF_CACHE/_PDF_CACHE at import, and the builder reads "
                "SEED_MARKETS / SEED_HYPERSCALERS — constants, never the "
                "cache. A change here cannot SILENTLY move the URL set: the "
                "only reachable failure is raising at import, which fails the "
                "build loudly. 0 qualifying commits 2026-08-09..09-12. "
                "★ routes/facility_slug.py was considered for this list and "
                "REFUSED: stable_hash8 computes the hash8 suffix of every "
                "facility URL (main.py:33597). Its 0 commits mean it is FROZEN "
                "on purpose — 'MUST stay byte-identical' — not that it is "
                "inert. Low churn is not low relevance.",
        },
    ),
    Guarded(
        workflow=".github/workflows/whats-new-post-deploy-purge.yml",
        entry_module="routes/infra_growth.py",
        entry_symbol="whats_new",
        floor=12,
        extras={
            ".github/workflows/whats-new-post-deploy-purge.yml":
                "as above — an edit to the trigger must fire the trigger",
            "scripts/workflow_trigger_modules.py":
                "derives this filter too, so a change to the slice changes "
                "which pushes purge — the edit must exercise itself",
            "scripts/purge_whats_new_after_deploy.py":
                "the purge itself; a change to how it waits or reads must be "
                "exercised on the push that makes it",
            "data/platform_updates.json":
                "★ THE DEPENDENCY NO IMPORT NAMES. platform_updates."
                "published_updates reads this file through STORE_PATH "
                "(routes/platform_updates.py:53) and splices the block into "
                "/api/v1/whats-new. Editing the JSON changes the published "
                "feed with no code change at all.",
            "routes/capability_announcements.py":
                "stage_announcement_pr() is the designed AUTHOR of "
                "data/platform_updates.json. The feed never imports it, so "
                "this is over-broad rather than load-bearing — kept because a "
                "spurious purge is cheap and a stale marketing page is not. "
                "Named here so the next reader knows it is a choice.",
        },
    ),
)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--workflow", default=None,
                    help="basename of one guarded workflow (default: all)")
    ap.add_argument("--why", action="store_true",
                    help="show what reaches each module")
    args = ap.parse_args(argv)
    for g in GUARDED:
        if args.workflow and not g.workflow.endswith(args.workflow):
            continue
        print("# %s  (%s:%s)" % (g.workflow, g.entry_module, g.entry_symbol))
        if args.why:
            prov = builder_modules(args.root, with_provenance=True,
                                   entry_module=g.entry_module,
                                   entry_symbol=g.entry_symbol)
            for rel, why in prov.items():
                print(rel)
                for w in why:
                    print("      <- %s" % w)
        else:
            for rel in sorted(g.expected(args.root)):
                print("      - '%s'" % rel)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
