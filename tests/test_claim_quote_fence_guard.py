"""Every read of ai_testimonials either excludes human customer quotes or is
on an allowlist with a reason (2026-09-24).

ai_testimonials holds AI-assistant quotes AND human customer quotes
(source='claim_quote': agent_name = the person, context = their company). A
reader that presents rows as AI quotes, or counts them as AI citations or
platforms, must carry util.testimonial_sources.NOT_CLAIM_QUOTE_SQL, or an
approved customer is published as an "AI agent". Before this guard the public
GET /api/v1/testimonials, /stats, /testimonials/live, the DC Hub Media feed and
rails, the live-proof count, /api/v1/site/stats and the north-star velocity
all did so.

How the scan works, and why it can fail:
  * AST, not text: comments and docstrings never count as a read or as a fence.
  * One SQL statement = the maximal string expression (a str constant, an
    f-string, or a `+` chain of them). A fence in a sibling statement of the
    same function does not cover this one.
  * A fence counts when the statement names NOT_CLAIM_QUOTE_SQL, contains its
    exact text, or interpolates a variable that is assigned one of those in
    the same function (dchub_media's `source_filter`).
  * The scan must find at least _MIN_READS reads, and every allowlist entry
    must still match a read, so a scan that finds nothing, or an allowlist
    that outlived its reader, fails.

routes/media_north_star.py builds `FROM {table}` at runtime, which no text scan
can see; its builder is checked directly below.
"""
import ast
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from util.testimonial_sources import NOT_CLAIM_QUOTE_SQL  # noqa: E402

_READ_RE = re.compile(r"\b(?:FROM|JOIN|USING)\s+ai_testimonials\b(?!_)", re.I)
_FENCE_NAME = "NOT_CLAIM_QUOTE_SQL"
_FENCE_TEXT = " ".join(NOT_CLAIM_QUOTE_SQL.split())

# Reads that correctly do NOT exclude claim_quote. Keyed by (file, enclosing
# function); every read inside that function is covered. file-only keys
# (function None) cover the whole file.
_ALLOW = {
    # (b) the human-customer path: these rows ARE the point.
    ("routes/cited_by.py", "_gather_cited_by_data"):
        "the /cited-by 'What customers say' section: approved claim_quote rows only",
    ("routes/testimonial_probe.py", "pending_testimonials"):
        "admin review queue for claim_quote rows (admin-gated)",
    ("flask_mcp_endpoints.py", "identify_key"):
        "capture path: duplicate check before inserting a claim_quote row",
    ("flask_mcp_endpoints.py", "claim_key_quote"):
        "capture path: duplicate check before inserting a claim_quote row",
    # (c) internal / admin / structural. Nothing here publishes a row as AI voice.
    ("routes/testimonial_probe.py", "_already_probed_today"):
        "probe idempotency, source LIKE 'probe_%' only",
    ("routes/testimonial_probe.py", "purge_refusals"):
        "admin cleanup of probe_% rows only",
    ("main.py", "_log_mcp_analytics"):
        "mcp-auto capture de-dup, source = 'mcp-auto' only",
    ("main.py", "delete_testimonial"):
        "admin DELETE by id",
    ("main.py", "seed_testimonials"):
        "seed de-dup by the seed's own quote text",
    ("main.py", "cleanup_testimonials"):
        "admin maintenance writes (DELETE), not a read that publishes",
    ("main.py", "refresh_testimonial_timestamps"):
        "admin maintenance: DELETE platform='test' rows",
    ("main.py", "_media_diagnose"):
        "internal diagnostic: raw table row count",
    ("main.py", "_health_deep"):
        "internal health check: table populated (>= 3 rows)",
    ("main.py", "_health_deep_v2_238"):
        "internal health check: table populated (>= 3 rows)",
    ("dchub_self_heal.py", "fix_backfill_testimonials"):
        "self-heal: seed only when the table is empty",
    ("testimonials_auto_capture.py", None):
        "unimported legacy module: mcp-auto de-dup and cleanup writes",
    # Out of scope for the change that added this guard: another change owns
    # it. It already carries the fence and has its own real-Postgres test
    # (tests/test_agent_broadcast_citations_sql.py).
    ("routes/agent_broadcast.py", None):
        "owned by the agent_broadcast change; fenced in-file, own SQL test",
}

# At least this many reads must be found, so a broken scan cannot pass.
_MIN_READS = 35


def _tracked_py_files():
    out = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True)
    for rel in out.split():
        if rel.startswith("tests/") or "/tests/" in rel:
            continue
        yield rel


def _parents(tree):
    par = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def _is_str_expr(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_str_expr(node.left) or _is_str_expr(node.right)
    return False


def _text(node):
    """The statement's literal text; interpolations become {name}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
            for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    if isinstance(node, ast.FormattedValue):
        return "{" + ast.unparse(node.value) + "}"
    return "{" + ast.unparse(node) + "}"


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _directly_fenced(node):
    if _FENCE_NAME in _names(node):
        return True
    return _FENCE_TEXT in " ".join(_text(node).split())


def _enclosing_func(node, par):
    cur = par.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = par.get(cur)
    return None


def _fenced(node, func):
    if _directly_fenced(node):
        return True
    if func is None:
        return False
    # One level of indirection: an interpolated variable assigned a fence.
    interpolated = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.FormattedValue) and isinstance(sub.value, ast.Name):
            interpolated.add(sub.value.id)
    for sub in ast.walk(func):
        if isinstance(sub, (ast.Assign, ast.AugAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            tnames = {t.id for t in targets if isinstance(t, ast.Name)}
            if tnames & interpolated and _is_str_expr(sub.value) and _directly_fenced(sub.value):
                return True
    return False


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                out.add(body[0].value)
    return out


def scan_reads():
    """[(file, function-or-None, lineno, fenced)] for every SQL read."""
    reads = []
    for rel in _tracked_py_files():
        path = os.path.join(ROOT, rel)
        try:
            src = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        if "ai_testimonials" not in src:
            continue
        tree = ast.parse(src, filename=rel)
        par = _parents(tree)
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not _is_str_expr(node):
                continue
            p = par.get(node)
            # Only the maximal string expression is a statement.
            if isinstance(p, (ast.JoinedStr, ast.FormattedValue)) or (
                    isinstance(p, ast.BinOp) and isinstance(p.op, ast.Add) and _is_str_expr(p)):
                continue
            # Bare string statements (docstrings, comment blocks) run nothing.
            if node in docs or isinstance(p, ast.Expr):
                continue
            if not _READ_RE.search(_text(node)):
                continue
            func = _enclosing_func(node, par)
            reads.append((rel, func.name if func else None, node.lineno,
                          _fenced(node, func)))
    return reads


def _allow_key(rel, fname):
    if (rel, fname) in _ALLOW:
        return (rel, fname)
    if (rel, None) in _ALLOW:
        return (rel, None)
    return None


def test_scan_finds_the_readers():
    reads = scan_reads()
    assert len(reads) >= _MIN_READS, reads
    # Known fenced public readers must be seen AS fenced, so a scan that
    # stopped recognising the fence fails here and not silently elsewhere.
    fenced = {(r, f) for r, f, _, ok in reads if ok}
    for key in [("main.py", "get_testimonials"),
                ("main.py", "testimonial_stats"),
                ("routes/dchub_media_hub.py", "testimonials_live"),
                ("dchub_media.py", "aggregate_announcements_v3")]:
        assert key in fenced, (key, sorted(fenced))


def test_every_ai_testimonials_read_is_fenced_or_allowlisted():
    bad = [f"{rel}:{line} in {fname or '<module>'}"
           for rel, fname, line, ok in scan_reads()
           if not ok and _allow_key(rel, fname) is None]
    assert not bad, (
        "These read FROM ai_testimonials without excluding human customer "
        "quotes. Add `AND {NOT_CLAIM_QUOTE_SQL}` (util/testimonial_sources.py) "
        "if the rows are presented or counted as AI quotes, citations or "
        "platforms; otherwise add the function to _ALLOW with a reason:\n  "
        + "\n  ".join(bad))


def test_allowlist_has_no_stale_entries():
    used = {_allow_key(rel, fname) for rel, fname, _, ok in scan_reads()}
    stale = [k for k in _ALLOW if k not in used]
    assert not stale, f"allowlist entries that match no read any more: {stale}"


def test_fence_text_is_null_safe_and_percent_free():
    flat = " ".join(NOT_CLAIM_QUOTE_SQL.split())
    assert flat == "COALESCE(source, '') <> 'claim_quote'"
    assert "%" not in NOT_CLAIM_QUOTE_SQL


def test_north_star_velocity_excludes_claim_quote():
    """routes/media_north_star builds `FROM {table}` at runtime."""
    mns = pytest.importorskip("routes.media_north_star")
    cols = {"quote", "source", "approved", "approved_at", "created_at",
            "platform", "agent_name", "url"}
    sql = mns._build_table_select(cols, "ai_testimonials",
                                  ("approved_at", "created_at"),
                                  approval_filter=True)
    assert NOT_CLAIM_QUOTE_SQL in sql, sql
