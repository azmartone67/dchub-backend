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
    TABLES_OUT.write_text(json.dumps({
        "routes_json_include": sorted(routes_json),
        "routes_json_exclude": sorted(routes_json_exclude),
        "worker_paths": sorted(worker_paths),
        "worker_prefixes": sorted(worker_prefixes),
    }, indent=2))
    print(f"_routes.json include: {len(routes_json)} entries, "
          f"exclude: {len(routes_json_exclude)} entries "
          f"({len(routes_json) + len(routes_json_exclude)}/98 rules)")
    print(f"worker PHASE_282_RAILWAY_PATHS (+ dispatch-guard exacts): {len(worker_paths)} entries")
    print(f"worker PHASE_282_PREFIXES (+ dispatch-guard prefixes): {len(worker_prefixes)} entries")
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
        # the bare prefix before its first "<".  "/news/<slug>" truncated to
        # "/news/" hits the deliberate "/news/" EXCLUDE and reads as uncovered,
        # while the paths it actually serves match the "/news/*" include and are
        # worker-routed — GET /news/some-article carries x-dc-worker-version,
        # measured 2026-09-05.  Substituting a segment keeps the bare-path
        # exclusions doing their job without condemning the children.
        probe = re.sub(r"<[^>]+>", "_", r)
        if not by_routes_json(probe):
            missing_routes_json.append(r)
        if not by_worker(probe):
            missing_worker.append(r)
    return missing_routes_json, missing_worker


def load_baseline() -> dict[str, set[str]]:
    """The enumerated debt.  Two lists, because the two tables fail differently.

    A path missing from _routes.json `include` means the worker is NEVER
    INVOKED — 404, or a same-named static file answers with no
    x-dc-worker-version header.  A path missing from the worker's own tables
    means the worker runs and refuses to forward — 403.  Baselining the union
    would let a path silently migrate from one failure to the other.
    """
    if not BASELINE.exists():
        return {"missing_routes_json": set(), "missing_worker": set()}
    data = json.loads(BASELINE.read_text())
    return {
        "missing_routes_json": set(data.get("missing_routes_json", [])),
        "missing_worker": set(data.get("missing_worker", [])),
    }


def cmd_diff(_args) -> int:
    flask = set(json.loads(FLASK_ROUTES_OUT.read_text()))
    tables = json.loads(TABLES_OUT.read_text())
    missing_routes_json, missing_worker = _uncovered(flask, tables)
    base = load_baseline()

    current = {"missing_routes_json": set(missing_routes_json),
               "missing_worker": set(missing_worker)}
    added = {k: sorted(current[k] - base[k]) for k in current}
    fixed = {k: sorted(base[k] - current[k]) for k in current}
    # Counted in PATHS, not in table-rows: one new route missing from both
    # tables is ONE new mis-registration, not two. Mixing the units made the
    # failure message say "2 NEW ... 129 pre-existing" for a single probe path.
    new_paths = set(added["missing_routes_json"]) | set(added["missing_worker"])
    n_added = len(new_paths)
    n_known = len(set(missing_routes_json) | set(missing_worker))

    LABEL = {
        "missing_routes_json":
            "missing from dchub-frontend/_routes.json `include` — the worker is "
            "NEVER INVOKED for these (404, or a same-named static file answers "
            "with NO x-dc-worker-version header)",
        "missing_worker":
            "missing from dchub-frontend/_worker.js PHASE_282 tables — the worker "
            "runs and declines to forward (403)",
    }
    for key in ("missing_routes_json", "missing_worker"):
        rows = sorted(current[key])
        if not rows:
            continue
        print(f"::warning::{len(rows)} Flask HTML route(s) {LABEL[key]}:")
        for r in rows[:30]:
            print(f"  - {r}{'   ★NEW' if r in added[key] else ''}")
        if len(rows) > 30:
            print(f"  …and {len(rows)-30} more")

    for key in ("missing_routes_json", "missing_worker"):
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

    if n_added:
        print(f"::error::route-table coherence ADVISORY — {n_added} NEW Flask route(s) "
              f"are not in the CF tables. This PR added mis-registration; the "
              f"{n_known - n_added} pre-existing uncovered route(s) are baselined "
              f"and ignored.")
        for key in ("missing_routes_json", "missing_worker"):
            for r in added[key]:
                print(f"  ★NEW UNCOVERED [{key}]: {r}")
        print("")
        print("  Fix: add the path to dchub-frontend/_routes.json 'include' — but MIND "
              "THE CAP. It is 98 rules counting include AND exclude TOGETHER (not 100, "
              "not include-only), it sits at 97/98, and rule 99 is dropped SILENTLY. "
              "Run `node scripts/check-edge-caps.mjs` in dchub-frontend before adding; "
              "do not read the cap off _routes.json, nothing in the file states it. "
              "With no slot free, the answer is usually to serve the page under an "
              "already-included prefix instead.")
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
