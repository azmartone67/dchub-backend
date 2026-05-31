"""
brain_source_map.py — Phase RR-4 (2026-05-31).

THE SOURCE-MAPPER for the DC Hub autonomous brain's Layer-5
"learn-backend-issues" pass.

PROBLEM THIS CLOSES
-------------------
brain_v2_layer5.learn_backend_issues() iterates ~87 actionable backend
findings every cron tick. Each finding is an ABSTRACT pointer:

  - a URL path        e.g. {"url": "/api/v1/facilities/delta"}
  - a synthetic cron  e.g. {"url": "dchub://cron/dcpi_recompute"}
  - a Flask route     e.g. {"url": "/markets/<slug>"}
  - a filename hint   e.g. {"issue": "...content_publisher.py drifted..."}
  - a cron schedule   e.g. {"issue": "cron '0 6 * * *' underproducing"}
  - a table name      e.g. {"url": "table:gas_pipelines"}

Layer 5 hard-codes a tiny BACKEND_ISSUE_SOURCE_FILES dict (2 entries).
Anything not in that dict short-circuits to:

    results.append({"url": url, "outcome": "no_source_map"})   # layer5:488

— i.e. it CANNOT resolve the abstract finding to a concrete
(file, line) and therefore cannot build the {file, search, replace}
proposal Claude needs. With only 2 mapped urls, ~85 of ~87 findings
fail `no_source_map` every cycle and the brain never learns from them.

WHAT THIS MODULE DOES
---------------------
`resolve_finding_to_sources(finding, repo_root=...)` turns an abstract
finding into a RANKED list of concrete candidate locations:

    [{"file": "routes/foo.py", "line": 123,
      "snippet": "@foo_bp.route('/api/v1/facilities/delta')",
      "confidence": 0.0..1.0,
      "match_kind": "route|filename|table|symbol|text"}, ...]

Resolution walks the repo's .py files ONCE and builds three in-process
indexes (cached behind a lock, keyed by repo_root):

  * route index   — every @app.route / @<bp>.route / @<bp>.get|post|...
                    / .add_url_rule(...) → URL pattern → (file, line).
                    `<param>` / `<int:id>` converters are normalized to a
                    regex so a concrete finding url like
                    /api/v1/dcpi/scores/ashburn matches the registered
                    pattern /api/v1/dcpi/scores/<slug>.
  * file index    — basename → [paths] for filename-hint findings.
  * table index   — table name → [(file, line, kind)] harvested from
                    CREATE TABLE / INSERT INTO / UPDATE / FROM / JOIN.

Symbol/text is a LIVE fallback grep (not pre-indexed) of the finding's
most distinctive token, so even a finding we have no structured handle
on still gets a best-effort location.

Ranking (most specific wins): exact route match > exact filename >
table DDL (CREATE TABLE) > table DML (INSERT/UPDATE) > table read
(FROM/JOIN) > symbol/text. Capped at the top 5 candidates.

HARD CONTRACT
-------------
  * NEVER raises. Any failure (bad regex, unreadable file, no DB, etc.)
    degrades to fewer/zero candidates. A miss returns [].
  * Pure read-only. It opens NO database and writes NOTHING. It only
    reads .py source files under repo_root.
  * Cheap to call repeatedly: the index build is memoized per repo_root.

ENDPOINT
--------
  GET /api/v1/brain/source-map?finding=<url-or-text>   admin-gated

Admin gate mirrors brain_v2_layer5._admin_guard() EXACTLY (X-Admin-Key
header OR admin_key query arg, checked against ADMIN_KEY imported from
brain_v2_layer4) so it guards identically to every other admin brain
endpoint. The response can echo source snippets, so it must be gated.

WIRING (applied by a human — see the task report):
  * brain_v2_layer5.learn_backend_issues should call
    resolve_finding_to_sources(issue) BEFORE emitting no_source_map and,
    on a hit, feed the candidate files into the existing
    excerpts/_build_code_prompt path.
  * main.py registers brain_source_map_bp next to brain_v2_layer5_bp.
"""
from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

# Mirror brain_v2_layer5's admin gate by reusing the SAME ADMIN_KEY it
# imports from layer4. Guarded so an import hiccup can never break module
# load (the blueprint must still register).
try:
    from routes.brain_v2_layer4 import ADMIN_KEY  # type: ignore
except Exception:  # pragma: no cover - defensive
    ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")

brain_source_map_bp = Blueprint("brain_source_map", __name__)


# ──────────────────────────────────────────────────────────────────
# Admin guard — byte-for-byte the same check as
# brain_v2_layer5._admin_guard (X-Admin-Key header or admin_key arg vs
# ADMIN_KEY). Returns an error Response tuple, or None when authorized.
# ──────────────────────────────────────────────────────────────────
def _admin_guard():
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key header required"), 401
    return None


# ──────────────────────────────────────────────────────────────────
# repo_root: two dirs up from this file (routes/ → repo). This matches
# brain_v2_layer5._read_window's resolution so candidate `file` paths
# come back repo-root-relative and drop straight into a Layer-5 proposal.
# ──────────────────────────────────────────────────────────────────
def _default_repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Directories never worth walking — VCS, caches, vendored deps, DB
# backups, virtualenvs. Walking the whole tree otherwise is ~800 files.
_SKIP_DIRS = {
    ".git", "__pycache__", ".claude", "node_modules", ".pg_migration_backups",
    "venv", ".venv", "env", ".pytest_cache", ".mypy_cache", "site-packages",
    "dist", "build", ".wrangler", ".next",
}

# Cap per-file read. Must be high enough to include the route-richest
# files: main.py (~1.35MB, ~385 @app.route entries) and
# brain_consistency_radar.py (~390KB). Capping below main.py silently
# drops the single most important file for route resolution — the bug
# that made /api/v1/geocode (a main.py route) unresolvable in testing.
# 3MB comfortably covers both while still excluding pathological
# machine-generated blobs.
_MAX_FILE_BYTES = 3_000_000


# ──────────────────────────────────────────────────────────────────
# Regexes for the route index.
#   @app.route("/x")            @app.route('/x', methods=[...])
#   @foo_bp.route("/x")         @foo_bp.get("/x")  .post  .put  .delete  .patch
#   app.add_url_rule("/x", ...) foo_bp.add_url_rule('/x', ...)
# The path is the first single/double-quoted string argument.
# ──────────────────────────────────────────────────────────────────
_RE_ROUTE_DECORATOR = re.compile(
    r"""@\s*[A-Za-z_][A-Za-z0-9_]*       # app / foo_bp
        \s*\.\s*
        (?:route|get|post|put|delete|patch|websocket)   # method
        \s*\(\s*
        (?P<q>['"])(?P<path>(?:\\.|(?!(?P=q)).)*)(?P=q)  # quoted path
    """,
    re.VERBOSE,
)
_RE_ADD_URL_RULE = re.compile(
    r"""\.\s*add_url_rule\s*\(\s*
        (?P<q>['"])(?P<path>(?:\\.|(?!(?P=q)).)*)(?P=q)
    """,
    re.VERBOSE,
)

# Flask path params: <slug>, <int:id>, <path:provider_name>, <string:x>.
_RE_PATH_PARAM = re.compile(r"<(?:[a-zA-Z_][a-zA-Z0-9_]*:)?[a-zA-Z_][a-zA-Z0-9_]*>")

# Table DDL/DML harvest. Captured group is the (possibly schema-qualified)
# table identifier. Reads (FROM/JOIN) and writes (CREATE/INSERT/UPDATE)
# are separate so we can rank a CREATE TABLE above a casual FROM.
_RE_TABLE_DDL = re.compile(
    r"\b(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)
_RE_TABLE_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|TRUNCATE(?:\s+TABLE)?)\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)
_RE_TABLE_READ = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)

# SQL keywords that are NOT table names but follow FROM/JOIN-ish tokens.
_SQL_NOISE = {
    "select", "where", "set", "values", "on", "as", "and", "or", "by",
    "group", "order", "limit", "offset", "having", "using", "returning",
    "into", "table", "only", "lateral", "natural", "cross", "inner",
    "left", "right", "full", "outer", "join", "exists", "not", "null",
}

# Tokens that carry no discriminating power as a text-search needle.
_TOKEN_STOP = {
    "api", "v1", "v2", "the", "and", "for", "with", "this", "that", "cron",
    "loop", "error", "errors", "failing", "stale", "dead", "issue", "issues",
    "http", "https", "dchub", "cloud", "com", "www", "underproducing",
    "backend", "warning", "alert", "found", "missing", "none", "null",
    "true", "false", "data", "row", "rows", "count", "value", "values",
}


# ──────────────────────────────────────────────────────────────────
# Index build (memoized per repo_root, lock-guarded).
# ──────────────────────────────────────────────────────────────────
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict = {}   # repo_root -> index dict


def _iter_py_files(repo_root: str):
    """Yield absolute paths of .py files under repo_root, skipping junk."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skip dirs in-place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _route_to_regex(pattern: str) -> Optional["re.Pattern"]:
    """Turn a Flask URL pattern into a compiled regex that matches a
    CONCRETE incoming path. `<slug>` / `<int:id>` / `<path:p>` become
    wildcard segments. Returns None if the pattern can't be compiled."""
    try:
        # Escape literal regex chars, then swap the escaped params back to
        # wildcards. <path:...> can span slashes; others stop at a slash.
        out = []
        idx = 0
        for m in _RE_PATH_PARAM.finditer(pattern):
            out.append(re.escape(pattern[idx:m.start()]))
            tok = m.group(0)
            if tok.startswith("<path:"):
                out.append(r".+")
            else:
                out.append(r"[^/]+")
            idx = m.end()
        out.append(re.escape(pattern[idx:]))
        body = "".join(out)
        # Tolerate an optional trailing slash on the concrete path.
        return re.compile(r"^" + body + r"/?$")
    except Exception:
        return None


def _snippet_at(lines: list, idx0: int) -> str:
    """One-line snippet (the matched line, trimmed)."""
    try:
        return lines[idx0].strip()[:300]
    except Exception:
        return ""


def _build_index(repo_root: str) -> dict:
    """Walk every .py file once and build the route/file/table indexes.
    Never raises — a bad file is skipped."""
    routes: list = []          # [{"raw","regex","file","line","snippet"}]
    files: dict = {}           # basename(lower) -> [rel_path, ...]
    tables: dict = {}          # table(lower) -> [{"file","line","kind","snippet"}]

    def _rel(p: str) -> str:
        try:
            return os.path.relpath(p, repo_root)
        except Exception:
            return p

    for path in _iter_py_files(repo_root):
        rel = _rel(path)
        base = os.path.basename(path).lower()
        files.setdefault(base, []).append(rel)
        try:
            if os.path.getsize(path) > _MAX_FILE_BYTES:
                # Still keep it in the file index, but skip line scanning.
                continue
        except Exception:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as e:  # pragma: no cover - defensive
            print(f"[brain_source_map] read {rel} failed: {e}", file=sys.stderr)
            continue

        lines = text.split("\n")
        for lineno, line in enumerate(lines, start=1):
            # --- routes ---
            if "route" in line or "add_url_rule" in line or ".get(" in line \
               or ".post(" in line or ".put(" in line or ".delete(" in line \
               or ".patch(" in line:
                for rx in (_RE_ROUTE_DECORATOR, _RE_ADD_URL_RULE):
                    mm = rx.search(line)
                    if mm:
                        raw = mm.group("path")
                        if raw.startswith("/"):
                            routes.append({
                                "raw": raw,
                                "regex": _route_to_regex(raw),
                                "file": rel,
                                "line": lineno,
                                "snippet": line.strip()[:300],
                            })
                        break

            # --- tables (only scan lines that look SQL-ish) ---
            up = line.upper()
            if ("TABLE" in up or "INSERT" in up or "UPDATE" in up
                    or "FROM" in up or "JOIN" in up or "DELETE" in up
                    or "TRUNCATE" in up):
                for rx, kind in (
                    (_RE_TABLE_DDL, "table_ddl"),
                    (_RE_TABLE_WRITE, "table_write"),
                    (_RE_TABLE_READ, "table_read"),
                ):
                    for tm in rx.finditer(line):
                        tbl = tm.group(1)
                        tbl_l = tbl.lower()
                        # Strip schema qualifier for the key (public.foo→foo)
                        key = tbl_l.split(".")[-1]
                        if not key or key in _SQL_NOISE:
                            continue
                        # Plausible identifier only (avoids FROM (subquery)).
                        if not re.match(r"^[a-z_][a-z0-9_]*$", key):
                            continue
                        tables.setdefault(key, []).append({
                            "file": rel,
                            "line": lineno,
                            "kind": kind,
                            "snippet": line.strip()[:300],
                        })

    return {
        "routes": routes,
        "files": files,
        "tables": tables,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "py_files": len(files),
        "route_count": len(routes),
        "table_count": len(tables),
    }


def _get_index(repo_root: str, *, rebuild: bool = False) -> dict:
    """Memoized index accessor. Lock-guarded so concurrent requests build
    at most once. Never raises — returns an empty index on catastrophic
    failure so callers still get [] rather than an exception."""
    key = os.path.abspath(repo_root)
    if not rebuild:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            return cached
    with _INDEX_LOCK:
        if not rebuild:
            cached = _INDEX_CACHE.get(key)
            if cached is not None:
                return cached
        try:
            idx = _build_index(key)
        except Exception as e:  # pragma: no cover - defensive
            print(f"[brain_source_map] index build failed: {e}", file=sys.stderr)
            idx = {"routes": [], "files": {}, "tables": {},
                   "built_at": None, "py_files": 0, "route_count": 0,
                   "table_count": 0, "error": str(e)[:200]}
        _INDEX_CACHE[key] = idx
        return idx


# ──────────────────────────────────────────────────────────────────
# Finding parsing → extract the structured handles we know how to
# resolve. A finding can be a raw string OR a dict (layer5 findings use
# {"url","issue"}). Everything is defensive; unknown shapes degrade to
# token extraction.
# ──────────────────────────────────────────────────────────────────
_RE_URL_PATH = re.compile(r"/[A-Za-z0-9_\-/<>:.]+")
_RE_PY_FILE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*\.py)\b")
_RE_CRON_URL = re.compile(r"dchub://cron/([A-Za-z0-9_\-]+)")
_RE_TABLE_HINT = re.compile(r"\btable[:=]\s*([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE)
_RE_CRON_SCHEDULE = re.compile(r"(?:^|[\s'\"])((?:[\d*,/\-]+\s+){4}[\d*,/\-]+)(?:[\s'\"]|$)")
_RE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _finding_text(finding) -> tuple[str, str]:
    """Return (url, free_text) from a finding that may be a str or dict."""
    if isinstance(finding, str):
        return finding.strip(), finding.strip()
    if isinstance(finding, dict):
        url = (finding.get("url") or finding.get("path")
               or finding.get("route") or "")
        free_parts = [
            str(finding.get("issue") or ""),
            str(finding.get("detail") or ""),
            str(finding.get("message") or ""),
            str(finding.get("title") or ""),
            str(finding.get("file") or finding.get("filename") or ""),
            str(finding.get("table") or ""),
            str(url or ""),
        ]
        return str(url or "").strip(), " ".join(p for p in free_parts if p).strip()
    return "", str(finding or "").strip()


def _distinctive_tokens(text: str, limit: int = 6) -> list:
    """Pull the most distinctive tokens for a fallback text grep. Prefers
    longer / snake_case / underscore-bearing tokens (e.g. function names,
    table names) and drops generic stopwords."""
    seen = set()
    scored = []
    for m in _RE_TOKEN.finditer(text or ""):
        tok = m.group(0)
        low = tok.lower()
        if low in _TOKEN_STOP or low in seen:
            continue
        seen.add(low)
        # Score: underscores (snake_case → likely a symbol) and length win.
        score = len(tok) + (5 if "_" in tok else 0)
        scored.append((score, tok))
    scored.sort(reverse=True)
    return [t for _, t in scored[:limit]]


# ──────────────────────────────────────────────────────────────────
# The resolver.
# ──────────────────────────────────────────────────────────────────
def resolve_finding_to_sources(finding: dict, repo_root: str = "") -> list:
    """Resolve an abstract brain finding to ranked concrete source
    locations.

    Args:
      finding: the finding dict from actionable_backend_issues (keys like
               "url" / "issue"), or a raw url-or-text string.
      repo_root: repo root to resolve against. Defaults to two dirs up
               from this file (matches brain_v2_layer5._read_window), so
               returned `file` paths are repo-root-relative.

    Returns:
      A list (≤5) of candidate dicts, ranked best-first:
        {"file": str, "line": int, "snippet": str,
         "confidence": float, "match_kind": "route|filename|table|symbol|text"}
      Empty list on any miss. NEVER raises.
    """
    try:
        root = os.path.abspath(repo_root) if repo_root else _default_repo_root()
        idx = _get_index(root)

        url, free_text = _finding_text(finding)
        candidates: list = []          # collected, then ranked + capped
        seen_keys = set()              # dedupe on (file, line)

        def _add(file, line, snippet, confidence, kind):
            key = (file, int(line or 0))
            if key in seen_keys:
                # Keep the higher-confidence sighting of the same location.
                for c in candidates:
                    if (c["file"], c["line"]) == key and confidence > c["confidence"]:
                        c["confidence"] = round(float(confidence), 3)
                        c["match_kind"] = kind
                        c["snippet"] = snippet or c["snippet"]
                    break
                return
            seen_keys.add(key)
            candidates.append({
                "file": file,
                "line": int(line or 0),
                "snippet": snippet or "",
                "confidence": round(float(confidence), 3),
                "match_kind": kind,
            })

        # ---- 1. ROUTE match -------------------------------------------
        # Extract candidate URL path(s) from the finding. dchub://cron/x
        # is NOT a real route; strip it to the loop name for symbol/file
        # resolution instead.
        url_paths: list = []
        cron_name = None
        cm = _RE_CRON_URL.search(url) or _RE_CRON_URL.search(free_text)
        if cm:
            cron_name = cm.group(1)
        if url.startswith("/"):
            url_paths.append(url.split("?", 1)[0])
        # also scan free text for bare /paths (some findings embed them)
        for pm in _RE_URL_PATH.finditer(free_text):
            p = pm.group(0)
            if p not in url_paths and not p.endswith(".py"):
                url_paths.append(p)

        for upath in url_paths[:4]:
            up_clean = upath.rstrip("/") or "/"
            for r in idx.get("routes", []):
                conf = None
                kind = "route"
                if r["raw"] == upath or r["raw"].rstrip("/") == up_clean:
                    conf = 0.98                       # exact literal match
                elif r["regex"] is not None and r["regex"].match(up_clean):
                    conf = 0.92                       # param-pattern match
                else:
                    # Prefix/parent relationship — but only when the
                    # shared prefix is a MEANINGFUL path, never a bare
                    # "/" (which would match every root route at 0.6 and
                    # bury the real hits). Require ≥2 path segments shared.
                    raw_clean = r["raw"].rstrip("/")
                    if ("<" not in r["raw"] and raw_clean.count("/") >= 2 and (
                            up_clean.startswith(raw_clean + "/")
                            or raw_clean.startswith(up_clean + "/"))):
                        conf = 0.6                    # prefix/parent match
                if conf is not None:
                    _add(r["file"], r["line"], r["snippet"], conf, kind)

        # ---- 2. FILENAME hint -----------------------------------------
        py_hits = set()
        for fm in _RE_PY_FILE.finditer(free_text + " " + url):
            py_hits.add(fm.group(1).lower())
        for base in py_hits:
            for rel in idx.get("files", {}).get(base, []):
                # Slight boost when the hinted file lives under routes/.
                conf = 0.9 if rel.replace("\\", "/").startswith("routes/") else 0.85
                _add(rel, 1, f"(file hint: {base})", conf, "filename")

        # ---- 3. TABLE name --------------------------------------------
        table_names = set()
        for tm in _RE_TABLE_HINT.finditer(free_text + " " + url):
            table_names.add(tm.group(1).split(".")[-1].lower())
        # url shaped like "table:gas_pipelines"
        if url.lower().startswith("table:"):
            table_names.add(url.split(":", 1)[1].split(".")[-1].strip().lower())
        # bare known-table token in the free text (only if it's in the index)
        if not table_names:
            for tok in _distinctive_tokens(free_text, limit=8):
                if tok.lower() in idx.get("tables", {}):
                    table_names.add(tok.lower())
        _kind_conf = {"table_ddl": 0.8, "table_write": 0.7, "table_read": 0.55}
        for tname in table_names:
            for loc in idx.get("tables", {}).get(tname, [])[:8]:
                _add(loc["file"], loc["line"], loc["snippet"],
                     _kind_conf.get(loc["kind"], 0.5), "table")

        # ---- 4. CRON name → symbol/file -------------------------------
        # A dchub://cron/<name> finding has no route; treat <name> as a
        # symbol to grep (the handler/function is usually named after it).
        if cron_name:
            for hit in _grep_token(root, cron_name, idx, limit=4):
                _add(hit["file"], hit["line"], hit["snippet"], 0.5, "symbol")

        # ---- 5. SYMBOL / TEXT fallback --------------------------------
        # Only fall back when we have nothing strong yet — keeps cost down.
        if not candidates or max(c["confidence"] for c in candidates) < 0.6:
            for tok in _distinctive_tokens(free_text or url, limit=4):
                kind = "symbol" if "_" in tok else "text"
                base_conf = 0.45 if "_" in tok else 0.3
                for hit in _grep_token(root, tok, idx, limit=3):
                    _add(hit["file"], hit["line"], hit["snippet"],
                         base_conf, kind)
                if len([c for c in candidates if c["confidence"] >= 0.3]) >= 8:
                    break

        # ---- rank + cap -----------------------------------------------
        # Specificity is encoded in the confidence we assigned per match
        # kind; sort by confidence desc, then by a kind-priority tiebreak,
        # then by file/line for determinism.
        kind_rank = {"route": 0, "filename": 1, "table": 2, "symbol": 3, "text": 4}
        candidates.sort(key=lambda c: (
            -c["confidence"], kind_rank.get(c["match_kind"], 9),
            c["file"], c["line"]))
        return candidates[:5]
    except Exception as e:  # pragma: no cover - the never-raise contract
        print(f"[brain_source_map] resolve failed: {e}", file=sys.stderr)
        return []


# ──────────────────────────────────────────────────────────────────
# Live token grep — the fallback when no structured handle resolved.
# Scans indexed files for the FIRST occurrence of a token. Bounded by
# `limit` distinct files. Never raises.
# ──────────────────────────────────────────────────────────────────
def _grep_token(repo_root: str, token: str, idx: dict, limit: int = 3) -> list:
    out: list = []
    if not token or len(token) < 3:
        return out
    try:
        needle = re.compile(r"\b" + re.escape(token) + r"\b")
    except Exception:
        return out
    # Walk the same file set the index used (its keys' paths), so we don't
    # re-walk the tree. files: basename -> [rel...]; flatten to rel paths.
    rel_paths = []
    for paths in idx.get("files", {}).values():
        rel_paths.extend(paths)
    for rel in rel_paths:
        if len(out) >= limit:
            break
        full = os.path.join(repo_root, rel)
        try:
            if os.path.getsize(full) > _MAX_FILE_BYTES:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if needle.search(line):
                        out.append({
                            "file": rel, "line": lineno,
                            "snippet": line.strip()[:300],
                        })
                        break  # first hit per file is enough
        except Exception:
            continue
    return out


# ──────────────────────────────────────────────────────────────────
# Endpoint — admin-gated, returns the ranked candidates. Testable.
# ──────────────────────────────────────────────────────────────────
@brain_source_map_bp.get("/api/v1/brain/source-map")
def brain_source_map():
    """Resolve a finding (url-or-text) to ranked candidate source
    locations. Admin-gated identically to the other brain endpoints
    (X-Admin-Key header or admin_key query arg).

    Query params:
      finding   the finding url-or-text to resolve (required).
                Accepts a raw string OR, via finding_url / finding_issue,
                the two structured keys layer5 findings carry.
      rebuild   "1" to force an index rebuild before resolving (admin
                tool for after a deploy; the index is otherwise cached).
    """
    auth_err = _admin_guard()
    if auth_err:
        return auth_err

    finding_arg = (request.args.get("finding") or "").strip()
    f_url = (request.args.get("finding_url") or "").strip()
    f_issue = (request.args.get("finding_issue") or "").strip()

    if request.args.get("rebuild") in ("1", "true", "yes"):
        _get_index(_default_repo_root(), rebuild=True)

    if not finding_arg and not f_url and not f_issue:
        return jsonify(error="finding (or finding_url/finding_issue) required",
                       hint="?finding=/api/v1/facilities/delta"), 400

    if f_url or f_issue:
        finding: dict = {"url": f_url, "issue": f_issue}
    elif finding_arg.startswith("/") or "://" in finding_arg \
            or finding_arg.lower().startswith("table:"):
        finding = {"url": finding_arg}
    else:
        finding = {"issue": finding_arg}

    candidates = resolve_finding_to_sources(finding)
    idx = _get_index(_default_repo_root())
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        finding=finding,
        resolved=bool(candidates),
        count=len(candidates),
        candidates=candidates,
        index={
            "py_files": idx.get("py_files", 0),
            "route_count": idx.get("route_count", 0),
            "table_count": idx.get("table_count", 0),
            "built_at": idx.get("built_at"),
        },
    ), 200
