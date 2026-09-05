"""r-inquiry-schema-fork (2026-09-05) — one table, two incompatible CREATE
TABLE statements, and a lead-capture endpoint that reported success anyway.

THE BUG. routes/enterprise.py and routes/enterprise_inquiry.py landed in the
same commit on 2026-06-30, each carrying its own

    CREATE TABLE IF NOT EXISTS enterprise_inquiries (...)

for the SAME table, with disjoint column sets:

    enterprise.py          org_name NOT NULL, expected_volume NOT NULL,
                           source_ip, user_agent, relay_status
                           — and no `status` column at all
    enterprise_inquiry.py  tier_requested, name, firm, notes, source,
                           ip_hash, status NOT NULL, contacted_at, notes_admin
                           — and no org_name / expected_volume

They overlap on `email` and `use_case` and nothing else. IF NOT EXISTS means
whichever endpoint took the first POST created the table and the other's DDL
became a silent no-op whose INSERT names columns that do not exist. Both are
live lead capture on a revenue surface, so one of them has been failing to
store since June — and which one depended on June traffic order, not on code.

★ AND THE FAILURE WAS INVISIBLE. submit_inquiry caught the insert exception,
logged it, swallowed a failing admin email too, and returned
{"ok": true} 201 "We'll be in touch within 24h." A submitter was told they had
been heard while the lead went nowhere. Same shape as the PDF report silently
dropping a market: not a missing answer, a wrong one.

WHAT IS GUARDED, and why these are executed rather than grepped:
  * test_every_inserted_column_is_declared parses the INSERT column lists out
    of the AST and checks them against the canonical DDL's columns. THIS is
    the invariant that was violated; a source-level "both files import the
    shared module" check would pass on an INSERT naming a column nobody
    declares.
  * test_only_one_module_defines_the_table pins the fork itself closed.
"""
import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

from util.enterprise_inquiries_schema import (  # noqa: E402
    HEAL_SQL, SCHEMA_SQL, declared_columns)

WRITERS = ["routes/enterprise.py", "routes/enterprise_inquiry.py"]


def _src(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def _inserted_columns(sql_text):
    """Column lists from every `INSERT INTO enterprise_inquiries (...)`."""
    out = []
    for m in re.finditer(
            r"INSERT\s+INTO\s+enterprise_inquiries\s*\(([^)]*)\)",
            sql_text, re.I | re.S):
        cols = [c.strip() for c in m.group(1).split(",")]
        out.append({c for c in cols if re.fullmatch(r"[a-z_][a-z0-9_]*", c)})
    return out


def _sql_strings(rel):
    """Every string constant in the module — INSERTs live in plain literals."""
    tree = ast.parse(_src(rel))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_the_canonical_schema_is_not_empty():
    """Every assertion below compares against declared_columns(); if the
    parser returned nothing they would all pass vacuously."""
    cols = declared_columns()
    assert len(cols) >= 15, f"only parsed {len(cols)} columns: {sorted(cols)}"
    # Both historical definitions must be represented, or the union is not one.
    assert {"org_name", "expected_volume", "relay_status"} <= cols
    assert {"tier_requested", "firm", "ip_hash", "status"} <= cols


def test_every_inserted_column_is_declared():
    """★ THE INVARIANT THAT WAS VIOLATED. Either writer naming a column the
    canonical table does not declare is exactly the June bug."""
    declared = declared_columns()
    found_any = False
    for rel in WRITERS:
        for cols in _inserted_columns("\n".join(_sql_strings(rel))):
            found_any = True
            missing = cols - declared
            assert not missing, (
                f"{rel} INSERTs column(s) {sorted(missing)} that "
                f"enterprise_inquiries does not declare — this is the fork")
    assert found_any, "no INSERT found in either writer; the parse is wrong"


def test_the_two_writers_insert_genuinely_different_columns():
    """Guards the guard. If both writers used identical column sets the test
    above would hold trivially and prove nothing about a union schema."""
    sets = []
    for rel in WRITERS:
        for cols in _inserted_columns("\n".join(_sql_strings(rel))):
            sets.append(cols)
    assert len(sets) >= 2
    assert sets[0] != sets[1], (
        "the two writers now insert the same columns — this file's premise "
        "is gone and it should be rewritten, not left passing")


def test_only_one_module_defines_the_table():
    """The fork, pinned closed. A second CREATE TABLE IF NOT EXISTS for this
    name silently no-ops against whichever ran first.

    Read out of the AST's STRING CONSTANTS, not the raw file text. The
    modules that used to own a definition now carry a comment explaining why
    they no longer do — and that comment necessarily quotes the statement it
    is describing. A text scan counts the explanation as a second definition,
    which would have made this guard unfixable-by-construction: the only way
    to pass would be to delete the note that says why the rule exists."""
    hits = []
    scanned = 0
    for path in sorted(REPO.rglob("*.py")):
        rel = str(path.relative_to(REPO))
        if rel.startswith("tests/") or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        scanned += 1
        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and re.search(
                        r"CREATE TABLE IF NOT EXISTS\s+enterprise_inquiries",
                        n.value, re.I)):
                hits.append(rel)
                break
    # Coverage floor: a glob that stopped matching would make `hits` empty and
    # this guard vacuously green.
    assert scanned > 200, f"only scanned {scanned} modules — the walk is broken"
    assert hits == ["util/enterprise_inquiries_schema.py"], (
        f"enterprise_inquiries is defined in {hits} — two IF NOT EXISTS "
        "definitions of one table means the loser is a silent no-op")


def test_both_writers_build_the_table_through_the_shared_module():
    for rel in WRITERS:
        assert "ensure_enterprise_inquiries" in _src(rel), (
            f"{rel} no longer creates the table through the shared schema")


def test_the_heal_adds_every_declared_column():
    """A column in the canonical DDL but not in HEAL_SQL is a column an
    ALREADY-EXISTING table never gets — the exact silent gap, one level up."""
    healed = set(re.findall(
        r"ADD COLUMN IF NOT EXISTS\s+([a-z_][a-z0-9_]*)", HEAL_SQL))
    # id/created_at come from the CREATE and are never added to a live table.
    expected = declared_columns() - {"id", "created_at"}
    assert expected <= healed, (
        f"HEAL_SQL never adds {sorted(expected - healed)} — a table created "
        "by the other historical DDL stays broken for those columns")


def test_the_heal_drops_not_null_on_columns_the_other_writer_omits():
    """A surviving `org_name TEXT NOT NULL` rejects every
    enterprise_inquiry.py insert even after the column exists, so adding the
    columns alone does not heal the table."""
    dropped = set(re.findall(
        r"ALTER COLUMN\s+([a-z_][a-z0-9_]*)\s+DROP NOT NULL", HEAL_SQL))
    assert {"org_name", "expected_volume"} <= dropped


def test_status_is_not_promoted_to_not_null():
    """Rows inherited from the enterprise.py-shaped table have no status, so
    SET NOT NULL would fail the whole heal. Readers tolerate it via
    util.status_taxonomy.status_histogram instead."""
    assert "SET NOT NULL" not in HEAL_SQL
    assert re.search(r"status\s+TEXT\s+DEFAULT", SCHEMA_SQL), (
        "status lost its default; inserts that omit it would write NULL")
    assert "UPDATE enterprise_inquiries SET status = 'new' WHERE status IS NULL" in HEAL_SQL


def test_the_heal_never_drops_a_column():
    """Both column sets carry real submissions."""
    assert "DROP COLUMN" not in HEAL_SQL.upper()


# ---------------------------------------------------------------------------
# The response contract.
# ---------------------------------------------------------------------------
def test_a_lost_lead_is_not_reported_as_success():
    """★ submit_inquiry returned 201 {"ok": true} with "We'll be in touch"
    even when BOTH the insert and the admin email raised."""
    src = _src("routes/enterprise_inquiry.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "submit_inquiry")
    seg = ast.get_source_segment(src, fn)
    assert "if new_id is None and not _notified:" in seg, (
        "submit_inquiry no longer checks whether BOTH capture paths failed — "
        "a lost lead is reported to the submitter as success again")
    assert "storage_failed" in seg and "503" in seg
    # And the success payload must say which path actually worked.
    assert '"stored":' in seg and '"notified":' in seg, (
        "the 201 no longer distinguishes a stored lead from an emailed one")


def test_the_heal_runs_once_per_process_not_once_per_request():
    """Both callers invoke ensure_enterprise_inquiries from a REQUEST path.
    ALTER ... DROP NOT NULL takes an ACCESS EXCLUSIVE lock and the status
    backfill scans the table, so re-running the heal per POST would put a
    lock and a scan in front of every lead submission."""
    import util.enterprise_inquiries_schema as mod

    class FakeCur:
        def __init__(self): self.n = 0
        def execute(self, sql): self.n += 1

    prev = mod._ENSURED
    try:
        mod._ENSURED = False
        cur = FakeCur()
        assert mod.ensure_enterprise_inquiries(cur) is True
        first = cur.n
        assert first == 3, f"expected CREATE+HEAL+INDEX, got {first} statements"
        assert mod.ensure_enterprise_inquiries(cur) is False
        assert cur.n == first, "the heal re-ran on the second call"
        # force= is the escape hatch, and it must actually work.
        assert mod.ensure_enterprise_inquiries(cur, force=True) is True
        assert cur.n == first * 2
    finally:
        mod._ENSURED = prev
