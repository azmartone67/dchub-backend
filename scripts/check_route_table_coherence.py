#!/usr/bin/env python3
r"""Route-table coherence — is every Flask HTML route reachable at the edge?

Phase ZZZZZ (2026-05-23) opened this class: /pockets/<slug> 404'd even though
Flask had the handler, because the same path must be in BOTH
dchub-frontend/_routes.json `include` AND dchub-frontend/_worker.js's PHASE_282
tables.  A path absent from `include` never reaches the worker, and the two
outcomes are BOTH silent:

  * 404 — the handler exists in Flask and is unreachable.
  * 200 from a same-named static file — the page looks alive and the handler
    never ran.  The tell is a missing x-dc-worker-version response header.

★ 2026-09-05 — this became a RATCHET.  It used to be advisory-only, because
~78% of discovered routes read as uncovered on every PR: a constant-red
non-signal that could never be made blocking (see the "flip to required once
warnings are clean for a week" note it shipped with — they never got clean).
The debt is now ENUMERATED in scripts/route_table_baseline.json, and anything
NEW fails.  Same shape as dchub-frontend/scripts/check-edge-caps.mjs and
tests/test_canonical_counts_drift.py's KNOWN_STALE_COUNT_DEBT: the list may
shrink, never grow.

★ WHY AN AST WALK AND NOT THE OLD LINE REGEX.  The previous extractor was

    ROUTE_RE = re.compile(r"@\w+\.route\(\s*['\"]([^'\"]+)['\"]")

which records the DECORATOR's path verbatim.  A blueprint can carry a
url_prefix, and then the served path is a different string entirely:

    redeem_tracking_bp = Blueprint("redeem_tracking", __name__,
                                   url_prefix="/api/v1/redeem")   # routes/redeem_tracking.py
    @redeem_tracking_bp.route("/click", methods=["GET", "POST"])

The regex yields "/click" — an HTML-looking path with no table entry, so it
reads as uncovered.  The real path is "/api/v1/redeem/click", which /api/* both
covers and (being an API path) excludes from this check entirely.  Dozens of
these.  Baselining the regex's output would have frozen a list of fictions.

Prefixes come from two places and BOTH are resolved here:
  1. Blueprint(name, __name__, url_prefix="/x")   — the constructor, common case
  2. app.register_blueprint(bp, url_prefix="/x")  — the registration site

The AST walk also picks up multi-line Blueprint() constructor calls (15 in this
repo) that a line regex cannot see at all, and app.add_url_rule() paths.

★ WHY THE WORKER HALF DOES NOT BLOCK (measured 2026-09-05).

_routes.json is a DECLARATIVE table: `include` minus `exclude` is the complete
answer to "does the worker run for this path", so that half is sound and it is
the half that fails the build.  The worker half asks "does the worker FORWARD
this to Flask", and that is decided by imperative code, not a table.  All 47
static worker-only entries in the baseline were probed at the live edge; 9 came
back with an x-railway-request-id header, meaning the origin answered them and
the model was simply wrong.  Three independent reasons, none fixable by reading
the two tables harder:

  1. PHASE_282 IS ONE BRANCH OF NINE.  _worker.js has 4 proxyToRailway() and 5
     proxyWithRetry() call sites.  /facilities/directory, /poe, /poe/query,
     /news/rss and /press-release/rss are forwarded by other branches —
     /facilities/directory comes back tagged
     x-dc-hub-source: facility-profile-dynamic-backend.
  2. ZONE-LEVEL ROUTES LIVE OUTSIDE THIS REPO.  /agents-md, /agents-md-inline
     and /mcp-server-card.json reach Railway with NO x-dc-worker-version at all
     — the Pages worker never ran and something at the zone forwarded them.
     Nothing in _worker.js can predict those.
  3. "THE WORKER KNOWS THE PATH" IS NOT "THE WORKER FORWARDS IT".  The whole
     /mcp family (/mcp, /mcp/, /mcp/health, /mcp/manifest, /mcp/sse,
     /mcp/tools/, /mcp/tools/call) answers 200 from the WORKER ITSELF
     (x-dc-hub-source: worker-mcp-get-health) and never reaches Flask.  Widening
     the model to every `pathname ===` / `startsWith` literal in the file was
     tried and scored WORSE: false positives 9 -> 3, but 8 new blind spots,
     because it cannot tell answering from forwarding.

So PHASE_282 membership is SUFFICIENT for forwarding and not NECESSARY, and a
gate that fails on a not-necessary condition cries wolf — which is how this
gate got switched off the first time.  The worker half is printed as a notice,
its measured exceptions are recorded in the baseline's
`worker_measured_forwarded`, and only _routes.json can turn the build red.

★ 2026-09-16 — THIS GATE IS TIME-DEPENDENT, AND THAT IS THE DESIGN.

The frontend checkout is UNPINNED, so the CF tables it diffs against are
whatever dchub-frontend's default branch holds AT RUN TIME.  A dchub-frontend
merge can therefore flip this gate red on a backend commit that already passed,
with no backend change: on 2026-09-16 the same SHA ce63775a9 passed at 01:25Z
(run 35044024785) and failed at 02:06Z (run 35046680105) because frontend#1491
swapped the "/spare-capacity/*" include for "/listings/*" in between.

Pinning is the WRONG fix — it rebuilds the vendored mirror #3871 deleted, and a
stale table makes this gate diff live routes against a fiction in BOTH
directions.  The fix is attribution: cmd_route_tables records which frontend
commit was read and the failure message names it, so a reader can tell "the PR
did it" from "the frontend moved" instead of being told the former.  See
_frontend_rev().

★ 2026-09-16 — IT NOW READS _redirects, AS A THIRD STATE AND NOT AS COVERAGE.

Until now only _routes.json and _worker.js were modelled, so a path whose edge
behaviour is a _redirects rule read as uncovered even though it answers
correctly in production — /spare-capacity and /spare-capacity/<ref> were
baselined for exactly that reason, which recorded a fiction.

The fix is NOT to call those covered.  "Covered by _routes.json include" and
"answered by a _redirects rule" are OPPOSITE facts about the Flask handler:

  * include  → the worker runs and forwards; the handler RUNS.
  * redirect → the worker never runs and CF answers from the static pipeline;
    the handler NEVER RUNS.  The path is reachable and may be doing exactly
    what was intended (a 301 stub like /spare-capacity), or a rule may have
    silently SHADOWED a page that was supposed to render — which is the
    /pockets class this whole gate exists to catch.

Collapsing those into one green would blind the gate to the second case, so
they are reported as a THIRD category, `shadowed_by_redirects`, with its own
baseline list.

★ WHAT IS FATAL DID NOT CHANGE.  The blocking set is still "Flask HTML routes
the worker is never invoked for" — exactly what it was before this paragraph.
_redirects only SPLITS that set for reporting and explains each half; a path
does not stop being fatal by acquiring a redirect, and does not become fatal by
losing one.  Migration between the two halves is real (301 ↔ 404) and is
reported loudly, but it is cross-repo and never fails the build, for the same
reason the rot notice below does not.

★ SCOPED TO THE UNCOVERED SET ON PURPOSE — AND IT IS THE FAIL-SAFE DIRECTION.
A _redirects rule is only consulted when the worker is NOT invoked for the path.
Measured: /news/some-article is in the `/news/*` include AND matches the
`/news/* -> /press-release/:splat 200` rule, and it answered WITH
x-dc-worker-version (2026-09-05) — the worker won, the rewrite never fired.
/spare-capacity/abc-def is in no include and matches `/spare-capacity/*`, and it
answered 301 with NO x-dc-worker-version and NO x-railway-request-id
(2026-09-16) — the redirect won.  So shadowing is only checked for paths already
computed as uncovered.  If that precedence is ever wrong, this under-reports
shadows; it can never mislabel a worker-routed path as shadowed.

★ AND THE MATCHER IS DELIBERATELY NOT _glob_re().  See _redirect_re():
_redirects is a DIFFERENT SYNTAX from _routes.json and the bare-path rule is
INVERTED between them.  Reusing _glob_re() here is the obvious mistake.

Run it locally exactly as CI does:

    python3 scripts/check_route_table_coherence.py flask-routes
    python3 scripts/check_route_table_coherence.py route-tables
    python3 scripts/check_route_table_coherence.py diff

★ The three verdict strings this file prints —
    "discovered N Flask HTML routes"
    "route-table coherence ADVISORY"
    "covered by both tables"
  — are GREPPED by check-route-tables.yml's gate-liveness ledger step.  Change
  the wording here and the gate records `unmeasured` forever, silently.  Update
  both in the same commit.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("ROUTE_COHERENCE_ROOT", ".")).resolve()
FRONTEND = pathlib.Path(
    os.environ.get("ROUTE_COHERENCE_FRONTEND", str(ROOT / "dchub-frontend"))
)
BASELINE = ROOT / "scripts" / "route_table_baseline.json"

FLASK_ROUTES_OUT = pathlib.Path(
    os.environ.get("ROUTE_COHERENCE_ROUTES_OUT", "/tmp/flask_html_routes.json")
)
TABLES_OUT = pathlib.Path(
    os.environ.get("ROUTE_COHERENCE_TABLES_OUT", "/tmp/route_tables.json")
)

# Directories that hold no served Flask handler.  The canonical frontend is
# checked out INTO this workspace at whatever ROUTE_COHERENCE_FRONTEND names
# (CI uses dchub-frontend, the same path the vendored mirror occupied until
# #3871), so it is skipped by RESOLVED PATH in _python_files(), not by name.  That repo
# holds 126 .py files today and none defines a route — but a single @app.route
# added to one of its build scripts would otherwise inject a phantom backend
# route and fail this gate on a change that never touched the backend.
SKIP_DIRS = (".git", "node_modules", ".claude", "dchub-frontend", ".venv", "venv")

# Paths we deliberately do NOT expect to proxy through the worker.
SKIP_PATTERNS = (
    "/<",            # catch-all dynamic patterns — handled inside Railway
    "/admin/",       # admin surfaces, intentionally not surfaced via CF
    "/.well-known/", # manifests
)
SKIP_LITERAL = {
    "/",
    "/health", "/robots.txt", "/sitemap.xml", "/favicon.ico",
    "/ai.txt", "/ai-plugin.json", "/llms.txt", "/manifest.json",
    "/.well-known/mcp.json", "/agents.md", "/AGENTS.md",
}

# Flask blueprints use the .get/.post shortcuts too (Flask 2.0+) — 40+ call
# sites here — so a .route-only extractor under-reads.  But FastAPI's @router.get
# uses the IDENTICAL shape, and its prefix comes from app.include_router(prefix=…)
# which is not a Flask concept.  Seven modules in this repo are FastAPI
# (replit_api_routes, publish_routes, services/daily/app, …); scanning them
# yielded bare "/all" and "/refresh" for paths really served at "/publish/all".
# They are a different app behind the same proxy, so they are skipped whole and
# COUNTED, never silently dropped.
ROUTE_DECORATORS = ("route", "get", "post", "put", "patch", "delete")
FASTAPI_MARKERS = ("APIRouter", "FastAPI")


# ── 1. Flask route extraction ────────────────────────────────────────────────

def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_blueprint_call(call: ast.Call) -> bool:
    fn = call.func
    return (isinstance(fn, ast.Name) and fn.id == "Blueprint") or (
        isinstance(fn, ast.Attribute) and fn.attr == "Blueprint"
    )


def _python_files(root: pathlib.Path):
    frontend = FRONTEND.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        here = pathlib.Path(dirpath).resolve()
        if here == frontend or frontend in here.parents:
            dirnames[:] = []
            continue
        rel = os.path.relpath(dirpath, root)
        if any(part in SKIP_DIRS for part in rel.split(os.sep)):
            continue
        for f in filenames:
            if f.endswith(".py"):
                yield pathlib.Path(dirpath) / f


def _register_blueprint_facts(trees: dict[pathlib.Path, ast.AST]):
    """Which blueprint VARIABLES get registered, and with what url_prefix.

    Keyed by variable name rather than by import graph on purpose.  main.py
    holds 656 register_blueprint() calls and 1,745 import nodes of which only
    109 are top-level — the rest are inside functions.  A top-level-import
    regex misses 36 registered blueprints outright; a name-keyed sweep over
    every tree cannot.
    """
    registered: set[str] = set()
    prefixes: dict[str, str] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "register_blueprint" or not node.args:
                continue
            arg = node.args[0]
            if not isinstance(arg, ast.Name):
                continue
            registered.add(arg.id)
            for kw in node.keywords:
                if kw.arg == "url_prefix":
                    val = _const_str(kw.value)
                    if val:
                        prefixes[arg.id] = val
    return registered, prefixes


def _join(prefix: str, rel: str) -> str:
    if not prefix:
        return rel
    return prefix.rstrip("/") + rel


def extract_flask_paths(root: pathlib.Path = ROOT) -> dict[str, str]:
    """Every path this Flask app serves → the file that declares it.

    Includes /api and /static; the HTML filter is applied separately so the
    full set stays available for sanity-checking the extractor itself.
    """
    trees: dict[pathlib.Path, ast.AST] = {}
    for path in _python_files(root):
        try:
            trees[path] = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            # Fail LOUD, not silent: "the scanner could not read it" and "the
            # scanner found nothing" must never be the same outcome.
            print(f"::warning::route extractor could not parse {path}", file=sys.stderr)

    registered, reg_prefix = _register_blueprint_facts(trees)

    found: dict[str, str] = {}
    skipped_fastapi: list[str] = []
    for path, tree in trees.items():
        rel_file = str(path.relative_to(root))

        if any(
            isinstance(n, ast.Name) and n.id in FASTAPI_MARKERS
            or isinstance(n, ast.alias) and n.name in FASTAPI_MARKERS
            for n in ast.walk(tree)
        ):
            skipped_fastapi.append(rel_file)
            continue

        # Blueprint variables declared in THIS file, and their ctor url_prefix.
        ctor_prefix: dict[str, str] = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            if not _is_blueprint_call(node.value):
                continue
            pre = ""
            for kw in node.value.keywords:
                if kw.arg == "url_prefix":
                    pre = _const_str(kw.value) or ""
            for target in node.targets:
                if isinstance(target, ast.Name):
                    ctor_prefix[target.id] = pre

        for node in ast.walk(tree):
            # @owner.route("/x") / .get / .post / …
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                        continue
                    if dec.func.attr not in ROUTE_DECORATORS or not dec.args:
                        continue
                    rel = _const_str(dec.args[0])
                    if not rel or not rel.startswith("/"):
                        continue
                    owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
                    if owner in ctor_prefix and owner not in registered:
                        # A blueprint declared here that nothing ever registers
                        # serves nothing.  Do not report it as uncovered.
                        continue
                    # register_blueprint's url_prefix WINS over the ctor's —
                    # Flask applies the registration-site value.
                    prefix = reg_prefix.get(owner) or ctor_prefix.get(owner, "")
                    found.setdefault(_join(prefix, rel), rel_file)
            # app.add_url_rule("/x", ...) — 46 call sites, invisible to a
            # decorator-only extractor.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "add_url_rule" and node.args:
                    rule = _const_str(node.args[0])
                    if rule and rule.startswith("/"):
                        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
                        prefix = reg_prefix.get(owner) or ctor_prefix.get(owner, "")
                        found.setdefault(_join(prefix, rule), rel_file)
    extract_flask_paths.skipped_fastapi = sorted(skipped_fastapi)
    return found


def static_backed_paths(frontend: pathlib.Path = FRONTEND) -> set[str]:
    """Paths a static file already answers — CF serves those without the worker."""
    static: set[str] = set()
    if not frontend.is_dir():
        return static
    for dirpath, _, filenames in os.walk(frontend):
        for f in filenames:
            if not f.endswith((".html", ".json", ".txt", ".md", ".xml")):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), frontend)
            static.add("/" + rel)
            if rel.endswith(".html"):
                static.add("/" + rel[:-5])
    return static


def html_routes(all_paths, static: set[str]) -> set[str]:
    out = set()
    for path in all_paths:
        if path.startswith("/api") or path.startswith("/static"):
            continue
        if path in SKIP_LITERAL:
            continue
        if any(path.startswith(s) for s in SKIP_PATTERNS):
            continue
        if path in static:
            continue
        out.add(path)
    return out


def cmd_flask_routes(_args) -> int:
    all_paths = extract_flask_paths()
    static = static_backed_paths()
    print(f"  {len(static)} static-backed paths in {FRONTEND}/")
    print(f"  {len(all_paths)} total Flask paths (all prefixes resolved)")
    skipped = getattr(extract_flask_paths, "skipped_fastapi", [])
    print(f"  {len(skipped)} FastAPI module(s) skipped (different app, "
          f"include_router prefixes): {', '.join(skipped) or 'none'}")
    routes = html_routes(all_paths, static)
    FLASK_ROUTES_OUT.write_text(json.dumps(sorted(routes), indent=2))
    print(f"discovered {len(routes)} Flask HTML routes")
    return 0


# ── 2. The two CF routing tables ─────────────────────────────────────────────

def _strip_js_line_comment(line: str) -> str:
    """Drop a trailing // comment, respecting single/double-quoted strings."""
    out, quote, i = [], None, 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                out.append(line[i:i + 2]); i += 2; continue
            if c == quote:
                quote = None
            out.append(c)
        elif c in "'\"`":
            quote = c
            out.append(c)
        elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _js_string_list(src: str, decl: str) -> set[str]:
    r"""Every string literal in the JS array/Set literal named `decl`.

    ★ NOT a regex.  The version this replaced was

        re.search(r"PHASE_282_PREFIXES\s*=\s*\[([^\]]+)\]", src, re.DOTALL)

    and `[^\]]+` stops at the FIRST `]` in the source — which, in this file,
    is a `]` inside a trailing // comment about 12 lines in.  It captured 6 of
    the 36 real prefixes and 134 of the real RAILWAY_PATHS entries, so 266 of
    356 Flask routes read as "missing from the worker tables" when they were
    not.  Baselining that would have frozen 266 fictions.

    Walks bracket depth over comment-stripped lines instead.
    """
    m = re.search(re.escape(decl) + r"\s*=\s*(?:new\s+Set\(\s*)?\[", src)
    if not m:
        return set()
    depth, out, i = 0, set(), m.end() - 1
    for line in src[i:].splitlines():
        code = _strip_js_line_comment(line)
        out.update(re.findall(r"'([^']*)'|\"([^\"]*)\"", code) and
                   [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", code)])
        depth += code.count("[") - code.count("]")
        if depth <= 0:
            break
    return {x for x in out if x.startswith("/")}


def _worker_fallthrough_prefixes(src: str) -> tuple[set[str], set[str]]:
    r"""The dispatch guard forwards more than the two tables.

        if (PHASE_282_RAILWAY_PATHS.has(pathname)
            || PHASE_282_PREFIXES.some(p => pathname.startsWith(p))
            || pathname.startsWith('/unlock/')
            || _IS_DCPI || _IS_DCGI || _IS_POCKETS || _IS_VI) {

    _IS_DCPI and friends are `const _IS_X = pathname === '/x' ||
    pathname.startsWith('/x/')` above the guard.  A checker that reads only the
    two named tables calls /dcpi/<slug>, /dcgi/TX, /pockets/<slug> and
    /visitor-intelligence/auth uncovered — the /pockets class this whole gate
    was built for.  Parsed, not hardcoded, so the guard tracks the worker.
    """
    exact: set[str] = set()
    prefix: set[str] = set()
    guard = re.search(
        r"if\s*\(\s*PHASE_282_RAILWAY_PATHS\.has\(pathname\)(.*?)\)\s*\{",
        src, re.DOTALL)
    blocks = []
    if guard:
        blocks.append(guard.group(1))
        for name in re.findall(r"(_IS_[A-Z0-9_]+)", guard.group(1)):
            d = re.search(re.escape("const " + name) + r"\s*=(.*?);", src, re.DOTALL)
            if d:
                blocks.append(d.group(1))
    for block in blocks:
        code = "\n".join(_strip_js_line_comment(l) for l in block.splitlines())
        prefix.update(re.findall(r"startsWith\(\s*'([^']+)'", code))
        exact.update(re.findall(r"pathname\s*===?\s*'([^']+)'", code))
    return {p for p in exact if p.startswith("/")}, {p for p in prefix if p.startswith("/")}


def _frontend_rev(frontend: pathlib.Path = FRONTEND) -> str:
    r"""Which dchub-frontend commit did this run actually read?

    ★ THE CHECKOUT IS NOT PINNED, ON PURPOSE.  check-route-tables.yml clones
    azmartone67/dchub-frontend with no `ref:`, so every run reads whatever is on
    that repo's default branch AT RUN TIME.  That is the FEATURE — #3871 removed
    a vendored mirror precisely because a pinned copy drifted into a different
    GENERATION of _routes.json and the gate spent weeks diffing live Flask routes
    against a fiction.  Pinning would bring that back.

    The cost of reading live is that this gate is TIME-DEPENDENT: the same
    backend commit can pass at 01:25Z and fail at 02:06Z with no backend change
    at all, because a dchub-frontend PR moved an include out from under it.  That
    is exactly what happened on 2026-09-16 (frontend#1491 swapped
    "/spare-capacity/*" for "/listings/*"); run 35044024785 and run 35046680105
    are the same backend SHA ce63775a9 with opposite verdicts.

    So the honest fix is not a pin, it is ATTRIBUTION: say which frontend commit
    was read, so a reader can check whether that side moved instead of being told
    the PR did it.  Returns "unknown" rather than raising — a missing rev must
    never be able to fail a routing check.
    """
    try:
        import subprocess

        def _git(*args) -> str:
            out = subprocess.run(["git", "-C", str(frontend), *args],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() if out.returncode == 0 else ""

        # ★ `git -C <dir> rev-parse HEAD` WALKS UP.  If the frontend checkout is
        # missing and `dchub-frontend/` is just an empty directory inside THIS
        # repo, that command cheerfully returns the BACKEND's HEAD — and we would
        # print a dchub-backend SHA labelled as the frontend rev, in the very
        # message whose job is to stop people blaming the wrong side.  Confirm the
        # toplevel IS the frontend before believing the rev.
        top = _git("rev-parse", "--show-toplevel")
        if not top or pathlib.Path(top).resolve() != pathlib.Path(frontend).resolve():
            return "unknown"
        return (_git("rev-parse", "HEAD") or "unknown")[:12]
    except Exception:
        # A missing rev must never fail a routing check.
        return "unknown"


def cmd_route_tables(_args) -> int:
    raw = json.loads((FRONTEND / "_routes.json").read_text())
    routes_json = set(raw.get("include", []))
    routes_json_exclude = set(raw.get("exclude", []))
    src = (FRONTEND / "_worker.js").read_text()
    worker_paths = _js_string_list(src, "PHASE_282_RAILWAY_PATHS")
    worker_prefixes = _js_string_list(src, "PHASE_282_PREFIXES")
    extra_exact, extra_prefix = _worker_fallthrough_prefixes(src)
    worker_paths |= extra_exact
    worker_prefixes |= extra_prefix
    rev = _frontend_rev()
    # ★ ORDERED, and recorded rather than re-read. cmd_diff is a DIFFERENT
    # PROCESS; re-parsing _redirects there would read whatever the checkout is
    # then, not what was measured here — the same reason frontend_rev is passed
    # through this file instead of re-derived.
    redirects = parse_redirects()
    TABLES_OUT.write_text(json.dumps({
        "frontend_rev": rev,
        "routes_json_include": sorted(routes_json),
        "routes_json_exclude": sorted(routes_json_exclude),
        "worker_paths": sorted(worker_paths),
        "worker_prefixes": sorted(worker_prefixes),
        "redirects": redirects,
    }, indent=2))
    print(f"read dchub-frontend @ {rev} (UNPINNED — this gate reads that repo's "
          f"default branch at run time; see _frontend_rev())")
    print(f"_routes.json include: {len(routes_json)} entries, "
          f"exclude: {len(routes_json_exclude)} entries "
          f"({len(routes_json) + len(routes_json_exclude)}/98 rules)")
    print(f"worker PHASE_282_RAILWAY_PATHS (+ dispatch-guard exacts): {len(worker_paths)} entries")
    print(f"worker PHASE_282_PREFIXES (+ dispatch-guard prefixes): {len(worker_prefixes)} entries")
    dyn = sum(1 for r in redirects if "*" in r[0] or ":" in r[0])
    print(f"_redirects: {len(redirects)} rules ({dyn} dynamic, {len(redirects) - dyn} static) "
          f"— answers paths the worker is NEVER invoked for; see _redirect_re()")
    return 0


# ── 3. The ratchet ───────────────────────────────────────────────────────────

def _glob_re(glob: str) -> re.Pattern:
    r"""Cloudflare Pages _routes.json glob semantics.

    ★ A FAITHFUL PORT of globToRe() in dchub-frontend/scripts/check-edge-caps.mjs,
    which is the authority here.  Two rules that are easy to get backwards:

      1. "/x/*" matches "/x/...", "/x/" AND BARE "/x".  The frontend's own note
         records getting this wrong and building a whole fix on it, and names the
         paths that prove it: /docs (301), /operators (200), /relay (404),
         /redeem (200) are listed ONLY as "/x/*" and all answer worker-side.
         Measured 2026-09-05: GET /redeem is 200 and DOES carry
         x-dc-worker-version, so a checker that reads "/redeem/*" as not
         covering "/redeem" puts a reachable path in the debt register.
      2. "*" anywhere else is a plain wildcard — "/static/og/*", "/agent*".
    """
    body = ".*".join(re.escape(part) for part in glob.split("*"))
    if glob.endswith("/*"):
        return re.compile(rf"^(?:{body}|{re.escape(glob[:-2])})$")
    return re.compile(rf"^{body}$")


def _covers(globs, path: str) -> bool:
    return any(_glob_re(g).match(path) for g in globs)


def _probe(route: str) -> str:
    r"""The concrete path a Flask rule is TESTED as.

    "/news/<slug>" truncated to "/news/" hits the deliberate "/news/" EXCLUDE
    and reads as uncovered, while the paths it actually serves match the
    "/news/*" include and are worker-routed — GET /news/some-article carries
    x-dc-worker-version, measured 2026-09-05.  Substituting a segment keeps the
    bare-path exclusions doing their job without condemning the children.

    ★ ONE SPELLING, TWO CONSUMERS.  _uncovered() and the _redirects split must
    probe the IDENTICAL string or a route can read as uncovered by one table and
    shadowed by the other, which is a state this checker would then report as
    both.  It lived inline in _uncovered() until the _redirects model needed it
    too; do not re-inline it.
    """
    return re.sub(r"<[^>]+>", "_", route)


# ── 3a. _redirects — the THIRD table ─────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r":[A-Za-z_][A-Za-z0-9_]*")


def _redirect_re(src: str) -> re.Pattern:
    r"""Cloudflare Pages _redirects SOURCE-path semantics.

    ★ THIS IS NOT _glob_re() AND MUST NOT BECOME IT.  _glob_re() is a faithful
    port of globToRe() in dchub-frontend/scripts/check-edge-caps.mjs, which is
    the authority for _routes.json.  It is NOT the authority here, and
    check-edge-caps.mjs never applies it to _redirects — that file only COUNTS
    _redirects lines against the dynamic/static caps, it never matches a path
    against one.  The two syntaxes differ, and the difference is INVERTED on the
    single rule most likely to be assumed shared:

      _routes.json   "/x/*" ALSO matches bare "/x".   (proven live: /redeem,
                     /docs, /operators, /relay are listed only as "/x/*" and all
                     answer worker-side)
      _redirects     "/x/*" does NOT match bare "/x".  A bare-path rule must be
                     written separately — which dchub-frontend does, three times
                     over: "/operators → /operators/", "/transactions →
                     /transactions/", and "/spare-capacity → /listings#..."
                     sitting immediately above "/spare-capacity/* → /listings".
                     That bare line exists BECAUSE the splat line does not cover
                     it; its own comment says so ("The bare path is covered by
                     the line above").

    Borrowing _glob_re() would therefore mark every bare "/x" as shadowed on the
    strength of an "/x/*" rule that cannot answer it — inventing coverage for a
    path that really does 404.

    Two more differences from _routes.json globs:
      * ":name" is a PLACEHOLDER matching exactly one path segment ([^/]+).
        _routes.json has no such syntax.  dchub-frontend uses it today
        ("/press-release/:slug").
      * "*" is a SPLAT and matches across "/" — "/dcip/*" answers
        "/dcip/a/b".  Same as _routes.json's "*", but stated because the
        placeholder right next to it does not.

    Trailing slashes are matched LITERALLY: "/operators" and "/operators/" are
    different rules in that file and are written as such.
    """
    body = []
    for part in src.split("*"):
        pos, chunk = 0, []
        for m in _PLACEHOLDER_RE.finditer(part):
            chunk.append(re.escape(part[pos:m.start()]))
            chunk.append(r"[^/]+")
            pos = m.end()
        chunk.append(re.escape(part[pos:]))
        body.append("".join(chunk))
    return re.compile("^" + ".*".join(body) + "$")


def parse_redirects(frontend: pathlib.Path = FRONTEND) -> list[list]:
    r"""_redirects as [source, destination, status], IN FILE ORDER.

    ★ ORDER IS THE SEMANTICS.  Cloudflare applies the FIRST matching rule and
    stops, so this must stay a list; a set or dict would silently pick a
    different winner than production for any overlapping pair, and the file has
    overlapping pairs by design (the bare "/spare-capacity" line only wins
    because it sits above "/spare-capacity/*").

    Malformed lines are DROPPED LOUDLY, never silently: "the file had a line we
    could not read" and "the file had no such rule" must not be one outcome.
    All 130 rules were 3 fields on 2026-09-16; a 2-field line is legal
    Cloudflare (status defaults to 302) and is accepted as such.
    """
    path = frontend / "_redirects"
    if not path.is_file():
        # Not fatal: the frontend checkout is optional-by-design in this gate
        # (check-route-tables.yml marks the clone continue-on-error).  An absent
        # file means "no shadowing known", which is the same conservative answer
        # this checker gave before it read the file at all.
        print(f"::warning::{path} not found — _redirects shadowing NOT modelled "
              f"this run; routes answered there will read as plain-uncovered.",
              file=sys.stderr)
        return []
    rules: list[list] = []
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/"):
            print(f"::warning::_redirects:{lineno}: unparsed rule {line!r}",
                  file=sys.stderr)
            continue
        status = 302  # Cloudflare's default when the third field is omitted.
        if len(parts) >= 3:
            try:
                status = int(parts[2])
            except ValueError:
                print(f"::warning::_redirects:{lineno}: non-numeric status "
                      f"{parts[2]!r} in {line!r}", file=sys.stderr)
                continue
        rules.append([parts[0], parts[1], status])
    return rules


def redirect_match(rules, path: str):
    """The FIRST _redirects rule answering `path`, or None. Order decides."""
    for rule in rules:
        if _redirect_re(rule[0]).match(path):
            return rule
    return None


def _uncovered(flask: set[str], tables: dict) -> tuple[list[str], list[str]]:
    routes_json = list(tables["routes_json_include"])
    # ★ exclude is not decoration. 15 entries exist precisely to claw bare paths
    # back OUT of a "/x/*" include (/ai, /news, /pricing, /interconnection-queue,
    # …). A checker that reads only `include` calls those covered when the worker
    # is never invoked for them — the exact 404-or-silent-static failure this
    # gate exists to catch, missed on the paths someone deliberately marked.
    routes_json_exclude = list(tables.get("routes_json_exclude", []))
    worker_paths = set(tables["worker_paths"])
    worker_prefixes = tuple(tables["worker_prefixes"])

    def by_routes_json(path: str) -> bool:
        return _covers(routes_json, path) and not _covers(routes_json_exclude, path)

    def by_worker(path: str) -> bool:
        return path in worker_paths or any(path.startswith(p) for p in worker_prefixes)

    missing_routes_json, missing_worker = [], []
    for r in sorted(flask):
        # ★ A dynamic route is tested as a REPRESENTATIVE CONCRETE PATH, not as
        # the bare prefix before its first "<" — see _probe(), which the
        # _redirects split shares so both tables judge the same string.
        probe = _probe(r)
        if not by_routes_json(probe):
            missing_routes_json.append(r)
        if not by_worker(probe):
            missing_worker.append(r)
    return missing_routes_json, missing_worker


def measured_forwarded() -> set[str]:
    r"""Paths the worker-table MODEL calls uncovered that production proves are
    forwarded to Flask.  Evidence, not opinion: each was probed at the live edge
    and came back with an x-railway-request-id header, which appears only when
    the request actually reached the Railway origin.
    """
    if not BASELINE.exists():
        return set()
    return set(json.loads(BASELINE.read_text()).get("worker_measured_forwarded", []))


def load_baseline() -> dict[str, set[str]]:
    """The enumerated debt.  THREE lists, because the tables fail differently.

    A path missing from _routes.json `include` means the worker is NEVER
    INVOKED — 404, or a same-named static file answers with no
    x-dc-worker-version header.  A path missing from the worker's own tables
    means the worker runs and refuses to forward — 403.  Baselining the union
    would let a path silently migrate from one failure to the other.

    ★ shadowed_by_redirects is a SPLIT OF THE FIRST, not a fourth outcome and
    not an allow-list of "fine" paths.  The worker is never invoked for these
    either — the difference is that a _redirects rule answers them instead of a
    404, so they are REACHABLE while their Flask handler is DEAD.  Kept separate
    precisely so the 301-stub case (/spare-capacity, intended) and the
    silently-shadowed-page case (the /pockets class, a bug) cannot report as the
    same green.  Both halves are equally fatal when NEW — see cmd_diff().
    """
    keys = ("missing_routes_json", "shadowed_by_redirects", "missing_worker")
    if not BASELINE.exists():
        return {k: set() for k in keys}
    data = json.loads(BASELINE.read_text())
    return {k: set(data.get(k, [])) for k in keys}


def cmd_diff(_args) -> int:
    flask = set(json.loads(FLASK_ROUTES_OUT.read_text()))
    tables = json.loads(TABLES_OUT.read_text())
    # Recorded by cmd_route_tables, which is a DIFFERENT PROCESS — re-deriving it
    # here would read whatever the checkout is now, not what was measured.
    frontend_rev = tables.get("frontend_rev", "unknown")
    missing_routes_json, missing_worker = _uncovered(flask, tables)
    # ★ A MEASUREMENT BEATS THE MODEL. See measured_forwarded().
    missing_worker = [p for p in missing_worker if p not in measured_forwarded()]
    base = load_baseline()

    # ★ SPLIT, NOT FORGIVEN. Every path here is still one the worker is never
    # invoked for; _redirects only says whether something answers it anyway.
    # Scoped to this set deliberately — see the docstring's "SCOPED TO THE
    # UNCOVERED SET ON PURPOSE": for a worker-routed path the rule never fires,
    # so asking would invent shadows that production does not have.
    redirects = tables.get("redirects", [])
    shadow_rule: dict[str, list] = {}
    plain, shadowed = [], []
    for r in missing_routes_json:
        hit = redirect_match(redirects, _probe(r))
        if hit:
            shadow_rule[r] = hit
            shadowed.append(r)
        else:
            plain.append(r)

    # ★ THE FATAL SET IS THE UNION OF THE TWO HALVES, NOT THE PLAIN HALF.
    # Acquiring a _redirects rule must not pay off a debt and losing one must
    # not create one: what blocks is still, exactly as before this split
    # existed, "a Flask HTML route the worker is newly never invoked for".
    # Reading `added` per-half instead would let a route dodge the ratchet by
    # arriving with a redirect already in place — the /spare-capacity shape,
    # which is precisely the change this gate should still make someone look at.
    routes_json_debt = base["missing_routes_json"] | base["shadowed_by_redirects"]
    added_uncovered = sorted(set(missing_routes_json) - routes_json_debt)

    # A path that only moved between the two halves is neither new debt nor a
    # payment — it is a 404 ↔ 301 change of behaviour, reported on its own below.
    migrated_to_shadow = sorted(base["missing_routes_json"] & set(shadowed))
    migrated_to_plain = sorted(base["shadowed_by_redirects"] & set(plain))
    migrated = set(migrated_to_shadow) | set(migrated_to_plain)

    current = {"missing_routes_json": set(plain),
               "shadowed_by_redirects": set(shadowed),
               "missing_worker": set(missing_worker)}
    added = {k: sorted(current[k] - base[k]) for k in current}
    fixed = {k: sorted(base[k] - current[k]) for k in current}
    for k in ("missing_routes_json", "shadowed_by_redirects"):
        added[k] = [r for r in added[k] if r not in migrated]
        fixed[k] = [r for r in fixed[k] if r not in migrated]

    # Counted in PATHS, not in table-rows: one new route missing from both
    # tables is ONE new mis-registration, not two. Mixing the units made the
    # failure message say "2 NEW ... 129 pre-existing" for a single probe path.
    # ★ ONLY the _routes.json half can FAIL the build — see the module docstring's
    # "WHY THE WORKER HALF DOES NOT BLOCK". The worker half is reported, never fatal.
    n_added = len(added_uncovered)
    n_known = len(set(missing_routes_json) | set(missing_worker))

    LABEL = {
        "missing_routes_json":
            "missing from dchub-frontend/_routes.json `include` AND unanswered by "
            "_redirects — the worker is NEVER INVOKED and nothing else replies "
            "(404, or a same-named static file answers with NO "
            "x-dc-worker-version header)",
        "shadowed_by_redirects":
            "missing from dchub-frontend/_routes.json `include` but ANSWERED BY A "
            "_redirects RULE — REACHABLE, and the Flask handler NEVER RUNS. Two "
            "very different things look identical from here: a deliberate "
            "redirect stub whose handler is meant to be dead, and a real page a "
            "rule has silently SHADOWED. Read the rule and decide PER PATH",
        "missing_worker":
            "missing from dchub-frontend/_worker.js PHASE_282 tables — the worker "
            "runs and declines to forward (403)",
    }
    for key in ("missing_routes_json", "shadowed_by_redirects", "missing_worker"):
        rows = sorted(current[key])
        if not rows:
            continue
        print(f"::warning::{len(rows)} Flask HTML route(s) {LABEL[key]}:")
        for r in rows[:30]:
            rule = shadow_rule.get(r)
            via = f"   via _redirects `{rule[0]} {rule[1]} {rule[2]}`" if rule else ""
            print(f"  - {r}{via}{'   ★NEW' if r in added_uncovered else ''}")
        if len(rows) > 30:
            print(f"  …and {len(rows)-30} more")

    # ★ A MIGRATION IS A BEHAVIOUR CHANGE, NOT A PAYMENT AND NOT NEW DEBT.
    # Loud, and deliberately non-fatal: both directions are normally caused by a
    # dchub-frontend commit, and failing on a cross-repo move is the constant-red
    # non-signal this ratchet exists to end (same reasoning as the rot notice).
    if migrated_to_plain:
        print(f"::warning::{len(migrated_to_plain)} baselined route(s) were ANSWERED BY "
              f"_redirects and no longer are — the rule that covered them is gone, so "
              f"they now 404. This is a REGRESSION, not drift. Restore the rule in "
              f"dchub-frontend/_redirects, or move the line to `missing_routes_json` in "
              f"{BASELINE.relative_to(ROOT)} if the 404 is intended:")
        for r in migrated_to_plain:
            print(f"  - {r}")
    if migrated_to_shadow:
        print(f"::notice::{len(migrated_to_shadow)} baselined route(s) that used to 404 are "
              f"now answered by a _redirects rule. Move the line from `missing_routes_json` "
              f"to `shadowed_by_redirects` in {BASELINE.relative_to(ROOT)}:")
        for r in migrated_to_shadow:
            rule = shadow_rule[r]
            print(f"  - {r}   via `{rule[0]} {rule[1]} {rule[2]}`")

    for key in ("missing_routes_json", "shadowed_by_redirects", "missing_worker"):
        if fixed[key]:
            # A baselined path that is now covered is a PAYMENT.  Say so and make
            # someone delete the line, or the register rots into a permanent hole
            # exactly the way an allow-list does.
            #
            # ★ A NOTICE, NOT A FAILURE — deliberately, and unlike
            # test_canonical_counts_drift.py's rot check, which DOES fail.  That
            # ledger and its debt live in one repo.  This one does not: the thing
            # that pays a debt here is normally a dchub-FRONTEND PR adding an
            # `include` entry.  Failing on rot would turn every subsequent
            # dchub-backend PR red until someone deleted a line in this file —
            # unrelated PRs blocked by a cross-repo change, which is precisely the
            # constant-red non-signal this ratchet exists to end.  So it is loud
            # and it is free to ignore; only NEW entries block.
            print(f"::notice::{len(fixed[key])} baselined route(s) are now covered "
                  f"({key}) — delete them from {BASELINE.relative_to(ROOT)}:")
            for r in fixed[key]:
                print(f"  - {r}")

    if added["missing_worker"]:
        # Reported, NEVER fatal. Going red here would be a coin-flip: measured
        # 2026-09-05, 9 of 47 probed worker-only entries were forwarded to Flask
        # anyway, and the fix this gate would demand (an _routes.json include
        # entry, against a table that was FULL at 98/98 on 2026-09-16) is the wrong
        # action for every one of them.
        print(f"::notice::{len(added['missing_worker'])} route(s) newly absent from "
              f"the _worker.js PHASE_282 tables. ADVISORY ONLY — PHASE_282 membership "
              f"is SUFFICIENT for forwarding, not NECESSARY. Confirm with a live probe "
              f"(x-railway-request-id present = the origin answered) before acting, and "
              f"record the answer in {BASELINE.relative_to(ROOT)}:")
        for r in added["missing_worker"]:
            print(f"  - {r}")

    if n_added:
        # ★ DO NOT SAY "THIS PR ADDED MIS-REGISTRATION".  It said exactly that
        # until 2026-09-16, and it could not know it: the frontend checkout is
        # UNPINNED (see _frontend_rev()), so the CF tables can move with no
        # backend commit at all.  On 2026-09-16 they did — frontend#1491 swapped
        # the "/spare-capacity/*" include for "/listings/*", and this gate went
        # red on backend SHA ce63775a9, which it had passed 41 minutes earlier
        # (runs 35044024785 pass → 35046680105 fail, same SHA).  Every backend PR
        # open at the time was told it had added mis-registration.  A guard that
        # names the wrong cause gets ignored, and this one is not even required,
        # so being ignored is all it takes to kill it.  Name BOTH candidates and
        # hand over the rev that decides between them.
        print(f"::error::route-table coherence ADVISORY — {n_added} NEW Flask route(s) "
              f"are not in the CF tables, read against dchub-frontend @ {frontend_rev}. "
              f"CAUSE IS NOT ASSUMED — it is one of TWO, and this gate cannot tell them "
              f"apart: (a) this PR added a route with no edge entry, or (b) dchub-frontend "
              f"moved an include/exclude out from under it, which needs NO backend commit "
              f"and will fail every open backend PR identically. Check that rev first: if "
              f"its _routes.json changed recently, this is (b) and the PR is innocent. The "
              f"{n_known - n_added} pre-existing uncovered route(s) are baselined and ignored.")
        for r in added_uncovered:
            rule = shadow_rule.get(r)
            if rule:
                print(f"  ★NEW UNCOVERED [shadowed_by_redirects]: {r}")
                print(f"      _redirects answers it: `{rule[0]} {rule[1]} {rule[2]}` — the "
                      f"path is REACHABLE but the Flask handler NEVER RUNS. Intended stub, "
                      f"or a rule that just shadowed a real page? Decide before baselining.")
            else:
                print(f"  ★NEW UNCOVERED [missing_routes_json]: {r}")
        print("")
        print("  Fix A: add the path to dchub-frontend/_routes.json 'include' — but MIND "
              "THE CAP. It is 98 rules counting include AND exclude TOGETHER (not 100, "
              "not include-only), it was FULL at 98/98 on 2026-09-16, and the rule past "
              "the cap is dropped SILENTLY. MEASURE it, do not trust this sentence: run "
              "`node scripts/check-edge-caps.mjs` in dchub-frontend before adding, and do "
              "not read the cap off _routes.json — nothing in the file states it. With no "
              "slot free, serving the page under an already-included prefix, or evicting a "
              "rule that has stopped earning its place, beats spending one.")
        print("  Fix B: if the path only needs to REDIRECT, put it in dchub-frontend/"
              "_redirects instead. That is a separate table with far more room "
              "(124/2000 static on 2026-09-16, MEASURE it) and it costs nothing against "
              "the 98. This checker DOES read _redirects now: the path then reports "
              "under `shadowed_by_redirects` rather than as a 404, and is still NEW — "
              "baseline it in THAT list, which records what actually happens (a "
              "reachable path with a dead Flask handler) instead of calling it uncovered.")
        print(f"  Or, if the route genuinely should not be edge-routed, add it to "
              f"{BASELINE.relative_to(ROOT)} WITH A REASON.")
        return 1

    if n_known:
        # ★ Deliberately does NOT say "ADVISORY". check-route-tables.yml's
        # ledger greps that token to record verdict=fail; printing it on a
        # clean run would peg the gate-liveness board at `fail` forever, which
        # is exactly the constant-red non-signal the ratchet exists to end.
        print(f"::notice::route-table coherence — {n_known} Flask route(s) not in "
              f"the CF tables, ALL BASELINED pre-existing drift, none NEW. "
              f"See {BASELINE.relative_to(ROOT)}.")

    if shadowed:
        # ★ PRINTED ON A GREEN RUN, ON PURPOSE. These paths ARE reachable, so
        # nothing here is failing — but their Flask handlers are dead code, and a
        # count that only appears on red would let that quietly become normal.
        print(f"::notice::{len(shadowed)} of those are ANSWERED BY _redirects, not by "
              f"Flask — reachable paths whose handler never runs. Not a failure; "
              f"listed above so a shadowed real page cannot hide among the stubs.")
    # ★ "covered by both tables" is GREPPED by check-route-tables.yml's ledger.
    # Do not reword it; add lines around it instead.
    print(f"OK — {len(flask)} Flask HTML routes covered by both tables or baselined "
          f"({n_known} known, 0 new).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("flask-routes", help="extract Flask HTML routes")
    sub.add_parser("route-tables", help="extract _routes.json include + _worker.js tables")
    sub.add_parser("diff", help="ratchet: fail on NEW uncovered routes")
    args = ap.parse_args()
    return {
        "flask-routes": cmd_flask_routes,
        "route-tables": cmd_route_tables,
        "diff": cmd_diff,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
