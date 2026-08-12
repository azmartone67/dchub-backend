"""Guard SH52-056: the upstream `UNKNOWN<id>` placeholder must never win.

WHY
───
The 2026-07-31 canary against production inserted 670 rows, and 668 of them
were EXACT coordinate twins of a row we already held — under a WORSE name:

    held 'HOLCOMBE'                    <-> upstream 'UNKNOWN107657'
    held 'South Bainbridge Substation' <-> upstream 'UNKNOWN107666'
    held 'CRIST'                       <-> upstream 'UNKNOWN107698'

That was read as "this upstream is a different vintage, writes are unsafe" and
the layer has been frozen at 2026-03-17 ever since. The real cause is narrower
and fixable: the ingest matched on (name, lat, lng), and `name` was compared
against a placeholder that upstream builds from the row's own ID.

Measured against the live FeatureServer 2026-08-12 by paging all 75,328
records: 38,479 names — 51.1% — are `UNKNOWN` + that row's ID. A name derived
from the id carries no independent information. It cannot identify a
substation and it cannot improve one we have already named.

CONTRACT
────────
  P1. _clean_name maps `UNKNOWN<digits>` to None, in any case, while leaving a
      real name (including one that merely CONTAINS the word unknown, and one
      that is legitimately numeric-suffixed) intact.
  P2. The row builder uses _clean_name for NAME — not the generic _clean, which
      only rejects the bare string 'UNKNOWN' and passes 'UNKNOWN107655'.
  P3. The UPSERT never lets a NULL/placeholder name overwrite a held one.
  P4. Identity does not come from `name` at all any more.

Run: python3 -m pytest tests/test_substation_placeholder_name_identity.py -v
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "substation_ingest.py")


def _src():
    s = open(MOD).read()
    assert s.strip(), "module source is EMPTY — an empty read must not pass"
    return s


def _tree():
    t = ast.parse(_src())
    assert t.body, "parsed module body is EMPTY"
    return t


def _clean_name():
    """Exec the real _clean_name (and the _clean it delegates to)."""
    tree = _tree()
    wanted = {"_clean", "_clean_name"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {f.name for f in fns} == wanted, (
        f"expected {wanted} at module level, found {[f.name for f in fns]}")
    pat = next((n for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_PLACEHOLDER_NAME" for t in n.targets)), None)
    assert pat is not None, "_PLACEHOLDER_NAME regex not found"
    ns = {"re": re}
    exec(compile(ast.Module(body=[pat] + fns, type_ignores=[]), MOD, "exec"), ns)
    return ns["_clean_name"]


# ── P1 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "UNKNOWN107655", "UNKNOWN107657", "unknown107666", "Unknown110134",
    "UNKNOWN0", "UNKNOWN", "  UNKNOWN107698  ",
])
def test_p1_placeholder_names_become_none(raw):
    assert _clean_name()(raw) is None, (
        f"{raw!r} survived as a name. 51.1% of upstream names have this shape; "
        "letting them through is what inserted 670 duplicates on 2026-07-31.")


@pytest.mark.parametrize("raw,expected", [
    ("HOLCOMBE", "HOLCOMBE"),
    ("South Bainbridge Substation", "South Bainbridge Substation"),
    ("CRIST", "CRIST"),
    # Must NOT over-match: these are real names, not placeholders.
    ("UNKNOWN CREEK", "UNKNOWN CREEK"),
    ("UNKNOWN RIVER 230", "UNKNOWN RIVER 230"),
    ("SUBSTATION 12", "SUBSTATION 12"),
    ("TAP161924", "TAP161924"),
])
def test_p1b_real_names_survive(raw, expected):
    assert _clean_name()(raw) == expected, (
        f"{raw!r} was destroyed — the placeholder pattern is over-matching and "
        "would blank real substation names")


# ── P2 ───────────────────────────────────────────────────────────────────────
def test_p2_row_builder_uses_clean_name_for_the_name_field():
    fn = next((n for n in ast.walk(_tree())
               if isinstance(n, ast.FunctionDef) and n.name == "_row_from_attrs"), None)
    assert fn is not None and fn.body, "_row_from_attrs missing or empty"
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and any(isinstance(a, ast.Call) and getattr(a.func, "attr", "") == "get"
                     and a.args and getattr(a.args[0], "value", None) == "NAME"
                     for a in n.args)]
    assert calls, "no call wrapping a.get('NAME') found in _row_from_attrs"
    assert {c.func.id for c in calls} == {"_clean_name"}, (
        f"NAME goes through {[c.func.id for c in calls]} — the generic _clean "
        "rejects only the bare string 'UNKNOWN' and passes 'UNKNOWN107655' "
        "straight through, which is the exact bug this closes")


# ── P3 / P4 ──────────────────────────────────────────────────────────────────
def test_p3_upsert_coalesces_name_and_never_keys_on_it():
    src = _src()
    m = re.search(r"_UPSERT_SQL\s*=\s*\"\"\"(.*?)\"\"\"", src, re.S)
    assert m, "_UPSERT_SQL not found"
    sql = m.group(1)
    assert re.search(r"(?i)name\s*=\s*COALESCE\s*\(\s*EXCLUDED\.name\s*,\s*substations\.name\s*\)", sql), (
        "name is not COALESCE'd — a matched row's validated name can be "
        "replaced by NULL (a rejected placeholder), losing ~38,000 names")
    target = re.search(r"(?i)ON CONFLICT\s*\(([^)]*)\)", sql)
    assert target, "no ON CONFLICT target"
    assert "name" not in target.group(1).lower(), (
        "`name` is back in the identity key. It is 51.1% placeholder upstream "
        "and float4-quantized alongside lat/lng — it cannot identify anything.")


def test_p4_writes_stay_blocked_until_the_link_backfill_runs():
    """Identity being resolved is NOT permission to run the bulk load."""
    src = _src()
    assert "writes disabled pending the hifld_id link backfill" in src, (
        "the write path opened. 78,356 of 79,686 held rows still have "
        "hifld_id NULL, so every upstream record reads as new and a full run "
        "inserts ~75,000 duplicate substations.")
    assert "409" in src[src.index("writes disabled pending the hifld_id"):
                        src.index("writes disabled pending the hifld_id") + 1500]


def test_p4b_the_reconcile_report_measures_and_never_writes():
    fn = next((n for n in ast.walk(_tree())
               if isinstance(n, ast.FunctionDef) and n.name == "_reconcile_report"), None)
    assert fn is not None and fn.body, "_reconcile_report missing or empty"
    # ★ Check the SQL this function actually EXECUTES, not the prose around it.
    # The first cut matched the substring "DELETE" and tripped on the comment
    # "reported, never deleted" — a guard that reads comments is testing the
    # documentation, and it fails for reasons that have nothing to do with the
    # behaviour it claims to protect.
    literals = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert literals, "no string literals in _reconcile_report — nothing to check"
    write = re.compile(r"(?is)\b(insert\s+into|update\s+\w+\s+set|delete\s+from|truncate)\b")
    offenders = [s for s in literals if write.search(s)]
    assert not offenders, (
        f"_reconcile_report executes a write: {offenders!r} — it must MEASURE "
        "only. A report that writes is not a report.")
    assert any(s.strip().upper().startswith("SELECT") for s in literals), (
        "_reconcile_report issues no SELECT — it does not read anything either, "
        "so this check would pass vacuously")
    # And it must never commit: the connection is read-only by intent.
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                and n.attr == "commit"], "_reconcile_report commits a transaction"


def test_p5_migration_does_not_reuse_an_index_name_bound_to_another_column():
    """The no-op that read as green.

    `CREATE UNIQUE INDEX IF NOT EXISTS substations_hifld_id_uniq ON
    substations (hifld_id) ...` SILENTLY DID NOTHING: an index of that NAME
    already exists on this table, defined ON hifld_objectid — the stale name
    left behind when that column was renamed. IF NOT EXISTS matches on name, so
    the statement succeeded, `SELECT indexname WHERE indexname=...` returned the
    OLD index, and the check passed. It surfaced only when the real ON CONFLICT
    was executed against production and raised "no unique or exclusion
    constraint matching the ON CONFLICT specification".
    """
    path = os.path.join(ROOT, "migrations",
                        "2026-08-12_substation_hifld_id_identity.sql")
    assert os.path.exists(path), "identity migration missing"
    sql = open(path).read()
    low = sql.lower()

    # Names already bound to a DIFFERENT column on the live table.
    for taken in ("substations_hifld_id_uniq", "substations_hifld_objectid_uniq",
                  "idx_substations_hifld_oid", "substations_name_lat_lng_uniq"):
        assert f"index if not exists {taken}" not in low, (
            f"migration creates {taken!r}, a name already bound to another "
            "column — IF NOT EXISTS makes that a silent no-op")

    m = re.search(r"(?is)create\s+unique\s+index\s+if\s+not\s+exists\s+(\w+)\s+on\s+substations\s*\(\s*hifld_id\s*\)",
                  sql)
    assert m, "no CREATE UNIQUE INDEX ... ON substations (hifld_id)"
    assert m.group(1) == "substations_hifld_asset_id_uniq", (
        f"unexpected index name {m.group(1)!r}")
    assert re.search(r"(?is)on\s+substations\s*\(\s*hifld_id\s*\)\s*where\s+hifld_id\s+is\s+not\s+null", sql), (
        "the index must be PARTIAL — 126k rows have no upstream id, and the "
        "ON CONFLICT in substation_ingest.py repeats this predicate to infer it")

    # And the migration must verify itself by DEFINITION, not by name.
    assert "indexdef" in low and "raise exception" in low, (
        "the migration does not fail loudly when the CREATE is a no-op — "
        "verifying an index by its name is what hid this for a full cycle")

    for forbidden in ("drop column", "delete from substations", "truncate",
                      "drop index", "alter column"):
        assert forbidden not in low, f"migration must not {forbidden!r}"
    # It may only seed hifld_id — never rewrite a published identity.
    for upd in re.findall(r"(?is)update\s+substations\s+set\s+(\w+)", sql):
        assert upd == "hifld_id", (
            f"migration UPDATEs substations.{upd} — only the new hifld_id "
            "column may be written; names, slugs and hifld_objectid are frozen")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="control: proves this file actually runs")
def test_zzz_must_fail_control():
    assert False, "control"
