"""
brain_code_scanner.py — Brain reads its own CODEBASE (2026-06-07, Round 3).
============================================================================

PROBLEM
-------
Round 1 + 2 wired the brain to dashboards (funnel-health, sentinel-inbox,
media-mix), then to PR outcomes (brain_pr_outcomes), then to its own
self-assessments (brain_self_perception). The remaining blind spot: the
brain has never looked at its OWN SOURCE FILES.

  · It doesn't know which routes are huge.
  · It doesn't know which functions are dead.
  · It doesn't know which files lack tests.
  · It doesn't know what stale code is being modified weekly.

This module closes that gap. Once a day at 03:30 UTC it walks routes/*.py
(~430 files post-Round-3) and computes:

  · line_count
  · function_count
  · max_nesting depth (a cheap complexity proxy)
  · called_from_count (how many OTHER files import / reference the
    public top-level functions defined in this file)
  · has_tests (file basename appears in tests/ or test_*.py somewhere)
  · last_modified mtime
  · recent_commit_count_7d (cheap git log probe; 0 if not a repo)

Surfaces hotspots:
  · top-10 highest-complexity
  · top-10 untested
  · top-10 stale-but-modified-this-week (high churn, no tests)
  · top-10 large + uncalled (likely dead)

Writes one row per file to brain_code_inventory (file_path PK). The next
scan UPSERTs; columns track diffs over time so the operator can see "this
file grew from 500 → 1200 lines without growing test coverage".

Feeds INTO strategic synthesis via gather_code_inventory_context() so the
next L6 prompt sees: "here are the hotspots in YOUR own codebase".

ENDPOINTS
---------
  POST /api/v1/admin/brain/code-scanner/run        trigger scan (admin)
  GET  /api/v1/admin/brain/code-scanner/preview    dry-run (admin)
  GET  /api/v1/admin/brain/code-scanner/status     config + health (admin)
  GET  /api/v1/brain/code-inventory/hotspots       top-N (public read)
  GET  /api/v1/brain/code-inventory/file           per-file detail
  GET  /admin/brain-code-inventory                 dashboard

CRON
----
  crawler_scheduler.SCHEDULE "brain_code_scanner" (3, 3) at 03:30 UTC.
  Self-gates same-day idempotency via brain_code_inventory_runs.

COST
----
  Pure file IO + grep + ast.parse. Zero Claude tokens. ~$0/run.

SAFETY
------
  · BRAIN_R3_DISABLE=1                      master kill switch (covers
                                            scanner + architecture + Q&A)
  · BRAIN_CODE_SCANNER_DISABLE=1            per-unlock kill switch
  · Read-only — never edits source.
  · Capped at routes/*.py ≤ 5000 files (sanity guard).
  · Bounded ast.walk per file (skips files >2 MB).
"""
from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Optional

from flask import Blueprint, jsonify, render_template_string, request

logger = logging.getLogger(__name__)

brain_code_scanner_bp = Blueprint("brain_code_scanner", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir))
_ROUTES_DIR = os.path.join(_REPO_ROOT, "routes")
_TESTS_DIR = os.path.join(_REPO_ROOT, "tests")
_MAX_FILE_BYTES = 2 * 1024 * 1024   # 2 MB sanity cap per file
_MAX_FILES = 5000                    # absolute upper bound


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return (_truthy(os.environ.get("BRAIN_R3_DISABLE"))
            or _truthy(os.environ.get("BRAIN_CODE_SCANNER_DISABLE")))


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


# ─── DB ─────────────────────────────────────────────────────────────

def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_code_inventory (
    file_path           TEXT PRIMARY KEY,
    line_count          INTEGER NOT NULL DEFAULT 0,
    function_count      INTEGER NOT NULL DEFAULT 0,
    class_count         INTEGER NOT NULL DEFAULT 0,
    max_nesting         INTEGER NOT NULL DEFAULT 0,
    called_from_count   INTEGER NOT NULL DEFAULT 0,
    public_func_count   INTEGER NOT NULL DEFAULT 0,
    has_tests           BOOLEAN NOT NULL DEFAULT FALSE,
    last_modified       TIMESTAMPTZ,
    commits_7d          INTEGER NOT NULL DEFAULT 0,
    bytes               BIGINT NOT NULL DEFAULT 0,
    sha256_short        TEXT,
    last_scanned        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prev_line_count     INTEGER,
    prev_function_count INTEGER,
    notes               JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_bci_complexity
    ON brain_code_inventory(max_nesting DESC, line_count DESC);
CREATE INDEX IF NOT EXISTS ix_bci_untested
    ON brain_code_inventory(has_tests, line_count DESC);
CREATE INDEX IF NOT EXISTS ix_bci_stale_modified
    ON brain_code_inventory(commits_7d DESC, last_modified DESC);

CREATE TABLE IF NOT EXISTS brain_code_inventory_runs (
    id                  BIGSERIAL PRIMARY KEY,
    ran_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ran_on              DATE NOT NULL DEFAULT CURRENT_DATE,
    files_scanned       INTEGER NOT NULL DEFAULT 0,
    files_changed       INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    findings_count      INTEGER NOT NULL DEFAULT 0,
    summary             TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bci_runs_day
    ON brain_code_inventory_runs(ran_on);
"""


def _ensure_schema(c) -> None:
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("brain_code_scanner _ensure_schema failed: %s", e)


# ─── AST analysis ─────────────────────────────────────────────────

def _max_nesting_depth(tree: ast.AST) -> int:
    """Max depth of nested control flow / functions / classes. Cheap
    complexity proxy — code with depth >5 is almost always hard to test
    + hard to read. Pure-ast.walk would lose nesting info; instead we
    descend manually counting only block-introducing nodes."""
    BLOCK_NODES = (
        ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
        ast.For, ast.AsyncFor, ast.While, ast.If, ast.Try,
        ast.With, ast.AsyncWith,
    )

    def _walk(node: ast.AST, depth: int, best: list) -> None:
        if depth > best[0]:
            best[0] = depth
        for child in ast.iter_child_nodes(node):
            if isinstance(child, BLOCK_NODES):
                _walk(child, depth + 1, best)
            else:
                _walk(child, depth, best)

    best = [0]
    _walk(tree, 0, best)
    return best[0]


def _top_level_public_funcs(tree: ast.AST) -> list:
    """Top-level public functions (not starting with _).
    Used to compute called_from_count downstream."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out.append(node.name)
    return out


def _count_funcs_classes(tree: ast.AST) -> tuple:
    """(function_count, class_count) — all levels."""
    fc = sum(1 for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    cc = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    return fc, cc


# ─── Scanner ──────────────────────────────────────────────────────

_IGNORE_DIRS = {"__pycache__", ".git", ".pg_migration_backups",
                ".wrangler", ".pytest_cache", "node_modules"}


def _iter_route_files() -> list:
    """Yield absolute paths to routes/*.py (non-recursive — only the
    routes/ directory level). Skips dunder + hidden + backup files."""
    out = []
    try:
        for name in os.listdir(_ROUTES_DIR):
            if not name.endswith(".py"):
                continue
            if name.startswith("."):
                continue
            full = os.path.join(_ROUTES_DIR, name)
            if not os.path.isfile(full):
                continue
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    continue
            except Exception:
                continue
            out.append(full)
            if len(out) >= _MAX_FILES:
                break
    except Exception as e:
        logger.error("scanner _iter_route_files failed: %s", e)
    return sorted(out)


def _scan_one(path: str) -> dict:
    """Parse a single file and return all metrics. Never raises."""
    rel = os.path.relpath(path, _REPO_ROOT)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            src = f.read()
    except Exception as e:
        return {"file_path": rel, "_error": f"read:{e}"}

    line_count = src.count("\n") + (1 if src and not src.endswith("\n") else 0)
    byte_size = len(src.encode("utf-8", errors="ignore"))
    sha = hashlib.sha256(src.encode("utf-8", errors="ignore")).hexdigest()[:12]

    try:
        st = os.stat(path)
        mtime = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc)
    except Exception:
        mtime = None

    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError as e:
        return {"file_path": rel,
                "line_count": line_count, "function_count": 0,
                "class_count": 0, "max_nesting": 0,
                "public_funcs": [], "bytes": byte_size,
                "sha256_short": sha, "last_modified": mtime,
                "_error": f"syntax:{e.lineno}"}

    fc, cc = _count_funcs_classes(tree)
    nesting = _max_nesting_depth(tree)
    public_funcs = _top_level_public_funcs(tree)

    return {
        "file_path": rel,
        "line_count": line_count,
        "function_count": fc,
        "class_count": cc,
        "max_nesting": nesting,
        "public_funcs": public_funcs,
        "bytes": byte_size,
        "sha256_short": sha,
        "last_modified": mtime,
    }


# ─── Cross-file reference counter ──────────────────────────────────

def _build_reference_index(files: list) -> dict:
    """For each scanned file, count how many OTHER files reference any
    of its public top-level function names. Quick grep-style index — not
    perfect (false positives on common names like 'run', 'get_db') but
    good enough to spot the obviously-dead 0-callers files.

    Returns {file_path: called_from_count}.
    """
    # First pass: read every file's source (we already have results;
    # re-open is fast — cached at OS layer after the scan).
    all_sources = {}
    for path in files:
        rel = os.path.relpath(path, _REPO_ROOT)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                all_sources[rel] = f.read()
        except Exception:
            all_sources[rel] = ""

    # Also pull main.py + crawler_scheduler.py + tests/ as "outside
    # callers" so route files that only main.py imports show up as called.
    for extra in ("main.py", "crawler_scheduler.py"):
        p = os.path.join(_REPO_ROOT, extra)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    all_sources[extra] = f.read()
            except Exception:
                pass
    try:
        if os.path.isdir(_TESTS_DIR):
            for tname in os.listdir(_TESTS_DIR):
                if not tname.endswith(".py"):
                    continue
                tp = os.path.join(_TESTS_DIR, tname)
                try:
                    with open(tp, "r", encoding="utf-8",
                              errors="ignore") as f:
                        all_sources[f"tests/{tname}"] = f.read()
                except Exception:
                    pass
    except Exception:
        pass

    return all_sources


def _count_callers(rel_path: str, public_funcs: list,
                   all_sources: dict) -> int:
    """Count files OTHER than rel_path that reference any public_funcs
    name OR the module's own name. Very conservative — only word-boundary
    matches; deduped by file."""
    if not public_funcs:
        # No public surface — fall back to module-import detection
        mod_name = os.path.splitext(os.path.basename(rel_path))[0]
        callers = set()
        # `routes.<mod>` or `from routes.<mod>` import
        pat = re.compile(rf"\broutes\.{re.escape(mod_name)}\b")
        for fp, src in all_sources.items():
            if fp == rel_path:
                continue
            if pat.search(src):
                callers.add(fp)
        return len(callers)

    # Build a single regex of all public funcs as word-boundary OR
    parts = [re.escape(name) for name in public_funcs]
    pat = re.compile(r"\b(?:" + "|".join(parts) + r")\b")
    callers = set()
    for fp, src in all_sources.items():
        if fp == rel_path:
            continue
        if pat.search(src):
            callers.add(fp)
    return len(callers)


def _has_tests_for(rel_path: str, all_sources: dict) -> bool:
    """Cheap test-coverage proxy: does any tests/*.py mention the file's
    module name? (e.g. `from routes.brain_pr_opener` or just the bare
    word brain_pr_opener.) Also check tests/* basenames."""
    mod_name = os.path.splitext(os.path.basename(rel_path))[0]
    needle = re.compile(rf"\b{re.escape(mod_name)}\b")

    # tests/test_<mod>.py
    candidate = os.path.join(_TESTS_DIR, f"test_{mod_name}.py")
    if os.path.exists(candidate):
        return True

    # Any tests/*.py mentions this mod_name
    for fp, src in all_sources.items():
        if not fp.startswith("tests/"):
            continue
        if needle.search(src):
            return True
    return False


# ─── Git probe ────────────────────────────────────────────────────

def _git_commits_7d_for(rel_path: str) -> int:
    """Count commits to rel_path in last 7d. Falls back to 0 on any
    error (subprocess timeout, no git, detached HEAD, etc)."""
    try:
        r = subprocess.run(
            ["git", "log", "--since=7.days", "--oneline", "--", rel_path],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return 0
        out = (r.stdout or "").strip()
        if not out:
            return 0
        return len([line for line in out.splitlines() if line.strip()])
    except Exception:
        return 0


# ─── Persistence ──────────────────────────────────────────────────

def _upsert_one(c, row: dict) -> bool:
    """UPSERT one inventory row. Returns True if row materially changed
    (line_count or function_count diff)."""
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            # Read prior to detect change
            cur.execute(
                """SELECT line_count, function_count
                     FROM brain_code_inventory
                    WHERE file_path = %s""",
                (row["file_path"],))
            prior = cur.fetchone()
            prev_lc = int(prior[0]) if prior else None
            prev_fc = int(prior[1]) if prior else None
            material_change = (
                prior is None
                or prev_lc != row["line_count"]
                or prev_fc != row["function_count"])

            cur.execute(
                """INSERT INTO brain_code_inventory (
                       file_path, line_count, function_count,
                       class_count, max_nesting, called_from_count,
                       public_func_count, has_tests, last_modified,
                       commits_7d, bytes, sha256_short, last_scanned,
                       prev_line_count, prev_function_count, notes
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       NOW(), %s, %s, %s::jsonb
                   )
                   ON CONFLICT (file_path) DO UPDATE
                       SET line_count = EXCLUDED.line_count,
                           function_count = EXCLUDED.function_count,
                           class_count = EXCLUDED.class_count,
                           max_nesting = EXCLUDED.max_nesting,
                           called_from_count = EXCLUDED.called_from_count,
                           public_func_count = EXCLUDED.public_func_count,
                           has_tests = EXCLUDED.has_tests,
                           last_modified = EXCLUDED.last_modified,
                           commits_7d = EXCLUDED.commits_7d,
                           bytes = EXCLUDED.bytes,
                           sha256_short = EXCLUDED.sha256_short,
                           last_scanned = NOW(),
                           prev_line_count = brain_code_inventory.line_count,
                           prev_function_count = brain_code_inventory.function_count,
                           notes = EXCLUDED.notes""",
                (row["file_path"], row["line_count"],
                 row["function_count"], row.get("class_count", 0),
                 row.get("max_nesting", 0),
                 row.get("called_from_count", 0),
                 row.get("public_func_count", 0),
                 bool(row.get("has_tests", False)),
                 row.get("last_modified"),
                 row.get("commits_7d", 0),
                 row.get("bytes", 0),
                 row.get("sha256_short"),
                 prev_lc, prev_fc,
                 json.dumps(row.get("notes") or {}, default=str)))
        try: c.commit()
        except Exception: pass
        return material_change
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("scanner _upsert_one failed for %s: %s",
                        row.get("file_path"), e)
        return False


def _write_run_summary(c, files_scanned: int, files_changed: int,
                        duration_ms: int, findings_count: int,
                        summary: str) -> int:
    """Write one row per UTC day (UPSERT). Returns row id."""
    if c is None:
        return 0
    try:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_code_inventory_runs (
                       files_scanned, files_changed, duration_ms,
                       findings_count, summary)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (ran_on) DO UPDATE
                       SET files_scanned = EXCLUDED.files_scanned,
                           files_changed = EXCLUDED.files_changed,
                           duration_ms = EXCLUDED.duration_ms,
                           findings_count = EXCLUDED.findings_count,
                           summary = EXCLUDED.summary,
                           ran_at = NOW()
                   RETURNING id""",
                (files_scanned, files_changed, duration_ms,
                 findings_count, summary[:1200]))
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        return int(row[0]) if row else 0
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("scanner _write_run_summary failed: %s", e)
        return 0


# ─── Hotspot queries ──────────────────────────────────────────────

def _hotspots(c, limit: int = 10) -> dict:
    """Return the four hotspot lists used by the dashboard + the
    L6 strategic context feed."""
    if c is None:
        return {"_note": "no_db"}
    out = {
        "highest_complexity": [],
        "untested_large":     [],
        "stale_but_modified": [],
        "likely_dead":        [],
    }
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT file_path, line_count, function_count,
                          max_nesting, has_tests, commits_7d,
                          called_from_count
                     FROM brain_code_inventory
                    ORDER BY max_nesting DESC, line_count DESC
                    LIMIT %s""", (limit,))
            for r in (cur.fetchall() or []):
                out["highest_complexity"].append({
                    "file_path": r[0], "line_count": int(r[1] or 0),
                    "function_count": int(r[2] or 0),
                    "max_nesting": int(r[3] or 0),
                    "has_tests": bool(r[4]),
                    "commits_7d": int(r[5] or 0),
                    "called_from_count": int(r[6] or 0),
                })
            cur.execute(
                """SELECT file_path, line_count, function_count,
                          commits_7d, called_from_count
                     FROM brain_code_inventory
                    WHERE has_tests = FALSE
                      AND line_count >= 200
                    ORDER BY line_count DESC LIMIT %s""", (limit,))
            for r in (cur.fetchall() or []):
                out["untested_large"].append({
                    "file_path": r[0], "line_count": int(r[1] or 0),
                    "function_count": int(r[2] or 0),
                    "commits_7d": int(r[3] or 0),
                    "called_from_count": int(r[4] or 0),
                })
            cur.execute(
                """SELECT file_path, line_count, commits_7d,
                          has_tests, last_modified
                     FROM brain_code_inventory
                    WHERE commits_7d >= 1
                    ORDER BY commits_7d DESC, line_count DESC
                    LIMIT %s""", (limit,))
            for r in (cur.fetchall() or []):
                out["stale_but_modified"].append({
                    "file_path": r[0], "line_count": int(r[1] or 0),
                    "commits_7d": int(r[2] or 0),
                    "has_tests": bool(r[3]),
                    "last_modified": (r[4].isoformat()
                                       if r[4] else None),
                })
            cur.execute(
                """SELECT file_path, line_count, called_from_count,
                          public_func_count, last_modified
                     FROM brain_code_inventory
                    WHERE called_from_count = 0
                      AND public_func_count >= 1
                      AND line_count >= 100
                    ORDER BY line_count DESC LIMIT %s""", (limit,))
            for r in (cur.fetchall() or []):
                out["likely_dead"].append({
                    "file_path": r[0], "line_count": int(r[1] or 0),
                    "called_from_count": int(r[2] or 0),
                    "public_func_count": int(r[3] or 0),
                    "last_modified": (r[4].isoformat()
                                       if r[4] else None),
                })
    except Exception as e:
        out["_error"] = str(e)[:200]
    return out


# Public helper for L6 strategic synthesis to consume.
def gather_code_inventory_context() -> dict:
    """Return a small, prompt-ready snapshot of the inventory. Used by
    brain_strategic_planner to feed the L6 prompt as another channel."""
    c = _get_db()
    if c is None:
        return {"_note": "no_db"}
    try:
        out = _hotspots(c, limit=5)
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*),
                          SUM(line_count),
                          SUM(CASE WHEN has_tests THEN 1 ELSE 0 END),
                          MAX(last_scanned)
                     FROM brain_code_inventory""")
            r = cur.fetchone() or (0, 0, 0, None)
            out["totals"] = {
                "files": int(r[0] or 0),
                "lines": int(r[1] or 0),
                "files_with_tests": int(r[2] or 0),
                "last_scanned": (r[3].isoformat()
                                   if r[3] else None),
            }
            cur.execute(
                """SELECT MAX(ran_at), MAX(files_scanned),
                          MAX(findings_count)
                     FROM brain_code_inventory_runs""")
            r2 = cur.fetchone() or (None, 0, 0)
            out["last_run"] = {
                "ran_at": (r2[0].isoformat() if r2[0] else None),
                "files_scanned": int(r2[1] or 0),
                "findings_count": int(r2[2] or 0),
            }
        return out
    except Exception as e:
        return {"_note": "read_failed", "error": str(e)[:200]}
    finally:
        try: c.close()
        except Exception: pass


# ─── Run ──────────────────────────────────────────────────────────

def run_code_scan(force: bool = False) -> dict:
    """Scan routes/*.py end-to-end. Returns a summary dict (never raises).

    Idempotency: one row/UTC date in brain_code_inventory_runs unless
    force=True. Returns the cached row id if already ran today."""
    started = time.time()

    if _kill_switch_on():
        return {"ok": False, "reason": "kill_switch_on",
                "env": "BRAIN_R3_DISABLE or BRAIN_CODE_SCANNER_DISABLE"}

    c = _get_db()
    if c is None:
        return {"ok": False, "reason": "no_db"}
    _ensure_schema(c)

    # Idempotency check
    if not force:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT id, files_scanned, findings_count
                         FROM brain_code_inventory_runs
                        WHERE ran_on = CURRENT_DATE
                        ORDER BY ran_at DESC LIMIT 1""")
                row = cur.fetchone()
            if row:
                return {"ok": True, "from_cache": True,
                        "run_id": int(row[0]),
                        "files_scanned": int(row[1] or 0),
                        "findings_count": int(row[2] or 0),
                        "note": "Already ran today; pass force=1."}
        except Exception:
            pass

    files = _iter_route_files()
    all_sources = _build_reference_index(files)

    results = []
    files_changed = 0
    for path in files:
        try:
            row = _scan_one(path)
            rel = row["file_path"]
            row["called_from_count"] = _count_callers(
                rel, row.get("public_funcs") or [], all_sources)
            row["public_func_count"] = len(row.get("public_funcs") or [])
            row["has_tests"] = _has_tests_for(rel, all_sources)
            row["commits_7d"] = _git_commits_7d_for(rel)
            # Sanity: don't try to write the public_funcs list itself
            # (notes is for forward-compat free-form metadata only)
            row["notes"] = {"public_funcs":
                              (row.pop("public_funcs", None) or [])[:20]}
            if _upsert_one(c, row):
                files_changed += 1
            results.append(row)
        except Exception as e:
            logger.warning("scanner: %s failed: %s", path, e)
            continue

    duration_ms = int((time.time() - started) * 1000)

    # Findings count = sum of hotspot list lengths
    hot = _hotspots(c, limit=10)
    findings = sum(len(v) for k, v in hot.items()
                    if isinstance(v, list))

    summary = (
        f"Scanned {len(files)} files in {duration_ms}ms. "
        f"{files_changed} materially changed. "
        f"Hotspots: complexity={len(hot.get('highest_complexity') or [])}, "
        f"untested_large={len(hot.get('untested_large') or [])}, "
        f"churn={len(hot.get('stale_but_modified') or [])}, "
        f"likely_dead={len(hot.get('likely_dead') or [])}."
    )

    run_id = _write_run_summary(c, len(files), files_changed,
                                  duration_ms, findings, summary)

    try: c.close()
    except Exception: pass

    return {
        "ok": True,
        "from_cache": False,
        "run_id": run_id,
        "files_scanned": len(files),
        "files_changed": files_changed,
        "duration_ms": duration_ms,
        "findings_count": findings,
        "summary": summary,
        "hotspots": hot,
    }


# ─── HTTP routes ─────────────────────────────────────────────────

@brain_code_scanner_bp.route(
    "/api/v1/admin/brain/code-scanner/run", methods=["POST", "GET"])
def code_scanner_run():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    force = (_truthy(request.args.get("force"))
              or _truthy(body.get("force")))
    return jsonify(run_code_scan(force=force)), 200


@brain_code_scanner_bp.route(
    "/api/v1/admin/brain/code-scanner/preview", methods=["GET"])
def code_scanner_preview():
    """Dry-run: count files + estimate cost without writing."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    files = _iter_route_files()
    return jsonify(
        ok=True, dry_run=True,
        files_found=len(files),
        max_files=_MAX_FILES,
        first_10=[os.path.relpath(p, _REPO_ROOT) for p in files[:10]],
        last_10=[os.path.relpath(p, _REPO_ROOT) for p in files[-10:]],
    ), 200


@brain_code_scanner_bp.route(
    "/api/v1/admin/brain/code-scanner/status", methods=["GET"])
def code_scanner_status():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    c = _get_db()
    last_run = None
    totals = None
    if c is not None:
        _ensure_schema(c)
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT id, ran_at, ran_on, files_scanned,
                              files_changed, duration_ms, findings_count,
                              summary
                         FROM brain_code_inventory_runs
                        ORDER BY ran_at DESC LIMIT 1""")
                r = cur.fetchone()
                if r:
                    last_run = {
                        "id": int(r[0]), "ran_at": r[1].isoformat(),
                        "ran_on": str(r[2]),
                        "files_scanned": int(r[3] or 0),
                        "files_changed": int(r[4] or 0),
                        "duration_ms": int(r[5] or 0),
                        "findings_count": int(r[6] or 0),
                        "summary": r[7],
                    }
                cur.execute(
                    """SELECT COUNT(*),
                              SUM(line_count),
                              SUM(CASE WHEN has_tests THEN 1 ELSE 0 END)
                         FROM brain_code_inventory""")
                t = cur.fetchone() or (0, 0, 0)
                totals = {"files": int(t[0] or 0),
                          "lines": int(t[1] or 0),
                          "files_with_tests": int(t[2] or 0)}
        except Exception as e:
            last_run = {"_error": str(e)[:200]}
        finally:
            try: c.close()
            except Exception: pass
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        admin_key_set=bool(_admin_key()),
        repo_root=_REPO_ROOT,
        routes_dir=_ROUTES_DIR,
        tests_dir=_TESTS_DIR,
        last_run=last_run,
        totals=totals,
        env_overrides={
            "BRAIN_R3_DISABLE":             "master kill (1=off)",
            "BRAIN_CODE_SCANNER_DISABLE":   "per-unlock kill (1=off)",
        },
    ), 200


@brain_code_scanner_bp.route(
    "/api/v1/brain/code-inventory/hotspots", methods=["GET"])
def code_inventory_hotspots():
    """PUBLIC read — top-N hotspots in each category."""
    try:
        limit = int(request.args.get("limit") or 10)
    except Exception:
        limit = 10
    limit = max(1, min(limit, 50))
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        return jsonify(ok=True, limit=limit,
                        hotspots=_hotspots(c, limit=limit)), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_code_scanner_bp.route(
    "/api/v1/brain/code-inventory/file", methods=["GET"])
def code_inventory_file():
    """PUBLIC read — per-file inventory row (?path=routes/foo.py)."""
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify(ok=False, error="missing path"), 400
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT file_path, line_count, function_count,
                          class_count, max_nesting, called_from_count,
                          public_func_count, has_tests, last_modified,
                          commits_7d, bytes, sha256_short, last_scanned,
                          prev_line_count, prev_function_count, notes
                     FROM brain_code_inventory
                    WHERE file_path = %s""", (path,))
            r = cur.fetchone()
        if not r:
            return jsonify(ok=False, error="not_found",
                            path=path), 404
        notes = r[15] if isinstance(r[15], dict) else (
            json.loads(r[15]) if r[15] else {})
        return jsonify(
            ok=True,
            file_path=r[0],
            line_count=int(r[1] or 0),
            function_count=int(r[2] or 0),
            class_count=int(r[3] or 0),
            max_nesting=int(r[4] or 0),
            called_from_count=int(r[5] or 0),
            public_func_count=int(r[6] or 0),
            has_tests=bool(r[7]),
            last_modified=(r[8].isoformat() if r[8] else None),
            commits_7d=int(r[9] or 0),
            bytes=int(r[10] or 0),
            sha256_short=r[11],
            last_scanned=(r[12].isoformat() if r[12] else None),
            prev_line_count=(int(r[13]) if r[13] is not None else None),
            prev_function_count=(int(r[14])
                                   if r[14] is not None else None),
            notes=notes,
        ), 200
    finally:
        try: c.close()
        except Exception: pass


# ─── Dashboard ────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Brain — Code Inventory</title>
<style>
 body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 1200px; margin: 0 auto; padding: 2rem 1rem;
         color: #111; background: #fafafa; }
 h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
 h2 { font-size: 1.05rem; margin: 1.5rem 0 0.5rem; color: #444; }
 .meta { font-size: 0.85rem; color: #666; margin-bottom: 1rem; }
 table { width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #e5e5e5; }
 th, td { padding: 0.4rem 0.6rem; text-align: left;
           border-bottom: 1px solid #f0f0f0; font-size: 0.85rem; }
 th { background: #f5f5f5; font-weight: 600; }
 .num { text-align: right; font-variant-numeric: tabular-nums; }
 .red { color: #b00; font-weight: 600; }
 .green { color: #0a6; }
 .pill { display: inline-block; padding: 0.05rem 0.4rem;
          border-radius: 4px; font-size: 0.7rem; background: #eef;
          margin-left: 0.3rem; }
 .topbar { display:flex; justify-content:space-between;
            align-items:center; margin-bottom:1.5rem; }
 button { background: #06f; color: white; border: 0;
           padding: 0.5rem 1rem; border-radius: 4px; cursor: pointer; }
 #status { font-size: 0.8rem; color: #666; margin-left: 0.5rem; }
</style></head><body>
<div class="topbar">
  <h1>Brain — Code Inventory</h1>
  <div>
    <button onclick="runScan()">Run Scan Now</button>
    <span id="status"></span>
  </div>
</div>
<div class="meta" id="meta">Loading...</div>

<h2>Top-10 Highest Complexity (max nesting depth)</h2>
<table id="t-complexity"><thead><tr>
<th>File</th><th class="num">Lines</th><th class="num">Funcs</th>
<th class="num">Nesting</th><th>Tests?</th><th class="num">Callers</th>
</tr></thead><tbody></tbody></table>

<h2>Top-10 Untested + Large (≥200 lines, no tests)</h2>
<table id="t-untested"><thead><tr>
<th>File</th><th class="num">Lines</th><th class="num">Funcs</th>
<th class="num">Commits/7d</th><th class="num">Callers</th>
</tr></thead><tbody></tbody></table>

<h2>Top-10 Stale-But-Modified (high churn, last 7d)</h2>
<table id="t-churn"><thead><tr>
<th>File</th><th class="num">Lines</th><th class="num">Commits/7d</th>
<th>Tests?</th><th>Last Modified</th>
</tr></thead><tbody></tbody></table>

<h2>Top-10 Likely Dead (0 callers, ≥100 lines, has public funcs)</h2>
<table id="t-dead"><thead><tr>
<th>File</th><th class="num">Lines</th><th class="num">Public Funcs</th>
<th class="num">Callers</th><th>Last Modified</th>
</tr></thead><tbody></tbody></table>

<script>
const ADMIN_KEY =
  new URLSearchParams(location.search).get('admin_key') || '';

function row(...cells){
  const tr = document.createElement('tr');
  cells.forEach(c => {
    const td = document.createElement('td');
    if (c && c.cls) { td.className = c.cls; td.textContent = c.text; }
    else td.textContent = c == null ? '' : String(c);
    tr.appendChild(td);
  });
  return tr;
}

async function load(){
  try {
    const r = await fetch(
      '/api/v1/brain/code-inventory/hotspots?limit=10');
    const j = await r.json();
    const h = (j.hotspots || {});
    const fill = (id, list, cols) => {
      const tb = document.querySelector('#'+id+' tbody');
      tb.innerHTML = '';
      (list || []).forEach(item => {
        tb.appendChild(row(...cols(item)));
      });
    };
    fill('t-complexity', h.highest_complexity, x => [
      x.file_path,
      {cls:'num', text:x.line_count},
      {cls:'num', text:x.function_count},
      {cls:'num', text:x.max_nesting > 5 ? '⚠️ ' + x.max_nesting : x.max_nesting},
      x.has_tests ? '✓' : '✗',
      {cls:'num', text:x.called_from_count},
    ]);
    fill('t-untested', h.untested_large, x => [
      x.file_path,
      {cls:'num', text:x.line_count},
      {cls:'num', text:x.function_count},
      {cls:'num', text:x.commits_7d},
      {cls:'num', text:x.called_from_count},
    ]);
    fill('t-churn', h.stale_but_modified, x => [
      x.file_path,
      {cls:'num', text:x.line_count},
      {cls:'num', text:x.commits_7d},
      x.has_tests ? '✓' : '✗',
      x.last_modified ? x.last_modified.slice(0,19) : '',
    ]);
    fill('t-dead', h.likely_dead, x => [
      x.file_path,
      {cls:'num', text:x.line_count},
      {cls:'num', text:x.public_func_count},
      {cls:'num', text:x.called_from_count},
      x.last_modified ? x.last_modified.slice(0,19) : '',
    ]);
    document.getElementById('meta').textContent =
      'Loaded. Toggle Run Scan to refresh inventory.';
  } catch (e) {
    document.getElementById('meta').textContent = 'Load error: ' + e;
  }
}

async function runScan(){
  const s = document.getElementById('status');
  s.textContent = 'Scanning...';
  try {
    const url = '/api/v1/admin/brain/code-scanner/run'
      + (ADMIN_KEY ? '?admin_key=' + encodeURIComponent(ADMIN_KEY) : '');
    const r = await fetch(url, {method:'POST'});
    const j = await r.json();
    s.textContent = j.ok
      ? `Scanned ${j.files_scanned} files in ${j.duration_ms}ms`
      : `Error: ${j.reason || j.error}`;
    load();
  } catch (e) {
    s.textContent = 'Error: ' + e;
  }
}

load();
</script>
</body></html>"""


@brain_code_scanner_bp.route(
    "/admin/brain-code-inventory", methods=["GET"])
def code_inventory_dashboard():
    """Dashboard. Read-only (PUBLIC read on hotspots). To trigger a
    scan, the URL must include ?admin_key=<key>."""
    return render_template_string(_DASHBOARD_HTML)
