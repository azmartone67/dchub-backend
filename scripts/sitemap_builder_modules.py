#!/usr/bin/env python3
"""scripts/sitemap_builder_modules.py — which files can change the emitted
sitemap URL set.

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

#: The function that produces the PUBLISHED artefact. It calls
#: _build_sitemap_sections for the gated families and again, through
#: _build_sitemap_facilities_ungated, for the AI family — so one entry covers
#: both. Naming the outer one also covers the shard/index rendering.
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


def builder_modules(root=REPO_ROOT, with_provenance=False):
    """Repo-relative paths whose CONTENT can change the emitted URL set.

    Always includes the entry module itself. Deterministic: sorted, and it
    reads the tree rather than importing anything, so it needs no database,
    no network and none of the app's runtime dependencies.
    """
    cache = {}
    if _index(root, ENTRY_MODULE, cache) is None:
        raise BuilderEntryMissing("cannot parse %s" % ENTRY_MODULE)
    if ENTRY_SYMBOL not in _index(root, ENTRY_MODULE, cache)[0]:
        raise BuilderEntryMissing(
            "%s has no top-level %s(). The sitemap entry point was renamed or "
            "moved; this derivation is measuring nothing until it is updated."
            % (ENTRY_MODULE, ENTRY_SYMBOL))

    seen, queue, found = set(), [(ENTRY_MODULE, ENTRY_SYMBOL)], {}
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
            if not target or target == rel or target == ENTRY_MODULE:
                continue
            found.setdefault(target, set()).add(
                "%s:%s -> %s" % (rel, sym, orig or dotted))
            queue.append((target, _MODULE_CONSTANTS))
            if orig:
                queue.append((target, orig))

    found.setdefault(ENTRY_MODULE, set()).add("the builder itself")
    if with_provenance:
        return {k: sorted(v) for k, v in sorted(found.items())}
    return sorted(found)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--why", action="store_true",
                    help="show what reaches each module")
    args = ap.parse_args(argv)
    if args.why:
        for rel, why in builder_modules(args.root, with_provenance=True).items():
            print(rel)
            for w in why:
                print("      <- %s" % w)
    else:
        for rel in builder_modules(args.root):
            print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
