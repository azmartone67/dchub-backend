"""tests/test_restore_verify_dump_race.py — the DR gate must not call a
post-dump table "data loss" (2026-08-31).

THE INCIDENT. The weekly restore test went red for the first time in six weeks:

    [X] SIGNIFICANT source table absent from restore: gsc_daily_performance (~55071 rows)

Nothing was lost. `restore_verify.py` compared the restored dump against LIVE
prod, and those are two different points in time:

    dump began     2026-08-31 09:41:50Z   (neon_backup_20260831_094150.sql.gz)
    gsc rows land  2026-08-31 10:15:53Z .. 10:18:38Z   (all 55,071 of them)
    verify ran     2026-08-31 11:27:00Z

The table did not exist when the dump was taken, so a CORRECT dump could not
contain it — and the gate failed the backup for it. A DR gate that cries wolf is
one nobody reads, which is precisely how a genuinely unrestorable backup gets
waved through.

The fix: the dump ships an inventory of what existed when it ran, and the
verifier compares against THAT. These tests pin both halves — the race is gone
AND the gate still bites on real loss.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_restore_verify_dump_race.py -v
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "restore_verify.py"


def _mod():
    spec = importlib.util.spec_from_file_location("restore_verify", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _never_skip(_name):
    return False


# --------------------------------------------------------------------------
# The gate must still BITE. If these ever pass vacuously the rest is theatre.
# --------------------------------------------------------------------------

def test_a_significant_table_truly_missing_still_fails():
    """★The whole point of the gate. A big table in the dump's own inventory
    that did not come back IS data loss, and must land in sig_missing."""
    m = _mod()
    src = {"users": 50_000, "deals": 2_000}
    expected, minor, sig = m.classify_missing(src, restored={"deals"}, skip=_never_skip)
    assert sig == ["users"], "a 50k-row table absent from the restore must be a real problem"
    assert minor == [] and expected == []


def test_a_small_missing_table_is_a_warning_not_a_failure():
    m = _mod()
    src = {"tiny_lookup": 12}
    expected, minor, sig = m.classify_missing(src, restored=set(), skip=_never_skip)
    assert sig == [] and minor == ["tiny_lookup"]


def test_the_significance_floor_is_the_documented_one():
    """A table exactly AT the floor counts; one row under does not."""
    m = _mod()
    floor = m.SIGNIFICANT_ROWS
    _, _, sig_at = m.classify_missing({"t": floor}, set(), _never_skip)
    _, minor_under, sig_under = m.classify_missing({"t": floor - 1}, set(), _never_skip)
    assert sig_at == ["t"], "a table at the floor must fail the gate"
    assert sig_under == [] and minor_under == ["t"]


def test_extension_typed_tables_are_excused_regardless_of_size():
    """Unchanged behaviour: a vanilla container cannot create geometry/vector
    columns, so those tables are expected-absent, never loss."""
    m = _mod()
    src = {"fcc_fiber_hex": 900_000}
    expected, minor, sig = m.classify_missing(
        src, restored=set(), skip=lambda t: t == "fcc_fiber_hex")
    assert expected == ["fcc_fiber_hex"] and sig == [] and minor == []


# --------------------------------------------------------------------------
# ★THE REGRESSION — the 2026-08-31 false RED.
# --------------------------------------------------------------------------

def test_a_table_created_after_the_dump_is_not_data_loss():
    """★REGRESSION. Compared against the DUMP's inventory, a table that appeared
    in prod afterwards is not 'missing' — it is simply not part of this dump.

    Uses the real incident's numbers."""
    m = _mod()
    manifest_tables = {"users": 50_000, "deals": 2_000}      # what existed at 09:41:50
    restored = {"users", "deals"}                            # the dump restored cleanly
    expected, minor, sig = m.classify_missing(manifest_tables, restored, _never_skip)
    assert sig == [], "a clean restore of everything the dump held must not fail"
    assert minor == [] and expected == []


def test_the_live_compare_is_what_produced_the_false_red():
    """Pins the defect itself, so a revert to the live-prod basis is caught.

    Same restore, but compared against prod AS READ LATER — which is what the
    gate used to do — and gsc_daily_performance is reported as real loss."""
    m = _mod()
    live_at_1127 = {"users": 50_000, "deals": 2_000, "gsc_daily_performance": 55_071}
    restored = {"users", "deals"}
    _, _, sig = m.classify_missing(live_at_1127, restored, _never_skip)
    assert sig == ["gsc_daily_performance"], (
        "this test documents the OLD basis; if it stops reporting the false "
        "positive, classify_missing's contract changed and the manifest wiring "
        "in main() needs rechecking")


# --------------------------------------------------------------------------
# load_manifest: it must refuse to excuse everything.
# --------------------------------------------------------------------------

def test_missing_manifest_returns_none_so_the_caller_falls_back(tmp_path):
    m = _mod()
    assert m.load_manifest(str(tmp_path / "nope.json")) is None


def test_an_empty_inventory_is_refused(tmp_path):
    """★A manifest claiming zero tables would mark EVERY missing table as
    'created after the dump' and silently pass a totally failed restore."""
    m = _mod()
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"taken_at": "2026-08-31T09:41:50+00:00", "tables": {}}))
    assert m.load_manifest(str(p)) is None, \
        "an empty inventory must be refused, not trusted"


def test_a_malformed_manifest_is_refused(tmp_path):
    m = _mod()
    p = tmp_path / "m.json"
    p.write_text("{not json")
    assert m.load_manifest(str(p)) is None
    p.write_text(json.dumps({"tables": ["users"]}))   # list, not a dict
    assert m.load_manifest(str(p)) is None


def test_a_good_manifest_is_read(tmp_path):
    m = _mod()
    p = tmp_path / "m.json"
    p.write_text(json.dumps({
        "taken_at": "2026-08-31T09:41:50+00:00",
        "dump_key": "neon_backup_20260831_094150.sql.gz",
        "tables": {"users": 50_000},
    }))
    got = m.load_manifest(str(p))
    assert got["taken_at"] == "2026-08-31T09:41:50+00:00"
    assert got["tables"] == {"users": 50_000}


# --------------------------------------------------------------------------
# The manifest the backup WRITES. A wrong inventory is worse than none: it
# would excuse genuinely missing tables as "created after the dump".
# --------------------------------------------------------------------------

def _backup():
    spec = importlib.util.spec_from_file_location(
        "backup_neon_to_r2", _ROOT / "backup_neon_to_r2.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_manifest_rows_parse_to_names_and_counts():
    b = _backup()
    assert b.parse_manifest_rows("users|50000\ndeals|2000\n") == {
        "users": 50_000, "deals": 2_000}


def test_rows_without_a_usable_count_are_dropped_not_guessed():
    """★A row we cannot read must not enter the inventory at all — a table
    recorded with a wrong count is a table the DR gate then mis-judges."""
    b = _backup()
    got = b.parse_manifest_rows("good|10\nbare_row\nbad|notanumber\n|12\n\n")
    assert got == {"good": 10}


def test_the_count_is_taken_from_the_last_field():
    """Split on the LAST separator so an odd name cannot shift the number."""
    b = _backup()
    assert b.parse_manifest_rows("weird|name|123\n") == {"weird|name": 123}


def test_empty_psql_output_yields_an_empty_inventory():
    b = _backup()
    assert b.parse_manifest_rows("") == {}
    assert b.parse_manifest_rows(None) == {}
