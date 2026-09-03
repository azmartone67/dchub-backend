#!/usr/bin/env python3
"""
DATASET INVENTORY GUARD  (r-dataset, 2026-09-02)
=============================================================================
Derived inventory of the TABLES this backend reads and writes, tiered by
whether the PRODUCT PUBLISHES A CLAIM ABOUT THEM.

Idiom deliberately mirrors scripts/api_response_contract.py:
  extract | baseline | check ; derived JSON baseline ; sanity floor ;
  three-valued PASS / FAIL / UNMEASURED.

TIER-1 PREDICATE (the only tier CI fences on):
  A table is TIER-1 iff at least one of:
    (a) MCP-SERVED  - it is read by the Flask handler that backs a declared
        MCP tool. The tool -> REST-path map is itself derived from code:
        server.mjs trackedTool(...callAPI('<path>')) and
        routes/tools_manifest.py::_TOOL_REST.
    (b) FRESHNESS-CLAIMED - it is read by an in-scope public /api/ handler
        whose response emits a temporal/provenance claim key
        (as_of / last_updated / freshness / provenance / vintage / ...).
  Both are assertions the product makes about the data being current.
=============================================================================
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from typing import Any

REPO = os.environ.get(
    "DINV_REPO",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)  # derived from this file, never a hardcoded checkout path — CI clones elsewhere

SCHEMA_VERSION = 1

EXCLUDED_FILE_RE = re.compile(
    r"^(dchub-frontend/|docs/|tests/|test/|PATCHES/|tmp/|outputs/|results/"
    r"|replit/|github-repo/|mcp-directory/|registries/|cf-workers/|cloudflare/"
    r"|enhancements/|infrastructure_output/|kmz_output/)"
    r"|(^|/)\.venv/|(^|/)venv/|(^|/)node_modules/|(^|/)migrations/"
)

COVERED_PREFIXES = ("/api/",)
EXCLUDED_PATH_RE = re.compile(
    r"^/api/(admin|internal|debug|_)"
    r"|^/api/v1/(admin|internal|debug)"
    r"|^/api/ops/"
)

# A response key that ASSERTS the data is current / sourced. Emitting one of
# these is the product making a claim about the dataset behind it.
CLAIM_KEYS = {
    "as_of", "as_of_date", "as_of_utc", "data_as_of", "asof",
    "last_updated", "last_update", "last_updated_at", "updated_at",
    "last_refresh", "last_refreshed", "refreshed_at", "last_synced",
    "last_sync", "freshness", "data_freshness", "stale", "is_stale",
    "staleness_hours", "age_hours", "age_days", "data_vintage", "vintage",
    "provenance", "snapshot_date", "snapshot_at", "data_date",
    "last_ingested", "ingested_at", "coverage_as_of", "last_seen_at",
}

SQL_NOISE = {
    "select", "where", "from", "join", "on", "and", "or", "as", "by",
    "order", "group", "limit", "offset", "union", "all", "distinct",
    "case", "when", "then", "else", "end", "not", "null", "is", "in",
    "exists", "having", "with", "values", "set", "into", "using",
    "left", "right", "inner", "outer", "full", "cross", "lateral",
    "dual", "only", "returning", "conflict", "do", "nothing", "update",
    "generate_series", "unnest", "json_array_elements", "true", "false",
    "current_date", "current_timestamp", "now", "table", "temp", "temporary",
}
SYS_PREFIXES = ("pg_", "information_schema", "sqlite_")

# Keyword must be UPPERCASE: this codebase writes SQL keywords in caps, and
# lowercase "from the ..." in a docstring is prose, not a query.
_RE_READ = re.compile(r"\b(?:FROM|JOIN)\s+(?:ONLY\s+)?([A-Za-z_][A-Za-z0-9_]*)"
                      r"(?=\s|$|\)|,|;)")
_RE_INSERT = re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_RE_UPDATE = re.compile(r"\bUPDATE\s+(?:ONLY\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+SET\b", re.I)
_RE_DELETE = re.compile(r"\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_RE_CREATE = re.compile(r"\bCREATE\s+(?:UNLOGGED\s+|TEMP\s+|TEMPORARY\s+)?TABLE"
                        r"(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
# `x AS (` is a CTE or a derived-table alias. Never a base table.
_RE_ALIAS = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", re.I)
# `) alias` / `) AS alias` after a derived table.
_RE_DERIVED = re.compile(r"\)\s*(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)
_RE_SQLISH = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE)\b", re.I)
# EXTRACT(YEAR FROM queue_date) / SUBSTRING(x FROM 2) are NOT table reads.
_RE_FROMFN = re.compile(r"\b(EXTRACT|SUBSTRING|TRIM|POSITION|OVERLAY)\s*\([^()]*\)", re.I)


def _norm(t: str) -> str:
    return t.strip().lower()


def _keep(t: str) -> bool:
    t = _norm(t)
    if not t or t in SQL_NOISE:
        return False
    if t.startswith(SYS_PREFIXES):
        return False
    if len(t) < 3:
        return False
    return True


def sql_tables(text: str) -> tuple[set[str], set[str], set[str]]:
    """(reads, writes, created) base-table names in one SQL-ish string."""
    if not _RE_SQLISH.search(text):
        return set(), set(), set()
    prev = None
    while prev != text:                      # EXTRACT(... FROM col) is not a read
        prev = text
        text = _RE_FROMFN.sub(" ", text)
    aliases = {_norm(m) for m in _RE_ALIAS.findall(text)}
    aliases |= {_norm(m) for m in _RE_DERIVED.findall(text)}
    created = {_norm(m) for m in _RE_CREATE.findall(text)}
    reads = {_norm(m) for m in _RE_READ.findall(text)} - aliases - created
    writes = ({_norm(m) for m in _RE_INSERT.findall(text)}
              | {_norm(m) for m in _RE_UPDATE.findall(text)}
              | {_norm(m) for m in _RE_DELETE.findall(text)}) - aliases
    return ({t for t in reads if _keep(t)},
            {t for t in writes if _keep(t)},
            {t for t in created if _keep(t)})


# ---------------------------------------------------------------- AST helpers

def _strings_in(node: ast.AST) -> list[str]:
    """Every string literal under `node`, with f-string literal parts joined."""
    out: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append("".join(
                p.value for p in n.values
                if isinstance(p, ast.Constant) and isinstance(p.value, str)
            ))
    return out


def _claim_keys_in(node: ast.AST) -> set[str]:
    """Claim keys the function EMITS: dict-literal keys and d['k'] = ... targets."""
    found: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    if k.value.lower() in CLAIM_KEYS:
                        found.add(k.value.lower())
        elif isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant) \
                        and isinstance(tgt.slice.value, str) \
                        and tgt.slice.value.lower() in CLAIM_KEYS:
                    found.add(tgt.slice.value.lower())
        elif isinstance(n, ast.Call):
            # jsonify(as_of=..., ...) / dict(as_of=...)
            for kw in n.keywords:
                if kw.arg and kw.arg.lower() in CLAIM_KEYS:
                    found.add(kw.arg.lower())
    return found


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_decorators(fn: ast.AST) -> list[tuple[str, list[str]]]:
    found = []
    for dec in getattr(fn, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        f = dec.func
        if not (isinstance(f, ast.Attribute) and f.attr in ("route", "get", "post")):
            continue
        if not dec.args:
            continue
        path = _str_const(dec.args[0])
        if not path:
            continue
        methods = ["GET"] if f.attr in ("route", "get") else ["POST"]
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                m = [_str_const(e) for e in kw.value.elts]
                m = [x.upper() for x in m if x]
                if m:
                    methods = m
        found.append((path, methods))
    return found


def _in_scope(path: str) -> bool:
    return path.startswith(COVERED_PREFIXES) and not EXCLUDED_PATH_RE.search(path)


_RE_PARAM = re.compile(r"<[^>]*>")


def norm_path(p: str) -> str:
    p = _RE_PARAM.sub("<*>", p.rstrip("/") or "/")
    return p.lower()


# ------------------------------------------------------- MCP tool -> path map

_RE_TRACKED = re.compile(r"trackedTool\(\s*srv\s*,\s*['\"]([A-Za-z0-9_]+)['\"]")
_RE_CALLAPI = re.compile(r"callAPI\(\s*[`'\"]([^`'\"$]*)")


def mcp_tool_paths() -> dict[str, list[str]]:
    """tool name -> [rest path]. Derived from code, two independent sources."""
    tools: dict[str, set[str]] = defaultdict(set)

    # 1. server.mjs — the MCP server's own handlers.
    mjs = os.path.join(REPO, "server.mjs")
    if os.path.exists(mjs):
        src = open(mjs, encoding="utf-8", errors="replace").read()
        marks = [(m.start(), m.group(1)) for m in _RE_TRACKED.finditer(src)]
        for i, (pos, name) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
            for p in _RE_CALLAPI.findall(src[pos:end]):
                if p.startswith("/api/"):
                    tools[name].add(norm_path(p))

    # 2. routes/tools_manifest.py::_TOOL_REST — the published parity map.
    tm = os.path.join(REPO, "routes", "tools_manifest.py")
    if os.path.exists(tm):
        tree = ast.parse(open(tm, encoding="utf-8", errors="replace").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_TOOL_REST" for t in n.targets):
                if isinstance(n.value, ast.Dict):
                    for k, v in zip(n.value.keys, n.value.values):
                        name = _str_const(k)
                        if not name or not isinstance(v, ast.Tuple) or not v.elts:
                            continue
                        p = _str_const(v.elts[0])
                        if p and p.startswith("/api/"):
                            tools[name].add(norm_path(p))
    return {k: sorted(v) for k, v in tools.items()}


# ------------------------------------------------------------------- extract

# --------------------------------------------------- corroboration + registries

def _all_git_files(pat: str) -> list[str]:
    out = subprocess.run(["git", "-C", REPO, "ls-files", pat],
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f]


def ddl_corpus() -> set[str]:
    """Every name that a CREATE TABLE anywhere in the repo defines.

    Includes migrations/ and *.sql, which the read scan excludes: a table
    is corroborated as REAL by its DDL even when only one file reads it.
    """
    names: set[str] = set()
    for rel in _all_git_files("*.sql") + _all_git_files("*.py"):
        if re.search(r"(^|/)node_modules/|(^|/)\.venv/|(^|/)venv/", rel):
            continue
        try:
            src = open(os.path.join(REPO, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _RE_CREATE.findall(src):
            n = _norm(m)
            if _keep(n):
                names.add(n)
    return names


_REGISTRIES = {
    "infra_growth._LAYERS": ("routes/infra_growth.py", "_LAYERS"),
    "data_freshness_radar._DOMAINS": ("routes/data_freshness_radar.py", "_DOMAINS"),
    "_freshness.QUERIES": ("routes/_freshness.py", "QUERIES"),
}


def registry_tables() -> dict[str, list[str]]:
    """table -> [registries watching it]. Derived from the registry literals,
    positionally: entry[1] is the table (or list of tables); a SQL string is
    parsed for its FROM. Column names in entry[2] are NOT tables."""
    watched: dict[str, set[str]] = defaultdict(set)

    def _take(node, label):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if _RE_SQLISH.search(v):
                r, _w, _c = sql_tables(v)
                for t in r:
                    watched[t].add(label)
            elif re.fullmatch(r"[a-z_][a-z0-9_]*", v) and _keep(v):
                watched[_norm(v)].add(label)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for e in node.elts:
                _take(e, label)

    for label, (rel, var) in _REGISTRIES.items():
        full = os.path.join(REPO, rel)
        if not os.path.exists(full):
            continue
        try:
            tree = ast.parse(open(full, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == var for t in n.targets)):
                continue
            val = n.value
            if isinstance(val, ast.Dict):            # QUERIES: {field: sql}
                for v in val.values:
                    _take(v, label)
            elif isinstance(val, (ast.List, ast.Tuple)):
                for e in val.elts:                   # _LAYERS / _DOMAINS rows
                    if isinstance(e, (ast.Tuple, ast.List)) and len(e.elts) >= 2:
                        _take(e.elts[1], label)      # entry[1] IS the table slot
                    else:
                        _take(e, label)
    return {k: sorted(v) for k, v in watched.items()}


# A writer file is DRIVEN if something in this repo actually runs it.
_DRIVER_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml",
                 "Procfile", "railway.json", "railway.toml", "nixpacks.toml",
                 "*.sh")


def driver_corpus() -> str:
    """Concatenated text of everything that LAUNCHES code in this repo."""
    parts = []
    for g in _DRIVER_GLOBS:
        for rel in _all_git_files(g):
            if re.search(r"(^|/)node_modules/|(^|/)\.venv/|(^|/)venv/", rel):
                continue
            try:
                parts.append(open(os.path.join(REPO, rel),
                                  encoding="utf-8", errors="replace").read())
            except OSError:
                pass
    return "\n".join(parts)


def _git_files() -> list[str]:
    out = subprocess.run(["git", "-C", REPO, "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f and not EXCLUDED_FILE_RE.search(f)]


CALL_DEPTH = 2


def _called_names(node: ast.AST) -> set[str]:
    """Names of functions this node calls: f(), mod.f(), obj.f()."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _module_candidates(mod: str, rel: str) -> list[str]:
    """Repo-relative .py paths a module name could resolve to."""
    if not mod:
        return []
    parts = mod.split(".")
    cands = ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]
    here = os.path.dirname(rel)
    if here:
        cands += [here + "/" + "/".join(parts) + ".py",
                  here + "/" + "/".join(parts) + "/__init__.py"]
    return cands



# ═════════════════════════════════════════════════════════════════════════════
# THE FOREIGN GATE  (grafted from the external-origin proposal, 2026-09-02)
# ═════════════════════════════════════════════════════════════════════════════
#
# TIER-1 = CLAIM ∧ FOREIGN. The claim half names the harm — "we told a customer
# this was current and it was five months stale". The foreign half is what makes
# the fence readable: a table whose rows WE mint cannot freeze in the March
# sense, because our own traffic keeps writing it. Backtested over 30 days of
# real commits, claim-alone produced 12 findings/month of which 10 were
# bookkeeping (brain_*, gate_*, media_*); gated on foreign it produced 3, all
# real. Intersecting is not a compromise — it removes exactly the noise class.
#
# A table is FOREIGN if any of these code-only signals holds:
#   S1  a workflow-run loader writes it (the runner-pattern class)
#   S2  a writer file also fetches from an EXTERNAL host (requests/urlopen/…)
#   S3  it is written by a module that reads a bulk source (CSV/Excel/ArcGIS)
#   S4  its name carries a source token seen in an external fetch URL
# Everything else is treated as ours-by-default. A false NEGATIVE here silences
# a table; that is the safe direction, and U2 reports the bulk count so a drift
# in foreignness is visible even though it never fails per-table.

_URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")
_OURS_RE = re.compile(r"https?://(localhost|127\.|.*dchub|.*railway|.*up\.railway)", re.I)
_FETCH_RE = re.compile(
    r"\brequests\.(get|post)\b|\burlopen\b|\bhttpx\.|\bread_csv\b|\bread_excel\b"
    r"|\bfeedparser\b|arcgis|FeatureServer|/query\?")

# Hosts that are our own plumbing or generic infrastructure, not a data source.
_GENERIC_HOSTS = set("""
www api data services service gov com org net io co uk edu us github githubusercontent raw
amazonaws cloudfront googleapis google cdn static app localhost example test dchub railway
vercel render herokuapp openai anthropic claude stripe slack twitter linkedin facebook youtube
maps rest server files download downloads public media img images assets python pypi docs
schema ietf apache opensource creativecommons wikipedia sourceforge fonts gstatic cdnjs
cloudflare resend smithery glama json xml html mcp brain chatgpt agent hub neon upstash sentry
uptimerobot postmark sendgrid twilio
""".split())


def _ext_hosts(text: str) -> set[str]:
    """Second-level labels of EXTERNAL hosts referenced in this file."""
    out = set()
    for m in _URL_RE.finditer(text):
        if _OURS_RE.match(m.group(0)):
            continue
        labels = [x for x in m.group(1).lower().split(".") if x]
        if len(labels) < 2:
            continue
        org = labels[-2]
        if len(org) >= 3 and org not in _GENERIC_HOSTS and not org.isdigit():
            out.add(org)
    return out


def foreign_tables(all_tables, files, texts, writes, workflow_modules):
    """Tables whose rows originate OUTSIDE this system. Returns {table: [signals]}.

    ★ Iterates ALL tables, not just those with writers. Two of the four datasets
    this program exists to catch have ZERO writers anywhere in the repo, so any
    signal read off a writer file cannot see them. The writer-based signals stay
    (they are the strongest when present); the name and schema signals are what
    reach a table nothing writes.
    """
    fetchy = {f for f in files if _FETCH_RE.search(texts.get(f, ""))}
    source_tokens = set()
    for f in fetchy:
        source_tokens |= _ext_hosts(texts.get(f, ""))

    out: dict[str, list[str]] = {}
    for tbl in all_tables:
        sig = []
        wfiles = {s.split(":")[0] for s in writes.get(tbl, ())}
        if wfiles & set(workflow_modules):
            sig.append("workflow_loader")
        if wfiles & fetchy:
            sig.append("fetching_writer")
        # Writer-independent from here down.
        parts = {p for p in tbl.split("_") if len(p) >= 3}
        if parts & source_tokens:
            sig.append("source_token")
        if tbl.startswith("discovered_"):
            # Repo-wide convention: discovered_* is crawl/discovery OUTPUT.
            sig.append("crawl_output")
        if sig:
            out[tbl] = sig
    return out

def extract() -> dict[str, Any]:
    files = _git_files()
    parse_errors: list[str] = []
    fn_tables: dict[tuple[str, str], set[str]] = {}
    fn_calls: dict[tuple[str, str], set[str]] = {}
    fn_claims: dict[tuple[str, str], set[str]] = {}
    imports: dict[tuple[str, str], tuple[list[str], str]] = {}
    handlers: list[tuple[str, str, str, list[str]]] = []
    # Line spans of ANY route handler, per file: a write inside one is
    # REQUEST-DRIVEN (user traffic runs it), not a batch loader.
    req_spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    fn_span: dict[tuple[str, str], tuple[int, int]] = {}
    all_handlers: list[tuple[str, str]] = []

    reads: dict[str, set[str]] = defaultdict(set)      # table -> "file:line"
    writes: dict[str, set[str]] = defaultdict(set)
    creates: dict[str, set[str]] = defaultdict(set)
    route_reads: dict[str, set[str]] = defaultdict(set)   # table -> route ids
    claim_reads: dict[str, set[str]] = defaultdict(set)   # table -> route ids w/ claim
    path_tables: dict[str, set[str]] = defaultdict(set)   # norm path -> tables read
    route_src: dict[str, str] = {}
    serving_files: dict[str, set[str]] = defaultdict(set)  # table -> files that route-read it

    file_text: dict[str, str] = {}                        # rel -> source, for the foreign gate

    for rel in files:
        full = os.path.join(REPO, rel)
        try:
            src = open(full, encoding="utf-8", errors="replace").read()
            file_text[rel] = src
            tree = ast.parse(src)
        except SyntaxError as e:
            parse_errors.append(f"{rel}: {e}")
            continue
        except OSError as e:
            parse_errors.append(f"{rel}: [Errno {e.errno}] {e.strerror}")
            continue

        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and _route_decorators(fn):
                req_spans[rel].append((fn.lineno, getattr(fn, "end_lineno", fn.lineno)))

        # --- per-file function index: name -> direct tables, callees ---
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fr = set()
                for st in _strings_in(fn):
                    r, _w, _c = sql_tables(st)
                    fr |= r
                fn_span[(rel, fn.name)] = (fn.lineno, getattr(fn, "end_lineno", fn.lineno))
                fn_tables[(rel, fn.name)] = fr
                fn_calls[(rel, fn.name)] = _called_names(fn)
                fn_claims[(rel, fn.name)] = _claim_keys_in(fn)
        # --- import map: local name -> [(module_rel_candidates, orig)] ---
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    local = a.asname or a.name
                    imports[(rel, local)] = (_module_candidates(n.module or "", rel), a.name)

        # Every SQL string anywhere in the file -> read/write sites.
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                texts = [(n.value, n.lineno)]
            elif isinstance(n, ast.JoinedStr):
                texts = [("".join(p.value for p in n.values
                                  if isinstance(p, ast.Constant)
                                  and isinstance(p.value, str)), n.lineno)]
            else:
                continue
            for text, lineno in texts:
                r, w, c = sql_tables(text)
                site = f"{rel}:{lineno}"
                for t in r:
                    reads[t].add(site)
                for t in w:
                    writes[t].add(site)
                for t in c:
                    creates[t].add(site)

        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decs = _route_decorators(fn)
                if decs:
                    all_handlers.append((rel, fn.name))
                for p, m in decs:
                    if _in_scope(p):
                        handlers.append((rel, fn.name, p, m))
    # -- resolve each handler through up to CALL_DEPTH hops of the call graph --
    fileset = set(files)

    def _resolve(rel: str, name: str) -> list[tuple[str, str]]:
        """Where a name called from `rel` is defined."""
        if (rel, name) in fn_tables:
            return [(rel, name)]
        imp = imports.get((rel, name))
        if imp:
            cands, orig = imp
            hits = [(c, orig) for c in cands if c in fileset and (c, orig) in fn_tables]
            if hits:
                return hits
        return []

    # Everything reachable from ANY route handler. A write in one of these is
    # on a LIVE path: user traffic or a cron that POSTs to a job endpoint (the
    # runner pattern - tools/infra_fetch.py -> *-ingest.yml - has no Python
    # INSERT in the loader file at all, so file-name matching alone misses it).
    reached: set[tuple[str, str]] = set()
    frontier0 = list(all_handlers)
    for _hop in range(CALL_DEPTH + 1):
        nxt = []
        for key in frontier0:
            if key in reached or key not in fn_tables:
                continue
            reached.add(key)
            for callee in fn_calls.get(key, ()):
                nxt.extend(_resolve(key[0], callee))
        frontier0 = nxt
        if not frontier0:
            break

    for rel, fname, path, methods in handlers:
        seen: set[tuple[str, str]] = set()
        frontier = [(rel, fname)]
        fr: set[str] = set()
        claims: set[str] = set()
        for _hop in range(CALL_DEPTH):
            nxt = []
            for key in frontier:
                if key in seen or key not in fn_tables:
                    continue
                seen.add(key)
                fr |= fn_tables[key]
                claims |= fn_claims.get(key, set())
                for callee in fn_calls.get(key, ()):
                    nxt.extend(_resolve(key[0], callee))
            frontier = nxt
            if not frontier:
                break
        if True:
            for path_, methods_ in [(path, methods)]:
                np = norm_path(path_)
                for method in methods_:
                    if method in ("OPTIONS", "HEAD"):
                        continue
                    rid = f"{method} {path_}"
                    route_src[rid] = f"{rel}:{fname}"
                    for t in fr:
                        route_reads[t].add(rid)
                        serving_files[t].add(rel)
                        path_tables[np].add(t)
                        if claims:
                            claim_reads[t].add(rid)

    # MCP tool -> tables (through the handler that backs its REST path)
    tool_paths = mcp_tool_paths()
    mcp_tables: dict[str, set[str]] = defaultdict(set)   # table -> tools
    tool_hit = {}
    for tool, paths in tool_paths.items():
        hit = set()
        for p in paths:
            hit |= path_tables.get(p, set())
        tool_hit[tool] = sorted(hit)
        for t in hit:
            mcp_tables[t].add(tool)

    ddl = ddl_corpus()
    watched = registry_tables()
    drivers = driver_corpus()
    driven_cache: dict[str, bool] = {}

    # THE FOREIGN GATE. A module counts as workflow-run if a workflow/Procfile/
    # script text names it — the same corpus `_driven` uses, so the two cannot
    # disagree about what "launched by something" means.
    _wf_modules = [f for f in files
                   if f in drivers or os.path.basename(f) in drivers]
    # `all_tables` is computed further down; foreign is filled in there so the
    # signals can run over the corroborated universe rather than raw matches.
    foreign: dict[str, list[str]] = {}

    reached_spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for key in reached:
        if key in fn_span:
            reached_spans[key[0]].append(fn_span[key])

    def _request_driven(site: str) -> bool:
        f, _, ln = site.rpartition(":")
        try:
            n = int(ln)
        except ValueError:
            return False
        return (any(a <= n <= b for a, b in req_spans.get(f, ()))
                or any(a <= n <= b for a, b in reached_spans.get(f, ())))

    def _driven(fpath: str) -> bool:
        if fpath not in driven_cache:
            base = os.path.basename(fpath)
            driven_cache[fpath] = (fpath in drivers) or (base in drivers)
        return driven_cache[fpath]

    def _read_files(t: str) -> int:
        return len({s.rsplit(":", 1)[0] for s in reads.get(t, ())})

    # CORROBORATION. A bare identifier after FROM can still be a CTE or a
    # derived-table alias split across f-string chunks. A name counts as a
    # real table only if the repo corroborates it: its DDL exists, something
    # writes it, or two different files read it.
    all_tables = {t for t in (set(reads) | set(writes))
                  if t in ddl or writes.get(t) or _read_files(t) >= 2}
    uncorroborated = sorted((set(reads) | set(writes)) - all_tables)

    foreign.update(foreign_tables(all_tables, files, file_text, writes, _wf_modules))

    tables: dict[str, Any] = {}
    for t in sorted(all_tables):
        rd = sorted(reads.get(t, ()))
        wr = sorted(writes.get(t, ()))
        mcp = sorted(mcp_tables.get(t, ()))
        cl = sorted(claim_reads.get(t, ()))
        rt = sorted(route_reads.get(t, ()))
        # ★ TIER-1 = CLAIM ∧ FOREIGN. A claim over rows we mint ourselves cannot
        # freeze the way the March cluster froze — our own traffic keeps writing
        # it — so the foreign half is what makes this fence readable rather than
        # a monthly bookkeeping flood. A table that is claimed but ours drops to
        # tier 2: still inventoried, still reachable by the named fences, just
        # not required to carry a registry entry.
        if (mcp or cl) and t in foreign:
            tier = 1
        elif not rd and wr:
            tier = "write_only"
        elif rt:
            tier = 2
        else:
            tier = 3
        tables[t] = {
            "tier": tier,
            "why": (["mcp_served"] if mcp else []) + (["freshness_claimed"] if cl else []),
            "foreign": t in foreign,
            "foreign_signals": foreign.get(t, []),
            "claimed_but_ours": bool(mcp or cl) and t not in foreign,
            "mcp_tools": mcp,
            "claim_routes": cl[:8],
            "serving_routes": len(rt),
            "read_sites": len(rd),
            "write_sites": len(wr),
            "created_in": sorted(creates.get(t, ()))[:3],
            "first_read": rd[0] if rd else None,
            "first_write": wr[0] if wr else None,
            "zero_writer": bool(rd) and not wr,
            # A LIVE write path = a write inside a route handler (user traffic
            # runs it) or in a file some workflow / Procfile / script launches.
            "live_write_paths": sorted(
                {w.rsplit(":", 1)[0] for w in wr
                 if _request_driven(w) or _driven(w.rsplit(":", 1)[0])}),
            "orphan_loader_files": sorted(
                {w.rsplit(":", 1)[0] for w in wr
                 if not _request_driven(w) and not _driven(w.rsplit(":", 1)[0])}),
            "watched_by": watched.get(t, []),
        }

    def n(tier):
        return sum(1 for v in tables.values() if v["tier"] == tier)

    t1 = {k: v for k, v in tables.items() if v["tier"] == 1}
    t1_zero_writer = sorted(k for k, v in t1.items() if v["zero_writer"])
    t1_unwatched = sorted(k for k, v in t1.items() if not v["watched_by"])
    t1_zw_unwatched = sorted(set(t1_zero_writer) & set(t1_unwatched))
    # NO LIVE WRITE PATH: nothing writes it, or its only writers are files no
    # workflow / Procfile / shell script in this repo ever launches.
    t1_no_live_writer = sorted(
        k for k, v in t1.items()
        if not v["live_write_paths"])
    t1_nlw_unwatched = sorted(set(t1_no_live_writer) & set(t1_unwatched))
    write_only = sorted(k for k, v in tables.items() if v["tier"] == "write_only")

    # ★ CLAIMED-ORPHAN. Any table the product publishes as current — tier-1 OR
    # claimed-but-ours — whose writers are all undriven (or which has none).
    # This is the March mechanism stated structurally, and it is deliberately
    # NOT gated on foreignness: metro_fiber_summary is a hand-curated literal
    # written by a one-shot seed script, so it is honestly not foreign, and
    # stretching "foreign" to reach it would be exactly the fragile rule that
    # makes a fence rot. It is claimed, it is orphaned, and that is enough.
    claimed_orphan = sorted(
        k for k, v in tables.items()
        if (v["tier"] == 1 or v.get("claimed_but_ours"))
        and not v["live_write_paths"])

    return {
        "schema_version": SCHEMA_VERSION,
        "_readme": ("DERIVED FILE - do not hand-edit. Regenerate with: "
                    "python3 scripts/dataset_inventory.py baseline"),
        "scope": {
            "covered_prefixes": list(COVERED_PREFIXES),
            "excluded_path_regex": EXCLUDED_PATH_RE.pattern,
            "excluded_file_regex": EXCLUDED_FILE_RE.pattern,
            "tier1_predicate": ("read by the handler backing a declared MCP tool "
                                "(server.mjs / routes/tools_manifest.py) OR read by an "
                                "in-scope public /api/ handler that emits a freshness/"
                                "provenance claim key"),
            "claim_keys": sorted(CLAIM_KEYS),
        },
        "stats": {
            "python_files_scanned": len(files),
            "tables_total": len(tables),
            "tier1_claimed": n(1),
            "tier1_mcp_served": sum(1 for v in tables.values() if v["mcp_tools"]),
            "tier1_freshness_claimed": sum(1 for v in tables.values() if v["claim_routes"]),
            "tier2_served_no_claim": n(2),
            "tier3_internal_not_covered": n(3),
            "write_only_not_covered": n("write_only"),
            "mcp_tools_mapped": len(tool_paths),
            "uncorroborated_names_dropped": len(uncorroborated),
            # THE MARCH-2026 SIGNATURE, published as a NUMBER (no written
            # reason demanded per item): the product asserts these are current,
            # no code in this repo writes them, and no registry watches them.
            "tier1_zero_writer": len(t1_zero_writer),
            "tier1_no_live_writer": len(t1_no_live_writer),
            "tier1_no_live_writer_AND_unwatched": len(t1_nlw_unwatched),
            "tier1_unwatched_by_any_registry": len(t1_unwatched),
            "tier1_zero_writer_AND_unwatched": len(t1_zw_unwatched),
            "registry_watched_tables": len(watched),
        "claimed_orphan_writer": len(claimed_orphan),
            "parse_errors": len(parse_errors),
        },
        "tier1_zero_writer": t1_zero_writer,
        "tier1_no_live_writer": t1_no_live_writer,
        "tier1_no_live_writer_and_unwatched": t1_nlw_unwatched,
        "tier1_zero_writer_and_unwatched": t1_zw_unwatched,
        "write_only_tables": write_only,
        "parse_errors": parse_errors,
        "mcp_tool_tables": tool_hit,
        "tables": tables,
    }


if False:
    s = extract()
    mode = sys.argv[1] if len(sys.argv) > 1 else "extract"
    if mode == "stats":
        print(json.dumps(s["stats"], indent=2))
    else:
        json.dump(s, sys.stdout, indent=1)


# ═════════════════════════════════════════════════════════════════════════════
# BASELINE / CHECK
# ═════════════════════════════════════════════════════════════════════════════

BASELINE_PATH = os.path.join(REPO, "contracts", "dataset_inventory.json")
EXCEPTIONS_PATH = os.path.join(REPO, "contracts", "dataset_inventory_exceptions.json")

# A tier-1 surface this much smaller than baseline means the EXTRACTOR broke,
# not that the product stopped claiming things. UNMEASURED, never a FAIL flood.
SANITY_FLOOR = 0.70


def _load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _exceptions() -> dict[str, set[str]]:
    """table -> {finding codes deliberately allowed}."""
    out: dict[str, set[str]] = defaultdict(set)
    if not os.path.exists(EXCEPTIONS_PATH):
        return out
    try:
        raw = _load(EXCEPTIONS_PATH)
    except Exception:
        return out
    for e in raw.get("allowed", []):
        t = e.get("table")
        if t:
            out[t] |= set(e.get("findings", []))
    return out


def check(baseline_path: str = BASELINE_PATH, verbose: bool = True,
          surface: dict[str, Any] | None = None) -> int:
    """0 = PASS   1 = FAIL   2 = UNMEASURED (a CI failure, never a pass)."""
    unmeasured: list[str] = []
    failures: list[dict[str, Any]] = []

    if not os.path.exists(baseline_path):
        return _emit("UNMEASURED", [], [
            f"baseline missing: {baseline_path} — run "
            f"`python3 scripts/dataset_inventory.py baseline`"], verbose)
    try:
        base = _load(baseline_path)
    except Exception as e:
        return _emit("UNMEASURED", [], [f"baseline unreadable: {e}"], verbose)
    if base.get("schema_version") != SCHEMA_VERSION:
        return _emit("UNMEASURED", [], [
            f"baseline schema_version={base.get('schema_version')} "
            f"!= {SCHEMA_VERSION}; regenerate the baseline"], verbose)

    if surface is not None:
        cur = surface
    else:
        try:
            cur = extract()
        except Exception as e:  # noqa: BLE001 — ANY extractor failure is unmeasured
            return _emit("UNMEASURED", [],
                         [f"extractor raised {type(e).__name__}: {e}"], verbose)

    b_t, c_t = base.get("tables", {}), cur.get("tables", {})

    # ── VACUOUS-PASS GUARDS ────────────────────────────────────────────────
    if not c_t:
        return _emit("UNMEASURED", [], [
            "computed inventory is EMPTY (0 tables). The extractor found "
            "nothing — this is never a pass."], verbose)
    # ★ A MALFORMED BASELINE IS UNMEASURED, NOT A FAILURE. Reading
    # base["stats"]["tier1_claimed"] straight off a truncated or hand-edited
    # file raises KeyError, which surfaces as a traceback and exit 1 — i.e. as
    # "this PR broke something" when the truth is "the guard cannot measure".
    # Conflating those two is the defect this whole guard exists to prevent, so
    # it must not be present in the guard itself.
    for _name, _obj in (("baseline", base), ("computed", cur)):
        if not isinstance(_obj.get("stats"), dict) or \
                "tier1_claimed" not in _obj["stats"]:
            return _emit("UNMEASURED", [], [
                f"{_name} inventory has no stats.tier1_claimed — it is "
                f"truncated, hand-edited, or from an older schema. Regenerate "
                f"with `python3 scripts/dataset_inventory.py baseline`."], verbose)
    b1 = base["stats"]["tier1_claimed"]
    c1 = cur["stats"]["tier1_claimed"]
    if b1 and c1 < b1 * SANITY_FLOOR:
        return _emit("UNMEASURED", [], [
            f"EXTRACTOR COLLAPSE: tier-1 surface {b1} -> {c1} "
            f"(< {int(SANITY_FLOOR * 100)}% floor). The extractor is far more "
            f"likely broken than {b1 - c1} datasets genuinely losing their "
            f"published claim. Refusing to guess."], verbose)
    new_pe = set(cur.get("parse_errors", [])) - set(base.get("parse_errors", []))
    unmeasured += [f"file no longer parses (its tables are unmeasurable): {p}"
                   for p in sorted(new_pe)]

    exc = _exceptions()

    def fail(code, table, msg, fix):
        if code in exc.get(table, ()):
            return
        failures.append({"code": code, "table": table, "why": msg, "fix": fix})

    for t, c in sorted(c_t.items()):
        b = b_t.get(t)

        # (a) A NEW dataset started carrying a published claim, and nothing
        #     watches it. THE ONLY decision this guard ever demands.
        if c["tier"] == 1 and not c["watched_by"]:
            if b is None or b["tier"] != 1:
                fail("NEW_TIER1_UNWATCHED", t,
                     f"now published as current by {c['why']} "
                     f"({', '.join(c['mcp_tools'][:3] or c['claim_routes'][:2])}) "
                     f"but no freshness registry watches it",
                     "add it to routes/data_freshness_radar.py::_DOMAINS or "
                     "routes/infra_growth.py::_LAYERS, or log a reason in "
                     "contracts/dataset_inventory_exceptions.json")

        # (b) THE MARCH-2026 REGRESSION: the loader went away, the claim stayed.
        if c["tier"] == 1 and b is not None and b["tier"] == 1 \
                and b.get("live_write_paths") and not c.get("live_write_paths"):
            fail("LOST_LIVE_WRITE_PATH", t,
                 f"lost its live write path ({', '.join(b['live_write_paths'][:3])}) "
                 f"while the product still publishes it as current",
                 "restore the loader / its workflow, drop the claim, or log a "
                 "reason in contracts/dataset_inventory_exceptions.json")

        # (c) A NEW write-only table: unserved dataset, or dead loader.
        if c["tier"] == "write_only" and (b is None or b["tier"] != "write_only"):
            fail("NEW_WRITE_ONLY", t,
                 f"{c['write_sites']} write site(s), ZERO reads anywhere in the "
                 f"repo (first: {c['first_write']})",
                 "either serve it (a read path) or delete the loader — a table "
                 "nobody reads is an unserved dataset or a dead loader, and it "
                 "needs an answer either way")

    verdict = "FAIL" if failures else ("UNMEASURED" if unmeasured else "PASS")
    return _emit(verdict, failures, unmeasured, verbose)


def _emit(verdict, failures, unmeasured, verbose) -> int:
    if verbose:
        for f in failures:
            print(f"✗ {f['code']}  {f['table']}\n    {f['why']}\n    → {f['fix']}")
        for u in unmeasured:
            print(f"? UNMEASURED  {u}")
        print(f"{verdict} — {len(failures)} failure(s), "
              f"{len(unmeasured)} unmeasured")
    return {"PASS": 0, "FAIL": 1, "UNMEASURED": 2}[verdict]


def _main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "extract":
        json.dump(extract(), sys.stdout, indent=1)
        print()
        return 0
    if cmd == "stats":
        print(json.dumps(extract()["stats"], indent=2))
        return 0
    if cmd == "baseline":
        s = extract()
        os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
        with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=1)
            fh.write("\n")
        st = s["stats"]
        print(f"wrote {BASELINE_PATH}")
        print(f"  {st['tables_total']} tables — {st['tier1_claimed']} TIER-1 "
              f"(published claim), {st['tier2_served_no_claim']} served-no-claim, "
              f"{st['tier3_internal_not_covered']} internal/NOT COVERED, "
              f"{st['write_only_not_covered']} write-only/NOT COVERED")
        print(f"  tier-1 with NO live write path: {st['tier1_no_live_writer']}; "
              f"unwatched by any registry: "
              f"{st['tier1_unwatched_by_any_registry']}")
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(_main())
