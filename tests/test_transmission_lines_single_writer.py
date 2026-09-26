"""Guard: transmission_lines has ONE row writer. 2026-09-23.

WHY
───
Two writers owned the table and undid each other every week:

  · routes/transmission_ingest.py — weekly EIA full-replace (94,619 lines,
    dataLastEditDate 2025-08-26). Its DELETE clears source IN ('hifld', ...).
  · land_power_crawler.crawl_transmission_lines — nightly upsert of the HIFLD
    services5 layer (89,744 lines, FROZEN at 2021-02-25), ON CONFLICT (hifld_id).

Every Monday the replace deleted the crawler's 934 HIFLD-only rows; the next
crawl that survived a deploy re-added them. infra_growth_snapshot read 95,569
most days and 94,635 on 09-07, 09-14 and 09-21, and /whats-new published
"+7d -934". The 934 are superseded 2021 records — 907 of them lie within 30 m
of EIA lines carrying other ids — so the EIA set is the correct state.

Two more writers put rows in the table that were not transmission lines:
autonomous_brain's news extractor (16 headlines stored as 'operational' lines)
and /api/jobs/transmission-refresh, which TRUNCATEd the table and reloaded a
superseded 52,244-row layer keyed by OBJECTID.

WHAT THIS PINS
──────────────
No module but routes/transmission_ingest.py holds a SQL string that adds or
removes transmission_lines rows (INSERT / DELETE / TRUNCATE / COPY / MERGE).
UPDATEs of derived columns (fix_transmission_state.py) do not change which
lines exist and are out of scope.

It reads string CONSTANTS from the AST, docstrings excluded — so a comment or
docstring that quotes the old INSERT (this file's included) cannot trip it, and
an f-string's literal head ("INSERT INTO transmission_lines (") still counts.
Blind spot, stated: a table name held in a variable and interpolated
(f"INSERT INTO {t}") is not seen.
"""
import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
OWNER = "routes/transmission_ingest.py"

# Row-population verbs followed by the bare table name. The negative lookahead
# keeps transmission_lines_eia out; requiring whitespace before the name keeps
# osm_/discovered_transmission_lines out.
_WRITE = re.compile(
    r"\b(INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|COPY|MERGE\s+INTO)"
    r"\s+(?:ONLY\s+)?(?:\"?public\"?\s*\.\s*)?\"?transmission_lines\"?(?![A-Za-z0-9_])",
    re.IGNORECASE)

# Relative path parts never scanned. Matched on the path RELATIVE to the repo,
# so a checkout that itself lives under .claude/worktrees still scans.
_SKIP_PARTS = {".git", "tests", "node_modules", "venv", ".venv", "__pycache__"}


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def _writes_in(text):
    """Verbs (upper-cased, first word) of every transmission_lines row write."""
    return [m.group(1).split()[0].upper() for m in _WRITE.finditer(text)]


def _scan():
    """{relpath: [verbs]} for every module holding a row write, plus the count
    of modules scanned."""
    hits, scanned = {}, 0
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if _SKIP_PARTS.intersection(rel.parts):
            continue
        scanned += 1
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            # Unparseable here is not unrunnable elsewhere: read the raw text.
            verbs = _writes_in(src)
        else:
            skip = _docstring_ids(tree)
            verbs = [v for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and id(n) not in skip
                     for v in _writes_in(n.value)]
        if verbs:
            hits[rel.as_posix()] = verbs
    return hits, scanned


@pytest.fixture(scope="module")
def scan():
    return _scan()


def test_the_scan_read_the_repo(scan):
    """Floor: a scan that read nothing would pass the next test vacuously."""
    _, scanned = scan
    assert scanned >= 1000, f"only {scanned} modules scanned — the walk is broken"


def test_the_owner_is_seen_writing(scan):
    """Positive control: the one real writer must be FOUND, with the DELETE and
    the INSERT of its full-replace. If this fails the matcher is broken, not
    the table."""
    hits, _ = scan
    verbs = set(hits.get(OWNER, ()))
    assert {"DELETE", "INSERT"} <= verbs, (
        f"{OWNER} shows {sorted(verbs) or 'no'} transmission_lines writes — the "
        f"scan cannot see the writer it exists to protect")


def test_transmission_lines_has_one_row_writer(scan):
    hits, _ = scan
    others = {p: v for p, v in hits.items() if p != OWNER}
    assert not others, (
        "transmission_lines is owned by the weekly EIA full-replace in "
        f"{OWNER}. A second writer re-creates the weekly 934-row flap (or "
        f"worse, a TRUNCATE onto a stale layer). Offenders: {others}")


@pytest.mark.parametrize("sql,verbs", [
    ("INSERT INTO transmission_lines (hifld_id) VALUES (%s) ON CONFLICT DO NOTHING", ["INSERT"]),
    ("  insert into\n  transmission_lines\n (a)", ["INSERT"]),
    ("DELETE FROM transmission_lines WHERE source IN %s", ["DELETE"]),
    ("TRUNCATE TABLE transmission_lines RESTART IDENTITY", ["TRUNCATE"]),
    ("TRUNCATE transmission_lines", ["TRUNCATE"]),
    ("COPY transmission_lines FROM STDIN", ["COPY"]),
    ('INSERT INTO public."transmission_lines" (a)', ["INSERT"]),
    ("INSERT INTO transmission_lines_eia (a)", []),
    ("INSERT INTO osm_transmission_lines (a)", []),
    ("DELETE FROM discovered_transmission_lines", []),
    ("UPDATE transmission_lines SET state=%s", []),
    ("SELECT COUNT(*) FROM transmission_lines", []),
], ids=lambda x: x if isinstance(x, str) else None)
def test_the_matcher(sql, verbs):
    assert _writes_in(sql) == verbs


def test_docstrings_are_not_writes(tmp_path):
    """A docstring that quotes the old INSERT is prose, not a writer; the same
    text as a live string is one."""
    doc = '"""INSERT INTO transmission_lines (a) VALUES (1) ON CONFLICT DO NOTHING"""\n'
    live = 'SQL = "INSERT INTO transmission_lines (a) VALUES (1) ON CONFLICT DO NOTHING"\n'
    for src, want in ((doc, []), (doc + live, ["INSERT"])):
        tree = ast.parse(src)
        skip = _docstring_ids(tree)
        got = [v for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in skip for v in _writes_in(n.value)]
        assert got == want, (src, got)
