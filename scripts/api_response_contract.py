#!/usr/bin/env python3
"""
API RESPONSE CONTRACT GUARD  (r-contract, 2026-08-09)
═════════════════════════════════════════════════════════════════════════════

WHY THIS EXISTS
---------------
2026-08-08: a backend change to /api/v1/ai/reach altered its response shape.
A frontend reader of the OLD shape then rendered "0 AI PLATFORMS CONNECTED"
on the live /ai page — a confident zero manufactured from an `undefined`
lookup. Nothing warned. The frontend-side guard (scripts/qa-api-contract.mjs
in dchub-frontend) catches this class, but only REACTIVELY: after deploy, in
production, by the owner.

This is the PROACTIVE half. It runs on the BACKEND PR, against the PR's OWN
code, and fails when a public JSON response key DISAPPEARS or is RENAMED
without a compatibility alias.

DESIGN — the four things that make it not-rot
---------------------------------------------
a. The key surface is DERIVED, never hand-listed. It is computed by AST
   analysis of the Flask handlers in this repo. There is no curated list of
   endpoints or keys to maintain, so there is no second source of truth to
   drift. (There IS a small exceptions file for DELIBERATE removals — that is
   an audit log of exceptions, not a description of the surface.)

b. The baseline is a committed snapshot of that derived surface, regenerable
   by exactly one command:

       python3 scripts/api_response_contract.py baseline

c. On PR, `check` diffs the freshly-derived surface against the baseline.
      key REMOVED    -> FAIL
      key RENAMED    -> FAIL (reported as a rename, with the suspected new name)
      key ADDED      -> PASS. Additive change is NEVER blocked.
      endpoint gone  -> FAIL (every key it served disappeared)

d. THREE-VALUED: PASS / FAIL / UNMEASURED. If the surface cannot be computed
   — file won't parse, extractor crashed, an endpoint's response dict became
   dynamic so its keys are no longer knowable — the result is UNMEASURED, and
   UNMEASURED is a CI FAILURE, never a pass. A flattering zero is a bug:
   an empty or implausibly-shrunken surface is UNMEASURED, not "no
   regressions found".

WHAT "RESOLVED" MEANS (and the honest limits)
---------------------------------------------
A handler's response is resolved when its returned dict can be reconstructed
statically: dict literals, plus a local variable built up by `out = {...}` /
`out["k"] = ...` / `out.update({...})` / `out.setdefault("k", ...)`.

  * A `**splat` inside the dict marks that level `open`: the literal keys we
    can see are still contract, but extra keys may exist that we cannot see.
    Open levels never produce a REMOVED finding for keys we never saw.
  * A handler returning `jsonify(f(x))` or a dict we cannot reconstruct is
    OPAQUE. Opaque endpoints are listed BY NAME in the baseline. They are
    explicitly NOT COVERED — this guard does not imply it protects them.
  * If an endpoint goes resolved -> opaque, that is UNMEASURED for that
    endpoint, NOT a mass "all its keys were removed". Refactoring a response
    into a helper function must not be able to launder a key deletion into a
    silent pass, and must not fake a 40-key removal either.

Only non-error returns are recorded: `return jsonify({...}), 401` and friends
are skipped, so error-branch churn does not create phantom contract breaks.

USAGE
-----
    python3 scripts/api_response_contract.py extract      # print surface JSON
    python3 scripts/api_response_contract.py baseline     # (re)write baseline
    python3 scripts/api_response_contract.py check        # diff vs baseline

Exit codes for `check`:   0 = PASS   1 = FAIL   2 = UNMEASURED
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import subprocess
import sys
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(REPO, "contracts", "api_response_surface.json")
EXCEPTIONS_PATH = os.path.join(REPO, "contracts", "api_response_exceptions.json")

SCHEMA_VERSION = 1

# ── SCOPE ────────────────────────────────────────────────────────────────────
# Public JSON surface = routes whose path starts with one of these. Admin and
# internal-key-gated surfaces are deliberately out of scope: they have no
# public readers and churn constantly.
COVERED_PREFIXES = ("/api/",)
EXCLUDED_PATH_RE = re.compile(
    r"^/api/(admin|internal|debug|_)"
    r"|^/api/v1/(admin|internal|debug)"
    r"|^/api/ops/"
)

# Directories that are not the running backend: vendored frontend mirror,
# docs, tests, patch archives, virtualenvs.
EXCLUDED_FILE_RE = re.compile(
    r"^(dchub-frontend/|docs/|tests/|test/|PATCHES/|tmp/|outputs/|results/"
    r"|replit/|github-repo/|mcp-directory/|registries/|cf-workers/|cloudflare/"
    r"|enhancements/|infrastructure_output/|kmz_output/)"
    r"|(^|/)\.venv/|(^|/)node_modules/|(^|/)migrations/"
)

# Max nesting depth recorded. The audit's real bugs all live at depth 1-2
# (report.total_facilities, stats.facilities_distinct). 3 gives headroom
# without exploding the baseline.
MAX_DEPTH = 3

# A surface this much smaller than baseline means the EXTRACTOR broke, not
# that someone deleted 30% of the API. Report UNMEASURED, not a FAIL flood.
SANITY_FLOOR = 0.70


# ═════════════════════════════════════════════════════════════════════════════
# EXTRACTION
# ═════════════════════════════════════════════════════════════════════════════

class Unresolved(Exception):
    """The expression cannot be reconstructed statically."""


def _git_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", REPO, "ls-files", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f and not EXCLUDED_FILE_RE.search(f)]


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_decorators(fn: ast.AST) -> list[tuple[str, list[str]]]:
    """Return [(path, methods)] for @<bp>.route(...) decorators on fn."""
    found: list[tuple[str, list[str]]] = []
    for dec in getattr(fn, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if not (isinstance(func, ast.Attribute) and func.attr == "route"):
            continue
        if not dec.args:
            continue
        path = _str_const(dec.args[0])
        if not path:
            continue
        methods = ["GET"]
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                m = [_str_const(e) for e in kw.value.elts]
                m = [x.upper() for x in m if x]
                if m:
                    methods = m
        found.append((path, methods))
    return found


def _in_scope(path: str) -> bool:
    if not path.startswith(COVERED_PREFIXES):
        return False
    if EXCLUDED_PATH_RE.search(path):
        return False
    return True


class _DictBuilder:
    """
    Reconstructs a response dict statically.

    `keys` maps dotted path -> True. `open_at` is the set of dotted prefixes
    ("" = top level) where a **splat or dynamic key means unseen keys may
    exist, so absence at that level is not evidence of removal.
    """

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.open_at: set[str] = set()
        # (id(node), prefix) already expanded. `d = {**d, "x": 1}` and mutually
        # recursive locals otherwise loop forever; a RecursionError here would
        # surface as UNMEASURED, which is safe but useless.
        self._seen: set[tuple[int, str]] = set()

    def add_dict(self, node: ast.Dict, prefix: str, depth: int, scope: "_FnScope") -> None:
        if depth > MAX_DEPTH:
            self.open_at.add(prefix)
            return
        mark = (id(node), prefix)
        if mark in self._seen:
            self.open_at.add(prefix)
            return
        self._seen.add(mark)
        for k, v in zip(node.keys, node.values):
            if k is None:  # **splat
                self.open_at.add(prefix)
                # A splat of a literal dict is still knowable.
                inner = scope.resolve(v)
                if isinstance(inner, ast.Dict):
                    self.add_dict(inner, prefix, depth, scope)
                continue
            name = _str_const(k)
            if name is None:
                self.open_at.add(prefix)
                continue
            dotted = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
            self.keys.add(dotted)
            self.add_value(v, dotted, depth, scope)

    def add_value(self, v: ast.AST, dotted: str, depth: int, scope: "_FnScope") -> None:
        """Expand a dict VALUE one level down, at path `dotted`."""
        if depth + 1 > MAX_DEPTH:
            return
        resolved = scope.resolve(v)
        if isinstance(resolved, ast.Dict):
            self.add_dict(resolved, dotted, depth + 1, scope)
            # `stats = {}` … `stats["facilities_distinct"] = …` … then nested as
            # a VALUE (`jsonify({"stats": stats})`). Without this the wrapper-key
            # surface — exactly the audit's database.html:623 class — is invisible.
            if isinstance(v, ast.Name):
                self.apply_mutations(v.id, dotted, depth + 1, scope)
        elif isinstance(resolved, (ast.List, ast.Tuple)):
            for elt in resolved.elts:
                e = scope.resolve(elt)
                if isinstance(e, ast.Dict):
                    self.add_dict(e, f"{dotted}[]", depth + 1, scope)
        elif isinstance(resolved, ast.ListComp):
            e = scope.resolve(resolved.elt)
            if isinstance(e, ast.Dict):
                self.add_dict(e, f"{dotted}[]", depth + 1, scope)
        elif isinstance(resolved, (ast.Call, ast.Await, ast.Attribute,
                                   ast.Subscript, ast.Name, ast.IfExp)):
            # Value comes from somewhere we cannot follow (helper call, ORM row,
            # a local we could not pin to one dict literal). Mark the level OPEN
            # so we never claim to know its children — an unknown child must not
            # later read as a removal.
            self.open_at.add(dotted)

    def apply_mutations(self, var: str, prefix: str, depth: int, scope: "_FnScope") -> None:
        """Fold `var["k"] = v` / `var.update({...})` / `var.setdefault(...)`."""
        if depth > MAX_DEPTH:
            return
        if var in scope.tainted:
            # `stats[_akey] = …` / `.update(row)` — EXTRA, unknowable keys may
            # exist. That makes the level OPEN; it does NOT make the keys we CAN
            # see fake. Dropping them here is what hid stats.facilities_distinct.
            self.open_at.add(prefix)
        for k, v in scope.mutations.get(var, []):
            if k == "**":
                self.add_dict(v, prefix, depth, scope)
                continue
            dotted = f"{prefix}.{k}" if prefix else k
            self.keys.add(dotted)
            self.add_value(v, dotted, depth, scope)


class _FnScope:
    """
    Tiny intra-function constant propagator. Handles the dominant pattern in
    this codebase:  out = {...} ; out["k"] = v ; out.update({...}).
    """

    def __init__(self, fn: ast.AST) -> None:
        self.assigns: dict[str, list[ast.AST]] = {}
        self.mutations: dict[str, list[tuple[str, ast.AST]]] = {}
        self.tainted: set[str] = set()
        self._walk(fn)

    def _walk(self, fn: ast.AST) -> None:
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                # `stats: dict = {}` is an AnnAssign, not an Assign. Missing it
                # made /api/v1/stats/canonical resolve to {ok, error} only.
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is None:
                    continue
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        self.assigns.setdefault(tgt.id, []).append(node.value)
                    elif (isinstance(tgt, ast.Subscript)
                          and isinstance(tgt.value, ast.Name)):
                        k = _str_const(tgt.slice)
                        if k is None:
                            self.tainted.add(tgt.value.id)
                        else:
                            self.mutations.setdefault(tgt.value.id, []).append((k, node.value))
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    tgt = f.value.id
                    if f.attr == "update":
                        if node.args:
                            inner = node.args[0]
                            if isinstance(inner, ast.Dict):
                                self.mutations.setdefault(tgt, []).append(("**", inner))
                            else:
                                self.tainted.add(tgt)
                        for kw in node.keywords:
                            if kw.arg:
                                self.mutations.setdefault(tgt, []).append((kw.arg, kw.value))
                            else:
                                self.tainted.add(tgt)
                    elif f.attr == "setdefault" and len(node.args) >= 1:
                        k = _str_const(node.args[0])
                        if k is None:
                            self.tainted.add(tgt)
                        else:
                            val = node.args[1] if len(node.args) > 1 else ast.Constant(None)
                            self.mutations.setdefault(tgt, []).append((k, val))
                    elif f.attr in ("pop", "popitem", "clear"):
                        self.tainted.add(tgt)

    def resolve(self, node: ast.AST) -> ast.AST:
        """Resolve a Name to its dict literal if unambiguous; else return node."""
        seen = 0
        while isinstance(node, ast.Name) and seen < 4:
            seen += 1
            vals = self.assigns.get(node.id)
            if not vals:
                return node
            dicts = [v for v in vals if isinstance(v, ast.Dict)]
            if len(dicts) != len(vals) or not dicts:
                # reassigned from something non-literal somewhere -> give up
                return node if len(dicts) != 1 else dicts[0]
            node = dicts[0] if len(dicts) == 1 else dicts[0]
        return node

    def name_is_tainted(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.tainted


def _extract_returns(fn: ast.AST, scope: "_FnScope") -> list[ast.AST]:
    """Non-error `return jsonify(X)` / `return X` payload expressions."""
    payloads: list[ast.AST] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        val = node.value
        status: int | None = None
        if isinstance(val, ast.Tuple) and len(val.elts) >= 2:
            s = val.elts[1]
            if isinstance(s, ast.Constant) and isinstance(s.value, int):
                status = s.value
            val = val.elts[0]
        if status is not None and not (200 <= status < 300):
            continue  # error branch — not part of the success contract
        # `resp = jsonify(payload); resp.headers[...] = ...; return resp, 200`
        # is the standard cache-header idiom here. Follow the local one hop.
        if isinstance(val, ast.Name):
            bound = scope.assigns.get(val.id) or []
            calls = [b for b in bound
                     if isinstance(b, ast.Call)
                     and getattr(b.func, "id", getattr(b.func, "attr", "")) == "jsonify"]
            if len(calls) == 1 and calls[0].args:
                payloads.append(calls[0].args[0])
                continue
        if isinstance(val, ast.Call):
            f = val.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == "jsonify" and val.args:
                payloads.append(val.args[0])
                continue
            if fname == "jsonify" and val.keywords:
                d = ast.Dict(
                    keys=[ast.Constant(kw.arg) if kw.arg else None for kw in val.keywords],
                    values=[kw.value for kw in val.keywords],
                )
                payloads.append(d)
                continue
            continue
        if isinstance(val, ast.Dict):
            payloads.append(val)
    return payloads


def extract_surface() -> dict[str, Any]:
    endpoints: dict[str, dict[str, Any]] = {}
    parse_errors: list[str] = []
    files = _git_files()

    for rel in files:
        full = os.path.join(REPO, rel)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            tree = ast.parse(src)
        except SyntaxError as e:
            parse_errors.append(f"{rel}: {e}")
            continue
        except OSError as e:
            # str(e) appends e.filename — the ABSOLUTE path we just opened.
            # This list is written into a COMMITTED artifact, so that path
            # becomes repo truth: it shipped once as a /private/tmp/.../wt-*
            # path from whichever worktree happened to regenerate the
            # baseline while the file was mid-delete. `rel` already names the
            # file, repo-relative; keep the reason, drop the machine.
            parse_errors.append(f"{rel}: [Errno {e.errno}] {e.strerror}")
            continue

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            routes = _route_decorators(fn)
            if not routes:
                continue
            in_scope = [(p, m) for p, m in routes if _in_scope(p)]
            if not in_scope:
                continue

            scope = _FnScope(fn)
            builder = _DictBuilder()
            payloads = _extract_returns(fn, scope)
            resolved_any = False
            opaque_any = False
            for p in payloads:
                if scope.name_is_tainted(p):
                    opaque_any = True
                    continue
                r = scope.resolve(p)
                if isinstance(r, ast.Dict):
                    builder.add_dict(r, "", 0, scope)
                    if isinstance(p, ast.Name):
                        builder.apply_mutations(p.id, "", 0, scope)
                    resolved_any = True
                else:
                    opaque_any = True

            if not payloads:
                resolution = "opaque"
            elif resolved_any and not opaque_any:
                resolution = "resolved"
            elif resolved_any and opaque_any:
                resolution = "partial"
            else:
                resolution = "opaque"

            for path, methods in in_scope:
                for method in methods:
                    if method in ("OPTIONS", "HEAD"):
                        continue
                    eid = f"{method} {path}"
                    rec = {
                        "source": f"{rel}:{fn.lineno}",
                        "handler": fn.name,
                        "resolution": resolution,
                        "open_at": sorted(builder.open_at),
                        "keys": sorted(builder.keys),
                    }
                    prev = endpoints.get(eid)
                    if prev is None:
                        endpoints[eid] = rec
                    else:
                        # Duplicate registration (a real thing in this repo).
                        # Union the keys; the weakest resolution wins so we
                        # never claim more certainty than we have.
                        order = {"resolved": 2, "partial": 1, "opaque": 0}
                        merged_keys = sorted(set(prev["keys"]) | set(rec["keys"]))
                        merged_open = sorted(set(prev["open_at"]) | set(rec["open_at"]))
                        keep = prev if order[prev["resolution"]] <= order[rec["resolution"]] else rec
                        keep = dict(keep)
                        keep["resolution"] = min(
                            (prev["resolution"], rec["resolution"]), key=lambda r: order[r]
                        )
                        keep["keys"] = merged_keys
                        keep["open_at"] = merged_open
                        keep["source"] = prev["source"] + "," + rec["source"]
                        endpoints[eid] = keep

    resolved = [e for e in endpoints.values() if e["resolution"] == "resolved"]
    partial = [e for e in endpoints.values() if e["resolution"] == "partial"]
    opaque = [e for e in endpoints.values() if e["resolution"] == "opaque"]
    total_keys = sum(len(e["keys"]) for e in endpoints.values())

    # Honest coverage split: a key whose PARENT level is `open` (**splat or a
    # dynamic key name) could still be served invisibly, so its disappearance
    # is UNMEASURED, not FAIL. Only strictly-protected keys yield a hard FAIL.
    strict_keys = open_keys = 0
    for e in endpoints.values():
        if e["resolution"] == "opaque":
            continue
        oa = set(e["open_at"])
        for k in e["keys"]:
            parent = k.rsplit(".", 1)[0] if "." in k else ""
            if parent in oa:
                open_keys += 1
            else:
                strict_keys += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "_readme": (
            "DERIVED FILE — do not hand-edit. Regenerate with: "
            "python3 scripts/api_response_contract.py baseline"
        ),
        "scope": {
            "covered_prefixes": list(COVERED_PREFIXES),
            "excluded_path_regex": EXCLUDED_PATH_RE.pattern,
            "excluded_file_regex": EXCLUDED_FILE_RE.pattern,
            "max_depth": MAX_DEPTH,
            "note": (
                "Only endpoints with resolution=resolved|partial are PROTECTED. "
                "resolution=opaque endpoints are IN SCOPE BUT NOT COVERED — their "
                "response dict cannot be reconstructed statically. They are listed "
                "so the uncovered set is explicit."
            ),
        },
        "stats": {
            "python_files_scanned": len(files),
            "endpoints_total": len(endpoints),
            "endpoints_resolved": len(resolved),
            "endpoints_partial": len(partial),
            "endpoints_opaque_not_covered": len(opaque),
            "keys_total": total_keys,
            "keys_strictly_protected": strict_keys,
            "keys_open_level_unmeasured_on_removal": open_keys,
            "parse_errors": len(parse_errors),
        },
        "parse_errors": parse_errors,
        "endpoints": dict(sorted(endpoints.items())),
    }


# ═════════════════════════════════════════════════════════════════════════════
# READER ATTRIBUTION  ("who reads it")
# ═════════════════════════════════════════════════════════════════════════════

_READER_EXT = (".html", ".js", ".mjs", ".jsx", ".ts", ".tsx")


def _reader_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", REPO, "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.splitlines() if f.endswith(_READER_EXT)]


_reader_cache: dict[str, list[str]] | None = None


def find_readers(path: str, key: str) -> tuple[list[str], str]:
    """
    Candidate readers of `key` from `path`, found by scanning in-repo HTML/JS
    that mentions the endpoint path. Returns (hits, provenance_note).

    HONESTY: an empty result is NOT proof nobody reads it. The production
    frontend lives in the separate dchub-frontend repo; this repo carries only
    a partial mirror. Empty is reported as "none found in THIS repo".
    """
    global _reader_cache
    if _reader_cache is None:
        _reader_cache = {}
        for rel in _reader_files():
            try:
                with open(os.path.join(REPO, rel), "r", encoding="utf-8",
                          errors="replace") as fh:
                    _reader_cache[rel] = fh.read().splitlines()
            except OSError:
                continue

    leaf = key.split(".")[-1].replace("[]", "")
    hits: list[str] = []
    for rel, lines in _reader_cache.items():
        if not any(path in ln for ln in lines):
            continue
        for i, ln in enumerate(lines, 1):
            if re.search(rf"[.\[]\s*[\"']?{re.escape(leaf)}\b", ln):
                hits.append(f"{rel}:{i}")
                if len(hits) >= 6:
                    return hits, "in-repo scan"
    return hits, "in-repo scan"


# ═════════════════════════════════════════════════════════════════════════════
# DIFF / CHECK
# ═════════════════════════════════════════════════════════════════════════════

def _load_json(p: str) -> Any:
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_exceptions() -> dict[str, set[str]]:
    """endpoint -> {allowed removed keys}. Deliberate, audited removals."""
    if not os.path.exists(EXCEPTIONS_PATH):
        return {}
    try:
        raw = _load_json(EXCEPTIONS_PATH)
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    for entry in raw.get("allowed_removals", []):
        ep = entry.get("endpoint")
        keys = entry.get("keys") or ([entry["key"]] if entry.get("key") else [])
        if ep and keys:
            out.setdefault(ep, set()).update(keys)
    return out


def _rename_candidate(removed: str, added: set[str]) -> str | None:
    """Suspected rename: an added key close to the removed one."""
    base = removed.rsplit(".", 1)
    prefix = base[0] if len(base) == 2 else ""
    leaf = base[-1]
    pool = [a for a in added if (a.rsplit(".", 1)[0] if "." in a else "") == prefix]
    names = [a.rsplit(".", 1)[-1] for a in pool]
    match = difflib.get_close_matches(leaf, names, n=1, cutoff=0.6)
    if match:
        return pool[names.index(match[0])]
    # token containment: platforms_count vs active_platforms
    lt = set(re.split(r"[_\W]+", leaf.lower())) - {""}
    for a, n in zip(pool, names):
        nt = set(re.split(r"[_\W]+", n.lower())) - {""}
        if lt & nt:
            return a
    return None


def check(baseline_path: str = BASELINE_PATH, verbose: bool = True,
          surface: dict[str, Any] | None = None) -> int:
    """
    Diff the current surface against the baseline.

    `surface` is an injection point used ONLY by the self-test, so that the
    self-test exercises THIS function — the real run path — rather than a
    reimplementation of it that could drift into always-passing.
    """
    unmeasured: list[str] = []
    failures: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []

    # ── 1. baseline must exist and parse ────────────────────────────────────
    if not os.path.exists(baseline_path):
        _emit("UNMEASURED", [], [
            f"baseline missing: {baseline_path} — run "
            f"`python3 scripts/api_response_contract.py baseline`"], verbose)
        return 2
    try:
        base = _load_json(baseline_path)
    except Exception as e:
        _emit("UNMEASURED", [], [f"baseline unreadable: {e}"], verbose)
        return 2
    if base.get("schema_version") != SCHEMA_VERSION:
        _emit("UNMEASURED", [], [
            f"baseline schema_version={base.get('schema_version')} != {SCHEMA_VERSION}; "
            f"regenerate the baseline"], verbose)
        return 2

    # ── 2. compute the current surface; a crash here is UNMEASURED ─────────
    if surface is not None:
        cur = surface
    else:
        try:
            cur = extract_surface()
        except Exception as e:  # noqa: BLE001 - ANY extractor failure is unmeasured
            _emit("UNMEASURED", [],
                  [f"extractor raised {type(e).__name__}: {e}"], verbose)
            return 2

    b_eps: dict[str, Any] = base.get("endpoints", {})
    c_eps: dict[str, Any] = cur.get("endpoints", {})

    # ── 3. VACUOUS-PASS GUARDS — an empty/shrunken surface is never a PASS ──
    if not c_eps:
        _emit("UNMEASURED", [], [
            "computed surface is EMPTY (0 endpoints). The extractor found nothing — "
            "this is never a pass."], verbose)
        return 2
    b_prot = sum(1 for e in b_eps.values() if e["resolution"] in ("resolved", "partial"))
    c_prot = sum(1 for e in c_eps.values() if e["resolution"] in ("resolved", "partial"))
    if b_prot and c_prot < b_prot * SANITY_FLOOR:
        _emit("UNMEASURED", [], [
            f"protected-endpoint count collapsed {b_prot} -> {c_prot} "
            f"(< {int(SANITY_FLOOR*100)}% floor). The EXTRACTOR is more likely broken "
            f"than {b_prot - c_prot} endpoints genuinely deleted. Refusing to guess."],
            verbose)
        return 2
    new_parse_errors = set(cur.get("parse_errors", [])) - set(base.get("parse_errors", []))
    if new_parse_errors:
        unmeasured.extend(
            f"file no longer parses (its endpoints are unmeasurable): {p}"
            for p in sorted(new_parse_errors)
        )

    exceptions = _load_exceptions()

    # ── 4. per-endpoint diff ───────────────────────────────────────────────
    for eid, brec in sorted(b_eps.items()):
        if brec["resolution"] == "opaque":
            continue  # was never covered
        crec = c_eps.get(eid)
        allowed = exceptions.get(eid, set())

        if crec is None:
            removed = [k for k in brec["keys"] if k not in allowed]
            if removed:
                failures.append({
                    "kind": "endpoint_removed",
                    "endpoint": eid,
                    "source": brec["source"],
                    "keys": removed,
                })
            continue

        if crec["resolution"] == "opaque":
            degraded.append({
                "endpoint": eid,
                "source": crec["source"],
                "was": brec["resolution"],
                "keys_at_baseline": len(brec["keys"]),
            })
            continue

        b_keys, c_keys = set(brec["keys"]), set(crec["keys"])
        added = c_keys - b_keys
        open_now = set(crec.get("open_at", []))

        for k in sorted(b_keys - c_keys):
            if k in allowed:
                continue
            # A level that became `open` (**splat / dynamic) may still serve
            # the key — we simply cannot see it. Unmeasured, not removed.
            parent = k.rsplit(".", 1)[0] if "." in k else ""
            if parent in open_now:
                degraded.append({
                    "endpoint": eid, "source": crec["source"],
                    "was": brec["resolution"], "key": k,
                    "reason": f"level '{parent or '<root>'}' became dynamic",
                })
                continue
            failures.append({
                "kind": "key_renamed" if _rename_candidate(k, added) else "key_removed",
                "endpoint": eid,
                "source": crec["source"],
                "key": k,
                "suspected_new_name": _rename_candidate(k, added),
            })

    # ── 5. report ──────────────────────────────────────────────────────────
    for d in degraded:
        if "key" in d:
            unmeasured.append(
                f"{d['endpoint']}  key '{d['key']}' -> {d['reason']}  ({d['source']})")
        else:
            unmeasured.append(
                f"{d['endpoint']}  resolution {d['was']} -> opaque; "
                f"{d['keys_at_baseline']} keys no longer measurable  ({d['source']})")

    if failures:
        verdict = "FAIL"
        code = 1
    elif unmeasured:
        verdict = "UNMEASURED"
        code = 2
    else:
        verdict = "PASS"
        code = 0

    _emit(verdict, failures, unmeasured, verbose, cur=cur, base=base)
    return code


def _emit(verdict: str, failures: list[dict[str, Any]], unmeasured: list[str],
          verbose: bool, cur: dict | None = None, base: dict | None = None) -> None:
    if not verbose:
        return
    print("═" * 78)
    print("API RESPONSE CONTRACT GUARD")
    print("═" * 78)
    if cur and base:
        cs, bs = cur["stats"], base["stats"]
        print(f"  baseline : {bs['endpoints_resolved']} resolved + "
              f"{bs['endpoints_partial']} partial endpoints, {bs['keys_total']} keys")
        print(f"  this PR  : {cs['endpoints_resolved']} resolved + "
              f"{cs['endpoints_partial']} partial endpoints, {cs['keys_total']} keys")
        print(f"  NOT COVERED (opaque, response dict not statically knowable): "
              f"{cs['endpoints_opaque_not_covered']} endpoints")
    print()

    if failures:
        print(f"❌ FAIL — {len(failures)} contract break(s). "
              f"A key vanished from a public response.\n")
        for f in failures:
            if f["kind"] == "endpoint_removed":
                print(f"  ENDPOINT REMOVED  {f['endpoint']}")
                print(f"    was: {f['source']}")
                print(f"    keys lost ({len(f['keys'])}): {', '.join(f['keys'][:12])}"
                      + (" …" if len(f["keys"]) > 12 else ""))
                sample = f["keys"][:3]
            else:
                label = "KEY RENAMED" if f["kind"] == "key_renamed" else "KEY REMOVED"
                print(f"  {label}  {f['endpoint']}")
                print(f"    key : {f['key']}")
                if f.get("suspected_new_name"):
                    print(f"    now : {f['suspected_new_name']}  "
                          f"<- rename with NO compatibility alias")
                print(f"    at  : {f['source']}")
                sample = [f["key"]]
            path = f["endpoint"].split(" ", 1)[1]
            for k in sample:
                readers, prov = find_readers(path, k)
                if readers:
                    print(f"    read by ({prov}): {', '.join(readers)}")
                else:
                    print(f"    read by: none found in THIS repo — the production "
                          f"frontend lives in dchub-frontend; check there before "
                          f"assuming unused")
            print()
        print("  TO FIX (pick one):")
        print("    1. Keep serving the old key alongside the new one (compat alias).")
        print("    2. If the removal is deliberate, add an entry to")
        print("       contracts/api_response_exceptions.json with a reason, then")
        print("       regenerate the baseline.")
        print()

    if unmeasured:
        print(f"⚠️  UNMEASURED — {len(unmeasured)} item(s) could not be measured. "
              f"This is a CI FAILURE, not a pass.\n")
        for u in unmeasured:
            print(f"  {u}")
        print()
        print("  A response that became dynamic is not 'fine' — it is invisible to")
        print("  this guard. Either keep the dict literal in the handler, or accept")
        print("  that the endpoint drops out of coverage by regenerating the baseline.")
        print()

    if verdict == "PASS":
        print("✅ PASS — no public response key was removed or renamed.")
        print("   (Added keys are always allowed; additive change is never blocked.)")
    print("═" * 78)
    print(f"VERDICT: {verdict}")


# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("cmd", choices=["extract", "baseline", "check"])
    ap.add_argument("--baseline", default=BASELINE_PATH)
    args = ap.parse_args()

    if args.cmd == "extract":
        json.dump(extract_surface(), sys.stdout, indent=2, sort_keys=False)
        print()
        return 0

    if args.cmd == "baseline":
        surface = extract_surface()
        os.makedirs(os.path.dirname(args.baseline), exist_ok=True)
        with open(args.baseline, "w", encoding="utf-8") as fh:
            json.dump(surface, fh, indent=1, sort_keys=False)
            fh.write("\n")
        s = surface["stats"]
        print(f"wrote {args.baseline}")
        print(f"  {s['endpoints_total']} in-scope endpoints "
              f"({s['endpoints_resolved']} resolved, {s['endpoints_partial']} partial, "
              f"{s['endpoints_opaque_not_covered']} opaque/NOT COVERED)")
        print(f"  {s['keys_total']} protected keys from "
              f"{s['python_files_scanned']} python files")
        return 0

    return check(args.baseline)


if __name__ == "__main__":
    sys.exit(main())
